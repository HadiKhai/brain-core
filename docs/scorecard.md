# Evidence scorecards

`brain scorecard <node> --file <manifest.json>` evaluates an observation window
without editing it. This is a receipt evaluator: the collector must supply actual
observations and attest their coverage. It neither reads conversations nor assumes
that a missing incident record proves a zero. Fixture tests are not evidence of
a successful real observation window.

A version 1 manifest contains a `window` with timezone-bearing `start` and `end`,
plus `measurements` keyed by metric. For example:

```json
{
  "version": 1,
  "window": {"start": "2026-01-01T20:00:00Z", "end": "2026-01-02T06:00:00Z"},
  "measurements": {
    "unauthorized_publications": {
      "value": 0,
      "coverage": {"start": "2026-01-01T20:00:00Z", "end": "2026-01-02T06:00:00Z"},
      "evidence": [{"path": "reports/publication-audit.json", "sha256": "<64 lowercase hexadecimal characters>"}]
    }
  }
}
```

Every receipt path is relative to the node folder, stays inside it and names a
regular unlinked source file. Its SHA-256 must still match. Coverage must include
the whole requested window. Missing, malformed, changed or partial evidence makes
the metric `unknown`. Counts must be nonnegative integers, not booleans. Durations
are nonnegative finite seconds.

Zero targets are `context_reexplanations`, `stale_answers`,
`unauthorized_publications`, `checkout_edits`, `missing_history`, `boundary_leaks`,
`repeated_corrections`, `repeated_library_explanations` and
`repository_boundary_mentions`. `project_setup_seconds` must be less than 120;
`machine_setup_seconds` less than 900 and `machine_setup_commands` at most three.

`must_use_cases` has a list value of `{case, runtime, passed}` entries, with a
nonempty `must_cases` list at manifest level. Each case must appear exactly once
for every runtime configured in the instance. Missing cells remain unknown;
explicit failures fail. Empty matrices cannot pass. The overall result fails if
any metric fails, passes only if every metric passes, and otherwise stays unknown.

Output links each scored measurement to its verified receipts and preserves the
collector evidence boundary. Keep manifests and real receipts in private reports,
not in the shared core repository.
