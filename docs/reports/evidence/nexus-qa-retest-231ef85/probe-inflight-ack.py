import asyncio,json,sys,os,tempfile
from pathlib import Path
import httpx
from fastapi import Request
REPO=next(p for p in Path(__file__).resolve().parents if (p/'.git').exists());sys.path.insert(0,str(REPO/'services/agent-service'))
import agent_service as module
import uvicorn
# 只在此隔离测试进程补全 Request 名称，以独立验证去重逻辑；不改源码或运行中的服务。
module.Request=Request
captured={};uvicorn.run=lambda app,**kwargs:captured.update(app=app)
os.environ['AGENTSCOPE_WORKSPACE_DIR']=tempfile.mkdtemp(prefix='nexus-qa-inflight-')
module.main()
async def run():
 started=asyncio.Event();release=asyncio.Event();calls=[]
 async def rejected_delivery(*a,**kw):
  calls.append('attempt');started.set();await release.wait();raise OSError('inbox unavailable')
 module.deliver_task_hint=rejected_delivery
 body={'agent_id':'probe-agent','session_id':'probe-session','user_id':'nexus','hint':'probe','event_id':'unique-inflight-qa-231ef85'}
 transport=httpx.ASGITransport(app=captured['app'],raise_app_exceptions=False)
 async with httpx.AsyncClient(transport=transport,base_url='http://probe',headers={'X-User-ID':'nexus'}) as client:
  first=asyncio.create_task(client.post('/nexus/task-hint',json=body));await asyncio.wait_for(started.wait(),5)
  second=await client.post('/nexus/task-hint',json=body);release.set();failed=await first
  out={'first_http':failed.status_code,'second_http':second.status_code,'second_body':second.json(),'inbox_attempts':len(calls),'inbox_accepted':0,'test_scope':'isolated ASGI; injected global Request only to bypass independent route annotation defect'}
  print(json.dumps(out,indent=2));(REPO/'.astra/qa-retest-231ef85/inflight-ack-proof.json').write_text(json.dumps(out,indent=2))
asyncio.run(run())
