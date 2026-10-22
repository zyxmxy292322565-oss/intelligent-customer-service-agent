"""Read-only final verification of new fixtures and protected history."""
import sys
from pathlib import Path
import json
import subprocess
import pymysql

ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'data/r6b_20261020'
if __name__=='__main__':
    info=json.loads(subprocess.check_output(['docker','inspect','phase5a-mysql-20261015'],text=True))[0]
    password=dict(x.split('=',1) for x in info['Config']['Env'] if '=' in x)['MYSQL_ROOT_PASSWORD']
    con=pymysql.connect(host='127.0.0.1',port=3312,user='root',password=password,cursorclass=pymysql.cursors.DictCursor)
    results=json.loads((OUT/'results.json').read_text())
    snapshots={}
    with con.cursor() as c:
        c.execute('SELECT @@server_uuid AS uuid');assert c.fetchone()['uuid']=='c162676c-c840-11f1-bff1-0a7c71283ea5'
        for result in results:
            db=result['database'];assert db.startswith('phase5a_r6')
            counts={}
            for table in ['agent_tasks','agent_task_steps','agent_pending_actions','agent_action_receipts','after_sales']:
                c.execute(f'SELECT COUNT(*) AS n FROM `{db}`.`{table}`');counts[table]=c.fetchone()['n']
            c.execute(f'SELECT id,status,execution_id,recovery_status FROM `{db}`.agent_tasks ORDER BY id')
            snapshots[db]={'counts':counts,'tasks':c.fetchall()}
        c.execute('SELECT id,status,current_step FROM phase5a_r6_20261019_a.agent_tasks WHERE id=2')
        snapshots['protected_task2']=c.fetchone()
        c.execute('SELECT id,status,confirmation_id FROM phase5a_r6h_20261020_a.agent_pending_actions WHERE confirmation_id=%s',('CONF_31F0D005F7C540DF',))
        snapshots['protected_r6h_preview']=c.fetchone()
    con.close()
    (OUT/'snapshots.json').write_text(json.dumps(snapshots,indent=2),encoding='utf-8')
    print(json.dumps(snapshots,indent=2))
