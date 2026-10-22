"""Run current fairness/regression suites in new retained isolation schemas.

No service lifecycle commands, formal credentials, cleanup SQL, or real LLM/RAG
calls. Output directory is unique so previous evidence is never overwritten.
"""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import pymysql

ROOT = Path(__file__).resolve().parents[1]
UUID = 'c162676c-c840-11f1-bff1-0a7c71283ea5'
TABLES = ('users', 'orders', 'order_items', 'agent_tasks', 'agent_task_steps',
          'agent_pending_actions', 'agent_action_receipts', 'after_sales')

CHILD = '''import io,json,sys,time,unittest
from db.connection import get_connection
module=sys.argv[1]
if module=='tests.test_agent_flow':
    from tests.test_agent_flow import AgentFlowMySQLTests
    # Preserve this run's random new schema instead of the fixture's DROP.
    def retain(cls):
        if cls.database_patch is not None:
            cls.database_patch.stop();cls.database_patch=None
        print('RETAINED_AGENTFLOW_DATABASE='+cls.database_name)
    AgentFlowMySQLTests.tearDownClass=classmethod(retain)
suite=(unittest.defaultTestLoader.discover('.',pattern='test*.py')
       if module=='offline' else unittest.defaultTestLoader.loadTestsFromName(module))
stream=io.StringIO();start=time.monotonic()
result=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
print(stream.getvalue())
stats=dict(total=result.testsRun,passed=result.testsRun-len(result.skipped)-len(result.failures)-len(result.errors),
failed=len(result.failures),errors=len(result.errors),skipped=len(result.skipped),seconds=round(time.monotonic()-start,3),
skip_reasons=[dict(test=t.id(),reason=r) for t,r in result.skipped])
print('RESULT_JSON='+json.dumps(stats,ensure_ascii=False))
sys.exit(not result.wasSuccessful())
'''


def run():
    out = ROOT / 'data' / ('fairness_20261020_' + uuid.uuid4().hex[:8])
    out.mkdir(parents=True, exist_ok=False)
    # Inspect only; never start the unsafe legacy RAG container.
    info = json.loads(subprocess.check_output(
        ['docker', 'inspect', 'phase5a-mysql-20261015'], text=True))[0]
    assert info['State']['Running']
    assert any(m.get('Name') == 'phase5a_mysql_20261015'
               and m['Destination'] == '/var/lib/mysql' for m in info['Mounts'])
    assert any(p['HostPort'] == '3312' for p in info['NetworkSettings']['Ports']['3306/tcp'])
    values = dict(v.split('=', 1) for v in info['Config']['Env'] if '=' in v)
    password = values['MYSQL_ROOT_PASSWORD']
    def server():
        return pymysql.connect(host='127.0.0.1', port=3312, user='root',
                               password=password, cursorclass=pymysql.cursors.DictCursor)
    def counts(database):
        answer = {}
        with server() as con, con.cursor() as cursor:
            for table in TABLES:
                cursor.execute('SELECT COUNT(*) n FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s', (database, table))
                if cursor.fetchone()['n']:
                    cursor.execute(f'SELECT COUNT(*) n FROM `{database}`.`{table}`')
                    answer[table] = cursor.fetchone()['n']
                else:
                    answer[table] = 0
        return answer
    def protected():
        with server() as con, con.cursor() as c:
            c.execute('SELECT id,status,current_step FROM phase5a_r6_20261019_a.agent_tasks WHERE id=2')
            return c.fetchone()
    with server() as con, con.cursor() as c:
        c.execute('SELECT @@server_uuid uuid')
        assert c.fetchone()['uuid'] == UUID
    evidence = dict(mysql_host='127.0.0.1', mysql_port=3312, server_uuid=UUID,
                    mysql_volume='phase5a_mysql_20261015', protected_before=protected())
    # Redis is not used by these suites; no real RAG endpoint is called.
    env = os.environ.copy()
    for key in list(env):
        if key.startswith('RUN_'): env.pop(key)
    env.update(APP_ENV='test', MYSQL_HOST='127.0.0.1', MYSQL_PORT='3312',
               MYSQL_USER='root', MYSQL_PASSWORD=password, MYSQL_DATABASE='phase5a_no_write',
               REDIS_HOST='127.0.0.1', REDIS_PORT='16389', REDIS_DB='0', REDIS_PASSWORD='',
               POLICY_RAG_SERVICE_URL='http://127.0.0.1:1', NO_PROXY='127.0.0.1,localhost',
               PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
    groups = [
        ('heartbeat', None, None, 'tests.test_task_scheduler_fairness'),
        ('fairness_mysql', 'phase5a_fair_20261020_', 'RUN_FAIRNESS_MYSQL', 'tests.test_task_scheduler_fairness_mysql'),
        ('b', 'phase5a_r6b_20261020_', 'RUN_R6B_MYSQL', 'tests.test_r6b_coordination_mysql'),
        ('e', 'phase5a_r6e_20261020_b', 'RUN_R6E_MYSQL', 'tests.test_r6e_action_receipts_mysql'),
        ('f', 'phase5a_r6f_20261020_', 'RUN_R6F_MYSQL', 'tests.test_r6f_task_execution_mysql'),
        ('g', 'phase5a_r6g_20261020_', 'RUN_R6G_MYSQL', 'tests.test_r6g_task_recovery_mysql'),
        ('agentflow', None, 'RUN_AGENT_MYSQL_TESTS', 'tests.test_agent_flow'),
        ('offline', None, None, 'offline'),
    ]
    results = []
    for name, prefix, flag, module in groups:
        current = env.copy()
        database = None
        if prefix:
            database = prefix + uuid.uuid4().hex[:8]
            with server() as con, con.cursor() as c:
                c.execute(f'CREATE DATABASE `{database}` CHARACTER SET utf8mb4')
            current['MYSQL_DATABASE'] = database
        if flag: current[flag] = '1'
        before = counts(database) if database else None
        result = subprocess.run([sys.executable, '-B', '-c', CHILD, module],
                                cwd=ROOT, env=current, capture_output=True,
                                text=True, encoding='utf-8', timeout=240)
        log = result.stdout + result.stderr
        (out / f'{name}.log').write_text(log, encoding='utf-8')
        stats = next((json.loads(line[len('RESULT_JSON='):]) for line in result.stdout.splitlines()
                      if line.startswith('RESULT_JSON=')), {})
        if name == 'agentflow':
            database = next((line.split('=', 1)[1] for line in result.stdout.splitlines()
                             if line.startswith('RETAINED_AGENTFLOW_DATABASE=')), None)
            before = dict.fromkeys(TABLES, 0)
        entry = dict(group=name, module=module, database=database, exit=result.returncode,
                     before=before, after=counts(database) if database else None, **stats)
        results.append(entry)
        evidence.update(results=results, protected_after=protected())
        (out / 'results.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({k: v for k, v in entry.items() if k not in ('skip_reasons',)}, ensure_ascii=False), flush=True)
        if result.returncode:
            print(log[-6000:]); print('EVIDENCE_DIR='+str(out)); return result.returncode
    assert evidence['protected_before'] == evidence['protected_after']
    print('EVIDENCE_DIR='+str(out))
    return 0


if __name__ == '__main__':
    sys.exit(run())
