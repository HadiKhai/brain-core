import { test, expect } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/?k=demo#view=graph");
  await expect(page.locator(".graph-canvas svg")).toBeVisible();
});

test("graph captures trackpad and Safari pinch without trapping ordinary wheel scrolling", async ({ page }) => {
  const canvas = page.locator(".graph-canvas svg");
  const result = await canvas.evaluate((svg) => {
    const transform = () => svg.querySelector("[data-graph-viewport]")?.getAttribute("transform");
    const initial = transform();
    const wheel = new WheelEvent("wheel", { bubbles: true, cancelable: true, ctrlKey: true, deltaY: -100 });
    svg.dispatchEvent(wheel);
    const ordinary = new WheelEvent("wheel", { bubbles: true, cancelable: true, deltaY: 100 });
    svg.dispatchEvent(ordinary);
    return { pinchPrevented: wheel.defaultPrevented, ordinaryPrevented: ordinary.defaultPrevented, initial };
  });
  expect(result.pinchPrevented).toBe(true);
  expect(result.ordinaryPrevented).toBe(false);
  await expect(canvas.locator("[data-graph-viewport]")).not.toHaveAttribute("transform", result.initial!);
  const beforeGesture = await canvas.locator("[data-graph-viewport]").getAttribute("transform");
  const prevented = await canvas.evaluate((svg) => ["gesturestart", "gesturechange", "gestureend"].map((type) => {
    const event = new Event(type, { bubbles: true, cancelable: true });
    Object.defineProperty(event, "scale", { value: type === "gesturestart" ? 1 : 1.6 });
    svg.dispatchEvent(event);
    return event.defaultPrevented;
  }));
  expect(prevented).toEqual([true, true, true]);
  await expect(canvas.locator("[data-graph-viewport]")).not.toHaveAttribute("transform", beforeGesture!);
  await canvas.focus();
  await page.keyboard.press("Home");
  await expect(canvas).toHaveAttribute("data-zoom", "1");
  await page.keyboard.press("+");
  await expect(canvas).toHaveAttribute("data-zoom", "1.25");
});

for (const theme of ["light", "dark"]) {
  test(`${theme}: all projects and highest-degree specialist/learning nodes have permanent labels and every node has an accessible tooltip`, async ({ page }) => {
    await page.evaluate((value) => document.documentElement.classList.toggle("dark", value === "dark"), theme);
    const projects = page.locator("[data-page=graph] .gnode.kind-node");
    for (const node of await projects.all()) await expect(node.locator("text")).toBeVisible();
    await expect(page.locator("[data-page=graph] .gnode.kind-specialist text")).toHaveCount(Math.min(3, await page.locator("[data-page=graph] .gnode.kind-specialist").count()));
    await expect(page.locator("[data-page=graph] .gnode.kind-learning text")).toHaveCount(Math.min(3, await page.locator("[data-page=graph] .gnode.kind-learning").count()));
    for (const kind of ["specialist", "learning"]) {
      const labelsFollowDegree = await page.locator(`[data-page=graph] .gnode.kind-${kind}`).evaluateAll((records) => {
        const ranked = records.map((e) => ({ key: e.getAttribute("data-graph-key")!, degree: Number(e.getAttribute("data-links")), label: !!e.querySelector("text") }))
          .sort((a, b) => b.degree - a.degree || a.key.localeCompare(b.key));
        return ranked.slice(0, 3).every((record) => record.label);
      });
      expect(labelsFollowDegree).toBe(true);
    }
    const nodes = page.locator("[data-page=graph] .gnode");
    for (const node of await nodes.all()) {
      await node.focus();
      const tooltip = page.getByRole("tooltip");
      await expect(tooltip).toBeVisible();
      await expect(tooltip).toContainText(/Project|Specialist|Learning/);
      await expect(tooltip).toContainText("Last activity");
      await expect(tooltip).toContainText(/\d+ links?/);
      await expect(tooltip.locator("[data-graph-name]")).not.toHaveText("");
      await expect(tooltip.locator("[data-graph-project]")).not.toHaveText("");
      expect(await node.getAttribute("aria-describedby")).toBe(await tooltip.getAttribute("id"));
    }
    await page.keyboard.press("Escape");
    await expect(page.getByRole("tooltip")).toBeHidden();
    await projects.first().locator(".dot").hover();
    await expect(page.getByRole("tooltip")).toBeVisible();
  });
}

test("graph node click and keyboard activation open the corresponding record", async ({ page }) => {
  await page.locator('[data-page=graph] .gnode[data-graph-key="node:field-guide"] .dot').click();
  await expect(page).toHaveURL(/view=project.*node=field-guide/);
  await page.goto("/?k=demo#view=graph");
  await page.locator('[data-page=graph] .gnode[data-graph-key="specialist:root/product-reviewer"]').focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/view=specialists.*item=root%2Fproduct-reviewer/);
  await expect(page.getByRole("dialog")).toContainText("Product reviewer");
  await page.goto("/?k=demo#view=graph");
  await page.locator('[data-page=graph] .gnode[data-graph-key="learning:lesson-0"]').focus();
  await page.keyboard.press("Space");
  await expect(page).toHaveURL(/view=learnings.*item=lesson-0/);
  await expect(page.getByRole("dialog")).toContainText("Keep filters next to the work");
});

test("dense graph keeps a permanent, collision-free label for every project", async ({ page }) => {
  await page.goto("/?k=hundred#view=graph");
  const projects = page.locator("[data-page=graph] .gnode.kind-node");
  await expect(projects).toHaveCount(100);
  await expect(projects.locator("text")).toHaveCount(100);
  const overlaps = await page.locator(".graph-canvas svg").evaluate((svg) => {
    const labels = Array.from(svg.querySelectorAll(".gnode text")).map((e) => ({ title: e.textContent, box: e.getBoundingClientRect() }));
    return labels.flatMap((a, i) => labels.slice(i + 1).filter((b) => a.box.left < b.box.right && a.box.right > b.box.left && a.box.top < b.box.bottom && a.box.bottom > b.box.top)
      .map((b) => ({ first: a.title, second: b.title, firstBounds: a.box.toJSON(), secondBounds: b.box.toJSON() })));
  });
  expect(overlaps).toEqual([]);
});

for (const scenario of ["ten", "hundred"]) {
  test(`${scenario}: graph side list shares the table frame and keeps its pager reachable`, async ({ page }) => {
    await page.goto(`/?k=${scenario}#view=graph`);
    const aside = page.locator("[data-page=graph] .graph-side");
    const frame = aside.locator("[data-list-frame]");
    await expect(frame).toBeVisible();
    await expect(frame.locator(".list-header")).toContainText(`1–10 of ${scenario === "ten" ? 10 : 100} projects`);
    await expect(frame.getByRole("combobox", { name: "Rows per page", exact: true })).toBeVisible();
    expect(await aside.evaluate((e) => getComputedStyle(e).overflowY)).toBe("visible");
    const footer = frame.locator(".table-footer");
    const before = (await footer.boundingBox())!;
    const bounds = (await aside.boundingBox())!;
    expect(before.y + before.height).toBeLessThanOrEqual(bounds.y + bounds.height);
    await frame.locator(".list-scroll").evaluate((e) => { e.scrollTop = e.scrollHeight; });
    expect((await footer.boundingBox())!.y).toBeCloseTo(before.y, 0);
    if (scenario === "hundred") {
      await footer.getByRole("button", { name: "Next", exact: true }).click();
      await expect(frame.locator(".list-header")).toContainText("11–20 of 100 projects");
    }
  });
}
