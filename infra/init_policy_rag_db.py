"""Create a separate RAG database and account on an existing MySQL volume."""

import os
import re

import pymysql


RAG_DATABASE = "rag_demo"
DEV_PASSWORD = "local-dev-rag-only-change-me"


def main() -> None:
    user = os.getenv("RAG_MYSQL_USER", "rag_agent")
    password = os.getenv("RAG_MYSQL_PASSWORD", "")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,32}", user):
        raise ValueError("RAG_MYSQL_USER must contain only letters, digits, or underscores")
    if user.lower() in {"root", os.getenv("MYSQL_APP_USER", "agent").lower()}:
        raise ValueError("RAG_MYSQL_USER must be distinct from MySQL root and app users")
    if not password:
        raise ValueError("RAG_MYSQL_PASSWORD is required")
    if os.getenv("APP_ENV", "development").lower() == "production" and password == DEV_PASSWORD:
        raise ValueError("Set a unique RAG_MYSQL_PASSWORD in production")

    connection = pymysql.connect(
        host=os.getenv("MYSQL_HOST", "mysql"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user="root",
        password=os.environ["MYSQL_ROOT_PASSWORD"],
        autocommit=True,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "CREATE DATABASE IF NOT EXISTS `rag_demo` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
            cursor.execute(
                f"CREATE USER IF NOT EXISTS '{user}'@'%%' IDENTIFIED BY %s",
                (password,),
            )
            cursor.execute(
                f"ALTER USER '{user}'@'%%' IDENTIFIED BY %s",
                (password,),
            )
            cursor.execute(
                f"GRANT ALL PRIVILEGES ON `{RAG_DATABASE}`.* TO '{user}'@'%'"
            )
    finally:
        connection.close()


if __name__ == "__main__":
    main()
