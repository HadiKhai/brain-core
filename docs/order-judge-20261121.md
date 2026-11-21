# Sentence judge modules awaiting batch-1 wiring (2026-11-21)

PR #8 contains new files only relative to main `6c716ded`. The coordinator's
File ownership instruction is applied: existing report/capture hooks, tests,
pytest configuration and lane ownership are restored byte-for-byte to that base.
No runtime hook imports these new modules yet. Wait for the coordinator's batch-1
merge notification, then add one small wiring commit on the new main.

## New files and responsibilities

- `lib/brain/order_judge_prompt.py`: sentence/clause order instructions, report
  prompt definitions, schemas, exact-span judge parsing and protocol-qualified
  order cache. Shared gate comparisons are read-only imports from the existing
  report module; the active report implementation is unchanged.
- `lib/brain/capture_selection.py`: capture instruction/schema constants,
  structural agent-frame/header and harness-resume filtering, exact previous-
  assistant quote selection, bounded prior context across the stop cursor,
  classification entry point and original-substring validation. Structural
  provenance helpers and exception identities are read-only imports from capture.
- `tests/test_order_judge.py`: explicitly imports the two prepared modules for
  standalone proof. It is not assigned to `tests/lanes.py` until wiring is allowed.
- `docs/order-judge-attempt1-20261121.json`: original prompt's full real outputs.
- `docs/order-judge-replays-20261121.json`: final prompt's full real outputs.
- `docs/order-judge-timeout-retry-20261121.json`: one bounded transport retry,
  bound to the original artifact SHA; no semantic miss may be retried away.
- `docs/order-judge-test-results-20261121.json`: current module-only test result
  and explicitly historical earlier integration results.
- `docs/order-judge-20261121.md`: this handback and ownership/wiring plan.

## Wiring after the coordinator's notification

1. Rebase onto the new main, retaining batch 1's warn policy, stop-refresh and
   capture retry/gap behavior.
2. Delegate report order classification/cache and claim judging to the new order
   module; delegate capture input selection/snapshot/classification to the new
   capture module. Keep active hook entry points and existing error identities.
3. Assign the replay file and receipt inputs in `tests/lanes.py`, register the
   model marker, and adapt existing input-selection expectations in the two
   affected test files. These existing-file edits are intentionally not present.
4. Run affected tests and the exact-head cloud full suite. The current modules'
   proof establishes the prepared protocol, not active hook or release integration.

The extracted prompt, schema, selected inputs and parser source fingerprint is
identical to the retained real-model run. The standalone gate verifies that
identity and all 105 completed judgments. No additional provider calls were
made for this move. Four lanes were used for real calls and local affected tests.
The unchanged deployment model remains `claude-haiku-5-5`.

## Rules that apply: audit

| Rule | Pass/fail | Evidence / limit |
| --- | --- | --- |
| Core non-opinionated, declared policy inert by default | PASS | New order module has general sentence definitions; no operator-specific policy or default enforcement change. |
| R14/R19 provider and model | PASS | Codex gpt-6.1-sol high builder; no child agent; Codex 91% at the 21:05 +02:00 reading. |
| R76 no word lists/regex for meaning | PASS | `lib/brain/order_judge_prompt.py:151`, `lib/brain/capture_selection.py:274`: model verdicts and structural enum/span/protocol checks only. |
| R80 system instructions, user data | PASS | `lib/brain/order_judge_prompt.py:151`, `lib/brain/order_judge_prompt.py:177`; unchanged `lib/brain/ai_json.py:89`. |
| R94 instruction failures first | PASS | Order definitions/examples address row 54's question-shaped action request; failed first outputs retained. |
| R86 one PR/review/cloud CI | FAIL (awaiting wiring/review) | Same PR, independent review pending; lane registration/full integration are explicitly held by the coordinator. |
| R73 independent work in parallel | PASS | Replay pool has four workers; standalone tests run with `-n 4`. |
| R96 file ownership and file list | PASS | PR net diff contains added files only; active report/capture and all existing configuration/test files equal base. |
| R87 no unauthorized cuts/parking | PASS | All cases and behaviors retained in new modules; wiring waits by explicit coordinator instruction. |
| R10 clock-time forecasts | PASS | New-file push remains before 2026-11-21 23:45 +02:00. No builder-hour estimate. |
| R79 source read | PASS | Extracted source and restored-file diff inspected before handback. |
| R79 released/installed/checked where it failed | FAIL (not authorized yet) | No active hook wiring, merge, tag, upgrade or other-laptop proof. |
| Budget stop at 93% | PASS | Codex 91%; no extra paid model rerun for extraction. |
| R32 local machine and cloud full suite | FAIL (full gate awaits wiring) | Machine allowed at 21:05; only seven standalone tests, at most four workers. Full integration gate waits for authorized wiring. |

## Final repeated real-run proof

The final prompt produced 105/105 correct returned outcomes (35 cases, three
successful judgments each). The initial final-prompt run had eight transport
timeouts; each was retried exactly once under R88. All eight retries passed.
The initial 105 attempts plus eight retries give 105/113 successful invocations
at the case-pair level. Every original timeout remains in its immutable artifact;
the retry artifact cites its SHA. The prepared module test rejects a semantic miss, a second
transport retry, a changed protocol or a retry against another source artifact.

The second full attempt took 219.446 seconds. Retry duration and exact usage are
recorded below. The timeouts coincided with a held machine reading (CPU idle
8.18%, load 1.66/1.04/0.66) but causality is unproven; later calls in that same run
succeeded. Machine admission was allowed before the bounded retry. No timeout
or concurrency limit was raised. R03 was not used to request an unnecessary
approval: the original semantic miss was fixed; transport timeouts were a new
failure, given one bounded per-item retry, not another semantic tuning round.

Each row below gives one returned sentence verdict from the final prompt and
one report-judge authorizing span, verbatim. Every per-repetition verdict and
usage record is in `order-judge-replays-20261121.json`; absence is stated as such.

| Case | N returned | Passes | Transport timeouts | Sentence verdict and quoted span | Report authorizing span |
| --- | ---: | ---: | ---: | --- | --- |
| 52 | 3 | 3 | 0 | one_off: "go"; question: "also tell me again who i should ask gfor the plist and what i should tell him" | "go" |
| 53 | 3 | 3 | 0 | one_off: "go" | "go" |
| 54-question | 3 | 3 | 0 | one_off: "can you test it"; question: "or do you want me to test or what" | "can you test it" |
| 54-start | 3 | 3 | 0 | one_off: "okay lets start you do your part and i do mine"; one_off: "give me step by step what you want me to do" | "okay lets start you do your part and i do mine" |
| 54-numbered | 3 | 3 | 0 | one_off: "1. yes" | "1. yes" |
| 54-deadline | 3 | 3 | 0 | one_off: "also i need everything done in max 20 hours" | "also i need everything done in max 20 hours" |
| 54-access | 3 | 3 | 0 | other: "ionut gave us access to polkadot ios"; one_off: "check it" | "ionut gave us access to polkadot ios check it" |
| 54-unfinished | 3 | 3 | 0 | one_off: "we still haven't fully tested t3ams browser" | "we still haven't fully tested t3ams browser" |
| 61 | 3 | 3 | 0 | one_off: "go ahead"; one_off: "dont wait for me" | "go ahead dont wait for me" |
| 61-quote | 3 | 3 | 0 | one_off: "go ahead dont wait for me" | "go ahead dont wait for me" |
| 70 | 3 | 3 | 0 | one_off: "Give me screenshots of the app"; one_off: "Yes lets do no name posts as text only for now" | "Yes lets do no name posts as text only for now" |
| 70-yes | 3 | 3 | 0 | one_off: "yes" | "yes" |
| 70-prior | 3 | 3 | 0 | one_off: "I TOLD YOU SINCE THE MORNING TO FUCKING DO IT" | "I TOLD YOU SINCE THE MORNING TO FUCKING DO IT" |
| 70-both | 3 | 3 | 0 | one_off: "yes lets go ahead with both 20k lines and cli" | "yes lets go ahead with both 20k lines and cli" |
| 70-rule | 3 | 3 | 0 | standing_instruction: "always register a name for any test account" | "always register a name for any test account" |
| 70-username | 3 | 3 | 0 | one_off: "put no username rather than a dot" | "put no username rather than a dot" |
| 70-review | 3 | 3 | 0 | one_off: "yes"; question: "also can you check what we have updates for humanity and t3ams?" | "yes" |
| 70-add | 3 | 3 | 0 | one_off: "add it to trinity now" | "add it to trinity now" |
| 70-send | 3 | 3 | 0 | one_off: "send it send it send it please stop wiating for me" | "send it send it send it please stop wiating for me" |
| 72-everything | 3 | 3 | 0 | one_off: "do everything tonight" | "do everything tonight" |
| 72-run | 3 | 3 | 0 | one_off: "run it"; one_off: "make sure everything works"; one_off: "then put the cimments" | "run it, make sure everything works and then put the cimments" |
| 79 | 3 | 3 | 0 | one_off: "Yes go ahead"; question: "what about his second pr?"; one_off: "Also yes on the lublish"; question: "Everything is done?" | "1. Yes go ahead what about his second pr? Also yes on the lublish"; "1. Yes go ahead what about his second pr? Also yes on the lublish" |
| 79-setup | 3 | 3 | 2 | one_off: "kets set up from 0"; question: "i dont understand why we need to fund it"; question: "woiuld a user from mobile need that?" | "kets set up from 0" |
| 79-merge | 3 | 3 | 3 | one_off: "yes"; one_off: "go ahead lets nerge everything" | "yes and go ahead lets nerge everything" |
| 79-study | 3 | 3 | 3 | one_off: "i neeed you to fully understand how honor and attestation work"; other: "we can't go into it not understanding everything" | "i neeed you to fully understand how honor and attestation work" |
| 79-transcript | 3 | 3 | 0 | one_off: "Test Finger Tracking: Evaluate and implement optimizations for finger movement tracking latency to prevent lag." | "Evaluate and implement optimizations for finger movement tracking latency to prevent lag." |
| 79-pointer | 3 | 3 | 0 | one_off: "same in both here" | "same in both here" |
| 79-fault | 3 | 3 | 0 | one_off: "i uploaded 1 image not 2"; one_off: "also can't they be uploaded in parallel?" | "i uploaded 1 image not 2, also can't they be uploaded in parallel?" |
| 79-prohibition | 3 | 3 | 0 | standing_instruction: "NEVER FROM THE PUBLIC GATEWAY ALWAYS EVERYTHING HAS TO PASS THROUGH THE HOST" | "NEVER FROM THE PUBLIC GATEWAY ALWAYS EVERYTHING HAS TO PASS THROUGH THE HOST" |
| 79-test-fault | 3 | 3 | 0 | other: "when i posted something and then clicked on view story"; other: "(which is an unecessary extra step)"; one_off: "the link in my browser stayed like that ... instead of going to the id of the post" | "instead of going to the id of the post" |
| 79-check | 3 | 3 | 0 | one_off: "check this, if its good enough lets merge it"; one_off: "don't be picky" | "check this, if its good enough lets merge it don't be picky" |
| 74-thread | 3 | 3 | 0 | question: "What is happening?" | no live_claim |
| 80-instruction | 3 | 3 | 0 | question: "How do I continue?" | no live_claim |
| 80-draft | 3 | 3 | 0 | one_off: "Draft a reply." | no live_claim |
| negative-20261101 | 3 | 3 | 0 | question: "who the fuck told you to work on brain"; other: "now i was just fucking asking" |  |

## Spend and runtime

Bounded timeout retry: 19.216 seconds, four concurrent lanes.
Retained usage from 402 successful CLI responses across both protocols and
the bounded retry: `{"cache_creation_input_tokens": 223580, "cache_read_input_tokens": 1080195, "input_tokens": 990, "output_tokens": 196230}`. This excludes
usage unavailable for the eight timed-out episodes and two exploratory calls.
All retained transports are subscription `claude_cli`, not API-key calls. The
client does not expose an invoice dollar total; no guessed dollar amount is
reported. Codex weekly usage was 91% at the 20:55 +02:00 reading, below 93%.


## Current module checks and ownership proof

`python3 -B -m pytest -q -p no:cacheprovider -n 4 -o "markers=model: retained real-model protocol evidence" tests/test_order_judge.py`: 7 passed in 0.33s.

Existing files restored to base (current bytes equal `git show 6c716ded:<path>`):

| Existing file | SHA-256 | Equal to base |
| --- | --- | --- |
| `lib/brain/report_gates.py` | `af0506405e759538a905465edcb5dd8af91381e37a6b70078d9ca336a15e39bd` | PASS |
| `lib/brain/instruction_capture.py` | `62a716772eca66e8b60ec269115c6c4a7cda837e92912aa07fe43d3a4a31e3c7` | PASS |
| `tests/test_messages.py` | `e60247ce19c2751bd8c49bba684dd63648ef3e459363e62341ff087b8c898255` | PASS |
| `tests/test_conversation_answers.py` | `923ba5553e962ee3eda77cff1361690bcdce25660b96998c4b4d19a920f730fc` | PASS |
| `tests/lanes.py` | `d2faf77751c9dd6aa01c92c1f1187c5a7721ab36bba797260142438742429bdd` | PASS |
| `pytest.ini` | `bde23fc9f13d14ea8a74ee137bfec4c037c907186f71251fb543bf9605252383` | PASS |

The prior 136-test integrated result is historical for commit `93766d50`; it is
not claimed for the current unwired head. No local full suite was run.
