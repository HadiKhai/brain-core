import { test, expect } from "@playwright/test";
import { readFileSync, mkdirSync, writeFileSync } from "node:fs";
import { resolve, join } from "node:path";
const audit = readFileSync(resolve('../tests/ui-fitness.js'), 'utf8').trim();
const folder = process.env.UI_TOUR_DIR;

const views = ['overview','inbox','projects','project','specialists','jobs','threads','usage','learnings','graph'];
const hashFor = (v: string) => v === 'project' ? 'view=project&node=field-guide&tab=needs' : 'view=' + v;
test('section rows, footers, toolbars and pages share rendered alignment', async ({browser}) => {
  test.setTimeout(240_000);
  const evidence: unknown[] = [];
  for (const [width,height] of [[1440,900],[1280,720],[390,844],[320,640]]) for (const colorScheme of ['light','dark'] as const) {
    const context = await browser.newContext({viewport:{width,height},colorScheme});
    const page = await context.newPage();
    await page.goto('/?k=demo');
    let widthBefore = 0;
    for (const view of views) {
      await page.evaluate(h => { location.hash = h; }, hashFor(view));
      await expect(page.locator(`[data-page=${view}]`)).toBeVisible();
      await page.evaluate(async()=>{document.querySelector('#workspace-content')!.scrollTop=0;await document.fonts.ready;await new Promise(r=>setTimeout(r,300));});
      const result = await page.evaluate(audit);
      const primary=page.locator('#screen-action-host :is([data-slot=button].bg-primary, a.primary-link)');
      if(view !== 'projects' && view !== 'graph') await expect(primary).toHaveCount(1);
      let hoverContrast = null;
      if(await primary.count()) {
        await primary.hover();
        await page.evaluate(async()=>{await Promise.all(document.getAnimations().filter(a=>a.effect?.getTiming().iterations!==Infinity).map(a=>a.finished.catch(()=>{})));});
        hoverContrast = (await page.evaluate(audit)).contrast;
        await page.locator('.page-header h1').hover();
      }
      evidence.push({width,height,colorScheme,view,result,hoverContrast});
      const pageWidth = await page.locator('.page-header').evaluate(e=>e.getBoundingClientRect().width);
      if (widthBefore) expect(Math.abs(pageWidth-widthBefore)).toBeLessThanOrEqual(1);
      widthBefore=pageWidth;
      if (view === 'inbox') {
        await expect(page.getByRole('button',{name:'Clear filters',exact:true})).toBeDisabled();
        await expect(page.locator('[data-page=inbox] .pager')).toHaveCount(1);
        await expect(page.locator('[data-page=inbox] .pager').getByRole('button', {name:'Next',exact:true})).toBeDisabled();
        await expect(page.locator('[data-page=inbox] .table-footer .page-position')).toHaveText('Page 1 of 1');
        await expect(page.locator('[data-page=inbox] .list-header .list-count')).toContainText('1–2 of 2 requests');
        expect(await page.getByRole('combobox',{name:'Inbox project',exact:true}).evaluate(e=>e.scrollWidth<=e.clientWidth+1)).toBe(true);
      }
      if (view === 'project') {
        const notes = page.locator('[data-page=project] [aria-label=Notes]');
        await expect(notes.locator('.preview-markdown li').first()).toContainText('Preserve keyboard focus');
        expect((await notes.locator('.text-preview').allTextContents()).join('\n')).not.toContain('- Preserve keyboard focus');
      }
    }
    await context.close();
  }
  if (folder) {mkdirSync(folder,{recursive:true});writeFileSync(join(folder,'alignment-evidence.json'),JSON.stringify(evidence,null,2));}
});

test('fitness audit rejects a removed section inset', async ({page}) => {
  const evidence: {mutation:string;failure:string}[] = [];
  const rejects = async (mutation:string, message:string) => {
    let failure = '';
    try { await page.evaluate(audit); } catch(error) { failure=String(error); }
    expect(failure).toContain(message);
    evidence.push({mutation,failure});
  };
  await page.goto('/?k=demo#view=inbox');
  await expect(page.locator('[data-page=inbox] [data-item]').first()).toBeVisible();
  await page.evaluate(audit);
  await page.evaluate(()=>{document.querySelector<HTMLElement>('[data-page=inbox] .rows')!.style.marginLeft='-14px';});
  await rejects('Pull the rows 14px out of their section', 'section content inset');
  await page.evaluate(()=>{document.querySelector<HTMLElement>('[data-page=inbox] .rows')!.style.removeProperty('margin-left');});
  await page.evaluate(audit);
  await page.evaluate(()=>{document.querySelector<HTMLElement>('[data-page=inbox] .table-footer')!.style.marginLeft='14px';});
  await rejects('Indent the list footer by 14px', 'section footer and toolbar inset');
  await page.evaluate(()=>{document.querySelector<HTMLElement>('[data-page=inbox] .table-footer')!.style.removeProperty('margin-left');});
  await page.evaluate(audit);
  if(folder) {mkdirSync(folder,{recursive:true});writeFileSync(join(folder,'alignment-mutations.json'),JSON.stringify(evidence,null,2));}
});

test('overlay phone gutters stay symmetric and reject a reserved strip', async ({browser}) => {
  const evidence: unknown[] = [];
  for (const [width,height] of [[390,844],[320,640]]) for (const colorScheme of ['light','dark'] as const) {
    const context = await browser.newContext({viewport:{width,height},colorScheme});
    const page = await context.newPage();
    await page.goto('/?k=demo');
    await page.evaluate(async()=>{await document.fonts.ready;await new Promise(r=>setTimeout(r,300));});
    const before = await page.evaluate(audit);
    expect(before.alignment.phoneGutters.left).toBe(16);
    expect(before.alignment.phoneGutters.right).toBe(16);
    // Simulate an incorrectly reserved 8px strip: overlay scrollbar implementations
    // may correctly leave `stable` alone without any geometric effect.
    const mutation = await page.addStyleTag({content:'.inset-scroll { scrollbar-gutter: stable !important; padding-right: calc(16px + 8px) !important; }'});
    let failure = '';
    try { await page.evaluate(audit); } catch(error) {failure=String(error);}
    expect(failure).toContain('equal phone page gutters');
    await mutation.evaluate(e=>e.remove());
    const restored = await page.evaluate(audit);
    evidence.push({width,height,colorScheme,before,mutation:'Reserve an extra 8px strip on an overlay-scrollbar page',failure,restored});
    await context.close();
  }
  if(folder) {mkdirSync(folder,{recursive:true});writeFileSync(join(folder,'phone-gutter-mutations.json'),JSON.stringify(evidence,null,2));}
});
