# Status refresh under concurrent writes

Base: `origin/main` at `4316cbfb`. Branch: `fix/status-refresh-lock-20261120`.
This is the builder report; fresh coordinator review and the cloud full suite remain independent gates.

The two regression tests failed on the original runtime code (2 failed in 3.95 s).
The installed command also reproduced the reported input-churn error on three real runs
(see the aggregate-only `installed_baseline` measurements in `evidence.json`). The first
probe followed the launcher's normal instance re-exec, so it is baseline evidence only.
All fixed live runs set `BRAIN_NO_REEXEC=1` to execute this worktree's code.

## Cause and source trace

- `lib/brain/status_refresh.py:398` selects policy/local bindings, node discovery and knowledge,
  source-rule files, displayed coordinator/job/task state, receipt artifacts, wake state,
  registered ready packets and declared worktree verify inputs. Git answers have their
  own ref/config/dependency fences in `GitInputs`; Git object stores are not walked.
- `lib/brain/status_refresh.py:100` originally recursively fingerprinted directory identity, size,
  mtime and ctime. Selecting whole attempt/handback folders therefore included write-only
  log output. Library discovery and its membership fence also entered run-output trees.
- The synchronized concurrent writer changes `job-attempts/logs/worker.log`,
  `job-handbacks/worker.log`, `job-trains/logs/worker.log`, `handback-queue/logs/worker.log`,
  and copied `library/output.md` files under `full-gates` and `spec-runs`, including nested
  report output. The original fence rejected these changes; `tests/test_status.py:1534` now passes.
- Additional live path-only tracing identified `.brain/wake/lifecycle.json` and a
  coordinator's `.brain/wake/<node>` directory. A separate safe content comparison found
  that the changed lifecycle field was `inbox_retired`. Retiring absent sources is scan
  output; it does not change a currently visible question. Atomic wake-file replacement
  changes directory clocks without changing pending membership. Private traces stayed local.

## Change

`lib/brain/instance.py:205` prunes native execution trees from knowledge discovery, preserving
legitimate declared nodes with those names. `lib/brain/status_refresh.py:398` fences exact ledger-derived
attempt/handback JSON, flat train/queue JSON, and referenced input maps; unrelated logs and
unreferenced maps are excluded. Missing selected records remain explicit inputs.

`lib/brain/status_refresh.py:40` retains the complete lifecycle state except retirement generations
for sources absent from the current inbox/question queues. Reappearing sources and changed
visible generations still invalidate the refresh (`tests/test_status.py:1570`). An invocation-local
cache is fenced by the physical sidecar generation and all inbox/question-source generations;
read-only reconciliation copies only the dictionaries it mutates. `lib/brain/status_refresh.py:506`
checks names and directory identity, rather than directory write clocks. Pending wake additions,
new attempt records, library changes, policy edits and Git changes remain fenced.

`lib/brain/instance.py:631` now retains the existing instance writer lock for the final admitted
refresh. Ordinary projections still run unlocked. `lib/brain/status_refresh.py:643` keeps bounded rebuilding
for external editors/Git writers that do not obey that lock; retry counts, admission deadlines
and test timeouts were not increased. Policy/binding drift still refuses stale publication.

## Proof and limits

- Exact final runtime: 16 focused tests passed in
  11.25 s at four workers; the strengthened hook test then passed
  in 3.63 s. Earlier affected-file runs covered status, knowledge and
  events: 297 passed, 2
  existing skips, 209.63 s. That broader run preceded
  the final lifecycle memo optimization; it is not represented as exact-head full-suite proof.
- `tests/test_status.py:1446` uses a separate lock-obeying writer. Forced fallback maximum physical
  hold: 0.710699 s; writer wait: 0.369376 s. An independent Stop hook
  ran successfully during ownership in 0.205504 s, inside the existing 1 s hook budget.
- Real root command (normal root operation, as confirmed by the reported root command and
  inbox incident): 3/3 exit 0 while the owned output writer performed
  2010 writes. Wall range 5.430-6.091 s;
  maximum sampled own lock interval 3.480 s. Sampling interval is
  0.02 s; adjacent intervals separated by a shorter gap may be merged. Cumulative lock hold
  is recorded separately in `evidence.json`. These live runs used ordinary admission;
  forced fallback is separately proven by the process test.
- A cold full-instance reconciliation after the failed intermediate candidate also succeeded
  (37.718 s, sampled max hold 7.551 s). This is retained as a limit: cold repair is materially
  slower than ordinary refresh. Stop hooks use their existing lock-free path; the test proves
  that path remains usable during final writer ownership. This is not an assertion that every
  unrelated writer never waits, nor a new hard deadline for all external Git operations.
- Two profiling runs of the intermediate lifecycle-copy implementation were interrupted by
  this builder after 139.700 s and 92.188 s. Their cause was repeated copying of all-node
  receipt history; selective copies and generation-fenced memoization fixed it. Those runs
  are excluded from the final success count. No installed core was changed.
- No local full suite, provider AI calls, merge, tag, upgrade or installed-core edits.
  AI/provider spend: USD 0. Only owned proof-output folders were removed.

Repeat the real probe with explicit instance selection:

```sh
python3 -B docs/status-refresh-lock/probe.py --root <instance-root> \
  --brain <this-worktree>/bin/brain --instance <registered-name> \
  --runs 3 --writer-interval .02
```

Repeats are serial because they publish the same root status file; each overlaps the independent
output writer. Affected tests use four workers. Cloud execution follows the existing sharded workflow.
The PR and exact-head cloud run URL/state are handed back through the host, without changing this
commit just to attach its own SHA.

## Applicable-rule table

| Rule | Result | Evidence |
| --- | --- | --- |
| No word lists/regex for meaning (R76) | PASS | `lib/brain/status_refresh.py:398`: existing path/JSON identity grammars only; no meaning classifier. |
| Instruction failures first (R94) | PASS (not triggered) | `lib/brain/status_refresh.py:40`: no model or prompt change, no model call. |
| System prompt per AI call (R80) | PASS (not triggered) | `docs/status-refresh-lock/evidence.json:207`: provider spend 0; no AI call path changed. |
| Non-opinionated core | PASS | `lib/brain/instance.py:205`: native storage structure only; no instance rule names/settings. |
| No arbitrary numbers (R95) | PASS | `docs/status-refresh-lock/evidence.json:92`: measured timings; existing retry/admission constants retained. |
| Numbers computed by code (R59) | PASS | `docs/status-refresh-lock/evidence.json:92`: parsed test results and measured run aggregates. |
| Parallel by default (R73) | PASS | `tests/test_status.py:1534`, `docs/status-refresh-lock/probe.py:11`: concurrent writers; tests at four workers; serial repeats share a write target. |
| Private traces local (R84) | PASS | `docs/status-refresh-lock/probe.py:11`: only aggregate timings/status exported; synthetic public tests; private command output captured locally. |
| One builder/PR/review/full gate (R86) | PASS in builder scope | `tests/test_status.py:1446`: one builder, no delegates; one PR and one cloud run; fresh review belongs to coordinator. |
| Read code behind fix (R79) | PASS | `lib/brain/status_refresh.py:398`, `lib/brain/instance.py:631`: full final runtime diff inspected, not inferred from counts. |
| Ordered work not cut/deferred (R87) | PASS | `tests/test_status.py:1534`, `tests/test_status.py:1446`, `tests/test_status.py:1570`: output exclusion, input fences, ownership and hook behavior implemented/proven. |
| git -C always (R74) | PASS | Branch operations use absolute `git -C`; no shell checkout or shared-root edits. |
| Real result before review (R60) | PASS | `docs/status-refresh-lock/evidence.json:92`, `docs/status-refresh-lock/probe.py:11`: three successful real root refreshes with active writes. |
| Machine limit / no local full suite | PASS | Affected files and focused cases only, max four local workers; full suite submitted on `ci/fix/status-refresh-lock-20261120`. |

## Decisions and remaining gates

The discovered lifecycle retirement churn and atomic wake-directory churn were fixed in this
same PR because they prevented the requested real root proof. Current episodes and explicit
record paths stay fenced; no broad state fence was simply disabled. No proposed global Brain
lesson was written by this worker. Coordinator: inspect the exact final diff, arrange the one
fresh review, read the cloud full-suite result, and retain the no-install boundary until its gate.
