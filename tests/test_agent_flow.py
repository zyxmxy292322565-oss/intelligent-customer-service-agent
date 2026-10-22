"""真实 MySQL 链路测试；仅在 RUN_AGENT_MYSQL_TESTS=1 时运行。"""

import os
import re
import threading
import time
import unittest
import uuid
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from fastapi.testclient import TestClient

import main
from agent.confirmation_service import confirm_action, expire_pending_actions
from db import connection as db_connection
from db.init_db import create_tables
from db.migrate_action_receipts import migrate_action_receipts
from db.migrate_task_execution import migrate_task_execution
from db.migrate_task_recovery import migrate_task_recovery
from schemas.agent_schema import AgentChatRequest, AgentConfirmRequest, BusinessParams, IntentResult
from services.after_sale_service import create_after_sale_service
from services.task_service import reconcile_finished_agent_tasks


@unittest.skipUnless(os.getenv("RUN_AGENT_MYSQL_TESTS") == "1", "需要显式开启真实 MySQL 测试")
class AgentFlowMySQLTests(unittest.TestCase):
    """每次运行创建随机测试库；不读取或写入项目业务库。"""

    @classmethod
    def setUpClass(cls):
        cls.database_name = "p2_agent_test_" + uuid.uuid4().hex[:12]
        cls.database_created = False
        cls.database_patch = None
        if not re.fullmatch(r"p2_agent_test_[0-9a-f]{12}", cls.database_name):
            raise RuntimeError("测试数据库名称校验失败")
        server = db_connection.get_server_connection()
        try:
            with server.cursor() as cursor:
                cursor.execute(
                    f"CREATE DATABASE `{cls.database_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
            server.commit()
            cls.database_created = True
        finally:
            server.close()
        cls.database_patch = patch.object(db_connection, "MYSQL_DATABASE", cls.database_name)
        cls.database_patch.start()
        try:
            create_tables()
            migrate_action_receipts()
            migrate_task_execution()
            migrate_task_recovery()
        except Exception:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        if cls.database_patch is not None:
            cls.database_patch.stop()
            cls.database_patch = None
        if cls.database_created:
            if not re.fullmatch(r"p2_agent_test_[0-9a-f]{12}", cls.database_name):
                raise RuntimeError("拒绝删除非测试数据库")
            server = db_connection.get_server_connection()
            try:
                with server.cursor() as cursor:
                    cursor.execute(
                        "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s",
                        (cls.database_name,),
                    )
                    if cursor.fetchone() is not None:
                        cursor.execute(f"DROP DATABASE `{cls.database_name}`")
                server.commit()
            finally:
                server.close()
            cls.database_created = False

    def setUp(self):
        self.user_id, self.order_id, self.order_item_id = self._seed_user_order("无线键盘")
        self.route_patch = patch(
            "agent.input_analyzer.route_intent",
            side_effect=lambda text: IntentResult(intent="create_after_sale"),
        )
        self.extract_patch = patch(
            "agent.input_analyzer.extract_business_params",
            side_effect=self._extract_params,
        )
        self.route_patch.start()
        self.extract_patch.start()
        self.addCleanup(self.route_patch.stop)
        self.addCleanup(self.extract_patch.stop)
        # Existing flow/state-machine tests use deliberately minimal order
        # fixtures; policy eligibility itself is tested with trusted rows in
        # test_policy_check and test_policy_workflow.
        test_policy_result = {
            "decision": "ELIGIBLE_TO_APPLY", "policy_type": "isolated_test_rule",
            "policy_message": "隔离测试中的申请路径已核验，仍需审核。",
            "days_since_delivery": 20, "policy_deadline_days": 30,
            "requires_review": True, "sources": ["隔离测试政策.txt"],
            "policy_versions": {"隔离测试政策.txt": "isolation-v1"},
            "policy_file_hashes": {"隔离测试政策.txt": "isolated"},
            "evidence": [{"source": "隔离测试政策.txt", "file_hash": "isolated",
                          "content": "仅用于隔离业务链路测试"}],
        }
        policy_patch = patch("agent.write_workflow.check_after_sale_policy",
                             return_value=test_policy_result)
        policy_patch.start()
        self.addCleanup(policy_patch.stop)
        execution_policy_patch = patch("services.policy_execution_service.check_after_sale_policy",
                                       return_value=test_policy_result)
        execution_policy_patch.start()
        self.addCleanup(execution_policy_patch.stop)

    @staticmethod
    def _extract_params(text):
        product_name = "不存在商品" if "不存在商品" in text else (
            "无线键盘" if "无线键盘" in text else None
        )
        return BusinessParams(
            product_name=product_name,
            reason="质量不好" if "质量不好" in text else None,
            after_sale_type="return_refund" if "退货退款" in text else None,
        )

    @staticmethod
    def _query(sql, params=(), *, all_rows=False):
        connection = db_connection.get_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
                return cursor.fetchall() if all_rows else cursor.fetchone()
        finally:
            connection.close()

    def _seed_user_order(self, product_name):
        connection = db_connection.get_connection()
        suffix = uuid.uuid4().hex[:12]
        try:
            with connection.cursor() as cursor:
                cursor.execute("INSERT INTO users (username) VALUES (%s)", ("p2_" + suffix,))
                user_id = cursor.lastrowid
                cursor.execute(
                    "INSERT INTO orders (order_no,user_id,total_amount,status,delivered_at) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    ("ORD_" + suffix, user_id, 199.00, "delivered",
                     datetime.now() - timedelta(days=20)),
                )
                order_id = cursor.lastrowid
                cursor.execute(
                    "INSERT INTO order_items (order_id,product_name,quantity,unit_price) "
                    "VALUES (%s,%s,%s,%s)",
                    (order_id, product_name, 1, 199.00),
                )
                order_item_id = cursor.lastrowid
            connection.commit()
            return user_id, order_id, order_item_id
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _chat(self, text, user_id=None):
        effective_user_id = self.user_id if user_id is None else user_id
        return main.agent_chat(AgentChatRequest(
            user_id=effective_user_id,
            message=text,
        ), current_user_id=effective_user_id)

    def test_jwt_http_order_and_spoofed_body_use_token_identity(self):
        """隔离 MySQL：登录、订单读取和 Agent 任务均使用 Token 用户。"""
        other_user_id, _, _ = self._seed_user_order("其他商品")
        with patch("middleware.rate_limit.global_limiter.allow", return_value=True), \
             patch("middleware.rate_limit.user_limiter.allow", return_value=True), \
             TestClient(main.app) as client:
            login = client.post("/auth/login", json={"user_id": self.user_id})
            self.assertEqual(login.status_code, 200)
            headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
            orders = client.get(f"/users/{self.user_id}/orders", headers=headers)
            self.assertEqual(orders.status_code, 200)
            self.assertEqual(client.get(f"/users/{other_user_id}/orders", headers=headers).status_code, 403)
            chat = client.post("/agent/chat", json={
                "user_id": other_user_id, "message": "我要退无线键盘，质量不好"
            }, headers=headers)
        self.assertEqual(chat.status_code, 200)
        self.assertEqual(chat.json()["code"], "AFTER_SALE_TYPE_REQUIRED")
        self.assertIsNotNone(self._query(
            "SELECT id FROM agent_tasks WHERE user_id=%s ORDER BY id DESC LIMIT 1", (self.user_id,)
        ))
        self.assertIsNone(self._query(
            "SELECT id FROM agent_tasks WHERE user_id=%s ORDER BY id DESC LIMIT 1", (other_user_id,)
        ))

    def _confirm(self, confirmation_id, user_id=None):
        effective_user_id = self.user_id if user_id is None else user_id
        return main.agent_confirm(AgentConfirmRequest(
            user_id=effective_user_id,
            confirmation_id=confirmation_id,
        ), current_user_id=effective_user_id)

    def _task(self):
        return self._query(
            "SELECT * FROM agent_tasks WHERE user_id=%s ORDER BY id DESC LIMIT 1",
            (self.user_id,),
        )

    def _action(self, confirmation_id):
        return self._query(
            "SELECT * FROM agent_pending_actions WHERE confirmation_id=%s",
            (confirmation_id,),
        )

    def _steps(self, task_id):
        return self._query(
            "SELECT * FROM agent_task_steps WHERE task_id=%s ORDER BY id",
            (task_id,), all_rows=True,
        )

    def _ready(self):
        response = self._chat("我要退无线键盘，质量不好，退货退款")
        self.assertEqual(response.code, "ACTION_PREVIEW_READY")
        return response.data["confirmation_id"]

    def _created_sale(self):
        confirmation_id = self._ready()
        action = self._action(confirmation_id)
        created = self._confirm(confirmation_id)
        self.assertEqual(created["code"], "ACTION_EXECUTED")
        sale = self._query("SELECT * FROM after_sales WHERE idempotency_key=%s", (confirmation_id,))
        self.assertIsNotNone(sale)
        return sale, action["conversation_id"]

    def _operation_chat(self, message, conversation_id, user_id=None):
        effective_user_id = self.user_id if user_id is None else user_id
        return main.agent_chat(AgentChatRequest(
            user_id=effective_user_id, conversation_id=conversation_id, message=message,
        ), current_user_id=effective_user_id)

    def _operation_confirm(self, confirmation_id, conversation_id, user_id=None):
        effective_user_id = self.user_id if user_id is None else user_id
        return main.agent_confirm(AgentConfirmRequest(
            user_id=effective_user_id, conversation_id=conversation_id,
            confirmation_id=confirmation_id,
        ), current_user_id=effective_user_id)

    def test_cancel_sale_preview_confirm_repeat_and_executed_guard(self):
        sale, conversation_id = self._created_sale()
        preview = self._operation_chat("取消我的售后", conversation_id)
        self.assertEqual(preview.code, "ACTION_PREVIEW_READY")
        self.assertEqual(preview.data["preview"]["after_sale_no"], sale["after_sale_no"])
        self.assertEqual(self._query("SELECT status FROM after_sales WHERE id=%s", (sale["id"],))["status"], "pending")
        action_id = preview.data["confirmation_id"]
        done = self._operation_confirm(action_id, conversation_id)
        self.assertEqual(done["code"], "ACTION_EXECUTED")
        self.assertEqual(done["data"]["result"]["data"]["operation"], "cancel_after_sale")
        self.assertEqual(self._query("SELECT status FROM after_sales WHERE id=%s", (sale["id"],))["status"], "cancelled")
        self.assertEqual(self._action(action_id)["status"], "executed")
        self.assertEqual(self._operation_confirm(action_id, conversation_id)["code"], "IDEMPOTENT_REPLAY")
        self.assertEqual(self._operation_chat("我不想退了", conversation_id).code, "AFTER_SALE_ALREADY_CANCELLED")

    def test_change_type_preview_confirm_and_repeat(self):
        sale, conversation_id = self._created_sale()
        preview = self._operation_chat("不要退款了，换维修", conversation_id)
        self.assertEqual(preview.code, "ACTION_PREVIEW_READY")
        self.assertEqual(preview.data["preview"]["old_type"], "return_refund")
        self.assertEqual(preview.data["preview"]["new_type"], "repair")
        self.assertEqual(self._query("SELECT type FROM after_sales WHERE id=%s", (sale["id"],))["type"], "return_refund")
        action_id = preview.data["confirmation_id"]
        self.assertEqual(self._operation_confirm(action_id, conversation_id)["code"], "ACTION_EXECUTED")
        changed = self._query("SELECT type,refund_amount FROM after_sales WHERE id=%s", (sale["id"],))
        self.assertEqual(changed["type"], "repair")
        self.assertIsNone(changed["refund_amount"])
        self.assertEqual(self._operation_confirm(action_id, conversation_id)["code"], "IDEMPOTENT_REPLAY")
        self.assertEqual(self._operation_chat("改成维修", conversation_id).code, "AFTER_SALE_TYPE_UNCHANGED")

    def test_sale_operation_owner_conversation_and_non_pending_guards(self):
        from services.after_sale_operation_service import execute_after_sale_operation
        from services.conversation_service import create_conversation
        sale, conversation_id = self._created_sale()
        other_conversation = create_conversation(self.user_id)["id"]
        self.assertEqual(self._operation_chat("取消我的售后", other_conversation).code, "AFTER_SALE_NOT_FOUND")
        cross_conversation = self._operation_chat(
            "取消售后 " + sale["after_sale_no"], other_conversation,
        )
        self.assertEqual(cross_conversation.code, "ACTION_PREVIEW_READY")
        self.assertEqual(cross_conversation.data["preview"]["after_sale_id"], sale["id"])
        pending = self._operation_chat("取消我的售后", conversation_id)
        self.assertEqual(pending.code, "ACTION_PREVIEW_READY")
        self.assertEqual(self._operation_confirm(
            pending.data["confirmation_id"], other_conversation,
        )["code"], "ACTION_NOT_FOUND")
        other_user, _, _ = self._seed_user_order("其他商品")
        other_user_conversation = create_conversation(other_user)["id"]
        self.assertEqual(self._operation_chat(
            "取消售后 " + sale["after_sale_no"], other_user_conversation, other_user,
        ).code, "AFTER_SALE_NOT_FOUND")
        self.assertEqual(self._operation_confirm(
            pending.data["confirmation_id"], conversation_id, other_user,
        )["code"], "ACTION_NOT_FOUND")
        denied = execute_after_sale_operation(
            user_id=other_user, conversation_id=conversation_id,
            action_type="cancel_after_sale", preview={"after_sale_no": sale["after_sale_no"]},
        )
        self.assertEqual(denied["code"], "ACTION_NOT_FOUND")
        self.assertEqual(self._query("SELECT status FROM after_sales WHERE id=%s", (sale["id"],))["status"], "pending")
        connection = db_connection.get_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute("UPDATE after_sales SET status='completed' WHERE id=%s", (sale["id"],))
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(self._operation_chat("取消我的售后", conversation_id).code, "AFTER_SALE_NOT_PENDING")
        self.assertEqual(self._operation_chat("改成维修", conversation_id).code, "AFTER_SALE_NOT_FOUND")
        rejected = self._operation_confirm(pending.data["confirmation_id"], conversation_id)
        self.assertEqual(rejected["code"], "ACTION_EXECUTION_FAILED")
        self.assertEqual(self._action(pending.data["confirmation_id"])["status"], "failed")
        self.assertEqual(self._query("SELECT status FROM after_sales WHERE id=%s", (sale["id"],))["status"], "completed")

    def test_successful_after_sale_creation_and_trace(self):
        confirmation_id = self._ready()
        task_before = self._task()
        self.assertEqual(task_before["status"], "waiting_confirm")
        self.assertEqual(self._action(confirmation_id)["status"], "pending")
        self.assertEqual(self._action(confirmation_id)["task_id"], task_before["id"])

        response = self._confirm(confirmation_id)
        self.assertEqual(response["code"], "ACTION_EXECUTED")
        self.assertEqual(self._task()["status"], "completed")
        self.assertEqual(self._task()["current_step"], "completed")
        self.assertEqual(self._action(confirmation_id)["status"], "executed")
        self.assertIsNone(self._action(confirmation_id)["error_message"])
        after_sale = self._query(
            "SELECT * FROM after_sales WHERE idempotency_key=%s", (confirmation_id,)
        )
        self.assertIsNotNone(after_sale)
        self.assertEqual(after_sale["order_item_id"], self.order_item_id)
        self.assertEqual(after_sale["user_id"], self.user_id)
        steps = self._steps(task_before["id"])
        self.assertEqual([step["step_name"] for step in steps], [
            "input_analysis", "context_merge", "resolve_order", "policy_check", "build_preview",
            "create_pending_action", "confirm", "execute",
        ])
        self.assertTrue(all(step["status"] == "SUCCESS" for step in steps))
        self.assertTrue(all(step["latency_ms"] is not None for step in steps))
        self.assertTrue(all(step["trace_id"] == steps[0]["trace_id"] for step in steps[:6]))
        self.assertTrue(all(step["trace_id"] == response["trace_id"] for step in steps[6:]))

    def test_cancel_pending_action_checks_owner_and_blocks_confirmation(self):
        confirmation_id = self._ready()
        other_user_id, _, _ = self._seed_user_order("其他商品")
        request = AgentConfirmRequest(confirmation_id=confirmation_id)
        self.assertEqual(
            main.agent_cancel(request, current_user_id=other_user_id)["code"], "ACTION_NOT_FOUND"
        )
        self.assertEqual(self._action(confirmation_id)["status"], "pending")
        cancelled = main.agent_cancel(request, current_user_id=self.user_id)
        self.assertEqual(cancelled["data"]["status"], "cancelled")
        self.assertEqual(self._action(confirmation_id)["status"], "cancelled")
        self.assertEqual(self._confirm(confirmation_id)["code"], "INVALID_ACTION_STATUS")
        self.assertIsNone(self._query(
            "SELECT id FROM after_sales WHERE idempotency_key=%s", (confirmation_id,)
        ))

    def test_expiry_sweep_only_changes_old_pending_actions(self):
        confirmation_id = self._ready()
        connection = db_connection.get_connection()
        extra_ids = {}
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE agent_pending_actions SET created_at=DATE_SUB(NOW(), INTERVAL 8 DAY) "
                    "WHERE confirmation_id=%s", (confirmation_id,),
                )
                for status in ("confirmed", "executing", "executed", "failed", "pending"):
                    extra_id = "CONF_EXP_" + uuid.uuid4().hex[:16]
                    extra_ids[status] = extra_id
                    cursor.execute(
                        "INSERT INTO agent_pending_actions "
                        "(confirmation_id,user_id,action_type,preview_json,status,created_at) "
                        "VALUES (%s,%s,'create_after_sale','{}',%s,DATE_SUB(NOW(), INTERVAL %s DAY))",
                        (extra_id, self.user_id, status, 0 if status == "pending" else 8),
                    )
            connection.commit()
        finally:
            connection.close()

        self.assertEqual(expire_pending_actions(), 1)
        self.assertEqual(self._action(confirmation_id)["status"], "expired")
        self.assertIsNone(self._query(
            "SELECT current_confirmation_id FROM agent_sessions WHERE user_id=%s", (self.user_id,)
        )["current_confirmation_id"])
        self.assertEqual(self._confirm(confirmation_id)["code"], "INVALID_ACTION_STATUS")
        for status, extra_id in extra_ids.items():
            self.assertEqual(self._action(extra_id)["status"], status)

    def test_confirm_expires_old_pending_even_before_sweep(self):
        confirmation_id = self._ready()
        connection = db_connection.get_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE agent_pending_actions SET created_at=DATE_SUB(NOW(), INTERVAL 8 DAY) "
                    "WHERE confirmation_id=%s", (confirmation_id,),
                )
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(self._confirm(confirmation_id)["code"], "INVALID_ACTION_STATUS")
        self.assertEqual(self._action(confirmation_id)["status"], "expired")

    def test_two_turn_context_merge_keeps_same_task(self):
        first = self._chat("我要退无线键盘，质量不好")
        self.assertEqual(first.code, "AFTER_SALE_TYPE_REQUIRED")
        task_id = self._task()["id"]
        self.assertEqual(self._task()["status"], "waiting_input")
        second = self._chat("退货退款")
        self.assertEqual(second.code, "ACTION_PREVIEW_READY")
        self.assertEqual(self._task()["id"], task_id)
        self.assertEqual(second.data["preview"]["reason"], "质量不好")
        self.assertEqual(self._action(second.data["confirmation_id"])["task_id"], task_id)

    def test_three_turn_missing_type_then_reason_reaches_preview(self):
        with patch("agent.input_analyzer.route_intent", side_effect=lambda text: IntentResult(
            intent="create_after_sale" if "无线键盘" in text else "unknown"
        )) as route:
            first = self._chat("我要申请无线键盘售后")
            self.assertEqual(first.code, "AFTER_SALE_TYPE_REQUIRED")
            task_id = self._task()["id"]
            self.assertEqual(self._task()["status"], "waiting_input")
            self.assertIn('"product_name": "无线键盘"', self._task()["context"])

            second = self._chat("退货退款")
            self.assertEqual(second.code, "AFTER_SALE_REASON_REQUIRED")
            self.assertEqual(self._task()["id"], task_id)
            self.assertEqual(self._task()["status"], "waiting_input")
            self.assertIn('"after_sale_type": "return_refund"', self._task()["context"])

            third = self._chat("质量不好")
            self.assertEqual(third.code, "ACTION_PREVIEW_READY")
            self.assertEqual(self._task()["id"], task_id)
            self.assertEqual(self._task()["status"], "waiting_confirm")
            self.assertEqual(third.data["preview"]["product_name"], "无线键盘")
            self.assertEqual(third.data["preview"]["after_sale_type"], "return_refund")
            self.assertEqual(third.data["preview"]["reason"], "质量不好")
            self.assertEqual(self._action(third.data["confirmation_id"])["task_id"], task_id)
            route.assert_called_once_with("我要申请无线键盘售后")

    def test_single_turn_create_after_sale_still_reaches_preview(self):
        result = self._chat("我要申请无线键盘退货退款，因为质量不好")
        self.assertEqual(result.code, "ACTION_PREVIEW_READY")
        self.assertEqual(result.data["preview"]["product_name"], "无线键盘")
        self.assertEqual(result.data["preview"]["after_sale_type"], "return_refund")
        self.assertEqual(result.data["preview"]["reason"], "质量不好")

    def test_legacy_waiting_input_recovers_original_intent(self):
        self.assertEqual(self._chat("我要申请无线键盘售后").code, "AFTER_SALE_TYPE_REQUIRED")
        task_id = self._task()["id"]
        connection = db_connection.get_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute("UPDATE agent_tasks SET context=%s WHERE id=%s",
                               ('{"product_name": "无线键盘"}', task_id))
            connection.commit()
        finally:
            connection.close()
        # Intent recovery is now deliberately performed by the shared input
        # analyzer, rather than by the runtime module's legacy import.
        with patch("agent.input_analyzer.route_intent", return_value=IntentResult(intent="create_after_sale")) as original:
            second = self._chat("退货退款")
        self.assertEqual(second.code, "AFTER_SALE_REASON_REQUIRED")
        self.assertEqual(self._task()["id"], task_id)
        original.assert_called_once_with("我要申请无线键盘售后")

    def test_missing_type_and_reason(self):
        for text, expected in (
            ("我要退无线键盘，质量不好", "AFTER_SALE_TYPE_REQUIRED"),
            ("我要退无线键盘，退货退款", "AFTER_SALE_REASON_REQUIRED"),
        ):
            with self.subTest(expected=expected):
                user_id, _, _ = self._seed_user_order("无线键盘")
                result = self._chat(text, user_id)
                self.assertEqual(result.code, expected)
                task = self._query(
                    "SELECT status FROM agent_tasks WHERE user_id=%s ORDER BY id DESC LIMIT 1",
                    (user_id,),
                )
                self.assertEqual(task["status"], "waiting_input")
                self.assertIsNone(self._query(
                    "SELECT id FROM agent_pending_actions WHERE user_id=%s", (user_id,)
                ))

    def test_product_not_found_records_failed_step(self):
        result = self._chat("我要退不存在商品，退货退款，质量不好")
        self.assertEqual(result.code, "PRODUCT_NOT_FOUND")
        task = self._task()
        self.assertEqual(task["status"], "failed")
        step = self._query(
            "SELECT * FROM agent_task_steps WHERE task_id=%s AND step_name='resolve_order'",
            (task["id"],),
        )
        self.assertEqual(step["status"], "FAILED")
        self.assertEqual(step["trace_id"], result.trace_id)

    def test_policy_query_uses_read_tool_without_pending_action(self):
        policy = {
            "success": True, "code": "RAG_OK", "message": "找到依据",
            "answer": "请参考规则，具体结果以订单核验为准。",
            "evidence": [{"source": "returns.md", "content": "退货规则"}],
        }
        with patch("agent.input_analyzer.route_intent", return_value=IntentResult(intent="query_policy")), \
             patch("agent.read_workflow.policy_rag_tool") as tool, \
             patch("agent.policy_response.generate_policy_answer", return_value="质量问题可申请检测，是否退款以核验为准。"):
            tool.invoke.return_value = policy
            response = self._chat("AirPods用了20天还能退吗？")
        self.assertEqual(response.code, "POLICY_RESULT_READY")
        self.assertTrue(response.success)
        self.assertEqual(response.data["answer"], "质量问题可申请检测，是否退款以核验为准。")
        tool.invoke.assert_called_once_with({"question": "AirPods用了20天还能退吗？"})
        self.assertIsNone(self._query(
            "SELECT id FROM agent_pending_actions WHERE user_id=%s", (self.user_id,)
        ))
        self.assertIsNone(self._query(
            "SELECT id FROM after_sales WHERE user_id=%s", (self.user_id,)
        ))

    def test_new_existing_sale_is_rejected_before_atomic_claim(self):
        confirmation_id = self._ready()
        connection = db_connection.get_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO after_sales "
                    "(after_sale_no,user_id,order_id,order_item_id,type,reason,status) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    ("AS_EXIST_" + uuid.uuid4().hex[:12], self.user_id,
                     self.order_id, self.order_item_id, "refund", "历史售后", "pending"),
                )
            connection.commit()
        finally:
            connection.close()
        response = self._confirm(confirmation_id)
        self.assertEqual(response["code"], "AFTER_SALE_ALREADY_EXISTS")
        self.assertEqual(self._action(confirmation_id)["status"], "confirmed")
        self.assertEqual(self._task()["status"], "waiting_confirm")
        self.assertIsNone(self._action(confirmation_id)["error_message"])
        self.assertIsNone(self._query(
            "SELECT id FROM after_sales WHERE idempotency_key=%s", (confirmation_id,)
        ))
        step = self._query(
            "SELECT * FROM agent_task_steps WHERE task_id=%s AND step_name='execute'",
            (self._task()["id"],),
        )
        self.assertEqual(step["status"], "FAILED")
        self.assertEqual(step["trace_id"], response["trace_id"])

    def test_idempotent_replay_calls_business_service_once(self):
        confirmation_id = self._ready()
        with patch("agent.action_executor.create_after_sale_service", wraps=create_after_sale_service) as service:
            first = self._confirm(confirmation_id)
            action_after_first = self._action(confirmation_id)
            task_after_first = self._task()
            second = self._confirm(confirmation_id)
        self.assertEqual(first["code"], "ACTION_EXECUTED")
        self.assertEqual(second["code"], "IDEMPOTENT_REPLAY")
        self.assertEqual(service.call_count, 1)
        self.assertEqual(self._action(confirmation_id)["status"], "executed")
        self.assertEqual(self._task()["status"], "completed")
        self.assertEqual(self._action(confirmation_id)["execution_result_json"], action_after_first["execution_result_json"])
        self.assertEqual(self._action(confirmation_id)["updated_at"], action_after_first["updated_at"])
        self.assertEqual(self._task()["updated_at"], task_after_first["updated_at"])
        row = self._query(
            "SELECT COUNT(*) AS count FROM after_sales WHERE idempotency_key=%s",
            (confirmation_id,),
        )
        self.assertEqual(row["count"], 1)

    def test_final_states_survive_new_database_connection(self):
        """模拟服务重启后重连 MySQL，终态必须持久且一致。"""
        confirmation_id = self._ready()
        task_id = self._task()["id"]
        self.assertEqual(self._confirm(confirmation_id)["code"], "ACTION_EXECUTED")

        connection = db_connection.get_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT status, current_step FROM agent_tasks WHERE id=%s", (task_id,))
                task = cursor.fetchone()
                cursor.execute(
                    "SELECT status, error_message FROM agent_pending_actions WHERE confirmation_id=%s",
                    (confirmation_id,),
                )
                action = cursor.fetchone()
        finally:
            connection.close()
        self.assertEqual(task, {"status": "completed", "current_step": "completed"})
        self.assertEqual(action, {"status": "executed", "error_message": None})

    def test_legacy_failed_task_can_be_reconciled_after_restart(self):
        """模拟旧版本留下的状态错位，恢复时按最新 Action 修复且可重复运行。"""
        confirmation_id = self._ready()
        with patch("agent.action_executor.create_after_sale_service", return_value={
            "success": False, "code": "AFTER_SALE_ALREADY_EXISTS", "message": "已有售后",
        }):
            self.assertEqual(self._confirm(confirmation_id)["code"], "ACTION_EXECUTION_FAILED")
        task_id = self._task()["id"]
        connection = db_connection.get_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE agent_tasks SET status='waiting_confirm', current_step='waiting_confirm' "
                    "WHERE id=%s", (task_id,),
                )
                cursor.execute(
                    "UPDATE agent_pending_actions SET error_message=NULL WHERE confirmation_id=%s",
                    (confirmation_id,),
                )
            connection.commit()
        finally:
            connection.close()

        self.assertEqual(reconcile_finished_agent_tasks(), 1)
        self.assertEqual(reconcile_finished_agent_tasks(), 0)
        self.assertEqual(self._task()["status"], "failed")
        self.assertEqual(self._task()["current_step"], "failed")
        self.assertEqual(self._action(confirmation_id)["status"], "failed")
        self.assertEqual(self._action(confirmation_id)["error_message"], "已有售后")

    def test_other_user_cannot_confirm(self):
        confirmation_id = self._ready()
        other_user_id, _, _ = self._seed_user_order("其他商品")
        response = self._confirm(confirmation_id, other_user_id)
        self.assertEqual(response["code"], "ACTION_NOT_FOUND")
        self.assertEqual(self._action(confirmation_id)["status"], "pending")
        self.assertIsNone(self._query(
            "SELECT id FROM after_sales WHERE idempotency_key=%s", (confirmation_id,)
        ))
        step = self._query(
            "SELECT * FROM agent_task_steps WHERE trace_id=%s AND step_name='confirm'",
            (response["trace_id"],),
        )
        self.assertEqual(step["status"], "FAILED")
        self.assertIsNone(step["task_id"])

    def test_concurrent_confirm_executes_once(self):
        confirmation_id = self._ready()
        barrier = threading.Barrier(2)
        service_lock = threading.Lock()
        service_calls = []

        def synchronized_confirm(*args, **kwargs):
            barrier.wait(timeout=10)
            return confirm_action(*args, **kwargs)

        def slow_service(*args, **kwargs):
            with service_lock:
                service_calls.append(1)
            time.sleep(0.1)
            return create_after_sale_service(*args, **kwargs)

        with patch("main.confirm_action", side_effect=synchronized_confirm):
            with patch("agent.action_executor.create_after_sale_service", side_effect=slow_service):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = [executor.submit(self._confirm, confirmation_id) for _ in range(2)]
                    results = [future.result(timeout=15) for future in futures]
        self.assertEqual(sum(result["code"] == "ACTION_EXECUTED" for result in results), 1)
        self.assertIn(results[0]["code"], {
            "ACTION_EXECUTED", "ACTION_ALREADY_CONFIRMED", "ACTION_EXECUTING",
            "ACTION_ALREADY_CLAIMED", "IDEMPOTENT_REPLAY",
        })
        self.assertIn(results[1]["code"], {
            "ACTION_EXECUTED", "ACTION_ALREADY_CONFIRMED", "ACTION_EXECUTING",
            "ACTION_ALREADY_CLAIMED", "IDEMPOTENT_REPLAY",
        })
        self.assertEqual(len(service_calls), 1)
        self.assertEqual(self._action(confirmation_id)["status"], "executed")
        self.assertEqual(self._query(
            "SELECT COUNT(*) AS count FROM after_sales WHERE idempotency_key=%s",
            (confirmation_id,),
        )["count"], 1)


if __name__ == "__main__":
    unittest.main()
