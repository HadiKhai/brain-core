# Sweep instruction-capture failure

Cause: `rule_committed` parsed Git `%cI` with Python 3.9 `fromisoformat`
without normalizing its UTC Z suffix. A commit created with TZ=UTC produces
that suffix (observed Git 2.54.0 Apple and Git 2.56.0). For example,
`2026-11-20T05:07:57Z` raises ValueError on Python 3.9.6. The capture exception
handler records capture_evidence_unavailable and returns None, omitting the
standing-instruction stop reason; the hook can then return only its notice.

This is inherited, not introduced by any sweep PR. The original instruction
capture file on main and initial batch has SHA256
5938cf4d2c6f5db5b4d9075e5bbc912094a79c9c8d3980c635b75cdb7bc92c7e.
The fix is carried on timestamp/hook PR #5. No test-order dependency was needed:
ordinary local runs use the operator's +02:00 commit timezone, masking the bug.

Minimal reproduction on initial batch 6923fecd:

```
TZ=UTC python -B -m pytest -p no:cacheprovider \
  tests/test_conversation_answers.py::InstructionCapture::test_standing_instruction_without_rule_commit_blocks_and_names_quote
```

That failed 1 test in 4.76 s with the installed Apple Git. With isolated Git
2.56.0 plus TZ=UTC, the whole file reproduced exactly the cloud's 13 failures
(52 passed) in 64.35 s at -n 4. The fixed branch passed 66/66 in 63.86 s under
the same Git/UTC setup. This is an actual Git run, not only mocked timestamps.

Fix: normalize Z to +00:00 before parsing the commit timestamp, matching the
existing normalization of transcript timestamps. A portable regression rewrites
actual Git commit timestamps to UTC Z and checks missing-rule rejection, then
actual citing-commit acceptance and absence of an instruction-capture gap.
It failed before the fix (1 failed in 2.55 s). No exception path, speaker check,
quote rule, gate requirement, identity or provider behavior was relaxed.

No CI branch, installed core, main or release was changed. No provider calls.
