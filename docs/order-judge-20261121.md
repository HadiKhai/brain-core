# Sentence order judge and capture input selection (2026-11-21)

Items 7 and 8 of the speed-and-gates order. Implementation in the assigned
`fix/judge-orders-20261121` worktree, based on main `6c716ded` (release-7.5.57).
The builder runtime is Codex gpt-6.1-sol, high. No child agents were started.

## Changes and ownership

- `lib/brain/report_gates.py`: sentence/clause order protocol, definitions and
  examples; sentence schema descriptions; exact-span parsing shared with capture;
  protocol-qualified order cache; report prompt excludes non-page assertions.
- `lib/brain/instruction_capture.py`: structural input selection only: agent
  frames and headers, harness resume notices, previous-assistant quote lines,
  bounded prior-assistant context across the stop cursor, original-span checks.
- `tests/test_order_judge.py`: repeated real-model runner and canonical release
  tests for retained verdicts, source hashes and structural selection.
- `tests/test_messages.py`, `tests/test_conversation_answers.py`: update input
  expectations and preserve operator-remainder and parent-sender safeguards.
- `tests/lanes.py`, `pytest.ini`: reminders lane ownership and model marker.
- `docs/order-judge-replays-20261121.json`: final real-model outputs and usage.
- `docs/order-judge-attempt1-20261121.json`: first attempt, including its misses.
- `docs/order-judge-timeout-retry-20261121.json`: one bounded retry per transport timeout, bound to the original artifact SHA.
- `docs/order-judge-test-results-20261121.json`: affected-test counts and durations.
- `docs/order-judge-20261121.md`: this evidence and rule audit.

The stop-refresh policy and capture retry/gap failure paths remain owned by
`fix/turn-holds-20261121`. That branch was absent and main was still `6c716ded`
at the remote check on 2026-11-21 20:52 +02:00. Check again before the push.

## Model/release verification contract

The real runner invokes the production `judge_orders` and `judge_claims` with
`claude-haiku-5-5`, then the production gate comparison. Operator message bytes
come from the failure rows. Preceding replies and outgoing work are reconstructed
context except where the failure file quotes them; row 61 also has a derived
quote-plus-order variant. No semantic regex, keyword classifier, replacement
model or postprocessing judge is used. Four independent replay lanes run at once;
each lane performs its classification and claim comparison in dependency order.

`python3 -B tests/test_order_judge.py --live --repeats 3` refreshes real proof.
The `model`-marked canonical test belongs to the reminders release lane. Cloud
and release checks replay retained real outputs, validate every quoted span,
recompute every outcome, and require the current prompt/schema/parser/case hash.
They make no fresh paid calls on cloud CI. This distinction is intentional:
retained provider proof is not described as a fresh cloud-model run.

The first attempt ran 32 cases three times: 31 cases passed all repetitions;
row 54's question-shaped test request failed all three. Both models sometimes
read it as information-seeking. The second protocol defines action requests
phrased as questions and allocation of already ordered testing explicitly.
The original failed outputs remain in the attempt-1 artifact. Rows 74/80 add
report-claim checks for thread status, operator instructions and quoted drafts.

## Boundaries and machine

Only the assigned worktree was edited. No main checkout, installed core,
`~/Brain`, deployment, merge, tag or upgrade was written or performed. Git calls
use `git -C`. Before local tests, `brain machine` reported heavy work allowed,
22.1 GB free memory and load 0.41/0.33/0.25. Local pytest uses `-n 4` and
`-p no:cacheprovider`; no local full suite runs. `brain usage` reported Codex 90%
(reading 20:45 +02:00), below this order's 93% clean-push stop line.

Release/installation and the inaccessible other laptop are coordinator/operator
work after this PR. This handback establishes source, affected-test and local
real-model proof; it does not claim the installed failure is fixed.

## Rules that apply: audit

| Rule | Pass/fail | Evidence and boundary |
| --- | --- | --- |
| Core non-opinionated, declared policy inert by default | PASS | `lib/brain/report_gates.py:98`: general sentence definitions, no named operator policy or default enforcement change. |
| R14/R19 provider and model | PASS | This report:5; builder is Codex gpt-6.1-sol high, no delegates. Final usage reading recorded below. |
| R76 no lexical meaning classifier | PASS | `lib/brain/report_gates.py:157`, `lib/brain/instruction_capture.py:363`: model verdicts; code validates structural spans only. |
| R80 system instructions, user data | PASS | `lib/brain/report_gates.py:164`, `lib/brain/report_gates.py:500`; unchanged `lib/brain/ai_json.py:89`. |
| R94 instruction failures first | PASS | `lib/brain/report_gates.py:110`: clarified action-shaped questions after row 54 failed; first raw run retained. |
| R86 one small PR, one review, cloud CI | FAIL (review pending) | One PR covers items 7/8; independent review is coordinator-owned, not performed by this implementation sub-agent. Cloud run linked in handback. |
| R73 independent work in parallel | PASS | `tests/test_order_judge.py:152`: four model lanes; local affected pytest files use `-n 4`. |
| R96 list touched files before parallel builders | PASS | User brief names the two shared files and owners; this report lists every touched file. No builder was launched here. |
| R87 no unauthorized cuts/parking | PASS | All cases remain in the release gate, including the timeout rows. No case removed, skip added, timeout raised, or bad result rewritten. |
| R10 clock-time forecasts | PASS | No duration-based delivery forecast made; absolute deadline 2026-11-21 23:45 +02:00. Push time in handback. |
| R79 read the source/diff | PASS | Both production diffs were read before reporting changes; final diff checked with `git diff --check`. |
| R79 released, installed and checked where it failed | FAIL (outside builder authority) | No merge/tag/upgrade authorized. Other laptop is unreachable. This remains implementation proof, not a shipped fix. |
| Budget: stop at Codex 93% on a clean push | PASS | Observed Codex weekly 91% (20:55 reading), with 2026-11-26 01:21 +02:00 reset. No further builders/reviewers started. |
| R32 machine before local heavy work; full suite in cloud | PASS | Machine allowed before both affected runs; at most four local workers; canonical cloud full-suite branch is `ci/fix/judge-orders-20261121`. |

## Final repeated real-run proof

The final prompt produced 105/105 correct returned outcomes (35 cases, three
successful judgments each). The initial final-prompt run had eight transport
timeouts; each was retried exactly once under R88. All eight retries passed.
The initial 105 attempts plus eight retries give 105/113 successful invocations
at the case-pair level. Every original timeout remains in its immutable artifact;
the retry artifact cites its SHA. The gate rejects a semantic miss, a second
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

## Affected local checks

`python3 -B -m pytest -q -p no:cacheprovider -n 4 tests/test_messages.py tests/test_conversation_answers.py tests/test_order_judge.py tests/test_repo_lanes.py`: 134 passed, one retained-model failure while transport retry evidence was incomplete, 219.95 seconds.

After the one bounded transport retry and retry-guard test, `python3 -B -m pytest -q -p no:cacheprovider -n 4 tests/test_order_judge.py`: 6 passed in 0.40s. The combined latest results cover 136 unique affected tests, all passing, with no local full suite. `tests/check_lanes.py` passed: 39 lanes, 120 canonical unit files. Raw model evidence and the machine-readable test summary remain under docs.
