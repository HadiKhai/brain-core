# Brain core

A generic framework for coordinating work across a tree of nodes with AI agents, on any
runtime and host. It holds no instance data: each instance binds it in its own `instance.md`
and uses it as a git submodule at `core/`.

| Path | What it is |
| --- | --- |
| `MODEL.md` | Primitives, axioms, procedures, and the runtime and host contracts. The source of truth. |
| `roles/` | Coordinator, worker and specialist protocols. |
| `skills/` | Judgment procedures: node-new, node-sync, node-brief, node-transition, home-job, review, learn, teach, hire, check, machine-setup. |
| `templates/` | Node, root, item, specialist and new-home templates. |
| `adapters/runtimes/` | One small file per agent runtime. |
| `adapters/hosts/` | One mapping per host, plus a small `.env` the tool reads. |
| `bin/brain` | The tool. Python 3.9+, standard library only, nothing to install. |
| `install.sh` | One-line install, new instance or another machine. |
| `tests/` | `brain selftest` (scenarios, throwaway folders) and `live.py` (the same use cases on every runtime). |

## Install

A new instance (asks for your id, runtimes and timezone):

```
curl -fsSL https://raw.githubusercontent.com/HadiKhai/brain-core/main/install.sh | sh
```

The same instance on another machine:

```
curl -fsSL https://raw.githubusercontent.com/HadiKhai/brain-core/main/install.sh | sh -s -- --from <your instance's git URL>
```

Both put a `brain` command in `~/.local/bin` (and on your PATH), and a short pointer to the
instance in each runtime's user-wide instructions (`--no-global` skips it). Then open your host
at the instance folder and say "set up this machine" (skill `machine-setup`).

Several brains on one machine (say one personal, one for an employer) never share files. Give
each its own folder and, if you like, its own runtime config folders:

```
curl -fsSL https://raw.githubusercontent.com/HadiKhai/brain-core/main/install.sh | sh -s -- --dir ~/WorkBrain --name work --global claude=~/.claude-work
```

The one `brain` command reaches all of them. It uses the brain whose folders you're in (its
instance, or a node's home, workbench or worktree), else the one whose pointer is in the
session's config folder, else the default (`brain use <name>`); `brain -b <name>` picks one.

## Use

You mostly talk to coordinators, and they run `brain` for you. By hand:

| Command | Does |
| --- | --- |
| `brain` | dashboard: tree, what needs you, coordinators |
| `brain ui [--open]` | the same, live in your browser: projects, work in flight with its worktrees and ports, what needs you, activity, decisions (read-only, this machine only) |
| `brain show <node>` | everything about one node, with this machine's IDs |
| `brain new <id> --title T [--owners a,b] [--home path]` | create a node (ids nest with `/`) |
| `brain set <id> key=value` | fields; host IDs and home paths stay on this machine |
| `brain relabel`, `move`, `archive` | re-file a node |
| `brain principal add <id>` | a new person, group or organization |
| `brain owners <id> a,b` / `brain seal <id> --owners a,b --home path` | change owners; give a node its own home |
| `brain split`, `merge` | reshape nodes, keeping provenance |
| `brain revoke <principal>` | end a relationship: quarantine and archive |
| `brain sync <id>` / `brain sync --all` | what happened in a node and its home since the last sync |
| `brain wt add/list/rm/prune` | one worktree per job beside an external home, with setup and a port |
| `brain jobs [id]` | every job's hours, commits and review rounds (the node's `job-ledger.md`): medians, open jobs, causes |
| `brain stale <id> <host time>` | freshness check: is a node's status behind its thread, whatever the timezones |
| `brain workbench <id>`, `brain report <id> "topic"`, `brain reports` | scripts and reports, in a predictable place |
| `brain brief <id> [--branch B] --task-file F` | a worker brief: where it runs, its ports, the node's limits, how to ask and hand back |
| `brain guard <id> [path ...]` | a home and its worktrees hold none of the words in the node's `never-name.txt` |
| `brain review <id> --branch B` | brief for a review by a runtime the builder didn't use |
| `brain schedule list/add/bind` | recurring work, shared; each machine binds what it runs |
| `brain link --global [runtime=folder ...]` | a pointer to the instance in runtimes' user-wide instructions (default: each runtime's own config folder) |
| `brain brains`, `brain use <name>`, `brain -b <name> ...`, `brain --all` | several brains on one machine (say personal and work): list, set the default, pick one, every dashboard |
| `brain item add/list/scope/quarantine/restore`, `brain usable` | the library and its scopes |
| `brain specialist new`, `brain lesson`, `brain hire` | reusable experts and their lessons |
| `brain check [--fix]` | verify the mechanically checkable axioms; repair the safe things |
| `brain machine` | this machine's bindings |
| `brain upgrade` | update `core/` to the latest version, then link and check |

The never-name guard checks working files and staged contents, including populated submodules
recursively, plus branch names and commit messages. Ignored untracked files are excluded.
Unavailable submodules or read errors make the scan incomplete (exit 2); matches exit 1 and
a complete clean scan exits 0. It catches accidental slips; it is not a defence against
deliberate evasion.

**Small work.** For a quick fix in one repository, open the repository itself in your host, not
its node. The Brain's rules don't apply there and nothing is recorded; `brain sync` shows the
node's coordinator what changed next time.

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
2. `python3 core/tests/live.py` (axiom 22: the same scenarios on every runtime, with real model calls)
3. Commit here; instances pick it up with `brain upgrade`.

When several CLI versions are installed, select the one under test explicitly:

```
LIVE_CODEX_BIN=/path/to/codex LIVE_CLAUDE_BIN=/path/to/claude python3 tests/live.py
```

The runner prints each executable and version. Codex hook approval uses the same selected
executable as its scenarios. A CLI process failure marks that runtime blocked and stops its
remaining queued calls; the suite still exits nonzero. Dashboard desktop notifications require
the page to remain open; all displayed clock times use the instance timezone.

For dashboard checks, open a throwaway populated instance with `brain ui` and evaluate
`tests/ui-checks.js` using the host's browser tool. It checks timezone conversions, notification
transitions and keyed navigation, then restores the page state. It intercepts notifications
for the test, so it does not deliver native desktop notifications.
