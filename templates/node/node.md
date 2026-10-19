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
For home jobs, set `verify: <command>` with `brain set`; `brain ready --record` runs it in
the worker's worktree before hand-back.

`runtime` pins worker briefs to that engine. At its subscription stop line, the brief keeps the
pin and names an alternative; choose `brain brief --runtime <runtime>` explicitly to use it.
