# Hook refusal sources and message clock proof

The two investigated calls were cancelled during an incoming-message stream
interrupt. No completed Brain hook denial or unanswered permission prompt is
recorded for either call. The evening incident occurred at 22:04:44 local time,
slightly later than the approximate incident report.

The local session and host protocol logs establish this sequence:

| Event | Morning (UTC) | Evening (UTC) |
| --- | --- | --- |
| Assistant emits tool call | 06:22:18.495 | 20:04:44.547 |
| Host offers incoming agent input, priority `now` | 06:22:18.543 | 20:04:44.591 |
| Runtime cancels PreToolUse / returns cancelled tool result | 06:22:18.573 | 20:04:44.608 |
| Result terminal reason | `aborted_streaming` | `aborted_streaming` |
| Result permission denials | empty | empty |
| Preceding input permission mode | `bypassPermissions` | `bypassPermissions` |

The session records have `hook_cancelled` attachments and `toolDenialKind:
cancelled`; the host protocol records `non_execution_kind: cancelled`. The stock
refusal text is generated for cancellation and does not prove an operator order.
The morning call-to-cancellation interval is 78 ms; the evening interval is 61 ms.
Both begin processing the incoming message immediately afterward. These are
host/runtime cancellations, not evidence of a policy guard rejection.

Current user, instance, node and coordinator-worktree Claude settings contain no
matching `ask` rule. The worktree's local settings bind the instance root and
disable unrelated plugins; its shared settings register the Brain command hook.
No instance settings were changed. Current settings alone cannot establish their
historical bytes; the contemporaneous bypass mode, empty denial list and
interrupt sequence establish the incident diagnosis. The private source paths
and exact line references are supplied in the builder hand-back; no private
message text or raw session logs are copied into this repository.

## Changed behavior

All completed Brain PreToolUse refusals identify themselves with
`brain-hook:pretool:`; stop holds use `brain-hook:stop:`. Coordinator instructions
distinguish these from permission requests, host guards and interrupted streams.
A failed tool call holds only that call; a global hold requires an actual quoted
operator instruction. Unknown source remains unknown.

`brain agent-message` and `brain msg` stamp `sent_at` in the envelope from the
instance clock, with an explicit UTC offset. Outside an instance they use the
local system clock. The send hook refreshes stale/legacy frames and frames raw
send messages through `updatedInput`. It preserves message body bytes, envelope
integrity and every other tool argument; it neither auto-approves nor requests
permission. A denied send receives no rewrite. Old envelope formats still decode;
uncovered operator text still cannot be hidden inside a valid frame.

`brain notify` already records its instance clock in the durable receipt. Its
inbox publication now uses that exact receipt timestamp, including during
interrupted-publication recovery, instead of taking a second clock reading.
Body labels are never interpreted or corrected.

Input rewriting follows the [runtime's documented PreToolUse
contract](https://code.claude.com/docs/en/hooks#pretooluse-decision-control).
Native permissions still apply. Auto mode can refuse rewritten input for another
review; the role explains retrying the exact returned input. A formatter stamp is
preparation time, not proof of transport delivery or authentication.

## Standalone CLI proof

Run `python3 -B docs/trials/host-message-20261120/run.py` from this checkout.
The script invokes actual CLI processes and the hook entrypoint in a disposable
instance under this folder. It uses a declared non-hour timezone, isolated local
configuration and generic data. Three independent formatting/notification commands
run concurrently; the two dependent hook probes run afterward.

[proof.json](proof.json) retains the successful run: both formatter stamps and
the refreshed hook stamp fall in the observed clock window, the notification
publication matches its receipt, CRLF/Unicode/trailing spaces survive exactly,
and the marked deny contains no `updatedInput`. It also verifies that an operator
remainder is not hidden and that native permission is not granted. The fixture
directory is removed by its owner on exit. No host send or paid model call is
made, and no installed core or instance settings are changed. Host/runtime code
is outside this repository; this patch does not change stream preemption there.
