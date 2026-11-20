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
// Execute the real presenter JSX with transparent UI primitives, without a DOM/server.
function renderUsagePresenter(file, name, props) {
  const js = ts.transpile(readFileSync(new URL(`../src/${file}.tsx`, import.meta.url), "utf8"), {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React,
  });
  const react = { createElement: (type, props, ...children) => typeof type === "function"
    ? type({ ...props, children }) : { children }, Fragment: Symbol("fragment") };
  const primitives = new Proxy({}, { get: () => ({ children }) => children });
  const exports = {};
  new Function("exports", "require", "React", js)(exports, id => id === "./model" ? m : primitives, react);
  const text = node => node == null || typeof node === "boolean" ? "" : Array.isArray(node)
    ? node.map(text).join(" ") : typeof node === "object" ? text(node.children) : String(node);
  return text(exports[name](props));
}

test("usage and overview presenters retain stale age, verification and safe lookup error", () => {
  const row = { id: "one", runtime: "claude", label: "Fixture", source: "lookup",
    read_at: Date.now() / 1000 - 469 * 3600, note: "Claude usage lookup refused (HTTP 503)",
    windows: { seven_day: { used_percent: 20, resets_at: Date.now() / 1000 + 86400 } } };
  const s = { usage: [row], usage_policy: { stop_percent: 85, max_age_seconds: 120 } };
  for (const [file, name] of [["overview", "Meters"], ["usage", "Usage"]]) {
    const text = renderUsagePresenter(file, name, { s, active: true });
    assert.match(text, /469 h/, file);
    assert.match(text, /unverified/i, file);
    assert.match(text, /HTTP 503/, file);
  }
});

test("usage presenter separates signed out and unavailable from exhausted capacity", () => {
  const row = { id: "one", label: "Fixture", read_at: Date.now() / 1000, source: "lookup" };
  const state = rows => ({ usage: rows, usage_policy: { stop_percent: 85, max_age_seconds: 120 } });
  const signedOut = renderUsagePresenter("usage", "Usage", { s: state([{ ...row, signed_out: true }]), active: true });
  assert.match(signedOut, /signed out/i);
  assert.doesNotMatch(signedOut, /all subscriptions are at the stop line/i);
  const unavailable = renderUsagePresenter("usage", "Usage", { s: state([{ ...row, read_at: null }]), active: true });
  assert.match(unavailable, /unavailable|unverified/i);
  const exhausted = renderUsagePresenter("usage", "Usage", { s: state([{ ...row,
    windows: { seven_day: { used_percent: 94, resets_at: Date.now() / 1000 + 86400 } } }]), active: true });
  assert.match(exhausted, /stop line/i);
});
test("budget badges use replacement-round counters while lifetime usage stays available", () => {
  const lifetime = { processed: 900000, compactions: 8, active_h: 9 };
  const open = { usage: lifetime, age_h: 12, round_usage: { processed: 2000, compactions: 0, hours: 0.25 } };
  assert.deepEqual(m.budgetUsage(open), { processed: 2000, compactions: 0, hours: 0.25 });
  assert.equal(open.usage, lifetime);
  assert.deepEqual(m.budgetUsage({ ...open, round_usage: null }), { processed: 900000, compactions: 8, hours: 12 });
  assert.deepEqual(m.budgetUsage({ ...open, round_usage: { processed: null, compactions: null, hours: null } }),
    { processed: null, compactions: null, hours: null });
});
test("closed ledger rows and stale worktrees never masquerade as in-flight jobs", () => {
  const n = {
    id: "demo",
    jobs: {
      rows: [
        { Job: "closed", Outcome: "merged" },
        { Job: "reused", Outcome: "merged" },
      ],
      open: [{ Job: "reused", Started: "new" }],
    },
    work: [
      { branch: "closed", state: "Review" },
      { branch: "reused", state: "Working" },
      { branch: "finished", state: "Closed · complete" },
    ],
    worktrees: [{ branch: "closed" }, { branch: "orphan" }],
  };
  assert.deepEqual(
    m.flight([n]).map((x) => x.job.branch),
    ["reused", "orphan"],
  );
  assert.equal(m.flight([n])[1].job.state, "Worktree only");
});
test("hire outcome joins the exact job incarnation", () => {
  const n = [
    {
      id: "demo",
      jobs: {
        rows: [
          {
            Job: "same",
            Started: "2026-01-01",
            Ended: "2026-01-02",
            Outcome: "merged",
          },
          { Job: "same", Started: "2026-02-01", Outcome: "abandoned" },
        ],
      },
    },
  ];
  assert.equal(
    m.hireRow({ node: "demo", job: "same", time: "2026-01-01T12:00:00Z" }, n)
      .Outcome,
    "merged",
  );
  assert.equal(
    m.hireRow({ node: "demo", job: "same", started: "2026-02-01" }, n).Outcome,
    "abandoned",
  );
});
test("unknown, absent, stale, expired and threshold readings stay distinct", () => {
  const row = { read_at: 1000 },
    p = { stop_percent: 73, max_age_seconds: 120 };
  assert.equal(m.windowState(undefined, row, p, 1001).state, "Unknown");
  assert.equal(
    m.windowState({ absent: true }, row, p, 1001).state,
    "No window",
  );
  assert.equal(
    m.windowState({ used_percent: 73 }, row, p, 1001).state,
    "At stop line",
  );
  assert.equal(
    m.windowState({ used_percent: 63 }, row, p, 1001).state,
    "Near stop line",
  );
  assert.equal(
    m.windowState({ used_percent: 62.9 }, row, p, 1001).state,
    "Normal",
  );
  assert.equal(
    m.windowState({ used_percent: 80 }, row, p, 1201).state,
    "Stale · unverified",
  );
  assert.equal(
    m.windowState({ used_percent: 20, resets_at: 999 }, row, p, 1001).state,
    "Expired · unknown",
  );
});
test("proposals precede accepted items and specialist provenance is retained", () => {
  const rows = m.learningRows({
    items: {
      all: [
        { id: "a", status: "active" },
        { id: "p", status: "proposed", title: "Review me" },
      ],
    },
    specialists: [
      {
        id: "tester",
        node: "demo",
        lessons: [{ text: "Test safely", from: "demo", time: "2026-01-01" }],
      },
    ],
  });
  assert.equal(rows[0].id, "p");
  assert.equal(
    rows.find((r) => r.kind === "Specialist lesson").provenance.ref,
    "specialists/tester/lessons.md",
  );
});
test("commands preserve exact shell arguments and links reject executable schemes", () => {
  assert.equal(
    m.quote("a'b; $(touch /tmp/nope)"),
    "'a'\\''b; $(touch /tmp/nope)'",
  );
  for (const link of [
    "javascript:alert(1)",
    "data:text/html,hi",
    "file:///etc/passwd",
  ])
    assert.equal(m.safeHref(link), undefined);
  assert.equal(m.safeHref("https://example.com"), "https://example.com");
  assert.equal(
    m.readable("01234567-abcd-abcd-abcd-0123456789ab"),
    "Linked conversation",
  );
});
test("editable surfaces use shadcn controls and never browser disclosure/tooltips/dialogs", () => {
  for (const file of readdirSync(new URL("../src", import.meta.url)).filter(
    (f) => f.endsWith(".tsx"),
  )) {
    const s = readFileSync(new URL("../src/" + file, import.meta.url), "utf8");
    assert.doesNotMatch(s, /<(?:select|details|summary|input|button)\b/, file);
    assert.doesNotMatch(
      s,
      /\b(?:window\.)?(?:alert|confirm|prompt)\s*\(/,
      file,
    );
    assert.doesNotMatch(s, /<[a-z][^>]+\btitle=/, file);
  }
});

test("status copy preserves sentences and punctuation", () => {
  assert.equal(
    m.label("Reviewing narrow-screen navigation"),
    "Reviewing narrow-screen navigation",
  );
  assert.equal(m.label("stale_status"), "Stale status");
});

test("project tree preserves depth, orphans and cycle containment", () => {
  const rows = m.treeRows([
    { id: "leaf", parent: "child" },
    { id: "root" },
    { id: "child", parent: "root" },
    { id: "orphan", parent: "missing" },
    { id: "cycle", parent: "cycle" },
    { id: "archive", archived: true },
  ]);
  assert.deepEqual(
    rows.map((r) => [r.node.id, r.depth]),
    [
      ["root", 0],
      ["child", 1],
      ["leaf", 2],
      ["orphan", 0],
      ["cycle", 0],
    ],
  );
});

test("missing job accounting is not a zero measurement and timezone formatting is explicit", () => {
  assert.equal(
    m.hasMeasuredUsage({ Usage: "not counted: ExampleError" }),
    false,
  );
  assert.equal(m.hasMeasuredUsage({ Usage: "fixture", "Fresh in": "0" }), true);
  assert.match(m.time("2026-10-20T10:00:00Z", "Asia/Tokyo"), /7:00 PM/);
  assert.match(m.readable("At 2026-10-20T10:00:00Z", "Asia/Tokyo"), /7:00 PM/);
});

test("machine pressure preserves processor, memory and disk warning thresholds", () => {
  const rows = m.machineHealth({
    load: [7.5],
    cpus: 8,
    mem_free_gb: 7,
    mem_total_gb: 32,
    disk_free_gb: 39,
    disk_total_gb: 1000,
    disk_reserve_gb: 40,
    disk_floor_gb: 40,
  });
  assert.equal(rows.length, 3);
  assert.ok(rows.every((r) => r.state === "Critical pressure"));
  assert.equal(
    m.machineHealth({ load: [6], cpus: 8 })[0].state,
    "Near capacity",
  );
  assert.deepEqual(m.machineHealth({}), []);
});

test("job state kinds stay short while arbitrary descriptions remain readable", () => {
  assert.deepEqual(m.jobState("Reviewing navigation and keyboard focus"), {kind:"Reviewing", description:"navigation and keyboard focus"});
  assert.deepEqual(m.jobState("ready to merge · checks passed"), {kind:"ready to merge", description:"checks passed"});
  assert.deepEqual(m.jobState("Unusual freeform update"), {kind:"In progress", description:"Unusual freeform update"});
});

test("task copy hides paths and job ids while preserving usable links", () => {
  assert.equal(m.humanText("Review report ~/demo/reviews/HANDOFF.md for job/navigation. Check ready packet."), "Review the report for this job. Check review checks.");
  assert.equal(m.humanText("<script>literal, untrusted content</script>"), "<script>literal, untrusted content</script>");
  assert.equal(m.humanText("Read /opt/work/review.md"), "Read the project files");
  assert.equal(m.humanText("[Source](https://example.com/docs/review.md)"), "[Source](https://example.com/docs/review.md)");
});

test("subscription stop lines override the default in meters and routing", () => {
  const now = Date.now() / 1000;
  const policy = { stop_percent: 85, max_age_seconds: 120 };
  const row = { id: "one", runtime: "codex", source: "lookup", read_at: now, stop_percent: 50,
    windows: { seven_day: { used_percent: 50, resets_at: now + 86400 } } };
  assert.equal(m.windowState(row.windows.seven_day, row, policy, now).state, "At stop line");
  const other = { ...row, id: "two", stop_percent: 95 };
  assert.equal(m.windowState(other.windows.seven_day, other, policy, now).state, "Normal");
  const state = { usage: [row, other], usage_policy: policy, usage_routing_policy: { pacing_margin: 10 } };
  assert.equal(m.routing(state).row.id, "two");
  assert.equal(m.windowState({ used_percent: 85 }, { read_at: now }, policy, now).state, "At stop line");
});

test("signed-out subscriptions never contribute headroom or stop-line checks", () => {
  const now = Date.now() / 1000;
  const policy = { stop_percent: 85, max_age_seconds: 120 };
  const active = { id: "active", read_at: now, windows: {
    seven_day: { used_percent: 40, resets_at: now + 86400, source: "lookup" } } };
  for (const used of [0, 99]) {
    const signedOut = { ...active, id: "signed-out", signed_out: true,
      windows: { seven_day: { ...active.windows.seven_day, used_percent: used } } };
    assert.equal(m.routing({ usage: [signedOut, active], usage_policy: policy }).row.id, "active");
    assert.equal(m.routing({ usage: [signedOut], usage_policy: policy }), undefined);
  }
});

test("Needs you keeps one item per id and leaves advice out", () => {
  const request = { id: "one", kind: "inbox", node: "demo", text: "question: Which color?", recipient: "operator", options: ["Blue", "Gray"], recommendation: "Blue" };
  const another = { ...request, id: "two" };
  const state = { inbox: [request, { ...request }, another], due: [
    { ...request }, { id: "step", node: "demo", kind: "handback", text: "Review the report" },
    { id: "advice", node: "demo", kind: "job", informational: true, text: "Consider a smaller job" },
  ] };
  const view = m.needsView(state, "demo");
  assert.deepEqual(view.items.map(i => i.id), ["one", "two", "step"]);
  assert.deepEqual(view.inbox.map(i => i.id), ["one", "two"]);
  assert.deepEqual(view.due.map(i => i.id), ["step"]);
  assert.equal(m.needAsk("Choose a color; waiting on operator; clears when chosen; since: 2020-01-01; recommendation: blue; default: gray"),
    "Choose a color; waiting on operator; clears when chosen");
  assert.equal(state.inbox.length, 3);
});


test("disk status follows declared reserve and bounded floor without guessed thresholds", () => {
  const disk = { disk_free_gb: 18, disk_total_gb: 1000, disk_reserve_gb: 43, disk_floor_gb: 12 };
  assert.equal(m.machineHealth(disk)[0].state, "Below reserve · admitted");
  assert.equal(m.machineHealth({ ...disk, disk_floor_gb: 43 })[0].state, "Critical pressure");
  assert.equal(m.machineHealth({ ...disk, disk_free_gb: 43 })[0].state, "Normal");
  assert.equal(m.machineHealth({ disk_free_gb: 18, disk_total_gb: 1000 })[0].state, "Reserve unknown");
});
