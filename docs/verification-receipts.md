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

Readiness integrations call `review.validate_ready_reviews` and
`preview_evidence.validate_ready_preview` before verification and when reading an
existing packet. Both return refusal reasons without running a host or browser.
