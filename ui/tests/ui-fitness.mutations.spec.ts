import { test, expect, type Page } from "@playwright/test";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { audit, sidebarShape } from "./fitness-rules";

// Every §15 check is shown able to fail: clean before, a violation after one targeted break, clean again after undo.
const out = process.env.UI_FITNESS_OUT;
const evidence: unknown[] = [];
test.afterAll(() => { if (out) { mkdirSync(out, { recursive: true }); writeFileSync(join(out, "mutations.json"), JSON.stringify(evidence, null, 1)); } });
async function open(page: Page, hash: string, scenario = "demo") {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto(`/?k=${scenario}#${hash}`);
  await page.waitForSelector("[data-page]:not([hidden])");
  await page.waitForTimeout(150);
}
async function proves(page: Page, rule: string, name: string, mutate: () => Promise<unknown>, undo: () => Promise<unknown>, expected: string, check = () => page.evaluate(audit, rule)) {
  expect(await check(), `${rule} clean before “${name}”`).toEqual([]);
  await mutate();
  await page.waitForTimeout(100);
  const found = await check();
  expect(found.length, `${rule} rejects “${name}”`).toBeGreaterThan(0);
  expect(found.map((v) => v.detail).join("\n"), `${rule} names the broken rule for “${name}”`).toContain(expected);
  await undo();
  await page.waitForTimeout(100);
  expect(await check(), `${rule} clean after undoing “${name}”`).toEqual([]);
  evidence.push({ rule, mutation: name, rejected: found.map((v) => v.detail) });
}
const style = (page: Page, css: string) => page.addStyleTag({ content: css }).then((h) => h);

test("one-title rejects a second visible page name", async ({ page }) => {
  await open(page, "view=learnings");
  await proves(page, "one-title", "the page name repeated in the top bar",
    () => page.evaluate(() => { const t = document.createElement("span"); t.id = "mut"; t.textContent = "Learnings"; document.querySelector(".topbar-left")!.append(t); }),
    () => page.evaluate(() => document.getElementById("mut")!.remove()), "visible 2 times above the fold");
});

test("options rejects menu text that overflows without an ellipsis", async ({ page }) => {
  await open(page, "view=learnings", "long");
  await page.getByRole("combobox", { name: "Specialist", exact: true }).click();
  await page.locator("[role=listbox] [role=option]").first().waitFor();
  let tag: any;
  await proves(page, "options", "ellipsis removed from option text",
    async () => { tag = await style(page, "[role=option], [role=option] * { text-overflow: clip !important; white-space: nowrap !important; } [role=listbox] { max-width: 240px !important; }"); },
    () => tag.evaluate((e: Element) => e.remove()), "overflows without an ellipsis");
});

test("options rejects a name clamped to fewer lines with no full text", async ({ page }) => {
  await open(page, "view=learnings", "longtitle");
  await page.getByRole("combobox", { name: "Specialist", exact: true }).click();
  await page.locator("[role=listbox] [role=option]").first().waitFor();
  let tag: any;
  await proves(page, "options", "picker names clamped to one line",
    async () => { tag = await style(page, ".picker-item-title { display: -webkit-box !important; -webkit-line-clamp: 1; -webkit-box-orient: vertical; overflow: hidden !important; }"); },
    () => tag.evaluate((e: Element) => e.remove()), "clamped vertically");
});

test("scrollbars rejects the platform default", async ({ page }) => {
  await open(page, "view=jobs", "hundred");
  let tag: any;
  await proves(page, "scrollbars", "scrollbar styling reset to auto",
    async () => { tag = await style(page, "* { scrollbar-width: auto !important; scrollbar-color: auto !important; }"); },
    () => tag.evaluate((e: Element) => e.remove()), "uses the platform scrollbar");
});

test("hierarchy rejects a missing sub-item and a page of many sections", async ({ page, request }) => {
  await open(page, "view=jobs");
  const state = await (await request.get("/api/state?k=demo")).json();
  await proves(page, "hierarchy", "Agents loses its sub-items",
    () => page.evaluate(() => { const sub = [...document.querySelectorAll("[data-slot=sidebar-menu-sub]")].find((u) => u.textContent!.includes("Specialists"))!; (window as any).__mut = [sub, sub.parentElement]; sub.remove(); }),
    () => page.evaluate(() => { const [sub, parent] = (window as any).__mut; parent.append(sub); }),
    "Agents has no “Specialists” sub-item", () => page.evaluate(sidebarShape, state));
  await proves(page, "hierarchy", "two more top-level sections on one page",
    () => page.evaluate(() => { for (let i = 0; i < 2; i++) { const s = document.createElement("section"); s.className = "mut surface"; s.textContent = "Extra area"; document.querySelector("[data-page=jobs]")!.append(s); } }),
    () => page.evaluate(() => document.querySelectorAll(".mut").forEach((e) => e.remove())), "top-level sections on one page");
});

test("job rejects a page that is only an empty state", async ({ page }) => {
  await open(page, "view=threads");
  await proves(page, "job", "the page reduced to an empty state",
    () => page.evaluate(() => { const p = document.querySelector<HTMLElement>("[data-page=threads]")!; [...p.children].forEach((c) => ((c as HTMLElement).dataset.hiddenByMut = "", (c as HTMLElement).hidden = true)); const e = document.createElement("div"); e.className = "empty mut"; e.textContent = "Nothing here yet."; p.append(e); }),
    () => page.evaluate(() => { document.querySelector(".mut")!.remove(); document.querySelectorAll<HTMLElement>("[data-hidden-by-mut]").forEach((c) => { c.hidden = false; delete c.dataset.hiddenByMut; }); }), "only an empty state");
});

test("surfaces rejects a third surface level and a box around one item", async ({ page }) => {
  await open(page, "view=inbox");
  const wrap = (css: string) => page.evaluate((css) => {
    const row = document.querySelector("[data-page=inbox] [data-item]")!;
    const box = document.createElement("div"); box.className = "mut"; box.setAttribute("style", css);
    row.replaceWith(box); box.append(row);
  }, css);
  const unwrap = () => page.evaluate(() => { const b = document.querySelector(".mut")!; b.replaceWith(...b.childNodes); });
  await proves(page, "surfaces", "a grey box inside a black box inside the section",
    () => page.evaluate(() => {
      const row = document.querySelector("[data-page=inbox] [data-item]")!;
      const outer = document.createElement("div"); outer.className = "mut"; outer.style.cssText = "background:#000;padding:12px";
      const inner = document.createElement("div"); inner.className = "mut-inner"; inner.style.cssText = "background:#777;padding:12px";
      row.replaceWith(outer); outer.append(inner); inner.append(row); const sib = document.createElement("p"); sib.textContent = "x"; inner.append(sib);
    }),
    () => page.evaluate(() => { const o = document.querySelector(".mut")!, row = o.querySelector("[data-item]")!; o.replaceWith(row); }), "surface depth 3");
  await proves(page, "surfaces", "a painted box around a single row", () => wrap("background:#808080;padding:8px"), unwrap, "box around a single item");
  await proves(page, "surfaces", "an inner scroll for short content",
    () => page.evaluate(() => { const r = document.querySelector<HTMLElement>("[data-page=inbox] .rows")!; r.style.maxHeight = "60px"; r.style.overflowY = "auto"; }),
    () => page.evaluate(() => { const r = document.querySelector<HTMLElement>("[data-page=inbox] .rows")!; r.style.removeProperty("max-height"); r.style.removeProperty("overflow-y"); }), "inner scroll for short content");
});

test("lean rejects tall list items", async ({ page }) => {
  await open(page, "view=learnings");
  let tag: any;
  await proves(page, "lean", "rows padded to 120px",
    async () => { tag = await style(page, "[data-item] { min-height: 120px !important; }"); },
    () => tag.evaluate((e: Element) => e.remove()), "over 8% of a 900px viewport");
});

test("chaining rejects a scroll trap and a nested scroll area", async ({ page }) => {
  await open(page, "view=jobs", "hundred");
  let tag: any;
  await proves(page, "chaining", "overscroll contained on the page",
    async () => { tag = await style(page, "#workspace-content { overscroll-behavior: contain !important; }"); },
    () => tag.evaluate((e: Element) => e.remove()), "traps scrolling");
  await proves(page, "chaining", "a scroll area inside a scroll area",
    () => page.evaluate(() => { const t = document.querySelector<HTMLElement>("[data-page=jobs] [data-slot=table-container]")!; t.style.cssText = "max-height:300px;overflow:auto"; const b = t.querySelector<HTMLElement>("tbody")!; b.style.cssText = "display:block;max-height:120px;overflow:auto"; }),
    () => page.evaluate(() => { const t = document.querySelector<HTMLElement>("[data-page=jobs] [data-slot=table-container]")!; t.style.cssText = ""; t.querySelector<HTMLElement>("tbody")!.style.cssText = ""; }), "nested 2 deep");
});

test("overview rejects a dump", async ({ page }) => {
  await open(page, "view=overview");
  await proves(page, "overview", "the overview grows past two viewports",
    () => page.evaluate(() => { const d = document.createElement("div"); d.className = "mut"; d.style.height = "2000px"; document.querySelector("[data-page=overview]")!.append(d); }),
    () => page.evaluate(() => document.querySelector(".mut")!.remove()), "viewports");
  await proves(page, "overview", "four inline lists without View all",
    () => page.evaluate(() => { for (let i = 0; i < 4; i++) { const s = document.createElement("section"); s.className = "mut"; s.innerHTML = "<ul><li>a</li><li>b</li></ul>"; document.querySelector(".overview-grid")!.append(s); } }),
    () => page.evaluate(() => document.querySelectorAll(".mut").forEach((e) => e.remove())), "inline lists without");
});
