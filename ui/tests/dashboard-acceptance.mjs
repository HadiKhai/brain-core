// Optional development-tool runner; the serving core still needs only stdlib Python.
// --tools selects an existing UI checkout with the pinned browser dependencies.
import { createRequire } from 'node:module';
import { readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { parseArgs } from 'node:util';
import { execFileSync } from 'node:child_process';
import { createDashboardOracle } from './dashboard-oracle.mjs';

const { values } = parseArgs({ options: {
  'url-file': { type: 'string' }, output: { type: 'string' }, tools: { type: 'string' },
} });
if (!values['url-file'] || !values.output) throw Error('Supply --url-file and --output');
const here = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(path.resolve(values.tools || path.join(here, '..'), 'package.json'));
const { chromium } = require('@playwright/test');
const ts = require('typescript');
const { url: target } = JSON.parse(await readFile(values['url-file'], 'utf8'));
const url = new URL(target);
if (url.protocol !== 'http:' || !['127.0.0.1', 'localhost'].includes(url.hostname) || !url.searchParams.get('k'))
  throw Error('The target must be an authenticated loopback serving URL');
const source = await readFile(path.join(here, 'dashboard-acceptance.js'), 'utf8');
const modelCode = ts.transpile(await readFile(path.join(here, '../src/model.ts'), 'utf8'),
  { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 });
const head = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: path.join(here, '../..'), encoding: 'utf8' }).trim();
const browser = await chromium.launch({ headless: true });
const result = { head,
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
  let firstReading;
  page.on('response', response => {
    if (!firstReading && new URL(response.url()).origin === url.origin &&
        new URL(response.url()).pathname === '/api/state' && response.ok()) {
      firstReading = response.json().then(state => ({ state }), error => ({ error: error.message }));
    }
  });
  const evaluate = (mode, deliveredReading) => page.evaluate(async ({ source, modelCode, oracleCode, mode, deliveredReading }) => {
    const model = new Function('exports', modelCode + ';return exports;')({});
    const oracle = new Function('return (' + oracleCode + ')')()(model);
    return new Function('model', 'oracle', 'reading', 'return (' + source + ')(' + JSON.stringify(mode) + ', model, oracle, reading)')(model, oracle, deliveredReading);
  }, { source, modelCode, oracleCode: createDashboardOracle.toString(), mode, deliveredReading });
  page.on('pageerror', () => result.pageErrors++);
  page.on('console', message => { if (message.type() === 'error') result.consoleErrors++; });
  for (let i = 0; i < 3; i++) {
    firstReading = null;
    url.searchParams.set('sample', String(i));
    await page.goto(url.href, { waitUntil: 'domcontentloaded', timeout: 15000 });
    const deadline = Date.now() + 15000;
    while (!firstReading) {
      if (Date.now() >= deadline) throw Error('The app made no successful backend state request');
      await page.waitForTimeout(5);
    }
    const observed = await firstReading;
    if (observed.error) throw Error('Could not read the app backend response');
    result.firstPaint.push(await evaluate('first-reading', observed.state));
  }
  result.views = await evaluate('views');
  result.medianFirstPaintMs = result.firstPaint.map(row => row.domFirstDataPaintUpperBoundMs).sort((a, b) => a - b)[1];
  if (result.pageErrors || result.consoleErrors || result.forbiddenRequests) throw Error('Browser diagnostics failed');
} finally {
  await browser.close();
  await writeFile(values.output, JSON.stringify(result, null, 2) + '\n', { mode: 0o600 });
}
console.log(JSON.stringify({ head: result.head, headless: true, medianFirstPaintMs: result.medianFirstPaintMs,
  viewsComplete: result.views?.allViewsComplete, pageErrors: result.pageErrors, consoleErrors: result.consoleErrors }));
