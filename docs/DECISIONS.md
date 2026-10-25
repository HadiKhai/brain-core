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

## D412: Required review and preview receipts gate execution and readiness

The verifier calls the independent review and preview receipt validators before
recording execution and in ordinary, display and train readiness readers. Policy
refusals cannot be bypassed by importing a direct-full receipt. Missing validator
APIs refuse when their policy is required; installations with no corresponding
policy retain existing behavior. Receipt schemas and writers remain owned by the
review and preview modules.

## D413: Rehearsal drift is inconclusive and denied cleanup stops signaling

Rehearsal observes external input identity around both private lanes. A changed
external operand withholds a pass and reports comparison drift rather than
attributing it to candidate code. Independent safety or incomplete-baseline
failures remain failures. Report observations inspect indexes and directory/link
metadata without recursively scanning report assets.

Cleanup records each group signal attempt with PID, process group, signal, UTC
instant and outcome. EPERM is terminal for that probe's signaling: it retains a
leftover with observed members and a plain stop command, persists it immediately
to the node inbox, and prevents passing evidence. The generated child supervisor
never signals from atexit, so it cannot send a later child kill after outer cleanup
was denied. Only the outer supervisor owns group cleanup.

## D414: Landing reconciles observed integration and delegates exact settlement

`land` reconciles completed exact-head jobs already integrated by an external Git
fast-forward, using native readiness and closure checks without rerunning a gate.
It then uses the native batch train. After successful publication, outside the
instance lock, it delegates only the accepted closed job keys to the lifecycle
settlement adapter. Missing adapters, active threads and incomplete confirmations
remain pending independently of the landed SHA. Repeated calls can resume pending
settlement without rebuilding an already-published batch.

Train execution journals retain the exact full command, duration and log digest
for later release validation. No generic product preview lifecycle is inferred.

## D415: Opt-in scheduled green releases use atomic non-forced Git pushes

Explicit `release_auto=on` authorizes the native sampler's two configured daily
release windows (default 09:00 and 21:00). A repository-scoped stable lock and
durable private Git-metadata ledger cap attempts at two per calendar day, across
instances. Prepared and uncertain attempts count toward the cap and fence retries.

The executor requires a published exact-main train, a successful full run with
unchanged command/log digest and measured duration, native independent-review
receipts for every accepted job, and the next numbered CHANGES release section.
It checks origin against the train's recorded cached origin base and verifies
ancestry before an atomic ordinary Git push of the exact SHA and release-N tag.
A temporary pre-push guard checks the remote IDs advertised to Git and then runs
the original executable pre-push hook unchanged. Receive-pack rejects subsequent
ref races. There are no forced refspecs or force options;
an unconfirmed result remains unknown and requires reconciliation. Confirmed
release metadata includes SHA, tag and full-gate time.

## D416: Native verifier ownership includes cleanup and optional typed facts

When the native managed-admission API is available, each command keeps one producer
context across admission, execution and all cleanup actions. Actual subprocess
status reaches the token. Cleanup attempts queue release, phase publication,
owned Docker cleanup and diagnostics while preserving a primary failure.
Configured shared resources use the native lease around execution and cleanup;
a failed command retains its resource claim until cleanup is established.
Unconfigured resource leases perform no registry IO.

Successful actual monolithic full executions can emit typed `verify_completed`
facts bound to job, exact head, base and execution fingerprint. Focused runs and
imported/reused proof emit none; explicit reverify is classified separately.
Telemetry uses the event module's controlled schema and is absent on installations
without that module.
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

## D1303: Task backfill is additive and source-addressed

The backfill importer reads explicit list entries from node-local backlogs,
build orders, inboxes and relevant status sections, plus open ledger jobs.
Stable source fingerprints make replays and source line shifts idempotent.
Every imported task links an exact source quote and location. A missing original
request date is recorded as file observation time with an explicit date basis.
Existing task records and source prose are preserved. Imported prose stays in
backlog until its runnable state and dependencies are explicit; open jobs retain
their exact job identity. External issue references on provider-backed nodes wait
for a provider pull rather than becoming duplicate local product issues.

## D1304: Requirements retain sources and rules activate explicitly

Requirements are immutable source records. Explicit rule creation builds a
current `rules.md` index and preserves superseded rule records. Imported prose
never activates itself, and a manually maintained rules index refuses overwrite.
Install/upgrade runs additive task migration and documents repairs on failure;
read-only commands never silently migrate storage or contact providers.
