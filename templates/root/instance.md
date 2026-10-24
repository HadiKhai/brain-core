---
name: {{NAME}}
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

- `name`: what this Brain is called on every machine (`brain -b <name>`), when one machine holds several.
- `principals`: every owner known to this instance. Add one whenever a new owner appears.
- `runtimes`: the agent runtimes in use. Each needs an adapter in `core/adapters/runtimes/`.
- `default_runtime`: the instance-wide runtime fallback when per-class routing is configured.
- `route_runtime_<class>`: an optional runtime for one job class.
- `route_model_<runtime>`: an optional default model name for that runtime.
- `route_model_<runtime>_<class>`: an optional model name for one runtime and class.
  Classes are `build`, `diagnose`, `review`, `ui`, `migration`, `docs`, and `mechanical`;
  unknown class suffixes are errors. Use the exact configured model name.
  The same routing keys can be set in a node's `node.md`. Resolution for each field is:
  explicit `brain brief --runtime` / `--model`, node per-class value, node default
  (`runtime` / `route_model_<runtime>`), instance per-class value, instance default
  (`default_runtime` / `route_model_<runtime>`), then the existing class/runtime default.
  Empty values fall through. Models are resolved for the selected runtime only.
  If either file has per-class keys, briefs print the deciding runtime and model sources,
  including fallback sources for classes without settings. Without per-class keys,
  existing routing and brief output remain unchanged.
- `host`: the host in use. Its adapter is `core/adapters/hosts/<host>.md`.
- `label_keys`: the label dimensions in use. Labels never carry rules (axiom 2).
- `trace_patterns`: strings that must never appear in a sealed node's home (axiom 6).

## Notes
