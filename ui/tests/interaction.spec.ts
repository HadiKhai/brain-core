import { test, expect, chromium, webkit } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
test("real touch activates mobile navigation, filters, paging and the fixed import dock", async ({
  browser,
}) => {
  test.setTimeout(90_000);
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
    await sheet.getByRole("button", { name, exact: true }).or(sheet.getByRole("link", { name: new RegExp("^" + name) })).first().tap();
    await expect(sheet).not.toBeVisible();
  };
  await nav("Learnings");
  await page.getByRole("tab", { name: /^Accepted/ }).tap();
  await expect(page.locator("[data-page=learnings] .list-header .list-count")).toHaveText("1–5 of 5 learnings");
  await page.getByRole("button", { name: "Clear filters" }).tap();
  await page.getByRole("textbox", { name: "Search learnings" }).tap();
  await page.getByRole("textbox", { name: "Search learnings" }).fill("filters");
  await expect(page.locator("[data-page=learnings] .list-header .list-count")).toHaveText("1–1 of 1 learning");
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
  await nav("Jobs");
  const ledger = page.locator("[data-page=jobs] .data-table").first();
  await expect(ledger.getByRole("combobox", { name: "Sort by" })).toContainText(
    "Original order",
  );
  await ledger.getByRole("button", { name: "Next", exact: true }).tap();
  await expect(ledger.locator(".table-footer")).toContainText("11–14 of 14");
  await ledger.getByRole("combobox", { name: "Sort by" }).tap();
  await page.getByRole("option", { name: "Job", exact: true }).tap();
  await page.getByRole("button", { name: "Toggle Sidebar" }).tap();
  await page.locator("[data-mobile=true]").getByRole("button", { name: "Add projects" }).tap();
  await page.getByRole("checkbox", { name: "Pocket Atlas", exact: true }).tap();
  const dock = page.locator(".add-sheet .sheet-dock");
  await dock.getByRole("button", {name:"Review import",exact:true}).tap();
  await expect(page.locator(".reading-sheet").getByRole("textbox", { name: "Command to copy" })).toHaveValue("brain adopt --parent 'root' -- '/demo/pocket-atlas'");
  await page.locator(".reading-sheet").getByRole("button",{name:"Close",exact:true}).tap();
  const before = await dock.boundingBox();
  await page.locator(".add-sheet .sheet-body").evaluate((e) => (e.scrollTop = e.scrollHeight));
  const after = await dock.boundingBox();
  expect(after!.y).toBe(before!.y);
  await dock.getByRole("combobox", { name: "Parent project" }).tap();
  await page.getByRole("option", { name: /^Field Guide/ }).tap();
  await dock.getByRole("button",{name:"Review import",exact:true}).tap();
  await expect(page.locator(".reading-sheet").getByRole("textbox")).toHaveValue("brain adopt --parent 'field-guide' -- '/demo/pocket-atlas'");
  await page.locator(".reading-sheet").getByRole("button",{name:"Close",exact:true}).tap();
  await dock.getByRole("button", { name: "Clear", exact: true }).tap();
  await expect(dock.getByRole("button", { name: "Review import" })).toHaveCount(0);
  await page.locator(".add-sheet").getByRole("button",{name:"Close",exact:true}).tap();
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
  await page.keyboard.press("ArrowDown");
  await expect(page.getByRole("button", { name: "Inbox", exact: true })).toBeFocused();
  await expect(page).toHaveURL(/view=inbox/);
  await page.keyboard.press("ArrowDown");
  await expect(page).toHaveURL(/view=projects/);
  await page.keyboard.press("End");
  await expect(page).toHaveURL(/view=graph/);
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
  // The reference picker: keyboard opens it with the most relevant records, typing searches the server.
  await page.keyboard.press("Enter");
  await expect(page.getByRole("option").first()).toBeVisible();
  await page.keyboard.type("guide");
  await expect(page.getByRole("option")).toHaveCount(1);
  await expect(page.getByRole("option").locator("mark")).toHaveText("Guide");
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("combobox", { name: "Project", exact: true })).toContainText("Field Guide");
  await page.getByRole("button", { name: "Clear filters" }).click();
  await search.focus();
  await page.keyboard.type("filters");
  await expect(page.locator("[data-page=learnings] .list-header .list-count")).toHaveText("1–1 of 1 learning");
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
  await expect(page.locator(".reading-sheet .markdown").first()).toBeVisible();
  await page.keyboard.press("Escape");
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
      "inbox",
      "projects",
      "specialists",
      "jobs",
      "threads",
      "learnings",
      "usage",
      "graph",
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
    await page.keyboard.press("Escape");
    await page.evaluate(() => (location.hash = "view=overview&add=1"));
    await page
      .getByRole("checkbox", { name: "Pocket Atlas", exact: true })
      .click();
    await page.locator(".add-sheet .sheet-dock").getByRole("button", {name:"Review import",exact:true}).click();
    await expect(
      page.locator(".reading-sheet").getByRole("textbox", { name: "Command to copy" }),
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
  await page.goto("/?k=empty#view=projects");
  await expect(page.locator("[data-page=projects]")).toContainText("No projects yet.");
  await page.goto("/?k=empty#view=overview");
  await expect(page.locator("[data-page=overview] [data-kpi]").first()).toContainText("0");
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

test("browser Back restores each destination page scroll position", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 600 });
  await page.goto("/?k=hundred&history=1#view=usage");
  await expect(page.locator("[data-page=usage]")).toBeVisible();
  const restored = await page.locator("#workspace-content").evaluate((e) => { e.scrollTop = 300; return e.scrollTop; });
  expect(restored).toBeGreaterThan(0);
  await page.getByRole("button", { name: "Learnings", exact: true }).click();
  await expect(page).toHaveURL(/view=learnings/);
  await page.goBack();
  await expect(page).toHaveURL(/view=usage/);
  await expect.poll(() => page.locator("#workspace-content").evaluate((e) => e.scrollTop)).toBe(restored);
});

test("browser Back retains a full-height list's own scroll position", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 600 });
  await page.goto("/?k=hundred#view=learnings");
  const list = page.locator("[data-page=learnings] .list-scroll");
  await expect(list).toBeVisible();
  const restored = await list.evaluate(e => { e.scrollTop = 300; return e.scrollTop; });
  expect(restored).toBeGreaterThan(0);
  await page.getByRole("link", { name: /^Usage/ }).click();
  await page.goBack();
  await expect(page).toHaveURL(/view=learnings/);
  await expect.poll(() => list.evaluate(e => e.scrollTop)).toBe(restored);
});

test("job state separates its kind from the clamped readable description", async ({ page }) => {
  await page.goto("/?k=long&badge=1#view=threads");
  for (const width of [1440, 320]) {
    await page.setViewportSize({ width, height: 900 });
    const row = page.locator("[data-page=threads] .row.thread").first();
    await expect(row.locator(".status").first()).toHaveText("Reviewing");
    const description = row.locator(".flight-description");
    await expect(description).toContainText("complete narrow-screen navigation");
    expect(await description.evaluate(e => e.clientHeight <= 2 * parseFloat(getComputedStyle(e).lineHeight) + 1)).toBe(true);
    await description.locator("..").focus();
    await expect(page.locator('[data-slot="tooltip-content"]')).toContainText("preserving the project context");
    await page.keyboard.press("Escape");
  }
});

for (const [engine, browserType] of Object.entries({ chromium, webkit })) {
  test(`${engine}: project page, reader scrim, focus, metadata, keyboard and activity`, async () => {
    const browser = await browserType.launch();
    const page = await browser.newPage();
    for (const width of [1440, 320]) {
      await page.setViewportSize({ width, height: 900 });
      for (const theme of ["light", "dark"]) {
        await page.emulateMedia({ colorScheme: theme as "light" | "dark" });
        // Old addresses still land: `node=` opens the project's page.
        await page.goto("http://127.0.0.1:4109/?k=demo#view=overview&node=field-guide&tab=threads");
        const project = page.locator("[data-page=project]");
        await expect(page.locator(".page-header h1")).toHaveText("Field Guide");
        await expect(page.locator('[data-slot="sidebar"]')).toHaveCount(width >= 768 ? 1 : 0);
        await project.getByRole("button", { name: "Check job", exact: true }).first().click();
        const dialog = page.getByRole("dialog");
        await expect(dialog).toBeVisible();
        await expect(dialog.getByRole("button", { name: "Close", exact: true })).toBeFocused();
        expect(await page.locator('[data-slot="sheet-overlay"]').evaluate(e => getComputedStyle(e).backdropFilter)).toBe("none");
        await expect.poll(() => dialog.evaluate(e => getComputedStyle(e).opacity)).toBe("1");
        for (let i = 0; i < 6; i++) {
          await page.keyboard.press("Tab");
          await expect.poll(() => dialog.evaluate(e => e.contains(document.activeElement))).toBe(true);
        }
        await page.keyboard.press("Escape");
        await expect(dialog).not.toBeVisible();
        await project.getByRole("tab", { name: /^About/ }).click();
        await expect(project.locator(".detail-facts dt")).toHaveText(["State", "Runtime", "Owners", "Labels", "Updated", "Home"]);
        const dots = await project.locator('.status').evaluateAll(es => es.filter(e => e.getClientRects().length).map(e => {
          const a=e.getBoundingClientRect(), b=e.querySelector('.status-dot')!.getBoundingClientRect();
          return Math.abs((a.top+a.bottom)/2-(b.top+b.bottom)/2);
        }));
        expect(dots.every(d => d < 1)).toBe(true);
        await project.getByRole("tab", { name: /^Activity/ }).click();
        await expect(project.locator("[data-item]").first()).toBeVisible();
        const spark = project.locator('.spark').first();
        await expect(spark).toHaveAttribute("aria-label", /Commits per hour, last 72 hours/);
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
    page.locator("[data-page=overview] [data-kpi]").first(),
  ).toBeVisible();
  await expect
    .poll(() => page.evaluate(() => (window as any).__notifications.length))
    .toBe(2);
});
