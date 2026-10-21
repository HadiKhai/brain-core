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
  await page.clock.fastForward(6_000);
  expect(await openStreams()).toBe(0);
  await expect(page.getByText('Updates each minute', { exact: true })).toBeVisible();
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
  await page.clock.fastForward(6_000);
  expect(await openStreams()).toBe(0);
  await page.evaluate(() => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })));
  await expect.poll(openStreams).toBe(1);
});

test('opted-in notifications keep a bounded hidden-tab poll', async ({ page }) => {
  await page.clock.install();
  await page.addInitScript(() => {
    const w = window as any;
    w.notices = [];
    w.hiddenForTest = false;
    Object.defineProperty(document, 'hidden', { get: () => w.hiddenForTest });
    localStorage.setItem('brain-notify', '1');
    w.Notification = class {
      static permission = 'granted';
      constructor(title: string) { w.notices.push(title); }
    };
    w.EventSource = class { close() {} };
  });
  let reads = 0;
  await page.route('**/api/state?*', async route => {
    const data = await (await route.fetch()).json();
    if (++reads > 1) data.inbox.push({ id: 'new-hidden-question', node: 'field-guide', text: 'A made-up review question', kind: 'inbox' });
    await route.fulfill({ json: data });
  });
  await page.goto('/?k=demo');
  await expect(page.locator('[data-page]:not([hidden])')).toBeVisible();
  const initialNotices = await page.evaluate(() => (window as any).notices.length);
  await page.evaluate(() => { (window as any).hiddenForTest = true; document.dispatchEvent(new Event('visibilitychange')); });
  await page.clock.fastForward(60_001);
  await expect.poll(() => reads).toBe(2);
  await expect.poll(() => page.evaluate(() => (window as any).notices.length)).toBe(initialNotices + 1);
  await page.clock.fastForward(59_000);
  expect(reads).toBe(2);
});
