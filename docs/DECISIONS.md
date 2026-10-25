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

## D101: Configurable required separation reasons

The shared brief reason resolver accepts a node-local `separation_policy` of
`warn` (the compatible default) or `required`. Required mode refuses a missing
reason before brief output or route recording. Explicit reasons, contract reasons
and independent review defaults satisfy it; an explicit urgent waiver remains
available. Invalid policy values fail closed. This controls brief generation;
it does not infer a safe agent cap or cancel overlapping work.

## D102: Nested build-order authority stays explicit

Only unchecked Markdown list tasks inside a Build order section are suggested.
Bullets and ordered list markers may nest; completed, parked, operator-waiting or
already-open parent entries suppress their descendants. Section heading depth
defines its boundary. Comments and fenced examples remain excluded. Backtick code
spans preserve exact branch names including legal commas and semicolons; plain
mentions end at whitespace or Markdown brackets, with a terminal period ignored.
Use code spans for branch names next to ambiguous prose punctuation. Existing
capacity arithmetic and unreadable-ledger behavior remain unchanged.

## D103: Coordinator pressure requires attributable queue evidence

Root receives queue pressure advisories above three items or ten minutes of
oldest-item wait. Native pending wake identities and file timestamps establish
local queue pressure. Host busy alerts additionally require a fresh, bound-thread
snapshot with an actual run start and queued wakes. Missing host facts remain
unknown. The consumer never infers a start from an inventory update timestamp.
Episode identities exclude display ages and reuse the ordinary watcher receipt.

## D104: Score outcomes only from complete, verified observation receipts

Scorecards evaluate explicit observation windows against stable numeric targets
and required case/runtime coverage. A zero passes only with hash-matching source
receipts and collector-attested full-window coverage. Missing or changed evidence
stays unknown. The command automates scoring, not incident discovery or proof of
the collector's completeness. Overall success requires every metric to pass.

## D105: Diagnostic permits require canonical source receipts

A diagnostic episode binds an open job generation, actual worktree HEAD and
question. Source references are checked against tracked regular Git blobs and
line bounds. Callers explicitly classify a finding as source-readable, ambiguous
or runtime-dependent and state their answer or evidence gap. Source-readable
findings cannot admit probes. Probe setup permits are idempotent while pending;
two recorded setup failures refuse a third. Receipts remain private and immutable
across a refusal. This is a supported command policy, not a shell interceptor or
an inferred semantic classifier.

## D106: Causal progress and communication metrics use typed observations

Causal views join an exact ledger job generation to its contract outcome,
observed phase transitions and declared dependency keys. Controlled reason codes
supply owner roles, clearing conditions and next gates; absent evidence remains
unknown. Communication metrics measure native queue pressure, recorded merges,
joined question episodes and explicitly classified turns. Stable references
deduplicate turns and questions. Reconstructed or future records cannot establish
measured phases or elapsed times. Counts retain their recorded-evidence scope.

## D107: Recurring review classes come from independent structured receipts

The recurrence detector consumes explicit review receipt classifications, scoped
to the exact job generation. Only major/blocker classes repeated across distinct
reviewer-thread/head rounds qualify. Duplicate receipts and reused contexts do
not create rounds; spelling normalization never interprets finding prose. Advice
is historical and informational, with receipt evidence. It does not reopen or
close findings, validate host independence or replace the review gate.

## D108: Permission-repeat advice must match an explicit authority observation

Detect requests repeated after an explicit authorization observation only when
job generation, head, phase, action and authorization reference all match. A
revocation or phase exit invalidates the comparison. This is informational audit
advice, not an authorization engine; no prose inference can grant permission.

## D109: Acknowledgement waits need explicit delivery and obligation facts

Advise only when the current observed phase is waiting for a named delivery
receipt whose delivered handoff explicitly required no acknowledgement. A later
acknowledgement requirement or progress clears the comparison. Never infer
receipt obligations from silence or treat a formatted message as delivered.

## D110: Rerun advice compares complete successful-run identities

Compare observed successful full runs only when head, integration base and input
fingerprint match, with distinct stable run references. Failed, explicitly forced
or incompletely fingerprinted runs reset the comparison. This informational exact-
head detector never skips verification or expands the native proof reuse policy.

## D111: Infrastructure displacement compares observed phase intervals

Compare only explicitly classified execution/working phase intervals following a
named product request. Unknown classification is excluded; no task prose is
classified. Report infrastructure time exceeding product time before recorded
outcome delivery, retaining interval receipts and the elapsed-phase scope. A
matching delivery fact closes the comparison. This is informational, not a gate
or a claim about CPU use or the necessity of setup.

## D112: Preview usability receipts bind to the actual branch head

After more than two distinct completed check runs, compare their head to the
actual branch tip and its latest supplied usable/unusable/exempt preview receipt.
A saved URL or old-head receipt cannot establish usability. Unknown branch tips
remain unknown. Receipt collectors own actual rendering and data checks; this
projection never substitutes a browser claim or launches an environment.

## D113: Product-test delay timers anchor to an observed request episode

Measure 60 minutes from the first observed request for each named product outcome.
Duplicate request exports cannot reset the clock. An observed product-test start
or matching outcome delivery clears it. Absence of a start is reported as missing
recorded evidence, never proof that an unexported external test did not run.

## D114: Closure state and source containment retain proof boundaries

A ledger attempt ending without a recorded merge is displayed as
`closed_without_delivery`, never as done. A recorded merge remains explicitly a
ledger observation. Scorecard receipt sources reject hard links as well as
symbolic links, so a locally named receipt cannot silently refer to another
shared file identity. Neither rule upgrades source assertions into independent
live or delivery proof.

## D115: Observed job evidence stays inside its temporal episode

Current causal views and fact advisories exclude observations before the exact
ledger generation's recorded start. Review recurrence also excludes future and
pre-generation receipts using the snapshot clock. Unknown or reconstructed
history cannot establish measured current progress. Equal-time facts retain
journal order rather than being reordered by opaque identities.

## D116: Queue metrics consume the transport owner's coalesced-turn projection

When installed, the transport's same-binding wake_turn_queue hook supplies the
union of pending items and an unconsumed coalesced turn, preserving its first
enqueue time. The consumer never reads or mutates transport state itself. An
unknown projection stays unknown; the legacy pending-only fallback is labelled
as such while the hook is absent. Queue timestamps never establish host run start.
