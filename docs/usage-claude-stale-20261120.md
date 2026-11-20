# Claude usage freshness and signed-out accounts

Builder evidence at 2026-11-20 05:27 +02:00 (`bin/brain -b personal now`).
Base: `4316cbfb125b8f2781c1d7c728a9bf7f6cf259df`.

## Cause

The main Claude lookup is succeeding. The real OAuth usage endpoint returns
`five_hour: null`, a valid weekly window, and `limits` entries for weekly total
and a scoped model. At the base, `lib/brain/usage.py:176` drops null fields;
`usage_merge` (`:530` at the base) retains the old five-hour observation and takes
the minimum window timestamp for the account. Its October five-hour reading thus
made a freshly updated weekly reading appear 469 hours old.

The secondary account is a separate, confirmed sign-out: its Keychain entry is
readable and contains an OAuth object without an access token. The failing path
is `usage_keychain` → `usage_claude`'s token check, not a refused HTTP call.
The exact safe diagnostic is `No stored Claude OAuth token`; no request is sent.
Per the coordinator's correction, that temporary account needs no credential
repair. No credential was changed.

Machine evidence was read locally from `~/.config/brain/usage.json`,
`~/Library/LaunchAgents/org.brain.usage.plist`, `launchctl print`, and
`~/Library/Logs/brain-usage.log`. The cache was checked at 05:14 with a 300-second
running poller; the main weekly window had that fresh timestamp while its old
five-hour window set the aggregate timestamp. The log tail contained Python
malloc warnings and no per-account lookup diagnostic. At the base,
`cmd_usage`'s watch loop only logged exceptions escaping the whole refresh;
normal per-account error notes stayed solely in the cache.

## Change and regression coverage

- `lib/brain/usage.py:231`: accept explicit null session absence only with a valid
  weekly response. Omitted/malformed fields and failed lookups do not erase history.
  `tests/test_usage.py:873` exercises endpoint parsing, merge, cache validation,
  display, negative shapes, and precedence of a real session limit.
- `lib/brain/usage.py:320`: distinguish a confirmed missing OAuth token from
  unavailable Keychain access, including a missing entry versus denied alternate
  entry. `tests/test_usage.py:993` covers both and the mixed-entry case.
- `lib/brain/usage.py:564`: retain credential state with historical observations;
  unavailable reads cannot clear sign-out. A usable login explicitly clears it.
- `lib/brain/usage.py:689`: signed-out rows expose no windows and show the latest
  successful window's date. `:757` and `:814` exclude those rows before capacity
  selection and stop-line checks; `ui/src/model.ts:212` does the same in dashboard
  routing. `tests/test_usage.py:926` covers historical 0% and 99%, no other
  account, CLI/JSON/UI output, unavailable reads and restored login;
  `ui/tests/model.test.mjs:241` independently covers dashboard routing.
- `lib/brain/usage.py:697`: per-window age/staleness; `:1060` labels stale CLI
  values with age and unverified status. `tests/test_usage.py:910` verifies a stale
  five-hour observation alongside a fresh weekly window and a lookup error.
- `lib/brain/usage.py:1025`: poller logs safe per-account errors with clock-derived
  timestamps. `tests/test_usage.py:979` covers the HTTP-status diagnostic. Raw
  exceptions, response bodies, headers and credential values are never logged.

## Real-run proof

Ran the worktree launcher against the actual registered personal instance and
actual stored credentials. To keep the instance and installed poller unchanged,
used a disposable machine-cache directory under this checkout's `docs/`, copied
the current machine registry/subscriptions/cache into it, and set `BRAIN_CONFIG`
to that registry and `BRAIN_NO_REEXEC=1`. No mock or test endpoint was used.

Commands in that environment:

```sh
bin/brain -b personal usage --refresh
bin/brain -b personal usage
```

Both exit 0; combined duration 2.470 seconds. Actual aggregate observations:

| Account | Five-hour | Weekly | Reading / age | State |
| --- | --- | --- | --- | --- |
| Main Claude | none | 94%, resets 2026-11-24 19:00 +02:00 | 2026-11-20 05:27 +02:00 / 0 s | lookup, fresh |
| Secondary Claude | hidden | hidden | last read 2026-11-18 18:02 +02:00 | signed out |

The scoped model window was 63% and equally fresh. No secret value, account ID,
raw provider payload, transcript or private text is retained in this report.
The temporary cache was removed. The live installed cache is deliberately not
rewritten by this proof; normal operation will refresh it after the gated release.

## Validation

The two initial regression tests failed on the base in 3.30 seconds: retained
historical session value and absent stale labels. They passed after the fix.
Affected Python files passed 84 tests in 78.89 seconds before the final Keychain
distinction test was added. Final targeted Keychain/sign-out/parser checks passed
5 tests in 5.57 seconds. The final affected-file rerun passed all 85 tests in
39.40 seconds with `-n 4`.
Dashboard model tests passed 16 tests in 381.085 ms. `git diff --check` is clean.

Only `tests/test_usage.py` and `tests/test_usage_rounds.py` were run locally, with
at most four workers; no local full suite. Cloud full suite is requested once on
`ci/fix/usage-claude-stale-20261120`; its exact run URL/state and final head are
in the hand-back. Fresh review belongs to the coordinator; no child was started.

## Rule evidence

| Rule from the sweep brief | Builder result | Evidence |
| --- | --- | --- |
| R76, no text-meaning heuristics | PASS | `lib/brain/usage.py:231`, `:398`: typed provider fields and credential presence only. |
| R94, instructions before patching model misses | PASS (not applicable) | `lib/brain/usage.py:372`: provider usage transport/parser, no model inference. |
| R80, system prompt for every AI call | PASS (not applicable) | `lib/brain/usage.py:404`: authenticated usage GET, no AI generation call. |
| Non-opinionated core | PASS | `lib/brain/usage.py:231`, `:814`: provider protocol and generic credential state; no instance name or rule hardcoded. |
| R95 / R59, declared or computed numbers | PASS | `lib/brain/usage.py:697`: measured timestamp ages and existing configured cache interval; Security item-not-found status is a protocol constant at `:353`. |
| R73, independent work concurrent | PASS | Affected files run with `-n 4`; dashboard model checks and independent reads ran concurrently. |
| R84, private text stays local | PASS | `lib/brain/usage.py:410`, `:485`: no bodies or secret-bearing exception messages; fixtures in `tests/test_usage.py:873` are generic. |
| R86, one builder / PR / review / full gate | PASS for builder scope | No child; one PR and one cloud full-suite trigger; coordinator owns the fresh review. |
| R79, read code behind claims | PASS | This report's change map and complete builder diff inspection before commit. |
| R87, no ordered scope cut | PASS | Main freshness, stale/error visibility and the corrected signed-out routing requirement are covered; credentials unchanged by explicit instruction. |
| R74, git directory form | PASS | All builder git operations use `git -C` with the assigned absolute worktree. |
| R60, real result before review | PASS | Live worktree CLI proof above, both exit 0 with fresh main reading. |
| Machine / cleanup / no installed-core edits | PASS | Only affected files locally, four workers maximum; disposable cache/dependency link removed; no installed-core change or local full suite. |

Decisions made: use the existing absent-window representation; retain cached
history while suppressing it for signed-out accounts; keep unknown Keychain
access distinct from sign-out; use a disposable cache for real-instance proof.
Proposed lesson: an explicit absent provider window is a current observation,
and credential state must be separate from retained usage observations.

Spend: no paid AI/provider generation. Subscription usage endpoints only.
