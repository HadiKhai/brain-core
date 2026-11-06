# Capture standing instructions before a coordinator stops

The stop hook checks only a node's bound `coordinator` or an exact entry in its
`coordinator_sessions`. Workers in the same folder are excluded. It runs before
the `relay only` shortcut and preserves the existing second-stop escape.

With `ANTHROPIC_API_KEY` in the hook environment, the stdlib client
calls `claude-haiku-5-5` once for the batch, with instructions in a system block
marked `cache_control: ephemeral`, and message data alone in the user payload.
Provider cache eligibility depends on the provider's minimum prefix size.
No implicit retry or alternative model is used. The entire provider exchange,
including DNS and response reads, has a 20-second deadline.
The child uses isolated Python imports, a fixed module directory and an environment
containing only the API key, so project files cannot shadow stdlib imports.

Without an API key, install and log into the `claude` CLI with a subscription.
The client invokes `claude -p --model claude-haiku-5-5` with a separate
`--system-prompt`, `--json-schema` and JSON output; message data travels only on
stdin. It uses `--safe-mode`, empty `--setting-sources`, explicit disabled-hook
and memory settings, an empty strict MCP configuration, disabled slash commands
and Chrome, no tools and no session persistence. It never uses `--bare`, which
disables subscription authentication. The CLI must support these flags.
The child runs in a new temporary directory under `/tmp`, outside the project
and auth home, with only HOME, PATH, USER and LOGNAME inherited. USER preserves
macOS keychain subscription lookup; no parent session or configuration env is
inherited. It sets
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` and `BRAIN_CAPTURE_CHILD=1`;
the latter makes a recursively invoked stop hook return before reading stdin
or looking for an instance. The same 20-second deadline covers CLI execution.

The AI returns one or more `standing_instruction`, `one_off`, `question` or
`other` items per message. Code checks complete message coverage, types, enums
and nonempty verbatim substrings; it does not classify meaning with keywords.

Provenance uses Claude's user role, content block types, `isMeta`,
`isCompactSummary`, `isSidechain`, `promptSource`, `turnOrigin`, origin and sender
fields. Codex uses `event_msg/user_message`, completed `UserMessage` items, or
legacy response-item user roles. Tool results and explicit nonoperator senders
are excluded. Older native user records have no sender; SDK user records also
may omit it. These use the runtime's user role as the provenance boundary;
the hook cannot independently authenticate a human behind an unlabelled SDK
message. Host adapters should retain sender metadata for forwarded messages.
When that metadata does not reach a runtime transcript, prepare agent sends with
`brain agent-message --message-file <file>` or `brain msg`. These commands frame
the exact body with the fixed marker, a UTF-8 byte length and a SHA-256 digest.
Generated automated briefs, coordinator launches and wake templates use the same
framing before host transport. Capture skips
only complete frames covering the entire input, allowing newline separators
between coalesced frames. Any unproven remainder before, between or after frames
keeps the whole original input for classification. Legacy prefix-only messages,
truncated frames and changed bodies are also classified. No host lookup or
message-meaning heuristic runs in the hook. Existing host prompts need explicit
regeneration to gain framing; no host schedule is changed by the hook or by
installing the core. Framing alone does not prove a host-modified delivery:
webhook data insertion and appended footers invalidate the template digest, and
a delegated-task wrapper introduces uncovered text. Those delivered turns are
conservatively classified in full, including untrusted webhook data, until
complete host provenance is available. Regeneration does not solve that boundary.
This can produce false instruction captures and adds classifier calls for those
wakes/delegations; it never excludes a possible operator remainder. Host schedule
definition receipts from the older unframed format also need explicit
reconciliation before provisioning again.
Keep operator messages in their original thread so their instructions are captured.

Local historical repair accepts an explicit `--agent-batches-json` export for
coalesced agent sends. Map each gap filename to an array of objects containing
`source_turn_sha256`, `separator` (exactly one newline), and `messages` (original
host items in order). Every item must have explicit MCP agent provenance,
untruncated text, a unique item ID and the same receiving `sourceThreadId`.
Exactly one batch must reconstruct the whole source byte for byte; quote overlap
alone cannot close a gap. Receipts retain the source hash and every constituent's
original ID, record digest and text digest. No merged host item is manufactured.
Same-receiver consistency is checked within the supplied export; historical gap
files do not independently bind a receiving host thread. That provenance remains
the exporter's responsibility, as with the existing single-message repair.
The existing single-message export path remains supported. Gap bytes and inbox
history are retained, and `--require` still reports every unmet required gap.

A private per-session cursor records complete-line offsets and an inode/anchor
fingerprint. Every new message since the preceding stop is included, including
steering messages. Codex's duplicated response-item representation is excluded
when clean operator events exist. On first use, explicit previous completion
markers bound the current turn; at most four MiB are read. Truncation, replaced
files, malformed records and a missing boundary in a capped bootstrap are gaps.

A standing instruction blocks unless a reachable same-turn commit adds its
verbatim quote to both `AGENTS.md` and `library/instance-rules.md`, or to the
node's `AGENTS.md`. Its rule section must carry `Gate: tests/example.py::test_rule`
(multiple references separated by `; `) or `Gate: none (reason)`. Instance Gate
metadata belongs in the library section; a node's Gate belongs in its rule
bullet. A Gate in an unrelated section does not count. Commit timestamps must
be at or after the input timestamp, at Git's second precision; after the first
stop, commits must also be newer than the preceding captured instance head.
Uncommitted edits and old commits do not satisfy capture.

Provider unavailability records a deduplicated JSON `instruction-capture-gap`
entry in the node inbox and allows capture to complete. Existing notice, upgrade
and note guards still run. Gap lines are excluded from note-event fingerprints;
legacy missing-key gaps are deduplicated per node/session. A busy inbox uses the existing
durable hook inbox queue. The skipped batch is consumed after its gap is recorded.
On the HTTP transport, invalid classification structure blocks once and is
retained without paid retries. CLI timeout, auth failure, malformed JSON,
missing structured output or invalid classification structure instead record
a gap and allow capture to complete, without a retry. Every CLI failure retains
its returncode, failure stage, elapsed time and exact raw stdout/stderr byte
tails (up to 4 KiB each, base64-encoded). Timeout records include partial output
and the deadline; transport/parser errors include the exception class and a
bounded message; error responses include the response subtype. Classification
schema failures retain the original CLI output and validation cause too.
These diagnostics are local mode-0600 files under
`.brain/instruction-capture/diagnostics/`, at most 16 KiB per file. The inbox
contains only a structured cause, exit status when present, and an opaque
diagnostic reference; provider text and exception messages never enter it.
Successful calls write no diagnostic and keep the existing public usage shape.
The second stop rechecks any newly committed rules, records only remaining
unconverted or unclassified work as a gap, and always allows exit, even with bad
state or unsafe links. If an inbox cannot safely be written, structured stderr
diagnostics record the gap instead. Replaced transcripts reset the cursor so the
next stop can bootstrap again. Coordinators must reconcile these visible gaps; they are not evidence
that the instructions were converted. State and usage remain local under
`.brain/instruction-capture/`; raw failure output stays only in the private local
diagnostic files. No diagnostic is sent to a cloud service.

The shared rule registry can replace the small structural Gate reader during
integration. Rule sweep creation and contract/review rule tables are separate
consumers of that registry.

Provider request fields follow the official [structured output documentation](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)
and [prompt caching documentation](https://platform.claude.com/docs/en/build-with-claude/prompt-caching).
