# Brain core

A generic framework for coordinating work across a tree of nodes with AI agents, on any
runtime and host. It holds no instance data: each instance binds it in its own `instance.md`
and uses it as a git submodule at `core/`.

| Path | What it is |
| --- | --- |
| `MODEL.md` | Primitives, axioms, procedures, and the runtime and host contracts. The source of truth. |
| `roles/` | Coordinator, worker and specialist protocols. |
| `skills/` | Judgment procedures: node-new, node-sync, node-brief, node-transition, learn, teach, hire, check, machine-setup. |
| `templates/` | Node, root, item, specialist and new-home templates. |
| `adapters/runtimes/` | One small file per agent runtime. |
| `adapters/hosts/` | One mapping per host, plus a small `.env` the tool reads. |
| `bin/brain` | The tool. Python 3.9+, standard library only, nothing to install. |
| `install.sh` | One-line install, new instance or another machine. |
| `tests/` | `brain selftest` (scenarios, throwaway folders) and `parity.sh` (runtimes). |

## Install

A new instance (asks for your id, runtimes and timezone):

```
curl -fsSL https://raw.githubusercontent.com/HadiKhai/brain-core/main/install.sh | sh
```

The same instance on another machine:

```
curl -fsSL https://raw.githubusercontent.com/HadiKhai/brain-core/main/install.sh | sh -s -- --from <your instance's git URL>
```

Both put a `brain` command in `~/.local/bin`. Then open your host at the instance folder and
say "set up this machine" (skill `machine-setup`).

## Use

You mostly talk to coordinators, and they run `brain` for you. By hand:

| Command | Does |
| --- | --- |
| `brain` | dashboard: tree, what needs you, coordinators |
| `brain show <node>` | everything about one node, with this machine's IDs |
| `brain new <id> --title T [--owners a,b] [--home path]` | create a node (ids nest with `/`) |
| `brain set <id> key=value` | fields; host IDs and home paths stay on this machine |
| `brain relabel`, `move`, `archive` | re-file a node |
| `brain principal add <id>` | a new person, group or organization |
| `brain owners <id> a,b` / `brain seal <id> --owners a,b --home path` | change owners; give a node its own home |
| `brain split`, `merge` | reshape nodes, keeping provenance |
| `brain revoke <principal>` | end a relationship: quarantine and archive |
| `brain sync <id>` | what happened in a node and its home since the last sync |
| `brain item add/list/scope/quarantine/restore`, `brain usable` | the library and its scopes |
| `brain specialist new`, `brain lesson`, `brain hire` | reusable experts and their lessons |
| `brain check [--fix]` | verify the mechanically checkable axioms; repair the safe things |
| `brain machine` | this machine's bindings |
| `brain upgrade` | update `core/` to the latest version, then link and check |

## Two machines, two people

- **Shared state** is the instance's git repo. **Machine state** (host thread and project IDs,
  local paths of external homes) is `.brain/local.json`, which git ignores (axiom 23).
- **Collaborators** each have their own instance, or none. They meet only in a sealed node's
  home (a shared repo), never in each other's instance (axioms 6 and 9). `brain sync` is how a
  coordinator learns what others did there.

## Changing the core

The core must stay generic: if a change needs a specific name, it belongs in an instance.
After any change:

1. `brain selftest`
2. `core/tests/parity.sh <a node folder>` (axiom 22)
3. Commit here; instances pick it up with `brain upgrade`.
