# Code decisions

## D001: Node-local full gates and native machine capacity

Full-gate exclusion is keyed by the resolved node id within an instance. The
stable lock is `.brain/gates/<sha256-node-id>.lock`; display labels do not define
identity. `gate-lock --node <node>` selects a node explicitly. Without that option,
node folders, registered homes, workbenches and worktrees are ranked together by
the most specific containing path. Repeated anchors for one node are deduplicated;
equal-depth matches for different nodes and unbound context refuse. A containing
node folder cannot mask a deeper registered home or workbench.

Native monolithic verification and lane verification acquire the node lock before
joining machine admission. A lane run retains the node lock across every command,
while each command joins the native FIFO queue afresh. Different nodes can run
concurrently without same-node waiters consuming machine slots. Capacity remains
in the existing machine policy: configurable `verify_day` and `verify_night`, with
defaults of two daytime slots and one nighttime slot. A gate wrapper does not
reserve an additional machine slot.

Nested commands retain the same node's descriptor through exec and explicit
`pass_fds`. Inherited proof binds the node id, stable path, regular-file inode and
held open-file-description lock; incomplete, closed, stale, unlocked or mismatched
proof refuses. Owners close their descriptors without explicitly unlocking, so a
surviving verification child retains exclusion. Lock files are never replaced or
unlinked during normal operation. Lock-path safety checks reject linked and
nonregular targets without interfering with scoped verification-log cleanup.

Regression tests use isolated instances and controlled process barriers to prove
same-node exclusion, different-node overlap, configured capacities, inherited
ownership and cleanup, without generating system load.

Isolated scenario fixtures temporarily remove only enclosing gate environment
proof from both their subprocess environment and in-process environment, restoring
it during cleanup. They retain the enclosing descriptor and lock. Runtime
cross-node proof refusal remains unchanged. An in-repository regression runs
existing subprocess and native lane fixtures under a real gate wrapper, then
checks that enclosing proof is restored and its descriptor still owns the lock.

## D002: Select the initial job runtime before the first ledger append

Resolve a new worktree job's runtime with the existing `worker_route` resolver
before `job_started` appends its first ledger row. The initial row then reflects
the creation-time builder route, including existing runtime pins, class defaults
and inherited model selection.

A later brief's explicit `--runtime` or `--model` choice remains a later route
update through `record_job_route`. It does not change the original committed
ledger revision. Reusing the resolver preserves runtime/model precedence and
model-aware subscription selection; this change adds no flags or routing semantics.

The regression reads the first committed ledger revision before generating a
brief, then checks that the brief retains the expected runtime and model. Its
cases cover class defaults, a node runtime pin, node and instance class routing,
and inherited model configuration without activating inherited runtime policy.

## D1101: Inspect current work and batch dashboard repository evidence

The dashboard retains metadata for every recorded worktree. Git status, socket,
readiness and file-list inspection apply to worktrees with an open ledger job or
an active status-table row. Closed worktrees expose unknown activity values;
they are never presented as clean or ready from an omitted inspection. Reopening
a job or changing the active-work table invalidates repository discovery.

Due evidence batches branch tips and merged membership in one local ref snapshot
per repository and integration mode. It retains closed and unrecorded worktrees
for reconciliation. Dashboard refreshes may reuse this evidence with the same
file/ref metadata keys as their readiness display cache. Ref changes, worktree
existence, bindings and ledger inputs invalidate it. The cache is context-local;
explicit lifecycle and readiness actions read current evidence outside it.

Tests cover a large closed-worktree inventory, active-row recovery, reopened
jobs, batched ref reads, invalidation and an explicit action after cached display.

## D1102: Bind preview tours to source and artifact evidence

`preview <node> --record <receipt.json>` validates an externally captured preview
tour before replacing its machine-local receipt. Evidence names the configured
preview address and tour, a bound source worktree or home at a clean HEAD, an
explicit capture time, and SHA-256 digests for the tour and its linked PNG
screenshots. Early worktree milestones can be recorded before a main landing.

The read-only check and due queue reject missing, stale or malformed evidence.
A bare URL does not discharge the screenshot-tour need. Failed recording retains
the prior receipt; changing source, address, tour binding or artifact bytes makes
it stale. The receipt validates supplied capture evidence, not visual quality or
the accuracy of application data. Those remain the browser review's responsibility.
No network request, browser launch or source change occurs during validation.

## D1103: Separate round budget meters from lifetime job accounting

Open-job payloads retain lifetime `usage` and add optional `round_usage` from the
accounting backend. Budget token/time and compaction badges use replacement-round
counters when present. Missing counters remain unknown; invalid receipts show an
unavailable meter instead of charging the previous round or displaying zero.
Older accounting backends retain their existing lifetime behavior.

Usage receipt metadata invalidates only the affected job node's display cache.
Preview receipt and external tour/screenshot metadata also participate in node
cache keys. These readers never publish lifecycle or accounting state.

The Jobs summary consumes the accounting service's daily throughput rows, including
zero-merge days and measurement coverage. The existing project picker scopes those
rows. The summary opens the table in the existing reader, keeping one main list
on the Jobs page, and cannot shrink below its contents. The duration chart and
lifetime totals remain visible in their existing locations.
