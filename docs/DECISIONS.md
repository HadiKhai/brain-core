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

## D201: Preserve advisory launch estimates alongside measured job actuals

Launch estimates use merged work with the same declared class (legacy branch
family inference remains a fallback). An optional contract Size section supplies
files and added/deleted lines. The five nearest known sizes use log distances;
without planned size, the estimate uses all same-class samples. Wall time is
class-wide; active time and tokens require the selected runtime. Each metric
requires three observations and shows median plus observed minimum and maximum.

Ordinary resumes retain the original launch estimate. A changed class or runtime
recomputes it. Hand-back captures measured usage and committed Git contribution
size, excluding uncommitted and unrelated rebased changes. Close-time accounting
refreshes known values without replacing missing measurements with zero. Ledger
consumption sanitizes worker actuals to numeric facts and Git identities.

Weekly share is approximate and advisory. Configurable per-runtime conversions
start at 51 million processed tokens per percent for Codex and 68 million for
Claude; fresh-token conversion starts at 1.1 million. Three explicitly attributed
merged-job meter observations refine conversion. Account-wide deltas are never
assumed attributable to one concurrent job. These estimates never affect routing,
admission, readiness or limits.

## D202: Share specialist and ordinary launch forecasting

Specialist hires and worker briefs resolve their route first, then use the same
saved estimate helper and persist the same record. A selected model conditions
samples on that exact model and runtime; changing the model refreshes the
estimate. An unspecified runtime-default model retains class/runtime forecasting
and does not invent a model identity. Sparse model history stays sparse.

## D203: Record readiness wall duration and report slow verification

Each readiness attempt records monotonic wall duration, including preflight and
admission, in its worker record. Ledger consumption retains numeric seconds and
whether the result reused proof. Reused proof records zero new execution time;
it never recharges the historical lane durations. Existing records with no
duration remain unknown. CLI job advice shows the measurement basis and flags
runs above verify_warn_seconds (node or instance; default 600 seconds). This
advice does not change readiness or introduce a timeout.

## D204: Share queue ordering between admission and capacity forecasts

Queued verification uses a stable priority view. A node can opt into landing
priority with verify_priority=landing; other work retains normal priority. After
ten minutes normal work ages ahead of landing requests, and legacy records with
no enqueue timestamp retain precedence. List order breaks ties, preserving
explicit coordinator reordering within a priority band. Running reservations
are never preempted or reordered. Node locks and machine slot limits are unchanged.

Each gate captures its expected duration at enqueue: verify_estimate_seconds,
or the median of at least three measured completed non-reused readiness runs
on that node. Queue and running projections simulate free slots with recorded
start times and gate durations. Missing, overrun or unsafe evidence yields unknown.
The projection reads recorded evidence without process probes or queue writes.
Reported estimates describe capacity only; environmental holds are not predicted.
