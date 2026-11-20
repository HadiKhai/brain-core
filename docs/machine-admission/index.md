# Bounded machine admission

Item 3 implementation; base `4316cbfb125b8f2781c1d7c728a9bf7f6cf259df`.

`brain machine admit --disk-floor N --until HH:MM [--reason TEXT]` writes only a private machine
admission journal. Each record includes action, OS user, absolute creation time, reason, requested
floor and expiry. `list` retains audit history; `cancel` records cancellation. Only the latest record
can be active. Expired or cancelled records never reactivate older admissions. Reads do not write
state; no timer runs and the base policy is never rewritten. Invalid journals hold admission closed.
Other resource limits retain their existing behavior. The unchanged default disk reserve and any
declared machine reserve remain the source of truth.

- CLI and journal: `lib/brain/cli.py:805`, `lib/brain/machine.py:556`.
- Policy overlay and hold instructions: `lib/brain/machine.py:653`, `lib/brain/machine.py:689`.
- Dashboard data and display: `lib/brain/machine.py:1518`, `ui/src/usage.tsx:233`, `ui/src/model.ts:323`.
- Tests: `tests/test_machine.py:3581`, `ui/tests/model.test.mjs:260`.
- Shipped dashboard bundle rebuilt under `ui/dist/`.

## Validation

- Affected machine files: 199 passed in 104.60 seconds, four workers, with checkout `lib/` on
  PYTHONPATH. First invocation without it had 198 passes and one import failure in an existing
  durability test. That test imported the standalone model helper before its canonical package
  was discoverable. Its setup now scopes the checkout library path around those imports:
  `tests/test_machine_durability.py:60`. A separate attempt to use fixture-package imports was
  rejected by the test because the standalone helper deliberately imports its canonical package;
  its temporary model files were cleaned by the fixture. The corrected test passed alone in
  0.67 seconds without PYTHONPATH. The other 22 durability tests passed before that correction.
- Ten bounded-admission tests passed in 1.33 seconds (four workers), including exact expiry,
  cancellation/audit, malformed journal, invalid deadlines and floors, cached disk hold refresh,
  dashboard fields, unchanged default reserve, supersession and concurrent real CLI writers.
- UI unit tests: 47 passed in 599.44 ms. Production build passed (Vite 1.99 seconds plus tsc);
  existing large-bundle warning remains informational.
- Lane inventory passed: 39 lanes, 119 unit files. `git diff --check` passed.
- No local full suite. One cloud Full suite run is requested on `ci/fix/machine-admit-20261120`;
  its exact URL/state is in the PR handback. Independent fresh review belongs to the coordinator.

## Real result before review

[`proof.txt`](proof.txt) is output of [`prove.py`](prove.py), using this checkout's `bin/brain`
with actual disk sampling and a temporary HOME/instance/policy. Measured disk was 15 GB; the
fixture reserve was computed as measured free disk plus 1 GB. `heavy` refused; an admission at the
measured floor admitted it; after the short clock deadline the same command refused again.
The base policy SHA256 remained unchanged. There was one admission audit event and no restore write.
Local OS identity and temporary paths are redacted in the retained transcript. Production policy
and installed cores were untouched.

[`check-dashboard.mjs`](check-dashboard.mjs) rendered the production bundle against generic
repository fixture data with installed Chrome. It asserted reserve/floor/expiry/actor/reason display
at 1440 and 390 px; document scroll width equalled viewport width in both. The initially missing
Playwright browser executable was resolved by using installed Chrome without a browser download.
Screenshots: [`desktop`](dashboard-1440.png), [`phone`](dashboard-390.png). Both were visually read.

## Applicable rule evidence

| Rule | Result | Evidence |
| --- | --- | --- |
| No meaning lexicons | PASS | `lib/brain/machine.py:586` parses clock syntax; `:689` refreshes core-generated resource strings, not user meaning. |
| Instruction failures first | PASS (no AI change) | `lib/brain/machine.py:606` is structural admission only. |
| System prompt for AI calls | PASS (no AI calls) | `lib/brain/machine.py:606`; proof runs local CLI only, `docs/machine-admission/prove.py:22`. |
| Non-opinionated core | PASS | `lib/brain/machine.py:653` overlays declared policy; no instance rule names or operator identities embedded in core. |
| Declared numbers / computed values | PASS | `lib/brain/machine.py:644`, `:653`; `ui/src/model.ts:328`; proof reserve computed in `docs/machine-admission/prove.py:41`. |
| Parallel independent work | PASS | Two affected files ran with four workers; UI checks ran concurrently; concurrent writer proof `tests/test_machine.py:3710`. |
| Private text stays local | PASS | Private journal publication `lib/brain/machine.py:638`; screenshots use generic fixture `docs/machine-admission/check-dashboard.mjs:27`. |
| One builder / PR / review / full gate | PASS (builder boundary) | One builder, no child launches, one PR and one cloud run; fresh review remains coordinator-owned. |
| Read claimed fix source | PASS | Source diff inspected; pointers above identify each changed behavior and proof script. |
| No ordered cuts | PASS | CLI, hold text, dashboard, tests, real proof, PR and cloud run all included. |
| git -C | PASS | All builder Git commands used explicit worktree via `git -C`. |
| Real result before review | PASS | `docs/machine-admission/proof.txt:8` hold, admission, expiry hold and policy hash proof. |
| Machine / full gate constraints | PASS | Four-worker affected files only locally; cloud branch runs full suite; no release/merge/tag/upgrade. |

## Decisions and remaining gates

Deadlines in clock form must be later today (no silently inferred next day); ISO offset timestamps
support overnight work. Seconds permit short expiry proofs. Reasons are optional for compatibility
with the ordered command shape; omitted reasons record the explicit request action. Audit identity
is the actual OS account rather than a caller-supplied name. No other machine-limit override was
added. Independent review and cloud full-suite completion must be checked by the coordinator before
landing. No installed-core release was attempted. Paid AI/provider spend: USD 0; hosted runner billing
is not reported by these tools.

Proposed lesson: none; the command directly replaces manual policy edits and restoration timers.
