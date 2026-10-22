"""Policy gate integration tests without real MySQL, RAG, or confirmations."""

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

from agent.context_resolver import resolve_order_item_by_product, resolve_state_context
from agent.graph.nodes import build_preview_node, wait_confirmation_node
from agent.step_trace import trace_step as real_trace_step
from agent.write_workflow import run_write_workflow
from schemas.agent_schema import CreateAfterSalePreview
from services.policy_demo_rules import DEMO_POLICY_SOURCES
from services.policy_rag_client import query_policy_rag


TZ = timezone(timedelta(hours=8))
DELIVERED = (datetime.now(TZ) - timedelta(days=20)).replace(tzinfo=None)
SOURCE = DEMO_POLICY_SOURCES["耳机类商品规则.txt"]


def rag_result(*, source=SOURCE, refused=False):
    return {
        "success": True, "code": "RAG_OK", "message": "找到政策",
        "refused": refused,
        "evidence": [] if refused else [{
            "source": source.filename, "file_hash": source.file_hash,
            # The real endpoint may omit the version due to a metadata-key mismatch.
            "version": None, "content": "耳机签收后30天内可提出质量问题售后申请，需检测审核。",
        }],
    }


def order_rows(*, category="耳机", delivered=DELIVERED, user_id=1):
    return {
        "user": {"id": user_id}, "orders": [{
            "id": 10, "user_id": user_id, "order_no": "ORD_10", "status": "delivered",
            "created_at": datetime(2026, 9, 1), "paid_at": datetime(2026, 9, 1),
            "delivered_at": delivered,
            "items": [{"id": 20, "order_id": 10, "product_name": "AirPods Pro 2",
                       "category": category, "quantity": 1, "unit_price": 1899}],
        }],
    }


class RecordingStep:
    def __init__(self, name, records):
        self.name = name
        self.records = records

    def __enter__(self):
        self.records.append([self.name, None])
        return self

    def set_result(self, result):
        self.records[-1][1] = result

    def __exit__(self, *_args):
        return False


class PolicyWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.records = []
        self.pending = Mock(return_value={"confirmation_id": "CONF_20", "status": "pending"})
        for target, kwargs in (
            ("agent.context_resolver.get_user_orders_service", {"return_value": order_rows()}),
            ("agent.write_workflow.trace_step", {"side_effect":
                lambda _task, _trace, name, _input: RecordingStep(name, self.records)}),
            ("agent.write_workflow.create_pending_action", {"side_effect": self.pending}),
            ("agent.write_workflow.active_after_sale_for_preview", {"return_value": None}),
            ("agent.write_workflow.save_current_confirmation", {}),
            ("services.policy_rag_client.query_policy_rag", {"return_value": rag_result()}),
        ):
            active = patch(target, **kwargs)
            active.start()
            self.addCleanup(active.stop)

    @staticmethod
    def analysis(*, product="AirPods Pro", sale_type="return_refund", reason="单侧无声，质量不好"):
        return SimpleNamespace(intent="create_after_sale", params=SimpleNamespace(
            product_name=product, after_sale_type=sale_type, reason=reason,
        ))

    def run_workflow(self, analysis=None):
        return run_write_workflow("我要退AirPods Pro", 1, analysis or self.analysis(),
                                  trace_id="trace-policy", task_id=42)

    def test_missing_parameters_do_not_resolve_order_or_check_policy(self):
        with patch("agent.write_workflow.resolve_order_item_by_product") as resolve, \
             patch("agent.write_workflow.check_after_sale_policy") as check:
            result = self.run_workflow(self.analysis(sale_type=None))
        self.assertEqual(result["code"], "AFTER_SALE_TYPE_REQUIRED")
        resolve.assert_not_called()
        check.assert_not_called()
        self.pending.assert_not_called()

    def test_resolver_returns_database_category_dates_and_ownership(self):
        result = resolve_order_item_by_product(1, "AirPods Pro")
        self.assertTrue(result["success"])
        data = result["data"]
        self.assertEqual(data["user_id"], 1)
        self.assertEqual(data["order_id"], 10)
        self.assertEqual(data["order_item_id"], 20)
        self.assertEqual(data["category"], "耳机")
        self.assertEqual(data["delivered_at"], DELIVERED)
        self.assertEqual(data["created_at"], datetime(2026, 9, 1))
        self.assertEqual(data["paid_at"], datetime(2026, 9, 1))
        state = {"params": {"product_name": "AirPods Pro"},
                 "current_user_id": 1, "trace_id": "trace-policy"}
        self.assertEqual(resolve_state_context(state)["resolved_context"]["category"], "耳机")

    def test_earphone_quality_eligible_builds_preview_and_pending_after_policy(self):
        result = self.run_workflow()
        self.assertEqual(result["code"], "ACTION_PREVIEW_READY")
        preview = result["data"]["preview"]
        self.assertEqual(preview["order_no"], "ORD_10")
        self.assertEqual(preview["policy"]["decision"], "ELIGIBLE_TO_APPLY")
        self.assertEqual(preview["policy"]["days_since_delivery"], 20)
        self.assertTrue(preview["policy"]["requires_review"])
        self.assertEqual(preview["policy"]["evidence"][0]["source"], SOURCE.filename)
        self.assertEqual(preview["policy"]["policy_file_hashes"][SOURCE.filename], SOURCE.file_hash)
        snapshot = preview["policy_snapshot"]
        self.assertEqual(snapshot["policy_decision"], "ELIGIBLE_TO_APPLY")
        self.assertEqual(snapshot["rule_id"], "earphone_quality_return")
        self.assertEqual(snapshot["order_id"], 10)
        self.assertEqual(snapshot["order_item_id"], 20)
        self.assertEqual(snapshot["after_sale_type"], "return_refund")
        self.assertEqual(snapshot["delivered_at"], DELIVERED.isoformat())
        self.assertEqual(snapshot["policy_file_hashes"][SOURCE.filename], SOURCE.file_hash)
        self.pending.assert_called_once()
        self.assertEqual(self.pending.call_args.kwargs["preview"], preview)
        self.assertEqual([record[0] for record in self.records],
                         ["resolve_order", "policy_check", "build_preview", "create_pending_action"])
        traced = self.records[1][1]
        self.assertEqual(traced["decision"], "ELIGIBLE_TO_APPLY")
        self.assertEqual(traced["policy_sources"], [SOURCE.filename])
        self.assertNotIn("reason", traced)
        self.assertNotIn("evidence", traced)

    def test_conflicting_raw_reason_blocks_preview_in_both_paths(self):
        raw = "我的AirPods Pro 2没有质量问题，但是左边没声音，我想申请退货退款"
        analysis = self.analysis(reason="左边没声音")
        analysis.params.original_reason_text = raw
        legacy = self.run_workflow(analysis)
        self.assertEqual(legacy["code"], "NEEDS_REVIEW")
        context = resolve_order_item_by_product(1, "AirPods Pro")["data"]
        graph = build_preview_node({"current_user_id": 1, "user_text": raw,
            "params": {"after_sale_type": "return_refund", "reason": "左边没声音",
                       "original_reason_text": raw},
            "resolved_context": context, "trace_id": "trace-conflict", "error": None})
        self.assertEqual(graph["error"]["code"], "NEEDS_REVIEW")
        self.pending.assert_not_called()

    def test_active_sale_blocks_preview_before_policy(self):
        with patch("agent.write_workflow.active_after_sale_for_preview",
                   return_value={"id": 9, "after_sale_no": "AS_EXIST", "status": "pending"}), \
             patch("agent.write_workflow.check_after_sale_policy") as policy:
            result = self.run_workflow()
        self.assertEqual(result["code"], "AFTER_SALE_ALREADY_EXISTS")
        self.assertEqual(result["data"]["after_sale_no"], "AS_EXIST")
        policy.assert_not_called()
        self.pending.assert_not_called()

    def test_foreign_resolved_order_cannot_build_preview(self):
        with patch("agent.write_workflow.resolve_order_item_by_product", return_value={
            "success": True, "data": {**resolve_order_item_by_product(1, "AirPods Pro")["data"], "user_id": 2}
        }), patch("agent.write_workflow.active_after_sale_for_preview") as lookup:
            result = self.run_workflow()
        self.assertEqual(result["code"], "ORDER_NOT_FOUND")
        lookup.assert_not_called()

    def test_four_decisions_gate_pending_action(self):
        for decision in ("INELIGIBLE", "NEEDS_REVIEW", "POLICY_UNAVAILABLE"):
            with self.subTest(decision=decision), \
                 patch("agent.write_workflow.check_after_sale_policy", return_value={
                     "decision": decision, "policy_message": "政策需要核对", "sources": [],
                 }):
                self.pending.reset_mock()
                self.records.clear()
                result = self.run_workflow()
                self.assertEqual(result["code"], decision)
                self.assertFalse(result["success"])
                self.assertNotIn("confirmation_id", result.get("data") or {})
                self.assertEqual([row[0] for row in self.records], ["resolve_order", "policy_check"])
                self.pending.assert_not_called()

    def test_wireless_mouse_and_missing_delivery_do_not_create_pending(self):
        with patch("agent.context_resolver.get_user_orders_service",
                   return_value=order_rows(category="电脑配件")):
            result = self.run_workflow()
        self.assertEqual(result["code"], "NEEDS_REVIEW")
        self.pending.assert_not_called()
        with patch("agent.context_resolver.get_user_orders_service",
                   return_value=order_rows(delivered=None)):
            result = self.run_workflow()
        self.assertEqual(result["code"], "NEEDS_REVIEW")
        self.pending.assert_not_called()

    def test_no_reason_unverified_condition_and_wrong_owner_do_not_pass(self):
        recent_delivery = (datetime.now(TZ) - timedelta(days=5)).replace(tzinfo=None)
        with patch("agent.context_resolver.get_user_orders_service",
                   return_value=order_rows(delivered=recent_delivery)):
            result = self.run_workflow(self.analysis(reason="不喜欢了"))
        self.assertEqual(result["code"], "NEEDS_REVIEW")
        self.pending.assert_not_called()
        with patch("agent.context_resolver.get_user_orders_service",
                   return_value=None):
            result = self.run_workflow()
        self.assertEqual(result["code"], "USER_NOT_FOUND")
        self.pending.assert_not_called()

    def test_negated_quality_uses_no_reason_gate_in_legacy_and_graph(self):
        analysis = self.analysis(reason="没有质量问题，就是不喜欢了")
        legacy = self.run_workflow(analysis)
        self.assertEqual(legacy["code"], "INELIGIBLE")
        self.pending.assert_not_called()

        context = resolve_order_item_by_product(1, "AirPods Pro")["data"]
        graph = build_preview_node({
            "current_user_id": 1,
            "params": {"after_sale_type": "return_refund", "reason": analysis.params.reason},
            "resolved_context": context,
            "trace_id": "trace-negated-quality", "error": None,
        })
        self.assertEqual(graph["error"]["code"], "INELIGIBLE")
        self.assertNotIn("confirmation_id", graph)
        self.pending.assert_not_called()

    def test_graph_build_node_uses_same_gate_and_does_not_confirm(self):
        context = resolve_order_item_by_product(1, "AirPods Pro")["data"]
        state = {
            "current_user_id": 1,
            "params": {"after_sale_type": "return_refund", "reason": "质量不好"},
            "resolved_context": context,
            "trace_id": "trace-graph", "error": None,
        }
        with patch("agent.graph.nodes.confirm_action") as confirm:
            result = build_preview_node(state)
        self.assertEqual(result["confirmation_id"], "CONF_20")
        self.assertEqual(result["action_preview"]["policy"]["decision"], "ELIGIBLE_TO_APPLY")
        confirm.assert_not_called()
        self.pending.assert_called_once()
        self.pending.reset_mock()
        with patch("agent.write_workflow.check_after_sale_policy", return_value={
            "decision": "NEEDS_REVIEW", "policy_message": "待核实", "sources": [],
        }):
            rejected = build_preview_node(state)
        self.assertEqual(rejected["error"]["code"], "NEEDS_REVIEW")
        self.assertNotIn("confirmation_id", rejected)
        self.pending.assert_not_called()
        self.assertEqual(wait_confirmation_node({"error": rejected["error"],
                                                "confirmation_id": None}), {})

    def test_incomplete_db_context_cannot_call_pending_even_if_policy_is_mocked(self):
        with patch("agent.write_workflow.check_after_sale_policy") as check:
            result = build_preview_node({
                "current_user_id": 1,
                "params": {"after_sale_type": "return_refund", "reason": "质量不好"},
                "resolved_context": {"order_no": "ORD_10", "order_item_id": 20,
                                     "product_name": "AirPods Pro 2"},
                "trace_id": "trace-graph", "error": None,
            })
        self.assertEqual(result["error"]["code"], "POLICY_CONTEXT_INCOMPLETE")
        check.assert_not_called()
        self.pending.assert_not_called()

    def test_real_trace_step_records_decision_status_and_latency_without_database(self):
        finished = []
        with patch("agent.write_workflow.trace_step", real_trace_step), \
             patch("agent.step_trace._start_step", side_effect=iter(range(1, 10))), \
             patch("agent.step_trace._finish_step", side_effect=lambda *args: finished.append(args)), \
             patch("agent.write_workflow.check_after_sale_policy", return_value={
                 "decision": "NEEDS_REVIEW", "policy_message": "待人工核实", "sources": [],
             }):
            result = self.run_workflow()
        self.assertEqual(result["code"], "NEEDS_REVIEW")
        # resolve_order is step 1; policy_check is step 2. An intentional
        # review decision is not an execution error.
        self.assertEqual(len(finished), 2)
        self.assertEqual(finished[1][1], "SUCCESS")
        self.assertEqual(finished[1][2]["decision"], "NEEDS_REVIEW")
        self.assertIsInstance(finished[1][4], float)
        self.pending.assert_not_called()


class PolicyRAGContractTests(unittest.TestCase):
    def test_existing_post_contract_retains_source_hash_and_optional_version(self):
        http_response = httpx.Response(
            200, json=rag_result(),
            request=httpx.Request("POST", "http://example/rag/query"),
        )
        with patch("services.policy_rag_client.httpx.post", return_value=http_response) as post:
            response = query_policy_rag("耳机质量问题申请期限是什么？", top_k=5)
        self.assertTrue(response.success)
        self.assertEqual(response.evidence[0].source, SOURCE.filename)
        self.assertEqual(response.evidence[0].file_hash, SOURCE.file_hash)
        self.assertIsNone(response.evidence[0].version)
        self.assertTrue(post.call_args.args[0].endswith("/rag/query"))
        self.assertEqual(post.call_args.kwargs["json"]["top_k"], 5)

    def test_rag_refusal_and_timeout_are_not_approval(self):
        with patch("services.policy_rag_client.httpx.post", return_value=httpx.Response(
            200, json=rag_result(refused=True),
            request=httpx.Request("POST", "http://example/rag/query"),
        )):
            self.assertTrue(query_policy_rag("无关问题").refused)
        with patch("services.policy_rag_client.httpx.post", side_effect=httpx.TimeoutException("timeout")):
            response = query_policy_rag("耳机质量问题")
        self.assertFalse(response.success)
        self.assertEqual(response.code, "RAG_TIMEOUT")


if __name__ == "__main__":
    unittest.main()
