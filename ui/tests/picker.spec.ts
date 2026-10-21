import { test, expect, type Page } from "@playwright/test";

// ui-designer §13 checklist for the shared reference picker, against the server search endpoint.
const open = async (page: Page, scenario: string) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`/?k=${scenario}#view=learnings`);
  const picker = page.getByRole("combobox", { name: "Project", exact: true });
  await picker.click();
  return page.getByRole("searchbox", { name: "Search Project" }).or(page.getByRole("combobox", { name: "Search Project" }));
};
const titles = (page: Page) => page.getByRole("option").locator(".picker-item-title").allInnerTexts();

test("empty focus lists the most relevant records at once, and one letter searches the server", async ({ page }) => {
  const search = await open(page, "many");
  await expect(page.getByRole("option").first()).toBeVisible();
  expect((await titles(page)).length).toBeGreaterThanOrEqual(10);
  await expect(search).toBeFocused();
  const requests: string[] = [];
  page.on("request", (r) => r.url().includes("/api/search") && requests.push(new URL(r.url()).searchParams.get("q") || ""));
  await search.pressSequentially("r", { delay: 30 });
  await expect.poll(() => requests.includes("r")).toBe(true);
  const first = await titles(page);
  expect(first.every((t) => /r/i.test(t))).toBe(true);
  expect(first[0]).toMatch(/^R/);   // names that start with the query come first
});

test("a surname, a mid-word query and no match rank and say what to do", async ({ page }) => {
  const search = await open(page, "many");
  await search.fill("khoury");
  await expect.poll(async () => (await titles(page))[0] || "").toMatch(/^\w+ Khoury/);
  await expect(page.getByRole("option").first().locator("mark")).toHaveText("Khoury");
  await search.fill("oury");
  await expect.poll(async () => (await titles(page))[0] || "").toMatch(/Khoury/);
  await search.fill("zzqq");
  await expect(page.getByRole("status").filter({ hasText: "No match for “zzqq”" })).toBeVisible();
  await page.getByRole("button", { name: "Clear search" }).click();
  await expect(search).toHaveValue("");
  await expect(page.getByRole("option").first()).toBeVisible();
});

test("1,000+ records page in on scroll, keyboard picks one and the chip names it", async ({ page }) => {
  const search = await open(page, "many");
  await search.fill("project");
  await search.fill("");
  await search.fill("a");
  await expect(page.getByRole("option")).toHaveCount(20);
  const list = page.locator("[data-slot=combobox-list]");
  for (let i = 0; i < 3; i++) {
    await list.evaluate((e) => (e.scrollTop = e.scrollHeight));
    await page.waitForTimeout(250);
  }
  expect(await page.getByRole("option").count()).toBeGreaterThanOrEqual(60);
  await search.fill("lina khoury 0012");
  await expect(page.getByRole("option")).toHaveCount(1);
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  const picker = page.getByRole("combobox", { name: "Project", exact: true });
  await expect(picker).toContainText("Lina Khoury 0012");
  await expect(picker).toContainText("Meadow workspace");   // the secondary line tells similar names apart
  await page.getByRole("button", { name: "Clear Project" }).click();
  await expect(picker).toContainText("All projects");
});

test("a slow network shows a skeleton, and a late response never replaces a newer one", async ({ page }) => {
  await open(page, "slow");
  await expect(page.locator(".picker-loading [data-slot=skeleton]").first()).toBeVisible();
  await expect(page.getByRole("option").first()).toBeVisible({ timeout: 5000 });
  const search = page.getByRole("searchbox", { name: "Search Project" }).or(page.getByRole("combobox", { name: "Search Project" }));
  await search.fill("obs");
  await page.waitForTimeout(300);
  await search.fill("field");
  await expect.poll(async () => (await titles(page)).join("|"), { timeout: 6000 }).toBe("Field Guide");
  await page.waitForTimeout(1500);
  expect((await titles(page)).join("|")).toBe("Field Guide");
});

test("coordinator steps name their job and render inline code", async ({ page }) => {
  await page.goto("/?k=demo#view=inbox&tab=due");
  const due = page.locator("[data-page=inbox] .row.due");
  await expect(due.first()).toContainText("Navigation");
  await expect(due.first()).not.toContainText("this job");
  await expect(due.nth(1).locator("code.inline-code")).toHaveText("brain status observatory --refresh");
  await page.evaluate(() => (location.hash = "view=inbox"));
  const blocked = page.locator("[data-page=inbox] .row.request").filter({ hasText: "launch note" });
  await expect(blocked).toContainText("Project status");
  await expect(blocked.locator(".row-line small")).not.toContainText("Morgan");
});
