from db.connection import get_connection
from db.repository import (
    get_user_by_id,
    get_orders_by_user_id,
    get_order_items_by_order_ids,
    get_order_by_order_no_and_user_id,
    get_order_items_by_order_id
)

def get_user_orders_service(user_id: int):
    """
    查询用户以及该用户的全部订单和订单商品。

    如果用户不存在，返回 None。
    """

    connection = get_connection()
    cursor = connection.cursor()

    try:
        user = get_user_by_id(
            cursor,
            user_id
        )

        if user is None:
            return None

        orders = get_orders_by_user_id(
            cursor,
            user_id
        )

        order_ids = [
            order["id"]
            for order in orders
        ]

        all_items = get_order_items_by_order_ids(
            cursor,
            order_ids
        )

        items_by_order = {}

        for item in all_items:
            order_id = item["order_id"]

            if order_id not in items_by_order:
                items_by_order[order_id] = []

            items_by_order[order_id].append(item)

        for order in orders:
            order["items"] = items_by_order.get(
                order["id"],
                []
            )

        return {
            "user": user,
            "order_count": len(orders),
            "orders": orders
        }

    finally:
        cursor.close()
        connection.close()

def get_order_detail_service(
    user_id: int,
    order_no: str
):
    """
    根据用户ID和订单业务编号，
    查询具体订单以及订单中的商品。

    如果订单不存在或不属于当前用户，
    返回 None。
    """

    connection = get_connection()
    cursor = connection.cursor()

    try:
        order = get_order_by_order_no_and_user_id(
            cursor,
            user_id,
            order_no
        )

        if order is None:
            return None

        items = get_order_items_by_order_id(
            cursor,
            order["id"]
        )

        order["items"] = items

        return order

    finally:
        cursor.close()
        connection.close()