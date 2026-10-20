import asyncio,json,sys,tempfile
from pathlib import Path
from datetime import UTC,datetime
import httpx
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
REPO=next(p for p in Path(__file__).resolve().parents if (p/'.git').exists());sys.path.insert(0,str(REPO/'services/task-platform'))
from app.models import Base,TaskWakeOutbox
from app.runtime.task_wakeup import deliver_pending,HttpWakeSender
from sqlalchemy import select
async def run():
 e=json.loads((REPO/'.astra/qa-20261016/qa-employee.json').read_text())
 with tempfile.TemporaryDirectory() as d:
  engine=create_async_engine('sqlite+aiosqlite:///'+d+'/isolated.db')
  async with engine.begin() as c:await c.run_sync(Base.metadata.create_all)
  factory=async_sessionmaker(engine,expire_on_commit=False)
  async with factory() as s:
   now=datetime.now(UTC);row=TaskWakeOutbox(event_id=999876,task_id='QA-WAKE-CONTRACT',responsible_employee_id=e['id'],source_session_id=e['mailbox_session_id'],status='pending',hint='接口协议验证，不执行工程动作。',attempts=0,next_retry_at=now,created_at=now,updated_at=now);s.add(row);await s.commit()
   delivered=await deliver_pending(s,HttpWakeSender());await s.refresh(row);out={'delivered':delivered,'status':row.status,'attempts':row.attempts,'last_error':row.last_error}
   print(json.dumps(out,ensure_ascii=False,indent=2));(REPO/'.astra/qa-retest-231ef85/wake-sender-end-to-end.json').write_text(json.dumps(out,ensure_ascii=False,indent=2))
  await engine.dispose()
asyncio.run(run())
