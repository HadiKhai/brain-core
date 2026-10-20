import { test, expect } from "@playwright/test";
import { readFileSync, mkdirSync, writeFileSync } from "node:fs";
import { resolve, join } from "node:path";
const audit = readFileSync(resolve('../tests/ui-fitness.js'), 'utf8').trim();
const folder = process.env.UI_TOUR_DIR;

test('section content, actions, group headers and pages share rendered alignment', async ({browser}) => {
  test.setTimeout(180_000);
  const evidence: unknown[] = [];
  for (const [width,height] of [[1440,900],[1280,720],[390,844],[320,640]]) for (const colorScheme of ['light','dark'] as const) {
    const context = await browser.newContext({viewport:{width,height},colorScheme});
    const page = await context.newPage();
    await page.goto('/?k=demo');
    let widthBefore = 0;
    for (const view of ['overview','inbox','learnings','agents','usage','imports','detail']) {
      await page.evaluate(v => { location.hash=`view=${v === 'detail' ? 'overview&node=field-guide' : v}`; }, view);
      if (view === 'detail') await expect(page.locator('.node-sheet')).toBeVisible();
      else await expect(page.locator(`[data-page=${view}]`)).toBeVisible();
      await page.evaluate(async()=>{document.querySelector('#workspace-content')!.scrollTop=0;await document.fonts.ready;await new Promise(r=>setTimeout(r,300));});
      const result = await page.evaluate(audit);
      evidence.push({width,height,colorScheme,view,result});
      const pageWidth = await page.locator('.page-header').evaluate(e=>e.getBoundingClientRect().width);
      if (widthBefore) expect(Math.abs(pageWidth-widthBefore)).toBeLessThanOrEqual(1);
      widthBefore=pageWidth;
      if (view === 'inbox') {
        await expect(page.getByRole('button',{name:'Clear filters',exact:true})).toBeDisabled();
        await expect(page.locator('[data-page=inbox] .pager')).toHaveCount(0);
        await expect(page.locator('[data-page=inbox] .table-footer')).toContainText('1–1 of 1 request');
        expect(await page.getByRole('combobox',{name:'Inbox project',exact:true}).evaluate(e=>{const t=e.querySelector('[data-slot=select-value]')!;return t.scrollWidth<=t.clientWidth;})).toBe(true);
      }
      if (view === 'detail') {
        const preview = page.locator('.node-sheet .text-preview');
        await expect(preview.first()).toBeVisible();
        await expect(page.locator('.node-sheet .preview-markdown li').first()).toContainText('Preserve keyboard focus');
        expect((await preview.allTextContents()).join('\n')).not.toContain('- Preserve keyboard focus');
        await page.keyboard.press('Escape');
      }
    }
    await context.close();
  }
  if (folder) {mkdirSync(folder,{recursive:true});writeFileSync(join(folder,'alignment-evidence.json'),JSON.stringify(evidence,null,2));}
});

test('fitness audit rejects a removed content inset and a shortened group header', async ({page}) => {
  await page.goto('/?k=demo#view=overview&node=field-guide');
  await expect(page.locator('.node-sheet')).toBeVisible();
  await page.evaluate(()=>{document.querySelector<HTMLElement>('.node-sheet .section-content')!.style.paddingInlineStart='0px';});
  await expect(page.evaluate(audit)).rejects.toThrow('section content inset');
  await page.evaluate(()=>{document.querySelector<HTMLElement>('.node-sheet .section-content')!.style.removeProperty('padding-inline-start');});
  await page.evaluate(audit);
  await page.keyboard.press('Escape');
  await expect(page.locator('.node-sheet')).not.toBeVisible();
  await page.evaluate(async()=>{await Promise.all(document.getAnimations().filter(a=>a.effect?.getTiming().iterations!==Infinity).map(a=>a.finished.catch(()=>{})));});
  await page.evaluate(()=>{document.querySelector<HTMLElement>('.attention-split .disclosure-button')!.style.width='calc(100% - 14px)';});
  await expect(page.evaluate(audit)).rejects.toThrow('group header spans container');
  await page.evaluate(()=>{document.querySelector<HTMLElement>('.attention-split .disclosure-button')!.style.removeProperty('width');});
  await page.evaluate(audit);
});
