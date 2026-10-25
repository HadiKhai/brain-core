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
brain task add demo APP-1 --title 'Export report' requirement=REQ-1 decisions=repo:D123,private:D-12 owner=developer status=ready
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
