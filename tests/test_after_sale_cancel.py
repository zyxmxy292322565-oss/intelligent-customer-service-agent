"""Cancellation routing and lookup tests without touching the business database."""

import unittest
from contextlib import nullcontext
from unittest.mock import MagicMock, patch

from agent.after_sale_operations import detect_after_sale_operation, prepare_after_sale_operation
from agent.conversation_scope import conversation_scope
from services.after_sale_operation_service import (
    execute_after_sale_operation,
    list_owned_pending_after_sales_by_product,
)


def _sale(sale_id=7, number="ASDEB4F899E2A8464C", product="智能手表", status="pending"):
    return {"id": sale_id, "after_sale_no": number, "product_name": product,
            "status": status, "type": "return_refund"}


class CancelOperationTests(unittest.TestCase):
    def _prepare(self, message, *, by_no=None, by_product=None, by_conversation=None):
        step = MagicMock()
        with patch("agent.after_sale_operations.create_agent_task", return_value={"task_id": 81}), \
             patch("agent.after_sale_operations.trace_step", return_value=nullcontext(step)), \
             patch("agent.after_sale_operations.list_owned_after_sales_by_no", return_value=by_no or []) as number_lookup, \
             patch("agent.after_sale_operations.list_owned_pending_after_sales_by_product", return_value=by_product or []) as product_lookup, \
             patch("agent.after_sale_operations.list_conversation_after_sales", return_value=by_conversation or []) as conversation_lookup, \
             patch("agent.after_sale_operations.create_pending_action", return_value={"confirmation_id": "CONF_NEW"}) as pending, \
             patch("agent.after_sale_operations.save_current_confirmation"), \
             patch("agent.after_sale_operations.update_agent_task_context"):
            with conversation_scope(1, 99):
                result = prepare_after_sale_operation(
                    message, 1, "trace-test", detect_after_sale_operation(message),
                )
        return result, number_lookup, product_lookup, conversation_lookup, pending

    def test_cancel_expressions_and_existing_fixed_phrases(self):
        for message in ("取消售后", "撤销售后", "取消我的售后", "取消申请", "不想退了",
                        "取消我的智能手表售后", "取消AirPods售后", "撤销手机售后",
                        "我不要这个售后了", "不要了", "不想要了"):
            with self.subTest(message=message):
                self.assertEqual(detect_after_sale_operation(message), ("cancel_after_sale", None))

    def test_new_conversation_product_preview(self):
        result, number_lookup, product_lookup, conversation_lookup, _ = self._prepare(
            "取消我的智能手表售后", by_product=[_sale()],
        )
        self.assertEqual(result["code"], "ACTION_PREVIEW_READY")
        self.assertEqual(result["data"]["preview"]["after_sale_id"], 7)
        product_lookup.assert_called_once_with(1, "智能手表")
        number_lookup.assert_not_called()
        conversation_lookup.assert_not_called()

    def test_new_conversation_number_takes_priority(self):
        result, number_lookup, product_lookup, conversation_lookup, _ = self._prepare(
            "取消售后申请 ASDEB4F899E2A8464C", by_no=[_sale()],
        )
        self.assertEqual(result["code"], "ACTION_PREVIEW_READY")
        number_lookup.assert_called_once_with(1, "ASDEB4F899E2A8464C")
        product_lookup.assert_not_called()
        conversation_lookup.assert_not_called()

    def test_other_user_number_is_not_found(self):
        result, number_lookup, _, _, pending = self._prepare("取消售后 ASDEB4F899E2A8464C")
        self.assertEqual(result["code"], "AFTER_SALE_NOT_FOUND")
        number_lookup.assert_called_once_with(1, "ASDEB4F899E2A8464C")
        pending.assert_not_called()

    def test_cancelled_sale_is_rejected(self):
        result, _, _, _, pending = self._prepare(
            "取消售后 ASDEB4F899E2A8464C", by_no=[_sale(status="cancelled")],
        )
        self.assertEqual(result["code"], "AFTER_SALE_ALREADY_CANCELLED")
        pending.assert_not_called()

    def test_multiple_product_sales_require_selection(self):
        result, _, product_lookup, _, pending = self._prepare(
            "取消AirPods售后", by_product=[_sale(), _sale(8, "AS1111111111111111")],
        )
        self.assertEqual(result["code"], "AFTER_SALE_SELECTION_REQUIRED")
        product_lookup.assert_called_once_with(1, "AirPods")
        pending.assert_not_called()

    def test_product_lookup_filters_owner_name_and_pending_in_sql(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [_sale()]
        with patch("services.after_sale_operation_service.get_connection", return_value=connection):
            rows = list_owned_pending_after_sales_by_product(1, "智能手表")
        self.assertEqual(rows[0]["after_sale_no"], "ASDEB4F899E2A8464C")
        sql, params = cursor.execute.call_args.args
        self.assertIn("s.user_id = %s", sql)
        self.assertIn("s.status = 'pending'", sql)
        self.assertIn("oi.product_name", sql)
        self.assertEqual(params, (1, "智能手表"))

    def test_execute_rechecks_sale_id_user_and_pending_state(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = {"id": 7, "after_sale_no": "ASDEB4F899E2A8464C", "status": "pending"}
        cursor.rowcount = 1
        with patch("services.after_sale_operation_service.get_connection", return_value=connection), \
             patch("services.after_sale_operation_service.create_after_sale_status_log"):
            result = execute_after_sale_operation(
                user_id=1, conversation_id=99, action_type="cancel_after_sale",
                preview={"after_sale_id": 7, "after_sale_no": "ASDEB4F899E2A8464C"},
            )
        self.assertTrue(result["success"])
        self.assertEqual(cursor.execute.call_args_list[0].args[1], (7, 1, "ASDEB4F899E2A8464C"))
        update_sql, params = cursor.execute.call_args_list[1].args
        self.assertIn("status = 'pending'", update_sql)
        self.assertEqual(params, (7, 1))


if __name__ == "__main__":
    unittest.main()
