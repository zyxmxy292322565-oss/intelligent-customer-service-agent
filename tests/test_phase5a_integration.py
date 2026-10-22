"""Opt-in Stage 5A checks against a separately verified MySQL/RAG stack.

Never point this at the demo database. The opt-in guard deliberately requires
the exact isolated container name and loopback port established for this run.
"""

import os
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
import json
from datetime import datetime, timedelta
from unittest.mock import patch
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

import main
from agent.graph import runtime as graph_runtime
from agent.graph import state_graph
from langgraph.checkpoint.sqlite import SqliteSaver
from auth.jwt import create_access_token
from schemas.agent_schema import BusinessParams, IntentResult

from db import connection as db_connection
from db.init_db import create_tables
from services.after_sale_service import create_after_sale_service
from services.policy_check_service import check_after_sale_policy
from services.policy_demo_rules import DEMO_POLICY_SOURCES
from services.policy_rag_client import query_policy_rag


@unittest.skipUnless(os.getenv("RUN_PHASE5A_TESTS") == "1", "requires isolated Stage 5A services")
class Phase5AIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if (os.getenv("PHASE5A_ISOLATED_CONTAINER") != "phase5a-mysql-20261015"
                or db_connection.MYSQL_HOST != "127.0.0.1"
                or db_connection.MYSQL_PORT != 3312):
            raise RuntimeError("Stage 5A tests must use the verified isolated MySQL container")
        cls.database_name = "phase5a_test_" + uuid.uuid4().hex[:12]
        server = db_connection.get_server_connection()
        try:
            with server.cursor() as cursor:
                cursor.execute(f"CREATE DATABASE `{cls.database_name}` CHARACTER SET utf8mb4")
            server.commit()
        finally:
            server.close()
        cls.database_patch = patch.object(db_connection, "MYSQL_DATABASE", cls.database_name)
        cls.database_patch.start()
        create_tables()

    @classmethod
    def tearDownClass(cls):
        cls.database_patch.stop()
        if cls.database_name.startswith("phase5a_test_"):
            server = db_connection.get_server_connection()
            try:
                with server.cursor() as cursor:
                    cursor.execute(f"DROP DATABASE `{cls.database_name}`")
                server.commit()
            finally:
                server.close()

    def setUp(self):
        self.user_id, self.order_no, self.item_id, self.other_item_id = self._seed()
        source = DEMO_POLICY_SOURCES["耳机类商品规则.txt"]
        self.policy = {
            "decision": "ELIGIBLE_TO_APPLY", "policy_type": "earphone_quality_return",
            "policy_message": "可以提出申请，仍需核验", "requires_review": True,
            "days_since_delivery": 20, "policy_deadline_days": 30,
            "sources": [source.filename],
            "policy_versions": {source.filename: source.document_version_label},
            "policy_file_hashes": {source.filename: source.file_hash},
            "evidence": [{"source": source.filename, "file_hash": source.file_hash,
                          "content": "耳机质量问题可提出申请"}],
        }

    def _seed(self):
        con = db_connection.get_connection()
        try:
            with con.cursor() as cur:
                cur.execute("INSERT INTO users (username) VALUES (%s)", ("phase5a_" + uuid.uuid4().hex[:8],))
                user_id = cur.lastrowid
                order_no = "ORD5A" + uuid.uuid4().hex[:12]
                cur.execute(
                    "INSERT INTO orders (order_no,user_id,total_amount,status,delivered_at) "
                    "VALUES (%s,%s,%s,'delivered',%s)",
                    (order_no, user_id, 2998, datetime.now() - timedelta(days=20)),
                )
                order_id = cur.lastrowid
                item_ids = []
                for name in ("AirPods Pro 2", "备用耳机"):
                    cur.execute(
                        "INSERT INTO order_items (order_id,product_name,category,quantity,unit_price) "
                        "VALUES (%s,%s,'耳机',1,1499)", (order_id, name),
                    )
                    item_ids.append(cur.lastrowid)
            con.commit()
            return user_id, order_no, *item_ids
        finally:
            con.close()

    def _rows(self, item_id=None):
        con = db_connection.get_connection()
        try:
            with con.cursor() as cur:
                cur.execute("SELECT * FROM orders WHERE order_no=%s", (self.order_no,))
                order = cur.fetchone()
                cur.execute("SELECT * FROM order_items WHERE id=%s", (item_id or self.item_id,))
                item = cur.fetchone()
            return order, item
        finally:
            con.close()

    def _count(self, item_id=None):
        con = db_connection.get_connection()
        try:
            with con.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS n FROM after_sales WHERE order_item_id=%s", (item_id or self.item_id,))
                return cur.fetchone()["n"]
        finally:
            con.close()

    def _create(self, key, item_id=None):
        return create_after_sale_service(
            user_id=self.user_id, order_no=self.order_no,
            order_item_id=item_id or self.item_id, after_sale_type="return_refund",
            reason="单侧无声，疑似质量问题", idempotency_key=key,
        )

    def test_real_rag_policy_scenarios(self):
        order, item = self._rows()
        def evaluate(reason, *, verified=False):
            current_order, current_item = self._rows()
            return check_after_sale_policy(
                user_id=self.user_id, order=current_order, order_item=current_item,
                after_sale_type="return_refund", reason=reason,
                item_condition_verified=verified, rag_query=query_policy_rag,
            )

        def set_delivery(days):
            con = db_connection.get_connection()
            try:
                with con.cursor() as cur:
                    cur.execute("UPDATE orders SET delivered_at=%s WHERE order_no=%s",
                                (None if days is None else datetime.now() - timedelta(days=days), self.order_no))
                con.commit()
            finally:
                con.close()

        set_delivery(5)
        self.assertEqual(evaluate("不喜欢，申请无理由退货", verified=True)["decision"],
                         "ELIGIBLE_TO_APPLY")
        set_delivery(10)
        self.assertEqual(evaluate("不喜欢，申请无理由退货", verified=True)["decision"],
                         "INELIGIBLE")
        set_delivery(20)
        result = evaluate("单侧无声，疑似质量问题")
        self.assertEqual(result["decision"], "ELIGIBLE_TO_APPLY", result)
        self.assertTrue(result["requires_review"])
        self.assertEqual(result["policy_file_hashes"]["耳机类商品规则.txt"],
                         DEMO_POLICY_SOURCES["耳机类商品规则.txt"].file_hash)
        set_delivery(31)
        self.assertEqual(evaluate("单侧无声，疑似质量问题")["decision"], "NEEDS_REVIEW")
        item["category"] = "电脑配件"
        self.assertEqual(check_after_sale_policy(
            user_id=self.user_id, order=order, order_item=item,
            after_sale_type="return_refund", reason="左键失灵", rag_query=query_policy_rag,
        )["decision"], "NEEDS_REVIEW")
        set_delivery(None)
        self.assertEqual(evaluate("单侧无声")["decision"], "NEEDS_REVIEW")

    def test_real_rag_outage_fails_closed(self):
        import services.policy_rag_client as client
        order, item = self._rows()
        with patch.object(client, "POLICY_RAG_SERVICE_URL", "http://127.0.0.1:18999"):
            result = check_after_sale_policy(
                user_id=self.user_id, order=order, order_item=item,
                after_sale_type="return_refund", reason="单侧无声",
                rag_query=query_policy_rag,
            )
        self.assertEqual(result["decision"], "POLICY_UNAVAILABLE")

    def _agent_http(self, *, graph=False):
        with patch("main.get_settings", return_value=SimpleNamespace(use_langgraph_agent=graph)), \
             patch("agent.input_analyzer.route_intent", return_value=IntentResult(intent="create_after_sale")), \
             patch("agent.input_analyzer.extract_business_params", return_value=BusinessParams(
                 product_name="AirPods Pro 2", after_sale_type="return_refund",
                 reason="单侧无声，疑似质量问题",
             )), \
             patch("middleware.rate_limit.global_limiter.allow", return_value=True), \
             patch("middleware.rate_limit.user_limiter.allow", return_value=True), \
             patch("services.policy_rag_client.httpx.post", wraps=httpx.post) as rag_http_calls, \
             TestClient(main.app) as client:
            headers = {"Authorization": f"Bearer {create_access_token(self.user_id)}"}
            chat = client.post("/agent/chat", headers=headers, json={
                "user_id": 999999, "message": "我要退AirPods Pro 2，单侧无声，质量问题，退货退款",
            })
            self.assertEqual(chat.status_code, 200, chat.text)
            result = chat.json()
            self.assertEqual(result["code"], "ACTION_PREVIEW_READY", result)
            confirmation_id = result["data"]["confirmation_id"]
            self.assertEqual(self._count(), 0, "Preview must not create an after-sale")
            confirm = client.post("/agent/confirm", headers=headers, json={
                "user_id": 999999, "confirmation_id": confirmation_id,
            })
            self.assertEqual(confirm.status_code, 200, confirm.text)
            self.assertEqual(confirm.json()["code"], "ACTION_EXECUTED", confirm.json())
            replay = client.post("/agent/confirm", headers=headers, json={
                "confirmation_id": confirmation_id,
            })
            self.assertEqual(replay.json()["code"], "IDEMPOTENT_REPLAY", replay.json())
            self.assertEqual(self._count(), 1)
            con = db_connection.get_connection()
            try:
                with con.cursor() as cur:
                    cur.execute("SELECT status,task_id,preview_json FROM agent_pending_actions "
                                "WHERE confirmation_id=%s", (confirmation_id,))
                    action = cur.fetchone()
                    cur.execute("SELECT status FROM agent_tasks WHERE id=%s", (action["task_id"],))
                    task = cur.fetchone()
                    cur.execute("SELECT status FROM after_sales WHERE order_item_id=%s", (self.item_id,))
                    after_sale = cur.fetchone()
                self.assertEqual(action["status"], "executed")
                self.assertEqual(task["status"], "completed")
                self.assertEqual(after_sale["status"], "pending")
                self.assertIn("policy_snapshot", action["preview_json"])
            finally:
                con.close()
            return rag_http_calls.call_count

    def test_legacy_http_preview_confirm_real_rag(self):
        count = self._agent_http(graph=False)
        self.assertEqual(count, 2, "Preview and execution preflight each retrieve real evidence")
        print("STAGE5A legacy_rag_calls", count)

    def test_graph_http_preview_confirm_real_rag(self):
        # The Graph checkpoint is redirected to a disposable SQLite file, not
        # the workspace's existing Demo checkpoint.
        with tempfile.TemporaryDirectory(prefix="phase5a_graph_") as tempdir:
            checkpoint_connection = sqlite3.connect(
                os.path.join(tempdir, "checkpoints.sqlite3"), check_same_thread=False,
            )
            saver = SqliteSaver(checkpoint_connection, serde=state_graph.CHECKPOINT_SERIALIZER)
            try:
                with patch.object(state_graph, "CHECKPOINTER", saver):
                    graph_runtime._get_graph.cache_clear()
                    count = self._agent_http(graph=True)
            finally:
                graph_runtime._get_graph.cache_clear()
                checkpoint_connection.close()
        self.assertEqual(count, 3, "Graph planning retrieves policy once before Preview and once at each policy gate")
        print("STAGE5A graph_rag_calls", count)

    def test_direct_http_uses_real_policy_gate_and_token_owner(self):
        with patch("middleware.rate_limit.global_limiter.allow", return_value=True), \
             patch("middleware.rate_limit.user_limiter.allow", return_value=True), \
             TestClient(main.app) as client:
            headers = {"Authorization": f"Bearer {create_access_token(self.user_id)}"}
            body = {"user_id": 999999, "order_no": self.order_no,
                    "order_item_id": self.item_id, "type": "return_refund",
                    "reason": "单侧无声，疑似质量问题"}
            response = client.post("/after-sales", headers=headers, json=body)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["data"]["user_id"], self.user_id)
            self.assertEqual(self._count(), 1)

    def test_confirmed_rag_outage_retries_same_confirmation_id(self):
        import services.policy_rag_client as rag_client
        with patch("main.get_settings", return_value=SimpleNamespace(use_langgraph_agent=False)), \
             patch("agent.input_analyzer.route_intent", return_value=IntentResult(intent="create_after_sale")), \
             patch("agent.input_analyzer.extract_business_params", return_value=BusinessParams(
                 product_name="AirPods Pro 2", after_sale_type="return_refund",
                 reason="单侧无声，疑似质量问题",
             )), \
             patch("middleware.rate_limit.global_limiter.allow", return_value=True), \
             patch("middleware.rate_limit.user_limiter.allow", return_value=True), \
             TestClient(main.app) as client:
            headers = {"Authorization": f"Bearer {create_access_token(self.user_id)}"}
            preview = client.post("/agent/chat", headers=headers, json={
                "message": "AirPods Pro 2单侧无声，申请退货退款",
            }).json()
            self.assertEqual(preview["code"], "ACTION_PREVIEW_READY", preview)
            resource = preview["data"]["confirmation_id"]
            with patch.object(rag_client, "POLICY_RAG_SERVICE_URL", "http://127.0.0.1:18999"):
                outage = client.post("/agent/confirm", headers=headers, json={
                    "confirmation_id": resource,
                }).json()
            self.assertEqual(outage["code"], "POLICY_UNAVAILABLE", outage)
            con = db_connection.get_connection()
            try:
                with con.cursor() as cur:
                    cur.execute("SELECT status FROM agent_pending_actions WHERE confirmation_id=%s", (resource,))
                    self.assertEqual(cur.fetchone()["status"], "confirmed")
            finally:
                con.close()
            self.assertEqual(self._count(), 0)
            retry = client.post("/agent/confirm", headers=headers, json={
                "confirmation_id": resource,
            }).json()
            self.assertEqual(retry["code"], "ACTION_EXECUTED", retry)
            replay = client.post("/agent/confirm", headers=headers, json={
                "confirmation_id": resource,
            }).json()
            self.assertEqual(replay["code"], "IDEMPOTENT_REPLAY", replay)
            self.assertEqual(self._count(), 1)

    def test_delivery_change_invalidates_real_preview(self):
        with patch("main.get_settings", return_value=SimpleNamespace(use_langgraph_agent=False)), \
             patch("agent.input_analyzer.route_intent", return_value=IntentResult(intent="create_after_sale")), \
             patch("agent.input_analyzer.extract_business_params", return_value=BusinessParams(
                 product_name="AirPods Pro 2", after_sale_type="return_refund",
                 reason="单侧无声，疑似质量问题",
             )), \
             patch("middleware.rate_limit.global_limiter.allow", return_value=True), \
             patch("middleware.rate_limit.user_limiter.allow", return_value=True), \
             TestClient(main.app) as client:
            headers = {"Authorization": f"Bearer {create_access_token(self.user_id)}"}
            preview = client.post("/agent/chat", headers=headers, json={
                "message": "AirPods Pro 2单侧无声，申请退货退款",
            }).json()
            self.assertEqual(preview["code"], "ACTION_PREVIEW_READY", preview)
            resource = preview["data"]["confirmation_id"]
            con = db_connection.get_connection()
            try:
                with con.cursor() as cur:
                    cur.execute("UPDATE orders SET delivered_at=%s WHERE order_no=%s",
                                (datetime.now() - timedelta(days=22), self.order_no))
                con.commit()
            finally:
                con.close()
            confirm = client.post("/agent/confirm", headers=headers, json={
                "confirmation_id": resource,
            }).json()
            self.assertEqual(confirm["code"], "PREVIEW_RECHECK_REQUIRED", confirm)
            self.assertEqual(self._count(), 0)

    def test_uncovered_product_and_rag_outage_make_no_pending_action(self):
        import services.policy_rag_client as rag_client
        with patch("main.get_settings", return_value=SimpleNamespace(use_langgraph_agent=False)), \
             patch("agent.input_analyzer.route_intent", return_value=IntentResult(intent="create_after_sale")), \
             patch("agent.input_analyzer.extract_business_params", return_value=BusinessParams(
                 product_name="AirPods Pro 2", after_sale_type="return_refund",
                 reason="单侧无声，疑似质量问题",
             )), \
             patch("middleware.rate_limit.global_limiter.allow", return_value=True), \
             patch("middleware.rate_limit.user_limiter.allow", return_value=True), \
             TestClient(main.app) as client:
            headers = {"Authorization": f"Bearer {create_access_token(self.user_id)}"}
            con = db_connection.get_connection()
            try:
                with con.cursor() as cur:
                    cur.execute("UPDATE order_items SET category='电脑配件' WHERE id=%s", (self.item_id,))
                con.commit()
            finally:
                con.close()
            review = client.post("/agent/chat", headers=headers, json={
                "message": "AirPods Pro 2单侧无声，申请退货退款",
            }).json()
            self.assertEqual(review["code"], "NEEDS_REVIEW", review)
            con = db_connection.get_connection()
            try:
                with con.cursor() as cur:
                    cur.execute("SELECT COUNT(*) AS n FROM agent_pending_actions WHERE user_id=%s", (self.user_id,))
                    self.assertEqual(cur.fetchone()["n"], 0)
                    cur.execute("UPDATE order_items SET category='耳机' WHERE id=%s", (self.item_id,))
                con.commit()
            finally:
                con.close()
            with patch.object(rag_client, "POLICY_RAG_SERVICE_URL", "http://127.0.0.1:18999"):
                unavailable = client.post("/agent/chat", headers=headers, json={
                    "message": "AirPods Pro 2单侧无声，申请退货退款",
                }).json()
            self.assertEqual(unavailable["code"], "POLICY_UNAVAILABLE", unavailable)
            con = db_connection.get_connection()
            try:
                with con.cursor() as cur:
                    cur.execute("SELECT COUNT(*) AS n FROM agent_pending_actions WHERE user_id=%s", (self.user_id,))
                    self.assertEqual(cur.fetchone()["n"], 0)
            finally:
                con.close()

    def test_different_keys_one_item_real_lock_wait_and_read_committed(self):
        import services.after_sale_service as service
        original = service.recheck_policy_under_lock
        entered = threading.Event()
        first_key = "5A-" + uuid.uuid4().hex
        second_key = "5A-" + uuid.uuid4().hex

        def delayed(*args, **kwargs):
            if not entered.is_set():
                entered.set()
                time.sleep(1.0)
            return original(*args, **kwargs)

        def run(key):
            start = time.monotonic()
            result = self._create(key)
            return result["code"], round(time.monotonic() - start, 2)

        with patch.object(service, "recheck_policy_under_lock", side_effect=delayed), \
             patch("services.policy_execution_service.check_after_sale_policy", return_value=self.policy), \
             ThreadPoolExecutor(max_workers=2, thread_name_prefix="first-create") as pool:
            first = pool.submit(run, first_key)
            self.assertTrue(entered.wait(10))
            second = pool.submit(run, second_key)
            results = (first.result(timeout=15), second.result(timeout=15))
        self.assertEqual(results[0][0], "SUCCESS", results)
        self.assertEqual(results[1][0], "AFTER_SALE_ALREADY_EXISTS", results)
        self.assertEqual(self._count(), 1)
        print("STAGE5A different_keys", results, "final_count", self._count())

    def test_different_items_do_not_block_each_other(self):
        import services.after_sale_service as service
        original = service.recheck_policy_under_lock
        entered = threading.Event()

        def delayed(*args, **kwargs):
            if kwargs["item"]["id"] == self.item_id:
                entered.set()
                time.sleep(1.0)
            return original(*args, **kwargs)

        with patch.object(service, "recheck_policy_under_lock", side_effect=delayed), \
             patch("services.policy_execution_service.check_after_sale_policy", return_value=self.policy), \
             ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self._create, "5A-" + uuid.uuid4().hex, self.item_id)
            self.assertTrue(entered.wait(10))
            start = time.monotonic()
            second = pool.submit(self._create, "5A-" + uuid.uuid4().hex, self.other_item_id)
            second_result = second.result(timeout=10)
            second_elapsed = time.monotonic() - start
            first_result = first.result(timeout=10)
        self.assertEqual((first_result["code"], second_result["code"]), ("SUCCESS", "SUCCESS"))
        self.assertLess(second_elapsed, 0.9)
        self.assertEqual((self._count(self.item_id), self._count(self.other_item_id)), (1, 1))
        print("STAGE5A different_items second_elapsed", round(second_elapsed, 2))

    def test_same_idempotency_key_replays_without_duplicate(self):
        key = "5A-" + uuid.uuid4().hex
        with patch("services.policy_execution_service.check_after_sale_policy", return_value=self.policy):
            first = self._create(key)
            second = self._create(key)
        self.assertEqual((first["code"], second["code"]), ("SUCCESS", "IDEMPOTENT_REPLAY"))
        self.assertEqual(self._count(), 1)

    def test_lock_wait_timeout_rolls_back_without_insert(self):
        import services.after_sale_service as service
        blocker = db_connection.get_connection()
        original_get_connection = db_connection.get_connection
        try:
            with blocker.cursor() as cur:
                cur.execute("SELECT id FROM order_items WHERE id=%s FOR UPDATE", (self.item_id,))

            def short_timeout_connection():
                con = original_get_connection()
                with con.cursor() as cur:
                    cur.execute("SET SESSION innodb_lock_wait_timeout=1")
                return con

            with patch.object(service, "get_connection", side_effect=short_timeout_connection), \
                 patch("services.policy_execution_service.check_after_sale_policy", return_value=self.policy):
                result = self._create("5A-" + uuid.uuid4().hex)
            self.assertEqual(result["code"], "DATABASE_RETRYABLE", result)
        finally:
            blocker.rollback()
            blocker.close()
        self.assertEqual(self._count(), 0)

    def test_insert_and_status_log_are_atomic(self):
        with patch("services.policy_execution_service.check_after_sale_policy", return_value=self.policy), \
             patch("services.after_sale_service.create_after_sale_status_log", side_effect=RuntimeError("test rollback")):
            with self.assertRaisesRegex(RuntimeError, "test rollback"):
                self._create("5A-" + uuid.uuid4().hex)
        self.assertEqual(self._count(), 0)

    def test_two_distinct_actions_concurrent_http_confirm(self):
        from services.conversation_service import create_conversation
        from schemas.agent_schema import BusinessParams, IntentResult
        conversation_ids = [create_conversation(self.user_id)["id"] for _ in range(2)]
        headers = {"Authorization": f"Bearer {create_access_token(self.user_id)}"}
        with patch("main.get_settings", return_value=SimpleNamespace(use_langgraph_agent=False)), \
             patch("agent.input_analyzer.route_intent", return_value=IntentResult(intent="create_after_sale")), \
             patch("agent.input_analyzer.extract_business_params", return_value=BusinessParams(
                 product_name="AirPods Pro 2", after_sale_type="return_refund",
                 reason="单侧无声，疑似质量问题")), \
             patch("middleware.rate_limit.global_limiter.allow", return_value=True), \
             patch("middleware.rate_limit.user_limiter.allow", return_value=True), \
             TestClient(main.app) as client:
            resources = []
            for conversation_id in conversation_ids:
                preview = client.post("/agent/chat", headers=headers, json={
                    "conversation_id": conversation_id,
                    "message": "AirPods Pro 2单侧无声，申请退货退款",
                }).json()
                self.assertEqual(preview["code"], "ACTION_PREVIEW_READY", preview)
                resources.append(preview["data"]["confirmation_id"])
            self.assertNotEqual(*resources)
            gate = threading.Barrier(2)

            def confirm(resource, conversation_id):
                gate.wait(timeout=10)
                with TestClient(main.app) as other_client:
                    return other_client.post("/agent/confirm", headers=headers, json={
                        "conversation_id": conversation_id, "confirmation_id": resource,
                    }).json()

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(confirm, resource, conversation_id)
                           for resource, conversation_id in zip(resources, conversation_ids)]
                results = [future.result(timeout=30) for future in futures]
            con = db_connection.get_connection()
            try:
                with con.cursor() as cur:
                    cur.execute("SELECT confirmation_id,status,task_id FROM agent_pending_actions "
                                "WHERE confirmation_id IN (%s,%s)", tuple(resources))
                    actions = {row["confirmation_id"]: row for row in cur.fetchall()}
                    cur.execute("SELECT COUNT(*) AS n FROM after_sales WHERE order_item_id=%s", (self.item_id,))
                    count = cur.fetchone()["n"]
            finally:
                con.close()
            self.assertEqual(count, 1)
            self.assertEqual(sum(result["code"] == "ACTION_EXECUTED" for result in results), 1, results)
            self.assertEqual(sum(action["status"] == "executed" for action in actions.values()), 1)
            self.assertTrue(all(action["status"] != "executing" for action in actions.values()))
            self.assertTrue(all(result["code"] != "ACTION_EXECUTED" or result["success"]
                                for result in results))
            print("STAGE5A distinct_actions", resources, [r["code"] for r in results],
                  {k: v["status"] for k, v in actions.items()}, "sales", count)

    def test_real_innodb_deadlock_rolls_back_victim(self):
        barrier = threading.Barrier(2)
        results = []
        result_lock = threading.Lock()

        def transaction(first, second, marker):
            con = db_connection.get_connection()
            try:
                with con.cursor() as cur:
                    cur.execute("SET SESSION innodb_lock_wait_timeout=5")
                    cur.execute("UPDATE order_items SET unit_price=%s WHERE id=%s", (marker, first))
                    barrier.wait(timeout=8)
                    cur.execute("SELECT id FROM order_items WHERE id=%s FOR UPDATE", (second,))
                con.commit()
                outcome = ("committed", None)
            except Exception as exc:
                con.rollback()
                outcome = ("rolled_back", exc.args[0] if exc.args else None)
            finally:
                con.close()
            with result_lock:
                results.append(outcome)

        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(transaction, self.item_id, self.other_item_id, 1601)
            b = pool.submit(transaction, self.other_item_id, self.item_id, 1602)
            a.result(timeout=15)
            b.result(timeout=15)
        self.assertEqual(sorted(results), [("committed", None), ("rolled_back", 1213)], results)
        con = db_connection.get_connection()
        try:
            with con.cursor() as cur:
                cur.execute("SELECT unit_price FROM order_items WHERE id IN (%s,%s)",
                            (self.item_id, self.other_item_id))
                prices = [int(row["unit_price"]) for row in cur.fetchall()]
        finally:
            con.close()
        self.assertEqual(sum(price != 1499 for price in prices), 1, prices)
        self.assertEqual(self._count(), 0)
        print("STAGE5A deadlock", results, "item_prices", prices)


    @unittest.skipUnless(os.getenv("RUN_PHASE5A_LLM_TESTS") == "1", "requires configured live LLM")
    def test_live_llm_three_turn_same_task_real_rag(self):
        with patch("main.get_settings", return_value=SimpleNamespace(use_langgraph_agent=False)), \
             patch("middleware.rate_limit.global_limiter.allow", return_value=True), \
             patch("middleware.rate_limit.user_limiter.allow", return_value=True), \
             TestClient(main.app) as client:
            headers = {"Authorization": f"Bearer {create_access_token(self.user_id)}"}
            conversation_id = None
            results = []
            for message in ("我要申请AirPods Pro 2售后", "退货退款", "左边耳机没有声音"):
                body = {"message": message}
                if conversation_id is not None:
                    body["conversation_id"] = conversation_id
                response = client.post("/agent/chat", headers=headers, json=body)
                self.assertEqual(response.status_code, 200, response.text)
                results.append(response.json())
                con = db_connection.get_connection()
                try:
                    with con.cursor() as cur:
                        cur.execute("SELECT id FROM conversations WHERE user_id=%s ORDER BY id DESC LIMIT 1",
                                    (self.user_id,))
                        conversation_id = cur.fetchone()["id"]
                finally:
                    con.close()
            self.assertEqual([r["code"] for r in results], [
                "AFTER_SALE_TYPE_REQUIRED", "AFTER_SALE_REASON_REQUIRED", "ACTION_PREVIEW_READY",
            ], results)
            self.assertEqual(self._count(), 0)
            self.assertEqual(results[2]["data"]["preview"]["order_item_id"], self.item_id)
            print("STAGE5A live_llm_three_turn", [r["code"] for r in results])

    @unittest.skipUnless(os.getenv("RUN_PHASE5A_LLM_TESTS") == "1", "requires configured live LLM")
    def test_live_llm_graph_three_turn_same_task_real_rag(self):
        """Graph must recover business context from MySQL, not its checkpoint."""
        with tempfile.TemporaryDirectory(prefix="phase5a_graph_multiturn_") as tempdir:
            checkpoint_connection = sqlite3.connect(
                os.path.join(tempdir, "checkpoints.sqlite3"), check_same_thread=False,
            )
            saver = SqliteSaver(checkpoint_connection, serde=state_graph.CHECKPOINT_SERIALIZER)
            try:
                with patch.object(state_graph, "CHECKPOINTER", saver), \
                     patch("main.get_settings", return_value=SimpleNamespace(use_langgraph_agent=True)), \
                     patch("middleware.rate_limit.global_limiter.allow", return_value=True), \
                     patch("middleware.rate_limit.user_limiter.allow", return_value=True), \
                     TestClient(main.app) as client:
                    graph_runtime._get_graph.cache_clear()
                    headers = {"Authorization": f"Bearer {create_access_token(self.user_id)}"}
                    conversation_id = None
                    results = []
                    for message in (
                        "我要申请AirPods Pro 2售后",
                        "退货退款",
                        "左边耳机没有声音",
                    ):
                        body = {"message": message}
                        if conversation_id is not None:
                            body["conversation_id"] = conversation_id
                        response = client.post("/agent/chat", headers=headers, json=body)
                        self.assertEqual(response.status_code, 200, response.text)
                        results.append(response.json())
                        con = db_connection.get_connection()
                        try:
                            with con.cursor() as cur:
                                cur.execute(
                                    "SELECT id FROM conversations WHERE user_id=%s "
                                    "ORDER BY id DESC LIMIT 1", (self.user_id,),
                                )
                                conversation_id = cur.fetchone()["id"]
                        finally:
                            con.close()
            finally:
                graph_runtime._get_graph.cache_clear()
                checkpoint_connection.close()

        self.assertEqual([result["code"] for result in results], [
            "AFTER_SALE_TYPE_REQUIRED",
            "AFTER_SALE_REASON_REQUIRED",
            "ACTION_PREVIEW_READY",
        ], results)
        self.assertEqual(self._count(), 0)
        self.assertEqual(results[2]["data"]["preview"]["order_item_id"], self.item_id)
        self.assertEqual(len({result["trace_id"] for result in results}), 3)
        con = db_connection.get_connection()
        try:
            with con.cursor() as cur:
                cur.execute(
                    "SELECT id,context FROM agent_tasks WHERE user_id=%s AND conversation_id=%s",
                    (self.user_id, conversation_id),
                )
                tasks = cur.fetchall()
            self.assertEqual(len(tasks), 1, tasks)
            context = tasks[0]["context"]
            context = json.loads(context) if isinstance(context, str) else context
            self.assertEqual(context["product_name"], "AirPods Pro 2")
            self.assertEqual(context["after_sale_type"], "return_refund")
            self.assertIn("没有声音", context["original_reason_text"])
        finally:
            con.close()
        print("STAGE5A live_llm_graph_three_turn", [r["code"] for r in results])

    @unittest.skipUnless(os.getenv("RUN_PHASE5A_LLM_TESTS") == "1", "requires configured live LLM")
    def test_live_llm_one_shot_real_rag(self):
        with patch("main.get_settings", return_value=SimpleNamespace(use_langgraph_agent=False)), \
             patch("middleware.rate_limit.global_limiter.allow", return_value=True), \
             patch("middleware.rate_limit.user_limiter.allow", return_value=True), \
             TestClient(main.app) as client:
            headers = {"Authorization": f"Bearer {create_access_token(self.user_id)}"}
            result = client.post("/agent/chat", headers=headers, json={
                "message": "我的AirPods Pro 2左边没有声音，想申请退货退款。",
            }).json()
        self.assertEqual(result["code"], "ACTION_PREVIEW_READY", result)
        preview = result["data"]["preview"]
        self.assertEqual(preview["product_name"], "AirPods Pro 2")
        self.assertEqual(preview["after_sale_type"], "return_refund")
        self.assertEqual(preview["order_item_id"], self.item_id)
        self.assertEqual(self._count(), 0)
        print("STAGE5A live_llm_one_shot", result["code"], preview["order_item_id"])

    def test_old_pending_action_without_snapshot_and_foreign_user_http(self):
        with patch("main.get_settings", return_value=SimpleNamespace(use_langgraph_agent=False)), \
             patch("agent.input_analyzer.route_intent", return_value=IntentResult(intent="create_after_sale")), \
             patch("agent.input_analyzer.extract_business_params", return_value=BusinessParams(
                 product_name="AirPods Pro 2", after_sale_type="return_refund",
                 reason="单侧无声，疑似质量问题")), \
             patch("middleware.rate_limit.global_limiter.allow", return_value=True), \
             patch("middleware.rate_limit.user_limiter.allow", return_value=True), \
             TestClient(main.app) as client:
            owner = {"Authorization": f"Bearer {create_access_token(self.user_id)}"}
            preview = client.post("/agent/chat", headers=owner, json={
                "message": "AirPods Pro 2质量问题，申请退货退款",
            }).json()
            self.assertEqual(preview["code"], "ACTION_PREVIEW_READY", preview)
            resource = preview["data"]["confirmation_id"]
            con = db_connection.get_connection()
            try:
                with con.cursor() as cur:
                    cur.execute("SELECT preview_json FROM agent_pending_actions WHERE confirmation_id=%s", (resource,))
                    raw = cur.fetchone()["preview_json"]
                    value = json.loads(raw) if isinstance(raw, str) else raw
                    value.pop("policy_snapshot", None)
                    cur.execute("UPDATE agent_pending_actions SET preview_json=%s WHERE confirmation_id=%s",
                                (json.dumps(value, ensure_ascii=False), resource))
                    cur.execute("INSERT INTO users (username) VALUES ('phase5a_foreign')")
                    foreign_id = cur.lastrowid
                con.commit()
            finally:
                con.close()
            foreign = {"Authorization": f"Bearer {create_access_token(foreign_id)}"}
            denied = client.post("/agent/confirm", headers=foreign, json={
                "confirmation_id": resource,
            }).json()
            self.assertEqual(denied["code"], "ACTION_NOT_FOUND", denied)
            rejected = client.post("/agent/confirm", headers=owner, json={
                "confirmation_id": resource,
            }).json()
            self.assertEqual(rejected["code"], "PREVIEW_RECHECK_REQUIRED", rejected)
            self.assertEqual(self._count(), 0)

    def test_executed_history_replays_even_if_old_preview_has_no_snapshot(self):
        with patch("main.get_settings", return_value=SimpleNamespace(use_langgraph_agent=False)), \
             patch("agent.input_analyzer.route_intent", return_value=IntentResult(intent="create_after_sale")), \
             patch("agent.input_analyzer.extract_business_params", return_value=BusinessParams(
                 product_name="AirPods Pro 2", after_sale_type="return_refund",
                 reason="单侧无声，疑似质量问题")), \
             patch("middleware.rate_limit.global_limiter.allow", return_value=True), \
             patch("middleware.rate_limit.user_limiter.allow", return_value=True), \
             TestClient(main.app) as client:
            headers = {"Authorization": f"Bearer {create_access_token(self.user_id)}"}
            preview = client.post("/agent/chat", headers=headers, json={
                "message": "AirPods Pro 2质量问题，申请退货退款",
            }).json()
            resource = preview["data"]["confirmation_id"]
            first = client.post("/agent/confirm", headers=headers, json={"confirmation_id": resource}).json()
            self.assertEqual(first["code"], "ACTION_EXECUTED", first)
            con = db_connection.get_connection()
            try:
                with con.cursor() as cur:
                    cur.execute("UPDATE agent_pending_actions SET preview_json='{}' WHERE confirmation_id=%s",
                                (resource,))
                con.commit()
            finally:
                con.close()
            replay = client.post("/agent/confirm", headers=headers, json={"confirmation_id": resource}).json()
            self.assertEqual(replay["code"], "IDEMPOTENT_REPLAY", replay)
            self.assertEqual(self._count(), 1)

    def test_explicit_version_and_hash_change_reject_old_preview(self):
        import services.policy_rag_client as rag_client
        with patch("main.get_settings", return_value=SimpleNamespace(use_langgraph_agent=False)), \
             patch("agent.input_analyzer.route_intent", return_value=IntentResult(intent="create_after_sale")), \
             patch("agent.input_analyzer.extract_business_params", return_value=BusinessParams(
                 product_name="AirPods Pro 2", after_sale_type="return_refund",
                 reason="单侧无声，疑似质量问题")), \
             patch("middleware.rate_limit.global_limiter.allow", return_value=True), \
             patch("middleware.rate_limit.user_limiter.allow", return_value=True), \
             TestClient(main.app) as client:
            headers = {"Authorization": f"Bearer {create_access_token(self.user_id)}"}
            preview = client.post("/agent/chat", headers=headers, json={
                "message": "AirPods Pro 2质量问题，申请退货退款",
            }).json()
            self.assertEqual(preview["code"], "ACTION_PREVIEW_READY", preview)
            resource = preview["data"]["confirmation_id"]
            original = rag_client.query_policy_rag
            for field, changed in (("version", "v999"), ("file_hash", "changed-hash")):
                def changed_response(*args, **kwargs):
                    response = original(*args, **kwargs).model_copy(deep=True)
                    setattr(response.evidence[0], field, changed)
                    return response
                with patch.object(rag_client, "query_policy_rag", side_effect=changed_response):
                    result = client.post("/agent/confirm", headers=headers,
                                         json={"confirmation_id": resource}).json()
                self.assertEqual(result["code"], "PREVIEW_RECHECK_REQUIRED", (field, result))
            self.assertEqual(self._count(), 0)


if __name__ == "__main__":
    unittest.main()
