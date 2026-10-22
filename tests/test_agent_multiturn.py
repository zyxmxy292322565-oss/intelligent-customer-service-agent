"""No-MySQL regression tests for create_after_sale parameter completion."""

import unittest
from unittest.mock import patch

from agent.runtime import run_agent
from schemas.agent_schema import BusinessParams, IntentResult


class _Step:
    def __enter__(self):
        return self

    def set_result(self, _result):
        pass

    def __exit__(self, *_args):
        return False


class CreateAfterSaleMultiturnTests(unittest.TestCase):
    def setUp(self):
        self.task = None
        self.context = {}
        self.route_inputs = []
        self.pending_actions = []

        def active(_user_id):
            return self.task if self.task and self.task["status"] in {
                "running", "waiting_input", "waiting_confirm"
            } else None

        def create_task(**kwargs):
            self.task = {"id": 42, "status": "running", "goal": kwargs["goal"]}
            return {"task_id": 42}

        def update_context(*, task_id, context, execution_id=None):
            self.assertEqual(task_id, 42)
            self.context = context.copy()

        def update_status(*, task_id, current_step, status, execution_id=None):
            self.assertEqual(task_id, 42)
            self.task["status"] = status
            self.task["current_step"] = current_step

        def route(text):
            self.route_inputs.append(text)
            return IntentResult(intent="create_after_sale" if "无线鼠标" in text else "unknown")

        def extract(text):
            return BusinessParams(
                product_name="无线鼠标" if "无线鼠标" in text else None,
                after_sale_type="return_refund" if "退货退款" in text else None,
                reason="质量不好" if "质量不好" in text else (
                    "左边没声音" if "左边没声音" in text else None),
            )

        def create_action(**kwargs):
            self.pending_actions.append(kwargs)
            return {"confirmation_id": "CONF_TEST", "status": "pending"}

        resolver_result = {
            "success": True, "code": "SUCCESS", "data": {
                "user_id": 1,
                "order_id": 12, "order_no": "ORD202610140006",
                "order_status": "delivered", "category": "电脑配件",
                "created_at": None, "paid_at": None, "delivered_at": None,
                "order_item_id": 12, "product_name": "无线鼠标",
                "quantity": 1, "unit_price": 199,
            },
        }
        for target, kwargs in (
            ("agent.runtime.get_active_agent_task", {"side_effect": active}),
            ("agent.runtime.create_agent_task", {"side_effect": create_task}),
            ("agent.runtime.claim_agent_task_execution", {"return_value": True}),
            ("agent.runtime.get_task_context", {"side_effect": lambda _id, _execution_id=None: self.context.copy()}),
            ("agent.runtime.update_task_context", {"side_effect": update_context}),
            ("agent.runtime.update_agent_task_context", {"side_effect": update_status}),
            ("agent.runtime.trace_step", {"return_value": _Step()}),
            ("agent.write_workflow.trace_step", {"return_value": _Step()}),
            ("agent.input_analyzer.route_intent", {"side_effect": route}),
            ("agent.runtime.route_intent", {"side_effect": route}),
            ("agent.input_analyzer.extract_business_params", {"side_effect": extract}),
            ("agent.write_workflow.resolve_order_item_by_product", {"return_value": resolver_result}),
            ("agent.write_workflow.create_pending_action", {"side_effect": create_action}),
            ("agent.write_workflow.active_after_sale_for_preview", {"return_value": None}),
            ("agent.write_workflow.save_current_confirmation", {}),
        ):
            replacement = patch(target, **kwargs)
            replacement.start()
            self.addCleanup(replacement.stop)

    def test_three_turn_completion_reuses_task_but_uncovered_product_needs_review(self):
        first = run_agent("我要申请无线鼠标售后", 1, "trace-1")
        self.assertEqual(first["code"], "AFTER_SALE_TYPE_REQUIRED")
        self.assertEqual(self.context["product_name"], "无线鼠标")
        self.assertEqual(self.task["status"], "waiting_input")

        second = run_agent("退货退款", 1, "trace-2")
        self.assertEqual(second["code"], "AFTER_SALE_REASON_REQUIRED")
        self.assertEqual(self.context["after_sale_type"], "return_refund")
        self.assertEqual(self.task["status"], "waiting_input")
        self.assertEqual(self.pending_actions, [])

        third = run_agent("质量不好", 1, "trace-3")
        self.assertEqual(third["code"], "NEEDS_REVIEW")
        self.assertEqual(self.task["id"], 42)
        self.assertEqual(self.context["reason"], "质量不好")
        self.assertEqual(len(self.pending_actions), 0)
        self.assertEqual(self.route_inputs, ["我要申请无线鼠标售后"])

    def test_single_turn_uncovered_product_cannot_create_preview(self):
        result = run_agent("我要申请无线鼠标退货退款，因为质量不好", 1, "trace-single")
        self.assertEqual(result["code"], "NEEDS_REVIEW")
        self.assertEqual(self.task["status"], "completed")
        self.assertEqual(len(self.pending_actions), 0)

    def test_existing_waiting_task_without_saved_intent_uses_original_goal(self):
        run_agent("我要申请无线鼠标售后", 1, "trace-1")
        self.context.pop("_intent")
        second = run_agent("退货退款", 1, "trace-2")
        self.assertEqual(second["code"], "AFTER_SALE_REASON_REQUIRED")
        self.assertEqual(self.route_inputs, ["我要申请无线鼠标售后"] * 2)

    def test_reason_correction_replaces_previous_source_in_context(self):
        run_agent("我要申请无线鼠标售后", 1, "trace-1")
        run_agent("退货退款，因为质量不好", 1, "trace-2")
        self.assertEqual(self.context["original_reason_text"], "退货退款，因为质量不好")
        # Simulate a later explicit correction while the task is still open.
        self.task["status"] = "waiting_input"
        run_agent("更正原因：没有质量问题，就是不喜欢了", 1, "trace-3")
        self.assertEqual(self.context["original_reason_text"], "更正原因：没有质量问题，就是不喜欢了")
        self.assertNotIn("质量不好；", self.context["original_reason_text"])


if __name__ == "__main__":
    unittest.main()
