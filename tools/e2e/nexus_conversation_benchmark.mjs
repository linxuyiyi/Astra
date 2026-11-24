// 用法见 docs/reviews/nexus-v1.6.0-conversation.md，传入 v1.6.0 的 dist 路径。
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {
  fixture,
  prompt,
  conclusion,
  lastHistory,
  groupLast,
  groupName,
  owner,
} from './chat_conversation_fixture.mjs';
const { chromium } = await import(process.env.NEXUS_PLAYWRIGHT_MODULE || 'playwright');
const [dist, output, runsArg = '3'] = process.argv.slice(2);
assert(dist && output, '需要指定 dist 目录和证据目录');
await fs.mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true });
const report = {
  browser: browser.version(),
  node: process.version,
  os: [os.platform(), os.release(), os.cpus()[0].model].join(' / '),
  viewport: [1440, 900],
  samples: [],
};
const waitMessage = (page, text) =>
  page.waitForFunction(
    (needle) => document.querySelector('[data-testid="chat-messages"]')?.textContent.includes(needle),
    text,
    { timeout: 15000 },
  );
const clock = (page) => page.evaluate(() => performance.timeOrigin + performance.now());
const paint = (page) =>
  page.evaluate(
    () =>
      new Promise((resolve) =>
        requestAnimationFrame(() =>
          requestAnimationFrame(() => resolve(performance.timeOrigin + performance.now())),
        ),
      ),
  );
const button = (page, name) => page.getByRole('button', { name: new RegExp(name) }).first();
const composerInput = (page) => page.getByTestId('compose-input');
const scrollSelector = '[data-testid="chat-messages"]';
async function scrollFrames(page) {
  return page.evaluate(async (selector) => {
    const viewport = document.querySelector(selector);
    if (!viewport || viewport.scrollHeight < 500) throw new Error('消息视口不可滚动');
    const bottom = viewport.scrollHeight;
    const intervals = [];
    let last = await new Promise(requestAnimationFrame);
    for (let i = 0; i < 16; i++) {
      viewport.scrollTop = bottom - viewport.clientHeight - (i < 8 ? i : 15 - i) * 70;
      const now = await new Promise(requestAnimationFrame);
      intervals.push(now - last);
      last = now;
    }
    viewport.scrollTop = bottom;
    return Math.round(intervals.sort((a, b) => a - b).at(-1));
  }, scrollSelector);
}
async function inputFrames(page) {
    await composerInput(page).fill('');
    await page.evaluate(() => {
      window.inputLatencies = [];
      document.addEventListener('input', () => {
        const time = performance.now();
        requestAnimationFrame(() =>
          requestAnimationFrame(() => window.inputLatencies.push(performance.now() - time)),
        );
      });
    });
    await composerInput(page).pressSequentially('regression-check', { delay: 28 });
    await paint(page);
    return page.evaluate(() =>
      Math.round(
        window.inputLatencies.sort((a, b) => a - b)[Math.ceil(window.inputLatencies.length * 0.95) - 1],
      ),
    );
}
async function performanceCase(index) {
  const app = await fixture(dist);
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
  });
  await context.addInitScript((uid) => localStorage.setItem('nexus_owner_id', uid), owner);
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (err) => errors.push(err.message));
  const metrics = {};
  try {
    await page.goto(app.url + '/chat?employee=qa', { waitUntil: 'domcontentloaded' });
    await composerInput(page).waitFor();
    await waitMessage(page, lastHistory);
    metrics.coldHistoryMs = Math.round(
      (await paint(page)) - (await page.evaluate(() => performance.timeOrigin)),
    );
    await page.waitForTimeout(250);
    metrics.renderedNodes = await page.evaluate(() => document.getElementsByTagName('*').length);
    if (!index) await page.screenshot({ path: path.join(output, '01-private.png') });
    metrics.scrollWorstFrameMs = await scrollFrames(page);
    metrics.inputToFrameP95Ms = await inputFrames(page);
    await composerInput(page).fill(prompt);
    const client = await context.newCDPSession(page);
    await client.send('Performance.enable');
    const browserMetrics = async () =>
      Object.fromEntries((await client.send('Performance.getMetrics')).metrics.map((m) => [m.name, m.value]));
    await page.evaluate(() => {
      window.work = { records: 0, callbacks: 0 };
      new MutationObserver((records) => {
        window.work.records += records.length;
        window.work.callbacks++;
      }).observe(document.body, { subtree: true, characterData: true, childList: true, attributes: true });
      document.addEventListener(
        'keydown',
        (event) => {
          if (event.key === 'Enter') window.submitted = performance.now();
        },
        { capture: true, once: true },
      );
    });
    // 在页面本身记录首次出现消息的帧；避开 Playwright 轮询和调用延迟。
    await page.evaluate((text) => {
      const observer = new MutationObserver(() => {
        if (!window.submitted || !document.body.textContent.includes(text)) return;
        const rows = document.querySelectorAll('[data-message-id][data-kind=self]');
        if (![...rows].some((r) => r.textContent.includes(text))) return;
        observer.disconnect();
        requestAnimationFrame(() => {
          window.firstMessageFrame = performance.now() - window.submitted;
        });
      });
      observer.observe(document.body, { subtree: true, childList: true, characterData: true });
    }, prompt);
    const start = await browserMetrics();
    await composerInput(page).press('Enter');
    await waitMessage(page, '流式验收');
    await paint(page);
    metrics.streamBottomGapPx = await page.evaluate(() => {
      const el = document.querySelector('[data-testid="chat-messages"]');
      return Math.round(el.scrollHeight - el.scrollTop - el.clientHeight);
    });
    const viewport = page.getByTestId('chat-messages');
    await viewport.hover();
    await page.mouse.wheel(0, -350);
    await waitMessage(page, conclusion);
    await paint(page);
    const end = await browserMetrics();
    Object.assign(
      metrics,
      await page.evaluate(() => ({
        sentMessageFrameMs:
          typeof window.firstMessageFrame === 'number' ? Math.round(window.firstMessageFrame) : null,
        mutationRecords: window.work.records,
        mutationCallbacks: window.work.callbacks,
        stayedOffBottom: (() => {
          const node = document.querySelector('[data-testid="chat-messages"]');
          return node.scrollHeight - node.clientHeight - node.scrollTop > 100;
        })(),
      })),
    );
    assert(metrics.sentMessageFrameMs !== null, '没有捕捉到本次用户消息的首帧');
    metrics.mainThreadTaskMs = Math.round(1000 * (end.TaskDuration - start.TaskDuration));
    metrics.scriptMs = Math.round(1000 * (end.ScriptDuration - start.ScriptDuration));
    metrics.layoutMs = Math.round(1000 * (end.LayoutDuration - start.LayoutDuration));
    metrics.layoutCount = end.LayoutCount - start.LayoutCount;
    const switchTime = await clock(page);
    await button(page, groupName).click();
    await page.waitForURL(/group=release/);
    await composerInput(page).waitFor();
    await waitMessage(page, groupLast);
    metrics.switchToGroupMs = Math.round((await paint(page)) - switchTime);
    if (!index) await page.screenshot({ path: path.join(output, '02-group.png') });
    await composerInput(page).fill('@林舟 请确认');
    await composerInput(page).press('Enter');
    await waitMessage(page, '周宁收到群组请求。');
    assert((await viewport.innerText()).includes('林舟收到群组请求。'));
    const privateEntry = page
      .locator('.chat-target')
      .filter({ has: page.getByText('林舟', { exact: true }) })
      .first();
    await privateEntry.click();
    await page.waitForURL(/employee=qa/);
    await composerInput(page).waitFor();
    await waitMessage(page, conclusion);
    assert.equal(
      await viewport.locator('[data-message-id]').filter({ hasText: '收到群组请求。' }).count(),
      0,
    );
    await page.reload({ waitUntil: 'domcontentloaded' });
    await composerInput(page).waitFor();
    await waitMessage(page, conclusion);
    metrics.reloadHistoryMs = Math.round(
      (await paint(page)) - (await page.evaluate(() => performance.timeOrigin)),
    );
    const rows = viewport.locator('[data-message-id]');
    const older = viewport.getByTestId('chat-load-older');
    for (let i = 0; i < 7 && await older.count(); i++) {
      const previous = await rows.count();
      await older.click();
      await page.waitForFunction(n => document.querySelectorAll('[data-message-id]').length > n, previous);
    }
    assert.equal(await rows.count(), 322);
    metrics.allHistoryInputP95Ms = await inputFrames(page);
    metrics.allHistoryScrollWorstMs = await scrollFrames(page);
    assert.deepEqual(errors, []);
    metrics.pageErrors = errors;
    report.samples.push(metrics);
    console.log(JSON.stringify(metrics));
  } catch (err) {
    console.error('错误时页面', (await page.locator('body').innerText()).slice(-850), errors);
    await page.screenshot({ path: path.join(output, 'failure.png') });
    throw err;
  } finally {
    await context.close();
    await app.close();
  }
}
try {
  for (let i = 0; i < Number(runsArg); i++) await performanceCase(i);
  await fs.writeFile(path.join(output, 'results.json'), JSON.stringify(report, null, 2) + '\n');
} finally {
  await browser.close();
}
