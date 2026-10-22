"""DeepSeek structured-output compatibility; no network requests are made."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.messages import HumanMessage

from agent.intent_router import route_intent
from agent.parameter_extractor import extract_business_params
from llm.client import get_llm
from schemas.agent_schema import BusinessParams, IntentResult


class StructuredOutputCompatibilityTests(unittest.TestCase):
    def test_function_calling_disables_thinking_for_both_schemas(self):
        settings = SimpleNamespace(
            deepseek_model="deepseek-v4-flash",
            deepseek_api_key="test-key",
            deepseek_base_url="https://api.deepseek.com",
        )
        with patch("llm.client.get_settings", return_value=settings):
            for schema in (IntentResult, BusinessParams):
                with self.subTest(schema=schema.__name__):
                    structured = get_llm(disable_thinking=True).with_structured_output(
                        schema, method="function_calling"
                    )
                    payload = structured.first.bound._get_request_payload(
                        [HumanMessage(content="我要退货")], **structured.first.kwargs
                    )
                    self.assertEqual(payload["extra_body"], {"thinking": {"type": "disabled"}})
                    self.assertEqual(payload["tool_choice"]["function"]["name"], schema.__name__)

            default_llm = get_llm()
            self.assertIsNone(default_llm.extra_body)

    def test_analyzer_calls_keep_structured_output_types(self):
        with patch("agent.intent_router.get_llm") as intent_llm:
            intent_llm.return_value.with_structured_output.return_value.invoke.return_value = (
                IntentResult(intent="create_after_sale")
            )
            intent_result = route_intent("我要退货")
            intent_llm.assert_called_once_with(disable_thinking=True, input_analysis=True)
            self.assertEqual(intent_result.intent, "create_after_sale")

        with patch("agent.parameter_extractor.get_llm") as params_llm:
            params_llm.return_value.with_structured_output.return_value.invoke.return_value = (
                BusinessParams(after_sale_type="return_refund")
            )
            params_result = extract_business_params("退货退款")
            params_llm.assert_called_once_with(disable_thinking=True, input_analysis=True)
            self.assertEqual(params_result.after_sale_type, "return_refund")


if __name__ == "__main__":
    unittest.main()
