# Evidence policy

`lib/brain/evidence_policy.py` refuses these at the command that would break
them. Tests: `tests/test_evidence_policy.py`. Receipts are recorded with
`brain evidence <node> --branch <branch> --kind <kind> --file <json>` and bind
the job's exact branch head; every receipt carries `version: 1`, `kind`,
`node`, `branch`, `job_key` and `head`.

## Contract declarations (checked at `brain jobs --contract-file`)

```
## Scored work
test_set: data/fixed-set.jsonl      # absolute or home-relative; frozen by its hash
test_set_sha256: <64 hex>
threshold: 0.8
cap: 50                             # most items a capped real run may use
scorer: tools/score.py              # repository path of the code that computes the score
model_choice: yes                   # optional: the job chooses a model
data_class: public                  # required with model_choice; restricted/sealed never leave
providers: 2                        # optional, 2-9

## Design reference
- Home: design/home.png             # every touched screen and its reference file

## Reading list                     # required when contract_reading_list=required
- src/app.py:10-40 why it matters

## Proven
- the parser handles empty input; Ref: tests/test_parser.py::test_empty

setup_corrections: 0                # optional: no setup correction for this job
```

## Gates

| Moment | Refuses |
| --- | --- |
| `brain brief` relaunching a job with work | task or shared context carrying an inherited no-retry, attempt-spent or stop clause (AI judge; operator or contract limits pass; unavailable judge passes) |
| `brain jobs --contract-file` | invalid scored-work, design-reference or required reading-list declarations |
| `brain review` on scored work | no real result at this head, a zero or a score below threshold; with `model_choice`, no cross-provider selection |
| `brain handback` on scored work | `--result` without a score-read receipt; `done` without a report opening with `## What was removed / kept` |
| `brain evidence --kind setup-correction` | a second correction, a selector not in the source, anything but a selector or start-format fix, expanded bounds, `setup_corrections: 0` |
| ready state | a design-referenced job without a design check, or with a failed checklist row |

## Receipt fields

- `real-result`: `test_set_sha256`, `items` (1..cap), `score`, `threshold`
  (the contract's), `test_set_check {reference_score >= threshold, method}`, `run_ref`.
- `model-selection`: `test_set_sha256`, `items`, `data_class`, `candidates`
  (`provider`, `model`, `run_ref`, `run_sha256`, `score`, `cost_usd`,
  `latency_s`; each run receipt names the same provider, model, items and set),
  `chosen`.
- `score-read`: `scorer {path, blob}` (blob at the head), 3 `cases`
  (`input_ref`, `output_ref`, `check`), `measures`, `does_not_measure`.
- `setup-correction`: `contract_sha256`, `correction` (`selector` or
  `start_format`), `before`, `after` (a selector exists at the head), `bounds: original`.
- `design-check`: `screens` (`screen`, `reference {path, sha256}`,
  `build {path, sha256}`), `checklist` (buttons, spacing, links, validation,
  navigation, consistency: `verdict`, `note`), `checker {threadId, runtime, model}`
  on a runtime other than the builder's.

## Report format for scored hand-backs

```
## What was removed / kept
### Right
1. Ref: item-4; Source: set:4; Removed: duplicate of item-3
### Wrong
1. Ref: item-9; Source: set:9; Removed: a pronoun fragment; name the subject instead
```

`None found: <how the items were read>` may replace Wrong examples. Code checks
only this structure and its place before any other section; whether each
example is judged right against its source is the reviewer's judgement.
