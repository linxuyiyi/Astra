import asyncio,json,tempfile,sys
from pathlib import Path
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
REPO=next(p for p in Path(__file__).resolve().parents if (p/'.git').exists());sys.path.insert(0,str(REPO/'services/task-platform'))
from app.models import Base,Task,DigitalEmployee,OpenCodeExecutionEnvironment
from app.runtime.environment import ExecutionEnvironmentRuntime
from app.task_tools import TaskTools,nexus_employee_id
async def run():
 root=str(REPO/'.astra/qa-20261016/workspaces');env=OpenCodeExecutionEnvironment(id=999,employee_id='qa-runtime-probe',mode='LOCAL',name='QA Runtime collision',opencode_path=str(REPO/'.astra/qa-20261016/runtime/node_modules/.bin/opencode'),opencode_server_port=14096,workspace_root=root,status='AVAILABLE')
 checks=await ExecutionEnvironmentRuntime().test(env);connectivity=[{'name':x.name,'passed':x.passed,'detail':x.detail} for x in checks]
 with tempfile.TemporaryDirectory() as d:
  engine=create_async_engine('sqlite+aiosqlite:///'+d+'/isolated.db')
  async with engine.begin() as c:await c.run_sync(Base.metadata.create_all)
  factory=async_sessionmaker(engine,expire_on_commit=False)
  async with factory() as s:
   w=DigitalEmployee(id='qa-runtime-probe',name='QA Runtime',role='QA',enabled=True);w.execution_environment=env;s.add(w);s.add(Task(id='QA-RETEST-RUNTIME',task_type='ISSUE',category_id='testing-platform',title='QA Runtime error',description='Do not execute.',status='PENDING',responsible_employee_id='qa-owner',employee_id=w.id,acceptance_criteria=[]));await s.commit()
   token=nexus_employee_id.set('qa-owner')
   try:result=await TaskTools(s).start('QA-RETEST-RUNTIME')
   finally:nexus_employee_id.reset(token)
  await engine.dispose()
 out={'serve_identity_checks':connectivity,'structured_start_failure':result};print(json.dumps(out,ensure_ascii=False,indent=2));(REPO/'.astra/qa-retest-231ef85/runtime-proof.json').write_text(json.dumps(out,ensure_ascii=False,indent=2))
asyncio.run(run())
