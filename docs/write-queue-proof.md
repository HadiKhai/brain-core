# Phase-one write queue proof

The analyzer reads files directly. It imports no core modules, takes no instance
lock and runs no instance commands. All timestamps require a UTC offset; windows
include their start and exclude their end. Command samples use completion time.
Percentiles use the nearest rank. A single caller at or above one second fails
the caller latency bar, even when p99 is below one second.

```sh
python3 -B tools/write_queue_proof.py --events "$ROOT/.brain/timings/events.jsonl" --events "$CALLS" --queue "$ROOT/.brain/write-queue" --start "$START" --end "$END" > "$REPORT"
```

Omit the second `--events` for a historical internal-timing baseline. Exit 2 means
unreadable/malformed evidence or unavailable queue evidence; the JSON report
still records unavailable queue data. Exit 0 means the report was produced,
not that the live proof passed. Inspect every gate, `evidence`, unfinished calls
and the window's `complete` flag. `COLLECTED` describes parsing completeness only.
This tool does not publish readiness or a LIVE verdict.

## Workload driver (deployment operator only)

```sh
python3 -B tools/write_queue_driver.py --root "$ROOT" --brain "$LAUNCHER" -n 200 --concurrency 20 --output "$CALLS"
```

The default prints a plan without writing anything. Add `--execute` only with
authority to write the target instance. It creates a uniquely named dedicated
proof task, then issues exactly N alternating marked inbox additions and metadata
task patches to the `brain-core` node, followed by one `status --refresh`. Setup
is a separately timed task-add call. No handbacks, readiness, lifecycle changes,
other jobs or other nodes are generated. Existing legitimate handback producers
must supply their own launch-to-exit observations to prove that family.

The exclusive JSONL output records starts before launching each process, fsyncs
each record, and records success, exit code, stderr cause, queue acceptance echo
and whole-process elapsed seconds. It preserves failures and timeouts. A timeout
can have an ambiguous acceptance: keep the request ID and inspect its durable
receipt. The driver never retries or treats timeout as cancellation of the queue.
It leaves its marked proof records in place for inspection.

The driver does not wait for effects. Run the normal queue receipt barriers for
its IDs and inspect task/inbox effects before accepting the effect gate. Re-run
the analyzer after the burst drains; its backlog is a current snapshot, separate
from the event window. Record installed source/core identity, service identity,
window, starting backlog, final backlog, oldest age and drain duration alongside
the report. Controlled restarts and crash injection require separate authority.

## Evidence binding and known gaps

`tools/write_queue_schema.py` contains field bindings. The current adapter follows
the phase-one v2 queue: `index` contains `id`/`hash`, immutable `<id>.request`
contains `version`/`id`/`hash`/`payload`, and `<id>.receipt` contains matching
`id`/`hash`, terminal `state`, `sequence` and rejection `reason`. Sequence is the
first index **byte offset**, not an ordinal. Identical retries reconcile once.
Hash mismatch, a missing receipt, orphan/duplicate receipt or a receipt after an
unapplied predecessor remains a failed reconciliation.

The index currently has no acceptance timestamps, so window selection uses
acknowledged producer IDs plus every attempted ID found in the durable index,
including timeout/nonzero outcomes and starts without completion. Unrelated indexed requests remain
visible in the total backlog. Current requests also lack an acceptance timestamp,
so exact oldest age is UNKNOWN. File mtime is not substituted for queue age.
If the producer schema gains timestamp fields, bind them in the adapter and add
the corresponding age calculation before claiming that gate.

Current legacy timing records expose only command words, internal elapsed time,
success and elapsed lock phases. They cannot distinguish task-set from task-show,
unchecked handback-enqueue from checked synchronous handback, inbox-add from read,
or refreshed status from plain status. Those remain under `observed_commands`.
Subcommands with explicit action/refresh/enqueue metadata enter the per-family
internal series. No historical command-word aggregate is relabeled as a producer.

Producer lock bypass requires explicit nonnegative integer `lock_attempts` and
`lock_acquisitions`, with at least as many internal observations per request ID
as externally measured invocations. One timing cannot cover multiple same-ID retries.
Zero rounded lock wait never proves bypass. `lock_wait_failure` classifies global
failure causes across internal and caller outcomes; an unclassified failed call
keeps the global zero-failure gate UNKNOWN. Observations with unique matching
call IDs (or unique request IDs and matching family) count once; ambiguous retry
matches remain separate. The adapter allows these field names to change in one
place when the producer instrumentation is committed. Failed calls with positive
wait are reported as suspected, never invented as confirmed lock failures.
Refresh outcomes use the same identity join and retain every unmatched invocation;
matched observations count once and preserve any failure or unknown outcome.

UNKNOWN is not a pass. This phase-one harness does not establish contention
overlap, application effects, five-minute drain capacity or restart recovery.
