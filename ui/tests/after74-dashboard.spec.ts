import { test as base, expect } from "@playwright/test";

const test = base.extend<{ state: any }>({
  state: async ({ request }, use) => {
    const original = await (await request.get('/api/state?k=demo')).json();
    const state = structuredClone(original);
    state.inbox = Array.from({ length: 35 }, (_, i) => ({ ...state.inbox[0], id: `ask-${i}`, text: `question: Choose the layout ${i}?`, source: 'field-guide/inbox.md' }));
    state.inbox.push(...[
      { id: 'order', text: 'DECISION verify-speed: Land the reviewed fix', recipient: 'coordinator' },
      { id: 'answered', text: 'question: Answered layout?', answered: true },
      { id: 'expired', text: 'question: Expired layout?', deadline: '2000-01-01T00:00:00Z' },
      { id: 'superseded', text: 'question: Superseded layout?', state: 'superseded' },
      { id: 'incomplete', text: 'question: Incomplete layout?', options: [] },
    ].map(row => ({ ...state.inbox[0], ...row })));
    state.due = [state.inbox[35], { ...state.due[0], id: 'ready' }, { ...state.due[0], id: 'closed', state: 'done' }];
    state.usage = Array.from({ length: 35 }, (_, i) => ({ ...state.usage[0], id: `usage-${i}`, label: `Subscription ${i}` }));
    const node = state.nodes[1];
    node.jobs.unsettled = 1;
    node.work.push({ branch: 'job/finished-layout', state: 'merged' }, { branch: 'job/old-unrecorded', state: 'Review approved' });
    node.jobs.rows.push({ Job: 'job/finished-layout', Ended: '2026-01-01', Outcome: 'merged' });
    node.jobs.rows.push(...Array.from({ length: 30 }, (_, i) => ({ ...node.jobs.rows[0], Job: `job/archive-${i}` })));
    node.tasks = { rows: Array.from({ length: 35 }, (_, i) => ({ id: `TASK-${i}`, title: `Layout task ${i}`, status: 'ready', priority: 'P2', owner: 'Demo developer' })) };
    await request.post('/fixture/update', { data: state });
    try { await use(state); } finally { await request.post('/fixture/update', { data: original }); }
  },
});

test('requests and decisions exclude coordinator orders and closed asks while Archive keeps source', async ({ page, state }) => {
  await page.goto('/?k=demo#view=inbox');
  const inbox = page.locator('[data-page=inbox]');
  await expect(inbox.getByRole('tab', { name: 'Requests 35', exact: true })).toBeVisible();
  await expect(inbox.getByRole('tab', { name: 'Open decisions 35', exact: true })).toBeVisible();
  await expect(inbox.locator('.request')).toHaveCount(10);
  await expect(inbox.locator('.request').first()).toContainText('Waiting on you');
  await inbox.getByRole('tab', { name: /^Coordinator steps/ }).click();
  await expect(inbox.locator('.due')).toHaveCount(1);
  await expect(inbox).not.toContainText('Land the reviewed fix');
  await inbox.getByRole('tab', { name: /^Archive/ }).click();
  await expect(inbox.locator('.request')).toHaveCount(6);
  await expect(inbox).not.toContainText('Waiting on you');
  await inbox.locator('.request').filter({ hasText: 'Answered layout' }).getByRole('button', { name: 'Read source' }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Technical details', exact: true }).click();
  await expect(page.getByRole('dialog').locator('.technical-facts')).toContainText('field-guide/inbox.md');
  await page.keyboard.press('Escape');
  await page.goto('/?k=demo#view=overview');
  await expect(page.locator('[data-page=overview] [data-kpi]').first().locator('.kpi-value')).toHaveText('35');
});

test('sidebar shows unfinished jobs and reveals history and settlement guidance on demand', async ({ page, state }) => {
  await page.goto('/?k=demo');
  const nav = page.getByRole('navigation', { name: 'Workspace navigation' });
  await expect(nav.getByText('Navigation', { exact: true })).toBeVisible();
  await expect(nav.getByText('Finished layout', { exact: true })).toHaveCount(0);
  await expect(nav.getByText('Old unrecorded', { exact: true })).toHaveCount(0);
  await nav.getByRole('button', { name: 'Show past jobs', exact: true }).click();
  await expect(nav.getByText('Finished layout', { exact: true })).toBeVisible();
  await expect(nav.getByText('Old unrecorded', { exact: true })).toBeVisible();
  await nav.getByRole('button', { name: 'Hide past jobs', exact: true }).click();
  await expect(nav.getByText('Finished layout', { exact: true })).toHaveCount(0);
  await nav.getByRole('button', { name: 'Settle threads (1)', exact: true }).click();
  await page.locator('[data-page=project]').getByRole('button', { name: 'Settle threads (1)', exact: true }).click();
  await expect(page.getByRole('dialog')).toContainText('check that no work or question is pending');
  await expect(page.getByRole('dialog').getByRole('link', { name: 'Open coordinator' })).toBeVisible();
});

for (const view of ['inbox', 'usage', 'jobs', 'tasks']) {
  test(`${view}: Show more preserves ten rows, appends ten and resets with navigation and filters`, async ({ page, state }) => {
    await page.goto(`/?k=demo#view=${view}`);
    const root = page.locator(`[data-page=${view}]`);
    if (view === 'tasks') await root.getByRole('tab', { name: 'List', exact: true }).click();
    const frame = root.locator('[data-list-frame]').first();
    const rows = view === 'inbox' ? frame.locator('.request') : view === 'usage' ? frame.locator('.subscription') : frame.locator('tbody tr');
    await expect(rows).toHaveCount(10);
    const first = await rows.first().textContent();
    await frame.getByRole('button', { name: 'Show more', exact: true }).click();
    await expect(rows).toHaveCount(20);
    await expect(rows.first()).toHaveText(first!);
    await expect(frame.locator('.list-count')).toContainText('1–20 of');
    await frame.getByRole('button', { name: 'Show more', exact: true }).click();
    await expect(rows).toHaveCount(30);
    await frame.getByRole('button', { name: 'Show more', exact: true }).click();
    const more = frame.getByRole('button', { name: 'Show more', exact: true });
    for (let i = 0; i < 5 && await more.isEnabled(); i++) await more.click();
    await expect(more).toBeDisabled();
    await frame.getByRole('button', { name: 'Next', exact: true }).click();
    await expect(rows).toHaveCount(10);
    await expect(frame.locator('.list-count')).toContainText('11–20 of');
    await frame.getByRole('button', { name: 'First', exact: true }).click();
    await expect(frame.locator('.list-count')).toContainText('1–10 of');
    if (view === 'inbox' || view === 'tasks') {
      await frame.getByRole('button', { name: 'Show more', exact: true }).click();
      await root.getByRole('textbox', { name: view === 'inbox' ? 'Search inbox' : 'Search tasks' }).fill(view === 'inbox' ? 'layout 34?' : 'Layout task 34');
      await expect(rows).toHaveCount(1);
      await expect(frame.locator('.list-count')).toContainText('1–1 of');
    }
  });
}


test('Show more stays visible and preserves scroll at phone width', async ({ page, state }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/?k=demo#view=inbox');
  const frame = page.locator('[data-page=inbox] [data-list-frame]');
  await expect(frame.getByRole('button', { name: 'Show more', exact: true })).toBeVisible();
  await frame.locator('.list-scroll').evaluate(e => e.scrollTop = 150);
  const top = await frame.locator('.list-scroll').evaluate(e => e.scrollTop);
  await frame.getByRole('button', { name: 'Show more', exact: true }).click();
  await expect(frame.locator('.request')).toHaveCount(20);
  expect(await frame.locator('.list-scroll').evaluate(e => e.scrollTop)).toBe(top);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
});


test('sidebar history and ledger-only jobs open their selected destination', async ({ page, request, state }) => {
  state.nodes[1].jobs.open.push({ Job: 'job/ledger-only', Started: '2026-01-01' });
  await request.post('/fixture/update', { data: state });
  await page.goto('/?k=demo');
  const nav = page.getByRole('navigation', { name: 'Workspace navigation' });
  await nav.getByRole('button', { name: 'Show past jobs', exact: true }).click();
  await nav.getByText('Finished layout', { exact: true }).click();
  await expect(page.locator('[data-page=project] [data-thread="job/finished-layout"].highlighted')).toBeVisible();
  await nav.getByText('Ledger only', { exact: true }).click();
  await expect(page.locator('[data-page=project] [data-thread="job/ledger-only"].highlighted')).toBeVisible();
});

test('Graph keeps Show more visible and appends its next ten projects', async ({ page, request, state }) => {
  state.nodes.push(...Array.from({ length: 35 }, (_, i) => ({ id: `project-${i}`, title: `Project ${i}`, parent: 'root', jobs: { rows: [], open: [] }, work: [] })));
  await request.post('/fixture/update', { data: state });
  await page.goto('/?k=demo#view=graph');
  const frame = page.locator('[data-page=graph] .graph-side [data-list-frame]');
  await expect(frame.getByRole('button', { name: 'Show more', exact: true })).toBeVisible();
  await expect(frame.locator('.list-count')).toContainText('1–10 of');
  await frame.getByRole('button', { name: 'Show more', exact: true }).click();
  await expect(frame.locator('.list-count')).toContainText('1–20 of');
});

test('typed recommendations and defaults show option labels with identifiers in details', async ({ page, request, state }) => {
  state.inbox = [{ ...state.inbox[0], options: [{ id: 'opaque-a', label: 'Keep the navigation' }, { id: 'opaque-b', label: 'Collapse the navigation' }], recommendation: 'opaque-a', default: 'opaque-b' }];
  state.due = [];
  await request.post('/fixture/update', { data: state });
  await page.goto('/?k=demo#view=inbox');
  const row = page.locator('[data-page=inbox] .request');
  await expect(row).toContainText('Recommended: Keep the navigation');
  await expect(row).not.toContainText('opaque-a');
  await expect(row).not.toContainText('opaque-b');
  await row.getByRole('button', { name: 'Read request', exact: true }).click();
  await expect(page.getByRole('dialog')).toContainText('Collapse the navigation');
  await page.getByRole('dialog').getByRole('button', { name: 'Technical details', exact: true }).click();
  await expect(page.getByRole('dialog').locator('.technical-facts')).toContainText('opaque-a');
});
