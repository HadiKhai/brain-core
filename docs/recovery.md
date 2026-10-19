# Recovery rehearsals

Run the real-agent rehearsals on both runtimes:

```sh
LIVE_CODEX_BIN=/Users/hadikhai/.bun/bin/codex python3 tests/live.py --runtimes claude,codex -k recover_
```

Use `--keep` to retain successful fixtures; failures retain theirs automatically. Each scenario
starts a fresh session without its predecessor's conversation. Checks judge files, hand-backs,
ledger rows and git state, rather than accepting the agent's claim of success.

- `recover_coordinator_replaced`: an open job has a completed unified hand-back, review evidence,
  judgement sections and an inbox message. A replacement discovers it through `due`, checks
  readiness, performs an explicitly authorized local merge, records the lessons outcome, prunes
  and refreshes facts. It preserves prior judgement and the worker record, does not repeat worker
  verification, and reports only new inbox needs.
- `recover_worker_other_runtime`: the previous runtime's job has a partial commit, dirty tracked
  work and an untracked note. After a recorded usage-limit switch, a fresh worker on the other
  runtime receives the regenerated brief. It keeps that work, finishes and commits the job,
  leaves one ready hand-back, and preserves main and coordinator-owned files. The interrupted
  work and usage-limit event are fixtures; this does not exhaust a real subscription or test
  host cancellation.
- `recover_usage_before_two_jobs`: fixture `usage.json` puts Claude above the stop line. The
  coordinator prepares two Codex launch plans and generated briefs with subscription, host
  provider and reasons. A pinned Claude node keeps its pin, gets an inbox notice and waits for
  an explicit alternative choice. Host launches are unavailable: this proves routing decisions
  and launch artifacts, not host execution or live billing readings.

Run the cheap crash rehearsals, or the full unit suite:

```sh
BRAIN_CORE=$PWD bin/brain selftest -k recover_sigkill
BRAIN_CORE=$PWD bin/brain selftest
```

- `test_recover_sigkill_status_handback_inbox_and_jobs`: a child-only replacement hook pauses
  immediately before and after `os.replace`; SIGKILL leaves the exact old or complete new file
  for status refresh, hand-back, inbox reported ids and ledger writes. A subsequent writing
  command must finish within ten seconds, proving the instance lock is available. Inbox delivery
  may repeat if killed before reported ids persist; after persistence it stays acknowledged.
- `test_recover_sigkill_usage_poller_restarts_and_keeps_last_reading`: kill a real watch process
  while it owns both poller locks, restart and kill again. Readers retain the last complete
  fixture reading and its original timestamp and age. Restart acquires stale-PID locks and
  a third watch process publishes a complete new reading. Only subscription lookup is injected;
  locking, cache writes and reader commands are real.

The coordinator rule explicitly keeps old unresolved requests in internal status, without repeating
them as reminders in a recovery report. Job-ledger writes use the same atomic replacement helper
as hand-backs and generated status.

SIGKILL may leave an unused same-folder temporary file. These tests establish per-file process
crash safety and lock release, not a transaction across files/git commits or power-loss durability.
Review completion is fixture evidence; these rehearsals do not test independent reviewer quality.
