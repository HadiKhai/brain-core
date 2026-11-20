import { test, expect } from '@playwright/test';
import { visualExpect } from './visual-checks';

test('a deliberate 14px measurement miss warns with values and a screenshot while passing', async ({page}) => {
  await page.goto('/?k=demo#view=inbox');
  const header = page.locator('.page-header');
  await expect(header).toBeVisible();
  const before = await header.evaluate(e => e.getBoundingClientRect().left);
  await header.evaluate(e => { e.style.transform = 'translateX(14px)'; });
  const after = await header.evaluate(e => e.getBoundingClientRect().left);
  await visualExpect(page, after-before, 'deliberate alignment mutation in px').toBe(0);
  const info = test.info();
  const warning = info.annotations.find(a => a.type === 'visual-warning');
  expect(warning).toBeDefined();
  const issue = JSON.parse(warning!.description!).issues[0];
  expect(issue.actual).toBe(14);
  expect(issue.expected).toBe(0);
  expect(info.attachments.some(a => a.contentType === 'image/png')).toBe(true);
  expect(info.errors).toEqual([]);
  await header.evaluate(e => { e.style.removeProperty('transform'); });
});
