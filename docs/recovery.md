# Recovery rehearsals

Run the real-agent rehearsals on both runtimes:

```sh
LIVE_CODEX_BIN=/path/to/codex python3 tests/live.py --runtimes claude,codex -k recover_
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

## Event-stream recovery contract

Snapshot files remain authoritative during the event comparison period. The event
stream records Brain's own state writes: ledger, bindings, worktree records,
inbox, status, items, schedules and node metadata. External Git operations in
worktrees are outside the stream. Worktree setup is a separate operation.

A locked writer appends a metadata-only `begin`, performs its state mutations,
then appends `commit` after its last successful state write. Both records use the
instance clock and an operation reference. Each record is one O_APPEND line,
fsync'd. The commit lists affected resources, including removals; resource
fingerprints contain no note or message bodies. Display text is screened with the existing
scrub. Schema keys, timestamps, operation references, opaque node/resource identities and
hashes are never rewritten. Resource paths are optional scrubbed display fields; membership
uses a separate opaque scope. Monthly rotation happens under the existing instance lock.

A begin without a commit identifies an interrupted operation. The events-only
view must fail closed while such an operation is unresolved. The next locked
refresh reconciles the operation against authoritative snapshot files and
appends `reconcile`, explicitly flagging the recovery. A concurrent operation
still in progress must not be mistaken for an interrupted operation. Comparison
also detects state changes with no hook; it must run before accepting a new
snapshot into the event-derived view so that a later write cannot hide a gap.
This protocol provides recoverability, not atomic visibility across files,
Git commits and the event log. The write-without-event stop condition concerns
missing hook coverage, demonstrated by the scripted-day and mutation checks;
it does not prohibit the documented interruption window.

Hooks never acquire the instance lock. After a successful inbox append, a hook
writes one short metadata-only O_APPEND line to a hook outbox. The next locked
command folds complete outbox records into the stream. Durable per-file byte acknowledgements
provide idempotence across interruption without truncating an outbox while a hook appends. The hook hash is an
invalidation: folding observes the latest inbox hash under the writer lock rather than
replaying an obsolete hook-time hash. Folding runs before and after status maintenance.
Hook delivery time and journal-fold time are distinct; preserve the original timestamp.

Switching `status_source=events` remains an explicit coordinator/operator action,
requires the comparison gate and this recovery rule, and never happens
implicitly. Snapshot files stay readable after the switch. On startup, a missing
or interrupted write must be reconciled from snapshots and flagged before the
events-only view can become available again.

Initialization records the first complete instance as a bootstrap baseline; no prior
Brain state exists to replay. A rejected command without persisted changes records
`abort`; a partially persisted failure retains its unresolved begin. A completed write with
a negative verdict (not-ready hand-back or check with remaining problems) commits with its
exit code. A generated-status maintenance failure warns and commits the primary mutation,
invalidating the unavailable projection until a refresh; it never invites a duplicate retry.
Operation leases distinguish an active writer (including unlocked verification/setup) from a
dead process. A locked refresh never reconciles another live writer.

Comparison checks Brain-derived facts and Active work. External worktree Git state, local-merge
markers, ready/preview signals affected by external Git, and clock-derived due counts are
classified as out of scope. Resource identities and hashes still detect missing hooks even
when an omitted write changes no displayed fact. Pre-write `coverage` checks record gaps
before a later command can overwrite evidence; they never claim to have compared views.
The comparison-day gate requires real view comparisons spanning a day as well as clean
recent coverage. Refresh timestamps and machine telemetry are outside the projection.
Lesson prose, report bodies, inbox messages, schedule briefs and backlog item text
never enter events: only generated presence/count metadata and resource hashes do.
Thread and PR references remain metadata; ambiguous reused branches lose the hint
rather than attributing an old thread to a new job.

`brain events <node> --compare --json` lists unexplained difference IDs. A coordinator
may review one and run `brain events <node> --explain <id> --reason "<reason>"`;
only a fingerprint is retained, and the full rationale belongs in decisions.md.
The switch gate requires a matching current view, a comparison spanning at least a
day and no unexplained differences during the last day. Root switching requires
that gate for every existing non-archived node. Explanations never bypass an active
or interrupted write, missing current view, or pending hook metadata.

Malformed or partial advisory records fail the events view closed. The next locked writer
validates journal, outbox, cursor and comparison-history data, quarantines rejected bytes in
0600 `.brain/*.rejected.*` files, and records flagged stream damage. It advances the outbox
acknowledgement without truncating the source. Snapshot writes continue while files are
authoritative; a locked `status --refresh` reconciles from files and explicitly clears the
flag. Rejected bytes may contain incomplete private hook data: do not publish them. A lost
or corrupt derived index is rebuilt from retained journal bytes. Ordinary journal reads do
not rescreen historical structure. These guarantees concern process interruption and
observable append failures, not cross-file or power-loss atomicity.

A derived materialized index retains resource hashes, projections, open operations, damage
and outbox acknowledgements. Normal commands validate file identity/size and a small tail
anchor, then seek only new journal bytes. Rotation retains the index across rename; detected
replacement, truncation or edits rebuild it. Comparison indexing retains the last day plus
the first real comparison per node. Audit listing can still read complete history explicitly.
These caches are disposable, are not sources of truth, and do not authenticate arbitrary
coordinated edits to retained logs and caches.

Generated facts and tables do not make the Stop hook demand an immediate status commit.
Brain records provenance for its own Now cap and archive, allowing the hourly/close commit
to include them while preserving pre-existing judgement/archive dirt. Other Now edits still
require coordinator action. Only affected nodes are maintained; initial indexing does not
rewrite unrelated status files. Unique bounded thread/PR cells preserve spaces and escaped
pipes. The State column is generated; move handwritten State notes to Now or decisions.

Current projections are captured from snapshot rendering and retained in commit records.
`--from-events` renders that committed projection without consulting snapshot content. This
is a metadata projection cache, not typed event-fact reconstruction; resource-hash comparison
is the independent missing-hook detector. Typed reconstruction remains an explicit scope
limit for the coordinator before accepting phase 4 for a real instance.

Focused event scenarios live in tests/test_events.py. They cover a scripted job
day, removal of a commit or entire operation as mutation controls, recovery,
lock-free/idempotent hook folding, privacy screening, rotation, explicit switching,
thread-reference retention, Now overflow and status commit throttling. Full-suite
and train-rehearsal proof must be reported separately from those focused scenarios.

The atomic writer syncs the replacement file before rename and its containing
directory afterward. A failure before rename preserves the old file; a failure
syncing the directory is reported after the complete new file becomes visible.
The ordering tests in `tests/test_atomic_durability.py` cover both cases. They do
not simulate power loss or establish a cross-file/Git transaction, and persistence
of newly created ancestor directories is outside this one-file guarantee.
