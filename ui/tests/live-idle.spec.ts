import { test, expect } from '@playwright/test';

test('live transport backs off when idle, pauses when hidden and resumes on activity', async ({ page }) => {
  await page.clock.install();
  await page.addInitScript(() => {
    const w = window as any;
    w.streams = [];
    w.hiddenForTest = false;
    Object.defineProperty(document, 'hidden', { get: () => w.hiddenForTest });
    w.EventSource = class {
      closed = false;
      constructor() { w.streams.push(this); }
      close() { this.closed = true; }
    };
  });
  let reads = 0;
  page.on('request', request => { if (new URL(request.url()).pathname === '/api/state') reads++; });
  await page.goto('/?k=demo');
  await expect(page.locator('[data-page]:not([hidden])')).toBeVisible();
  const openStreams = () => page.evaluate(() => (window as any).streams.filter((s: any) => !s.closed).length);
  await expect.poll(openStreams).toBe(1);
  await page.clock.fastForward(31_000);
  expect(await openStreams()).toBe(0);
  const initial = reads;
  await page.clock.fastForward(58_000);
  expect(reads).toBe(initial);
  await page.clock.fastForward(2_000);
  await expect.poll(() => reads).toBe(initial + 1);
  expect(await openStreams()).toBe(0);
  await page.evaluate(() => { (window as any).hiddenForTest = true; document.dispatchEvent(new Event('visibilitychange')); });
  const hiddenReads = reads;
  await page.clock.fastForward(180_000);
  expect(reads).toBe(hiddenReads);
  await page.evaluate(() => { (window as any).hiddenForTest = false; document.dispatchEvent(new Event('visibilitychange')); });
  await expect.poll(openStreams).toBe(1);
  await page.clock.fastForward(31_000);
  expect(await openStreams()).toBe(0);
  await page.evaluate(() => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })));
  await expect.poll(openStreams).toBe(1);
});
