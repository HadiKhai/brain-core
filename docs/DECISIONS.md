# Code decisions

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

## D003: Project restart limits as protected coordinator work

Usage limits produce a protected `job-restart` due item for the job's owning
coordinator: at least three compactions or at least twice a known positive token
limit, measured from the existing successful rebrief baseline. Each counter can
trigger independently; missing evidence remains unknown. An open episode stays
open through missing telemetry or a changed limit until a successful rebrief or
terminal job evidence resolves it. Terminal handbacks, locally merged and closed
jobs do not acquire restart work. A manual compaction-restart rebrief also sets
the usage baseline when no usage scan preceded it.

The existing episode receipt keeps due and watcher identities stable as counts
change. Restart work blocks `due --quiet`, cannot be acknowledged or deferred,
and belongs to the decision wake kinds, so it does not depend on informational
usage advice. The coordinator executes the restart through the existing brief
command; reading due never stops or launches a worker.

Coordinator rotation uses the selected current runtime session's measured
context with a default threshold of 300000 tokens. Explicit token and turn
settings retain precedence, and unavailable session evidence creates no invented
usage or rotation event. Tests cover exact boundaries, independent and unknown
counters, terminal jobs, episode renewal and the quiet-due control path.

## D601: Adopt an existing linked checkout without changing its work

`jobs --start --branch --thread` registers an existing linked Git checkout and
an explicit worker reference. The home checkout, absent or ambiguous branches,
coordinator identities and conflicting historical/active bindings are refused.
An exact repeated adoption preserves the original ledger identity. The initial
planned runtime uses the same route resolver as worktree creation.

Adoption preserves HEAD and dirty files, assigns bookkeeping ports and never
runs setup or launches a thread. Host activity remains unknown. It creates no
terminal handback; the adopted worker uses the normal contract, brief and
handback commands as work continues. Tests use an existing dirty checkout and
cover registration, idempotence, conflicting workers and protected checkouts.

## D602: Use the native host tool bridge with explicit outcomes

The host RPC client calls the app's existing ACP bridge using argument arrays,
finite JSON and a bounded subprocess. It decodes structured and text JSON tool
results, rejects unavailable transport and reports ambiguous delivery failures
without retrying mutations. Provider output is never echoed into error messages.

Thread organization requires one explicit singular target and rejects unknown
argument names before transport. This protects calls made by this client; it
does not change the external host parser or other callers. Tests mock the bridge
and cover argument construction, decoding, missing transport, malformed data,
rejected calls and timeout behavior without contacting a live host.

## D603: Guard messages only for positively bound workers and coordinators

The worker-message guard allows start, done, blocked and question messages to
coordinators. It rejects other message kinds only when the host supplied an
explicit caller identity, that identity has exactly one recognized ledger-job
binding, and the target is a recorded coordinator. Coordinator, peer, unmapped
and ambiguous callers are not reclassified as workers.

The helper supplies the native PreToolUse adapter with a denial reason; it does
not install hooks or intercept tools outside that adapter. Runtime session IDs
must be bound truthfully by the host to receive enforcement. Tests cover all
four kinds, progress chatter, linked references and unknown/protected callers.

## D604: Confirm native host settlement before publishing a settled ledger job

The explicit lifecycle settlement command enumerates every bound host project
with nested threads and checks complete, stable pagination. Recorded worker
references, descendant edges and unquoted first-prompt job markers establish
ownership. Coordinators, callers, open jobs, pending requests and unknown host
activity remain protected. Unknown project threads prevent a false exhaustive
job attestation even when known terminal workers can be safely settled.

Each singular settlement call receives a fresh local-ownership and host-state
check. A second exhaustive listing must confirm settled terminal threads before
the existing attestation validator publishes the job receipt and ledger stamp.
Unexpected self-settlement is immediately undone and reported. Missing transport
or ambiguous outcomes stay pending; they never become a successful stamp.

The train finish API accepts exact closed job keys and returns separate settled,
pending and error collections. It cannot sweep unrelated jobs. Tests exercise
nested workers, live/pending children, changed ownership, incomplete inventory,
unattributed threads, repeated execution and exact accepted-job scoping using a
fake native host. These tests do not establish live host integration or atomic
host-side compare-and-set behavior between the last query and settlement.

## D605: Reconcile exact conversation answers without guessing stale requests

Runtime question markers incorporate a hash of the actual session, tool call
and question identity. A successful matching tool-answer result, or a single
adjacent operator reply selecting an offered option, supplies positive answer
evidence. Free-text guesses, ambiguous multi-question replies, cancellations,
foreign sessions and old call episodes do not close another request.

The stop hook reads a bounded transcript snapshot and queues screened evidence
without waiting for the lifecycle lock. Ordinary inbox/due reads apply it to the
exact current request episode through the existing answer receipt. Evidence is
retained until that transaction is durably visible; a failed write keeps the
question open and the answer available for retry. Historical source questions
and answer provenance remain recorded. Legacy questions without origin evidence
still require an explicit answer receipt.

Tests use both native runtime record shapes and cover chat selections, exact
free-text tool answers, ambiguous or cancelled replies, foreign/old identities,
recurrence and failed publication. No host request is cancelled by reconciliation.

## D606: Advance worker restarts through fenced native maintenance steps

Native lifecycle maintenance advances one recorded restart at a time. It first
enumerates nested host threads, preserves live children and pending questions,
checks exact ledger, workspace and worker ownership, and interrupts only the
observed run with a stable host request key. A later invocation must observe a
terminal old worker before launching the recorded original task in the existing
worktree with an explicit native model selection.

A private launch-intent receipt precedes the non-idempotent host launch. Lost
responses require unique first-prompt marker recovery from exhaustive inventory;
they never permit another launch. Replacement workspace/model observations and
fresh local ownership checks precede binding. Retired worker references remain
available for eventual closed-job settlement. A captured prelaunch usage snapshot
is acknowledged only after replacement binding; startup usage remains charged.
Brief preparation can defer acknowledgement for this path, while ordinary brief
delivery preserves its existing behavior.

The defer guard belongs to the ordinary branch resume brief, including its
manual-compaction acknowledgement, and is exercised through the native brief
generator. Import studies retain their separate delivery behavior.

Tests exercise exact-run interruption, pending/live-child refusal, missing usage,
changed ownership/workspaces, unknown launch outcomes, post-binding publication
failure and replacement startup accounting. Maintenance is an explicit scheduler
entry point; file-only status, due and watcher reads do not query or mutate host
threads. Tests use a fake host. No live scheduling, interruption, launch or
host-side atomic compare-and-set guarantee is established by these tests.

## D607: Accept coordinator rotation from the replacement host context

Automatic rotation first enumerates nested threads and registered delegated
tasks. Live, pending, unmeasured or unmatched children keep their parent; the
adapter has no reparent operation, so rotation waits. Old-parent task records
remain unchanged. Recognized schedule and webhook bindings are inventoried
before a handover and a single durable launch intent. Unknown launch outcomes
require exact first-prompt recovery and never trigger a second launch.

The replacement accepts its own launch ticket using its actual host identity,
observed workspace and runtime session identity. Only that context can rebind
host tasks to itself. Fresh host observations must confirm those bindings before
the local coordinator changes. Every prior coordinator remains in rotation
history. After another child/question check, the replacement can interrupt only
the observed old run; settlement waits for terminal host confirmation. Native
maintenance resumes unfinished retirement even when the fresh session is below
the configured rotation threshold.

Native calls run outside the instance writer lock. Separate action receipts
serialize rotation while short local publication locks protect bindings. Host
schedule changes and local binding publication are recoverable steps, not an
atomic cross-system transaction. Tests use a fake host, cover replacement-side
binding, child/unknown-transport refusal, uncertain launch outcomes, resumable
retirement and writer-lock boundaries. Live host integration and scheduler
installation remain separate validation.

## D608: Require current thread settlement before releasing a closed workspace

The workspace cleanup callback accepts only exact closed ledger identities,
start/end receipts, workspace and recorded thread references. It forces a fresh
exhaustive host settlement sweep even when the ledger already has a historical
settlement stamp. Live children, questions, changed ownership and unknown host
state refuse cleanup. Worker bindings are removed only after confirmation and
remain retained as retired references; a second ownership check prevents
unbinding a concurrently replaced worker.

The callback returns a boolean to the workspace owner. It never removes files,
cancels requests or treats a prior stamp as current host evidence. The workspace
owner remains responsible for its final branch/live-owner check and removal.
Tests cover current settlement, retained live descendants, unknown/open jobs,
unbinding with retained references and ownership changes after a host action.

The accepted-job finish API also forces this fresh validation before reporting
success and unbinding workers. A historical stamp cannot override a currently
live descendant; accepted-job references remain retained after unbinding.

## D609: Launch recorded unbound jobs without resetting their initial allowance

Native maintenance can launch an unbound open job only from its recorded original
brief and existing workspace. An exhaustive bounded host inventory must not find
an unbound conversation already using that checkout; such work requires explicit
adoption. A private launch-intent receipt precedes the host call, and unknown
outcomes require unique first-prompt recovery rather than another launch.

The observed replacement workspace, branch and model must match before binding.
Fresh source ownership is checked immediately before launch and publication.
Initial launch never acknowledges restart usage or resets a lifetime baseline.
Tests cover one launch/binding, unknown existing workspace ownership and lost
launch responses. Host execution and scheduler installation are separate proof.
