# Trusted local preview drivers

Preview drivers implement the existing `acquire(context)`, `step(name, context)`
and `release(context)` API. Core owns the lifecycle sequence and durable intent;
drivers own resource fencing, recoverable snapshots, restoration and actual
quiescence, including descendants. A subprocess exit or pipe EOF does not prove
product quiescence. This transport grants no remote-host authority.

Registration is explicit, local and disabled by default. Installing core does
not register a product. Keep registration packets, installed registrations,
resource names and receipts out of shared repositories.

Create a user-owned mode-0600 JSON packet with these required fields (example
paths and digests are placeholders):

```json
{
  "version": 1,
  "name": "example",
  "protocol": "example-v1",
  "executable": "/opt/example/bin/runtime",
  "argv": ["/opt/example/product/driver.js", "preview"],
  "checkout": "/opt/example/product",
  "head": "0123456789abcdef0123456789abcdef01234567",
  "files": {
    "/opt/example/bin/runtime": "<64 lowercase hex characters>",
    "/opt/example/product/driver.js": "<64 lowercase hex characters>"
  }
}
```

Run `brain preview-adapter <node> --register <absolute-private-packet.json>`.
Core validates before storing a mode-0600 registration at the node's
`.brain/preview-adapter.json`. It pins physical checkout device/inode, exact
clean Git HEAD, literal argv, and each file's device/inode/SHA-256. Paths must
be absolute and physical, with trusted user/root ownership and no group/world
write permission (a sticky root-owned temporary ancestor is allowed). Files
must be regular and singly linked. The executable must be executable and
included in `files`; every absolute file argument must also be included. There
are at most 32 arguments and 32 pinned files, each at most 128 MiB. Registration
and installed JSON are each limited to 64 KiB. Duplicate JSON keys, unknown
fields, nonfinite numbers and unsupported versions refuse.

The optional `operation_deadlines` field enables the long-operation extension
below. No other additional registration fields are accepted.

This is a trusted executable boundary, not a sandbox. The operator must pin all
driver code inputs, including runtime-loaded code, and trust the executable's
interpreter, libraries and product semantics. No shell, module import,
discovery, install, network connection or listener is supplied by core. Drivers
receive only `PATH=/usr/bin:/bin` and `LANG=C.UTF-8`; they must not depend on
ambient credentials or runtime injection variables. Their working directory is
the registered checkout. Checkout identity checks disable Git fsmonitor and
global/system Git configuration.

Registration cannot overwrite an existing registration. Explicit operator
removal of the private registration file permits a later registration; removal
does not clear pending lifecycle intent, leases or receipts and never authorizes
replay. Do not remove lifecycle state to force a retry.

The separate existing `preview_upgrade` setting remains strict and unchanged:
version 1, enabled true, adapter name, protocol, resource, and named backup,
migration, restart, acceptance and rollback operations are all required. Configure
it with the supported `brain set` JSON setting only after the product contract is
ready. A registration alone does not enable preview work.

The existing train reconciliation, post-publication and merged-job hooks load
the node's registration in each invoked process before resolving the adapter.
For explicit reconciliation, use
`brain preview-upgrade <node> --head <exact-landed-main-commit>` and inspect its
JSON `state`. A pending result is not acceptance. The command uses that same
hook and engine; it cannot invoke an arbitrary operation. Stable node/resource
locks, host ownership, current main, config, one-publication and receipt guards
continue to apply. Reconciliation returns existing per-head state; it never
blindly retries an unknown operation.

## JSON over stdio, version 1

Each method starts one direct executable invocation. Send one UTF-8 JSON value
and EOF on stdin; return one JSON value and EOF on stdout, then exit zero. Input
and stdout are each at most 65,536 bytes, stderr at most 16,384 bytes, and a
direct invocation has a ten-second deadline. Identity hashing and each Git
identity command also have ten-second limits. The executable and registration
are checked before and after every method. Runtime diagnostics belong on stderr;
core does not publish them in lifecycle reasons.

The request has exactly these fields:

| Field | Value |
| --- | --- |
| `version` | integer `1` |
| `adapter`, `protocol` | exact registered strings |
| `request_id` | fresh 32-character lowercase hex token |
| `method` | `acquire`, `step`, or `release` |
| `step` | lifecycle step name for `step`; otherwise null |
| `context_sha256` | SHA-256 of the UTF-8 Python `json.dumps(context, sort_keys=True, allow_nan=False)` representation |
| `context` | existing engine's context, passed unchanged |

The response echoes every field except `context` exactly, adding only `result`.
Echo the supplied `context_sha256`; a cross-language driver need not reproduce
Python serialization. Missing fields, extra fields, duplicate keys, trailing
JSON, changed request/protocol/version/context binding or nonzero exit refuse.

| Method | Exact `result` keys and meaning |
| --- | --- |
| `acquire` | `resource`, `host`, `run_id` echo context; nonempty bounded `lease` and `receipt` strings |
| `step` | boolean `ok`, nonempty bounded `receipt`, exact `ownership`, `quiescent: true` |
| `release` | exact `ownership`, `released: true`, `quiescent: true` |

Lease and receipt strings are at most 4096 characters. Step context includes
node, head, host, run ID, resource, ownership, protocol, named operation,
idempotency key and the prior backup receipt. Acquire context omits ownership;
release context includes it. Receipts are opaque to core: the product must
actually implement the promised snapshot, fencing, acceptance and restore.

Only a valid, quiescent `ok: false` step result is a known failure that allows
the existing engine's rollback behavior. Transport errors, timeout, malformed
responses, changed registration, unconfirmed quiescence or foreign ownership
leave durable intent pending. Core joins or terminates and joins only its owned
direct child; it neither kills descendants nor treats direct-child shutdown as
product shutdown. No rollback, release or replay follows an unknown effect.
Product-owned reconciliation remains a separate obligation.

## Long operations: start and status

An adapter may opt individual lifecycle operation types into asynchronous
transport by adding `operation_deadlines` to its private registration packet:

```json
{"operation_deadlines": {"migration": 300, "backup": 120}}
```

Keys are `acquire`, `backup`, `migration`, `restart`, `acceptance`, `rollback`
or `release` (lifecycle step names, not product-specific configured commands).
Values are integer seconds from 1 through 86400; booleans are invalid. The map
must be nonempty. An omitted type retains the existing direct method, schema
and ten-second cap. Registrations without this field remain compatible.

For an opted-in operation, core invokes `start` once, then `status` until a
terminal response. Every invocation still sends one JSON value and EOF and
must exit within ten seconds, including closing stdout/stderr. The adapter
must arrange product-owned durable execution; it must not leave an inherited
stdio pipe open in a background worker. Core supplies no daemon or listener.

The request uses all version-1 fields above, with `method` equal to `start` or
`status`, the original lifecycle `step` (null for acquire/release), and one
additional field, `operation`, with exactly:

| Field | Value |
| --- | --- |
| `id` | core-generated fresh 32-character lowercase hex operation id |
| `method` | original `acquire`, `step` or `release` |
| `timeout_seconds` | registered total deadline for this operation type |

The same operation id, original method, step, timeout and unchanged context
are sent on every poll; each call has its own fresh `request_id`. The response
echoes every request field except `context`, exactly as for short calls,
including the entire `operation` object, and adds only `result`. Core refuses
any changed adapter, protocol, version, request, context or operation binding.
Registration, checkout and pinned file identities are rechecked before and
after every start/status invocation. Adapters must bind their durable operation
record to this entire operation identity and context, including resource,
host, run id, ownership, head and idempotency key when present. A foreign id or
context must never read or start another operation. `status` is read-only;
neither polling nor an unknown id authorizes starting or replaying work.

The long-operation `result` has exactly `state`, `receipt` and `value`:

| `state` | `value` and meaning |
| --- | --- |
| `pending` | null; execution is still in progress |
| `done` | the original method's exact result object; a step must have `ok: true` |
| `failed` | a step result with `ok: false`, exact ownership and `quiescent: true`; a known failure eligible for the existing rollback path |
| `unknown` | null; the adapter cannot establish the outcome, including an unknown/lost operation id |

`receipt` is always a nonempty string of at most 4096 characters, providing
product-owned evidence for that observation. Terminal values retain all
original receipt, ownership, release and quiescence requirements; an envelope
receipt alone proves no success or quiescence. Acquire/release cannot report a
known `failed` result: inability to prove ownership or release is `unknown`.
`start` may return any of these states, acknowledging the supplied id through
its echoed operation envelope. A pending start returns promptly; the operation
id remains the handle for subsequent status calls.

Core measures the total deadline with its monotonic clock starting immediately
before the first start invocation (including identity checks and polling).
Status calls and identity checks do not reset it. Each subprocess receives at
most the remaining operation budget and the ten-second per-call cap. A result
observed at or after the total deadline is **outcome unknown**, never success
or known failure, even if its payload says done. Core stops polling at expiry;
it does not claim cancellation, kill product descendants, rollback, release,
restart a lost operation or replay a start. Transport errors, crashes mid-op,
lost ids, malformed states, invalid evidence and identity changes also leave
the existing durable lifecycle intent pending for product reconciliation.
Interrupted core processes do not resume polling automatically; their pending
intent continues to fence new work. This extension changes transport only,
not the lifecycle sequence or the existing reconciliation authority.
