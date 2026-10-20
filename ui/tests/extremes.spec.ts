import { test, expect } from "@playwright/test";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const scenarios = ["empty", "single", "ten", "hundred", "thousand", "longtext", "longtitle", "rtl"];
const views = ["overview", "inbox", "learnings", "agents", "usage", "imports", "detail"];
const sizes = [[1440,900],[1280,720],[390,844],[320,640]];
const folder = process.env.UI_TOUR_DIR;
if (folder) mkdirSync(folder, { recursive: true });

for (const scenario of scenarios) test(`seeded ${scenario}: every screen contains its content`, async ({ browser }) => {
  test.setTimeout(180_000);
  const evidence: unknown[] = [];
  for (const [width,height] of sizes) for (const colorScheme of ["light","dark"] as const) {
    const context = await browser.newContext({ viewport:{width,height}, colorScheme, deviceScaleFactor:1 });
    const page = await context.newPage();
    const errors: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.goto(`/?k=${scenario}`);
    await page.waitForSelector("[data-page=overview]");
    await page.evaluate(() => document.fonts.ready);
    for (const view of views) {
      await page.evaluate(v => { location.hash = `view=${v === "detail" ? "overview&node=field-guide" : v}`; }, view);
      if (view === "detail") await expect(page.getByRole("dialog")).toBeVisible();
      else await expect(page.locator(`[data-page=${view}]`)).toBeVisible();
      await page.evaluate(async () => {
        document.querySelectorAll<HTMLElement>("#workspace-content,.section-body").forEach(e => {e.scrollTop=0;});
        await new Promise(r => setTimeout(r,220));
        await Promise.all(document.getAnimations().filter(a => a.effect?.getTiming().iterations !== Infinity).map(a => a.finished.catch(() => {})));
      });
      const audit = await page.evaluate(() => {
        const visible = (e: Element) => !!e.getClientRects().length && !e.closest("[hidden],[aria-hidden=true]");
        const root = document.querySelector<HTMLElement>("#workspace-content")!;
        const cells = [...document.querySelectorAll<HTMLElement>(".cell-preview")].filter(visible);
        const sections = [...document.querySelectorAll<HTMLElement>(".section-body")].filter(visible);
        const primary = document.querySelector("#screen-action-host")!.getBoundingClientRect();
        const requests = [...document.querySelectorAll(".correspondence")].filter(visible);
        return {
          overflow: document.documentElement.scrollWidth > innerWidth || root.scrollWidth > root.clientWidth + 1,
          clippedCells: cells.filter(e => e.clientHeight > 3 * parseFloat(getComputedStyle(e).lineHeight) + 2).length,
          unbounded: sections.filter(e => !e.closest('[data-page="inbox"]') && getComputedStyle(e).maxHeight === "none").length,
          requestCount: requests.length,
          missingActions: requests.filter(e => !e.querySelector("a,button")).length,
          primaryAboveFold: primary.height > 0 && primary.bottom <= innerHeight,
          dark: document.documentElement.classList.contains("dark"),
          rtl: [...document.querySelectorAll<HTMLElement>(".readable[dir=auto]")].filter(e => visible(e) && getComputedStyle(e).direction === "rtl").length,
        };
      });
      expect(audit.overflow, `${scenario}/${view}/${width}/${colorScheme}`).toBe(false);
      expect(audit.clippedCells).toBe(0);
      expect(audit.unbounded).toBe(0);
      expect(audit.missingActions).toBe(0);
      if (view !== "detail") expect(audit.primaryAboveFold).toBe(true);
      expect(audit.dark).toBe(colorScheme === "dark");
      if (view === "overview") expect(audit.requestCount).toBeLessThanOrEqual(5);
      if (view === "inbox") expect(audit.requestCount).toBeLessThanOrEqual(10);
      if (scenario === "rtl" && ["overview","inbox","learnings","agents"].includes(view)) expect(audit.rtl).toBeGreaterThan(0);
      if (folder) await page.screenshot({ path:join(folder,`${scenario}-${view}-${width}x${height}-${colorScheme}.png`), animations:"disabled" });
      evidence.push({scenario,view,width,height,colorScheme,...audit});
      if (view === "detail") await page.keyboard.press("Escape");
    }
    expect(errors).toEqual([]);
    await context.close();
  }
  if (folder) writeFileSync(join(folder,`${scenario}-evidence.json`),JSON.stringify(evidence,null,2));
});

test("one thousand requests stay reachable and have conversation feedback", async ({ page, context }) => {
  await page.goto("/?k=thousand");
  await expect(page.locator('[data-page=overview] .correspondence')).toHaveCount(5);
  await page.getByRole("button",{name:"Open Inbox (1000)",exact:true}).click();
  const inbox = page.locator('[data-page=inbox]');
  await expect(inbox.locator(".correspondence")).toHaveCount(10);
  await expect(inbox.locator(".table-footer")).toContainText("1–10 of 1000 requests");
  await inbox.getByRole("button",{name:"Next",exact:true}).click();
  await expect(inbox.locator(".table-footer")).toContainText("11–20 of 1000 requests");
  await page.getByRole("textbox",{name:"Search inbox"}).fill("no matching synthetic question");
  await expect(inbox).toContainText("Nothing waiting here.");
  await inbox.getByRole("button",{name:"Clear filters"}).click();
  await context.route("**/conversation", route => route.fulfill({body:"Synthetic conversation opened"}));
  const opened = page.waitForEvent("popup");
  await inbox.getByRole("link",{name:"Answer",exact:true}).first().click();
  const conversation = await opened;
  await expect(conversation.locator("body")).toHaveText("Synthetic conversation opened");
  await expect(inbox.getByRole("status").first()).toContainText("Continue in the conversation");
  await conversation.close();
});

test("long hire briefs, proposal decisions and overflowing specialist options have full readers", async ({ page, context }) => {
  await page.goto("/?k=longtext#view=agents");
  const hire = page.locator('.specialist-entry').first();
  await hire.getByRole("button",{name:"Read brief",exact:true}).first().click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("Review the proposed navigation");
  expect((await dialog.locator(".markdown").innerText()).split(/\s+/).length).toBeGreaterThanOrEqual(2000);
  await page.keyboard.press("Escape");
  await page.goto("/?k=longtitle#view=learnings");
  await page.getByRole("combobox",{name:"Specialist",exact:true}).click();
  const option = page.getByRole("option").nth(1);
  await option.hover();
  await expect(page.locator('[data-slot=tooltip-content][data-open]')).toContainText("thoughtful project notebook");
  expect(await option.evaluate(e => e.getBoundingClientRect().right <= innerWidth)).toBe(true);
  await page.keyboard.press("Escape");
  await page.goto("/?k=demo#view=learnings");
  await page.locator('#screen-action-host').getByRole("button",{name:"Review proposal",exact:true}).click();
  await dialog.getByRole("textbox",{name:"Reviewer name"}).fill("Morgan");
  await context.grantPermissions(["clipboard-write","clipboard-read"]);
  await dialog.getByRole("button",{name:"Copy acceptance",exact:true}).click();
  await expect(dialog.getByRole("status")).toContainText("Copied. Run the command");
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe("brain item accept --reviewed-by 'Morgan' -- 'lesson-0'");
  await page.evaluate(() => Object.defineProperty(navigator,"clipboard",{configurable:true,value:{writeText:async()=>{throw new Error("Synthetic denied");}}}));
  await dialog.getByRole("combobox",{name:"Decision",exact:true}).click();
  await page.getByRole("option",{name:"Reject",exact:true}).click();
  await dialog.getByRole("button",{name:"Copy rejection",exact:true}).click();
  await expect(dialog.getByRole("status")).toContainText("Copy failed. Open the command");
  await dialog.getByRole("button",{name:"View command",exact:true}).click();
  await expect(dialog.locator("pre.verbatim").first()).toHaveText("brain item reject -- 'lesson-0'");
});

test("unknown attention kinds keep generic actions and optional advice stays readable", async ({page}) => {
  const state = await (await page.request.get('/api/state?k=demo')).json();
  state.inbox = [{...state.inbox[0], kind:'blocked-overdue', escalated:true, age_hours:49, since:'2026-10-19T09:00:00Z', recommendation:'Discuss a smaller next step.', default:'Keep open for review', text:'Review report /opt/demo/HANDOFF.md for job/synthetic-review. Check the ready packet.'}];
  await page.addInitScript(s => {(window as any).__STATE__=s;},state);
  await page.goto('/?k=demo#view=inbox');
  const inbox = page.locator('[data-page=inbox]');
  await expect(inbox).toContainText('Discuss a smaller next step.');
  await expect(inbox.getByRole('link',{name:'Review: Keep open for review',exact:true})).toBeVisible();
  const copy = await inbox.innerText();
  expect(copy).not.toMatch(/\/opt\/|job\/|ready packet|blocked-overdue/);
  await inbox.getByRole('button',{name:'Read request',exact:true}).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByRole('button',{name:'Technical details',exact:true}).click();
  await expect(dialog.locator('pre.verbatim')).toContainText('/opt/demo/HANDOFF.md');
});

test("touch decision feedback and import undo preserve the operator's place", async ({browser}) => {
  const context=await browser.newContext({viewport:{width:320,height:640},hasTouch:true,isMobile:true});
  const page=await context.newPage();
  await page.goto('/?k=demo#view=learnings');
  await page.locator('#screen-action-host').getByRole('button',{name:'Review proposal',exact:true}).tap();
  const dialog=page.getByRole('dialog');
  await dialog.getByRole('textbox',{name:'Reviewer name'}).fill('Morgan');
  await page.evaluate(()=>Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:()=>new Promise<void>(resolve=>{(window as any).__finishCopy=resolve;})}}));
  await dialog.getByRole('button',{name:'Copy acceptance',exact:true}).tap();
  await expect(dialog.getByRole('status')).toHaveText('Copying…');
  await page.evaluate(()=>(window as any).__finishCopy());
  await expect(dialog.getByRole('status')).toContainText('Copied. Run the command');
  await dialog.getByRole('button',{name:'Close',exact:true}).tap();
  await page.locator('#screen-action-host').getByRole('button',{name:'Review proposal',exact:true}).tap();
  await expect(dialog.getByRole('textbox',{name:'Reviewer name'})).toHaveValue('Morgan');
  await dialog.getByRole('button',{name:'Close',exact:true}).tap();
  await page.getByRole('button',{name:'Toggle Sidebar'}).tap();
  await page.locator('[data-mobile=true]').getByRole('button',{name:'Import checklist',exact:true}).tap();
  await page.getByRole('checkbox',{name:'Pocket Atlas',exact:true}).tap();
  const dock=page.locator('#action-dock-host');
  await dock.getByRole('button',{name:'Clear',exact:true}).tap();
  await dock.getByRole('button',{name:'Undo clear',exact:true}).tap();
  await dock.getByRole('button',{name:'Review import',exact:true}).tap();
  await expect(page.getByRole('dialog').getByRole('textbox',{name:'Command to copy'})).toHaveValue("brain adopt --parent 'root' -- '/demo/pocket-atlas'");
  await context.close();
});
