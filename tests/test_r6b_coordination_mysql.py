"""Fresh-schema, retained real MySQL fixtures for R6B coordination."""
import os
import json
import threading
import unittest
from unittest.mock import patch

from tests import test_r6g_task_recovery_mysql as helpers


@unittest.skipUnless(os.getenv('RUN_R6B_MYSQL')=='1','requires verified fresh R6B MySQL')
class CoordinationMySQLTests(unittest.TestCase):
    row=helpers.TaskRecoveryMySQLTests.row
    execute=helpers.TaskRecoveryMySQLTests.execute
    task=helpers.TaskRecoveryMySQLTests.task
    expire=helpers.TaskRecoveryMySQLTests.expire
    action=helpers.TaskRecoveryMySQLTests.action
    claim_and_recover=helpers.TaskRecoveryMySQLTests.claim_and_recover
    counts=classmethod(helpers.TaskRecoveryMySQLTests.counts.__func__)

    @classmethod
    def setUpClass(cls):
        assert os.environ['MYSQL_HOST']=='127.0.0.1' and os.environ['MYSQL_PORT']=='3312'
        assert os.environ['MYSQL_DATABASE'].startswith('phase5a_r6b_20261020_')
        from db.connection import get_connection
        con=get_connection()
        with con.cursor() as c:
            c.execute('SELECT @@server_uuid AS uuid');assert c.fetchone()['uuid']==helpers.TaskRecoveryMySQLTests.UUID
            c.execute('SHOW TABLES');assert not c.fetchall()
        con.close()
        from infra.bootstrap import main
        main();main()
        con=get_connection()
        with con.cursor() as c:
            c.execute("INSERT INTO users(username) VALUES ('r6b-owner'),('r6b-other')")
            c.execute("INSERT INTO conversations(user_id,title) VALUES (1,'r6b')")
            c.execute("INSERT INTO orders(order_no,user_id,total_amount,status) VALUES ('R6B',1,100,'delivered')")
            cls.order_id=c.lastrowid
            c.execute("INSERT INTO order_items(order_id,product_name,quantity,unit_price) VALUES (%s,'r6b',1,100)",(cls.order_id,))
            cls.item_id=c.lastrowid
        con.commit();con.close()
        print('R6B BASELINE',cls.counts())

    @classmethod
    def tearDownClass(cls):print('R6B FINAL',cls.counts())

    def test_01_agent_owner_blocks_confirm_before_action_claim(self):
        from agent.confirmation_store import claim_pending_action_execution
        tid=self.task('agent-owner','EXEC-B-AGENT')
        self.action(tid,'CONF-B-BLOCK','confirmed')
        self.assertFalse(claim_pending_action_execution('CONF-B-BLOCK'))
        self.assertEqual(self.row("SELECT status FROM agent_pending_actions WHERE confirmation_id='CONF-B-BLOCK'")['status'],'confirmed')

    def test_02_confirm_owner_blocks_agent_and_finishes_atomically(self):
        from agent.confirmation_store import claim_pending_action_execution,update_pending_action_execution
        from services.task_service import update_agent_task_context,claim_agent_task_execution
        tid=self.task('confirm-owner','EXEC-B-PREVIEW')
        self.action(tid,'CONF-B-OWN','confirmed')
        update_agent_task_context(tid,'waiting_confirm','waiting_confirm','EXEC-B-PREVIEW')
        self.assertTrue(claim_pending_action_execution('CONF-B-OWN'))
        self.assertFalse(claim_agent_task_execution(tid,1,1,'EXEC-B-NEW'))
        self.assertTrue(self.row('SELECT execution_id FROM agent_tasks WHERE id=%s',(tid,))['execution_id'].startswith('CONFEXEC_'))
        update_pending_action_execution('CONF-B-OWN','executed',{'success':True})
        self.assertEqual(self.row('SELECT status FROM agent_tasks WHERE id=%s',(tid,))['status'],'completed')

    def test_03_simultaneous_agent_confirm_only_one_wins(self):
        from agent.confirmation_store import claim_pending_action_execution
        from services.task_service import update_agent_task_context,claim_agent_task_execution
        tid=self.task('race','EXEC-B-RACE')
        self.action(tid,'CONF-B-RACE','confirmed')
        update_agent_task_context(tid,'waiting_input','waiting_input','EXEC-B-RACE')
        gate=threading.Barrier(2);answers=[];errors=[]
        def worker(kind):
            try:
                gate.wait()
                answers.append(claim_pending_action_execution('CONF-B-RACE') if kind else claim_agent_task_execution(tid,1,1,'EXEC-B-WINNER'))
            except Exception as exc:errors.append(exc)
        threads=[threading.Thread(target=worker,args=(v,)) for v in (0,1)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertFalse(errors);self.assertEqual(sorted(answers),[False,True])

    def test_04_committed_create_finalization_error_keeps_original_proof(self):
        from agent.action_executor import execute_confirmed_action
        from agent.confirmation_store import get_pending_action,update_pending_action_execution
        from services.task_service import update_agent_task_context
        tid=self.task('commit-proof','EXEC-B-CREATE')
        preview={'order_no':'R6B','order_item_id':self.item_id,'after_sale_type':'repair','reason':'quality'}
        self.action(tid,'CONF-B-CREATE','confirmed',preview=preview)
        update_agent_task_context(tid,'waiting_confirm','waiting_confirm','EXEC-B-CREATE')
        self.execute("INSERT INTO after_sales(after_sale_no,user_id,order_id,order_item_id,type,reason,status,idempotency_key) VALUES ('AS-B-CREATE',1,%s,%s,'repair','quality','pending','CONF-B-CREATE')",(self.order_id,self.item_id))
        def unavailable(*args,**kwargs):raise RuntimeError('injected finalizer outage')
        with patch('agent.action_executor.create_after_sale_service',return_value={'success':True}),patch('agent.action_executor.update_pending_action_execution',side_effect=unavailable):
            result=execute_confirmed_action('CONF-B-CREATE',1)
        self.assertEqual(result['code'],'ACTION_RESULT_UNCERTAIN')
        self.assertEqual(get_pending_action('CONF-B-CREATE',1)['status'],'executing')
        owner=self.row('SELECT execution_id FROM agent_tasks WHERE id=%s',(tid,))['execution_id']
        self.expire(tid)
        self.assertEqual(self.claim_and_recover(tid,owner,'REC-B-CREATE'),'completed')
        self.assertEqual(self.row("SELECT COUNT(*) AS n FROM after_sales WHERE idempotency_key='CONF-B-CREATE'")['n'],1)

    def test_05_trace_failed_terminal_write_outage_later_recovery(self):
        from agent.step_trace import trace_step
        from services.task_service import update_agent_task_context
        tid=self.task('trace-outage','EXEC-B-TRACE')
        with trace_step(tid,'TRACE-B','input_analysis') as step:step.set_result({'success':False,'code':'LLM_TIMEOUT'})
        import pymysql
        with patch('services.task_service.get_connection',side_effect=pymysql.err.OperationalError(2006,'offline')):
            with self.assertRaises(pymysql.err.OperationalError):update_agent_task_context(tid,'failed','failed','EXEC-B-TRACE')
        self.assertEqual(self.row('SELECT status FROM agent_tasks WHERE id=%s',(tid,))['status'],'running')
        self.expire(tid);self.assertEqual(self.claim_and_recover(tid,'EXEC-B-TRACE','REC-B-TRACE'),'failed')

    def test_06_repeated_confirm_claim_only_one_owner(self):
        from agent.confirmation_store import claim_pending_action_execution
        from services.task_service import update_agent_task_context
        tid=self.task('confirm-duplicate','EXEC-B-DUP')
        self.action(tid,'CONF-B-DUP','confirmed')
        update_agent_task_context(tid,'waiting_confirm','waiting_confirm','EXEC-B-DUP')
        self.assertTrue(claim_pending_action_execution('CONF-B-DUP'))
        self.assertFalse(claim_pending_action_execution('CONF-B-DUP'))

    def test_07_late_receipt_can_converge_review_without_reexecution(self):
        from services.after_sale_operation_service import execute_after_sale_operation
        from services.task_recovery_service import recover_expired_agent_tasks
        tid=self.task('late-proof','EXEC-B-LATE')
        sale_id=self.execute("INSERT INTO after_sales(after_sale_no,user_id,order_id,order_item_id,type,reason,status) VALUES ('AS-B-LATE',1,%s,%s,'repair','quality','pending')",(self.order_id,self.item_id))
        preview={'after_sale_id':sale_id,'after_sale_no':'AS-B-LATE'}
        self.action(tid,'CONF-B-LATE','executing','cancel_after_sale',preview)
        self.expire(tid)
        self.assertEqual(self.claim_and_recover(tid,'EXEC-B-LATE','REC-B-LATE'),'review_required')
        # Simulate the original in-flight worker committing late, using exactly
        # its original operation identity, not a new confirmation.
        result=execute_after_sale_operation(user_id=1,conversation_id=1,action_type='cancel_after_sale',preview=preview,confirmation_id='CONF-B-LATE')
        self.assertTrue(result['success'])
        self.execute("UPDATE agent_tasks SET recovery_started_at=DATE_SUB(NOW(),INTERVAL 120 SECOND) WHERE id=%s",(tid,))
        summary=recover_expired_agent_tasks(lease_seconds=60,recovery_claim_seconds=60,batch_size=30)
        self.assertGreaterEqual(summary['completed'],1)
        self.assertEqual(self.row('SELECT status FROM agent_tasks WHERE id=%s',(tid,))['status'],'completed')
        before=self.counts()
        recover_expired_agent_tasks(lease_seconds=60,recovery_claim_seconds=60,batch_size=30)
        self.assertEqual(before,self.counts())

    def test_08_same_second_heartbeat_preserves_owner(self):
        from services.task_service import renew_agent_task_lease,update_agent_task_context
        tid=self.task('heartbeat','EXEC-B-HB')
        self.assertTrue(renew_agent_task_lease(tid,'EXEC-B-HB'))
        self.assertTrue(renew_agent_task_lease(tid,'EXEC-B-HB'))
        update_agent_task_context(tid,'completed','completed','EXEC-B-HB')

    def test_09_distinct_keys_same_item_keep_real_transaction_duplicate_guard(self):
        from services.after_sale_service import create_after_sale_service
        from services.policy_execution_service import validate_policy_before_create
        from services.policy_demo_rules import DEMO_POLICY_SOURCES
        oid=self.execute("INSERT INTO orders(order_no,user_id,total_amount,status,delivered_at) VALUES ('R6B-RACE',1,100,'delivered',DATE_SUB(NOW(),INTERVAL 5 DAY))")
        item=self.execute("INSERT INTO order_items(order_id,product_name,category,quantity,unit_price) VALUES (%s,'AirPods Pro 2','耳机',1,100)",(oid,))
        source=DEMO_POLICY_SOURCES['耳机类商品规则.txt']
        policy={'decision':'ELIGIBLE_TO_APPLY','policy_type':'earphone_quality_return','policy_message':'隔离规则夹具',
                'requires_review':True,'days_since_delivery':5,'policy_deadline_days':30,
                'sources':[source.filename],'policy_versions':{source.filename:'v1'},
                'policy_file_hashes':{source.filename:source.file_hash},
                'evidence':[{'source':source.filename,'file_hash':source.file_hash,'content':'隔离证据夹具','version':'v1'}]}
        barrier=threading.Barrier(2);answers=[];errors=[]
        def gated(**kwargs):
            result=validate_policy_before_create(**kwargs)
            barrier.wait(timeout=10)
            return result
        def worker(key):
            try:
                answers.append(create_after_sale_service(1,'R6B-RACE',item,'return_refund','左耳没声音',idempotency_key=key))
            except Exception as exc:errors.append(exc)
        with patch('services.policy_execution_service.check_after_sale_policy',return_value=policy),patch('services.after_sale_service.validate_policy_before_create',side_effect=gated):
            threads=[threading.Thread(target=worker,args=(key,)) for key in ('CONF-B-DIFF1','CONF-B-DIFF2')]
            for t in threads:t.start()
            for t in threads:t.join(timeout=20)
        self.assertFalse(errors)
        self.assertEqual(sum(r['success'] for r in answers),1)
        self.assertEqual(self.row('SELECT COUNT(*) AS n FROM after_sales WHERE order_item_id=%s',(item,))['n'],1)
        self.assertIn('AFTER_SALE_ALREADY_EXISTS',[r['code'] for r in answers])

    def test_10_recovered_preview_can_confirm_with_renewable_owner(self):
        from agent.confirmation_store import claim_pending_action_execution,update_pending_action_status
        from services.task_service import renew_agent_task_lease
        tid=self.task('recovered-confirm','EXEC-B-RESTORE')
        self.action(tid,'CONF-B-RESTORE','pending')
        self.expire(tid)
        self.assertEqual(self.claim_and_recover(tid,'EXEC-B-RESTORE','REC-B-RESTORE'),'waiting_confirm')
        update_pending_action_status('CONF-B-RESTORE','confirmed')
        self.assertTrue(claim_pending_action_execution('CONF-B-RESTORE'))
        row=self.row('SELECT execution_id,recovery_id FROM agent_tasks WHERE id=%s',(tid,))
        self.assertIsNone(row['recovery_id'])
        self.assertTrue(renew_agent_task_lease(tid,row['execution_id']))
