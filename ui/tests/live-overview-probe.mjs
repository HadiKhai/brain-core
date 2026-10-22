// Run outside Playwright Test so a failed live assertion cannot save private DOM,
// an access key, or page URLs in its automatic failure artifacts. Output counts only.
import { chromium } from 'playwright';
let browser;
const result = { validTarget: false, overview: false, kpis: 0, nonempty: false, fatal: 0, consoleErrors: 0, pageErrors: 0, forbiddenRequests: 0, readyMs: null, retained: false };
try {
  const url = new URL(process.env.BRAIN_UI_LIVE_URL);
  if (url.protocol !== 'http:' || !['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname) || !url.searchParams.get('k')) throw Error();
  result.validTarget = true;
  url.hash = 'view=overview';
  browser = await chromium.launch({ channel: process.env.BRAIN_UI_BROWSER_CHANNEL || undefined, headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, serviceWorkers: 'block' });
  const page = await context.newPage();
  page.on('console', message => { if (message.type() === 'error') result.consoleErrors++; });
  page.on('pageerror', () => result.pageErrors++);
  await context.route('**/*', route => {
    const request = route.request();
    if (request.method() !== 'GET' || new URL(request.url()).origin !== url.origin) {
      result.forbiddenRequests++;
      return route.abort();
    }
    return route.continue();
  });
  const start = Date.now();
  await page.goto(url.href, { waitUntil: 'domcontentloaded', timeout: 60_000 });
  const overview = page.locator('[data-page="overview"]:not([hidden])');
  await overview.waitFor({ state: 'visible', timeout: 60_000 });
  result.readyMs = Date.now() - start;
  result.overview = true;
  result.kpis = await overview.locator('[data-kpi]').count();
  result.nonempty = (await overview.innerText()).trim().length > 100;
  await page.waitForTimeout(8_000); // Includes the idle transition; the reading must remain.
  result.retained = await overview.isVisible();
  result.fatal = await page.locator('.fatal-error').count();
} catch {
  result.failed = true; // Never emit an exception, URL, raw response or live text.
} finally {
  try { await browser?.close(); } catch { result.failed = true; }
}
process.stdout.write(JSON.stringify(result));
