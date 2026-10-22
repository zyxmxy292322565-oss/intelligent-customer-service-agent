"""Real R6H HTTP checks; all writes use the explicitly named isolated schema."""
import sys
sys.path.insert(0,str(__import__('pathlib').Path(__file__).resolve().parents[1]))
from scripts.r6h_isolated_acceptance import *
import pymysql

def run():
    report={'requests':[]}
    old=json.loads(docker('inspect','r6h-rag-20261020'))[0]
    env=dict(v.split('=',1) for v in old['Config']['Env'] if '=' in v)
    assert env['MYSQL_DATABASE']==RDB and env['MYSQL_HOST']=='host.docker.internal'
    con=pymysql.connect(host='127.0.0.1',port=3312,user='root',password=env['MYSQL_PASSWORD'],database=DB,charset='utf8mb4',cursorclass=pymysql.cursors.DictCursor)
    with con.cursor() as c:
        c.execute('SELECT @@server_uuid AS uuid'); assert c.fetchone()['uuid']==UUID
        c.execute('CREATE DATABASE phase5a_r6h_rag_20261020_b CHARACTER SET utf8mb4')
    docker('stop','r6h-rag-20261020')
    docker('volume','create','r6h_20261020_index_clean')
    args=['run','-d','--name','r6h-rag-clean-20261020','-p','127.0.0.1:18018:8000',
          '--mount',f'type=bind,src={RAG},dst=/app,readonly',
          '--mount','type=volume,src=r6h_20261020_index_clean,dst=/app/cache,volume-nocopy',
          '-v','r6h_20261020_models:/root/.cache/huggingface']
    env['MYSQL_DATABASE']='phase5a_r6h_rag_20261020_b'
    for key,value in env.items():args+=['-e',key+'='+value]
    args+=['app-policy-rag:latest','uvicorn','api_app:app','--host','0.0.0.0','--port','8000']
    docker(*args); wait('http://127.0.0.1:18018/health',240)
    report['empty_index']=requests.get('http://127.0.0.1:18018/rag_status').json()
    assert report['empty_index']['index_count']==0
    for path in sorted((RAG/'policy_docs').glob('*.txt')):
        with path.open('rb') as f:
            r=requests.post('http://127.0.0.1:18018/upload',files={'file':(path.name,f,'text/plain')},data={'version':'v1','created_by':'r6h-isolated'},timeout=180)
        r.raise_for_status(); assert r.json()['success']
    report['index']=requests.get('http://127.0.0.1:18018/rag_status').json()
    def call(label,url,payload,headers=None):
        started=time.monotonic()
        try:
            r=requests.post(url,json=payload,headers=headers,timeout=100)
            data=r.json(); status=r.status_code
        except Exception as exc:data={'exception':type(exc).__name__};status=None
        row={'label':label,'input':payload,'http_status':status,'elapsed_seconds':round(time.monotonic()-started,3),'response':data}
        report['requests'].append(row)
        (OUT/'http_results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
        print(label,status,round(row['elapsed_seconds'],3),data.get('code'),flush=True)
        return data
    for q in ['耳机7天无理由退货有什么条件？','AirPods用了20天左耳没声音可以申请售后吗？','退款多久到账？','无线鼠标保修多少年？','今天天气怎么样？']:
        call('rag','http://127.0.0.1:18018/rag/query',{'question':q,'top_k':5})
    with con.cursor() as c:
        c.execute("INSERT INTO users(username) VALUES ('r6h-user')")
        c.execute("INSERT INTO orders(order_no,user_id,total_amount,status,delivered_at) VALUES ('R6H-ORDER',1,1899,'delivered',DATE_SUB(NOW(),INTERVAL 5 DAY))")
        oid=c.lastrowid
        c.execute("INSERT INTO order_items(order_id,product_name,category,quantity,unit_price) VALUES (%s,'AirPods Pro 2','耳机',1,1899)",(oid,))
    con.commit()
    tables=['users','orders','order_items','after_sales','agent_tasks','agent_task_steps','agent_pending_actions','conversations','conversation_messages','agent_action_receipts']
    def counts():
        con.commit()
        with con.cursor() as c:
            result={}
            for table in tables:c.execute('SELECT COUNT(*) AS n FROM '+table);result[table]=c.fetchone()['n']
            return result
    report['before']=counts()
    token=requests.post('http://127.0.0.1:18019/auth/login',json={'user_id':1},timeout=5).json()['access_token']
    headers={'Authorization':'Bearer '+token}
    def conversation():
        r=requests.post('http://127.0.0.1:18019/agent/conversations',json={'title':'R6H隔离验收'},headers=headers,timeout=5)
        r.raise_for_status();return r.json()['data']['id']
    for label,messages in [
        ('policy',['AirPods Pro 2无理由退货和质量问题申请期限分别是什么？']),
        ('multiturn',['我要申请AirPods Pro 2售后','退货退款','左边耳机没有声音']),
        ('conflict',['我的AirPods Pro 2没有质量问题，但是左边没声音，想申请退货退款']),
        ('uncovered',['无线鼠标左键失灵，想申请退货退款']),
    ]:
        cid=conversation()
        for message in messages:call(label,'http://127.0.0.1:18019/agent/chat',{'message':message,'conversation_id':cid},headers)
    report['after']=counts()
    with con.cursor() as c:
        c.execute('SELECT id,status,current_step,context FROM agent_tasks ORDER BY id');report['tasks']=c.fetchall()
        c.execute('SELECT task_id,trace_id,step_name,status,latency_ms,error_message FROM agent_task_steps ORDER BY id');report['steps']=c.fetchall()
        c.execute('SELECT id,confirmation_id,status FROM agent_pending_actions');report['actions']=c.fetchall()
    assert report['after']['after_sales']==0
    (OUT/'http_results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    con.close()

if __name__=='__main__':run()
