import { test, expect, type Page } from "@playwright/test";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { audit, sidebarTargets, sidebarShape, type Violation } from "./fitness-rules";

// ui-designer §15: one check per lean-structure rule, over every sidebar destination.
const out = process.env.UI_FITNESS_OUT;
const record = (name: string, found: Violation[]) => {
  if (!out) return;
  mkdirSync(out, { recursive: true });
  writeFileSync(join(out, name + ".json"), JSON.stringify(found, null, 1));
};
async function open(page: Page, scenario = "demo", theme: "light" | "dark" = "light", width = 1440, height = 900) {
  await page.setViewportSize({ width, height });
  await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
  await page.goto(`/?k=${scenario}`);
  await page.waitForSelector("[data-page]:not([hidden])");
}
// Visit every sidebar destination and sub-item; `check` returns the violations on that route.
async function everyRoute(page: Page, check: (label: string) => Promise<Violation[]>) {
  const found: Violation[] = [];
  const targets = await page.evaluate(sidebarTargets);
  const seen = new Set<string>();
  for (const t of targets) {
    const button = page.locator(`[data-fitness-target="${t}"]`);
    const label = ((await button.textContent()) || "").replace(/\s+/g, " ").trim();
    await button.click();
    await page.waitForTimeout(120);
    const route = await page.evaluate(() => location.hash);
    if (seen.has(route)) continue;
    seen.add(route);
    for (const v of await check(label)) found.push({ ...v, detail: `${label} (${route}): ${v.detail}` });
  }
  return found;
}
test.setTimeout(300_000);
const ruleOnEveryRoute = (rule: string, scenarios = ["demo"], themes: ("light" | "dark")[] = ["light"]) =>
  test(`§15 ${rule}`, async ({ page }) => {
    const found: Violation[] = [];
    for (const scenario of scenarios)
      for (const theme of themes) {
        await open(page, scenario, theme);
        found.push(...(await everyRoute(page, async () => page.evaluate(audit, rule))).map((v) => ({ ...v, detail: `[${scenario}/${theme}] ${v.detail}` })));
      }
    record(rule, found);
    expect(found, found.map((v) => v.detail).join("\n")).toEqual([]);
  });

ruleOnEveryRoute("one-title");
ruleOnEveryRoute("scrollbars", ["demo", "hundred"], ["light", "dark"]);
ruleOnEveryRoute("job", ["demo", "empty"]);
ruleOnEveryRoute("surfaces", ["demo", "single", "ten"]);
ruleOnEveryRoute("lean", ["demo", "ten"]);

test("§15 one-title at phone width", async ({ page }) => {
  await open(page, "demo", "light", 390, 844);
  const found: Violation[] = [];
  for (const view of ["overview", "learnings"]) {
    await page.evaluate((v) => (location.hash = "view=" + v), view);
    await page.waitForTimeout(120);
    found.push(...(await page.evaluate(audit, "one-title")));
  }
  record("one-title-phone", found);
  expect(found, found.map((v) => v.detail).join("\n")).toEqual([]);
});

test("§15 options never overflow", async ({ page }) => {
  const found: Violation[] = [];
  for (const scenario of ["long", "longtitle"]) {
  await open(page, scenario);
  found.push(...await everyRoute(page, async () => {
    const result: Violation[] = [];
    const triggers = page.locator("[data-page]:not([hidden]) [role=combobox]:visible");
    for (let i = 0; i < (await triggers.count()); i++) {
      try {
        await triggers.nth(i).click({ timeout: 3000 });
      } catch {
        result.push({ rule: "options", detail: `combobox ${i} could not be opened` });
        continue;
      }
      await page.waitForTimeout(150);
      result.push(...(await page.evaluate(audit, "options")));
      await page.keyboard.press("Escape");
      await page.locator("[role=listbox]:visible").waitFor({ state: "detached", timeout: 2000 }).catch(() => page.keyboard.press("Escape"));
    }
    return result;
  }));
  }
  record("options", found);
  expect(found, found.map((v) => v.detail).join("\n")).toEqual([]);
});

test("§15 sidebar hierarchy", async ({ page, request }) => {
  await open(page);
  const state = await (await request.get("/api/state?k=demo")).json();
  const found = [...(await page.evaluate(sidebarShape, state)), ...(await everyRoute(page, async () => page.evaluate(audit, "hierarchy")))];
  record("hierarchy", found);
  expect(found, found.map((v) => v.detail).join("\n")).toEqual([]);
});

test("§15 scroll chains to the page", async ({ page }) => {
  const found: Violation[] = [];
  for (const scenario of ["demo", "hundred"]) {
    await open(page, scenario);
    found.push(...(await everyRoute(page, async (label) => {
      const result = await page.evaluate(audit, "chaining");
      // Behaviour, not only style: wheel past the end of every inner scroll area.
      const inner = await page.evaluate(() => {
        const page = document.querySelector("#workspace-content")!;
        document.querySelectorAll("[data-fitness-inner]").forEach((e) => e.removeAttribute("data-fitness-inner"));
        return [...document.querySelectorAll("[data-page]:not([hidden]) *")].filter((e) => {
          const cs = getComputedStyle(e);
          return /(auto|scroll)/.test(cs.overflowY) && e.scrollHeight > e.clientHeight + 1 && e.getBoundingClientRect().height > 40 && !e.closest("[role=dialog]");
        }).map((e, i) => { e.setAttribute("data-fitness-inner", String(i)); return i; }).filter(() => page.scrollHeight > page.clientHeight);
      });
      for (const i of inner) {
        const box = page.locator(`[data-fitness-inner="${i}"]`);
        await page.locator("#workspace-content").evaluate((e) => (e.scrollTop = 0));
        await box.scrollIntoViewIfNeeded();
        const before = await page.locator("#workspace-content").evaluate((e) => { const top = e.scrollTop; return { top, room: e.scrollHeight - e.clientHeight - top }; });
        if (before.room < 20) continue;
        await box.evaluate((e) => (e.scrollTop = e.scrollHeight));
        const r = (await box.boundingBox())!;
        await page.mouse.move(r.x + r.width / 2, r.y + Math.min(r.height / 2, 40));
        await page.mouse.wheel(0, 300);
        await page.waitForTimeout(250);
        const after = await page.locator("#workspace-content").evaluate((e) => e.scrollTop);
        if (after <= before.top) result.push({ rule: "chaining", detail: `wheel at the end of inner area ${i} did not scroll the page (${label})` });
      }
      return result;
    })).map((v) => ({ ...v, detail: `[${scenario}] ${v.detail}` })));
  }
  record("chaining", found);
  expect(found, found.map((v) => v.detail).join("\n")).toEqual([]);
});

test("§15 overview is a decision dashboard", async ({ page }) => {
  const found: Violation[] = [];
  for (const scenario of ["demo", "hundred"]) {
    await open(page, scenario);
    await page.evaluate(() => (location.hash = "view=overview"));
    await page.waitForTimeout(150);
    found.push(...(await page.evaluate(audit, "overview")).map((v) => ({ ...v, detail: `[${scenario}] ${v.detail}` })));
  }
  record("overview", found);
  expect(found, found.map((v) => v.detail).join("\n")).toEqual([]);
});

test("r9 gutters, header action and input icon follow their tokens at desktop and phone widths", async ({ page }) => {
  const found: Violation[] = [];
  for (const width of [1440, 320]) for (const theme of ["light", "dark"] as const) {
    await open(page, "hundred", theme, width, 900);
    if (width < 768) await page.getByRole("button", { name: "Toggle Sidebar", exact: true }).click();
    await page.waitForTimeout(150);
    found.push(...(await page.evaluate(audit, "spacing")).map((v) => ({ ...v, detail: `[${width}/${theme}] ${v.detail}` })));
  }
  record("spacing", found);
  expect(found, found.map((v) => v.detail).join("\n")).toEqual([]);
});
