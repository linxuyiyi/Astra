import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fixture, owner } from './chat_conversation_fixture.mjs';

const [before, after, output] = process.argv.slice(2);
assert(before && after && output, '需要指定修复前和修复后的 dist 目录，以及证据目录');
await fs.mkdir(output, { recursive: true });
const { chromium } = await import(process.env.NEXUS_PLAYWRIGHT_MODULE || 'playwright');
const browser = await chromium.launch({ headless: true });
const sizes = [[1440, 900], [1280, 720]];
const scenarios = [
  ['private', '/chat?employee=qa', '当前验收记录'],
  ['group', '/chat?group=release', '发布窗口已确认。请把浏览器复核结果补进回归报告'],
];

async function measure(variant, dist) {
  const app = await fixture(dist, { regression: true, peerFailure: false });
  const measurements = [];
  try {
    for (const [width, height] of sizes) {
      const page = await browser.newPage({
        viewport: { width, height }, locale: 'zh-CN', timezoneId: 'Asia/Shanghai',
      });
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.addInitScript(uid => localStorage.setItem('nexus_owner_id', uid), owner);
      try {
        for (const [scenario, route, sentinel] of scenarios) {
          await page.goto(app.url + route);
          const viewport = page.getByTestId('chat-messages');
          await viewport.getByText(sentinel).last().waitFor();
          const result = await viewport.evaluate(async element => {
            await document.fonts.ready;
            element.scrollTop = element.scrollHeight;
            await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
            const viewportBox = element.getBoundingClientRect();
            const rows = [...element.querySelectorAll('.nc-message')];
            const fullyVisibleMessageRows = rows.filter(row => {
              const box = row.getBoundingClientRect();
              return box.top >= viewportBox.top && box.bottom <= viewportBox.bottom;
            }).length;
            const prose = getComputedStyle([...element.querySelectorAll('.nc-prose')].at(-1));
            return {
              viewportHeight: element.clientHeight,
              horizontalOverflow: element.scrollWidth > element.clientWidth + 1,
              fullyVisibleMessageRows,
              allRenderedRows: rows.length,
              proseFontPx: parseFloat(prose.fontSize),
              proseLineHeightPx: parseFloat(prose.lineHeight),
            };
          });
          assert.equal(result.horizontalOverflow, false);
          assert.deepEqual(errors, []);
          await page.screenshot({ path: path.join(output, `${variant}-${scenario}-${width}.png`) });
          measurements.push({ variant, scenario, viewport: [width, height], ...result });
        }
      } finally {
        await page.close();
      }
    }
  } finally {
    await app.close();
  }
  return measurements;
}

try {
  const baseline = await measure('before', before);
  const repaired = await measure('after', after);
  repaired.forEach((row, index) => {
    const prior = baseline[index];
    assert(row.fullyVisibleMessageRows > prior.fullyVisibleMessageRows, JSON.stringify([prior, row]));
  });
  const report = {
    chromium: browser.version(),
    node: process.version,
    scenario: 'Production bundles with the same 320-private and 95-group history fixtures, bottom anchored',
    measurements: [...baseline, ...repaired],
  };
  await fs.writeFile(path.join(output, 'density.json'), JSON.stringify(report, null, 2) + '\n');
} finally {
  await browser.close();
}
