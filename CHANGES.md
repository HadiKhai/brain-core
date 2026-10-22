# Changes, for the agents that run a Brain

Newest first. Each entry says what changed in rules, skills and commands, and what to do after
upgrading. `brain changes <node>` shows the entries this node's coordinator hasn't read yet, and
`brain due <node>` lists them until `brain changes <node> --seen`. Entry headings are
`## <date>: <title>`; once released, a heading never changes.

## 2026-10-22: Prune finished jobs with annotated terminal states

For agents:
- Active work accepts terminal labels such as `merged — shipped` and `closed: retired`,
  including case and surrounding whitespace differences. Cleanup checks the row's
  state explicitly instead of treating every recorded thread as live. Unknown or
  nonterminal states stay protected and refusals name the state to reconcile.
- Notes do not override dirty work, unmerged commits, checkout identity or another
  open job claiming the folder. Cleanup still lists recorded worker references.

After upgrading:
- A finished job's terminal state may keep its explanatory note. Confirm the worker
  is done before marking it terminal; a displayed state is not a host activity check.

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

## 2026-10-20: Settle closed jobs' host threads

For agents:
- Closing a job prints its worker reference, job marker and thread-settlement checklist.
  The coordinator discovers and checks threads through the host, then records
  `brain jobs <node> --branch <branch> --settled`. Brain never calls the host API.
- Never settle coordinators, open jobs, running/waiting threads or threads with pending
  requests. Unknown ownership or activity leaves the job unsettled; partial cleanup does too.
- Legacy jobs without markers or worker references can be cleared after the host adapter's
  exhaustive project-list sweep: `brain jobs <node> --settled --all-closed` records all closed
  rows together, refuses any target branch with open work, and leaves open rows untouched.
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
