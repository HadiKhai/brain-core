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

test("status copy preserves sentences and punctuation",()=>{assert.equal(m.label("Reviewing narrow-screen navigation"),"Reviewing narrow-screen navigation");assert.equal(m.label("stale_status"),"Stale status")})

test("project tree preserves depth, orphans and cycle containment",()=>{const rows=m.treeRows([{id:'leaf',parent:'child'},{id:'root'},{id:'child',parent:'root'},{id:'orphan',parent:'missing'},{id:'cycle',parent:'cycle'},{id:'archive',archived:true}]);assert.deepEqual(rows.map(r=>[r.node.id,r.depth]),[['root',0],['child',1],['leaf',2],['orphan',0],['cycle',0]])})
