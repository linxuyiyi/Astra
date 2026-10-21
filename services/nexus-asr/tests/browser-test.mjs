import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from './playwright.mjs';

const BASE = process.env.NEXUS_ASR_BASE || 'http://127.0.0.1:19899';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const audio = path.join(root, 'testdata', 'zh-5s-mic-48k.wav');

const browser = await chromium.launch({
  channel: 'chrome',
  headless: true,
  args: [
    '--use-fake-ui-for-media-stream',
    '--use-fake-device-for-media-stream',
    `--use-file-for-fake-audio-capture=${audio}`,
  ],
});
const page = await browser.newPage({ viewport: { width: 1280, height: 1100 } });
const errors = [];
page.on('pageerror', (e) => errors.push(e.message));
page.on('console', (m) => {
  if (m.type() === 'error') errors.push(m.text());
});

await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
await page.waitForFunction(() => !document.getElementById('micBtn').disabled, null, { timeout: 60000 });
console.log('page ready');
await page.click('#micBtn');
await page.waitForFunction(() => document.getElementById('micBtn').classList.contains('recording'), null, { timeout: 10000 });
await page.waitForTimeout(5200);
await page.click('#micBtn');
await page.waitForFunction(() => document.getElementById('transcript').value.length > 0, null, { timeout: 60000 });
const result = await page.evaluate(() => ({
  text: document.getElementById('transcript').value,
  duration: document.getElementById('statDuration').textContent,
  total: document.getElementById('statTotal').textContent,
  elapsed: document.getElementById('statElapsed').textContent,
  rtf: document.getElementById('statRtf').textContent,
  bytes: document.getElementById('statBytes').textContent,
  requestId: document.getElementById('rId').textContent,
  status: document.getElementById('rStatus').textContent,
}));
console.log(JSON.stringify(result, null, 2));

// Retry path: reuse the last recording.
await page.click('#retryBtn');
await page.waitForFunction(() => document.getElementById('rStatus').textContent === '失败'
  || document.getElementById('reqTag').textContent === '完成', null, { timeout: 60000 });
await page.waitForTimeout(500);
const retry = await page.evaluate(() => ({
  status: document.getElementById('rStatus').textContent,
  text: document.getElementById('transcript').value,
}));
console.log('retry', JSON.stringify(retry));

await page.screenshot({ path: path.join(root, 'docs', 'screenshot-test-page.png'), fullPage: true });
console.log('page errors:', errors.length ? errors : 'none');
await browser.close();
if (!result.text.includes('会议纪要')) {
  console.error('unexpected transcript');
  process.exit(1);
}
