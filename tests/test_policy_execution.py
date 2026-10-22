"""Phase 4 policy and concurrency contracts; never touches a real database."""

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import main
import pymysql
from fastapi import HTTPException
from agent.action_executor import execute_confirmed_action
from db.repository import (
    get_order_by_order_no_and_user_id, get_order_item_by_id_and_order_id,
)
from schemas.agent_schema import AgentConfirmRequest
from schemas.after_sale_schema import CreateAfterSaleRequest
from services.after_sale_service import create_after_sale_service
from services.policy_check_service import check_after_sale_policy
from services.policy_demo_rules import DEMO_POLICY_SOURCES
from services.policy_execution_service import (
    PolicyValidation, active_after_sale_for_preview, build_policy_snapshot,
    validate_policy_before_create,
)


SOURCE = DEMO_POLICY_SOURCES["耳机类商品规则.txt"]
DELIVERED = (datetime.now(timezone(timedelta(hours=8))) - timedelta(days=20)).replace(tzinfo=None)


def evidence(*, source=SOURCE, version=None, file_hash=None):
    return [{"source": source.filename, "version": version,
             "file_hash": file_hash or source.file_hash,
             "content": "耳机签收后30天内可申请质量问题售后，仍需检测审核。"}]


class PolicyExecutionTests(unittest.TestCase):
    def setUp(self):
        self.order = {"id": 10, "user_id": 1, "order_no": "ORD_10",
                      "status": "delivered", "delivered_at": DELIVERED,
                      "created_at": datetime(2026, 9, 1), "paid_at": datetime(2026, 9, 1)}
        self.item = {"id": 20, "order_id": 10, "product_name": "AirPods Pro 2",
                     "category": "耳机", "quantity": 1, "unit_price": 1899}
        self.policy = self._policy()
        self.snapshot = build_policy_snapshot(
            self.policy, order_id=10, order_item_id=20, order_status="delivered",
            delivered_at=DELIVERED, category="耳机", product_name="AirPods Pro 2",
            after_sale_type="return_refund", reason="质量不好", snapshot_version=2,
        )

    def _policy(self, *, rag=None):
        return check_after_sale_policy(
            user_id=1, order=self.order, order_item=self.item,
            after_sale_type="return_refund", reason="质量不好",
            rag_query=lambda **_: {"success": True, "refused": False,
                                    "evidence": evidence() if rag is None else rag},
        )

    def _gate(self, snapshot=None, require_snapshot=True, rag=None):
        conn = Mock()
        conn.cursor.return_value = Mock()
        with patch("services.policy_execution_service.get_connection", return_value=conn), \
             patch("services.policy_execution_service.get_order_by_order_no_and_user_id", return_value=self.order), \
             patch("services.policy_execution_service.get_order_item_by_id_and_order_id", return_value=self.item), \
             patch("services.policy_execution_service.get_active_after_sale_by_order_item_id", return_value=None), \
             patch("services.policy_rag_client.query_policy_rag", return_value={
                 "success": True, "refused": False,
                 "evidence": evidence() if rag is None else rag,
             }):
            return validate_policy_before_create(
                user_id=1, order_no="ORD_10", order_item_id=20,
                after_sale_type="return_refund", reason="质量不好",
                snapshot=self.snapshot if snapshot is None else snapshot,
                require_snapshot=require_snapshot,
            )

    def test_current_preview_passes_and_old_preview_is_rejected(self):
        self.assertEqual(self._gate()["code"], "POLICY_VALIDATED")
        stale = {**self.snapshot, "checked_at":
                 (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()}
        self.assertEqual(self._gate(snapshot=stale)["code"], "PREVIEW_RECHECK_REQUIRED")
        with patch("services.policy_execution_service.get_connection") as db:
            old = validate_policy_before_create(
                user_id=1, order_no="ORD_10", order_item_id=20,
                after_sale_type="return_refund", reason="质量不好",
                require_snapshot=True,
            )
        self.assertEqual(old["code"], "PREVIEW_RECHECK_REQUIRED")
        db.assert_not_called()

    def test_preview_lookup_requires_owned_order_item_and_reuses_active_status_rule(self):
        conn = Mock()
        with patch("services.policy_execution_service.get_connection", return_value=conn), \
             patch("services.policy_execution_service.get_order_by_order_no_and_user_id",
                   return_value=self.order), \
             patch("services.policy_execution_service.get_order_item_by_id_and_order_id",
                   return_value=self.item), \
             patch("services.policy_execution_service.get_active_after_sale_by_order_item_id",
                   return_value={"id": 99, "user_id": 1, "status": "pending"}) as active:
            self.assertEqual(active_after_sale_for_preview(1, "ORD_10", 20)["id"], 99)
            active.assert_called_once_with(conn.cursor.return_value, 20)
        with patch("services.policy_execution_service.get_connection", return_value=conn), \
             patch("services.policy_execution_service.get_order_by_order_no_and_user_id",
                   return_value=None), \
             patch("services.policy_execution_service.get_active_after_sale_by_order_item_id") as active:
            self.assertEqual(active_after_sale_for_preview(2, "ORD_10", 20), {"ownership_error": True})
            active.assert_not_called()

    def test_order_delivery_category_and_policy_snapshot_changes_fail_closed(self):
        changes = (
            (self.order, "status", "cancelled"),
            (self.order, "delivered_at", DELIVERED - timedelta(days=1)),
            (self.item, "category", "电脑配件"),
        )
        for target, key, value in changes:
            with self.subTest(key=key):
                old = target[key]
                target[key] = value
                self.assertEqual(self._gate()["code"], "PREVIEW_RECHECK_REQUIRED")
                target[key] = old
        for key, value in (("policy_versions", {SOURCE.filename: "other"}),
                           ("policy_file_hashes", {SOURCE.filename: "wrong"}),
                           ("rule_id", "other"),
                           ("reason_hash", "tampered")):
            with self.subTest(key=key):
                changed = {**self.snapshot, key: value}
                self.assertEqual(self._gate(snapshot=changed)["code"], "PREVIEW_RECHECK_REQUIRED")

    def test_deadline_and_rag_outage_never_allow_creation(self):
        self.order["delivered_at"] = DELIVERED - timedelta(days=12)
        self.assertEqual(self._gate()["code"], "PREVIEW_RECHECK_REQUIRED")
        self.order["delivered_at"] = DELIVERED
        with patch("services.policy_rag_client.query_policy_rag", side_effect=RuntimeError("down")):
            # The local helper's RAG mock would override this patch; call the
            # deterministic policy engine directly for the outage contract.
            result = check_after_sale_policy(
                user_id=1, order=self.order, order_item=self.item,
                after_sale_type="return_refund", reason="质量不好",
            )
        self.assertEqual(result["decision"], "POLICY_UNAVAILABLE")
        self.assertEqual(self._gate(rag=[])["code"], "POLICY_UNAVAILABLE")

    def test_changed_hash_or_explicit_version_is_not_treated_as_valid(self):
        self.assertEqual(self._gate(rag=evidence(file_hash="changed"))["code"],
                         "PREVIEW_RECHECK_REQUIRED")
        self.assertEqual(self._gate(rag=evidence(version="v2"))["code"],
                         "PREVIEW_RECHECK_REQUIRED")

    def test_current_user_ownership_and_new_active_sale_fail_before_rag(self):
        self.order["user_id"] = 2
        self.assertEqual(self._gate()["code"], "PREVIEW_RECHECK_REQUIRED")
        self.order["user_id"] = 1
        conn = Mock()
        with patch("services.policy_execution_service.get_connection", return_value=conn), \
             patch("services.policy_execution_service.get_order_by_order_no_and_user_id", return_value=self.order), \
             patch("services.policy_execution_service.get_order_item_by_id_and_order_id", return_value=self.item), \
             patch("services.policy_execution_service.get_active_after_sale_by_order_item_id",
                   return_value={"id": 90}), \
             patch("services.policy_execution_service.check_after_sale_policy") as policy:
            result = validate_policy_before_create(
                user_id=1, order_no="ORD_10", order_item_id=20,
                after_sale_type="return_refund", reason="质量不好",
                snapshot=self.snapshot, require_snapshot=True,
            )
        self.assertEqual(result["code"], "AFTER_SALE_ALREADY_EXISTS")
        policy.assert_not_called()

    def test_direct_service_uses_policy_and_rechecks_while_item_locked(self):
        conn = Mock()
        cursor = conn.cursor.return_value
        calls = []
        def order_lookup(*args, **kwargs):
            calls.append(("order", kwargs))
            return self.order
        def item_lookup(*args, **kwargs):
            calls.append(("item", kwargs))
            return self.item
        def active_lookup(*args, **kwargs):
            calls.append(("active", kwargs))
            return None
        validation = self._gate()["validation"]
        with patch("services.after_sale_service.get_connection", return_value=conn), \
             patch("services.after_sale_service.validate_policy_before_create",
                   return_value={"success": True, "validation": validation}) as gate, \
             patch("services.after_sale_service.get_order_by_order_no_and_user_id", side_effect=order_lookup), \
             patch("services.after_sale_service.get_order_item_by_id_and_order_id", side_effect=item_lookup), \
             patch("services.after_sale_service.get_active_after_sale_by_order_item_id", side_effect=active_lookup), \
             patch("services.after_sale_service.create_after_sale", return_value=77) as insert, \
             patch("services.after_sale_service.create_after_sale_status_log"):
            result = create_after_sale_service(1, "ORD_10", 20, "return_refund", "质量不好")
        self.assertTrue(result["success"])
        gate.assert_called_once()
        self.assertEqual([name for name, _ in calls], ["order", "item", "active"])
        self.assertTrue(calls[0][1]["lock_for_share"])
        self.assertTrue(calls[1][1]["for_update"])
        self.assertIn("READ COMMITTED", cursor.execute.call_args_list[0].args[0])
        insert.assert_called_once()
        conn.commit.assert_called_once()

    def test_concurrent_second_reader_rechecks_existing_sale_after_lock(self):
        conn = Mock()
        validation = self._gate()["validation"]
        with patch("services.after_sale_service.get_connection", return_value=conn), \
             patch("services.after_sale_service.get_order_by_order_no_and_user_id", return_value=self.order), \
             patch("services.after_sale_service.get_order_item_by_id_and_order_id", return_value=self.item), \
             patch("services.after_sale_service.get_active_after_sale_by_order_item_id",
                   return_value={"id": 90}), \
             patch("services.after_sale_service.create_after_sale") as insert:
            result = create_after_sale_service(1, "ORD_10", 20, "return_refund", "质量不好",
                                               _policy_validation=validation)
        self.assertEqual(result["code"], "AFTER_SALE_ALREADY_EXISTS")
        insert.assert_not_called()

    def test_different_idempotency_keys_cannot_skip_post_lock_duplicate_check(self):
        validation = self._gate()["validation"]
        conn = Mock()
        with patch("services.after_sale_service.get_connection", return_value=conn), \
             patch("services.after_sale_service.existing_after_sale_for_key", return_value=None), \
             patch("services.after_sale_service.get_after_sale_by_idempotency_key", return_value=None), \
             patch("services.after_sale_service.get_order_by_order_no_and_user_id", return_value=self.order), \
             patch("services.after_sale_service.get_order_item_by_id_and_order_id", return_value=self.item), \
             patch("services.after_sale_service.get_active_after_sale_by_order_item_id",
                   side_effect=[None, {"id": 77}]) as active, \
             patch("services.after_sale_service.create_after_sale", return_value=77) as insert, \
             patch("services.after_sale_service.create_after_sale_status_log"):
            first = create_after_sale_service(1, "ORD_10", 20, "return_refund", "质量不好",
                                              idempotency_key="CONF_A",
                                              _policy_validation=validation)
            second = create_after_sale_service(1, "ORD_10", 20, "return_refund", "质量不好",
                                               idempotency_key="CONF_B",
                                               _policy_validation=validation)
        self.assertTrue(first["success"])
        self.assertEqual(second["code"], "AFTER_SALE_ALREADY_EXISTS")
        self.assertEqual(active.call_count, 2)
        insert.assert_called_once()

    def test_direct_service_policy_refusals_never_open_write_transaction(self):
        for code in ("INELIGIBLE", "NEEDS_REVIEW", "POLICY_UNAVAILABLE"):
            with self.subTest(code=code), \
                 patch("services.after_sale_service.validate_policy_before_create",
                       return_value={"success": False, "code": code, "message": "不自动创建"}), \
                 patch("services.after_sale_service.get_connection") as db:
                result = create_after_sale_service(1, "ORD_10", 20, "return_refund", "质量不好")
            self.assertEqual(result["code"], code)
            db.assert_not_called()

    def test_direct_http_endpoint_uses_service_gate_and_policy_error(self):
        request = CreateAfterSaleRequest(user_id=2, order_no="ORD_10", order_item_id=20,
                                         type="return_refund", reason="质量不好")
        with patch("main.create_after_sale_service", return_value={
            "success": False, "code": "NEEDS_REVIEW", "message": "请联系人工客服",
        }) as service:
            with self.assertRaises(HTTPException) as error:
                main.create_after_sale_api(request, current_user_id=1)
        self.assertEqual(error.exception.status_code, 422)
        self.assertEqual(service.call_args.kwargs["user_id"], 1)

    def test_deadlock_and_lock_wait_roll_back_and_report_retryable_failure(self):
        validation = self._gate()["validation"]
        for mysql_code in (1205, 1213):
            with self.subTest(mysql_code=mysql_code):
                conn = Mock()
                with patch("services.after_sale_service.get_connection", return_value=conn), \
                     patch("services.after_sale_service.get_order_by_order_no_and_user_id", return_value=self.order), \
                     patch("services.after_sale_service.get_order_item_by_id_and_order_id", return_value=self.item), \
                     patch("services.after_sale_service.get_active_after_sale_by_order_item_id", return_value=None), \
                     patch("services.after_sale_service.create_after_sale",
                           side_effect=pymysql.err.OperationalError(mysql_code, "lock error")):
                    result = create_after_sale_service(1, "ORD_10", 20, "return_refund", "质量不好",
                                                       _policy_validation=validation)
                self.assertEqual(result["code"], "DATABASE_RETRYABLE")
                conn.rollback.assert_called_once()
                conn.commit.assert_not_called()

    def test_order_changes_while_waiting_for_item_lock_cannot_insert(self):
        validation = self._gate()["validation"]
        changed = {**self.order, "status": "cancelled"}
        conn = Mock()
        with patch("services.after_sale_service.get_connection", return_value=conn), \
             patch("services.after_sale_service.get_order_by_order_no_and_user_id", return_value=changed), \
             patch("services.after_sale_service.get_order_item_by_id_and_order_id", return_value=self.item), \
             patch("services.after_sale_service.create_after_sale") as insert:
            result = create_after_sale_service(1, "ORD_10", 20, "return_refund", "质量不好",
                                               _policy_validation=validation)
        self.assertEqual(result["code"], "PREVIEW_RECHECK_REQUIRED")
        insert.assert_not_called()

    def test_repository_emits_narrow_locking_queries(self):
        cursor = Mock()
        get_order_by_order_no_and_user_id(cursor, 1, "ORD_10", lock_for_share=True)
        self.assertIn("FOR SHARE", cursor.execute.call_args.args[0])
        get_order_item_by_id_and_order_id(cursor, 20, 10, for_update=True)
        self.assertIn("WHERE id = %s", cursor.execute.call_args.args[0])
        self.assertIn("AND order_id = %s", cursor.execute.call_args.args[0])
        self.assertIn("FOR UPDATE", cursor.execute.call_args.args[0])

    def test_confirmed_outage_retries_same_id_without_resetting_failed(self):
        action = {"confirmation_id": "CONF_1", "user_id": 1, "status": "confirmed",
                  "action_type": "create_after_sale", "task_id": 4,
                  "preview": {"order_no": "ORD_10", "order_item_id": 20,
                              "after_sale_type": "return_refund", "reason": "质量不好",
                              "policy_reason_text": "质量不好",
                              "policy_snapshot": self.snapshot}}
        status = {"value": "confirmed"}
        def lookup(_id, _user):
            return {**action, "status": status["value"]}
        def claim(_id):
            if status["value"] != "confirmed":
                return False
            status["value"] = "executing"
            return True
        def finish(**kwargs):
            status["value"] = kwargs["status"]
            return {"status": kwargs["status"]}
        with patch("agent.action_executor.get_pending_action", side_effect=lookup), \
             patch("agent.action_executor.existing_after_sale_for_key", return_value=None), \
             patch("agent.action_executor.validate_policy_before_create", side_effect=[
                 {"success": False, "code": "POLICY_UNAVAILABLE", "message": "暂不可用"},
                 {"success": True, "validation": self._gate()["validation"]},
             ]), \
             patch("agent.action_executor.claim_pending_action_execution", side_effect=claim) as cas, \
             patch("agent.action_executor.create_after_sale_service", return_value={
                 "success": True, "code": "SUCCESS", "data": {"id": 90},
             }) as create, \
             patch("agent.action_executor.update_pending_action_execution", side_effect=finish), \
             patch("agent.action_executor.clear_current_confirmation"):
            self.assertEqual(execute_confirmed_action("CONF_1", 1)["code"], "POLICY_UNAVAILABLE")
            self.assertEqual(status["value"], "confirmed")
            cas.assert_not_called()
            self.assertEqual(execute_confirmed_action("CONF_1", 1)["code"], "ACTION_EXECUTED")
            self.assertEqual(status["value"], "executed")
            create.assert_called_once()
            self.assertEqual(create.call_args.kwargs["idempotency_key"], "CONF_1")
        action["status"] = "failed"
        with patch("agent.action_executor.get_pending_action", return_value=action), \
             patch("agent.action_executor.claim_pending_action_execution") as cas:
            self.assertEqual(execute_confirmed_action("CONF_1", 1)["code"], "ACTION_NOT_CONFIRMED")
            cas.assert_not_called()

    def test_main_confirmed_retry_routes_to_executor_same_resource(self):
        with patch("main.get_pending_action", return_value={"task_id": 4}), \
             patch("main.confirm_action", return_value={
                 "success": False, "code": "ACTION_ALREADY_CONFIRMED", "message": "已确认",
             }), \
             patch("main.execute_confirmed_action", return_value={
                 "success": True, "code": "ACTION_EXECUTED", "message": "已执行",
             }) as execute, \
             patch("agent.step_trace._start_step", return_value=1), \
             patch("agent.step_trace._finish_step"):
            result = main.agent_confirm(AgentConfirmRequest(confirmation_id="CONF_1"), 1)
        self.assertEqual(result["code"], "ACTION_EXECUTED")
        execute.assert_called_once()
        self.assertEqual(execute.call_args.kwargs["confirmation_id"], "CONF_1")

    def test_old_preview_foreign_user_and_executing_do_not_claim(self):
        action = {"user_id": 1, "status": "confirmed", "action_type": "create_after_sale",
                  "preview": {"order_no": "ORD_10", "order_item_id": 20,
                              "after_sale_type": "return_refund", "reason": "质量不好"}}
        with patch("agent.action_executor.get_pending_action", return_value=action), \
             patch("agent.action_executor.existing_after_sale_for_key", return_value=None), \
             patch("agent.action_executor.claim_pending_action_execution") as cas:
            self.assertEqual(execute_confirmed_action("CONF_1", 1)["code"],
                             "PREVIEW_RECHECK_REQUIRED")
            cas.assert_not_called()
        action["preview"]["policy_snapshot"] = {**self.snapshot, "snapshot_version": 1}
        with patch("agent.action_executor.get_pending_action", return_value=action), \
             patch("agent.action_executor.existing_after_sale_for_key", return_value=None), \
             patch("agent.action_executor.claim_pending_action_execution") as cas:
            self.assertEqual(execute_confirmed_action("CONF_1", 1)["code"],
                             "PREVIEW_RECHECK_REQUIRED")
            cas.assert_not_called()
        with patch("agent.action_executor.get_pending_action", return_value=None), \
             patch("agent.action_executor.claim_pending_action_execution") as cas:
            self.assertEqual(execute_confirmed_action("CONF_1", 2)["code"], "ACTION_NOT_FOUND")
            cas.assert_not_called()
        action["status"] = "executing"
        with patch("agent.action_executor.get_pending_action", return_value=action), \
             patch("agent.action_executor.claim_pending_action_execution") as cas:
            self.assertEqual(execute_confirmed_action("CONF_1", 1)["code"], "ACTION_EXECUTING")
            cas.assert_not_called()

    def test_committed_same_key_replays_without_a_new_policy_call(self):
        action = {"user_id": 1, "status": "confirmed", "action_type": "create_after_sale",
                  "preview": {"order_no": "ORD_10", "order_item_id": 20,
                              "after_sale_type": "return_refund", "reason": "质量不好"}}
        with patch("agent.action_executor.get_pending_action", return_value=action), \
             patch("agent.action_executor.existing_after_sale_for_key", return_value={"id": 8}), \
             patch("agent.action_executor.validate_policy_before_create") as policy, \
             patch("agent.action_executor.claim_pending_action_execution", return_value=True), \
             patch("agent.action_executor.create_after_sale_service", return_value={
                 "success": True, "code": "IDEMPOTENT_REPLAY", "data": {"id": 8},
             }) as service, \
             patch("agent.action_executor.update_pending_action_execution",
                   return_value={"status": "executed"}), \
             patch("agent.action_executor.clear_current_confirmation"):
            self.assertEqual(execute_confirmed_action("CONF_1", 1)["code"], "ACTION_EXECUTED")
        policy.assert_not_called()
        self.assertEqual(service.call_args.kwargs["idempotency_key"], "CONF_1")

    def test_two_confirmed_requests_have_only_one_mock_atomic_claim_winner(self):
        validation = self._gate()["validation"]
        status = {"value": "confirmed"}
        lock = threading.Lock()
        barrier = threading.Barrier(2)
        action = {"user_id": 1, "action_type": "create_after_sale",
                  "preview": {"order_no": "ORD_10", "order_item_id": 20,
                              "after_sale_type": "return_refund", "reason": "质量不好",
                              "policy_reason_text": "质量不好",
                              "policy_snapshot": self.snapshot}}
        def lookup(*_args):
            with lock:
                return {**action, "status": status["value"]}
        def gate(**_kwargs):
            barrier.wait(timeout=3)
            return {"success": True, "validation": validation}
        def claim(_id):
            with lock:
                if status["value"] != "confirmed":
                    return False
                status["value"] = "executing"
                return True
        def finish(**kwargs):
            with lock:
                status["value"] = kwargs["status"]
            return {"status": kwargs["status"]}
        with patch("agent.action_executor.get_pending_action", side_effect=lookup), \
             patch("agent.action_executor.existing_after_sale_for_key", return_value=None), \
             patch("agent.action_executor.validate_policy_before_create", side_effect=gate), \
             patch("agent.action_executor.claim_pending_action_execution", side_effect=claim), \
             patch("agent.action_executor.create_after_sale_service", return_value={
                 "success": True, "code": "SUCCESS", "data": {"id": 8},
             }) as service, \
             patch("agent.action_executor.update_pending_action_execution", side_effect=finish), \
             patch("agent.action_executor.clear_current_confirmation"):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(execute_confirmed_action, "CONF_1", 1) for _ in range(2)]
                results = [future.result(timeout=5) for future in futures]
        self.assertEqual(sum(row["code"] == "ACTION_EXECUTED" for row in results), 1)
        service.assert_called_once()


if __name__ == "__main__":
    unittest.main()
