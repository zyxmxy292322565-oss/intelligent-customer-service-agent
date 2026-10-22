from db.connection import get_connection
from agent.conversation_scope import current_scope
import uuid
import logging
import threading
import time
import pymysql
from contextvars import ContextVar
from functools import wraps
from collections import OrderedDict
from core.settings import get_settings

import json


logger = logging.getLogger("services.task_service")
_lease_registry: OrderedDict[tuple[int, str], None] = OrderedDict()
_lease_registry_lock = threading.Lock()
_request_leases = ContextVar('request_task_leases', default=None)
_heartbeat_batch = ContextVar('heartbeat_batch_connection', default=None)


def task_execution_lifecycle(function):
    """Stop renewing when a request ends, including failed terminal writes."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        leases = set()
        token = _request_leases.set(leases)
        try:
            return function(*args, **kwargs)
        finally:
            for task_id, owner in leases:
                unregister_agent_task_lease(task_id, owner)
            _request_leases.reset(token)
    return wrapped


class TaskExecutionConflict(RuntimeError):
    """The caller no longer owns the database-backed Task execution."""


def new_agent_execution_id() -> str:
    return "EXEC_" + uuid.uuid4().hex.upper()


def register_agent_task_lease(task_id: int, execution_id: str) -> None:
    """Register an execution for the process-level heartbeat loop.

    The registry is only a source of renewal attempts.  MySQL remains the
    authority and every renewal is conditional on the database owner.
    """
    with _lease_registry_lock:
        _lease_registry.setdefault((task_id, execution_id), None)
    leases = _request_leases.get()
    if leases is not None:
        leases.add((task_id, execution_id))


def unregister_agent_task_lease(task_id: int, execution_id: str) -> None:
    with _lease_registry_lock:
        _lease_registry.pop((task_id, execution_id), None)


def renew_agent_task_lease(task_id: int, execution_id: str) -> bool:
    lease_seconds = get_settings().task_lease_seconds
    batch = _heartbeat_batch.get()
    if batch is not None:
        if time.monotonic() >= batch['deadline']:
            raise TimeoutError('Heartbeat batch budget exhausted')
        if batch['connection'] is None:
            batch['connection'] = get_connection()
        connection = batch['connection']
    else:
        connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                UPDATE agent_tasks
                SET execution_heartbeat_at=NOW()
                WHERE id=%s AND execution_id=%s
                  AND status='running' AND recovery_id IS NULL
                  AND execution_heartbeat_at > DATE_SUB(NOW(), INTERVAL %s SECOND)
            """, (task_id, execution_id, lease_seconds))
            renewed = cursor.rowcount == 1
            if not renewed:
                # DATETIME has second precision: a same-second heartbeat is a
                # valid no-op, not proof that ownership was lost.
                cursor.execute("""
                    SELECT id FROM agent_tasks WHERE id=%s AND execution_id=%s
                    AND status='running' AND recovery_id IS NULL
                    AND execution_heartbeat_at > DATE_SUB(NOW(), INTERVAL %s SECOND)
                """, (task_id, execution_id, lease_seconds))
                renewed = cursor.fetchone() is not None
        connection.commit()
        return renewed
    except Exception:
        connection.rollback()
        raise
    finally:
        if batch is None:
            connection.close()


def renew_registered_task_leases(limit: int = 100) -> dict:
    """Reserve a round-robin batch before I/O, even when a renewal fails.

    The lock coordinates concurrent schedulers in this process only. Each
    process renews its own live requests; MySQL still arbitrates owners. A
    process restart does not resurrect registrations for abandoned requests.
    """
    with _lease_registry_lock:
        leases = []
        for _ in range(min(len(_lease_registry), max(1, limit))):
            lease, value = _lease_registry.popitem(last=False)
            leases.append(lease)
            _lease_registry[lease] = value
    renewed = 0
    lost = 0
    errors = 0
    batch = {'connection': None, 'deadline': time.monotonic() + 4.0}
    token = _heartbeat_batch.set(batch)
    try:
        for task_id, execution_id in leases:
            try:
                if renew_agent_task_lease(task_id, execution_id):
                    renewed += 1
                else:
                    unregister_agent_task_lease(task_id, execution_id)
                    lost += 1
            except Exception:
                errors += 1
                if batch['connection'] is not None:
                    batch['connection'].close()
                    batch['connection'] = None
                logger.exception(
                    "task lease renewal failed task_id=%s execution_id=%s",
                    task_id, execution_id,
                )
    finally:
        _heartbeat_batch.reset(token)
        if batch['connection'] is not None:
            batch['connection'].close()
    return {"renewed": renewed, "lost": lost, "errors": errors}


def _task_owner_matches(cursor, task_id: int, owner: str | None, scope) -> bool:
    sql = "SELECT id FROM agent_tasks WHERE id=%s"
    params: list = [task_id]
    if scope is not None:
        sql += " AND user_id=%s AND conversation_id=%s"
        params.extend((scope.user_id, scope.conversation_id))
    if owner is None:
        sql += " AND execution_id IS NULL"
    else:
        sql += " AND execution_id=%s"
        params.append(owner)
    cursor.execute(sql, tuple(params))
    return cursor.fetchone() is not None

def create_agent_task(
    user_id: int,
    task_type: str,
    goal: str,
    intent: str,
    execution_id: str | None = None,
):
    """
    创建 Agent 任务
    """

    scope = current_scope()
    if scope is not None and scope.user_id != user_id:
        raise ValueError("Agent task user does not match conversation scope")
    connection = get_connection()

    cursor = None

    try:

        cursor = connection.cursor()


        task_no = (
            "TASK_"
            +
            uuid.uuid4().hex[:16].upper()
        )


        sql = """
        INSERT INTO agent_tasks
        (
            task_no,
            user_id,
            conversation_id,
            task_type,
            goal,
            status,
            current_step,
            context
            ,execution_id
            ,execution_started_at
            ,execution_heartbeat_at
        )
        VALUES
        (
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            CASE WHEN %s IS NULL THEN NULL ELSE NOW() END,
            CASE WHEN %s IS NULL THEN NULL ELSE NOW() END
        )
        """


        cursor.execute(
            sql,
            (
                task_no,
                user_id,
                scope.conversation_id if scope is not None else None,
                task_type,
                goal,
                "running",
                intent,
                json.dumps({}),
                execution_id,
                execution_id,
                execution_id,
            )
        )


        connection.commit()


        answer = {

            "task_id":
                cursor.lastrowid,

            "task_no":
                task_no

        }
        if execution_id is not None:
            register_agent_task_lease(answer["task_id"], execution_id)
        return answer


    except Exception:

        connection.rollback()

        raise


    finally:

        if cursor:

            cursor.close()

        connection.close()


def claim_agent_task_execution(
    task_id: int,
    user_id: int,
    conversation_id: int | None,
    execution_id: str,
) -> bool:
    """Atomically claim an unowned running/waiting-input Task."""
    if not execution_id:
        raise ValueError("execution_id is required")
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            sql = """
                UPDATE agent_tasks
                SET execution_id=%s, execution_started_at=NOW(),
                    execution_heartbeat_at=NOW(),
                    recovery_id=NULL, recovery_source_execution_id=NULL,
                    recovery_started_at=NULL, recovery_status=NULL,
                    recovery_reason=NULL,
                    status='running', current_step='running'
                WHERE id=%s AND user_id=%s
                  AND execution_id IS NULL
                  AND status IN ('running', 'waiting_input')
            """
            params: list = [execution_id, task_id, user_id]
            if conversation_id is None:
                sql += " AND conversation_id IS NULL"
            else:
                sql += " AND conversation_id=%s"
                params.append(conversation_id)
            cursor.execute(sql, tuple(params))
            claimed = cursor.rowcount == 1
        connection.commit()
        if claimed:
            register_agent_task_lease(task_id, execution_id)
        return claimed
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()





def get_active_agent_task(
    user_id:int
):
    """
    查询用户当前未完成任务
    """


    scope = current_scope()
    if scope is not None and scope.user_id != user_id:
        raise ValueError("Agent task user does not match conversation scope")
    connection = get_connection()

    cursor = None


    try:

        cursor = connection.cursor()


        sql = """
        SELECT *
        FROM agent_tasks
        WHERE user_id=%s
        AND status IN
        (
            'running',
            'waiting_input',
            'waiting_confirm',
            'recovering',
            'recovery_review'
        )
        """
        params = [user_id]
        if scope is not None:
            sql += " AND conversation_id=%s"
            params.append(scope.conversation_id)
        sql += """
        ORDER BY id DESC
        LIMIT 1
        """


        cursor.execute(
            sql,
            tuple(params)
        )

        row = cursor.fetchone()

        if row is None:
            return None

        return row



    finally:

        if cursor:

            cursor.close()

        connection.close()





def update_agent_task_context(
    task_id:int,
    current_step:str,
    status:str,
    execution_id: str | None = None,
):
    started = time.monotonic()
    for attempt in range(3):
        try:
            return _update_agent_task_context_once(task_id, current_step, status, execution_id)
        except pymysql.err.OperationalError as exc:
            # Only known rolled-back lock errors are immediately retryable.
            if (not exc.args or exc.args[0] not in {1205, 1213} or attempt == 2
                    or time.monotonic() - started + 0.05 * (attempt + 1) >= 5):
                raise
            time.sleep(0.05 * (attempt + 1))


def _update_agent_task_context_once(task_id, current_step, status, execution_id=None):

    connection = get_connection()

    cursor = None


    try:

        cursor = connection.cursor()


        scope = current_scope()
        owner = execution_id or (scope.execution_id if scope is not None else None)
        releases_owner = status in {"waiting_input", "waiting_confirm", "completed", "failed"}
        sql = """
        UPDATE agent_tasks
        SET
            current_step=%s,
            status=%s,
            execution_id=CASE WHEN %s THEN NULL ELSE execution_id END,
            execution_started_at=CASE WHEN %s THEN NULL ELSE execution_started_at END,
            execution_heartbeat_at=CASE WHEN %s THEN NULL ELSE execution_heartbeat_at END
        WHERE id=%s
        """
        params = [
            current_step, status, releases_owner, releases_owner,
            releases_owner, task_id,
        ]
        if scope is not None:
            sql += " AND user_id=%s AND conversation_id=%s"
            params.extend((scope.user_id, scope.conversation_id))
        if owner is None:
            sql += " AND execution_id IS NULL AND status IN ('running','waiting_input','waiting_confirm')"
        else:
            sql += " AND execution_id=%s"
            params.append(owner)


        cursor.execute(
            sql,
            tuple(params)
        )

        affected_rows = cursor.rowcount
        if affected_rows != 1 and (
            releases_owner or not _task_owner_matches(cursor, task_id, owner, scope)
        ):
            connection.rollback()
            raise TaskExecutionConflict("Task execution ownership was lost")
        connection.commit()
        if releases_owner and owner is not None:
            unregister_agent_task_lease(task_id, owner)
        return affected_rows

    except Exception:

        connection.rollback()

        raise

    finally:

        if cursor:

            cursor.close()

        connection.close()

def complete_agent_task(
    task_id:int,
    execution_id: str | None = None,
):

    update_agent_task_context(
        task_id,
        "completed",
        "completed",
        execution_id,
    )


def reconcile_finished_agent_tasks() -> int:
    """一次性修复旧版本遗留的终态 Action / 活动 Task 不一致记录。"""
    connection = get_connection()
    cursor = None
    try:
        cursor = connection.cursor()
        cursor.execute("""
            UPDATE agent_tasks AS t
            JOIN agent_pending_actions AS a
              ON a.task_id = t.id AND a.user_id = t.user_id
            SET t.status = CASE a.status WHEN 'executed' THEN 'completed' ELSE 'failed' END,
                t.current_step = CASE a.status WHEN 'executed' THEN 'completed' ELSE 'failed' END
            WHERE t.status IN ('running', 'waiting_input', 'waiting_confirm')
              AND t.execution_id IS NULL
              AND a.status IN ('executed', 'failed')
              AND NOT EXISTS (
                  SELECT 1 FROM agent_pending_actions AS newer
                  WHERE newer.task_id = a.task_id AND newer.id > a.id
              )
        """)
        count = cursor.rowcount
        cursor.execute("""
            UPDATE agent_pending_actions AS a
            JOIN agent_tasks AS t
              ON a.task_id = t.id AND a.user_id = t.user_id
            SET a.error_message = COALESCE(
                JSON_UNQUOTE(JSON_EXTRACT(a.execution_result_json, '$.message')),
                JSON_UNQUOTE(JSON_EXTRACT(a.execution_result_json, '$.error')),
                JSON_UNQUOTE(JSON_EXTRACT(a.execution_result_json, '$.code')),
                '写操作执行失败'
            )
            WHERE a.status = 'failed'
              AND a.error_message IS NULL
              AND t.status = 'failed'
        """)
        connection.commit()
        return count
    except Exception:
        connection.rollback()
        raise
    finally:
        if cursor is not None:
            cursor.close()
        connection.close()

def merge_task_params(
    old_params:dict,
    new_params:dict
):

    result = old_params.copy()


    for key,value in new_params.items():

        if value is not None:

            result[key] = value

    return result

def get_task_context(task_id:int, execution_id: str | None = None):

    connection = get_connection()

    try:

        cursor = connection.cursor()

        scope = current_scope()
        sql = """
        SELECT
            context
        FROM agent_tasks
        WHERE id=%s
        """
        params = [task_id]
        if scope is not None:
            sql += " AND user_id=%s AND conversation_id=%s"
            params.extend((scope.user_id, scope.conversation_id))
        owner = execution_id or (scope.execution_id if scope is not None else None)
        if owner is not None:
            sql += " AND execution_id=%s"
            params.append(owner)

        cursor.execute(
            sql,
            tuple(params)
        )

        row = cursor.fetchone()

        if row and row["context"]:

            if isinstance(row["context"], str):
                return json.loads(
                    row["context"]
                )

            return row["context"]


        return {}


    finally:

        cursor.close()

        connection.close()

def update_task_context(
    task_id:int,
    context:dict,
    execution_id: str | None = None,
):

    connection = get_connection()

    cursor = None

    try:

        cursor = connection.cursor()


        scope = current_scope()
        sql="""
        UPDATE agent_tasks
        SET context=%s
        WHERE id=%s
        """
        params = [json.dumps(context), task_id]
        if scope is not None:
            sql += " AND user_id=%s AND conversation_id=%s"
            params.extend((scope.user_id, scope.conversation_id))
        owner = execution_id or (scope.execution_id if scope is not None else None)
        if owner is None:
            sql += " AND execution_id IS NULL AND status IN ('running','waiting_input','waiting_confirm')"
        else:
            sql += " AND execution_id=%s"
            params.append(owner)


        cursor.execute(
            sql,
            tuple(params)
        )


        affected_rows = cursor.rowcount
        if affected_rows != 1 and not _task_owner_matches(cursor, task_id, owner, scope):
            connection.rollback()
            raise TaskExecutionConflict("Task execution ownership was lost")
        connection.commit()
        return affected_rows


    except Exception:

        connection.rollback()

        raise


    finally:

        if cursor:

            cursor.close()

        connection.close()
