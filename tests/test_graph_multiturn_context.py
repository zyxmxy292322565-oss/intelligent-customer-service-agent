"""LangGraph adapter tests for MySQL-backed multi-turn task context."""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from langgraph.types import Interrupt

from agent.conversation_scope import conversation_scope
from agent.graph.runtime import run_graph_agent
from agent.graph.state_graph import build_graph
from agent.input_analyzer import analyze_state_input
from schemas.agent_schema import BusinessParams, IntentResult


class _Step:
    def __enter__(self):
        return self

    def set_result(self, _result):
        pass

    def __exit__(self, *_args):
        return False


class _Graph:
    """Runs the real Graph input adapter and a deterministic terminal plan."""

    def __init__(self, existing_after_sale: bool = False):
        self.calls = []
        self.existing_after_sale = existing_after_sale

    def invoke(self, state, config):
        state = dict(state)
        state.update(analyze_state_input(state))
        self.calls.append((dict(state), config))
        params = state["params"]
        missing = [
            field for field in ("product_name", "after_sale_type", "reason")
            if not params.get(field)
        ]
        if missing:
            state["plan"] = SimpleNamespace(steps=[SimpleNamespace(
                step_type="clarify", required_fields=missing
            )])
            state["final_answer"] = "请补充售后申请信息"
            return state
        if self.existing_after_sale:
            state["error"] = {
                "code": "AFTER_SALE_ALREADY_EXISTS",
                "message": "该商品已有进行中的售后申请",
            }
            return state
        state["confirmation_id"] = "CONF_GRAPH_MULTI"
        state["final_answer"] = "请确认"
        state["__interrupt__"] = (
            Interrupt(value={"preview": dict(params)}, id="graph-multi"),
        )
        return state


class GraphMultiturnContextTests(unittest.TestCase):
    def setUp(self):
        self.tasks = {}
        self.contexts = {}
        self.next_task_id = 100
        self.graph = _Graph()

        def key_for_scope():
            from agent.conversation_scope import current_scope
            scope = current_scope()
            return scope.user_id, scope.conversation_id

        def active(user_id):
            task = self.tasks.get(key_for_scope())
            if task and task["user_id"] == user_id and task["status"] in {
                "running", "waiting_input", "waiting_confirm"
            }:
                return dict(task)
            return None

        def create_task(**kwargs):
            scope_key = key_for_scope()
            task_id = self.next_task_id
            self.next_task_id += 1
            self.tasks[scope_key] = {
                "id": task_id,
                "user_id": kwargs["user_id"],
                "conversation_id": scope_key[1],
                "goal": kwargs["goal"],
                "status": "running",
            }
            self.contexts[task_id] = {}
            return {"task_id": task_id}

        def get_context(task_id, _execution_id=None):
            return dict(self.contexts.get(task_id, {}))

        def save_context(*, task_id, context, execution_id=None):
            self.contexts[task_id] = dict(context)

        def save_status(task_id, current_step, status, execution_id=None):
            for task in self.tasks.values():
                if task["id"] == task_id:
                    task["status"] = status
                    task["current_step"] = current_step
                    return
            self.fail(f"unknown task {task_id}")

        def route(text):
            return IntentResult(
                intent="create_after_sale" if "AirPods" in text else "unknown"
            )

        def extract(text):
            reason = None
            if "左边" in text or "左耳" in text:
                reason = "左边没声音"
            elif "不喜欢" in text:
                reason = "不喜欢"
            elif "质量不好" in text:
                reason = "质量不好"
            elif "不确定" in text:
                reason = text
            return BusinessParams(
                product_name="AirPods Pro 2" if "AirPods" in text else None,
                after_sale_type=(
                    "return_refund" if "退货退款" in text else
                    "exchange" if "换货" in text else None
                ),
                reason=reason,
            )

        patches = (
            patch("agent.graph.runtime.get_active_agent_task", side_effect=active),
            patch("agent.graph.runtime.create_agent_task", side_effect=create_task),
            patch("agent.graph.runtime.claim_agent_task_execution", return_value=True),
            patch("agent.graph.runtime.get_task_context", side_effect=get_context),
            patch("agent.graph.runtime.update_task_context", side_effect=save_context),
            patch("agent.graph.runtime.update_agent_task_context", side_effect=save_status),
            patch("agent.graph.runtime.trace_step", return_value=_Step()),
            patch("agent.graph.runtime._get_graph", side_effect=lambda: self.graph),
            patch("agent.input_analyzer.route_intent", side_effect=route),
            patch("agent.graph.runtime.route_intent", side_effect=route),
            patch("agent.input_analyzer.extract_business_params", side_effect=extract),
        )
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def _run(self, text, user=1, conversation=10, trace="TRACE"):
        with conversation_scope(user, conversation):
            return run_graph_agent(text, user, trace)

    def test_graph_three_turn_completion_uses_one_task(self):
        first = self._run("我要申请 AirPods Pro 2 售后", trace="TRACE-1")
        second = self._run("退货退款", trace="TRACE-2")
        third = self._run("左边耳机没有声音", trace="TRACE-3")
        self.assertEqual(first["code"], "AFTER_SALE_TYPE_REQUIRED")
        self.assertEqual(second["code"], "AFTER_SALE_REASON_REQUIRED")
        self.assertEqual(third["code"], "ACTION_PREVIEW_READY")
        self.assertEqual(self.tasks[(1, 10)]["id"], 100)
        self.assertEqual(self.next_task_id, 101)

    def test_compiled_graph_three_turn_business_path(self):
        self.next_task_id = 910000
        compiled_graph = build_graph()
        resolved = {
            "order_id": 70,
            "order_no": "ORD_GRAPH_MULTI",
            "order_status": "delivered",
            "delivered_at": "2026-10-15T10:00:00",
            "category": "耳机",
            "order_item_id": 71,
            "product_name": "AirPods Pro 2",
        }
        pending = {
            "success": True,
            "code": "ACTION_PREVIEW_READY",
            "message": "请确认",
            "data": {
                "confirmation_id": "CONF_COMPILED_MULTI",
                "preview": dict(resolved),
            },
        }
        with patch("agent.graph.runtime._get_graph", return_value=compiled_graph), \
             patch("agent.graph.nodes.resolve_state_context", return_value={
                 "resolved_context": resolved, "error": None,
             }), \
             patch.dict("agent.graph.nodes.READ_TOOL_REGISTRY", {
                 "policy_rag_tool": lambda **_kwargs: {
                     "success": True,
                     "evidence": [{
                         "source": "耳机类商品规则.txt",
                         "file_hash": "abc123",
                         "content": "质量问题30天内可申请售后",
                     }],
                 },
             }), \
             patch("agent.graph.nodes.build_create_after_sale_pending_action",
                   return_value=pending):
            first = self._run("我要申请 AirPods Pro 2 售后", trace="TRACE-C1")
            second = self._run("退货退款", trace="TRACE-C2")
            third = self._run("左边耳机没有声音", trace="TRACE-C3")
        self.assertEqual(first["code"], "AFTER_SALE_TYPE_REQUIRED")
        self.assertEqual(second["code"], "AFTER_SALE_REASON_REQUIRED")
        self.assertEqual(third["code"], "ACTION_PREVIEW_READY")
        self.assertEqual(third["data"]["confirmation_id"], "CONF_COMPILED_MULTI")
        self.assertEqual(self.contexts[910000]["product_name"], "AirPods Pro 2")
        self.assertEqual(self.contexts[910000]["after_sale_type"], "return_refund")
        self.assertEqual(self.contexts[910000]["reason"], "左边没声音")

    def test_second_turn_restores_product_name(self):
        self._run("我要申请 AirPods Pro 2 售后", trace="TRACE-1")
        self._run("退货退款", trace="TRACE-2")
        context = self.contexts[100]
        self.assertEqual(context["product_name"], "AirPods Pro 2")
        self.assertEqual(context["after_sale_type"], "return_refund")
        self.assertEqual(self.graph.calls[-1][0]["task_id"], "100")

    def test_current_reason_correction_replaces_old_reason_source(self):
        self._run("我要申请 AirPods Pro 2 售后", trace="TRACE-1")
        self._run("退货退款", trace="TRACE-2")
        self._run("质量不好", trace="TRACE-3")
        self.tasks[(1, 10)]["status"] = "waiting_input"
        self._run("更正原因：没有质量问题，就是不喜欢了", trace="TRACE-4")
        context = self.contexts[100]
        self.assertEqual(context["reason"], "不喜欢")
        self.assertEqual(
            context["original_reason_text"],
            "更正原因：没有质量问题，就是不喜欢了",
        )
        self.assertNotIn("质量不好；", context["original_reason_text"])

    def test_different_conversations_do_not_share_context(self):
        self._run("我要申请 AirPods Pro 2 售后", conversation=10, trace="TRACE-A")
        result = self._run("退货退款", conversation=11, trace="TRACE-B")
        self.assertNotEqual(result["code"], "ACTION_PREVIEW_READY")
        self.assertNotEqual(self.tasks[(1, 10)]["id"], self.tasks[(1, 11)]["id"])
        self.assertNotIn("product_name", self.contexts[self.tasks[(1, 11)]["id"]])

    def test_different_users_do_not_share_context(self):
        self._run("我要申请 AirPods Pro 2 售后", user=1, conversation=10)
        result = self._run("退货退款", user=2, conversation=10)
        self.assertNotEqual(result["code"], "ACTION_PREVIEW_READY")
        self.assertNotEqual(self.tasks[(1, 10)]["id"], self.tasks[(2, 10)]["id"])

    def test_backend_restart_recovers_from_persisted_context(self):
        self._run("我要申请 AirPods Pro 2 售后", trace="TRACE-1")
        persisted = dict(self.contexts[100])
        self.graph = _Graph()  # no in-process Graph state survives
        self.contexts[100] = persisted
        result = self._run("退货退款", trace="TRACE-2")
        self.assertEqual(result["code"], "AFTER_SALE_REASON_REQUIRED")
        self.assertEqual(self.contexts[100]["product_name"], "AirPods Pro 2")

    def test_single_turn_still_creates_preview(self):
        result = self._run(
            "我要申请 AirPods Pro 2 退货退款，质量不好",
            trace="TRACE-SINGLE",
        )
        self.assertEqual(result["code"], "ACTION_PREVIEW_READY")
        self.assertEqual(self.contexts[100]["original_reason_text"],
                         "我要申请 AirPods Pro 2 退货退款，质量不好")

    def test_conflicting_reason_preserves_complete_current_utterance(self):
        result = self._run(
            "我的AirPods Pro 2没有质量问题，但是左边没声音，我想申请退货退款",
            trace="TRACE-CONFLICT",
        )
        self.assertEqual(result["code"], "ACTION_PREVIEW_READY")
        context = self.contexts[100]
        self.assertEqual(context["reason"], "左边没声音")
        self.assertEqual(
            context["original_reason_text"],
            "我的AirPods Pro 2没有质量问题，但是左边没声音，我想申请退货退款",
        )

    def test_existing_after_sale_error_never_returns_preview(self):
        self.graph = _Graph(existing_after_sale=True)
        result = self._run(
            "我要申请 AirPods Pro 2 退货退款，质量不好",
            trace="TRACE-DUP",
        )
        self.assertEqual(result["code"], "AFTER_SALE_ALREADY_EXISTS")
        self.assertFalse(result["success"])
        self.assertEqual(self.tasks[(1, 10)]["status"], "failed")

    def test_completed_task_starts_new_task_and_trace_does_not_change_context_scope(self):
        self._run(
            "我要申请 AirPods Pro 2 退货退款，质量不好",
            trace="TRACE-OLD",
        )
        self.tasks[(1, 10)]["status"] = "completed"
        result = self._run("退货退款", trace="TRACE-NEW")
        self.assertNotEqual(result["code"], "ACTION_PREVIEW_READY")
        self.assertEqual(self.tasks[(1, 10)]["id"], 101)
        self.assertNotEqual(self.contexts[100], self.contexts[101])


if __name__ == "__main__":
    unittest.main()
