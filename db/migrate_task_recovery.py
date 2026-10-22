"""Add bounded lease/recovery metadata for Agent task executions.

The migration is additive and idempotent.  Deployment tooling decides when it
is applied; importing application modules never changes a database schema.
"""

from db.connection import get_connection


_COLUMNS = {
    "execution_heartbeat_at": "DATETIME NULL",
    "recovery_id": "VARCHAR(64) NULL",
    "recovery_source_execution_id": "VARCHAR(64) NULL",
    "recovery_started_at": "DATETIME NULL",
    "recovery_status": "VARCHAR(32) NULL",
    "recovery_reason": "VARCHAR(255) NULL",
}


def migrate_task_recovery() -> None:
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT COLUMN_NAME
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA=DATABASE()
                  AND TABLE_NAME='agent_tasks'
            """)
            columns = {row["COLUMN_NAME"] for row in cursor.fetchall()}
            for name, definition in _COLUMNS.items():
                if name not in columns:
                    cursor.execute(
                        f"ALTER TABLE agent_tasks ADD COLUMN {name} {definition}"
                    )

            cursor.execute("""
                SELECT INDEX_NAME
                FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA=DATABASE()
                  AND TABLE_NAME='agent_tasks'
            """)
            indexes = {row["INDEX_NAME"] for row in cursor.fetchall()}
            if "uk_agent_tasks_recovery_id" not in indexes:
                cursor.execute(
                    "CREATE UNIQUE INDEX uk_agent_tasks_recovery_id "
                    "ON agent_tasks(recovery_id)"
                )
            if "idx_agent_tasks_lease_scan" not in indexes:
                cursor.execute(
                    "CREATE INDEX idx_agent_tasks_lease_scan "
                    "ON agent_tasks(status, execution_heartbeat_at)"
                )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    migrate_task_recovery()
