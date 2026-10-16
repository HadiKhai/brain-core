---
operator: {{OPERATOR}}
principals: {{OPERATOR}}
runtimes: {{RUNTIMES}}
default_runtime: {{DEFAULT_RUNTIME}}
host: {{HOST}}
timezone: {{TZ}}
label_keys: area, kind, state
trace_patterns: {{ROOT}}
---

# Instance

Binds `core/MODEL.md` to real things. The fields above are read by `core/bin/brain`:

- `principals`: every owner known to this instance. Add one whenever a new owner appears.
- `runtimes`: the agent runtimes in use. Each needs an adapter in `core/adapters/runtimes/`.
- `host`: the host in use. Its adapter is `core/adapters/hosts/<host>.md`.
- `label_keys`: the label dimensions in use. Labels never carry rules (axiom 2).
- `trace_patterns`: strings that must never appear in a sealed node's home (axiom 6).

## Notes
