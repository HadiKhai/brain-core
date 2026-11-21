# Batch 1 file ownership (before editing)

Base: `6c716deda35684ee186fcc6c7e75b54c5b7b087a`; branch: `fix/turn-holds-20261121`.

1. Stop refresh contract: `lib/brain/hosts.py`, `lib/brain/status_refresh.py`
   (stop deferral and bounded retry only), `lib/brain/cli.py`, `tests/test_hosts.py`,
   `tests/test_turn_holds.py`, `tests/lanes.py` (register the new test file).
2. Declared order warning policy: `lib/brain/report_gates.py`,
   `tests/test_messages.py`, `tests/test_turn_holds.py`, `docs/report-gates.md`.
3. Explicit wake outcomes: `lib/brain/hosts.py`, `lib/brain/wake_turns.py`,
   `tests/test_wake_confirmation.py`.
4. Capture retry and readable gaps: `lib/brain/instruction_capture.py`,
   `lib/brain/ai_json.py` (optional transport timeout so two tries fit the existing hook bound),
   `tests/test_conversation_answers.py`, `tests/test_turn_holds.py`.

Proof scripts and captured output belong in this folder. No edits to `status.py`,
`inbox_notices.py`, `command_reads.py`, or refresh computation internals. Batch 2,
semantic classification changes, release, installation and merge remain outside
this builder's authority. No delegates are launched by this implementation child.
