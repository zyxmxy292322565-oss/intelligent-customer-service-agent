"""Read-only Agent trace queries scoped to the authenticated user."""

from db.connection import get_connection


def list_user_tasks(user_id: int, conversation_id: int | None = None) -> list[dict]:
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            sql = "SELECT id, status, created_at FROM agent_tasks WHERE user_id=%s"
            params = [user_id]
            if conversation_id is not None:
                sql += " AND conversation_id=%s"
                params.append(conversation_id)
            cursor.execute(sql + " ORDER BY id DESC LIMIT 20", tuple(params))
            return list(cursor.fetchall())
    finally:
        connection.rollback()
        connection.close()


def get_user_task_trace(
    user_id: int, task_id: int, conversation_id: int | None = None
) -> dict | None:
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            sql = "SELECT id, status, created_at FROM agent_tasks WHERE id=%s AND user_id=%s"
            params = [task_id, user_id]
            if conversation_id is not None:
                sql += " AND conversation_id=%s"
                params.append(conversation_id)
            cursor.execute(sql + " LIMIT 1", tuple(params))
            task = cursor.fetchone()
            if task is None:
                return None
            cursor.execute(
                "SELECT task_id, trace_id, step_name, status, latency_ms, created_at "
                "FROM agent_task_steps WHERE task_id=%s ORDER BY id ASC LIMIT 200",
                (task_id,),
            )
            return {"task": task, "steps": list(cursor.fetchall())}
    finally:
        connection.rollback()
        connection.close()
