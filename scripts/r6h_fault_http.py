"""Controlled HTTP fault injection; not a real upstream model response."""
import sys
sys.path.insert(0,str(__import__('pathlib').Path(__file__).resolve().parents[1]))
from scripts.r6h_isolated_acceptance import *
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import threading

if __name__=='__main__':
    os.environ['NO_PROXY']='127.0.0.1,localhost'
    os.environ['MYSQL_DATABASE']=DB
    from services import policy_rag_client as client
    class Handler(BaseHTTPRequestHandler):
        mode='invalid'
        def log_message(self,*args):pass
        def do_POST(self):
            if self.mode=='connection':
                self.connection.close();self.close_connection=True;return
            if self.mode=='timeout':time.sleep(0.4)
            self.send_response(200);self.end_headers()
            try:self.wfile.write(b'{"success":true,"evidence":"invalid"}')
            except OSError:pass
    server=ThreadingHTTPServer(('127.0.0.1',18021),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    results=[]
    for mode,url in [('invalid','http://127.0.0.1:18021'),('timeout','http://127.0.0.1:18021'),('connection','http://127.0.0.1:18021')]:
        Handler.mode=mode;client.POLICY_RAG_SERVICE_URL=url;client.POLICY_RAG_TIMEOUT_SECONDS=2 if mode=='connection' else 0.1
        start=time.monotonic();result=client.query_policy_rag('隔离故障分类')
        results.append({'mode':mode,'elapsed_seconds':round(time.monotonic()-start,3),'code':result.code})
    server.shutdown()
    (OUT/'fault_http.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
    print(json.dumps(results))
