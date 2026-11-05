# Capture standing instructions before a coordinator stops

The stop hook checks only a node's bound `coordinator` or an exact entry in its
`coordinator_sessions`. Workers in the same folder are excluded. It runs before
the `relay only` shortcut and preserves the existing second-stop escape.

Configure `ANTHROPIC_API_KEY` in the hook process environment. The stdlib client
calls `claude-haiku-5-5` once for the batch, with instructions in a system block
marked `cache_control: ephemeral`, and message data alone in the user payload.
Provider cache eligibility depends on the provider's minimum prefix size.
No implicit retry or alternative model is used. The entire provider exchange,
including DNS and response reads, has a 20-second deadline.
The child uses isolated Python imports, a fixed module directory and an environment
containing only the API key, so project files cannot shadow stdlib imports.

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
a missing key is deduplicated per node/session. A busy inbox uses the existing
durable hook inbox queue. The skipped batch is consumed after its gap is recorded.
Invalid classification structure blocks once and is retained without paid retries.
The second stop rechecks any newly committed rules, records only remaining
unconverted or unclassified work as a gap, and always allows exit, even with bad
state or unsafe links. If an inbox cannot safely be written, structured stderr
diagnostics record the gap instead. Replaced transcripts reset the cursor so the
next stop can bootstrap again. Coordinators must reconcile these visible gaps; they are not evidence
that the instructions were converted. State and usage remain local under
`.brain/instruction-capture/`; no raw message or provider error body is logged.

The shared rule registry can replace the small structural Gate reader during
integration. Rule sweep creation and contract/review rule tables are separate
consumers of that registry.

Provider request fields follow the official [structured output documentation](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)
and [prompt caching documentation](https://platform.claude.com/docs/en/build-with-claude/prompt-caching).
