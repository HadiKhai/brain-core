# Recorded introspection

`brain introspect <node> [--branch <branch>] [--hours 24] [--limit 50]` shows causal
job progress and communication metrics. It is a read-only projection. It does not
query a host, read transcripts, execute gates or grant authority.

For each exact job generation it reads the contract Outcome, latest observed
`phase_changed` fact and named dependency keys. Explicit reason codes identify a
blocker, responsible role, clearing condition and next gate. Supported codes are
`review_pending`, `operator_decision`, `capacity_wait`, `dependency_pending`,
`tests_pending`, `integration_pending` and `authorization_pending`. Missing causal
facts remain null. A dependency's `recorded_merged` label reports the ledger,
without upgrading it to verified feature delivery. The ledger remains the source
for a closed attempt; the view does not change it.

Typed facts use the event store's `record_fact`/`facts` hooks. Observed timestamps,
exact job keys and event IDs supply attribution. Future, reconstructed and
wrong-job events cannot establish current observed phases or durations. Recording
an event is a collector's assertion, not independent confirmation of its meaning.

Communication metrics cover the selected interval:

- Queue depth and oldest wait come from native pending wake files or a fresh,
  exact-thread host snapshot. Native zero means no native pending items; it says
  nothing about an unexported host queue.
- Recorded merges per hour use ledger rows whose merged Ended timestamps fall in
  the interval. This measures recorded merges, not deployed features.
- `question_opened` and `question_answered` facts join on a stable question episode
  in `evidence.ref`. Answered mean wait covers answers in the interval; oldest open
  wait includes unresolved questions opened before it.
- `turn_completed` facts join on stable turn IDs in `evidence.ref` and explicitly
  classify `reason_code` as `decision` or `routine`. Their share covers observed,
  classified turns only. Conflicting labels are excluded and counted. No observed
  turns yields null, not a zero decision share.

Facts contain controlled metadata only. Free-form bodies, permission prose and
transcripts are never interpreted as classifications. Keep the event journal and
source receipts private to the instance.

Repeated review-class advice consumes the review receipt reader when available.
It compares explicit major/blocker classes for the same exact job generation
across independent receipt rounds. Case, spaces and hyphens normalize taxonomy
spelling; no finding prose is classified. A round is its reviewer thread plus
reviewed head, so duplicate exports do not manufacture recurrence. Minor findings,
other jobs and receipts declaring reused/builder context do not establish it.
The historical recurrence is informational even if a later finding was closed;
the receipt subsystem owns current finding state and input-bound reopening.

A repeated-permission advisory requires an observed `authorization_granted` and
later `permission_requested` with the same exact job generation, head, phase,
action `reason_code` and authorization receipt in `evidence.ref`. Revocation or
leaving that phase clears the comparison. Unknown authorization, changed heads
and other scopes do not trigger advice. The advisory never grants authority or
interprets contract prose; an outward action still follows its actual policy.
