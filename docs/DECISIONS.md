# Code decisions

## D002: Select the initial job runtime before the first ledger append

Resolve a new worktree job's runtime with the existing `worker_route` resolver
before `job_started` appends its first ledger row. The initial row then reflects
the creation-time builder route, including existing runtime pins, class defaults
and inherited model selection.

A later brief's explicit `--runtime` or `--model` choice remains a later route
update through `record_job_route`. It does not change the original committed
ledger revision. Reusing the resolver preserves runtime/model precedence and
model-aware subscription selection; this change adds no flags or routing semantics.

The regression reads the first committed ledger revision before generating a
brief, then checks that the brief retains the expected runtime and model. Its
cases cover class defaults, a node runtime pin, node and instance class routing,
and inherited model configuration without activating inherited runtime policy.

## D003: Project restart limits as protected coordinator work

Usage limits produce a protected `job-restart` due item for the job's owning
coordinator: at least three compactions or at least twice a known positive token
limit, measured from the existing successful rebrief baseline. Each counter can
trigger independently; missing evidence remains unknown. An open episode stays
open through missing telemetry or a changed limit until a successful rebrief or
terminal job evidence resolves it. Terminal handbacks, locally merged and closed
jobs do not acquire restart work. A manual compaction-restart rebrief also sets
the usage baseline when no usage scan preceded it.

The existing episode receipt keeps due and watcher identities stable as counts
change. Restart work blocks `due --quiet`, cannot be acknowledged or deferred,
and belongs to the decision wake kinds, so it does not depend on informational
usage advice. The coordinator executes the restart through the existing brief
command; reading due never stops or launches a worker.

Coordinator rotation uses the selected current runtime session's measured
context with a default threshold of 300000 tokens. Explicit token and turn
settings retain precedence, and unavailable session evidence creates no invented
usage or rotation event. Tests cover exact boundaries, independent and unknown
counters, terminal jobs, episode renewal and the quiet-due control path.
