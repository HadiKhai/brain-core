# Runtime adapters

A runtime can serve the framework if it meets the runtime contract in `core/MODEL.md`. Its
adapter, `<runtime>.env`, tells `core/bin/brain` how to expose the neutral files to it:

- `INSTRUCTIONS_LINK`: the file name this runtime reads instead of `AGENTS.md`. `brain link`
  makes it a symlink to `AGENTS.md` in every node. Leave it empty if the runtime reads
  `AGENTS.md` itself.
- `SKILLS_DIR`: where this runtime looks for skills, relative to the instance root. `brain link`
  points it at `core/skills`.
- `HOOKS_LINK`, `HOOKS_SOURCE`, `HOOKS_SCOPE`: where the runtime reads its project hooks, which
  file in this folder to link there, and whether it goes in every node folder (`node`) or only
  the instance root (`root`). The hook is `brain hook stop`: when a turn ends in a node folder
  whose notes are behind (coordinator files changed after `status.md`, uncommitted, or
  `status.md` older than an hour), it makes the agent continue and record or say there's nothing
  to record. `HOOKS_APPROVAL=codex`: the runtime runs project hooks only once approved;
  `brain hook approve` does that, with the operator's consent.
- `GLOBAL_INSTRUCTIONS`: the runtime's user-wide instruction file. `brain link --global` adds a
  short, marked pointer to the instance there. `GLOBAL_HOME_ENV` names the environment variable
  that moves the runtime's config folder, and `GLOBAL_HOME_DEFAULT` is that folder's default.
  `brain link --global <runtime>=<folder>` puts the pointer in other config folders instead
  (a machine with several brains, or several accounts); a session whose `GLOBAL_HOME_ENV` names
  such a folder reaches that brain.

Inheritance (axiom 5) relies on the runtime loading instruction files from parent folders.

| Runtime | How it loads parent folders |
| --- | --- |
| claude | `CLAUDE.md` in the working folder and every parent. Imports (`@file`) in parent files are not followed, which is why links are used. Hooks: only the working folder's `.claude/settings.json`, so each node has one. |
| codex | `AGENTS.md` from the git root down to the working folder. Hooks: `.codex/hooks.json` from the git root down, once the project is trusted and each hook approved (an edited hook needs approving again). |

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

## Questions at stop

With a `transcript_path` in the Stop payload, `brain hook stop` reads that file only and
captures unanswered question-tool calls from its latest turn. Claude `AskUserQuestion`
tool-use records and Codex `response_item` function calls named `request_user_input` are
supported. Successful Claude tool results and Codex outputs with nonempty `answers` settle
the call. Error/cancelled calls stay pending. Question text and options pass the import
secret/never-name screening, then become one inbox line per distinct screened question.
No answers, transcript paths, tool ids or session ids enter the inbox. Removing the line
permits a later recurrence. An active stop-hook retry still captures without holding the turn.

Codex evidence (2026-10-20): codex-cli 0.144.4's generated experimental app-server schema
contains `ToolRequestUserInputParams` and `ToolRequestUserInputResponse` (an `answers` map).
This establishes the question/answer shape, not delivery of `transcript_path` by every host
or a live question-to-hook run. Fixture tests cover rollout parsing. Hosts or runtime versions
that omit the transcript path, never stop while a request is pending, or bypass runtime hooks
remain a gap: use host pending-request checks. The hook never calls the host API.

Hook failures and unreadable transcripts fail open; host pending checks remain the fallback.
A busy inbox saves a screened line in a private local queue: `brain inbox` and `brain due`
include it immediately and retry the append without waiting. All CLI inbox appends share the
same short file lock. External homes must install the Stop hook and have `brain` on PATH; the adapter
command falls back to it outside a Brain folder and resolves registered worktree bindings.
