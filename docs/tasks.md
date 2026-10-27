# Tasks

Each node keeps one `tasks/<id>.md` per task. Requirements preserve exact quotes
under `requirements/REQ-*.md`. JSON-valued Markdown frontmatter preserves punctuation
and multiline text without a runtime dependency beyond Python 3.9.

## Adopt after installation or upgrade

Installation and upgrade call `brain task migrate --all`. It imports explicit
backlog/build-order/inbox entries, relevant status lists and open ledger jobs,
without changing the source documents. It records source locations and stable
fingerprints. A missing request date is explicitly labelled as file observation
time rather than presented as the date of an ask. Repeating migration is safe.

Run `brain task check <node>`. Resolve unsupported schemas or linked storage before
continuing. Never delete existing task records to make migration pass. Review the
imported backlog using the original sources, link actual dependencies, then mark
the next actionable items ready. Narrative history is not automatically activated
as a rule. Provider issue references on external-backed nodes await a provider pull.

## Daily use

```sh
brain task requirement-add demo REQ-1 --quote 'Export a report' --at '2026-01-01 10:00 +00:00' --ref inbox.md:4
brain task add demo APP-1 --title 'Export report' requirement=REQ-1 decisions=repo:D123,private:D-12 owner=developer status=ready scope=report.py
brain task next demo --json
brain wt add demo job/export --task-id APP-1
brain task list demo --status parked
brain task graph demo
```

`task set <node> <id> key=value...` updates fields. Supported kinds are feature,
bug, fix, test, chore and research. Priorities are P0–P3. `order` determines build
order; dependencies must finish before `next` selects a ready task. `parent`
records grouping and is checked for cycles. `--owner` filters ownership.

Active statuses require an exact open job key and branch. A merged or done task
requires `merge_sha` already on local main, `code_path` and nonempty `evidence`.
Closing a verified landed job fills these automatically. Other closures park its
task with a resume condition. A parked or dropped dependency does not count as done.
A failure event is replay-safe: `task event <node> <id> --event-id <round>
--reason '<failure>'`. Two distinct rounds park it. Repair, then explicitly resume
with `task set ... status=ready`; this retains its failure history.

## Requirements and current rules

`task requirements <node> [REQ-id]` reads source records. `task rule-add <node>
D-12 --text '<rule>' --at '<absolute date>' --ref '<source>'` explicitly activates
a current rule and generates `rules.md`. `--supersedes D-11` preserves the old
record as superseded and indexes it under archived rules. `task rules <node>`
returns only active rules; `--all` includes the archive. A manually maintained
`rules.md` is never overwritten. Historical decision diaries remain intact.

Shared engineering decisions belong in the repository's own `docs/DECISIONS.md`
in the same code commit. Private context stays in the node's private decision
log. Tasks reference these with `repo:D123` or `private:D-12`; references do not
copy their text. Repository decision backfills must use that repository's normal
reviewed change process and exclude personal, private and operational context.

## Optional external source

Local tasks need no credentials or network. For a node already using Linear, set
`tasks.source=linear`, `tasks.linear.team_id`, `tasks.linear.started_state_id`
and `tasks.linear.done_state_id` to that team's UUIDs. Provide its API key or
OAuth token as `BRAIN_LINEAR_TOKEN` in the process environment, never in node
files. `brain task sync <node>` pulls and retries pending job state changes.
Wake delivery and job start also pull; read-only task/due/dashboard views do not.

Linear owns product text, assignee, priority, state and blocker relations. Internal
job/requirement/decision fields remain local. Provider completion is a provider
fact, not proof of a local native gate. `brain task create <node> --title '<product
title>' --description '<product description>'` searches for duplicates first.
Only that explicit product text goes to creation; lifecycle writes send only
provider issue and workflow UUIDs. Local administrative tasks can still be added
with `source=local`. Network errors retain the mirror and pending write intent.

## Preserve obligations across attempts and dependencies

Cross-node dependencies use `node:id` and require the same owner set and sealing
boundary. `task reconcile <node> --event-id <landing-ref>` reevaluates dependents
idempotently. The event journal calls this on landing. A node/ancestor pause,
`tasks.hold`, per-task `hold`, native machine hold or configured `tasks.capacity`
prevents dispatch; review dates never remove a hold. `task brief <node>` indexes
every open and parked obligation for cold handovers.

Before starting code, declare `scope` as comma-separated repository paths. A task
retains its exact `baseline_sha`; completion must change code paths covering all
of that scope on main. `superseded_by` links transfer work without claiming it
finished while successors remain open. Drops require `drop_decision=repo:D123`
or `private:D-123`. Parked records require `park_reason`, `resume`, `owner`,
`review_at` and `next_action`. `last_progress_at` drives stable 24-hour aging;
bookkeeping updates do not reset it.

`task attempt <node> <id> <attempt-id> --branch <branch> --head <SHA> --ref <receipt>`
registers plain-Git work. Operational work uses `completion_kind=ops` and does not
require a branch. `--close` ends the attempt while leaving its obligation open.
`task alias <node> <id> <old> <new>` preserves the task ID through a branch rename.

`completion_kind` is `merge`, `ops` or `report`. Operations require a JSON
`completion_receipt` with `ref`, `scope`, `result=passed` and an absolute `at`.
Reports also require a node-local `artifact`, exact `sha256` and `version`.
A code decision or commit alone is not operational proof. The dashboard separates
unproven completion, parked debt, deliberate drops and the count of attempts.

Stopped-work studies can be imported using `task backfill <node> --register
<file.json> --apply`. The version-1 register contains `obligations`, each with
`node`, stable `source_id`, `title`, `source_ref`, `aliases` and raw `receipts`
containing `ref` and `scope`. Imports create open reconciliation tasks and retain
raw receipts verbatim. They never bulk-close, revive retired implementations or
present uncertain scope as delivered. Replays preserve existing task progress.

## Contract-gated launch integration

`task launch-plan <node> [task-id]` selects through the same dependency/hold checks
as `task next`. It requires a task-specific node-local `contract`, an explicit
`authority=repo:D123` or `private:D-123`, and declared code scope. It returns
existing `wt add`, `jobs --contract-file` and `brief` command arrays, plus the
contract digest. The configured native lifecycle/host adapter owns actual launch
and rebinding. Generating the plan performs no host action. A detected Markdown
build-order entry is not launch authority.

### Repairing legacy imports

`brain task migrate NODE` imports explicit entries in `backlog.md` and
`build-order.md`, and open jobs with a matching unmerged local worktree. It never
imports inbox messages, status history, closed attempts, or their requirements.

`brain task counts NODE` reports tasks by status and origin, active source counts,
requirement totals/drops and pending inbox lines. Reads do not drain notices.

`brain task reconcile-records NODE` plans repairs without writing. Add `--apply`
to retain non-work imports as dropped with a per-node private decision. Exact
main-delivery evidence closes imported jobs; abandoned attempts retain their ledger
cause, and incomplete evidence remains parked with a resume action. The command
never edits the ledger or deletes records. `--all` selects unarchived nodes.

Add `--drain-notices` to expire terminal machine notices through retained lifecycle
receipts when applying repairs. Normal `brain inbox` delivery also expires those
notices after output succeeds. Source lines and exact answer evidence are retained;
questions, worker failures and machine holds remain pending.
