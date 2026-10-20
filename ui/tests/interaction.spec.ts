import { test, expect, chromium, webkit } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
test("real touch activates mobile navigation, filters, paging and the fixed import dock", async ({
  browser,
}) => {
  const context = await browser.newContext({
    viewport: { width: 320, height: 640 },
    hasTouch: true,
    isMobile: true,
    deviceScaleFactor: 1,
  });
  const page = await context.newPage();
  await page.goto("/?k=demo");
  const nav = async (name: string) => {
    await page.getByRole("button", { name: "Toggle Sidebar" }).tap();
    const sheet = page.locator("[data-mobile=true]");
    await expect(
      sheet.getByRole("button", { name: "Close navigation" }),
    ).toBeVisible();
    await sheet.getByRole("button", { name, exact: true }).tap();
    await expect(sheet).not.toBeVisible();
  };
  await nav("Learnings");
  await page.getByRole("combobox", { name: "State", exact: true }).tap();
  await page.getByRole("option", { name: "Accepted (5)", exact: true }).tap();
  await expect(page.locator(".results-meta")).toHaveText("5 learnings");
  await page.getByRole("button", { name: "Clear filters" }).tap();
  await page.getByRole("textbox", { name: "Search learnings" }).tap();
  await page.getByRole("textbox", { name: "Search learnings" }).fill("filters");
  await expect(page.locator(".results-meta")).toHaveText("1 learning");
  expect(
    await page.evaluate(
      () =>
        getComputedStyle(
          document.querySelector('input[placeholder="Search learnings"]')!,
        ).fontSize,
    ),
  ).toBe("16px");
  await nav("Usage");
  await nav("Learnings");
  await expect(
    page.getByRole("textbox", { name: "Search learnings" }),
  ).toHaveValue("filters");
  await page.getByRole("button", { name: "Clear filters" }).tap();
  await nav("Agents");
  const ledger = page.locator(".ledger-entry .data-table").first();
  await expect(ledger.getByRole("combobox", { name: "Sort by" })).toContainText(
    "Original order",
  );
  await ledger.getByRole("button", { name: "Next", exact: true }).tap();
  await expect(ledger.locator(".table-footer")).toContainText("11–14 of 14");
  await ledger.getByRole("combobox", { name: "Sort by" }).tap();
  await page.getByRole("option", { name: "Job", exact: true }).tap();
  await nav("Import checklist");
  await page.getByRole("checkbox", { name: "Pocket Atlas", exact: true }).tap();
  const dock = page.locator("#action-dock-host");
  await expect(
    dock.getByRole("textbox", { name: "Command to copy" }),
  ).toHaveValue("brain adopt --parent 'root' -- '/demo/pocket-atlas'");
  const before = await dock.boundingBox();
  await page
    .locator("#workspace-content")
    .evaluate((e) => (e.scrollTop = e.scrollHeight));
  const after = await dock.boundingBox();
  expect(after!.y).toBe(before!.y);
  await dock.getByRole("combobox", { name: "Parent project" }).tap();
  await page.getByRole("option", { name: "Field Guide", exact: true }).tap();
  await expect(dock.getByRole("textbox")).toHaveValue(
    "brain adopt --parent 'field-guide' -- '/demo/pocket-atlas'",
  );
  await dock.getByRole("button", { name: "Clear", exact: true }).tap();
  await expect(dock.getByRole("textbox")).toHaveCount(0);
  await nav("Overview");
  await page.getByRole("button", { name: "Toggle theme" }).tap();
  expect(
    await page.evaluate(() =>
      document.documentElement.classList.contains("dark"),
    ),
  ).toBe(true);
  await page.getByRole("button", { name: "Toggle Sidebar" }).tap();
  await page.getByRole("button", { name: "Close navigation" }).tap();
  await expect(page.locator("[data-mobile=true]")).not.toBeVisible();
  expect(
    await page.evaluate(
      () =>
        matchMedia("(pointer:coarse)").matches && navigator.maxTouchPoints > 0,
    ),
  ).toBe(true);
  await context.close();
});
test("keyboard navigation, visible focus, disclosures and collapsed rail stay usable", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/?k=demo");
  const overview = page.getByRole("button", { name: "Overview", exact: true });
  await overview.focus();
  await page.keyboard.press("ArrowRight");
  await expect(
    page.getByRole("button", { name: "Learnings", exact: true }),
  ).toBeFocused();
  await expect(page).toHaveURL(/view=learnings/);
  await page.keyboard.press("End");
  await expect(page).toHaveURL(/view=imports/);
  await page.keyboard.press("Home");
  await expect(page).toHaveURL(/view=overview/);
  await page.getByRole("button", { name: "Toggle Sidebar" }).click();
  const rail = page.locator("[data-state=collapsed][data-slot=sidebar]");
  await expect(rail).toBeVisible();
  await expect
    .poll(() =>
      page
        .locator("[data-slot=sidebar-menu-button] svg")
        .first()
        .evaluate((e) => {
          const icon = e.getBoundingClientRect(),
            button = e.closest("button")!.getBoundingClientRect();
          return icon.left >= button.left && icon.right <= button.right;
        }),
    )
    .toBe(true);
  await page.getByRole("button", { name: "Learnings", exact: true }).hover();
  await expect(page.locator("[data-slot=tooltip-content]")).toHaveText(
    "Learnings",
  );
  await page.getByRole("button", { name: "Learnings", exact: true }).focus();
  await expect(page.locator("[data-slot=tooltip-content]")).toHaveText(
    "Learnings",
  );
  await page.keyboard.press("Enter");
  await expect(rail).toBeVisible();
  const search = page.getByRole("textbox", { name: "Search learnings" });
  await search.focus();
  await page.keyboard.press("Tab");
  await expect(
    page.getByRole("combobox", { name: "Project", exact: true }),
  ).toBeFocused();
  await page.keyboard.press("Enter");
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Clear filters" }).click();
  await search.focus();
  await page.keyboard.type("filters");
  await expect(page.locator(".results-meta")).toHaveText("1 learning");
  const focus = await search.evaluate((e) => ({
    groupShadow: getComputedStyle(e.closest("[data-slot=input-group]")!)
      .boxShadow,
    innerShadow: getComputedStyle(e).boxShadow,
  }));
  expect(focus.groupShadow).not.toBe("none");
  await page.getByRole("button", { name: "Clear filters" }).click();
  await page
    .getByRole("button", { name: "Read learning", exact: true })
    .first()
    .focus();
  await page.keyboard.press("Enter");
  await expect(page.locator(".learning-entry .markdown").first()).toBeVisible();
  expect(
    await page.locator('button button, button [tabindex="0"]').count(),
  ).toBe(0);
});
test("browser assertions at narrow width and contrast geometry remain executable", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/?k=demo");
  await page.waitForSelector("[data-page=overview]");
  const result = await page.evaluate(
    readFileSync(resolve("..", "tests", "ui-checks.js"), "utf8"),
  );
  expect(result.passed).toBeGreaterThanOrEqual(60);
});

test("long labels, missing values, untrusted markdown and exact import paths remain usable", async ({
  page,
}) => {
  for (const width of [1440, 320]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/?k=long&width=" + width);
    await page.waitForSelector("[data-page=overview]");
    for (const view of [
      "overview",
      "learnings",
      "agents",
      "usage",
      "imports",
    ]) {
      await page.evaluate((v) => (location.hash = "view=" + v), view);
      await expect(page.locator(`[data-page=${view}]`)).toBeVisible();
      expect(
        await page.evaluate(
          () =>
            document.documentElement.scrollWidth <= innerWidth &&
            document.querySelector("#workspace-content")!.scrollWidth <=
              document.querySelector("#workspace-content")!.clientWidth,
        ),
      ).toBe(true);
    }
    await page.evaluate(() => (location.hash = "view=learnings"));
    await page
      .getByRole("button", { name: "Read learning", exact: true })
      .first()
      .click();
    expect(await page.locator("[data-ui-attack]").count()).toBe(0);
    expect(await page.evaluate(() => Boolean((window as any).uiAttack))).toBe(
      false,
    );
    await page.evaluate(() => (location.hash = "view=imports"));
    await page
      .getByRole("checkbox", { name: "Pocket Atlas", exact: true })
      .click();
    await expect(
      page.getByRole("textbox", { name: "Command to copy" }),
    ).toHaveValue(
      "brain adopt --parent 'root' -- '/demo/a '\\''quoted'\\''; $(touch nope)'",
    );
  }
});

test("saved snapshots and loading, empty, error, permission and partial states are explicit", async ({
  page,
}) => {
  await page.goto("/snapshot");
  await expect(page.locator(".topbar")).toContainText("Snapshot");
  expect(
    await page.evaluate(
      () =>
        performance
          .getEntriesByType("resource")
          .filter((r) => r.name.includes("/api/")).length,
    ),
  ).toBe(0);
  await page.goto("/?k=empty");
  await expect(page.locator(".project-tree")).toContainText("No projects yet.");
  await page.goto("/?k=error");
  await expect(
    page.getByText("Could not reach your brain", { exact: true }),
  ).toBeVisible();
  await page.goto("/");
  await expect(
    page.getByText("Open your private workspace link", { exact: true }),
  ).toBeVisible();
  await page.goto("/?k=loading");
  await expect(page.locator(".skeleton-page")).toBeVisible();
  await expect(page.locator("[data-slot=skeleton]").first()).toBeVisible();
  await page.goto("/?k=partial#view=usage");
  await expect(
    page.getByText("Partial reading", { exact: true }),
  ).toBeAttached();
  await expect(page.getByText(/14 of 42 transcripts counted/)).toBeAttached();
});

test("browser Back restores each destination scroll position", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/?k=demo&history=1#view=learnings");
  await page.waitForSelector(".learning-entry");
  await page.locator("#workspace-content").evaluate((e) => (e.scrollTop = 400));
  await page.getByRole("button", { name: "Usage", exact: true }).click();
  await expect(page).toHaveURL(/view=usage/);
  await page.goBack();
  await expect(page).toHaveURL(/view=learnings/);
  await expect
    .poll(() => page.locator("#workspace-content").evaluate((e) => e.scrollTop))
    .toBe(400);
});

test("job state separates its kind from the clamped readable description", async ({ page }) => {
  await page.goto("/?k=long&badge=1");
  for (const width of [1440, 320]) {
    await page.setViewportSize({ width, height: 900 });
    const row = page.locator(".flight-row").first();
    await expect(row.locator(".status").first()).toHaveText("Reviewing");
    const description = row.locator(".flight-description");
    await expect(description).toContainText("complete narrow-screen navigation");
    expect(await description.evaluate(e => e.clientHeight <= 2 * parseFloat(getComputedStyle(e).lineHeight) + 1)).toBe(true);
    await description.focus();
    await expect(page.locator('[data-slot="tooltip-content"]')).toContainText("preserving the project context");
    await page.keyboard.press("Escape");
  }
});

for (const [engine, browserType] of Object.entries({ chromium, webkit })) {
  test(`${engine}: detail scrim, focus, metadata, keyboard and activity`, async () => {
    const browser = await browserType.launch();
    const page = await browser.newPage();
    for (const width of [1440, 320]) {
      await page.setViewportSize({ width, height: 900 });
      for (const theme of ["light", "dark"]) {
        await page.emulateMedia({ colorScheme: theme as "light" | "dark" });
        await page.goto("http://127.0.0.1:4109/?k=demo#view=overview&node=field-guide");
        const dialog = page.getByRole("dialog");
        await expect(dialog).toBeVisible();
        await expect(dialog.getByRole("button", { name: "Close", exact: true })).toBeFocused();
        expect(await page.locator('[data-slot="sheet-overlay"]').evaluate(e => getComputedStyle(e).backdropFilter)).toBe("none");
        await expect(page.locator('[data-slot="sidebar"]')).toHaveCount(1);
        await expect(dialog.locator("dt")).toHaveText(["Owners", "Labels", "Updated", "Home"]);
        const link = dialog.getByRole("link", { name: "Open coordinator" });
        expect((await link.boundingBox())!.width).toBeLessThan(160);
        for (let i = 0; i < 6; i++) {
          await page.keyboard.press("Tab");
          expect(await dialog.evaluate(e => e.contains(document.activeElement))).toBe(true);
        }
        const dots = await page.locator('.node-sheet .status').evaluateAll(es => es.map(e => {
          const a=e.getBoundingClientRect(), b=e.querySelector('.status-dot')!.getBoundingClientRect();
          return Math.abs((a.top+a.bottom)/2-(b.top+b.bottom)/2);
        }));
        expect(dots.every(d => d < 1)).toBe(true);
        await page.screenshot({ path: `/Users/hadikhai/WebstormProjects/brain-core-work/reports/ui-redesign/tour/detail-${engine}-${width}-${theme}.png` });
        await page.keyboard.press("Escape");
        await expect(dialog).not.toBeVisible();
        const axis = page.locator('.heat-axis').first();
        await axis.scrollIntoViewIfNeeded();
        await expect(axis).toContainText("72 h ago");
        const cell = page.locator('.heat-grid .tip-trigger').first();
        await cell.focus();
        await expect(page.locator('[data-slot="tooltip-content"]')).toContainText(/Oct.*commit/);
        await page.keyboard.press("Escape");
      }
    }
    await browser.close();
  });
}

test("a corrupt notification cache recovers without losing the workspace", async ({
  page,
}) => {
  await page.goto("/snapshot?notify=granted&cache=before");
  await page.evaluate(() => {
    localStorage.setItem("brain-notify", "1");
    localStorage.setItem(
      "brain-inbox-seen:" + JSON.stringify(["meadow-demo", "~/demo/meadow"]),
      "{}",
    );
  });
  await page.goto("/snapshot?notify=granted&cache=after");
  await expect(
    page.getByRole("heading", { name: "Overview", exact: true }),
  ).toBeVisible();
  await expect
    .poll(() => page.evaluate(() => (window as any).__notifications.length))
    .toBe(1);
});
