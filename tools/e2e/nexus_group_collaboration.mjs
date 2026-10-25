/** 两个独立 Chromium 进程验收真实服务；使用临时 UID，不创建账号。 */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
const { chromium } = await import(process.env.NEXUS_PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.NEXUS_E2E_URL;
const ownerUid = process.env.NEXUS_E2E_OWNER_UID || `owner_${Date.now()}`;
assert(base);
const evidence = process.env.NEXUS_E2E_EVIDENCE || '/tmp/nexus-group-browser';
await fs.mkdir(evidence, { recursive: true });
const browsers = await Promise.all([chromium.launch({ headless: true }), chromium.launch({ headless: true })]);
const [owner, other] = await Promise.all(browsers.map(b => b.newPage({ viewport: { width: 1440, height: 900 } })));
const errors = []; for (const p of [owner, other]) p.on('pageerror', e => errors.push(e.message));
const report = {};
async function enterUid(page, uid) {
  await page.goto(`${base}/chat`);
  await page.getByPlaceholder('输入之前使用的 UID').fill(uid);
  await page.getByRole('button', { name: '进入', exact: true }).click();
  await page.getByRole('button', { name: '新建群组', exact: true }).waitFor({ timeout: 20000 });
}
async function frame(page) { const f = page.frameLocator('iframe[title="Nexus 对话"]'); await f.locator('textarea').waitFor(); return f; }
async function send(page, text) { const f = await frame(page); await f.locator('textarea').fill(text); await f.getByRole('button', { name: '发送', exact: true }).click(); await f.getByText(text, { exact: true }).waitFor(); }
async function history(page, cid) { return (await page.request.get(`${base}/api/v1/channels/${cid}/messages`)).json(); }
async function waitGroup(page, previous) {
  const handle = await page.waitForFunction(prev => {
    const current = new URL(location.href).searchParams.get('group');
    return current && current !== prev ? current : false;
  }, previous, { timeout: 20000 });
  return handle.jsonValue();
}
async function waitInvite(page) {
  const input = page.getByRole('dialog').locator('input');
  await input.waitFor();
  for (let i = 0; i < 100; i += 1) {
    const value = await input.inputValue();
    if (value.startsWith('http')) return value;
    await page.waitForTimeout(100);
  }
  throw new Error('邀请链接未生成');
}
try {
  await enterUid(owner, ownerUid);
  await enterUid(other, `human_${Date.now()}`);
  const before = new URL(owner.url()).searchParams.get('group') || '';
  await owner.getByRole('button', { name: '新建群组', exact: true }).click();
  const dialog = owner.getByRole('dialog');
  await dialog.locator('input').first().fill(`多人协作验收-${Date.now()}`);
  await dialog.locator('textarea').fill('两个真实人类与数字员工共享讨论');
  await dialog.locator('.el-checkbox').first().waitFor({ timeout: 20000 });
  await dialog.getByRole('button', { name: '创建群组', exact: true }).click();
  await dialog.waitFor({ state: 'hidden', timeout: 20000 });
  await frame(owner);
  // 创建后路由可能仍指向旧群组；必须等待新的 group 查询参数，不能立即读取 URL。
  const cid = await waitGroup(owner, before); report.channel_id = cid;
  await owner.getByRole('button', { name: '成员', exact: true }).click();
  await owner.getByRole('button', { name: '邀请人类成员', exact: true }).click();
  const invite = await waitInvite(owner);
  await owner.keyboard.press('Escape');
  await other.goto(invite);
  const joinDialog = other.getByRole('dialog');
  await joinDialog.getByRole('button', { name: '加入', exact: true }).waitFor();
  await joinDialog.getByRole('button', { name: '加入', exact: true }).click();
  await joinDialog.waitFor({ state: 'hidden', timeout: 20000 });
  // 加入成功后 group 查询参数由异步路由写入，先等它落到被邀请的群组。
  const joinedHandle = await other.waitForFunction(
    expected => new URL(location.href).searchParams.get('group') === expected,
    cid,
    { timeout: 20000 },
  );
  assert.equal(await joinedHandle.jsonValue(), true);
  await frame(other);
  await send(owner, '我已经整理好会议记录');
  await (await frame(other)).getByText('我已经整理好会议记录', { exact: true }).waitFor();
  await send(other, '收到，我会补充结论');
  await (await frame(owner)).getByText('收到，我会补充结论', { exact: true }).waitFor();
  const discussion = (await history(owner, cid)).channel.discussion;
  assert.equal(discussion, null, JSON.stringify(discussion)); report.human_realtime = true;
  for (const page of [owner, other]) {
    const f = await frame(page);
    assert.equal(await f.locator('.group-message[data-self=true]').count(), 1);
    assert.equal(await f.locator('.group-message[data-self=false]').count(), 1);
    const rows = await f.locator('.group-message').evaluateAll(items => items.map(row => ({ self: row.dataset.self,
      direction: getComputedStyle(row).flexDirection, label: getComputedStyle(row.querySelector('.group-message-label')).justifyContent,
      bubble: getComputedStyle(row.querySelector('[data-slot=message]')).flexDirection })));
    assert(rows.every(r => r.self === 'true' ? r.direction === 'row-reverse' && r.label === 'flex-end' : r.direction === 'row' && r.bubble === 'row'));
    assert(await f.locator('body').evaluate(b => b.scrollWidth <= b.clientWidth + 1));
  }
  report.message_alignment = true;
  await Promise.all([send(owner, '并发记录甲'), send(other, '并发记录乙')]);
  for (const page of [owner, other]) for (const text of ['并发记录甲', '并发记录乙']) await (await frame(page)).getByText(text, { exact: true }).waitFor();
  const ids = await Promise.all([owner, other].map(async p => (await history(p, cid)).items.map(x => x.id)));
  assert.deepEqual(ids[0], ids[1]); assert.equal(new Set(ids[0]).size, ids[0].length); report.concurrent_order = true;
  await owner.screenshot({ path: `${evidence}/owner.png`, fullPage: true });
  await other.screenshot({ path: `${evidence}/colleague.png`, fullPage: true });
  await send(owner, '@小牛码 @小掌柜 你们相互介绍下。');
  // 用 Node 侧轮询持久历史，避免页面上下文到点后仍读到刚入队的一帧。
  const deadline = Date.now() + 150000;
  let done = await history(owner, cid);
  while (Date.now() < deadline) {
    const state = done.channel.discussion?.state;
    if (['completed', 'limited', 'failed', 'interrupted'].includes(state)) break;
    await owner.waitForTimeout(1000);
    done = await history(owner, cid);
  }
  assert.equal(done.channel.discussion?.state, 'completed', JSON.stringify(done.channel.discussion));
  const replies = done.items.filter(x => x.discussion_id === done.channel.discussion.id && x.sender_type === 'employee');
  assert(replies.length >= 2 && replies.length <= 6); assert.equal(new Set(replies.map(x => x.sender_id)).size, 2);
  report.employee_discussion = replies.map(x => ({ name: x.sender_name, text: x.text }));
  await owner.screenshot({ path: `${evidence}/discussion.png`, fullPage: true });
  await other.getByRole('button', { name: '成员', exact: true }).click();
  await other.getByRole('button', { name: '退出群组', exact: true }).click();
  // 退出是异步请求，轮询到服务端撤权完成再断言。
  let revoked = 200;
  for (let i = 0; i < 50 && revoked !== 403; i += 1) {
    await other.waitForTimeout(200);
    revoked = (await other.request.get(`${base}/api/v1/channels/${cid}/messages`)).status();
  }
  assert.equal(revoked, 403); report.leave_revoked = true;
  assert.deepEqual(errors, []); report.page_errors = errors;
  await fs.writeFile(`${evidence}/results.json`, JSON.stringify(report, null, 2));
  console.log(JSON.stringify({ passed: true, evidence, ...report }));
} finally { await Promise.all(browsers.map(b => b.close())); }
