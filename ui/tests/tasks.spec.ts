import { test, expect } from "@playwright/test";
import { mkdirSync } from "node:fs";
import { join } from "node:path";

test.use({ baseURL: process.env.BRAIN_TASK_UI_URL || "http://127.0.0.1:4192" });
const shots = process.env.BRAIN_TASK_SCREENSHOTS;
async function shot(page: import("@playwright/test").Page, name: string) {
  if (shots) { mkdirSync(shots, { recursive: true }); await page.screenshot({ path: join(shots, name + ".png"), fullPage: true }); }
}

test("global task board preserves parked and dropped debt and opens the exact job", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/snapshot?scenario=tasks#view=tasks');
  const tasks = page.locator('[data-page=tasks]');
  await expect(tasks.locator('.task-card')).toHaveCount(6);
  await expect(tasks.getByRole('region', { name: 'Waiting tasks' })).toContainText('Narrow the parser contract');
  await expect(tasks.getByRole('region', { name: 'Dropped tasks' })).toContainText('Retain the obsolete preview');
  await expect(tasks.getByRole('region', { name: 'Completed tasks' })).not.toContainText('obsolete');
  await shot(page, 'tasks-board');
  await tasks.getByRole('button', { name: 'View job' }).click();
  await expect(page).toHaveURL(/node=field-guide/);
  await expect(page).toHaveURL(/thread=job%2Fnavigation/);
  await shot(page, 'reference-project');
});

test("dependency map and filters retain blocker context", async ({ page }) => {
  await page.goto('/snapshot?scenario=tasks#view=tasks');
  const tasks = page.locator('[data-page=tasks]');
  await tasks.getByRole('tab', { name: 'Dependencies', exact: true }).click();
  const dependent = tasks.getByRole('listitem').filter({ hasText: 'Share the finished report' });
  await expect(dependent).toContainText('Depends on Export the field report');
  await expect(dependent).toContainText('1 unfinished');
  await shot(page, 'tasks-dependencies');
  await tasks.getByRole('textbox', { name: 'Search tasks' }).fill('parser');
  await expect(tasks.getByRole('listitem')).toHaveCount(1);
  await tasks.getByRole('textbox', { name: 'Search tasks' }).fill('');
  await tasks.getByRole('combobox', { name: 'Filter tasks by owner' }).click();
  await page.getByRole('option', { name: 'Another developer', exact: true }).click();
  await expect(tasks.getByRole('listitem')).toHaveCount(1);
  await expect(tasks).toContainText('Translate the public report');
});

test("project task tab is scoped and mobile layout has no page overflow", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/snapshot?scenario=tasks#view=project&node=field-guide&tab=tasks');
  const project = page.locator('[data-page=project]');
  const tasks = project.getByRole('region', { name: 'Tasks', exact: true });
  await expect(tasks.locator('.task-card')).toHaveCount(5);
  await expect(tasks).not.toContainText('Translate the public report');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await shot(page, 'tasks-mobile-project');
  await tasks.getByRole('button', { name: 'Read task', exact: true }).first().click();
  await expect(page.getByRole('dialog')).toContainText('Keep the source request');
});
