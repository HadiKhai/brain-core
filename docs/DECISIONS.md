# Code decisions

- Rehearsal stores timestamp-preserving copies in a private directory beside the workbench, outside captured source roots; every owned allocation requires an explicit parent and refuses physical paths under system temp, macOS aging roots or HOME dot-directories before starting, while native cleanup still retires its bulk data.

## D31005: Reconcile only absent, durably recorded local queue processes

Before releasing a verifier bootstrap to exec, the foreground queue records the
child PID and OS start stamp, verifies it is its direct child, and binds that
inventory to the original owner, boot-scoped host, run/lease ID and lease inode.
The record grants takeover authority for 24 hours from first registration;
expiry only refuses recovery and does not stop normal execution. Legacy
dispatched runs without this evidence retain their existing refusal.

Explicit `queue run --once --request-id ID --recover` serializes on a stable
per-request takeover lock and writes a durable intent before reconciliation.
The original owner and every recorded process must already be absent. A live,
reused or unknown identity refuses, preserving run/attempt evidence and recording
why. A PID/start observation cannot authorize a later numeric-PID signal: this
path obtains no lifetime-bound process handle and sends no process signals.
A matching live process therefore refuses automatic stop even if every recorded
ownership field still matches. No additional observation closes PID reuse.

Only after the recorded processes are already gone, the exact job/attempt/binding
and run/lease remain unchanged, and the original inherited lease can be
exclusively acquired does recovery interrupt the request and attempt. It never
replays verifier work, publishes success, wakes a coordinator or settles a host
thread. Unknown descendants retain uncertainty; unrecorded descendants are not
claimed. Legacy intent or potentially sent-signal evidence requires inspection.
Separate durable writes are not a multi-file transaction or namespace lock.

Takeover audits remain beside requests as `<ID>.takeover.json`. Both ordinary
selection and queue projection enumerate only exact 32-hex request basenames;
audits never enter the request parser. Explicit request validation remains
strict, including rejection of an audit basename. Successful and refused audits
are retained and do not block selection/projection of another queued request.

## D31004: Fence queued terminal publication with the live foreground capability

Final queued publication and invalidation reread the job, attempt and dispatched
request under the instance writer lock. They validate the current host/run and
the original process-owned lease descriptor/inode before terminal mutation. A
False queue terminal result refuses handback, attempt retirement and wake writes.
Queued exceptions retain running reconciliation evidence and never replace or
remove an unknown lease; unqueued attempt cleanup retains its existing behavior.

The queue terminal transition precedes handback/attempt publication under that
lock. These remain separate durable file writes: a later storage failure can
leave partial evidence requiring explicit reconciliation. This does not provide
a multi-file transaction, an external namespace lock, descendant shutdown proof
or a native host settlement capability. Existing exact-publication recovery and
its quiescence/host/binding predicates remain unchanged.

## D40002: Persist directories through checked physical authority

An internal directory alias selects a physical parent inside its instance once.
Durable directory creation holds that parent's component descriptors, opens each
child without following links, and checks identity and physical location before
and after syncing its parent. Existing entries are synced again before deeper
creation, so an interrupted earlier call cannot skip ancestor persistence.
Local settings hold the same checked directory descriptor chain through durable
descent and atomic publication. The initially selected physical name enters
descent without further alias resolution; replacement with an internal alias
is refused. Retargeting only the original alias keeps the selected directory.

Other callers that separately create directories and publish files establish
separate authority lifetimes; this local-settings guarantee does not bridge
their operation boundaries.

Outward aliases, replaced physical ancestors and shared files remain refused.
Each sync failure propagates; no multi-file transaction, namespace lock or
physical power-loss guarantee is implied.

## D40007: Keep unavailable coordinator measurements unknown in rehearsal

Coordinator context and turn counts have no authorized Brain-owned aggregate in
this rehearsal contract. The frozen read adapter returns the native unknown
value for that API and continues to refuse provider discovery. A coordinator
session binding must not turn an otherwise read-only measurement into a provider
scan or strand a leased refresh. The control covers due, UI and native refresh
with a fabricated coordinator session; raw discovery remains refused.


## D40006: Exclude provider installation routes from aggregate-only rehearsal

The machine-local `global` map records runtime installation directories used to
find a Brain from a provider environment. Rehearsal has a private HOME and reads
one attested aggregate capsule; provider installation routes are outside that
input contract. Copy preparation omits this map only from its owned source
copy. It preserves project homes, workbenches, worktrees, privacy policy and the
external observer's link refusal. The original local bindings are never written.


## D40004: Lease preparation writers and prove their exact projections

Locked command preparation starts its native operation before folding hooks or
reconstructing record sources. Event-source refusals inspect coverage without
publishing repairs first. Preparation failures close their own lease and retain
the original error even if terminal publication fails. A copied empty lease
file cannot transfer a kernel lock: private clones discard only their own fresh
copies and preserve all begin records for native reconciliation.

Rehearsal attests each record object by its content address, schema and text hash;
record materialization and hook acknowledgements require matching native writer,
lock and lease evidence. Hook and reconciliation batches share one observed
resource operand, verified again at completion. Reconciliation consumes each deleted-resource tombstone once, even when it closes
several interrupted operations. Archive rollover and status-commit stamps require
exact independently derived bytes and permissions under the live operation.
Historical prefixes, live foreign leases, lifecycle completion and unexplained
residual writes remain strict.

## D40005: Freeze aggregate accounting and stream large history evidence

Both rehearsal cores consume one digest-bound snapshot of existing Brain-owned
accounting aggregates at the frozen instant. Missing aggregates remain unknown.
The snapshot binds immutable job-generation fields and explicit worktree scope;
provider directories, configuration, credentials and transcripts are not copied
or discovered. Machine totals without an authorized aggregate remain explicitly
incomplete, and any unplanned provider accounting discovery refuses. Accounting writers retain their strict counter-generation checks;
usage readers use the fail-soft generation handling from the integration train.

Large journal and comparison operands stream into private owned spool files,
with source identity, stable metadata and whole-file digests checked on capture
and consumption. Comparison history and its native cache are independently
replayed and reconstructed with bounded reads. Unsupported or damaged evidence
still refuses proof; the ordinary capture bound remains 32 MiB. Bulk spools use
the same owned terminal cleanup as the clones.


## D40003: Count nested open callbacks as one audited operation

Python 3.9 pathlib uses an opener for io.open, producing a second os.open audit
callback inside the same operation. Consume that callback only when an identical
path/flags pair was already recorded from its exact parent frame. Consuming the
pair also requires the captured standard-library Path.open code in both frames.
It clears the pair; another operation must produce another record. Built-in open,
direct os.open and repeated pathlib attempts remain individually observed. The
violation marker is created before a pair can be consumed, so write refusal,
unexpected-write outcomes and path permissions do not change.

## D40002: Journal the strict rehearsal fixture's binding changes

The real-core fixture configures its fabricated delivery binding through the
native settings command before freezing its source. Directly rewriting machine
bindings after the last native transaction left an unexplained baseline gap.
An unrelated check repair correctly refused to adopt that gap. Keep that
refusal, complete history/replay checks and all original rehearsal assertions;
fix the fixture's writer instead of granting paths or forgiving residual state.
The delivery server's finalizer also encloses this setup command.

## D40001: Preserve an owned allocation capability across worker death

The outer copy scope starts a separate cleanup guardian and transfers the exact
new allocation over an anonymous socket. A receipt alone never grants cleanup
authority. Normal exit still uses native retention, then dismisses the guardian.
On worker death, recovery requires the captured directory identities, immutable
receipt binding, a dead owner and proven absence of every registered process
group. A live registered group may be stopped only while its leader still has
the witnessed OS start identity. Launch gaps, denied or reused groups, explicit
uncertainty, changed identities and invalid receipts retain bytes. Nested copy
scopes register launches with their enclosing guardian.
Every successful publication transfers a complete receipt witness; recovery
requires exact equality, including mutable metadata, before any filesystem move.

Recovery reuses native checkpoint, staged retirement and publication checks. It
can recover the same captured inode if worker death interrupted staged deletion.
Repeat cancellation signals are deferred during terminal cleanup and handlers
are restored afterward. The existing body exception remains primary, including
retirement publication failure. This is process recovery, not a guarantee after
all supervisors die or storage/ownership evidence becomes unavailable.

Report warnings use logical regular-file bytes strictly above one binary GiB.
Traversal remains bounded, metadata-only and link-refusing; unknown sizes never
become known-small results.

## D31003: Keep the owned-copy body exception primary during exit

When retirement publication fails while a body exception is already unwinding,
OwnedCopies passes that original exception object and traceback into native
finalization. The body exception remains primary, with the retirement failure
as its cause. A further native finalization failure remains secondary in that
cause chain. Without a body exception, the retirement failure remains primary.
Successful exit keeps the existing native cleanup behavior and predicates.

This decision narrows R6-INT-2 to exit/error composition. It changes no ownership,
identity, process, evidence, ancestor uncertainty or deletion predicates. The
stopped original ten-file review remains open; focused proof or a narrow source
review of this contract does not establish whole R6 acceptance.

## D30006: Propagate retention uncertainty before fallible cleanup

Owned copy scopes capture their enclosing in-memory owners on entry. A known
refusal or cleanup failure marks that entire chain uncertain before inspecting,
restoring, publishing or checkpointing retained copies. This state is monotonic;
successful cleanup retains all existing ownership and process predicates.
An already-incomplete child capture marks the captured owner chain before the
first pre-delete checkpoint, including when that checkpoint itself fails.
Staged rollback remains best-effort under the original inode checks. A secondary
rollback or checkpoint error is chained to the primary exception using Python 3.9
exception causes; the primary exception and surviving original bytes remain
observable. No receipt grants deletion authority or later recovery ownership.

## D30002: Native retention owns final bulk deletion

Bulk scopes supply exact allocated-directory identity, synced terminal receipts
and observed process termination. They never independently delete. The native
retention scope encloses setup, commands and early failure, checks child capture
completion and renews ownership after terminal publication before cleanup.
Missing retention results, incomplete capture, changed identity, unknown process
ownership and publication failures retain copies and refuse enclosing cleanup.
The final directory is staged under its private owner until publication succeeds;
a failed publication restores the same inode where ownership still holds.
Bulk history and native diagnostic capsules remain separate. Opt-in evidence
controls and ordinary non-bulk cleanup retain their existing behavior. As with
other filesystem cleanup, concurrent kernel-level replacement or partial delete
failure is not a multi-file transaction or a descendant identity guarantee.

D18402 below records the earlier bulk design; its independent deletion authority
is superseded by this decision. No historical cleanup authority is introduced.

## D28002: Preserve the publication error when temporary cleanup fails

Before replacement, a failed atomic write removes only its captured temporary
inode. If that cleanup also fails, the original publication exception remains
the reported error, with the cleanup error chained as its cause. Failed cleanup
may leave the owned temporary file behind. A post-replacement directory sync
failure still reports the persistence error with the complete new file visible.

## D21003: Hold directory authority through publication and private reads

An accepted folder shortcut supplies a checked physical parent. Publication opens
that parent component by component without following links, holds its directory
descriptors, and uses relative names for creation, replacement and cleanup. A
later alias change cannot select a different publication directory. Descriptor
identity and location checks refuse detected directory moves. These checks do
not provide a namespace lock against arbitrary external renames during a kernel
operation.

`atomic_write(path, text, private=True, create=True)` also supports private caches
and metadata in newly created directories. New private directories use mode 0700
and new files use 0600; replacement preserves an existing regular file's mode.
File flush/fsync precedes replacement and parent fsync follows it. A post-replace
sync error reports failure with the complete new file already visible. This is
per-file persistence ordering, not a multi-file transaction or power-loss proof.

Private journal readers, objects, appenders and leases use held directory
authority too. Lease cleanup holds the original directory and inode; explicit
reconciliation locks dead leases through publication and never removes a
replacement name. Temporary cleanup requires the captured inode and leaves
unknown replacements intact. Final symlink, hardlink and live-owner refusals
remain enforced. Fault-injection fixtures address relative names through the
supplied descriptors while retaining their old/new bytes, modes, staging,
durability and recovery assertions.

## D20003: Preserve automatic adoption sources and deterministic runtime proofs

Install and upgrade task migration retain ledger bytes. Imported task records keep
job keys and source provenance; explicit task backfill may attach ledger Task cells,
including after an earlier automatic import, without creating duplicate records.

Partial spend coverage publishes a genuine bounded parsing slice behind a fixture
release barrier before exercising background completion. It retains incomplete,
file count, rebuild count and read-only assertions without relying on startup speed.
The legacy brief oracle accepts the resolver's extra-path argument and verifies it
is empty in the byte-comparison scenario. Release notices live under dated headings
following the changes document's title and introduction.
## D18402: Retire only bulk copies allocated by the current rehearsal

Native rehearsals and clone-copy preparation allocate a private run directory
with one bulk child. An in-memory capability binds the creator PID, random run
identity and both directory inodes to a synced receipt. It never adopts an
existing report, source, worktree or historical copy. A terminal scope removes
only that bulk child after ownership, containment, link checks, reaped leaders
and absent owned process groups all hold. Missing or changed ownership, denied
signals, live descendants and unknown process state retain the bytes. Nested
uncertainty prevents deletion by the enclosing run. The receipt keeps exit,
identity and signal facts without source contents, argv or exception messages;
normal redacted rehearsal evidence and explicit private diffs remain separate.
The outer main-thread scope turns SIGTERM cancellation into stack unwinding and
restores the prior signal handler afterwards. Uncatchable termination retains
copies; no later run inherits authority to delete them.
Standalone external snapshot callers retain their copies unless enclosed in an
owned scope. There is no historical cleanup command or inferred deletion grant.

Ordinary check warns when a report folder's logical regular-file bytes exceed
1,000,000,000 (decimal GB). It uses the existing per-node workbench report roots,
shares a 10,000-entry/100 ms metadata budget across them, reads no contents and
never follows links. An unreadable, linked or budget-limited size is unknown;
an observed lower bound above the threshold still proves a warning. Warnings
are advisory and have no automatic fix or business mutation.

## D18401: Exercise native persistence contracts in focused fixtures

Atomic replacement and crash fixtures change a recorded input before writing
status, so the candidate bytes must differ from the complete old file. They
retain old-or-new byte checks, file modes and commit identity checks. The durable
event comparison index is validated as JSON separately from temporary debris.

The real-core integration fixture includes its generated evidence in a failing
assertion; unexpected writes remain failures.

Import fixtures distinguish automatically refreshed facts from a manual input
change that still needs refresh, without activating imported content. Schedule
fixtures preserve the automated-turn prefix and multiline body while parsing
the provider separately and keeping reads byte-preserving. Lane coverage's
negative fixture removes all incoming edges to the nonselective module and
checks the transitive closure before asserting that coverage fails closed.

## D18401.1: Localize rehearsal constructor stubs

Rehearsal fixtures replace the constructor binding in the rehearsal module,
leaving the shared instance module and its real Instance type intact. Denied
process cleanup publishes to an independent initialized diagnostic instance;
tests retain inbox content and native journal evidence alongside every denied
group, member, signal and manual stop assertion.

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
pins and the selected non-PATH environment. An unused PATH suffix leaves this
semantic identity unchanged when all resolved tools remain identical. Readiness
uses the recorded PATH to reproduce the original environment. Execution evidence
separately binds that exact selected environment (D20001); a fresh handback with a
different PATH reruns lanes even when semantic identity remains equal.

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
## D417: Readiness policies and command cleanup share the native execution path

Required review and preview receipts are checked before verification, on every
packet read, and when reusing cached display facts. Durable unresolved or
reopened major findings remain refusals even when a newer receipt omits them.

The verifier uses the managed admission token through execution and cleanup.
Admission and queue projections use the same stable priority order, including
manual ordering and aging. Setup and command reservations retain distinct
terminal identities. A node gate is acquired before machine admission; every
cleanup runs before terminal success can be published. Numeric command failure
and cancellation retain their original outcome when cleanup also fails.
Configured resource leases span cleanup, and failed executions retain their
claim. Explicit due ticks invoke configured owned-resource cleanup before
reporting leftover diagnostics. Terminal delivery receipts remain durable across
routing changes. Verification lane ownership and nonselective input mappings
must pass the repository coverage checker as executable checks.

## D418: Sampler release checks are optional and setup owns its admission

The native machine sampler invokes an optional release tick after its watcher.
The release extension owns explicit node opt-in, exact proof checks, scheduling
windows and durable attempt caps. Missing extension is compatible; dependency
failures inside an installed extension stay visible. Ordinary worktree creation
requires no heavy admission. Its optional setup command owns managed admission
and records the actual setup result separately from later verification. Its
configured resource lease spans cleanup and retains failed claims. Release
review checks also validate the durable finding union after job closure.

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

## D801: Separate Docker diagnostics from explicitly pinned cleanup

Read-only leftover reporting grants no cleanup authority. A node enables cleanup
with `docker_cleanup_prefixes` (literal namespaces ending in `-verify-*` or
`-job-*`), `docker_cleanup_host` (trusted local Unix endpoint), and
`docker_cleanup_daemon` (the exact Docker daemon ID). The default mode is `stop`.
Conflicting environment selectors, a different effective endpoint, or a changed
daemon ID refuse mutation. Commands pin that endpoint and clear ambient Docker
and Compose selectors; no project compose file is loaded.

A container also needs the exact node/machine `brain.docker-scope` and both
`brain.owner-pid` and `brain.owner-stamp` labels. Only a definitely absent process
qualifies; unknown or recycled identities retain resources. Every connected
project, Testcontainers session, named volume and network peer participates in
ownership checks. Preview/dev/production identities, bind mounts, automatic
removal, incomplete inventories and resources younger than 30 minutes veto work.
Cleanup stops only immutable inspected container IDs, checks all peers immediately
before each mutation, and reports a stopped ID only after confirmation. One
five-second deadline bounds each hook call. Missing Docker/configuration is a
quiet no-op; failure returns diagnostics without replacing a run's primary result.

## D802: Remove only unused, explicitly owned test Docker resources

`docker_cleanup_mode: remove` adds deletion after the stop pass. Container removal
uses exact IDs without force or volume flags. A restart is refused by Docker;
container removal never implicitly removes volumes. Volumes and networks need
the same scope, owner and age proof as containers. Every container, including
stopped and foreign containers, participates in the attachment veto. Network
inspection must show no endpoints. Reinspection must match the captured resource
metadata before deletion; Docker's own in-use refusal remains authoritative.
Named resources must stay inside their owning project namespace; project labels
alone cannot authorize deletion of an externally named shared resource. Owner PIDs
are bounded to the OS process-id range before probing.
Errors keep resources for a later run. No images, bind contents, compose files,
global prune, forced disconnect or host lifecycle APIs are touched.

## D803: Bind machine reservations to an explicit operating-system boot

New reservations retain a boot identity in addition to parent/child process birth
identities. Linux reads the kernel boot UUID; macOS reads `kern.boottime` seconds
and microseconds. A known mismatch reclaims an earlier-boot reservation without
probing its numeric PIDs. Missing or unknown boot readings preserve existing
process identity behavior and never imply death. Legacy queue rows remain valid.
No uptime subtraction, clock-derived estimate, service change or reboot probe is
needed. Queue readers accept the optional field and keep old compatibility.

## D804: Show bounded CPU observations when machine admission is held

Held machine diagnostics collect up to three top CPU observations using one
one-second `ps` command. Only PID, CPU percentage and executable basename are
shown; command arguments and full paths are excluded. Invalid/nonfinite readings
or probe failures leave the diagnostic unknown. This observation explains current
CPU activity without claiming causation or changing admission policy. Unheld
state reads do not run the probe. Percentages above 100 are valid on multicore
systems.

## D805: One durable terminal producer through admission and cleanup

A managed machine run creates its private record before admission and owns the
terminal transition through cleanup. It retains the original cause and one stable
terminal event identity; repeated finalization cannot overwrite an ended run.
Success is provisional until cleanup finishes. Cleanup attempts every action,
records secondary errors and receives the operation's primary exception explicitly;
a handled exception in a caller cannot suppress a new cleanup failure.

A definitely dead owner can recover an abandoned run. Unknown identity is recorded
as uncertainty, never inferred death. A live managed owner's queue-release window
cannot produce a competing recovery cause. Pending terminal records are retained
until the transport's stable receipt confirms actual delivery. Failed enqueue is
retried using the original event identity and reason. Cross-route transport claims
and accepted-delivery receipts belong to the wake transport; producer code does
not send directly. Catchable termination unwinds through the same finalizer and
restores embedding applications' signal handlers.

`machine_run(kind, instance, node, job=None, clock=None)` is the context API;
`machine_wait(..., token=token)` admits the managed token and `token.result` holds
the command result. `machine_cleanup(token, actions, primary=error)` runs cleanup.
The admission-only command uses that context without changing its public behavior.
Other command runners integrate the same context around admission, launch and
cleanup rather than creating a second terminal path.

## D806: Keep worktree cleanup facts separate from host settlement

`worktree_cleanup_result(node, branch)` returns versioned facts: workspace
presence, every recorded job incarnation, thread references and the required
host inspection sequence. It never attests host settlement or thread ownership.
Missing workspaces forbid messaging stale threads. The host owns liveness,
pending-request, coordinator and open-job protection checks.

`remove_worktree(..., thread_hook=callback)` optionally runs that host integration
before deletion. Only an explicit `True` result permits removal; refusal or an
exception retains the checkout. Branch and live ownership checks run again after
the callback. The returned post-removal result contains the original workspace
reference for host inspection without authorizing a stale-thread message.
Default callers preserve their existing checklist and removal behavior.

## D807: Record and bound held admission without guessing command semantics

Queued work records its owner, cause, reading, start time and clearing condition.
The waiting process retains its continuation and resumes on a clear CPU reading.
The private wait producer reports at 15 minutes to the owner and at 45 minutes to
root, once per stable wait; resumed and terminal events have separate identities.
Due integrations can consume `machine_wait_items` and `machine_wait_notices`.

Admission prefers known CPU idle and memory pressure over I/O-inflated load;
unknown CPU data retains the existing sustained-load fallback. Telemetry uses
bounded standard-library probes and preserves thermal recovery evidence. Legacy
sampler recovery remains unchanged when CPU telemetry is unavailable.

After `max_hold_minutes` (default 20), only a runner that explicitly proves it can
enforce reduced parallelism may admit CPU/load-held work at one slot. Unknown
commands fail closed through the terminal finalizer. Memory, disk, thermal and
invalid-policy holds never degrade into unsafe execution. FIFO capacity is never
bypassed. `machine_environment` exports the recorded reduction; the command runner
must enforce its supported command form rather than rewrite arbitrary shell/Make
syntax. Resource-free worktree creation remains light; heavy setup needs a
runner-owned command lifetime, separate from admission-only permission.

## D808: Machine-wide cooperative service leases retain uncertain lifetimes

Canonical resource keys identify shared databases, previews and services across
nodes and instances. A stable per-resource transaction lock protects one durable
claim; stale tokens cannot release a replacement claim. Concurrent creators use
exclusive creation and reopen the stable lock inode without replacing it.
Records contain opaque
resource/scope hashes, process birth identities and boot identity, without raw
resource names, commands or instance paths. Acquisition of several keys rolls back
partial claims if any resource is busy.

Persistent lifetimes are the default: parent death or reboot cannot prove service
cleanup. Successful owners explicitly release after cleanup. Failed service bodies
retain ownership for inspection. Process-scoped leases are an explicit narrower
option; they reclaim only after all recorded process identities are gone or a
known boot changes. A live or unknown child retains the claim after parent loss.
Unknown child registration records uncertainty instead of permitting reclamation.
Leases never stop a service or delete its data.

Existing runners can use `configured_resource_lease(instance, node)`, reading
comma-separated `verify_resources` canonical keys. An absent setting performs no
lease IO. This is cooperative exclusion: every consumer of a shared service must
use the same key and retain its lease through actual cleanup.

## D809: Admit worktree setup after light checkout creation

CLI dispatch no longer gates worktree creation itself. The checkout, port and job
record are created before any heavy setup waits. A node without setup, or an
explicit `--no-setup`, never joins machine admission. Actual setup keeps the
existing resource-admission policy and runs inside the managed terminal context,
so setup failure and admission refusal have durable causes. Setup duration counts
execution separately from admission wait. This does not add arbitrary process-tree
supervision or change heavy-command slot semantics.

## D810: Reuse the native sampler for an optional release tick

After its watcher hook, the native minute sampler lazily invokes
`release.tick_releases(instance)` when the release extension is installed. Only
absence of that extension is ignored; dependency/import failures inside it remain
visible. The extension owns explicit opt-in policy, eligible windows and durable
attempt fencing. The hook adds no timer, daemon, configuration activation or
separate publication mechanism.

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

## D205: Compare whole-job wave forecasts with fresh weekly headroom

The jobs/usage budget view sums complete per-job weekly-share forecasts against
the selected subscription's fresh seven-day reading and recorded stop line. It
shows the median sum, observed-maximum sum, and whether each fits current headroom.
Missing job samples or stale, unknown, expired or future readings keep fit unknown.
The estimate is account-wide and approximate; work already spent is not subtracted.
Five-hour and model-specific capacity fit remain unknown. This read-only advisory
uses cached usage only and cannot change routing or admission.

## D206: Refresh daily throughput from durable measured facts

Every jobs/dashboard snapshot computes the last fourteen calendar days from the
ledger, using elapsed hours for today and a full day for earlier dates. Only
merged jobs with valid non-future end times count. Rates are merges/hour and
calendar minutes/merge; token means use measured merges only, with coverage
counts. Recorded run/reuse counts expose reruns without inferring absent history.

Native admission records monotonic delay in a context-local collector during
readiness. The hand-back accumulates known admission delay across attempts and
ledger consumption retains numeric counts and seconds. Older incomplete delay
history stays unknown. Admission time includes native capacity/environment holds;
other wait causes are unmeasured. The dashboard component displays measured
coverage and does not infer causes from wall time. Rollups refresh on snapshot
reads without a scheduler, host lifecycle action or generated-file writes.

## D1001: Keep governance identifiers out of content screening and worker output

Exact configured principal identifiers in library and specialist scope metadata
are governance values. Screen all other metadata and content, including duplicate
governance keys, with the existing never-name checks. Use the same frontmatter
grammar for parsing and streaming scans. Limit on-disk exemptions to recognized
instance library, role and lesson paths; external copies and ordinary files stay
strict, including Git index blobs and symlinks.

Proposal acceptance, hiring, recall and connection discovery apply this distinction
without changing stored scopes. Worker-facing item output removes governance
lines when those lines contain a forbidden principal. Final briefs remain subject
to strict content checks.

## D1002: Validate decision scope and external fact citations

Dated entries in node decision logs carry an explicit `Applies to:` scope.
`check` warns on missing scope and on external fact claims without a `Source:`
URL or `probe:` command. Explicit fact labels and common vendor/provider policy
phrasing trigger the citation check. This validates evidence metadata only; it
never executes a probe or claims that a cited source proves the statement.

`decision_scope_since` on a node or instance can exempt earlier dated entries.
Without a cutoff, all dated entries are checked. Template examples are ignored.

## D1003: Enforce declared tool patterns with exact single-use allowances

`hook pretool` matches explicit `Forbidden actions` bullets in node instructions,
inheriting only to the nearest ownership boundary. Shell bullets are conservative
textual glob searches over a whole command, including compound commands; `tool:`
patterns match tool identities. This declared subset is not a shell sandbox.

`allow --once` records an exact shell command, resolved working directory and node.
It does not run the command. A locked atomic state update consumes the allowance
before returning permission, with opaque issued/consumed audit events. Policy
changes invalidate allowances. Hook errors fail open, matching existing runtime
hook behavior. Runtime adapter templates register the hook; updating an adapter
file does not install or activate it on a machine.

An optional worker-message validator receives only explicit host session identity
and raw tool arguments; the action engine never guesses a caller's role.

## D1004: Expire explicit operational limits without granting outward consent

A limit may declare one absolute `Expires:` timestamp. Brief generation omits the
limit after that time, without changing its source record. Invalid or duplicate
expiry metadata keeps the limit active and produces a check warning. Narrative
`Lift when:` conditions remain explanatory; core does not guess that they hold.

Publication policy continues to snapshot every original limit, including expired
ones. An operational expiry never grants push, publication or deletion consent.

## D1005: Review independence is a fresh non-builder thread identity

Instances and nodes may select `review_independence=fresh-thread`, with optional
`review_runtime` and `review_model`. Same-runtime and same-model reviewers then
qualify when their actual host identity belongs to a distinct, fresh context that
did not build the change. Missing identities or unknown/reused context refuse.
Cross-runtime routing remains the compatibility default. A legacy same-runtime
exception needs an absolute expiry and an explicit reason; it still requires the
fresh-thread identity receipt before readiness.

`review --receipt-file` records a completed review separately from briefing a new
round. A receipt names the exact job generation and code head, observed builder
and reviewer runtime/model/thread, context independence and verdict. Builder
identity must match recorded worker bindings and the builder route. Recording
requires an observation from the last five minutes; the immutable observation
remains valid for the same code. Later code or a blocked verdict needs new proof.
These are coordinator-supplied host observations, not independent live-host queries.

Thread identities and receipt histories live under machine-local `.brain/`.
The shared ledger stores only the local receipt reference and required-proof flag.
Readiness consumes a pure validator so the gate owner can enforce it before work.

## D1006: Bind preview rendering and source comparisons to code and URLs

A configured preview or explicit `requires_preview_evidence=true` requires an
exact-head receipt. Each presented preview/review URL needs a successful rendering
observation and evidence reference, plus three distinct displayed items compared
with their source values and references. Wrong URLs, absent rendering evidence,
fewer than three distinct matches, future observations and stale heads refuse.

The recording command stores supplied browser observations locally; it does not
pretend to open a browser itself. Readiness uses the pure validator. A screenshot
tour receipt and these source comparisons prove different properties.

## D1007: Preserve finding closure until its code inputs change

Findings have stable IDs, failure classes, severity, explicit tracked input paths
and closure evidence. Machine-local state retains closed findings across later
reviews and unrelated commits. A change to a bound Git object reopens the finding;
closing it again requires evidence against the new inputs. Existing IDs cannot
silently rebind their input scope. Review observations retain their original
verdict while the finding-state projection provides durable closure history.

## D1008: Preserve guard scope through policy changes and linked directories

A fresh explicit allowance can replace an obsolete allowance for the same exact
action after the declared rule set changes. The old authorization is recorded as
invalidated before the new one is issued; an unconsumed current-policy allowance
still cannot be duplicated.

Governance exemptions require real instance paths below the instance root.
Linked intermediate library or specialist directories never grant an exemption
to foreign file contents, even when the final file itself is regular.

## D1009: Apply receipt policies before verification and on packet reads

Readiness calls the independently owned review and preview validators before any
lane mapping or verifier starts, and again when reading normal, display or train
packets. Required policies refuse if their validator API is unavailable. Default
unconfigured nodes keep their existing behavior. A typed configuration reader
treats only a real instance settings dictionary as configuration.

Presented `review_urls` are machine-local, like `preview`; the settings command
accepts a JSON array string and the receipt validator checks every configured URL.
The schema integration uses actual receipt writers and a fabricated v3 hand-back,
with a fake verifier, so it proves admission and read enforcement without running
a real gate or claiming browser observations.

## D1010: Revalidate receipt policy inside the display cache

A display-cache hit rechecks required receipt policy against the cached code head.
Policy-only changes replace only the prior policy reasons and readiness flag;
Git and input facts keep their existing cache identity. Restoring valid evidence
clears a cached policy refusal without rerunning unrelated validation. Cache-private
provenance is held as dictionary attributes, outside serialized result fields.
Policy reasons are privacy-screened before entering cache provenance or output.
Full and train validations continue to bypass the display cache.

## D1011: Screen shared retrieval candidates with typed governance metadata

Lexical ranking and optional local semantic retrieval share the same candidate
iterator. It applies state, scope and source-content checks once, masking only
exact known principal values in governance fields. Body text and unknown values
remain screened. Query ranking does not bypass candidate eligibility.

## D1012: Reconcile readiness with the durable finding union

A passing exact-head identity receipt also requires a readable durable finding
union. Omission preserves earlier findings; open or input-reopened majors and
blockers refuse readiness. Closed findings survive unrelated commits. Relevant
changes, including deletion, require an explicit evidence-backed closure at the
new commit. Finding scope, severity and class cannot be rebound under the same ID.
Persisted records validate their schema and input hashes against their recorded
commit before either readiness reads or receipt writes can trust them. Missing
state after a recorded identity review refuses replacement rather than resetting
its history.

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

## D705: Native restarts acknowledge their captured pre-launch usage

`acknowledge_usage_restart(..., observed_usage=<live_usage dict>)` accepts an
explicit captured snapshot after a replacement has launched and been bound. The
helper never re-reads lifetime counters in this mode, so replacement startup and
work remain charged to its new round. Processed tokens, compactions and active
minutes must all be known nonnegative integers, with positive measured sessions
and host/transcript provenance. Missing, unknown, boolean or regressed counters refuse without changing the existing receipt. The supplied
snapshot is copied and lifetime totals are never mutated.

Omitting the keyword retains the existing post-delivery live-read behavior.
Explicitly passing `None` is an unavailable snapshot and never selects that fallback.
Episode and successful-delivery requirements remain unchanged. The native caller
owns matching the captured snapshot to its exact job/restart operation.

## D1305: External task ownership and durable state writeback

`tasks.source=linear` selects an opt-in stdlib GraphQL adapter; local remains the
default. Linear owns issue identifiers, product text, workflow, priority, assignee
and blocker relations. Pulls preserve internal job, requirement and decision data,
validate complete pagination before writing, and retain mirrors on errors.
Provider completion is displayed as provider evidence, separately from a local
verified code landing. Parked internal work retains its resume condition.

Job state writes persist retry intent before the API request. Requests contain
only issue/state UUIDs, never internal task metadata. Product issue creation
searches the configured team for a matching title before creating an issue.
Credentials come only from the process environment and are never persisted.
Read-only dashboard/due paths perform no provider requests. Wakes and job starts
pull explicitly; failed writes remain pending for the next sync.

API contract: [Linear GraphQL](https://linear.app/developers/graphql), with field
and relation definitions from the [official schema](https://github.com/linear/linear/blob/master/packages/sdk/src/schema.graphql).

## D1306: Stable source tasks preserve progress across producer retries

`upsert_source_task` derives a local task and requirement identity from a stable
producer source ID. Repeated observations return the existing record unchanged,
including parked and dropped states. New observations begin in backlog with
source provenance and an explicitly observed date. Read-only consumers never
invoke the producer mutation API.

## D1307: Task views consume the screened read-only snapshot

The dashboard's global Tasks view and project Tasks tab share one board and
explicit dependency map. They show ownership, blocked work, parked resume
conditions, source requests and links to jobs. Dropped work has its own group;
provider completion is labelled as external evidence. Search and ownership
filters work locally. Task snapshots screen nested strings with the node guard,
retain the existing keyed state endpoint and never mutate records or pull a
provider while rendering. Malformed storage produces a repair message.

## D1308: Cross-node dependencies and scoped completion retain obligations

Task references may use `node:id` within the same owner set and sealing boundary.
Dependency, parent and successor cycles refuse before writes. A landing event
reevaluates dependent blocked tasks idempotently; holds and explicit pauses remain
in force until explicitly removed. Reconsideration dates never authorize unpause.

Code completion requires a changed task baseline, delivering commit on intended
main and changed paths covering every declared scope. An open successor cannot
close transferred work. Operations require scoped, dated passed receipts; reports
add an exact node-local artifact digest/version. Provider completion remains an
external fact. Drops require their decision reference.

Task IDs survive attempts and branch aliases. Plain-Git and operational attempts
can be registered without running host actions. Parked obligations retain owner,
reason, resume condition, review date and next action in cold briefs. Aging uses
last substantive progress and a stable episode, never a bookkeeping refresh.

## D1309: Stopped-work imports retain evidence without inventing delivery

A versioned source register groups aliases under stable obligations and retains
raw heads, scope and receipt references. Importing it creates open reconciliation
tasks, including for uncertain or retired implementations. Replays preserve task
progress. Dashboard delivery labels are derived from current scoped evidence;
stale or unknown completion is visible as unproven rather than silently counted.
Task landing facts carry bounded references and fingerprints, with retry intent
retained until the metadata-only event journal confirms publication.

## D1310: Dispatch plans require task-specific authority and contract

The task engine exposes native launch commands only after dependency, hold,
capacity, explicit decision authority, contract completeness and code-scope
checks. It never treats a detected Markdown entry as execution authority.
Actual host launch and rebinding stay with the configured lifecycle adapter;
plan generation creates neither a thread nor a worktree.

## D1311: Train task completion validates the whole final graph

Train finalization publishes linked task outcomes after the atomic ledger update
and before marking its write-ahead journal complete. The accepted job identities,
successful check on the exact integration tip, main ancestry and scoped delivery
proof determine completion. Missing proof parks the obligation with a resume
condition; changed task bindings retain the journal for reconciliation.

All prospective task outcomes and dependencies validate together before any task
file is replaced. Per-task receipt identities make interrupted publication
replayable without resetting later progress or duplicating evidence. Pending
landing facts publish only after every outcome is durable. External completion
intent enters the existing provider sync outbox; finalization performs no network
request.

## D901: Local semantic recall is optional and fails back to lexical recall

An optional, short-lived ONNX worker uses the pinned multilingual-e5-small int8
model ([pinned artifacts](https://huggingface.co/Xenova/multilingual-e5-small/tree/761b726dd34fb83930e26aab4e9ac3899aa1fa78))
with query/passage prefixes, masked mean pooling and normalized cosine
ranking. No daemon, API key, remote inference or torch dependency is required.
The core remains Python 3.9 standard library only. Optional runtime packages
are detected without importing the model. Unknown or less than 2 GB available
RAM, missing packages, malformed optional configuration, offline operation and any inference/index failure retain
lexical retrieval. `brain check` reports the current mode; install and upgrade
already run that check. `brain recall --setup` prepares the model explicitly.

Artifacts download lazily with pinned revision, lengths and SHA256 integrity;
tests prohibit downloads. Ordinary recall bounds optional work to 12 seconds;
explicit setup allows 180 seconds. Model setup has no outward publication.
The private `.brain/semantic/` cache binds vectors to model and content hashes.
Linked document sources are excluded. Eligibility (scope, state, source content) is checked before embedding and every
cache read, including deletion/revocation. Proposals remain inactive. There is
no online provider enabled. The index covers usable library items and the target node's own knowledge,
decision notes and report indexes. External transcript archives are not indexed.

## D902: Proposal briefs summarize evidence without activating it

`proposals --brief` presents each pending claim, citation, source, scope and
individual supported decision commands. `--json` exposes the same bounded sheets.
The existing full view remains the source for complete evidence. Holder and
source screening applies to every sheet, including commands and paths. Brief
rendering is read-only and refuses combination with activation. Specialist/rule
rejections remain inactive source decisions where no rejection CLI exists.

## D903: Recurrence receipts consume evidence, not direction authority

Synthesis is due after three fresh corrections to one principle or five fresh
unclassified corrections. Exact occurrence hashes preserve duplicate records
and stable identities across reordering. An explicit `due --synthesis-seen` receipt
consumes only the current set, refusing stale/wrong-node sets, without rewriting
lessons, provenance, scope or direction. Reads remain repeatable until handled.
Distillation proposes an evidence-backed diff and requires an explicit decision
before applying it. Corrupt receipts, lessons and source policy fail closed with
repair notices. Inactive or out-of-scope specialist lessons never participate.

## D904: Enforcement uses declared rules and scoped correction evidence

Stable rule ids in direction Principles and role/standard Standards, Checklist
or Never sections carry `enforced_by` paths or `wish`. Lesson capture resolves
that same declaration set; duplicate ids across sources refuse. Paths are relative to the
node's bound home and cannot escape it. Check reports missing declared files;
facts and dashboard JSON expose wish counts and percentages. Two scoped wish
corrections emit a stable check-work notice. Rule declarations and correction
snapshots are preserved separately; installing a declared check clears that wish.

Task records are produced only during lesson capture or explicit
`enforcement --sync-tasks`, using the task domain's idempotent source API.
Read-only due, facts and dashboard calls never create tasks. Source references name the declaring standards/direction file. Source identities
are stable per target/principle so observations preserve parked/dropped progress.
Inactive, foreign-scope and malformed evidence cannot create work. Free-form
legacy prose is not guessed into new rule ids or silently activated.

## D905: Feedback closes through executed mapped proof

Node-local feedback records have stable ids and link a correction to its
principle, job, check and before/after evidence. Read-only list/due/facts/UI paths
never change the ledger. Add and map retain open state; a mapping change clears
old proof. Close executes the mapped relative check inside its registered job
worktree (or the ledger job's home), with a 30-second budget. Python and shell
checks use explicit interpreters; no shell command string is evaluated.

Only exit zero with unchanged check bytes and Git HEAD records closure. Proof
contains check SHA256, HEAD, job, check and timestamp. Missing evidence, a failed
check, timeout or concurrent source change leaves feedback open. Corrupt stores
are retained with a repair notice. This proof records the executed check result,
not the semantic adequacy of the check or an authenticated reviewer identity.

## D906: Native teaching applies an adaptation through the target harness

`teach` prepares a scope-checked brief, or applies an explicitly supplied native
adaptation with `--file --to --check`. External homes require an existing
registered worktree and matching branch. The target file stays inside that home;
linked files, repository metadata and generated outputs refuse. Existing files
require an exact expected content hash. Boundary crossings require every target
owner's explicit consent even when the item scope permits use.

The target's own check must pass without changing the adaptation or source
snapshot. Failure restores only this operation's unchanged copy; unexpected
concurrent edits are retained. Private machine receipts bind source and native
hashes to executed proof without putting source provenance into the native copy.
The copy remains uncommitted for the target's normal change process; teaching
neither propagates future updates nor publishes or accepts knowledge proposals.

## D907: Semantic documents retain verified source ownership through embedding

Document discovery rejects linked bindings, notes and report ancestors before
reading content. The opened regular file must have one link and its physical
node/home owner must match the caller; registered workbenches cannot override a
more specific node owner. Source bytes are bounded and read from the verified
descriptor. The same screened bytes enter embedding directly, with no second path
read that could substitute a different source. Caller provenance is assigned only
after these checks. Rejected sources never enter model inputs or vector caches;
ordinary same-node documents and lexical fallback remain available.

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

## D1204: Generate commitments from task records and landing evidence

The read-only digest uses task requirements for dated promise sources. A task is
reported done only with its completion status, a merge commit reachable from
local main, a code path and evidence. External completion without local proof,
testing, parked and dropped work remain visible as unfinished commitments.
Runnable next work requires proven completed dependencies. Missing estimates and
promise dates stay unknown. Blocked work alone does not invent a human decision.

## D1205: Generate daily measurement summaries locally

An optional `daily_retro=on` root setting lets a sampler tick write the previous
local day's completed-job summary once per machine. It records measured review
rounds, elapsed time, failures and repeated compaction signals. Missing readings
remain unknown. The generated output stays under `.brain/daily-retro`; it does
not send messages or activate lessons, rules or decisions. `brain digest --retro`
can regenerate a selected day on stdout without changing the stored snapshot.

## D1206: Delegate digest completion to the task domain

Commitment reporting calls `task_flow.completion` for each completed record.
This preserves the domain's changed-baseline code scope, operational receipt and
versioned report-artifact checks instead of maintaining a weaker duplicate
predicate. Provider-completed mirrored tasks are labelled external provider truth
with local implementation unproven. Technical blockers remain unfinished work;
they are not inferred human approval requests.

## D1207: Provision host bindings through the native transport

Explicit host actions use the supported T3 ACP MCP transport with argument arrays,
without a shell or custom host build. New-node provisioning registers its node
workspace, launches its coordinator with an explicit model selection, saves each
returned binding locally and pins the exact thread. An interrupted launch leaves
a pending marker; retries cannot blindly create another conversation. Conflicting
concurrent bindings are preserved for reconciliation.

Webhook setup must run in the bound coordinator's context. Its portable record
contains no capability URL. A stable per-binding request id deduplicates host
creation, and only a response confirming the bound thread may publish a local
wake URL. Missing remote access retains the task id and reports the setup gap.
PR watch setup also requires the coordinator context and positive host confirmation.
Pinning is not claimed to change host auto-settlement policy.

## D1208: Sync the parent directory after atomic file replacement

Atomic publication syncs the new file data, renames the complete same-folder
temporary, then syncs the containing directory. A pre-publication failure leaves
the old file intact. A directory-sync failure is reported after the new complete
file is visible, so an error does not imply rollback. Tests prove ordering and
failure behavior; physical power loss and cross-file/Git transactions require
separate evidence. Durability still depends on filesystem and storage guarantees.

## D1209: Apply portable recurring schedules from their coordinator

The native host bridge converts a declared hourly, minute interval, daily or
weekday record into a structured host trigger. Creation must run in the target's
bound coordinator, and only a response confirming that coordinator may be bound
locally. The automated brief requests the work; the host provides scheduling and
the agent supplies semantic sync/brief execution. Definition receipts make repeat
setup idempotent and refuse changed or legacy bindings that need reconciliation.
The core does not invent a separate scheduler daemon or an LLM summary engine.

## D1210: Synchronize idle-view assertions with metadata publication

The idle-view server fixture acknowledges a poll only after a complete metadata
scan that began after that poll request. A response from an earlier publication
cannot supply exact-build-count assertions. Control files live outside dashboard
inputs, so the barrier itself cannot trigger a rebuild. The fixture's wall clock
is controlled independently from real scheduling time; a dedicated regression
advances the five-minute age boundary and still requires state invalidation.
Production UI code, timeouts and exact cache assertions remain unchanged.

## D1211: Preserve task-domain dispatch and source-date semantics in digests

Next-work reporting uses the task domain's ownership-aware dependency and hold
checks, including qualified cross-node references. Backfilled file-observation
and job-start dates are labelled context instead of being presented as exact
promise dates. Focused integration exercises the real scoped completion checker
against changed code, no-op code, operational receipts, versioned and changed
report artifacts, and provider-completed mirrors.

## D1212: Refuse unavailable native host transport before provisioning state

The host CLI checks its supported adapter and native MCP transport before model
routing or writing any pending lifecycle marker. A known unavailable transport
uses the documented adapter workflow; it does not leave an ambiguous launch to
reconcile. Once a host launch actually starts, its existing interruption marker
and no-blind-retry behavior still apply.

## D1213: Preserve returned host identities across binding rotations

If a coordinator binding changes during a completed host request, preserve the
new binding and retain the returned thread/task/watch identity locally for
reconciliation. Do not claim that the old coordinator is current, publish its
wake URL, discard the known result, or blindly repeat the host operation. Pinning
also rechecks the binding before clearing its pending operation marker.

## D1214: Fence worktree cleanup across released accounting

Removal and pruning observe the checkout identity, exact branch and integration
refs, local ancestry, and complete tracked/untracked porcelain status before a
released scan. After accounting retakes the instance lock, fresh local Git reads
must match that generation before removal or ledger closure. Changed or
unreadable evidence retains the workspace, including for explicit forced removal.
The final outcome uses the rechecked ancestry; squash-merge evidence is reusable
only for unchanged inputs. Display caches cannot supply destructive evidence.
Existing submodule retention and live-work checks remain in force. This is a
bounded revalidation, not a lease excluding concurrent external Git writes after
the final observation; Git's own removal refusal remains necessary.

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

## D117: New introspection tests have explicit lane ownership

The introspection, diagnostic and scorecard suites each belong to declared test
lanes. Their nonselective runtime modules, including the shared typed-event
import, are explicit shared inputs because command dispatch and fixture loading
can import them in any lane. Coverage validation remains fail-closed; no runner,
selection assertion or canonical test class is removed or weakened.

## D1101: Inspect current work and batch dashboard repository evidence

The dashboard retains metadata for every recorded worktree. Git status, socket,
readiness and file-list inspection apply to worktrees with an open ledger job or
an active status-table row. Closed worktrees expose unknown activity values;
they are never presented as clean or ready from an omitted inspection. Reopening
a job or changing the active-work table invalidates repository discovery.

Due evidence batches branch tips and merged membership in one local ref snapshot
per repository and integration mode. It retains closed and unrecorded worktrees
for reconciliation. Dashboard refreshes may reuse this evidence with the same
file/ref metadata keys as their readiness display cache. Ref changes, worktree
existence, bindings and ledger inputs invalidate it. The cache is context-local;
explicit lifecycle and readiness actions read current evidence outside it.

Tests cover a large closed-worktree inventory, active-row recovery, reopened
jobs, batched ref reads, invalidation and an explicit action after cached display.

## D1102: Bind preview tours to source and artifact evidence

`preview <node> --record <receipt.json>` validates an externally captured preview
tour before replacing its machine-local receipt. Evidence names the configured
preview address and tour, a bound source worktree or home at a clean HEAD, an
explicit capture time, and SHA-256 digests for the tour and its linked PNG
screenshots. Early worktree milestones can be recorded before a main landing.

The read-only check and due queue reject missing, stale or malformed evidence.
A bare URL does not discharge the screenshot-tour need. Failed recording retains
the prior receipt; changing source, address, tour binding or artifact bytes makes
it stale. The receipt validates supplied capture evidence, not visual quality or
the accuracy of application data. Those remain the browser review's responsibility.
No network request, browser launch or source change occurs during validation.

## D1103: Separate round budget meters from lifetime job accounting

Open-job payloads retain lifetime `usage` and add optional `round_usage` from the
accounting backend. Budget token/time and compaction badges use replacement-round
counters when present. Missing counters remain unknown; invalid receipts show an
unavailable meter instead of charging the previous round or displaying zero.
Older accounting backends retain their existing lifetime behavior.

Usage receipt metadata invalidates only the affected job node's display cache.
Preview receipt and external tour/screenshot metadata also participate in node
cache keys. These readers never publish lifecycle or accounting state.

The Jobs summary consumes the accounting service's daily throughput rows, including
zero-merge days and measurement coverage. The existing project picker scopes those
rows. The summary opens the table in the existing reader, keeping one main list
on the Jobs page, and cannot shrink below its contents. The duration chart and
lifetime totals remain visible in their existing locations.

## D1104: Retain source ownership across generated completion displays

Event status projections preserve a bound Active work row's source state,
including unknown states and terminal annotations. Ledger completion alone cannot
turn live ownership into terminal cleanup authorization. Ambiguous, unmatched,
incomplete and still-live rows beside settled ledger entries remain visible across
repeated projections. Branch backticks and annotations use the cleanup reader's
matching rules.

An explicit job-close transition updates one unambiguous source row. Explicit
terminal source labels continue to permit cleanup subject to the existing Git,
dirty-worktree and competing-owner guards. Generated facts and ledger timestamps
never establish host liveness or safe thread settlement.

Failed and cancelled setup operations attribute durable inbox hooks before
checking for partial writes. A hook-only failure records its abort and retains
the hook; actual unfinished source writes remain pending for reconciliation.
The original setup result or exception remains authoritative.

Worktree cleanup records current journal metadata for replay without automatically
rewriting the status document, Active work table or status archive. Explicit
status refresh still materializes the view and caps the Now section. Other event
projection paths remain available.

## D18101: Preserve operation and generated-status ownership

Journal command routing accepts scalar node selectors; collections of item IDs
do not identify a node. A node refresh beside a live released writer may leave
that writer's pending subtree untouched. It cannot reconcile the same subtree,
global machine state, damaged journals or interrupted operations. Explicit
instance reconciliation retains its live-writer refusal.

Generated facts and recorded Now/archive transformations do not imply missing
coordinator judgement. Unowned prose remains visible as uncommitted work. Node
creation, merge and change acknowledgement finish with generated status committed;
routine refresh retains hourly throttling. Machine-only binding changes and
import-study handbacks record their event view without rewriting coordinator
status. Cleanup still never materializes status implicitly.

Metadata-only status retains typed restart reasons while excluding private
handback prose. Event writes resolve validated contained directory aliases to a
physical parent before no-follow opens and descriptor comparisons; linked files,
hard links and moved descriptors remain refused.

## D18201: Validate study completion against its actual open job

Handback preflight identifies an open matching import-study row before applying
ordinary home-job review requirements. A study may publish blocked, question or
done with absent or zero review rounds, without a home readiness packet. A branch
name alone does not establish this exemption. Reverification and imported full
receipts remain home verification operations and are refused for studies; queued
requests must pass the same actual-row validation before publishing an attempt.

Compatibility scenario fixtures keep the historical jobs owner of `job_started`
separate from the native task row hook. Fabricated origin repositories provide
local cached default refs without fetching, preserving fail-closed cleanup.
Advisory fixtures supply the routing interface, distinguish round budget resets
from lifetime advice, and bind independent coordinators when proving local
recurrence or settlement scope. Batch eligibility remains advice with ready=false.
Managed success notices are answered through their inbox episode while retaining
source history; lesson outcomes are checked independently. Cleanup snapshot tests
assert unchanged status bytes until an explicit refresh. Invalid reverify requests
retain a no-additional-run assertion.
## D18301: Separate service serialization from ordinary state and verify admission

Machine service install and uninstall share the instance's bounded service
reservation. Their ordinary state lock is released while waiting for the
reservation and while calling the service manager, then the existing released
transaction rechecks its inputs. Dry-run previews do not acquire the reservation.

Managed verification fixtures use real Instances and private storage. Cheap
checks keep their result and terminal outcome through cleanup, and full admission
starts only after a successful cheap check. Fitness uses heavy admission; ordinary
verify lanes retain FIFO slot admission. Separate contention assertions prove
fitness coexistence and verify requeue ordering, with every owned attempt finalized.

Code-home admission fixtures declare a harmless verify command and a recorded
coordinator tick before testing thermal holds. These prerequisites do not weaken
the admission refusal, quiet output or absent-worktree assertions.
## D1501: Complete dashboard usage before clearing fill state

The dashboard drains existing usage caches in bounded bootstrap slices and keeps
advancing incomplete aggregates between metadata polls. Partial values remain
visible, and the filling state clears only when usage aggregation is complete.
No transcript content is published and viewing does not persist usage caches.

## D1502: External rehearsal capture is declared and machine-local

An optional `rehearse_external` declaration in local bindings selects exact home,
worktree or workbench roots for one private capture before comparison. Both lanes
read the same detached capture, including dirty and ignored bytes. Unknown
bindings retain drift detection. Capture is fixed after preparation; it does not
claim an atomic historical snapshot against concurrent writers during copying.
No declaration preserves the existing observation mode.

## D1503: Facts projection has its own explicit ownership phase

A node configured with `status_projection=owned-facts` can run `status --project`
as a separate phase. The phase updates only the delimited generated facts block,
preserves all source bytes outside it and all archive bytes, refuses active or
interrupted writers, and leaves unchanged facts untouched. Cleanup still never
rewrites status. Ordinary explicit refresh and journal replay remain available.
No scheduler, background service or implicit cleanup invocation is added.

## D1504: Parallel selftests redistribute the remaining queue

Parallel selftests use the pinned runner's work-stealing scheduler with worker
restarts disabled and unchanged canonical suite selection. Focused dispatch
proof cannot establish the duration or readiness of a complete suite.

## D18501: Compare recovery snapshots at one instance time

The dashboard recovery scenario fixes the existing instance clock while comparing
completed SSE contents with a later state request. Derived ages and forecast
measurement timestamps then describe the same observation. Server scheduling,
retry barriers and demand still use the real monotonic clock. The scenario keeps
its complete payload equality, private-error screening, partial-to-complete
transition and single initialization worker assertions. Runtime publication and
refresh semantics are unchanged.

## D18502: Dashboard due uses the coordinator read projection

The dashboard reads `coordinator_due_items` once for its selected nodes, matching
the CLI's routing of ordinary needs, unread changes and completion notices. It
copies items before adding display fields and retains their existing IDs. A read
does not reconcile events or deliver completions. The ledger scenario preserves
all landing notices, verifies repeated reads retain their identities, and checks
that viewing preserves ledger, completion journal and watch delivery state and
does not invoke provider polling or host delivery. Existing reminder lifecycle
reconciliation is unchanged.


## D18601: Preserve native completion and pre-creation admission contracts

Focused cheap-check fixtures distinguish a nonzero result (`failed`) from an
exception (`error`), retaining exit 7, queue release and the prohibition on full
admission after a failed cheap check. Queue preflight receives the actual bound
job row, so open import studies retain their documented zero-review completion
while ordinary jobs and invalid study flags still refuse before publication.

Worktree creation checks the canonical machine policy and reading before creating
the workbench, branch, worktree, local binding or ledger row, including with
`--no-setup`. A held start uses managed native admission and preserves its terminal
record; healthy light creation does not acquire a heavy reservation. Setup retains
its own resource reservation. Invalid machine policy has no fallback.

Completion display fixtures retain real landed notices with stable source-owned
identities. A quiet tick remains actionable when such a notice exists, even when
other advice is informational. Lesson outcomes are checked independently of the
preview evidence lifecycle: setting a URL and tour path still leaves the exact
source-bound screenshot-tour need until a real receipt is recorded.

Integrated fixture files have explicit, unique status, rehearsal and dashboard
lane ownership. Existing command selection and worker policy are unchanged.


## D18602: Queue study fixtures use the native study identity grammar

A fabricated study uses an `import:` prefix followed by a supported commit hash.
An arbitrary label is an ordinary job and must not gain the study exemption.
The existing single-owner queue fixture uses a valid study identity for its
positive zero-review case and retains ordinary-job and invalid-flag refusals.
## D18701: Separate snapshot refresh from active journal reconciliation

Snapshot-backed status refresh leaves event baseline reconciliation deferred while
a foreign writer holds a live lease. It renders the existing snapshot sources
without adopting pending bytes or closing the writer. Idle refresh still recovers
interrupted records. Explicit reconciliation and event-backed refresh retain
their existing live-owner refusals and replay rules.

Optional rehearsal preparation no longer invokes the declared external-capture
helper. The standalone helper and its tests remain available, but a machine-local
declaration alone does not activate it in rehearsal. Optional rehearsal policy,
its original baseline assertions and its failure outcomes remain unchanged.

Contained-directory alias publication remains unresolved: existing containment
assertions require supported internal aliases, while atomic directory sync uses
no-follow opens. Removing those assertions or relaxing escaping-link/hard-link
checks is not a valid scope reduction. Existing durability behavior is retained
pending a coherent publication contract.

## D18801: Publish through the parent resolved inside containment

The target check returns the canonical parent it has already verified stays inside
the instance, preserving the final filename and the existing escaping-link and
hard-link checks. Atomic publication uses that checked parent for its temporary
file, replacement and no-follow directory open. An internal directory alias thus
retains its supported behavior without removing directory fsync. File sync still
precedes replacement, and directory sync errors still propagate after publication.


## D18901: Keep strict real-core rehearsal as an optional diagnostic

Only `RehearsalNativeCore::test_real_core_cli_clean_candidate_and_byte_identical_source`
moves from the aggregate per-change collection to `tests/nightly_rehearse.py`.
The existing outside-job-lane inventory classifies that file; it remains an input
to the rehearsal lane. The original class, method and assertions are unchanged,
and every other canonical test remains collected exactly once. The explicit
command is recorded in CHANGES.md and the optional file's module documentation.

Real-core write accounting has seven unresolved unexpected writes. They must be
named and then fixed or justified before this issue can close. Optional execution
retains the strict source snapshots and write comparisons. No runtime allowlist,
journal suppression or rehearsal success is introduced by this classification.
The first future nightly result remains the coordinator's reporting obligation;
this change creates no schedule or delivery integration.

## D19001: Retain the checked event publication target

Event storage retains the canonical target returned by its containment check
before creating directories or opening files. A contained directory alias that
changes afterward cannot redirect publication to its new destination. Appends,
journal indexes, repair markers, leases, comparison and status metadata, and
immutable event objects share this boundary. Final-file link and hard-link
refusals, no-follow opens, descriptor identity checks and persistence ordering
remain in force. Standalone paths retain their physical-parent behavior.

## D19101: Flush test identities and enforce per-test timeouts

Development pytest runs require the pinned pytest-timeout plugin and apply a
120-second signal timeout by default, including setup and teardown. End-user
execution remains Python 3.9 and standard-library-only. A controller-side hook
streams each phase's nodeid, outcome and duration, then a completion with combined
phase duration and failure precedence. Failure phases retain their traceback.
Every event flushes immediately to terminal output and, when requested with
`--completed-results=/private/new-file.jsonl`, an exclusively created JSONL file.
Under xdist only the controller writes that file. A parent interruption can
therefore retain completed identities even when final JUnit and duration tables
have not been written. Existing native verification logs capture the terminal
stream without command changes; private structured evidence is opt-in. The
configuration is a shared lane input. Collection and business assertions remain
unchanged; synthetic proofs cover interruption, xdist timeout and fixture errors.

## D20001: Separate semantic lane identity from execution evidence

Snapshots retain a semantic context for configuration, inputs and resolved tools,
with selected non-PATH environment values distinguishing unset from empty. An
irrelevant PATH suffix preserves this identity (D404). A separate versioned
evidence context digests the semantic context and the complete selected recorded
environment, including PATH. Ready packets store this evidence context in
`lane_context_hash`; validation, reuse and emission all use it. Missing or legacy
identity-only evidence refuses readiness and requires fresh execution.

Resolving command tokens alone cannot identify interpreters selected by an env
shebang or helpers launched from a module. Reading readiness strictly restores
the packet's recorded environment before computing evidence; an unrelated caller
PATH therefore preserves readiness. A subsequent handback uses its current
environment and reruns lanes if that evidence context changes. Recorded PATH
tampering refuses readiness even when semantic identity remains unchanged.
Existing executable identity, ownership, input, port and full-receipt guards
remain in place.

The private input-sidecar regression permits its fixture's generated status
facts to commit before asserting whole-tree cleanliness. Hourly status commit
coalescing otherwise leaves an unrelated generated status edit. The original
clean-tree, private-hash, missing-evidence and reuse assertions remain intact;
an explicit ignore-rule assertion also checks the sidecar is ignored by Git.

Fabricated lane snapshots used by gate-ownership and machine-admission tests
also supply distinct semantic and evidence contexts, including exception-release
fixtures. Their shape follows the production snapshot contract so result emission
can complete before the existing ownership, queue-order and cleanup assertions.
Production validation still refuses missing or legacy evidence identities.

## D20002: Isolate completion reader cases by delivery owner

Completion read-failure fixtures bind a fresh fabricated recipient for each
independent failure mode. Successful frozen snapshots acknowledge identities for
their delivery owner, so reusing that owner makes a later case test suppression
of already shown work instead of unreadable completion evidence. Existing
assertions still require unknown job or ledger notices and mixed requests to
survive while unrelated stale notices retire. Additional assertions prove the
recipient starts without shown identities and that a second request with the
same identities cannot repeat delivery, including during the read failure.

The native resume-brief fixture accepts the routing helper's explicit `extra`
argument and checks its empty context list. It retains the early-acknowledgement
trap and verifies the complete restart lifecycle record is unchanged by brief
preparation. Runtime delivery, ownership and baseline rules remain unchanged.

## D22001: Private connection outputs do not refresh shared status

Connection plans and saved briefs keep their normal write lock, operation lease,
durable journal and replay views. When neither the command nor its maintenance
changes tracked instance state, automatic status materialization is unnecessary:
a clock boundary alone must not dirty the shared tree. Actual shared changes,
including writes made during maintenance, still materialize their projection.
Explicit status refresh and other commands retain their existing behavior.

Regression coverage crosses the existing instance clock seam while retaining the
original plan, digest, gating and clean-tree assertions. Connected controls cover
private plan variants, saved and unsaved briefs, and genuine shared mutations.

## D22004: Make explicit-refresh assertions depend on semantic fixture inputs

The shared-creation regression changes the fixture's published-preview input
after proving automatic projection stayed suppressed. It asserts stale status
and unchanged bytes before explicit refresh, then retains byte inequality and
currentness checks and requires the exact generated preview line. The original
clock-based suppression control remains intact without extending its scope.

## D27001: Isolate fast fixture construction from subprocess contracts

Scenario setup may explicitly opt into fresh in-process CLI state for fixture
initialization and named setup commands. Assertions still call the real launcher
through the existing subprocess helpers. Each setup call restores environment,
arguments and working directory and discards its isolated package, so command
caches and touched paths do not carry into another call. Evidence capture keeps
the original child process path.

On macOS, fixture initialization clones a source-only core template with APFS
copy-on-write files. It runs native initialization anew for every instance; no
journal, derived index, Git history or machine binding is copied between brains.
Other platforms retain ordinary independent file copies. Installed core edits
remain private to each fixture, and initialization/launcher subprocess controls
remain in the suite.

## D21001: Bind dashboard caches to current inputs

The file-only first reading remains independent of repository inspection, usage aggregation,
private cache availability and business write locks. One background worker builds the complete
reading and retries initialization on demand after a failure.

A detached observation holds lifecycle inputs for one render. Reconciliation uses copies;
viewing never delivers queued questions, persists reminder generations, advances stall clocks
or acknowledges machine holds. Existing recorded identities win; prospective identities are
stable display values until an owning command records an episode. Commands outside the
observation retain their normal transactions.

Private derived caches reuse metadata only for the same code, instance, input metadata,
usage-source metadata, access policy, machine-need state and discovery arguments. Usage
aggregates still require an authorized opened source with matching identity and cursor.
Changed inputs revoke completion before another slice; revoked access clears prior totals.
Private publication requires the held-directory atomic writer; without that capability the
view continues without persistence. Cache failures are misses. Neither cache reuse nor a
complete display is verification or landing evidence.

## D28001: Reuse dashboard directory inventories without caching file stamps

Keep a process-local inventory of each input directory, bound to its current identity,
mode, link count and modification/change timestamps. Reuse its child names only while
that binding is unchanged. Continue checking every known file's metadata on each reading,
so in-place writes invalidate the reading even when parent directory timestamps do not
change. Rescan changed directories and discard removed descendants. Do not descend through
child directory symlinks or change the existing input exclusions.

The index contains no source contents and writes no files. It does not replace source
authorization, usage aggregation, private publication or the completion marker. Its reuse
alone establishes neither complete warm performance nor browser paint timing.

## D21002: Separate captured operands from rehearsal publication

Optional external capture constructs independent bindings on a real Instance;
the capture helper never rewrites its caller's local configuration. Only the
rehearsal's private preparation copy persists those bindings. Both command lanes
read the same detached captures outside their writable boundaries. Unknown or
undeclared operands retain drift detection; source capture is fixed input rather
than a claim of an atomic historical snapshot. Captured bytes are audited after
both lanes. Native journal ownership, live leases and process cleanup remain real.

Unexpected-write policy remains strict, including journal writes. Private raw
sidecars retain each changed path and operation and each denied Python write
attempt, even when both lanes performed the same writes. Published evidence
contains counts and SHA-256 fingerprints of filesystem-encoded lane-relative
paths, never raw paths or contents. These diagnostics attribute a failed run;
they do not turn it into a pass. Rehearsal remains advisory for readiness and
landing, and the original real-core diagnostic remains optional with unchanged
assertions.

## D21004: Prove native transaction effects before comparing journal writes

An absent communications store is an empty observation. Reconciliation does not
create a directory, lock or store for that observation. Existing stores retain
the exclusive lock, typed validation and routine-only deadline defaults.

Rehearsal retains exact private before/after journal bytes and separately observes
native command entry, append, finish and lease closure. A journal mutation can
compare as expected only when those observations belong to the launched process,
match its command and live lease, preserve prior records, account for every new
record and comparison, and agree with independent replay and derived indexes.
Unknown kinds, missing or reordered records, duplicates, foreign ownership,
corrupt indexes, unresolved partial writes and unexplained residual paths fail
closed. No product path gains a write permission from transaction evidence.

The observer runs only in isolated mutating probes and keeps native publication,
locking, journal history and the original optional acceptance assertions intact.
Private evidence never enters the public diagnostic channels. This is an advisory
comparison contract, not readiness, release or arbitrary-code authenticity proof.

D21004 evidence addendum: an explicitly requested isolated diagnostic may persist
its authentic private operands, supervision fields, source/bootstrap identities
and rejection checkpoint before assertions and teardown. Storage is owner-only
outside repositories; public diagnostics contain only screened checkpoint/category
and an artifact name/hash. A post-run manifest binds the native receipt to that
immutable packet. Evidence collection does not alter transaction acceptance,
permissions, journal records or assertions, and cannot recover prior lost bytes.

## D21005: Attribute pre-begin coverage separately from comparisons

Native coverage runs under the instance lock before an event lease exists. The
isolated observer witnesses the native lock acquisition, command identity and
explicitly absent lease at entry, each coverage publication and exit. It captures
the operands consumed by the native detector without moving its computation.
The later begin and live lease cannot supply missing pre-begin ownership.

Coverage and comparison history have distinct strict native schemas. Every new
history row must match one ordered observation, and each complete detector call
must match all affected nodes and its exact resource, interruption and fault
operands. Historical bytes, independent replay, indexes, terminal ownership and
residual-write checks still bind acceptance. Missing, caught or partial evidence
fails closed. An old incomplete capture remains unproven; a new positive requires
a separately retained authentic capture. No producer fields or path permissions
are added by this proof.

## D21006: Retain actual helper evidence before fixture deletion

An explicitly requested private diagnostic initializes an incomplete capsule
before native helper initialization, saves the actual capture operands and
ordered trace incrementally, and forwards validator diagnostics with its exact
invocation instant and result. Early assertions and exceptions retain partial
evidence with unreached fields explicitly unavailable. A pre-delete fixture hook
persists again before removal; failed retention preserves the owned fixture and
marks evidence incomplete. Owner-only artifacts bind source identities and a
post-run receipt manifest. Uncatchable termination can still prevent retention.
This instrumentation changes no native publication, acceptance rule, assertion,
schema or write permission and supplies no qualification by itself.

## D1401: Queue recovery requires a quiescent run or an exact publication

Foreground handback claims record a random run generation before dispatch. An
explicit recovery of one request first acquires the same stable exclusive lease,
checks the process start identity and revalidates the exact job, attempt and input
binding. It can reclaim a pre-dispatch claim or reconcile its exact already-written
terminal handback. It preserves prior generations and fences their publication.
Only a schema-valid terminal handback can reconcile a publication.
The native verifier inherits the lease descriptor through its bootstrap and exec.
Legacy claims, live or unknown owners and dispatched runs without descendant
shutdown proof remain retained. Recovery never signals a PID or infers that an
unlocked descriptor proves every descendant has stopped.

## D1402: Cleanup tells workers before fresh settlement

The explicit worktree cleanup settlement option uses the existing closed-job
callback. A durable notice binds the exact job, thread and checkout inode before
the native send. An accepted notice is not repeated; an unknown outcome stays
retained. Sending may start a final turn, so removal waits for a later terminal,
request-free host observation and the existing exhaustive settlement validation.
Already-settled threads receive no message, and missing checkouts never wake old
threads. Current ownership and worktree bindings are rechecked before removal.

## D1403: Preview upgrades use product-owned protocols after landing

Per-node machine-local configuration names a registered trusted adapter, resource,
protocol revision and explicit backup, migration, restart, acceptance and rollback
operations. Core provides no shell executor or inferred product protocol. Product
adapters synchronously own and join their resources, return bounded opaque step
receipts, and implement snapshot restoration plus acceptance during rollback.

Landing hooks run only after confirmed publication. A stable node lock serializes
heads and each step intent is durable before crossing the product boundary.
Successful heads are idempotent; failed acceptance rolls back to the saved backup
and remains failed. Unknown/interrupted effects, configuration changes and failed
rollback stay unresolved and block further automatic heads. Missing configuration
or unsupported adapters remain pending. These core receipts do not establish real
product backup, migration, restart or host acceptance.

## D28004: Lifecycle authority stays with the exact local host and current owner

Code-only candidate. Queue runner generations and preview operation receipts bind
to a kernel boot UUID; a hostname, PID or boot timestamp cannot establish authority
over another machine. Unknown, foreign and legacy identities retain their evidence
without local PID probing, automatic replay or inferred success. A reboot requires
explicit reconciliation rather than adoption of prior lifecycle authority.

Preview completion requires a structurally complete ordered product receipt, not
just terminal state text. Missing product adapters remain pending. The registered
product adapter must own its resource and synchronously join each operation;
the core provides no distributed lease, generic executor or backup implementation.

Settlement reloads exact closed-job worker bindings after host observation and
before mutation. A partial transport failure reports the threads already freshly
confirmed, retains the job and never invents exhaustive completion. Host atomic
ownership/settlement and dispatched-descendant shutdown still require their real
capabilities; no PID-only cancellation or automatic takeover is introduced.

Preview execution now requires synchronous product-owned acquisition and release.
The returned ownership binds the exact configured resource, kernel boot and run
with an opaque lease and receipt. Each step echoes that ownership and confirms
quiescent descendants before another step or rollback starts. A separate local
resource lock serializes competing node locks; it does not claim distributed
ownership. Unknown acquisition, step or release outcomes retain pending evidence
without retries. Earlier receipts lacking ownership or release cannot authorize
another operation. Product adapters still supply real backup, migration, restart,
acceptance, rollback, lease enforcement and descendant shutdown semantics.

The native organization schema has no conditional revision/run/descendant
predicate. Its settlement transport therefore refuses automatic mutation before
starting a bridge process. Mock-host settlement evidence does not establish that
missing native capability. Worker restart and initial-launch receipts are boot
bound, and every native worker call rechecks the boot. Queue dispatch, verifier
lease inheritance and terminal mutation require the caller's live descriptor,
matching process stamp and unchanged lease inode. A copied lease token grants no
such authority. This guards the owned dispatch boundary; arbitrary dispatched
process-tree shutdown remains unsupported, and crash takeover stays fail closed.

## D30001: Address hosts privately and complete interrupted metadata explicitly

Addressed host descriptors live only in the machine-local bindings file. Each
entry names one machine, adapter and node binding set. Selecting a remote entry
produces a remote plan; it never redirects the native bridge to another machine.
Local execution checks the selected adapter and exact bindings before each
native request. Inventory registration does not register a provider or replace
active bindings. Conflicting entries require explicit reconciliation. Ordinary
inventory output omits host IDs and machine names.

Coordinator provisioning may require an adapter capability before creating any
pending operation or host resource. The T3 adapter supports pinning and PR
watching, but exposes no control to disable idle auto-settlement. Requiring that
control refuses provisioning; pinning alone does not satisfy it.

Local binding saves and settings batches persist ancestor directory entries
before publishing child records. Atomic file publication retains data-sync,
replacement and parent-sync ordering. Existing directory entries are re-synced
so a retry does not assume an interrupted mkdir was durable.

An unfinished settings packet retains exact before/after metadata and owns the
next publication. Explicit recovery completes only named metadata files under
the writer lock, while Git HEAD and target/index states still match the packet.
Foreign changes, links, competing packets and unknown commit outcomes refuse
recovery. A path-limited commit preserves unrelated staged entries. Completed
packets make repeated completion idempotent. This is process-interruption
recoverability, not atomic cross-file visibility or a guarantee about physical
power loss, Git storage durability or host transactions.

## D28005: Include explicit recovery in every consumer lane

Metadata recovery is a nonselective runtime module, including when loaded only
inside an explicit command handler. Its callable source is a shared lane input,
so changing recovery invalidates every consumer's verification. A regression
checks this selection and keeps the durability tests owned exactly once by the
machine lane. Recovery ownership, refusal and persistence semantics are unchanged.

## D1601: Retain quiet notices before publishing a daily inbox brief

An explicit machine-local message policy gates typed notification publication
using the instance timezone. Deferred notices survive restarts. One immutable
daily snapshot owns inbox delivery; interrupted publication reuses that snapshot
and idempotent inbox marker. New notices cannot overwrite it. Questions retain
their immediate path and ordinary notices have no implicit urgency override.
Morning briefs are inbox-only and never widen root's question-only wake rule.
Other host and messaging channels require their own delivery integration.

## D1602: Revalidate prepared task authority at native initial launch

Task plans use their contract's real separation reason. Required mode refuses a
missing reason. The native initial-launch path requires the exact active task,
job and contract binding and rechecks authority, dependencies and holds before
calling the lifecycle adapter. A prepared attempt keeps its allocated capacity
slot while competing work still counts. Arbitrary Markdown never authorizes a
host launch. Existing lifecycle receipts own uncertain-outcome recovery.

## D1603: Combine documented native run starts with owned queue evidence

The host exporter reads actual active-run identity and its matching absolute
start timestamp from the native thread-read schema. Fresh bound observations
combine with the coalesced Brain queue for busy-coordinator advisories. Host
question requests, last-update times and unobserved queue state do not substitute
for queued wakes or run starts. Unsupported host-only queue observations remain
unknown.

## D28003: Reserve an operator brief's local delivery day before publication

The explicit operator inbox summary aggregates retained quiet-hour notices from
active nodes. It uses a separate immutable snapshot and daily reservation from
node inbox briefs. Recovery claims the current local delivery day before inbox
publication, preventing a recovered snapshot and a fresh snapshot on that day.
Later notices remain retained for a subsequent day. Empty calls reserve nothing.
The instance timezone and explicit quiet-hour policy apply; questions keep their
existing immediate path. This local boundary neither sends host messages nor
configures schedules, and an inbox receipt does not prove person delivery.

## Explicit local preview transport

Trusted product preview registration uses a separately versioned, machine-private
JSON packet and finite JSON over stdio. The registered executable, literal argv,
file digests and clean checkout identity are pinned and revalidated. Each actual
landing/reconciliation process bootstraps its own adapter before the existing
three-method lifecycle engine resolves it. Transport faults retain unknown
effects as pending; they do not trigger release, rollback or replay. Direct-child
exit never supplies product/descendant quiescence. Product fencing and retained
snapshot restoration remain product responsibilities. See
[the supported driver contract](preview-adapters.md).

## D30003: Publish complete fixture templates and clean only owned modules

Build the source-only fixture template in a private temporary directory and
publish it only after copytree succeeds. A failed copy removes that partial
directory and leaves the cache empty, allowing the next initialization to retry
from complete source. Initialization still creates every instance's state anew.

Fixture commands execute a fresh, unregistered wrapper rather than reusing the
named module in sys.modules. The modular loader selects a namespace with no
preexisting root or descendants and exposes that exact namespace before loading.
The caller removes only that package and its descendants in finally, including
when loading or command execution fails. Preexisting wrappers, packages and
unrelated modules survive with their original objects and state. Environment,
argv, cwd and bytecode restoration and real assertion/evidence subprocess paths
remain unchanged. Regression controls inject a partial copy and import failure,
exercise command success and failure, and preserve colliding and unrelated module
objects while checking owned-package cleanup.

## D30004: Keep forced-platform template publication coverage portable

The template publication regression forces the Darwin branch to exercise atomic
cache construction on every host. Scope a deterministic clone subprocess fake
to its recovery call: require the exact executable, flags, constructed template,
destination and subprocess options, then copy that actual template using the
saved real copytree and return a completed result. Wrong operands fail before
copying. This retains the real interrupted-copy failure, partial-directory
cleanup, cache and content checks, and module ownership and context restoration
assertions. Separate real-platform clone, inode and launcher controls continue
to exercise the actual platform implementation.

## D30005: Opt audited history and wake fixtures into fresh initialization

HistorySizes and QuestionWake explicitly use the existing in-process fixture
initializer. HistorySizes inherits its initial construction from History.setUp;
its additional instance in the named-folder test is independently initialized.
QuestionWake initializes after loading its own isolated core and before any
transport or process mocks enter. The fixture helper loads and discards a
different package, preserving that test-owned core and its later mocks.

These calls construct instances without asserting initializer output, launcher
behavior or subprocess operands. Every original method body, assertion and
subcase remains unchanged, including inherited history methods, containment
mutations, live webhook requests, hanging transports, detached workers and
mocked wake-launcher checks. Other commands keep their existing execution paths.

Scenario remains default-off. Initializer and launcher controls retain real
subprocess execution, and evidence-bearing fixture calls retain the existing
real-process fallback. Each opt-in call still runs initialization against a new
instance directory with independent Git, journal, index, registry and local
state; only source files use the existing platform-specific clone optimization.
Connections, EventViews and RoundTwo already opt in directly or through their
base class, so their bootstrap paths require no additional change.


## D40003: Resolve command-only handlers after CLI parsing

The CLI used to import every command module and bind every handler before
argparse selected a command. Keep the complete parser, its metadata from the
owning modules, and shared dispatch/guard dependencies. Represent command-only
handlers with a small CLI-local deferred reference, then resolve only the selected
reference after parsing. Normal package-relative import returns the real module;
attribute lookup returns its current handler. Do not cache functions or replace
module objects, import loaders, fixture packages, or subprocess CLI execution.

Resolution occurs before the existing scrub/scan/machine comparisons, writes
classification, outward-link checks, instance lock and command-event context.
Brain selection, instance-core reexecution, help, argument definitions and error
handling retain their existing order. Shared imports remain eager where parser
metadata or dispatch requires them; this change does not claim to eliminate that
shared dependency graph. Contract controls cover unused modules during real
launcher help and current patches on the real owning module.


### Launcher help control dependency ownership

Keep the real launcher help negative-import control in the canonical train-owned test class. Its unchanged literal train command/module assertions are dependencies recognized by the strict lane scanner. The control still executes the real launcher subprocess; no fixture initialization path or dependency-scanner exemption is introduced.

## Current quiet policy applies to same-key notice recovery

A nonquestion retry validates the current quiet policy before recovering its
durable receipt. When quiet hours apply, it persists deferral on that receipt
under the communication store lock, after checking for a content collision.
Stable identity, timestamp and content remain unchanged. Existing deferral is
never cleared by a later retry: morning publication retains ownership of the
pending notice and its exactly-once inbox marker. Typed questions keep their
immediate path and existing action authority. This changes no host transport,
schedule or root routing policy.

A completed direct publication must not become pending morning work on a quiet
retry. Legacy receipts establish that state from their existing inbox marker,
read under the inbox writer's lock; observation or durability errors refuse the
retry before deferral changes. Content collision validation still comes first.
Both published and unpublished quiet retries suppress inbox and wake delivery.
Direct notice append rechecks the durable receipt and current quiet policy under
the communication store lock, serializing it with recovery after the initial
receipt has been saved. Existing deferral remains owned by morning delivery.
The instance writer lock precedes this publication critical section, matching
the inbox event writer's lock order even when called outside CLI dispatch.

Atomic file publication writes and fsyncs at 0600, then applies and fsyncs the existing destination mode (0644 for a new public file, 0600 for private targets) before rename; mode-preparation failures abort publication and attempt private cleanup.

Private local bindings, host/wake state, inventories, caches and execution receipts explicitly select private publication (0600), including durable-directory and metadata-recovery writers; generic shared content retains existing destination modes and its public default.

Committed synthesis acknowledgements and task-sync state retain shared modes; private writable opens, inventories, hook logs, content locks, rehearsal state/traces and model manifests enforce 0600 before content writes, while read-only opens never change permissions.

## D28002: Measure dashboard acceptance on real serving code and source files

Qualify complete warm fill with an isolated synthetic instance, real transcript records
and an authenticated integrated server. After the initial fill, change metadata and append
usage; stop the warm timer only when both changes appear in a complete backend reading.
Check source-derived entity inventories and independently calculated token totals. Keep
startup and warm refill timings separate, and retain three samples and their medians.

Browser acceptance navigates the actual serving URL without injected snapshots or mocked
responses. Bound first real-data paint by the rendered DOM and two animation frames; report
the browser engine and this conservative timing definition. Audit every view, paginate all
lists, compare graph entity identities, and inspect every project's jobs, tasks and threads.
Preserve unavailable readings as unavailable. A synthetic acceptance result qualifies its
recorded data envelope, not arbitrary production sizes or the integrated release gate.

Completeness compares each request and coordinator step as a nonempty display record,
including project context and the job title where shown. Require exact record inventories
and reject blank, duplicate, ambiguous or substituted rows. Compare every overview KPI
label and value with the backend, retaining unavailable machine readings as unavailable.
Challenge the same oracle with blanked request/step bodies, repeated/substituted rows
and altered KPI values; all negative controls must be rejected before acceptance passes.
For first-data paint, derive the oracle from the actual HTTP response delivered to the
app. Do not add a second state request to the measured navigation merely to obtain
expected values; that changes the workload instead of observing the displayed reading.

## D40005: Bound read commands with shared observations and dated UI snapshots

A read command shares session discovery, local Git inventories, ledger parsing
and status facts for its lifetime. SQLite keeps rebuildable usage aggregates and
opaque session-discovery metadata under the ignored private state directory.
Parser offsets consume appended whole lines; changed file identity, truncation
or same-size rewrite invalidates the aggregate. Final ledger or worker episodes reuse their
measured totals, and runaway advice inspects live or unsettled work only. Cache failures
leave source-based reads available. Existing JSON aggregates remain compatible.

Every CLI invocation records elapsed and phase durations, including instance-lock
wait, in `.brain/timings/events.jsonl`. The operational journal retains its
existing transaction records. Commands over five seconds also save cumulative
function profiles. `brain timings --slow --command due` retrieves observations;
arguments, transcript text and raw session identities never enter these records.
The timing stream has a separate `lock_wait_s` field, and timing-write failures never fail the command.

Inbox computation and due watcher scans run outside the exclusive instance lock.
Inbox delivery precedes a short transaction that rereads and merges reporter
acknowledgements. Watch scans retain their scan/reconciliation receipt transaction;
only receipt and wake publication uses the short instance-lock phase.

`brain ui --json` returns an honest initial projection on a cache miss: problems
and readiness are unknown, and filling-in/source metadata explains its scope.
A background fill computes without an exclusive instance lock. A completed private
snapshot can be returned with its observation time and age; changed visibility,
permissions, code or scan selection refuses reuse. `--wait` requests a fresh full
snapshot for scripts and release rehearsals. Cached UI observations grant no action
or readiness authority: check, guard and the native readiness gates remain current
and unchanged.

Accounting regressions inspect the cache keyed to the measured job episode;
fixture setup may retain an earlier episode's cache, so directory iteration is
not a reliable selector for the aggregate under test.

Inbox acknowledgement publication errors retain the already-flushed delivery
and warning, then propagate the original error with a nonzero exit. They never
report successful persistence. A stale-generation no-op may still repeat later
without overwriting a newer acknowledgement or resolution.

## D40006: Preserve strict read effects and native telemetry evidence

Telemetry follows the selected command's declared effect. Mutations and the
explicit command-read snapshot paths retain it; pure reads, rejected commands,
and explicit UI discovery scans publish no telemetry or detached UI cache.
This narrows D40005's invocation-wide publication rule. Slow profiles now live
inside the append-only timing record, with at most 30 sanitized functions and
a hard 16 KiB UTF-8 limit including the newline. Existing profile files remain
readable; new commands create no separate profile file.

Rehearsal observes the native timing producer's exact append operand and its
before/after bytes and private metadata. Only an observed append to
`.brain/timings/events.jsonl`, plus its directory on first creation, can be
proven. Missing observations, rewritten or extra bytes, and unsafe modes refuse
proof. General write permissions and the strict zero-residual assertions stay
unchanged.

Read projections retain the configuration and local state of their loaded
instance. Due's Docker observations precede watcher and display evidence.
Introduction-date caching is scoped to an explicit command snapshot, and Git
commit freshness evidence remains present after status metadata is generated.
Live and detached UI observations expose the same top-level metadata fields.

## Short writer holds for sampling and worker completion

Machine sampling collects and reduces a reading before attempting its nonblocking
publication lock. Publication compares the observed state and current policy;
a concurrent replacement wins and the old sample is skipped. Pending episode
transitions remain recoverable across interrupted publication.

Worker completion releases the CLI lock before readiness setup and accounting.
Attempt bindings are fingerprinted outside the lock, with node, ledger, attempt,
Git-ref and selected-input generations checked again before publication. Native
readiness facts and attempt writes release their physical holds before the next
computation, even inside a suspended outer CLI scope. Terminal status projection
also computes outside the lock; concurrent source changes invalidate that view
without undoing the durable handback.

Wake drains retain their destination and delivery fences without acquiring the
instance lock for projection, rate waits or transport. A changed coordinator or
transport generation after projection leaves requests pending for the current
binding. Command timings separately report physical instance-lock hold and wait
seconds; nested scopes count each physical hold once and exclude released work.

Task creation and updates prepare and validate their prospective graph outside
the writer lock, then fence task/requirement, job and Git-ref generations before
saving. Receipt validation follows the same pattern for node bindings, ledger
identity and exact code; a review observation must still be fresh at publication.
Generated status for task and receipt writes computes outside the lock too.
These changes preserve task counts and the existing graph/completion rules.

Watcher source generations cover inbox/status, tasks, ledgers, attempts, recorded
communications and external Git refs in addition to episode receipts. Derived
lifecycle observations written by the scan are excluded from that input fence.
Routine question defaults reconcile before observation. Damaged episode storage
retains fail-open delivery with fresh identities and no retirement authority;
source drift refuses stale publication without acknowledging the old scan.

## D40007: Capture UI observation metadata before publication

Full and initial UI producers include the canonical observation envelope. Its
date is the already-captured `brain.now`, at the same precision; source kind is
`live`, and loading metadata uses stable schema-version-1 snapshot defaults.
The publisher preserves that captured observation. Active transport progress
remains on `/api/loading`, SSE event IDs and version headers. Detached CLI JSON
keeps its cache-specific dates, age, failure and unknown-state annotations.
An unchanged complete publication therefore equals the standalone full view,
while CLI and live views keep the same top-level fields.

Disposable verification setup clears its status-commit coalescing stamp before
adding a worktree. A minute boundary can otherwise refresh root's generated
`Facts refreshed` timestamp without committing it, leaving setup dirty before
the handback under test. This fixture correction retains the pre-handback stamp
clear and every strict assertion; production hourly coalescing is unchanged.

## D28003: Keep task migration limited to explicit work and retain reconciliation history

Automatic migration imports backlog/build-order entries and open jobs with a matching,
unmerged local Git worktree without a stopped worker signal. Inbox messages, status history, and explicitly labelled decision/history sections
are observations rather than work sources. Pending inbox requests stay in their
original delivery lifecycle when their legacy task copies are dropped. The explicit record reconciliation command defaults to a read-only
plan and retains every task, requirement, source line and ledger row. It records one
private decision per node for dropped imports. Proven main delivery closes imported
job tasks with their baseline, delivering commit and changed paths; incomplete proof
keeps a visible reconciliation obligation. Shared requirement records stay active while
any linked task remains active.

Terminal machine completion/exit notices expire only after their successful inbox
delivery, or an explicit drain. Expiry is a retained lifecycle receipt, separate from
an answer, and never suppresses a question, worker failure or actionable machine hold.

Handled inbox archiving is explicit in reconciliation: only strictly answered episodes
or expired terminal notices qualify. Publish exact original lines and episode proof to
the node-local inbox archive durably before replacing the active inbox. Replays reuse
the retained episode/line marker, so an interrupted replacement cannot lose a message
or duplicate its retained copy. Unknown state stays pending. Closed jobs use retained
delivery evidence even when their worktrees have already been pruned.

Archive publication fences both captured file generations and holds the native inbox
append lock through source replacement. An appender verifies its opened inode after
locking; a replaced inode is retried or reported busy to the hook's durable queue.
The instance lock alone cannot fence nonblocking hook question delivery.

Mixed backlog sections can contain policy history as well as work. Exclude standalone
authority announcements, review-independence rules, spent allowances and resume
conditions using their explicit forms, without dropping ordinary work or manual tasks.
Cleared preview bindings are missing job evidence, never proof of abandonment.

After integration with unlocked task validation, a new unlinked backlog/ready task
fences its exact destination and requirement plus inherited node policy. It does not
scan unrelated task/requirement bodies or directories under the instance lock. Linked
and completed mutations retain the full graph, artifact and Git evidence fences.

Unlinked new task creation validates only the new record, with direct requirement
lookups. Linked additions, existing-record edits and explicit task checks retain
whole-graph validation, including qualified and cross-node return edges. This avoids repeatedly parsing every requirement
for every existing task while holding the instance lock for a single unrelated add.

For a successful new backlog/ready task whose native operation changes no ledger/status
resources, retain the native command journal and skip the unrelated status/usage
projection. Resource changes, configured managed indexes and other transitions retain
normal maintenance, including partially failed index regeneration.

## D28004: Show task workflow and downstream dependencies from one snapshot

The task board keeps five stable columns (backlog, ready, in progress, parked and done),
with dropped records available through an explicit toggle. Review/testing states remain
in progress; blocked tasks and delivery claims without proof remain parked. Filtering
by node, owner or text affects both the board and dependency canvas.

Use a directed SVG canvas with a deterministic layered layout and a compact grid for
independent tasks. Edges point from a dependency to its dependent. Node-qualified task
identities preserve cross-node links and duplicate local IDs; downstream traversal is
transitive and bounded for malformed provider cycles. Filters hide edges with missing
endpoints while retaining full-snapshot downstream context in task details. No graph
library is needed. Pan, zoom and task reading work with pointer and keyboard controls.

The existing read-only task snapshot reports dispatch holds and a ready-now flag using
the same dependency and hold checks as task selection. A ready status alone does not
promise that a task can start. Source records remain authoritative; this visualization
does not change workflow state or reconcile records.

## D28005: Prioritize active dependency chains and retain full task context

The task canvas defaults to connected active tasks, laid out left to right by dependency
depth. Completed/dropped work and unlinked tasks are available through Show all. Unlinked
tasks are listed separately and never shrink a dependency graph into an unreadable grid.
When no applicable edges exist, show an explicit no-links explanation. State colours have
text labels in a legend; blocked work has a visible marker and parked work is collapsed.
Task navigation counts exclude dropped records.

Parse title markup with `mdast-util-from-markdown`, already locked and bundled through
react-markdown, now declared directly because task presentation calls it. This adds no
new transitive or bundled package. Cards use short plain-text summaries and three-line
clamps; graph labels use one-line ellipsis. The reader retains the full source text and
original title. Branch-based task titles bypass prose identifier substitution, so a real
job name is not reduced to a generic label. Hide empty dependency facts.

Task tab selection paints immediately instead of fading from the previous selection.
Visual evidence waits for finite shell animations to settle before capture. Reconciliation
and proof of job completion remain backend responsibilities; collapsing a parked row
never changes its state or evidence.

## D40004: Bind job preview evidence to its current worktree generation

Preview receipts may opt into `scope: job` and include `worktree_generation`
from native binding authority (consumer access is superseded by D40008 below).
This permits an
owned preview without changing the node's configured preview or review URLs.
Receipts without job scope retain the existing exact node-URL policy.

A job URL must use HTTP(S) and either a loopback host (`localhost`, `127.0.0.1`,
or `::1`) on the current worktree's allocated port block, or an exact URL
declared in that job's ledger `Preview URLs` cell (a JSON string array) or
contract `## Preview URLs` section (one URL per line, optionally bulleted and
backtick-quoted). Credentials in job preview URLs are refused. Declarations
are exact URLs, not host or port wildcards.

Job receipts bind the open job key, exact head, current node binding, worktree
creation record, physical checkout and Git metadata identities, allocated ports
and current URL declarations. Validation reloads authority from disk, verifies
the checked-out branch and head against the node's home repository, and refuses
a missing, replaced or rebound worktree. Both required and supplied optional
job receipts are revalidated by the readiness policy before full verification.
A receipt still needs a rendered-page observation and three distinct matching
source comparisons for every URL; recording remains a supplied observation,
not an automated browser check.

## Private rehearsal copy and alternate writer permissions

Sampler/watcher lock writers and generated rehearsal diagnostics enforce 0600 on held inodes; rehearsal copying separates private state and private-mode sources from native cloning, writes copies through 0600 descriptors, and preserves ordinary shared modes after content fsync.

Probe instance files and detached Git administration use the same checked copy writer, including tightening existing destination inodes before truncation. Source descriptors are opened nonblocking and validated as regular files before reading, so unsupported FIFOs refuse without hanging.

## Rehearsal preparation does not probe delivery

Copied webhook bindings never authorize delivery during rehearsal preparation.
Do not seed command copies with a live probe or a fabricated successful status:
retain the captured cache, leave delivery results empty, and record
`delivery not probed (rehearsal)` in diagnostic warnings. Command execution keeps
its existing OS network boundary and strict comparison rules; a passing rehearsal
does not certify webhook delivery.

Baseline diagnostic fixtures assert the complete warning list, including the
explicit skipped-delivery notice alongside their existing baseline warnings.


## Prove read observations during isolated rehearsal

Read probes observe command telemetry through the same exact native append proof
as mutations. Full synchronous UI probes additionally attest only native cache-lock
creation and the cache producer's exact atomic payloads, private modes and file
identities. Missing or altered observations grant no write permission; operational
journal proofs and zero-residual assertions remain unchanged. UI privacy keys use
a provider-permission signature separately from transcript discovery, allowing a
frozen accounting adapter to bind the captured aggregate identity without opening
provider roots or interpreting unavailable measurements as zero.
The UI JSON cache producer shares the frozen wall clock with the rendered view,
so observation dates and ages compare without discarding those fields.

## Read Git status and diffs without refreshing the index

The shared Git helper supplies `GIT_OPTIONAL_LOCKS=0` only to status/diff child
processes, including reads with per-command configuration or explicit environments.
This prevents optional index refresh writes without changing Git writers or the
parent environment. Worktree activity, cleanup, review readiness, configuration
protection and source cleanliness continue to include untracked files. The core
revision label uses `status -uno`: it describes tracked code changes and already
excluded untracked files. Tests cover stale index metadata, an existing index
lock, explicit environment overrides, and paths with spaces, Unicode or newlines.
Git diff can refresh stale index metadata even with optional locks disabled.
Shared diff calls and the binary retention diagnostic therefore use a disposable
private index, preserving normal name-only/raw output; disabling automatic refresh
would expose stat-only changes. Copies preserve the source descriptor's mtime for
racy-clean detection. Split-index status/diff reads first flatten the copied index in a scratch
repository with no objects, refs or inherited Git configuration. The actual read
retains its original Git directory and configuration, including matching and
nonmatching conditional includes. Restoring the original index mtime after
conversion preserves racy-clean detection. Writers stay unchanged.

## Reuse term-scan observations within a command

Mechanical checks share term-scan results across nodes and the two fix passes.
File results use the absolute path, device, inode, size, nanosecond modification
and change times, mode, owner, group and link count, plus the complete folded term
set and governance context. Each lookup opens the file and checks its descriptor;
only reads whose identity remains stable are retained. Read failures are never
cached. No file observations survive the command.
Small files with identical byte hashes share decoding and matching after each
file's bytes have been read; large files retain the bounded streaming path.

Index blobs always read their complete bodies before sharing matches by byte
hash. Bounded batches of requests reduce pipe round trips without buffering
large responses or trusting object headers as proof of body readability.
Detected file changes report an incomplete scan rather than a clean result.
File inventories, index entries, paths, commit messages, refs,
governance decisions and scan errors remain current; results keep the original
labels, ordering and redaction. The cache stores match sets rather than file text.

## Keep private blob verdicts between commands

An explicit `check --fix` may publish `.brain/term-scan-cache.json` with mode
0600 through checked directory authority and generation-checked replacement.
Read commands only consume it. The rebuildable cache contains canonical Git blob
IDs and boolean verdicts, opaque hashes of policy/path bindings, and numeric file
identities; it never contains terms, labels, paths or source text. A changed word
list, scanner implementation/version, Unicode runtime or governance context
invalidates reuse. Failed verdicts are scanned again for their current labels.

A working file must still open successfully and retain its complete descriptor
identity before a clean verdict is reused. Index reuse additionally requires a
prior complete canonical body read in that object store, with unchanged readable
loose-object or pack/index storage. Alternates and replacement refs disable that
shortcut. Changed storage during reuse reports an incomplete scan; cache damage,
unavailable storage and publication races fall back to current source reads.
The complete loaded schema must contain only the allowed digest/OID keys,
boolean verdicts and bounded integer identities; any malformed record rebuilds
the artifact. Reftable storage conservatively disables persistent index reuse.
Git directory/index bindings are checked around each inventory and after use.
Fresh tagged Git inventories distinguish untracked names from stage records,
including names containing tabs/newlines. At most four inventory/file readers
overlap in copied command contexts; labels, ordering and publication remain in
the caller. Large bodies retain bounded streaming while their Git IDs are hashed.
An inventoried repository retains its classification if its Git marker disappears;
saved inventory errors and later binding failures cannot become a plain folder scan.
Regular-file checks use a freshly opened nonblocking descriptor, preventing a FIFO
replacement from blocking and avoiding a redundant pathname stat on each warm file.
Scanner identity includes its compiled implementation as well as source hashes,
so a command loaded before an upgrade cannot publish as the newly installed code.
The compiled identity includes normalized positional and keyword-only defaults;
unsupported defaults disable persistent reuse/publication while source scanning
continues normally.
Only exact immutable scalar/tuple defaults are supported. Callable, mutable,
unordered and subclass defaults remain opaque and disable persistence; keyword
binding order is preserved. The streaming hash helper selects SHA-1 inside its
constructor from a None default instead of encoding a callable as a bare symbol.
The executing scanner/cache module code is captured too, including top-level
initializers, so a global-only source upgrade also changes the loaded identity.

## D40004: Poll opted-in preview operations without relaxing call bounds

Private adapter registrations may declare `operation_deadlines` per lifecycle
operation type. These operations use one identity-bound start and read-only status
polls; unlisted operations keep the existing direct protocol. Each invocation
retains its ten-second cap, while one monotonic deadline includes all polling and
identity checks. Only a valid quiescent terminal method result can complete a
lifecycle step. Expiry, lost operation ids, crashes and binding changes preserve
pending intent without replay, rollback or release. Product drivers own durable
execution and reconciliation; the transport creates no second lifecycle engine.
The same absolute deadline reaches request encoding and subprocess creation, so
serialization and scheduling time cannot renew the remaining operation budget.

## D40006: Select per-change test consumers and keep a separate full release gate

A node can opt into a project-owned `verify_change` command for native readiness
(`ready --record`, handback, and `brain gate`). `verify` remains the complete
release-batch and nightly command. The native runner supplies the exact local
integration base and candidate head as `BRAIN_VERIFY_BASE` and `BRAIN_VERIFY_HEAD`.
It binds the readiness packet to all verification configuration, inputs, ports
and the candidate. A selected pass is change evidence and emits no full-pass fact
or adoptable full receipt. `full-run`, `brain gate --train`, `brain gate --nightly`
and the merge train always execute the complete command, bypassing both selection
mechanisms. Configure nightly execution with the complete gate; ordinary change
readiness never replaces the release batch's full run before its tag.

For this repository, set `verify_change=python3 -B tests/per_change_gate.py` and
retain the complete canonical pytest command in `verify`. The command always
runs lane coverage and the native guard before its selected unit tests, stops on
the first failure, and preserves the canonical serial tail after parallel tests.
`--plan --base <commit>` prints selection and its measured duration without
running tests. Root/shared inputs, unsupported or removed paths, unavailable Git
history, invalid source or incomplete ownership select the complete suite.
When both `verify_change` and generic verify lanes are configured, the explicit
project-owned change command owns readiness. Its packet binds the complete
verification configuration without executing or claiming generic lane evidence.
The landing train still runs `verify` on the exact staged result, so a green
one-job landing pays for one full run. Nodes must configure `verify_change` (or
affected lanes) to use this method; a node with only `verify` retains its complete
readiness fallback. Full-run and explicit full gates continue to bypass selection.

The source-derived import map is the repository's existing `tests/lanes.py` map:
relative core imports are followed transitively; test helper imports and embedded
Python are scanned; definition-time changes affect every lane's startup identity.
Compare a selective module's startup AST with its integration-base source before
narrowing its consumers. Changed or unavailable startup identity selects full.
The fixture loader imports every module, and most core modules form a strongly
connected graph. Keep these modules nonselective. Only the separately audited
`train` dispatch/call boundary can narrow module-body changes; test-file changes
also select importing helper consumers. Unknown/new dependencies cannot silently
reduce coverage: `check_lanes.py` requires complete canonical ownership and
nonselective module inputs. Selection reads source on every invocation, uses
NUL-delimited Git paths, disables rename heuristics and optional Git locks, and
includes staged, unstaged and untracked inputs. There is no time-based cache.

## Recover atomic publication from durable intended-mode records

Atomic publication persists a private intended-mode record alongside the staging file
before applying its mode and renaming. The record binds the destination name, device,
inode, size, modification timestamp and content digest to the intended mode. A writer
recovers a matching published inode before deriving the next publication's mode;
readers never repair permissions. Recovery refuses unsafe records or changed content,
retains intent on failure, and caps private targets at 0600. Active publication records
are locked, and successful publication removes its owned record only after directory
sync. Unpublished staging and records for other inodes are left for reconciliation;
recovery never promotes them. Chmod failures still abort publication and attempt
private cleanup, preserving the original failure if cleanup also fails.

## D40008: Fence task mutations by their validation dependencies

Task mutations fence records from the target node and nodes reached through
its prospective dependency, parent and successor graph. Completion artifacts
and code proof homes are selected from that same graph. Code completion fences
local main and replacement refs (including every Git symbolic-ref chain link),
packed refs, Git
configuration and ancestry metadata in both worktree and common directories.
Unrelated branch refs are excluded from this task-specific fence; other Git
publication fences retain their broader scope.

Validation still runs outside the instance writer lock. After reacquiring that
lock, publication compares fresh generations of every selected input before
saving. Relevant drift refuses without writing the prepared task. Automatic
retries are not needed to tolerate unrelated builder commits and are omitted
so genuine validation conflicts remain explicit. Requirements are read once per
validation pass, using a local snapshot protected by the same generation fence,
instead of rereading the entire requirement directory for each task row.

Repository file observations share an open directory descriptor to avoid resolving
the same root for every file. Each file still opens nonblocking and checks its own
descriptor; a changed or unavailable root after observation makes the scan incomplete.
The directory authority is scoped to that observation and always closed/reset.

## D40008: Expose a read-only native job preview receipt skeleton

Consumers use `brain preview-receipt-skeleton <node> --branch <job> --json`.
This supersedes D40004's instruction to use the Python
`job_preview_generation(instance, node, job_row, head)` helper. Product workers
must not import core or reconstruct its generation hash.

The CLI prints the recorder's version-1 job receipt shape with current node,
open job key, branch, exact head, native worktree generation, descriptive live
binding metadata and an allocated loopback URL. Observation timestamp, renderer,
render result/artifact and three source comparisons remain null until supplied
from actual observation. An unfilled skeleton cannot pass recorder validation.
See `docs/verification-receipts.md` for the complete output contract.

The accessor reads without registration, instance locking or telemetry writes,
rechecks authority before emitting JSON, and refuses unavailable/stale bindings.
It changes neither global preview/review URLs nor recording/readiness policy.
The recorder still validates the supplied receipt against live authority.

A working file must still open successfully and retain its complete descriptor
identity before a clean verdict is reused. Index reuse additionally requires a
prior complete canonical body read in that object store, with unchanged readable
loose-object or pack/index storage. Alternates and replacement refs disable that
shortcut. Changed storage during reuse reports an incomplete scan; cache damage,
unavailable storage and publication races fall back to current source reads.
The complete loaded schema must contain only the allowed digest/OID keys,
boolean verdicts and bounded integer identities; any malformed record rebuilds
the artifact. Reftable storage conservatively disables persistent index reuse.
Git directory/index bindings are checked around each inventory and after use.
Fresh tagged Git inventories distinguish untracked names from stage records,
including names containing tabs/newlines. At most four inventory/file readers
overlap in copied command contexts; labels, ordering and publication remain in
the caller. Large bodies retain bounded streaming while their Git IDs are hashed.
An inventoried repository retains its classification if its Git marker disappears;
saved inventory errors and later binding failures cannot become a plain folder scan.
Regular-file checks use a freshly opened nonblocking descriptor, preventing a FIFO
replacement from blocking and avoiding a redundant pathname stat on each warm file.
Scanner identity includes its compiled implementation as well as source hashes,
so a command loaded before an upgrade cannot publish as the newly installed code.
The compiled identity includes normalized positional and keyword-only defaults;
unsupported defaults disable persistent reuse/publication while source scanning
continues normally.
Only exact immutable scalar/tuple defaults are supported. Callable, mutable,
unordered and subclass defaults remain opaque and disable persistence; keyword
binding order is preserved. The streaming hash helper selects SHA-1 inside its
constructor from a None default instead of encoding a callable as a bare symbol.
Read commands share branch tips, commit dates and linked-worktree HEADs from one
ref/worktree enumeration per repository. Ancestry queries, commit counts and
clean-title checks share one commit graph per base, with roots pinned to the
enumerated commit IDs even if refs move during the command. Unfinished-title findings retain the original Git
log order. Configured signature display retains the native date-log read. These
derived reads expire at the end of the command, without a time
limit or persistent authority. Failed or malformed batches fall back to the
individual Git reads, including local job-state observations. Read-only `ready`
uses this scope; readiness recording and other write paths keep their existing
reads and writes.

## Reduce transcript candidate and timestamp overhead

Usage parsing uses one compiled byte-pattern search for the existing candidate
markers instead of repeatedly scanning large tool-output lines. Candidate JSON
parsing and accounting rules stay unchanged. UTC timestamp conversion reuses
pure Gregorian month arithmetic in a bounded cache; the key contains only year
and month. Day, hour, minute and second overflow retains calendar.timegm's
normalization, and fractional seconds retain their previous interpretation.
No transcript observation or accounting result is retained by this arithmetic
cache, and the aggregate schema does not change.

## Retain guard blob verdicts for detached dashboard fills

A full detached dashboard fill keeps its own term verdicts under the private
UI cache directory. It uses the scanner's existing compiled/source identity,
policy, descriptor, object-storage and Git-binding checks. Every fill still
reads inventories, paths, refs and commit messages; a changed source, unsafe
storage or damaged cache falls back to scanning. The ordinary check cache and
its explicit publication policy remain separate. A fill whose optional storage
was refused does not publish this cache.

## Preserve generation-aware watchers and revalidate warm file reads

Integration keeps one generation-aware WatchGroups definition. Both the
persistent and per-command term-file shortcuts recheck the opened descriptor
after cache lookup; a concurrent change reports an incomplete scan. Warm
lookups retain the same descriptor-stability obligation as complete body reads.

The compiled candidate filter described above is removed: full-parser samples
showed a regression despite an isolated candidate-search win. Retain literal
candidate checks and the independent Gregorian-month arithmetic optimization.

## Select one detached lifecycle record before copying

Read-only round-usage projection selects its job record from the command's
captured lifecycle ledger before making a defensive deep copy. Unrelated
receipts are not copied for each job. The selected nested data remains detached,
mutations cannot reach the captured ledger, and a new observation reads current
source state. Mutating projections retain whole-ledger defensive copies.

## Bound independent dashboard worktree reads

Full worktree projection runs at most four independent readers concurrently.
Each reader inherits the command's observation, Git and readiness contexts;
shared refs are resolved before launch. Results retain sorted branch order,
closed work remains metadata-only, and any source failure rejects the complete
result instead of publishing a partial list.

Projected restart advice also detaches only its selected usage record. The
projection carries the original ledger entry count separately, so selecting a
small record cannot bypass the global capacity bound. Explicit restart writes
retain the ordinary whole-ledger lock and publication path.

## Retain private source-bound session discovery for dashboard rebuilds

Dashboard fills retain canonical session-discovery digests beside their usage
aggregates. Metadata reuse requires the current parser version and complete
file identity, and opens the current source descriptor before accepting a UI
cache hit. A changed descriptor is incomplete; malformed records and older
versions trigger native source discovery. Records contain opaque identifiers,
location digests and numeric identities only. Current allowed/forbidden source
roots are checked independently on each discovery pass.

Disabling optional dashboard storage now applies to nested usage and discovery
publication as well as the final snapshot. Nested scopes cannot re-enable
publication denied by an outer read-only scope. Ordinary accounting storage
and explicit accounting commands retain their existing behavior.


## 2026-10-27: Open the last complete dashboard reading while its replacement builds

An authenticated browser startup may seed its first response from the existing private full JSON snapshot only after the existing code, instance, scan selection, word policy and allowed-source privacy binding validates. Explicitly partial/unavailable-format records fall back to the file-only startup. The displayed capture timestamp and age stay attached to that reading; a background rebuild replaces it only after usage and metadata are complete. Cached deliveries revalidate the stored snapshot and current privacy policy. Browser full rebuilds persist a complete snapshot under their original privacy fence. A changed core version intentionally invalidates the old snapshot, so installation needs one complete fill under the new version before this warm startup path is available.

Concurrent explicit JSON waiters first attempt the fill lock without blocking. A follower joins the existing owner, then consumes its newly published validated complete generation. It never queues another rebuild; failed or unpublished owners fail closed for the follower. The source-bound input-index work is parked separately while this urgent 7.3a change is reviewed.

## 2026-10-27: Allow overlapping handback attempt creation

Creating a handback attempt fingerprints its refs and verification inputs without
holding the instance writer lock. It fences the ledger, policy, local bindings,
Git metadata and selected inputs across that read, then writes the replacement
attempt under the retaken lock. The previous attempt is an output being replaced,
not a binding input: another enqueue may replace it during the unlocked read
without invalidating creation. Both queue requests remain durable; the last
attempt published owns the current job, and an older queued attempt still fails
the runner's identity check. Completion publication continues to fence the exact
attempt, and lease ownership and PID birth-stamp checks remain unchanged.

## D40010: Queue low-risk metadata and unchecked messages before instance locking

Inbox additions, a narrow allowlist of task metadata patches, and unchecked
handback enqueue requests use immutable instance-level request files. A single
O_APPEND index establishes order by byte position. The existing handback queue
module owns this transport as well as the unchanged checked foreground runner.
A detached, independently leased applier publishes one applied/rejected receipt
per payload-bound request ID. Task replay compares prepared before/after records;
inbox replay uses retained identity markers, and handback replay retains its
publication, attempt and failure-event identity. Unexplained drift blocks replay.

Acceptance is distinct from application. Dependent readers explicitly wait for
the request receipt. Authority/status/dependency transitions, checked verification
and item mutations remain synchronous. An installed service, item intents and a
general multi-file crash-recovery matrix are separate work.

## D40009: Bound live accounting to command and job observations

Each command captures transcript discovery once. A job aggregate's revision
contains only files selected by the existing permission-checked job attribution,
plus its policy, ledger and hand-back inputs. Unrelated transcript appends cannot
invalidate every job aggregate. Live accounting and runaway checks select fresh
observed worker threads or unended jobs updated within 24 hours (start/update
clocks and their own receipt/contract/attempt modification times). Stale rows keep
recorded final usage where available; unavailable live observations remain unknown.
The memo scope names accounting reads only; mutation guards remain fresh. Parser
changes and moving refresh computation outside the writer lock are separate work.

## 2026-10-27: Batch direct wake observations inside each watcher scan

The watcher entry point owns a command read snapshot even when called directly
by a wake drain, pending-item revalidation or delivery snapshot. This shares
transcript discovery, job attribution and repeated Git display reads across the
jobs in that scan. Each standalone call gets a fresh scope; existing due/watch
callers retain their enclosing command scope. Communication reconciliation runs
before the snapshot. Source-signature and receipt-generation checks still run
before publication, so cached reads cannot authorize a changed source state.
The scope stores no cross-command watcher output or delivery authority.

## 2026-10-27: Reuse observations inside a fenced status computation

An unlocked status computation shares repeated ledger and projection reads only
inside its build callback. Input discovery and publication validation remain
outside that scope. Git commands retain their ordinary fresh execution: changes
to global or included Git configuration must remain observable on repeated
queries. Existing semantic revalidation and final generation checks remain
mandatory; a changed answer refuses publication. No cache survives a computation
or grants writer, readiness or completion authority.

Registered worktree tips and merged-branch names are queried in two batches.
The merged-name answer preserves ancestry booleans across unrelated integration
commits; malformed queries fall back to the individual checks. The explicit
refresh prepares its comparison snapshot and replacement text in one fenced
computation, then records comparison and publishes under the writer lock.

## 2026-10-27: Retain routine inbox notices outside open-ask counts

Ordinary inbox reads archive answered episodes and routine notices older than
24 hours, or notices with a matching delivery receipt from their owning node.
Ancestor delivery receipts cannot close a child's notices. Only explicit machine
completion, Docker report, FYI and landing formats qualify; question markers,
question punctuation, actionable wording and unknown formats remain open.
Undated Docker reports age from their Git introduction or uncommitted source
mtime. Other undated lines require an explicit handling receipt.

Archival reuses the native append lock and source/archive generation fences.
It publishes exact retained source bytes and episode metadata before replacing
the active inbox, persists episode identities before removal, and commits all
successfully published inbox/archive pairs together once per batch, including
when a later node refuses publication. Terminal delivery expiry is scoped to
the owning reporter; an ancestor read cannot consume a child notice. Weekly sweeps and dashboard
reads do not archive; dashboard inbox counts include only open asks.

A coordinator can inspect IDs with `brain inbox <node> --json`, then record an
answer with `brain answer <item-id> "<answer>"` or record completed action with
`brain inbox-seen <node> <item-id> "<action taken>"`. The latter accepts only that
node's own current episode, retains the action receipt and archives handled
lines immediately. Reading an inbox without `--new` does not acknowledge FYIs.

## 2026-10-27: Batch event coverage and refuse malformed writes before admission

Resource coverage maps opaque scope identities to one discovered node list per
scan, independent of the number of changed resources. Resource-list construction
continues to sort nodes once per batch. Hook records retain individual durable
journal appends; their derived replay index is saved once before publishing the
outbox cursor. A crash before cursor publication replays the journal acknowledgements
without duplicating hooks.

CLI proposal acceptance checks reviewer names before opening the writer lock or
event operation. Names match case-insensitively and persist the configured spelling;
errors list the operator and non-revoked principals. Task add/set likewise parse
and validate argument shape before admission. Handlers repeat validation, including
current authority and state-dependent checks, in their existing mutation path.

## 2026-10-27: Fence task and handback inputs without unrelated Git branches

An unlinked task update validates and fences its own record, requirement, active
ledger and completion proof. Linked updates retain the full dependency graph
check, but scalar validation and completion proof concern the changed task and
its dependencies. Unrelated completed tasks do not introduce Git refs into that
write's read set. The same-task generation must still match after computation;
a concurrent replacement refuses publication and preserves the other writer's
bytes.

Git generation fences accept explicit refs and follow their symbolic targets.
Task completion watches intended main and replacement refs without checkout
HEAD. A handback watches its job branch, cached integration refs, its checkout's
HEAD and verify inputs. Generated status watches the branches it renders. An
unrelated worker commit cannot invalidate those observations; changes to a
selected ref, policy, task, requirement or owned attempt still refuse. Packed
ref storage, replacement refs and repository configuration remain fenced.

The event batching implementation includes its optional derived-index persistence
argument and shared sorted resource-scope lookup on the current integration base;
neither relies on a previous status-refresh implementation.

## 2026-10-27: Finish dashboard accounting without restarting a bounded file walk

Machine-wide accounting defaults to a complete pass. Explicitly budgeted callers
retain partial results; the server uses its existing resumable file queue for
background slices. Unchanged in-memory aggregates validate the current source
descriptor and permissions before returning, without opening the usage database
or reading transcript bytes. Background slices share one database snapshot.
The filling marker names the outstanding token-file counts while accounting is
partial, and disappears only when the displayed reading is complete.

## 2026-10-27: Landing completes task IDs and reconciliation commits one batch

Native land/train publication and verified job closure complete the exact task
IDs bound to delivered jobs as `done`, with the integration commit on local main
as evidence. Retain each job's changed paths, task baseline, scope validation,
attempt identity and exact verification requirements. Incomplete proof stays
parked. Existing landing facts make eligible blocked dependents ready without
dispatching work or removing holds. Coordinators record waits through task
add/set `depends_on`, whose ID, ownership and cycle checks remain authoritative.

Reconcile-records apply owns one locked commit batch on its instance. Publication,
synchronous landing-event consumers and dependent updates share that commit;
journal facts and pending intents remain durable for interruption recovery.
Nested commits defer their paths rather than staging unrelated work. Read-only
planning does not enter the batch. Board and canvas match/attempt totals exclude
dropped records until the explicit Show dropped toggle includes them.

## 2026-10-27: Read subscription meters independently of dashboard snapshots

Initial, cached JSON and periodic dashboard readings overlay the machine usage
cache through the same subscription reader as the CLI. Current subscription
permissions still filter every row, and display or host-binding edits do not
invalidate a reading bound to the same runtime folder. Window observations keep
their original timestamps and sources. A provider's explicit absence of a
five-hour limit remains distinct from unknown usage and is displayed as None.

With no explicit subscription declarations, Claude discovery includes every
credential-bearing configuration folder under the user's `.claude*` directories
alongside the configured default. Discovery checks credential-file or Keychain
item existence only; it never requests credential values. Explicit declarations
remain authoritative and discovery deduplicates folder aliases.

`spend --add-source RUNTIME=FOLDER` appends an already permitted subscription
folder to the local source bindings without editing runtime instruction files.
It owns a short local lock and emits no instance event or timing records. Spend
continues to count the allowed subscription folders, so registering an already
allowed folder cannot double-count it or grant access to another subscription.
Fixture-mode machine service installation and removal refuse before invoking
an OS service manager; service tests explicitly intercept the production calls.
## 2026-10-27: Distinguish initial gate admission from running ownership

Node gates keep their stable flock inode while waiting for machine admission.
Holder metadata starts conservatively busy, records `waiting-for-admission`
during the first native or inherited `heavy` admission, then becomes `running`
before the admitted caller can continue. The upgrade idle guard ignores only a
waiting phase whose gate owner and admission reporter have matching live process
birth stamps. Missing, malformed, legacy or uncertain metadata stays busy when
the flock is held. The inherited gate node is the default for `heavy`.

Phase transitions use the instance writer lock, shared with upgrade, but release
it before admission waits. Running is sticky for the gate lifetime: later lane
waits and overlapping inherited admissions cannot hide previously started work.
Native verification carries gate context explicitly across its admission calls.
No phase transition unlocks, replaces or removes the gate file, and surviving
children retain the inherited descriptor after a parent exits.

The idle guard and the next gate acquisition report stale holder PIDs. An
unlocked stale file is idle and reused in place; a stale PID with a still-held
descriptor remains busy because a surviving child may own the gate. Controlled
process barriers cover admission, upgrade refusal, same-node competitors, inode
continuity, failed admission and dead reporters without generating machine load.
Short writes also release a newly acquired physical instance lock when native
readiness has suspended an outer CLI scope with nonzero logical lock depth.
## Scope preview evidence to the exact rendered-input diff

A bound preview requires an exact-head observation when the integration diff
changes rendered inputs, or when that classification is uncertain. A node may
explicitly declare `ui_globs` (JSON strings or comma-separated globs); otherwise
`ui`/`rendered` verify lanes supply inputs, strict inputs and transitive lane
dependencies. Unsupported or absent declarations, failed maps and unreadable
Git data retain the receipt requirement. Rename endpoints and deleted inputs
are both classified. Supplied receipts retain all existing validation even for
backend-only changes. Gate packets record the base, head, changed paths,
declarations and applicability, and readiness recomputes the decision rather
than accepting a cached exemption. Classification changes during verification
refuse publication. A retained train base must precede the current integration
base, so it cannot narrow away rendered changes.
Generated classifiers use the verifier's root, base, head and scope environment;
new packets retain the existing lane-environment format for subsequent reads.
Legacy generated-map packets without that environment require a receipt.
Policy signatures enclose the live load as well as classification.
Full-receipt adoption defers preview validation until the original packet's
environment is available; independent review and integration preflight stay first.

## 2026-10-27: Bound nested gate-proof fixture initialization

The enclosing-gate scenario initializes its nested instances through the existing
isolated in-process setup helper and pristine initialized fixture templates.
Each instance still gets independent files and fresh module state.
The asserted gate commands, native lane execution, descriptor
checks and environment restoration continue through their original paths. This
removes repeated setup CLI startup from the existing 30-second subprocess bound.

## 2026-10-28: Report only abandoned gate holders

An in-process gate owner marks its holder row `released` when it closes its
descriptor, so the next acquisition and the upgrade idle guard report a stale
holder only when a dead holder never marked its close. Released is never idle
while the flock is held: a surviving child still keeps the gate busy, and a
dead owner with a held descriptor is still reported. The mark is best-effort
and takes no instance lock, so unwinding never waits on or fails over it.
Gate phase writes take the instance lock by root, as gate paths do.
## 2026-10-27: Reuse ledger parsing and batch terminal handback display reads

Ledger observations still read the current file on every call. A bounded cache
stores only immutable parsed rows keyed by the complete text and default columns;
each consumer receives fresh dictionaries. Same-size edits, restored timestamps,
legacy columns and caller mutations cannot reuse stale ledger state.

After a handback publishes, its generated status projection uses the existing
command read snapshot within the existing unlocked source fence. Repeated display
queries share one observation. The snapshot ends before the fence is revalidated
and before generated status or journal publication; attempt ownership, readiness
and publication checks outside this display scope remain fresh.
## 2026-10-27: Bound write-display work without weakening generation fences

Scoped Git fences inspect loose ref headers before asking Git to resolve symbolic
chains. Ordinary and missing branch refs need no subprocess per branch; symbolic
targets, linked metadata, packed refs and reftable storage remain in the generation
fence. No layout or generation result is retained across publication rechecks.

Terminal write projections share one command read snapshot for task updates as
well as handbacks. Readiness and mutation validation remain outside that scope.
Machine telemetry is sampled concurrently with the generated view while the writer
lock is released, then rendered from that same observation. It remains display-only.
Task reverse edges are constructed in one pass, preserving record order and one
entry per dependent even when a dependency is repeated.

## 2026-10-27: Cache generated ledger facts by independent semantic inputs

Generated status keeps bounded, content-addressed string sections in the existing
private derived database. Ledger records, local worktree/ref observations, worker
projections, merge baseline, metadata mode, timezone and privacy terms bind each
section. Input dictionaries retain their order. Only changed sections render
again; missing or damaged storage falls back to fresh computation. Cache entries
are bound to implementation bytes and contain no raw input records. Two bounded
generations accommodate alternating merge baselines within the same display
context; admission telemetry that is not rendered does not invalidate strings.

Tasks, inbox requests, time thresholds, reminder lifecycle and readiness are
observed on every command. No cached status section authorizes an action or skips
the existing unlocked publication fence. Finished-job display uses a linear
ordered lookup, retaining duplicate ledger keys rather than repeatedly hashing
every ledger row for every due item.

## 2026-10-28: Fence selected status projections as one batch

Writers prepare every selected node's generated status view and text in one
unlocked window, fenced on the union of their source records and integration
refs. A concurrent change retries the complete batch for scoped writers; other
writers refuse it. All prepared views then publish under the retained writer
lock, so rendering a later node cannot cause an earlier stale view to overwrite
a concurrent writer's journal evidence.
## 2026-10-27: Publish an explicit refresh from one maintenance preparation

A normal explicit refresh defers its projection to the enclosing status
operation's maintenance step, after reconciliation and index maintenance. The
selected node consumes its previous merged set exactly once; other maintained
nodes retain their existing merge baselines. One unlocked, fenced preparation
produces both the comparison snapshot and replacement text. Comparison retains
merged_before; only the selected file-source publication consumes merged. The comparison is
recorded under the writer lock before any prepared status is published. Input
drift refuses publication and marks maintenance unsuccessful. Direct calls and
archived-node refreshes retain their existing immediate path. No preparation or
read observation survives the operation, and event-source views still come from
committed replay.

## 2026-10-28: Keep handback timeout evidence separate from enqueue race repair

The inherited HandbackLifecycle WIP uses a 30-second process budget and a
pipe-bound foreign helper. Rechecking the saved 12/20 baseline found seven
subprocess timeouts and one child-readiness timeout, with no unlocked-state
fencing failure. The concurrent-attempt creation repair is already in main
(`572f305e`); the WIP does not constitute another queue-race repair. Preserve
the outcome assertions and treat the timeout change as provisional until the
rebased candidate passes the required admitted runs and independent review.
Old-head run counts do not establish the result on the rebased candidate.
