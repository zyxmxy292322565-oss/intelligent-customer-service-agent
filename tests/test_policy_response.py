"""Policy answer generation stays grounded in retrieved evidence."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent.policy_response import format_policy_result
from agent.policy_response_generator import generate_policy_answer


EVIDENCE = [
    {"source": "耳机类商品规则.txt", "content": "AirPods 无理由退货 7 天；质量问题 30 天内可申请检测。"},
    {"source": "耳机类商品规则.txt", "content": "是否退款须核对订单和检测结果。"},
]


class PolicyResponseTests(unittest.TestCase):
    def test_generator_receives_question_and_evidence_without_tools(self):
        with patch("agent.policy_response_generator.get_llm") as get_llm:
            get_llm.return_value.invoke.return_value = SimpleNamespace(
                content="20天已超过无理由退货期；如有质量问题，可申请检测。依据：耳机类商品规则。"
            )
            answer = generate_policy_answer("AirPods用了20天还能退吗？", EVIDENCE)

        self.assertIn("质量问题", answer)
        get_llm.assert_called_once_with(disable_thinking=True)
        messages = get_llm.return_value.invoke.call_args.args[0]
        self.assertIn("AirPods用了20天还能退吗？", messages[1].content)
        self.assertIn("耳机类商品规则.txt", messages[1].content)
        self.assertIn("不得判定具体订单一定可以退款", messages[0].content)

    def test_success_contains_answer_evidence_and_unique_sources(self):
        with patch("agent.policy_response.generate_policy_answer", return_value="20天不能无理由退货；质量问题可申请检测。"):
            result = format_policy_result(
                {"success": True, "code": "SUCCESS", "evidence": EVIDENCE},
                "AirPods用了20天还能退吗？",
            )
        self.assertEqual(result["code"], "POLICY_RESULT_READY")
        self.assertEqual(result["data"]["answer"], result["message"])
        self.assertEqual(result["data"]["evidence"], EVIDENCE)
        self.assertEqual(result["data"]["sources"], ["耳机类商品规则.txt"])
        self.assertNotIn("confirmation_id", result["data"])

    def test_unrelated_question_refused_without_llm(self):
        with patch("agent.policy_response.generate_policy_answer") as generator:
            result = format_policy_result(
                {"success": True, "refused": True, "evidence": []},
                "今天天气怎么样？",
            )
        self.assertEqual(result["code"], "POLICY_EVIDENCE_INSUFFICIENT")
        self.assertIsNone(result["data"]["answer"])
        generator.assert_not_called()

    def test_llm_error_does_not_claim_policy_answer(self):
        with patch("agent.policy_response.generate_policy_answer", side_effect=RuntimeError("LLM unavailable")):
            result = format_policy_result(
                {"success": True, "evidence": EVIDENCE},
                "AirPods用了20天还能退吗？",
            )
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "POLICY_ANSWER_GENERATION_FAILED")
        self.assertIsNone(result["data"]["answer"])
        self.assertEqual(result["data"]["sources"], ["耳机类商品规则.txt"])


if __name__ == "__main__":
    unittest.main()
