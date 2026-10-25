# Commitment digests and daily retros

`brain digest app` reads task records and their requirements. Its sections are
Promised, Done, Not done, Needs you, and Next. `--json` returns the same projection.
The task-store capability must be installed. A requirement's `at` and `ref` name
the promise source; missing dates and estimates remain unknown.

Done requires a completed task, a commit reachable from local main, a code path,
and evidence. A provider's external completion state alone does not prove local
implementation. Parked and dropped commitments remain in Not done with their
reasons. A ready task appears in Next only after its dependencies have proven
completion. A technical blocker does not become a request for human approval.

`brain digest app --retro --day 2026-01-01` summarizes the jobs that ended on that
local calendar date. Omit `--day` for yesterday. It reports recorded failures,
review rounds, elapsed hours and repeated compaction signals. Unknown metrics
remain unknown; this is a measurement summary, not an inferred root cause.

Set `brain set root daily_retro=on` to let the existing machine sampler write
one snapshot of yesterday per node and machine. Files live under
`.brain/daily-retro/<node-digest>/<date>.json`. This sends no messages and edits
no shared state, decisions or lessons. The sampler must already be running;
this setting does not install or schedule it. Run `digest --retro` to regenerate
stdout if later evidence changes a historical day.
