"""Run preserved suites in fresh guarded isolation schemas, retain records."""
import sys
sys.path.insert(0,str(__import__('pathlib').Path(__file__).resolve().parents[1]))
from scripts.r6h_isolated_acceptance import *
import pymysql

if __name__=='__main__':
    info=json.loads(docker('inspect','phase5a-mysql-20261015'))[0]
    values=dict(v.split('=',1) for v in info['Config']['Env'] if '=' in v)
    env=os.environ.copy()
    env.update(APP_ENV='test',MYSQL_HOST='127.0.0.1',MYSQL_PORT='3312',MYSQL_USER='root',MYSQL_PASSWORD=values['MYSQL_ROOT_PASSWORD'],REDIS_HOST='127.0.0.1',REDIS_PORT='16389',REDIS_PASSWORD='',PYTHONIOENCODING='utf-8')
    results=[]
    for kind,flag,module in [('e','RUN_R6E_MYSQL','tests.test_r6e_action_receipts_mysql'),('f','RUN_R6F_MYSQL','tests.test_r6f_task_execution_mysql'),('g','RUN_R6G_MYSQL','tests.test_r6g_task_recovery_mysql')]:
        database=f'phase5a_r6{kind}_20261020_'+('b' if kind=='e' else '')+'h1'
        con=pymysql.connect(host='127.0.0.1',port=3312,user='root',password=values['MYSQL_ROOT_PASSWORD'])
        with con.cursor() as c:
            c.execute('SELECT @@server_uuid');assert c.fetchone()[0]==UUID
            c.execute(f'CREATE DATABASE `{database}` CHARACTER SET utf8mb4')
        con.close()
        current=env.copy();current.update(MYSQL_DATABASE=database);current[flag]='1'
        r=subprocess.run([str(ROOT/'venv/Scripts/python.exe'),'-m','unittest',module,'-v'],env=current,cwd=ROOT,capture_output=True,text=True,encoding='utf-8')
        (OUT/f'regression_{kind}.log').write_text(r.stdout+r.stderr,encoding='utf-8')
        results.append({'group':kind,'database':database,'exit':r.returncode})
        print(results[-1],flush=True)
    env['MYSQL_DATABASE']=DB
    r=subprocess.run([str(ROOT/'venv/Scripts/python.exe'),'-m','unittest','discover','-s','.','-p','test*.py'],env=env,cwd=ROOT,capture_output=True,text=True,encoding='utf-8')
    (OUT/'offline.log').write_text(r.stdout+r.stderr,encoding='utf-8')
    results.append({'group':'offline','exit':r.returncode})
    (OUT/'regression.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
    print(results[-1])
