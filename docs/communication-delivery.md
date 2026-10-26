# Notification delivery and native task launch

`brain notify` is the typed Brain notification boundary. Machine-local root
`message_policy` may contain `quiet_start` and `quiet_end` as `HH:MM` values.
The window uses the instance's configured timezone, includes its start, excludes
its end, and may cross midnight. An invalid timezone or policy refuses a notice;
an absent policy preserves existing notification behavior.

During that window, landing, park and FYI notices are retained in the local
communication store without inbox publication or a coordinator doorbell.
Same-key recovery rechecks the current policy, including when an earlier attempt
saved a receipt outside quiet hours and stopped before publication. A retry in
quiet hours durably defers that retained notice without changing its identity or
content. Deferral remains in force after quiet hours until morning publication.
`brain morning-brief <node>` publishes one immutable snapshot of deferred notices
per local date in that node's inbox. Repeated calls do not publish another brief.
Interrupted publication recovers the exact prior snapshot; later work remains
retained for a subsequent daily brief. The original notices remain available.
`brain due` reports retained notices without waking a coordinator for the brief.

`brain morning-brief root --operator` explicitly aggregates retained quiet-hour
notices from active nodes into one root inbox snapshot per local delivery date.
It requires the same explicit policy and refuses publication during quiet hours.
Its receipt is separate from node inbox receipts: a coordinator reading a notice
does not remove it from the operator summary. Empty calls consume no daily slot.
An interrupted snapshot keeps its exact content and idempotent inbox marker;
recovering it reserves the recovery date, leaving later work for the next date.
This command publishes locally and does not contact an operator or configure a
schedule. Node-only briefs retain their existing snapshot-date semantics.

Typed questions retain their immediate question path during quiet hours.
There is no urgency override for an untyped notice. Routine deadline defaults
remain distinct from authority to execute outward or destructive actions.
Morning briefs never wake root; root's wake channel remains question-only.

This boundary does not intercept arbitrary host sends, assistant final messages,
email or other messaging channels. Inbox publication is not proof that a person
received a host notification. No schedule or host channel is configured here.

`brain tasks launch-plan <node> [task]` validates ready-task authority,
dependencies, holds, code scope and a complete node-local contract. In required
separation mode, the contract must state its actual `Why separate`; the planner
does not invent a generic reason. Execute the returned native preparation
commands to create and register the task's worktree and brief.

`brain tasks launch <node> <task>` then launches that prepared task through the
native lifecycle adapter. It requires one exact open task-bound job, active task
state, the same current contract bytes and valid authority. It checks holds and
dependencies again. The initial lifecycle launch repeats these checks before its
host mutation, including launches reached through native maintenance. A prepared
attempt retains its own capacity slot; competing attempts still count. Unknown
launch outcomes retain the lifecycle intent rather than repeating a launch.
Markdown build-order text provides no launch authority.

The opt-in host exporter reads a bound coordinator's documented `activeRunId`
and matching `recentRuns[].startedAt` using `t3_thread_read`. A matching running
run, an absolute start time and fresh unchanged bindings are required. No
`updatedAt` or request timestamp substitutes for a run start. Busy advisories
combine this evidence with Brain's native coalesced queue. Pending host questions
are not queued wakes. Missing, stale or conflicting start/queue evidence remains
unknown. Host-only queue entries outside Brain's owned queue are not measured.
