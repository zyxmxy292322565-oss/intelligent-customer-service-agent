"""Explicitly guarded integration checks against a disposable isolated schema."""
import os
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pymysql

from tests.test_r6_isolated_http import Handler


@unittest.skipUnless(os.getenv('RUN_R6_ISOLATED_MYSQL') == '1', 'requires verified R6 isolated MySQL')
class IsolatedMySQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        host=os.environ['MYSQL_HOST']; port=int(os.environ['MYSQL_PORT']); database=os.environ['MYSQL_DATABASE']
        if host!='127.0.0.1' or port!=3312 or not database.startswith('phase5a_r6_'):
            raise RuntimeError('Refusing non-isolated MySQL target')
        server=pymysql.connect(host=host,port=port,user=os.environ['MYSQL_USER'],password=os.environ['MYSQL_PASSWORD'])
        with server.cursor() as c:
            c.execute('SELECT @@server_uuid'); uuid=c.fetchone()[0]
            if uuid != os.environ['R6_MYSQL_UUID'] or uuid == '5fce1f14-c3ba-11f1-a38f-c687b132c42a':
                raise RuntimeError('Unexpected or production MySQL UUID')
            c.execute('CREATE DATABASE `'+database+'` CHARACTER SET utf8mb4')
        server.commit(); server.close()
        from db.init_db import create_tables
        create_tables()
        from db.connection import get_connection
        con=get_connection()
        with con.cursor() as c:
            c.execute("INSERT INTO users (username) VALUES ('r6_isolated_user')")
            cls.user_id=c.lastrowid
        con.commit(); con.close()
        cls.http=ThreadingHTTPServer(('127.0.0.1',0), Handler)
        cls.http.daemon_threads=True
        threading.Thread(target=cls.http.serve_forever,daemon=True).start()
        cls.settings=SimpleNamespace(deepseek_model='fake',deepseek_api_key='local-test-only',
            deepseek_base_url='http://127.0.0.1:%d/v1'%cls.http.server_port)
        print('ISOLATED database=%s server_uuid=%s baseline tasks=0 steps=0 pending=0 after_sales=0' % (database,uuid))

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown(); cls.http.server_close()
        from db.connection import get_connection
        con=get_connection()
        with con.cursor() as c:
            for table in ('agent_tasks','agent_task_steps','agent_pending_actions','after_sales'):
                c.execute('SELECT COUNT(*) AS n FROM '+table); print('ISOLATED final %s=%s' % (table,c.fetchone()['n']))
            c.execute('SELECT id,status,current_step FROM agent_tasks ORDER BY id'); print('ISOLATED tasks',c.fetchall())
        con.close()

    def test_01_actual_sdk_timeout_task_and_trace(self):
        from agent.runtime import run_agent
        from db.connection import get_connection
        with patch('llm.client.get_settings',return_value=self.settings), \
             patch('agent.runtime.run_read_workflow') as read, patch('agent.runtime.run_write_workflow') as write:
            start=time.monotonic(); result=run_agent('slow',self.user_id,'r6-real-socket-timeout'); elapsed=time.monotonic()-start
        con=get_connection()
        with con.cursor() as c:
            c.execute("SELECT id,status,current_step FROM agent_tasks WHERE goal='slow'"); task=c.fetchone()
            c.execute('SELECT status,error_message,latency_ms FROM agent_task_steps WHERE task_id=%s',(task['id'],)); step=c.fetchone()
        con.close()
        print('ISOLATED real_socket elapsed=%.3f task=%s step=%s result=%s' % (elapsed,task,step,result['code']))
        self.assertEqual(result['code'],'INPUT_ANALYSIS_TIMEOUT')
        self.assertEqual(task['status'],'failed'); self.assertEqual(step['status'],'FAILED')
        self.assertIn('stage=intent category=http_timeout',step['error_message'])
        read.assert_not_called(); write.assert_not_called()

    def test_02_status_update_database_failure_and_recovery(self):
        from agent.runtime import run_agent
        from db.connection import get_connection
        # Simulate only the terminal status write failing; the MySQL instance is not stopped.
        with patch('agent.input_analyzer.route_intent',side_effect=httpx.ReadTimeout('local')), \
             patch('agent.runtime.update_agent_task_context',side_effect=pymysql.err.OperationalError('simulated status write outage')):
            with self.assertRaises(pymysql.err.OperationalError): run_agent('r6-status-outage',self.user_id,'r6-db-outage')
        con=get_connection()
        with con.cursor() as c:
            c.execute("SELECT id,status,current_step FROM agent_tasks WHERE goal='r6-status-outage'"); task=c.fetchone()
            c.execute('SELECT status,error_message FROM agent_task_steps WHERE task_id=%s',(task['id'],)); step=c.fetchone()
        con.close()
        print('ISOLATED simulated_status_outage task=%s step=%s' % (task,step))
        # Acceptance deliberately demands a recoverable terminal state; do not
        # mutate this task merely to make the check pass.
        self.assertEqual(task['status'],'failed','DB status-write failure leaves task running; no automatic recovery demonstrated')


if __name__=='__main__': unittest.main(verbosity=2)
