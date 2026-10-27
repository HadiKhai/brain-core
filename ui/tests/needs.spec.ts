import { test, expect } from "@playwright/test";

const base = process.env.BRAIN_TEST_UI_URL || "http://127.0.0.1:4109";
test("Needs you deduplicates source ids and shows the ask without advice", async ({ page }) => {
  const state = await (await page.request.get(base + "/api/state?k=demo")).json();
  const text = "Choose a color; waiting on operator; clears when chosen; since: 2020-01-01; recommendation: Blue advice; default: Gray advice";
  const request = { id: "one-need", node: "field-guide", kind: "blocked-overdue", waiting_on_operator: true, text, first_seen: "2020-01-01T00:00:00Z", recommendation: "Blue advice", default: "Gray advice" };
  state.inbox = [request, { ...request }];
  state.due = [{ ...request }, { id: "informational", node: "field-guide", kind: "job", informational: true, text: "Split this job advice" }];
  state.nodes.find((n: any) => n.id === "field-guide").blocked = [text];
  state.usage[0].stop_percent = 50;
  state.usage[0].read_at = Date.now() / 1000;
  await page.addInitScript(s => { (window as any).__STATE__ = s; }, state);
  await page.goto(base + "/?k=demo#view=project&node=field-guide&tab=needs");
  const project = page.locator("[data-page=project]");
  await expect(project.getByRole("tab", { name: /Needs you/ })).toContainText("1");
  await expect(project.locator(".row.request")).toHaveCount(1);
  await expect(project.locator(".row.request")).toContainText("Choose a color");
  await expect(project.locator(".row.request")).not.toContainText("Blue advice");
  await expect(project.locator(".row.request")).not.toContainText("Gray advice");
  await expect(project).not.toContainText("Split this job advice");
  if (process.env.BRAIN_TEST_SCREENSHOT_DIR) await page.screenshot({ path: process.env.BRAIN_TEST_SCREENSHOT_DIR + "/needs-project.png" });
  await page.goto(base + "/?k=demo#view=overview");
  const needs = page.getByRole("region", { name: "Needs you", exact: true });
  await expect(needs.locator(".row.request")).toHaveCount(1);
  await expect(needs).not.toContainText("Blue advice");
  await expect(needs).not.toContainText("Split this job advice");
  if (process.env.BRAIN_TEST_SCREENSHOT_DIR) await page.screenshot({ path: process.env.BRAIN_TEST_SCREENSHOT_DIR + "/needs-overview.png" });
  await page.goto(base + "/?k=demo#view=usage");
  const subscription = page.locator(".subscription").first();
  await expect(subscription).toContainText("At stop line");
  await expect(subscription).toContainText("Skipped for new jobs");
  if (process.env.BRAIN_TEST_SCREENSHOT_DIR) await page.screenshot({ path: process.env.BRAIN_TEST_SCREENSHOT_DIR + "/subscription-stop.png" });
});


test("Needs you counts two open decisions and excludes inbox noise and machine steps", async ({ page }) => {
  const state = await (await page.request.get(base + "/api/state?k=demo")).json();
  for (const node of state.nodes) { node.blocked = []; node.blocked_metadata = []; }
  const question = { first_seen: "2026-10-27T10:00:00Z", id: "choose-color", node: "field-guide", kind: "inbox", text: "2026-10-27 10:00 +00:00 from worker: question: Choose a color?" };
  const decision = { first_seen: "2026-10-27T10:00:00Z", id: "choose-launch", node: "field-guide", kind: "blocked", waiting_on_operator: true, text: "Choose the launch date" };
  state.inbox = [question, { ...question }, decision,
    { id: "notice", node: "field-guide", kind: "inbox", text: "2026-10-27 10:01 +00:00 from worker: done: shipped the question reader?" },
    { id: "machine", kind: "machine-alert", text: "Machine needs attention" },
    { id: "answered", kind: "question", answered: true, text: "Already answered" },
    { id: "resolved", kind: "decision", state: "resolved", text: "Already resolved" }];
  state.due = [{ ...question }, { id: "ready", node: "field-guide", kind: "handback", ready: true, text: "Review ready job" },
    { id: "status", kind: "stale-status", text: "Update status" }];
  await page.addInitScript(s => { (window as any).__STATE__ = s; }, state);
  await page.goto(base + "/?k=demo#view=overview");
  const header = page.locator('[data-page=overview] [data-kpi]').filter({ has: page.locator('.kpi-label', { hasText: /^Needs you$/ }) });
  await expect(header.locator('.kpi-value')).toHaveText("2");
  const needs = page.getByRole("region", { name: "Needs you", exact: true });
  await expect(needs.getByRole("tab", { name: "Open decisions 2", exact: true })).toHaveAttribute("aria-selected", "true");
  await expect(needs.locator('.row.request')).toHaveCount(2);
  await expect(needs).not.toContainText("shipped the question reader");
  await needs.getByRole("button", { name: "View all", exact: true }).click();
  await expect(page.locator('[data-page=inbox] .row.request')).toHaveCount(2);
  await page.goto(base + "/?k=demo#view=project&node=field-guide&tab=needs");
  await expect(page.locator('[data-page=project] .kpi-value').first()).toHaveText("2");
  await expect(page.locator('[data-page=project]').getByRole("tab", { name: "Needs you 2", exact: true })).toHaveAttribute("aria-selected", "true");
  await expect(page.locator('[data-page=project] .row.request')).toHaveCount(2);
});
