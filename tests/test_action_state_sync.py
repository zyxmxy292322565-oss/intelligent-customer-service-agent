"""Action 终态与所属 Agent Task 同事务收尾的无数据库回归测试。"""

import unittest
from unittest.mock import Mock, patch

from agent.confirmation_store import update_pending_action_execution
from db.repository import finish_agent_pending_action, finish_agent_task_for_action


class ActionStateSyncTests(unittest.TestCase):
    def setUp(self):
        self.connection = Mock()
        self.cursor = self.connection.cursor.return_value
        self.cursor.fetchone.return_value = {'task_id': 12, 'id': 12}
        self.action_row = {
            "id": 1, "confirmation_id": "CONF_TEST", "user_id": 7, "task_id": 12,
            "action_type": "create_after_sale", "preview_json": "{}",
            "status": "executed", "execution_result_json": '{"success": true}',
            "created_at": None, "updated_at": None, "confirmed_at": None,
            "executed_at": None,
        }

    def test_success_finishes_action_and_its_task_before_commit(self):
        result = {"success": True, "code": "AFTER_SALE_CREATED"}
        with patch("agent.confirmation_store.get_connection", return_value=self.connection), \
             patch("agent.confirmation_store.finish_agent_pending_action", return_value=1) as finish, \
             patch("agent.confirmation_store.finish_agent_task_for_action", return_value=1) as sync, \
             patch("agent.confirmation_store.get_agent_pending_action_by_confirmation_id", return_value=self.action_row):
            update_pending_action_execution("CONF_TEST", "executed", result)
        self.assertEqual(finish.call_args.kwargs["error_message"], None)
        sync.assert_called_once_with(self.cursor, "CONF_TEST", "executed")
        self.connection.commit.assert_called_once()
        self.connection.rollback.assert_not_called()

    def test_failure_writes_existing_error_field_and_fails_task(self):
        result = {"success": False, "code": "AFTER_SALE_ALREADY_EXISTS", "message": "已有售后"}
        with patch("agent.confirmation_store.get_connection", return_value=self.connection), \
             patch("agent.confirmation_store.finish_agent_pending_action", return_value=1) as finish, \
             patch("agent.confirmation_store.finish_agent_task_for_action", return_value=1) as sync, \
             patch("agent.confirmation_store.get_agent_pending_action_by_confirmation_id", return_value=self.action_row):
            update_pending_action_execution("CONF_TEST", "failed", result)
        self.assertEqual(finish.call_args.kwargs["error_message"], "已有售后")
        sync.assert_called_once_with(self.cursor, "CONF_TEST", "failed")
        self.connection.commit.assert_called_once()

    def test_task_sync_error_rolls_back_action_finish(self):
        with patch("agent.confirmation_store.get_connection", return_value=self.connection), \
             patch("agent.confirmation_store.finish_agent_pending_action", return_value=1), \
             patch("agent.confirmation_store.finish_agent_task_for_action", side_effect=RuntimeError("db error")):
            with self.assertRaisesRegex(RuntimeError, "db error"):
                update_pending_action_execution("CONF_TEST", "failed", {"error": "failed"})
        self.connection.rollback.assert_called_once()
        self.connection.commit.assert_not_called()

    def test_conditional_sql_targets_only_the_linked_owner_and_latest_action(self):
        cursor = Mock(rowcount=1)
        finish_agent_task_for_action(cursor, "CONF_TEST", "failed")
        sql, params = cursor.execute.call_args.args
        self.assertIn("a.task_id = t.id AND a.user_id = t.user_id", sql)
        self.assertIn("a.confirmation_id = %s", sql)
        self.assertIn("t.execution_id IS NULL", sql)
        self.assertIn("newer.task_id = a.task_id AND newer.id > a.id", sql)
        self.assertEqual(params, ("failed", "failed", "CONF_TEST", "failed"))

        finish_agent_pending_action(cursor, "CONF_TEST", "failed", "{}", "已有售后")
        sql, params = cursor.execute.call_args.args
        self.assertIn("error_message = %s", sql)
        self.assertIn("status = 'executing'", sql)
        self.assertEqual(params, ("failed", "{}", "已有售后", "CONF_TEST"))


if __name__ == "__main__":
    unittest.main()
