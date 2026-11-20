// Render the production bundle against the repository's generic dashboard fixture.
import { createRequire } from 'node:module';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import net from 'node:net';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const require = createRequire(path.join(root, 'ui/package.json'));
const { chromium } = require('@playwright/test');
const socket = net.createServer();
await new Promise(resolve => socket.listen(0, '127.0.0.1', resolve));
const port = socket.address().port;
await new Promise(resolve => socket.close(resolve));
const server = spawn('python3', [path.join(root, 'ui/tests/fixture.py'), '--port', String(port)],
  { cwd: path.join(root, 'ui'), env: { ...process.env, FIXTURE_QUIET: '1' }, stdio: ['ignore', 'pipe', 'pipe'] });
let browser;
try {
  await Promise.race([once(server.stdout, 'data'), once(server, 'exit').then(() => { throw Error('fixture exited'); })]);
  browser = await chromium.launch({ headless: true, channel: 'chrome' });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
  const now = Date.now() / 1000;
  await page.route('**/api/events?*', route => route.abort());
  await page.route('**/api/state?*', async route => {
    const response = await route.fetch();
    const data = await response.json();
    data.machine = { ...data.machine, disk_free_gb: 18, disk_total_gb: 1000, disk_reserve_gb: 43,
      disk_floor_gb: 12, admission: { action: 'admit', at: now, until: now + 60,
      disk_floor_gb: 12, actor: 'operator', reason: 'one named gate' } };
    await route.fulfill({ response, json: data });
  });
  await page.goto(`http://127.0.0.1:${port}/?k=demo#view=usage`);
  const usage = page.locator('[data-page=usage]');
  await usage.getByText('GB free · reserve 43 GB · admission floor 12 GB', { exact: true }).waitFor();
  await usage.getByRole('status').filter({ hasText: 'Bounded disk admission: 12 GB until' }).waitFor();
  assert.match(await usage.textContent(), /operator · one named gate/);
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1100 });
    await usage.getByText('Disk free', { exact: true }).scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(root, `docs/machine-admission/dashboard-${width}.png`) });
    const geometry = await page.evaluate(() => ({ viewport: innerWidth, document: document.documentElement.scrollWidth }));
    console.log(JSON.stringify({ width, ...geometry, reserve: 43, floor: 12, expiryVisible: true }));
    assert.ok(geometry.document <= geometry.viewport, 'horizontal document overflow');
  }
  console.log('PASS: production bundle shows free disk, declared reserve, active floor, actor, reason and expiry at both widths');
} finally {
  if (browser) await browser.close();
  server.kill('SIGTERM');
  await once(server, 'exit');
}
