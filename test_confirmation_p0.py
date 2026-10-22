"""确认链路 P0 回归测试：使用内存替身，不访问真实 MySQL 或创建售后。"""

import asyncio
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, Mock, patch

import main
from agent.action_executor import execute_confirmed_action
from agent.confirmation_service import (
    cancel_action, close_pending_action, confirm_action, expire_pending_actions,
)
from db.repository import (
    clear_expired_current_confirmations,
    confirm_agent_pending_action,
    expire_agent_pending_action_if_due,
    expire_agent_pending_actions,
    finish_agent_pending_action,
    get_agent_pending_action_by_confirmation_id,
    update_agent_pending_action_status,
)
from schemas.agent_schema import AgentConfirmRequest


class ConfirmationFlowTests(unittest.TestCase):
    def setUp(self):
        self.action = {
            "confirmation_id": "CONF_TEST_1",
            "user_id": 1,
            "action_type": "create_after_sale",
            "status": "pending",
            "preview": {
                "order_no": "ORDER_TEST",
                "order_item_id": 42,
                "after_sale_type": "return_refund",
                "reason": "质量不好",
                "policy_reason_text": "质量不好",
                "policy_snapshot": {"snapshot_version": 2},
            },
            "execution_result": None,
        }
        self.service_result = {"success": True, "code": "AFTER_SALE_CREATED", "data": {"id": 9}}
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.service = self.stack.enter_context(
            patch("agent.action_executor.create_after_sale_service", return_value=self.service_result)
        )
        self.stack.enter_context(patch(
            "agent.action_executor.existing_after_sale_for_key", return_value=None
        ))
        self.stack.enter_context(patch(
            "agent.action_executor.validate_policy_before_create",
            return_value={"success": True, "validation": "validated"},
        ))
        self.clear_session = self.stack.enter_context(
            patch("agent.action_executor.clear_current_confirmation")
        )
        self.stack.enter_context(patch("main.get_pending_action", side_effect=self.get_action))
        self.stack.enter_context(patch("agent.step_trace._start_step", return_value=1))
        self.trace_finish = self.stack.enter_context(patch("agent.step_trace._finish_step"))
        self.stack.enter_context(patch("agent.confirmation_service.get_pending_action", side_effect=self.get_action))
        self.stack.enter_context(patch("agent.confirmation_service.expire_pending_action_if_due", return_value=False))
        self.stack.enter_context(patch("agent.confirmation_service.clear_current_confirmation"))
        self.stack.enter_context(patch("agent.action_executor.get_pending_action", side_effect=self.get_action))
        self.stack.enter_context(
            patch("agent.confirmation_service.update_pending_action_status", side_effect=self.update_status)
        )
        self.stack.enter_context(
            patch("agent.action_executor.claim_pending_action_execution", side_effect=self.claim_execution)
        )
        self.stack.enter_context(
            patch("agent.action_executor.update_pending_action_execution", side_effect=self.finish_execution)
        )

    def get_action(self, confirmation_id, current_user_id=None):
        if confirmation_id != self.action["confirmation_id"]:
            return None
        if current_user_id is not None and current_user_id != self.action["user_id"]:
            return None
        return self.action

    def update_status(self, confirmation_id, new_status):
        if self.get_action(confirmation_id) is None or self.action["status"] != "pending":
            return None
        self.action["status"] = new_status
        return self.action

    def claim_execution(self, confirmation_id):
        if self.get_action(confirmation_id) is None or self.action["status"] != "confirmed":
            return False
        self.action["status"] = "executing"
        return True

    def finish_execution(self, confirmation_id, status, execution_result):
        if self.get_action(confirmation_id) is None or self.action["status"] != "executing":
            return None
        self.action["status"] = status
        self.action["execution_result"] = execution_result
        return self.action

    def confirm(self, user_id=1):
        return main.agent_confirm(
            AgentConfirmRequest(user_id=user_id, confirmation_id=self.action["confirmation_id"]),
            current_user_id=user_id,
        )

    def test_normal_confirm_and_idempotent_replay(self):
        first = self.confirm()
        self.assertEqual(first["code"], "ACTION_EXECUTED")
        self.assertEqual(self.action["status"], "executed")
        self.service.assert_called_once_with(
            user_id=1, order_no="ORDER_TEST", order_item_id=42,
            after_sale_type="return_refund", reason="质量不好",
            idempotency_key="CONF_TEST_1",
            _policy_validation="validated",
            _policy_reason_text="质量不好",
        )
        second = self.confirm()
        self.assertTrue(second["success"])
        self.assertEqual(second["code"], "IDEMPOTENT_REPLAY")
        self.assertEqual(second["data"], self.service_result)
        self.service.assert_called_once()
        self.clear_session.assert_called_once_with(user_id=1, confirmation_id="CONF_TEST_1")

    def test_execution_failure_marks_action_and_trace_failed(self):
        self.service.return_value = {
            "success": False, "code": "AFTER_SALE_ALREADY_EXISTS", "message": "已有售后"
        }
        result = self.confirm()
        self.assertEqual(result["code"], "ACTION_EXECUTION_FAILED")
        self.assertEqual(self.action["status"], "failed")
        self.assertEqual(self.action["execution_result"]["code"], "AFTER_SALE_ALREADY_EXISTS")
        self.assertIn("FAILED", [call.args[1] for call in self.trace_finish.call_args_list])
        self.service.assert_called_once()

    def test_foreign_confirmation_id_does_not_execute_or_replay(self):
        result = self.confirm(user_id=2)
        self.assertEqual(result["code"], "ACTION_NOT_FOUND")
        self.assertEqual(self.action["status"], "pending")
        self.service.assert_not_called()
        self.action["status"] = "executed"
        self.action["execution_result"] = self.service_result
        self.assertEqual(self.confirm(user_id=2)["code"], "ACTION_NOT_FOUND")

    def test_already_executed_action_cannot_be_executed_again(self):
        self.action["status"] = "executed"
        result = execute_confirmed_action("CONF_TEST_1", 1)
        self.assertEqual(result["code"], "ACTION_ALREADY_EXECUTED")
        self.service.assert_not_called()

    def test_abnormal_and_terminal_states_are_rejected(self):
        for status in ("executing", "failed", "cancelled", "expired"):
            with self.subTest(status=status):
                self.action["status"] = status
                result = self.confirm()
                self.assertFalse(result["success"])
                self.assertNotEqual(result["code"], "ACTION_EXECUTED")
        self.service.assert_not_called()

    def test_cancel_and_expire_are_pending_only(self):
        result = close_pending_action("CONF_TEST_1", 1, "cancelled")
        self.assertEqual(result["data"]["status"], "cancelled")
        self.assertEqual(self.confirm()["code"], "INVALID_ACTION_STATUS")
        self.action["status"] = "pending"
        self.assertEqual(close_pending_action("CONF_TEST_1", 1, "expired")["data"]["status"], "expired")
        self.action["status"] = "pending"
        self.assertEqual(close_pending_action("CONF_TEST_1", 2, "cancelled")["code"], "ACTION_NOT_FOUND")
        self.assertEqual(self.action["status"], "pending")

    def test_cancel_action_rejects_foreign_and_terminal_actions(self):
        self.assertEqual(cancel_action("CONF_TEST_1", 2)["code"], "ACTION_NOT_FOUND")
        self.assertEqual(self.action["status"], "pending")
        self.assertEqual(cancel_action("CONF_TEST_1", 1)["data"]["status"], "cancelled")
        self.assertEqual(self.confirm()["code"], "INVALID_ACTION_STATUS")
        for status in ("confirmed", "executed", "failed"):
            with self.subTest(status=status):
                self.action["status"] = status
                self.assertEqual(cancel_action("CONF_TEST_1", 1)["code"], "INVALID_ACTION_STATUS")
                self.assertEqual(self.action["status"], status)
        self.service.assert_not_called()

    def test_expired_action_cannot_confirm(self):
        self.action["status"] = "expired"
        self.assertEqual(self.confirm()["code"], "INVALID_ACTION_STATUS")
        self.service.assert_not_called()

    def test_expiration_service_uses_seven_day_default(self):
        with patch("agent.confirmation_service.expire_pending_actions_store", return_value=2) as sweep:
            self.assertEqual(expire_pending_actions(), 2)
        sweep.assert_called_once_with(7)

    def test_missing_replay_result_is_not_reported_as_success(self):
        self.action["status"] = "executed"
        self.assertEqual(self.confirm()["code"], "EXECUTION_RESULT_NOT_FOUND")
        self.service.assert_not_called()

    def test_pending_status_changed_during_confirm(self):
        with patch("agent.confirmation_service.update_pending_action_status", return_value=None):
            result = confirm_action("CONF_TEST_1", 1)
        self.assertEqual(result["code"], "ACTION_CONFIRM_CONFLICT")
        self.service.assert_not_called()


class ConditionalSqlTests(unittest.TestCase):
    def test_expiry_only_changes_old_pending_and_clears_expired_pointer(self):
        cursor = Mock(rowcount=1)
        expire_agent_pending_actions(cursor)
        sql, params = cursor.execute.call_args.args
        self.assertIn("status = 'pending'", sql)
        self.assertIn("created_at <= DATE_SUB(NOW(), INTERVAL %s DAY)", sql)
        self.assertEqual(params, (7,))

        expire_agent_pending_action_if_due(cursor, "CONF_TEST_1", 1)
        sql, params = cursor.execute.call_args.args
        self.assertIn("AND user_id = %s", sql)
        self.assertIn("status = 'pending'", sql)
        self.assertEqual(params, ("CONF_TEST_1", 1, 7))

        clear_expired_current_confirmations(cursor, "CONF_TEST_1")
        sql, params = cursor.execute.call_args.args
        self.assertIn("a.status = 'expired'", sql)
        self.assertIn("a.confirmation_id = %s", sql)
        self.assertEqual(params, ("CONF_TEST_1",))

    def test_action_lookup_includes_current_user(self):
        cursor = Mock()
        cursor.fetchone.return_value = None
        get_agent_pending_action_by_confirmation_id(cursor, "CONF_TEST_1", user_id=1)
        self.assertIn("AND user_id = %s", cursor.execute.call_args.args[0])
        self.assertEqual(cursor.execute.call_args.args[1], ("CONF_TEST_1", 1))

    def test_confirm_and_finish_only_accept_expected_prior_status(self):
        cursor = Mock(rowcount=1)
        confirm_agent_pending_action(cursor, "CONF_TEST_1")
        self.assertIn("AND status = 'pending'", cursor.execute.call_args.args[0])
        self.assertIn("created_at > DATE_SUB(NOW(), INTERVAL %s DAY)", cursor.execute.call_args.args[0])
        update_agent_pending_action_status(cursor, "CONF_TEST_1", "cancelled")
        self.assertIn("AND status = 'pending'", cursor.execute.call_args.args[0])
        finish_agent_pending_action(cursor, "CONF_TEST_1", "executed", "{}")
        self.assertIn("AND status = 'executing'", cursor.execute.call_args.args[0])


class ExpirySchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_periodic_worker_runs_expiry_sweep(self):
        with patch("main.asyncio.sleep", new_callable=AsyncMock) as sleep, \
             patch("main.asyncio.to_thread", new_callable=AsyncMock) as to_thread:
            sleep.side_effect = [None, asyncio.CancelledError()]
            with self.assertRaises(asyncio.CancelledError):
                await main._expire_pending_actions_periodically()
        sleep.assert_any_await(60 * 60)
        to_thread.assert_awaited_once_with(main.expire_pending_actions)


if __name__ == "__main__":
    unittest.main()
