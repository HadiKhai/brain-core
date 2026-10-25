---
name: home-job
description: Run one job (a fix, a feature, tests, an audit) in a node's external git home without touching the operator's own checkout - a worktree per job, a worker per worktree, a fresh-context non-builder review, a pull request, then cleanup. Use when work must change code or files in a node's home, especially several jobs at once.
---

# A job in an external home

Run by the node's coordinator. One job = one branch = one worktree = one worker = one pull
request. The operator's own checkout of the home is never touched.

Group work by concern before creating worktrees: one agent can investigate, implement and test
related changes. Split for independent ownership, distinct expertise or independent review, and
state why. Gather shared facts once; pass `--context-file <file>` to each brief rather than
asking several workers to rediscover them. Check `brain jobs <id>` / `brain due <id>` for
overlapping inputs and merge related jobs or share the context. A pair sharing a context file
clears its overlap advice from due; jobs still shows the shared-context fact.

Before launching, run `brain usage`. Use only a subscription allowed for this brain and below
the stop line; follow the subscription and host instance in the brief. Unknown usage must be
stated; never run a polling loop in an agent.

0. **Design and setup first.** UI work starts with a UX-first design job using the
   ui-designer specialist’s §0, before any UI code. Read its current role, follow its design
   process, and put the accepted design and acceptance criteria in the later code job’s brief.
   If the specialist is absent, route the design job through `hire`; do not skip design.
   Before a code node’s first job, set `verify=<command>` and bind a coordinator interval
   tick with `brain schedule add` and `brain schedule bind`. Missing setup warns rather
   than stranding existing jobs.
1. **Worktree.** Choose the integration mode before launching: `brain set <id> merge=pr` for
   host PR merges; `merge=local` (the default) for coordinator-owned local integration.
   `merge=pr` refuses every policy decision other than allow, including empty policy;
   conflicting or unmapped reviewed limits are quoted with their rule/source.
   Have the coordinator prepare the explicit `publication-policy.json` mapping described in
   README; core never interprets prose as an authorization engine. A mode is a workflow choice,
   and grants no push or PR consent. Unknown policy goes to the coordinator.
   Never reuse a merged job's folder for a new branch. Create a fresh worktree for each
   new job; leave the old folder on its recorded branch until cleanup.
   `core/bin/brain wt add <id> <branch> [--base <ref>]`. It creates the worktree
   in the node's workbench (beside the home, never inside it or the instance), runs the node's
   `worktree-setup.sh` if it has one (env files, installs), and gives the job its own port.
   `wt add` waits for setup to finish, prints its elapsed time and returns a nonzero status on
   failure; only a completed setup gets the worker-launch instruction.
   If setup fails, fix the script before going on: a worker without its env files wastes its run.
   - No `worktree-setup.sh` yet and the project needs env files or installs? Write one in the
     node folder (it runs with `MAIN`, `WORKTREE`, `PORT`, `BRANCH`, `NODE` and `WORKBENCH` set).
   - The project runs several services at once? `core/bin/brain set <id> ports=<N>` gives each
     worktree a block of N ports: `PORT` to `PORT_LAST` (`PORTS` = N), apart from every other
     worktree on this machine, in any brain.
2. **Record it** in `status.md` under **Active work**: `| <branch> | <thread> | <PR or -> | <state> |`.
   **Write its contract once:** `core/bin/brain jobs <id> --branch <branch> --contract-file <file>` with
   `## Outcome`, `## Authorized` (the steps the worker may take), `## Discretion` (routine,
   reversible fixes it may make within the same scope), `## Stop when`, `## Needs consent` and
   `## Done means`, optional `## Why separate` and `## Inputs` (one home-relative or absolute path per line),
   and if it helps a `## Budget` (`30M tokens, 6 h`: tokens processed, wall hours),
   sized from `brain jobs <id>`'s measured medians, never padded. Every brief, hire and review for
   the job carries it, so the worker acts within it instead of asking, and you answer from it
   instead of re-deciding. `brain jobs <id>` shows each open job's tokens so far and flags one
   over budget, over twice the median, compacting again and again, or mostly spent by sub-agents.
3. **Worker.** Host `launch` in the node's `home_project`, in the existing worktree (path and
   branch from step 1), on the node's runtime unless the job calls for another. The brief is
   self-contained (axiom 9). A runtime pin at its stop line stays pinned: use the named alternative
   only after explicitly choosing `--runtime`; never silently switch it. Write the task (what done means, where reports go:
   `core/bin/brain report <id> "<topic>"` gives a folder; commit on the branch; push the branch
   and open a pull request only when the operator or the home's rules allow it, axiom 24), then
   Give each job an explicit class (`build`, `diagnose`, `review`, `ui`, `migration`, `docs`,
   or `mechanical`) using `--class` or contract `## Class`. Builders use Codex regardless
   of weekly usage; diagnose/review use Claude. Explicit `--runtime` and node pins win.
   At Codex's stop line, keep Codex; only explicit `--runtime claude` uses the named alternative.
   `route_weekly_cap` does not switch builders; automatic other-runtime retries are disabled. Configure `route_model_codex` /
   `route_model_claude` on the node or instance; `--model` wins. Without a model setting,
   the ledger records `runtime default` and exact `modelSelection` is unavailable.
   Effort: build/ui/migration/review high, diagnose xhigh, docs/mechanical medium.
   Override with contract `## Effort` or `--effort` (the flag wins). The brief prints
   `modelSelection` with the host instance, configured model and reasoningEffort;
   use that selection when launching. Independent reviews use a fresh context that did not build the change; the same runtime
   and model are allowed when authorized. Record the actual reviewer thread ID, runtime and model.
   Wrap it with `core/bin/brain brief <id> --branch <branch> --task-file <file>
   --why-separate "<independent concern>" --context-file <shared findings>`: that adds the
   worktree, port, limits, guard step and hand-back. Use `core/bin/brain hire` when a
   specialist fits; pass `--branch <branch>` so its brief has the same finishing step.
   Workers send the coordinator only start, done, blocked or question; no progress notes, receipts, acknowledgements or hash relays.
   Shared-commit peers coordinate directly: use `brain brief --peer <thread>` to name the peer in both briefs; ask for its id once if absent.
   Pass `--why-separate "<distinct expertise>"` to hire and include the shared findings in its task text.
   Keep a host interval tick on the coordinator: `brain due <id>` plus host pending requests;
   empty means stop silently. The worker's file is durable; a host notification is optional.
4. **Review and hand back.** Review in a fresh non-builder context before the operator sees it (the
   `review` skill). Set `brain set <id> 'verify=<command>'` beforehand. The worker finishes
   reviews, commits, rebases on the integration main (`origin/<default>` when origin exists, otherwise `main`) and runs one command:
   `brain handback <id> --branch <branch> --state done --reviews <completed rounds>
   --report <path> --lessons '<proposals or none: reason>'`. This checks main, runs verify and
   records the packet, state and proposed lessons in one worker-owned `job-handbacks/<key>.json`.
   A green run that still proves the commit is reused rather than rerun: nothing changed since it,
   or only paths the node declares can't affect verify (`brain set <id> 'verify_skip=docs/*,*.md'`),
   and the files verify reads that git doesn't track are unchanged, as are the job's ports
   (`verify_inputs` globs; `.env,.env.*` unless the node names its own).
   `--reverify` runs it anyway. A verify that changes the branch, local main or the worktree records
   nothing: commit or discard what it changed, then hand back again.
   Input refusals name added, removed or modified worktree-relative paths, never contents or
   per-file hashes. Per-path evidence lives in ignored 0600 sidecars beside the worker record;
   maps share the five-log retention; missing sidecars limit diagnostics, never the digest check.
   `verify modified its own inputs: <path>` means the run itself changed them:
   make `worktree-setup.sh` produce the generated settings before the first verify, or stop the
   test runner writing them. Remove a file from `verify_inputs` only if it cannot affect the result.
   `changed since the last verify` means the input changed between runs. Older packets without
   a per-path map still refuse changed evidence, but cannot name the old paths; hand back again.
   Each fresh verify prints its private 0600 log path under the node's `job-handbacks/` folder,
   ignores logs in instance Git, redacts never-name words before writing, and reports elapsed time and output counts every five
   seconds. The five newest logs per job are retained; reuse creates no log or new run. A failure
   prints the last 15 lines of each failing lane plus the overall tail. For interleaved parallel
   groups or named steps, prefix every line with `[lane NAME]`; a line containing `FAIL`, `FAILED`,
   `FAILURE`, `ERROR`, or `exit N` (nonzero N) marks that lane failed. Names are 1-120 characters
   without `]` or line breaks. Other output uses the overall tail; inspect the full log for more.
   Nodes can opt into named lanes with
   `verify_lanes=static: npm run lint | src/**,package.json; ui: npm test | src/**,tests/**`.
   Alternatively `verify_lanes_from=<command>` prints a JSON array of objects with `name`,
   `command`, `inputs` (list of repo-relative globs), and optional `requires` (lane names),
   `strict_inputs` (globs), `always` (boolean), `schedule` (`job` or `nightly`). Use one source.
   Globs support `**` as a complete path segment, `*`, `?`, and leading `!` exclusions;
   braces, character classes, extglobs and leading `#` are refused. Positive patterns match
   dotfiles, including hidden directories. A negative pattern excludes a dot path only when
   each dot segment is explicit: `config/.babelrc,!config/*` keeps `.babelrc`, while
   `!config/.*` excludes it. Inputs match Git-tracked paths; tracked symlinks hash the link
   text, not the target. Declare target files separately when tests read them.
   The last `|` separates an inline command from its inputs, so shell pipelines work.
   Whitespace/`|` in a glob must name an actual tracked path. Each lane needs a positive
   input pattern unless `always` is true, and each map needs at least one job lane.
   Unmatched positive patterns warn. Map commands time out after five seconds and fail closed.
   UI/status display recorded evidence without executing the map or hashing the repository;
   explicit `ready` and train validation perform the complete check.
   Hand-back runs affected job lanes instead of the full `verify` command. An unchanged lane's
   last green result can be reused on the same integration base, with the same command,
   declared file contents, ignored `verify_inputs` and ports. `strict_inputs` forces a lane
   when a matching path changed since its green; `always` disables that lane's reuse.
   Requirements order execution; a failed requirement skips its dependents. Nightly lanes
   are omitted from job hand-backs. Keep `verify` as the complete train/nightly command,
   including nightly tests. `verify_lanes_check=<command>` is an optional coverage gate:
   the project must prove every test belongs to exactly one lane; Brain never parses source.
   Map output, verify configuration, lockfiles and toolchain changes force all job lanes.
   Each lane records its exit status, elapsed seconds and full private log; `brain ready`
   displays those results and reuse. The five newest logs are kept per lane per job.
   During the job, run the checks
   the change affects; leave the full verify to this one command.
   No separate packet command, host message or Lessons section is required. Blockers/questions
   use the same command with `--state blocked`/`question` and no --reviews; the report states why.
   Questions also append to inbox; a blocked human need adds `--need-human "<need>"`.
   `ready` and handback use cached `origin/HEAD` when origin exists, otherwise local `main`.
   Neither fetches. The coordinator explicitly runs `brain sync <id>` (or `git fetch origin`)
   before the final check. If origin's default changes, explicitly run `git remote set-head origin -a`.
   A missing cached default refuses readiness. With `merge=local` (the default), the job must
   also include unpublished local default-branch commits, and that local default must already
   contain origin's default. The coordinator explicitly reconciles it in a coordinator-owned
   worktree before handing back; never update the operator checkout. Use `merge=pr` when the
   operator owns that checkout and integration happens on the host. In PR mode, local lag is informational:
   rebase the job on `origin/<default>` and hand back again, leaving the operator checkout alone.
   To update a local default branch, use a separate worktree and `git merge --ff-only origin/<default>`
   only when that branch is not checked out by the operator; otherwise leave it untouched.
5. **Pull request.** Before proposing an outward step, read the generated publication policy.
   `brain policy <id> --action pr` must allow the PR; `brain policy <id> --action push` must
   allow the push. A deny quotes the binding rule/source; an unknown needs the coordinator.
   If `land_check` is configured, run `brain land-check <id> --branch <job> --run` on the
   exact candidate, then `brain land-check <id> --branch <job> --push-step` to generate the
   supported push command. Missing, failed or stale evidence refuses the step. Changed heads,
   commands, configuration or replacement attempts require a fresh passing check. These checks
   grant neither readiness nor additional outward consent. Only after those checks, permission
   and review does the worker take the explicitly authorized push/PR step.
   Have the host watch it (host `watch`), so reviews, checks and conflicts come back to you.
   Update the Active work row.
   Before anything is merged: `core/bin/brain guard <id> <worktree>` must print `guard: clean`
   when the node (or one above it) has a `never-name.txt`.
   The coordinator runs `brain ready <id> --branch <branch>` immediately before merging and
   merges only on `ready`. For a team using host PR merges, configure `brain set <id> merge=pr`.
   This records the workflow choice; it never automatically pushes or merges. After `ready`,
   the coordinator explicitly runs `gh pr merge <number> --merge --match-head-commit <verified-head>`
   in the job worktree (or the home's required squash strategy). Host checks and current base
   rules still apply; if the base moves, rebase and hand back again before retrying.
   Then run `brain sync <id>` to observe the merged remote default before pruning the job:
   its verified tree and base let sync associate a squash with the existing ledger row.
   PR mode supports merge commits and squash; do not use the host's rebase strategy, whose
   rewritten commits cannot be associated with the verified job by Git identity. Local
   fast-forward integrations retain the verified job's whole commit range in its ledger row.
   A moved main goes back to the worker for rebase and verification;
   the coordinator does not repeat its tests. Only the coordinator merges to main.
6. **Done.** When the operator (and the home's owners) merge it, set the row to `merged`, record
   the outcome in `status.md`, turn the hand-back's proposed reusable lessons into library items (the
   `learn` skill), record their ids with `core/bin/brain jobs <id> --branch <branch> --lessons
   <id1,id2>` (or `--lessons "none: <reason>"`), and run `core/bin/brain wt prune <id>`. It removes only clean, merged
   worktrees. For a dropped job, first set its Active work row to `abandoned`, preserving the
   worker thread reference, then run `core/bin/brain wt rm <id> <branch> --cause "<why>"`.
   Either way the node's `job-ledger.md` gets the job's hours, commits and review rounds
   (`core/bin/brain jobs <id>`). Cleanup prints lessons and the report first, deletes ready records,
   and retains a merged-job hand-back until its lesson outcome is recorded. The ledger keeps history.
   Follow the printed thread checklist and host adapter: find the worker from Active work,
   delegated/sub-agent descendants and reviewers by `brain-job: <key>`. Settle only when the
   ledger is merged or closed; never coordinators, open-job threads, running/waiting threads,
   or threads with pending requests. Unknown state or ownership means leave unsettled.
   After closing the job, settle each job thread through the host with one explicit thread ID
   per call and confirm each is settled. Save a fresh version 1 JSON export following the
   host adapter's schema, then record
   `brain jobs <id> --branch <branch> --settled --attestation-file <export.json>`.
   Use the printed `--job-key <key>` for a reused branch. The export must be at most five
   minutes old, cover both bound host/home projects and nested threads, and contain exactly
   the selected job keys with terminal settled threads and no pending requests. This is
   coordinator-supplied evidence; core does not independently query or settle host threads.
   Preserve the Active work thread reference.
   Legacy jobs missing markers or worker references use the host adapter's complete-project
   sweep: exhaust unsettled thread lists, protect open/active/coordinator threads and settle
   safely done closed work. After no attributable unsettled closed-work threads remain,
   `brain jobs <id> --settled --all-closed --attestation-file <export.json>` records all closed
   rows together using a fresh exhaustive export for every selected job and both project lists;
   branches with open ledger jobs cause refusal. A later job never inherits an old settlement stamp.
   `brain due` counts closed-but-unsettled jobs; `brain due root` includes child settlement counts.

Never merge, push to a shared branch, deploy or delete a branch without explicit consent
(axiom 24). Several jobs can run at once; each has its own worktree and port.
Core enforces its supported instruction/landing path; it cannot intercept an arbitrary external
`git push`. The land-check command runs only on explicit invocation or a configured local train,
never during due/status/watch reads. An absent setting adds no check and grants no permission.


## Machine care is core behaviour

Full-gate exclusion is per node: use
`brain gate-lock --node <node> --label "component verify" -- CMD [ARGS...]`
around a complete queue, standalone gate or forced hand-back. Native verification
also takes the same node lock before admission and retains it across all lanes.
Different nodes can run concurrently within the configured machine capacity;
never wrap them in an instance-wide gate lock or a cross-node turn file. Labels
do not define identity. Nested wrappers must target the same node and preserve
`BRAIN_GATE_FD`, `BRAIN_GATE_PATH` and `BRAIN_GATE_NODE`; Python subprocesses must
forward the validated descriptor using `pass_fds`, alongside existing FDs.
Closed, stale, unlocked or mismatched inherited proof refuses. Stable lock files
in `.brain/gates/` are never unlinked or replaced. The wrapper does not consume
an extra machine slot.

Before starting heavy work outside `brain wt add` or a fresh job verify, run `brain heavy --node <node>`.
Those two commands already wait for machine admission. Never start a replacement to bypass
held work. Running work is left alone. A one-minute load spike does not hold work. With fresh
sampler history, five consecutive minutes of high five-minute load, or three minutes of
unresolved thermal warning, hold new heavy work. Without fresh samples, the current five-minute load
and thermal warning apply immediately. Low free memory or disk also hold admission.

`brain machine` and `brain due --machine` show machine readings, holds and queued work. Full verifies
share a FIFO queue across every brain using the machine's configuration: two slots by day,
one overnight (22:00–08:00 in the system timezone, independent of a launcher's `TZ`). They wait with progress, preserving their
command, environment, stdin and exit status. Existing runs finish even if the limit falls.

Machine policy lives in `~/.local/state/brain/machine-care.json`, outside project homes.
It is independent of `BRAIN_CONFIG` and `XDG_CONFIG_HOME`, so separate registries share slots. It is a JSON object; omitted keys use these defaults: `load5_per_cpu: 0.8`,
`memory_free_gb: 8`, `disk_free_gb: 40`, `verify_day: 2`, `verify_night: 1`,
`night_start: 22`, `night_end: 8`. Default reserves are capped at 25% of total RAM and
20% of total disk on small machines; explicit settings are honored and impossible settings
name the configuration to adjust. Override thresholds there, once for the machine;
do not copy resource rules into node briefs. Equal night hours disable the night interval.
Unknown measurements are shown as unknown. An unknown thermal reading cannot clear an
observed thermal warning; three known normal minutes clear an episode. Conservatively,
one observed warning followed by two unknown minute readings counts as unresolved heat;
an unavailable reading cannot establish that the warning ended. Initial unknown
telemetry alone does not invent thermal pressure.

`brain machine sample` is a local, non-LLM sampler. It keeps at most 1,440 readings over
24 hours in the instance's private `.brain/machine.json`; guard and verify use readings no
older than three minutes, then fall back to live measurement. A fresh but incomplete
window uses its latest reading conservatively, so starting the sampler cannot weaken admission. `brain machine install` prints
a one-minute launchd plist (macOS) or cron line. Only run `install --yes` after operator
consent; `uninstall` also previews unless `--yes` is given. No service is installed by an
ordinary command. Heat episode transitions create one committed inbox notice each (one opening, one clearing);
ordinary `due <node>` shows a machine action only for that node's queued/held start. Root aggregates this brain's waiting work into one line. Episode transitions use the single root inbox action until handled; they never create a line per child.
Other load and queue counts stay in `due --machine`, the live view and status snapshots. `due --quiet`
checks the same node actions, including its own held start, so another node's load cannot wake an idle coordinator. The marked `machine_wake` call site awaits the core wake integration.
Wait progress reports changes and a heartbeat each minute. Keep long waits and hand-backs
in a persistent terminal session; preserve the queued process rather than letting an agent
tool timeout kill it and starting a replacement. A busy instance minute skips sampling;
missing/stale history still gets a live admission check. Invalid policy keeps diagnostics
readable and holds queued heavy work in place until repaired. Episode delivery checks
instance/Git links before committing; unsafe metadata keeps the notice pending. Tests inject fabricated readings;
never generate load or heat to test the guard.

After your second context compaction, hand back `--state blocked` with
`restart: compactions` and stop; your coordinator restarts you from a fresh brief
that resumes your commits. Record the marker with `brain handback <node> --branch
<branch> --state blocked --restart compactions --report <path>`. The coordinator
uses the exact `brain brief --branch` command in `brain due` and the existing resume section.

Generated briefs warn above `brief_max_kb` in node.md (8 KB by default) and refuse
at twice that size, naming the largest sections. Follow referenced role/standards
and library paths before working; their bodies stay out of the brief. Shorten task
or shared context, preserving node limits, never-name guard and ask/hand-back steps.

Branch briefs retain task-file paths (or inline task text) and shared-context paths in
machine-local worktree metadata. A restart command replays them; successful re-briefing
acknowledges that worker restart. Keep referenced input files available. Legacy restarts
without recorded inputs require the coordinator to supply the original task explicitly.

Automatic replay applies only when none of `--task`, `--task-file` or `--context-file`
is supplied. An explicit task replaces the saved task and drops saved context unless
context is passed again. Passing context alone also starts a new set of brief inputs.

## Local merge train

For `merge=local`, the coordinator may run `brain train <node>` after worker reviews and
hand-backs. It queues ready open jobs by their first-ready timestamp, or in the exact order
`--jobs job/a job/b` gives. Explicit lists refuse as a whole if any job is not ready.
Per-job readiness uses affected verify lanes when configured, otherwise the full verify.
Train admission checks the recorded base, so main advancing does not require another
worker verify while queued. Ordinary `brain ready` and per-job merges keep their current-base rules.

Main must be unoccupied: detach it only in a coordinator-owned checkout; never change the
operator checkout to make a train run. PR-mode homes use the existing per-job flow. A train
uses a new `train/<date>-<n>` branch and separate staging worktree, runs the node setup script,
and reserves its own ports. The green batch needs one full verify. Conflicts eject a job
with file names. Red batches verify the baseline and bisect prefixes; the first job making
a green prefix red returns to its worker with the failing log. This also assigns an
interaction to its later job. A red baseline stops without blaming any job.

Read the node's `job-trains/*.json` journal for the queue, ejections, full run logs and final
tip. Ejected jobs remain open and need a new worker hand-back before another train will
admit them. Pass the reason/log to the existing worker thread using the host adapter.
A green train fast-forwards local main with a compare-and-swap and records each accepted
job once with its Batch column. It prints a release command and never pushes. Re-running
`brain train` reconciles an interrupted publication before starting new work. An interrupted
staging run is never evidence for release. Retained dirty staging and its port reservation
need coordinator inspection before cleanup; train branches and journals remain as evidence.

Train failure logs are pinned as private 0600 ignored files in `job-trains/`; journals
including ejections are committed even when publication stops. Published jobs closed by
cleanup retain their Batch and merged outcome; recovery marks matching closed rows reconciled.

## Retire a legacy reused-folder record

Do not force cleanup of a merged job whose folder now holds a follow-up branch. The
coordinator first moves the live checkout, preserving its files, with
`git worktree move <reused-folder> <fresh-followup-folder>` in the home repository.
Update only the follow-up job's machine-local worktree `path` binding to that destination;
keep the old merged job's binding at its original, now missing path. Coordinate the move
with the worker and re-launch its thread on the new existing worktree if needed. Preserve
its ledger and worktree state. Mark only the finished job's Active work row terminal.
Then inspect `brain wt prune <id> --dry-run`: the old missing record may be retired while
the follow-up remains protected. Run prune only after confirming those exact targets.
Never delete the live folder or switch its branch merely to satisfy the old record.

## Core release rehearsal

`brain rehearse <id> --branch <branch>` is an optional diagnostic report for a
committed core candidate. Never start it automatically as part of review,
hand-back, readiness or landing. Legacy `rehearse=required` settings do not add
a readiness or landing requirement. Missing, failed or stale rehearsal evidence
does not block otherwise valid proof; preserve raw failures and report what ran.

When explicitly requested, run machine admission and the node's heavy-work lock
first. Rehearsal compares installed and candidate commands on temporary copies,
including due, check, ready, status, brief warnings, UI JSON and the Stop hook.
Live external homes are read-only observations under the OS sandbox; network
access is denied. Evidence lives in the workbench; current passing evidence may
be included in the ready packet. Changed candidate, main or installed-core inputs
make that report stale without changing readiness. Unsupported sandbox platforms
refuse the diagnostic; never bypass isolation.

Rehearsal also sends one real constant-event POST per distinct bound coordinator webhook.
It uses an explicit User-Agent, never prints the URL, and fails on any non-2xx
status (including redirects). The temporary-copy command sandbox denies network;
this authorized delivery probe runs separately against the real bindings.
