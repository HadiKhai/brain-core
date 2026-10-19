# Verification checkpoint: 2026-10-19

The changes since `90a3a39` are a local checkpoint. The current live-runtime gate is
incomplete; this checkpoint is not an instruction to upgrade an active instance.

## Implemented

- Job duration, commit counts, review rounds and outcomes in each node's `job-ledger.md`.
- Timestamp offsets and `brain stale` for comparing status with host activity.
- Dashboard activity indicators, opt-in notifications and a per-server API key.
- Instance locking, scoped commits, stop-hook resilience and scan error reporting.
- Never-name scans of working files, staged blobs, branch names, commit messages and
  populated submodules recursively; unavailable submodules make the result incomplete.
- Round-eight fixes for staged content, submodules, tracked deletions and sealing output.
- Runtime links follow registered nodes instead of recursively walking Git object storage.
  The first full suite reproduced a disappearing `.git/objects` directory during the old
  walk. A regression case now checks that unrelated folders receive no runtime links.
- Failed worktree setup returns a nonzero status while retaining the worktree and its output.
- Dashboard clocks, chart labels, job times and decisions use the instance timezone, including
  historical daylight-saving offsets. A question that clears and returns can notify again.
- Explicit live-runtime executable selection, executable/version reporting, and blocked-runtime
  handling. A CLI process failure stops further queued calls to that runtime and keeps the
  overall gate nonzero; behavioral failures remain distinct.

## Executed after e63d682

- `python3 -B -m unittest discover -s tests -p test_brain.py`:
  **71 tests passed**, 78.373 seconds. New regressions cover setup failure preservation and
  stopping unavailable-runtime calls without reporting a green gate.
- `LIVE_CODEX_BIN=<selected codex executable> python3 -B tests/live.py --runtimes codex --jobs 4`:
  **14/14 live scenarios passed** on codex-cli **0.162.1**, after the final source changes.
  An earlier complete run using the same version also passed 14/14.
- `tests/ui-checks.js`, evaluated in the T3 collaborative browser against a throwaway populated
  instance: **19 checks passed**. Covers UTC, Beirut and New York clocks in winter and summer,
  chart labels, decision/job dates, keyed navigation, first-paint quietness, duplicate prevention,
  and a question returning after resolution. Native notification construction was intercepted;
  this proves selection behavior, not OS delivery.
- Open notes, close notes and reload in the real browser: the API key remained in the URL,
  both projects loaded, and the live connection returned.
- `git diff --check`: passed.

## Prior checkpoint e63d682

- `python3 -B -m unittest test_brain.Hardening8 test_brain.Specialists` from `tests/`:
  **5 tests passed**.
- `python3 -B -m unittest discover -s tests -p test_brain.py`:
  **69 tests passed**, 76.782 seconds, after the runtime-link fix.
- `git diff --check`: passed.
- Read-only scan of a real development repository, including working files, index and
  Git history, using a synthetic absent term: 0.25 seconds, zero read errors.

## Live-runtime evidence and limits

- The inherited completed run before these final fixes passed all 14 Codex scenarios.
  Claude passed two scenarios; its remaining 12 returned the subscription session limit.
  Those 12 do not establish scenario behavior.
- The first fresh `python3 -B tests/live.py --runtimes codex --jobs 4` attempt returned HTTP 400:
  `The 'gpt-6.1-sol' model is not supported when using Codex with a ChatGPT account.`
  That attempt used codex-cli **0.144.4** from PATH. Selecting the separately installed
  **0.162.1** executable resolved it; the successful full runs above supersede that blocker.
- No further Claude calls were made. Global login and model settings were not changed.
- The earlier T3 hand-back probes are recorded in `adapters/hosts/t3.md`; they were not
  repeated at this checkpoint. Dashboard browser checks were repeated as described above.
- The final fixes have regression coverage but have not received another independent
  review after round eight. A fresh successful live run on both runtimes remains required
  before treating the complete release gate as passed; Codex has now passed, Claude remains
  pending its subscription reset.

## Functional boundaries

- Dashboard activity comes from recorded work, Git and filesystem changes. It does not query
  live T3 agent states, and a recent file write is not proof that an agent is still running.
- Desktop notifications require an open dashboard page. There is no background notification service.
- Job metrics cover elapsed time, commits, review counts, outcomes and causes. Automatic model
  usage/cost accounting is not implemented.
- The second-machine installation has throwaway-folder coverage; a second physical laptop
  has not been exercised at this checkpoint.

No active instance was upgraded, and no checkpoint changes were pushed.
