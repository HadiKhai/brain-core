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


## D501: Consumption-aware wake transport is opt-in

A machine-local `wake_coalesce=on` binding enables one outstanding wake per
coordinator/transport pair. Legacy bindings retain delivery behavior. A separate
turn receipt aggregates opaque identities and fixed cause counts until a visible
`due` consumes it. Queue keys include the binding in this mode, so a new
coordinator does not inherit an old coordinator's consumption state. One reminder
attempt follows thirty minutes without consumption. Root due reports the stuck
coordinator. Invalid or unwritable turn state never supplies suppressing evidence.

The receipt is reserved before transport and consumption fences its completion,
including a coordinator that runs due before transport returns. Consumption merges
complete same-binding arrivals and leaves damaged records available for delivery.
This mechanism cannot determine whether a host is idle or draining its queue.


## D502: Durable wake acceptance is separate from display history

Port the retained outbox's binding-scoped shown-item ledger and terminal receipts.
Only successful frozen snapshots acknowledge shown identities; later arrivals
remain deliverable. The ledger outlives bounded history. Damaged evidence does not
certify delivery and is retained before replacement. A single retry of receipt
publication does not call transport again.

Terminal identities (`machine-exited`, `machine-completed`) outlive route changes
and generic request TTL. Destination drains acquire a nonblocking instance-wide
fence, recheck accepted identities, send and publish the receipt before releasing
it. This prevents competing root/owner routes delivering one terminal event twice.
Physical death or ambiguous remote acceptance before durable publication still
cannot guarantee exactly-once delivery. Producers own durable reason capture.


## D503: Automated brief attribution

Generated worker/delegated, wake and schedule briefs begin with an explicit
automated-turn marker. The common formatter prefixes once; schedule readers also
mark legacy records when exposing their briefs. Roles require cancellation with
no operator message to be attributed to the host, with one retry or a file-based
route. Generic cancellation text alone is not evidence of operator refusal.


## D504: Typed notification channels and bounded routine defaults

`notify` records idempotent, screened notifications before ringing a doorbell.
Root landings, parks and FYIs are inbox-only; typed questions wake their answering
coordinator. Questions require explicit option action categories, recommendation,
absolute deadline and a matching default. Deadline reconciliation records a choice
only when every option is routine. Any outward or destructive option disables
this path. Core never executes an option or grants action authorization.

Question state retains explicit and default answers. Answered typed question inbox
lines are hidden by projection, preserving their original receipt bytes. Arbitrary
external host sends remain outside the native notification API's interception.


## D505: Normalize rendered status telemetry for stale detection

Port the retained rendered-fingerprint implementation. Comparison and publication
share one normalizer with named patterns for token meters, elapsed durations,
restart counters and live snapshot fields. Recorded identities, decision-relevant
numbers, dates, queue ownership, HTTP results and readiness facts remain visible
and fingerprinted. Runtime-switch details and retained delivery results are part
of the render, without running their commands or probes.


The retained fingerprint test for `rehearse=required` predates removal of rehearsal
as a readiness gate. Its current regression asserts that the retired option does
not restore a gate or change status semantics; the fingerprint feature does not
reintroduce that obsolete behavior.


## D506: Visible wake ownership and question-only root bindings

Port the retained due/inbox union to consumption-aware bindings via machine-local
`wake_policy=visible`. Unbound descendants route to their nearest bound ancestor;
its due view includes that source work. Its watcher also includes fresh inbox
rollups and its own stale judgement/advice. Recorded completion ownership follows
the source coordinator in visible mode. Existing bindings retain legacy selection
until configured. New wake setup instructions enable visible/coalescing mode for
coordinators and `wake_policy=questions` for root.

Question-only native root bindings retire positively identified non-question
notifications while retaining opaque/damaged pending requests for recovery. Typed
question flags survive queue serialization; arbitrary host sends remain outside
this boundary. Root can retain landings and parks in inbox/event state without a
transport wake. These settings are machine-local, never automatically installed.


## D507: Native host exports are complete, bounded ownership snapshots

An explicit `host-export` command reuses the shared native RPC bridge. It lists
bound projects, includes subagents, follows bounded pagination, and queries pending
request ids only for bound coordinators/open-job workers. Snapshot files retain
status/run identities and hashed pending ids, never transcripts or question text.
Binding rotation, unknown threads, malformed responses, deadline exhaustion and
partial reads refuse publication and preserve prior bytes. Readers require fresh,
complete exact-binding coverage; unknown host activity stays unknown.

`host_export=on` enables this read-only export on the existing sampler. It does
not install or start a service. Pending questions join due/wake sources; validated
failed worker runs join the existing failure handler without replacing manual
exports. No exporter invents coordinator start times or host queue timing. The
shared RPC module is an integration dependency, not a second client.


Transport item identities use the full 64-character digest, distinct from the
shorter job keys. Question classification flags stay in queue metadata; display
history preserves its identity/kind-only schema. Export RPC waits never hold the
instance writer lock; a separate nonblocking export lock prevents stale concurrent
publications.


## D508: Reject malformed wake identities at the typed boundary

Identity projection accepts only dictionaries with 64-character hexadecimal string
ids and nonempty string kinds. Numeric, boolean, null, collection and malformed
string ids become unknown metadata; they never reach string-only transport code.
The existing opaque-request fallback carries that pending file through transport.
Failed transport preserves original bytes. Successful transport alone retires it.


## D509: Register delegation before relying on host completion delivery

Explicit native `delegate` verifies the calling host context against the bound
parent, persists a stable idempotency key, then calls the shared RPC bridge. Unknown
outcomes keep the intent for explicit same-key retry. Observers query only tasks
owned by their current bound host context. Waiting nested children or pending
child turns never count as completed tasks.

A terminal task/run identity is recorded durably before publishing its completion
inbox/doorbell. Immutable terminal observations are not polled again. File watchers
can recover a doorbell after interrupted publication; root results remain inbox-
only. Registration and observation do not replace host-owned automatic completion
behavior. Without a configured native parent context/observer or reachable host,
universal instantaneous delivery cannot be guaranteed by this layer.


## D510: Invalid wake consumption receipts never suppress transport

Consumption receipts require canonical binding/token identities, nonnegative finite
nonfuture timestamps, known typed items, and distinct item/path identities.
Malformed receipts remain byte-for-byte in place and supply no coalescing or
consumption authority. Valid receipts for a retired binding are archived intact
before a replacement is reserved. Transport falls back to its existing bounded
rate window when a receipt is damaged.

A wall-clock rollback makes a now-future receipt invalid immediately. The separate
transport timestamp bounds deferral to one existing rate interval; it does not wait
for the old receipt's wall time. This preserves queued work without mutating the
damaged receipt or inventing a successful consume/acknowledgement.


## D511: Explicitly disabled wake delivery preserves cached refusal evidence

Machine-local `wake_enabled=off` disables enqueue/drain/probe for that route,
including inherited fallback delivery. Capabilities, queued work and cached results
remain intact. Checks show the explicit disabled state and retained delivery result
as evidence without claiming current health. Unspecified/on routes keep their
existing fatal non-2xx checks; an old successful probe cannot negate a refusal.

A pause label or a cached error alone never implies this disabled state. The flag
must describe an explicit operational decision. This separates intentional disabled
delivery from actual refusal while retaining the original evidence. It does not
change host tasks, install anything, probe, or enable a route.


## D512: Dispatch current questions through the native outbox

`dispatch --run` performs bounded file-only routing of typed questions, positively
marked legacy host questions and validated host-question exports. It selects the
nearest coordinator, reuses current watcher identities and writes binding-scoped
acceptance receipts only after the outbox accepts the request. Failed enqueue is
retryable; repeated dispatch neither re-relays results nor creates another ask.
Landings, parks, FYIs and unclassified blocked prose never become new questions.

`dispatcher=on` runs this native dispatcher on the existing sampler. Plain dispatch
remains a read-only plan. No model thread is launched and no host activity is
inferred. Native execution is the implemented mechanism; a separate persistent
model dispatcher thread remains outside this capability. Activation is explicit,
with disabled routes left disabled.


## D513: Compare display baselines by their retained identity/kind schema

In-flight snapshot validation uses the same identity/kind projection as retained
history. A question's internal queue-classification flag does not invalidate a
previous display baseline. This preserves the established history schema while
keeping already-shown questions out of the next `due --since-wake` view, including
when the host starts before transport returns.


## D514: Retain invalid-input warning episodes independently of host export

A malformed manual worker observation remains one warning episode until that
manual source becomes valid. Joining an optional valid host export cannot clear
the manual warning marker. Unrelated inbox delivery and host observations still
proceed while the damaged manual source is retained.
