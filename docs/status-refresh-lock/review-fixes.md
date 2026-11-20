# Review fixes for PR #6

Historical handback at `6b661c81`. The coordinator corrected the earlier fail-open
instruction and budget requirements; [guard-corrections.md](guard-corrections.md)
contains the current denial behavior, derived settings, checks and rule table.

Reviewed head: `bca87792fe0b682cf5a09f9fceb8ff59fa91da21`.
All three findings are fixed by normal commits on the same branch, including a configured-route follow-up. No rebase or force-push.
This is a builder fix report, not a new independent review or a full-suite green claim.

| Finding | Verdict | Fix | Regression tests |
| --- | --- | --- | --- |
| 1. Hook appends defeat final admission | FIXED | `lib/brain/status_refresh.py:59` hashes and validates the exact inbox prefix read by `lib/brain/status.py:358`. Later appended bytes are admissible; prefix edits, truncation and namespace changes refuse. `lib/brain/events.py:846` keeps an unread suffix's event view invalid until a following refresh consumes it. | `tests/test_status.py:401`: `test_review_hook_appends_preserve_prefix_and_appear_next_refresh` invokes real `hosts.capture_hook_questions` in another process during retained ownership. `tests/test_status.py:464`: `test_review_cli_hook_append_is_not_certified_until_next_refresh` exercises the real nonblocking hook appender, event invalidation and a following CLI refresh. |
| 2. Hook timeout safety unbounded | FIXED | `lib/brain/action_guard.py:200` caps PreTool allowance-lock waits at the declared one-second setting and durably records a metadata-only `fail-open` decision, preserving unconsumed allowances. `lib/brain/instance.py:329` declares a five-second final-admission budget. `lib/brain/common.py:938` gives each Git call only the remaining budget and terminates Git and helpers as a group; generation checkpoints also refuse an expired budget. | `tests/test_guard.py:1445`: `test_review_contended_pretool_fails_open_records_and_preserves_allowance` runs the actual `hook pretool` CLI in another process while the instance lock is held. `tests/test_status.py:491`: `test_review_locked_git_subprocess_has_an_ownership_deadline` runs a controlled stalled Git executable. |
| 3. Malformed retirement state aborts refresh; content-dependent exclusion | FIXED | `lib/brain/status_refresh.py:86` wraps the complete projection/reconciliation in error handling. `lib/brain/status_refresh.py:153` conservatively fingerprints malformed state while leaving display repair/fail-open behavior intact. `lib/brain/status_refresh.py:140` excludes `inbox` and `inbox_retired` by schema field and separately fences current projected source/episode identities; it does not filter retirement entries according to inbox text before hashing them. Typed inbox reminder receipts are projected for the same frozen sources; other and legacy reminder kinds remain conservative. Wake queue/turn files are delivery output and are not read by `snapshot_view`/`facts_block`, so they are excluded from status inputs. | `tests/test_status.py:445`: `test_review_malformed_retired_inbox_keeps_fail_open_refresh` proves `inbox_retired.root=[]` refreshes safely and a changed invalid value still changes identity. Existing visible-recurrence and prefix-edit negatives remain active. |

## Red/green and measurements

The exact five regression test methods were run against a temporary export of the reviewed
runtime using `BRAIN_CORE`, without modifying that commit or the shared checkout. All five failed
in 8.06 s: two locked-build churn refusals, the malformed-retirement ValueError,
a Git deadline failure, and a contended PreTool wait failure. The owned export was removed.
The current focused run passed 7 tests in 12.54 s.

The preceding fix commit passed the three affected files (`tests/test_status.py`, `tests/test_guard.py`, `tests/test_events.py`): **363 passed, 2 existing skips in 325.54 s**. The configured-route follow-up changes the status-input selection only. Final configured-route status lane: **185 passed, 1 existing skip in 157.84 s**, three workers. No local full suite was run.

- Forced final-admission maximum physical hold: **0.552091 s**;
  competing writer wait: **0.349007 s**.
- Independent Stop hook during ownership: **0.192608 s**.
- Contended **PreTool CLI**: **1.106508 s** wall, with **1.021799 s**
  recorded lock wait and a durable fail-open decision. Its exact allowance remained unchanged
  under contention and was consumed only after normal ownership became available.
- The stalled Git control uses a declared 0.15 s test ownership budget, while its executable
  attempts 60 s of work; it returns the ownership-deadline refusal within the existing 3 s
  control deadline and leaves no controlled Git/sleep process behind.
- Both shipped hook adapters declare 30 s (`adapters/runtimes/codex-hooks.json:14`). The one-second PreTool wait cap
  applies to **any** instance-lock holder. The five-second Git/checkpoint budget applies to
  **final admitted** refresh ownership, not every ordinary/cold repair section or arbitrary
  filesystem operation. Cleanup/journal bookkeeping remains outside the Git subprocess timer.

Final-code real instance proof (`BRAIN_NO_REEXEC=1`, normal root refresh command):
**3/3 exit 0 during 9096 owned run-output writes**. First run,
including full-instance repair, took 58.679 s with a sampled maximum hold of
12.480 s. The following ordinary runs took
8.090 s and 8.208 s, with sampled maximum holds
3.253 s and 3.338 s.
These were ordinary-admission runs. Final-admission and contended PreTool behavior are
independently forced in process tests. The 0.02 s sampler can merge intervals separated by
shorter gaps. Cumulative holds are recorded separately; the cold run is not represented
as meeting the five-second final-admission Git/checkpoint budget.

All test names, measured aggregates and runtime file SHA-256s are in
[review-fixes-evidence.json](review-fixes-evidence.json). Private instance output and transcripts
remain local. No fake hooks, private questions or client names were added to public fixtures.
The hook capture test uses a configured coordinator/wake route, synthetic transcript data and the real core append, lifecycle and pending-wake publication paths. Only transport execution is disabled in that fixture. The configured-route negative control failed on the first fix candidate; the queued run for that candidate was cancelled before submitting the corrected head.

## Rule table

| Rule | Result | Evidence |
| --- | --- | --- |
| R76 no word lists/regex for meaning | PASS | `lib/brain/status_refresh.py:59`, `lib/brain/status_refresh.py:140`: bytes, field names, paths and existing episode-reader results; no new text classifier. |
| R94 instruction failures first | PASS (not triggered) | `lib/brain/status_refresh.py:86`: no model or prompt change. |
| R80 system prompt per AI call | PASS (not triggered) | `docs/status-refresh-lock/review-fixes-evidence.json:135`: provider AI spend 0; no provider calls. |
| Non-opinionated core | PASS | `lib/brain/instance.py:329`, `lib/brain/action_guard.py:200`: generic mechanism budgets; no instance rule names embedded in core. |
| R95 no arbitrary numbers | PASS | Declared 5 s ownership and 1 s hook-wait settings; `adapters/runtimes/codex-hooks.json:14` has the existing 30 s adapter budget. Measurements are retained, not guessed. |
| R59 numbers computed by code | PASS | `docs/status-refresh-lock/review-fixes-evidence.json:3`: parsed pytest summaries and measured process/live-run aggregates. |
| R73 parallel by default | PASS | `docs/status-refresh-lock/review-fixes-evidence.json:15`: final affected-file run uses three workers, alongside one live proof command; process tests create real concurrent writers/hooks. Root refresh repeats are serial because they publish the same status file. |
| R84 private traces local | PASS | `docs/status-refresh-lock/probe.py:11`, `tests/test_guard.py:1445`: exported live data is timing/count metadata only; test questions are synthetic; hook decision records contain opaque hashes. |
| R86 proportional process | PASS | `docs/status-refresh-lock/review-fixes-evidence.json:144`: same builder and PR; the one fresh review's findings fixed directly. No delegates or additional review loop. One new-head cloud suite is requested explicitly by the coordinator. |
| R79 read code behind fix | PASS | Final diff inspected at `lib/brain/status_refresh.py:59`, `lib/brain/action_guard.py:200`, `lib/brain/common.py:938`, `lib/brain/status_refresh.py:153`. |
| R87 no ordered work cut | PASS | `docs/status-refresh-lock/review-fixes-evidence.json:148`: all three review findings and their negative controls are addressed in this commit. |
| R74 git -C | PASS | `docs/status-refresh-lock/review-fixes-evidence.json:153`: all repository Git operations use the assigned absolute worktree with `git -C`; no force-push. |
| R60 real result before review | PASS | `docs/status-refresh-lock/probe.py:11` and `review-fixes-evidence.json`: three successful real root refreshes with active writes, plus actual hook and PreTool process proofs. |
| R78 per-rule evidence | PASS | `docs/status-refresh-lock/review-fixes.md:53`: this table and the finding-by-finding source/test table above. |
| Machine/order process | PASS | `docs/status-refresh-lock/review-fixes-evidence.json:15`: only affected test files, final run `-n 3`; no local full suite, merge, tag, upgrade or installed-core edits. Only owned fixture/proof leftovers removed. |

Provider AI spend: **USD 0**. New-head cloud full suite and coordinator fix verification remain
separate gates; their exact head and run URL/state are handed back without editing this commit
merely to add its own SHA. The original handback report remains as history.
