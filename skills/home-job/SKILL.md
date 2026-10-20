---
name: home-job
description: Run one job (a fix, a feature, tests, an audit) in a node's external git home without touching the operator's own checkout - a worktree per job, a worker per worktree, a second-runtime review, a pull request, then cleanup. Use when work must change code or files in a node's home, especially several jobs at once.
---

# A job in an external home

Run by the node's coordinator. One job = one branch = one worktree = one worker = one pull
request. The operator's own checkout of the home is never touched.

Before launching, run `brain usage`. Use only a subscription allowed for this brain and below
the stop line; follow the subscription and host instance in the brief. Unknown usage must be
stated; never run a polling loop in an agent.

1. **Worktree.** `core/bin/brain wt add <id> <branch> [--base <ref>]`. It creates the worktree
   in the node's workbench (beside the home, never inside it or the instance), runs the node's
   `worktree-setup.sh` if it has one (env files, installs), and gives the job its own port.
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
   `## Done means`, and if it helps a `## Budget` (`30M tokens, 6 h`: tokens processed, wall hours),
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
   wrap it with `core/bin/brain brief <id> --branch <branch> --task-file <file>`: that adds the
   worktree, port, limits, guard step and hand-back. Use `core/bin/brain hire` when a
   specialist fits; pass `--branch <branch>` so its brief has the same finishing step.
   Keep a host interval tick on the coordinator: `brain due <id>` plus host pending requests;
   empty means stop silently. The worker's file is durable; a host notification is optional.
4. **Review and hand back.** Review by the other runtime before the operator sees it (the
   `review` skill). Set `brain set <id> 'verify=<command>'` beforehand. The worker finishes
   reviews, commits, rebases on `main` and runs one command:
   `brain handback <id> --branch <branch> --state done --reviews <completed rounds>
   --report <path> --lessons '<proposals or none: reason>'`. This checks main, runs verify and
   records the packet, state and proposed lessons in one worker-owned `job-handbacks/<key>.json`.
   A green run that still proves the commit is reused rather than rerun: nothing changed since it,
   or only paths the node declares can't affect verify (`brain set <id> 'verify_skip=docs/*,*.md'`),
   and the files verify reads that git doesn't track are unchanged, as are the job's ports
   (`verify_inputs` globs; `.env,.env.*` unless the node names its own).
   `--reverify` runs it anyway. A verify that changes the branch, local main or the worktree records
   nothing: commit or discard what it changed, then hand back again.
   Each fresh verify prints its private 0600 log path under the node's `job-handbacks/` folder,
   ignores logs in instance Git, redacts never-name words before writing, and reports elapsed time and output counts every five
   seconds. The five newest logs per job are retained; reuse creates no log or new run. A failure
   prints the last 15 lines of each failing lane plus the overall tail. For interleaved parallel
   groups or named steps, prefix every line with `[lane NAME]`; a line containing `FAIL`, `FAILED`,
   `FAILURE`, `ERROR`, or `exit N` (nonzero N) marks that lane failed. Names are 1-120 characters
   without `]` or line breaks. Other output uses the overall tail; inspect the full log for more.
   During the job, run the checks
   the change affects; leave the full verify to this one command.
   No separate packet command, host message or Lessons section is required. Blockers/questions
   use the same command with `--state blocked`/`question` and no --reviews; the report states why.
   Questions also append to inbox; a blocked human need adds `--need-human "<need>"`.
   `ready` compares against local `main` and never fetches. Homes merging through GitHub pull
   requests must fetch and update local `main` before `ready`; the worker then rebases and hands back again.
5. **Pull request.** Once pushing is allowed and the review is addressed, the worker opens it.
   Have the host watch it (host `watch`), so reviews, checks and conflicts come back to you.
   Update the Active work row.
   Before anything is merged: `core/bin/brain guard <id> <worktree>` must print `guard: clean`
   when the node (or one above it) has a `never-name.txt`.
   The coordinator runs `brain ready <id> --branch <branch>` immediately before merging and
   merges only on `ready`. A moved main goes back to the worker for rebase and verification;
   the coordinator does not repeat its tests. Only the coordinator merges to main.
6. **Done.** When the operator (and the home's owners) merge it, set the row to `merged`, record
   the outcome in `status.md`, turn the hand-back's proposed reusable lessons into library items (the
   `learn` skill), record their ids with `core/bin/brain jobs <id> --branch <branch> --lessons
   <id1,id2>` (or `--lessons "none: <reason>"`), and run `core/bin/brain wt prune <id>`. It removes only clean, merged
   worktrees. A job dropped instead: `core/bin/brain wt rm <id> <branch> --cause "<why>"`.
   Either way the node's `job-ledger.md` gets the job's hours, commits and review rounds
   (`core/bin/brain jobs <id>`). Cleanup prints lessons and the report first, deletes ready records,
   and retains a merged-job hand-back until its lesson outcome is recorded. The ledger keeps history.

Never merge, push to a shared branch, deploy or delete a branch without explicit consent
(axiom 24). Several jobs can run at once; each has its own worktree and port.


## Machine care is core behaviour

Before starting heavy work outside `brain wt add` or a fresh job verify, run `brain heavy`.
Those two commands already wait for machine admission. Never start a replacement to bypass
held work. Running work is left alone. A one-minute load spike does not hold work. With fresh
sampler history, five consecutive minutes of high five-minute load, or three minutes of
unresolved thermal warning, hold new heavy work. Without fresh samples, the current five-minute load
and thermal warning apply immediately. Low free memory or disk also hold admission.

`brain machine` and `brain due` show machine readings, holds and queued work. Full verifies
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
the active hold appears once in due. `due --machine` also shows a clear machine; `due --quiet`
checks node actions only, so shared queue counts do not wake every brain's coordinator. The marked `machine_wake` call site awaits the core wake integration.
Wait progress reports changes and a heartbeat each minute. Keep long waits and hand-backs
in a persistent terminal session; preserve the queued process rather than letting an agent
tool timeout kill it and starting a replacement. A busy instance minute skips sampling;
missing/stale history still gets a live admission check. Invalid policy keeps diagnostics
readable and holds queued heavy work in place until repaired. Episode delivery checks
instance/Git links before committing; unsafe metadata keeps the notice pending. Tests inject fabricated readings;
never generate load or heat to test the guard.
