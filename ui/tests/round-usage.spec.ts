import { test, expect } from "@playwright/test";
import { audit } from "./fitness-rules";

test("restarted job badges use round counters and retain lifetime usage", async ({ page, request }) => {
  const state = await (await request.get("/api/state?k=demo")).json();
  const original = structuredClone(state.nodes);
  const row = state.nodes.find((node: any) => node.id === "field-guide").jobs.open[0];
  row.usage = { ...row.usage, processed: 900000, compactions: 8 };
  row.age_h = 12;
  row.round_usage = { processed: 2000, compactions: 0, hours: 0.25 };
  await request.post("/fixture/update", { data: { nodes: state.nodes } });
  try {
    await page.goto("/?k=demo#view=jobs");
    const job = page.locator('[data-page="jobs"] [data-detail-row]').filter({ hasText: "Field Guide" }).filter({ hasText: "Navigation" }).first();
    const flags = job.locator("td").first();
    await expect(job).toContainText("900K");
    await expect(flags).not.toContainText("Over token budget");
    await expect(flags).not.toContainText("Over time budget");
    await expect(flags).not.toContainText("8 compactions");
    row.round_usage = { state: "unavailable", processed: null, compactions: null, hours: null };
    await request.post("/fixture/update", { data: { nodes: state.nodes } });
    await expect(job).toContainText("Round usage unavailable");
    await expect(job).not.toContainText("Over token budget");
  } finally {
    await request.post("/fixture/update", { data: { nodes: original } });
  }
});

test("daily throughput shows zero days and measurement coverage", async ({ page, request }) => {
  const state = await (await request.get("/api/state?k=demo")).json();
  const original = structuredClone(state.nodes);
  const day = { date: "2026-01-15", merged: 0, merges_per_hour: 0, minutes_per_merge: null,
    tokens_per_merge: null, measured_merges: 0, verify_runs: 0, verify_reused: 0, reruns: 0,
    verify_measured_jobs: 0, queue_seconds: null, queue_measured_jobs: 0 };
  state.nodes[0].jobs.throughput = [day];
  state.nodes[1].jobs.throughput = [{ ...day, merged: 2, merges_per_hour: 0.5, minutes_per_merge: 120,
    tokens_per_merge: 4000, measured_merges: 1, verify_runs: 3, reruns: 1, verify_reused: 1,
    verify_measured_jobs: 2, queue_seconds: 3.5, queue_measured_jobs: 1 }];
  await request.post("/fixture/update", { data: { nodes: state.nodes } });
  try {
    await page.goto("/?k=demo#view=jobs");
    expect((await page.getByRole("region", { name: "Jobs at a glance", exact: true }).boundingBox())!.height).toBeGreaterThan(100);
    expect(await page.evaluate(audit, "hierarchy")).toEqual([]);
    await page.getByRole("button", { name: "Daily throughput", exact: true }).click();
    const throughput = page.getByRole("region", { name: "Daily throughput", exact: true });
    await expect(throughput).toContainText("0.00");
    await expect(throughput).toContainText("Unmeasured");
    await expect(throughput).toContainText("1 / 2 merges measured");
    await expect(throughput).toContainText("3 runs · 1 reruns · 1 reused");
    await expect(throughput).toContainText("3.50 s");
    const lastColumn = (await throughput.getByRole("columnheader").last().boundingBox())!;
    const dialog = (await page.getByRole("dialog").boundingBox())!;
    expect(lastColumn.x + lastColumn.width).toBeLessThanOrEqual(dialog.x + dialog.width);
  } finally {
    await request.post("/fixture/update", { data: { nodes: original } });
  }
});
