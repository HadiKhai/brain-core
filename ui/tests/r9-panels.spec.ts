import { test as base, expect } from "@playwright/test";

const test = base.extend<{ state: any }>({
  state: async ({ request }, use) => {
    const original = await (await request.get("/api/state?k=demo")).json();
    const state = structuredClone(original);
    const specialist = state.specialists[0];
    specialist.role_summary = "Review the shape of a product before implementation.";
    specialist.role_body = "# Product reviewer\n\nReview the shape of a product before implementation.\n\n## When to hire\nBefore a navigation change or a release review.";
    specialist.hire_command = "brain hire product-reviewer field-guide --task 'Review navigation'";
    specialist.lessons = Array.from({ length: 4 }, (_, i) => ({ text: `Synthetic lesson ${i}`, from: "field-guide", time: `2026-10-${20 - i}T09:00:00Z` }));
    state.nodes.find((n: any) => n.id === "observatory").coordinator = "";
    const project = state.nodes.find((n: any) => n.id === "field-guide");
    project.work[0].link = "t3-thread://v1/00000000-0000-4000-8000-000000000001";
    project.work[0].state = "In progress";
    project.jobs.open[0].Started = "2026-10-19T09:00:00Z";
    project.jobs.open[0].age_h = 30;
    project.jobs.rows.find((r: any) => r.Job === "job/navigation").Started = "2026-10-19T09:00:00Z";
    await request.post("/fixture/update", { data: state });
    try { await use(state); } finally { await request.post("/fixture/update", { data: original }); }
  },
});

test("specialist row opens a role panel with purpose, lessons, related knowledge, hires and copy command", async ({ page, state }) => {
  await page.goto("/?k=demo#view=specialists");
  await page.locator("[data-page=specialists] .row.specialist").first().getByText("Product reviewer", { exact: true }).click();
  const panel = page.getByRole("dialog");
  await expect(panel).toContainText("Review the shape of a product before implementation.");
  await expect(panel).toContainText("When to hire");
  await expect(panel).toContainText("4 lessons");
  await expect(panel).toContainText("Synthetic lesson 0");
  await expect(panel).toContainText("Synthetic lesson 2");
  await expect(panel).not.toContainText("Synthetic lesson 3");
  await expect(panel).toContainText("Related library items");
  await expect(panel).toContainText("Field Guide");
  await expect(panel.getByRole("textbox", { name: "Command to copy" })).toHaveValue(state.specialists[0].hire_command);
  await expect(panel.getByRole("button", { name: "Copy hire command", exact: true })).toBeVisible();
});

test("unverified desktop thread routes are copy-only and never dead anchors", async ({ page, context, state }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.goto("/?k=demo#view=threads");
  const row = page.locator('[data-page=threads] [data-thread="job/navigation"]').first();
  await expect(row.locator('a[href^="t3-thread:"],a[href^="t3code:"]')).toHaveCount(0);
  await row.getByRole("button", { name: "Copy thread link", exact: true }).click();
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe(state.nodes.find((n: any) => n.id === "field-guide").work[0].link);
});

test("project with no coordinator offers setup instead of an empty finder", async ({ page, state }) => {
  await page.goto("/?k=demo#view=project&node=observatory");
  await expect(page.getByRole("button", { name: "Find coordinator", exact: true })).toHaveCount(0);
  await page.locator("#screen-action-host").getByRole("button", { name: "Set up coordinator", exact: true }).click();
  await expect(page.getByRole("dialog")).toContainText("machine-setup");
});

test("long-running work shows its age and stale state without claiming a fault", async ({ page, state }) => {
  await page.goto("/?k=demo#view=threads");
  const row = page.locator('[data-page=threads] [data-thread="job/navigation"]').first();
  await expect(row).toContainText("Stale");
  await expect(row).toContainText("30 h open");
  await expect(row).not.toContainText("Blocked by fault");
});

test("blocked status distinguishes an operator wait from recorded failure evidence", async ({ page, request, state }) => {
  const project = state.nodes.find((n: any) => n.id === "observatory");
  project.blocked = ["Review the proposed launch knowledge", "Recover the failed build"];
  project.blocked_metadata = [
    { waiting_on: "the operator", since: "2026-10-19T09:00:00Z", age_hours: 30 },
    { fault: true, since: "2026-10-20T09:00:00Z", age_hours: 9 },
  ];
  await request.post("/fixture/update", { data: { nodes: state.nodes } });
  await page.goto("/?k=demo#view=project&node=observatory&tab=needs");
  const work = page.locator('[data-page=project] [aria-label=Work]');
  await expect(work).toContainText("Waiting on you");
  await expect(work).toContainText("30 h waiting");
  await expect(work).toContainText("Blocked by fault");
  await expect(work).toContainText("9 h waiting");
});
