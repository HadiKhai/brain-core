import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import ts from "typescript";
const load = async (file) => {
  const js = ts.transpile(readFileSync(new URL("../src/" + file, import.meta.url), "utf8"), { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 });
  return import("data:text/javascript," + encodeURIComponent(js));
};
const rank = await load("rank.ts"), model = await load("model.ts"), graph = await load("graph-layout.ts");

test("snapshot search ranks exactly like the server: exact, start, word start, contains, then A to Z", () => {
  const names = ["Mariana", "Bruno Ana", "Ánalyst", "Ana Lima", "ana", "Zed"];
  const state = { nodes: names.map((title, i) => ({ id: "n" + i, title, updated: `2026-01-0${i + 1}` })) };
  const ours = rank.localSearch(state, "node", "ana").items.map((i) => i.title);
  assert.deepEqual(ours, ["ana", "Ana Lima", "Ánalyst", "Bruno Ana", "Mariana"]);
  const py = JSON.parse(execFileSync("python3", ["-c", `
import json, sys
sys.path.insert(0, sys.argv[1])
from brain import ui as m
print(json.dumps([m.ui_search(json.loads(sys.argv[2]), "node", q)["items"] for q in ("ana", "", "lima", "مدرسه")]))`,
    new URL("../../lib", import.meta.url).pathname, JSON.stringify({ ...state, nodes: [...state.nodes, { id: "ar", title: "مدرسة" }] })], { encoding: "utf8" }));
  const local = ["ana", "", "lima", "مدرسه"].map((q) => rank.localSearch({ ...state, nodes: [...state.nodes, { id: "ar", title: "مدرسة" }] }, "node", q).items);
  assert.deepEqual(local, py);
  const many = { nodes: Array.from({ length: 45 }, (_, i) => ({ id: "p" + i, title: "Project " + i })) };
  assert.deepEqual([0, 20, 40].map((o) => rank.localSearch(many, "node", "project", o).next), [20, 40, null]);
});

test("a Due card names its job from the record, and the sender comes from the record, never the text", () => {
  const due = model.dueParts({ kind: "handback", branch: "job/navigation-review", text: "job/navigation-review: ready to merge; review it", ready: true });
  assert.equal(due.title, "Navigation review");
  assert.equal(due.body, "ready to merge; review it");
  assert.equal(due.label, "Ready to merge");
  assert.equal(model.dueParts({ kind: "stale-status", text: "update `brain status x --refresh`" }).title, "");
  assert.equal(model.sender({ kind: "blocked", from: "", text: "Waiting on copy from Morgan: a read" }), "Project status");
  assert.equal(model.sender({ kind: "inbox", from: "worker", text: "2026-01-01 00:00 from someone-else: question" }), "worker");
});

test("each open thread has one tone, and graph labels never overlap nodes or each other", () => {
  assert.equal(model.tone({ job: { branch: "a", state: "reviewing the layout" } }), "review");
  assert.equal(model.tone({ job: { branch: "a", state: "building" } }, [{ ready: true, branch: "a" }]), "ready");
  assert.equal(model.tone({ job: { branch: "a", state: "Worktree only" } }), "idle");
  assert.equal(model.jobTitle("import:14e945a9a4bfd7aeb0084b84"), "Import");
  assert.equal(model.jobTitle("job/ui-redesign"), "Ui redesign");
  for (const count of [0, 1, 100, 400]) {
    const nodes = Array.from({ length: count }, (_, i) => ({ key: "k" + i, kind: i % 3 ? "learning" : "node", id: String(i), title: "A project with a long name " + i, r: 6, recent: false, x: 0, y: 0, vx: 0, vy: 0 }));
    const edges = nodes.slice(1).map((_, i) => ({ a: 0, b: i + 1, kind: "source" }));
    graph.layout(nodes, edges, 80);
    const labels = graph.placeLabels(nodes, new Set(nodes.map((n) => n.key)));
    const boxes = nodes.filter((n) => labels.has(n.key)).map((p) => graph.labelBox(p, labels.get(p.key)));
    for (let i = 0; i < boxes.length; i++) {
      for (let j = i + 1; j < boxes.length; j++) assert.ok(!(boxes[i].x0 < boxes[j].x1 && boxes[i].x1 > boxes[j].x0 && boxes[i].y0 < boxes[j].y1 && boxes[i].y1 > boxes[j].y0), "labels overlap");
      for (const p of nodes) assert.ok(!(p.x + p.r > boxes[i].x0 && p.x - p.r < boxes[i].x1 && p.y + p.r > boxes[i].y0 && p.y - p.r < boxes[i].y1), "label covers a node");
    }
    if (count) assert.ok(labels.size > 0);
  }
});
