"""Insert an isolated, repeatable set of DEMO records into the local Docker DB.

Run with --apply. Without it, the script only checks for existing DEMO markers.
No existing rows are updated or deleted. All inserts use one transaction.
"""

import argparse
import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import pymysql
from dotenv import dotenv_values


DB_NAME = "consumer_service_agent"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def as_json(value):
    return json.dumps(value, ensure_ascii=False)


def insert(cursor, counts, table, fields, values):
    names = ", ".join(f"`{field}`" for field in fields)
    placeholders = ", ".join(["%s"] * len(fields))
    cursor.execute(
        f"INSERT INTO `{table}` ({names}) VALUES ({placeholders})",
        values,
    )
    counts[table] += 1
    return cursor.lastrowid


def make_preview(user_id, order_no, item_id, product, sale_type, reason):
    return {
        "user_id": user_id,
        "order_no": order_no,
        "order_item_id": item_id,
        "product_name": product,
        "after_sale_type": sale_type,
        "reason": reason,
    }


def add_trace(cursor, counts, task_id, trace_id, steps, failed_at=None):
    for index, step_name in enumerate(steps):
        failed = step_name == failed_at
        insert(
            cursor, counts, "agent_task_steps",
            ("task_id", "trace_id", "step_name", "input_json", "output_json",
             "status", "error_message", "latency_ms"),
            (task_id, trace_id, step_name,
             as_json({"demo": True, "step": step_name}),
             as_json({"code": "PRODUCT_NOT_FOUND" if failed else "SUCCESS"}),
             "FAILED" if failed else "SUCCESS",
             "DEMO 商品不存在" if failed else None,
             float(12 + index * 7)),
        )


def preflight(cursor):
    checks = (
        ("users", "username", ("demo_user_001", "demo_user_002")),
        ("orders", "order_no", ("DEMO_ORD_%",)),
        ("after_sales", "after_sale_no", ("DEMO_AS_%",)),
        ("conversations", "title", ("DEMO %",)),
        ("agent_tasks", "task_no", ("DEMO_TASK_%",)),
        ("agent_pending_actions", "confirmation_id", ("DEMO_CONF_%",)),
    )
    collisions = []
    for table, field, values in checks:
        if len(values) == 1:
            cursor.execute(
                f"SELECT COUNT(*) FROM `{table}` WHERE `{field}` LIKE %s",
                values,
            )
        else:
            cursor.execute(
                f"SELECT COUNT(*) FROM `{table}` WHERE `{field}` IN (%s, %s)",
                values,
            )
        count = cursor.fetchone()[0]
        if count:
            collisions.append(f"{table}: {count}")
    if collisions:
        raise RuntimeError(
            "Existing DEMO markers found; refusing to change existing data: "
            + ", ".join(collisions)
        )


def seed(cursor):
    counts = Counter()
    now = datetime.now()

    user1 = insert(cursor, counts, "users", ("username", "phone"),
                   ("demo_user_001", None))
    user2 = insert(cursor, counts, "users", ("username", "phone"),
                   ("demo_user_002", None))

    catalog = (
        ("AIRPODS", user1, "AirPods Pro 2", "耳机", 1899, 20),
        ("WATCH", user1, "智能手表", "智能穿戴", 999, 5),
        ("PHONE", user2, "手机", "手机", 3999, 2),
        ("KEYBOARD", user1, "机械键盘", "电脑配件", 699, 4),
        ("TABLET", user1, "平板电脑", "平板", 2599, 1),
    )
    orders = {}
    for name, owner, product, category, price, days_ago in catalog:
        order_no = f"DEMO_ORD_{name}_001"
        delivered = now - timedelta(days=days_ago)
        order_id = insert(
            cursor, counts, "orders",
            ("order_no", "user_id", "total_amount", "status", "paid_at", "delivered_at"),
            (order_no, owner, price, "delivered", delivered - timedelta(days=2), delivered),
        )
        item_id = insert(
            cursor, counts, "order_items",
            ("order_id", "product_name", "category", "quantity", "unit_price"),
            (order_id, product, category, 1, price),
        )
        orders[name] = (order_id, item_id, order_no, product, price, owner)

    conversation_specs = (
        ("AIRPODS", user1, "DEMO AirPods政策咨询"),
        ("WATCH", user1, "DEMO 智能手表售后与类型修改"),
        ("KEYBOARD", user1, "DEMO 机械键盘取消售后"),
        ("TABLET", user1, "DEMO 平板预览与人工转接"),
        ("PHONE", user2, "DEMO 手机质量售后与客服回复"),
    )
    conversations = {
        name: insert(cursor, counts, "conversations", ("user_id", "title", "status"),
                     (owner, title, "active"))
        for name, owner, title in conversation_specs
    }

    histories = {
        "AIRPODS": (
            ("user", "AirPods用了20天还能退吗？"),
            ("assistant", "无理由退货通常限签收后7天；若存在质量问题，可按耳机类政策申请30天内售后，保修仍需按政策核验。"),
        ),
        "WATCH": (
            ("user", "我的智能手表有问题，想申请退货退款。"),
            ("assistant", "已记录智能手表售后申请。"),
            ("user", "我想改成维修。"),
            ("human_agent", "您好，我可以协助您核对维修方案；请先确认当前申请状态。"),
        ),
        "KEYBOARD": (
            ("user", "机械键盘质量不好，我想退货。"),
            ("assistant", "已记录机械键盘售后申请。"),
            ("user", "我不想退了，取消售后。"),
            ("assistant", "这笔机械键盘售后已取消。"),
        ),
        "TABLET": (
            ("user", "平板电脑质量不好，我想退货退款。"),
            ("assistant", "已生成平板电脑售后预览，等待您确认。"),
        ),
        "PHONE": (
            ("user", "我的手机有质量问题，需要维修。"),
            ("assistant", "已提交手机维修售后申请。"),
            ("user", "请转人工客服。"),
            ("human_agent", "您好，已收到您的手机售后问题，我们会继续协助处理。"),
        ),
    }
    for name, messages in histories.items():
        for role, content in messages:
            insert(cursor, counts, "conversation_messages",
                   ("conversation_id", "role", "content"),
                   (conversations[name], role, content))

    task_specs = (
        ("WATCH", "completed", "execute", "为智能手表创建售后申请", "return_refund", "智能手表质量问题"),
        ("PHONE", "completed", "execute", "为手机创建维修申请", "repair", "手机质量问题"),
        ("KEYBOARD", "completed", "execute", "为机械键盘创建售后申请", "return_refund", "机械键盘质量问题"),
        ("TABLET", "waiting_confirm", "waiting_confirm", "为平板电脑创建售后预览", "return_refund", "平板电脑质量问题"),
    )
    tasks = {}
    for name, status, current_step, goal, sale_type, reason in task_specs:
        order_id, item_id, order_no, product, price, owner = orders[name]
        task_id = insert(
            cursor, counts, "agent_tasks",
            ("task_no", "user_id", "conversation_id", "task_type", "goal",
             "status", "current_step", "context"),
            (f"DEMO_TASK_{name}_001", owner, conversations[name], "agent", goal,
             status, current_step,
             as_json({"product_name": product, "after_sale_type": sale_type,
                      "reason": reason})),
        )
        tasks[name] = task_id
        preview = make_preview(owner, order_no, item_id, product, sale_type, reason)
        confirmation_id = f"DEMO_CONF_{name}_CREATE_001"
        if name == "TABLET":
            result, confirmed_at, executed_at, action_status = None, None, None, "pending"
        else:
            sale_no = f"DEMO_AS_{name}_001"
            sale_status = {"WATCH": "pending", "PHONE": "executed", "KEYBOARD": "cancelled"}[name]
            result = {
                "success": True, "code": "SUCCESS", "message": "售后申请创建成功",
                "data": {"after_sale_no": sale_no, "user_id": owner,
                         "order_no": order_no, "order_item_id": item_id,
                         "type": sale_type, "status": "pending",
                         "refund_amount": price if sale_type == "return_refund" else None},
            }
            confirmed_at, executed_at, action_status = now, now, "executed"
        insert(
            cursor, counts, "agent_pending_actions",
            ("confirmation_id", "user_id", "conversation_id", "task_id", "action_type",
             "preview_json", "status", "execution_result_json", "confirmed_at", "executed_at"),
            (confirmation_id, owner, conversations[name], task_id, "create_after_sale",
             as_json(preview), action_status, as_json(result) if result else None,
             confirmed_at, executed_at),
        )
        if name != "TABLET":
            sale_id = insert(
                cursor, counts, "after_sales",
                ("after_sale_no", "idempotency_key", "user_id", "order_id",
                 "order_item_id", "type", "reason", "status", "refund_amount"),
                (sale_no, confirmation_id, owner, order_id, item_id, sale_type,
                 reason, sale_status, price if sale_type == "return_refund" else None),
            )
            insert(cursor, counts, "after_sale_status_logs",
                   ("after_sale_id", "from_status", "to_status", "operator_type", "operator_id", "remark"),
                   (sale_id, None, "pending", "user", str(owner), "DEMO 创建售后"))
            if sale_status != "pending":
                insert(cursor, counts, "after_sale_status_logs",
                       ("after_sale_id", "from_status", "to_status", "operator_type", "operator_id", "remark"),
                       (sale_id, "pending", sale_status, "system" if sale_status == "executed" else "user",
                        str(owner), "DEMO 状态演示"))
        steps = ["input_analysis", "context_merge", "resolve_order", "build_preview",
                 "create_pending_action"]
        if name != "TABLET":
            steps += ["confirm", "execute"]
        add_trace(cursor, counts, task_id, f"DEMO_TRACE_{name}_001", steps)

    failed_task = insert(
        cursor, counts, "agent_tasks",
        ("task_no", "user_id", "conversation_id", "task_type", "goal",
         "status", "current_step", "context"),
        ("DEMO_TASK_FAILED_001", user1, conversations["AIRPODS"], "agent",
         "DEMO 查询不存在的商品", "failed", "resolve_order",
         as_json({"product_name": "DEMO 不存在的商品"})),
    )
    add_trace(cursor, counts, failed_task, "DEMO_TRACE_FAILED_001",
              ["input_analysis", "context_merge", "resolve_order"], "resolve_order")

    for name, status, reason in (
        ("TABLET", "waiting", "DEMO 用户请求人工确认平板售后"),
        ("WATCH", "assigned", "DEMO 智能手表售后类型修改咨询"),
        ("PHONE", "completed", "DEMO 手机质量售后咨询已处理"),
    ):
        insert(cursor, counts, "handoff_task",
               ("user_id", "conversation_id", "reason", "status"),
               (orders[name][5], conversations[name], reason, status))

    return counts, {"demo_user_001": user1, "demo_user_002": user2}, conversations


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="commit DEMO inserts")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3308)
    args = parser.parse_args()
    password = dotenv_values(PROJECT_ROOT / ".env").get("MYSQL_ROOT_PASSWORD")
    if not password:
        raise RuntimeError("MYSQL_ROOT_PASSWORD is missing from .env")
    connection = pymysql.connect(
        host=args.host, port=args.port, user="root", password=password,
        database=DB_NAME, charset="utf8mb4", autocommit=False,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT DATABASE()")
            if cursor.fetchone()[0] != DB_NAME:
                raise RuntimeError("Connected to an unexpected database")
            preflight(cursor)
            if not args.apply:
                print("Preflight passed; no rows inserted. Rerun with --apply to seed.")
                return
            counts, users, conversations = seed(cursor)
        connection.commit()
        print("Committed DEMO records:", dict(sorted(counts.items())))
        print("Demo users:", users)
        print("Demo conversations:", conversations)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    main()
