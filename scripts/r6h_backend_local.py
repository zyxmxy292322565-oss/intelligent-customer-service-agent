"""Launch only the isolated Backend with explicit loopback proxy exclusion."""
import sys
sys.path.insert(0,str(__import__('pathlib').Path(__file__).resolve().parents[1]))
from scripts.r6h_isolated_acceptance import *

if __name__=='__main__':
    info=json.loads(docker('inspect','phase5a-mysql-20261015'))[0]
    password=dict(v.split('=',1) for v in info['Config']['Env'] if '=' in v)['MYSQL_ROOT_PASSWORD']
    env=os.environ.copy();env.update({k:str(v) for k,v in dotenv_values(ROOT/'.env').items() if v is not None})
    env.update(MYSQL_HOST='127.0.0.1',MYSQL_PORT='3312',MYSQL_USER='root',MYSQL_PASSWORD=password,MYSQL_DATABASE=DB,
               REDIS_HOST='127.0.0.1',REDIS_PORT='16389',REDIS_DB='0',REDIS_PASSWORD='',REDIS_KEY_PREFIX='r6h',
               POLICY_RAG_SERVICE_URL='http://127.0.0.1:18018',APP_ENV='test',USE_LANGGRAPH_AGENT='false',
               NO_PROXY='127.0.0.1,localhost,host.docker.internal',PYTHONIOENCODING='utf-8')
    log=(OUT/'backend_noproxy.log').open('w',encoding='utf-8')
    process=subprocess.Popen([str(ROOT/'venv/Scripts/python.exe'),'-m','uvicorn','main:app','--host','127.0.0.1','--port','18020'],cwd=ROOT,env=env,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
    (OUT/'backend_noproxy.pid').write_text(str(process.pid))
    wait('http://127.0.0.1:18020/health',60)
    print('isolated Backend ready on 18020')
