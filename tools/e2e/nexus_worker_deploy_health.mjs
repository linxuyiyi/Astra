// 桌面组件交互回归。隔离夹具验证诊断、刷新、运维分页和日志，不操作真实 Worker。
import { createRequire } from 'node:module'
import assert from 'node:assert/strict'
import path from 'node:path'

const require = createRequire(import.meta.url)
const { chromium } = require(process.env.PLAYWRIGHT_PATH || path.resolve('frontend/node_modules/playwright'))
const base = process.env.UI_BASE_URL || 'http://127.0.0.1:15189'
const browser = await chromium.launch({ channel: process.env.CHROMIUM_CHANNEL, headless: true })
const page = await browser.newPage({ viewport: { width: 1440, height: 960 } })
page.setDefaultTimeout(7500)
await page.clock.install({ time: new Date('2026-11-05T00:00:00Z') })
await page.addInitScript(() => {
  localStorage.setItem('nexus_owner_id', 'worker-ui-regression')
  document.cookie = 'nexus_uid=worker-ui-regression; Path=/; SameSite=Lax'
})
const errors = []
page.on('pageerror', error => errors.push(error.message))
const timestamp = '2026-11-05T00:00:00Z'
const worker = {
  id: 'w1', name: '构建 Worker 01', description: '', enabled: false,
  task_types: ['ISSUE'], category_ids: [], status: 'OFFLINE', runtime_engine: 'opencode', mode: 'LINUX_REMOTE',
  host: 'fixture.invalid', port: 22, username: 'nexus', workspace_root: '/srv/project', model_name: 'route-test',
  repository_url: 'https://git.fixture/project', branch: 'main', skill_refs: [], mcp_refs: [],
  online: false, has_ssh_password: true, has_git_token: false, epoch: 3, last_heartbeat_at: timestamp, agent_version: '1.5.0',
  created_at: timestamp, updated_at: timestamp,
  occupied_tasks: [{ id: 'T-A', title: '研发占用任务' }, { id: 'T-B', title: '等待验收任务' }],
}
const workers = [worker, ...['OFFLINE', 'IDLE', 'WORKING', 'PAUSED'].flatMap(status =>
  [true, false].map(enabled => ({ ...worker, id: status + enabled, name: status + '-' + enabled, status, enabled, occupied_tasks: [] }))),
  ...Array.from({ length: 15 }, (_, n) => ({ ...worker, id: 'extra-' + n, name: '额外 Worker ' + n, occupied_tasks: [] }))]
const occupiedTask = {
  id: 'T-A', title: '研发占用任务', status: 'RUNNING', waiting_reason: null,
  worker_name: worker.name, agent_worker_id: worker.id, current_session_id: null, workspace: null,
  attachments: [], acceptance_criteria: [], updated_at: timestamp, description: '真实组件夹具',
}
const equipment = (key, label, state, fields = {}) => ({ key, label, state, diff: [], ...fields })
let reviewActive = false
const health = () => ({
  agent_worker_id: worker.id, enabled: worker.enabled, readiness: worker.status === 'OFFLINE' ? 'UNKNOWN' : 'DEGRADED',
  connection: { state: worker.status === 'OFFLINE' ? 'offline' : 'online' },
  blocking: [
    ...(worker.occupied_tasks.length ? [{ code: 'occupied_task', message: 'Worker 有未释放任务' }] : []),
    ...(history.some(row => row.stage === 'stop_unconfirmed') ? [{ code: 'maintenance_active', message: 'Worker 正在维护' }] : []),
    ...(reviewActive ? [{ code: 'review_active', message: 'Worker 仍是未完成经验复盘的来源' }] : []),
  ],
  runtime_health: { equipment: [
    equipment('worker_agent', 'Worker Agent', worker.status === 'OFFLINE' ? 'unknown' : 'ready', { actual_revision: '1.5.0' }),
    equipment('opencode', 'OpenCode', worker.status === 'OFFLINE' ? 'unknown' : 'ready', { actual_revision: worker.status === 'OFFLINE' ? null : '1.18.35' }),
    equipment('model', '模型', 'ready', { detail: '生效模型 route-test' }),
    equipment('mcp', 'MCP', 'degraded', { diff: ['MCP 调用超时'], error: 'MCP 调用超时' }),
    equipment('skills', 'Skill', 'drifted', { diff: ['Skill qa 文件缺失'] }),
    equipment('git', 'Git 仓库', 'ready'),
  ] },
  maintenance: history.find(row => row.state === 'RUNNING') ?? null,
  repository: { authentication_verified: true, url: worker.repository_url, branch: 'main', commit: 'abcdef0123456789', dirty: true },
})
const operation = (id, action, state, fields = {}) => ({
  id, agent_worker_id: worker.id, action, state, stage: 'completed', attempt: 1, requested_at: timestamp,
  error_message: null, ...fields,
})
const history = [
  operation('op-expired', 'deploy', 'FAILED', { stage: 'stop_unconfirmed', error_message: '等待确认远端已停止' }),
  ...Array.from({ length: 12 }, (_, n) => operation('op-check-' + n, 'check', 'SUCCEEDED')),
]
let lastWorkerQuery, lastMaintenanceQuery, deployed = false, streams = 0
let workerReads = 0, healthReads = 0, failHealth = false
let historyBefore
let holdHealth
await page.route('**/api/**', async route => {
  const req = route.request(), url = new URL(req.url()), pathname = url.pathname, q = url.searchParams
  if (!url.pathname.startsWith('/api/')) return route.continue()
  let json = [], headers
  if (pathname === '/api/workers') {
    if (q.has('q')) lastWorkerQuery = Object.fromEntries(q)
    const rows = workers.filter(row => (!q.get('status') || row.status === q.get('status')) &&
      (!q.get('q') || row.name.includes(q.get('q'))))
    const offset = Number(q.get('offset') || 0)
    json = rows.slice(offset, offset + Number(q.get('limit') || 200))
    headers = { 'X-Total-Count': String(rows.length) }
  } else if (pathname === '/api/workers/w1') {
    if (req.method() === 'PUT') {
      assert.equal('id' in req.postDataJSON(), false)
      Object.assign(worker, req.postDataJSON())
    } else workerReads++
    json = worker
  } else if (pathname === '/api/workers/w1/pause' || pathname === '/api/workers/w1/resume') {
    worker.status = pathname.endsWith('/pause') ? 'PAUSED' : (worker.occupied_tasks.length ? 'WORKING' : 'IDLE')
    json = worker
  } else if (pathname === '/api/workers/w1/health') {
    healthReads++
    if (holdHealth) await holdHealth
    if (failHealth) return route.fulfill({ status: 503, json: { detail: '诊断暂不可用' } })
    json = health()
  } else if (pathname === '/api/workers/w1/maintenance') {
    if (req.method() === 'POST') {
      assert.equal(req.postDataJSON().action, 'deploy')
      json = operation('op-deploy', 'deploy', 'QUEUED', { stage: 'queued' })
      history.unshift(json)
      deployed = true
    } else {
      lastMaintenanceQuery = Object.fromEntries(q)
      const rows = history.filter(row => (!q.get('state') || row.state === q.get('state')) && (!q.get('action') || row.action === q.get('action')))
      const offset = Number(q.get('offset'))
      json = { items: rows.slice(offset, offset + Number(q.get('limit'))), total: rows.length }
    }
  } else if (pathname.startsWith('/api/maintenance/')) {
    const [, , , id, action] = pathname.split('/')
    const row = history.find(item => item.id === id)
    if (action === 'events' && !pathname.endsWith('/page')) {
      if (id === 'op-deploy') streams++
      const frames = Array.from({ length: 1050 }, (_, n) => ({
        id: n + 1, name: '部署日志', message: '日志 ' + (n + 1), attempt: 1, type: 'step', state: 'RUNNING',
      }))
      frames.at(-1).message = '当前连接代次验证通过'
      if (id === 'op-deploy') Object.assign(row, { state: 'SUCCEEDED', stage: 'completed' })
      const after = Number(q.get('after_id'))
      const selected = id !== 'op-deploy' ? [] : after === 0 ? frames : [
        frames.at(-1), { ...frames.at(-1), id: 1051, message: '续订追加日志' },
      ]
      const body = selected.map(frame =>
        'id: ' + frame.id + '\nevent: message\ndata: ' + JSON.stringify({ ...frame, created_at: timestamp }) + '\n\n').join('')
      return route.fulfill({ contentType: 'text/event-stream', body: body + (id === 'op-deploy' && after === 0 ? '' : 'event: end\ndata: {}\n\n') })
    }
    if (action === 'events' && pathname.endsWith('/page')) {
      historyBefore = Number(q.get('before_id'))
      const items = Array.from({ length: 200 }, (_, n) => ({
        id: historyBefore - 200 + n, message: '历史日志 ' + n, attempt: 1, type: 'step', created_at: timestamp,
      }))
      return route.fulfill({ json: { items, next_cursor: items[0].id } })
    }
    if (action === 'confirm-stopped') row.stage = 'stopped_confirmed'
    json = row
  } else if (pathname === '/api/tasks/T-A') json = occupiedTask
  else if (pathname === '/api/v1/employees') json = { items: [] }
  else if (pathname === '/api/v1/runtime') json = { agentscope_version: '2.0.10', configured: false, reachable: false }
  else if (pathname.includes('metrics')) json = {}
  return route.fulfill({ json, headers })
})

try {
  await page.goto(base + '/settings/workers')
  const list = page.getByTestId('worker-list')
  const labels = { OFFLINE: '离线', IDLE: '空闲', WORKING: '工作中', PAUSED: '已暂停' }
  const classes = { OFFLINE: 'astra-tag--neutral', IDLE: 'astra-tag--neutral', WORKING: 'el-tag--info', PAUSED: 'el-tag--warning' }
  for (const status of Object.keys(labels)) {
    for (const enabled of [true, false]) {
      const row = list.getByRole('row').filter({ hasText: status + '-' + enabled })
      await row.waitFor()
      assert.match(await row.innerText(), new RegExp(enabled ? '已启用' : '已停用'))
      assert.match(await row.locator('.el-tag').filter({ hasText: new RegExp('^' + labels[status] + '$') }).getAttribute('class'), new RegExp(classes[status]))
    }
  }
  await list.locator('.el-pager li').filter({ hasText: /^2$/ }).click()
  await list.getByText('额外 Worker 14', { exact: true }).waitFor()
  assert.equal(lastWorkerQuery.offset, '20')
  await list.locator('.el-select').filter({ has: page.getByRole('combobox', { name: 'Worker 状态' }) }).click()
  await page.getByRole('option', { name: '离线', exact: true }).click()
  await list.getByRole('textbox', { name: 'Worker 搜索' }).fill('构建')
  await list.getByRole('button', { name: '搜索', exact: true }).click()
  await list.getByRole('button', { name: '查看 构建 Worker 01', exact: true }).waitFor()
  assert.equal(lastWorkerQuery.status, 'OFFLINE')
  assert.equal(lastWorkerQuery.offset, '0')
  await page.waitForFunction(() => !document.querySelector('[data-testid=worker-list] .el-loading-mask'))
  worker.online = true
  await page.clock.fastForward(8_001)
  await list.getByRole('row').filter({ hasText: worker.name }).getByText('在线', { exact: true }).waitFor()
  assert.equal(lastWorkerQuery.q, '构建')
  worker.online = false
  await list.getByRole('link', { name: '研发占用任务' }).click()
  await page.getByTestId('task-detail').getByRole('heading', { name: '研发占用任务' }).waitFor()
  await page.goBack()
  await list.getByRole('button', { name: '查看 构建 Worker 01', exact: true }).click()

  const detail = page.getByTestId('worker-detail')
  const env = detail.getByRole('region', { name: '运行环境' })
  const manual = detail.locator('header').first().getByRole('button', { name: '刷新', exact: true })
  await env.getByText('未连接', { exact: true }).waitFor()
  await env.getByText('MCP 调用超时', { exact: true }).waitFor()
  await env.getByText('Skill qa 文件缺失', { exact: true }).waitFor()
  await env.getByText('有未提交修改', { exact: true }).waitFor()
  await detail.getByText('任务调度 已停用').waitFor()
  await detail.getByText('T-A', { exact: true }).waitFor()
  await detail.getByText('T-B', { exact: true }).waitFor()
  assert.equal(await detail.getByRole('button', { name: '删除', exact: true }).isDisabled(), true)
  assert.equal(await detail.getByRole('button', { name: '一键部署', exact: true }).isDisabled(), true)
  assert.equal(await detail.getByRole('button', { name: '检查', exact: true }).isDisabled(), true)
  await detail.getByRole('button', { name: '编辑配置', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '编辑 Worker' })
  assert.equal(await dialog.locator('input[type=password]').first().isDisabled(), true)
  await dialog.locator('.el-switch__core').click()
  await dialog.getByRole('button', { name: '保存', exact: true }).click()
  await detail.getByText('任务调度 已启用').waitFor()

  worker.status = 'WORKING'
  worker.epoch++
  await page.clock.fastForward(10_001)
  await env.getByText('版本 1.18.35', { exact: true }).waitFor()
  await detail.getByText('工作中', { exact: true }).waitFor()
  await detail.getByRole('button', { name: '暂停 Worker', exact: true }).click()
  await detail.getByText('已暂停', { exact: true }).waitFor()
  await detail.getByRole('button', { name: '恢复 Worker', exact: true }).click()
  await detail.getByText('工作中', { exact: true }).waitFor()
  await detail.locator('header').first().locator('button:not(.is-loading)').filter({ hasText: /^刷新$/ }).waitFor()

  let releaseHealth
  holdHealth = new Promise(resolve => { releaseHealth = resolve })
  const beforeHold = healthReads
  await Promise.all([page.waitForRequest(request => request.url().endsWith('/health')), manual.click()])
  assert.equal(healthReads, beforeHold + 1)
  await page.clock.fastForward(21_000)
  assert.equal(healthReads, beforeHold + 1)
  holdHealth = undefined
  releaseHealth()
  await detail.getByText(/更新于/).waitFor()

  failHealth = true
  await manual.click()
  await detail.getByText(/刷新失败.*诊断暂不可用/).waitFor()
  await env.getByText('MCP 调用超时', { exact: true }).waitFor()
  await detail.getByText(/更新于/).waitFor()
  failHealth = false
  worker.occupied_tasks = []
  worker.status = 'IDLE'
  await manual.click()
  await detail.getByText('无占用任务', { exact: true }).waitFor()
  await detail.getByText('空闲', { exact: true }).waitFor()
  assert.equal(await detail.getByRole('button', { name: '删除', exact: true }).isDisabled(), true)
  await detail.getByRole('row').filter({ hasText: '停止待确认' }).getByRole('button', { name: '详情' }).click()
  const stuckDetail = page.getByTestId('worker-maintenance-detail')
  await stuckDetail.getByRole('button', { name: '确认远端已停止' }).click()
  await page.getByRole('dialog').getByRole('button', { name: '确定', exact: true }).click()
  await stuckDetail.getByRole('button', { name: '重试操作' }).waitFor()
  await stuckDetail.getByRole('button', { name: '返回 Worker' }).click()
  await detail.getByRole('heading', { name: worker.name }).waitFor()
  assert.equal(await detail.getByRole('button', { name: '删除', exact: true }).isEnabled(), true)

  const maintenance = detail.getByTestId('worker-maintenance')
  reviewActive = true
  await manual.click()
  const reviewBlocker = maintenance.getByText('Worker 仍是未完成经验复盘的来源', { exact: true })
  await reviewBlocker.waitFor()
  for (const name of ['一键部署', '更新仓库', '重启']) {
    assert.equal(await maintenance.getByRole('button', { name, exact: true }).isDisabled(), true)
  }
  assert.equal(await maintenance.getByRole('button', { name: '检查', exact: true }).isEnabled(), true)
  reviewActive = false
  await manual.click()
  await reviewBlocker.waitFor({ state: 'hidden' })
  for (const name of ['一键部署', '更新仓库', '重启']) {
    assert.equal(await maintenance.getByRole('button', { name, exact: true }).isEnabled(), true)
  }

  const externalOperation = operation('op-external', 'deploy', 'RUNNING', { error_message: '外部用户发起' })
  history.unshift(externalOperation)
  await page.clock.fastForward(10_001)
  await maintenance.getByText('外部用户发起', { exact: true }).waitFor()
  externalOperation.state = 'SUCCEEDED'
  await maintenance.locator('.el-select').filter({ has: page.getByRole('combobox', { name: '运维操作类型' }) }).click()
  await page.getByRole('option', { name: '检查', exact: false }).first().click()
  await maintenance.locator('.el-pager li').filter({ hasText: /^2$/ }).click()
  await maintenance.getByRole('button', { name: '详情', exact: true }).first().waitFor()
  assert.equal(lastMaintenanceQuery.offset, '10')
  assert.equal(lastMaintenanceQuery.action, 'check')
  await maintenance.locator('.el-select').filter({ has: page.getByRole('combobox', { name: '运维操作状态' }) }).click()
  await page.getByRole('option', { name: '成功', exact: true }).click()
  await page.waitForFunction(() => document.querySelector('[aria-label="运维操作分页"] .el-pager .is-active')?.textContent.trim() === '1')
  assert.equal(lastMaintenanceQuery.state, 'SUCCEEDED')
  assert.equal(lastMaintenanceQuery.offset, '0')
  await maintenance.getByRole('button', { name: '一键部署', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '确定', exact: true }).click()
  const operationDetail = page.getByTestId('worker-maintenance-detail')
  const log = operationDetail.getByRole('log')
  await log.getByText('当前连接代次验证通过').waitFor()
  assert.equal(await log.locator('.log-message').count(), 500)
  assert.equal(await log.locator('.log-message').first().textContent(), '日志 551')
  await operationDetail.getByRole('button', { name: '更早日志' }).click()
  await log.getByText('历史日志 0', { exact: true }).waitFor()
  assert.equal(historyBefore, 551)
  assert.equal(await log.locator('.log-message').count(), 200)
  await operationDetail.getByRole('button', { name: '返回最新' }).click()
  await log.getByText('当前连接代次验证通过').waitFor()
  assert.equal(deployed, true)
  await operationDetail.getByRole('button', { name: '重试读取' }).click()
  await log.getByText('续订追加日志', { exact: true }).waitFor()
  assert.equal(await log.getByText('当前连接代次验证通过', { exact: true }).count(), 1)
  assert.equal(await log.locator('.log-message').count(), 500)
  assert.equal(streams, 2)
  const readsAfterLeave = healthReads
  await page.clock.fastForward(25_000)
  assert.equal(healthReads, readsAfterLeave)
  assert.ok(workerReads > 4)
  assert.deepEqual(errors, [])
  console.log('PASS: 四态和调度组合、离线占用、诊断、自动和手动刷新、取消重叠、列表筛选分页、部署日志和离开清理')
} catch (error) {
  console.error({ errors, url: page.url(), body: (await page.locator('body').innerText()).slice(0, 1200) })
  throw error
} finally {
  await browser.close()
}
