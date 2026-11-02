// 隔离 UI 回归：API 全部使用夹具，不连接或修改真实 Worker。
import { createRequire } from 'node:module';
import assert from 'node:assert/strict';
import path from 'node:path';
import fs from 'node:fs/promises';
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_PATH || path.resolve('frontend/node_modules/playwright'));
const baseUrl = process.env.UI_BASE_URL || 'http://127.0.0.1:15189';
const outputDir = process.env.UI_EVIDENCE_DIR || '/tmp/nexus-worker-deploy-health-ui';
await fs.mkdir(outputDir, { recursive: true });
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 1050 } });
const errors = [];
const requests = [];
page.on('pageerror', error => errors.push(error.message));
await page.addInitScript(() => localStorage.setItem('nexus_owner_id', 'worker-regression'));
const timestamp = '2026-11-02T00:00:00Z';
const health = {
  environment_id: 1, employee_id: 'w1', enabled: true, readiness: 'DRIFTED',
  target_digest: 'fixture', target_matched: true, schedulable: false,
  connection: { state: 'online', channel: 'worker', epoch: 3, last_heartbeat_at: timestamp, observed_at: timestamp },
  runtime_health: { status: 'DRIFTED', readiness: 'DRIFTED', last_checked_at: timestamp,
    last_error: 'OpenCode 启动失败：fixture bind failed', equipment: [] },
  repository: null,
  maintenance: { state: 'FAILED', operation_id: 'op-old', stage: 'failed' },
  blocking: [{ code: 'config_drifted', message: '配置待同步，请执行一键部署' }],
};
const worker = {
  id: 'w1', name: '测试 Worker', role: '开发', description: '', enabled: true,
  auto_claim: true, auto_execute: false, task_types: ['ISSUE'], category_ids: [],
  digital_employee_id: null, status: 'IDLE', current_task_count: 0, worker_health: health,
  execution_environment: { id: 1, employee_id: 'w1', mode: 'LINUX_REMOTE', runtime_engine: 'opencode',
    name: '构建 Worker 01', host: 'fixture.invalid', port: 22, username: 'nexus',
    workspace_root: '/srv/project', opencode_server_port: 14096, status: 'DRIFTED',
    labels: [], status_detail: {}, opencode_path: 'opencode', has_ssh_password: true },
};
const oldOperation = {
  id: 'op-old', environment_id: 1, action: 'check', state: 'FAILED', stage: 'failed',
  attempt: 1, requested_by: 'regression', requested_at: timestamp, cancel_requested: false,
  error_code: 'health_check_failed', error_message: '健康检查未通过',
  target_revision: {}, after_snapshot: {}, events: [],
};
let activeOperation = null;
let streamCount = 0;
await page.route('**/api/**', async route => {
  const req = route.request();
  const pathname = new URL(req.url()).pathname;
  if (!pathname.startsWith('/api/')) return route.continue();
  requests.push({ pathname, method: req.method(), body: req.postDataJSON() });
  let data = [];
  if (pathname === '/api/agent-workers') data = [worker];
  else if (pathname === '/api/agent-workers/w1' && req.method() === 'PATCH') {
    worker.enabled = req.postDataJSON().enabled;
    health.enabled = worker.enabled;
    data = worker;
  } else if (pathname === '/api/agent-workers/1/health') data = health;
  else if (pathname === '/api/agent-workers/1/maintenance' && req.method() === 'POST') {
    assert.deepEqual(req.postDataJSON(), { action: 'deploy' });
    activeOperation = { ...oldOperation, id: 'op-deploy', action: 'deploy', state: 'QUEUED', stage: 'queued' };
    data = activeOperation;
  } else if (pathname === '/api/agent-workers/1/maintenance') {
    data = { items: activeOperation ? [activeOperation, oldOperation] : [oldOperation] };
  } else if (pathname.endsWith('/events') && pathname.startsWith('/api/maintenance/')) {
    streamCount += 1;
    activeOperation = { ...activeOperation, state: 'SUCCEEDED', stage: 'completed' };
    return route.fulfill({ status: 200, contentType: 'text/event-stream', body: 'event: end\ndata: {}\n\n' });
  } else if (pathname === '/api/maintenance/op-old') data = oldOperation;
  else if (pathname === '/api/maintenance/op-deploy') data = activeOperation;
  else if (pathname === '/api/v1/employees') data = { items: [] };
  else if (pathname.includes('metrics')) data = {};
  else if (pathname.includes('stream')) return route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' });
  return route.fulfill({ json: data });
});
try {
  await page.goto(`${baseUrl}/settings/workers`);
  await page.getByText('构建 Worker 01', { exact: true }).waitFor();
  assert.equal(await page.getByText('待同步', { exact: true }).count(), 1);
  assert.equal(requests.filter(req => req.pathname.endsWith('/health')).length, 0);
  await page.screenshot({ path: path.join(outputDir, 'worker-list.png'), fullPage: true });
  await page.getByRole('button', { name: '查看', exact: true }).click();
  await page.getByRole('tab', { name: '维护', exact: true }).click();
  await page.getByText('待同步，请点一键部署应用已保存的配置。', { exact: true }).waitFor();
  await page.getByText('OpenCode 启动失败：fixture bind failed', { exact: true }).waitFor();
  assert.equal(streamCount, 0, '终态记录不订阅 SSE');
  const enabledSwitch = page.getByTestId('worker-enabled').locator('.el-switch__core');
  await enabledSwitch.click();
  await page.waitForFunction(() => document.querySelector('[data-testid="worker-enabled"] input')?.getAttribute('aria-checked') === 'false');
  assert.equal(worker.enabled, false);
  assert.equal(requests.filter(req => req.method === 'POST' && req.pathname.endsWith('/maintenance')).length, 0);
  await enabledSwitch.click();
  await page.waitForFunction(() => document.querySelector('[data-testid="worker-enabled"] input')?.getAttribute('aria-checked') === 'true');
  assert.equal(worker.enabled, true);
  await page.screenshot({ path: path.join(outputDir, 'worker-maintenance.png'), fullPage: true });
  await page.getByTestId('deploy-environment').click();
  await page.getByRole('dialog').getByRole('button', { name: '一键部署', exact: true }).click();
  await page.waitForResponse(response => response.url().includes('/api/maintenance/op-deploy/events'));
  await page.getByText('成功', { exact: true }).first().waitFor();
  assert.ok(streamCount >= 1, '活跃操作订阅 SSE');
  assert.equal(requests.some(req => /reconcile|\/disable|\/restore/.test(req.pathname)), false);
  assert.deepEqual(errors, []);
  await fs.writeFile(path.join(outputDir, 'result.json'), JSON.stringify({
    result: 'passed', listHealthRequests: 0, enabledPatches: requests.filter(req => req.method === 'PATCH').length,
    streamCount, errors,
  }, null, 2));
  console.log(JSON.stringify({ result: 'passed', outputDir, streamCount }));
} catch (error) {
  console.error(JSON.stringify({ errors, requests, body: await page.locator('body').innerText() }));
  await page.screenshot({ path: path.join(outputDir, 'failure.png'), fullPage: true });
  throw error;
} finally {
  await browser.close();
}
