"""Pinned, demonstration-only policy facts; not a merchant approval policy.

Hashes are MD5 of the seven original UTF-8 policy files, matching the RAG
upload API's ``file_hash``.  A changed/re-uploaded document must be reviewed
and this manifest updated deliberately; a matching filename is insufficient.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PolicySource:
    filename: str
    file_hash: str
    document_version_label: str
    rag_metadata_version: str
    purpose: str


DEMO_POLICY_SOURCES = {
    "保修政策.txt": PolicySource(
        "保修政策.txt", "993065f72d8d4d7ad4acdca827f11b51",
        "演示版 v1.0", "v1", "保修咨询；无统一保修期限",
    ),
    "电子产品售后规则.txt": PolicySource(
        "电子产品售后规则.txt", "07632f4bacca0cb27d294ed1d0fad448",
        "演示版 v1.0", "v1", "手机、平板、耳机、电脑的申请条件",
    ),
    "耳机类商品规则.txt": PolicySource(
        "耳机类商品规则.txt", "b7a520517d33f5c79c3dd48a3c4e037f",
        "演示版 v1.0", "v1", "AirPods 与其他耳机专项申请条件",
    ),
    "换货流程.txt": PolicySource(
        "换货流程.txt", "3e33ac04a78ea86bf67268f41c859476",
        "演示版 v1.0", "v1", "质量问题换货申请及后续核验",
    ),
    "退货退款政策.txt": PolicySource(
        "退货退款政策.txt", "4ccddbe6514a5350c12c0a8aa649d320",
        "演示版 v1.0", "v1", "普通商品及符合条件电子产品退货通则；不单独扩展品类",
    ),
    "退款到账规则.txt": PolicySource(
        "退款到账规则.txt", "73a17ce0d5782bbf27c500b9849e1d93",
        "演示版 v1.0", "v1", "审核后到账说明；不是申请资格规则",
    ),
    "质量问题售后规则.txt": PolicySource(
        "质量问题售后规则.txt", "5bbed93c440e9c9efa31fcb2ac8e51b1",
        "演示版 v1.0", "v1", "可核实质量问题的申请通则；不单独扩展品类",
    ),
}


@dataclass(frozen=True)
class DemoPolicyRule:
    rule_id: str
    categories: tuple[str, ...]
    application_type: str
    reason_kind: str
    deadline_days: int | None
    required_sources: tuple[str, ...]
    requires_item_condition_verification: bool = False
    requires_review_after_application: bool = True
    # The documents say "within 7/30 days from delivery" but do not specify
    # the exact final instant.  The deadline day itself cannot auto-pass/fail.
    deadline_boundary_confirmed: bool = False


DEMO_POLICY_RULES = (
    DemoPolicyRule(
        "earphone_no_reason", ("耳机",), "return_refund", "no_reason", 7,
        ("耳机类商品规则.txt",), True,
    ),
    DemoPolicyRule(
        "electronics_no_reason", ("手机", "平板", "电脑"),
        "return_refund", "no_reason", 7, ("电子产品售后规则.txt",), True,
    ),
    DemoPolicyRule(
        "earphone_quality_return", ("耳机",), "return_refund", "quality", 30,
        ("耳机类商品规则.txt",),
    ),
    DemoPolicyRule(
        "electronics_quality_return", ("手机", "平板", "电脑"),
        "return_refund", "quality", 30, ("电子产品售后规则.txt",),
    ),
    DemoPolicyRule(
        "earphone_quality_exchange", ("耳机",), "exchange", "quality", 30,
        ("耳机类商品规则.txt",),
    ),
    DemoPolicyRule(
        "electronics_quality_exchange", ("手机", "平板", "电脑"),
        "exchange", "quality", 30,
        ("电子产品售后规则.txt", "换货流程.txt"),
    ),
    DemoPolicyRule(
        "electronics_repair_guidance", ("耳机", "手机", "平板", "电脑"),
        "repair", "quality", None, ("保修政策.txt",),
    ),
)
