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

## D1201: Observe node gate ownership before core replacement

An upgrade inventory observes each node's native gate lock as well as machine
admission and repository train ownership. Admission may be disabled while a gate
still runs. An exclusively held gate refuses upgrade; a retained unlocked gate
file is idle. The observer never reads holder metadata as proof or removes locks.
Linked, nonregular or unreadable gate inventory refuses before core mutation.
This check covers native wrappers; unregistered external processes cannot be
inferred from job labels or elapsed time.

## D1202: Release existing state ownership for slow observations

Slow network lookups, transcript scans and provider refreshes release an already
held state lock, then revalidate their explicit policy, binding and output inputs
before publication. Read-only callers do not acquire a state lock. The observer
reloads local bindings on retake so unrelated concurrent writes survive. Job
accounting fences the original job identity and preserves newer host usage.
Destructive worktree callers prepare accounting before removal and retain the
existing ownership and lifecycle refusal checks.

Machine service operations use an independent bounded reservation after releasing
state. Commit and core-upgrade transactions retain their existing serialization;
this protocol does not provide cross-file crash recovery or Git/core exchange.
Contention tests use explicit ready/release barriers. Positive controls execute
sibling commands while the slow boundary remains paused; negative controls
observe the actual held flock without waiting for a timing threshold.

## D1203: Record schedule providers explicitly

A schedule may declare a shared runtime provider; its machine-local host binding
may override it in a separate provider map. Keep host id strings compatible with
existing readers. Rebinding without a provider or unbinding removes stale local
metadata. Check warns on a mismatch with the resolved node's explicit runtime pin
and applies that node's redaction policy. No provider is inferred from a host id.
