# Sweep rehearsal regression

PR #4 introduced an unconditional `pwd.getpwuid(os.getuid())` while formatting
free-disk holds. macOS rehearsal denies the account lookup, raising `KeyError`.
The refresh records a maintenance failure; its event fault then correctly makes
transaction proof unproven. This accounts for all nine failures in the batch's
paired-file reproduction, including the apparent independent replay-final failure.

Minimal reproduction on the original batch:
`python -B -m pytest -p no:cacheprovider tests/test_rehearse.py::RehearsalSandbox::test_real_refresh_command_proves_journal_without_allowing_residuals`

Fix: retain the kernel uid as `uid:<number>` when passwd lookup raises KeyError
or OSError. The OS identity remains authoritative; environment names are not used.

Validation in this branch: BoundedMachineAdmission 20 passed in 2.91 s;
RehearsalCloneCaches plus the real-refresh test 15 passed in 19.25 s, at -n 4.
The latter invokes the actual CLI in its macOS sandbox and validates the retained
journal, projection writes and absence of unexplained residuals.

The already published admission-race fix c5d524fe is an ancestor of this change.
No installed core, release, main or CI branch changed.
