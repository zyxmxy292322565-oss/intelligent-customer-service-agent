from langchain_core.tools import tool

from services.after_sale_service import (
    get_user_after_sales_service,
    get_after_sale_detail_service,
    get_after_sale_history_service
)


@tool
def get_user_after_sales_tool(
    user_id: int
) -> dict:
    """
    查询指定用户的全部售后申请列表。

    当用户没有提供具体售后编号，
    但想查询自己的售后申请、最近售后、
    某个商品对应的售后申请时使用。

    参数：
    user_id：用户ID
    """

    result = get_user_after_sales_service(
        user_id=user_id
    )

    return result


@tool
def get_after_sale_detail_tool(
    user_id: int,
    after_sale_no: str
) -> dict:
    """
    查询指定用户某一条售后申请的详细信息。

    当用户已经提供具体售后编号，
    并询问当前状态、售后类型、
    退款金额、商品信息等内容时使用。

    参数：
    user_id：用户ID
    after_sale_no：售后业务编号
    """

    result = get_after_sale_detail_service(
        user_id=user_id,
        after_sale_no=after_sale_no
    )

    return result


@tool
def get_after_sale_history_tool(
    user_id: int,
    after_sale_no: str
) -> dict:
    """
    查询指定用户某一条售后申请的完整状态变化历史。

    当用户已经提供具体售后编号，
    并询问售后处理过程、状态变化记录、
    什么时候进入某个状态等历史信息时使用。

    参数：
    user_id：用户ID
    after_sale_no：售后业务编号
    """

    result = get_after_sale_history_service(
        user_id=user_id,
        after_sale_no=after_sale_no
    )

    return result