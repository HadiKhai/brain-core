# Inbox and wake read performance

Builder D, based on `6c716deda35684ee186fcc6c7e75b54c5b7b087a` (7.5.57).
Production changes are reminders.py, shortest_ids in status.py, wake_reads.py,
minimal hosts.py wiring, and the optional archive call in inbox_notices.py. No storage
format, command_reads.py, status_refresh.py, transport delivery or exit-code changes.

The private Brain was cloned with macOS `cp -cR` before profiling. Each command
uses its own clone; outward symlinks are removed from the clone, never followed
for writes. Host delivery, background launch and provider sync are mocked. No
live Brain command is profiled and no live instance data is committed here.
The clone's lifecycle file is 2,029,882 bytes. These are local source profiles,
not a release speed-gate result or proof on the other laptop.

| Profile | Before calls | Before seconds | After calls | After seconds |
| --- | ---: | ---: | ---: | ---: |
| Inbox command | 1 | 14.465034 | 1 | 2.219874 |
| lifecycle_read, inbox | 1,700 | 9.929087 cumulative | 1,700 | 0.246198 cumulative |
| lifecycle JSON parses, inbox | 1,700 | 9.090538 cumulative | 6 | 0.033572 cumulative |
| shortest_ids | 1 | 0.697661 cumulative | 1 | 0.001770 cumulative |
| status.py:1902 prefix genexpr | 2,884,902 | 0.586339 cumulative | 0 | 0 |
| Wake read side, twelve queued files | 12 | 59.753166 wall | 12 | 19.655258 wall |
| watch_groups, wake | 12 | 59.494235 cumulative | 1 | 18.128397 cumulative |
| due_items, wake | 134 | 13.177800 cumulative | 13 | 9.912533 cumulative |

`measure.py` reproduces the two profiles. Its wake workload revalidates twelve
synthetic, expired due-question references against the copied real instance,
using the same function as wake_drain. It never sends or consumes a wake. The
baseline returned false for all twelve files (correct retirement). It is not a
complete wake transport benchmark. Profiles are concurrent with inbox, matching
the order's parallel-read requirement; cProfile adds overhead.

Implementation:

- Cache one validated lifecycle generation by absolute path, mtime_ns and size,
  additionally fencing device, inode and ctime. Every lookup retains no-follow,
  single-link and descriptor-path validation. Read snapshots remain detached via
  a process-local pickle of already validated JSON; only request-ID lookup shares
  the decoded value. Writes invalidate the cache.
- Find shortest unique IDs using sorted distinct IDs and their two neighbours,
  preserving duplicate and prefix semantics without all-pairs comparisons.
- Scope due identity observation to a drain. Reuse it only while source, due
  receipt and lifecycle generations hold. Changed or unknown evidence retains
  pending work; shown, route and terminal checks still read afresh per file.

The profiles use the production logic before its ownership-only move into
wake_reads.py; the moved function bodies are AST-identical. No timing rerun was
needed for that move. Inbox wall time fell 84.65%;
wake read-side wall time fell 67.11%. Calls and seconds above are read directly
from retained cProfile statistics. Cumulative rows overlap; do not add them.
The lifecycle still reads after a write: six parses in the real inbox run are
six invalidated generations, rather than 1,700 reparses. The synthetic unchanged
80-question command test requires exactly one parse. Wake returned false for all
twelve expired references before and after. This profile does not establish the
order's full failing-laptop release gate: builder B owns that fixture/gate.

Validation:

- Focused pytest, two workers, three affected files selected with `-k`: 8 tests,
  7 passed and 1 failed in 11.59 s. This was before the final no-op-write fix.
- Two single-test observations of that failing parse-count case took 2.950 s and
  2.844 s; both retained the failure (2 parses rather than 1). No green is claimed
  from these. The final implementation corrects the measured underlying write;
  final assertion proof is assigned to the cloud full suite, not another local
  loop. The extra no-op/type-preservation test is included in that suite.
- A bounded diagnostic observed three no-op reconciliations: pickle bytes changed
  while canonical JSON did not. After the fix all three retain their generation;
  see noop-reconciliation-diagnosis.txt. This is diagnostic proof, not a test pass.
- The first attempted local pytest run was interrupted after 133.63 s before any
  test completed: fixture initialization was copying large profiling scratch
  data from docs. Scratch was moved into the fixture's excluded node_modules
  directory. No timeout was raised, assertion removed or test skipped.
- `git diff --check` passed. No local full suite. Cloud full suite is triggered by
  pushing the identical head to `ci/fix/inbox-speed-20261121`; read its exact-head
  URL/state in the hand-back. Cross-provider review is pending with the coordinator.

Machine: initial admission allowed, 21.2 GB free memory, 105 GB free disk, normal
thermal state. A later observation showed battery power at 51%; a clean checkpoint
was pushed immediately and no wider local affected/full suite was started. Only
bounded diagnostics and command profiles followed. Raw usage and power readings
are retained in usage.txt and power.txt. Latest cached Codex weekly usage: 91%,
below this order's 93% stop line. No paid model/provider calls.

Files changed and evidence:

- lib/brain/reminders.py:37: type-sensitive value comparison prevents false writes
  from pickle alias/dictionary-order differences.
- lib/brain/reminders.py:48: one parsed file generation; detached mutable snapshots,
  unchanged no-follow/link checks, external-generation invalidation.
- lib/brain/reminders.py:129: compare values on a pickle mismatch before publishing;
  invalidate before a real write. lib/brain/reminders.py:342: shared request lookup.
- lib/brain/status.py:1900: sorted-neighbour shortest-ID lookup.
- lib/brain/wake_reads.py:12 and :25: wake_read_scope and wake_current_due_ids
  hold drain-local, source/receipt-fenced read reuse in a separate helper module.
  hosts.py has only the lazy import and two call-site integrations. No edits to
  wake_turns.py, delivery/exit code, or status.py due_*/machine_* functions.
- tests/test_due_lifecycle.py:38, :62, :76, :98 and :107 cover parse counts, snapshots,
  local/external writes, link safety, aborted writes, no-op writes and scalar types.
- tests/test_status.py:2448 compares prefix results to the original reference.
- tests/test_wake_confirmation.py:77, :91 and :112 cover one scan for twelve files,
  a fresh next run, changed source/lifecycle/receipt inputs and unknown inventories.

Rules that apply (PASS is confined to builder scope; remaining release gates
are explicitly FAIL/pending):

| Rule from the order | Pass/fail | Evidence, file:line |
| --- | --- | --- |
| Non-opinionated core; declared policy only, inert by default | PASS | reminders.py:48, status.py:1900 and wake_reads.py:25 are generic read optimizations; no instance rule becomes core policy. |
| R14/R19 provider/model | PASS | Builder runtime is Codex gpt-6.1-sol high; usage.txt:4 records 91%; no delegates launched. |
| R76 no word lists/regex for meaning | PASS | status.py:1900 compares opaque IDs only; reminders.py:37 checks JSON structure/types. No semantic matcher added. |
| R80 system instructions/data separation | PASS | No AI call added or made; wake_reads.py:25 is file-only. |
| R94 fix AI instructions first | PASS | No AI behavior changed; measured decode/scanning causes fixed at reminders.py:48 and wake_reads.py:25. |
| R86 one PR, one review, cloud CI | FAIL/pending | One PR #7 and CI branch; cross-provider review and final cloud outcome remain coordinator gates. See validation above. |
| R73 independent work in parallel | PASS | Before and final inbox/wake profiles launched concurrently; focused run used two workers. measure.py:84 records per-command wall time. |
| R96 declare files before parallel builders | PASS | Builder D's files declared before edits; only this builder used, no children. Scope is recorded above. |
| R87 nothing cut/parked/deferred without OK | PASS | All three assigned code paths implemented. Release/installation belong to coordinator; no assigned functionality removed. |
| R10 clock-time forecasts from live state | PASS | No builder-hour forecast used; initial push occurred at 21:02 Beirut, ahead of the 23:45 deadline. |
| R79 read diff and prove release/install/failing instance | FAIL/pending | Source diff read; retained profiles and tests are local evidence. No merge, tag, upgrade, release, install or other-laptop result claimed. |
| Codex 93% cap | PASS | usage.txt:4 records 91%; clean checkpoint and final pushes within that reading. |
| R32 machine check; cloud full suite | PASS | Machine checked before local affected work; pytest used two workers and no full suite. Battery checkpoint pushed; cloud branch is the full-suite route. |

Dissection of the local red count gate:

1. Ref: regression-output.txt:101 and regression-results.jsonl:1. The 80-question
   command parsed twice. Seven independent regression tests passed; this failure
   was retained rather than hidden by a looser count.
2. Ref: parse-count-recheck.txt:1 and parse-count-diagnosis.txt:1. Limiting the spy
   to the lifecycle module still found two parses of equal-length, equal-content
   lifecycle JSON. The initial unrelated-parser explanation was incorrect.
3. Ref: noop-reconciliation-diagnosis.txt:1 and reminders.py:126. A fresh direct
   diagnostic of three reconciliations established that pickle bytes change on
   equal JSON because of aliases/order. The revised value comparator retains the
   file generation for all three observations.

Cause: bytewise pickle comparison mistook representation changes for state
changes, rewriting lifecycle.json and invalidating its parsed generation.
Items covered: 80 fixture questions; 3 no-op reconciliation observations.
Ranked fixes: (1) type-sensitive structural comparison only after a pickle
mismatch (implemented); (2) canonical JSON comparison (correct but encodes the
full ledger twice); (3) always publish and reparse (retains avoidable work).
Recommendation: 1. Final cloud assertions and cross-provider review remain open.

Decisions made: preserve all receipts and identities; keep mutable read snapshots
detached; after any changed/unknown wake source retain pending work for its next
fresh command. No storage redesign or edits to B/E/batch-1-owned internals.
Proposed lesson: compare JSON state values, including scalar types, rather than
pickle alias representation when deciding whether a receipt ledger changed.

Ownership clarification (coordinator): status.py changes are restricted to
shortest_ids; inbox_items is also authorized but unchanged. All due_* and
machine_* functions remain byte-for-byte unchanged. Wake read reuse lives in
new module lib/brain/wake_reads.py, functions wake_read_scope and
wake_current_due_ids. Batch 1 retains wake_turns.py and its delivery/exit code.
The archive-maintenance seam now uses lifecycle_store(optional=True), as
described below; it returns immediately on contention instead of replanning.

Optional-maintenance API (coordinator follow-up):

- reminders.py:93 adds keyword-only optional=False. With optional=True, ownership
  is attempted using LOCK_EX | LOCK_NB. Contention yields None; no lifecycle read
  or write is attempted, and callers skip their optional operation.
- inbox_notices.py:230 opts the read-triggered archive into this API and returns
  zero immediately on None. This is the only additional production file touched.
- Ordinary writes retain LOCK_EX blocking behavior. Durable retirement after an
  archive has already published source changes remains an ordinary write.
- tests/test_inbox_noise.py:755 adds one regression test using a real held flock.
  It checks immediate skip, unchanged ledger/source, no archive publication, and
  the lock modes for ordinary and optional uncontended writes. Two existing
  archive tests were adapted to the one-attempt skip and new keyword.
- The new test and both adapted tests passed: 3 tests in 5.551 s, one process;
  see optional-maintenance-tests.txt. No wider local suite was run on battery.
  The earlier performance profiles precede this small API addition.

Caller contract for the refresh builder:

```python
with reminders.lifecycle_store(inst, optional=True) as data:
    if data is None:
        return 0  # skip optional maintenance; serve the read
    # Apply the optional maintenance using this owned snapshot.
```
