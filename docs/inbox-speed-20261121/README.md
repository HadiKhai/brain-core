# Inbox and wake read performance

Builder D, based on `6c716deda35684ee186fcc6c7e75b54c5b7b087a` (7.5.57).
Only reminders.py, status.py and hosts.py production code changes. No storage
format, command_reads.py, status_refresh.py, transport delivery or exit-code changes.

The private Brain was cloned with macOS `cp -cR` before profiling. Each command
uses its own clone; outward symlinks are removed from the clone, never followed
for writes. Host delivery, background launch and provider sync are mocked. No
live Brain command is profiled and no live instance data is committed here.
The clone's lifecycle file is 2,029,882 bytes. These are local source profiles,
not a release speed-gate result or proof on the other laptop.

| Profile | Before calls | Before seconds | After calls | After seconds |
| --- | ---: | ---: | ---: | ---: |
| Inbox command | 1 | 14.465034 | pending | pending |
| lifecycle_read, inbox | 1,700 | 9.929087 cumulative | pending | pending |
| JSON raw_decode, inbox (all JSON) | 1,715 | 9.093405 cumulative | pending | pending |
| shortest_ids | 1 | 0.697661 cumulative | pending | pending |
| status.py:1902 prefix genexpr | 2,884,902 | 0.586339 cumulative | removed | pending |
| Wake read side, twelve queued files | 12 | 59.753166 wall | pending | pending |
| watch_groups, wake | 12 | 59.494235 cumulative | pending | pending |
| due_items, wake | 134 | 13.177800 cumulative | pending | pending |

`measure.py` reproduces the two profiles. Its wake workload revalidates twelve
synthetic, expired due-question references against the copied real instance,
using the same function as wake_drain. It never sends or consumes a wake. The
baseline returned false for all twelve files (correct retirement). It is not a
complete wake transport benchmark. Profiles are concurrent with inbox, matching
the order's parallel-read requirement; cProfile adds overhead.

Implementation:

- Cache one validated lifecycle generation by absolute path, mtime_ns and size,
  additionally fencing device, inode and ctime. Every lookup retains no-follow,
  single-link and descriptor-path validation. Read snapshots remain detached via
  a process-local pickle of already validated JSON; only request-ID lookup shares
  the decoded value. Writes invalidate the cache.
- Find shortest unique IDs using sorted distinct IDs and their two neighbours,
  preserving duplicate and prefix semantics without all-pairs comparisons.
- Scope due identity observation to a drain. Reuse it only while source, due
  receipt and lifecycle generations hold. Changed or unknown evidence retains
  pending work; shown, route and terminal checks still read afresh per file.

Checkpoint validation: seven new regression checks passed; the initial count
check counted unrelated JSON consumers and failed (2 versus 1). Its spy is now
limited to the lifecycle module; recheck pending. The first attempted local run
was interrupted after 133.63 seconds before any test completed because the
fixture copied large scratch data from docs. Scratch now lives in node_modules,
which instance initialization already excludes. No timeout was increased.

Machine: initial admission allowed, 21.2 GB free memory, 105 GB free disk, normal
thermal state. Latest observation was battery power at 51%; no further local
heavy runs. Codex latest cached weekly reading: 91%, below this order's 93% cap.
No paid model/provider calls. Cloud full suite and cross-provider review pending.
No merge, tag, release, install or upgrade is authorized to this builder.
