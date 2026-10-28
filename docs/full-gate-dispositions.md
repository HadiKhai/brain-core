# Native full-gate quarantine evidence

`brain full-run NODE --branch BRANCH --reviews N` retains a native receipt for
both green and red runs. A failed command still exits nonzero, and no hand-back
is published. Logs are retained with the receipt independently of worker-log
rotation.

For a red pytest gate with a complete native `test-result:` inventory, rerun
each failure on the unchanged head:

```sh
brain full-isolate NODE --branch BRANCH --full-receipt RED_ID \
  --test 'tests/test_brain.py::Example::test_method'
```

The receipt records the original gate selector and native source path. The
runner supports direct `python [-B] -m pytest` commands with ordinary test-file
selectors, verbosity, no-cache, timeout and parallel-worker options. Unknown
options refuse. It removes parallelism for the isolated run. A passing process
alone is insufficient: one completed passing test, with the same identity and
source as the original failure, is required at disposition validation.

Commit only `@unittest.skip("reason")` additions on those methods, adding a
plain `import unittest` if necessary. The quarantine must be a single direct
child of the red head. Put this request in a JSON file, including every failure
exactly once:

```json
{
  "full_receipt": "RED_ID",
  "quarantines": [
    {
      "test": "tests/test_brain.py::Example::test_method",
      "isolated_receipt": "ISOLATED_ID",
      "reason": "quarantined: unchanged isolated run passed; repair timing fixture"
    }
  ]
}
```

```sh
brain full-quarantine NODE --branch BRANCH --file request.json
brain handback NODE --branch BRANCH --state done --reviews N \
  --summary 'Quarantine ready for landing' --full-receipt DISPOSITION_ID
```

Disposition recording creates one local repair task per quarantined failure,
including its reason and receipt/head provenance. Repeating the same request
reuses those tasks without resetting their progress. The original receipt is
never rewritten. The adopted packet still records full exit 1 and the original
verified head; it also identifies the exact quarantine head and disposition.
Readiness validates the full evidence graph again. Changed logs, missing tasks,
failed or missing isolated runs, altered assertions, other source changes,
changed configuration or inputs, and other heads all refuse reuse. Existing
independent review and preview requirements continue to apply to the quarantine
head. This path authorizes landing evidence; it never claims a green full run.
