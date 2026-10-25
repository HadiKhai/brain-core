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

`brain host watch app --url <pull-request-url>` starts the host's PR watch from
the bound coordinator and checks the returned confirmation. The host maintains
checks/reviews/conflict watching. This command does not infer merge readiness.
Live host reachability, remote access and provider behavior require an actual
host session; synthetic host tests prove the core request/binding behavior.
