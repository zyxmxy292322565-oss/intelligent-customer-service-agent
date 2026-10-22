"""Retained fresh-schema R6B verification; refuses the formal MySQL instance."""
import os
import sys
import json
import uuid
import subprocess
from pathlib import Path
import pymysql

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'data'/'r6b_20261020';OUT.mkdir(parents=True,exist_ok=True)

if __name__=='__main__':
    instance=json.loads(subprocess.check_output(['docker','inspect','phase5a-mysql-20261015'],text=True))[0]
    assert any(m.get('Name')=='phase5a_mysql_20261015' and m['Destination']=='/var/lib/mysql' for m in instance['Mounts'])
    assert instance['NetworkSettings']['Ports']['3306/tcp'][0]['HostPort']=='3312'
    password=dict(x.split('=',1) for x in instance['Config']['Env'] if '=' in x)['MYSQL_ROOT_PASSWORD']
    env=os.environ.copy();env.update(MYSQL_HOST='127.0.0.1',MYSQL_PORT='3312',MYSQL_USER='root',MYSQL_PASSWORD=password,APP_ENV='test',PYTHONIOENCODING='utf-8',NO_PROXY='127.0.0.1,localhost',REDIS_HOST='127.0.0.1',REDIS_PORT='16389',REDIS_PASSWORD='')
    results=[]
    for prefix,flag,module in [
        ('phase5a_r6b_20261020_','RUN_R6B_MYSQL','tests.test_r6b_coordination_mysql'),
        ('phase5a_r6e_20261020_b','RUN_R6E_MYSQL','tests.test_r6e_action_receipts_mysql'),
        ('phase5a_r6f_20261020_','RUN_R6F_MYSQL','tests.test_r6f_task_execution_mysql'),
        ('phase5a_r6g_20261020_','RUN_R6G_MYSQL','tests.test_r6g_task_recovery_mysql'),
    ]:
        database=prefix+uuid.uuid4().hex[:8]
        con=pymysql.connect(host='127.0.0.1',port=3312,user='root',password=password)
        with con.cursor() as c:
            c.execute('SELECT @@server_uuid');assert c.fetchone()[0]=='c162676c-c840-11f1-bff1-0a7c71283ea5'
            c.execute(f'CREATE DATABASE `{database}` CHARACTER SET utf8mb4')
        con.close()
        current=env.copy();current.update(MYSQL_DATABASE=database);current[flag]='1'
        r=subprocess.run([str(ROOT/'venv/Scripts/python.exe'),'-m','unittest',module,'-v'],cwd=ROOT,env=current,capture_output=True,text=True,encoding='utf-8')
        (OUT/(flag+'.log')).write_text(r.stdout+r.stderr,encoding='utf-8')
        (OUT/(database+'.log')).write_text(r.stdout+r.stderr,encoding='utf-8')
        results.append({'module':module,'database':database,'exit':r.returncode})
        (OUT/'results.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
        print(results[-1],flush=True)
        if r.returncode:sys.exit(r.returncode)
