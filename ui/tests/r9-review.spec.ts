import { test, expect } from "@playwright/test";
import { spawn, type ChildProcess } from "node:child_process";
import { resolve } from "node:path";
let server: ChildProcess;
async function start(write = true): Promise<string> {
  server = spawn("python3", [resolve("tests/review-fixture.py"), ...(write ? ["--write"] : [])], { stdio: ["ignore", "pipe", "pipe"] });
  return new Promise((accept, reject) => {
    let output = "", error = "";
    const timer = setTimeout(() => reject(new Error("Synthetic review server startup timed out: " + error)), 30_000);
    server.stderr!.on("data", chunk => { error += chunk; });
    server.stdout!.on("data", chunk => { output += chunk; const url = output.match(/http:\/\/127\.0\.0\.1:\d+\/\?k=[\w-]+/); if (url) { clearTimeout(timer); accept(url[0]); } });
    server.on("exit", code => { clearTimeout(timer); reject(new Error("Synthetic server exited " + code + ": " + error)); });
  });
}
test.afterEach(async () => { if (server && server.exitCode === null) { server.kill("SIGTERM"); await new Promise(r => server.once("exit", r)); } });

test("local write mode is off by default and exposes no decision buttons", async ({ page }) => {
  await page.goto(await start(false) + "#view=learnings&state=proposed");
  await expect(page.locator(".workspace-footer")).toContainText("Read-only view");
  await expect(page.getByRole("button", { name: /Approve all|^Accept$|^Reject$/ })).toHaveCount(0);
});

test("bulk review confirms exact node scopes and records real core changes with a receipt", async ({ page, request }) => {
  const url = await start();
  await page.goto(url + "#view=learnings&state=proposed");
  await expect(page.locator(".workspace-footer")).toContainText("Local proposal reviews enabled");
  await page.locator('[data-page=learnings]').getByRole("button", { name: "Approve all (2)", exact: true }).click();
  const dialog = page.getByRole("dialog").last();
  await expect(dialog).toContainText("1 · root · scope: Demo reviewer");
  await expect(dialog).toContainText("1 · garden · scope: Demo reviewer");
  await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
  const api = url.replace("/?", "/api/state?");
  expect((await (await request.get(api)).json()).items.all.every((i: any) => i.status === "proposed")).toBe(true);
  await page.locator('[data-page=learnings]').getByRole("button", { name: "Approve all (2)", exact: true }).click();
  await page.getByRole("button", { name: "Confirm approve (2)", exact: true }).click();
  await expect(dialog).toContainText("2 proposals reviewed by Demo reviewer via local UI.");
  await dialog.getByRole("button", { name: /lesson-a.md · exact changes/ }).click();
  await expect(dialog).toContainText("+accepted_via: local UI");
  const state = await (await request.get(api)).json();
  expect(state.items.all.map((i: any) => i.status)).toEqual(["active", "active"]);
  expect(state.items.all.every((i: any) => i.scope.join(", ") === "Demo reviewer")).toBe(true);
});

test("specialist proposals offer accept and reject; rejection records the operator and preserves scope", async ({ page, request }) => {
  const url = await start();
  await page.goto(url + "#view=specialists&item=root%2Flayout-guide");
  const panel = page.getByRole("dialog").first();
  await panel.getByRole("button", { name: "Reject", exact: true }).click();
  await page.getByRole("button", { name: "Confirm reject (1)", exact: true }).click();
  await expect(page.getByRole("dialog").last()).toContainText("1 proposal reviewed by Demo reviewer via local UI.");
  const state = await (await request.get(url.replace("/?", "/api/state?"))).json();
  expect(state.specialists[0].status).toBe("rejected");
  expect(state.specialists[0].scope).toEqual(["Demo reviewer"]);
});
