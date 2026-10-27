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
  await expect(canvas.locator('.task-graph-node')).toHaveCount(6);
  await expect(canvas.locator('[data-task-from="field-guide:APP-1"][data-task-to="field-guide:APP-2"]')).toHaveCount(1);
  await expect(canvas.locator('[data-task-key="field-guide:APP-2"]')).not.toHaveAttribute('data-ready');
  await expect(canvas.locator('[data-task-key="observatory:DEMO-7"]')).toHaveAttribute('data-ready', 'true');
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
  await expect(tasks.locator('.task-graph-node')).toHaveCount(5);
  await tasks.getByRole('textbox', { name: 'Search tasks' }).fill('parser');
  await expect(tasks.locator('.task-graph-node')).toHaveCount(1);
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
