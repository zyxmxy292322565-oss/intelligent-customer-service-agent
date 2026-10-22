"""Controlled loopback-only HTTP tests; never contacts the configured LLM."""
import json
import os
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from agent.input_analyzer import analyze_agent_input
from agent.input_budget import InputAnalysisError, run_analysis_stage
from schemas.agent_schema import BusinessParams, IntentResult


class Handler(BaseHTTPRequestHandler):
    gate = None
    entered = None
    lock = threading.Lock()
    def log_message(self, *_):
        pass

    def do_GET(self):
        with self.lock:
            self.entered.append(1)
        self.gate.wait(45)
        try:
            self.send_response(200); self.end_headers(); self.wfile.write(b'ok')
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        name = request['tool_choice']['function']['name']
        text = request['messages'][-1]['content']
        if 'disconnect' in text:
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        if 'slow' in text:
            time.sleep(16)
        args = '{bad' if 'invalid' in text else json.dumps(
            {'intent': 'query_policy'} if name == 'IntentResult' else {'product_name': '耳机'}, ensure_ascii=False)
        response = {'id':'fake', 'object':'chat.completion', 'created':1, 'model':'fake',
                    'choices':[{'index':0,'finish_reason':'tool_calls','message':{'role':'assistant','content':None,
                    'tool_calls':[{'id':'t1','type':'function','function':{'name':name,'arguments':args}}]}}]}
        encoded = json.dumps(response, ensure_ascii=False).encode()
        try:
            self.send_response(200); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(encoded))); self.end_headers(); self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError):
            pass


@unittest.skipUnless(os.getenv('RUN_R6_LOOPBACK') == '1', 'requires explicit loopback integration opt-in')
class RealLoopbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.settings = SimpleNamespace(deepseek_model='fake', deepseek_api_key='local-test-only',
            deepseek_base_url='http://127.0.0.1:%d/v1' % cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close()

    def run_analysis(self, text):
        with patch('llm.client.get_settings', return_value=self.settings):
            return analyze_agent_input(text, trace_id='local-r6', task_id=1)

    def test_01_fast_both_stages(self):
        t=time.monotonic(); result=self.run_analysis('quick')
        print('MEASURE fast_both=%.3fs' % (time.monotonic()-t))
        self.assertEqual(result.intent, 'query_policy'); self.assertEqual(result.params.product_name, '耳机')

    def test_02_disconnect(self):
        t=time.monotonic()
        with self.assertRaises(InputAnalysisError) as ctx: self.run_analysis('disconnect')
        print('MEASURE disconnect=%.3fs category=%s' % (time.monotonic()-t,ctx.exception.category))
        self.assertEqual(ctx.exception.category,'upstream_error')

    def test_03_invalid_structured(self):
        t=time.monotonic()
        with self.assertRaises(InputAnalysisError) as ctx: self.run_analysis('invalid')
        print('MEASURE invalid=%.3fs category=%s' % (time.monotonic()-t,ctx.exception.category))
        self.assertEqual(ctx.exception.category,'invalid_response')

    def test_04_actual_sdk_timeout_both_stages(self):
        from agent.intent_router import route_intent
        from agent.parameter_extractor import extract_business_params
        results={}
        def run(stage,func):
            t=time.monotonic()
            try: run_analysis_stage(stage,func,'slow',time.monotonic()+40)
            except InputAnalysisError as e: results[stage]=(time.monotonic()-t,e.category)
        with patch('llm.client.get_settings', return_value=self.settings):
            threads=[threading.Thread(target=run,args=('intent',route_intent)),threading.Thread(target=run,args=('parameters',extract_business_params))]
            for th in threads: th.start()
            for th in threads: th.join(22)
        print('MEASURE SDK_timeout',results)
        self.assertEqual(set(results),{'intent','parameters'})
        for elapsed,kind in results.values():
            self.assertEqual(kind,'http_timeout'); self.assertGreaterEqual(elapsed,14); self.assertLess(elapsed,20)

    def test_05_wall_deadline_and_slots_recover(self):
        # Use real HTTP connections to this local server; shorten stage deadline
        # to exercise saturation without waiting 20 seconds in every test.
        release=threading.Event(); entered=[]; outcomes=[]
        Handler.gate=release; Handler.entered=entered
        def upstream(_):
            return httpx.get('http://127.0.0.1:%d/' % self.server.server_port, timeout=5, trust_env=False).text
        def run():
            try: run_analysis_stage('intent',upstream,'x',time.monotonic()+2)
            except InputAnalysisError as e: outcomes.append(e.category)
        with patch('agent.input_budget.INPUT_ANALYSIS_STAGE_SECONDS',.2):
            threads=[threading.Thread(target=run) for _ in range(8)]
            for th in threads: th.start()
            end=time.monotonic()+1
            while len(entered)<8 and time.monotonic()<end: time.sleep(.005)
            self.assertEqual(len(entered),8)
            t=time.monotonic()
            with self.assertRaises(InputAnalysisError) as ctx: run_analysis_stage('intent',upstream,'x',time.monotonic()+1)
            print('MEASURE ninth=%.3fs category=%s' % (time.monotonic()-t,ctx.exception.category))
            self.assertEqual(ctx.exception.category,'capacity_exhausted')
            for th in threads: th.join(1)
            self.assertEqual(outcomes.count('stage_timeout'),8)
            self.assertEqual(sum(t.name=='agent-input-llm' and t.is_alive() for t in threading.enumerate()),8)
            release.set()
            end=time.monotonic()+2
            while any(t.name=='agent-input-llm' and t.is_alive() for t in threading.enumerate()) and time.monotonic()<end: time.sleep(.01)
        self.assertFalse(any(t.name=='agent-input-llm' and t.is_alive() for t in threading.enumerate()))
        self.assertEqual(self.run_analysis('quick').intent,'query_policy')

    def test_06_real_total_wall_deadline(self):
        # The helper's wall limit is checked independently of the SDK socket timeout.
        gate=threading.Event(); t=time.monotonic()
        with patch('agent.input_budget.INPUT_ANALYSIS_STAGE_SECONDS',50):
            try:
                with self.assertRaises(InputAnalysisError) as ctx:
                    run_analysis_stage('intent',lambda _:gate.wait(41),'x',time.monotonic()+40)
                elapsed=time.monotonic()-t
            finally: gate.set()
        print('MEASURE total_deadline=%.3fs category=%s' % (elapsed,ctx.exception.category))
        self.assertEqual(ctx.exception.category,'analysis_timeout'); self.assertGreaterEqual(elapsed,39); self.assertLess(elapsed,42)

    def test_07_real_stage_wall_deadline(self):
        release=threading.Event(); Handler.gate=release; Handler.entered=[]
        def upstream(_):
            # Simulate a peer that has not yet completed, with an HTTP timeout
            # longer than the analyzer's independent wall-clock deadline.
            return httpx.get('http://127.0.0.1:%d/' % self.server.server_port, timeout=30, trust_env=False).text
        start=time.monotonic()
        try:
            with self.assertRaises(InputAnalysisError) as ctx:
                run_analysis_stage('intent',upstream,'x',time.monotonic()+40)
            elapsed=time.monotonic()-start
            alive=sum(t.name=='agent-input-llm' and t.is_alive() for t in threading.enumerate())
            print('MEASURE stage_deadline=%.3fs category=%s remaining_workers=%s' % (elapsed,ctx.exception.category,alive))
            self.assertEqual(ctx.exception.category,'stage_timeout'); self.assertGreaterEqual(elapsed,19); self.assertLess(elapsed,22)
            self.assertGreaterEqual(alive,1)
        finally:
            release.set()


if __name__ == '__main__': unittest.main(verbosity=2)
