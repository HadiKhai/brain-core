# Code decisions

## D001: Node-local full gates and native machine capacity

Full-gate exclusion is keyed by the resolved node id within an instance. The
stable lock is `.brain/gates/<sha256-node-id>.lock`; display labels do not define
identity. `gate-lock --node <node>` selects a node explicitly. Without that option,
node folders, registered homes, workbenches and worktrees are ranked together by
the most specific containing path. Repeated anchors for one node are deduplicated;
equal-depth matches for different nodes and unbound context refuse. A containing
node folder cannot mask a deeper registered home or workbench.

Native monolithic verification and lane verification acquire the node lock before
joining machine admission. A lane run retains the node lock across every command,
while each command joins the native FIFO queue afresh. Different nodes can run
concurrently without same-node waiters consuming machine slots. Capacity remains
in the existing machine policy: configurable `verify_day` and `verify_night`, with
defaults of two daytime slots and one nighttime slot. A gate wrapper does not
reserve an additional machine slot.

Nested commands retain the same node's descriptor through exec and explicit
`pass_fds`. Inherited proof binds the node id, stable path, regular-file inode and
held open-file-description lock; incomplete, closed, stale, unlocked or mismatched
proof refuses. Owners close their descriptors without explicitly unlocking, so a
surviving verification child retains exclusion. Lock files are never replaced or
unlinked during normal operation. Lock-path safety checks reject linked and
nonregular targets without interfering with scoped verification-log cleanup.

Regression tests use isolated instances and controlled process barriers to prove
same-node exclusion, different-node overlap, configured capacities, inherited
ownership and cleanup, without generating system load.

Isolated scenario fixtures temporarily remove only enclosing gate environment
proof from both their subprocess environment and in-process environment, restoring
it during cleanup. They retain the enclosing descriptor and lock. Runtime
cross-node proof refusal remains unchanged. An in-repository regression runs
existing subprocess and native lane fixtures under a real gate wrapper, then
checks that enclosing proof is restored and its descriptor still owns the lock.

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



## D801: Separate Docker diagnostics from explicitly pinned cleanup

Read-only leftover reporting grants no cleanup authority. A node enables cleanup
with `docker_cleanup_prefixes` (literal namespaces ending in `-verify-*` or
`-job-*`), `docker_cleanup_host` (trusted local Unix endpoint), and
`docker_cleanup_daemon` (the exact Docker daemon ID). The default mode is `stop`.
Conflicting environment selectors, a different effective endpoint, or a changed
daemon ID refuse mutation. Commands pin that endpoint and clear ambient Docker
and Compose selectors; no project compose file is loaded.

A container also needs the exact node/machine `brain.docker-scope` and both
`brain.owner-pid` and `brain.owner-stamp` labels. Only a definitely absent process
qualifies; unknown or recycled identities retain resources. Every connected
project, Testcontainers session, named volume and network peer participates in
ownership checks. Preview/dev/production identities, bind mounts, automatic
removal, incomplete inventories and resources younger than 30 minutes veto work.
Cleanup stops only immutable inspected container IDs, checks all peers immediately
before each mutation, and reports a stopped ID only after confirmation. One
five-second deadline bounds each hook call. Missing Docker/configuration is a
quiet no-op; failure returns diagnostics without replacing a run's primary result.

## D802: Remove only unused, explicitly owned test Docker resources

`docker_cleanup_mode: remove` adds deletion after the stop pass. Container removal
uses exact IDs without force or volume flags. A restart is refused by Docker;
container removal never implicitly removes volumes. Volumes and networks need
the same scope, owner and age proof as containers. Every container, including
stopped and foreign containers, participates in the attachment veto. Network
inspection must show no endpoints. Reinspection must match the captured resource
metadata before deletion; Docker's own in-use refusal remains authoritative.
Errors keep resources for a later run. No images, bind contents, compose files,
global prune, forced disconnect or host lifecycle APIs are touched.

## D803: Bind machine reservations to an explicit operating-system boot

New reservations retain a boot identity in addition to parent/child process birth
identities. Linux reads the kernel boot UUID; macOS reads `kern.boottime` seconds
and microseconds. A known mismatch reclaims an earlier-boot reservation without
probing its numeric PIDs. Missing or unknown boot readings preserve existing
process identity behavior and never imply death. Legacy queue rows remain valid.
No uptime subtraction, clock-derived estimate, service change or reboot probe is
needed. Queue readers accept the optional field and keep old compatibility.

## D804: Show bounded CPU observations when machine admission is held

Held machine diagnostics collect up to three top CPU observations using one
one-second `ps` command. Only PID, CPU percentage and executable basename are
shown; command arguments and full paths are excluded. Invalid/nonfinite readings
or probe failures leave the diagnostic unknown. This observation explains current
CPU activity without claiming causation or changing admission policy. Unheld
state reads do not run the probe. Percentages above 100 are valid on multicore
systems.

## D805: One durable terminal producer through admission and cleanup

A managed machine run creates its private record before admission and owns the
terminal transition through cleanup. It retains the original cause and one stable
terminal event identity; repeated finalization cannot overwrite an ended run.
Success is provisional until cleanup finishes. Cleanup attempts every action,
records secondary errors and receives the operation's primary exception explicitly;
a handled exception in a caller cannot suppress a new cleanup failure.

A definitely dead owner can recover an abandoned run. Unknown identity is recorded
as uncertainty, never inferred death. A live managed owner's queue-release window
cannot produce a competing recovery cause. Pending terminal records are retained
until the transport's stable receipt confirms actual delivery. Failed enqueue is
retried using the original event identity and reason. Cross-route transport claims
and accepted-delivery receipts belong to the wake transport; producer code does
not send directly. Catchable termination unwinds through the same finalizer and
restores embedding applications' signal handlers.

`machine_run(kind, instance, node, job=None, clock=None)` is the context API;
`machine_wait(..., token=token)` admits the managed token and `token.result` holds
the command result. `machine_cleanup(token, actions, primary=error)` runs cleanup.
The admission-only command uses that context without changing its public behavior.
Other command runners integrate the same context around admission, launch and
cleanup rather than creating a second terminal path.

## D806: Keep worktree cleanup facts separate from host settlement

`worktree_cleanup_result(node, branch)` returns versioned facts: workspace
presence, every recorded job incarnation, thread references and the required
host inspection sequence. It never attests host settlement or thread ownership.
Missing workspaces forbid messaging stale threads. The host owns liveness,
pending-request, coordinator and open-job protection checks.

`remove_worktree(..., thread_hook=callback)` optionally runs that host integration
before deletion. Only an explicit `True` result permits removal; refusal or an
exception retains the checkout. Branch and live ownership checks run again after
the callback. The returned post-removal result contains the original workspace
reference for host inspection without authorizing a stale-thread message.
Default callers preserve their existing checklist and removal behavior.
