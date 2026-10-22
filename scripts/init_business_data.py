"""Reset only consumer_service_agent rows and seed acceptance scenarios.

Dry-run by default. Use --apply --include-agent-sessions after reviewing the
reported counts. The delete-and-insert operation is a single InnoDB transaction.
If the target users and all five target orders already exist, reruns are no-ops.
"""

import argparse
import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pymysql
from dotenv import dotenv_values


DB_NAME = "consumer_service_agent"
ROOT = Path(__file__).resolve().parents[1]
ORDER_NOS = tuple(f"ORD20261014000{number}" for number in range(1, 6))
DELETE_ORDER = (
    "agent_task_steps",
    "after_sale_status_logs",
    "conversation_messages",
    "handoff_task",
    "agent_pending_actions",
    "agent_sessions",
    "after_sales",
    "agent_tasks",
    "order_items",
    "orders",
    "conversations",
    "users",
)


def json_value(value):
    return json.dumps(value, ensure_ascii=False)


def insert(cursor, added, table, values):
    columns = ", ".join(f"`{column}`" for column in values)
    placeholders = ", ".join(["%s"] * len(values))
    cursor.execute(
        f"INSERT INTO `{table}` ({columns}) VALUES ({placeholders})",
        tuple(values.values()),
    )
    added[table] += 1
    return cursor.lastrowid


def new_id(prefix):
    return prefix + uuid4().hex[:16].upper()


def current_counts(cursor):
    counts = {}
    for table in DELETE_ORDER:
        cursor.execute(f"SELECT COUNT(*) AS n FROM `{table}`")
        counts[table] = cursor.fetchone()["n"]
    return counts


def initialization_state(cursor):
    cursor.execute(
        "SELECT id, username, phone FROM users WHERE username IN (%s, %s)",
        ("zhangsan", "lisi"),
    )
    users = {row["username"]: row for row in cursor.fetchall()}
    cursor.execute(
        "SELECT order_no FROM orders WHERE order_no IN (%s, %s, %s, %s, %s)",
        ORDER_NOS,
    )
    orders = {row["order_no"] for row in cursor.fetchall()}
    if not users and not orders:
        return "new"
    if (
        set(users) == {"zhangsan", "lisi"}
        and users["zhangsan"]["phone"] == "13800000001"
        and users["lisi"]["phone"] == "13800000002"
        and orders == set(ORDER_NOS)
    ):
        return "initialized"
    raise RuntimeError(
        "Target usernames/order numbers partially exist or conflict; "
        "refusing to overwrite them. Inspect the database manually."
    )


def add_messages(cursor, added, conversation_id, messages):
    for role, content in messages:
        insert(cursor, added, "conversation_messages", {
            "conversation_id": conversation_id,
            "role": role,
            "content": content,
        })


def add_trace(cursor, added, task_id, steps, failed_at=None):
    trace_id = new_id("TRACE_")
    for index, step_name in enumerate(steps):
        failed = step_name == failed_at
        insert(cursor, added, "agent_task_steps", {
            "task_id": task_id,
            "trace_id": trace_id,
            "step_name": step_name,
            "status": "FAILED" if failed else "SUCCESS",
            "input_json": json_value({"step": step_name}),
            "output_json": json_value({"code": "PRODUCT_NOT_FOUND" if failed else "SUCCESS"}),
            "error_message": "未找到对应商品订单" if failed else None,
            "latency_ms": float(12 + index * 8),
        })


def add_task(cursor, added, user_id, conversation_id, goal, status, context, step):
    return insert(cursor, added, "agent_tasks", {
        "task_no": new_id("TASK_"),
        "user_id": user_id,
        "conversation_id": conversation_id,
        "task_type": "agent",
        "goal": goal,
        "status": status,
        "current_step": step,
        "context": json_value(context),
    })


def make_preview(user_id, order, sale_type, reason):
    return {
        "user_id": user_id,
        "order_no": order["order_no"],
        "order_item_id": order["item_id"],
        "product_name": order["product_name"],
        "after_sale_type": sale_type,
        "reason": reason,
    }


def add_action(cursor, added, user_id, conversation_id, task_id, preview,
               status, result=None):
    confirmation_id = new_id("CONF_")
    insert(cursor, added, "agent_pending_actions", {
        "confirmation_id": confirmation_id,
        "user_id": user_id,
        "conversation_id": conversation_id,
        "task_id": task_id,
        "action_type": "create_after_sale",
        "preview_json": json_value(preview),
        "status": status,
        "execution_result_json": json_value(result) if result else None,
        "confirmed_at": datetime.now() if status == "executed" else None,
        "executed_at": datetime.now() if status == "executed" else None,
    })
    return confirmation_id


def add_sale(cursor, added, user_id, conversation_id, order, sale_type,
             reason, sale_status):
    context = {
        "product_name": order["product_name"],
        "after_sale_type": sale_type,
        "reason": reason,
    }
    task_id = add_task(
        cursor, added, user_id, conversation_id,
        f"为{order['product_name']}创建售后申请", "completed", context, "execute",
    )
    sale_no = new_id("AS")
    refund_amount = order["price"] if sale_type in {"refund", "return_refund"} else None
    service_result = {
        "success": True,
        "code": "SUCCESS",
        "message": "售后申请创建成功",
        "data": {
            "after_sale_no": sale_no,
            "user_id": user_id,
            "order_no": order["order_no"],
            "order_item_id": order["item_id"],
            "type": sale_type,
            "status": "pending",
            "refund_amount": refund_amount,
        },
    }
    confirmation_id = add_action(
        cursor, added, user_id, conversation_id, task_id,
        make_preview(user_id, order, sale_type, reason),
        "executed", service_result,
    )
    sale_id = insert(cursor, added, "after_sales", {
        "after_sale_no": sale_no,
        "idempotency_key": confirmation_id,
        "user_id": user_id,
        "order_id": order["order_id"],
        "order_item_id": order["item_id"],
        "type": sale_type,
        "reason": reason,
        "status": sale_status,
        "refund_amount": refund_amount,
    })
    insert(cursor, added, "after_sale_status_logs", {
        "after_sale_id": sale_id,
        "from_status": None,
        "to_status": "pending",
        "operator_type": "user",
        "operator_id": str(user_id),
        "remark": "创建售后申请",
    })
    if sale_status != "pending":
        insert(cursor, added, "after_sale_status_logs", {
            "after_sale_id": sale_id,
            "from_status": "pending",
            "to_status": sale_status,
            "operator_type": "user" if sale_status == "cancelled" else "system",
            "operator_id": str(user_id),
            "remark": "售后状态演示",
        })
    add_trace(cursor, added, task_id, (
        "input_analysis", "context_merge", "resolve_order",
        "build_preview", "create_pending_action", "confirm", "execute",
    ))
    return sale_no


def seed(cursor):
    added = Counter()
    insert(cursor, added, "users", {
        "id": 1, "username": "zhangsan", "phone": "13800000001",
    })
    insert(cursor, added, "users", {
        "id": 2, "username": "lisi", "phone": "13800000002",
    })
    zhangsan, lisi = 1, 2

    catalog = (
        ("airpods", zhangsan, ORDER_NOS[0], "AirPods Pro 2", "耳机", 1899, 20),
        ("watch", zhangsan, ORDER_NOS[1], "智能手表", "智能穿戴", 999, 5),
        ("keyboard", zhangsan, ORDER_NOS[2], "机械键盘", "电脑配件", 699, 4),
        ("phone", lisi, ORDER_NOS[3], "手机", "手机", 3999, 2),
        ("tablet", lisi, ORDER_NOS[4], "平板电脑", "平板", 2599, 1),
    )
    orders = {}
    for key, user_id, order_no, product, category, price, days_ago in catalog:
        delivered = datetime.now() - timedelta(days=days_ago)
        order_id = insert(cursor, added, "orders", {
            "order_no": order_no,
            "user_id": user_id,
            "total_amount": price,
            "status": "delivered",
            "paid_at": delivered - timedelta(days=2),
            "delivered_at": delivered,
        })
        item_id = insert(cursor, added, "order_items", {
            "order_id": order_id,
            "product_name": product,
            "category": category,
            "quantity": 1,
            "unit_price": price,
        })
        orders[key] = {
            "order_id": order_id, "item_id": item_id, "order_no": order_no,
            "product_name": product, "price": price,
        }

    conversation_specs = (
        ("policy", zhangsan, "AirPods 政策咨询"),
        ("application", zhangsan, "AirPods 售后申请"),
        ("watch", zhangsan, "智能手表售后处理"),
        ("keyboard", zhangsan, "机械键盘取消记录"),
        ("phone", lisi, "手机售后与人工客服"),
        ("tablet", lisi, "平板电脑待确认申请"),
        ("clarify", zhangsan, "补充售后参数"),
        ("failed", lisi, "商品查询失败记录"),
    )
    conversations = {
        key: insert(cursor, added, "conversations", {
            "user_id": owner, "title": title, "status": "active",
        })
        for key, owner, title in conversation_specs
    }
    add_messages(cursor, added, conversations["policy"], (
        ("user", "AirPods用了20天还能退吗？"),
        ("assistant", "无理由退货通常限签收后7天；如有可核实的质量问题，可申请质量售后，具体以检测和审核为准。"),
    ))
    add_messages(cursor, added, conversations["application"], (
        ("user", "我想了解AirPods Pro 2售后申请流程。"),
        ("assistant", "请说明售后类型与具体原因，确认预览后才会创建申请。"),
    ))
    add_messages(cursor, added, conversations["watch"], (
        ("user", "智能手表有质量问题，我想退货退款。"),
        ("assistant", "已记录智能手表售后申请，可在原会话申请修改类型或取消。"),
        ("user", "我要人工客服。"),
        ("human_agent", "您好，我是人工客服，请问有什么可以帮助您？"),
    ))
    add_messages(cursor, added, conversations["keyboard"], (
        ("user", "机械键盘质量不好，我想退货。"),
        ("assistant", "机械键盘售后申请已取消。"),
    ))
    add_messages(cursor, added, conversations["phone"], (
        ("user", "我的手机存在质量问题，申请退货退款。"),
        ("assistant", "已记录手机售后处理结果。"),
        ("user", "我要人工客服。"),
        ("human_agent", "您好，我是人工客服，请问有什么可以帮助您？"),
    ))
    add_messages(cursor, added, conversations["tablet"], (
        ("user", "平板电脑质量不好，我想申请退货退款。"),
        ("assistant", "售后方案已生成，请确认后提交。"),
    ))
    add_messages(cursor, added, conversations["clarify"], (
        ("user", "我要申请售后。"),
        ("assistant", "请补充商品、售后类型和具体原因。"),
    ))
    add_messages(cursor, added, conversations["failed"], (
        ("user", "我要为不存在的商品申请售后。"),
        ("assistant", "没有找到对应的商品订单，请核对商品名称。"),
    ))

    add_sale(cursor, added, zhangsan, conversations["watch"], orders["watch"],
             "return_refund", "质量问题", "pending")
    add_sale(cursor, added, zhangsan, conversations["keyboard"], orders["keyboard"],
             "return_refund", "质量问题", "cancelled")
    add_sale(cursor, added, lisi, conversations["phone"], orders["phone"],
             "return_refund", "质量问题", "executed")

    tablet_context = {
        "product_name": "平板电脑", "after_sale_type": "return_refund",
        "reason": "质量问题",
    }
    tablet_task = add_task(
        cursor, added, lisi, conversations["tablet"],
        "为平板电脑创建售后预览", "waiting_confirm", tablet_context,
        "waiting_confirm",
    )
    tablet_confirmation = add_action(
        cursor, added, lisi, conversations["tablet"], tablet_task,
        make_preview(lisi, orders["tablet"], "return_refund", "质量问题"),
        "pending",
    )
    insert(cursor, added, "agent_sessions", {
        "user_id": lisi,
        "current_confirmation_id": tablet_confirmation,
        "status": "active",
    })
    add_trace(cursor, added, tablet_task, (
        "input_analysis", "context_merge", "resolve_order",
        "build_preview", "create_pending_action",
    ))

    waiting_task = add_task(
        cursor, added, zhangsan, conversations["clarify"],
        "补充售后申请参数", "waiting_input", {}, "waiting_input",
    )
    add_trace(cursor, added, waiting_task, ("input_analysis", "context_merge"))
    failed_task = add_task(
        cursor, added, lisi, conversations["failed"],
        "查询不存在的商品", "failed", {"product_name": "不存在的商品"},
        "resolve_order",
    )
    add_trace(cursor, added, failed_task,
              ("input_analysis", "context_merge", "resolve_order"),
              failed_at="resolve_order")

    for user_id, conversation_id, status, reason in (
        (lisi, conversations["tablet"], "waiting", "咨询平板电脑售后确认"),
        (zhangsan, conversations["watch"], "assigned", "咨询智能手表售后类型修改"),
        (lisi, conversations["phone"], "completed", "咨询手机质量售后结果"),
    ):
        insert(cursor, added, "handoff_task", {
            "user_id": user_id,
            "conversation_id": conversation_id,
            "reason": reason,
            "status": status,
        })
    return added


def verify(cursor):
    counts = current_counts(cursor)
    expected = {
        "users": 2, "orders": 5, "order_items": 5, "after_sales": 3,
        "conversations": 8, "agent_pending_actions": 4, "handoff_task": 3,
    }
    for table, count in expected.items():
        if counts[table] != count:
            raise RuntimeError(f"Expected {count} rows in {table}, got {counts[table]}")
    cursor.execute("""
        SELECT COUNT(*) AS n FROM orders o JOIN order_items i ON i.order_id=o.id
        JOIN users u ON u.id=o.user_id
        WHERE o.order_no IN (%s, %s, %s, %s, %s)
    """, ORDER_NOS)
    if cursor.fetchone()["n"] != 5:
        raise RuntimeError("Order/user/item relations are incomplete")
    cursor.execute("""
        SELECT COUNT(*) AS n FROM after_sales s
        JOIN agent_pending_actions a ON a.confirmation_id=s.idempotency_key
        WHERE a.action_type='create_after_sale' AND a.user_id=s.user_id
          AND a.conversation_id IS NOT NULL
    """)
    if cursor.fetchone()["n"] != 3:
        raise RuntimeError("After-sale origin actions are incomplete")
    cursor.execute("""
        SELECT COUNT(*) AS n FROM agent_pending_actions a
        JOIN order_items i ON i.id=CAST(
            JSON_UNQUOTE(JSON_EXTRACT(a.preview_json, '$.order_item_id')) AS UNSIGNED
        )
        WHERE a.status='pending' AND i.product_name='平板电脑'
    """)
    if cursor.fetchone()["n"] != 1:
        raise RuntimeError("Tablet preview action is missing")
    cursor.execute("""
        SELECT COUNT(*) AS n FROM after_sales s
        JOIN order_items i ON i.id=s.order_item_id
        WHERE i.product_name='AirPods Pro 2'
    """)
    if cursor.fetchone()["n"]:
        raise RuntimeError("AirPods acceptance order must have no after-sale record")
    for table, field, wanted in (
        ("after_sales", "status", {"pending", "cancelled", "executed"}),
        ("agent_tasks", "status", {"completed", "failed", "waiting_input", "waiting_confirm"}),
        ("handoff_task", "status", {"waiting", "assigned", "completed"}),
        ("conversation_messages", "role", {"user", "assistant", "human_agent"}),
    ):
        cursor.execute(f"SELECT DISTINCT `{field}` AS value FROM `{table}`")
        actual = {row["value"] for row in cursor.fetchall()}
        if not wanted.issubset(actual):
            raise RuntimeError(f"{table} is missing required {field} values: {wanted - actual}")
    cursor.execute("""
        SELECT COUNT(*) AS n FROM agent_tasks t
        JOIN conversations c ON c.id=t.conversation_id
        WHERE t.user_id<>c.user_id
    """)
    if cursor.fetchone()["n"]:
        raise RuntimeError("Agent task conversation ownership mismatch")
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--include-agent-sessions", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3308)
    args = parser.parse_args()
    password = dotenv_values(ROOT / ".env").get("MYSQL_ROOT_PASSWORD")
    if not password:
        raise RuntimeError("MYSQL_ROOT_PASSWORD missing from .env")
    connection = pymysql.connect(
        host=args.host, port=args.port, user="root", password=password,
        database=DB_NAME, charset="utf8mb4", autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT DATABASE() AS db")
            if cursor.fetchone()["db"] != DB_NAME:
                raise RuntimeError("Unexpected database; refusing to proceed")
            state = initialization_state(cursor)
            if state == "initialized":
                print("Target users and five orders already exist; no changes made.")
                return
            before = current_counts(cursor)
            print("Current row counts:", before)
            if not args.apply:
                print("Dry run only. No rows deleted or inserted.")
                return
            if not args.include_agent_sessions:
                raise RuntimeError(
                    "agent_sessions references users; explicit "
                    "--include-agent-sessions is required"
                )
            deleted = Counter()
            for table in DELETE_ORDER:
                cursor.execute(f"DELETE FROM `{table}`")
                deleted[table] = cursor.rowcount
            added = seed(cursor)
            totals = verify(cursor)
        connection.commit()
        print("Deleted business rows:", dict(deleted))
        print("Inserted acceptance rows:", dict(added))
        print("Current totals:", totals)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    main()
