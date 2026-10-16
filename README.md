# Brain core

A generic framework for coordinating work across a tree of nodes with AI agents on any
runtime and host. It contains no instance data: an instance binds it in its own
`instance.md`.

| Path | What it is |
| --- | --- |
| `MODEL.md` | Primitives, axioms, procedures, and the runtime and host contracts. The source of truth. |
| `roles/` | Coordinator, worker and specialist protocols. |
| `skills/` | Judgment procedures: node-new, node-sync, node-brief, node-transition, learn, teach, hire, check. |
| `templates/` | Node, root, item and specialist templates. |
| `adapters/runtimes/` | One small file per agent runtime. |
| `adapters/hosts/` | One mapping per host. |
| `bin/brain` | Mechanics: init, new, set, relabel, owners, move, archive, usable, tree, status, registry, link, check. |

## Start a new instance

```
core/bin/brain init ~/MyBrain --operator <id> --title "<title>" --runtimes claude,codex --host t3 --tz <Area/City>
```

This copies the core, creates the root node, links the runtimes, and makes the first commit.
Then fill in the operator section of the root `AGENTS.md`, and start the root coordinator
(see `skills/node-new`, step 5).

## Changing the core

The core must stay generic: if a change needs a specific name, it belongs in an instance.
After any change, run the parity test: run every runtime in a node and check that it sees
the same rules and skills (axiom 22).
