"""Presentation-only policy result tests; no Streamlit server or backend."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "frontend"))
from components.result import render_result  # noqa: E402


class PolicyFrontendTests(unittest.TestCase):
    @patch("components.result.st")
    def test_eligible_preview_is_natural_language_and_evidence_collapsed(self, st):
        render_result({
            "success": True, "code": "ACTION_PREVIEW_READY",
            "data": {"confirmation_id": "CONF_20", "preview": {
                "product_name": "AirPods Pro 2", "order_no": "ORD_20",
                "after_sale_type": "return_refund", "reason": "单侧无声",
                "policy": {
                    "decision": "ELIGIBLE_TO_APPLY", "days_since_delivery": 20,
                    "policy_message": "符合当前已知的质量问题售后申请期限，仍需审核。",
                    "requires_review": True,
                    "evidence": [{"source": "耳机类商品规则.txt", "version": "演示版 v1.0",
                                  "content": "质量问题30天内可以提出申请。"}],
                },
            }},
        })
        shown = [call.args[0] for call in st.write.call_args_list]
        self.assertIn("商品：AirPods Pro 2", shown)
        self.assertIn("订单：ORD_20", shown)
        self.assertIn("签收情况：已签收20天", shown)
        self.assertTrue(any("仍需审核" in value for value in shown))
        self.assertFalse(any("ELIGIBLE_TO_APPLY" in value for value in shown))
        st.expander.assert_called_once_with("查看政策依据")
        st.json.assert_not_called()

    @patch("components.result.st")
    def test_needs_review_and_unavailable_show_friendly_messages(self, st):
        render_result({"success": False, "code": "NEEDS_REVIEW", "message": "internal"})
        self.assertIn("转人工客服", st.warning.call_args.args[0])
        st.reset_mock()
        render_result({"success": False, "code": "POLICY_UNAVAILABLE", "message": "timeout"})
        self.assertIn("稍后重试", st.warning.call_args.args[0])
        self.assertNotIn("timeout", st.warning.call_args.args[0])
        st.json.assert_not_called()

    @patch("components.result.st")
    def test_ineligible_explains_only_the_specific_path(self, st):
        render_result({"success": False, "code": "INELIGIBLE",
                       "message": "已超出无理由退货路径，其他政策可咨询人工客服。"})
        self.assertIn("无理由退货路径", st.warning.call_args.args[0])
        self.assertIn("其他政策", st.warning.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
