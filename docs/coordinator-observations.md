# Coordinator observations

Root's due list and file watcher report `coordinator-queue` when a bound
coordinator has more than three queued items or its oldest item has waited more
than ten minutes. Native `.brain/wake/<URL-encoded-node-id>/*.pending` files
supply local queue facts when the transport queue projection is unavailable. A coalesced file's distinct item IDs count separately;
its modification time supplies queue age. Unknown or unsafe files are retained
and do not establish facts. Transport queues already delivered to a host are
not visible in this native source.

A host exporter may supply `.brain/coordinator-runs.json`:

```json
{
  "version": 1,
  "observed_at": "2026-01-01T12:00:00Z",
  "runs": [{
    "node": "app",
    "thread": "example-thread",
    "run": "example-run",
    "status": "active",
    "started_at": "2026-01-01T11:35:00Z",
    "queued_wakes": [{"id": "example-wake", "queued_at": "2026-01-01T11:59:00Z"}]
  }]
}
```

Each included node has exactly one exhaustive queue observation for its bound
coordinator. All timestamps carry timezones. Future, contradictory, linked,
oversized or more-than-three-minute-old observations establish no host activity.
Only a matching active run with queued wakes produces `coordinator-busy` after
`coordinator_stall_minutes` (default 20). Missing host data means unknown, not idle
or busy. No host exporter is installed by this feature. A host inventory without
actual start times and queued wakes cannot produce this snapshot.

Advisory episodes identify the oldest queued item or active run, independently
of changing display ages. Ordinary watcher receipts deliver an episode once;
queue progress or a new run can establish a new episode. These are advisories,
not cancellation, rotation or permission to change host state.

When the transport owner supplies `hosts.wake_turn_queue(inst, node)`, the reader
uses its same-binding union of `turn.json` and pending-file items, including a
delivered but unconsumed turn. The hook returns known/item_count/oldest_at and
unconsumed_wake; oldest_at is enqueue time, never host run start. Unknown or
malformed queue projections remain unknown. Until that hook is installed, the
metrics source remains `native_pending_only`. Neither source can establish busy.
