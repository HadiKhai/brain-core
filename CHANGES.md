# Changes, for the agents that run a Brain

Newest first. Each entry says what changed in rules, skills and commands, and what to do after
upgrading. `brain changes <node>` shows the entries this node's coordinator hasn't read yet, and
`brain due <node>` lists them until `brain changes <node> --seen`. Entry headings are
`## <date>: <title>`; once released, a heading never changes.

## 2026-10-25: Rehearsal is an optional diagnostic

For agents: Ready, hand-back and local train landing no longer require passing
rehearsal evidence, including for legacy nodes with `rehearse=required`. Old
hand-back reasons for that specific gate are ignored; all other refusals remain.
Review briefs and skills no longer instruct agents to start rehearsal automatically.
`brain rehearse` remains an explicit diagnostic with its sandbox, raw failure
reports and exact candidate/main/installed-core evidence checks intact. Current
passing evidence can still accompany a ready packet.

After upgrading: No migration or waiver is needed for legacy rehearsal settings.
Use ordinary review, verify, readiness and landing checks; rehearsal never
substitutes for them. Run it only when explicitly requested, with native admission,
and preserve its actual report even when it fails or becomes stale.

## 2026-10-25: Release 3

For agents:
- The dashboard opens with real file-backed data and a filling-in notice before
  slow repository checks finish. The notice stays until complete data arrives;
  failed readings retain useful data and can recover on refresh. Passive startup
  avoids lifecycle writes and blocking history reads.
- Dashboard reuse checks authenticated server identity. Legacy and unrelated
  listeners are retained rather than stopped from a guessed working directory.
- Default watcher wakes focus on questions, worker results and decisions.
  Bookkeeping stays in `brain due`. Unknown or conflicting queued question
  metadata is preserved for delivery instead of being discarded.
- Job reminder identities follow durable job and attempt episodes, so changing
  hand-back wording does not repeat merged-job reminders. Usage reset offsets
  are evaluated at the reset instant. Docker reports remain informational and
  wait for the configured leftover age.
- Explicit ancestor model settings inherit to descendants without changing
  runtime-selection policy. Nearest model settings and local or CLI overrides
  retain precedence, and inheritance stops at ownership boundaries.
- `brain gate-lock` preserves command arguments, exit status and signals, with
  validated descriptor forwarding through native verification. In this release
  its lock is still instance-wide; node-local locking is a later fix.
- Coordinators drain absorbed host-queue messages at turn start. Closed-thread
  settlement requires a fresh host attestation export.

After upgrading:
- No data migration is needed. Run `brain ui --open` to reuse or replace a server
  whose identity is verified; stop a legacy server in its own terminal first.
- Review ancestor model settings and drain coordinator queues normally. Existing
  worktree cleanup retains unsafe or uncertain cases; automatic submodule
  worktree removal is not included.
- This release contains the fourteen local commits through `4ad1978`, plus this
  changelog only. It reuses that code's full verification: 1,116 passed, one
  skipped, 298.865 seconds through owned cleanup.
- Fixes 1–12 belong to release-4. In particular, optional rehearsal, node-local
  gate locks and the new restart/review enforcement are not included here.
  Existing rehearsal requirements and other readiness checks remain unchanged.


## 2026-10-25: Inherit node model routing without changing runtime selection

For agents: Model routing reads explicit ancestor node settings, nearest first,
stopping at owner boundaries. Runtime selection keeps its existing local and
instance precedence; ancestor model settings do not activate a different runtime
policy. Models are resolved for that runtime before model-specific quota checks.
Local model overrides and CLI flags still win, and no settings are copied into
node files. Worker and review briefs use the same model lookup.

After upgrading: No migration. Review explicit ancestor model routing when
choosing models for descendant jobs. Worktree cleanup behavior is unchanged.

## 2026-10-25: Decision wakes and shared gate lock

For agents: Default sampler wakes carry only marked questions and worker results,
stalled jobs, overdue blockers/questions and existing restart/rotation decisions.
Bookkeeping remains visible in `brain due`; explicit watcher kinds retain opt-in
delivery, including durable retries and rerouting. Identified old bookkeeping
receipts no longer bypass the default filter; unknown question metadata remains
fail-open. The additional job-restart wake feature is parked and not included;
existing due usage advice, budgets and restart/rebrief behavior are unchanged.
Unknown queued kinds and contradictory identity/kind pairs retain their original
metadata for fail-open opaque delivery; new unknown sampler kinds stay excluded.

`brain gate-lock [--label LABEL] -- CMD [ARGS...]` serializes commands through
one instance lock and preserves command arguments, signals and exit status.
The native verify launcher forwards the validated gate descriptor alongside its
bootstrap descriptor. Exec restores Python-ignored child signals, including SIGPIPE.

After upgrading: Wrap each complete full gate in `brain gate-lock --label
"component verify" -- ...`, retaining machine admission. Forward its validated FD
at other Python subprocess boundaries; native verify does this automatically.
Keep existing turn protection until every route is installed and confirmed. Do
not remove the lock file. Coordinators drain absorbed host queue messages at each
turn's start.


## 2026-10-24: Open the dashboard before slow repository checks finish

For agents:
- The dashboard first serves real node and job files with a filling-in marker,
  then publishes the complete reading. Refreshing and failed readings show their age.
- The marker uses the dashboard's loading notice and stays visible until the
  complete reading arrives, including when an idle tab closes its live stream.
  Failed refreshes keep the partial reading available and explain the delay.
- Repeated read-only Git queries share one build's results. Explicit checks and
  later builds still read Git again.
- `brain ui --open` reuses a matching local server or replaces an identified old
  dashboard through its authenticated identity on the requested port. A legacy
  dashboard without that identity and any unrelated listener are left running; the
  command reports the busy port instead of silently choosing another one.

After upgrading:
- Run `brain ui --open` again to replace an older dashboard. No setup is needed.
- If a legacy dashboard has no identity record, stop it in its own terminal first;
  a working directory or process command cannot prove which instance it serves.
## 2026-10-24: Stable job reminder identities and quiet Docker reports

Merged-ledger and lesson-outcome needs ignore hand-back wording. Job advice latches each
condition until a successful rebrief; live counters remain display-only and advice never
wakes. Docker inbox reports require stopped containers, unknown ownership and age beyond
`docker_leftover_timeout_seconds` (default/minimum 1800); reports remain informational.
After upgrading: no action required.

## 2026-10-24: M7: Upgrade around idle retained jobs and worktrees

rehearsal evidence waived (coordinator instruction, 2026-10-24)

For agents: Explicit and automatic core upgrades refuse live heavy/verify admission
holders and held repository train locks, plus unreadable inventory. Parked or blocked
jobs and retained worktrees no longer prevent upgrades; a one-line notice counts
idle open jobs/worktrees. Dead or recycled-PID admission leases are idle and remain
untouched.

Documentation: Coordinator and home-job settlement instructions now require a fresh version 1
`--attestation-file <export.json>` after closing the job and confirming each host thread settled.

After upgrading: No migration. Retry after in-flight work stops; idle jobs and
worktrees can stay in place. Admission and lock ownership rules are unchanged.

## 2026-10-24: Namespace reserved worktree ports in child environments

rehearsal evidence waived (release 1 waiver pattern; rehearse sandbox self-check refuses)

For agents:
- Verify, worktree setup and train commands receive reserved ports as `BRAIN_PORT`,
  `BRAIN_PORTS` and `BRAIN_PORT_LAST`. Raw `PORT`, `PORTS` and `PORT_LAST` are no
  longer added by default. Port reservations and allocation are unchanged.
- Set `verify_export_port: true` on a node to also export the raw names. Existing
  operator environment values, including an empty value, always pass through untouched.

After upgrading:
- A node whose scripts read `PORT`, `PORTS` or `PORT_LAST` must either set
  `verify_export_port=true` with `brain set`, or switch to the `BRAIN_PORT` names.
- After upgrading, verification packets from before this release must be re-verified.
  Effective raw-port values and the export policy now bind both full and lane
  verification reuse.

## 2026-10-24: M6: Inspect installed core without fetching

rehearsal evidence waived (release 1 waiver pattern; rehearse sandbox self-check refuses)

For agents: brain version reports the installed SHA/date, named cached origin ref, local behind count or unknown/diverged ancestry, and cache age. Existing update notices remain intact.

After upgrading: No migration. Use --ref to choose an origin cache ref. Fresh remote discovery remains outside this command.

## 2026-10-24: B11: Briefs explicitly omit commit attribution

rehearsal evidence waived (release 1 waiver pattern; rehearse sandbox self-check refuses)

For agents: Worker, import, small-job and specialist briefs supply no co-author trailer for any selected runtime/model, including explicit overrides and unknown runtimes.

After upgrading: No migration or historical rewriting. Configurable attribution identities remain future scope.

## 2026-10-24: M7: Refuse explicit upgrades while jobs are open

rehearsal evidence waived (release 1 waiver pattern; rehearse sandbox self-check refuses)

For agents: brain upgrade checks job/worktree inventories before changing core and refuses unreadable inventory as busy. The refusal lists affected jobs and locally cached change entries, with an idle retry instruction.

After upgrading: Finish jobs and release their worktrees before retrying. Idle upgrades retain their existing behavior. Deferred upgrades and cross-machine migration remain outside this change.

## 2026-10-24: M9: Apply an explicit settings batch once

rehearsal evidence waived (release 1 waiver pattern; rehearse sandbox self-check refuses)

For agents: brain settings batch accepts set and schedule-add operations, validates the complete list before publication, keeps machine-local settings private, and commits only named shared paths once. Unrelated staged work is preserved.

After upgrading: No migration; single commands remain available. Dirty target paths are refused. Interrupted publication retains a private pending/failure packet for coordinator recovery; automatic rollback and hourly squashing remain outside scope.

## 2026-10-24: P18: Charge brief inputs by provenance

rehearsal evidence waived (release 1 waiver pattern; rehearse sandbox self-check refuses)

For agents: Brief allowances charge task and embedded context UTF-8 bytes captured before assembly. Fixed generated instructions are excluded; both input and total bytes are reported, and diagnostics name coordinator-controlled sections.

After upgrading: No migration. brief_max_kb still warns above the configured allowance and refuses at twice it. A separate absolute total ceiling of 256 KiB applies. Safety text is never silently truncated; automatic context summarization remains future scope.

## 2026-10-24: Release 2: Known in this release

For agents:
- upgrade in-flight check does not see native rehearsal or ownerless wrapper locks (no owner evidence)
- brain land remains parked; use the approved manual landing path.
- heavy-run remains parked (2 P1 + P2; redesign deferred).
- rehearse EPERM atexit kill remains parked (P1).
- rehearse harness linked-external-observation refusal remains parked (harness-r2).
- P6 cancellation is deferred.
- Native rehearsal remains blocked by the sandbox self-check refusal
  (rehearse.py:877).
- Clone comparison retains the harness HOME identity gap for the usage-reading
  probe; it does not establish equivalent usage-reading observations.
- test: cross-process limit test uses a barrier
  Plain-branch review inventory assertions use the isolated fixture environment.

After upgrading:
- These parked jobs, deferrals and evidence gaps remain open. Release 2 follows
  the approved rehearsal waiver and manual landing path.

## 2026-10-24: B5 Reviewed publication policy at settings and instruction boundaries

rehearsal evidence waived (coordinator decision A, 2026-10-24); direct admitted full verify passed

For agents: `merge=pr` refuses a conflicting or unmapped current reviewed Limits snapshot,
quoting its rule/source. Briefs and `brain policy` distinguish allow/deny/unknown; merge
selection grants no push/PR consent. Explicit policy is supplied review evidence, not a
natural-language authorization engine.
Limits stop at the nearest sealed node, including that node's own rules. Empty PR policy
also refuses; parent rules beyond an ownership boundary never appear in generated output.

After upgrading: coordinators map current limits in `publication-policy.json` before PR
mode or outward steps. Legacy ambiguous limits require their decision. Arbitrary external
commands and a general action guard remain outside this slice.

## 2026-10-24: B6 Complete supplied rules-snapshot activation

rehearsal evidence waived (coordinator decision A, 2026-10-24); direct admitted full verify passed

For agents: one protected due item follows each pending rules snapshot. Explicit
`brain proposals <node> --approve-all --review-manifest <file>` atomically activates its
complete independently reviewed rules with attribution/provenance in native instructions;
changed/partial/revoked/self-approved evidence refuses without partial writes.
A valid explicit legacy rejection resolves its activation reminder without activating rules;
reviewed-but-unactivated snapshots still require the activation decision.

After upgrading: legacy prose stays pending until the coordinator prepares the documented
rules-only JSON snapshot and complete manifest. Review records are supplied evidence,
never authenticated identity. No automatic import activation; bulk roles/items and future
imports remain outside this rules-only slice.

## 2026-10-24: P10 Exact-candidate land-check receipts

rehearsal evidence waived (coordinator decision A, 2026-10-24); direct admitted full verify passed

For agents: configured `land_check` runs through one reusable explicit candidate check,
retains passing/failing job-ledger receipts, and blocks supported landing/push instructions
when evidence is absent, failed or stale. Local trains check their exact integration tip.
The supported push-step command only prints a SHA-pinned command; it never pushes.

After upgrading: configure `land_check=<command>` where needed and use `brain land-check`
before each supported outward step. Missing settings add no check or permission. Receipts
do not establish readiness or consent; arbitrary external pushes and host publication
enforcement remain outside core's supported-path boundary.
## 2026-10-24: M8 answer inbox request episodes and report weekly age

rehearsal evidence waived (release 1 waiver pattern; rehearse sandbox self-check refuses)

For agents:
- Inbox output groups stable request IDs and age by node. `brain answer <id> "<text>"`
  records an answer to one current episode, retaining its original source and history.
- `brain inbox --weekly` reports unanswered requests at least seven days old without
  discarding them. `--new` remains reporting acknowledgement, separate from answers.

After upgrading:
- No migration is required. Answer receipts are bounded machine-local state under
  `.brain/wake/`; no new host schedule or automatic age-based resolution is installed.

## 2026-10-24: P4 acknowledge a specific parked handback episode

rehearsal evidence waived (release 1 waiver pattern; rehearse sandbox self-check refuses)

For agents:
- `brain due --ack <id> --reason "<text>" --resume "<condition>" [--until <time>]`
  handles one eligible blocked handback. Changed episodes and expired resume times
  reappear; unanswered human questions, failures and restart needs stay visible.
- `brain due --handled` and generated status retain the parked state and handling reason.

After upgrading:
- Existing handbacks remain readable; no migration or automatic resume execution.
  Receipts are local and never replace the underlying worker signal.

## 2026-10-24: P14 defer eligible reminders until absolute expiry

rehearsal evidence waived (release 1 waiver pattern; rehearse sandbox self-check refuses)

For agents:
- `brain due --defer <id> <absolute-until> --reason "<text>"` defers a missing-preview
  episode. Changed needs and expiry rearm it; protected questions and failures cannot
  be deferred. A `preview=none` setting and the preview requirement remain intact.

After upgrading:
- No migration is required. Deferrals use bounded local receipts and never edit
  readiness, verification or preview requirements. Other reminder kinds remain protected.

## 2026-10-24: P15 opt in to explicit upstream index fingerprints

rehearsal evidence waived (release 1 waiver pattern; rehearse sandbox self-check refuses)

For agents:
- Configure `upstream_inputs` and `upstream_output` as explicit files within a node's
  allowed home. `brain check` flags missing, stale or unknown indexes; unreadable
  inputs never count as fresh. `brain index <node> --record` records current fingerprints.

After upgrading:
- Unconfigured nodes are unchanged. No script is run; explicitly regenerate the
  index before recording. Automatic regeneration remains outside this freshness slice.

## 2026-10-24: M12 print coordinator-thread wake provisioning plans

rehearsal evidence waived (release 1 waiver pattern; rehearse sandbox self-check refuses)

For agents:
- `brain machine wake-setup <node>` names the current bound coordinator, prints the
  structured T3 webhook payload to run from that thread, and gives local bind steps.
  Unsupported hosts and missing coordinators produce explicit setup gaps.

After upgrading:
- No bindings migrate and no host call occurs. Re-run the plan after rebinding;
  keep returned capabilities private. Actual host provisioning remains separate.

## 2026-10-24: R1 request a fresh brief at sustained context or token limits

rehearsal evidence waived (release 1 waiver pattern; rehearse sandbox self-check refuses)

For agents:
- Due raises one stable `restart from brief` item at three compactions or twice
  the job's token budget, naming both measures. It never automatically stops a job.
- Successful rebrief delivery records a job-scoped usage baseline so pre-restart
  counters do not immediately alert again. Only the coordinator restarts the worker.

After upgrading:
- No migration is required. Initial briefs do not reset budgets; restart baselines
  are bounded machine-local receipts. Automatic worker termination remains outside scope.
## 2026-10-24: M5 explicit hand-back summaries and questions

rehearsal evidence waived (release 1 waiver pattern; rehearse sandbox self-check refuses)

For agents: Every new hand-back needs a nonempty one-line `--summary`; `--state question` also needs `--question`. Schema v3 stores these fields without reading the report for a substitute.

After upgrading: Add these flags to hand-back scripts and `ready --record`. Legacy records remain readable and explicitly show missing summary/question fields; no historical report is rewritten.

## 2026-10-24: P5 durable replacement hand-back attempts

rehearsal evidence waived (release 1 waiver pattern; rehearse sandbox self-check refuses)

For agents: Before native verification/admission, a separate job-key/head/base/input-bound attempt records queued or verifying. Due and live records show the replacement and label earlier completion historical. Changed, closed or replaced jobs and superseded attempts cannot publish a stale result.

After upgrading: No readiness/reuse rule changes. Interrupted attempts require inspection and a fresh hand-back; they never restore the previous completion as current.

## 2026-10-24: P6 durable requests with an explicit foreground runner

rehearsal evidence waived (release 1 waiver pattern; rehearse sandbox self-check refuses)

For agents: `handback --enqueue` returns a durable request ID. `queue run <node> --once` claims one request with an exclusive lease and uses the synchronous native verifier/reservation and terminal writer. Repeated claims cannot publish again. No service is installed.

After upgrading: Run the foreground command explicitly; a queued request can wait indefinitely for it. Abandoned running leases remain unknown and are never adopted by PID reuse.

Known: P6 cancellation/crash recovery deferred until the heavy-run ownership redesign.

## 2026-10-24: P2 fresh per-job settlement attestations

rehearsal evidence waived (release 1 waiver pattern; rehearse sandbox self-check refuses)

For agents: `jobs --settled` requires `--attestation-file` with fresh exhaustive project/nested-thread coverage, exact job keys, terminal settled threads and no pending requests. All entries are checked before any ledger stamp; currently bound coordinators/open jobs remain protected. Printed settle instructions use one explicit `threadId` each.

After upgrading: Replace bare per-job or `--all-closed` boolean attestations with the documented version 1 export. This is coordinator-supplied evidence, not independent live host verification. Core performs no host settlement.

## 2026-10-24: M4 recorded live job projection and generated Active work

rehearsal evidence waived (release 1 waiver pattern; rehearse sandbox self-check refuses)

For agents: `jobs --live` joins ledger, current and historical summaries, active attempt, durable request/native admission position and recorded owner, and next action. Generated Active work uses the same projection.

After upgrading: Treat queued/verifying as recorded states; host and owner activity remain unknown without a host observation. No ETA or live-thread claim is inferred.

## 2026-10-24: M3 bounded file-only dispatch plans

rehearsal evidence waived (release 1 waiver pattern; rehearse sandbox self-check refuses)

For agents: `dispatch` prints a stable plan of explicit inbox/operator asks and owning node targets, with output/node limits and a partial indicator. An empty bounded scan is silent. It uses the read-only request projection and never acknowledges delivery.

After upgrading: An existing coordinator can consume the plan. No conversation is launched, no wake is retargeted, and no model/host pending query occurs. Dispatcher-thread/service rollout remains deferred.

## 2026-10-24: R2 close manually landed or parked ledger jobs

rehearsal evidence waived (release 1 waiver pattern; rehearse sandbox self-check refuses)

For agents: `jobs <node> --branch <branch> --close` confirms the branch head is an ancestor of local main and records merged closure with the main SHA. A parked job can be closed explicitly with a one-line `--reason`, retaining its worktree and evidence.

After upgrading: Record lesson outcomes and fresh settlement evidence as usual. A separate merge record is unnecessary; closure neither removes resources nor performs host settlement.

## 2026-10-24: Configure worker runtime and model defaults per job class

rehearsal evidence waived (root decision A, 2026-10-24); re-rehearsed natively before the release 1 tag

For agents:
- Briefs accept `route_runtime_<class>` and `route_model_<runtime>_<class>` in
  node.md and instance.md for every supported job class. Explicit flags win, then
  node per-class, node default, instance per-class, and instance default settings.
- Per-class routing names the deciding sources. Unknown class suffixes fail rather
  than silently falling back. Files without per-class keys retain legacy output.

After upgrading:
- No migration is needed. Add optional class keys to opt into per-class defaults;
  see the settings references in instance.md and node.md.

## 2026-10-24: Clear worker messages and small workflow fixes

rehearsal evidence waived (root decision A, 2026-10-24); re-rehearsed natively before the release 1 tag

For agents:
- Ready rejects unfinished WIP, fixup! and squash! commit titles anywhere in the
  job's commit range. Verify refusals name moved refs with their old and new
  commits; every main move still invalidates verification.
- `brain msg` prepares text and prints `NOT SENT: use the host send`. Deliver the
  prepared message through the host explicitly.
- `wt add` reuses cached submodule objects with independent checkout metadata
  before setup, without fetching. `wt prune` reports submodule worktrees and
  retains them and their shared module data.
- Read-only stop-hook relays may finish with `relay only`, without repeating a
  previous answer. Unanswered questions are still captured.
- Subscriptions may override `stop_percent`; otherwise they inherit the existing
  machine-wide default (85 when omitted). Routing and meters use each value.
- Needs-you lists deduplicate source item IDs and keep informational advice out;
  recommendation and default details remain available in the request reader.
- A wake bound to a coordinator satisfies the code-node tick diagnostic.

After upgrading:
- Finish or squash unfinished commits before handing back. Send formatted worker
  messages through the host. Cached submodules need no extra setup; uncached
  modules remain the setup script's responsibility. Submodule worktrees reported
  by prune are retained for explicit cleanup.
- Existing stop lines keep their behavior. Set `stop_percent` on a subscription
  only when it needs a different threshold. No other configuration change is needed.

## 2026-10-24: Keep overdue escalation wakes quiet until the item changes

rehearsal evidence waived (root decision A, 2026-10-24); re-rehearsed natively before the release 1 tag

For agents:
- Ordinary due ticks and the watcher share persistent receipts for overdue
  questions. An unchanged escalation stays quiet after transport receipts expire;
  a changed question or a cleared and reopened question can wake again.
- Coordinator wake payloads include a non-empty `reason`: opaque item keys and
  a new-or-changed or escalation-tier cause. No question text or node names leave
  the instance. Manual requests without item metadata carry an opaque request key.
- Due output and machine hold episode handling retain their existing behavior.
- The replay showed no live-noise reduction: real sequence 4→4 wakes;
  empty-source sequence 0→0 wakes.
- Unreadable wake receipts retain their evidence and let current work wake.
  Failed episode publication and corrupt or unreadable episode state preserve
  accepted pending wakes; unknown identities never prove that work resolved.
  Unknown state can repeat a wake until repaired. Healthy acknowledgements
  retain only open needs and durable pending requests, retiring closed episodes.

After upgrading:
- Keep private wake receipts. Inspect the wake reason and `.brain/wake/` delivery
  history when diagnosing a repeat; no new configuration is needed.

## 2026-10-24: Keep agreed local build orders moving

rehearsal evidence waived (root decision A, 2026-10-24); re-rehearsed natively before the release 1 tag

For agents:
- `brain due` reports `idle-with-runnable-work` when an agreed backlog task fits
  below the node's open-job cap. Its text names the next runnable tasks, in order;
  ordinary due receipts wake once per unchanged need.
- In `backlog.md`, only Markdown tasks under `## Build order` participate:
  `- [ ] text` is unstarted, `- [x] text` is done, `(parked: reason)` parks an
  unchecked task, and `(waits: operator)` blocks it on the operator. A task naming
  a `job/X` branch open in the ledger is already in progress. Other sections,
  comments and fenced examples are ignored, including existing NEXT prose.
- `parallel_jobs: N` in node frontmatter is a positive integer, default 1.
  Every open ledger job counts toward it. Set it with `brain set <id> parallel_jobs=N`;
  invalid values are refused and `brain check` flags invalid hand edits.
- Coordinators need no further go for local worktrees, builds, tests, reviews or
  local merges under the landing rules. Outward actions still need consent.

After upgrading:
- Put agreed work in your node's backlog Build order section and set its parallel
  cap when more than one open job is allowed. Existing backlog prose is unchanged
  and creates no reminders until you opt into the task format. Node documentation
  in `templates/node/node.md` describes the format and capacity rules.

## 2026-10-24: Known in this release

For agents:
- brain land (parked).
- heavy-run (2 P1 + P2, redesign in backlog).
- rehearse EPERM atexit kill (P1).
- rehearse harness linked-external-observation refusal (harness-r2 parked).
- native re-rehearsal unavailable: sandbox self-check refusal (rehearse.py:877), harness bytes unchanged since 3ddfd1a
- usage-reading probe not covered by clone comparison (harness HOME identity); verified live on the operator machine; verify ×2 and clean-install check pass

After upgrading:
- These issues remain open. Release 1 uses the approved manual landing path;
  re-rehearse natively before creating the release 1 tag.

## 2026-10-23: Rehearse JSON probes without collapsing private identities

For agents:
- JSON probes parse stdout separately from stderr. Shared warnings remain valid;
  changed stderr still fails comparison, including changes hidden by redaction.
- Due scans may write their exact private Docker receipt and lock files.
  Unrelated inbox writes and outward writes still fail rehearsal.
- UI structure and duplicate identities are checked before protected names are
  redacted. Content-free digests preserve distinct nodes, branches, readiness
  and reasons without publishing raw JSON.

After upgrading:
- Retry rehearsals refused by shared scan warnings, private receipt updates or
  protected UI identity collisions. No configuration change is needed.

## 2026-10-23: Report abandoned test Docker resources without changing them

For agents:
- Verify/gate completion, job closure, train closure and ordinary node ticks can
  report abandoned test Compose projects and Testcontainers sessions to the node
  inbox. Docker commands only inspect and list; containers are never stopped or
  removed. Known live owners and preview/dev/foreign resources are excluded.
- Reports include owner PID/job/reservation when unambiguous, age and state.
  Unknown owners stay explicitly unknown. Invalid timestamps retain the resource
  with an unknown age. One five-second deadline covers all Docker commands and
  nodes in a tick; failure stops further checks and reports a partial result.
- Private episode receipts suppress repeated inbox appends. Incomplete scans keep
  known leftovers and receipts; a complete disappearance rearms a later episode.

After upgrading:
- Set `docker_leftover_prefixes=maple-verify-*,maple-job-*` on a node, using its
  own literal test namespace. Without valid prefixes no Docker check runs.
  Testcontainers sessions also need `brain.scope` or `brain.docker-scope` (the
  colon-joined machine scope hashes) or a matching `brain.reservation`; a session label alone is insufficient.
  Optional `brain.owner-pid` and `brain.job` labels identify the owner. Keep the
  private `.brain/docker-leftovers.json` receipt across restarts.

## 2026-10-23: Wake once for a silent frozen or queued job

For agents:
- An open ledger job marked `frozen` or `queued` in the Active work table wakes
  its coordinator once after 15 minutes without observed activity. The due item
  names the job. A local commit, recorded review, queue-state change, or verifier
  log activity resets the clock; a current hand-back or legacy completion ends
  monitoring. An acknowledged compaction restart resumes monitoring with a new
  silence clock. Parked, blocked, and explicitly human-waiting jobs do not
  produce stall wakes.
- Job stalls reuse the ordinary due episode receipts. Silence starts when the
  state is first observed, so an upgrade does not guess the age of old labels.
  Coordinator-run escalation awaits a host activity/queued-wake source.

After upgrading:
- Keep the Active work state current. Set `job_stall_minutes=<minutes>` on a
  node to change the default 15-minute threshold (minimum one minute). No host
  service change is needed; the existing watcher and wake binding deliver it.

## 2026-10-23: Rehearse clones inputs and keeps private local diffs

For agents:
- Rehearsal copies use APFS copy-on-write when available, with independent plain
  copies elsewhere. Installed and candidate probes share one frozen snapshot.
- Git relative date cutoffs use the same captured clock as the instance and its
  helpers, as do data clocks for UI activity, usage and cache ages. Lock deadlines
  and elapsed timers stay live. A Git that cannot bind the clock refuses rehearsal.
- Comparison preserves semantic dates, quantities, due identities and error
  operands before publication formatting. The complete parsed UI is compared,
  including node membership, value types, readiness reasons and numeric values.
- Exact, named exceptions cover live machine load/free-memory/free-disk readings,
  their observed numbers inside machine reasons, and the brief renderer's measured
  byte counts. Policy thresholds, units, reason membership and field structure
  still compare. Evidence names every normalized difference; publication-only
  timestamp/size substitutions cannot erase a semantic regression.
- Raw stdout/stderr diffs stay in owner-only files under `local-only/` beside the
  workbench's redacted rehearsal evidence, outside the instance and repositories.
  Raw content and sidecar paths are never printed or included in evidence.

After upgrading:
- Retry rehearsals affected by live writes or relative Git date cutoffs. Inspect
  the local sidecar to attribute regressions; keep those files on this machine.
  The OS write/network sandbox remains required.

## 2026-10-23: Readiness uses the recorded verify environment

For agents:
- Lane hand-backs record the PATH and selected environment variables used by
  verification. Readiness rechecks tool identities under that recorded environment,
  so a different coordinator shell does not invalidate evidence. PATH changes in
  verification still invalidate reuse, including tools invoked indirectly.

After upgrading:
- Existing lane hand-backs lack the recorded environment and refuse readiness.
  Run one new hand-back to refresh them before checking readiness.

## 2026-10-23: Release verify admission between lane commands

For agents:
- Each lane command and lane fitness check releases its machine-care slot when
  it finishes. The next command joins the FIFO queue afresh, behind verifies
  already waiting, instead of holding one slot across the entire lane sequence.
- A single verify command keeps its reservation for its whole run, including
  any parallel children it starts. Slot limits and crashed-owner cleanup remain
  unchanged.
- Host regressions use a gated transport, an isolated wake clock, and fixture machine state.

After upgrading:
- No configuration change is needed. Start a new hand-back or gate process to
  use the per-command admission behavior.

## 2026-10-23: Wake once per changed due episode

For agents:
- Ordinary due needs keep private open-episode receipts. Unchanged needs stay quiet
  through child activity; a changed need or one resolved and later seen again wakes.
- Ready notifications use the worker's wake episode; landed notifications use the
  job episode. Verification timestamps, counts, commits and batch bookkeeping do
  not create new completion notices. Genuine new completions still wake once.
- Resolved pending due work retires without dropping inbox questions or decisions.
  Machine hold and escalation receipts keep their existing behavior.

After upgrading:
- No configuration change is needed. Keep private wake receipts when restarting
  the watcher; do not clear them to refresh displayed facts.

## 2026-10-23: Wake once per queued machine-hold episode

For agents:
- Queued machine holds share a private episode receipt across ordinary `due` ticks
  and watcher wakes. Changing timestamps, load readings or queue counts does not
  reopen a hold; recovery or a different reason does. Thermal and long-hold
  alerts retain their existing escalation behavior.
- Pending recovered holds retire without dropping mixed questions or other work.
  Routine sampler transitions remain private and quiet.

After upgrading:
- Ordinary `due` reports each queued hold once; `due --quiet` consumes that same
  receipt. Use `due --machine` for current readings without consuming a receipt.
  No service or policy change is needed.

## 2026-10-22: Refresh repository lane ownership after batch integration

For agents:
- Repository lanes include the inbox, prune, frozen-rehearsal, UI and watcher
  regression files added by the integrated batch. The coverage gate still refuses
  any new unassigned test or asset.
- The pinned test requirements and Git ignore configuration are shared inputs of
  every lane. Browser check scripts belong to the UI lane's inputs while their
  standalone execution remains separate from unit verification.

After upgrading:
- Run `python3 -B tests/check_lanes.py` when adding tests or test assets, and assign
  their lane ownership before handing back. Keep the full configured parallel
  unit command for integration verification.

## 2026-10-22: Parallel test verification

For agents:
- Test tools are pinned in `requirements-test.txt`, including pytest-xdist. The
  complete compatibility suite can run across worker processes with `-n auto` or
  an explicit worker count. The core runtime remains Python 3.9 and stdlib-only.
- Keep the same test selection and disable worker restarts with
  `--max-worker-restart=0` so crashed workers fail verification.

After upgrading:
- For development, install tools with `python3 -m pip install -r requirements-test.txt`
  and run `python3 -m pytest -q -p no:cacheprovider -n auto --max-worker-restart=0 tests/test_brain.py`.
  End users need no additional packages.

## 2026-10-22: Rehearse attributes check regressions and completed baseline failures

For agents:
- Completed nonzero baseline checks no longer count as incomplete solely because
  advisory text changed. Baseline timeouts and unexpected writes still refuse.
- Evidence and printed diagnostics include newly added check/fix error lines
  after normalization and redaction of protected terms, paths and private ids.
  Incomplete baselines identify timeout or unexpected writes and both exit codes.
- Identical baseline failures still produce named warnings.

After upgrading:
- Retry rehearsals refused only for changed advisory text on an existing failed
  check. Use diagnostics to attribute remaining regressions; pre-existing failures
  still need attention even when the comparison passes.

## 2026-10-22: Rehearse compares one frozen brain snapshot

For agents:
- Installed and candidate probes start from one private snapshot. Later live
  edits do not invalidate the comparison; snapshot writes still refuse evidence.
- Both lanes and their Python helpers use the snapshot's single captured time
  through the instance clock, keeping age metadata equal without masking real
  age or escalation changes.
- Due items compare by node, kind and id. Exact new notices attributable solely
  to candidate CHANGES.md entries are expected. Other new due items still fail.
- Brief warning byte counts are normalized. Identical baseline failures produce
  named warnings in evidence and printed output without refusing the comparison.

After upgrading:
- Retry rehearsals refused for live edits, change notices or warning sizes.
  Read any named baseline failure warnings; a passing comparison does not mean
  those pre-existing failures have been repaired.

## 2026-10-22: Stop hooks distinguish new events from old notes

For agents:
- Future-date diagnostics inspect `Updated:` lines and timestamps at the start of
  dated headings or list entries. Planned dates in prose, fenced examples, comments
  and generated facts no longer warn; future recorded entries still do.
- An old judgement timestamp alone no longer holds a read-only turn at stop.
  Changed notes, future `Updated:` times, upgrade notices and question capture keep
  their existing handling.
- Stop reminders name the changed hand-back and its state, inbox entry count or
  job ledger. Empty records and unchanged runtime bookkeeping do not trigger a
  new reminder. Fresh hand-back timestamps and changed completion data still do.
  Hook-local content hashes deduplicate diagnostics without changing facts or
  readiness evidence.

After upgrading:
- No migration is needed. Keep entry timestamps at the start of dated headings or
  list entries, and keep planned dates in their descriptive text. Coordinators
  still handle real job/inbox events and refresh generated facts when needed.

## 2026-10-22: Stable completion wakes and explicit live checks

For agents:
- Rotation wake ids follow the coordinator/session and reason category, so live
  token and turn counts do not wake it again. A new coordinator/session rearms it.
- Each done hand-back publication gets a fresh completion episode, including a
  second hand-back after rebasing. Display-only changes do not replay it.
- Root receives deduplicated notices for new recorded successful ready packets
  and merged ledger transactions. Several completions share one queued root wake;
  private child reports and branch names do not enter these notices. Inspect
  `brain due root --since-wake` after a completion wake, then the owning queues.
  These are notifications, never permission to land: the watcher checks stored
  result/identity fields and cached refs, never runs readiness, verification,
  rehearsal, lane commands or host queries. Done alone never implies ready.
- Queued completion notices are checked again before sending. Stale notices are
  dropped while questions and other protected requests in the same wake survive.
  Unreadable evidence preserves only its own opaque job/ledger scope; it cannot
  exempt another job's known-stale notice from filtering.
- `brain check` reads cached webhook probe/delivery results, even when stale.
  Only `brain check --live` sends a real POST. `--fix` never sends, even with
  `--live`. The shared explicit live-probe helper is unchanged.

After upgrading:
- Existing recorded ready packets and merged ledger transactions notify root once
  on the first watch tick; existing needs are not silently baselined away. Private
  wake acknowledgements deduplicate later ticks. No new service is required.
- Default check reports untested bindings and result age. Use `--live` when an
  actual delivery probe is intended. Normal checks never wake a coordinator.

## 2026-10-22: Prune finished jobs with annotated terminal states

For agents:
- Active work accepts terminal labels such as `merged — shipped` and `closed: retired`,
  including case and surrounding whitespace differences. Cleanup checks the row's
  state explicitly instead of treating every recorded thread as live. Unknown or
  nonterminal states stay protected and refusals name the state to reconcile.
- Notes do not override dirty work, unmerged commits, checkout identity or another
  open job claiming the folder. Cleanup still lists recorded worker references.
- Stale-target warnings match thread links and annotated references by exact ID.
  A reused branch's current worktree only establishes a worker's workspace when
  its binding identifies the current job; historical or ambiguous targets warn.

After upgrading:
- A finished job's terminal state may keep its explanatory note. Confirm the worker
  is done before marking it terminal; a displayed state is not a host activity check.

## 2026-10-21: Rehearse redacts protected probe output

For agents:
- Installed and candidate probe outputs use the same protected-term redaction
  before comparison and digesting. Protected names in another node's output no
  longer refuse a rehearsal; surrounding changes still affect comparisons.
- Evidence keeps only digests, counts and fixed regression labels. Probe output
  and protected terms stay out of evidence, printed output and refusal messages.

After upgrading:
- Retry rehearsals refused for protected probe output. No policy or configuration
  change is required.

## 2026-10-21: Rehearse tolerates live runtime churn

For agents:
- Rehearsal source stability checks bind tracked file contents, installed-core
  gitlinks, branch and tag refs, and HEAD. Runtime caches, machine-local, untracked and
  ignored files, private checkpoint refs and Git bookkeeping can change while
  the source is copied or the rehearsal runs. Copied repositories must still have
  every object reachable from HEAD, branches and tags. Copy mutation audits remain exhaustive.

After upgrading:
- Retry rehearsals that refused a busy source. Tracked content or real ref changes
  still refuse evidence; no policy or configuration change is required.

## 2026-10-21: Repository unit verification lanes

For agents:
- `tests/lanes.py` prints this repository's job lane map. Canonical test classes
  run once across the lanes; catch-all checks the compatibility entry and map.
- Inputs include transitive module dependencies and reused test helpers. A train
  function-body change selects train and catch-all; shared core/support changes
  select every lane. Import-time core changes invalidate the complete map.
- `tests/check_lanes.py` refuses unassigned or multiply assigned tests, missing
  runtime-local classes, uncovered repository inputs and drops in nonselective
  core coverage. Live runtime and browser suites are listed explicitly outside
  the unit job lanes; unit-test imports of their helpers still declare inputs.

After upgrading:
- Development nodes for this repository can set
  `verify_lanes_from=python3 -B tests/lanes.py` and
  `verify_lanes_check=python3 -B tests/check_lanes.py`. Keep the full unit verify
  command for trains; existing documentation/image skip rules still apply.
- Assign new unit tests in `tests/lanes.py` and run the coverage gate. The strongly
  connected core uses conservative dependency inputs; a test-only change can
  reuse unrelated lanes.

## 2026-10-21: Corrections carry principle and check tags

For agents:
- `brain lesson` accepts `--principle <id>` from the target's `direction.md` and
  snapshots its `enforced_by` check (or `wish`) alongside existing provenance and scope.
  `--node` records a node correction from itself or a descendant without an owner
  boundary; `--at` still selects a specialist's library.
- The learn skill maps corrections to existing principles. Missing matches and old
  lessons are `unclassified`. Direction changes still require operator approval.
- Damaged lesson tags retain source policy and warn in `brain check`, which also
  validates node lessons' source references.

After upgrading:
- Read `skills/learn/SKILL.md`. Use the optional direction templates for stable
  principle ids. Existing lessons need no migration or scope changes.

## 2026-10-21: Rehearse core candidates before release

For agents:
- `brain rehearse <node> --branch <branch>` compares the installed core with the
  committed candidate on temporary brain copies, including Git metadata. It checks
  errors, ready flips, due noise, lost brief warnings, unexpected writes and hook
  latency; private command output is never published. Each distinct bound coordinator
  webhook also receives one real constant-event POST; non-2xx statuses fail the run
  and print only their status code, never the URL.
  Their results seed only the temporary copies so sandboxed checks reuse the real probe.
- Evidence is stored in the external workbench and included in the ready packet.
  `rehearse=required` refuses readiness without a passing run for the current head,
  integration main and installed-core baseline. Reviewers run rehearsal for core changes.

After upgrading:
- Older rehearsal evidence without an installed-core baseline must be rerun.
- Set `rehearse=required` on core development nodes; commit and rebase before
  rehearsing. Take machine admission and the heavy-work lock before a run.
- Rehearsal currently requires macOS `sandbox-exec` and refuses elsewhere. Live
  homes are observed read-only; subprocess writes stay in temporary copies and
  network access is denied. No installation or migration runs on the live brain.

## 2026-10-21: Named verify lanes reuse unchanged job checks

For agents:
- Nodes may set inline `verify_lanes` (`name: command | globs`, separated by semicolons),
  or `verify_lanes_from`, a command printing the documented JSON lane map. Hand-backs
  run affected job lanes and reuse each lane's last green on the same base. Changes to
  map output, verify configuration, lockfiles or toolchain force every job lane.
- Lane glob matching includes newline paths and positive dotfile patterns. Exclusions require
  explicit dot segments. Inline pipelines use the last `|` as the input separator; ambiguous
  globs, lanes with no positive inputs (unless always), and all-nightly maps are refused.
- Map commands time out after five seconds. UI/status use recorded lane evidence; explicit
  readiness/train checks still execute the map and hash inputs. Nested JSON fails closed.
- Each lane records status, duration and a full private log; `brain ready` shows them.
  Dependencies skip after failure; strict inputs and always-run lanes can force execution.
  Optional `verify_lanes_check` checks project-owned test coverage. Nightly lanes are left
  to the complete node `verify` command, which trains/nightlies continue to execute.
- Train callers can check a packet against its recorded integration base with
  `ready_state(..., train=True)`; ordinary readiness still requires current main.

After upgrading:
- Existing nodes need no migration. To opt in, keep a complete `verify` command and add
  a lane map and coverage gate as described in the home-job skill. Inputs use tracked
  repo-relative globs with `**`, `*`, `?` and leading `!` exclusions only.

## 2026-10-21: Actionable file watcher replaces frequent AI ticks

For agents:
- `brain watch --once` queues wakes only for new actionable due identities. Repeats,
  readiness display changes and stale-status alone do not wake. Screened current excerpts
  and opaque acknowledgement history stay in `.brain/watch.json` on this machine.
- The machine-care minute sampler also runs the watcher. Failed deliveries retain their
  queue and inbox. Root receives overdue-question wakes; supported Stop-hook questions
  and question/blocked hand-backs keep their existing direct wake path.
- The host adapter documents machine-local bound-worker failure records. A failed run
  wakes once, including at-capacity failures. The router disables automatic other-runtime
  retries: no switch or retry command is recorded. Builders stay on Codex; an eligible
  alternative at the stop line requires an explicit runtime choice by the coordinator.
  There is no host exporter in this release: without a separately configured exporter, failure
  detection waits for the hourly host tick (up to one hour plus the next watcher minute).
- Malformed host failure input is isolated; invalid routing still preserves the failure notice.
  Watcher acknowledgement state remains fail-closed, with a short sampler diagnostic and
  `brain due root` repair notice. Bind workers with `brain set <node> worker:<job-key>=<thread>`.

After upgrading:
- Read the T3 adapter, coordinator role and machine-setup skill. With operator consent,
  install the machine-care unit if missing and test wake delivery, then replace frequent AI ticks
  (including root's) with hourly host safety-net ticks. Keep existing schedules until the
  transport works. Bind workers and arrange host failure-file export; CLI never queries T3.

## 2026-10-21: Worktree cleanup preserves live jobs

For agents:
- `wt prune` keeps worktrees with a different checked-out branch/HEAD, a different open ledger job
  by branch or registered path, or a live bound Active work thread, and prints why.
  `wt rm` shares these guards, including with `--force`; prune continues with other jobs.
- `wt add` refuses occupied or job-owned folders. Never reuse a merged job's folder for
  a follow-up branch; create a fresh worktree.
- Creation `--base` is provenance only. Merge checks use the local branch named by origin/HEAD (main without origin),
  or the cached origin default in PR mode, so SHA and deleted creation bases no longer strand merged worktrees.

After upgrading:
- Mark finished Active work rows terminal before cleanup; cleanup still closes its own
  ledger row. Existing worktree records need no migration; the next prune uses the integration
  branch automatically. Local status facts and UI ahead counts use the same target.
  See `skills/home-job/SKILL.md` to retire legacy reused-folder records safely.
  See `adapters/hosts/t3.md` for recovery of dead-workspace threads.

## 2026-10-21: Hired specialists run in the named job worktree

For agents:
- `brain hire --branch <branch>` now names the registered job worktree, branch
  and port block in its run line, just as `brain brief --branch` does. An unknown
  branch or a missing worktree is refused rather than pointing at the home.

After upgrading:
- Run `brain wt add <node> <branch>` first if the job has no registered worktree.
- Regenerate specialist briefs for branch jobs before launching them. Hires
  without `--branch` keep their existing home-based run line.

## 2026-10-21: Route jobs by declared class and effort

For agents:
- `brief`, `hire` and `review` accept `--class`; workers also use contract `## Class`.
  Build/ui/migration/docs/mechanical use Codex; weekly usage never switches builders
  to Claude. Diagnose/review use Claude. Explicit runtimes and node pins win. At
  a stop line the brief keeps the runtime and names eligible alternatives for an
  explicit `--runtime` choice. `route_weekly_cap` no longer switches builders;
  `class_allows_fallback` disables automatic other-runtime retries, including legacy jobs.
- Effort defaults: build/ui/migration/review high; diagnose xhigh; docs/mechanical medium.
  Contract `## Effort` overrides the default; `--effort` wins. Briefs print exact
  `modelSelection` using the configured model, host instance and reasoningEffort.
- The v3 ledger preserves `Class`, planned `Model` and `Effort` alongside observed
  `Models`. Old rows and unknown columns survive. Review metadata stays separate.

After upgrading:
- Declare class and optional effort. Set node/instance `route_model_codex` and
  `route_model_claude` to name launch models; `--model` overrides them. Without
  a model setting, the planned model is `runtime default` and `modelSelection`
  explicitly says unavailable. Configure it before launching.

## 2026-10-21: Bounded briefs and compaction restarts

For agents:
- Brief, hire and review warn above 8 KB of UTF-8 output and refuse at 16 KB,
  naming the largest sections. Node `brief_max_kb` changes the warning threshold.
- Specialist standards and library items are referenced by path. Read them before work.
- After the second context compaction, stop with a blocked hand-back carrying
  `--restart compactions`; due supplies the resume brief command and preserves commits.
  Branch briefs record task/context inputs locally and replay them on restart; successful
  re-briefing acknowledges that restart. Automatic replay applies only when no task or
  context flags are passed; explicit task/context flags start a new set of inputs.
  Review briefs keep their read-only instructions.

After upgrading:
- Reload worker, hire and home-job instructions. Shorten oversized task/context text
  without removing limits, guard or hand-back steps; no data migration is needed.

## 2026-10-21: Reviews use one full round and a fix check

For agents:
- `brain review` records the round, kind, reviewed head and exception reason for existing open jobs.
  It never creates a job for a plain branch; its brief says when rounds are unrecorded and uncapped.
  A second full round or any third recorded brief requires `--reason security|schema|privacy`.
- `--check-fix --findings <prior.json>` includes only prior blockers/majors and the fix range.
  Findings are JSON objects with severity and text. `--fix-base` overrides the previous head.
- Each brief reports contract-test gaps, the optional `fitness=<command>` gate and seed lint
  against committed test/fixture content using guard/scrub. Fitness runs only in a registered,
  clean worktree at the reviewed head; it otherwise reports skipped. Timeout stops its process
  group after 30 seconds. Never-name evidence refuses the brief. Reviewers must report rehearsal
  on real-shaped copies. `--contract` selects a contract instead of the recorded job contract.
- Review no longer runs the full `scan_terms` guard pass: the brief and every committed
  evidence blob still pass through `no_terms_in`. Run `brain guard` at hand-back for the
  complete repository guard check.

After upgrading:
- Annotate Done means items with `test: tests/file.py::test_name` or `gap: explanation`.
  Existing review counts remain counted; use the appropriate exception reason for more rounds.

## 2026-10-21: Four worker message kinds

For agents:
- Send coordinators only start, done, blocked or question. Coordinate shared commits directly with peers named in the brief.
- `brain msg <node> <kind> "<one line>"` adds known branch/report context for host delivery. Hand-back remains available.
- Briefs state the protocol and `brain msg` rejects other kinds. Small and generic workers can format messages without a ledger job.
- `brain brief --peer <thread>` names a direct shared-commit peer; include it in both worker briefs.

After upgrading:
- Re-read the worker/coordinator roles. Name peer threads in shared-commit briefs; no migration is needed.

## 2026-10-21: Ledger review evidence and coordinator spend

For agents:
- Ledger writes repair review counts from retained, matching completed hand-backs,
  including closed rows. Open rows retain any later review increments. Rows without
  valid review evidence keep their counts.
- `brain spend root` and nodes without external homes count sessions in their exact
  node folder. External homes retain the existing repository attribution rules.
- Spend also counts recorded coordinator runtime session IDs and their delegated
  descendants from scratch folders.
  Subscription restrictions, date filters and aggregate-only storage still apply.

After upgrading:
- The next ledger write repairs retained review evidence; deleted hand-backs cannot
  reconstruct old counts. No manual ledger migration is needed.
- Record runtime session IDs with `brain set <node> coordinator_sessions=id1,id2`.
  This appends to the machine-local `.brain/local.json` list, retains earlier IDs,
  and moves a repeated ID to the end so the last ID is current. Use
  `coordinator_sessions:=id1,id2` to replace the list; an empty value clears it.
  Use runtime session IDs, not host thread links. No conversation text is needed.


## 2026-10-21: Ready jobs can merge in a local train

For agents:
- `brain train <node> [--jobs job/a job/b]` stages ready jobs in order and runs one full
  verify for a green batch. Conflicts return file lists; red batches bisect prefixes
  and return the failing log before releasing the remaining jobs.
- Train admission validates each job at its recorded base even when main has advanced.
  Ordinary per-job readiness and PR merges retain their existing rules.
- Batch journals retain ejections and publication recovery state. Merged ledger rows carry
  a Batch column, preserved by the v3 unknown-column rule. Train never pushes.
- Main rebases count as occupied, including detached linked worktrees. Cleanup after a
  published-but-unrecorded train preserves merged outcomes and Batch; recovery reconciles
  matching closed rows. Failed logs are pinned privately; empty/aborted journals are committed.
- A lone red full prefix is confirmed before ejection; the baseline is proved once per batch.

After upgrading:
- Use trains only with local integration and an unoccupied main branch. Never detach
  an operator checkout. Keep PR-mode homes on the existing per-job flow.
- Route journal ejections to the existing worker; a fresh hand-back is required before
  retry. Review retained staging worktrees after interrupted runs. Release remains explicit.

## 2026-10-21: Webhook wakes identify their sender and report refusal

For agents:
- Webhook wakes send a constant User-Agent and the same constant event payload.
- Due reports the last failed delivery status once per coordinator, without the webhook URL.
- Check probes bound coordinator webhooks, reusing each binding's result for 24 hours and
  showing its age. `brain check --live` forces fresh delivery; every HTTP status outside 2xx fails check.
  Transport errors warn and are not cached. `check --fix` only reads cached probe results.
- Live probes keep the drain's delivery diagnostics intact.
- `hosts.wake_probe(inst, n)` shares this probe with rehearsal and returns a code or safe error.

After upgrading:
- Pending wakes drain on their own after upgrading; no requeue or binding change is needed.

## 2026-10-21: A new local dashboard

For agents:
- `brain ui` now serves a React and shadcn dashboard: dark first with a light mode, phone layouts and
  keyboard navigation. The sidebar holds the brain's tree (projects, their coordinators and live
  threads), Agents (Specialists, Jobs, Threads, Usage), Learnings and a searchable brain graph.
  Overview is a decision dashboard: what needs you, what moved, what is running.
- Every existing capability stays; the import checklist became the Add projects sheet beside the
  tree, and old addresses (`#view=agents`, `#view=imports`) still land.
- `ui --json` adds `from` to Inbox items and `branch` to Due items. `/api/search` (keyed) pages and
  ranks projects, specialists, learnings and jobs for the dashboard's pickers.
- A core worktree's `brain ui` serves that worktree's build; its first output line names the core
  path and commit.
- Python serves the committed build; running the dashboard still needs only Python 3.9. Dashboard
  sources, the ui-fitness gate and build instructions are in `ui/README.md`; dependency licenses and
  bundled notices are checked.

After upgrading:
- Reopen `brain ui --open`. Scripts that read the first line of `brain ui` output for the address
  should read the line that contains `?k=` instead.
- Developers: run the documented UI checks, including the ui-fitness gate, with made-up data before
  changing the dashboard.

## 2026-10-21: Name changed verify inputs and finish setup

For agents:
- Ready refusals identify added, removed and modified inputs by worktree-relative path, never
  content or per-file hashes. Paths are screened against never-name words. The existing digest,
  port, ref and tree rules still determine readiness and evidence reuse. Per-file fingerprints stay
  in ignored 0600 local sidecars under `job-handbacks`, outside instance Git history and UI output.
  Each sidecar is retained with its verify log; older unpaired sidecars are pruned on the next run.
- `verify modified its own inputs: <path>` identifies changes during the run. Make setup produce
  generated settings before the first verify, or stop the test runner writing them. Exclude a
  file from `verify_inputs` only if it cannot affect the result. Changes between runs say
  `changed since the last verify`. Old packets without per-path evidence still refuse changes.
- `wt add` waits for the node's setup script, reports its duration and returns its failure status.
  Failed setup retains the worktree for repair and gives no worker-launch instruction.

After upgrading:
- Read the named paths when readiness refuses. Fix setup rather than spending another full verify
  discovering a first-run env rewrite. The next handback adds private per-path diagnostic evidence.

## 2026-10-21: Rotate coordinators from files

For agents:
- `brain due` raises `rotate` at 150k latest input-context tokens or 50 conversation
  turns from Claude/Codex accounting; configure `rotate_tokens=` / `rotate_turns=` per node.
  Tool responses do not count as turns. Unknown new-session bindings request registration.
- `brain rotate <node>` writes `handover.md` from status, decisions, backlog, inbox, all open
  jobs/hand-backs, schedules and pending wake state, and prints the coordinator's host steps.
  It never calls the host or copies transcript text. Reconcile live host tasks and asks before
  stopping the old thread. Wake capability URLs stay in machine-local bindings.
- Host task IDs and previous coordinator/worktree bindings stay in an ignored local sidecar.
- Deferred items go into `backlog.md` immediately; new nodes include it.

After upgrading:
- Read the coordinator role and T3 adapter. Record runtime session IDs in the ordered
  `coordinator_sessions` machine binding with `brain set <node> coordinator_sessions=<new id>`;
  this retains prior IDs and selects the new ID last. Host thread IDs are different.
  Spend includes every recorded coordinator session.
- Create `backlog.md` for older nodes when deferring work. Launch the new coordinator with
  the handover, update the binding, rebind schedules/wakes, then stop and settle the old one.

## 2026-10-21: Core mechanics split into Python modules

For agents:
- The former single-file CLI now lives under `lib/brain/`, grouped by subsystem;
  `bin/brain` is a thin launcher. Commands, output and exit codes behave the same.
- Scenario tests are grouped in `tests/test_<module>.py`. See MODEL.md for the
  module map; `brain selftest`, unittest discovery and existing
  `pytest tests/test_brain.py` verify commands still run the same suite.

After upgrading:
- No migration or new dependency is needed. Keep the complete core checkout,
  including `lib/`, together; installed commands, hooks and upgrades use it.

## 2026-10-21: Wake delivery leaves other commands responsive

For agents:
- Wake requests never wait for the instance or delivery lock. A drain holds only its node's
  delivery lock while waiting for the rate window or sending; a second drain exits at once.
- A drain hands late reasons to a replacement after releasing its delivery lock, including
  when its deadline expires or its coordinator changes. Failed attempts do not respawn forever.
- Detached drains discard the requester's instance-lock bypass capability.
- Queued reasons and delivery deduplication keep their existing behavior. Machine bindings
  publish atomically so an unlocked wake reader cannot see a half-written configuration.

After upgrading:
- Re-set `wake=` bindings that were cleared while wake delivery was disabled.

## 2026-10-21: Questions wake coordinators within minutes

For agents:
- `brain wake <node> "<reason>"` queues a machine-local, deduplicated wake for its coordinator
  or root. Stop-hook questions and question/blocked hand-backs call it automatically.
- A detached stdlib worker rate-limits attempts to two minutes, retains failures and delivers
  held reasons without LLM polling. The payload is a constant event; no question text leaves.
- Due and live items flag unanswered captured/hand-back questions after ten minutes and route
  them to root. `question_minutes=` changes the threshold; answered inbox lines must be removed.

After upgrading:
- Read `adapters/hosts/t3.md`, `roles/coordinator.md` and `skills/machine-setup/SKILL.md`.
  With consent, configure a webhook task in each existing coordinator thread and store its
  URL in `wake=`; T3 Connect remote access is needed if no URL is returned. Hosts without
  webhooks retain ticks. Keep root's five-minute fallback and live pending-request checks.
- `wake=` and root's `wake_default=` stay in `.brain/local.json`. Test with made-up questions;
  verify the inbox independently of notification success. Reapprove changed runtime hooks.

## 2026-10-21: Group agents by concern and share context

For agents:
- Gather common context once, group related investigation and implementation in one job,
  and state why each separate agent is needed. `brain brief --context-file <file>` embeds
  shared findings (repeatable); `--why-separate "<reason>"` or contract `## Why separate`
  carries the reason. Hire uses the same option and contract fallback; review defaults to
  its independent second-runtime role when no reason is supplied. Old worker/hire calls warn
  on stderr even with `--out`, naming the flag and contract alternative; their brief neutrally
  states that the coordinator supplied no reason. An explicitly empty reason refuses unless
  `--allow-unexplained` is set for urgent work.
- Optional contract `## Inputs` lists paths, one per line. Declared inputs and context files
  recorded by branch briefs on this machine let `brain jobs` and `brain due` flag each
  overlapping open job once, with advice to merge related work or share a context brief.
  Sharing a context file resolves the pair and clears its due advice; jobs retains a softer
  shared-context line. Directory descendants and globs against literal paths count; common
  Markdown bullet styles work. Put paths on their own lines without notes. The check reads
  local records only and never gates a launch. Old contracts still work.

After upgrading:
- Coordinators: read the updated coordinator role and home-job skill. Add inputs and a
  separation reason when briefing new work; old jobs need no migration.

## 2026-10-21: Node hygiene and waiting decisions

For agents:
- Keep status Now within 30 nonblank lines. Missing/malformed generated facts is a check error;
  Now overflow and dead backticked paths/Markdown links in brief/status/knowledge warn.
- Blocked bullets carry `since: <brain now timestamp>; recommendation: <action>; default: <fallback>`
  after the waiting/clears-when text. Keep since stable. Over 48 hours escalates in due, inbox
  and live attention (`blocked-overdue`, `escalated`, `age_hours`, `since`, recommendation/default).
  Legacy bullets, including wrapped ones, age from their first physical line’s Git introduction,
  or file mtime if uncommitted. Clock changes do not re-notify; crossing the threshold does. Defaults never grant consent.
- Code nodes need verify and a recorded coordinator tick (bound on its coordinator machine)
  before the first job; missing setup warns in wt add and check. Root daily briefs send behind signals; each coordinator
  syncs itself. UI work starts with a UX design job (ui-designer §0) before implementation.

After upgrading:
- Run `brain check --fix` (upgrade also runs it): absent facts blocks are generated while
  preserving judgement bytes and Updated. Half/duplicate/malformed markers stay errors and
  are never auto-edited; repair those markers explicitly, then refresh. Move old Now material to Recent changes
  or decisions. Repair dead references or restore the named file/folder. Backticks must name
  explicit paths (`./file`, `folder/`, a path with a file extension) or known structural files;
  ambiguous bare names are skipped. Markdown link targets are always checked.
- Add since/recommendation/default to unresolved blocked bullets without resetting their age.
  Legacy age inference is approximate for short/common first lines and moved nodes; set
  `since:` to the original waiting time explicitly so those cases retain their correct age.
  Set code nodes’ `verify=<command>`, schedule coordinator intervals on one machine and
  record/bind them with `brain schedule add` / `brain schedule bind`. Existing jobs continue with warnings.
- Update existing root daily-brief schedules to send behind signals and let child coordinators
  run sync; use the revised T3 tick template. Overdue-only ticks report new inbox items then
  stop without committing unchanged status. Archived nodes are exempt from the new hygiene
  checks. Load the revised coordinator, daily-brief and home-job rules. The daily-brief skill
  signals behind children without running their sync or waiting for it.

## 2026-10-21: Machine care and shared verify slots

For agents:
- New heavy work waits through sustained load, thermal pressure, low free memory or disk.
  Worktree creation and fresh verifies gate themselves; run `brain heavy` before other heavy work.
- Full verifies share a machine-wide FIFO queue: two by day, one at night. Waiting reports
  progress; commands, environment, stdin and exit status stay intact. Running work is left alone.
- Ordinary due reports only this node's held/queued work or one root episode inbox action;
  busy-machine facts stay in `due --machine`, status snapshots and the live view. `brain machine
  sample` records a bounded private window; active heat episodes raise one inbox notice.

After upgrading:
- Configure thresholds in `~/.local/state/brain/machine-care.json` if defaults do
  not fit the machine. See the coordinator role's Machine care section; remove copied brief rules.
- `brain machine install` previews the minute sampler; installation needs operator consent
  and `--yes`. Ordinary commands do not install a service. The wake integration call site is
  marked for the separate wake job; sampling itself never calls an LLM.

## 2026-10-21: Check note paths in their home and exempt study setup

For agents:
- Inline project paths in node notes resolve against the bound external home, or the
  node folder for nodes without an external home. Unbound home paths, `<placeholder>`
  examples and slash-prefixed routes with no existing filesystem root component no
  longer produce dead-path warnings. Absolute filesystem references still warn.
- Markdown links stay relative to the note; explicit Brain files and folders still
  warn when missing, including on machines without a bound home. Leading `../`
  references remain relative to the node folder.
- `kind:study` nodes no longer need a verify command or coordinator tick merely
  because their home is a Git repository or their metadata names a code stack.

After upgrading:
- Run `brain check` again. Use a Markdown link for a node-relative note or attachment;
  keep inline project paths relative to the project's home. No migration is needed.

## 2026-10-21: Keep the human inbox focused

For agents:
- Legacy machine hold/clear records are filtered from inbox, dashboard attention
  and due without deleting their history. The human inbox shows at most one
  rolling machine alert, only for fresh thermal pressure or a hold over 60 minutes.
  The private episode log and one-wake-per-episode escalation remain unchanged.
- Nodes labelled `state:paused` are hidden from inbox and human attention. Their
  records remain available, and coordinator due and protected wake routes still
  carry questions, blocked hand-backs, failures and overdue questions.
- Nodes labelled `kind:study` do not produce a missing-coordinator setup warning.
- Job budget and median advice stays with its owning node: its explicit inbox
  and due show the advice; parent and root human inboxes do not. Root-owned job
  advice appears in root due. Advice remains informational and never wakes a model.
  The live dashboard's attention view retains job advice for each owning node.

After upgrading:
- No migration command is required. Historical machine lines remain on disk but
  stop appearing as inbox work. Resuming a paused node reveals its unresolved needs.
- Inspect job budget/median advice with `brain inbox <owning-node>` or
  `brain due <owning-node>`; use root due for root-owned jobs.
- Refresh status for affected nodes once: changed human-attention counts can make
  their generated facts stale after upgrading. This refresh-only work never wakes
  a model. Each new exceptional machine episode has its own inbox reporting id,
  even when no inbox check occurred while the prior episode was closed.

## 2026-10-21: Machine care transitions stay quiet

For agents:
- Machine hold/clear transitions no longer append to root's inbox or wake a
  coordinator. The private `.brain/machine.json` keeps the latest 100 transitions;
  an active hold appears as one informational line in `brain due root`.
- Root receives at most one machine escalation per episode: immediately on a fresh
  thermal-pressure reading, or once a heavy-work hold exceeds 60 minutes. Thermal
  pressure and duration share one identity, so both conditions cannot wake twice.
  Unknown thermal readings do not clear observed thermal pressure. A recovered
  load-only hold re-arms the next episode even when thermal telemetry is unavailable.
- Informational machine lines are excluded from the watcher and quiet due checks.
  Questions, blocked hand-backs, failures and overdue questions keep their routes.

After upgrading:
- No service change is required. Old generated machine inbox lines are ignored by
  due; identified queued transition-only wakes are retired. Unknown legacy queue
  entries still deliver because they may contain questions.
- Existing holds without a start time begin their duration clock at the first new
  sample. Closed episodes remain in the private log and disappear from due.

## 2026-10-21: Show new wake items and keep child housekeeping local

For agents:
- `brain due <node> --since-wake` shows new actionable items relative to the wake
  before the latest delivery, using stable item ids. Items that caused the latest
  wake stay visible while older due work is hidden. `--json` includes those ids;
  `--quiet` exits 1 when there are new items. Before the first delivery it shows all
  wake items. It excludes status refresh work and informational machine readings.
- Each delivered wake records item ids and kinds in the instance's private
  `.brain/wake/<coordinator>/history.json`, retaining 100 deliveries and their
  queue snapshots. Failed wakes do not advance the baseline. Manual wakes without
  matching items record an empty trigger set. Reading the flag never sends a wake
  or checks upgrades. A history write failure is diagnostic and never replays a wake.
- Child unsettled threads stay with their own coordinator, including when a pending
  wake is retargeted after that coordinator is removed. Budget displays remain
  informational and do not generate watcher wakes. Questions,
  blocked hand-backs, failures and overdue questions keep their wake routes.
  Existing unchanged-overdue-block deduplication is retained and regression-tested.
- Machine care keeps transitions in private state, with one rolling active hold
  display; see the quiet-transition entry above for escalation thresholds.

After upgrading:
- No migration or service installation is needed. Delivery history starts with
  the next wake. Ordinary `brain due` keeps its full informational view; use
  `--since-wake` to see what is new for the latest wake, including during delivery.
- Old generated open/close inbox notices are informational; they no longer wake
  coordinators and may be removed during normal inbox cleanup.

## 2026-10-21: Keep worker threads visible before pruning

For agents:
- Worktree prune/removal and train staging cleanup list bound worker threads before removal,
  with host steps to message the finished worker first, then safely settle it.
  Job-close checklists remind coordinators to message before pruning. Cleanup still
  permits already settled jobs; no host action is automatic.
- Dry-run lists the host plan before actual cleanup. A real removal warns against
  sending afterwards; inspect and settle missed threads through the host only.
- Message helpers and rotation handovers warn about stale job-thread workspaces,
  naming the job and suggesting a fresh thread in a fresh worktree for follow-on work.
  Import studies need no worktree; unregistered machine-local paths require a host
  check. Rotation omits already settled history from its warnings.
- `brain wt prune --dry-run` only lists candidates and bindings; it no longer fetches
  or prunes Git worktree metadata.

After upgrading:
- Read prune's thread list before cleanup. Finish host messages and settlement while
  the workspace exists; use a new thread and worktree for later work after pruning.

## 2026-10-20: Settle closed jobs' host threads

For agents:
- Closing a job prints its worker reference, job marker and thread-settlement checklist.
  The coordinator discovers and checks threads through the host, then records
  `brain jobs <node> --branch <branch> --settled --attestation-file <export.json>`
  with a fresh version 1 export (required since the 2026-10-24 P2 update). Brain never calls
  the host API.
- Never settle coordinators, open jobs, running/waiting threads or threads with pending
  requests. Unknown ownership or activity leaves the job unsettled; partial cleanup does too.
- Legacy jobs without markers or worker references can be cleared after the host adapter's
  exhaustive project-list sweep: `brain jobs <node> --settled --all-closed --attestation-file <export.json>`
  records all closed rows together with the same fresh exhaustive evidence, refuses any target
  branch with open work, and leaves open rows untouched.
  List exhaustion proves no attributable unsettled threads remain; search only finds candidates.
- `brain jobs` shows at most ten unsettled rows, then the remaining count.
- `brain due`, generated facts and live state count closed jobs with unsettled threads,
  including old ledger rows. `brain due root` now includes child settlement counts alongside the root's own work.

After upgrading:
- Read the host adapter's settlement checklist and update coordinator ticks. Inspect each
  node's backlog, settle only safely closed jobs and record settlement after the host checks.
  Older v3 cores preserve the new Settled column when rewriting ledgers.

## 2026-10-20: Team-owned external homes

For agents:
- Ready and handback compare cached origin's default branch; stale local default branches show
  their lag without being modified. Homes without origin still compare local main. Local/unset
  merge mode also requires a local default containing origin; PR mode permits a stale local checkout.
- Explicit sync records external default-branch integrations once per commit, including squash
  merges and direct commits, without claiming review or runtime usage. Owned integration ranges
  survive job closure and lesson cleanup, including when push or sync happens later.
- `merge=pr` records the coordinator's explicit `gh pr merge` workflow; no automatic merge.

After upgrading:
- Existing ready evidence remains valid when its captured local integration base is unchanged
  and contains origin's default. Reading it never rewrites worker files; the next handback
  records the new fields and can reuse the unchanged green verify run.
- Fetch explicitly before final readiness. If origin's cached default is missing or changed,
  explicitly run `git remote set-head origin -a`. Until repaired, readiness and sync marking
  refuse rather than skip integrations. After a history rewrite, inspect it and explicitly
  use `brain sync <id> --reset-main` to rescan from the common ancestor (last-sync window for unrelated history), retaining ledger deduplication. Rebase job worktrees on that remote default and hand back again.
- For team PR workflows, set `brain set <id> merge=pr` and follow home-job's head-pinned merge step.

## 2026-10-20: Runtime questions reach the inbox

For agents:
- Stop hooks capture unanswered Claude/Codex question-tool calls from the supplied transcript,
  screen them and append deduplicated node inbox lines. `brain inbox` and `brain due` see them.
  Hook failures stay fail-open; live host pending requests still need checking.
- Root's T3 tick checks every descendant coordinator's pending requests using its `brain show`
  binding. Scheduled tasks retain the provider they were created under.

After upgrading:
- Re-read `roles/coordinator.md`, `skills/machine-setup/SKILL.md` and `adapters/hosts/t3.md`.
  Refresh root's tick; verify hooks with made-up questions. Reapprove changed Codex hooks.
  With the operator's consent, install the runtime's user-level Stop hook once per machine
  and keep `brain` on PATH. Never install Brain hooks in a home or worktree. Claude uses its
  user settings; Codex uses user-level hooks when supported, otherwise host pending checks.
- Transcript scanning now walks backward in chunks to the last operator message, capped at
  four MiB per stop; questions beyond that cap rely on host pending checks.

## 2026-10-20: Retained verify output and progress

For agents:
- Fresh `handback --state done --reviews N` and `ready --record` verify runs print a private
  0600 log path under the node's `job-handbacks/` folder. Never-name words are redacted as output
  is written; logs are ignored by instance Git. Progress reports elapsed time and output counts every five seconds, including silence.
- Failures print each failing lane's last 15 lines and the overall tail. Prefix each lane's output
  with `[lane NAME]` and mark failure with `FAIL`, `FAILED`, `FAILURE`, `ERROR`, or `exit N`
  (nonzero). Unmarked output falls back to the overall tail. Five logs per job are retained.
- Verify evidence reuse is unchanged; a reused green run creates no new log or execution.

After upgrading:
- The first fresh handback adds the private-log Git exclusion. Until then, `brain check` reports
  the missing exclusion; `brain check --fix` adds it too. Add lane prefixes to parallel verify
  scripts to get separate tails.

## 2026-10-20: History from folders named as the node's

For agents:
- `brain history <node> --also <folder>` (repeatable) also reads sessions that ran in that very
  folder (not in folders below it): work done on the node from somewhere other than its home or
  worktrees, such as a scratch thread. `brain spend <node> --also <folder>` counts them too, and a
  sub-agent only when its own folder counts. A folder holding the home folder or a brain, inside a
  brain or another repository, or holding another project's home, is refused.
- `brain history` and `brain spend` no longer count a session by the remote it recorded when its
  folder still exists and git reads it as another repository; the recorded remote speaks only for
  folders that are gone or aren't repositories. A session inside a brain's folders never counts.

After upgrading:
- Nothing, unless a node's history was made mostly outside its home: then ask the operator which
  folders to name, and extract again with `--fresh --also <folder>`.


## 2026-10-20: Time and tokens per job

For agents:
- When a job closes, its ledger row records what it cost on this machine: tokens by kind (fresh
  input, cache reads and writes, output, reasoning), active and agent hours, tool calls,
  compactions, sub-agents and their share, models. They come from the runtimes' transcripts of
  the job's own sessions: those in its worktree, those given a brief naming it (briefs now carry
  a `brain-job:` line; reviewers are found by it), and those its hand-back recorded. Aggregates
  only. Older ledgers are read and rewritten in the new format; columns a newer core adds are kept.
- `brain jobs` shows medians of tokens and active hours, each open job's spend so far, and advice
  when a job is over twice the median, compacts three times or more, or sub-agents spend most of
  it. Forecasts give active hours and tokens of similar merged jobs. The live view shows the same,
  flags such jobs under attention, and totals this machine per runtime per day and week.
- A contract may set `## Budget` (e.g. `30M tokens, 6 h`). Briefs carry it; `brain handback`
  prints the spend against it; `brain jobs` and the live view flag a job over it.
- `brain spend <node>` looks back over every past session about a node's home (opt-in, like
  `brain history`); `brain spend --machine` totals every session here.
- Codex's compressed sessions (`.jsonl.zst`) are read through `zstd`, by `brain history` too:
  earlier extractions missed them.
- A host that reports usage per thread: `brain jobs <node> --branch <b> --usage-file <json>`.

After upgrading:
- Ask the operator whether to look back: `brain spend <node>` for each node with an external
  home, so the live view has history from the start.
- If earlier `brain history` studies ran on a machine with compressed Codex sessions, extract
  again (`brain history <node> --fresh`, add `--technical` for a technical re-read): they were
  missed.
- Set a `## Budget` in contracts once `brain jobs` shows measured medians for that kind of job.

## 2026-10-20: Briefs to files, merges, history batch sizes

For agents:
- `--out` on `brain brief`, `brain review`, `brain hire` and `brain history … brief` writes a new
  private file, never through a link, and refuses the node's home, its checkouts and the instance.
  Write briefs to a folder of your own outside them (or let the command print the brief).
- `brain history <node>` sizes batches so the runtime that compacted soonest here still holds one
  (60k at most); `--batch-tokens` still sets them yourself.
- `brain connect <node> check` rejects a digest citing more than 30 code files of one home.
- `brain check` gives one Blocked warning per node, with a count. `.brain/hooks.log` says when
  the stop hook held a turn for an upgrade notice.
- `brain merge` no longer fails its commit when the merged node has empty folders.
- A brain holding a link that leads out of it, where brain writes (its own files, `.brain/`, library,
  specialists, a node folder or its job, library, source and report folders), refuses every command
  that changes something until the link is gone; `brain check` lists such links.

After upgrading:
- Run `brain check`. If it lists links leading out of the brain, remove them (or move what they
  point to into the brain).

## 2026-10-20: Upgrades reach running coordinators

For agents:
- A coordinator thread loads its rules when its session begins. While its node has unread change
  entries, the stop hook holds its turn once (a tick's too) and asks it to run `brain changes
  <node>`, re-read `core/roles/coordinator.md` and the skills the entries name, do their "After
  upgrading", then `brain changes <node> --seen`.

After upgrading:
- Nothing beyond what the entries below ask.

## 2026-10-20: Less ceremony per job

For agents:
- `brain changes <node>` shows what changed in core since you last read it; `brain due <node>`
  lists unread entries until `brain changes <node> --seen`. Read them at the start of a session.
- The root tick checks the core's remote daily and reports an available upgrade once. With
  `brain set root auto_upgrade=on` (per machine) it upgrades by itself whenever no job is open.
- `brain handback --reviews N` reuses a green verify run that still proves the commit: nothing
  changed since, or only paths in the node's `verify_skip` globs, and the untracked files verify
  reads (`verify_inputs`, `.env,.env.*` by default) and the job's ports are the same.
  `--reverify` forces a run. A verify that changes what it checks records nothing.
  Run the checks your change affects during the job; leave the full verify to handback.
- A job has a contract: `brain jobs <node> --branch <b> --contract-file <f>` with Outcome,
  Authorized, Discretion, Stop when, Needs consent and Done means. Briefs, hires and reviews
  carry it. Act within it without asking; report starts, results and decisions, not
  acknowledgements; hand back only on a stop condition or for what needs consent.
- Review briefs say which round it is and, from round 2, ask the reviewer to audit each earlier
  finding's whole class; from round 3 they ask for blockers only. `brain jobs` advises when a job
  passes three rounds, runs verify often, or has no contract, and says how long it took to be
  first ready.
- Blocked entries say what they wait on and what clears them:
  `- <what> — waiting on <who or what>; clears when <condition>` (`brain check` warns otherwise).

After upgrading:
- For each code node, set `verify_skip` for paths its verify can't be affected by (docs, notes),
  and `verify_inputs` if verify reads untracked files other than `.env` files.
- Write a contract for each open job, and for each new one before briefing its worker.
- Decide with the operator whether this machine should upgrade core by itself.

## 2026-10-20: Cross-project learning

For agents:
- History readers note the technical side too: how it's built, design rules, failures and fixes,
  code conventions, tools, kinds of work. The final pass writes the node's `knowledge.md` and
  proposes `method`, `style`, `fact` and `lesson` items with `--keywords`, and specialists.
- `brain item add --from a,b` and `brain specialist new --from a,b` learn from several nodes: the
  item lives at their lowest common ancestor, scoped to the owners they share. Widening it needs
  every source owner. Learning carries its sources' never-name words: holding one, it is offered,
  shown, hired or accepted nowhere until edited.
- `brain connect <node>` compares the nodes below it (the `connect` skill): plan, one digest reader
  per node, `check`, then one connector proposing shared items and specialists and writing
  `connections.md`.
- `brain proposals [<node>]` shows everything waiting for review in full; `brain item accept a b c`,
  `brain item reject a b` and `brain specialist accept a b --at <node>` take several ids at once.
- `brain recall <node> "<problem>"` lists what every project learned that this one may use. Briefs,
  hires and reviews point workers at it, and at `knowledge.md` (not in sealed nodes).

After upgrading:
- For nodes already studied with `brain history`, a technical re-read: `brain history <node>
  --technical --fresh`, then readers, check and the final pass as the history skill says.
- Once several nodes are studied: the `connect` skill at the root.
- Review what waits: `brain proposals`.

## 2026-10-19: Every past AI conversation about a node

For agents:
- `brain history <node>` extracts every Claude and Codex conversation about a node's home, from
  every subscription this brain may use, into batches a reader finishes before it compacts. The
  `history` skill: extract, readers per batch, `check`, merge, final pass.

After upgrading:
- Nothing required. Offer the study to the operator for nodes whose import study is done.

## Required separation reason policy

For agents: `brain set <node> separation_policy=required` makes a missing
separation reason refuse brief generation across workers, hires and reviews.
Use `--why-separate`, a contract reason, or the explicit urgent waiver.
After upgrading: existing nodes keep warning behavior unless they opt in.

## Nested build-order detection

For agents: idle-work detection accepts nested and numbered Markdown tasks and
exact branch names in code spans. Parent completion, parking, operator waits and
open jobs suppress descendants. Heading depth scopes the Build order section.
After upgrading: no migration; use code spans around punctuation-bearing branches.

## Coordinator backlog and busy-run advisories

For agents: root due/watch reports bound coordinator queues above three items or
ten minutes. Busy-run alerts require a fresh exact-thread host observation with
an actual start and queued wakes; native pending files alone cannot prove busy.
After upgrading: no host changes. Optional exporter schema and evidence limits
are in docs/coordinator-observations.md; the core does not install an exporter.

## Evidence-linked observation scorecards

For agents: `brain scorecard <node> --file <manifest>` scores supplied source
receipts, timing targets and required runtime cases. Missing, changed, unsafe or
incomplete evidence stays unknown. Synthetic test success proves no real night.
After upgrading: no migration or collector is installed; see docs/scorecard.md.

## Source-first diagnostic permits

For agents: `brain diagnose` records canonical source findings before admitting
one tracked probe attempt. Source answers do not admit probes; two setup failures
refuse a third within the same job/HEAD/question. Probe execution is external.
After upgrading: no migration; see docs/diagnostics.md for receipt and CLI format.

## Causal progress and communication metrics

For agents: `brain introspect` shows recorded outcome/phase/blocker/owner/clearing
condition/next gate and dependency states. It also reports queue depth, question
waits, recorded merges/hour and observed decision-turn share, retaining unknowns.
After upgrading: typed event producers supply observed phase and communication
facts; no host query or transcript classifier is installed. See docs/introspection.md.

## Repeated review-class advisories

For agents: introspection compares explicit major/blocker classes across distinct
independent receipt rounds and links recurring classes to their observed reviews.
After upgrading: the review receipt hook supplies classifications. Without it,
review evidence is unavailable; no transcript classification is attempted.

## Repeated permission-request advice

For agents: introspection flags an observed permission request inside an exact
recorded authorization scope; revocations, phase exits and changed heads prevent
false matches. The advisory grants no authority.
After upgrading: supply typed authorization/request facts with stable receipt refs.
