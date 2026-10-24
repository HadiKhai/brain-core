---
id: {{ID}}
title: {{TITLE}}
owners: {{OWNERS}}
labels: {{LABELS}}
home: {{HOME}}
runtime: {{RUNTIME}}
created: {{DATE}}
---

Structural record of this node, the same on every machine. Written by the parent's coordinator
through `core/bin/brain`. `owners` empty means: same as the parent. `home` is `here`, or
`external` when the node's own material lives elsewhere (a sealed node); then `home_url` says
where to get it. This machine's bindings (host IDs, the home's local path) are in
`.brain/local.json`: see `core/bin/brain show <id>`.
For home jobs, set `verify: <command>` with `brain set`; `brain handback --state done --reviews N` runs it in
the worker's worktree before hand-back.

`parallel_jobs: N` sets the maximum number of open ledger jobs (a positive integer,
default 1 when absent); set it with `brain set <id> parallel_jobs=N`. Record agreed
work as Markdown tasks under `## Build order` in this node's `backlog.md`, in order:

```md
## Build order
- [x] Finished task
- [ ] Build search in `job/search`
- [ ] Deferred task (parked: revisit after migration)
- [ ] Choose a provider (waits: operator)
```

An unchecked task is unstarted unless marked `(parked: reason)` or `(waits: operator)`,
or it names a `job/X` branch open in the ledger. Checked tasks are done. `brain due`
reports `idle-with-runnable-work` with the first runnable tasks that fit the remaining
slots. All open ledger jobs count toward the cap, including jobs awaiting review or
the operator; closed jobs do not. A legacy ledger or invalid cap produces no launch
advice; `brain check` reports an invalid cap. Lines outside this exact section,
comments and fenced examples are ignored; old conditional NEXT prose is untouched.
The reminder wakes once while unchanged and rearms after resolution or a change.

`runtime` pins worker briefs to that engine. At its subscription stop line, the brief keeps the
pin and names an alternative; choose `brain brief --runtime <runtime>` explicitly to use it.

Optional `route_runtime_<class>` and `route_model_<runtime>_<class>` fields choose a
runtime/model for a job class; `route_model_<runtime>` is the node's default model.
Classes: `build`, `diagnose`, `review`, `ui`, `migration`, `docs`, `mechanical`.
Unknown class suffixes are errors. For example, `brain set <id>
route_runtime_review=codex route_model_codex_review=example-review-model`.
Explicit brief flags win, followed by node per-class, node default, instance per-class,
instance default, and the existing class/runtime fallback. Empty fields fall through.
The full key reference is in `instance.md`. Per-class routing prints the deciding sources;
files without per-class settings retain legacy brief output.

Code projects record `preview=<URL/path>` and `preview_tour=<path>` via `brain set` after
running them. These bindings stay in `.brain/local.json`; never copy them into shared files.
`preview=none` clears the reminder when no preview applies. Only `preview_url=<deployed URL>`
is explicitly shareable here. A git home, `type:code`/`kind:code` label or `stack` field identifies
code nodes; merged jobs without a preview get a due/facts reminder.
