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
   nothing: commit or discard what it changed, then hand back again. During the job, run the checks
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
   Follow the printed thread checklist and host adapter: find the worker from Active work,
   delegated/sub-agent descendants and reviewers by `brain-job: <key>`. Settle only when the
   ledger is merged or closed; never coordinators, open-job threads, running/waiting threads,
   or threads with pending requests. Unknown state or ownership means leave unsettled.
   After all job threads are confirmed settled through the host, record
   `brain jobs <id> --branch <branch> --settled`. Preserve the Active work thread reference.
   `brain due` counts closed-but-unsettled jobs; `brain due root` includes child nodes.

Never merge, push to a shared branch, deploy or delete a branch without explicit consent
(axiom 24). Several jobs can run at once; each has its own worktree and port.
