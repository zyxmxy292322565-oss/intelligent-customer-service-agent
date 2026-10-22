"""Idempotent link for existing Agent tasks and Pending Actions."""

from db.connection import get_connection


def migrate_conversation_scope() -> None:
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA=DATABASE()
                  AND TABLE_NAME IN ('agent_tasks', 'agent_pending_actions')
                  AND COLUMN_NAME='conversation_id'
            """)
            columns = {row["TABLE_NAME"] for row in cursor.fetchall()}
            for table in ("agent_tasks", "agent_pending_actions"):
                if table not in columns:
                    cursor.execute(f"ALTER TABLE {table} ADD COLUMN conversation_id BIGINT NULL")

            cursor.execute("""
                SELECT TABLE_NAME, INDEX_NAME FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA=DATABASE()
                  AND TABLE_NAME IN ('agent_tasks', 'agent_pending_actions')
            """)
            indexes = {(row["TABLE_NAME"], row["INDEX_NAME"]) for row in cursor.fetchall()}
            for table, name in (
                ("agent_tasks", "idx_agent_tasks_user_conversation_status"),
                ("agent_pending_actions", "idx_pending_user_conversation_status"),
            ):
                if (table, name) not in indexes:
                    cursor.execute(
                        f"CREATE INDEX {name} ON {table}(user_id, conversation_id, status)"
                    )

            cursor.execute("""
                SELECT TABLE_NAME, CONSTRAINT_NAME FROM information_schema.REFERENTIAL_CONSTRAINTS
                WHERE CONSTRAINT_SCHEMA=DATABASE()
                  AND TABLE_NAME IN ('agent_tasks', 'agent_pending_actions')
            """)
            constraints = {(row["TABLE_NAME"], row["CONSTRAINT_NAME"]) for row in cursor.fetchall()}
            for table, name in (
                ("agent_tasks", "fk_agent_tasks_conversation"),
                ("agent_pending_actions", "fk_pending_conversation"),
            ):
                if (table, name) not in constraints:
                    cursor.execute(
                        f"ALTER TABLE {table} ADD CONSTRAINT {name} "
                        "FOREIGN KEY (conversation_id) REFERENCES conversations(id)"
                    )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    migrate_conversation_scope()
