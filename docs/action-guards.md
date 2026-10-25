# Declared tool action guards

`brain hook pretool` accepts runtime JSON with `cwd`, `tool_name` and
`tool_input`. Runtime adapters register it for `PreToolUse` after an explicit
link/install step. Merely editing an adapter does not activate a real hook.

Declare textual glob patterns in a node's instructions:

```markdown
## Forbidden actions
- git push*
- shell: rm -rf*
- tool: delete_remote_data
```

Unprefixed and `shell:` patterns match anywhere in a shell command, including
compound commands. `tool:` patterns match a tool identity. Use executable patterns,
not prose descriptions of intent. Rules inherit down to an ownership boundary.
Quoted examples may conservatively match. Encoding a command or invoking an
unrecognized runner is outside this textual guard's coverage; it is not a sandbox.

For an explicitly authorized exact command, run:

```sh
brain allow app 'git push origin fix/range' --once --cwd /path/to/job
```

This records an allowance without running the command. The next matching command
in that node and working folder consumes it atomically before permission returns.
Changing the command, working folder or rule set cannot reuse it. Concurrent hooks
cannot consume one allowance twice. Local audit events store action digests and
absolute timestamps, without the command body.

Matched actions emit the runtime's `PreToolUse` deny response. Malformed input,
unknown node context and bookkeeping errors fail open so a broken hook cannot
break a runtime. Absence of a denial therefore is not a general permission grant.
