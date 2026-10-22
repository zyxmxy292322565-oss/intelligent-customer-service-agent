from typing import Literal

from pydantic import BaseModel, Field


class CreateAfterSaleRequest(BaseModel):
    user_id: int | None = Field(
        default=None,
        gt=0,
        description="旧客户端兼容字段；身份以 Bearer Token 为准",
        deprecated=True
    )

    order_no: str = Field(
        min_length=1,
        max_length=64,
        description="订单业务编号"
    )

    order_item_id: int = Field(
        gt=0,
        description="订单商品ID"
    )

    type: Literal[
        "refund",
        "return_refund",
        "exchange",
        "repair"
    ]

    reason: str = Field(
        min_length=2,
        max_length=500,
        description="售后原因"
    )

class UpdateAfterSaleStatusRequest(BaseModel):
    new_status: Literal[
        "processing",
        "approved",
        "rejected",
        "completed"
    ]
