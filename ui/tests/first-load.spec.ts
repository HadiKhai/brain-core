import { test, expect } from '@playwright/test';

test('a large delayed first reading stays explicitly loading through idle and then fills', async ({ page }) => {
  await page.clock.install();
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  let waiting = false;
  await page.route('**/api/state?*', async route => {
    const data = await (await route.fetch()).json();
    data.transportPadding = 'x'.repeat(700_000); // Made-up large reading, never live data.
    waiting = true;
    await gate;
    await route.fulfill({ json: data });
  });
  await page.goto('/?k=demo');
  await expect.poll(() => waiting).toBe(true);
  await expect(page.getByText('Loading live state… 0s', { exact: true })).toBeVisible();
  await page.clock.fastForward(8_000);
  await expect(page.getByText('Loading live state… 8s', { exact: true })).toBeVisible();
  await expect(page.getByText('Workspace unavailable.', { exact: true })).toHaveCount(0);
  await expect(page.getByText('Updates each minute', { exact: true })).toHaveCount(0);
  await expect(page.getByText('Waiting for a reading', { exact: false })).toHaveCount(0);
  release();
  await expect(page.locator('[data-page="overview"] [data-kpi]')).toHaveCount(5);
  await expect(page.locator('[data-initial-load]')).toHaveCount(0);
});

test('a busy first load offers a retry after twenty seconds and the retry fills', async ({ page }) => {
  await page.clock.install();
  // Hold the response in browser memory without leaving an orphaned route on retry.
  await page.addInitScript(() => {
    const original = window.fetch;
    let first = true;
    window.fetch = (input, options) => {
      if (String(input).startsWith('/api/state') && first) {
        first = false;
        return new Promise((_, reject) => options?.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError'))));
      }
      return original(input, options);
    };
  });
  await page.goto('/?k=demo');
  await page.clock.fastForward(20_000);
  await expect(page.getByText('Still loading: the server is busy', { exact: true })).toBeVisible();
  await expect(page.getByText('Workspace unavailable.', { exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: 'Retry loading', exact: true }).click();
  await expect(page.locator('[data-page="overview"] [data-kpi]')).toHaveCount(5);
});

test('an unkeyed link explains how to open the complete private link without requesting state', async ({ page }) => {
  let requests = 0;
  page.on('request', request => { if (new URL(request.url()).pathname.startsWith('/api/')) requests++; });
  await page.goto('/');
  await expect(page.getByText('This link is missing its access key', { exact: true })).toBeVisible();
  await expect(page.getByRole('alert')).toContainText('Open the full link from your brief or brain show.');
  expect(requests).toBe(0);
  await expect(page.locator('.skeleton-page, .fatal-error, [data-initial-load]')).toHaveCount(0);
});

for (const failure of ['403', '503', 'network', 'invalid-json']) {
  test(`a first-load ${failure} stays an explicit error after the idle timer`, async ({ page }) => {
    await page.clock.install();
    await page.route('**/api/state?*', route => failure === 'network' ? route.abort('failed') : route.fulfill({
      status: failure === 'invalid-json' ? 200 : Number(failure),
      contentType: 'application/json', body: 'invalid reading',
    }));
    await page.goto('/?k=rejected');
    const title = failure === '403' ? 'This access key was not accepted' : 'Could not reach your brain';
    await expect(page.getByText(title, { exact: true })).toBeVisible();
    await page.clock.fastForward(8_000);
    await expect(page.getByText(title, { exact: true })).toBeVisible();
    await expect(page.locator('[data-initial-load], .skeleton-page')).toHaveCount(0);
    await expect(page.getByText('Updates each minute', { exact: true })).toHaveCount(0);
  });
}
