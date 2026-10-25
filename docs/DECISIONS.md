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

## D401: Git-owned landing and compatible batch advice

A clean main checkout is fast-forwarded by Git with autostash and ignored-file
replacement disabled. An unoccupied main uses one compare-and-swap ref update.
Publication never rolls back a checkout or ref. Closure requires an explicit
successful publication receipt; ambiguous results retain the journal for repair.
Host settlement remains pending until separately supplied, validated evidence.

A completed job with valid recorded-base evidence and a clean merge may receive
batch-eligibility advice after main advances. This advisory never marks it ready
against current main: the integration result still requires verification. Ordinary
readiness, conflict refusals and worker-owned packets remain unchanged. Display
advice validates recorded lane evidence without executing lane-map commands.

## D402: Focused execution is separate from readiness

`spec-run` accepts an explicit argument vector and runs it in a registered job
worktree using the existing node gate, machine admission and scrubbed logging.
It preserves the command's exit status and records a private focused receipt.
It never writes a ready packet or upgrades focused results to full proof.

## D403: Train configuration excludes thread routing

Train publication fingerprints semantic node configuration and local worktree
bindings. Worker/coordinator thread identities and coordinator session bookkeeping
are routing metadata, so rebinding them does not invalidate verified integration.
Worktree, verify, policy and other configuration changes still refuse publication.

## D404: Lane tool identity follows executable resolution

Lane context fingerprints resolved executable locations and file metadata, project
pins and the selected environment. An unused PATH suffix no longer invalidates
proof when all resolved tools remain identical. Recorded PATH is retained for
reproducing the original environment; a changed resolved tool still invalidates it.

## D405: Native direct-full receipts and early hand-back validation

`full-run` retains a native exact-candidate full packet without publishing a
completion. Hand-back can adopt its receipt ID after checking refs, command,
inputs, ports, lane context and retained log digests. Stale, failed or missing
receipts refuse rather than silently starting another full gate. Arbitrary
external JSON is not imported as native proof. Completion flags are validated
before creating an attempt or queue request, including incompatible receipt/retry
flags and invalid review counts, lessons or report text.

## D406: Optional parallel selftests retain the canonical suite

`selftest --jobs N` runs the canonical compatibility suite once through the pinned
pytest/xdist test tools, with worker restarts disabled. The default remains the
standard-library unittest path; runtime installation needs no test dependencies.
Parallelism is bounded to 32 workers. Runtime estimates require a measured full
run and are not inferred from a command-line change.

## D407: Declared cheap checks precede full admission

A node may declare `verify_cheap` as its format, lint, type, fitness and naming
check command. Native full and lane gates run it first with ordinary heavy
admission, before taking a full verify slot. Failure preserves its exit status
and prevents the full run. Its configuration is bound into readiness and direct
full receipts. Commands cannot be inferred reliably for arbitrary repositories;
unconfigured nodes retain their existing verifier contract.

## D408: Explicit native queue reordering is node-scoped

`queue --node NODE --move TOKEN --before TOKEN` changes the stored order of two
queued verify reservations under the existing native queue transaction. Missing,
ambiguous, running, non-verify and foreign-node targets refuse before mutation.
Priority bands and aging remain authoritative; manual order breaks ties within a
band. The foreground hand-back `queue run NODE --once` interface is preserved.

## D409: Moved-ref diagnostics follow the integration mode

PR integration uses the cached remote default as both base identities. Its move
is reported once under that remote ref, without inventing a local-main change.
Local integration continues to report independently moved local and remote refs
with their old and new commits.

## D410: Per-worktree environment overrides stay machine-local

`wt env` stores validated per-job additions and explicit unsets in local worktree
metadata, without editing shared env files or printing values. Native gate and
focused execution apply them before exporting reserved context. Control, Git
configuration and Python import variables cannot be overridden. Override changes
participate in readiness/attempt input identity, so old proof cannot be reused.

## D411: Rehearsal launched from a core worktree retains that build

An explicit core-worktree launcher keeps its selected implementation for rehearsal
and prints the choice before running. It cannot silently re-execute the installed
instance core. Ordinary launchers keep existing instance routing, and the explicit
`BRAIN_NO_REEXEC` override remains supported.
