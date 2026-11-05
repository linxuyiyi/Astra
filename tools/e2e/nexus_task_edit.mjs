// 桌面组件交互回归。挂载真实页面与抽屉，API 使用隔离夹具。
import { createRequire } from 'node:module'
import assert from 'node:assert/strict'
import path from 'node:path'

const require = createRequire(import.meta.url)
const { chromium } = require(process.env.PLAYWRIGHT_PATH || path.resolve('frontend/node_modules/playwright'))
const base = process.env.UI_BASE_URL || 'http://127.0.0.1:15179'
const browser = await chromium.launch({ channel: process.env.CHROMIUM_CHANNEL, headless: true })
const page = await browser.newPage({ viewport: { width: 1440, height: 940 }, permissions: ['clipboard-read', 'clipboard-write'] })
page.setDefaultTimeout(7500)
await page.addInitScript(() => {
  localStorage.setItem('nexus_owner_id', 'task-ui-regression')
  document.cookie = 'nexus_uid=task-ui-regression; Path=/; SameSite=Lax'
})
const errors = []
page.on('pageerror', error => errors.push(error.message))
const timestamp = '2026-11-05T00:00:00Z'
const worker = { id: 'wrk', name: '构建 Worker', status: 'WORKING', enabled: true, task_types: ['ISSUE'], category_ids: ['cat'], occupied_tasks: [] }
const descriptions = '任务描述\n'.repeat(100)
const task = (id, status, reason = null) => ({
  id, title: '任务 ' + id, status, waiting_reason: reason, task_type: 'ISSUE', priority: 'MEDIUM', category_id: 'cat',
  description: status === 'RUNNING' ? descriptions : '原始描述 ' + id, current_progress: '',
  acceptance_criteria: [{ id: 'criterion-' + id, content: '验收 ' + id }],
  attachments: [{ name: '说明-' + id + '.txt' }], origin_employee_id: 'e1',
  worker_name: worker.name, agent_worker_id: worker.id, pending_message_id: null,
  current_session_id: status === 'PENDING' ? null : 'ses-' + id,
  workspace: status === 'PENDING' ? null : '/work/' + id, updated_at: timestamp,
})
const tasks = [
  task('A', 'PENDING'), task('B', 'PENDING'), task('run', 'RUNNING'),
  task('question', 'WAITING', 'question'), task('permission', 'WAITING', 'permission'),
  task('confirmation', 'WAITING', 'confirmation'), task('paused', 'WAITING', 'paused'),
  task('canceled', 'CANCELED'), task('done', 'SUCCEEDED'),
  ...Array.from({ length: 13 }, (_, n) => task('extra-' + n, 'PENDING')),
]
let created, saved, lastQuery
const actionRequests = []
await page.route('**/api/**', async route => {
  const request = route.request(), url = new URL(request.url()), method = request.method()
  const parts = url.pathname.split('/')
  if (!url.pathname.startsWith('/api/')) return route.continue()
  let json = []
  let headers
  if (url.pathname === '/api/tasks') {
    if (method === 'POST') {
      created = request.postDataJSON()
      json = { ...task('created', 'PENDING'), ...created }
      tasks.unshift(json)
    } else {
      const q = url.searchParams
      if (q.has('q')) lastQuery = Object.fromEntries(q)
      const filtered = tasks.filter(row => (!q.get('status') || row.status === q.get('status')) &&
        (!q.get('q') || row.title.includes(q.get('q'))))
      const offset = Number(q.get('offset') || 0)
      json = filtered.slice(offset, offset + Number(q.get('limit') || 20))
      headers = { 'X-Total-Count': String(filtered.length) }
    }
  } else if (parts[2] === 'tasks' && parts[3]) {
    const row = tasks.find(item => item.id === parts[3])
    if (method === 'PATCH') {
      saved = request.postDataJSON()
      Object.assign(row, saved)
      json = row
    } else if (parts[4] === 'workspace') {
      json = { task_id: row.id, task_status: row.status, runtime: { session_id: row.current_session_id, workspace_ui: '/fixture-workspace.html' } }
    } else if (parts[4] === 'repo') json = { name: 'nexus-fixture', branch: 'topic/ui', commit: 'cafe123' }
    else if (parts[4] === 'events') json = Array.from({ length: 45 }, (_, id) => ({ id, message: '任务事件 ' + id, created_at: timestamp }))
    else if (method === 'POST') {
      actionRequests.push(parts[4])
      if (parts[4] === 'confirm') row.status = 'SUCCEEDED'
      if (parts[4] === 'stop') row.status = 'CANCELED'
      if (parts[4] === 'restart') Object.assign(row, { status: 'RUNNING', waiting_reason: null, current_session_id: 'ses-restarted' })
      json = row
    } else json = row
  } else if (url.pathname === '/api/workers') json = [worker]
  else if (url.pathname === '/api/task-categories') json = [{ id: 'cat', name: '开发分类' }]
  else if (url.pathname === '/api/v1/employees') json = { items: [{ id: 'e1', name: '员工甲' }] }
  else if (url.pathname === '/api/v1/runtime') json = { agentscope_version: '2.0.10', configured: false, reachable: false }
  else if (url.pathname.includes('metrics')) json = {}
  else if (url.pathname === '/api/library/review-jobs') json = Array.from({ length: 22 }, (_, id) => ({ id: 'rev-' + id, status: 'SUCCEEDED', phase: 'completed', candidate_ids: [] }))
  return route.fulfill({ json, headers })
})
await page.route('**/fixture-workspace.html?*', route => route.fulfill({
  contentType: 'text/html', body: '<h1>原生工作区夹具</h1>',
}))

try {
  await page.goto(base + '/tasks')
  const drawer = page.getByRole('dialog', { name: /创建任务|编辑任务/ })
  const field = label => drawer.locator('.el-form-item').filter({ has: page.locator('.el-form-item__label').filter({ hasText: new RegExp('^' + label + '$') }) })
  const openMenu = async id => page.getByRole('button', { name: '更多操作 任务 ' + id, exact: true }).click()
  const menu = name => page.getByRole('menuitem', { name, exact: true })
  const edit = async id => {
    await openMenu(id)
    await menu('编辑').click()
    await drawer.waitFor()
  }
  const close = async () => {
    await drawer.getByRole('button', { name: '取消', exact: true }).click()
    await drawer.waitFor({ state: 'hidden' })
  }
  const create = async () => {
    await page.getByRole('button', { name: '创建任务', exact: true }).click()
    await drawer.waitFor()
  }
  await edit('A')
  assert.equal(await field('任务标题').locator('input').inputValue(), '任务 A')
  assert.match(await field('执行 Worker').innerText(), /构建 Worker/)
  await drawer.getByText('说明-A.txt', { exact: true }).waitFor()
  assert.equal(await drawer.getByText('有未保存修改').count(), 0)
  await field('任务标题').locator('input').fill('修改后 A')
  await drawer.getByText('有未保存修改').waitFor()
  await drawer.getByRole('button', { name: '取消', exact: true }).click()
  await page.getByRole('button', { name: '继续编辑', exact: true }).click()
  assert.equal(await field('任务标题').locator('input').inputValue(), '修改后 A')
  await drawer.getByRole('button', { name: '保存修改', exact: true }).click()
  await drawer.waitFor({ state: 'hidden' })
  assert.equal(saved.title, '修改后 A')
  assert.equal(saved.description, '原始描述 A')
  assert.equal('agent_worker_id' in saved, false)
  await edit('B')
  assert.equal(await field('任务标题').locator('input').inputValue(), '任务 B')
  await drawer.getByText('说明-B.txt', { exact: true }).waitFor()
  assert.equal(await drawer.getByText('说明-A.txt', { exact: true }).count(), 0)
  await close()

  await create()
  await drawer.getByRole('button', { name: '创建任务', exact: true }).click()
  await drawer.getByText('请输入任务标题', { exact: true }).waitFor()
  await field('任务标题').locator('input').fill('草稿')
  await drawer.locator('input[type=file]').setInputFiles({ name: 'draft.txt', mimeType: 'text/plain', buffer: Buffer.from('draft') })
  await drawer.getByText('有未保存修改').waitFor()
  await drawer.getByRole('button', { name: '取消', exact: true }).click()
  await page.getByRole('button', { name: '放弃并关闭', exact: true }).click()
  await drawer.waitFor({ state: 'hidden' })
  await create()
  assert.equal(await field('任务标题').locator('input').inputValue(), '')
  for (const text of ['draft.txt', '请输入任务标题', '有未保存修改']) assert.equal(await drawer.getByText(text, { exact: true }).count(), 0)
  await field('任务标题').locator('input').fill('实际创建')
  await field('任务描述').locator('textarea').fill('创建请求验收')
  await field('任务分类').locator('.el-select').click()
  await page.getByRole('option', { name: '开发分类', exact: true }).click()
  await drawer.getByRole('button', { name: '创建任务', exact: true }).click()
  await drawer.waitFor({ state: 'hidden' })
  assert.equal(created.title, '实际创建')
  assert.deepEqual(created.attachments, [])

  for (const [id, visible, absent] of [
    ['B', '开始', '确认完成'], ['run', '停止回复', '确认完成'],
    ['question', '打开原生工作区', '确认完成'], ['permission', '打开原生工作区', '确认完成'],
    ['paused', '打开原生工作区', '继续'], ['confirmation', '确认完成', '继续'],
    ['canceled', '重新开始', '确认完成'], ['done', '删除', '重新开始'],
  ]) {
    await openMenu(id)
    await menu(visible).waitFor()
    assert.equal(await menu(absent).count(), 0)
    await page.getByRole('heading', { name: '任务大厅' }).click()
    await menu(visible).waitFor({ state: 'hidden' })
  }
  await page.locator('.el-pager li').filter({ hasText: /^2$/ }).click()
  await page.getByRole('row', { name: /任务 extra-12/ }).waitFor()
  assert.equal(lastQuery.offset, '20')
  await page.locator('.el-select').filter({ has: page.getByRole('combobox', { name: '任务状态' }) }).click()
  await page.getByRole('option', { name: '执行中', exact: true }).click()
  await page.getByRole('textbox', { name: '任务搜索' }).fill('任务 run')
  await page.getByRole('button', { name: '搜索', exact: true }).click()
  await page.getByRole('row', { name: /任务 run/ }).waitFor()
  assert.equal(lastQuery.q, '任务 run')
  assert.equal(lastQuery.status, 'RUNNING')
  assert.equal(lastQuery.offset, '0')
  await openMenu('run')
  await menu('打开原生工作区').click()
  const detail = page.getByTestId('task-detail')
  const frame = detail.getByTitle('OpenCode 任务工作区')
  await frame.waitFor()
  assert.match(await frame.getAttribute('src'), /task=run&session=ses-run/)
  await frame.contentFrame().getByRole('heading', { name: '原生工作区夹具' }).waitFor()
  await detail.getByText('topic/ui', { exact: true }).waitFor()
  await detail.getByRole('button', { name: '复制 ses-run' }).click()
  assert.equal(await page.evaluate(() => navigator.clipboard.readText()), 'ses-run')
  await detail.getByRole('button', { name: '复制 run', exact: true }).click()
  assert.equal(await page.evaluate(() => navigator.clipboard.readText()), 'run')
  const frameHeight = (await frame.boundingBox()).height
  assert.ok(frameHeight > 350)
  await detail.getByRole('tab', { name: '任务详情' }).click()
  const pane = detail.getByRole('tabpanel')
  assert.ok(await pane.evaluate(node => node.scrollHeight > node.clientHeight))
  assert.ok(Math.abs((await pane.boundingBox()).height - frameHeight) < 3)
  assert.ok(await detail.evaluate(node => node.scrollHeight <= node.clientHeight + 2))
  await detail.getByRole('tab', { name: '事件' }).click()
  await detail.getByText('任务事件 0', { exact: true }).waitFor()
  assert.ok(await pane.locator('.el-scrollbar__wrap').evaluate(node => node.scrollHeight > node.clientHeight))
  await detail.getByRole('tab', { name: '经验复盘' }).click()
  await detail.getByText('SUCCEEDED · completed').first().waitFor()
  assert.ok(await pane.evaluate(node => node.scrollHeight > node.clientHeight))
  await page.reload()
  await detail.getByRole('tab', { name: '经验复盘', selected: true }).waitFor()
  await detail.getByText('SUCCEEDED · completed').first().waitFor()
  await openMenu('run')
  await menu('取消任务').click()
  await page.getByRole('dialog').getByRole('button', { name: '确定', exact: true }).click()
  await detail.getByText('已取消', { exact: true }).waitFor()
  await page.goto(base + '/tasks/confirmation')
  await detail.getByRole('button', { name: '确认完成', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '确定', exact: true }).click()
  await detail.getByText('已完成', { exact: true }).waitFor()
  assert.equal(await detail.getByRole('button', { name: '确认完成' }).count(), 0)
  await page.goto(base + '/tasks/canceled')
  await openMenu('canceled')
  await menu('重新开始').click()
  await page.getByRole('dialog').getByRole('button', { name: '确定', exact: true }).click()
  await frame.waitFor()
  await detail.getByRole('button', { name: '复制 ses-restarted' }).waitFor()
  assert.deepEqual(actionRequests, ['stop', 'confirm', 'restart'])
  assert.deepEqual(errors, [])
  console.log('PASS: 创建与编辑抽屉、草稿丢弃、五态操作、三种等待场景、工作区与局部滚动、完成与重新开始')
} catch (error) {
  console.error({ errors, url: page.url(), body: (await page.locator('body').innerText()).slice(0, 1200) })
  throw error
} finally {
  await browser.close()
}
