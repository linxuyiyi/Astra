from pathlib import Path
import subprocess,json,socket,psutil,time
r=Path(__file__).resolve().parents[2];q=Path(__file__).resolve().parent;ports=[8000,8100,8110,8120,8201,8202,5174,4096]
def listeners():
 out={}
 for port in ports:
  with socket.socket() as s:s.settimeout(.2);out[str(port)]=s.connect_ex(('127.0.0.1',port))==0
 return out
def command(service,action,stage):
 p=subprocess.run(['bash',str(r/'ops/dev'/('start_'+service+'.sh')),action],cwd=r,start_new_session=True,capture_output=True,text=True,timeout=150);(q/(stage+'-'+service+'.log')).write_text(p.stdout+p.stderr);print(stage,service,p.returncode,flush=True)
 if p.returncode:raise RuntimeError(stage+' '+service+' failed')
def snapshot():
 result={}
 for name,file in [('astra','astra-service.json'),('single','service-ctl.json')]:
  p=r/'.astra/run'/file;result[name]=json.loads(p.read_text()) if p.exists() else {}
 return result
out={'initial_ports':listeners()}
if any(out['initial_ports'].values()):raise RuntimeError('expected clean ports before new manager start')
for name in ['agent_service','nexus_api','astra']:command(name,'start','first-start')
out['first_ports']=listeners();out['first_state']=snapshot();assert all(out['first_ports'].values())
first_records=[rec for state in out['first_state'].values() for rec in state.get('processes',{}).values()];assert first_records and all(rec.get('create_time') is not None for rec in first_records)
for name in ['astra','nexus_api','agent_service']:command(name,'stop','first-stop')
out['stopped_ports']=listeners();out['stopped_state']=snapshot();out['old_live_pids']=[]
for rec in first_records:
 try:
  p=psutil.Process(rec['pid'])
  if p.status()!=psutil.STATUS_ZOMBIE and abs(p.create_time()-float(rec['create_time']))<.01:out['old_live_pids'].append(rec['pid'])
 except psutil.NoSuchProcess:pass
assert not any(out['stopped_ports'].values()),out['stopped_ports'];assert not out['old_live_pids']
for name in ['agent_service','nexus_api','astra']:command(name,'start','second-start')
out['second_ports']=listeners();out['second_state']=snapshot();assert all(out['second_ports'].values());out['passed']=True
(q/'lifecycle-proof.json').write_text(json.dumps(out,ensure_ascii=False,indent=2));print('Lifecycle PASS: start -> stop -> all owned ports released -> start; all records have create_time',flush=True)
