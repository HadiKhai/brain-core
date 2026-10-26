// Optional development-tool runner; the serving core still needs only stdlib Python.
// --tools selects an existing UI checkout with the pinned browser dependencies.
import { createRequire } from 'node:module';
import { readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { parseArgs } from 'node:util';
import { execFileSync } from 'node:child_process';

const { values } = parseArgs({ options: {
  'url-file': { type: 'string' }, output: { type: 'string' }, tools: { type: 'string' },
} });
if (!values['url-file'] || !values.output) throw Error('Supply --url-file and --output');
const here = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(path.resolve(values.tools || path.join(here, '..'), 'package.json'));
const { chromium } = require('@playwright/test');
const { url: target } = JSON.parse(await readFile(values['url-file'], 'utf8'));
const url = new URL(target);
if (url.protocol !== 'http:' || !['127.0.0.1', 'localhost'].includes(url.hostname) || !url.searchParams.get('k'))
  throw Error('The target must be an authenticated loopback serving URL');
const source = await readFile(path.join(here, 'dashboard-acceptance.js'), 'utf8');
const browser = await chromium.launch({ headless: true });
const result = { head: execFileSync('git', ['rev-parse', 'HEAD'], { cwd: path.join(here, '../..'), encoding: 'utf8' }).trim(),
  browser: browser.version(), headless: true, firstPaint: [], views: null, pageErrors: 0, consoleErrors: 0, forbiddenRequests: 0 };
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, serviceWorkers: 'block' });
  await context.route('**/*', route => {
    const request = route.request();
    if (request.method() !== 'GET' || new URL(request.url()).origin !== url.origin) {
      result.forbiddenRequests++;
      return route.abort();
    }
    return route.continue();
  });
  const page = await context.newPage();
  page.on('pageerror', () => result.pageErrors++);
  page.on('console', message => { if (message.type() === 'error') result.consoleErrors++; });
  for (let i = 0; i < 3; i++) {
    url.searchParams.set('sample', String(i));
    await page.goto(url.href, { waitUntil: 'domcontentloaded', timeout: 15000 });
    result.firstPaint.push(await page.evaluate('(' + source + ')("first-reading")'));
  }
  result.views = await page.evaluate('(' + source + ')("views")');
  result.medianFirstPaintMs = result.firstPaint.map(row => row.domFirstDataPaintUpperBoundMs).sort((a, b) => a - b)[1];
  if (result.pageErrors || result.consoleErrors || result.forbiddenRequests) throw Error('Browser diagnostics failed');
} finally {
  await browser.close();
  await writeFile(values.output, JSON.stringify(result, null, 2) + '\n', { mode: 0o600 });
}
console.log(JSON.stringify({ head: result.head, headless: true, medianFirstPaintMs: result.medianFirstPaintMs,
  viewsComplete: result.views?.allViewsComplete, pageErrors: result.pageErrors, consoleErrors: result.consoleErrors }));
