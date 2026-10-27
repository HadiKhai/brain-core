import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import ts from "typescript";
const source = readFileSync(
  new URL("../src/model.ts", import.meta.url),
  "utf8",
);
const emitted = ts.transpile(source, {
  module: ts.ModuleKind.ESNext,
  target: ts.ScriptTarget.ES2022,
});
const m = await import(
  "data:text/javascript;base64," + Buffer.from(emitted).toString("base64")
);
test("open decisions use explicit asks and operator waits, never inbox volume", () => {
  const question = { id: "q", node: "demo", kind: "inbox", text: "inbox: 2026-10-27 10:00 +00:00 from worker: question: Which color?" };
  const blocked = { id: "b", node: "demo", kind: "blocked", text: "Choose a date", waiting_on_operator: true };
  const state = { nodes: [{ id: "demo", blocked: [blocked.text, "Waiting for CI"], blocked_metadata: [{ waiting_on_operator: true }, { waiting_on_operator: false }] }],
    inbox: [question, { ...question }, blocked, { id: "noise", kind: "inbox", text: "done: reviewed the question?" }],
    due: [{ ...question }, { id: "machine", kind: "machine", waiting_on_operator: true }, { id: "job", kind: "handback", ready: true }] };
  assert.deepEqual(m.needsView(state).decisions.map(i => i.id), ["q", "b"]);
  assert.deepEqual(m.needsView(state, "demo").decisions.map(i => i.id), ["q", "b"]);
  assert.deepEqual(m.needsView(state, "other").decisions, []);
  assert.equal(m.needsView(state).due.length, 2); // Coordinator work stays available separately.
  for (const closed of [{ resolved: true }, { answered: true }, { answer: "blue" }, { open: false }, { state: "closed" }, { status: "done" }])
    assert.equal(m.openDecision({ ...question, ...closed }), false);
  assert.equal(m.openDecision({ kind: "inbox", text: "decision: Choose a date" }), true);
  assert.equal(m.openDecision({ kind: "inbox", text: "Pick a date <!-- host-question:123 -->" }), true);
  assert.equal(m.openDecision({ kind: "inbox", text: "fyi: question: already handled" }), false);
  assert.equal(m.needsView({ nodes: [{ id: "demo", blocked: ["Choose a date"], blocked_metadata: [{ waiting_on_operator: true }] }] }).decisions.length, 1);
});
