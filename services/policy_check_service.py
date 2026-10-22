"""Deterministic, demonstration-only after-sale application policy check.

The caller supplies *trusted database rows*, never LLM-produced order facts.
This module does not create an after-sale record or approve a refund.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone as fixed_timezone
from pathlib import PurePath
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from services.policy_demo_rules import DEMO_POLICY_RULES, DEMO_POLICY_SOURCES


_QUALITY_TERMS = (
    "质量不好", "质量问题", "故障", "损坏", "无法开机", "开不了机",
    "连接异常", "单侧无声", "没有声音", "没声音", "不出声", "断断续续",
    "失灵", "坏了",
)
_CONCRETE_FAULT_TERMS = tuple(term for term in _QUALITY_TERMS if term not in {"质量不好", "质量问题"})
_NO_REASON_TERMS = ("无理由", "不喜欢", "不想要", "不合适", "买错", "不需要了")
_DAMAGE_TERMS = ("人为损坏", "摔坏", "进液", "泡水", "自己拆", "外力损坏")
_NEGATED_QUALITY_PATTERNS = (
    r"没(?:有)?(?:任何|什么)?质量问题",
    r"没发现质量问题",
    r"不(?:是|属于)质量问题",
    r"并不(?:是)?(?:因为)?质量问题",
    r"(?:商品|产品|设备|耳机|手机|平板|电脑)?(?:一切)?正常",
)
_UNCERTAIN_QUALITY_PATTERNS = (
    r"不确定(?:是)?不?是质量问题",
    r"(?:感觉|好像|可能)(?:是)?(?:有)?(?:质量)?问题",
)
_CATEGORIES = {
    "耳机": "耳机", "耳机类": "耳机", "无线耳机": "耳机", "有线耳机": "耳机",
    "手机": "手机", "平板": "平板", "平板电脑": "平板",
    "电脑": "电脑", "笔记本电脑": "电脑",
}


def _result(decision: str, message: str, **details) -> dict:
    return {
        "decision": decision,
        "allowed_to_apply": decision == "ELIGIBLE_TO_APPLY",
        "requires_review": decision != "INELIGIBLE",
        "policy_message": message,
        "policy_type": details.pop("policy_type", None),
        "days_since_delivery": details.pop("days_since_delivery", None),
        "policy_deadline_days": details.pop("policy_deadline_days", None),
        "sources": details.pop("sources", []),
        "policy_versions": details.pop("policy_versions", {}),
        "policy_file_hashes": details.pop("policy_file_hashes", {}),
        **details,
    }


def _reason_kind(reason: str) -> str | None:
    normalized = re.sub(r"\s+", "", reason or "")
    if any(term in normalized for term in _DAMAGE_TERMS):
        return None
    negated_quality = any(re.search(pattern, normalized) for pattern in _NEGATED_QUALITY_PATTERNS)
    uncertain_quality = any(re.search(pattern, normalized) for pattern in _UNCERTAIN_QUALITY_PATTERNS)
    concrete_fault = any(term in normalized for term in _CONCRETE_FAULT_TERMS)
    no_reason = any(term in normalized for term in _NO_REASON_TERMS)

    # A denial plus a concrete symptom is contradictory, and uncertainty is
    # never resolved by this deterministic policy gate. Both require review.
    if uncertain_quality or (negated_quality and concrete_fault):
        return None
    if negated_quality:
        return "no_reason" if no_reason else None

    quality = any(term in normalized for term in _QUALITY_TERMS)
    if quality == no_reason:
        return None
    return "quality" if quality else "no_reason"


def _read_delivery(value, business_tz) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    # MySQL DATETIME is timezone-less. Its business timezone must be explicit.
    return parsed.replace(tzinfo=business_tz) if parsed.tzinfo is None else parsed.astimezone(business_tz)


def _rag_dict(response) -> dict:
    return response.model_dump() if hasattr(response, "model_dump") else response


def check_after_sale_policy(
    *,
    user_id: int,
    order: dict,
    order_item: dict,
    after_sale_type: str,
    reason: str,
    item_condition_verified: bool = False,
    now: datetime | None = None,
    business_timezone: str = "Asia/Shanghai",
    rag_query=None,
    rules=DEMO_POLICY_RULES,
) -> dict:
    """Check eligibility to *apply*, not approval of the requested outcome.

    ``item_condition_verified`` is only for a trusted verification result; a
    customer's statement alone must not set it to true. The returned policy
    hashes/version are suitable for a later execution-time recheck.
    """
    if order.get("user_id") != user_id or order_item.get("order_id") != order.get("id"):
        return _result("NEEDS_REVIEW", "订单或商品归属无法核实，请联系人工客服。")

    category = _CATEGORIES.get(str(order_item.get("category") or "").strip())
    if category is None:
        return _result("NEEDS_REVIEW", "当前商品的售后政策需要进一步核实，您可以联系人工客服协助处理。")

    used_fixed_shanghai_offset = False
    try:
        timezone = ZoneInfo(business_timezone)
    except ZoneInfoNotFoundError:
        # Some Windows Python installations omit the IANA tzdata package.
        # Modern Asia/Shanghai business dates use UTC+08:00. No fallback is
        # made for other regions or historical dates with changing offsets.
        if business_timezone != "Asia/Shanghai":
            return _result("NEEDS_REVIEW", "业务时区尚未确认，请联系人工客服核实签收时间。")
        timezone = fixed_timezone(timedelta(hours=8), "Asia/Shanghai")
        used_fixed_shanghai_offset = True
    delivered = _read_delivery(order.get("delivered_at"), timezone)
    if delivered is None:
        return _result("NEEDS_REVIEW", "签收时间待核实，您可以联系人工客服协助处理。")
    current = now or datetime.now(timezone)
    if current.tzinfo is None:
        return _result("NEEDS_REVIEW", "当前时间缺少时区信息，无法核实申请期限。")
    current = current.astimezone(timezone)
    if used_fixed_shanghai_offset and (delivered.year < 1992 or current.year < 1992):
        return _result("NEEDS_REVIEW", "历史时区数据不足，请人工核实签收时间。")
    if delivered > current:
        return _result("NEEDS_REVIEW", "签收时间晚于当前时间，请联系人工客服核实订单。")
    days = (current.date() - delivered.date()).days

    if order.get("status") != "delivered":
        return _result("NEEDS_REVIEW", "订单状态与签收记录需要进一步核实。", days_since_delivery=days)
    if not reason or not reason.strip():
        return _result("NEEDS_REVIEW", "申请原因需要进一步说明。", days_since_delivery=days)
    reason_kind = _reason_kind(reason)
    if reason_kind is None:
        return _result("NEEDS_REVIEW", "申请原因或商品状态需要人工核实。", days_since_delivery=days)

    candidates = [
        rule for rule in rules
        if category in rule.categories
        and rule.application_type == after_sale_type
        and rule.reason_kind == reason_kind
    ]
    if not candidates:
        return _result("NEEDS_REVIEW", "没有适用于该商品和申请类型的已核对演示规则，请联系人工客服。", days_since_delivery=days)
    if len({(rule.deadline_days, rule.required_sources) for rule in candidates}) != 1:
        return _result("NEEDS_REVIEW", "政策规则存在冲突，需要人工核实。", days_since_delivery=days)
    rule = candidates[0]
    expected = {name: DEMO_POLICY_SOURCES[name] for name in rule.required_sources}

    question = (
        f"{category}类商品签收后，因{reason_kind == 'quality' and '疑似质量问题' or '不喜欢/无理由'}"
        f"申请{after_sale_type}的期限、条件及例外是什么？"
    )
    if rag_query is None:
        from services.policy_rag_client import query_policy_rag
        rag_query = query_policy_rag
    try:
        rag = _rag_dict(rag_query(question=question, top_k=5))
    except Exception:
        return _result("POLICY_UNAVAILABLE", "政策知识服务暂不可用，请稍后重试或联系人工客服。", days_since_delivery=days)
    if not isinstance(rag, dict) or not rag.get("success") or rag.get("refused"):
        return _result("POLICY_UNAVAILABLE", "当前无法取得有效政策依据，请联系人工客服。", days_since_delivery=days)

    evidence = [item for item in (rag.get("evidence") or []) if isinstance(item, dict) and item.get("content")]
    if not evidence:
        return _result("POLICY_UNAVAILABLE", "没有检索到可核对的政策依据，请联系人工客服。", days_since_delivery=days)
    found = set()
    for item in evidence:
        # RAG sources are filenames; never trust a path prefix to select policy.
        name = PurePath(str(item.get("source") or "").replace("\\", "/")).name
        source = DEMO_POLICY_SOURCES.get(name)
        if source is None:
            return _result("NEEDS_REVIEW", "检索结果含未核对政策来源，需要人工核实。", days_since_delivery=days)
        # Old index entries may have neither version field. Their exact source
        # and original-file hash still pin the documented demo policy; an
        # explicit conflicting version is never accepted.
        returned_version = item.get("version")
        if item.get("file_hash") != source.file_hash or (
            returned_version is not None and returned_version != source.rag_metadata_version
        ):
            return _result("NEEDS_REVIEW", "政策来源或版本已变化，需要重新核对规则。", days_since_delivery=days)
        if name in expected:
            found.add(name)
    if found != set(expected):
        return _result("POLICY_UNAVAILABLE", "未检索到该申请路径所需的完整政策依据。", days_since_delivery=days)

    context = {
        "policy_type": rule.rule_id,
        "days_since_delivery": days,
        "policy_deadline_days": rule.deadline_days,
        "sources": list(expected),
        "policy_versions": {name: source.document_version_label for name, source in expected.items()},
        "policy_file_hashes": {name: source.file_hash for name, source in expected.items()},
        "product_name": order_item.get("product_name"),
        "after_sale_type": after_sale_type,
        "reason": reason,
        "evidence": [
            {
                "source": name,
                "version": DEMO_POLICY_SOURCES[name].document_version_label,
                "file_hash": DEMO_POLICY_SOURCES[name].file_hash,
                "content": item["content"],
            }
            for item in evidence
            if (name := PurePath(str(item.get("source") or "").replace("\\", "/")).name) in expected
        ],
    }
    if rule.deadline_days is None:
        return _result("NEEDS_REVIEW", "保修期限需按商品页面和保修凭证核实，不能自动判断维修资格。", **context)
    if days == rule.deadline_days and not rule.deadline_boundary_confirmed:
        return _result("NEEDS_REVIEW", "政策未明确截止日的精确时间边界，需要人工核实。", **context)
    if days > rule.deadline_days:
        if reason_kind == "quality":
            return _result("NEEDS_REVIEW", "已超出本演示规则的质量问题申请期，其他权利或保修途径需人工核实。", **context)
        return _result("INELIGIBLE", "已超出本演示规则的无理由退货路径；其他适用政策可咨询人工客服。", **context)
    if rule.requires_item_condition_verification and not item_condition_verified:
        return _result("NEEDS_REVIEW", "期限内，但商品完好、配件及商品页面限制尚待核实。", **context)
    return _result(
        "ELIGIBLE_TO_APPLY",
        "符合当前已核对演示规则的申请期限，可提出申请；商品状况与最终处理仍需检测和审核。",
        **context,
    )
