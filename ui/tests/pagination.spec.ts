import { test as base, expect } from "@playwright/test";

// A separate synthetic state per test, updated through the fixture's real event stream.
const test = base.extend<{ state: any }>({
  state: async ({ request }, use) => {
    const original = await (await request.get("/api/state?k=demo")).json();
    const state = structuredClone(original);
    const copies = (row: any, kind: string) => Array.from({ length: 35 }, (_, i) => ({ ...structuredClone(row), id: `${kind}-${i}` }));
    state.inbox = copies(state.inbox[0], "request");
    state.due = copies(state.due[0], "step");
    state.specialists = copies(state.specialists[0], "specialist").map((s: any, i: number) => ({ ...s, title: `Synthetic reviewer ${i}`, lessons: [] }));
    state.items.all = copies(state.items.all[0], "lesson").map((r: any) => ({ ...r, status: "proposed" }));
    state.items.recent = state.items.all;
    state.items.count = 35;
    state.schedules = copies(state.schedules[0], "schedule");
    state.usage = copies(state.usage[0], "subscription");
    const project = state.nodes.find((n: any) => n.id === "field-guide");
    project.jobs.rows = copies(project.jobs.rows[0], "job").map((r: any, i: number) => ({ ...r, Job: `job/synthetic-${i}` }));
    project.work = copies(project.work[0], "thread").map((r: any, i: number) => ({ ...r, branch: `job/live-${i}` }));
    project.worktrees = [];
    await request.post("/fixture/update", { data: state });
    try { await use(state); }
    finally { await request.post("/fixture/update", { data: original }); }
  },
});

test("Inbox history tab changes reset pagination without using the tab click handler", async ({ page, state }) => {
  await page.goto("/?k=demo#view=inbox");
  const inbox = page.locator("[data-page=inbox]");
  await inbox.getByRole("tab", { name: /^Coordinator steps/ }).click();
  await inbox.getByRole("button", { name: "Next", exact: true }).click();
  await inbox.getByRole("button", { name: "Next", exact: true }).click();
  await expect(inbox.locator(".list-header .list-count")).toContainText("21–30 of 35 steps");
  await page.goBack();
  await expect(inbox.getByRole("tab", { name: /^Requests/ })).toHaveAttribute("aria-selected", "true");
  await expect(inbox.locator(".list-header .list-count")).toContainText("1–10 of 35 requests");
});

test("Overview View all coordinator steps resets a retained Inbox page", async ({ page, request, state }) => {
  await request.post("/fixture/update", { data: { due: state.due.slice(0, 2) } });
  await page.goto("/?k=demo#view=inbox");
  const inbox = page.locator("[data-page=inbox]");
  await inbox.getByRole("button", { name: "Next", exact: true }).click();
  await inbox.getByRole("button", { name: "Next", exact: true }).click();
  await page.getByRole("button", { name: "Overview", exact: true }).click();
  const overview = page.locator("[data-page=overview] .needs");
  await overview.getByRole("tab", { name: /^Coordinator steps/ }).click();
  await overview.getByRole("tabpanel", { name: /^Coordinator steps/ }).getByRole("button", { name: "View all", exact: true }).click();
  await expect(inbox.locator(".row.due")).toHaveCount(2);
  await expect(inbox.locator(".list-header .list-count")).toContainText("1–2 of 2 steps");
});

test("Back to requests resets the Inbox page after coordinator steps disappear", async ({ page, request, state }) => {
  await page.goto("/?k=demo#view=inbox&tab=due");
  const inbox = page.locator("[data-page=inbox]");
  await inbox.getByRole("button", { name: "Next", exact: true }).click();
  await inbox.getByRole("button", { name: "Next", exact: true }).click();
  await request.post("/fixture/update", { data: { due: [] } });
  await page.locator("#screen-action-host").getByRole("button", { name: "Back to requests" }).click();
  await expect(inbox.locator(".list-header .list-count")).toContainText("1–10 of 35 requests");
  await expect(inbox.locator(".row.request")).toHaveCount(10);
});

for (const kind of ["inbox", "specialists", "learnings", "threads", "jobs", "usage"] as const) {
  test(`${kind}: live shrink clamps rows and footer together, and regrowth cannot revive a stale page`, async ({ page, request, state }) => {
    const hash = kind === "learnings" ? "view=learnings&state=proposed" : "view=" + kind;
    await page.goto("/?k=demo#" + hash);
    const root = page.locator(`[data-page=${kind}]`);
    const region = kind === "jobs" ? root.locator(".data-table").first() : root;
    const frame = kind === "jobs" ? region : region.locator("[data-list-frame]").first();
    const footer = frame.locator(".table-footer");
    const range = frame.locator(".list-header .list-count");
    await region.getByRole("button", { name: "Next", exact: true }).first().click();
    await region.getByRole("button", { name: "Next", exact: true }).first().click();
    await expect(range).toContainText("21–30 of");
    await expect(footer.locator(".page-position")).toHaveText("Page 3 of 4");
    const reduced = kind === "inbox" ? { inbox: state.inbox.slice(0, 2) }
      : kind === "specialists" ? { specialists: state.specialists.slice(0, 2) }
      : kind === "learnings" ? { specialists: [], items: { ...state.items, all: state.items.all.slice(0, 2), recent: [], count: 2 } }
      : kind === "threads" ? { nodes: state.nodes.map((n: any) => ({ ...n, work: n.work.slice(0, 2) })) }
      : kind === "jobs" ? { nodes: state.nodes.map((n: any) => ({ ...n, jobs: { ...n.jobs, rows: n.jobs.rows.slice(0, 2) } })) }
      : { usage: state.usage.slice(0, 2) };
    await request.post("/fixture/update", { data: reduced });
    const rows = kind === "usage" ? frame.locator(".subscription")
      : kind === "threads" ? frame.locator(".row.thread")
      : kind === "jobs" ? frame.locator("tbody tr")
      : frame.locator(".rows > [data-item]");
    await expect(rows).toHaveCount(2);
    await expect(range).toContainText("1–2 of 2");
    // A one-page result keeps its footer; the cursor must clamp in both visible places.
    await expect(footer).toBeVisible();
    await expect(footer.locator(".page-position")).toHaveText("Page 1 of 1");
    await expect(footer.getByRole("button", { name: "Previous", exact: true })).toBeDisabled();
    await expect(footer.getByRole("button", { name: "Next", exact: true })).toBeDisabled();
    await request.post("/fixture/update", { data: state });
    await expect(range).toContainText("1–10 of");
    await expect(footer.locator(".page-position")).toHaveText("Page 1 of 4");
    await expect(footer.getByRole("button", { name: "Next", exact: true })).toBeEnabled();
  });
}

test("Threads filter changes reset the page and keep the chosen row count", async ({ page, state }) => {
  await page.goto("/?k=demo#view=threads");
  const threads = page.locator("[data-page=threads]");
  await threads.getByRole("combobox", { name: "Rows per page" }).click();
  await page.getByRole("option", { name: "25", exact: true }).click();
  await threads.getByRole("button", { name: "Next", exact: true }).click();
  await threads.getByRole("textbox", { name: "Search threads" }).fill("Live");
  await expect(threads.locator(".list-header .list-count")).toContainText("1–25 of 35 threads");
  await expect(threads.locator(".row.thread")).toHaveCount(25);
});
