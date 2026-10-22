"""Read-only case context and conditional customer-service handoff transitions."""

import json

from db.connection import get_connection


class HandoffNotFound(Exception):
    pass


class HandoffStateConflict(Exception):
    pass


STATUSES = {"waiting", "assigned", "completed"}


def _case(cursor, handoff_id: int):
    cursor.execute("""
        SELECT h.id, h.user_id, h.conversation_id, h.reason, h.status,
               h.created_at, u.username, u.phone, c.title AS conversation_title
        FROM handoff_task AS h
        JOIN users AS u ON u.id = h.user_id
        JOIN conversations AS c ON c.id = h.conversation_id AND c.user_id = h.user_id
        WHERE h.id = %s LIMIT 1
    """, (handoff_id,))
    row = cursor.fetchone()
    if row is None:
        raise HandoffNotFound
    return row


def list_handoff_tasks(status: str = "waiting") -> list[dict]:
    if status not in STATUSES:
        raise ValueError("不支持的人工请求状态")
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT h.id, h.user_id, h.conversation_id, h.reason, h.status,
                       h.created_at, u.username, c.title AS conversation_title
                FROM handoff_task AS h
                JOIN users AS u ON u.id = h.user_id
                JOIN conversations AS c ON c.id = h.conversation_id AND c.user_id = h.user_id
                WHERE h.status = %s ORDER BY h.id DESC LIMIT 100
            """, (status,))
            return cursor.fetchall()
    finally:
        connection.close()


def get_handoff_detail(handoff_id: int) -> dict:
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            handoff = _case(cursor, handoff_id)
            user_id, conversation_id = handoff["user_id"], handoff["conversation_id"]
            cursor.execute("""
                SELECT m.id, m.role, m.content, m.created_at
                FROM conversation_messages AS m
                JOIN conversations AS c ON c.id = m.conversation_id
                WHERE c.id = %s AND c.user_id = %s ORDER BY m.id
            """, (conversation_id, user_id))
            messages = cursor.fetchall()
            cursor.execute("""
                SELECT s.after_sale_no, s.type, s.status, s.reason, s.refund_amount,
                       s.created_at, oi.product_name
                FROM after_sales AS s
                JOIN agent_pending_actions AS origin
                  ON origin.confirmation_id = s.idempotency_key
                 AND origin.action_type = 'create_after_sale'
                 AND origin.user_id = s.user_id
                JOIN order_items AS oi ON oi.id = s.order_item_id
                WHERE s.user_id = %s AND origin.conversation_id = %s
                ORDER BY s.id DESC
            """, (user_id, conversation_id))
            after_sales = cursor.fetchall()
            cursor.execute("""
                SELECT id, goal, status, current_step, created_at
                FROM agent_tasks WHERE user_id = %s AND conversation_id = %s
                ORDER BY id DESC LIMIT 100
            """, (user_id, conversation_id))
            agent_tasks = cursor.fetchall()
            cursor.execute("""
                SELECT step.task_id, step.step_name, step.status,
                       step.error_message, step.latency_ms, step.created_at
                FROM agent_task_steps AS step
                JOIN agent_tasks AS task ON task.id = step.task_id
                WHERE task.user_id = %s AND task.conversation_id = %s
                ORDER BY step.id DESC LIMIT 300
            """, (user_id, conversation_id))
            agent_steps = cursor.fetchall()
            cursor.execute("""
                SELECT confirmation_id, action_type, status, execution_result_json,
                       error_message, created_at
                FROM agent_pending_actions
                WHERE user_id = %s AND conversation_id = %s
                ORDER BY id DESC LIMIT 100
            """, (user_id, conversation_id))
            agent_results = cursor.fetchall()
            for action in agent_results:
                result = action["execution_result_json"]
                if isinstance(result, str):
                    try:
                        result = json.loads(result)
                    except ValueError:
                        result = None
                action["execution_result"] = result
                del action["execution_result_json"]
            return {
                "handoff": handoff,
                "messages": messages,
                "after_sales": after_sales,
                "agent_tasks": agent_tasks,
                "agent_steps": agent_steps,
                "agent_results": agent_results,
            }
    finally:
        connection.close()


def transition_handoff(handoff_id: int, from_status: str, to_status: str) -> dict:
    if (from_status, to_status) not in {("waiting", "assigned"), ("assigned", "completed")}:
        raise ValueError("不支持的人工请求状态流转")
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                UPDATE handoff_task AS h
                JOIN conversations AS c ON c.id = h.conversation_id AND c.user_id = h.user_id
                SET h.status = %s WHERE h.id = %s AND h.status = %s
            """, (to_status, handoff_id, from_status))
            if cursor.rowcount != 1:
                _case(cursor, handoff_id)
                raise HandoffStateConflict
            updated = _case(cursor, handoff_id)
        connection.commit()
        return updated
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def send_handoff_message(handoff_id: int, content: str) -> dict:
    """Append a human reply only while the owned handoff is assigned."""
    message = content.strip()
    if not message or len(message) > 2000:
        raise ValueError("客服回复内容应为 1 至 2000 字符")
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            # Lock the handoff so completion and message delivery cannot race.
            cursor.execute("""
                SELECT h.id, h.user_id, h.conversation_id, h.status
                FROM handoff_task AS h
                JOIN conversations AS c
                  ON c.id = h.conversation_id AND c.user_id = h.user_id
                WHERE h.id = %s FOR UPDATE
            """, (handoff_id,))
            handoff = cursor.fetchone()
            if handoff is None:
                raise HandoffNotFound
            if handoff["status"] != "assigned":
                raise HandoffStateConflict
            cursor.execute("""
                INSERT INTO conversation_messages (conversation_id, role, content)
                VALUES (%s, 'human_agent', %s)
            """, (handoff["conversation_id"], message))
            message_id = cursor.lastrowid
            cursor.execute("""
                UPDATE conversations SET updated_at = NOW()
                WHERE id = %s AND user_id = %s
            """, (handoff["conversation_id"], handoff["user_id"]))
            cursor.execute("""
                SELECT id, conversation_id, role, content, created_at
                FROM conversation_messages WHERE id = %s LIMIT 1
            """, (message_id,))
            saved = cursor.fetchone()
        connection.commit()
        return saved
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_assigned_handoff_for_conversation(conversation_id: int) -> dict | None:
    """Resolve the currently assigned case without exposing other conversations."""
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT h.id, h.user_id, h.conversation_id, h.status
                FROM handoff_task AS h
                JOIN conversations AS c
                  ON c.id = h.conversation_id AND c.user_id = h.user_id
                WHERE h.conversation_id = %s AND h.status = 'assigned'
                ORDER BY h.id DESC LIMIT 1
            """, (conversation_id,))
            return cursor.fetchone()
    finally:
        connection.close()
