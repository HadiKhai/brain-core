# Schedule providers

Schedules remain portable `when`, `node`, and `brief` records. An optional
`provider` is an explicitly selected instance runtime, not an inference from a
task id or a model name.

```
brain schedule add hourly-check --when hourly --node app --brief "Check work." --provider codex
brain schedule bind hourly-check host-task-id --provider codex
```

When binding a host task, copy its selected runtime with `--provider`. The local
binding overrides the shared declaration. Rebinding without `--provider`, or
unbinding, clears the previous local provider. Old string host ids and schedules
without provider metadata continue to work. Orphan provider metadata is ignored.

`brain check` warns when the recorded provider differs from the target node's
explicit runtime pin. It resolves moved nodes and redacts the target's private
terms. A warning does not change a host task, schedule, or node pin. Host execution
and the provider actually selected by a remote host require host evidence.
