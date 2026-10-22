"""Retained fresh-schema MySQL fairness tests; refuse all formal targets."""
from collections import OrderedDict
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

import pymysql
from tests import test_r6g_task_recovery_mysql as helpers


@unittest.skipUnless(os.getenv('RUN_FAIRNESS_MYSQL') == '1', 'requires fresh fairness MySQL')
class SchedulerFairnessMySQLTests(unittest.TestCase):
    DB_PREFIX = 'phase5a_fair_20261020_'
    UUID = helpers.TaskRecoveryMySQLTests.UUID
    setUpClass = classmethod(helpers.TaskRecoveryMySQLTests.setUpClass.__func__)
    tearDownClass = classmethod(helpers.TaskRecoveryMySQLTests.tearDownClass.__func__)
    counts = classmethod(helpers.TaskRecoveryMySQLTests.counts.__func__)
    row = helpers.TaskRecoveryMySQLTests.row
    execute = helpers.TaskRecoveryMySQLTests.execute
    task = helpers.TaskRecoveryMySQLTests.task
    action = helpers.TaskRecoveryMySQLTests.action
    expire = helpers.TaskRecoveryMySQLTests.expire
    claim_and_recover = helpers.TaskRecoveryMySQLTests.claim_and_recover

    def test_01_140_real_renewals_and_expired_owners(self):
        from services import task_service as tasks
        with patch.object(tasks, '_lease_registry', OrderedDict()):
            ids = [self.task(f'fair-{i}', f'FAIR-{i}') for i in range(140)]
            dead = self.task('expired', 'FAIR-EXPIRED')
            self.expire(dead)
            foreign = self.task('foreign-owner', 'FAIR-REAL')
            tasks.unregister_agent_task_lease(foreign, 'FAIR-REAL')
            tasks.register_agent_task_lease(foreign, 'FAIR-WRONG')
            seen = []
            real_renew = tasks.renew_agent_task_lease
            def renew(task, owner):
                answer = real_renew(task, owner)
                seen.append((task, answer))
                return answer
            rounds = []
            for _ in range(3):
                seen.clear()
                with patch.object(tasks, 'renew_agent_task_lease', side_effect=renew):
                    result = tasks.renew_registered_task_leases(100)
                rounds.append({'selected': list(seen), 'result': result})
            print('MYSQL_HEARTBEAT_ROUNDS', json.dumps(rounds))
            succeeded = {tid for r in rounds[:2] for tid, ok in r['selected'] if ok}
            self.assertTrue(set(ids).issubset(succeeded))
            self.assertFalse(real_renew(dead, 'FAIR-EXPIRED'))
            self.assertFalse(real_renew(foreign, 'FAIR-WRONG'))
            self.assertNotIn((dead, 'FAIR-EXPIRED'), tasks._lease_registry)
            # Same-second no-op and simultaneous valid-owner heartbeats.
            answers = []
            workers = [threading.Thread(target=lambda: answers.append(
                real_renew(ids[0], 'FAIR-0'))) for _ in range(2)]
            for worker in workers: worker.start()
            for worker in workers: worker.join(10)
            self.assertEqual(answers, [True, True])
            self.assertTrue(real_renew(ids[0], 'FAIR-0'))
            for i, tid in enumerate(ids):
                tasks.update_agent_task_context(tid, 'completed', 'completed', f'FAIR-{i}')

    def test_02_durable_oldest_due_order_survives_worker_restart(self):
        from services import task_recovery_service as recovery
        reviews = []
        for i in range(25):
            owner = f'FAIR-REVIEW-{i}'
            tid = self.task(f'review-{i}', owner)
            self.action(tid, f'CONF-FAIR-{i}', 'executing')
            self.expire(tid)
            self.assertEqual(self.claim_and_recover(tid, owner, f'REC-FAIR-{i}'),
                             'review_required')
            self.execute('UPDATE agent_tasks SET recovery_started_at=DATE_SUB(NOW(), INTERVAL 600 SECOND) WHERE id=%s', (tid,))
            reviews.append(tid)
        crashed = [self.task(f'crash-{i}', f'FAIR-CRASH-{i}') for i in range(9)]
        for tid in crashed: self.expire(tid)
        valid = [self.task(f'valid-{i}', f'FAIR-VALID-{i}') for i in range(3)]
        historical = self.task('no-owner')
        steps_before = self.row('SELECT COUNT(*) n FROM agent_task_steps')['n']
        rounds = []
        # Every scan is a fresh Python process: no hidden in-memory cursor.
        code = '''import json
from services import task_recovery_service as r
original=r.list_task_recovery_candidates
selected=[]
def record(*args):
    rows=original(*args); selected.extend(row['id'] for row in rows); return rows
r.list_task_recovery_candidates=record
summary=r.recover_expired_agent_tasks(lease_seconds=60,recovery_claim_seconds=1,batch_size=10)
print(json.dumps({'selected':selected,'summary':summary}))
'''
        for _ in range(4):
            result = subprocess.run([sys.executable, '-B', '-c', code],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            entry = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertLessEqual(entry['summary']['scanned'], 10)
            self.assertEqual(entry['summary']['errors'], 0)
            rounds.append(entry)
            time.sleep(1.05)
        print('MYSQL_RECOVERY_ROUNDS', json.dumps(rounds))
        selected = {tid for entry in rounds for tid in entry['selected']}
        self.assertTrue(set(crashed).issubset(selected))
        for tid in crashed:
            self.assertEqual(self.row('SELECT status FROM agent_tasks WHERE id=%s', (tid,))['status'], 'failed')
        for tid in reviews:
            row = self.row('SELECT status,recovery_reason FROM agent_tasks WHERE id=%s', (tid,))
            self.assertEqual((row['status'], row['recovery_reason']), ('recovery_review', 'BUSINESS_RESULT_UNKNOWN'))
        for tid in valid + [historical]:
            self.assertNotIn(tid, selected)
            self.assertEqual(self.row('SELECT status FROM agent_tasks WHERE id=%s', (tid,))['status'], 'running')
        self.assertGreater(self.row('SELECT COUNT(*) n FROM agent_task_steps')['n'], steps_before)
        self.assertEqual(self.row('SELECT COUNT(*) n FROM after_sales')['n'], 0)
        self.assertEqual(self.row('SELECT COUNT(*) n FROM agent_action_receipts')['n'], 0)

    def test_03_concurrent_claim_and_transient_database_failures(self):
        from services import task_recovery_service as recovery
        from services import task_service as tasks
        tid = self.task('race', 'FAIR-RACE')
        self.expire(tid)
        candidate = {'id': tid, 'execution_id': 'FAIR-RACE', 'recovery_id': None, 'status': 'running'}
        barrier, answers, errors = threading.Barrier(2), [], []
        def worker(owner):
            try:
                barrier.wait()
                answers.append((owner, recovery.claim_task_recovery(candidate, owner)))
            except Exception as exc: errors.append(str(exc))
        workers = [threading.Thread(target=worker, args=(f'REC-RACE-{i}',)) for i in range(2)]
        for worker in workers: worker.start()
        for worker in workers: worker.join(10)
        self.assertFalse(errors)
        self.assertEqual(sorted(ok for _, ok in answers), [False, True])
        self.assertFalse(tasks.renew_agent_task_lease(tid, 'FAIR-RACE'))
        winner = next(owner for owner, ok in answers if ok)
        real = recovery.get_connection
        with patch.object(recovery, 'get_connection', side_effect=[
                pymysql.err.OperationalError(2003, 'injected connection outage'), real()]):
            self.assertEqual(recovery._with_db_retry(lambda: recovery.recover_claimed_task(tid, winner)), 'failed')
        with patch.object(recovery, 'get_connection', side_effect=pymysql.err.OperationalError(2003, 'injected outage')) as call:
            with self.assertRaises(pymysql.err.OperationalError):
                recovery._with_db_retry(lambda: recovery.list_task_recovery_candidates(60, 60, 10))
            self.assertEqual(call.call_count, 3)
        self.assertEqual(self.row('SELECT status FROM agent_tasks WHERE id=%s', (tid,))['status'], 'failed')
