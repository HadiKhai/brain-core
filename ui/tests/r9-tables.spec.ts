import { test, expect } from "@playwright/test";

for (const view of ["inbox", "specialists", "learnings", "threads", "jobs"]) {
  test(`${view}: one shared frame keeps its pager in view while rows scroll`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(`/?k=hundred#view=${view}`);
    const frame = page.locator(`[data-page=${view}] [data-list-frame]`).first();
    await expect(frame).toBeVisible();
    await expect(frame.locator(".list-header")).toContainText(/of \d+/);
    await expect(frame.locator(".list-header").getByRole("combobox", { name: "Rows per page", exact: true })).toBeVisible();
    const footer = frame.locator(".table-footer");
    const before = await footer.boundingBox();
    expect(before!.y + before!.height).toBeLessThanOrEqual(880);
    await frame.locator(".list-scroll").evaluate((e) => { e.scrollTop = e.scrollHeight; });
    await expect(footer.getByRole("button", { name: "Next", exact: true })).toBeVisible();
    expect((await footer.boundingBox())!.y).toBeCloseTo(before!.y, 0);
    await footer.getByRole("button", { name: "Next", exact: true }).click();
    await expect(frame.locator(".list-header")).toContainText("11–20 of");
  });
}

for (const view of ["specialists", "learnings", "graph"]) {
  test(`${view}: the main card fills the available desktop height`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(`/?k=demo#view=${view}`);
    const card = page.locator(`[data-page=${view}] > .surface`).first();
    await expect(card).toBeVisible();
    const bottom = (await card.boundingBox())!;
    const footer = (await page.locator(".workspace-footer").boundingBox())!;
    expect(Math.abs(bottom.y + bottom.height - footer.y)).toBeLessThanOrEqual(2);
  });
}

for (const view of ["inbox", "specialists", "learnings", "jobs", "threads"]) {
  test(`${view}: row blank space and keyboard open details without stealing child controls`, async ({ page }) => {
    await page.goto(`/?k=demo#view=${view}`);
    const row = page.locator(`[data-page=${view}] [data-detail-row]`).first();
    await expect(row).toHaveAttribute("tabindex", "0");
    await row.click({ position: { x: 3, y: 3 } });
    await expect(page.getByRole("dialog")).toBeVisible();
    await page.keyboard.press("Escape");
    await row.focus();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("dialog")).toBeVisible();
    await page.keyboard.press("Escape");
    await row.focus();
    await page.keyboard.press("Space");
    await expect(page.getByRole("dialog")).toBeVisible();
    await page.getByRole("dialog").locator("[data-slot=sheet-close]").click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    const child = row.locator(".reader-trigger").first();
    if (await child.count()) {
      await child.focus();
      await page.keyboard.press("Enter");
      // The control follows its own action; it must never trigger a second row sheet.
      await expect(page.getByRole("dialog")).toHaveCount(1);
    }
  });
}
