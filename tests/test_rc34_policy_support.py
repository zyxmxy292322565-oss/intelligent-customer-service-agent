"""Backend contract boundary: dependency and answer generation are mocked."""
import unittest
from unittest.mock import patch
from agent.policy_response import format_policy_result, read_result_failed
from agent.read_workflow import run_read_workflow
from schemas.policy_rag_schema import PolicyRAGResponse
from types import SimpleNamespace

class PolicySupportContractTests(unittest.TestCase):
    def test_unfamiliar_product_policy_still_routes_to_read_only_lookup(self):
        from agent.intent_router import route_intent
        from schemas.agent_schema import IntentResult
        with patch('agent.intent_router.get_llm') as model:
            model.return_value.with_structured_output.return_value.invoke.return_value=IntentResult(intent='query_policy')
            r=route_intent('陌生商品的退款承诺是什么？')
            prompt=model.return_value.with_structured_output.return_value.invoke.call_args.args[0][0].content
        self.assertEqual(r.intent,'query_policy')
        self.assertIn('意图识别不得替代政策存在性核验',prompt)
        self.assertIn('明确要求现在创建售后申请时才选 create_after_sale',prompt)
    def test_refusal_code_preserved(self):
        with patch('agent.policy_response.generate_policy_answer') as llm:
            r=format_policy_result({'success':True,'code':'POLICY_NOT_FOUND','refused':True,'evidence':[]},'unknown policy')
        self.assertEqual(r['code'],'POLICY_NOT_FOUND');self.assertFalse(read_result_failed(r));llm.assert_not_called()
    def test_nonempty_evidence_not_enough_when_assessment_denies(self):
        with patch('agent.policy_response.generate_policy_answer') as llm:
            r=format_policy_result({'success':True,'code':'SUCCESS','evidence':[{'source':'generic.txt','content':'退款流程'}],
                                    'evidence_assessment':{'sufficient_for_answer':False}},'unsupported policy')
        self.assertEqual(r['code'],'POLICY_EVIDENCE_INSUFFICIENT');llm.assert_not_called()
    def test_technical_error_not_refusal(self):
        with patch('agent.policy_response.generate_policy_answer') as llm:
            r=format_policy_result({'success':False,'code':'RAG_TIMEOUT','evidence':[]},'policy')
        self.assertTrue(read_result_failed(r));self.assertEqual(r['code'],'RAG_TIMEOUT');llm.assert_not_called()
    def test_assessment_survives_http_schema(self):
        r=PolicyRAGResponse(success=True,code='POLICY_NOT_FOUND',message='no support',refused=True,
                            evidence_assessment={'sufficient_for_answer':False}).model_dump()
        self.assertIs(r['evidence_assessment']['sufficient_for_answer'],False)
    def test_unknown_assessment_is_conservative(self):
        with patch('agent.policy_response.generate_policy_answer') as llm:
            r=format_policy_result({'success':True,'evidence':[{'content':'text'}],'evidence_assessment':{}},'policy')
        self.assertEqual(r['code'],'POLICY_EVIDENCE_INSUFFICIENT');llm.assert_not_called()
    def test_read_route_refuses_without_answer_llm(self):
        with patch('agent.read_workflow.policy_rag_tool') as tool,patch('agent.policy_response.generate_policy_answer') as llm:
            tool.invoke.return_value={'success':True,'code':'POLICY_NOT_FOUND','refused':True,'evidence':[]}
            r=run_read_workflow('unknown policy',1,SimpleNamespace(intent='query_policy'))
        self.assertEqual(r['code'],'POLICY_NOT_FOUND');llm.assert_not_called()
    def test_compiled_graph_refusal_cannot_build_or_execute_preview(self):
        from agent.graph.runtime import run_graph_agent
        with patch('agent.graph.nodes.analyze_state_input',return_value={'intent':'query_policy','params':{}}), \
             patch.dict('agent.graph.nodes.READ_TOOL_REGISTRY',{'policy_rag_tool':lambda **kw:{'success':True,'code':'POLICY_NOT_FOUND','refused':True,'evidence':[]}}), \
             patch('agent.graph.nodes.build_create_after_sale_pending_action') as preview, \
             patch('agent.graph.nodes.confirm_action') as confirm, \
             patch('agent.graph.nodes.execute_confirmed_action') as execute, \
             patch('agent.policy_response.generate_policy_answer') as llm:
            result=run_graph_agent('无依据退款承诺是什么？',1,'TRACE_SUPPORT_REFUSAL')
        self.assertEqual(result['code'],'POLICY_NOT_FOUND')
        for call in [preview,confirm,execute,llm]:call.assert_not_called()

if __name__ == '__main__': unittest.main()
