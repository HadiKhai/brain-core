# {{TITLE}}

This folder is a Brain instance: a tree of nodes, coordinated by AI agents on any runtime.
Every agent working anywhere under it reads this file first.

- **The rules:** `core/MODEL.md`. Read it once per session, and obey the axioms.
- **This instance's specifics:** `instance.md` (operator, principals, runtimes, host, labels).
- **Your role** comes from your launch brief:
  - coordinator: follow `core/roles/coordinator.md`;
  - worker: follow `core/roles/worker.md` (this is the default when your brief names no role);
  - specialist: follow `core/roles/specialist.md` as well.
- **Nodes:** this folder is the root node. Child nodes live in `nodes/<id>/`, and so on at any
  depth. Each node has a `node.md`.
- **Tools:** `core/bin/brain` (run `core/bin/brain help`) and the skills in `core/skills/`. If
  your client doesn't list the skills, open `core/skills/<name>/SKILL.md` and follow it.
- **Runtimes:** instruction files named for a specific runtime (like `CLAUDE.md`) are links to
  `AGENTS.md`. Always edit `AGENTS.md`.

Any agent may run `brain usage`: read the machine cache before launching work, respect the
subscription's allowed brains and stop line, and follow the host instance chosen by the brief.

## Tasks on install or upgrade

Read `core/docs/tasks.md`. Run `brain task migrate --all` once after setup; it is
additive and repeatable. For a node with task records, use `brain task next <id>`
and pass its ID to `brain wt add <id> <branch> --task-id <task>`. Keep parked tasks
visible until resumed, and use exact requirements and decision references.

## About the operator

<!-- TODO(operator): 5-10 lines. Who you are, how you like answers, what agents must never do.
     Every node inherits this. -->

## Instance rules

<!-- Rules for every node in this instance that aren't in the core model. -->
