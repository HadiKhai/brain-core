# Claude usage freshness: implementation and review corrections

This report describes the final patch, including all four changes requested by the
fresh review of `f5ac03333bc3a41a5b1c81c1750ba50d6eedf210`. The original base is
`4316cbfb125b8f2781c1d7c728a9bf7f6cf259df`. Final head and cloud run URL/state are in
the hand-back; no merge, release, upgrade or installed-core change is performed.

## Original cause and machine evidence

The main Claude OAuth usage lookup succeeds. Its response has `five_hour: null`,
valid weekly usage, and weekly scoped limits. The original parser dropped the
explicit absence. Account-window merging retained the October session reading and
used the minimum window timestamp, making a fresh weekly reading appear 469 hours
old. `lib/brain/usage.py:234` accepts the explicit absence only with a valid weekly
response; omitted/malformed windows and failed lookups retain their observations.

The temporary secondary account has a readable Keychain OAuth entry with no access
token. `usage_claude` stops before HTTP with the safe diagnostic
`No stored Claude OAuth token`. No credential was changed or re-login requested.

Machine evidence was read locally from `~/.config/brain/usage.json`,
`~/Library/LaunchAgents/org.brain.usage.plist`, `launchctl print`, and
`~/Library/Logs/brain-usage.log`. The poller runs every 300 seconds. The main weekly
window had a fresh timestamp while the retained session window set the account's
age. The log tail contained malloc warnings, without per-account lookup errors:
the original watch loop logged only exceptions escaping the whole refresh.
The patch logs safe per-folder diagnostics with generated timestamps.

## Review findings: all fixed

| Finding | Fix and evidence | Regression test that failed before the fix |
| --- | --- | --- |
| 1. MAJOR: shared account identity overwrote folder sign-in state | `lib/brain/usage.py:575` merges only account observations; `:618` stores credential state and error on each subscription/folder; `:723` reads that state. Legacy account metadata is used only when the folder is unambiguous. | `tests/test_usage.py:1020` — `test_usage_shared_account_keeps_credential_state_per_folder`: both declaration orders, one shared account, one signed-in and one signed-out folder; only the signed-in folder participates in routing. |
| 2. MAJOR: Linux read failures became sign-out | `lib/brain/usage.py:375` separates FileNotFoundError from permission/read errors and invalid JSON, schema or text encoding. Only confirmed absence becomes sign-out; unavailable reads retain usage with a safe error and real age. | `tests/test_usage.py:1049` — `test_usage_linux_credential_read_error_is_unverified_not_signed_out`: PermissionError containing a fake private token, malformed JSON/schema, invalid UTF-8 and a genuinely absent file. |
| 3. MAJOR: routing/overview lost age and safe error | `lib/brain/usage.py:737` exposes per-window verification, age and safe error; `:757` carries evidence through selected and stop-line routing results; `:821` and `:882` keep failed fresh lookups unverified in ranking; `:1127` labels CLI errors unverified. `ui/src/model.ts:212` shares the presentation across `overview.tsx:154` and `usage.tsx:63`. Visible and accessible gauge text carries the evidence; CSS wraps it instead of clipping it. | `tests/test_usage.py:1085` — `test_usage_routing_explains_age_verification_and_lookup_error` checks below/above stop-line routing and fresh failed lookups; `ui/tests/model.test.mjs:31` — `usage and overview presenters retain stale age, verification and safe lookup error` executes both real JSX presenters. |
| 4. MINOR: signed-out-only capacity claimed exhaustion | `ui/src/model.ts:222` distinguishes signed out, unavailable/unverified and verified stop-line exhaustion; `ui/src/usage.tsx:89` uses that message. | `ui/tests/model.test.mjs:44` — `usage presenter separates signed out and unavailable from exhausted capacity`: signed-out-only, no usable reading and confirmed exhausted fixtures. |

The Python regression run failed all three new tests at the previous head in
3.44 seconds. Both new presenter tests failed there in 326.157 ms: overview showed
only a stale percentage, and the signed-out-only usage page claimed exhaustion.
All now pass. Existing cache/absence tests also detected one intermediate metadata
shape inconsistency; centralizing account-only merging fixed it without weakening
the assertion. Final results: 88 affected Python tests passed in 24.96 seconds
with four workers; all 18 UI model/presenter tests passed in 370.604 ms;
TypeScript exited 0 with no diagnostics.

## Validation and real-run proof

Only affected local files are run, at most four Python workers, with pytest cache
and bytecode publication disabled. No local full suite. Latest affected results
and durations are in the hand-back. The UI model file includes the executable JSX
presenter tests; TypeScript is checked with `tsc --noEmit --incremental false`.
The React best-practices skill was applied to the two TSX edits: shared derived
presentation, no new hooks/fetches/dependencies, stable keys, escaped text, and
accessible meter descriptions. No browser or visual-geometry proof is claimed.

The worktree launcher was run against the actual personal instance and actual
stored credentials, with no mock endpoint. A temporary directory under `docs/`
received a copy of the machine registry, subscriptions and usage cache. With
`BRAIN_CONFIG` pointing there and `BRAIN_NO_REEXEC=1`, the commands were:

```sh
bin/brain -b personal usage --refresh
bin/brain -b personal usage
bin/brain -b personal usage --json
```

The real run at 2026-11-20 05:42 +02:00 exited 0, with refresh plus read taking
1.917 seconds. Main Claude showed weekly 94%, five-hour none, age 0 s,
`verification=verified`, no error, and scoped weekly usage 63%. The secondary
account showed `signed_out`, last read 2026-11-18 18:02 +02:00, no windows and
`No stored Claude OAuth token`. Each row/window now carries its own verification
and safe error. The temporary cache was removed. The real instance cache,
credentials and installed poller were not changed. The final hand-back records
the final source inspection. A further final-source refresh/read at
2026-11-20 05:45 +02:00 exited 0 in 1.736 seconds with the same main reading and
secondary signed-out state; it also asserted that account-cache records contain
neither folder credential state nor folder lookup errors.

No raw credential, provider response body, account ID, transcript or private text
is retained here. Tests use generic fixtures. Dependency access used a temporary
read-only link to existing machine dependencies; the link and owned trial logs
are removed before commit. No paid AI generation calls were made.

## Rules from the sweep brief

| Rule | Result | Final evidence |
| --- | --- | --- |
| R76: no text-meaning heuristics | PASS | `lib/brain/usage.py:375`, `:618`: typed file/error/provider structure, not semantic keyword decisions. UI states use explicit enum equality. |
| R94: instructions before patching model misses | PASS (not applicable) | `lib/brain/usage.py:398`: usage transport/parser; no AI inference. |
| R80: system prompt for every AI call | PASS (not applicable) | `lib/brain/usage.py:430`: OAuth usage GET, not generation. |
| Non-opinionated core | PASS | `lib/brain/usage.py:618`, `ui/src/model.ts:222`: generic folder/account/window mechanisms; no instance rule or operator name in production code. |
| R95 / R59: declared or computed numbers | PASS | `lib/brain/usage.py:733`, `ui/src/model.ts:212`: timestamp arithmetic and existing configured freshness interval; no new cutoff. |
| R73: independent work concurrent | PASS | Python affected files use `-n 4`; UI checks and independent source reads run concurrently. |
| R84: private text stays local | PASS | `lib/brain/usage.py:375`: fixed safe error messages; `tests/test_usage.py:1049` proves a secret-bearing exception message is not retained. |
| R86: size process to job | PASS for builder scope | One builder, same PR, one new commit and one cloud run for this revised head; fresh re-check belongs to coordinator, no child launched. |
| R79: read code behind claims | PASS | Full final diff read; finding map above cites each implementation and regression. |
| R87: no ordered scope cut | PASS | All four findings fixed, plus original null-window freshness regression retained. |
| R74: git directory form | PASS | All git commands use `git -C` with the assigned absolute worktree. |
| R60: real result before review | PASS | Real worktree CLI commands and read-only instance proof above. |
| Machine, cleanup, no installed-core edits | PASS | Affected files only, at most four workers, no local full suite or installed-core changes; owned temporary cache/link/logs removed. |

Decisions made: account windows remain shared; auth/error state is per folder;
an unavailable read is an explicit unverified error, never a confirmed sign-out;
the same derived evidence formatter serves overview and usage meters.
Proposed lesson: account-wide limits and folder-local credentials are different
identities, and every presenter must keep observation provenance alongside a number.
