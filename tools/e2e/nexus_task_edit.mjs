// 隔离浏览器回归：所有 API 均为夹具，不连接或修改真实服务。
import { createRequire } from 'node:module';
import assert from 'node:assert/strict';
import path from 'node:path';
import fs from 'node:fs/promises';
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_PATH || path.resolve('frontend/node_modules/playwright'));
const baseUrl = process.env.UI_BASE_URL || 'http://127.0.0.1:15179';
const outputDir = path.resolve(process.env.UI_EVIDENCE_DIR || '/tmp/nexus-worker-task-evidence');
await fs.mkdir(outputDir, { recursive: true });
(async()=>{
 const browser=await chromium.launch({headless:true}); const page=await browser.newPage({viewport:{width:1440,height:1000}}); await page.addInitScript(()=>localStorage.setItem('nexus_owner_id','regression')); const errors=[]; page.on('pageerror',e=>errors.push(e.message));
 try {
 const worker={id:'w1',name:'员工甲',role:'开发',enabled:true,task_types:['ISSUE'],category_ids:['cat'],execution_environment:{id:1,name:'构建 Worker 01'},status:'IDLE'};
 const tasks=['A','B'].map((x,i)=>({id:'task-'+x,title:'已有任务'+x,description:'完整描述'+x,task_type:'ISSUE',category_id:'cat',priority:i?'LOW':'HIGH',acceptance_criteria:[{id:'c'+x,content:'验收'+x}],attachments:[{name:'说明'+x+'.txt'}],worker_id:'w1',employee_id:'w1',responsible_employee_id:'e1',status:'PENDING',runtime_state:'PENDING',runtime_status_label:'待执行',runtime_detail:'',current_progress:'',updated_at:'2026-10-30T00:00:00Z'}));let saved;
 await page.route('**/api/**', async route=>{const url=new URL(route.request().url());const path=url.pathname;if(!path.startsWith('/api/'))return route.continue();let data=[];
 if(path==='/api/tasks' )data=tasks;
 else if(path.startsWith('/api/tasks/') && route.request().method()==='PATCH'){saved=route.request().postDataJSON();Object.assign(tasks.find(t=>t.id===path.split('/').pop()),saved);data=tasks.find(t=>t.id===path.split('/').pop());}
 else if(path==='/api/agent-workers')data=[worker];
 else if(path==='/api/task-categories')data=[{id:'cat',name:'开发分类'}];
 else if(path==='/api/v1/employees')data={items:[{id:'e1',name:'员工甲',domain:'研发'}]};
 else if(path.includes('metrics'))data={};
 else if(path.includes('stream'))return route.fulfill({status:200,contentType:'text/event-stream',body:''});
 await route.fulfill({json:data}); });
 await page.goto(`${baseUrl}/tasks`);
 async function edit(title){await page.getByRole('button',{name:'更多操作 '+title,exact:true}).click();await page.getByRole('menuitem',{name:'编辑',exact:true}).click(); await page.getByRole('dialog').waitFor();}
 const drawer=page.getByRole('dialog');
 const field=label=>drawer.locator('.el-form-item').filter({has:page.locator('.el-form-item__label').filter({hasText:new RegExp('^'+label+'$')})});
 await edit('已有任务A');
 assert.equal(await field('任务标题').locator('input').inputValue(),'已有任务A');
 assert.equal(await field('任务描述').locator('textarea').inputValue(),'完整描述A');
 assert.equal(await field('验收标准 1').locator('input').inputValue(),'验收A');
 assert.match(await field('执行 Worker').innerText(),/构建 Worker 01/);
 assert.match(await field('责任数字员工').innerText(),/员工甲/);
 assert.match(await drawer.innerText(),/说明A.txt/);
 assert.equal(await drawer.getByText('有未保存修改').count(),0);
 assert.equal(await field('任务分类').locator('.el-select__wrapper.is-disabled').count(),1);
 assert.equal(await field('任务类型').getByRole('radio', {name:'问题',exact:true}).isDisabled(),true);
 await drawer.screenshot({path:path.join(outputDir, 'task-edit-filled.png')});
 await field('任务标题').locator('input').fill('已有任务A已修改');
 await drawer.getByRole('button',{name:'保存修改'}).click();await drawer.waitFor({state:'hidden'});
 assert.equal(saved.title,'已有任务A已修改');assert.equal(saved.description,'完整描述A');assert.equal(saved.acceptance_criteria[0].content,'验收A');assert.equal(saved.employee_id,'w1');
 await edit('已有任务B');assert.equal(await field('任务标题').locator('input').inputValue(),'已有任务B');assert.equal(await field('任务描述').locator('textarea').inputValue(),'完整描述B');
 await drawer.getByRole('button',{name:'取消',exact:true}).click();await drawer.waitFor({state:'hidden'});
 await page.getByTestId('task-create').click();await drawer.waitFor();assert.equal(await field('任务标题').locator('input').inputValue(),'');assert.equal(await field('任务描述').locator('textarea').inputValue(),'');
 await drawer.getByRole('button',{name:'取消',exact:true}).click();await drawer.waitFor({state:'hidden'});
 await edit('已有任务A已修改');assert.equal(await field('任务标题').locator('input').inputValue(),'已有任务A已修改');
 assert.deepEqual(errors,[]); console.log('PASS: edit prefill, Worker/owner labels, attachments, clean baseline, save payload, switch tasks, create reset, reopen saved task');
 } finally { await browser.close(); }
})().catch(e=>{console.error(e);process.exit(1)});
