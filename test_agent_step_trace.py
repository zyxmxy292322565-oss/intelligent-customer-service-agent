"""P1 Agent 步骤链路测试：隔离业务服务和 MySQL。"""

import json
import unittest
from datetime import datetime, timedelta
from contextlib import ExitStack
from unittest.mock import patch

import main
from agent import runtime
from agent.step_trace import _json
from schemas.agent_schema import AgentConfirmRequest, AgentInputAnalysis, BusinessParams
from services.policy_demo_rules import DEMO_POLICY_SOURCES


class AgentStepTraceTests(unittest.TestCase):
    def setUp(self):
        self.rows = {}
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)

        def start(task_id, trace_id, step_name, input_data):
            step_id = len(self.rows) + 1
            self.rows[step_id] = {
                "task_id": task_id, "trace_id": trace_id,
                "step_name": step_name, "input_data": input_data,
                "status": "START",
            }
            return step_id

        def finish(step_id, status, output_data, error_message, duration_ms):
            self.rows[step_id].update(
                status=status, output_data=output_data,
                error_message=error_message, duration_ms=duration_ms,
            )

        self.stack.enter_context(patch("agent.step_trace._start_step", side_effect=start))
        self.stack.enter_context(patch("agent.step_trace._finish_step", side_effect=finish))
        self.stack.enter_context(patch("agent.runtime.get_active_agent_task", return_value=None))
        self.stack.enter_context(patch("agent.runtime.create_agent_task", return_value={"task_id": 101}))
        self.stack.enter_context(patch("agent.runtime.get_task_context", return_value={}))
        self.stack.enter_context(patch("agent.runtime.update_task_context"))
        self.task_update = self.stack.enter_context(patch("agent.runtime.update_agent_task_context"))
        self.stack.enter_context(patch("agent.write_workflow.save_current_confirmation"))
        self.stack.enter_context(patch("agent.write_workflow.active_after_sale_for_preview", return_value=None))
        self.stack.enter_context(patch("main.get_pending_action", return_value={"task_id": 101}))
        self.stack.enter_context(patch("main.confirm_action", return_value={
            "success": True, "code": "ACTION_CONFIRMED", "message": "确认成功"
        }))
        self.stack.enter_context(patch("main.execute_confirmed_action", return_value={
            "success": True, "code": "ACTION_EXECUTED", "message": "执行成功"
        }))
        self.analyzer = self.stack.enter_context(patch("agent.runtime.analyze_agent_input", return_value=
            AgentInputAnalysis(intent="create_after_sale", params=BusinessParams(
                product_name="无线键盘", reason="质量不好", after_sale_type="return_refund"
            ))
        ))
        self.resolver = self.stack.enter_context(patch(
            "agent.write_workflow.resolve_order_item_by_product",
            return_value={"success": True, "data": {
                "user_id": 1, "order_id": 3, "order_no": "ORDER_TEST",
                "order_status": "delivered", "order_item_id": 4,
                "product_name": "无线键盘", "category": "耳机",
                "created_at": datetime(2026, 9, 1), "paid_at": datetime(2026, 9, 1),
                "delivered_at": datetime.now() - timedelta(days=20),
            }}
        ))
        source = DEMO_POLICY_SOURCES["耳机类商品规则.txt"]
        self.stack.enter_context(patch("agent.write_workflow.check_after_sale_policy", return_value={
            "decision": "ELIGIBLE_TO_APPLY", "policy_type": "earphone_quality_return",
            "days_since_delivery": 20, "policy_deadline_days": 30,
            "policy_message": "可提出申请，后续仍需审核。", "requires_review": True,
            "sources": [source.filename],
            "policy_versions": {source.filename: source.document_version_label},
            "policy_file_hashes": {source.filename: source.file_hash},
            "evidence": [],
        }))
        self.create_action = self.stack.enter_context(patch(
            "agent.write_workflow.create_pending_action",
            return_value={"confirmation_id": "CONF_TEST", "status": "pending"}
        ))

    def test_complete_chat_and_confirm_steps_share_task_and_per_request_trace(self):
        chat = runtime.run_agent("我要退无线键盘，质量不好", 1, "CHAT_TRACE")
        confirm = main.agent_confirm(
            AgentConfirmRequest(user_id=1, confirmation_id="CONF_TEST"), current_user_id=1
        )
        self.assertEqual(chat["code"], "ACTION_PREVIEW_READY")
        self.assertEqual(confirm["code"], "ACTION_EXECUTED")
        self.assertEqual([row["step_name"] for row in self.rows.values()], [
            "input_analysis", "context_merge", "resolve_order", "policy_check", "build_preview",
            "create_pending_action", "confirm", "execute",
        ])
        self.assertTrue(all(row["task_id"] == 101 for row in self.rows.values()))
        self.assertTrue(all(row["trace_id"] == "CHAT_TRACE" for row in list(self.rows.values())[:6]))
        self.assertTrue(all(row["trace_id"] == confirm["trace_id"] for row in list(self.rows.values())[6:]))
        self.assertTrue(all(row["status"] == "SUCCESS" for row in self.rows.values()))
        self.assertTrue(all(row["duration_ms"] >= 0 for row in self.rows.values()))
        self.create_action.assert_called_once_with(
            user_id=1, action_type="create_after_sale",
            preview=chat["data"]["preview"], trace_id="CHAT_TRACE", task_id=101,
        )

    def test_business_failure_is_recorded_without_changing_return(self):
        self.resolver.return_value = {
            "success": False, "code": "PRODUCT_NOT_FOUND", "message": "没有找到对应商品订单"
        }
        result = runtime.run_agent("我要退无线键盘，质量不好", 1, "FAILED_TRACE")
        self.assertEqual(result["code"], "PRODUCT_NOT_FOUND")
        step = list(self.rows.values())[-1]
        self.assertEqual(step["step_name"], "resolve_order")
        self.assertEqual(step["status"], "FAILED")
        self.assertEqual(step["error_message"], "没有找到对应商品订单")
        self.assertEqual(step["trace_id"], "FAILED_TRACE")
        self.create_action.assert_not_called()
        self.assertEqual(self.task_update.call_args.kwargs["status"], "failed")

    def test_missing_after_sale_type_waits_for_input(self):
        self.analyzer.return_value = AgentInputAnalysis(
            intent="create_after_sale",
            params=BusinessParams(product_name="无线键盘", reason="质量不好"),
        )
        result = runtime.run_agent("我要退无线键盘，质量不好", 1, "TYPE_TRACE")
        self.assertEqual(result["code"], "AFTER_SALE_TYPE_REQUIRED")
        self.resolver.assert_not_called()
        self.assertEqual(self.task_update.call_args.kwargs["status"], "waiting_input")
        self.assertEqual([row["step_name"] for row in self.rows.values()], [
            "input_analysis", "context_merge"
        ])

    def test_missing_reason_waits_for_input(self):
        self.analyzer.return_value = AgentInputAnalysis(
            intent="create_after_sale",
            params=BusinessParams(product_name="无线键盘", after_sale_type="return_refund"),
        )
        result = runtime.run_agent("我要退无线键盘，退货退款", 1, "REASON_TRACE")
        self.assertEqual(result["code"], "AFTER_SALE_REASON_REQUIRED")
        self.resolver.assert_not_called()
        self.assertEqual(self.task_update.call_args.kwargs["status"], "waiting_input")

    def test_exception_is_recorded_and_propagated(self):
        self.resolver.side_effect = RuntimeError("订单服务不可用")
        with self.assertRaisesRegex(RuntimeError, "订单服务不可用"):
            runtime.run_agent("我要退无线键盘，质量不好", 1, "ERROR_TRACE")
        step = list(self.rows.values())[-1]
        self.assertEqual(step["status"], "FAILED")
        self.assertIn("订单服务不可用", step["error_message"])
        self.assertEqual(step["trace_id"], "ERROR_TRACE")

    def test_sensitive_free_text_is_redacted(self):
        data = json.loads(_json({"reason": "质量不好", "details": {"token": "secret"}}))
        self.assertEqual(data, {"reason": "[REDACTED]", "details": {"token": "[REDACTED]"}})

    def test_trace_storage_failure_does_not_change_business_result(self):
        with patch("agent.step_trace._start_step", side_effect=RuntimeError("trace DB unavailable")):
            with patch("agent.step_trace.logging.exception"):
                result = runtime.run_agent("我要退无线键盘，质量不好", 1, "TRACE_DOWN")
        self.assertEqual(result["code"], "ACTION_PREVIEW_READY")
        self.create_action.assert_called_once()

    def test_unknown_confirmation_is_recorded_without_task_id(self):
        with patch("main.get_pending_action", return_value=None):
            with patch("main.confirm_action", return_value={
                "success": False, "code": "ACTION_NOT_FOUND", "message": "待确认操作不存在"
            }):
                result = main.agent_confirm(
                    AgentConfirmRequest(user_id=1, confirmation_id="CONF_UNKNOWN"),
                    current_user_id=1,
                )
        self.assertEqual(result["code"], "ACTION_NOT_FOUND")
        row = list(self.rows.values())[-1]
        self.assertIsNone(row["task_id"])
        self.assertEqual(row["step_name"], "confirm")
        self.assertEqual(row["status"], "FAILED")
        self.assertEqual(row["trace_id"], result["trace_id"])


if __name__ == "__main__":
    unittest.main()
