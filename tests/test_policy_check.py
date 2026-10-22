"""Pure-Python/Mock tests: no database, Docker, FAISS or LLM writes."""

import hashlib
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

from services.policy_check_service import _reason_kind, check_after_sale_policy
from services.policy_demo_rules import DEMO_POLICY_RULES, DEMO_POLICY_SOURCES


TZ = timezone(timedelta(hours=8), "Asia/Shanghai")
CURRENT = datetime(2026, 10, 15, 12, 0, tzinfo=TZ)


def evidence_for(*names):
    return {
        "success": True, "refused": False,
        "evidence": [
            {
                "source": name,
                "file_hash": DEMO_POLICY_SOURCES[name].file_hash,
                "version": DEMO_POLICY_SOURCES[name].rag_metadata_version,
                "content": "从订单签收日起算；退货7天、质量问题30天，仍需核验。",
            }
            for name in names
        ],
        "answer": "LLM 无权决定申请资格",
    }


class PolicyCheckTests(unittest.TestCase):
    def check(self, days, *, category="耳机", kind="quality", sale_type="return_refund",
              condition=False, delivered=None, now=CURRENT, rag=None, reason=None,
              rules=DEMO_POLICY_RULES, order_user=1, item_order=10, status="delivered"):
        if rag is None:
            source = "耳机类商品规则.txt" if category == "耳机" else "电子产品售后规则.txt"
            if sale_type == "repair":
                source = "保修政策.txt"
            rag = evidence_for(source)
        mock = Mock(return_value=rag)
        result = check_after_sale_policy(
            user_id=1,
            order={"id": 10, "user_id": order_user, "status": status,
                   "delivered_at": delivered if delivered is not None else
                   (CURRENT.replace(day=CURRENT.day - days) if days <= 14 else
                    datetime.fromtimestamp(CURRENT.timestamp() - days * 86400, TZ))},
            order_item={"id": 20, "order_id": item_order, "product_name": "AirPods Pro 2",
                        "category": category},
            after_sale_type=sale_type,
            reason=reason or ("质量不好" if kind == "quality" else "不喜欢了"),
            item_condition_verified=condition,
            now=now,
            rag_query=mock,
            rules=rules,
        )
        return result, mock

    def test_day_five_no_reason_with_verified_condition_can_apply(self):
        result, _ = self.check(5, kind="no_reason", condition=True)
        self.assertEqual(result["decision"], "ELIGIBLE_TO_APPLY")
        self.assertTrue(result["allowed_to_apply"])
        self.assertTrue(result["requires_review"])

    def test_concrete_fault_descriptions_are_classified_as_quality(self):
        reasons = (
            "耳机左边没有声音", "耳机没声音", "左边耳机不出声",
            "商品质量不好", "商品有质量问题", "买回来无法开机",
            "鼠标左键失灵", "耳机声音断断续续", "耳机一边不出声",
        )
        for reason in reasons:
            with self.subTest(reason=reason):
                self.assertEqual(_reason_kind(reason), "quality")

    def test_explicit_quality_denials_are_not_quality_reasons(self):
        reasons = (
            "没有质量问题", "没有任何质量问题", "商品没什么质量问题",
            "不是质量问题", "并不是因为质量问题", "没发现质量问题",
        )
        for reason in reasons:
            with self.subTest(reason=reason):
                self.assertIsNone(_reason_kind(reason))

    def test_quality_denial_with_no_reason_language_uses_no_reason_path(self):
        for reason in ("没有质量问题，就是不喜欢了", "商品正常，只是不想要了"):
            with self.subTest(reason=reason):
                self.assertEqual(_reason_kind(reason), "no_reason")

    def test_conflicting_or_uncertain_quality_language_requires_review(self):
        for reason in ("没有质量问题，但是左耳没声音", "不确定是不是质量问题", "感觉可能有问题"):
            with self.subTest(reason=reason):
                result, rag = self.check(20, reason=reason)
                self.assertEqual(result["decision"], "NEEDS_REVIEW")
                rag.assert_not_called()

    def test_day_twenty_quality_denial_cannot_use_thirty_day_quality_rule(self):
        result, rag = self.check(20, reason="没有质量问题，只是不喜欢了", condition=True)
        self.assertEqual(result["decision"], "INELIGIBLE")
        self.assertEqual(result["policy_type"], "earphone_no_reason")
        self.assertEqual(result["policy_deadline_days"], 7)
        rag.assert_called_once()

    def test_day_twenty_concrete_no_sound_symptom_uses_quality_application_rule(self):
        result, _ = self.check(20, reason="左耳没有声音")
        self.assertEqual(result["decision"], "ELIGIBLE_TO_APPLY")
        self.assertEqual(result["policy_type"], "earphone_quality_return")
        self.assertEqual(result["policy_deadline_days"], 30)
        self.assertTrue(result["requires_review"])

    def test_fault_words_do_not_bypass_category_or_evidence_gates(self):
        mouse, rag = self.check(5, category="电脑配件", reason="无线鼠标左键失灵")
        self.assertEqual(mouse["decision"], "NEEDS_REVIEW")
        rag.assert_not_called()
        insufficient, _ = self.check(5, reason="耳机没有声音", rag={"success": True, "evidence": []})
        self.assertEqual(insufficient["decision"], "POLICY_UNAVAILABLE")

    def test_day_five_unknown_item_condition_needs_review(self):
        result, _ = self.check(5, kind="no_reason")
        self.assertEqual(result["decision"], "NEEDS_REVIEW")

    def test_day_seven_boundary_is_not_invented(self):
        result, _ = self.check(7, kind="no_reason", condition=True)
        self.assertEqual(result["decision"], "NEEDS_REVIEW")

    def test_day_eight_and_ten_no_reason_are_out_of_standard_path(self):
        for day in (8, 10):
            with self.subTest(day=day):
                result, _ = self.check(day, kind="no_reason", condition=True)
                self.assertEqual(result["decision"], "INELIGIBLE")
                self.assertFalse(result["allowed_to_apply"])

    def test_day_twenty_quality_can_apply_but_not_get_refund_approval(self):
        result, _ = self.check(20)
        self.assertEqual(result["decision"], "ELIGIBLE_TO_APPLY")
        self.assertTrue(result["requires_review"])
        self.assertIn("审核", result["policy_message"])
        self.assertEqual(result["policy_deadline_days"], 30)

    def test_day_thirty_boundary_is_unconfirmed(self):
        result, _ = self.check(30)
        self.assertEqual(result["decision"], "NEEDS_REVIEW")

    def test_day_thirty_one_quality_needs_other_rights_review(self):
        result, _ = self.check(31)
        self.assertEqual(result["decision"], "NEEDS_REVIEW")

    def test_human_damage_not_assumed_manufacturing_defect(self):
        result, mock = self.check(5, reason="我把耳机摔坏了，质量不好")
        self.assertEqual(result["decision"], "NEEDS_REVIEW")
        mock.assert_not_called()

    def test_repair_has_no_invented_warranty_duration(self):
        result, _ = self.check(31, sale_type="repair")
        self.assertEqual(result["decision"], "NEEDS_REVIEW")
        self.assertIsNone(result["policy_deadline_days"])

    def test_missing_or_malformed_delivery_does_not_use_order_creation_date(self):
        for delivered in (None, "not-a-date"):
            with self.subTest(delivered=delivered):
                order = {"id": 10, "user_id": 1, "status": "delivered",
                         "delivered_at": delivered, "created_at": CURRENT}
                result = check_after_sale_policy(
                    user_id=1, order=order,
                    order_item={"id": 20, "order_id": 10, "category": "耳机"},
                    after_sale_type="return_refund", reason="质量不好",
                    now=CURRENT, rag_query=Mock(side_effect=AssertionError("must not query")),
                )
                self.assertEqual(result["decision"], "NEEDS_REVIEW")
                self.assertIsNone(result["days_since_delivery"])

    def test_future_delivery_needs_review(self):
        result, mock = self.check(0, delivered=datetime(2026, 10, 16, 12, tzinfo=TZ))
        self.assertEqual(result["decision"], "NEEDS_REVIEW")
        mock.assert_not_called()

    def test_unknown_category_and_wireless_mouse_need_review(self):
        for category in ("电脑配件", "智能手表"):
            with self.subTest(category=category):
                result, mock = self.check(5, category=category)
                self.assertEqual(result["decision"], "NEEDS_REVIEW")
                self.assertIn("人工客服", result["policy_message"])
                mock.assert_not_called()

    def test_no_evidence_or_refusal_is_unavailable(self):
        for rag in ({"success": True, "evidence": []},
                    {"success": True, "refused": True, "evidence": []}):
            with self.subTest(rag=rag):
                result, _ = self.check(20, rag=rag)
                self.assertEqual(result["decision"], "POLICY_UNAVAILABLE")

    def test_rag_service_exception_or_failure_is_unavailable(self):
        result = check_after_sale_policy(
            user_id=1,
            order={"id": 10, "user_id": 1, "status": "delivered",
                   "delivered_at": datetime(2026, 10, 1, 12)},
            order_item={"order_id": 10, "category": "耳机"},
            after_sale_type="return_refund", reason="质量不好", now=CURRENT,
            rag_query=Mock(side_effect=OSError("unavailable")),
        )
        self.assertEqual(result["decision"], "POLICY_UNAVAILABLE")
        result, _ = self.check(20, rag={"success": False, "evidence": []})
        self.assertEqual(result["decision"], "POLICY_UNAVAILABLE")

    def test_conflicting_rule_configuration_needs_review(self):
        original = next(rule for rule in DEMO_POLICY_RULES if rule.rule_id == "earphone_quality_return")
        conflict = replace(original, rule_id="conflict", deadline_days=15)
        result, mock = self.check(20, rules=(*DEMO_POLICY_RULES, conflict))
        self.assertEqual(result["decision"], "NEEDS_REVIEW")
        mock.assert_not_called()

    def test_unreviewed_evidence_source_needs_review(self):
        rag = evidence_for("耳机类商品规则.txt")
        rag["evidence"].append({"source": "unknown.txt", "content": "未知规则"})
        result, _ = self.check(20, rag=rag)
        self.assertEqual(result["decision"], "NEEDS_REVIEW")

    def test_source_version_and_file_hash_mismatch_needs_review(self):
        for field, value in (("version", "v2"), ("file_hash", "changed")):
            with self.subTest(field=field, value=value):
                rag = evidence_for("耳机类商品规则.txt")
                rag["evidence"][0][field] = value
                result, _ = self.check(20, rag=rag)
                self.assertEqual(result["decision"], "NEEDS_REVIEW")

    def test_missing_rag_version_uses_exact_pinned_file_hash(self):
        rag = evidence_for("耳机类商品规则.txt")
        rag["evidence"][0]["version"] = None
        result, _ = self.check(20, rag=rag)
        self.assertEqual(result["decision"], "ELIGIBLE_TO_APPLY")
        self.assertEqual(result["policy_versions"]["耳机类商品规则.txt"], "演示版 v1.0")

    def test_different_categories_use_distinct_sources_and_deadlines(self):
        earphone, _ = self.check(20)
        phone, _ = self.check(20, category="手机")
        self.assertEqual(earphone["decision"], "ELIGIBLE_TO_APPLY")
        self.assertEqual(phone["decision"], "ELIGIBLE_TO_APPLY")
        self.assertEqual(earphone["sources"], ["耳机类商品规则.txt"])
        self.assertEqual(phone["sources"], ["电子产品售后规则.txt"])
        wrong, _ = self.check(20, category="手机", rag=evidence_for("耳机类商品规则.txt"))
        self.assertEqual(wrong["decision"], "POLICY_UNAVAILABLE")

    def test_electronics_exchange_requires_both_category_and_exchange_sources(self):
        complete = evidence_for("电子产品售后规则.txt", "换货流程.txt")
        result, _ = self.check(20, category="手机", sale_type="exchange", rag=complete)
        self.assertEqual(result["decision"], "ELIGIBLE_TO_APPLY")
        incomplete, _ = self.check(20, category="手机", sale_type="exchange",
                                   rag=evidence_for("换货流程.txt"))
        self.assertEqual(incomplete["decision"], "POLICY_UNAVAILABLE")

    def test_ownership_or_item_relation_is_not_taken_from_client(self):
        for overrides in ({"order_user": 2}, {"item_order": 99}):
            result, mock = self.check(20, **overrides)
            self.assertEqual(result["decision"], "NEEDS_REVIEW")
            mock.assert_not_called()

    def test_cross_month_cross_year_and_timezone(self):
        winter_now = datetime(2027, 1, 2, 12, tzinfo=TZ)
        prior_year = datetime(2026, 12, 20, 11, tzinfo=TZ)
        result, _ = self.check(0, now=winter_now, delivered=prior_year)
        self.assertEqual(result["days_since_delivery"], 13)
        self.assertEqual(result["decision"], "ELIGIBLE_TO_APPLY")
        utc_delivery = datetime(2026, 10, 1, 4, tzinfo=timezone.utc)
        result, _ = self.check(0, delivered=utc_delivery)
        self.assertEqual(result["days_since_delivery"], 14)

    def test_untrusted_llm_answer_does_not_override_rule(self):
        rag = evidence_for("耳机类商品规则.txt")
        rag["answer"] = "LLM 说 10 天无理由退货一定批准"
        result, _ = self.check(10, kind="no_reason", condition=True, rag=rag)
        self.assertEqual(result["decision"], "INELIGIBLE")

    def test_pinned_manifest_matches_local_original_documents_when_present(self):
        docs = Path(__file__).resolve().parents[2] / "demo3_rag_qa" / "policy_docs"
        if not docs.is_dir():
            self.skipTest("RAG source documents are not mounted in this test environment")
        for name, source in DEMO_POLICY_SOURCES.items():
            with self.subTest(name=name):
                self.assertEqual(hashlib.md5((docs / name).read_bytes()).hexdigest(), source.file_hash)


if __name__ == "__main__":
    unittest.main()
