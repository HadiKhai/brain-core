# Declared order warning policy

`report_gate_order_policy: warn` in `instance.md` (or a node's front matter)
allows an outgoing order that the judge doubts. The pre-tool hook files one
plain-language warning in the root inbox, quoting the doubted outgoing sentence.
Repeated attempts of the same send in the same turn share one warning. A busy
inbox uses the existing durable hook queue. Without this setting, the existing
order refusal remains. Other report-evidence and ownership checks retain their contracts.

Stops require committed records, while generated status freshness is maintenance.
The hook marks stale facts with their first stale time; if a writer owns the
instance lock, `.brain/stop-refresh/` retains that timestamp until it can be
materialized. The next quiet `now`, `show`, `inbox`, `due` or non-refresh `status`
command starts one detached refresh attempt. It never waits for that attempt.
The worker uses the existing measured refresh bound, retains failed requests and
does not repeat automatically within the same command.

`brain wake NODE REASON` attempts delivery once and names the bound destination.
It returns zero only when this request's receipt was consumed by that coordinator's
`brain due`. Transport acceptance alone returns nonzero with that qualification;
disabled, stale, failed and queued routes also return nonzero with their reason.
Pending work remains durable. Internal `--drain` and `--retry` remain worker modes.

`wake_turn_consume(..., blocking=False)` tries the receipt lock with `LOCK_NB`.
A busy lock returns `None` without changing the receipt or pending work. Omitting
the option keeps the existing blocking behavior. This is available to callers
that must serve a read without waiting on wake bookkeeping.

Capture retries a failed classification once. If the retry fails, it consumes the
failed transcript batch, allows the stop and files a readable gap naming the
message's timestamp and first words. Exact diagnostic and reconciliation records
stay local under `.brain/instruction-capture/`. Valid standing instructions still
require their existing rule evidence.
