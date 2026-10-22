#!/usr/bin/env node
/**
 * Real browser acceptance for the Nexus digital-employee push-to-talk input.
 *
 * Chromium's fake microphone supplies a fixed WAV, but MediaRecorder, the Nexus
 * API, the gateway, SenseVoiceSmall and AgentScope are the real running
 * services. The script also covers keyboard edge cases and local shortcut
 * preferences.
 *
 * Env:
 *   NEXUS_BASE, NEXUS_UID, NEXUS_EMPLOYEE_ID, NEXUS_CHANNEL_ID,
 *   NEXUS_AUDIO, NEXUS_SCREENSHOT, PLAYWRIGHT_PATH
 */

import { execSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const playwrightPath = process.env.PLAYWRIGHT_PATH
  || new URL('../../../frontend/node_modules/playwright/index.mjs', import.meta.url).href;
const { chromium } = await import(playwrightPath);

const BASE = process.env.NEXUS_BASE || 'http://127.0.0.1:18120';
const UID = process.env.NEXUS_UID || 'local-qa';
const EMP = process.env.NEXUS_EMPLOYEE_ID || 'emp_815a4c618515';
const CHANNEL = process.env.NEXUS_CHANNEL_ID || 'ch_8236da560e51';
const AUDIO = process.env.NEXUS_AUDIO || '/tmp/case1-48k-padded.wav';
const SCREENSHOT = process.env.NEXUS_SCREENSHOT || '/tmp/nexus-ptt.png';
const REPORT = process.env.NEXUS_REPORT || '';
const ASR_DIR = process.env.NEXUS_ASR_DIR || fileURLToPath(new URL('..', import.meta.url));

const results = { checks: [], errors: [] };
const check = (name, ok, detail = '') => {
  results.checks.push({ name, ok: Boolean(ok), detail });
  if (!ok) results.errors.push(`${name}: ${detail}`);
  console.log(`${ok ? '✓' : '✗'} ${name}${detail ? ` — ${detail}` : ''}`);
};

function launch(fakeUi = true) {
  const args = [
    '--use-fake-device-for-media-stream',
    `--use-file-for-fake-audio-capture=${AUDIO}`,
  ];
  if (fakeUi) args.push('--use-fake-ui-for-media-stream');
  return chromium.launch({ headless: true, args });
}

async function newPage(browser, { permissions = ['microphone'], initScripts = [] } = {}) {
  const context = await browser.newContext({ permissions, viewport: { width: 1440, height: 960 } });
  await context.addCookies([{ name: 'nexus_uid', value: UID, domain: '127.0.0.1', path: '/' }]);
  await context.addInitScript((uid) => localStorage.setItem('nexus_owner_id', uid), UID);
  for (const script of initScripts) await context.addInitScript(script);
  const page = await context.newPage();
  const requests = [];
  const errors = [];
  page.on('request', (request) => {
    if (request.url().includes('/api/asr/transcribe')) requests.push(request.url());
  });
  page.on('pageerror', (error) => errors.push(error.message));
  return { context, page, requests, errors };
}

async function openChat(page, search) {
  await page.goto(`${BASE}/chat?${search}`, { waitUntil: 'domcontentloaded', timeout: 60000 });
  const frame = page.frameLocator('iframe.chat-island');
  const textarea = frame.locator('textarea');
  await textarea.waitFor({ state: 'visible', timeout: 60000 });
  const hint = frame.locator('[data-testid="asr-shortcut-hint"]');
  await hint.waitFor({ state: 'visible', timeout: 60000 });
  return { frame, textarea, hint };
}

async function statusText(frame) {
  return (await frame.locator('[data-testid="asr-status"]').innerText().catch(() => '')).trim();
}

async function waitIdle(frame, timeout = 30000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    const text = await statusText(frame);
    if (!text.includes('录音中') && !text.includes('正在识别') && !text.includes('正在请求')) return text;
    await new Promise((resolve) => setTimeout(resolve, 150));
  }
  return statusText(frame);
}

async function waitAdded(textarea, base, timeout = 60000) {
  const end = Date.now() + timeout;
  let value = '';
  while (Date.now() < end) {
    value = await textarea.inputValue();
    if (value.length > base.length) return value;
    await new Promise((resolve) => setTimeout(resolve, 150));
  }
  return value;
}

async function hold(page, frame, ms, { repeat = false, release = 'primary-first' } = {}) {
  await page.keyboard.down('Alt');
  await page.keyboard.down('k');
  await new Promise((resolve) => setTimeout(resolve, ms));
  if (repeat) {
    await frame.locator('textarea').evaluate(() => {
      window.dispatchEvent(new KeyboardEvent('keydown', {
        code: 'KeyK', key: 'k', altKey: true, repeat: true, bubbles: true, cancelable: true,
      }));
    });
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  if (release === 'modifier-first') {
    await page.keyboard.up('Alt');
    await page.keyboard.up('k');
  } else {
    await page.keyboard.up('k');
    await page.keyboard.up('Alt');
  }
}

async function fetchJson(url, init = {}, attempts = 3) {
  let lastError;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      const response = await fetch(url, init);
      if (!response.ok) throw new Error(`${url} -> ${response.status}`);
      return await response.json();
    } catch (error) {
      lastError = error;
      await new Promise((resolve) => setTimeout(resolve, 300 * (attempt + 1)));
    }
  }
  throw lastError;
}

async function api(path) {
  return fetchJson(`${BASE}${path}`, { headers: { 'X-Owner-Id': UID } });
}

async function conversationId(page) {
  const src = await page.locator('iframe.chat-island').getAttribute('src');
  return new URL(src, BASE).searchParams.get('conversation');
}

async function messageCount(conversation) {
  return (await api(`/api/v1/conversations/${conversation}`)).messages.length;
}

async function main() {
  // ── Core keyboard behaviour ─────────────────────────────────────────
  const browser = await launch();
  const privatePage = await newPage(browser);
  const page = privatePage.page;
  const { frame, textarea, hint } = await openChat(page, `type=contact&employee=${EMP}`);
  const hintText = (await hint.innerText()).replace(/\s+/g, ' ');
  check('default hint', hintText.includes('⌥ K') || hintText.includes('Alt + K'), hintText);

  // A single modifier or plain key must never start push-to-talk.
  const plainRequests = privatePage.requests.length;
  await textarea.focus();
  await page.keyboard.press('Space');
  await page.keyboard.press('KeyK');
  await page.waitForTimeout(500);
  check('plain key does not trigger', privatePage.requests.length === plainRequests && !(await statusText(frame)).includes('录音中'), await statusText(frame));
  await textarea.fill('');

  const conversation = await conversationId(page);
  const countBefore = await messageCount(conversation);
  const draft = 'PTT草稿：';
  await textarea.fill(draft);
  await textarea.focus();
  const requestBefore = privatePage.requests.length;
  await page.keyboard.down('Alt');
  await page.keyboard.down('k');
  await new Promise((resolve) => setTimeout(resolve, 1000));
  await page.screenshot({ path: SCREENSHOT.replace(/\.png$/, '-recording.png'), fullPage: true });
  await new Promise((resolve) => setTimeout(resolve, 1000));
  await frame.locator('textarea').evaluate(() => {
    window.dispatchEvent(new KeyboardEvent('keydown', {
      code: 'KeyK', key: 'k', altKey: true, repeat: true, bubbles: true, cancelable: true,
    }));
  });
  await page.waitForTimeout(200);
  await page.keyboard.up('k');
  await page.keyboard.up('Alt');
  const value = await waitAdded(textarea, draft);
  check('focus + hold/release K first', value.length > draft.length, JSON.stringify(value));
  check('no dead-key character', !value.includes('˚'), JSON.stringify(value));
  check('repeat keydown does not duplicate upload', privatePage.requests.length - requestBefore === 1, `${privatePage.requests.length - requestBefore} request(s)`);
  check('transcript not auto-sent', await messageCount(conversation) === countBefore, `messages ${countBefore}`);
  await waitIdle(frame);

  await textarea.fill('释放Alt：');
  await textarea.focus();
  const altBase = await textarea.inputValue();
  await hold(page, frame, 1400, { release: 'modifier-first' });
  const altValue = await waitAdded(textarea, altBase);
  check('release Alt first', altValue.length > altBase.length, JSON.stringify(altValue));
  await waitIdle(frame);

  const escBase = '取消草稿：';
  await textarea.fill(escBase);
  await textarea.focus();
  const escRequests = privatePage.requests.length;
  await page.keyboard.down('Alt');
  await page.keyboard.down('k');
  await new Promise((resolve) => setTimeout(resolve, 900));
  await page.keyboard.press('Escape');
  await page.keyboard.up('k');
  await page.keyboard.up('Alt');
  await new Promise((resolve) => setTimeout(resolve, 1200));
  check('Esc cancels without upload', await textarea.inputValue() === escBase && privatePage.requests.length === escRequests, await textarea.inputValue());

  const quickBase = '快速草稿：';
  await textarea.fill(quickBase);
  await textarea.focus();
  const quickRequests = privatePage.requests.length;
  await page.keyboard.down('Alt');
  await page.keyboard.down('k');
  await new Promise((resolve) => setTimeout(resolve, 60));
  await page.keyboard.up('k');
  await page.keyboard.up('Alt');
  await new Promise((resolve) => setTimeout(resolve, 1800));
  check('quick press does not get stuck or duplicate', privatePage.requests.length - quickRequests <= 1 && !(await statusText(frame)).includes('录音中'), `${privatePage.requests.length - quickRequests} request(s)`);
  await waitIdle(frame);

  const hiddenBase = '隐藏草稿：';
  await textarea.fill(hiddenBase);
  await textarea.focus();
  const hiddenRequests = privatePage.requests.length;
  await page.keyboard.down('Alt');
  await page.keyboard.down('k');
  await new Promise((resolve) => setTimeout(resolve, 900));
  await frame.locator('textarea').evaluate(() => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' });
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await page.keyboard.up('k');
  await page.keyboard.up('Alt');
  await new Promise((resolve) => setTimeout(resolve, 1200));
  await frame.locator('textarea').evaluate(() => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' });
    document.dispatchEvent(new Event('visibilitychange'));
  });
  check('page hidden cancels without upload', await textarea.inputValue() === hiddenBase && privatePage.requests.length === hiddenRequests, await textarea.inputValue());

  const blurBase = '失焦草稿：';
  await textarea.fill(blurBase);
  await textarea.focus();
  const blurRequests = privatePage.requests.length;
  await page.keyboard.down('Alt');
  await page.keyboard.down('k');
  await new Promise((resolve) => setTimeout(resolve, 900));
  await frame.locator('textarea').evaluate(() => window.dispatchEvent(new Event('blur')));
  await page.keyboard.up('k');
  await page.keyboard.up('Alt');
  await new Promise((resolve) => setTimeout(resolve, 1200));
  check('blur cancels without upload', await textarea.inputValue() === blurBase && privatePage.requests.length === blurRequests, await textarea.inputValue());

  // Real IME composition through CDP: Alt+K must not start recording.
  const imeBase = '输入法：';
  await textarea.fill(imeBase);
  await textarea.focus();
  const imeRequests = privatePage.requests.length;
  const cdp = await privatePage.context.newCDPSession(page);
  await cdp.send('Input.imeSetComposition', { text: 'ni', selectionStart: 2, selectionEnd: 2 });
  await page.waitForTimeout(250);
  await page.keyboard.down('Alt');
  await page.keyboard.down('k');
  await new Promise((resolve) => setTimeout(resolve, 700));
  const imeStatus = await statusText(frame);
  await page.keyboard.up('k');
  await page.keyboard.up('Alt');
  await cdp.send('Input.insertText', { text: '你' });
  await page.waitForTimeout(300);
  check('IME composition does not trigger', !imeStatus.includes('录音中') && privatePage.requests.length === imeRequests, imeStatus);
  await textarea.fill('');

  // ── Shortcut settings and persistence ───────────────────────────────
  await hint.click();
  const dialog = frame.locator('[data-testid="asr-shortcut-dialog"]');
  await dialog.waitFor({ state: 'visible', timeout: 10000 });
  await page.screenshot({ path: SCREENSHOT.replace(/\.png$/, '-settings.png'), fullPage: true });
  await page.keyboard.down('Alt');
  await page.keyboard.down('j');
  await page.keyboard.up('j');
  await page.keyboard.up('Alt');
  await page.waitForTimeout(300);
  await frame.locator('[data-testid="asr-shortcut-save"]').click();
  await page.waitForTimeout(400);
  const customHint = (await hint.innerText()).replace(/\s+/g, ' ');
  check('custom shortcut saves immediately', customHint.includes('⌥ J') || customHint.includes('Alt + J'), customHint);

  await textarea.fill('旧快捷键：');
  await textarea.focus();
  const oldRequests = privatePage.requests.length;
  await hold(page, frame, 700);
  await new Promise((resolve) => setTimeout(resolve, 800));
  const oldValue = await textarea.inputValue();
  check('old shortcut is inactive after change', privatePage.requests.length === oldRequests && oldValue.startsWith('旧快捷键：'), JSON.stringify(oldValue));

  await textarea.fill('新快捷键：');
  await textarea.focus();
  await page.keyboard.down('Alt');
  await page.keyboard.down('j');
  await new Promise((resolve) => setTimeout(resolve, 1200));
  await page.keyboard.up('j');
  await page.keyboard.up('Alt');
  const customValue = await waitAdded(textarea, '新快捷键：');
  check('new shortcut records', customValue.length > '新快捷键：'.length, JSON.stringify(customValue));
  await waitIdle(frame);

  await page.reload({ waitUntil: 'domcontentloaded' });
  const reloaded = await openChat(page, `type=contact&employee=${EMP}`);
  const reloadedHint = (await reloaded.hint.innerText()).replace(/\s+/g, ' ');
  check('shortcut persists after reload', reloadedHint.includes('⌥ J') || reloadedHint.includes('Alt + J'), reloadedHint);
  await reloaded.hint.click();
  const resetDialog = reloaded.frame.locator('[data-testid="asr-shortcut-dialog"]');
  await resetDialog.waitFor({ state: 'visible', timeout: 10000 });
  await reloaded.frame.locator('[data-testid="asr-shortcut-reset"]').click();
  await reloaded.frame.locator('[data-testid="asr-shortcut-save"]').click();
  await page.waitForTimeout(400);
  const defaultHint = (await reloaded.hint.innerText()).replace(/\s+/g, ' ');
  check('shortcut reset to default', defaultHint.includes('⌥ K') || defaultHint.includes('Alt + K'), defaultHint);

  // ── Original microphone button still works ──────────────────────────
  const micTextarea = reloaded.textarea;
  await micTextarea.fill('按钮草稿：');
  await micTextarea.focus();
  const micRequests = privatePage.requests.length;
  await reloaded.frame.getByRole('button', { name: '语音输入（转文字）' }).click();
  await page.waitForTimeout(1300);
  await reloaded.frame.getByRole('button', { name: '停止录音' }).click();
  const micValue = await waitAdded(micTextarea, '按钮草稿：');
  check('microphone button still records', micValue.length > '按钮草稿：'.length && privatePage.requests.length > micRequests, JSON.stringify(micValue));
  await waitIdle(reloaded.frame);

  // ── Final acceptance: hold, release, Enter sends to AgentScope ───────
  const finalBase = '';
  await micTextarea.fill(finalBase);
  await micTextarea.focus();
  const finalBefore = await messageCount(conversation);
  await hold(page, reloaded.frame, 4200);
  const finalValue = await waitAdded(micTextarea, finalBase, 60000);
  check('final PTT fills input', finalValue.length > 0, JSON.stringify(finalValue));
  check('final PTT does not auto-send', await messageCount(conversation) === finalBefore, `messages ${finalBefore}`);
  await micTextarea.press('Enter');
  const replyDeadline = Date.now() + 90000;
  let reply = null;
  let userSeen = false;
  while (Date.now() < replyDeadline) {
    const payload = await api(`/api/v1/conversations/${conversation}`);
    const messages = payload.messages || [];
    const userIndex = messages.findLastIndex((item) => item.sender === 'user' && item.text === finalValue);
    userSeen = userIndex >= 0;
    reply = [...messages].reverse().find((item) => item.sender !== 'user' && item.text);
    const replyIndex = reply ? messages.indexOf(reply) : -1;
    if (userSeen && reply && replyIndex > userIndex) break;
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  check('Enter sends recognized text', userSeen, JSON.stringify(finalValue));
  check('AgentScope replies', Boolean(reply?.text), reply?.text?.slice(0, 80) || '');
  if (reply?.text) {
    const snippet = reply.text.replace(/[*#`>_[\]()]/g, '').replace(/\s+/g, '').slice(0, 8);
    const end = Date.now() + 30000;
    let rendered = false;
    while (Date.now() < end) {
      const body = (await reloaded.frame.locator('body').innerText()).replace(/\s+/g, '');
      if (body.includes(snippet)) { rendered = true; break; }
      await page.waitForTimeout(300);
    }
    check('reply rendered in UI', rendered, snippet);
  }
  await page.screenshot({ path: SCREENSHOT, fullPage: true });
  results.final = { transcript: finalValue, reply: reply?.text?.slice(0, 300), screenshot: SCREENSHOT };

  await privatePage.context.close();
  await browser.close();

  // ── Release before permission resolves ──────────────────────────────
  const pendingBrowser = await launch();
  const pending = await newPage(pendingBrowser, {
    initScripts: [() => {
      const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
      navigator.mediaDevices.getUserMedia = async (constraints) => {
        await new Promise((resolve) => setTimeout(resolve, 1500));
        return original(constraints);
      };
    }],
  });
  const pendingChat = await openChat(pending.page, `type=contact&employee=${EMP}`);
  await pendingChat.textarea.fill('权限未完成：');
  await pendingChat.textarea.focus();
  await pending.page.keyboard.down('Alt');
  await pending.page.keyboard.down('k');
  await new Promise((resolve) => setTimeout(resolve, 100));
  await pending.page.keyboard.up('k');
  await pending.page.keyboard.up('Alt');
  await new Promise((resolve) => setTimeout(resolve, 2200));
  check('release before permission does not start later', pending.requests.length === 0 && await pendingChat.textarea.inputValue() === '权限未完成：', `${pending.requests.length} request(s)`);
  await pending.context.close();
  await pendingBrowser.close();

  // ── Permission denial (Web API rejection, UI branch) ────────────────
  const deniedBrowser = await launch();
  const denied = await newPage(deniedBrowser, {
    initScripts: [() => {
      Object.defineProperty(navigator.mediaDevices, 'getUserMedia', {
        configurable: true,
        value: () => Promise.reject(new DOMException('Permission denied', 'NotAllowedError')),
      });
    }],
  });
  const deniedChat = await openChat(denied.page, `type=contact&employee=${EMP}`);
  await deniedChat.textarea.focus();
  await hold(denied.page, deniedChat.frame, 800);
  await new Promise((resolve) => setTimeout(resolve, 800));
  const deniedBody = await deniedChat.frame.locator('body').innerText();
  check('permission denial is explained', deniedBody.includes('麦克风权限被拒绝') && denied.requests.length === 0, `${denied.requests.length} request(s)`);
  await denied.context.close();
  await deniedBrowser.close();

  // ── Switch conversation while transcribing ──────────────────────────
  const switchBrowser = await launch();
  const switched = await newPage(switchBrowser);
  const firstChat = await openChat(switched.page, `type=contact&employee=${EMP}`);
  await firstChat.textarea.focus();
  await hold(switched.page, firstChat.frame, 1200);
  await switched.page.goto(`${BASE}/chat?type=contact&employee=emp_cc1620f70577`, {
    waitUntil: 'domcontentloaded', timeout: 60000,
  });
  const secondFrame = switched.page.frameLocator('iframe.chat-island');
  const secondTextarea = secondFrame.locator('textarea');
  await secondTextarea.waitFor({ state: 'visible', timeout: 60000 });
  await switched.page.waitForTimeout(5000);
  check('switch conversation does not leak transcript', (await secondTextarea.inputValue()) === '', JSON.stringify(await secondTextarea.inputValue()));
  await switched.context.close();
  await switchBrowser.close();

  // ── Group chat PTT ──────────────────────────────────────────────────
  const groupBrowser = await launch();
  const group = await newPage(groupBrowser);
  const groupChat = await openChat(group.page, `type=group&group=${CHANNEL}`);
  const groupBefore = (await api(`/api/v1/channels/${CHANNEL}/messages`)).messages.length;
  await groupChat.textarea.fill('@阿明，');
  await groupChat.textarea.focus();
  await hold(group.page, groupChat.frame, 1800);
  const groupValue = await waitAdded(groupChat.textarea, '@阿明，');
  await group.page.screenshot({ path: SCREENSHOT.replace(/\.png$/, '-group.png'), fullPage: true });
  const groupAfter = (await api(`/api/v1/channels/${CHANNEL}/messages`)).messages.length;
  check('group PTT fills input', groupValue.length > '@阿明，'.length, JSON.stringify(groupValue));
  check('group PTT does not send', groupAfter === groupBefore, `messages ${groupBefore} -> ${groupAfter}`);
  await group.context.close();
  await groupBrowser.close();

  // ── PTT ASR outage + retry after service recovery ───────────────────
  const failureBrowser = await launch();
  const failure = await newPage(failureBrowser);
  const failureChat = await openChat(failure.page, `type=contact&employee=${EMP}`);
  const failureDraft = '失败草稿：';
  await failureChat.textarea.fill(failureDraft);
  await failureChat.textarea.focus();
  try {
    execSync('./nexus-asr.sh stop', { cwd: ASR_DIR, stdio: 'ignore' });
    await hold(failure.page, failureChat.frame, 900);
    let failureStatus = '';
    const failureDeadline = Date.now() + 60000;
    while (Date.now() < failureDeadline) {
      failureStatus = await statusText(failureChat.frame);
      if (failureStatus && !failureStatus.includes('录音中') && !failureStatus.includes('正在识别')) break;
      await new Promise((resolve) => setTimeout(resolve, 300));
    }
    check('PTT failure preserves draft', await failureChat.textarea.inputValue() === failureDraft, await failureChat.textarea.inputValue());
    check('PTT failure is explained', failureStatus.length > 0, failureStatus);
    execSync('./nexus-asr.sh start', { cwd: ASR_DIR, stdio: 'ignore' });
    const nodes = await fetchJson(`${BASE}/api/gateway/nodes`);
    const node = nodes.find((item) => item.name === 'Mac SenseVoiceSmall');
    for (let index = 0; index < 2 && node; index += 1) {
      await fetchJson(`${BASE}/api/gateway/nodes/${node.id}/probe`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ probe_types: ['ASR'] }),
      });
    }
    await failureChat.frame.getByRole('button', { name: '重试' }).click();
    const retryValue = await waitAdded(failureChat.textarea, failureDraft, 60000);
    check('PTT retry works after ASR recovery', retryValue.length > failureDraft.length && retryValue.startsWith(failureDraft), JSON.stringify(retryValue));
  } finally {
    try { execSync('./nexus-asr.sh start', { cwd: ASR_DIR, stdio: 'ignore' }); } catch { /* already running */ }
    await failure.context.close();
    await failureBrowser.close();
  }

  const payload = { checks: results.checks, final: results.final, errors: results.errors };
  if (REPORT) {
    const { writeFileSync } = await import('node:fs');
    writeFileSync(REPORT, `${JSON.stringify(payload, null, 2)}\n`);
  }
  console.log(JSON.stringify(payload, null, 2));
  if (results.errors.length) process.exitCode = 1;
}

await main();
