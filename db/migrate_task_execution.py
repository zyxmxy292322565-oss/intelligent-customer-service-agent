"""Add database-backed ownership for each Agent task execution.

This migration is explicit and additive.  Deployment tooling decides when it
is applied; importing application modules never changes a database schema.
"""

from db.connection import get_connection


def migrate_task_execution() -> None:
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT COLUMN_NAME
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA=DATABASE()
                  AND TABLE_NAME='agent_tasks'
                  AND COLUMN_NAME IN ('execution_id', 'execution_started_at')
            """)
            columns = {row["COLUMN_NAME"] for row in cursor.fetchall()}
            if "execution_id" not in columns:
                cursor.execute(
                    "ALTER TABLE agent_tasks ADD COLUMN execution_id VARCHAR(64) NULL"
                )
            if "execution_started_at" not in columns:
                cursor.execute(
                    "ALTER TABLE agent_tasks ADD COLUMN execution_started_at DATETIME NULL"
                )

            cursor.execute("""
                SELECT COUNT(*) AS count
                FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA=DATABASE()
                  AND TABLE_NAME='agent_tasks'
                  AND INDEX_NAME='uk_agent_tasks_execution_id'
            """)
            if cursor.fetchone()["count"] == 0:
                cursor.execute(
                    "CREATE UNIQUE INDEX uk_agent_tasks_execution_id "
                    "ON agent_tasks(execution_id)"
                )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    migrate_task_execution()
