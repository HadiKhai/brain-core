# Diagnose a failed result before handing it back

`brain handback` requires diagnosis when its state is blocked or failed, or its
summary reports blocked, failed, parked, refused or two strikes. A question
handback uses the same summary check. Ordinary done handbacks and ordinary
questions need no diagnosis. The operational `--state blocked --restart compactions`
path is exempt from the state trigger; failure wording and missed targets still apply.
Historical records and internal readiness diagnostics are not revalidated.

Record measured quality or latency with `--result NUMBER --target NUMBER`.
Both must be finite, in the same units. The default target direction is
`--target-direction at-least`; use `at-most` for a latency or cost ceiling.
A missed target requires diagnosis even for done results and compaction restarts.
Equal-to-target passes. These values are worker-supplied measurements, not a new
measurement taken by core, and are retained in the handback's `measurement` field.

Put this format in the file named by `--report`, or supply a separate Markdown
file with `--dissection PATH`. Reports are UTF-8 regular files up to 512 KiB.
Each labelled field takes one line. Cases must be numbered 1-3, and each starts
with `Ref:` followed by a stored-record path, id or URL.
Placeholder references such as `none`, `n/a`, `todo`, `unknown` or punctuation
alone are refused. Fixes are numbered in
preference order, with two or three entries; the recommendation names one rank.

```markdown
## Dissection
Cause: The parser read the display slot instead of the identity slot.
Items covered: 3
### Cases
1. Ref: records/a.json; The input contains both slots; the wrong one was read.
2. Ref: sample-b; The stored output shows the same slot selection.
3. Ref: https://example.test/records/c; The checker rejected that selection.
### Fixes
1. Correct slot selection; small change, addresses all three cases.
2. Replace the parser; larger cost, same expected coverage.
Recommendation: 1
```

Core checks format and references' presence; it does not open case references or
verify the diagnosis. Read the actual stored cases before writing them. The parsed
diagnosis, report path and SHA-256 are retained in `dissection`. A queued handback
captures that evidence before accepting the request, so later report edits do not
change the queued diagnosis. Changed evidence needs a new request identity.

For `brain notify <node> question ... --question-file question.json`, retain the
normal typed options, recommendation, default and deadline. Optional `state` and
`summary` fields describe the result being reported; the notification text is also
checked. Use numeric `result`, `target`, and `target_direction` (`at-least` or
`at-most`) for measurements. Put the Markdown section in the JSON `dissection`
string. Evidence becomes part of the stable question key's immutable content.
The rotation exception applies only to handbacks, not question files.

Append a diagnosis to the instance's failure ledger explicitly:

```sh
brain ledger add --failed 'Parser chose the wrong slot' \
  --first-explanation 'Input ambiguity' --cause 'Display slot read as identity' \
  --found-by agent --fix 'Select the identity slot'
```

`--found-by` accepts `hadi` or `agent`. The command writes one six-column row to
`library/failure-ledger.md` under today's instance-local `## YYYY-MM-DD` heading,
creating the file, heading and table when absent. Existing rows and other dates
are retained. Pipes and line breaks inside cells are escaped. An unrecognised
existing dated table refuses without replacing it. Ledger entry creation is
explicit; a dissection check does not append a ledger row automatically.
