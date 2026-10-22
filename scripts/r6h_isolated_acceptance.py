"""Explicit R6H isolation setup and real HTTP acceptance. Never targets production."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import secrets
import requests
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
RAG = ROOT.parent / 'demo3_rag_qa'
OUT = ROOT / 'data' / 'r6h_20261020'
OUT.mkdir(parents=True, exist_ok=True)
DB = 'phase5a_r6h_20261020_a'
RDB = 'phase5a_r6h_rag_20261020_a'
UUID = 'c162676c-c840-11f1-bff1-0a7c71283ea5'

def docker(*args):
    return subprocess.check_output(['docker', *args], text=True, encoding='utf-8').strip()

def setup():
    info = json.loads(docker('inspect', 'phase5a-mysql-20261015'))[0]
    values = dict(v.split('=', 1) for v in info['Config']['Env'] if '=' in v)
    password = values['MYSQL_ROOT_PASSWORD']
    import pymysql
    con = pymysql.connect(host='127.0.0.1', port=3312, user='root', password=password)
    with con.cursor() as c:
        c.execute('SELECT @@server_uuid')
        assert c.fetchone()[0] == UUID
        for db in (DB, RDB):
            c.execute('SHOW DATABASES LIKE %s', (db,))
            if c.fetchone():
                raise RuntimeError('Fresh database required: '+db)
            c.execute(f'CREATE DATABASE `{db}` CHARACTER SET utf8mb4')
    con.close()
    env = os.environ.copy()
    settings = dotenv_values(ROOT / '.env')
    env.update({k:str(v) for k,v in settings.items() if v is not None})
    env.update(MYSQL_HOST='127.0.0.1', MYSQL_PORT='3312', MYSQL_USER='root',
               MYSQL_PASSWORD=password, MYSQL_DATABASE=DB, APP_ENV='test',
               REDIS_HOST='127.0.0.1', REDIS_PORT='16389', REDIS_DB='0',
               REDIS_PASSWORD='', REDIS_KEY_PREFIX='r6h', USE_LANGGRAPH_AGENT='false',
               POLICY_RAG_SERVICE_URL='http://127.0.0.1:18018',
               NO_PROXY='127.0.0.1,localhost,host.docker.internal',
               JWT_SECRET_KEY=secrets.token_hex(32), PYTHONIOENCODING='utf-8')
    subprocess.run([str(ROOT/'venv/Scripts/python.exe'), '-m', 'infra.bootstrap'], env=env, cwd=ROOT, check=True)
    for name in ('r6h_20261020_redis','r6h_20261020_index','r6h_20261020_models'):
        docker('volume','create',name)
    docker('run','--rm','--mount','type=volume,src=app_policy_rag_model_cache,dst=/source,readonly',
           '--mount','type=volume,src=r6h_20261020_models,dst=/target','alpine:3.20',
           'sh','-c','cp -a /source/. /target/')
    docker('run','-d','--name','r6h-redis-20261020','-p','127.0.0.1:16389:6379',
           '-v','r6h_20261020_redis:/data','redis:7-alpine','redis-server','--appendonly','yes')
    rag_env = {k:env[k] for k in ('DEEPSEEK_API_KEY','DEEPSEEK_MODEL') if k in env}
    rag_env.update(DEEPSEEK_API_BASE=env.get('DEEPSEEK_BASE_URL') or env.get('DEEPSEEK_API_BASE','https://api.deepseek.com'),
                   MYSQL_HOST='host.docker.internal',MYSQL_PORT='3312',MYSQL_USER='root',
                   MYSQL_PASSWORD=password,MYSQL_DATABASE=RDB,REDIS_HOST='host.docker.internal',
                   REDIS_PORT='16389',REDIS_DB='1',REDIS_PASSWORD='',HF_HUB_OFFLINE='1',
                   TRANSFORMERS_OFFLINE='1',PYTHONIOENCODING='utf-8')
    args=['run','-d','--name','r6h-rag-20261020','-p','127.0.0.1:18018:8000',
          '--mount',f'type=bind,src={RAG},dst=/app,readonly',
          '--mount','type=volume,src=r6h_20261020_index,dst=/app/cache,volume-nocopy',
          '-v','r6h_20261020_models:/root/.cache/huggingface']
    for key,value in rag_env.items(): args += ['-e',key+'='+value]
    args += ['app-policy-rag:latest','uvicorn','api_app:app','--host','0.0.0.0','--port','8000']
    docker(*args)
    wait('http://127.0.0.1:18018/health',240)
    imports=[]
    for path in sorted((RAG/'policy_docs').glob('*.txt')):
        with path.open('rb') as f:
            response=requests.post('http://127.0.0.1:18018/upload',files={'file':(path.name,f,'text/plain')},
                                   data={'version':'','created_by':'r6h-isolated'},timeout=180)
        response.raise_for_status()
        imports.append({'file':path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'result':response.json()})
    (OUT/'imports.json').write_text(json.dumps(imports,ensure_ascii=False,indent=2),encoding='utf-8')
    log=(OUT/'backend.log').open('w',encoding='utf-8')
    process=subprocess.Popen([str(ROOT/'venv/Scripts/python.exe'),'-m','uvicorn','main:app',
                              '--host','127.0.0.1','--port','18019'],cwd=ROOT,env=env,stdout=log,stderr=log,
                             creationflags=subprocess.CREATE_NO_WINDOW)
    (OUT/'backend.pid').write_text(str(process.pid))
    wait('http://127.0.0.1:18019/health',60)
    return env

def wait(url,seconds):
    start=time.monotonic()
    while time.monotonic()-start<seconds:
        try:
            if requests.get(url,timeout=2).ok:return
        except requests.RequestException:pass
        time.sleep(2)
    raise RuntimeError('Service did not become ready: '+url)

if __name__=='__main__':
    setup()
    print('R6H isolated services initialized; no production targets used')
