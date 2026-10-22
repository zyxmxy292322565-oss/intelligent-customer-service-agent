"""Offline tests; no LLM, Redis or database is contacted."""
import logging
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from agent.input_analyzer import analyze_agent_input
from agent.input_budget import InputAnalysisError, run_analysis_stage
from agent.runtime import run_agent
from agent.graph.runtime import run_graph_agent
from agent.conversation_scope import conversation_scope
from schemas.agent_schema import BusinessParams, IntentResult
from llm.client import get_llm


class FakeStep:
    records = []
    def __init__(self, *args):
        self.args = args
    def __enter__(self):
        return self
    def set_result(self, value):
        self.value = value
    def __exit__(self, exc_type, exc, tb):
        self.records.append((self.args[2], type(exc).__name__ if exc else None, str(exc) if exc else None))
        return False


class InputAnalysisBudgetTests(unittest.TestCase):
    def setUp(self):
        FakeStep.records = []
        self.intent = patch("agent.input_analyzer.route_intent", return_value=IntentResult(intent="query_policy"))
        self.params = patch("agent.input_analyzer.extract_business_params", return_value=BusinessParams(product_name="耳机"))
        self.route = self.intent.start()
        self.extract = self.params.start()
        self.addCleanup(self.intent.stop)
        self.addCleanup(self.params.stop)

    def test_success_and_separate_logs_with_correlation(self):
        with self.assertLogs("agent.input_analyzer", level="INFO") as logs:
            result = analyze_agent_input("政策问题", trace_id="trace-x", task_id=4)
        self.assertEqual(result.intent, "query_policy")
        self.assertEqual(result.params.product_name, "耳机")
        self.assertTrue(any("stage=intent status=success" in s for s in logs.output))
        self.assertTrue(any("stage=parameters status=success" in s for s in logs.output))
        self.assertEqual(logs.records[-1].task_id, 4)
        self.assertEqual(logs.records[-1].request_id, "trace-x")

    def test_intent_http_timeout(self):
        self.route.side_effect = httpx.ReadTimeout("secret should not be logged")
        with self.assertLogs("agent.input_analyzer", "WARNING") as logs, self.assertRaises(InputAnalysisError) as ctx:
            analyze_agent_input("private prompt", trace_id="t", task_id=1)
        self.assertEqual((ctx.exception.stage, ctx.exception.category), ("intent", "http_timeout"))
        self.extract.assert_not_called()
        self.assertNotIn("secret", str(logs.output))
        self.assertNotIn("private prompt", str(logs.output))

    def test_parameters_http_timeout(self):
        self.extract.side_effect = httpx.ReadTimeout("timeout")
        with self.assertRaises(InputAnalysisError) as ctx:
            analyze_agent_input("政策")
        self.assertEqual((ctx.exception.stage, ctx.exception.category), ("parameters", "http_timeout"))

    def test_invalid_intent_structured_result(self):
        self.route.return_value = None
        with self.assertRaises(InputAnalysisError) as ctx:
            analyze_agent_input("政策")
        self.assertEqual(ctx.exception.category, "invalid_response")
        self.extract.assert_not_called()

    def test_invalid_parameter_structured_result(self):
        self.extract.return_value = {"unsafe": "result"}
        with self.assertRaises(InputAnalysisError) as ctx:
            analyze_agent_input("政策")
        self.assertEqual((ctx.exception.stage, ctx.exception.category), ("parameters", "invalid_response"))

    def test_wall_clock_timeout_does_not_continue_business(self):
        release = threading.Event()
        self.addCleanup(release.set)
        self.route.side_effect = lambda text: (release.wait(1), IntentResult(intent="query_policy"))[1]
        with patch("agent.input_budget.INPUT_ANALYSIS_STAGE_SECONDS", .02):
            start = time.monotonic()
            with self.assertRaises(InputAnalysisError) as ctx:
                analyze_agent_input("政策")
            self.assertLess(time.monotonic() - start, .5)
        self.assertEqual(ctx.exception.category, "stage_timeout")
        self.extract.assert_not_called()
        release.set()

    def test_total_deadline_separate_from_stage_limit(self):
        with self.assertRaises(InputAnalysisError) as ctx:
            run_analysis_stage("intent", lambda x: x, "x", time.monotonic()-1)
        self.assertEqual(ctx.exception.category, "analysis_timeout")

    def test_trace_records_failure_category_and_elapsed(self):
        from agent.step_trace import trace_step
        with patch("agent.step_trace._start_step", return_value=99), \
             patch("agent.step_trace._finish_step") as finish:
            with self.assertRaises(InputAnalysisError):
                with trace_step(42, "trace", "input_analysis"):
                    raise InputAnalysisError("parameters", "http_timeout")
        args = finish.call_args.args
        self.assertEqual(args[0:2], (99, "FAILED"))
        self.assertIn("stage=parameters category=http_timeout", args[3])
        self.assertGreaterEqual(args[4], 0)

    def test_capacity_exhaustion_fails_closed(self):
        with patch("agent.input_budget._slots") as slots:
            slots.acquire.return_value = False
            with self.assertRaises(InputAnalysisError) as ctx:
                run_analysis_stage("intent", lambda x: x, "x", time.monotonic()+1)
        self.assertEqual(ctx.exception.category, "capacity_exhausted")

    def test_frontend_displays_distinct_safe_categories(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "frontend"))
        from components.result import ERROR_MESSAGES
        self.assertNotEqual(ERROR_MESSAGES["REQUEST_TIMEOUT"], ERROR_MESSAGES["BACKEND_UNAVAILABLE"])
        self.assertNotEqual(ERROR_MESSAGES["INPUT_ANALYSIS_TIMEOUT"], ERROR_MESSAGES["REQUEST_TIMEOUT"])

    def test_http_client_has_no_implicit_retry(self):
        settings = SimpleNamespace(deepseek_model="m", deepseek_api_key="test", deepseek_base_url="https://example.invalid")
        with patch("llm.client.get_settings", return_value=settings):
            llm = get_llm(disable_thinking=True, input_analysis=True)
        self.assertEqual(llm.max_retries, 0)
        self.assertEqual(llm.request_timeout, 15.0)

    def test_legacy_failure_closes_task_and_trace_without_rag_or_preview(self):
        self.route.side_effect = httpx.ReadTimeout("timeout")
        with patch("agent.runtime.get_active_agent_task", return_value=None), \
             patch("agent.runtime.create_agent_task", return_value={"task_id": 42}), \
             patch("agent.runtime.get_task_context", return_value={}), \
             patch("agent.runtime.trace_step", FakeStep), \
             patch("agent.runtime.update_agent_task_context") as update, \
             patch("agent.runtime.run_read_workflow") as read, \
             patch("agent.runtime.run_write_workflow") as write:
            response = run_agent("政策", 1, "trace")
        self.assertEqual(response["code"], "INPUT_ANALYSIS_TIMEOUT")
        self.assertEqual(update.call_args.kwargs["task_id"], 42)
        self.assertEqual(update.call_args.kwargs["current_step"], "failed")
        self.assertEqual(update.call_args.kwargs["status"], "failed")
        self.assertTrue(update.call_args.kwargs["execution_id"].startswith("EXEC_"))
        read.assert_not_called(); write.assert_not_called()
        self.assertEqual(FakeStep.records[0][1], "InputAnalysisError")
        self.assertIn("stage=intent category=http_timeout", FakeStep.records[0][2])

    def test_legacy_success_routes_policy(self):
        with patch("agent.runtime.get_active_agent_task", return_value=None), \
             patch("agent.runtime.create_agent_task", return_value={"task_id": 42}), \
             patch("agent.runtime.get_task_context", return_value={}), \
             patch("agent.runtime.trace_step", FakeStep), \
             patch("agent.runtime.update_task_context"), patch("agent.runtime.update_agent_task_context"), \
             patch("agent.runtime.complete_agent_task"), \
             patch("agent.runtime.run_read_workflow", return_value={"code":"POLICY_RESULT_READY"}) as read:
            result = run_agent("政策", 1, "trace")
        self.assertEqual(result["code"], "POLICY_RESULT_READY")
        read.assert_called_once()

    def test_graph_failure_closes_task_without_tool(self):
        self.extract.side_effect = httpx.ReadTimeout("timeout")
        class Graph:
            def invoke(self, state, config):
                from agent.input_analyzer import analyze_state_input
                return analyze_state_input(state)
        with conversation_scope(1, 5), \
             patch("agent.graph.runtime.get_active_agent_task", return_value=None), \
             patch("agent.graph.runtime.create_agent_task", return_value={"task_id": 43}), \
             patch("agent.graph.runtime.get_task_context", return_value={}), \
             patch("agent.graph.runtime.trace_step", FakeStep), \
             patch("agent.graph.runtime.update_agent_task_context") as update, \
             patch("agent.graph.runtime._get_graph", return_value=Graph()):
            result = run_graph_agent("政策", 1, "trace")
        self.assertEqual(result["code"], "INPUT_ANALYSIS_TIMEOUT")
        self.assertEqual(update.call_args.args[:3], (43, "failed", "failed"))
        self.assertTrue(update.call_args.args[3].startswith("EXEC_"))
        self.assertIn("stage=parameters category=http_timeout", FakeStep.records[0][2])

    def test_frontend_request_errors_are_distinct(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "frontend"))
        from api.client import APIError, BackendAPI
        for exception, category in ((httpx.ReadTimeout("late"), "timeout"), (httpx.ConnectError("offline"), "connection")):
            with self.subTest(category=category), patch("api.client.httpx.request", side_effect=exception):
                with self.assertRaises(APIError) as ctx:
                    BackendAPI(base_url="http://example.invalid").chat("hello")
                self.assertEqual(ctx.exception.category, category)


if __name__ == "__main__":
    unittest.main()
