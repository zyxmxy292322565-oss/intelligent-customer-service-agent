from langchain_core.tools import tool

from services.order_service import (
    get_order_detail_service
)


@tool
def get_order_detail_tool(
    user_id: int,
    order_no: str
) -> dict:
    """
    查询指定用户的一条具体订单详情。

    当用户询问某个具体订单的信息时使用，
    例如订单状态、商品名称、订单金额等。

    参数：
    user_id：用户ID
    order_no：订单业务编号
    """

    result = get_order_detail_service(
        user_id=user_id,
        order_no=order_no
    )

    if result is None:
        return {
            "success": False,
            "code": "ORDER_NOT_FOUND",
            "message": "没有找到该订单"
        }

    return {
        "success": True,
        "code": "SUCCESS",
        "message": "订单查询成功",
        "data": result
    }