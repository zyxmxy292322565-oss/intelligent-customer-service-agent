"""为已有数据库补充 P1 埋点字段；可重复执行，不改动业务数据。"""

from db.connection import get_connection


def migrate_agent_trace():
    connection = get_connection()
    cursor = None
    try:
        cursor = connection.cursor()
        cursor.execute("""
            SELECT TABLE_NAME, COLUMN_NAME
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME IN ('agent_task_steps', 'agent_pending_actions')
        """)
        columns = {(row["TABLE_NAME"], row["COLUMN_NAME"]) for row in cursor.fetchall()}
        if ("agent_task_steps", "trace_id") not in columns:
            cursor.execute("ALTER TABLE agent_task_steps ADD COLUMN trace_id VARCHAR(64) NULL")
        if ("agent_pending_actions", "task_id") not in columns:
            cursor.execute("ALTER TABLE agent_pending_actions ADD COLUMN task_id BIGINT NULL")
        cursor.execute("""
            SELECT IS_NULLABLE
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'agent_task_steps'
              AND COLUMN_NAME = 'task_id'
        """)
        if cursor.fetchone()["IS_NULLABLE"] == "NO":
            cursor.execute("ALTER TABLE agent_task_steps MODIFY COLUMN task_id BIGINT NULL")
        cursor.execute("""
            SELECT COUNT(*) AS count
            FROM information_schema.STATISTICS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'agent_task_steps'
              AND INDEX_NAME = 'idx_agent_task_steps_trace_id'
        """)
        if cursor.fetchone()["count"] == 0:
            cursor.execute("CREATE INDEX idx_agent_task_steps_trace_id ON agent_task_steps(trace_id)")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        if cursor is not None:
            cursor.close()
        connection.close()


if __name__ == "__main__":
    migrate_agent_trace()
