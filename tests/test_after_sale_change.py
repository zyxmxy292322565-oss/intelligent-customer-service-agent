"""Cross-conversation type-change checks without touching the business database."""

import unittest
from contextlib import nullcontext
from unittest.mock import MagicMock, patch

from agent.after_sale_operations import detect_after_sale_operation, prepare_after_sale_operation
from agent.conversation_scope import conversation_scope
from services.after_sale_operation_service import (
    execute_after_sale_operation,
    list_owned_pending_after_sales_by_normalized_product,
)


NUMBER = "ASDEB4F899E2A8464C"


def _sale(*, sale_id=7, status="pending", sale_type="return_refund"):
    return {"id": sale_id, "after_sale_no": NUMBER, "product_name": "智能手表",
            "status": status, "type": sale_type, "unit_price": 999, "quantity": 1}


class ChangeAfterSaleTests(unittest.TestCase):
    def _prepare(self, message, *, by_no=None, by_product=None, by_any=None, by_conversation=None):
        step = MagicMock()
        with patch("agent.after_sale_operations.create_agent_task", return_value={"task_id": 81}), \
             patch("agent.after_sale_operations.trace_step", return_value=nullcontext(step)), \
             patch("agent.after_sale_operations.list_owned_after_sales_by_no", return_value=by_no or []) as by_no_call, \
             patch("agent.after_sale_operations.list_owned_pending_after_sales_by_normalized_product", return_value=by_product or []) as by_product_call, \
             patch("agent.after_sale_operations.list_owned_pending_after_sales", return_value=by_any or []) as by_any_call, \
             patch("agent.after_sale_operations.list_conversation_after_sales", return_value=by_conversation or []) as by_conversation_call, \
             patch("agent.after_sale_operations.create_pending_action", return_value={"confirmation_id": "CONF_CHANGE"}) as create_action, \
             patch("agent.after_sale_operations.save_current_confirmation"), \
             patch("agent.after_sale_operations.update_agent_task_context"):
            with conversation_scope(1, 99):
                result = prepare_after_sale_operation(
                    message, 1, "change-trace", detect_after_sale_operation(message),
                )
        self.by_any_call = by_any_call
        return result, by_no_call, by_product_call, by_conversation_call, create_action

    def test_change_expressions_extract_target_without_confusing_old_type(self):
        cases = {
            "修改我的智能手表售后类型": None,
            "把这个售后改成换货": "exchange",
            f"修改售后申请 {NUMBER}": None,
            "我的售后改成退款": "refund",
            "不要当前这个退货了，改换货": "exchange",
            "从维修改成换货": "exchange",
            "从换货改成维修": "repair",
        }
        for message, target in cases.items():
            with self.subTest(message=message):
                self.assertEqual(detect_after_sale_operation(message),
                                 ("change_after_sale_type", target))

    def test_missing_target_requests_type_without_preview(self):
        result, by_no, by_product, by_conversation, action = self._prepare(
            "修改我的智能手表售后类型", by_product=[_sale()],
        )
        self.assertEqual(result["code"], "AFTER_SALE_CHANGE_TYPE_REQUIRED")
        self.assertFalse(result["success"])
        by_no.assert_not_called()
        by_product.assert_not_called()
        by_conversation.assert_not_called()
        action.assert_not_called()

    def test_new_conversation_product_creates_preview(self):
        result, by_no, by_product, by_conversation, action = self._prepare(
            "修改我的智能手表售后类型为换货", by_product=[_sale()],
        )
        self.assertEqual(result["code"], "ACTION_PREVIEW_READY")
        self.assertEqual(result["data"]["preview"]["after_sale_id"], 7)
        self.assertEqual(result["data"]["preview"]["after_sale_no"], NUMBER)
        self.assertEqual(result["data"]["preview"]["old_type"], "return_refund")
        self.assertEqual(result["data"]["preview"]["new_type"], "exchange")
        by_product.assert_called_once_with(1, "智能手表")
        by_no.assert_not_called()
        by_conversation.assert_not_called()
        self.assertEqual(action.call_args.args[1:3], ("change_after_sale_type", result["data"]["preview"]))

    def test_new_conversation_number_takes_priority(self):
        result, by_no, by_product, by_conversation, _ = self._prepare(
            f"修改售后申请 {NUMBER} 改成维修", by_no=[_sale()],
        )
        self.assertEqual(result["code"], "ACTION_PREVIEW_READY")
        self.assertEqual(result["data"]["preview"]["new_type"], "repair")
        by_no.assert_called_once_with(1, NUMBER)
        by_product.assert_not_called()
        by_conversation.assert_not_called()

    def test_single_owned_pending_sale_is_selected_without_identifier(self):
        result, _, _, by_conversation, _ = self._prepare(
            "修改我的售后类型为维修", by_any=[_sale()],
        )
        self.assertEqual(result["code"], "ACTION_PREVIEW_READY")
        self.by_any_call.assert_called_once_with(1)
        by_conversation.assert_not_called()

    def test_no_identifier_with_zero_or_multiple_pending_sales(self):
        missing, _, _, _, missing_action = self._prepare("修改我的售后类型为维修")
        self.assertEqual(missing["code"], "AFTER_SALE_NOT_FOUND")
        missing_action.assert_not_called()
        ambiguous, _, _, _, ambiguous_action = self._prepare(
            "修改我的售后类型为维修", by_any=[_sale(), _sale(sale_id=8)],
        )
        self.assertEqual(ambiguous["code"], "AFTER_SALE_SELECTION_REQUIRED")
        self.assertIn("商品名称或申请编号", ambiguous["message"])
        ambiguous_action.assert_not_called()

    def test_product_spellings_resolve_same_owned_pending_sale(self):
        variants = {
            "修改我的AirPods Pro 2售后类型为维修": "AirPodsPro2",
            "修改我的AirPodsPro2售后类型为维修": "AirPodsPro2",
            "修改我的airpods pro2售后类型为维修": "airpodspro2",
            "修改我的AirPods售后类型为维修": "AirPods",
        }
        for message, extracted in variants.items():
            with self.subTest(message=message):
                result, _, product_call, conversation_call, _ = self._prepare(
                    message, by_product=[_sale()],
                )
                self.assertEqual(result["code"], "ACTION_PREVIEW_READY")
                self.assertEqual(result["data"]["preview"]["after_sale_id"], 7)
                product_call.assert_called_once_with(1, extracted)
                conversation_call.assert_not_called()

    def test_product_lookup_normalizes_spaces_and_case_without_dropping_owner(self):
        for name, normalized in (("AirPods Pro 2", "airpodspro2"),
                                 ("AirPodsPro2", "airpodspro2"),
                                 ("airpods pro2", "airpodspro2"),
                                 ("AirPods", "airpods")):
            with self.subTest(name=name):
                connection = MagicMock()
                cursor = connection.cursor.return_value.__enter__.return_value
                cursor.fetchall.return_value = [_sale()]
                with patch("services.after_sale_operation_service.get_connection", return_value=connection):
                    rows = list_owned_pending_after_sales_by_normalized_product(1, name)
                self.assertEqual(len(rows), 1)
                sql, params = cursor.execute.call_args.args
                self.assertIn("s.user_id = %s", sql)
                self.assertIn("s.status = 'pending'", sql)
                self.assertIn("REPLACE(oi.product_name, ' ', '')", sql)
                self.assertEqual(params, (1, normalized))

    def test_multiple_pending_sales_require_number(self):
        result, _, by_product, _, action = self._prepare(
            "修改我的智能手表售后类型为换货",
            by_product=[_sale(), _sale(sale_id=8)],
        )
        self.assertEqual(result["code"], "AFTER_SALE_SELECTION_REQUIRED")
        by_product.assert_called_once_with(1, "智能手表")
        action.assert_not_called()

    def test_other_user_number_cannot_create_preview(self):
        result, by_no, _, _, action = self._prepare(
            f"修改售后申请 {NUMBER} 改成换货",
        )
        self.assertEqual(result["code"], "AFTER_SALE_NOT_FOUND")
        by_no.assert_called_once_with(1, NUMBER)
        action.assert_not_called()

    def test_cancelled_and_executed_sales_cannot_be_changed(self):
        for status, code in (("cancelled", "AFTER_SALE_ALREADY_CANCELLED"),
                             ("executed", "AFTER_SALE_NOT_PENDING")):
            with self.subTest(status=status):
                result, _, _, _, action = self._prepare(
                    f"修改售后申请 {NUMBER} 改成换货",
                    by_no=[_sale(status=status)],
                )
                self.assertEqual(result["code"], code)
                action.assert_not_called()

    def test_execute_checks_id_owner_status_old_type_and_commits_once(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = _sale()
        cursor.rowcount = 1
        preview = {"after_sale_id": 7, "after_sale_no": NUMBER,
                   "old_type": "return_refund", "new_type": "exchange"}
        with patch("services.after_sale_operation_service.get_connection", return_value=connection):
            result = execute_after_sale_operation(
                user_id=1, conversation_id=99, action_type="change_after_sale_type", preview=preview,
            )
            cursor.fetchone.return_value = _sale(sale_type="exchange")
            replay = execute_after_sale_operation(
                user_id=1, conversation_id=99, action_type="change_after_sale_type", preview=preview,
            )
        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["type"], "exchange")
        self.assertEqual(replay["code"], "AFTER_SALE_TYPE_UNCHANGED")
        self.assertEqual(connection.commit.call_count, 1)
        self.assertEqual(cursor.execute.call_args_list[0].args[1], (7, 1, NUMBER))
        update_sql, params = cursor.execute.call_args_list[1].args
        self.assertIn("status = 'pending'", update_sql)
        self.assertIn("type = %s", update_sql)
        self.assertEqual(params, ("exchange", None, 7, 1, NUMBER, "return_refund"))

    def test_execute_rejects_missing_id_or_other_user(self):
        invalid = execute_after_sale_operation(
            user_id=1, conversation_id=99, action_type="change_after_sale_type",
            preview={"after_sale_no": NUMBER, "old_type": "return_refund", "new_type": "exchange"},
        )
        self.assertEqual(invalid["code"], "INVALID_PREVIEW")
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None
        with patch("services.after_sale_operation_service.get_connection", return_value=connection):
            denied = execute_after_sale_operation(
                user_id=2, conversation_id=99, action_type="change_after_sale_type",
                preview={"after_sale_id": 7, "after_sale_no": NUMBER,
                         "old_type": "return_refund", "new_type": "exchange"},
            )
        self.assertEqual(denied["code"], "ACTION_NOT_FOUND")
        self.assertEqual(cursor.execute.call_args.args[1], (7, 2, NUMBER))
        connection.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
