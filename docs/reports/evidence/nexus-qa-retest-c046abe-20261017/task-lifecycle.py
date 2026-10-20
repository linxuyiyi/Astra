import json,sys,time,subprocess,sqlite3
from pathlib import Path
import httpx
r=Path(__file__).resolve().parents[2];q=Path(__file__).resolve().parent;sys.path.insert(0,str(r/'services/nexus-api/tests'))
from test_live_agentscope import collect_events,deltas,tool_names
employee=json.loads((r/'.astra/qa-20261016/qa-employee.json').read_text());owner={'X-Owner-Id':'qa-c046abe-20261017'};title='QA-c046abe-完整任务闭环-'+str(int(time.time()));filename='qa-c046abe.txt';content='NEXUS_PHASE2_C046ABE_OK';out={'title':title}
with httpx.Client(base_url='http://127.0.0.1:8120',timeout=60) as n,httpx.Client(base_url='http://127.0.0.1:8100',timeout=90) as t:
 conv=n.post('/api/v1/employees/'+employee['id']+'/conversations',headers=owner,json={'name':'c046abe 完整任务验收'}).json();out['conversation_id']=conv['id'];print('conversation ready',flush=True)
 prompt=f'请实际使用 nexus_task_create 创建且仅创建一个任务，并使用 nexus_task_start 启动。task_type=ISSUE，category_id=testing-platform，worker_id=astra-01，title={title}，description=在当前工程根目录创建 {filename}，内容必须逐字为 {content}，只修改这个文件，不提交 Git。acceptance_criteria=[指定文件存在且内容正确,没有新增 Git 提交]。启动后查询状态并汇报编号。不要自行确认完成，不重试、不创建第二个任务。'
 res=n.post('/api/v1/conversations/'+conv['id']+'/messages',headers=owner,json={'text':prompt});res.raise_for_status();events=collect_events('http://127.0.0.1:8120/api/v1/conversations/'+conv['id']+'/stream',owner,None,120);out['creation_tools']=tool_names(events);out['creation_reply']=deltas(events)
 tasks=[x for x in t.get('/api/tasks').json() if x['title']==title];assert len(tasks)==1,tasks;task=tasks[0];out['task_id']=task['id'];print('real MCP created',task['id'],task['status'],flush=True)
 assert task['responsible_employee_id']==employee['id'];assert task['source_conversation_id']==conv['id'];assert task['source_session_id'];out['responsibility_and_source_correct']=True
 observations=[]
 for _ in range(60):
  cur=t.post('/api/tasks/'+task['id']+'/reconnect');cur.raise_for_status();cur=cur.json();binding=t.get('/api/tasks/'+task['id']+'/workspace').json();current=next((x for x in binding.get('sessions',[]) if x.get('current')),{});observations.append({'task_status':cur['status'],'runtime_state':cur['runtime_state'],'progress':cur['current_progress'],'binding_status':current.get('status')})
  if current.get('status')=='SUCCEEDED':break
  if cur['status'] in ['FAILED','CANCELED']:raise RuntimeError(str(observations[-1]))
  time.sleep(2)
 else:raise RuntimeError('native execution did not finish in allotted polling period')
 ws=Path(cur['workspace']);actual=(ws/filename).read_text();assert actual==content,actual
 head=subprocess.check_output(['git','-C',str(ws),'rev-parse','HEAD'],text=True).strip();base=subprocess.check_output(['git','-C',str(r/'.astra/qa-20261016/workspaces'),'rev-parse','HEAD'],text=True).strip();assert head==base
 out['observations']=observations;out['artifact']={'path':str(ws/filename),'content':actual,'git_head_unchanged':True};print('OpenCode finished; artifact and no-commit verified',flush=True)
 confirmed=t.post('/api/tasks/'+task['id']+'/confirm');confirmed.raise_for_status();assert confirmed.json()['status']=='SUCCEEDED';out['confirmed_status']='SUCCEEDED'
 for _ in range(40):
  with sqlite3.connect(r/'services/task-platform/data/astra.db') as db:rows=db.execute('SELECT event_id,status,attempts,last_error FROM task_wake_outbox WHERE task_id=?',(task['id'],)).fetchall()
  if rows and all(x[1]=='delivered' for x in rows):break
  time.sleep(1)
 else:raise RuntimeError('outbox not delivered: '+str(rows))
 out['outbox']=rows
 for _ in range(40):
  payload=n.get('/api/v1/conversations/'+conv['id'],headers=owner).json();messages=payload.get('messages',[]);tail=[x for x in messages if x.get('sender')==employee['id'] and task['id'] in x.get('text','') and 'SUCCEEDED' in x.get('text','')]
  if tail:out['employee_final_report']=tail[-1]['text'];break
  time.sleep(1)
 else:raise RuntimeError('no responsible-employee completion report')
 out['passed']=True;(q/'task-lifecycle-proof.json').write_text(json.dumps(out,ensure_ascii=False,indent=2));print(json.dumps({'passed':True,'task_id':task['id'],'outbox':rows,'employee_report':out['employee_final_report']},ensure_ascii=False),flush=True)
