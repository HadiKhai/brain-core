# Runtime adapters

A runtime can serve the framework if it meets the runtime contract in `core/MODEL.md`. Its
adapter, `<runtime>.env`, tells `core/bin/brain` how to expose the neutral files to it:

- `INSTRUCTIONS_LINK`: the file name this runtime reads instead of `AGENTS.md`. `brain link`
  makes it a symlink to `AGENTS.md` in every node. Leave it empty if the runtime reads
  `AGENTS.md` itself.
- `SKILLS_DIR`: where this runtime looks for skills, relative to the instance root. `brain link`
  points it at `core/skills`.
- `GLOBAL_INSTRUCTIONS`: the runtime's user-wide instruction file. `brain link --global` adds a
  short, marked pointer to the instance there. `GLOBAL_HOME_ENV` names the environment variable
  that moves the runtime's config folder, and `GLOBAL_HOME_DEFAULT` is that folder's default.

Inheritance (axiom 5) relies on the runtime loading instruction files from parent folders.

| Runtime | How it loads parent folders |
| --- | --- |
| claude | `CLAUDE.md` in the working folder and every parent. Imports (`@file`) in parent files are not followed, which is why links are used. |
| codex | `AGENTS.md` from the git root down to the working folder. |

To add a runtime, write `<name>.env`, add the name to `runtimes` in `instance.md`, run
`brain link`, and run `python3 core/tests/live.py` (axiom 22).

## Permissions a coordinator needs

A coordinator writes its node's files and commits them, so its runtime must be allowed to write
the instance folder **including `.git`**.

- **codex**: the `workspace-write` sandbox keeps `.git` read-only, so commits fail (found by
  `tests/live.py`, 2026-10-18). Run coordinators with full access; in a host, that is the
  thread's full-access mode.
- **claude**: the working folder is writable; a coordinator working from a node folder also needs
  the instance root (the host's project root, or `--add-dir`).
