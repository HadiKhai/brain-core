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

import { audit } from './fitness-rules';
import { reportMeasurements } from './visual-checks';

const menuFixture = (overflow = 'visible', ancestor = false) => `
  <style>
    #workspace-content { width: 800px; height: 400px; overflow: auto; }
    [role=listbox] { width: 100px; overflow: ${ancestor ? 'hidden' : 'visible'}; }
    [role=option] { width: ${ancestor ? 'max-content' : '100px'}; white-space: nowrap; overflow: ${overflow}; }
  </style>
  <main id="workspace-content"><section data-page="proof">
    <div role="listbox"><div role="option">A synthetic option whose full text extends past the menu edge</div></div>
  </section></main>`;

test('readable horizontal overflow warns with open-menu evidence and numeric bounds', async ({page}) => {
  await page.setContent(menuFixture());
  const issues = await page.evaluate(audit, 'options');
  expect(issues.length).toBeGreaterThan(0);
  expect(issues.filter(v => v.kind !== 'measurement')).toEqual([]);
  expect(issues.some(v => v.detail.includes('menu {') && v.detail.includes('option {') && v.detail.includes('"width":100'))).toBe(true);
  await reportMeasurements(page, issues);
  await expect(page.getByRole('listbox')).toBeVisible();
  expect(test.info().attachments.some(a => a.contentType === 'image/png')).toBe(true);
  expect(test.info().errors).toEqual([]);
});

for (const overflow of ['hidden', 'clip', 'ellipsis', 'ancestor']) {
  test(`${overflow}: genuinely clipped text without full-text access remains hard`, async ({page}) => {
    await page.setContent(menuFixture(overflow === 'ellipsis' ? 'hidden' : overflow === 'ancestor' ? 'visible' : overflow, overflow === 'ancestor'));
    if (overflow === 'ellipsis') await page.getByRole('option').evaluate(e => { e.style.textOverflow = 'ellipsis'; });
    const issues = await page.evaluate(audit, 'options');
    expect(issues.filter(v => v.kind !== 'measurement').some(v => v.detail.includes('no full text on hover'))).toBe(true);
  });
}

test('nested scroll geometry warns while actual wheel scrolling remains usable', async ({page}) => {
  await page.setContent(`<style>
    #workspace-content {height:200px;width:500px;overflow:auto}
    #outer {height:150px;width:400px;overflow:auto}
    #inner {height:100px;width:300px;overflow:auto}
    .filler {height:600px}
    </style><main id="workspace-content"><section data-page="proof">
    <div id="outer"><div id="inner"><div class="filler">Synthetic scroll content</div></div><div class="filler"></div></div>
    <div class="filler"></div></section></main>`);
  const issues = await page.evaluate(audit, 'chaining');
  expect(issues.some(v => v.detail.includes('nested 2 deep') && v.kind === 'measurement')).toBe(true);
  expect(issues.filter(v => v.kind !== 'measurement')).toEqual([]);
  await reportMeasurements(page, issues);
  await page.locator('#inner').hover();
  await page.mouse.wheel(0, 100);
  // Actual failure to scroll is still a functional assertion, without exact pixels.
  await expect.poll(() => page.locator('#inner').evaluate(e => e.scrollTop > 0)).toBe(true);
});

test('a one-pixel restore difference reports a warning without a test error', async ({page}) => {
  await page.setContent('<div id="scroll" style="height:100px;overflow:auto"><div style="height:500px">Synthetic scroll content</div></div>');
  const actual = await page.locator('#scroll').evaluate(e => { e.scrollTop = 301; return e.scrollTop; });
  await visualExpect(page, actual, 'scroll restoration in px').toBe(300);
  const warning = test.info().annotations.find(a => a.type === 'visual-warning');
  expect(JSON.parse(warning!.description!).issues[0]).toMatchObject({actual:301,expected:300});
  expect(test.info().errors).toEqual([]);
});
