"""Conversation-scoped, conditional updates for post-application operations."""

import hashlib
import json
import re

from pymysql.err import IntegrityError

from db.connection import get_connection
from db.repository import create_after_sale_status_log


SALE_TYPES = {"refund", "return_refund", "exchange", "repair"}


class OperationResultUncertain(RuntimeError):
    """The caller must not replay a write whose commit result is unknown."""


def _operation_fingerprint(action_type: str, preview: dict) -> str:
    """Bind a receipt to the immutable, business-relevant operation inputs."""
    fields = {
        "action_type": action_type,
        "after_sale_id": preview.get("after_sale_id"),
        "after_sale_no": preview.get("after_sale_no"),
        "old_type": preview.get("old_type"),
        "new_type": preview.get("new_type"),
        "cancel_reason": preview.get("cancel_reason"),
    }
    payload = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get_operation_receipt(
    confirmation_id: str, user_id: int, action_type: str, preview: dict,
) -> dict | None:
    """Return a verified committed receipt, never a result inferred from sale state."""
    fingerprint = _operation_fingerprint(action_type, preview)
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT user_id, action_type, request_fingerprint, status, result_json
                FROM agent_action_receipts WHERE confirmation_id=%s
            """, (confirmation_id,))
            row = cursor.fetchone()
        if row is None:
            return None
        if (row["user_id"] != user_id or row["action_type"] != action_type
                or row["request_fingerprint"] != fingerprint):
            raise ValueError("Operation receipt identity mismatch")
        if row["status"] != "succeeded" or row["result_json"] is None:
            raise OperationResultUncertain("Operation receipt is not a committed result")
        value = row["result_json"]
        return json.loads(value) if isinstance(value, str) else value
    finally:
        connection.close()


def _reserve_receipt(cursor, confirmation_id: str, user_id: int, action_type: str, fingerprint: str) -> None:
    cursor.execute("""
        INSERT INTO agent_action_receipts
            (confirmation_id, user_id, action_type, request_fingerprint, status)
        VALUES (%s, %s, %s, %s, 'processing')
    """, (confirmation_id, user_id, action_type, fingerprint))


def _store_receipt(cursor, confirmation_id: str, result: dict) -> None:
    cursor.execute("""
        UPDATE agent_action_receipts
        SET status='succeeded', result_json=%s
        WHERE confirmation_id=%s AND status='processing'
    """, (json.dumps(result, ensure_ascii=False, default=str), confirmation_id))
    if cursor.rowcount != 1:
        raise OperationResultUncertain("Operation receipt reservation was lost")


def list_conversation_after_sales(user_id: int, conversation_id: int) -> list[dict]:
    """Only sales created by an Agent action in this user's conversation."""
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT s.id, s.after_sale_no, s.user_id, s.order_item_id,
                       s.type, s.status, s.refund_amount, s.created_at,
                       oi.product_name, oi.unit_price, oi.quantity
                FROM after_sales AS s
                JOIN agent_pending_actions AS origin
                  ON origin.confirmation_id = s.idempotency_key
                 AND origin.action_type = 'create_after_sale'
                 AND origin.user_id = s.user_id
                JOIN order_items AS oi ON oi.id = s.order_item_id
                WHERE s.user_id = %s AND origin.conversation_id = %s
                ORDER BY s.id DESC
            """, (user_id, conversation_id))
            return cursor.fetchall()
    finally:
        connection.close()


def list_owned_after_sales_by_no(user_id: int, after_sale_no: str) -> list[dict]:
    """A sale number identifies an owned application independently of its conversation."""
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT s.id, s.after_sale_no, s.user_id, s.order_item_id,
                       s.type, s.status, s.refund_amount, s.created_at,
                       oi.product_name, oi.unit_price, oi.quantity
                FROM after_sales AS s
                JOIN order_items AS oi ON oi.id = s.order_item_id
                WHERE s.user_id = %s AND s.after_sale_no = %s
                LIMIT 1
            """, (user_id, after_sale_no))
            sale = cursor.fetchone()
            return [sale] if sale else []
    finally:
        connection.close()


def list_owned_pending_after_sales_by_product(user_id: int, product_name: str) -> list[dict]:
    """Search only this user's pending applications, allowing a partial product name."""
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT s.id, s.after_sale_no, s.user_id, s.order_item_id,
                       s.type, s.status, s.refund_amount, s.created_at,
                       oi.product_name, oi.unit_price, oi.quantity
                FROM after_sales AS s
                JOIN order_items AS oi ON oi.id = s.order_item_id
                WHERE s.user_id = %s AND s.status = 'pending'
                  AND LOCATE(LOWER(%s), LOWER(oi.product_name)) > 0
                ORDER BY s.id DESC
            """, (user_id, product_name))
            return cursor.fetchall()
    finally:
        connection.close()


def list_owned_pending_after_sales_by_normalized_product(user_id: int, product_name: str) -> list[dict]:
    """Ignore spaces and case when changing an owned pending sale's type."""
    normalized_name = re.sub(r"\s+", "", product_name).lower()
    if not normalized_name:
        return []
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT s.id, s.after_sale_no, s.user_id, s.order_item_id,
                       s.type, s.status, s.refund_amount, s.created_at,
                       oi.product_name, oi.unit_price, oi.quantity
                FROM after_sales AS s
                JOIN order_items AS oi ON oi.id = s.order_item_id
                WHERE s.user_id = %s AND s.status = 'pending'
                  AND LOCATE(%s, LOWER(REPLACE(oi.product_name, ' ', ''))) > 0
                ORDER BY s.id DESC
            """, (user_id, normalized_name))
            return cursor.fetchall()
    finally:
        connection.close()


def list_owned_pending_after_sales(user_id: int) -> list[dict]:
    """Return pending sales for one user when no sale number or product was supplied."""
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT s.id, s.after_sale_no, s.user_id, s.order_item_id,
                       s.type, s.status, s.refund_amount, s.created_at,
                       oi.product_name, oi.unit_price, oi.quantity
                FROM after_sales AS s
                JOIN order_items AS oi ON oi.id = s.order_item_id
                WHERE s.user_id = %s AND s.status = 'pending'
                ORDER BY s.id DESC
            """, (user_id,))
            return cursor.fetchall()
    finally:
        connection.close()


def _get_owned_sale_by_id_and_no(cursor, user_id: int, after_sale_id: int, after_sale_no: str):
    cursor.execute("""
        SELECT s.id, s.after_sale_no, s.type, s.status, s.order_item_id,
               oi.unit_price, oi.quantity
        FROM after_sales AS s
        JOIN order_items AS oi ON oi.id = s.order_item_id
        WHERE s.id = %s AND s.user_id = %s AND s.after_sale_no = %s
        LIMIT 1
    """, (after_sale_id, user_id, after_sale_no))
    return cursor.fetchone()


def _get_owned_sale_by_no(cursor, user_id: int, after_sale_no: str):
    cursor.execute("""
        SELECT s.id, s.after_sale_no, s.type, s.status, s.order_item_id,
               oi.unit_price, oi.quantity
        FROM after_sales AS s
        JOIN order_items AS oi ON oi.id = s.order_item_id
        WHERE s.user_id = %s AND s.after_sale_no = %s
        LIMIT 1
    """, (user_id, after_sale_no))
    return cursor.fetchone()


def _get_owned_sale(cursor, user_id: int, conversation_id: int, after_sale_no: str):
    cursor.execute("""
        SELECT s.id, s.after_sale_no, s.type, s.status, s.order_item_id,
               oi.unit_price, oi.quantity
        FROM after_sales AS s
        JOIN agent_pending_actions AS origin
          ON origin.confirmation_id = s.idempotency_key
         AND origin.action_type = 'create_after_sale'
         AND origin.user_id = s.user_id
        JOIN order_items AS oi ON oi.id = s.order_item_id
        WHERE s.user_id = %s AND origin.conversation_id = %s
          AND s.after_sale_no = %s
        LIMIT 1
    """, (user_id, conversation_id, after_sale_no))
    return cursor.fetchone()


def execute_after_sale_operation(
    *, user_id: int, conversation_id: int, action_type: str, preview: dict,
    confirmation_id: str | None = None,
) -> dict:
    """Recheck ownership and state at execution time, not only at preview time."""
    if action_type not in {"cancel_after_sale", "change_after_sale_type"}:
        raise ValueError("Unsupported after-sale operation")
    after_sale_no = preview.get("after_sale_no")
    if not isinstance(after_sale_no, str):
        return {"success": False, "code": "INVALID_PREVIEW", "message": "售后信息不完整"}
    if action_type == "change_after_sale_type" and (
        not isinstance(preview.get("after_sale_id"), int)
        or isinstance(preview["after_sale_id"], bool)
        or preview["after_sale_id"] <= 0
    ):
        return {"success": False, "code": "INVALID_PREVIEW", "message": "售后信息不完整"}

    fingerprint = _operation_fingerprint(action_type, preview)
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            if confirmation_id is not None:
                try:
                    _reserve_receipt(cursor, confirmation_id, user_id, action_type, fingerprint)
                except IntegrityError as exc:
                    # A duplicate key can only be resolved by reading the
                    # original durable receipt; never infer it from sale state.
                    connection.rollback()
                    result = get_operation_receipt(confirmation_id, user_id, action_type, preview)
                    if result is not None:
                        return result
                    raise OperationResultUncertain("Operation reservation outcome is unknown") from exc
            if action_type == "cancel_after_sale":
                after_sale_id = preview.get("after_sale_id")
                sale = (_get_owned_sale_by_id_and_no(cursor, user_id, after_sale_id, after_sale_no)
                        if isinstance(after_sale_id, int) and not isinstance(after_sale_id, bool)
                        else _get_owned_sale_by_no(cursor, user_id, after_sale_no))
            else:
                sale = _get_owned_sale_by_id_and_no(
                    cursor, user_id, preview["after_sale_id"], after_sale_no,
                )
            if sale is None:
                message = ("当前会话没有这笔售后申请" if action_type == "cancel_after_sale"
                           else "售后申请不存在或无权操作")
                connection.rollback()
                return {"success": False, "code": "ACTION_NOT_FOUND", "message": message}
            if sale["status"] == "cancelled":
                connection.rollback()
                return {"success": False, "code": "AFTER_SALE_ALREADY_CANCELLED", "message": "售后申请已经取消"}
            if sale["status"] != "pending":
                connection.rollback()
                return {"success": False, "code": "AFTER_SALE_NOT_PENDING", "message": "申请已进入处理流程，无法自行取消或修改"}

            if action_type == "cancel_after_sale":
                cursor.execute("""
                    UPDATE after_sales SET status = 'cancelled'
                    WHERE id = %s AND user_id = %s AND status = 'pending'
                """, (sale["id"], user_id))
                if cursor.rowcount != 1:
                    connection.rollback()
                    return {"success": False, "code": "AFTER_SALE_STATE_CONFLICT", "message": "申请状态已变化，请刷新后重试"}
                create_after_sale_status_log(
                    cursor, sale["id"], "pending", "cancelled", "user", str(user_id),
                    preview.get("cancel_reason") or "用户主动取消售后申请",
                )
                message = "售后申请已取消"
                data = {"operation": action_type, "after_sale_no": after_sale_no, "status": "cancelled"}
            else:
                new_type = preview.get("new_type")
                if new_type not in SALE_TYPES:
                    connection.rollback()
                    return {"success": False, "code": "AFTER_SALE_TYPE_REQUIRED", "message": "请选择售后类型"}
                if sale["type"] == new_type:
                    connection.rollback()
                    return {"success": False, "code": "AFTER_SALE_TYPE_UNCHANGED", "message": "申请已经是该售后类型"}
                if sale["type"] != preview.get("old_type"):
                    connection.rollback()
                    return {"success": False, "code": "AFTER_SALE_STATE_CONFLICT", "message": "申请类型已变化，请刷新后重试"}
                refund_amount = (
                    sale["unit_price"] * sale["quantity"]
                    if new_type in {"refund", "return_refund"} else None
                )
                cursor.execute("""
                    UPDATE after_sales SET type = %s, refund_amount = %s
                    WHERE id = %s AND user_id = %s AND after_sale_no = %s
                      AND status = 'pending' AND type = %s
                """, (new_type, refund_amount, sale["id"], user_id, after_sale_no, sale["type"]))
                if cursor.rowcount != 1:
                    connection.rollback()
                    return {"success": False, "code": "AFTER_SALE_STATE_CONFLICT", "message": "申请状态已变化，请刷新后重试"}
                message = "售后类型已修改"
                data = {"operation": action_type, "after_sale_no": after_sale_no, "type": new_type,
                        "status": "pending", "refund_amount": refund_amount}
            result = {"success": True, "code": "SUCCESS", "message": message, "data": data}
            if confirmation_id is not None:
                _store_receipt(cursor, confirmation_id, result)
            connection.commit()
            return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
