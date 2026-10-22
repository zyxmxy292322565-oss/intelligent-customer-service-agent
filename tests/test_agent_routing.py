"""Feature-flag routing and Graph response adapter tests; no LLM or MySQL."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

import main
from agent.graph.runtime import run_graph_agent
from agent.conversation_scope import conversation_scope, current_scope


class AgentRoutingTests(unittest.TestCase):
    def setUp(self):
        for name, value in (
            ("list_conversations", [{"id": 1}]),
            ("get_conversation", {"id": 1}),
            ("save_message", None),
        ):
            service_patch = patch(f"main.{name}", return_value=value)
            service_patch.start()
            self.addCleanup(service_patch.stop)
        self.limiter_patches = [
            patch("middleware.rate_limit.global_limiter.allow", return_value=True),
            patch("middleware.rate_limit.user_limiter.allow", return_value=True),
        ]
        for limiter_patch in self.limiter_patches:
            limiter_patch.start()
            self.addCleanup(limiter_patch.stop)

    def _chat(self, use_graph: bool, message: str):
        from auth.jwt import create_access_token

        settings = SimpleNamespace(use_langgraph_agent=use_graph)
        with patch("main.get_settings", return_value=settings), \
             patch("main.run_agent") as legacy, \
             patch("agent.graph.runtime.run_graph_agent") as graph, \
             TestClient(main.app) as client:
            result = {
                "success": True,
                "code": "ACTION_PREVIEW_READY",
                "message": "请确认",
                "data": {"confirmation_id": "CONF_ROUTE_TEST", "preview": {}},
            }
            legacy.return_value = result
            graph.return_value = result
            response = client.post(
                "/agent/chat",
                json={"user_id": 999, "message": message},
                headers={"Authorization": f"Bearer {create_access_token(1)}"},
            )
        self.assertEqual(response.status_code, 200)
        selected, unused = (graph, legacy) if use_graph else (legacy, graph)
        selected.assert_called_once()
        self.assertEqual(selected.call_args.kwargs["current_user_id"], 1)
        unused.assert_not_called()
        return response.json()

    def test_feature_flag_switches_runner_without_changing_response(self):
        message = "我要退AirPods Pro，质量不好，退货退款"
        legacy = self._chat(False, message)
        graph = self._chat(True, message)
        for response in (legacy, graph):
            self.assertEqual(response["success"], True)
            self.assertEqual(response["code"], "ACTION_PREVIEW_READY")
            self.assertEqual(response["data"]["confirmation_id"], "CONF_ROUTE_TEST")
        self.assertEqual(
            {key: value for key, value in legacy.items() if key != "trace_id"},
            {key: value for key, value in graph.items() if key != "trace_id"},
        )

    def test_graph_adapter_links_task_without_changing_graph_nodes(self):
        observed = []

        def graph_result(*args):
            scope = current_scope()
            observed.append((scope.conversation_id, scope.task_id))
            return {"success": True, "code": "ACTION_PREVIEW_READY", "data": {"confirmation_id": "CONF_1"}}

        with conversation_scope(1, 12), \
             patch("agent.graph.runtime.get_active_agent_task", return_value=None), \
             patch("agent.graph.runtime.create_agent_task", return_value={"task_id": 42}) as create, \
             patch("agent.graph.runtime.get_task_context", return_value={}), \
             patch("agent.graph.runtime.update_task_context"), \
             patch("agent.graph.runtime.trace_step"), \
             patch("agent.graph.runtime._run_graph_agent_core", side_effect=graph_result), \
             patch("agent.graph.runtime.update_agent_task_context") as update:
            result = run_graph_agent("我要退AirPods", 1, "TRACE_1")
        self.assertEqual(result["code"], "ACTION_PREVIEW_READY")
        self.assertEqual(observed, [(12, 42)])
        self.assertEqual(create.call_args.kwargs["user_id"], 1)
        self.assertEqual(update.call_args.args[:3], (42, "waiting_confirm", "waiting_confirm"))
        self.assertTrue(update.call_args.args[3].startswith("EXEC_"))

    def test_graph_adapter_returns_preview_from_interrupt(self):
        fake_graph = Mock()
        fake_graph.invoke.return_value = {
            "confirmation_id": "CONF_GRAPH_TEST",
            "final_answer": "请确认",
            "__interrupt__": (SimpleNamespace(value={"preview": {"product_name": "AirPods Pro"}}),),
        }
        with patch("agent.graph.runtime._get_graph", return_value=fake_graph):
            response = run_graph_agent("我要退AirPods Pro，质量不好，退货退款", 1, "TRACE_TEST")
        self.assertEqual(response["code"], "ACTION_PREVIEW_READY")
        self.assertEqual(response["data"]["confirmation_id"], "CONF_GRAPH_TEST")
        state = fake_graph.invoke.call_args.args[0]
        self.assertEqual(state["current_user_id"], 1)
        self.assertEqual(state["trace_id"], "TRACE_TEST")
        self.assertIn("task_id", state)
        self.assertIn("confirmation_id", state)
        self.assertEqual(fake_graph.invoke.call_args.kwargs["config"]["configurable"]["thread_id"], state["task_id"])

    def test_missing_sale_type_does_not_invent_confirmation_id(self):
        fake_graph = Mock()
        fake_graph.invoke.return_value = {
            "intent": "create_after_sale",
            "plan": SimpleNamespace(steps=[SimpleNamespace(
                step_type="clarify", required_fields=["after_sale_type"]
            )]),
            "final_answer": "请补充售后类型",
        }
        with patch("agent.graph.runtime._get_graph", return_value=fake_graph):
            response = run_graph_agent("我要退AirPods Pro，质量不好", 1, "TRACE_TEST")
        self.assertEqual(response["code"], "AFTER_SALE_TYPE_REQUIRED")
        self.assertFalse(response["success"])
        self.assertIsNone(response["data"])

    def test_compiled_graph_reaches_preview_without_executing_business_action(self):
        params = {
            "product_name": "AirPods Pro", "after_sale_type": "return_refund",
            "reason": "质量不好", "order_no": None, "after_sale_no": None,
        }
        context = {
            "order_no": "ORDER_GRAPH", "order_item_id": 42,
            "product_name": "AirPods Pro",
        }
        pending = {
            "success": True, "code": "ACTION_PREVIEW_READY", "message": "请确认",
            "data": {"confirmation_id": "CONF_GRAPH_REAL", "preview": context},
        }
        with patch("agent.graph.nodes.analyze_state_input", return_value={
            "intent": "create_after_sale", "params": params,
        }), patch("agent.graph.nodes.resolve_state_context", return_value={
            "resolved_context": context,
        }), patch.dict("agent.graph.nodes.READ_TOOL_REGISTRY", {
            "policy_rag_tool": lambda **kwargs: {"evidence": [{"source": "test"}]},
        }), patch("agent.graph.nodes.build_create_after_sale_pending_action", return_value=pending) as build, \
             patch("agent.graph.nodes.confirm_action") as confirm, \
             patch("agent.graph.nodes.execute_confirmed_action") as execute:
            response = run_graph_agent("我要退AirPods Pro，质量不好，退货退款", 1, "TRACE_GRAPH_FULL")
        self.assertEqual(response["code"], "ACTION_PREVIEW_READY")
        self.assertEqual(response["data"]["confirmation_id"], "CONF_GRAPH_REAL")
        build.assert_called_once()
        confirm.assert_not_called()
        execute.assert_not_called()

    def test_compiled_graph_preview_inherits_conversation_scope(self):
        observed = []

        def build_pending(**kwargs):
            scope = current_scope()
            observed.append((scope.user_id, scope.conversation_id, scope.task_id) if scope else None)
            return {
                "success": True, "code": "ACTION_PREVIEW_READY", "message": "请确认",
                "data": {"confirmation_id": "CONF_GRAPH_SCOPED", "preview": {
                    "order_no": "ORDER_GRAPH", "product_name": "AirPods Pro",
                    "policy": {"decision": "ELIGIBLE_TO_APPLY"},
                }},
            }

        params = {
            "product_name": "AirPods Pro", "after_sale_type": "return_refund",
            "reason": "质量不好", "order_no": None, "after_sale_no": None,
        }
        resolved = {
            "order_no": "ORDER_GRAPH", "order_item_id": 42,
            "product_name": "AirPods Pro",
        }
        with conversation_scope(1, 99), \
             patch("agent.graph.runtime.get_active_agent_task", return_value=None), \
             patch("agent.graph.runtime.create_agent_task", return_value={"task_id": 77}), \
             patch("agent.graph.runtime.get_task_context", return_value={}), \
             patch("agent.graph.runtime.update_task_context"), \
             patch("agent.graph.runtime.update_agent_task_context"), \
             patch("agent.graph.runtime.trace_step"), \
             patch("agent.graph.nodes.analyze_state_input", return_value={
                 "intent": "create_after_sale", "params": params,
             }), \
             patch("agent.graph.nodes.resolve_state_context", return_value={
                 "resolved_context": resolved,
             }), \
             patch("agent.graph.nodes.build_create_after_sale_pending_action", side_effect=build_pending):
            result = run_graph_agent("我要退AirPods Pro，质量不好，退货退款", 1, "TRACE_GRAPH_SCOPE")
        self.assertEqual(result["code"], "ACTION_PREVIEW_READY")
        self.assertEqual(observed, [(1, 99, 77)])


if __name__ == "__main__":
    unittest.main()
