"""Add one clean order for a first create_after_sale demonstration.

Run from the host with: python scripts/add_create_after_sale_demo.py
The default endpoint is the development Compose MySQL mapping, 127.0.0.1:3308.
No sale, task, pending action, or conversation is created by this script.
"""

import argparse
import os
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pymysql
from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATABASE = "consumer_service_agent"
ORDER_NO = "ORD202610140006"
USER_ID = 1
PRODUCT_NAME = "无线鼠标"
AMOUNT = Decimal("199.00")


def configuration(args):
    local = dotenv_values(PROJECT_ROOT / ".env")
    database = os.getenv("MYSQL_DATABASE") or local.get("MYSQL_DATABASE") or DATABASE
    if database != DATABASE:
        raise RuntimeError(f"Refusing to write to unexpected database: {database}")

    user = os.getenv("MYSQL_APP_USER") or local.get("MYSQL_APP_USER") or "agent"
    password = os.getenv("MYSQL_APP_PASSWORD") or local.get("MYSQL_APP_PASSWORD")
    if not password:
        raise RuntimeError("MYSQL_APP_PASSWORD is required")
    return {
        "host": args.host,
        "port": args.port,
        "user": user,
        "password": password,
        "database": DATABASE,
        "charset": "utf8mb4",
        "cursorclass": pymysql.cursors.DictCursor,
        "autocommit": False,
    }


def assert_clean(cursor, order_id, order_item_id):
    cursor.execute("SELECT COUNT(*) AS n FROM after_sales WHERE order_id = %s", (order_id,))
    if cursor.fetchone()["n"]:
        raise RuntimeError("Order already has an after-sale record; refusing to treat it as clean")

    cursor.execute(
        """SELECT COUNT(*) AS n FROM agent_pending_actions
           WHERE user_id = %s AND (
             JSON_UNQUOTE(JSON_EXTRACT(preview_json, '$.order_id')) = %s OR
             JSON_UNQUOTE(JSON_EXTRACT(preview_json, '$.order_item_id')) = %s OR
             JSON_UNQUOTE(JSON_EXTRACT(preview_json, '$.order_no')) = %s OR
             JSON_UNQUOTE(JSON_EXTRACT(preview_json, '$.product_name')) = %s
           )""",
        (USER_ID, str(order_id), str(order_item_id), ORDER_NO, PRODUCT_NAME),
    )
    if cursor.fetchone()["n"]:
        raise RuntimeError("Related pending-action history exists; order is not clean")

    cursor.execute(
        """SELECT COUNT(*) AS n FROM agent_tasks
           WHERE user_id = %s AND (
             JSON_UNQUOTE(JSON_EXTRACT(context, '$.order_id')) = %s OR
             JSON_UNQUOTE(JSON_EXTRACT(context, '$.order_item_id')) = %s OR
             JSON_UNQUOTE(JSON_EXTRACT(context, '$.order_no')) = %s OR
             JSON_UNQUOTE(JSON_EXTRACT(context, '$.product_name')) = %s OR
             goal LIKE %s
           )""",
        (USER_ID, str(order_id), str(order_item_id), ORDER_NO, PRODUCT_NAME,
         f"%{PRODUCT_NAME}%"),
    )
    if cursor.fetchone()["n"]:
        raise RuntimeError("Related agent-task history exists; order is not clean")


def add_order(connection):
    connection.begin()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT id FROM users WHERE id = %s", (USER_ID,))
            if cursor.fetchone() is None:
                raise RuntimeError("User 1 does not exist")

            cursor.execute(
                "SELECT id, user_id, total_amount, status FROM orders "
                "WHERE order_no = %s FOR UPDATE",
                (ORDER_NO,),
            )
            order = cursor.fetchone()
            if order is None:
                now = datetime.now()
                cursor.execute(
                    """INSERT INTO orders
                       (order_no, user_id, total_amount, status,
                        created_at, paid_at, delivered_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (ORDER_NO, USER_ID, AMOUNT, "delivered",
                     now - timedelta(days=3), now - timedelta(days=2),
                     now - timedelta(days=1)),
                )
                order_id = cursor.lastrowid
                cursor.execute(
                    """INSERT INTO order_items
                       (order_id, product_name, category, quantity, unit_price)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (order_id, PRODUCT_NAME, "电脑配件", 1, AMOUNT),
                )
                order_item_id = cursor.lastrowid
                outcome = "created"
            else:
                if (order["user_id"] != USER_ID or
                        order["total_amount"] != AMOUNT or
                        order["status"] != "delivered"):
                    raise RuntimeError("Order number exists with different values; refusing to modify it")
                order_id = order["id"]
                cursor.execute(
                    "SELECT id, product_name, quantity, unit_price "
                    "FROM order_items WHERE order_id = %s",
                    (order_id,),
                )
                items = cursor.fetchall()
                if (len(items) != 1 or items[0]["product_name"] != PRODUCT_NAME or
                        items[0]["quantity"] != 1 or items[0]["unit_price"] != AMOUNT):
                    raise RuntimeError("Existing order items differ; refusing to modify them")
                order_item_id = items[0]["id"]
                outcome = "already_exists"

            assert_clean(cursor, order_id, order_item_id)
        connection.commit()
        return outcome, order_id, order_item_id
    except Exception:
        connection.rollback()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3308)
    args = parser.parse_args()
    connection = pymysql.connect(**configuration(args))
    try:
        outcome, order_id, order_item_id = add_order(connection)
        print(f"{outcome}: order_id={order_id}, order_no={ORDER_NO}, "
              f"order_item_id={order_item_id}, product={PRODUCT_NAME}, user_id={USER_ID}")
    finally:
        connection.close()


if __name__ == "__main__":
    main()
