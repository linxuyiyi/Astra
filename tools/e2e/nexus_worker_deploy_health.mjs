// 桌面维护 UI 回归：API 夹具隔离真实 Worker；验证当前 v1.5.0 扁平结构。
import { createRequire } from 'node:module'
import assert from 'node:assert/strict'
import path from 'node:path'
import fs from 'node:fs/promises'
const require = createRequire(import.meta.url)
const { chromium } = require(process.env.PLAYWRIGHT_PATH || path.resolve('frontend/node_modules/playwright'))
const baseUrl = process.env.UI_BASE_URL || 'http://127.0.0.1:15189'
const outputDir = process.env.UI_EVIDENCE_DIR || '/tmp/nexus-v150-worker-ui'
await fs.mkdir(outputDir, { recursive: true })
const browser = await chromium.launch({ headless: true })
const page = await browser.newPage({ viewport: { width: 1440, height: 1050 } })
const errors = [], requests = []
page.on('pageerror', error => errors.push(error.message))
await page.addInitScript(() => {
  localStorage.setItem('nexus_owner_id', 'isolated-ui-test')
  document.cookie = 'nexus_uid=isolated-ui-test; Path=/; SameSite=Lax'
})
const timestamp = '2026-11-05T00:00:00Z'
const worker = {
  id: 'w1', name: '构建 Worker 01', description: '', enabled: true,
  task_types: ['ISSUE'], category_ids: [], status: 'OFFLINE',
  runtime_engine: 'opencode', mode: 'LINUX_REMOTE', host: 'fixture.invalid', port: 22,
  username: 'nexus', workspace_root: '/srv/project', model_name: 'route-test',
  repository_url: null, branch: null, skill_refs: [], mcp_refs: [],
  has_ssh_password: true, has_git_token: false, epoch: 3,
  last_heartbeat_at: timestamp, agent_version: '1.5.0', created_at: timestamp, updated_at: timestamp,
}
const health = { readiness: 'UNKNOWN', blocking: [{ message: 'Worker 尚未连接' }] }
let operation = {
  id: 'op-expired', agent_worker_id: 'w1', action: 'deploy', state: 'FAILED',
  stage: 'stop_unconfirmed', error_message: '控制进程失联，等待确认远端已停止',
}
let deployed = false
let streams = 0
await page.route('**/api/**', async route => {
  const req = route.request(), url = new URL(req.url()), pathname = url.pathname
  requests.push({ pathname, method: req.method() })
  let data = []
  if (pathname === '/api/workers') data = [worker]
  else if (pathname === '/api/workers/w1') {
    if (req.method() === 'PUT') {
      assert.equal('id' in req.postDataJSON(), false)
      Object.assign(worker, req.postDataJSON())
    }
    data = worker
  } else if (pathname === '/api/workers/w1/health') data = health
  else if (pathname === '/api/maintenance/op-expired/confirm-stopped') {
    operation = { ...operation, stage: 'stopped_confirmed' }
    data = operation
  } else if (pathname === '/api/workers/w1/maintenance') {
    if (req.method() === 'POST') {
      assert.equal(req.postDataJSON().action, 'deploy')
      operation = { ...operation, id: 'op-deploy', state: 'QUEUED', stage: 'queued', error_message: null }
      deployed = true
    }
    data = req.method() === 'POST' ? operation : { items: [operation] }
  } else if (pathname === '/api/maintenance/op-deploy/events') {
    streams += 1
    operation = { ...operation, state: 'SUCCEEDED', stage: 'completed' }
    const event = { id: 2, name: '就绪验收', message: '部署完成：当前连接代次验证通过' }
    return route.fulfill({ contentType: 'text/event-stream', body: `id: 2\nevent: message\ndata: ${JSON.stringify(event)}\n\nevent: end\ndata: {}\n\n` })
  } else if (pathname.endsWith('/events/page')) data = { items: [{ id: 1, name: '部署配置', message: '凭据已重新签发并启动 Worker' }], next_cursor: null }
  else if (pathname === '/api/v1/employees') data = { items: [] }
  else if (pathname === '/api/v1/runtime') data = { agentscope_version: '2.0.10', configured: false, reachable: false }
  else if (pathname.includes('metrics')) data = {}
  else if (pathname.includes('stream')) return route.fulfill({ contentType: 'text/event-stream', body: '' })
  return route.fulfill({ json: data })
})
try {
  await page.goto(`${baseUrl}/settings/workers`)
  await page.getByText(worker.name, { exact: true }).waitFor()
  await page.screenshot({ path: path.join(outputDir, 'worker-list.png'), fullPage: true })
  await page.getByText(worker.name, { exact: true }).click()
  await page.getByRole('heading', { name: worker.name }).waitFor()
  await page.getByRole('button', { name: '确认远端已停止', exact: true }).waitFor()
  assert.equal(await page.getByRole('button', { name: '一键部署', exact: true }).isDisabled(), true)
  await page.getByRole('button', { name: '确认远端已停止', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '确定', exact: true }).click()
  await page.getByRole('button', { name: '一键部署', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '确定', exact: true }).click()
  await page.getByText('成功', { exact: true }).waitFor()
  assert.equal(deployed, true)
  assert.equal(streams, 1)
  await page.getByText('成功', { exact: true }).click()
  await page.getByText('凭据已重新签发并启动 Worker', { exact: true }).waitFor()
  await page.screenshot({ path: path.join(outputDir, 'worker-maintenance.png'), fullPage: true })
  await page.getByRole('button', { name: '编辑配置', exact: true }).click()
  await page.getByRole('dialog').getByText('SSH 密码', { exact: true }).waitFor()
  const password = page.getByRole('dialog').locator('input[type="password"]').first()
  assert.equal(await password.inputValue(), '')
  await page.getByRole('dialog').locator('.el-switch__core').click()
  await page.getByRole('dialog').getByRole('button', { name: '保存', exact: true }).click()
  await page.getByRole('dialog').waitFor({ state: 'hidden' })
  assert.equal(worker.enabled, false)
  assert.deepEqual(errors, [])
  assert.equal(requests.some(req => req.pathname.includes('agent-workers')), false)
  await fs.writeFile(path.join(outputDir, 'result.json'), JSON.stringify({ result: 'passed', streams, requests, errors }, null, 2))
  console.log(JSON.stringify({ result: 'passed', outputDir }))
} catch (error) {
  await page.screenshot({ path: path.join(outputDir, 'failure.png'), fullPage: true })
  console.error(JSON.stringify({ errors, body: await page.locator('body').innerText() }))
  throw error
} finally {
  await browser.close()
}
