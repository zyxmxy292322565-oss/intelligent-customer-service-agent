"""Add durable receipts for post-application operations.

This migration is intentionally explicit: deployment tooling, not application
startup, chooses when it is applied.  It only creates an additive table.
"""

from db.connection import get_connection


def migrate_action_receipts() -> None:
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS agent_action_receipts (
                    confirmation_id VARCHAR(64) NOT NULL,
                    user_id BIGINT NOT NULL,
                    action_type VARCHAR(50) NOT NULL,
                    request_fingerprint CHAR(64) NOT NULL,
                    status VARCHAR(20) NOT NULL,
                    result_json JSON NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (confirmation_id),
                    KEY idx_action_receipts_user (user_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    migrate_action_receipts()
