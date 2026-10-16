# Runtime adapters

A runtime can serve the framework if it meets the runtime contract in `core/MODEL.md`. Its
adapter, `<runtime>.env`, tells `core/bin/brain` how to expose the neutral files to it:

- `INSTRUCTIONS_LINK`: the file name this runtime reads instead of `AGENTS.md`. `brain link`
  makes it a symlink to `AGENTS.md` in every node. Leave it empty if the runtime reads
  `AGENTS.md` itself.
- `SKILLS_DIR`: where this runtime looks for skills, relative to the instance root. `brain link`
  points it at `core/skills`.

Inheritance (axiom 5) relies on the runtime loading instruction files from parent folders.

| Runtime | How it loads parent folders |
| --- | --- |
| claude | `CLAUDE.md` in the working folder and every parent. Imports (`@file`) in parent files are not followed, which is why links are used. |
| codex | `AGENTS.md` from the git root down to the working folder. |

To add a runtime, write `<name>.env`, add the name to `runtimes` in `instance.md`, run
`brain link`, and re-run the parity test (axiom 22).
