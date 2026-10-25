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

## D301: Private write journal, recovery and metadata projections

Instance CLI writers append a durable begin before changing node state, followed
by a commit or abort under the instance lock. A partial failure leaves a pending
operation; a locked status refresh records the mismatch and reconciles the current
snapshot. This is write-ahead detection and explicit reconciliation, not rollback
across files or Git. Journals rotate monthly and remain under ignored `.brain/`.
Stop hooks append only an invalidation to a durable outbox and never acquire the
instance lock. The next writer folds those invalidations using current hashes.

Recorded status contains generated metadata, never worker summaries, questions,
lesson bodies or acknowledgement reasons. Display screening preserves opaque
identities, hashes and schema keys. Damaged records are quarantined privately;
linked journals refuse before repair or mutation. Disposable replay indexes bound
ordinary work to newly appended bytes.

## D302: Compare before selecting an event projection

Files remain the default source. Event-only status replays committed generated
projections without reading node content; it does not reconstruct typed domain
facts. Every refresh records an independent resource-hash comparison. The explicit
source switch requires a full comparison day and no unexplained mismatch or
interrupted operation, including every node for an instance-wide switch.
Existing snapshots remain available, and recovery records are labelled as snapshot
reconciliation rather than historical events.

## D303: Maintain status without a bookkeeping turn

Successful instance writes refresh affected nodes' generated facts and Active work.
The judgement timestamp is retained. Now sections retain thirty nonempty lines;
overflow moves to a dated status archive. Generated status commits coalesce to an
hour, or a job close, and exclude pre-existing judgement or archive edits. A failed
optional refresh warns and invalidates the projection while preserving the primary
write's terminal event. A dated backlog command stores prose in the node file and
only its resource metadata in the event journal.

## D304: Explicit typed facts for delivery measurements

A shared `record_fact` hook appends observed metadata with an exact job key,
absolute timestamp, phase/state and bounded evidence references. Classification
codes are identifiers, with no free-form reason text. Reconstructed facts are
labelled separately and never count as measured observations. No phase, preview
usability, authorization or failure classification is inferred from transcripts.

## D305: Event-backed ledger and hand-back materialization

Ledger and worker hand-back writers persist immutable private source objects, then
append a record event with the object's digest before materializing the conventional
file. Prose and raw receipts remain in the private object store, while the journal
contains identities, provenance and fingerprints. An applied acknowledgement closes
the per-file publication window. Pending records retry under the writer lock; a
changed target refuses recovery rather than overwriting independent work.

`events --rebuild-records` restores missing ledger/hand-back files from those event
sources. Existing snapshots are imported once as reconstructed records, retaining
raw bytes and distinguishing that import from observed historical events. This
provides event-derived paperwork without a coordinator transcription turn. Status
still replays committed generated metadata views as described in D302.

Landing facts (`landed`, `job_landed`, `task_landed`) trigger the optional tasks
module's dependency reconciliation with the exact event id and changed node.
Acknowledgements are durable facts. A failed application remains pending for the
next writer, and reconciliation must respect task holds and visibility. This hook
emits no host wake and performs no transcript classification.

## D306: Opt-in native maintenance for upstream source indexes

`upstream_generator=links` selects a standard-library generator over the node's
explicit `upstream_inputs` and `upstream_output`. It writes source links and content
fingerprints, then records freshness only if those inputs remained unchanged.
Successful instance writes maintain opted-in indexes, and `index --refresh` runs
that same generator explicitly. No external script, shell command or host action
is executed. Existing output must carry the native ownership marker; unmarked,
linked, shared or out-of-boundary files are preserved. Other index formats retain
the existing freshness warning and explicit recording workflow.

## D307: Measure the first usable preview from explicit observations

`events --metrics` measures the first evidence-bearing `preview_usable` observation
following `request_received` or `job_requested`, using absolute timestamps at
microsecond precision. A saved URL, publication event or Git commit is not evidence
of usability. Missing request or usability evidence produces an unknown duration.
Reconstructed history is labelled and excluded from measured observations. The
projection is read-only and uses exact job identities.

## D308: Phase time is observed working and waiting time

`phase_changed` explicitly names a phase and working/waiting/done state. Delivery
metrics integrate successive observed transitions until an explicit terminal event
or the query time. Done phases contribute no work or wait duration. Gaps before
the first observed phase remain unattributed; no transcript gap, process lifetime
or Git timestamp becomes invented working time. Reconstructed events stay excluded.

## D309: Rework reasons and dependency delays require explicit facts

`rework_started`/`rework_finished` measure episodes classified by the controlled
reason codes `review_finding` or `requirement_change`; missing classifications stay
unclassified. `dependency_declared`/`dependencies_changed` name exact dependency
identities, while `dependency_blocked`/`dependency_unblocked` measure observed waits.
The dependency with the largest recorded wait is reported with that explicit basis.
Declared dependencies without wait events do not invent delays. An observed landing
removes its identity from current blockers. These measurements do not predict a
future critical path; overlapping dependency waits must not be added as wall time.

## D310: Native short writers use the same event boundary as CLI writers

Direct inbox, job-attempt and foreground-queue writes enter a short journal scope
under the existing instance lock. This covers dispatcher and watcher paths that
intentionally bypass the CLI's long-lived writer scope. Calls already inside a CLI writer retain that operation identity while reacquiring
the short lock when necessary. In-flight parent writes are not mistaken for missing
external events. Applied record events also update resource coverage directly, so
native ledger publication cannot look like an unjournalled edit.
Stop-hook inbox writes retain their lock-free invalidation outbox protocol.

## D311: One canonical rendering defines generated facts and freshness

Generated refreshes and stale-status fingerprints use the same metadata rendering.
Sensitive summaries and reasons remain in source records without creating a
permanent stale-status loop. A lesson is labelled recorded only when an outcome
actually exists; required-but-missing and legacy-not-required states remain distinct.
Record identities and cached record entries validate before recovery. Immutable
source objects and their directory entry are fsynced before the journal references
those objects.

## D312: Native attempt transitions supply timing observations

A changed native attempt state emits a phase fact: queued is waiting for admission,
verifying is working in execution, and terminal is waiting for coordination. Exact
job identity and available head/base/input fingerprints accompany the observation.
Repeated writes of the same state emit no duplicate transition. This records native
runner states, not model activity or an inferred result. Actual successful full
verification remains a separate evidence-bearing fact from the verifier.

## D313: Lifecycle RPC runs outside the outer CLI writer lock

The lifecycle dispatcher bypasses the CLI's instance lock and event Command scope,
matching native dispatch and watch. Its RPC cannot hold that outer lock while
waiting on the host. Lifecycle publication retains its receipt locks and short
instance locks; ledger writes retain the event-backed write_jobs source hook.
