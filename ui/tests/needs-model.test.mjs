import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import ts from "typescript";
const source = readFileSync(new URL("../src/model.ts", import.meta.url), "utf8");
const emitted = ts.transpile(source, { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 });
const m = await import("data:text/javascript;base64," + Buffer.from(emitted).toString("base64"));
const ask = { id: "q", node: "demo", kind: "inbox", text: "question: Which color?", recipient: "operator", options: ["Blue", "Gray"], recommendation: "Blue" };

test("only open routed questions with choices and a recommendation need the operator", () => {
  assert.equal(m.openDecision(ask), true);
  assert.equal(m.openDecision({ ...ask, recipient: "maple" }, Date.now(), "maple"), true);
  for (const invalid of [{ recipient: "coordinator" }, { options: [] }, { recommendation: "" }, { text: "DECISION verify-speed: Land the fix" },
    { resolved: true }, { answered: true }, { answer: "Blue" }, { open: false }, { state: "closed" }, { status: "done" },
    { superseded: true }, { state: "expired" }, { deadline: "2000-01-01T00:00:00Z" }, { informational: true }, { kind: "machine-alert" }])
    assert.equal(m.openDecision({ ...ask, ...invalid }), false, JSON.stringify(invalid));
  assert.equal(m.openDecision({ ...ask, deadline: "2000-01-01T00:00:00Z", options: [{ id: "a", label: "Publish", action: "outward" }, { id: "b", label: "Wait", action: "routine" }], recommendation: "b" }), true);
  assert.equal(m.openDecision({ ...ask, options: [{ id: "a", label: "Blue" }, { id: "b", label: "Gray" }], recommendation: "missing" }), false);
});

test("requests, decisions and coordinator steps share lifecycle filtering and retain archive sources", () => {
  const order = { ...ask, id: "order", text: "DECISION verify-speed: Land the fix", source: "demo/inbox.md" };
  const closed = { ...ask, id: "closed", answered: true, source: "demo/inbox-archive.md" };
  const state = { inbox: [ask, { ...ask }, order, closed], due: [{ ...ask }, { id: "ready", kind: "handback", ready: true },
    { id: "old", kind: "handback", state: "done" }, { id: "advice", kind: "job", informational: true }] };
  const view = m.needsView(state);
  assert.deepEqual(view.inbox.map(i => i.id), ["q"]);
  assert.deepEqual(view.decisions.map(i => i.id), ["q"]);
  assert.deepEqual(view.due.map(i => i.id), ["ready"]);
  assert.deepEqual(view.archive.map(i => i.id), ["order", "closed", "old"]);
  assert.equal(view.archive[1].source, "demo/inbox-archive.md");
  assert.equal(state.inbox.length, 4);
});

test("sidebar hides closed jobs but retains unfinished and reopened branches behind no age cutoff", () => {
  const nodes = [{ id: "demo", work: [{ branch: "job/old", state: "merged" }, { branch: "job/open", state: "blocked" }, { branch: "job/unrecorded", state: "Review approved" }],
    jobs: { rows: [{ Job: "job/old", Ended: "2020-01-01" }, { Job: "job/open", Ended: "2020-01-01" }], open: [{ Job: "job/open" }] },
    worktrees: [{ branch: "job/open" }] }];
  assert.deepEqual(m.sidebarThreads(nodes).map(f => f.job.branch), ["job/open"]);
  assert.deepEqual(m.sidebarThreads(nodes, true).map(f => f.job.branch), ["job/open", "job/old", "job/unrecorded"]);
});
