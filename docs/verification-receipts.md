# Review and preview observation receipts

Receipts are agent-recorded observations, stored in machine-local `.brain/`.
Recording validates identities and evidence structure; it does not query a host
or browser. The caller supplies observations from the actual tools used.

## Independent review

Set `review_independence=fresh-thread` on a node or instance to accept the same
runtime and model in an independent non-builder thread. Optional `review_runtime`
and `review_model` select a route. The compatibility default is `cross-runtime`.

`brain review app --branch fix/range` prepares a review brief. After the review,
`brain review app --branch fix/range --receipt-file receipt.json` records its
observation without creating another review round. The receipt contains:

```json
{
  "version": 1,
  "node": "app",
  "job_key": "current-job-key",
  "branch": "fix/range",
  "head": "exact-commit-sha",
  "observed_at": "2026-05-01T10:00:00+00:00",
  "builder": {"threadId": "builder-id", "runtime": "codex", "model": "example-model"},
  "reviewer": {
    "threadId": "reviewer-id", "runtime": "codex", "model": "example-model",
    "fresh_context": true, "context_reused": false, "built_change": false
  },
  "verdict": "pass",
  "findings": []
}
```

Use actual host IDs and model names. Builder identity must match the recorded
worker binding and route. Planned reviewer runtime/model, when recorded, must
match the observation. Recording accepts observations no more than five minutes
old and never a future observation or one preceding this job generation.

A finding carries `id`, `class`, `severity` (`blocker`, `major`, `minor`), `text`,
`paths` (tracked repository-relative inputs), `state` (`open`, `closed`) and
`evidence` (required for closure). A pass cannot contain open blockers or majors.
Closed finding state survives unchanged code and unrelated commits; changing a
bound Git object reopens it. A finding ID cannot silently change its input scope.

Legacy temporary routing uses `same_runtime_review_until` with an absolute UTC
offset and `--same-runtime-reason`. It still requires a distinct fresh context;
a same-runtime receipt cannot bypass the configured policy or expiry.

## Preview source samples

A bound `preview` URL, machine-local `review_urls` list or
`requires_preview_evidence=true` requires rendering and source-comparison evidence.
Set additional presented URLs with `brain set app 'review_urls=["https://preview.example/review"]'`;
this JSON array remains machine-local.
Record it with `brain preview-receipt app --branch fix/range --file preview.json`.
The receipt includes `version`, `node`, `job_key`, `branch`, `head`, `checked_at`,
`renderer_threadId` and `urls`. Each URL entry has:

```json
{
  "url": "https://preview.example/app",
  "rendered": true,
  "render_evidence": "browser-render-artifact",
  "items": [
    {"page_item": "Person A", "source_ref": "people.json#1", "source_item": "Person A"},
    {"page_item": "Person B", "source_ref": "people.json#2", "source_item": "Person B"},
    {"page_item": "Person C", "source_ref": "people.json#3", "source_item": "Person C"}
  ]
}
```

All presented URLs must match current configuration. Each needs at least three
distinct displayed values and matching source values with source references.
The receipt binds the exact job generation and code head. A screenshot tour alone
does not establish data correctness.

### Own-job receipt skeleton

```sh
brain preview-receipt-skeleton app --branch fix/range --json > preview.json
# Fill the observation fields after inspecting the actual rendered page and sources.
brain preview-receipt app --branch fix/range --file preview.json
```

The accessor emits one JSON object on stdout (JSON is also the default without
`--json`). Exit 0 means current authority was read, not that evidence passed.
It fills `version: 1`, `scope: "job"`, `node`, `job_key`, `branch`, exact `head`
and opaque `worktree_generation` from the current open job and live worktree.
`binding` describes that authority: canonical absolute `home` and `worktree`
paths, worktree `created` (ISO timestamp), `start` commit, allocated `port` and
`ports` count, and the current exact declared URL list in `urls`. This metadata
is informational; the recorder re-reads binding authority rather than trusting it.

The top-level `urls` list initially has one owned URL,
`http://127.0.0.1:<allocated-port>/`. Set its path to the actual observed route,
or use another HTTP(S) loopback URL on the allocated port block or an exact
declared URL. Add entries for any other URLs being presented. Node preview and
review URLs do not change. Every URL entry still needs actual rendering and
at least three distinct matching source comparisons.

`checked_at` and `renderer_threadId` are `null`. Each URL has `rendered: null`,
`render_evidence: null`, and three `items` containing `null` values for
`page_item`, `source_ref`, and `source_item`. Replace these with the actual
observation timestamp, browser thread identity, `rendered: true`, render artifact
reference and observed source comparisons. An unfilled skeleton is deliberately
invalid. No timestamp, renderer, sample, PASS or readiness is invented.

The command takes no instance lock and creates no files, receipts, registry
entries, caches or timing events. Shell redirection above creates only the file
you explicitly choose. Missing/closed jobs, missing node/home/worktree bindings,
missing job refs, wrong checked-out branches or heads, invalid creation/port
metadata and inconsistent Git repositories refuse with nonzero exit, a reason
on stderr and no JSON on stdout. Authority changes detected during collection
also refuse; retry against the current binding. Removal/rebinding or code changes
after a successful read can invalidate the result: the recorder and readiness
policy retain every live generation and evidence check.

Readiness integrations call `review.validate_ready_reviews` and
`preview_evidence.validate_ready_preview` before verification and when reading an
existing packet. Both return refusal reasons without running a host or browser.

Readiness reconciles a pass receipt with the durable finding union at the same
candidate commit. Omitting an earlier major or blocker keeps it unresolved;
changed or deleted bound inputs reopen a closure. Unchanged closures survive
unrelated commits. Malformed or missing durable state refuses readiness and is
preserved for repair, rather than replaced by an empty pass receipt.

A bound preview's receipt requirement is scoped to the integration `base..head`
diff. Declare rendered inputs with `ui_globs: ui/**,templates/**` (or a JSON
array of glob strings) in the node frontmatter. These globs must include shared
code and data that affect rendering. Without `ui_globs`, a verify lane named
`ui` or `rendered` supplies its inputs, strict inputs and transitive prerequisite
inputs. An always-applicable rendered lane always requires a receipt. Generated
lane maps use the existing bounded map reader; no browser is launched.

A classified diff outside those inputs records `preview: not applicable, no
rendered paths touched` in the packet's `preview_scope`, with the complete Git
path list and base/head. Renames check both endpoints; deletions count too.
Absent declarations, unsupported/empty globs, failed maps, invalid bases and
unreadable diffs keep the receipt mandatory. Any supplied receipt is still
validated. Readiness recomputes classification, including for full gates, so
changing declarations cannot preserve an earlier exemption.
