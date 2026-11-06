# Launch policy

Declared in frontmatter; the nearest `node.md` (within its owner boundary) wins over
`instance.md`. An undeclared key restricts nothing. Values below are examples.

| Key | Example | Refuses |
| --- | --- | --- |
| `launch_allow_<role>` | `main-agent/model-a, code-agent/model-b` | a launch for `coordinator` or `worker` on any other `<instanceId>/<model>` |
| `launch_effort_<role>` | `high` | a launch whose `reasoningEffort` (or `effort`) option is unset or not listed |
| `launch_forbid` | `model-retired, retired-account` | any launch naming that instance id or model |
| `thread_pointers` (instance.md) | `lander.md` | a `t3_thread_send` to a thread that node file's line 1 named before (in any commit, or as observed) but no longer names |

When any role key is declared, a launch must name its provider and model; an inherited
host default cannot be checked. A launch's role is a coordinator when its message starts
with the rotation marker or a `Role: coordinator` line; otherwise it is a worker.

Where it is checked:

- PreToolUse hook (`brain hook pretool`): agent calls to `t3_thread_launch`,
  `delegate_task` and `t3_thread_send`, before the host sees them. Unreadable pointer state
  refuses the call.
- Core paths, before their host call: coordinator provisioning, coordinator rotation (which
  always passes an explicit selection: the declared one, else the predecessor's observed
  one), native worker launch and restart, and `brain delegate`.

Former pointers come from every committed version of the pointer file (read once per commit)
plus each line 1 seen before every agent tool call (so before an `Edit`, `Write`, Codex
`apply_patch` or shell command replaces it) and every send, kept in
`.brain/thread-pointers.json`. The first send to a replaced thread is refused.

`brain delegate` treats only a host refusal as a confirmed route failure. It then launches
the same work once as a visible thread in the node's home project on the same provider,
model and options. Its checks run before the intent is recorded, so a refusal there leaves
the failure retryable; it records the intent before the launch, never repeats an unknown
launch, and does not adopt a thread whose observed provider or model differs.
