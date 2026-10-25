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

## D901: Local semantic recall is optional and fails back to lexical recall

An optional, short-lived ONNX worker uses the pinned multilingual-e5-small int8
model with query/passage prefixes, masked mean pooling and normalized cosine
ranking. No daemon, API key, remote inference or torch dependency is required.
The core remains Python 3.9 standard library only. Optional runtime packages
are detected without importing the model. Unknown or less than 2 GB available
RAM, missing packages, offline operation and any inference/index failure retain
lexical retrieval. `brain check` reports the current mode; install and upgrade
already run that check. `brain recall --setup` prepares the model explicitly.

Artifacts download lazily with pinned revision, lengths and SHA256 integrity;
tests prohibit downloads. Ordinary recall bounds optional work to 12 seconds;
explicit setup allows 180 seconds. Model setup has no outward publication.
The private `.brain/semantic/` cache binds vectors to model and content hashes.
Eligibility (scope, state, source content) is checked before embedding and every
cache read, including deletion/revocation. Proposals remain inactive. There is
no online provider enabled. The index covers usable library items and the target node's own knowledge,
decision notes and report indexes. External transcript archives are not indexed.

## D902: Proposal briefs summarize evidence without activating it

`proposals --brief` presents each pending claim, citation, source, scope and
individual supported decision commands. `--json` exposes the same bounded sheets.
The existing full view remains the source for complete evidence. Holder and
source screening applies to every sheet, including commands and paths. Brief
rendering is read-only and refuses combination with activation. Specialist/rule
rejections remain inactive source decisions where no rejection CLI exists.

## D903: Recurrence receipts consume evidence, not direction authority

Synthesis is due after three fresh corrections to one principle or five fresh
unclassified corrections. Exact occurrence hashes preserve duplicate records
and stable identities across reordering. An explicit `due --synthesis-seen` receipt
consumes only the current set, refusing stale/wrong-node sets, without rewriting
lessons, provenance, scope or direction. Reads remain repeatable until handled.
Distillation proposes an evidence-backed diff and requires an explicit decision
before applying it. Corrupt receipts, lessons and source policy fail closed with
repair notices. Inactive or out-of-scope specialist lessons never participate.

## D904: Enforcement uses declared rules and scoped correction evidence

Stable rule ids in direction Principles and role/standard Standards, Checklist
or Never sections carry `enforced_by` paths or `wish`. Paths are relative to the
node's bound home and cannot escape it. Check reports missing declared files;
facts and dashboard JSON expose wish counts and percentages. Two scoped wish
corrections emit a stable check-work notice. Rule declarations and correction
snapshots are preserved separately; installing a declared check clears that wish.

Task records are produced only during lesson capture or explicit
`enforcement --sync-tasks`, using the task domain's idempotent source API.
Read-only due, facts and dashboard calls never create tasks. Source identities
are stable per target/principle so observations preserve parked/dropped progress.
Inactive, foreign-scope and malformed evidence cannot create work. Free-form
legacy prose is not guessed into new rule ids or silently activated.

## D905: Feedback closes through executed mapped proof

Node-local feedback records have stable ids and link a correction to its
principle, job, check and before/after evidence. Read-only list/due/facts/UI paths
never change the ledger. Add and map retain open state; a mapping change clears
old proof. Close executes the mapped relative check inside its registered job
worktree (or the ledger job's home), with a 30-second budget. Python and shell
checks use explicit interpreters; no shell command string is evaluated.

Only exit zero with unchanged check bytes and Git HEAD records closure. Proof
contains check SHA256, HEAD, job, check and timestamp. Missing evidence, a failed
check, timeout or concurrent source change leaves feedback open. Corrupt stores
are retained with a repair notice. This proof records the executed check result,
not the semantic adequacy of the check or an authenticated reviewer identity.

## D906: Native teaching applies an adaptation through the target harness

`teach` prepares a scope-checked brief, or applies an explicitly supplied native
adaptation with `--file --to --check`. External homes require an existing
registered worktree and matching branch. The target file stays inside that home;
linked files, repository metadata and generated outputs refuse. Existing files
require an exact expected content hash. Boundary crossings require every target
owner's explicit consent even when the item scope permits use.

The target's own check must pass without changing the adaptation or source
snapshot. Failure restores only this operation's unchanged copy; unexpected
concurrent edits are retained. Private machine receipts bind source and native
hashes to executed proof without putting source provenance into the native copy.
The copy remains uncommitted for the target's normal change process; teaching
neither propagates future updates nor publishes or accepts knowledge proposals.
