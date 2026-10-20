import asyncio,json,sys,tempfile
from datetime import UTC,datetime
from pathlib import Path
REPO = next(p for p in Path(__file__).resolve().parents if (p / '.git').exists())
sys.path.insert(0, str(REPO / 'services/task-platform'))
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
from sqlalchemy import select
from app.models import Base,Task,TaskWakeOutbox
from app.task_tools import TaskTools,nexus_employee_id
from app.runtime.task_wakeup import deliver_pending

async def run():
 with tempfile.TemporaryDirectory() as d:
  engine=create_async_engine('sqlite+aiosqlite:///'+d+'/probe.db')
  async with engine.begin() as c: await c.run_sync(Base.metadata.create_all)
  factory=async_sessionmaker(engine,expire_on_commit=False)
  async with factory() as s:
   s.add(Task(id='QA-OWNED',task_type='ISSUE',category_id='testing-platform',title='employee A task',description='probe',status='PENDING',responsible_employee_id='employee-A'))
   await s.commit()
   token=nexus_employee_id.set('employee-B')
   got=await TaskTools(s).get('QA-OWNED'); changed=await TaskTools(s).update('QA-OWNED',title='employee B changed A')
   nexus_employee_id.reset(token)
   missing=await TaskTools(s).list()
   ownership={'other_employee_get_ok':got['ok'],'other_employee_update_ok':changed['ok'],'new_title':changed['title'],'anonymous_list_count':len(missing['tasks'])}
   now=datetime.now(UTC)
   row=TaskWakeOutbox(event_id=1,task_id='QA-OWNED',responsible_employee_id='employee-A',source_session_id='session-A',status='pending',hint='probe',attempts=0,next_retry_at=now,created_at=now,updated_at=now)
   s.add(row);await s.commit()
  calls=[]; gate=asyncio.Event()
  class Sender:
   async def send(self,row):
    calls.append(row.id)
    if len(calls)==2:gate.set()
    await asyncio.wait_for(gate.wait(),5)
  async def send_once():
   async with factory() as s:return await deliver_pending(s,Sender())
  outcomes=await asyncio.gather(send_once(),send_once(),return_exceptions=True)
  duplicates={'same_outbox_send_count':len(calls),'outcomes':[str(x) for x in outcomes]}
  async with factory() as s:
   row=await s.scalar(select(TaskWakeOutbox));row.status='pending';row.attempts=29;await s.commit()
   class Failing:
    async def send(self,row):raise OSError('temporarily disconnected')
   await deliver_pending(s,Failing());status=row.status;attempts=row.attempts
   class Restored:
    async def send(self,row):raise AssertionError('should show whether retried')
   after=await deliver_pending(s,Restored())
   recovery={'status_after_outage':status,'attempts':attempts,'delivered_after_service_restored':after}
  await engine.dispose()
  print(json.dumps({'ownership':ownership,'duplicate_delivery':duplicates,'outage_recovery':recovery},indent=2))
asyncio.run(run())
