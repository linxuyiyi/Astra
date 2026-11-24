// 验证 v1.6.0 私聊与群聊入口，包括上传、慢响应、恢复、任务与只读人工操作。
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fixture, owner, conclusion, groupName } from './chat_conversation_fixture.mjs';
const { chromium } = await import(process.env.NEXUS_PLAYWRIGHT_MODULE || 'playwright');
const [dist, output] = process.argv.slice(2);
assert(dist && output);
await fs.mkdir(output, { recursive: true });
const app = await fixture(dist, { regression: true });
const browser = await chromium.launch({
  headless: true,
  args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'],
});
const page = await browser.newPage({
  viewport: { width: 1440, height: 900 },
  locale: 'zh-CN',
  timezoneId: 'Asia/Shanghai',
});
await page.addInitScript((uid) => localStorage.setItem('nexus_owner_id', uid), owner);
page.setDefaultTimeout(9000);
const errors = [],
  checks = [];
page.on('pageerror', (e) => errors.push(e.message));
const chat = page.locator('.nc-conversation');
const input = chat.getByRole('textbox', { name: '消息输入' });
const timeline = chat.getByTestId('chat-messages');
const waitText = (scope, text) => scope.getByText(text, { exact: false }).last().waitFor();
const select = async (part, name) => {
  await page
    .locator('.chat-target')
    .filter({ has: page.getByText(name, { exact: true }) })
    .first()
    .click();
  await page.waitForURL(new RegExp(part));
  await input.waitFor();
};
const send = async (text) => {
  await input.fill(text);
  await chat.getByRole('button', { name: '发送消息', exact: true }).click();
};
const waitDone = () => chat.getByTestId('streaming-placeholder').waitFor({ state: 'hidden' });
const screen = (name) => page.screenshot({ path: path.join(output, name + '.png') });
try {
  await page.goto(app.url + '/chat?employee=qa', { waitUntil: 'domcontentloaded' });
  await waitText(timeline, '当前验收记录');
  const tabs = page.getByRole('tablist', { name: '会话筛选' });
  await tabs.getByRole('tab', { name: '全部' }).focus();
  await page.keyboard.press('ArrowRight');
  assert.equal(await tabs.getByRole('tab', { name: '联系人', selected: true }).count(), 1);
  await page.keyboard.press('End');
  assert.equal(await tabs.getByRole('tab', { name: '群组', selected: true }).count(), 1);
  await page.keyboard.press('Home');
  assert.equal(await tabs.getByRole('tab', { name: '全部', selected: true }).count(), 1);
  await page.getByRole('button', { name: '查看资料' }).focus();
  await page.keyboard.press('Tab');
  const skip = chat.getByRole('link', { name: '跳到消息输入' });
  assert.equal(await skip.evaluate((node) => node === document.activeElement), true);
  await page.keyboard.press('Enter');
  assert.equal(await input.evaluate((node) => node === document.activeElement), true);
  checks.push('会话标签支持方向键和首尾导航，从资料按钮即可跳到消息输入');
  const initialRows = await timeline.locator('.nc-message').count();
  assert.equal(initialRows, 50);
  const older = timeline.getByRole('button', { name: '加载更早消息' });
  await older.click();
  const earliest = timeline.getByText('请核对第 220 批用例。', {exact:true});
  await earliest.waitFor({state:'attached'});
  await timeline.evaluate(el => { el.scrollTop = 0; });
  await earliest.waitFor();
  await timeline.evaluate(el => { el.scrollTop = el.scrollHeight; });
  assert.equal(await timeline.locator('.nc-message').count(), 100);
  checks.push('私聊按 50 条向前加载，消息数量与边界吻合');
  const tools = timeline.locator('.nc-tool-group').last();
  await waitText(tools, '已完成 2 项工具操作');
  assert.equal(await tools.getAttribute('open'), null);
  assert.equal(await tools.locator('.nc-tool').count(), 0);
  await screen('04-work-summary');
  await tools.locator('summary').click();
  const search = timeline
    .locator('details')
    .filter({ has: page.locator('summary', { hasText: '搜索资料' }) })
    .last();
  assert.equal(await search.getAttribute('open'), null);
  assert.match(await search.locator('summary').innerText(), /2 项资料 · 发布验收记录/);
  await search.locator('summary').click();
  await waitText(search, 'release/browser.md');
  const peer = timeline
    .locator('details')
    .filter({ has: page.locator('summary', { hasText: '联系同事' }) })
    .last();
  const peerSummary = peer.locator('summary');
  assert.equal(await peer.getAttribute('data-tone'), 'error');
  assert.match(await peerSummary.innerText(), /该员工已禁用/);
  await peer.locator('summary').click();
  await waitText(peer, '请核实窗口');
  checks.push('已完成的连续 MCP 调用折叠成摘要，展开后能查看调用，联系同事仍单独呈现');
  await page.context().grantPermissions(['microphone'], { origin: app.url });
  async function record() {
    const mic = chat.getByTestId('asr-mic');
    await mic.click();
    await chat.getByRole('button', { name: '停止录音' }).waitFor();
    await page.waitForTimeout(1400);
    const [request] = await Promise.all([
      page.waitForRequest((r) => r.url().endsWith('/api/asr/transcribe')),
      mic.click(),
    ]);
    assert.equal(request.method(), 'POST');
  }
  await record();
  await page.waitForFunction(() =>
    document.querySelector('[data-testid=compose-input]')?.value.includes('本轮语音草稿'),
  );
  assert.equal(app.requestLog.filter((r) => r.method === 'POST' && r.path.endsWith('/messages')).length, 0);
  await input.fill('仅此私聊草稿');
  await record();
  await select('group=release', groupName);
  await waitText(timeline, '发布窗口已确认');
  await page.waitForTimeout(750); // 让旧会话的固定转写响应抵达。
  assert.equal(await input.inputValue(), '');
  checks.push('Chromium 虚拟麦克风真实录音，转写进入草稿、没有自动发送，迟到响应未写入群聊');
  assert.equal(
    await timeline.getByRole('link', { name: /回归报告已生成/ }).getAttribute('href'),
    '/tasks/T-RELEASE',
  );
  await page.setViewportSize({ width: 1280, height: 720 });
  await timeline.evaluate((el) => { el.scrollTop = el.scrollHeight; });
  await screen('05-group-collaboration');
  await page.setViewportSize({ width: 1440, height: 900 });
  await chat.getByRole('button', { name: '提及数字员工' }).click();
  assert.equal(await input.inputValue(), '@');
  await page.keyboard.insertText('林');
  await chat.getByRole('option', { name: '林舟' }).click();
  assert.match(await input.inputValue(), /^@林舟 /);
  await input.press('End');
  await input.pressSequentially('review');
  assert.equal(await input.inputValue(), '@林舟 review');
  await chat.getByRole('button', { name: '发送消息', exact: true }).click();
  await chat.getByTestId('streaming-placeholder').getByText('林舟 · 正在等待响应', { exact: true }).waitFor();
  await waitText(timeline, '周宁收到群组请求');
  assert.equal(await timeline.locator('[data-sender=pm]').filter({ hasText: '周宁收到群组请求' }).count(), 1);
  assert.equal(await timeline.locator('[data-sender=qa]').filter({ hasText: '林舟收到群组请求' }).count(), 1);
  checks.push('组内 @ 选择、两位员工按身份回显、任务状态与详情链接');
  await select('employee=pm', '周宁');
  await waitText(chat, '正在等待人工操作');
  await waitText(timeline, '当前会话无法确认，可停止本轮');
  assert.equal(await chat.getByRole('button', { name: /确认操作|同意|拒绝/ }).count(), 0);
  await select('employee=qa', '林舟');
  await input.waitFor();
  await page.waitForFunction(
    () => document.querySelector('[data-testid=compose-input]')?.value === '仅此私聊草稿',
  );
  checks.push('草稿随会话恢复，切出等待人工操作的会话不会阻塞另一员工');
  await input.fill('');
  async function upload(name, mimeType, buffer) {
    const [picker] = await Promise.all([
      page.waitForEvent('filechooser'),
      chat.getByRole('button', { name: '添加附件' }).click(),
    ]);
    await picker.setFiles({ name, mimeType, buffer });
  }
  await upload('rejected.txt', 'text/plain', Buffer.from('不可接受的附件'));
  await waitText(chat, '测试附件被拒绝');
  await chat.getByRole('button', { name: '移除 rejected.txt' }).click();
  const expire = async (route) => {
    if (route.request().method() === 'POST')
      await route.fulfill({ status: 404, json: { detail: '附件文件已失效，请重新上传' } });
    else await route.continue();
  };
  const privateMessages = '**/api/v1/conversations/c-qa/messages';
  await page.route(privateMessages, expire);
  await upload('失效附件.txt', 'text/plain', Buffer.from('即将失效'));
  await send('服务拒绝时保留这条原稿');
  await waitText(chat, '附件文件已失效，请重新上传');
  await page.waitForFunction(() => document.querySelector('[data-testid=compose-input]')?.value === '服务拒绝时保留这条原稿');
  const removeExpired = chat.getByRole('button', { name: '移除 失效附件.txt' });
  await removeExpired.waitFor();
  assert.equal(await chat.getByRole('button', { name: '重试原消息' }).count(), 0);
  await removeExpired.click();
  await page.unroute(privateMessages, expire);
  await input.fill('');
  checks.push('服务确认附件失效时还原正文和附件，可移除后编辑重发');
  await upload('检查项.md', 'text/markdown', Buffer.from('## 验收清单\n\n本轮检查完成。'));
  await waitText(chat, '检查项.md');
  const png = Buffer.from(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jJa0AAAAASUVORK5CYII=',
    'base64',
  );
  await upload('截图.png', 'image/png', png);
  await chat.getByRole('button', { name: '发送消息', exact: true }).click();
  const image = timeline.getByRole('img', { name: '截图.png' });
  await image.waitFor();
  await page.waitForFunction(() => {
    const img = document.querySelector('img[alt="截图.png"]');
    return img?.complete && img.naturalWidth > 0;
  });
  const file = timeline.getByRole('link', { name: '检查项.md' });
  await file.waitFor();
  await timeline.getByRole('button', { name: '预览', exact: true }).click();
  await waitText(timeline, '本轮检查完成。');
  await waitText(timeline, conclusion);
  await waitDone();
  checks.push('上传失败原因与移除、纯附件发送、PNG 解码、Markdown 文件预览');
  await timeline.evaluate((el) => {
    el.scrollTop = el.scrollHeight;
  });
  await send('重试这条投递');
  await waitText(chat, '模拟网关暂不可用');
  await page.reload({ waitUntil: 'domcontentloaded' });
  await waitText(chat, '上次发送结果尚未确认');
  await input.fill('下一轮草稿');
  assert.equal(await chat.getByRole('button', { name: '发送消息', exact: true }).isEnabled(), false);
  await chat.getByRole('button', { name: '重试原消息', exact: true }).click();
  await waitText(chat, '正在等待响应');
  await waitText(timeline, '已发送');
  assert.equal(await timeline.locator('.nc-message:not([data-sender])').count(), 0);
  await chat.getByTestId('streaming-placeholder').scrollIntoViewIfNeeded();
  await screen('03-waiting');
  await waitText(timeline, '重试这条投递');
  await waitDone();
  assert.equal(await input.inputValue(), '下一轮草稿');
  const attempts = app.requestLog.filter((r) => r.text === '重试这条投递');
  assert.equal(attempts.length, 2);
  assert.equal(attempts[0].clientId, attempts[1].clientId);
  checks.push('刷新后恢复未确认投递，重试复用 client_id，接受后等待有明确反馈');
  await timeline.evaluate((el) => { el.scrollTop = 0; });
  await page.getByRole('button', { name: '回到最新消息' }).waitFor();
  const delayReceipt = async (route) => {
    const response = await route.fetch();
    await new Promise((resolve) => setTimeout(resolve, 300));
    await route.fulfill({ response });
  };
  await page.route(privateMessages, delayReceipt);
  await send('检查连接恢复');
  await page.waitForFunction(() => {
    const status = document.querySelector('[data-phase=sending]');
    const viewport = status?.closest('.nc-viewport')?.getBoundingClientRect();
    const box = status?.getBoundingClientRect();
    return box && viewport && box.top >= viewport.top && box.bottom <= viewport.bottom;
  });
  await timeline.getByText('检查连接恢复', { exact: true }).waitFor();
  await page.unroute(privateMessages, delayReceipt);
  assert(await timeline.getByText('检查连接恢复', { exact: true }).evaluate((node) => {
    const row = node.getBoundingClientRect(), viewport = node.closest('.nc-viewport').getBoundingClientRect();
    return row.top >= viewport.top && row.bottom <= viewport.bottom;
  }));
  checks.push('从历史位置主动发送，投递中状态与已发送的正文依次进入视口');
  await waitDone();
  const final = timeline.locator('.nc-message[data-kind=speaker]').last();
  await waitText(final, conclusion);
  const records =
    ((await final.locator('.nc-message-content').textContent()).match(/记录/g) || []).length - 1;
  assert.equal(records, 149); // i=1..158，排除 16 的九个倍数。
  await page.reload({ waitUntil: 'domcontentloaded' });
  await waitText(timeline, conclusion);
  assert.equal(
    await timeline.locator('.nc-message[data-kind=speaker]').filter({ hasText: conclusion }).count(),
    3,
  );
  checks.push('部分输出断线后从持久快照恢复，重复旧帧不重复文本；刷新保留连续三轮');
  await send('停止后续输出');
  await waitText(chat, '正在等待响应');
  await chat.getByRole('button', { name: '停止回复' }).click();
  await waitText(timeline, '回复已中断');
  await waitDone();
  assert(app.requestLog.some((r) => r.method === 'POST' && r.path.endsWith('/interrupt')));
  checks.push('停止请求经过 Nexus，原生结束事件恢复输入');
  await select('group=release', groupName);
  await waitText(timeline, '周宁收到群组请求');
  app.revokeGroup();
  await page.waitForFunction(
    (revokedGroup) => {
      const staleRows = [...document.querySelectorAll('.nc-message')].some((row) =>
        row.textContent.includes('群组核对记录'),
      );
      const stillListed = [...document.querySelectorAll('.chat-target b')].some((row) =>
        row.textContent === revokedGroup,
      );
      const activeGroup = new URLSearchParams(location.search).get('group') === 'release';
      return !staleRows && (!stillListed || !activeGroup || document.querySelector('[data-testid=compose-input]')?.disabled);
    },
    groupName,
  );
  checks.push('群成员权限被撤销后，当前时间线清空并禁止继续操作原会话');

  await page.route('**/api/v1/conversations/c-pm/messages?*', async (route) => {
    const response = await route.fetch();
    const snapshot = await response.json();
    await route.fulfill({ response, json: { ...snapshot, messages: [], is_running: false } });
  });
  await page.goto(app.url + '/chat?employee=pm', { waitUntil: 'domcontentloaded' });
  await chat.getByTestId('chat-empty').waitFor();
  const welcome = await chat.getByTestId('chat-empty').boundingBox();
  const composerBox = await chat.locator('.nc-composer').boundingBox();
  const distanceToComposer = composerBox.y - (welcome.y + welcome.height);
  assert(distanceToComposer >= 12 && distanceToComposer <= 48);
  await screen('06-empty');
  checks.push('空会话的身份提示与输入区保持同一操作区');
  assert.deepEqual(errors, []);
  await fs.writeFile(
    path.join(output, 'regression.json'),
    JSON.stringify({ checks, pageErrors: errors }, null, 2) + '\n',
  );
  console.log(checks.join('\n'));
} catch (err) {
  await screen('regression-failure');
  console.error(
    '已通过',
    checks,
    '页面',
    (await chat.innerText().catch(() => '')).slice(-900),
    'JS 错误',
    errors,
  );
  throw err;
} finally {
  await browser.close();
  await app.close();
}
