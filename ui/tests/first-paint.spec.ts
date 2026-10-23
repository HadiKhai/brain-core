import { test, expect } from '@playwright/test';

test('partial data stays marked after idle and backend completion until the full reading is delivered', async ({ page }) => {
  await page.clock.install();
  await page.addInitScript(() => {
    const w = window as any;
    w.streams = [];
    w.EventSource = class {
      closed = false;
      constructor() { w.streams.push(this); }
      close() { this.closed = true; }
    };
  });
  let full = false, reads = 0, waiting = false;
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/api/loading?*', route => route.fulfill({ json: { version: full ? 2 : 1, refreshing: !full, failed: false } }));
  await page.route('**/api/state?*', async route => {
    const data = await (await route.fetch()).json();
    reads++;
    data.brain.filling_in = reads === 1;
    if (reads > 1) { waiting = true; await gate; data.nodes[1].title = 'Full Field Guide'; }
    await route.fulfill({ json: data, headers: { 'X-Brain-UI-Version': reads === 1 ? '1' : '2' } });
  });
  try {
    await page.goto('/?k=demo');
    await expect(page.getByRole('button', { name: 'Field Guide', exact: true })).toBeVisible();
    await expect(page.locator('[data-reading-progress]')).toContainText('Filling in');
    await page.clock.fastForward(5_000);
    expect(await page.evaluate(() => (window as any).streams[0].closed)).toBe(true);
    full = true;
    await page.clock.fastForward(1_001);
    await expect.poll(() => waiting).toBe(true);
    await expect(page.locator('[data-reading-progress]')).toContainText(/Filling in.*\d+s old/);
    release();
    await expect(page.getByRole('button', { name: 'Full Field Guide', exact: true })).toBeVisible();
    await expect(page.locator('[data-reading-progress]')).toHaveCount(0);
  } finally { release(); }
});

test('an idle cached reading keeps its age until the current version is delivered', async ({ page }) => {
  await page.clock.install();
  await page.addInitScript(() => { (window as any).EventSource = class { close() {} }; });
  let version = 1, reads = 0;
  await page.route('**/api/loading?*', route => route.fulfill({ json: { version, refreshing: false, failed: false } }));
  await page.route('**/api/state?*', async route => {
    const data = await (await route.fetch()).json();
    reads++;
    await route.fulfill({ json: data, headers: { 'X-Brain-UI-Version': String(version) } });
  });
  await page.goto('/?k=demo');
  await expect(page.locator('[data-page="overview"] [data-kpi]')).toHaveCount(5);
  await expect(page.locator('[data-reading-progress]')).toHaveCount(0);
  await page.clock.fastForward(5_000);
  version = 2;
  await page.clock.fastForward(1_001);
  await expect(page.locator('[data-reading-progress]')).toContainText(/reading \d+s old/);
  expect(reads).toBe(1);
  await page.evaluate(() => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })));
  await expect.poll(() => reads).toBe(2);
  await expect(page.locator('[data-reading-progress]')).toHaveCount(0);
});
