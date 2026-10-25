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

## D701: Restart budgets have independent round counters

Lifetime usage remains the accounting source for ledger totals, forecasts and
historical comparisons. A successfully delivered restart brief records a private
baseline for processed tokens, compactions and available active minutes. Ordinary
brief regeneration does not reset that baseline. Manual compaction restart signals
use the same acknowledgement path. Failed brief delivery leaves it unchanged.

Budget and compaction warnings subtract this baseline. Active time budgets use the
measured minutes since restart when available; older receipts fall back to elapsed
time since acknowledgement. Unknown counters remain unknown and counter regression
refuses rather than creating spare allowance. Opening a subsequent restart episode
retains the current round's start. The dashboard consumes a separate `round_usage`
projection while lifetime usage remains available.

## D702: Usage caches track append offsets and permission metadata

Per-transcript aggregates, including exact job time windows, persist their parsed
byte offsets in the private spend cache. File identity, size, nanosecond timestamps,
mode and link count invalidate reused data; replacements, truncations and same-size
rewrites rebuild it. Growing append-only transcripts resume at the saved whole-line
offset. Per-job total caches contain aggregate counts and a hashed input signature,
never transcript text or workspace paths.

Live snapshots share one metadata revision across jobs. That revision includes
allowed and forbidden source roots, transcript metadata, hand-back changes and
job bindings; an unchanged caller revision cannot retain newly appended or forbidden
data. Cached host figures retain precedence. Read-only dashboard requests may use
existing caches and keep progress in memory, but never publish spend files. A later
explicit accounting request may publish that progress. Cache schema version 6
rebuilds older aggregates automatically. Full dashboard latency also depends on
its worktree, readiness and due callers.

## D703: Routed selections are checked against returned adapter receipts

`brain launch-check` compares an explicit declared provider instance, model and
options with actual returned host receipt fields. Provider aliases in declarations
must agree. Options may be objects or unique id/value descriptor arrays; string
and boolean values retain their types. Empty options must be explicit. Missing,
ambiguous or malformed fields return unknown; unequal fields return mismatch.
Both statuses exit nonzero. The checker accepts the host's structured-content
wrapper, bounds file reads and rejects duplicate JSON keys.

The command is read-only, invokes no host, and prints no supplied identifiers or
option values. A match attests only that returned metadata matches the declared
route. It neither enforces provider execution nor proves which model actually ran.

## D704: Model-fit learning fills only opted-in, unpinned defaults

The learner groups recorded jobs by exact class, runtime and effort. It accepts
reviewed merges and explicitly failed outcomes only when usage is measured and
attributed entirely to the recorded model. Open, parked, switched, mixed-model,
unknown and conflicting duplicate observations are excluded. Each model needs at
least three reviewed merges by default. Success rate and median review rounds
rank before median processed tokens and active hours. These recorded outcomes
are quality proxies; the learner does not infer causal model quality or prices.

`brain model-fit` reports the current evidence and recommendation without changing
configuration. With `model_fit=on`, brief routing may use that recommendation only
when no explicit, node, ancestor or instance model setting chose a model. Candidate
models must be declared in `model_fit_models_<runtime>`; no undeclared model or
runtime is introduced. `model_fit_min_samples` controls the minimum observations.
The feature defaults off. New ledger evidence updates recommendations automatically,
while runtime pins, effort selection and subscription/model quota checks retain
precedence. Receipt validation remains a separate adapter boundary.
