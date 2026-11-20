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

for (const access of ['title', 'aria-label', 'aria-labelledby', 'aria-describedby', 'tooltip']) {
  test(`a clipped secondary leaf with ${access} on its ancestor does not implicate fitting containers`, async ({page}) => {
    await page.setContent(`<style>
      #workspace-content {width:800px;height:400px;overflow:auto}
      [role=option] {width:292px;overflow:hidden}
      .copy {display:flex;flex-direction:column;width:292px}
      small {display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
      </style><main id="workspace-content"><section data-page="proof"><div role="listbox">
      <div role="option"><span class="copy"><span>Wrapped primary name</span><span id="access"><small>${'Long secondary context '.repeat(12)}</small></span></span></div>
      </div></section></main>`);
    await page.locator('#access').evaluate((e, access) => {
      const value = e.textContent!;
      if (access === 'tooltip') e.setAttribute('data-slot', 'tooltip-trigger');
      else if (access === 'aria-labelledby' || access === 'aria-describedby') {
        const description = document.createElement('span'); description.id = 'full-secondary'; description.textContent = value; description.hidden = true; document.body.append(description);
        e.setAttribute(access, description.id);
      } else e.setAttribute(access, value);
    }, access);
    // Ensure this really reproduces a fitted option containing an overflowing leaf.
    expect(await page.getByRole('option').evaluate(e => e.scrollWidth === e.clientWidth)).toBe(true);
    const issues = await page.evaluate(audit, 'options');
    expect(issues.filter(v => v.kind !== 'measurement')).toEqual([]);
  });
}

test('a sibling tooltip never masks a clipped primary name', async ({page}) => {
  await page.setContent(menuFixture('hidden'));
  await page.getByRole('option').evaluate(e => {
    const sibling = document.createElement('span'); sibling.dataset.slot = 'tooltip-trigger'; sibling.textContent = 'Other accessible context'; e.append(sibling);
  });
  const issues = await page.evaluate(audit, 'options');
  expect(issues.filter(v => v.kind !== 'measurement').some(v => v.detail.includes('no full text on hover'))).toBe(true);
});

test('the real secondary picker context is available in a tooltip while primary names wrap', async ({page}) => {
  await page.goto('/?k=long#view=learnings');
  await page.getByRole('combobox', {name:'Specialist',exact:true}).click();
  const secondary = page.getByRole('option').filter({hasText:'Layout engineer'}).locator('small');
  const full = await secondary.textContent();
  await secondary.hover();
  await expect(page.getByRole('tooltip')).toHaveText(full!);
  const issues = await page.evaluate(audit, 'options');
  expect(issues.filter(v => v.kind !== 'measurement')).toEqual([]);
});
