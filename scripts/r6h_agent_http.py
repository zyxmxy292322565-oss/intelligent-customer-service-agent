"""Continue HTTP acceptance against already initialized R6H services."""
import sys
sys.path.insert(0,str(__import__('pathlib').Path(__file__).resolve().parents[1]))
from scripts.r6h_isolated_acceptance import *
import pymysql

BASE=os.getenv('R6H_BACKEND_URL','http://127.0.0.1:18020')

if __name__=='__main__':
    info=json.loads(docker('inspect','phase5a-mysql-20261015'))[0]
    password=dict(v.split('=',1) for v in info['Config']['Env'] if '=' in v)['MYSQL_ROOT_PASSWORD']
    con=pymysql.connect(host='127.0.0.1',port=3312,user='root',password=password,database=DB,charset='utf8mb4',cursorclass=pymysql.cursors.DictCursor)
    with con.cursor() as c:c.execute('SELECT @@server_uuid AS uuid');assert c.fetchone()['uuid']==UUID
    report=json.loads((OUT/'http_results.json').read_text(encoding='utf-8'))
    tables=['users','orders','order_items','after_sales','agent_tasks','agent_task_steps','agent_pending_actions','conversations','conversation_messages','agent_action_receipts']
    def snapshot():
        con.commit();result={}
        with con.cursor() as c:
            for table in tables:c.execute('SELECT COUNT(*) AS n FROM '+table);result[table]=c.fetchone()['n']
        return result
    report['before']=snapshot()
    token=requests.post(BASE+'/auth/login',json={'user_id':1},timeout=5).json()['access_token']
    headers={'Authorization':'Bearer '+token}
    for label,messages in [
        ('policy',['AirPods Pro 2无理由退货和质量问题申请期限分别是什么？']),
        ('multiturn',['我要申请AirPods Pro 2售后','退货退款','左边耳机没有声音']),
        ('conflict',['我的AirPods Pro 2没有质量问题，但是左边没声音，想申请退货退款']),
        ('uncovered',['无线鼠标左键失灵，想申请退货退款']),
    ]:
        response=requests.post(BASE+'/agent/conversations',headers=headers,timeout=5)
        response.raise_for_status();cid=response.json()['data']['id']
        for message in messages:
            started=time.monotonic()
            try:
                r=requests.post(BASE+'/agent/chat',headers=headers,json={'message':message,'conversation_id':cid},timeout=100)
                data=r.json();status=r.status_code
            except Exception as exc:data={'exception':type(exc).__name__};status=None
            row={'label':label,'conversation_id':cid,'input':message,'http_status':status,'elapsed_seconds':round(time.monotonic()-started,3),'response':data}
            report['requests'].append(row)
            (OUT/'http_results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
            print(label,cid,status,row['elapsed_seconds'],data.get('code'),flush=True)
    report['after']=snapshot()
    with con.cursor() as c:
        c.execute('SELECT id,status,current_step,context FROM agent_tasks ORDER BY id');report['tasks']=c.fetchall()
        c.execute('SELECT task_id,trace_id,step_name,status,latency_ms,error_message FROM agent_task_steps ORDER BY id');report['steps']=c.fetchall()
        c.execute('SELECT id,confirmation_id,status FROM agent_pending_actions');report['actions']=c.fetchall()
    assert report['after']['after_sales']==0
    (OUT/'http_results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    con.close()
