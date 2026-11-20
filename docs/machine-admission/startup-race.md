# Concurrent admission startup

Follow-up to `fc1ef9eb15e4fa706845a6dcb38d1d40d4dfa5b6` on PR #4. The coordinator's order
requires a new commit, PR-branch push only, no force push and no CI-branch push.

## Cause and fix

At the previous head, `lib/brain/machine.py:599` opened/created the named lock before acquiring
any exclusion for its creation. The directory target had been checked earlier; acquisition did
not recover if its identity changed in that interval. Journal publication also reopened the
absolute pathname instead of retaining the parent used by the lock.

The observed failure is an `ENOENT` from that initial `openat(O_CREAT)` path during concurrent
startup. This work does not claim to identify a particular directory-renaming caller: admission
already used `mkdir(exist_ok=True)` and no directory replacement was found in that path.
A first implementation that only retried changed directory identities still failed its first
real CLI repetition; its aggregate receipt is retained in `repeat-concurrency-first-attempt.json`.
That result required serializing initial named-lock creation as well as protecting changed parents.

Current implementation:

- `lib/brain/machine.py:604` creates the state directory idempotently with `mkdir(..., exist_ok=True)`;
  it never stages or replaces the shared state directory.
- `lib/brain/machine.py:613` takes an exclusive lock on the pinned directory before opening the
  named lock relative to that directory. `:622` validates the directory and named lock again after
  acquisition, so waiting cannot silently adopt a detached/replaced directory or lock.
- `lib/brain/machine.py:629` retries acquisition once only if the directory disappeared or its
  device/inode changed. Unrelated errors propagate immediately; a second identity change fails.
  No transaction body is retried or replayed.
- `lib/brain/machine.py:641` reads and atomically publishes the journal through that same pinned
  directory target. Only the journal file is atomically replaced, never its directory.

## Evidence

Three deterministic tests in `BoundedMachineAdmission`, `tests/test_machine.py`:

- `test_admission_reopens_once_when_state_directory_changes_before_lock_open` at line 3855:
  injects a real directory replacement and the observed ENOENT; proves reacquisition uses a new
  inode and records exactly one event without changing the base policy.
- `test_admission_retries_directory_identity_change_at_most_once` at line 3877: a second replacement
  is refused, with no journal transaction published.
- `test_admission_does_not_retry_unrelated_lock_errors` at line 3900: an unchanged directory and
  permission refusal are not retried.

Before the implementation, the replacement tests failed and the permission test passed:
**2 failed, 1 passed in 0.90 seconds**. All three passed after the change.
The final admission class passed **19 tests in 2.80 seconds**, three workers. It ran independently
of the requested one-worker repetition loop, keeping the total test-worker count at four.

`repeat-concurrency.py` runs the unchanged public-CLI concurrency test in 30 separate pytest
invocations, sequentially, with its original subprocess deadline and no skips or retries inside
an invocation. `repeat-concurrency.json` retains every exit code, duration and source SHA256;
**30/30 passed in 77.871 seconds**. Both concurrent child commands must exit zero
and both audit records must remain for each invocation to pass. Fixtures use disposable state.

No local full suite, CI-branch push, merge, tag, upgrade, release or production policy change.

## Applicable rule evidence

| Rule | Result | Evidence |
| --- | --- | --- |
| No meaning lexicons | PASS | `lib/brain/machine.py:625` compares structural device/inode identities only. |
| AI instruction failures first | PASS (no AI change) | `lib/brain/machine.py:594` is filesystem acquisition; no model/prompt patch. |
| System prompts for AI calls | PASS (no AI calls) | `docs/machine-admission/repeat-concurrency.py:13` executes local pytest only. |
| Non-opinionated framework | PASS | `lib/brain/machine.py:597` uses machine state; no operator rule or project identity embedded in runtime code. |
| Declared numbers / computed measurements | PASS | `lib/brain/machine.py:598` bounds the explicitly ordered one retry; `docs/machine-admission/repeat-concurrency.py:17` uses the ordered 30 repetitions and computes durations/hashes. |
| Parallel independent work / machine limits | PASS | Three-worker affected class plus one-worker repetition loop; no full suite or extra CI push. |
| Private traces local | PASS | `lib/brain/machine.py:653` publishes through a private target; repeated CLI fixtures are generic and the receipt retains only aggregates and hashes. |
| One builder / PR / review scope | PASS (builder boundary) | Same builder and PR, no children; fresh review remains coordinator-owned. |
| Read source behind fix | PASS | Source diff inspected; each mechanism above maps to its implementation and tests. |
| No ordered cuts | PASS | Directory creation, stable parent, one recovery, class run and 30-run proof included. |
| git -C | PASS | Every builder Git command uses the assigned worktree explicitly. |
| Real result before review | PASS (30/30) | The test invokes two actual worktree CLI processes and checks both retained audit records on each run. |
