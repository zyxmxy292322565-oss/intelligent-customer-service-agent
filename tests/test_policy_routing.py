"""Policy intent routing stays read-only in both Agent modes."""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent.graph.runtime import run_graph_agent
from agent.intent_router import route_intent
from agent.read_workflow import run_read_workflow
from schemas.agent_schema import IntentResult


POLICY_RESULT = {
    "success": True,
    "code": "RAG_OK",
    "message": "找到政策依据",
    "answer": "可参考以下退货规则，实际申请仍需核验订单。",
    "evidence": [{"source": "returns.md", "content": "退货规则"}],
}


class PolicyRoutingTests(unittest.TestCase):
    def test_intent_schema_and_prompt_include_policy_questions(self):
        with patch("agent.intent_router.get_llm") as llm:
            llm.return_value.with_structured_output.return_value.invoke.return_value = (
                IntentResult(intent="query_policy")
            )
            self.assertEqual(route_intent("AirPods用了20天还能退吗？").intent, "query_policy")
            prompt = llm.return_value.with_structured_output.return_value.invoke.call_args.args[0][0].content
        self.assertIn("用了20天还能退吗", prompt)
        self.assertIn("create_after_sale", prompt)

    def test_legacy_read_path_calls_only_policy_tool(self):
        analysis = SimpleNamespace(intent="query_policy")
        with patch("agent.read_workflow.policy_rag_tool") as tool, \
             patch("agent.read_workflow.get_llm") as llm, \
             patch("agent.policy_response.generate_policy_answer", return_value="20天已过无理由退货期；质量问题可申请检测。") as generator:
            tool.invoke.return_value = POLICY_RESULT
            result = run_read_workflow("AirPods用了20天还能退吗？", 1, analysis)
        tool.invoke.assert_called_once_with({"question": "AirPods用了20天还能退吗？"})
        llm.assert_not_called()
        generator.assert_called_once_with("AirPods用了20天还能退吗？", POLICY_RESULT["evidence"])
        self.assertEqual(result["code"], "POLICY_RESULT_READY")
        self.assertEqual(result["data"]["answer"], "20天已过无理由退货期；质量问题可申请检测。")
        self.assertEqual(result["data"]["sources"], ["returns.md"])
        self.assertEqual(result["data"]["evidence"][0]["source"], "returns.md")
        self.assertNotIn("confirmation_id", result.get("data") or {})

    def test_compiled_graph_policy_path_does_not_build_preview(self):
        with patch("agent.graph.nodes.analyze_state_input", return_value={
            "intent": "query_policy", "params": {},
        }), patch.dict("agent.graph.nodes.READ_TOOL_REGISTRY", {
            "policy_rag_tool": lambda **kwargs: POLICY_RESULT,
        }), patch("agent.graph.nodes.build_create_after_sale_pending_action") as build, \
             patch("agent.graph.nodes.confirm_action") as confirm, \
             patch("agent.graph.nodes.execute_confirmed_action") as execute, \
             patch("agent.policy_response.generate_policy_answer", return_value="质量问题可申请检测。") as generator:
            result = run_graph_agent("AirPods用了20天还能退吗？", 1, "TRACE_POLICY_TEST")
        self.assertEqual(result["code"], "POLICY_RESULT_READY")
        self.assertEqual(result["data"]["answer"], "质量问题可申请检测。")
        generator.assert_called_once_with("AirPods用了20天还能退吗？", POLICY_RESULT["evidence"])
        self.assertEqual(result["data"]["evidence"][0]["source"], "returns.md")
        self.assertNotIn("confirmation_id", result.get("data") or {})
        build.assert_not_called()
        confirm.assert_not_called()
        execute.assert_not_called()

    def test_no_evidence_does_not_claim_policy_approval(self):
        analysis = SimpleNamespace(intent="query_policy")
        with patch("agent.read_workflow.policy_rag_tool") as tool, \
             patch("agent.policy_response.generate_policy_answer") as generator:
            tool.invoke.return_value = {
                "success": True, "code": "RAG_OK", "message": "依据不足", "refused": True,
                "evidence": [],
            }
            result = run_read_workflow("超过多少天可以退？", 1, analysis)
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "POLICY_EVIDENCE_INSUFFICIENT")
        generator.assert_not_called()


if __name__ == "__main__":
    unittest.main()
