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

## D1301: Node-local tasks with explicit dependencies and completion evidence

Store one Markdown file per task under a node's `tasks/`, using versioned,
JSON-valued frontmatter and a free-text description. Requirements have separate
`requirements/REQ-*.md` records preserving their exact quote, date and source.
Task decision references distinguish shared repository entries (`repo:D123`)
from private entries (`private:D-123`); neither reference copies decision text.

Task mutations validate the complete node dependency graph before writing.
Cycles, missing dependencies, unsupported schemas and linked storage refuse.
Active work requires an exact live ledger job; completion requires evidence,
a code path and a commit already on local main. Parked tasks require a reason
and resume condition and continue blocking dependents; dropping a task does not
silently satisfy dependencies. Provider-owned tasks retain external ownership
of their product fields; internal work can still use local records.

## D1302: Task-aware jobs preserve explicit opt-in and landing proof

Nodes with task records require a ready, unblocked task before creating a job
worktree. Nodes without records keep their existing job interface, and import
studies remain compatible. The ledger retains the task ID, while the task stores
the stable job key and branch. Identified failed rounds count once; two failures
park the task with an explicit resume condition.

Closing a job updates its task only from a successful native verification packet
whose head is already on main, retaining the changed code paths. Other closures
park the task rather than claiming completion. File-only due items surface ready,
unblocked, parked and inactive tasks without changing their state.
