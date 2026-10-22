from typing import Literal

from pydantic import BaseModel, Field


class BusinessParams(BaseModel):

    product_name: str | None = Field(
        default=None,
        description="用户明确提到的商品名称"
    )

    order_no: str | None = Field(
        default=None,
        description="用户明确提供的订单业务编号"
    )

    after_sale_no: str | None = Field(
        default=None,
        description="用户明确提供的售后业务编号"
    )

    after_sale_type: Literal[
        "refund",
        "return_refund",
        "exchange",
        "repair"
    ] | None = Field(
        default=None,
        description=(
            "用户明确表达的售后类型："
            "refund仅退款，"
            "return_refund退货退款，"
            "exchange换货，"
            "repair维修"
        )
    )

    reason: str | None = Field(
        default=None,
        description="用户明确表达的售后原因"
    )

    # Server-filled after extraction; never sourced from an LLM field.
    original_reason_text: str | None = Field(default=None, exclude=True)


class IntentResult(BaseModel):
    """
    用户业务意图识别结果。
    """

    intent: Literal[
        "query_order_detail",
        "query_after_sale_list",
        "query_after_sale_detail",
        "query_after_sale_history",
        "create_after_sale",
        "query_policy",
        "unknown"
    ] = Field(
        description="用户当前的业务意图"
    )

class AgentInputAnalysis(BaseModel):
    """
    Agent 输入分析结果。
    """

    intent: str

    params: BusinessParams

class CreateAfterSalePreview(BaseModel):
    """
    创建售后前的操作预览。
    """

    user_id: int

    order_no: str

    order_item_id: int

    product_name: str

    after_sale_type: Literal[
        "refund",
        "return_refund",
        "exchange",
        "repair"
    ]

    reason: str

    policy_reason_text: str | None = None

    # 仅用于展示与下阶段执行前复核；不能作为最终批准依据。
    policy: dict | None = None

    # 服务端生成并持久化到 preview_json；客户端不能提交此字段参与执行。
    policy_snapshot: dict | None = None


class AgentChatRequest(BaseModel):
    user_id: int | None = Field(
        default=None, description="旧客户端兼容字段；身份以 Bearer Token 为准", deprecated=True
    )

    message: str = Field(
        description="用户自然语言输入"
    )

    conversation_id: int | None = Field(
        default=None, description="可选会话 ID；必须属于当前 Token 用户"
    )

class AgentResponse(BaseModel):

    success: bool

    code: str

    message: str

    data: object | None = None

    trace_id: str | None = None

class AgentConfirmRequest(BaseModel):

    user_id: int | None = Field(default=None, deprecated=True)

    confirmation_id: str

    conversation_id: int | None = None
