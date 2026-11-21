# Batch 1 builder handback

Base: `6c716deda35684ee186fcc6c7e75b54c5b7b087a` (release-7.5.57).
Branch: `fix/turn-holds-20261121`. Only batch 1, items 1–4, is implemented.
The file ownership note was written before source edits: [scope.md](scope.md).

| Item | Implementation | Regression evidence |
| --- | --- | --- |
| 1. Stops and refresh | `lib/brain/hosts.py:1796` accepts committed records without requiring fresh generated facts. `lib/brain/status_refresh.py:44` retains the first stale timestamp and marks the facts block under immediate admission. If contended, the private receipt retains the timestamp until admission is available. `lib/brain/cli.py:844` starts maintenance at a quiet read; `lib/brain/status_refresh.py:96` bounds one detached refresh with the existing measured hook allowance. Its last output stays in the instance, and failed requests remain retryable. No refresh compute internals changed. | `tests/test_turn_holds.py:46`, `:54`, `:65`, `:82`: committed stop, contended stop, killed slow worker, real next-command refresh. |
| 2. Order warning | `lib/brain/report_gates.py:624` reads `report_gate_order_policy` from the node or instance, following the existing settings pattern. Only `warn` changes order refusals. `:629` files one quoted doubt line through the existing nonblocking root inbox path; `:753` allows the order. Without the setting, existing behavior remains. | `tests/test_messages.py:708`, `:724`: native pre-tool allow and deduplication, default refusal. |
| 3. Wake outcome | `lib/brain/hosts.py:1122` attempts the explicit request once and prints the result. `lib/brain/wake_turns.py:50` names the destination and returns success only for this request's bound consumption receipt. Disabled, stale, queued, failed, or accepted-but-unconfirmed requests return nonzero and retain their source work. Transport acceptance alone does not establish a started coordinator run. | `tests/test_wake_confirmation.py:466`, `:473`, `:481`, `:491`: disabled, accepted without consumption, consumed, stale after rotation. |
| 4. Capture failure | `lib/brain/instruction_capture.py:509` retries classification once and records a gap instead of blocking on timeout, invalid answer or provider failure. `:288` adds verbatim-quote guidance to the retry's system prompt. `:398` names the message time and first words in one readable inbox line, with full evidence local. `lib/brain/ai_json.py:96` accepts an optional transport bound; two stop attempts fit the existing adapter allowance. Valid standing instructions retain their existing evidence requirement. | `tests/test_conversation_answers.py:247`, `:283`, `:294`, `:515`: invented quotes remain rejected, timeout/retry/gap, provider recovery, invalid retry/gap. |

Local validation: 301 passed, 0 failed in 307.58 seconds across only the five affected
test files, at `-n 4`, with `-p no:cacheprovider`. The worker-launch assertion was then
strengthened to run the real quiet command and refresh; that replacement passed
separately in 11.22 seconds. Per-file counts and summed test durations are in
[test-summary.json](test-summary.json). Lane coverage passes: 39 lanes, 120 unit files
([lanes.txt](lanes.txt)). `git diff --check` passed. No local full suite was run.

The initial affected run had 297 passes and four failures: two obsolete capture
expectations, a retired consumed wake receipt, and a stale route hidden by rate
limiting. The expectations now enforce the requested nonblocking behavior, and
both wake cases were corrected; no timeout was raised, test skipped or check relaxed.

The real-run harness invokes this worktree's `bin/brain` on a disposable instance,
with an actual held instance lock, a local synthetic provider executable and shell
wake transports. It calls the real pre-tool hook and delivers its returned input to
a synthetic send sink. It contacts no real host, model provider or private instance.
Full command output: [real-run.txt](real-run.txt); reproducible harness: [prove.py](prove.py).

```text
status root --refresh: exit 1 (instance lock held)
hook stop: exit 0, stdout empty, 0.458 s; timestamped stale request retained
now: exit 0; queued refresh completed and stale mark cleared
hook pretool: exit 0, updatedInput returned; synthetic send sink received fixture-destination
Order check doubted "Continue the fixture trial."; the send was allowed by the declared warn policy.
wake disabled: exit 2, route disabled, thread fixture-disabled
wake unconfirmed: exit 2, transport accepted; no coordinator run confirmed
wake confirmed: exit 0, delivered request to confirmed thread fixture-confirmed; consumed with brain due
hook stop with provider timeouts: exit 0, stdout empty, 29.311 s; 2 attempts
Instruction capture gap: operator message 2026-01-01T12:30:00Z beginning "Preserve every original fixture source." ... provider timed out (timeout).
```

The exact measured times above are retained in `real-run.txt`; the stale timestamp
is local until a quiet command can materialize it during contention. Scratch state
and synthetic transports were removed. Synthetic provider spend: $0. No paid cloud
load test was run; GitHub Actions billing is not measured here.

## Rules that apply (verbatim from the order)

PASS/FAIL below distinguishes builder evidence from integration gates. FAIL means
that gate is still open; it does not claim installation or an independent review.

| Rule | Result | Evidence (file:line) |
| --- | --- | --- |
| brain-core is non-opinionated: no Hadi rule hardcoded in core; core enforces only declared policy, inert by default. | PASS | `lib/brain/report_gates.py:624`; `tests/test_messages.py:724`. No instance policy enabled by this PR. |
| R14/R19: builders, reviewers, readers on Codex gpt-6.1-sol (high); the coordinator on claude-opus-5-5; no Fable anywhere but root. | PASS for this builder | Runtime: Codex gpt-6.1-sol, high; no delegates launched. Builder-only scope: `docs/turn-holds-20261121/scope.md:19`. Reviewer and coordinator remain outside this implementation turn. |
| R76: no word lists or regex for meaning; the AI judges meaning from a proper prompt and schema. R80: instructions in the system prompt, data alone in the user message. R94: AI misses are instruction failures first. | PASS | `lib/brain/instruction_capture.py:288` retains AI classification and puts retry guidance in the system prompt; `lib/brain/ai_json.py:87` keeps separate system instructions and user data. Warning policy reads the judge's structured result, `lib/brain/report_gates.py:629`. |
| R86: small changes in one area ship together as one PR with one review and cloud CI. R73: independent work in parallel. R96: list the files each change touches before running builders in parallel. | FAIL — independent review and cloud result still open | One batch/branch/PR; one independent review remains the coordinator's gate. Affected tests ran at `-n 4` and the independent scratch proof alongside them (`docs/turn-holds-20261121/test-summary.json:2`). Ownership was recorded before edits; no child builders (`docs/turn-holds-20261121/scope.md:5`). Cloud Full suite is triggered by the requested `ci/fix/turn-holds-20261121` push. |
| R87: nothing cut, parked or deferred without Hadi's OK. R10: forecasts are clock times from live state. | PASS | All four ordered items above. Batch 2 and release/installation are outside this explicit builder scope, `docs/turn-holds-20261121/scope.md:18`. No speculative installation forecast made. |
| R79: read the diff behind a claimed fix. A fix is done when it is released, installed here, and checked on the instance where it failed. | FAIL — release/installation/failed-instance check remain open | Builder read the full source diff; local proof is `docs/turn-holds-20261121/real-run.txt:1`. This builder is explicitly prohibited from merging, tagging or upgrading and makes no released/installed claim. |
| Budget: Codex is at 90% of the week (reset 2026-11-26 01:21, no credit). This order may take Codex to 93%; at 93% stop at a clean push and tell root. Cloud runs for the full suite; R32 before any local heavy run. | PASS | `brain usage`: 90% before work, 91% at final local validation. `brain machine` before local tests: heavy work allowed, load/CPU 0.24/0.22/0.20, free memory 23.2 GB, disk 114 GB with 40 GB reserve, 0/2 verifies. Tests used at most four workers; no local full suite (`docs/turn-holds-20261121/test-summary.json:2`). |

Independent review, cloud Full suite completion, coordinator release/installation,
and a check on the originally failing instance remain required. No merge, tag,
upgrade, batch 2 work, or private-instance edit was performed.
