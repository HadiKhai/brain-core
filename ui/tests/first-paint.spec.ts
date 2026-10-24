import { test, expect } from '@playwright/test';
import { performance } from 'node:perf_hooks';

async function capture(page: import('@playwright/test').Page, phase: string) {
  if (process.env.UI_FIRST_PAINT_CAPTURE)
    await page.screenshot({ path: `${process.env.UI_FIRST_PAINT_CAPTURE}/${phase}.png` });
}

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
    const start = performance.now();
    await page.goto('/?k=demo');
    await expect(page.getByRole('button', { name: 'Field Guide', exact: true })).toBeVisible();
    await expect(page.locator('[data-reading-progress]')).toContainText('Filling in');
    const firstPaintMs = performance.now() - start;
    console.log(`partial reading visible: ${firstPaintMs.toFixed(1)}ms`);
    expect(firstPaintMs, 'partial data and its marker must render within two seconds').toBeLessThan(2_000);
    // Check three displayed values against the same synthetic source.
    const source = await (await page.request.get('/api/state?k=demo')).json();
    for (const node of source.nodes)
      await expect(page.getByRole('button', { name: node.title, exact: true })).toBeVisible();
    await capture(page, 'pending');
    await page.clock.fastForward(5_000);
    expect(await page.evaluate(() => (window as any).streams[0].closed)).toBe(true);
    full = true;
    await page.clock.fastForward(1_001);
    await expect.poll(() => waiting).toBe(true);
    await expect(page.locator('[data-reading-progress]')).toContainText(/Filling in.*\d+s old/);
    release();
    await expect(page.getByRole('button', { name: 'Full Field Guide', exact: true })).toBeVisible();
    await expect(page.locator('[data-reading-progress]')).toHaveCount(0);
    await capture(page, 'filled');
  } finally { release(); }
});

test('a failed partial reading uses the loading notice on a narrow screen until delivery succeeds', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.clock.install();
  await page.addInitScript(() => {
    const w = window as any;
    w.EventSource = class { constructor() { w.stream = this; } close() {} };
  });
  let full = false;
  await page.route('**/api/loading?*', route => route.fulfill({ json: { version: full ? 2 : 1, failed: !full, refreshing: false } }));
  await page.route('**/api/state?*', async route => {
    const data = await (await route.fetch()).json();
    data.brain.filling_in = true;
    await route.fulfill({ json: data, headers: { 'X-Brain-UI-Version': '1' } });
  });
  await page.goto('/?k=demo');
  const marker = page.locator('[data-reading-progress]');
  await expect(marker).toContainText(/Filling in.*Refresh failed/);
  await expect(marker).toHaveAttribute('data-slot', 'alert');
  const bounds = await marker.boundingBox();
  expect(bounds).not.toBeNull();
  expect(bounds!.x).toBeGreaterThanOrEqual(0);
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(390);
  await capture(page, 'narrow-pending');
  full = true;
  const data = await (await page.request.get('/api/state?k=demo')).json();
  data.brain.filling_in = false;
  data.nodes[1].title = 'Full Field Guide';
  await page.evaluate(value => (window as any).stream.onmessage({ data: JSON.stringify(value), lastEventId: '2' }), data);
  await page.clock.fastForward(1_001);
  await expect(marker).toHaveCount(0);
  await expect(page.locator('[data-page="overview"] [data-kpi]')).toHaveCount(5);
  await expect(page.locator('[data-page="overview"]')).toContainText('Full Field Guide');
  await capture(page, 'narrow-filled');
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
