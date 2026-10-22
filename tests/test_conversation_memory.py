"""Conversation persistence and ownership against an isolated MySQL database."""

import os
import re
import unittest
import uuid
from dataclasses import replace
from core.settings import get_settings
from unittest.mock import patch

from fastapi.testclient import TestClient

import main
from auth.jwt import create_access_token
from db import connection as db_connection
from db.init_db import create_tables
from db.migrate_conversation_scope import migrate_conversation_scope
from schemas.agent_schema import BusinessParams, IntentResult


@unittest.skipUnless(os.getenv("RUN_AGENT_MYSQL_TESTS") == "1", "需要显式开启真实 MySQL 测试")
class ConversationMemoryMySQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.database_name = "p2_agent_test_" + uuid.uuid4().hex[:12]
        if not re.fullmatch(r"p2_agent_test_[0-9a-f]{12}", cls.database_name):
            raise RuntimeError("测试数据库名称校验失败")
        server = db_connection.get_server_connection()
        try:
            with server.cursor() as cursor:
                cursor.execute(f"CREATE DATABASE `{cls.database_name}` CHARACTER SET utf8mb4")
            server.commit()
        finally:
            server.close()
        cls.database_patch = patch.object(db_connection, "MYSQL_DATABASE", cls.database_name)
        cls.database_patch.start()
        try:
            create_tables()
            migrate_conversation_scope()
            migrate_conversation_scope()
            connection = db_connection.get_connection()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("INSERT INTO users (username) VALUES ('conversation_user_1')")
                    cls.user_1 = cursor.lastrowid
                    cursor.execute("INSERT INTO users (username) VALUES ('conversation_user_2')")
                    cls.user_2 = cursor.lastrowid
                connection.commit()
            finally:
                connection.close()
        except Exception:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "database_patch", None):
            cls.database_patch.stop()
        if re.fullmatch(r"p2_agent_test_[0-9a-f]{12}", cls.database_name):
            server = db_connection.get_server_connection()
            try:
                with server.cursor() as cursor:
                    cursor.execute(f"DROP DATABASE `{cls.database_name}`")
                server.commit()
            finally:
                server.close()

    def test_logout_login_history_new_conversation_and_ownership(self):
        answer = "20天已超过无理由退货期限；质量问题可按政策申请检测，保修需核验。"
        with patch("middleware.rate_limit.global_limiter.allow", return_value=True), \
             patch("middleware.rate_limit.user_limiter.allow", return_value=True), \
             patch("main.run_agent", return_value={
                 "success": True, "code": "POLICY_RESULT_READY", "message": "政策回答",
                 "data": {"answer": answer, "evidence": [{"source": "耳机类商品规则.txt"}]},
             }) as runner, TestClient(main.app) as client:
            token_1 = client.post("/auth/login", json={"user_id": self.user_1}).json()["access_token"]
            headers_1 = {"Authorization": f"Bearer {token_1}"}
            created = client.post("/agent/conversations", headers=headers_1)
            self.assertEqual(created.status_code, 200)
            first_id = created.json()["data"]["id"]
            question = "AirPods用了20天还能退吗？"
            response = client.post("/agent/chat", json={
                "conversation_id": first_id, "user_id": self.user_2, "message": question,
            }, headers=headers_1)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["code"], "POLICY_RESULT_READY")
            runner.assert_called_once()
            self.assertEqual(runner.call_args.kwargs["current_user_id"], self.user_1)

            # A fresh login/client still reads the same persisted user/assistant messages.
            fresh_token = client.post("/auth/login", json={"user_id": self.user_1}).json()["access_token"]
            fresh_headers = {"Authorization": f"Bearer {fresh_token}"}
            history = client.get(f"/agent/conversations/{first_id}/messages", headers=fresh_headers)
            self.assertEqual([row["role"] for row in history.json()["data"]["messages"]], ["user", "assistant"])
            self.assertEqual(history.json()["data"]["messages"][0]["content"], question)
            self.assertEqual(history.json()["data"]["messages"][1]["content"], answer)

            second_id = client.post("/agent/conversations", headers=fresh_headers).json()["data"]["id"]
            self.assertNotEqual(first_id, second_id)
            self.assertEqual(client.get(
                f"/agent/conversations/{second_id}/messages", headers=fresh_headers
            ).json()["data"]["messages"], [])
            handoff = client.post(
                f"/agent/conversations/{second_id}/handoff",
                json={"reason": "用户请求人工客服"}, headers=fresh_headers,
            )
            self.assertEqual(handoff.json()["data"]["status"], "waiting")

            headers_2 = {"Authorization": f"Bearer {create_access_token(self.user_2)}"}
            self.assertEqual(client.get(f"/agent/conversations/{first_id}/messages", headers=headers_2).status_code, 404)
            self.assertEqual(client.post(
                f"/agent/conversations/{first_id}/handoff", json={"reason": "测试"}, headers=headers_2
            ).status_code, 404)
            self.assertEqual(client.post("/agent/chat", json={
                "conversation_id": first_id, "message": "越权消息"
            }, headers=headers_2).status_code, 404)
            self.assertEqual(runner.call_count, 1)

        connection = db_connection.get_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) AS n FROM agent_pending_actions")
                self.assertEqual(cursor.fetchone()["n"], 0)
                cursor.execute("SELECT status FROM handoff_task WHERE conversation_id=%s", (second_id,))
                self.assertEqual(cursor.fetchone()["status"], "waiting")
        finally:
            connection.close()

    def test_two_conversations_keep_tasks_context_actions_and_trace_separate(self):
        connection = db_connection.get_connection()
        try:
            with connection.cursor() as cursor:
                for product, order_no in (("AirPods Pro", "ORD_CONV_A"), ("手机", "ORD_CONV_B")):
                    cursor.execute(
                        "INSERT INTO orders (order_no,user_id,total_amount,status,delivered_at) "
                        "VALUES (%s,%s,%s,'delivered',NOW() - INTERVAL 20 DAY)",
                        (order_no, self.user_1, 199.00),
                    )
                    cursor.execute(
                        "INSERT INTO order_items (order_id,product_name,quantity,unit_price) "
                        "VALUES (%s,%s,1,199.00)",
                        (cursor.lastrowid, product),
                    )
            connection.commit()
        finally:
            connection.close()

        def extract(text):
            return BusinessParams(
                product_name="AirPods Pro" if "AirPods" in text else ("手机" if "手机" in text else None),
                reason="质量不好" if "质量不好" in text else ("屏幕坏了" if "屏幕坏了" in text else None),
                after_sale_type="repair" if "维修" in text else ("return_refund" if "退货退款" in text else None),
            )

        test_policy_result = {
            "decision": "ELIGIBLE_TO_APPLY", "policy_type": "isolated_test_rule",
            "policy_message": "隔离测试规则；仍需审核", "requires_review": True,
            "days_since_delivery": 20, "policy_deadline_days": 30,
            "sources": ["隔离测试政策.txt"],
            "policy_versions": {"隔离测试政策.txt": "isolation-v1"},
            "policy_file_hashes": {"隔离测试政策.txt": "isolated"},
            "evidence": [{"source": "隔离测试政策.txt", "file_hash": "isolated",
                          "content": "仅用于隔离业务链路测试"}],
        }
        with patch("middleware.rate_limit.global_limiter.allow", return_value=True), \
             patch("middleware.rate_limit.user_limiter.allow", return_value=True), \
             patch("main.get_settings", return_value=replace(
                 get_settings(), use_langgraph_agent=False, app_env="development")), \
             patch("agent.input_analyzer.route_intent", return_value=IntentResult(intent="create_after_sale")), \
             patch("agent.input_analyzer.extract_business_params", side_effect=extract), \
             patch("agent.write_workflow.check_after_sale_policy", return_value=test_policy_result), \
             patch("services.policy_execution_service.check_after_sale_policy",
                   return_value=test_policy_result), \
             TestClient(main.app) as client:
            token = client.post("/auth/login", json={"user_id": self.user_1}).json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            conv_a = client.post("/agent/conversations", headers=headers).json()["data"]["id"]
            conv_b = client.post("/agent/conversations", headers=headers).json()["data"]["id"]
            chat_a1 = client.post("/agent/chat", json={
                "conversation_id": conv_a, "message": "我要退AirPods，质量不好"
            }, headers=headers).json()
            self.assertEqual(chat_a1["code"], "AFTER_SALE_TYPE_REQUIRED")
            chat_b = client.post("/agent/chat", json={
                "conversation_id": conv_b, "message": "我要退手机，屏幕坏了，退货退款"
            }, headers=headers).json()
            self.assertEqual(chat_b["code"], "ACTION_PREVIEW_READY")
            chat_a2 = client.post("/agent/chat", json={
                "conversation_id": conv_a, "message": "退货退款"
            }, headers=headers).json()
            self.assertEqual(chat_a2["code"], "ACTION_PREVIEW_READY")
            self.assertEqual(chat_a2["data"]["preview"]["product_name"], "AirPods Pro")
            self.assertEqual(chat_b["data"]["preview"]["product_name"], "手机")

            tasks_a = client.get("/agent/tasks", params={"conversation_id": conv_a}, headers=headers).json()["data"]["tasks"]
            tasks_b = client.get("/agent/tasks", params={"conversation_id": conv_b}, headers=headers).json()["data"]["tasks"]
            self.assertEqual(len(tasks_a), 1)
            self.assertEqual(len(tasks_b), 1)
            self.assertNotEqual(tasks_a[0]["id"], tasks_b[0]["id"])
            self.assertEqual(client.get(
                f"/agent/trace/{tasks_a[0]['id']}", params={"conversation_id": conv_b}, headers=headers
            ).status_code, 404)

            action_a = chat_a2["data"]["confirmation_id"]
            action_b = chat_b["data"]["confirmation_id"]
            wrong_conversation = client.post("/agent/confirm", json={
                "confirmation_id": action_a, "conversation_id": conv_b,
            }, headers=headers)
            self.assertEqual(wrong_conversation.json()["code"], "ACTION_NOT_FOUND")
            confirmed_b = client.post("/agent/confirm", json={
                "confirmation_id": action_b, "conversation_id": conv_b,
            }, headers=headers).json()
            self.assertEqual(confirmed_b["code"], "ACTION_EXECUTED")

            renewed = client.post("/auth/login", json={"user_id": self.user_1}).json()["access_token"]
            renewed_headers = {"Authorization": f"Bearer {renewed}"}
            self.assertEqual(client.get(
                "/agent/tasks", params={"conversation_id": conv_a}, headers=renewed_headers
            ).json()["data"]["tasks"][0]["id"], tasks_a[0]["id"])

        connection = db_connection.get_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id, conversation_id, status, context FROM agent_tasks "
                    "WHERE id IN (%s,%s) ORDER BY id", (tasks_a[0]["id"], tasks_b[0]["id"]),
                )
                rows = cursor.fetchall()
                self.assertEqual({row["conversation_id"] for row in rows}, {conv_a, conv_b})
                cursor.execute(
                    "SELECT confirmation_id, conversation_id, task_id, status "
                    "FROM agent_pending_actions WHERE confirmation_id IN (%s,%s)",
                    (action_a, action_b),
                )
                actions = {row["confirmation_id"]: row for row in cursor.fetchall()}
                self.assertEqual(actions[action_a]["conversation_id"], conv_a)
                self.assertEqual(actions[action_b]["conversation_id"], conv_b)
                self.assertEqual(actions[action_a]["status"], "pending")
                self.assertEqual(actions[action_b]["status"], "executed")
                cursor.execute(
                    "SELECT a.conversation_id FROM after_sales AS s "
                    "JOIN agent_pending_actions AS a ON a.confirmation_id=s.idempotency_key "
                    "WHERE s.idempotency_key=%s", (action_b,),
                )
                self.assertEqual(cursor.fetchone()["conversation_id"], conv_b)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
