# Launch policy

Declared in frontmatter; the nearest `node.md` (within its owner boundary) wins over
`instance.md`. An undeclared key restricts nothing. Values below are examples.

| Key | Example | Refuses |
| --- | --- | --- |
| `launch_allow_<role>` | `main-agent/model-a, code-agent/model-b` | a launch for `coordinator` or `worker` on any other `<instanceId>/<model>` |
| `launch_effort_<role>` | `high` | a launch whose `reasoningEffort` (or `effort`) option is unset or not listed |
| `launch_forbid` | `model-retired, retired-account` | any launch naming that instance id or model |
| `launch_cap_<instanceId>` | `4` | a fifth admitted non-coordinator launch on that provider, and any capped launch whose brief lacks the `brain-job:` mark of an open ledger job |
| `thread_pointers` (instance.md) | `lander.md` | a `t3_thread_send` to a thread that node file's line 1 named before but no longer names |

When any role key is declared, a launch must name its provider and model; an inherited
host default cannot be checked. A launch's role is a coordinator when its message starts
with the rotation marker or a `Role: coordinator` line; otherwise it is a worker.

Where it is checked:

- PreToolUse hook (`brain hook pretool`): agent calls to `t3_thread_launch`,
  `delegate_task` and `t3_thread_send`, before the host sees them. Unreadable admission or
  pointer state refuses the call.
- Core paths, before their host call: coordinator provisioning, coordinator rotation (which
  always passes an explicit selection: the declared one, else the predecessor's observed
  one), native worker launch and restart, and `brain delegate`.

Admission slots live in `.brain/launch-admissions.json`, one per job and kind (thread or
delegation). A relaunch of the same job reuses its slot; a slot is released when its job
ends or records a hand-back. Pointer history lives in `.brain/thread-pointers.json`; a
pointer change is learned at the next send.

`brain delegate` treats only a host refusal as a confirmed route failure. It then launches
the same work once as a visible thread in the node's home project on the same provider,
model and options, records the intent first, never repeats an unknown launch, and does not
adopt a thread whose observed provider or model differs.
