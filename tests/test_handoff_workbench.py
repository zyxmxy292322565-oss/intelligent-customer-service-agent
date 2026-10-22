"""Human handoff APIs against an isolated disposable MySQL database."""

import os
import re
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import main
from auth.jwt import create_access_token
from db import connection as db_connection
from db.init_db import create_tables
from db.migrate_conversation_scope import migrate_conversation_scope


ADMIN_KEY = "test-only-admin-key-with-more-than-32-characters"


@unittest.skipUnless(os.getenv("RUN_AGENT_MYSQL_TESTS") == "1", "需要显式开启真实 MySQL 测试")
class HandoffWorkbenchMySQLTests(unittest.TestCase):
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
            connection = db_connection.get_connection()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("INSERT INTO users (username) VALUES ('customer_one')")
                    cls.user_one = cursor.lastrowid
                    cursor.execute("INSERT INTO users (username) VALUES ('customer_two')")
                    cls.user_two = cursor.lastrowid
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

    def _client(self):
        return TestClient(main.app)

    @staticmethod
    def _auth(user_id):
        return {"Authorization": f"Bearer {create_access_token(user_id)}"}

    @staticmethod
    def _admin():
        return {"X-Admin-Key": ADMIN_KEY}

    def test_request_permissions_transitions_and_conversation_isolation(self):
        with patch("middleware.rate_limit.global_limiter.allow", return_value=True), \
             patch("middleware.rate_limit.user_limiter.allow", return_value=True), \
             patch("auth.admin_dependency.get_settings", return_value=SimpleNamespace(admin_api_key=ADMIN_KEY)), \
             patch("main.run_agent") as runner, self._client() as client:
            conv_a = client.post("/agent/conversations", headers=self._auth(self.user_one)).json()["data"]["id"]
            conv_b = client.post("/agent/conversations", headers=self._auth(self.user_one)).json()["data"]["id"]
            first = client.post("/agent/chat", json={
                "conversation_id": conv_a, "message": "转人工",
            }, headers=self._auth(self.user_one))
            self.assertEqual(first.json()["code"], "HANDOFF_REQUESTED")
            self.assertEqual(first.json()["message"], "已为您转接人工客服，请稍候")
            runner.assert_not_called()
            client.post("/agent/chat", json={
                "conversation_id": conv_a, "message": "联系客服",
            }, headers=self._auth(self.user_one))
            client.post("/agent/chat", json={
                "conversation_id": conv_b, "message": "人工处理",
            }, headers=self._auth(self.user_one))
            self.assertEqual(client.post("/agent/chat", json={
                "conversation_id": conv_a, "message": "转人工",
            }, headers=self._auth(self.user_two)).status_code, 404)

            self.assertEqual(client.get("/admin/handoff/tasks").status_code, 401)
            self.assertEqual(client.get("/admin/handoff/tasks", headers=self._auth(self.user_one)).status_code, 401)
            self.assertEqual(client.get("/admin/handoff/tasks", headers={"X-Admin-Key": "wrong"}).status_code, 403)
            tasks = client.get("/admin/handoff/tasks", headers=self._admin()).json()["data"]["tasks"]
            self.assertEqual(len([item for item in tasks if item["conversation_id"] in {conv_a, conv_b}]), 2)
            task_a = next(item for item in tasks if item["conversation_id"] == conv_a)
            detail = client.get(f"/admin/handoff/{task_a['id']}", headers=self._admin()).json()["data"]
            self.assertEqual([m["content"] for m in detail["messages"] if m["role"] == "user"], ["转人工", "联系客服"])
            self.assertEqual(detail["handoff"]["user_id"], self.user_one)
            self.assertEqual(client.post(f"/admin/handoff/{task_a['id']}/complete", headers=self._admin()).status_code, 409)
            accepted = client.post(f"/admin/handoff/{task_a['id']}/accept", headers=self._admin())
            self.assertEqual(accepted.json()["data"]["status"], "assigned")
            self.assertEqual(client.post(f"/admin/handoff/{task_a['id']}/accept", headers=self._admin()).status_code, 409)
            completed = client.post(f"/admin/handoff/{task_a['id']}/complete", headers=self._admin())
            self.assertEqual(completed.json()["data"]["status"], "completed")
            self.assertEqual(client.post(f"/admin/handoff/{task_a['id']}/complete", headers=self._admin()).status_code, 409)

        connection = db_connection.get_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT user_id, conversation_id, reason, status FROM handoff_task WHERE id=%s", (task_a["id"],))
                saved = cursor.fetchone()
                self.assertEqual((saved["user_id"], saved["conversation_id"], saved["status"]),
                                 (self.user_one, conv_a, "completed"))
        finally:
            connection.close()

    def test_detail_contains_only_linked_sale_and_agent_results(self):
        with patch("middleware.rate_limit.global_limiter.allow", return_value=True), \
             patch("middleware.rate_limit.user_limiter.allow", return_value=True), \
             patch("auth.admin_dependency.get_settings", return_value=SimpleNamespace(admin_api_key=ADMIN_KEY)), \
             self._client() as client:
            conv_a = client.post("/agent/conversations", headers=self._auth(self.user_one)).json()["data"]["id"]
            conv_b = client.post("/agent/conversations", headers=self._auth(self.user_one)).json()["data"]["id"]
            handoff = client.post(f"/agent/conversations/{conv_a}/handoff", json={
                "reason": "需要核对售后",
            }, headers=self._auth(self.user_one)).json()["data"]
            connection = db_connection.get_connection()
            try:
                with connection.cursor() as cursor:
                    for conv, label in ((conv_a, "A"), (conv_b, "B")):
                        suffix = uuid.uuid4().hex[:12]
                        cursor.execute("INSERT INTO orders (order_no,user_id,total_amount,status) VALUES (%s,%s,100,'delivered')",
                                       ("ORD_" + suffix, self.user_one))
                        order_id = cursor.lastrowid
                        cursor.execute("INSERT INTO order_items (order_id,product_name,quantity,unit_price) VALUES (%s,%s,1,100)",
                                       (order_id, f"商品{label}"))
                        item_id = cursor.lastrowid
                        cursor.execute("INSERT INTO agent_tasks (task_no,user_id,conversation_id,task_type,goal,status,current_step,context) "
                                       "VALUES (%s,%s,%s,'agent',%s,'completed','completed','{}')",
                                       ("TASK_" + suffix, self.user_one, conv, f"申请商品{label}售后"))
                        task_id = cursor.lastrowid
                        cursor.execute("INSERT INTO agent_task_steps (task_id,trace_id,step_name,status,latency_ms) "
                                       "VALUES (%s,%s,'build_preview','SUCCESS',12)", (task_id, "TRACE_" + suffix))
                        confirmation_id = "CONF_" + suffix.upper()
                        cursor.execute("INSERT INTO agent_pending_actions "
                                       "(confirmation_id,user_id,conversation_id,task_id,action_type,preview_json,status,execution_result_json) "
                                       "VALUES (%s,%s,%s,%s,'create_after_sale','{}','executed','{\"success\":true}')",
                                       (confirmation_id, self.user_one, conv, task_id))
                        cursor.execute("INSERT INTO after_sales "
                                       "(after_sale_no,idempotency_key,user_id,order_id,order_item_id,type,reason,status) "
                                       "VALUES (%s,%s,%s,%s,%s,'repair','质量不好','pending')",
                                       ("AS" + suffix.upper(), confirmation_id, self.user_one, order_id, item_id))
                        cursor.execute("INSERT INTO conversation_messages (conversation_id,role,content) VALUES (%s,'user',%s)",
                                       (conv, f"商品{label}问题"))
                connection.commit()
            finally:
                connection.close()

            detail = client.get(f"/admin/handoff/{handoff['id']}", headers=self._admin()).json()["data"]
            self.assertEqual([sale["product_name"] for sale in detail["after_sales"]], ["商品A"])
            self.assertEqual([m["content"] for m in detail["messages"]], ["商品A问题"])
            self.assertEqual(len(detail["agent_tasks"]), 1)
            self.assertEqual(len(detail["agent_steps"]), 1)
            self.assertEqual(len(detail["agent_results"]), 1)
            self.assertEqual(detail["agent_results"][0]["execution_result"], {"success": True})

    def test_human_reply_is_owned_ordered_and_visible_after_relogin(self):
        from services.conversation_service import save_message

        with patch("middleware.rate_limit.global_limiter.allow", return_value=True), \
             patch("middleware.rate_limit.user_limiter.allow", return_value=True), \
             patch("auth.admin_dependency.get_settings", return_value=SimpleNamespace(admin_api_key=ADMIN_KEY)), \
             self._client() as client:
            headers = self._auth(self.user_one)
            conv_a = client.post("/agent/conversations", headers=headers).json()["data"]["id"]
            conv_b = client.post("/agent/conversations", headers=headers).json()["data"]["id"]
            save_message(self.user_one, conv_a, "user", "我的耳机有问题")
            save_message(self.user_one, conv_a, "assistant", "已为您转接人工客服，请稍候")
            save_message(self.user_one, conv_b, "user", "另一个会话的问题")
            task_a = client.post(f"/agent/conversations/{conv_a}/handoff", json={
                "reason": "需要人工解释",
            }, headers=headers).json()["data"]["id"]
            task_b = client.post(f"/agent/conversations/{conv_b}/handoff", json={
                "reason": "另一个会话",
            }, headers=headers).json()["data"]["id"]
            path_a = f"/admin/handoff/{task_a}/message"
            self.assertEqual(client.post(path_a, json={"content": "请提供订单号"}, headers=self._admin()).status_code, 409)
            self.assertEqual(client.post(path_a, json={"content": "请提供订单号"}).status_code, 401)
            self.assertEqual(client.post(path_a, json={"content": "请提供订单号"}, headers=headers).status_code, 401)
            self.assertEqual(client.post(path_a, json={"content": "请提供订单号"},
                                         headers={"X-Admin-Key": "wrong"}).status_code, 403)
            self.assertEqual(client.post(f"/admin/handoff/{task_a}/accept", headers=self._admin()).status_code, 200)
            self.assertEqual(client.post(path_a, json={"content": "   "}, headers=self._admin()).status_code, 422)
            sent = client.post(path_a, json={"content": "  您好，请提供订单号。  "}, headers=self._admin())
            self.assertEqual(sent.status_code, 200)
            self.assertEqual(sent.json()["data"]["role"], "human_agent")
            self.assertEqual(sent.json()["data"]["conversation_id"], conv_a)
            self.assertEqual(sent.json()["data"]["content"], "您好，请提供订单号。")
            self.assertEqual(client.get(f"/admin/handoff/{task_a}", headers=self._admin()).json()["data"]["messages"][-1]["role"], "human_agent")

            # A new login reads persisted history, not the previous browser session.
            renewed = client.post("/auth/login", json={"user_id": self.user_one}).json()["access_token"]
            history = client.get(f"/agent/conversations/{conv_a}/messages",
                                 headers={"Authorization": f"Bearer {renewed}"}).json()["data"]["messages"]
            self.assertEqual([row["role"] for row in history], ["user", "assistant", "human_agent"])
            self.assertEqual([row["content"] for row in history], [
                "我的耳机有问题", "已为您转接人工客服，请稍候", "您好，请提供订单号。",
            ])
            other_history = client.get(f"/agent/conversations/{conv_b}/messages", headers=headers).json()["data"]["messages"]
            self.assertEqual([row["content"] for row in other_history], ["另一个会话的问题"])
            self.assertEqual(client.get(f"/agent/conversations/{conv_a}/messages",
                                        headers=self._auth(self.user_two)).status_code, 404)
            self.assertEqual(client.post(f"/admin/handoff/{task_b}/message", json={"content": "错误会话"},
                                         headers=self._admin()).status_code, 409)
            self.assertEqual(client.post(f"/admin/handoff/{task_a}/complete", headers=self._admin()).status_code, 200)
            self.assertEqual(client.post(path_a, json={"content": "完成后仍发送"}, headers=self._admin()).status_code, 409)
            with self.assertRaises(ValueError):
                save_message(self.user_one, conv_a, "human_agent", "绕过管理员接口")

    def test_websocket_permissions_delivery_reconnect_and_order(self):
        from datetime import timedelta

        with patch("middleware.rate_limit.global_limiter.allow", return_value=True), \
             patch("middleware.rate_limit.user_limiter.allow", return_value=True), \
             patch("auth.admin_dependency.get_settings", return_value=SimpleNamespace(admin_api_key=ADMIN_KEY)), \
             patch("services.conversation_websocket.get_settings", return_value=SimpleNamespace(admin_api_key=ADMIN_KEY)), \
             self._client() as client:
            headers = self._auth(self.user_one)
            conv_a = client.post("/agent/conversations", headers=headers).json()["data"]["id"]
            conv_b = client.post("/agent/conversations", headers=headers).json()["data"]["id"]
            handoff_id = client.post(f"/agent/conversations/{conv_a}/handoff",
                                     json={"reason": "需要人工"}, headers=headers).json()["data"]["id"]
            path = f"/ws/conversations/{conv_a}"

            for invalid_headers, expected in (
                ({}, 4401),
                (self._auth(self.user_two), 4403),
                ({"Authorization": "Bearer invalid"}, 4401),
                ({"Authorization": "Bearer " + create_access_token(
                    self.user_one, expires_delta=timedelta(seconds=-1))}, 4401),
                ({"X-Admin-Key": "wrong"}, 4403),
                (self._admin(), 4403),  # not assigned yet
            ):
                with self.assertRaises(WebSocketDisconnect) as denied:
                    with client.websocket_connect(path, headers=invalid_headers):
                        pass
                self.assertEqual(denied.exception.code, expected)
            self.assertEqual(client.post(f"/admin/handoff/{handoff_id}/accept",
                                         headers=self._admin()).status_code, 200)
            with self.assertRaises(WebSocketDisconnect) as other_case:
                with client.websocket_connect(f"/ws/conversations/{conv_b}", headers=self._admin()):
                    pass
            self.assertEqual(other_case.exception.code, 4403)

            with client.websocket_connect(path, headers=self._admin()) as agent:
                self.assertEqual(agent.receive_json()["role"], "admin")
                with client.websocket_connect(path, headers=headers) as customer:
                    ready = customer.receive_json()
                    self.assertEqual(ready["role"], "user")
                    self.assertTrue(ready["handoff_assigned"])
                    agent.send_json({"type": "message", "content": "您好，请提供订单号"})
                    first = customer.receive_json()
                    self.assertEqual(first["type"], "message")
                    self.assertEqual(first["message"]["role"], "human_agent")
                    self.assertEqual(first["message"]["content"], "您好，请提供订单号")
                    self.assertEqual(agent.receive_json()["message"]["id"], first["message"]["id"])
                    customer.send_json({"type": "message", "role": "human_agent", "content": "订单号 123"})
                    customer.receive_json()  # own committed message echo
                    second = agent.receive_json()
                    self.assertEqual(second["message"]["role"], "user")
                    self.assertGreater(second["message"]["id"], first["message"]["id"])

                # Customer reconnects after a message was delivered while offline.
                agent.send_json({"type": "message", "content": "已经查到订单"})
                third = agent.receive_json()
                self.assertGreater(third["message"]["id"], second["message"]["id"])
                with client.websocket_connect(path, headers=headers) as reconnected:
                    reconnected.receive_json()
                    reconnected.send_json({"type": "sync", "after_id": first["message"]["id"]})
                    replay = [reconnected.receive_json(), reconnected.receive_json()]
                    self.assertEqual([row["message"]["content"] for row in replay],
                                     ["订单号 123", "已经查到订单"])
                    self.assertEqual(reconnected.receive_json()["type"], "sync_complete")

            history = client.get(f"/agent/conversations/{conv_a}/messages",
                                 headers=headers).json()["data"]["messages"]
            self.assertEqual([row["role"] for row in history], ["human_agent", "user", "human_agent"])
            self.assertEqual([row["id"] for row in history], sorted(row["id"] for row in history))
            other = client.get(f"/agent/conversations/{conv_b}/messages",
                               headers=headers).json()["data"]["messages"]
            self.assertEqual(other, [])

    def test_chat_http_messages_are_broadcast_as_user_then_assistant(self):
        with patch("middleware.rate_limit.global_limiter.allow", return_value=True), \
             patch("middleware.rate_limit.user_limiter.allow", return_value=True), \
             self._client() as client:
            headers = self._auth(self.user_one)
            conversation_id = client.post("/agent/conversations", headers=headers).json()["data"]["id"]
            with client.websocket_connect(f"/ws/conversations/{conversation_id}", headers=headers) as live:
                live.receive_json()
                response = client.post("/agent/chat", json={
                    "conversation_id": conversation_id, "message": "转人工",
                }, headers=headers)
                self.assertEqual(response.json()["code"], "HANDOFF_REQUESTED")
                events = [live.receive_json(), live.receive_json()]
                self.assertEqual([event["message"]["role"] for event in events],
                                 ["user", "assistant"])
                self.assertLess(events[0]["message"]["id"], events[1]["message"]["id"])

    def test_workbench_http_reply_reaches_open_customer_socket(self):
        with patch("middleware.rate_limit.global_limiter.allow", return_value=True), \
             patch("middleware.rate_limit.user_limiter.allow", return_value=True), \
             patch("auth.admin_dependency.get_settings", return_value=SimpleNamespace(admin_api_key=ADMIN_KEY)), \
             self._client() as client:
            headers = self._auth(self.user_one)
            conversation_id = client.post("/agent/conversations", headers=headers).json()["data"]["id"]
            handoff_id = client.post(f"/agent/conversations/{conversation_id}/handoff",
                                     json={"reason": "人工服务"}, headers=headers).json()["data"]["id"]
            client.post(f"/admin/handoff/{handoff_id}/accept", headers=self._admin())
            with client.websocket_connect(f"/ws/conversations/{conversation_id}", headers=headers) as live:
                live.receive_json()
                response = client.post(f"/admin/handoff/{handoff_id}/message",
                                       json={"content": "已为您接入客服"}, headers=self._admin())
                self.assertEqual(response.status_code, 200)
                event = live.receive_json()
                self.assertEqual(event["message"]["id"], response.json()["data"]["id"])
                self.assertEqual(event["message"]["role"], "human_agent")
