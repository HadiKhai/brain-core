# Guard denial and derived hook budgets

The coordinator corrected the earlier fail-open instruction after reading `6b661c81`.
This report supersedes the earlier guard behavior and numeric budget claims in
[review-fixes.md](review-fixes.md). Same PR and branch; one new normal commit, no force-push.
No `ci/` push or per-PR cloud suite: the coordinator owns the stacked-batch full gate.

| Finding | Result | Fix | Tests |
| --- | --- | --- | --- |
| Matched forbidden action bypassed its allowance on contention | FIXED | `lib/brain/action_guard.py:223` returns a clear busy/unchecked-allowance refusal. `lib/brain/action_guard.py:198` records `decision=deny`; audit-write errors cannot turn denial into permission. The allowance is untouched until a successful retry. `lib/brain/action_guard.py:193` returns for unmatched actions before loading budgets or acquiring allowance ownership. | `tests/test_guard.py:1445`: `test_review_contended_pretool_denies_records_and_preserves_allowance` runs the real PreTool CLI under another process's lock, checks the deny response and audit row, verifies preservation and retry consumption, and proves an unmatched action remains neutral even with an invalid budget setting. `tests/test_guard.py:1488` tests denial when audit writing fails. |
| Guessed lock-wait and refresh deadlines | FIXED | `lib/brain/hook_budget.py:13` reads declared adapter timeouts and measured calibration samples; `lib/brain/hook_budget.py:46` computes defaults and validates shorter declared environment settings. `lib/brain/instance.py:428` captures the derived refresh setting; `lib/brain/instance.py:335` supplies remaining ownership time to `lib/brain/common.py:938`. Neither old fixed deadline remains in the runtime. | `tests/test_guard.py:1500` changes adapter declarations and measurement samples and verifies that defaults change; it checks explicit overrides, rejects an over-budget override, and refuses missing calibration. `tests/test_status.py:491` exercises the real stalled-Git control with a declared test override, not a patched product constant. |

## Derivation, settings and limits

The shipped adapters both declare 30 s. PreTool uses the minimum declared `PreToolUse`
timeout; refresh uses the minimum across all declared adapter hook events.
`adapters/runtimes/hook-budget.json:3` retains measured calibration wall times of 0.192608375 s (Stop) and
1.106508292 s (contended PreTool). `adapters/runtimes/hook-budget.json:7` identifies commit `6b661c81`, the
public generic-fixture evidence artifact, and the exact source fields.

The reserved headroom is the maximum of those measured samples: **1.106508292000 s**.
It deliberately reserves the complete calibration wall time, including its contention wait,
as a conservative allowance for hook work; no guessed multiplier or fixed 1 s/5 s limit
is added. The default for each setting is therefore:

`min(applicable adapter timeout) - max(calibration samples) = 30 - 1.106508292000 = 28.893491708000 s`.

- `BRAIN_PRETOOL_LOCK_WAIT_SECONDS`: explicit allowance-lock wait setting.
- `BRAIN_REFRESH_LOCK_SECONDS`: explicit final-admission Git/checkpoint setting.

Explicit settings must be finite, nonnegative and no larger than the derived available time.
Missing/invalid budget evidence denies an already-matched action rather than authorizing it;
an unmatched action does not load this configuration. Adapter/measurement files are read when
a setting is resolved, so changed declarations affect the computed value without a stale cache.
The final refresh captures its setting at admission. The Git/checkpoint budget is not a hard
interrupt around arbitrary filesystem cleanup or every ordinary non-final repair section.

The default-contention probe is validation, not new calibration input: feeding a wait that
already consumes the computed budget back into its own headroom would create a feedback loop.
Calibration remains the explicitly identified baseline observation above.

## Checks and real command proof

- Before the fix: **4 failed in 5.92 s** (deny behavior,
  audit-failure safety, derived budget support, and the declared stalled-Git override).
- Focused green: **4 passed in 3.28 s**.
- Affected files at `-n 4`: **365 passed, 2
  existing skips in 229.56 s**. The files are listed at `docs/status-refresh-lock/guard-corrections-evidence.json:21`.
  No test timeout was raised, no retry was added and no skip was introduced.
- Separate real PreTool CLI invocation on a synthetic instance, with the actual default and
  no override: **deny in 29.015805 s**, below the declared 30 s adapter timeout.
  Recorded wait: 28.917941 s. The allowance stayed byte-for-byte unchanged
  during contention and was consumed only after ownership became available on retry.
- The short process tests declare a 0.15 s setting to test contention without spending the
  full default wait per test. Their existing 3 s control deadlines are unchanged.

Only generic fixture data and aggregates are retained. No private instance mutation was
needed for this correction; the R60 command proof is the actual PreTool CLI and allowance
state transition, not a mocked lock return. Provider AI calls: 0. No merge, installation,
new agent, per-PR full suite or `ci/` push. Earlier refresh/source proofs remain historical;
this artifact records the changed runtime file SHA-256 values.

## Applicable rule table

| Rule | Result | Evidence |
| --- | --- | --- |
| R76 no word lists/regex for meaning | PASS | `lib/brain/hook_budget.py:13`, `lib/brain/action_guard.py:223`: JSON timeout fields, numeric observations and existing declared rule matches only. |
| R94 instruction failures first | PASS (not triggered) | `lib/brain/action_guard.py:223`: no model or prompt changed. |
| R80 system prompt per AI call | PASS (not triggered) | `docs/status-refresh-lock/guard-corrections-evidence.json:62`: zero provider calls. |
| Non-opinionated core | PASS | `lib/brain/hook_budget.py:13`: runtime declarations and generic measured fixtures; no instance rule names embedded. |
| R95 no arbitrary numbers | PASS | `lib/brain/hook_budget.py:13`, `adapters/runtimes/hook-budget.json:3`, `adapters/runtimes/hook-budget.json:7`: defaults computed from declared timeouts minus retained measured wall time. |
| R59 numbers computed by code | PASS | `docs/status-refresh-lock/guard-corrections-evidence.json:27`, `docs/status-refresh-lock/guard-corrections-evidence.json:15`: parsed summaries and computed defaults. |
| R73 parallel by default | PASS | `docs/status-refresh-lock/guard-corrections-evidence.json:26`: affected files at four workers; process contention is real. |
| R84 private traces local | PASS | `lib/brain/action_guard.py:198`, `docs/status-refresh-lock/guard-corrections-evidence.json:39`: opaque audit hashes and synthetic command aggregates only. |
| R86 proportional process | PASS | `docs/status-refresh-lock/guard-corrections-evidence.json:61`: same builder/PR, no delegates or additional review; full suite belongs to coordinator's batch. |
| R79 read code behind fix | PASS | `lib/brain/action_guard.py:223`, `lib/brain/hook_budget.py:13`, `lib/brain/instance.py:428`: final changed source inspected. |
| R87 no ordered work cut | PASS | Both fixes and negative controls are present at `tests/test_guard.py:1445` and `tests/test_guard.py:1500`. |
| R74 git -C | PASS | `docs/status-refresh-lock/guard-corrections-evidence.json:60`: assigned worktree, normal commit and PR push only. |
| R60 real result before review | PASS | `docs/status-refresh-lock/guard-corrections-evidence.json:39`: actual contended default CLI denial, untouched allowance and successful retry consumption. |
| R78 per-rule evidence | PASS | `docs/status-refresh-lock/guard-corrections.md:63`: this rule table and the source/test table above. |
| Machine and latest CI instruction | PASS | `docs/status-refresh-lock/guard-corrections-evidence.json:21`, `docs/status-refresh-lock/guard-corrections-evidence.json:59`: affected files only, four workers, no local full suite or per-PR CI. |
