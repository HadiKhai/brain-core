import { test, expect } from "@playwright/test";
import { mkdirSync } from "node:fs";
import { join } from "node:path";

test.use({ baseURL: process.env.BRAIN_TASK_UI_URL || "http://127.0.0.1:4192" });
const shots = process.env.BRAIN_TASK_SCREENSHOTS;
async function shot(page: import("@playwright/test").Page, name: string) {
  if (shots) { mkdirSync(shots, { recursive: true }); await page.screenshot({ path: join(shots, name + ".png"), fullPage: true }); }
}

test("kanban keeps five columns, hides dropped by default and opens the exact job", async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 1000 });
  await page.goto('/snapshot?scenario=tasks#view=tasks');
  const tasks = page.locator('[data-page=tasks]');
  await expect(tasks.locator('.task-card')).toHaveCount(6);
  await expect(tasks.locator('.task-column')).toHaveCount(5);
  await expect(tasks.getByRole('region', { name: 'Parked tasks' })).toContainText('Narrow the parser contract');
  await expect(tasks.getByRole('region', { name: 'Parked tasks' })).toContainText('Reconcile an old delivery claim');
  await expect(tasks).toContainText('6 of 7 tasks · 2 attempts · dropped hidden');
  await expect(tasks.getByRole('region', { name: 'Done tasks' })).not.toContainText('old delivery claim');
  await expect(tasks.locator('[data-task-id="APP-1"]')).toContainText('Unlocks 1 downstream: APP-2');
  await expect(tasks.locator('[data-task-id="APP-1"]')).toContainText('job/navigation');
  await tasks.getByRole('button', { name: 'Show dropped' }).click();
  await expect(tasks.getByRole('region', { name: 'Dropped tasks' })).toContainText('Retain the obsolete preview');
  await expect(tasks.getByRole('region', { name: 'Done tasks' })).not.toContainText('obsolete');
  await shot(page, 'tasks-board');
  await tasks.getByRole('button', { name: 'View job' }).click();
  await expect(page).toHaveURL(/node=field-guide/);
  await expect(page).toHaveURL(/thread=job%2Fnavigation/);
});

test("canvas directs edges, highlights downstream and opens task details with pan and zoom", async ({ page }) => {
  await page.goto('/snapshot?scenario=tasks#view=tasks');
  const tasks = page.locator('[data-page=tasks]');
  await tasks.getByRole('tab', { name: 'Canvas', exact: true }).click();
  const canvas = tasks.getByRole('group', { name: 'Task dependency canvas' });
  await expect(canvas.locator('.task-graph-node')).toHaveCount(2);
  await expect(tasks.getByRole('tab', { name: 'Canvas', exact: true })).toHaveAttribute('aria-selected', 'true');
  await expect(tasks.getByRole('tab', { name: 'Board', exact: true })).toHaveAttribute('aria-selected', 'false');
  const tabColors = await tasks.getByRole('tab').evaluateAll(es => es.map(e => getComputedStyle(e).backgroundColor));
  expect(tabColors[0]).toBe('rgba(0, 0, 0, 0)');
  expect(tabColors[1]).not.toBe(tabColors[0]);
  await expect(tasks.getByLabel('Task status legend')).toContainText('Blocked');
  await expect(canvas.locator('[data-task-from="field-guide:APP-1"][data-task-to="field-guide:APP-2"]')).toHaveCount(1);
  await expect(canvas.locator('[data-task-key="field-guide:APP-2"]')).not.toHaveAttribute('data-ready');
  await expect(canvas.locator('[data-task-key="observatory:DEMO-7"]')).toHaveCount(0);
  await tasks.getByRole('button', { name: 'Show all 6 tasks', exact: true }).click();
  await expect(tasks.getByRole('region', { name: 'Tasks without links' })).toContainText('Translate the public report');
  const source = canvas.getByRole('button', { name: 'Open task APP-1:', exact: false });
  await source.focus();
  await expect(canvas.locator('[data-task-key="field-guide:APP-2"]')).toHaveAttribute('data-downstream', 'true');
  await expect(tasks.locator('.task-canvas-context')).toContainText('unlocks 1 downstream tasks: APP-2');
  await source.press('Enter');
  await expect(page.getByRole('dialog')).toContainText('Keep the source request');
  await expect(page.getByRole('dialog')).toContainText('APP-1');
  await page.keyboard.press('Escape');
  await tasks.getByRole('button', { name: 'Zoom in tasks' }).click();
  await expect(canvas).toHaveAttribute('data-zoom', '1.25');
  await tasks.getByRole('button', { name: 'Fit tasks' }).click();
  expect(Number(await canvas.getAttribute('data-zoom'))).toBeLessThanOrEqual(1);
  const viewport = canvas.locator(':scope > g');
  const before = await viewport.getAttribute('transform');
  await canvas.focus();
  await canvas.press('ArrowRight');
  expect(await viewport.getAttribute('transform')).not.toBe(before);
  await canvas.press('Home');
  await expect(viewport).toHaveAttribute('transform', before!);
  const box = await canvas.boundingBox();
  await page.mouse.move(box!.x + box!.width - 20, box!.y + box!.height - 20);
  await page.mouse.down();
  await page.mouse.move(box!.x + box!.width - 100, box!.y + box!.height - 60);
  await page.mouse.up();
  expect(await viewport.getAttribute('transform')).not.toBe(before);
  await tasks.getByRole('button', { name: 'Fit tasks' }).click();
  await shot(page, 'tasks-canvas');
});

test("node, owner and search filters work in both views and empty columns remain visible", async ({ page }) => {
  await page.goto('/snapshot?scenario=tasks#view=tasks');
  const tasks = page.locator('[data-page=tasks]');
  await tasks.getByRole('combobox', { name: 'Filter tasks by node' }).click();
  await page.getByRole('option', { name: 'Field Guide', exact: true }).click();
  await expect(tasks.locator('.task-card')).toHaveCount(5);
  await expect(tasks).not.toContainText('Translate the public report');
  await tasks.getByRole('tab', { name: 'Canvas', exact: true }).click();
  await expect(tasks.locator('.task-graph-node')).toHaveCount(2);
  await tasks.getByRole('textbox', { name: 'Search tasks' }).fill('parser');
  await expect(tasks).toContainText('No active dependency links.');
  await tasks.getByRole('button', { name: 'Show all 1 tasks', exact: true }).click();
  await expect(tasks.getByRole('region', { name: 'Tasks without links' })).toContainText('Repair recurring parser failure');
  await tasks.getByRole('textbox', { name: 'Search tasks' }).fill('no matches');
  await expect(tasks).toContainText('No tasks to draw.');
  await tasks.getByRole('tab', { name: 'Board', exact: true }).click();
  await expect(tasks).toContainText('No matching tasks.');
  await expect(tasks.locator('.task-column')).toHaveCount(5);
  await tasks.getByRole('textbox', { name: 'Search tasks' }).fill('');
  await tasks.getByRole('combobox', { name: 'Filter tasks by node' }).click();
  await page.getByRole('option', { name: 'All nodes', exact: true }).click();
  await tasks.getByRole('combobox', { name: 'Filter tasks by owner' }).click();
  await page.getByRole('option', { name: 'Another developer', exact: true }).click();
  await expect(tasks.locator('.task-card')).toHaveCount(1);
  await expect(tasks).toContainText('Translate the public report');
});

test("project task tab is scoped and mobile board scrolls within its surface", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/snapshot?scenario=tasks#view=project&node=field-guide&tab=tasks');
  const tasks = page.locator('[data-page=project]').getByRole('region', { name: 'Tasks', exact: true });
  await expect(tasks.locator('.task-card')).toHaveCount(5);
  await expect(tasks).not.toContainText('Translate the public report');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(await tasks.locator('.task-board').evaluate(e => e.scrollWidth > e.clientWidth)).toBe(true);
  // Ready is the second column; scroll its card into the board viewport before measuring.
  await tasks.locator('[data-task-id="APP-2"]').scrollIntoViewIfNeeded();
  const titleBox = await tasks.locator('[data-task-id="APP-2"] .task-card-copy').boundingBox();
  expect(titleBox?.width).toBeGreaterThan(120);
  await shot(page, 'tasks-mobile-project');
  await tasks.locator('[data-task-id="APP-2"]').getByRole('button', { name: 'Read task', exact: true }).click();
  await expect(page.getByRole('dialog')).toContainText('Keep the source request');
});

test("keyboard focus reveals distant tasks and viewport shortcuts work from a focused node", async ({ page }) => {
  await page.route('**/snapshot?scenario=tasks', async route => {
    const response = await route.fetch();
    const html = (await response.text()).replace('</head>', `<script>
      const seed = window.__STATE__.nodes[1].tasks.rows[0];
      window.__STATE__.nodes[1].tasks.rows = Array.from({length:1500}, (_, i) => ({...seed, id:'LARGE-'+i, title:'Large task '+i, status:'backlog', depends_on:i%2?['LARGE-'+(i-1)]:[], blocked_by:[], blocks:[]}));
      </script></head>`);
    await route.fulfill({ response, body: html });
  });
  await page.goto('/snapshot?scenario=tasks#view=tasks');
  const tasks = page.locator('[data-page=tasks]');
  await tasks.getByRole('tab', { name: 'Canvas', exact: true }).click();
  const canvas = tasks.getByRole('group', { name: 'Task dependency canvas' });
  const node = canvas.locator('[data-task-key="field-guide:LARGE-1000"]');
  await page.keyboard.press('Tab');
  await node.focus();
  const box = await node.boundingBox(), viewport = await canvas.boundingBox();
  expect(box!.x).toBeGreaterThanOrEqual(viewport!.x);
  expect(box!.y).toBeGreaterThanOrEqual(viewport!.y);
  expect(box!.x + box!.width).toBeLessThanOrEqual(viewport!.x + viewport!.width);
  expect(box!.y + box!.height).toBeLessThanOrEqual(viewport!.y + viewport!.height);
  const transform = await canvas.locator(':scope > g').getAttribute('transform');
  await node.press('ArrowDown');
  expect(await canvas.locator(':scope > g').getAttribute('transform')).not.toBe(transform);
  await node.press('+');
  await expect(canvas).toHaveAttribute('data-zoom', '1.25');
  await node.press('Enter');
  await expect(page.getByRole('dialog')).toContainText('Large task 1000');
});

test("short plain titles retain full context, parked tasks collapse and blocked work remains visible", async ({ page }) => {
  const full = '**Imported summary** ' + 'Retained context with [reference](https://example.com). '.repeat(25) + 'End of retained original note.';
  await page.route('**/snapshot?scenario=tasks', async route => {
    const response = await route.fetch();
    const html = (await response.text()).replace('</head>', `<script>
      window.__STATE__.nodes[1].tasks.rows.find(t=>t.id==='APP-1').title=${JSON.stringify(full)};
      window.__STATE__.nodes[1].tasks.rows.find(t=>t.id==='APP-5').title='Work on this job';
      window.__STATE__.nodes[1].tasks.rows.find(t=>t.id==='APP-5').branch='job/verify-log';
      window.__STATE__.nodes[1].tasks.rows.find(t=>t.id==='APP-2').depends_on=['APP-5'];
      window.__STATE__.nodes[1].tasks.rows.find(t=>t.id==='APP-2').blocked_by=[];
      window.__STATE__.nodes[1].tasks.rows.find(t=>t.id==='APP-3').status='blocked';
      </script></head>`);
    await route.fulfill({ response, body: html });
  });
  await page.goto('/snapshot?scenario=tasks#view=tasks');
  const tasks = page.locator('[data-page=tasks]');
  const imported = tasks.locator('[data-task-id="APP-1"]');
  await expect(imported.locator('.task-card-copy strong')).not.toContainText('**');
  expect((await imported.locator('.task-card-copy strong').innerText()).length).toBeLessThanOrEqual(120);
  expect(await imported.locator('.task-card-copy strong').evaluate(e => e.getBoundingClientRect().height <= parseFloat(getComputedStyle(e).lineHeight) * 3 + 1)).toBe(true);
  await imported.getByRole('button', { name: 'Read task', exact: true }).click();
  await expect(page.getByRole('dialog')).toContainText('End of retained original note.');
  await page.keyboard.press('Escape');
  await expect(tasks.locator('[data-task-id="APP-5"] .task-card-copy strong')).toHaveText('Work on job/verify-log');
  await expect(tasks.locator('[data-task-id="APP-2"] .task-facts')).toContainText('APP-5: Work on job/verify-log');
  await expect(tasks.locator('[data-task-id="APP-5"]')).not.toContainText('Depends on: None');
  await expect(tasks.locator('[data-task-id="APP-5"]')).not.toContainText('Blockers: None');
  await expect(tasks.locator('[data-task-id="APP-5"]')).not.toContainText('Unlocks 0');
  await expect(tasks.locator('[data-task-id="APP-3"]')).toBeVisible();
  await expect(tasks.locator('[data-task-id="APP-3"] .task-blocked')).toHaveText('Blocked');
  await expect(tasks.locator('[data-task-id="APP-6"]')).toBeHidden();
  await tasks.getByText('Show 1 parked tasks', { exact: true }).click();
  await expect(tasks.locator('[data-task-id="APP-6"]')).toBeVisible();
  const read = await imported.getByRole('button', { name: 'Read task' }).evaluate(e => ({ border: getComputedStyle(e).borderStyle, radius: getComputedStyle(e).borderRadius }));
  expect(read.border).toBe('solid');
  expect(read.radius).not.toBe('0px');
});

test("zero-link canvas explains the empty graph and show all lists tasks without a grid", async ({ page }) => {
  await page.goto('/snapshot?scenario=tasks#view=project&node=observatory&tab=tasks');
  const project = page.locator('[data-page=project]');
  const tasks = project.getByRole('region', { name: 'Tasks', exact: true });
  await expect(project.getByRole('tab', { name: 'Tasks 1', exact: true })).toHaveAttribute('aria-selected', 'true');
  await tasks.getByRole('tab', { name: 'Canvas', exact: true }).click();
  await expect(tasks).toContainText('No active dependency links.');
  await expect(tasks.locator('svg.task-canvas')).toHaveCount(0);
  await tasks.getByRole('button', { name: 'Show all 1 tasks', exact: true }).click();
  await expect(tasks).toContainText('No dependency links in these tasks.');
  await expect(tasks.getByRole('region', { name: 'Tasks without links' })).toContainText('Translate the public report');
  await expect(tasks.locator('svg.task-canvas')).toHaveCount(0);
});

test("task count excludes dropped and the settled navigation labels match expanded or icon mode", async ({ page }) => {
  await page.setViewportSize({ width: 1680, height: 1060 });
  await page.goto('/snapshot?scenario=tasks#view=project&node=field-guide&tab=tasks');
  await expect(page.locator('[data-page=project]').getByRole('tab', { name: 'Tasks 5', exact: true })).toHaveAttribute('aria-selected', 'true');
  for (const name of ['Overview', 'Inbox', 'Tasks']) {
    const button = page.getByRole('navigation', { name: 'Workspace navigation' }).getByRole('button', { name, exact: true });
    expect(await button.locator('span').last().evaluate(e => e.scrollWidth <= e.clientWidth)).toBe(true);
  }
  await page.getByRole('button', { name: 'Toggle Sidebar' }).click();
  const rail = page.locator('[data-slot=sidebar][data-state=collapsed]');
  await rail.evaluate(async e => { await Promise.all(e.getAnimations({ subtree: true }).filter(a=>a.effect?.getTiming().iterations!==Infinity).map(a=>a.finished.catch(()=>{}))); });
  for (const name of ['Overview', 'Inbox', 'Tasks']) {
    expect(await rail.getByRole('button', { name, exact: true }).locator('span').last().evaluate(e => e.getBoundingClientRect().width)).toBe(0);
  }
});
