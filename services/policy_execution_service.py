"""Execution-time policy gate shared by Agent confirmation and direct API.

Network retrieval finishes before the write transaction or Action claim.  The
write service rechecks database facts and the deterministic rule using the
already validated evidence while holding the item lock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256

from db.connection import get_connection
from db.repository import (
    get_active_after_sale_by_order_item_id,
    get_order_by_order_no_and_user_id,
    get_order_item_by_id_and_order_id,
    get_after_sale_by_idempotency_key,
)
from services.policy_check_service import check_after_sale_policy
from services.policy_demo_rules import DEMO_POLICY_SOURCES


def _date_text(value) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else value


def build_policy_snapshot(
    policy: dict, *, order_id: int, order_item_id: int, order_status: str,
    delivered_at, category: str | None, product_name: str,
    after_sale_type: str, reason: str, snapshot_version: int = 1,
) -> dict:
    """Only the server-side Preview builder may construct this saved snapshot."""
    return {
        "snapshot_version": snapshot_version,
        "policy_decision": policy["decision"],
        "rule_id": policy.get("policy_type"),
        "policy_sources": policy.get("sources"),
        "policy_versions": policy.get("policy_versions"),
        "policy_file_hashes": policy.get("policy_file_hashes"),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "order_id": order_id,
        "order_item_id": order_item_id,
        "order_status": order_status,
        "delivered_at": _date_text(delivered_at),
        "category": category,
        "product_name": product_name,
        "after_sale_type": after_sale_type,
        "reason_hash": sha256(reason.encode("utf-8")).hexdigest(),
    }


def _failure(code: str, message: str) -> dict:
    return {"success": False, "code": code, "message": message}


def existing_after_sale_for_key(idempotency_key: str, user_id: int) -> dict | None:
    connection = get_connection()
    cursor = connection.cursor()
    try:
        row = get_after_sale_by_idempotency_key(cursor, idempotency_key)
        return row if row is not None and row["user_id"] == user_id else None
    finally:
        cursor.close()
        connection.close()


def active_after_sale_for_preview(user_id: int, order_no: str, order_item_id: int) -> dict | None:
    """Read-only preflight; the execution transaction remains authoritative."""
    connection = get_connection()
    cursor = connection.cursor()
    try:
        order = get_order_by_order_no_and_user_id(cursor, user_id, order_no)
        if order is None or get_order_item_by_id_and_order_id(cursor, order_item_id, order["id"]) is None:
            return {"ownership_error": True}
        active = get_active_after_sale_by_order_item_id(cursor, order_item_id)
        if active is not None and active.get("user_id") != user_id:
            return {"ownership_error": True}
        return active
    finally:
        cursor.close()
        connection.close()


def _snapshot_matches(snapshot: dict | None, current: dict) -> bool:
    if not isinstance(snapshot, dict) or snapshot.get("snapshot_version") not in {1, 2}:
        return False
    if (
        snapshot.get("policy_decision") != "ELIGIBLE_TO_APPLY"
        or not snapshot.get("rule_id")
        or not snapshot.get("policy_sources")
        or not snapshot.get("policy_versions")
        or not snapshot.get("policy_file_hashes")
        or snapshot.get("order_id") is None
        or snapshot.get("order_item_id") is None
        or snapshot.get("delivered_at") is None
        or not snapshot.get("reason_hash")
    ):
        return False
    try:
        checked_at = datetime.fromisoformat(snapshot["checked_at"])
    except (KeyError, TypeError, ValueError):
        return False
    if checked_at.tzinfo is None or not (timedelta(0) <= datetime.now(timezone.utc) - checked_at < timedelta(days=7)):
        return False
    keys = (
        "policy_decision", "rule_id", "policy_sources", "policy_versions",
        "policy_file_hashes", "order_id", "order_item_id", "order_status",
        "delivered_at", "category", "product_name", "after_sale_type", "reason_hash",
    )
    return all(snapshot.get(key) == current.get(key) for key in keys)


@dataclass(frozen=True)
class PolicyValidation:
    user_id: int
    order_no: str
    order_item_id: int
    after_sale_type: str
    reason: str
    policy: dict
    order_id: int
    order_status: str
    product_name: str
    delivered_at: str | None
    category: str | None
    checked_at: datetime


def validate_policy_before_create(
    *, user_id: int, order_no: str, order_item_id: int,
    after_sale_type: str, reason: str, snapshot: dict | None = None,
    require_snapshot: bool = False,
) -> dict:
    """Fresh DB lookup and RAG check; never hold an item lock over the network."""
    if require_snapshot and not isinstance(snapshot, dict):
        return _failure("PREVIEW_RECHECK_REQUIRED", "历史售后预览缺少可信政策核验，请重新发起申请。")
    connection = get_connection()
    cursor = connection.cursor()
    try:
        order = get_order_by_order_no_and_user_id(cursor, user_id, order_no)
        if order is None:
            return _failure("ORDER_NOT_FOUND", "当前用户没有找到该订单，请核对申请信息。")
        item = get_order_item_by_id_and_order_id(cursor, order_item_id, order["id"])
        if item is None:
            return _failure("ORDER_ITEM_NOT_FOUND", "订单商品已变化，请重新核对申请信息。")
        if get_active_after_sale_by_order_item_id(cursor, order_item_id) is not None:
            return _failure("AFTER_SALE_ALREADY_EXISTS", "该商品已有进行中的售后申请。")
    finally:
        cursor.close()
        connection.close()

    policy = check_after_sale_policy(
        user_id=user_id, order=order, order_item=item,
        after_sale_type=after_sale_type, reason=reason,
    )
    current = build_policy_snapshot(
        policy, order_id=order["id"], order_item_id=item["id"],
        order_status=order["status"], delivered_at=order.get("delivered_at"),
        category=item.get("category"), product_name=item["product_name"],
        after_sale_type=after_sale_type, reason=reason,
        snapshot_version=snapshot.get("snapshot_version", 1) if isinstance(snapshot, dict) else 1,
    )
    if policy["decision"] == "POLICY_UNAVAILABLE":
        return _failure("POLICY_UNAVAILABLE", "暂时无法完成政策核验，请稍后重试或联系人工客服。")
    if require_snapshot and not _snapshot_matches(snapshot, current):
        return _failure("PREVIEW_RECHECK_REQUIRED", "售后政策或订单信息已发生变化，请重新核对申请信息后提交。")
    if policy["decision"] != "ELIGIBLE_TO_APPLY":
        messages = {
            "INELIGIBLE": policy["policy_message"],
            "NEEDS_REVIEW": "当前申请条件需要进一步核实，建议联系人工客服处理。",
            "POLICY_UNAVAILABLE": "暂时无法完成政策核验，请稍后重试或联系人工客服。",
        }
        return _failure(policy["decision"], messages.get(policy["decision"], "政策核验未完成。"))
    return {
        "success": True,
        "code": "POLICY_VALIDATED",
        "validation": PolicyValidation(
            user_id, order_no, order_item_id, after_sale_type, reason,
            policy, order["id"], order["status"], item["product_name"],
            _date_text(order.get("delivered_at")),
            item.get("category"), datetime.now(timezone.utc),
        ),
    }


def recheck_policy_under_lock(
    validation: PolicyValidation, *, user_id: int, order_no: str,
    order: dict, item: dict, after_sale_type: str, reason: str,
) -> dict:
    """Current DB facts + deterministic time rule; no RAG call under lock."""
    if not isinstance(validation, PolicyValidation) or (
        validation.user_id, validation.order_no, validation.order_item_id,
        validation.after_sale_type, validation.reason,
    ) != (user_id, order_no, item["id"], after_sale_type, reason):
        return _failure("POLICY_VALIDATION_REQUIRED", "需要重新进行政策核验。")
    if (
        order["id"] != validation.order_id
        or order["status"] != validation.order_status
        or item["product_name"] != validation.product_name
        or _date_text(order.get("delivered_at")) != validation.delivered_at
        or item.get("category") != validation.category
    ):
        return _failure("PREVIEW_RECHECK_REQUIRED", "订单信息已变化，请重新核对申请信息。")
    checked_evidence = [
        {**item, "version": DEMO_POLICY_SOURCES[item["source"]].rag_metadata_version}
        for item in (validation.policy.get("evidence") or [])
        if item.get("source") in DEMO_POLICY_SOURCES
    ]
    current = check_after_sale_policy(
        user_id=user_id, order=order, order_item=item,
        after_sale_type=after_sale_type, reason=reason,
        rag_query=lambda **_kwargs: {
            "success": True, "refused": False, "evidence": checked_evidence,
        },
    )
    if current["decision"] != "ELIGIBLE_TO_APPLY":
        return _failure("PREVIEW_RECHECK_REQUIRED", "申请条件已变化，请重新核对申请信息。")
    if any(current.get(key) != validation.policy.get(key) for key in (
        "policy_type", "sources", "policy_versions", "policy_file_hashes",
    )):
        return _failure("PREVIEW_RECHECK_REQUIRED", "政策规则已变化，请重新核对申请信息。")
    return {"success": True, "code": "POLICY_VALIDATED"}
