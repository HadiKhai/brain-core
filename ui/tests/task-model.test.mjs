import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import ts from "typescript";
const source = readFileSync(new URL("../src/task-model.ts", import.meta.url), "utf8");
let emitted = ts.transpile(source, { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 });
emitted = emitted.replace('"mdast-util-from-markdown"', JSON.stringify(new URL("../node_modules/mdast-util-from-markdown/index.js", import.meta.url).href));
const m = await import("data:text/javascript;base64," + Buffer.from(emitted).toString("base64"));
const row = (node, id, status = "backlog", depends_on = [], extra = {}) => ({ node, id, status, depends_on, title: id, ...extra });

test("kanban maps every backend state without hiding parked or unproven delivery", () => {
  const expected = { backlog: "Backlog", ready: "Ready", "in-progress": "In progress", "in-review": "In progress", testing: "In progress", blocked: "Parked", parked: "Parked", merged: "Done", done: "Done", dropped: "Dropped" };
  for (const [status, column] of Object.entries(expected)) assert.equal(m.taskColumn({ status }), column);
  assert.equal(m.taskColumn({ status: "done", delivery_state: "unproven" }), "Parked");
});

test("edges run from dependency to dependent with node-qualified identities", () => {
  const graph = m.taskGraph([row("a", "T-1"), row("a", "T-2", "ready", ["T-1", "b:T-1", "absent:T-9"]), row("b", "T-1")]);
  assert.deepEqual(graph.edges, [{ from: "a:T-1", to: "a:T-2" }, { from: "b:T-1", to: "a:T-2" }]);
  assert.equal(graph.incoming.get("a:T-1").size, 0);
  const positions = m.taskLayout(graph).positions;
  assert.ok(positions.get("a:T-1").x < positions.get("a:T-2").x);
});

test("downstream is transitive, deduplicates diamonds and terminates on cycles", () => {
  const graph = m.taskGraph([row("a", "1"), row("a", "2", "ready", ["1"]), row("a", "3", "ready", ["1"]), row("a", "4", "ready", ["2", "3"])]);
  assert.deepEqual([...m.downstream(graph, "a:1")].sort(), ["a:2", "a:3", "a:4"]);
  assert.deepEqual([...m.downstream(graph, "a:4")], []);
  const cycle = m.taskGraph([row("a", "1", "ready", ["2"]), row("a", "2", "ready", ["1"])]);
  assert.deepEqual([...m.downstream(cycle, "a:1")], ["a:2"]);
  assert.equal(m.taskLayout(cycle).positions.size, 2);
});

test("node, owner, search and dropped filters compose without changing graph context", () => {
  const rows = [row("a", "T-1", "ready", [], { owner: "Dev", title: "Publish export", branch: "job/export" }), row("a", "T-2", "dropped", ["T-1"], { owner: "Dev" }), row("b", "T-1", "ready", [], { owner: "Dev" })];
  assert.deepEqual(m.filterTasks(rows, "a", "Dev", "EXPORT", false).map(m.taskKey), ["a:T-1"]);
  assert.equal(m.filterTasks(rows, "a", "Dev", "", true).length, 2);
  assert.equal(m.filterTasks(rows, "", "", "", false).length, 2);
  assert.deepEqual([...m.downstream(m.taskGraph(rows), "a:T-1")], ["a:T-2"]);
});

test("real-size independent layouts pack tasks instead of a single unreadable column", () => {
  const graph = m.taskGraph(Array.from({ length: 1500 }, (_, i) => row("a", String(i))));
  const layout = m.taskLayout(graph);
  assert.equal(layout.positions.size, 1500);
  assert.equal(new Set([...layout.positions.values()].map(p => p.x + ":" + p.y)).size, 1500);
  assert.ok(layout.width < 1600);
});


test("titles use CommonMark text, preserve escaped punctuation and shorten raw dumps without losing stored text", () => {
  assert.equal(m.plainTaskTitle('**Bold** _title_ with `code` and [context](https://example.com)'), 'Bold title with code and context');
  assert.equal(m.plainTaskTitle('Keep snake_case and \\*literal stars\\*'), 'Keep snake_case and *literal stars*');
  assert.equal(m.plainTaskTitle('<b>Visible</b> &amp; ![image label](image.png)'), 'Visible & image label');
  assert.equal(m.taskTitle({ title: 'Work on job/verify-log', branch: 'job/verify-log' }), 'Work on job/verify-log');
  assert.equal(m.taskTitle({ title: 'Work on this job', branch: 'job/verify-log' }), 'Work on job/verify-log');
  const source = '**A raw imported note** ' + 'Full retained body. '.repeat(30);
  const task = { title: source };
  assert.ok(m.taskTitle(task).length <= 120);
  assert.equal(task.title, source);
});

test("default canvas includes only connected active tasks while totals exclude dropped", () => {
  const rows = [row('a', '1', 'ready'), row('a', '2', 'backlog', ['1']), row('a', '3', 'done', ['2']), row('a', '4', 'parked'), row('a', '5', 'dropped')];
  assert.deepEqual(m.connectedActiveTasks(rows).map(m.taskKey), ['a:1', 'a:2']);
  assert.equal(m.visibleTaskCount(rows), 4);
  assert.equal(m.activeTask({ status: 'parked' }), false);
  assert.equal(m.activeTask({ status: 'done', delivery_state: 'unproven' }), false);
  assert.equal(m.taskTone({ status: 'blocked' }), 'blocked');
  assert.equal(m.taskTone({ status: 'ready', blocked_by: ['1'] }), 'blocked');
  assert.equal(m.taskTone({ status: 'done', delivery_state: 'unproven' }), 'unproven');
});
