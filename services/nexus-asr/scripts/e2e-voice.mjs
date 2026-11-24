#!/usr/bin/env node
/**
 * Real browser E2E: fake microphone -> MediaRecorder -> Nexus API -> gateway
 * -> SenseVoiceSmall -> editable input -> manual send -> AgentScope reply.
 *
 * Env:
 *   NEXUS_BASE            default http://127.0.0.1:18120
 *   NEXUS_UID             default local-qa
 *   NEXUS_EMPLOYEE_ID     required for MODE=private
 *   NEXUS_CHANNEL_ID      required for MODE=group
 *   NEXUS_AUDIO           WAV file for Chromium's fake microphone
 *   NEXUS_RECORD_MS       recording length, default 4000
 *   NEXUS_FINAL_TEXT      text to put in the input after transcription
 *   MODE                  private | group
 *   PLAYWRIGHT_PATH       override Playwright entry point
 *
 * The fake microphone is a Chromium test device, not a mock of the ASR API:
 * the browser still runs getUserMedia + MediaRecorder and uploads real WebM.
 */
const playwrightPath = process.env.PLAYWRIGHT_PATH
  || new URL('../../../frontend/node_modules/playwright/index.mjs', import.meta.url).href;
const { chromium } = await import(playwrightPath);
import { prepareChatIdentity, messageText } from './browser-auth.mjs';
let apiCookie = '';

const BASE = process.env.NEXUS_BASE || 'http://127.0.0.1:18120';
const UID = process.env.NEXUS_UID || 'local-qa';
const MODE = process.env.MODE || 'private';
const EMP = process.env.NEXUS_EMPLOYEE_ID || '';
const CHANNEL = process.env.NEXUS_CHANNEL_ID || '';
const AUDIO = process.env.NEXUS_AUDIO || '/tmp/case1-48k.wav';
const RECORD_MS = Number(process.env.NEXUS_RECORD_MS || 4000);
const FINAL_TEXT = process.env.NEXUS_FINAL_TEXT || '';
const SCREENSHOT = process.env.NEXUS_SCREENSHOT || `/tmp/nexus-asr-e2e-${MODE}.png`;
if (MODE === 'private' && !EMP) throw new Error('NEXUS_EMPLOYEE_ID is required for MODE=private');
if (MODE === 'group' && !CHANNEL) throw new Error('NEXUS_CHANNEL_ID is required for MODE=group');

const CHAT_URL = MODE === 'group'
  ? `${BASE}/chat?group=${encodeURIComponent(CHANNEL)}`
  : `${BASE}/chat?employee=${encodeURIComponent(EMP)}`;

async function api(path, init = {}) {
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { Cookie: apiCookie, ...(init.headers || {}) },
  });
  if (!response.ok) throw new Error(`${path} -> ${response.status} ${await response.text()}`);
  return response.json();
}

async function loadConversationId() {
  const binding = await api(`/api/v1/employees/${encodeURIComponent(EMP)}/conversations`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
  });
  return binding.id;
}

async function waitReply(conversationId, finalText) {
  const path = MODE === 'group'
    ? `/api/v1/channels/${CHANNEL}/messages`
    : `/api/v1/conversations/${conversationId}/messages`;
  const latest = Date.now() + 90000;
  while (Date.now() < latest) {
    const payload = await api(path);
    const rows = payload.items ?? payload.messages.map(message => ({
      sender_type: message.role === 'user' ? 'human' : message.role === 'assistant' ? 'employee' : 'system',
      sender_name: message.name,
      text: messageText(message),
    }));
    const sent = rows.findLastIndex(row => row.sender_type === 'human' && row.text === finalText);
    const reply = sent >= 0 && rows.slice(sent + 1).find(row => row.sender_type === 'employee' && row.text);
    if (reply) return reply;
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  throw new Error('timed out waiting for the AgentScope reply');
}

const browser = await chromium.launch({
  headless: true,
  args: [
    '--use-fake-device-for-media-stream',
    '--use-fake-ui-for-media-stream',
    `--use-file-for-fake-audio-capture=${AUDIO}`,
    '--autoplay-policy=no-user-gesture-required',
  ],
});
try {
  const context = await browser.newContext({
    permissions: ['microphone'],
    viewport: { width: 1440, height: 960 },
  });
  apiCookie = await prepareChatIdentity(context, BASE, UID);

  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (error) => errors.push(`pageerror: ${error.message}`));
  page.on('requestfailed', (request) => {
    if (!request.url().includes('/stream') && !request.url().includes('/events')) {
      errors.push(`requestfailed: ${request.url()} ${request.failure()?.errorText}`);
    }
  });

  await page.goto(CHAT_URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
  const frame = page;
  const mic = frame.getByRole('button', { name: '语音输入（转文字）' });
  await mic.waitFor({ state: 'visible', timeout: 60000 });
  const textarea = frame.getByTestId('compose-input');
  await textarea.waitFor({ state: 'visible', timeout: 30000 });
  const bodyBefore = await frame.locator('body').innerText();

  await mic.click();
  await page.waitForTimeout(800);
  if (!(await frame.getByText(/录音中/).first().isVisible().catch(() => false))) {
    throw new Error('recording state did not start');
  }
  await page.waitForTimeout(RECORD_MS);
  const stopAt = Date.now();
  await frame.getByRole('button', { name: '停止录音' }).click();

  let transcript = '';
  const transcriptDeadline = Date.now() + 60000;
  while (Date.now() < transcriptDeadline) {
    transcript = (await textarea.inputValue().catch(() => '')).trim();
    if (transcript) break;
    await page.waitForTimeout(200);
  }
  const transcriptAt = Date.now();
  if (!transcript) throw new Error('no transcript appeared in the input');
  if ((await frame.locator('body').innerText()).includes(transcript) && !bodyBefore.includes(transcript)) {
    throw new Error('transcript was auto-sent without user confirmation');
  }

  const transcriptShot = SCREENSHOT.replace(/\.png$/, '-transcript.png');
  await page.screenshot({ path: transcriptShot, fullPage: true });
  const finalText = FINAL_TEXT || transcript;
  await textarea.fill(finalText);
  const sendAt = Date.now();
  await frame.getByRole('button', { name: '发送消息' }).click();
  await page.waitForTimeout(1200);
  const conversationId = MODE === 'group' ? null : await loadConversationId();
  const reply = await waitReply(conversationId, finalText);

  const compact = (value) => value.replace(/[*#`>_[\]()]/g, '').replace(/\s+/g, '');
  const snippet = compact(reply.text).slice(0, 10);
  let rendered = false;
  const uiDeadline = Date.now() + 45000;
  while (Date.now() < uiDeadline) {
    if (compact(await frame.locator('body').innerText()).includes(snippet)) {
      rendered = true;
      break;
    }
    await page.waitForTimeout(500);
  }
  if (!rendered) throw new Error(`reply not rendered in UI: ${snippet}`);
  const uiReplyAt = Date.now();
  await page.screenshot({ path: SCREENSHOT, fullPage: true });
  console.log(JSON.stringify({
    mode: MODE,
    url: CHAT_URL,
    audio: AUDIO,
    conversation_id: conversationId,
    transcript,
    final_text: finalText,
    stop_to_transcript_ms: transcriptAt - stopAt,
    send_to_reply_ms: uiReplyAt - sendAt,
    auto_sent: false,
    reply_speaker: reply.sender_name,
    reply: reply.text.slice(0, 300),
    screenshot: SCREENSHOT,
    transcript_screenshot: transcriptShot,
    errors,
  }, null, 2));
} finally {
  await browser.close();
}
