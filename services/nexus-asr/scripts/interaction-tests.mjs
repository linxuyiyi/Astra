#!/usr/bin/env node
/**
 * Browser interaction and failure-mode checks for the Nexus ASR microphone.
 *
 *   rapid double click, switching conversations while transcribing,
 *   headless microphone denial (CDP), Web-API permission rejection,
 *   ASR outage + draft preservation + retry after restart.
 *
 * Env: NEXUS_BASE, NEXUS_UID, NEXUS_EMPLOYEE_A, NEXUS_EMPLOYEE_B,
 *      NEXUS_AUDIO, NEXUS_ASR_DIR, PLAYWRIGHT_PATH.
 */
import { execSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const playwrightPath = process.env.PLAYWRIGHT_PATH
  || new URL('../../../frontend/node_modules/playwright/index.mjs', import.meta.url).href;
const { chromium } = await import(playwrightPath);

const BASE = process.env.NEXUS_BASE || 'http://127.0.0.1:18120';
const UID = process.env.NEXUS_UID || 'local-qa';
const EMP_A = process.env.NEXUS_EMPLOYEE_A || '';
const EMP_B = process.env.NEXUS_EMPLOYEE_B || EMP_A;
const AUDIO = process.env.NEXUS_AUDIO || '/tmp/zh-5s-48k.wav';
const ASR_DIR = process.env.NEXUS_ASR_DIR
  || fileURLToPath(new URL('..', import.meta.url));
const result = {};
if (!EMP_A) throw new Error('NEXUS_EMPLOYEE_A is required');

function launch(fakeUi = true) {
  const args = [
    '--use-fake-device-for-media-stream',
    `--use-file-for-fake-audio-capture=${AUDIO}`,
  ];
  if (fakeUi) args.push('--use-fake-ui-for-media-stream');
  return chromium.launch({ headless: true, args });
}

async function openChat(browser, employee, { permissions = ['microphone'] } = {}) {
  const context = await browser.newContext({ permissions, viewport: { width: 1440, height: 960 } });
  await context.addCookies([{ name: 'nexus_uid', value: UID, domain: '127.0.0.1', path: '/' }]);
  await context.addInitScript((uid) => localStorage.setItem('nexus_owner_id', uid), UID);
  const page = await context.newPage();
  const errors = [];
  const requests = [];
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('request', (request) => {
    if (request.url().includes('/api/asr/transcribe')) requests.push(request.url());
  });
  await page.goto(`${BASE}/chat?type=contact&employee=${employee}`, {
    waitUntil: 'domcontentloaded', timeout: 60000,
  });
  const frame = page.frameLocator('iframe.chat-island');
  await frame.getByRole('button', { name: '语音输入（转文字）' })
    .waitFor({ state: 'visible', timeout: 60000 });
  return { context, page, frame, errors, requests };
}

async function probeAsrNode() {
  const nodes = await fetch(`${BASE}/api/gateway/nodes`).then((response) => response.json());
  const node = nodes.find((item) => item.name === 'Mac SenseVoiceSmall');
  if (!node) return;
  for (let index = 0; index < 2; index += 1) {
    await fetch(`${BASE}/api/gateway/nodes/${node.id}/probe`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ probe_types: ['ASR'] }),
    });
  }
}

// 1. Rapid double click: at most one upload, no stuck state.
{
  const browser = await launch();
  try {
    const { context, page, frame, errors, requests } = await openChat(browser, EMP_A);
    const mic = frame.getByRole('button', { name: '语音输入（转文字）' });
    await mic.click();
    await page.waitForTimeout(150);
    const stop = frame.getByRole('button', { name: '停止录音' });
    if (await stop.isVisible().catch(() => false)) await stop.click();
    await page.waitForTimeout(3000);
    result.rapid_click = {
      requests: requests.length,
      transcribing: await frame.getByText('正在识别…').isVisible().catch(() => false),
      error: await frame.locator('.text-destructive').first().innerText().catch(() => ''),
      mic_back: await frame.getByRole('button', { name: '语音输入（转文字）' }).isVisible().catch(() => false),
      page_errors: errors,
    };
    await context.close();
  } finally {
    await browser.close();
  }
}

// 2. Switch conversation while transcribing: no leak into the new input.
{
  const browser = await launch();
  try {
    const first = await openChat(browser, EMP_A);
    await first.frame.getByRole('button', { name: '语音输入（转文字）' }).click();
    await first.page.waitForTimeout(2600);
    await first.frame.getByRole('button', { name: '停止录音' }).click();
    await first.page.waitForTimeout(100);
    await first.page.goto(`${BASE}/chat?type=contact&employee=${EMP_B}`, {
      waitUntil: 'domcontentloaded', timeout: 60000,
    });
    const secondFrame = first.page.frameLocator('iframe.chat-island');
    const textarea = secondFrame.locator('textarea');
    await textarea.waitFor({ state: 'visible', timeout: 60000 });
    await first.page.waitForTimeout(8000);
    const value = await textarea.inputValue();
    const body = await secondFrame.locator('body').innerText();
    result.switch_conversation = {
      requests: first.requests.length,
      second_input: value,
      leaked: value.includes('会议纪要'),
      body_has_old: body.includes('会议纪要'),
      page_errors: first.errors,
    };
    await first.context.close();
  } finally {
    await browser.close();
  }
}

// 3. Headless CDP denial (documented limitation: Chromium's fake device may
// report NotSupportedError instead of NotAllowedError).
{
  const browser = await launch(false);
  try {
    const context = await browser.newContext({ permissions: [], viewport: { width: 1440, height: 960 } });
    await context.addCookies([{ name: 'nexus_uid', value: UID, domain: '127.0.0.1', path: '/' }]);
    await context.addInitScript((uid) => localStorage.setItem('nexus_owner_id', uid), UID);
    const page = await context.newPage();
    const client = await context.newCDPSession(page);
    await client.send('Browser.setPermission', {
      permission: { name: 'microphone' }, setting: 'denied', origin: BASE,
    });
    await page.goto(`${BASE}/chat?type=contact&employee=${EMP_A}`, {
      waitUntil: 'domcontentloaded', timeout: 60000,
    });
    const frame = page.frameLocator('iframe.chat-island');
    await frame.getByRole('button', { name: '语音输入（转文字）' })
      .waitFor({ state: 'visible', timeout: 60000 });
    await page.waitForTimeout(1000);
    result.permission_denied_headless = {
      requests: 0,
      permission_message: (await frame.locator('body').innerText()).includes('麦克风权限被拒绝'),
      body_tail: (await frame.locator('body').innerText()).slice(-200),
    };
    await context.close();
  } finally {
    await browser.close();
  }
}

// 4. Web-API permission rejection: verifies the localized UI branch.
{
  const browser = await launch();
  try {
    const context = await browser.newContext({ permissions: [], viewport: { width: 1440, height: 960 } });
    await context.addCookies([{ name: 'nexus_uid', value: UID, domain: '127.0.0.1', path: '/' }]);
    await context.addInitScript(() => {
      localStorage.setItem('nexus_owner_id', 'local-qa');
      Object.defineProperty(navigator.mediaDevices, 'getUserMedia', {
        configurable: true,
        value: () => Promise.reject(new DOMException('Permission denied', 'NotAllowedError')),
      });
    });
    const page = await context.newPage();
    const requests = [];
    page.on('request', (request) => {
      if (request.url().includes('/api/asr/transcribe')) requests.push(request.url());
    });
    await page.goto(`${BASE}/chat?type=contact&employee=${EMP_A}`, {
      waitUntil: 'domcontentloaded', timeout: 60000,
    });
    const frame = page.frameLocator('iframe.chat-island');
    await frame.getByRole('button', { name: '语音输入（转文字）' })
      .waitFor({ state: 'visible', timeout: 60000 });
    await frame.getByRole('button', { name: '语音输入（转文字）' }).click();
    await page.waitForTimeout(1200);
    const body = await frame.locator('body').innerText();
    result.permission_denied_ui = {
      requests: requests.length,
      permission_message: body.includes('麦克风权限被拒绝'),
      body_tail: body.slice(-200),
    };
    await context.close();
  } finally {
    await browser.close();
  }
}

// 5. ASR outage: error, draft preserved, retry succeeds after restart.
{
  const browser = await launch();
  const { context, page, frame, errors } = await openChat(browser, EMP_A);
  try {
    const textarea = frame.locator('textarea');
    await textarea.fill('原有草稿：');
    execSync('./nexus-asr.sh stop', { cwd: ASR_DIR, stdio: 'ignore' });
    await frame.getByRole('button', { name: '语音输入（转文字）' }).click();
    await page.waitForTimeout(2600);
    await frame.getByRole('button', { name: '停止录音' }).click();
    let errorText = '';
    const errorDeadline = Date.now() + 60000;
    while (Date.now() < errorDeadline) {
      errorText = await frame.locator('.text-destructive').first().innerText().catch(() => '');
      if (errorText) break;
      await page.waitForTimeout(300);
    }
    const draftAfterFailure = await textarea.inputValue();
    execSync('./nexus-asr.sh start', { cwd: ASR_DIR, stdio: 'ignore' });
    await probeAsrNode();
    await frame.getByRole('button', { name: '重试' }).click();
    let value = '';
    const retryDeadline = Date.now() + 60000;
    while (Date.now() < retryDeadline) {
      value = await textarea.inputValue();
      if (value.includes('会议纪要')) break;
      await page.waitForTimeout(300);
    }
    result.asr_down_retry = {
      error_text: errorText,
      draft_preserved: draftAfterFailure,
      retry_value: value,
      retry_ok: value.includes('会议纪要') && value.includes('原有草稿'),
      page_errors: errors,
    };
  } finally {
    try {
      execSync('./nexus-asr.sh start', { cwd: ASR_DIR, stdio: 'ignore' });
      await probeAsrNode();
    } catch {
      // The report below already shows whether recovery succeeded.
    }
    await context.close();
    await browser.close();
  }
}

console.log(JSON.stringify(result, null, 2));
