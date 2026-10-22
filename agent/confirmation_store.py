import json
import uuid
import logging
import hashlib
from core.logging import log_extra

from db.connection import get_connection
from agent.conversation_scope import current_scope

from db.repository import (
    create_agent_pending_action,
    get_agent_pending_action_by_confirmation_id,
    update_agent_pending_action_status,
    confirm_agent_pending_action,
    finish_agent_pending_action,
    finish_agent_task_for_action,
    claim_agent_pending_action_execution,
    expire_agent_pending_actions,
    expire_agent_pending_action_if_due,
    clear_expired_current_confirmations,
    lock_agent_task_execution_owner,
)
from services.task_service import TaskExecutionConflict

logger = logging.getLogger("agent.confirmation_store")


def _build_action(row):

    if row is None:
        return None


    preview = row["preview_json"]

    if isinstance(preview, str):
        preview = json.loads(
            preview
        )


    execution_result = row[
        "execution_result_json"
    ]

    if isinstance(
        execution_result,
        str
    ):
        execution_result = json.loads(
            execution_result
        )


    return {

        "id": row["id"],

        "confirmation_id": (
            row["confirmation_id"]
        ),

        "user_id": row["user_id"],

        "conversation_id": row.get("conversation_id"),

        "task_id": row.get("task_id"),

        "action_type": row["action_type"],

        "preview": preview,

        "status": row["status"],

        "execution_result": execution_result,

        "created_at": row["created_at"],

        "updated_at": row["updated_at"],

        "confirmed_at": row["confirmed_at"],

        "executed_at": row["executed_at"]

    }



def create_pending_action(
    user_id: int,
    action_type: str,
    preview: dict,
    trace_id: str | None = None,
    task_id: int | None = None
):

    """
    创建待确认操作并持久化到 MySQL。
    """


    logger.info("creating pending action", extra=log_extra(
        trace_id=trace_id, task_id=task_id
    ))


    confirmation_id = (
        "CONF_"
        + uuid.uuid4().hex[:16].upper()
    )


    preview_json = json.dumps(
        preview,
        ensure_ascii=False,
        default=str
    )


    scope = current_scope()
    if scope is not None and scope.user_id != user_id:
        raise ValueError("Pending action user does not match conversation scope")
    conversation_id = scope.conversation_id if scope is not None else None
    if task_id is None and scope is not None:
        task_id = scope.task_id

    connection = get_connection()


    try:

        cursor = connection.cursor()

        if task_id is not None and scope is not None and scope.execution_id is not None:
            if not lock_agent_task_execution_owner(
                cursor,
                task_id,
                user_id,
                conversation_id,
                scope.execution_id,
            ):
                raise TaskExecutionConflict(
                    "Task execution ownership was lost before Preview creation"
                )


        create_agent_pending_action(

            cursor=cursor,

            confirmation_id=confirmation_id,

            user_id=user_id,

            action_type=action_type,

            preview_json=preview_json,

            task_id=task_id,
            conversation_id=conversation_id,

        )


        connection.commit()



        row = (
            get_agent_pending_action_by_confirmation_id(

                cursor=cursor,

                confirmation_id=confirmation_id

            )
        )


        return _build_action(
            row
        )


    except Exception:

        connection.rollback()

        raise


    finally:

        connection.close()



def get_pending_action(
    confirmation_id: str,
    current_user_id: int | None = None,
    conversation_id: int | None = None,
):

    if conversation_id is not None and current_user_id is None:
        raise ValueError("Conversation-scoped action lookup requires user ID")

    connection = get_connection()


    try:

        cursor = connection.cursor()


        row = (

            get_agent_pending_action_by_confirmation_id(

                cursor=cursor,

                confirmation_id=confirmation_id,

                user_id=current_user_id,
                conversation_id=conversation_id,

            )

        )


        return _build_action(
            row
        )


    finally:

        connection.close()



def update_pending_action_status(
    confirmation_id: str,
    new_status: str
):

    if new_status not in {"confirmed", "cancelled", "expired"}:
        raise ValueError("只允许 pending 转为 confirmed、cancelled 或 expired")

    connection = get_connection()


    try:

        cursor = connection.cursor()


        if new_status == "confirmed":

            affected_rows = (
                confirm_agent_pending_action(

                    cursor=cursor,

                    confirmation_id=confirmation_id

                )
            )


        else:

            affected_rows = (

                update_agent_pending_action_status(

                    cursor=cursor,

                    confirmation_id=confirmation_id,

                    new_status=new_status

                )

            )


        if affected_rows == 0:

            connection.rollback()

            return None



        connection.commit()



        row = (

            get_agent_pending_action_by_confirmation_id(

                cursor=cursor,

                confirmation_id=confirmation_id

            )

        )


        return _build_action(
            row
        )


    except Exception:

        connection.rollback()

        raise


    finally:

        connection.close()


def expire_pending_actions(max_age_days: int = 7) -> int:
    """将超过保留期的 pending 操作批量置为 expired，并清除旧确认指针。"""
    if max_age_days <= 0:
        raise ValueError("max_age_days 必须大于 0")
    connection = get_connection()
    try:
        cursor = connection.cursor()
        count = expire_agent_pending_actions(cursor, max_age_days)
        clear_expired_current_confirmations(cursor)
        connection.commit()
        return count
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def expire_pending_action_if_due(
    confirmation_id: str, current_user_id: int, max_age_days: int = 7
) -> bool:
    """确认前按资源 ID 检查时效，避免批处理间隔内确认过期操作。"""
    connection = get_connection()
    try:
        cursor = connection.cursor()
        changed = expire_agent_pending_action_if_due(
            cursor, confirmation_id, current_user_id, max_age_days
        )
        if changed:
            clear_expired_current_confirmations(cursor, confirmation_id)
        connection.commit()
        return changed == 1
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()



def update_pending_action_execution(
    confirmation_id: str,
    status: str,
    execution_result=None
):

    connection = get_connection()


    try:

        cursor = connection.cursor()

        # Match recovery's lock order: Task first, then Action. This lock is
        # held only while writing the two terminal states, never during I/O.
        cursor.execute('SELECT task_id FROM agent_pending_actions WHERE confirmation_id=%s', (confirmation_id,))
        origin = cursor.fetchone()
        if origin and origin.get('task_id') is not None:
            cursor.execute('SELECT id FROM agent_tasks WHERE id=%s FOR UPDATE', (origin['task_id'],))
            cursor.fetchone()


        if status not in {"executed", "failed"}:
            raise ValueError("执行中的状态只允许由原子领取操作设置")

        else:

            execution_result_json = json.dumps(

                execution_result,

                ensure_ascii=False,

                default=str

            )

            error_message = None
            if status == "failed":
                if isinstance(execution_result, dict):
                    error_message = (
                        execution_result.get("message")
                        or execution_result.get("error")
                        or execution_result.get("code")
                    )
                if error_message is None:
                    error_message = "写操作执行失败"


            affected_rows = (

                finish_agent_pending_action(

                    cursor=cursor,

                    confirmation_id=confirmation_id,

                    status=status,

                    execution_result_json=execution_result_json,

                    error_message=str(error_message) if error_message is not None else None

                )

            )



        if affected_rows == 0:

            connection.rollback()

            return None



        # 与 Action 终态写入共享事务，进程中断时一起提交或一起回滚。
        task_rows = finish_agent_task_for_action(cursor, confirmation_id, status)
        if task_rows != 1:
            raise RuntimeError("Task state coordination conflict")

        connection.commit()


        row = (

            get_agent_pending_action_by_confirmation_id(

                cursor=cursor,

                confirmation_id=confirmation_id

            )

        )


        return _build_action(
            row
        )


    except Exception:

        connection.rollback()

        raise


    finally:

        connection.close()



def claim_pending_action_execution(
    confirmation_id: str
) -> bool:


    connection = get_connection()

    cursor = connection.cursor()


    try:

        cursor.execute("SELECT task_id FROM agent_pending_actions WHERE confirmation_id=%s", (confirmation_id,))
        action_row = cursor.fetchone()
        if action_row is None or action_row.get('task_id') is None:
            connection.rollback()
            return False
        cursor.execute('SELECT id FROM agent_tasks WHERE id=%s FOR UPDATE', (action_row['task_id'],))
        cursor.fetchone()
        owner = 'CONFEXEC_' + hashlib.md5(confirmation_id.encode()).hexdigest()
        cursor.execute("""
            UPDATE agent_tasks AS t
            JOIN agent_pending_actions AS a ON a.task_id=t.id AND a.user_id=t.user_id
            SET t.execution_id=%s, t.execution_started_at=NOW(),
                t.execution_heartbeat_at=NOW(), t.status='running',
                t.current_step='confirm_execute', t.recovery_id=NULL,
                t.recovery_started_at=NULL, t.recovery_source_execution_id=NULL,
                t.recovery_status=NULL, t.recovery_reason=NULL
            WHERE a.confirmation_id=%s AND a.status='confirmed'
              AND t.execution_id IS NULL
              AND t.status IN ('waiting_confirm','waiting_input','running')
              AND NOT EXISTS (
                  SELECT 1 FROM agent_pending_actions AS newer
                  WHERE newer.task_id=a.task_id AND newer.id>a.id
              )
        """, (owner, confirmation_id))
        if cursor.rowcount != 1:
            connection.rollback()
            return False
        rowcount = claim_agent_pending_action_execution(

            cursor=cursor,

            confirmation_id=confirmation_id

        )

        if rowcount != 1:
            connection.rollback()
            return False
        connection.commit()


        if rowcount == 1:
            from services.task_service import register_agent_task_lease
            register_agent_task_lease(action_row['task_id'], owner)
        return rowcount == 1


    except Exception:

        connection.rollback()

        raise


    finally:

        cursor.close()

        connection.close()
