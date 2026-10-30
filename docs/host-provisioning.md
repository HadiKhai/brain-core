# Native host provisioning

The T3 adapter can use the host's supported ACP MCP transport when the current
session supplies `T3_ACP_MCP_NODE` and, where required,
`T3_ACP_MCP_ENTRYPOINT`. Without it, use the host adapter's manual workflow.
No custom host build or extra Python dependency is needed.

```
brain new app --title Application --runtime codex --brief "Build a sample." --coordinator --model selected-model
```

This explicitly requests a host project and a coordinator in the node folder.
The model can come from configured routing when `--model` is omitted. For an
existing node, use `brain host provision app --model selected-model`. Bindings
remain machine-local. Sealed external homes also receive their own host project.
The coordinator is pinned; pinning does not override a host's settlement policy.

An interrupted launch retains a local pending marker. Inspect the host before
retrying and bind an already-created coordinator with `brain set app
coordinator=<thread-id>`. Provisioning can then finish pinning without launching
again. An existing host project can likewise be bound with `host_project=<id>`;
if the pending operation produced no coordinator, reconcile that operation before
starting a new one. Never erase a pending marker merely to retry blindly.

The coordinator's initial brief requests `brain host wake-setup app`. This
creates the portable webhook schedule and the host task, verifies that the task
binds this exact coordinator, and stores the returned URL privately. This command
must run inside that coordinator. Missing T3 Connect remote access leaves the
host task id available for reconciliation and writes no invented wake URL.

Wake transport acceptance does not prove that a coordinator queued or started a
run. Every route retains a binding-fenced consumption episode until `brain due`
renders its captured reasons successfully. Shell success is recorded as a shell
outcome, never as an HTTP status. Explicit `wake_enabled=off` and known shell
no-ops retain pending reasons and show a disabled notice in the owner/root due
views. Disabling a route never activates a fallback.

An unconsumed episode becomes a visible stall after 120 seconds. Start state
remains UNKNOWN: unrelated newer host runs cannot confirm delivery. Without a
host-supported correlated receipt and idempotent retry contract, the core does
not blindly resend an accepted or unknown attempt. Reconcile the host before
recovery; re-provision a webhook when its coordinator changes. A known unchanged
webhook from a retired episode is refused. Preexisting remote schedule enablement
and binding still require host-side inspection.

External watchers should invoke `brain watch --once` on each pass to reconcile
pending episodes independently of rule refire intervals, and treat a failed wake
enqueue as a retryable failure. A successful enqueue proves durable local queue
publication, not host progress. The watcher itself is instance-owned code.

`brain host watch app --url <pull-request-url>` starts the host's PR watch from
the bound coordinator and checks the returned confirmation. The host maintains
checks/reviews/conflict watching. This command does not infer merge readiness.
Live host reachability, remote access and provider behavior require an actual
host session; synthetic host tests prove the core request/binding behavior.

## Recurring sync and briefs

Add a portable record, then apply it from that node's bound coordinator:

```
brain schedule add morning --when "daily 09:00" --node app --brief "Run node-sync, then node-brief." --provider codex
brain host schedule app --schedule-id morning --provider codex
```

Supported triggers are `hourly`, `every N minutes`, `daily HH:MM` and
`weekdays HH:MM`. Wall-clock triggers use the host's local timezone; ensure the
host clock matches the intended schedule. The host executes the automated agent
brief. Repeating unchanged setup reuses its binding; changed records and unknown
legacy bindings require reconciliation rather than creating duplicate tasks.
