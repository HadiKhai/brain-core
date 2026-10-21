import { test, expect } from "@playwright/test";

// The collapsed rail keeps every sub-item reachable: hover, focus and tap open a flyout, and
// choosing a destination navigates without expanding the rail (ui-designer §10.8).
test("collapsed rail: Agents and project sub-items open in a flyout by hover, focus and tap", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/?k=demo#view=overview");
  await page.getByRole("button", { name: "Toggle Sidebar" }).click();
  const rail = page.locator("[data-state=collapsed][data-slot=sidebar]");
  await expect(rail).toBeVisible();
  // The state attribute changes before the collapsing groups finish moving. Hover the settled
  // trigger: an early pointer can otherwise end up outside it as the group labels slide away.
  await rail.evaluate(async (e) => {
    await Promise.all(e.getAnimations({ subtree: true })
      .filter((a) => a.effect?.getTiming().iterations !== Infinity)
      .map((a) => a.finished.catch(() => {})));
  });
  const agents = rail.getByRole("button", { name: "Agents", exact: true });
  const flyout = page.locator(".rail-flyout");

  await agents.hover();
  await expect(flyout).toBeVisible();
  await expect(flyout.getByRole("menuitem")).toHaveText([/^Specialists/, /^Jobs/, /^Threads/, /^Usage/]);
  await flyout.getByRole("menuitem", { name: /^Threads/ }).click();
  await expect(page).toHaveURL(/view=threads/);
  await expect(rail).toBeVisible();          // still collapsed
  await expect(flyout).toBeHidden();

  await page.mouse.move(900, 500);
  await page.waitForTimeout(300);           // a flyout that just closed hands focus back without reopening
  await page.getByRole("button", { name: "Overview", exact: true }).focus();
  await page.keyboard.press("ArrowDown");   // keyboard focus moves along the rail
  await agents.focus();
  await expect(flyout).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(flyout).toBeHidden();

  await page.mouse.move(900, 500);
  await agents.click();                      // a tap or click both open it and keep the rail collapsed
  await expect(flyout).toBeVisible();
  await flyout.getByRole("menuitem", { name: /^Usage/ }).click();
  await expect(page).toHaveURL(/view=usage/);
  await expect(rail).toBeVisible();

  await page.mouse.move(900, 500);
  await rail.getByRole("button", { name: "Field Guide", exact: true }).hover();
  await expect(flyout).toBeVisible();
  await expect(flyout.getByRole("menuitem")).toHaveText([/^Project page/, /^Coordinator/, /^Navigation/]);
  await flyout.getByRole("menuitem", { name: /^Navigation/ }).click();
  await expect(page).toHaveURL(/view=project.*node=field-guide.*thread=job%2Fnavigation/);
  await expect(rail).toBeVisible();
});
