from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
import json,time,threading
states={};lock=threading.Lock()
now='2026-10-28T02:00:00Z'
class H(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def respond(self,x,code=200):
  raw=json.dumps(x,ensure_ascii=False).encode();self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
 def do_GET(self):
  p=self.path.split('?')[0]
  if p=='/api/v1/employees':return self.respond({'items':[{'id':'qa-employee','name':'输入框时序测试员工','enabled':True,'agentscope_agent_id':'qa-agent'}]})
  if p.startswith('/api/v1/conversations/'):
   cid=p.split('/')[4];s=states.setdefault(cid,{'accepted':False,'text':'','sending':False})
   if p.endswith('/messages'):
    if s['accepted'] and cid.startswith('first'):time.sleep(1)
    return self.respond({'messages':([{'id':'msg-'+cid,'name':'测试用户','role':'user','content':[{'id':'txt-'+cid,'type':'text','text':s['text'],'created_at':now}],'created_at':now}] if s['accepted'] else []),'event_cursor':'','is_running':s['sending']})
   if p.endswith('/stream'):
    self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Cache-Control','no-cache');self.end_headers()
    try:
     for i in range(120):
      self.wfile.write(b': keepalive\n\n');self.wfile.flush();time.sleep(.5)
    except (BrokenPipeError,ConnectionResetError):pass
    return
   return self.respond({'id':cid,'employee_id':'qa-employee','agentscope_session_id':('qa-session-'+cid if not cid.startswith('first') or s['accepted'] else '')})
  if p=='/qa/state':return self.respond(states)
  return self.respond({'detail':'未实现的测试接口'},404)
 def do_POST(self):
  p=self.path;body=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))) or b'{}')
  if p.endswith('/messages'):
   cid=p.split('/')[4];s=states.setdefault(cid,{'accepted':False,'text':'','sending':False});s['text']=body.get('text','');s['sending']=True
   if cid.startswith('slow'):time.sleep(8)
   else:time.sleep(.2)
   if cid.startswith('reject'):s['sending']=False;return self.respond({'detail':'测试拒绝：员工正在回复，请稍后重试'},409)
   s['accepted']=True
   self.respond({'message':{'id':'msg-'+cid},'agentscope':{'accepted':True,'session_id':'qa-session-'+cid}})
   # Keep the simulated reply running until an explicit test control call.
   return
  if p=='/qa/end':
   for s in states.values():s['sending']=False
   return self.respond({'ok':True})
  return self.respond({'ok':True})
ThreadingHTTPServer(('127.0.0.1',18845),H).serve_forever()
