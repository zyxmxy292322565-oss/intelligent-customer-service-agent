"""Additional isolated category gate and source hash verification."""
import sys
sys.path.insert(0,str(__import__('pathlib').Path(__file__).resolve().parents[1]))
from scripts.r6h_isolated_acceptance import *
import pymysql

if __name__=='__main__':
    info=json.loads(docker('inspect','phase5a-mysql-20261015'))[0]
    password=dict(v.split('=',1) for v in info['Config']['Env'] if '=' in v)['MYSQL_ROOT_PASSWORD']
    con=pymysql.connect(host='127.0.0.1',port=3312,user='root',password=password,database=DB,charset='utf8mb4')
    with con.cursor() as c:
        c.execute("INSERT INTO orders(order_no,user_id,total_amount,status,delivered_at) VALUES ('R6H-MOUSE',1,199,'delivered',DATE_SUB(NOW(),INTERVAL 5 DAY))")
        c.execute("INSERT INTO order_items(order_id,product_name,category,quantity,unit_price) VALUES (%s,'无线鼠标','鼠标',1,199)",(c.lastrowid,))
    con.commit();con.close()
    base='http://127.0.0.1:18020'
    token=requests.post(base+'/auth/login',json={'user_id':1}).json()['access_token']
    headers={'Authorization':'Bearer '+token}
    cid=requests.post(base+'/agent/conversations',headers=headers).json()['data']['id']
    started=time.monotonic()
    response=requests.post(base+'/agent/chat',headers=headers,json={'conversation_id':cid,'message':'无线鼠标左键失灵，申请退货退款'},timeout=90)
    result={'elapsed_seconds':round(time.monotonic()-started,3),'conversation_id':cid,'http_status':response.status_code,'response':response.json()}
    report=json.loads((OUT/'http_results.json').read_text(encoding='utf-8'))
    checks=[]
    for row in report['requests']:
        body=row['response'];evidence=body.get('evidence') or (body.get('data') or {}).get('evidence') or []
        for e in evidence:
            path=RAG/'policy_docs'/e['source']
            checks.append({'source':e['source'],'match':path.is_file() and hashlib.md5(path.read_bytes()).hexdigest()==e.get('file_hash'),'version':e.get('version')})
    result['source_checks']=checks
    (OUT/'category_and_hashes.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(result['response']['code'],result['elapsed_seconds'],'hashes',len(checks),'all_match',all(c['match'] for c in checks))
