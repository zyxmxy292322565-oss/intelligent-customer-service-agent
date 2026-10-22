"""JWT-owned conversation history and human handoff persistence."""

from db.connection import get_connection


class ConversationNotFound(Exception):
    pass


def _owned_conversation(cursor, user_id: int, conversation_id: int):
    cursor.execute(
        "SELECT id, user_id, title, status, created_at, updated_at "
        "FROM conversations WHERE id=%s AND user_id=%s LIMIT 1",
        (conversation_id, user_id),
    )
    conversation = cursor.fetchone()
    if conversation is None:
        raise ConversationNotFound
    return conversation


def create_conversation(user_id: int, title: str = "新会话") -> dict:
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO conversations (user_id, title, status) VALUES (%s, %s, 'active')",
                (user_id, title[:255] or "新会话"),
            )
            conversation_id = cursor.lastrowid
            conversation = _owned_conversation(cursor, user_id, conversation_id)
        connection.commit()
        return conversation
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_conversations(user_id: int) -> list[dict]:
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, title, status, created_at, updated_at FROM conversations "
                "WHERE user_id=%s ORDER BY updated_at DESC, id DESC LIMIT 100",
                (user_id,),
            )
            return cursor.fetchall()
    finally:
        connection.close()


def get_conversation(user_id: int, conversation_id: int) -> dict:
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            return _owned_conversation(cursor, user_id, conversation_id)
    finally:
        connection.close()


def get_conversation_messages(user_id: int, conversation_id: int) -> list[dict]:
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            _owned_conversation(cursor, user_id, conversation_id)
            cursor.execute(
                "SELECT id, role, content, created_at FROM conversation_messages "
                "WHERE conversation_id=%s ORDER BY id ASC",
                (conversation_id,),
            )
            return cursor.fetchall()
    finally:
        connection.close()


def get_conversation_messages_since(conversation_id: int, after_id: int) -> list[dict]:
    """Replay committed messages after a websocket reconnect (caller checks access)."""
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, conversation_id, role, content, created_at "
                "FROM conversation_messages WHERE conversation_id=%s AND id>%s ORDER BY id ASC",
                (conversation_id, after_id),
            )
            return cursor.fetchall()
    finally:
        connection.close()


def save_message(user_id: int, conversation_id: int, role: str, content: str) -> dict:
    if role not in {"user", "assistant", "system"}:
        raise ValueError("Unsupported conversation role")
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            conversation = _owned_conversation(cursor, user_id, conversation_id)
            cursor.execute(
                "INSERT INTO conversation_messages (conversation_id, role, content) "
                "VALUES (%s, %s, %s)",
                (conversation_id, role, content),
            )
            message_id = cursor.lastrowid
            if role == "user" and conversation["title"] == "新会话":
                cursor.execute(
                    "UPDATE conversations SET title=%s, updated_at=NOW() WHERE id=%s AND user_id=%s",
                    (content.strip()[:255] or "新会话", conversation_id, user_id),
                )
            else:
                cursor.execute(
                    "UPDATE conversations SET updated_at=NOW() WHERE id=%s AND user_id=%s",
                    (conversation_id, user_id),
                )
            cursor.execute(
                "SELECT id, conversation_id, role, content, created_at "
                "FROM conversation_messages WHERE id=%s",
                (message_id,),
            )
            saved = cursor.fetchone()
        connection.commit()
        return saved
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def create_handoff(user_id: int, conversation_id: int, reason: str) -> dict:
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            # Serialize concurrent requests from one conversation before checking
            # for an existing open handoff. Ownership remains enforced here.
            cursor.execute(
                "SELECT id FROM conversations WHERE id=%s AND user_id=%s FOR UPDATE",
                (conversation_id, user_id),
            )
            if cursor.fetchone() is None:
                raise ConversationNotFound
            cursor.execute(
                "SELECT id, status FROM handoff_task "
                "WHERE user_id=%s AND conversation_id=%s "
                "AND status IN ('waiting', 'assigned') ORDER BY id DESC LIMIT 1",
                (user_id, conversation_id),
            )
            existing = cursor.fetchone()
            if existing is not None:
                connection.commit()
                return existing
            cursor.execute(
                "INSERT INTO handoff_task (user_id, conversation_id, reason, status) "
                "VALUES (%s, %s, %s, 'waiting')",
                (user_id, conversation_id, reason),
            )
            task_id = cursor.lastrowid
        connection.commit()
        return {"id": task_id, "status": "waiting"}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
