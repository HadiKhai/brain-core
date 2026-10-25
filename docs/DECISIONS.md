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
