import { createRequire } from 'node:module';
import path from 'node:path';

const require = createRequire(import.meta.url);
const candidates = [
  process.env.PLAYWRIGHT_PATH,
  path.resolve(process.cwd(), 'node_modules/playwright'),
  '/Users/niuniu/程序/Astra/frontend/node_modules/playwright',
  'playwright',
].filter(Boolean);

let playwright;
let lastError;
for (const candidate of candidates) {
  try {
    playwright = require(candidate);
    break;
  } catch (err) {
    lastError = err;
  }
}
if (!playwright) {
  throw new Error('Playwright not found; set PLAYWRIGHT_PATH or npm i -D playwright\n' + lastError);
}
export const chromium = playwright.chromium;
