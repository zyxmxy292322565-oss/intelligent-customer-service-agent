"""Read-result contract tests. LLM/workflow mocks, no external I/O."""
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch
from agent.runtime import run_agent
from agent.graph.runtime import run_graph_agent
from agent.conversation_scope import conversation_scope
from agent.policy_response import read_result_failed

class Step:
    def __enter__(self): return self
    def set_result(self, value): self.result=value
    def __exit__(self,*args): return False

class ReadOutcomeTests(unittest.TestCase):
    def check(self, code, success, expected, graph=False, exception=False):
        module='agent.graph.runtime' if graph else 'agent.runtime'
        result={'success':success,'code':code,'message':'test','data':None}
        with ExitStack() as stack:
            for name,value in [('get_active_agent_task',None),('new_agent_execution_id','EXEC_TEST'),('create_agent_task',{'task_id':1}),('get_task_context',{}),('update_task_context',None),('trace_step',Step())]:
                stack.enter_context(patch(module+'.'+name,return_value=value))
            status=stack.enter_context(patch(module+'.update_agent_task_context'))
            complete=stack.enter_context(patch('agent.runtime.complete_agent_task'))
            write=stack.enter_context(patch('agent.runtime.run_write_workflow'))
            stack.enter_context(patch('agent.runtime.analyze_agent_input',return_value=SimpleNamespace(intent='query_policy',params=SimpleNamespace())))
            target=module+'._run_graph_agent_core' if graph else module+'.run_read_workflow'
            stack.enter_context(patch(target,**({'side_effect':RuntimeError('upstream')} if exception else {'return_value':result})))
            with conversation_scope(1,1):
                if exception:
                    with self.assertRaises(RuntimeError):run_agent('policy',1,'trace')
                else:
                    self.assertEqual((run_graph_agent if graph else run_agent)('policy',1,'trace'),result)
            write.assert_not_called()
            if expected=='completed' and not graph:complete.assert_called_once_with(1,'EXEC_TEST')
            else:
                call=status.call_args
                actual=call.kwargs.get('status') if call.kwargs else call.args[2]
                owner=call.kwargs.get('execution_id') if call.kwargs else call.args[3]
                self.assertEqual(actual,expected);self.assertEqual(owner,'EXEC_TEST')
                complete.assert_not_called()
    def test_success(self):self.check('POLICY_RESULT_READY',True,'completed')
    def test_unavailable(self):self.check('RAG_SERVICE_UNAVAILABLE',False,'failed')
    def test_timeout(self):self.check('RAG_TIMEOUT',False,'failed')
    def test_invalid(self):self.check('RAG_RESPONSE_INVALID',False,'failed')
    def test_answer_failure(self):self.check('POLICY_ANSWER_GENERATION_FAILED',False,'failed')
    def test_unknown_failure(self):self.check('UNKNOWN_ERROR',False,'failed')
    def test_not_found(self):self.check('POLICY_NOT_FOUND',False,'completed')
    def test_insufficient(self):self.check('POLICY_EVIDENCE_INSUFFICIENT',False,'completed')
    def test_review(self):self.check('NEEDS_REVIEW',False,'completed')
    def test_exception(self):self.check('EXCEPTION',False,'failed',exception=True)
    def test_graph_failure(self):self.check('RAG_TIMEOUT',False,'failed',graph=True)
    def test_graph_business(self):self.check('POLICY_NOT_FOUND',False,'completed',graph=True)
    def test_string_result(self):self.assertFalse(read_result_failed('订单查询完成'))
