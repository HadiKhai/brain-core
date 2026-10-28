# Operation timing

`brain timings` reads the local private `.brain/timings/events.jsonl` journal.
It keeps the existing last-50 default and supports:

```
brain timings --command status --node app --since 2026-10-01T00:00:00Z
brain timings --until 2026-11-01T00:00:00Z --slowest 10 --json
brain timings --command status --summary --json
brain time --node app --threshold 5 -- python3 script.py
```

Time bounds are inclusive ISO timestamps with a timezone, or Unix seconds.
`--summary` computes count, p50 and p95 over **all** matching records before
any last-50 or slowest limit. Percentiles use the nearest-rank definition.
Existing records attributed to root remain root records; history is not guessed.
New Brain commands use their selected node, otherwise their calling folder.
Command timestamps are local date strings; wrapper, hook and queue records use
Unix seconds for precision. Time filters accept both existing journal formats.

The wrapper requires `--` before the command and a Brain folder or registered home/worktree. It inherits
stdio, environment and cwd, runs argv directly, and returns the command's exit
status (128 + signal for signal termination; 127 missing, 126 other launch failure).
Only a screened executable basename, duration, status and opaque node reference
are logged. Arguments, output and environments are never recorded.

For slow commands a bounded process sample includes numeric PID/parent links,
observed first/last times and sampled CPU seconds. Samples run every 0.1 seconds
plus the cost of `ps`, with at most 100 retained process identities. Short-lived
or reparented children can be missed; this is an approximate observed tree, not
an accounting of every child. Fast commands omit the sample from their record.
Platforms without a usable `ps` still record command duration and status.
Brain commands already include slow phases and a bounded Python function profile.
The wrapper does not inject a profiler into arbitrary child interpreters.

## Optional runtime hooks (shipped, not installed)

`core/bin/brain-timing-hook` is a silent stdin-JSON entry point usable by Claude
Code PreToolUse/PostToolUse/PostToolUseFailure or a Codex host delivering equivalent
events. Adding it to user settings requires the operator. `brain link` does not
install it or change the existing safety hooks. Keep those safety hooks intact.

The host invokes the same script on both sides of each call, with:

```
{"hook_event_name":"PreToolUse","session_id":"fixture","tool_use_id":"call-1","tool_name":"Shell"}
{"hook_event_name":"PostToolUse","session_id":"fixture","tool_use_id":"call-1","tool_name":"Shell","tool_response":{"exit_code":0}}
```

`call_id` is accepted as an alternative to `tool_use_id`. Only the actual working
folder determines the Brain; a payload's `cwd` cannot redirect writes. Sessions
and call ids form a hash naming a private pending clock file; raw ids and tool
payloads are not persisted. Post consumes the pair once. Missing Pre, stale pairs,
unsupported event shapes, unrelated folders and storage failures fail open.
The hook cannot retrospectively sample a subprocess tree or profile a remote MCP
call. Use the wrapper for shell process details. Unknown runtime exit status stays
null; a failure event supplies failure status. Hook execution on any particular
host is unverified until the operator enables and tests it. Interrupted calls can
leave private pending clock files, as can missing or malformed Post events.
Pairs older than a day are not used and are removed when their Post arrives.
Hook durations use wall time to support separate Python 3.9/macOS processes;
backward clock jumps are refused, and forward clock adjustments affect duration.
The command wrapper uses a monotonic clock within its own process.
