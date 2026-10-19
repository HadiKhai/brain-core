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

## Executed at this checkpoint

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
- A fresh `python3 -B tests/live.py --runtimes codex --jobs 4` attempt returned HTTP 400:
  `The 'gpt-6.1-sol' model is not supported when using Codex with a ChatGPT account.`
  It was stopped after repeated identical rejections. These are infrastructure failures,
  not evidence that the behavioral scenarios pass or fail.
- No further Claude calls were made. Global login and model settings were not changed.
- The earlier T3 hand-back probes are recorded in `adapters/hosts/t3.md`; they were not
  repeated at this checkpoint. Browser rendering was not repeated either.
- The final fixes have regression coverage but have not received another independent
  review after round eight. A fresh successful live run on both runtimes remains required
  before treating the complete release gate as passed.

No active instance was upgraded, and no checkpoint changes were pushed.
