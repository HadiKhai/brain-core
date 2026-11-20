# Sweep timing failure

The failing assertion is inherited from main, not introduced by a sweep PR.
`lib/brain/guard.py` has identical Git blob d53250fb1932c455d2c620393531ef2c16380b08
on origin/main 4316cbfb and the initial sweep 6923fecd. The test calls
`file_mentions` directly; neither `common.git`, `owned_git` nor the refresh deadline
is reachable from that call.

The probe measured 20 pairs in each mode on each head. Base uncached/cached medians
were 0.055108/0.029981 s; batch 0.056611/0.029128 s. Both performed 40/20 stream
searches. An injected scheduling pause in the cached section measured 0.178044 s
on base and 0.180121 s on batch while preserving all results and cache work.
The CI failure (0.166945 vs 0.051036 s allowed) is consistent with that scheduler
sensitivity, without a source regression. Full samples: sweep-cache-measure.json.

Reproduction of the original check:
`python -B -m pytest -p no:cacheprovider tests/test_guard.py::ScanTermsCache::test_repeated_scan_avoids_duplicate_reads_with_timing_budget`

Deterministic reproduction of its flaw: pause the cached measurement by 0.17 s
between its two calls. The unchanged implementation fails the old ratio on both
base and batch. This pause models lost scheduling time; it is not a new budget.

Fix carried in PR #6, which already owns the guard-test changes in this batch:
measure bytes from actual read calls and actual stream searches. Two uncached
scans must read twice the file size and search twice; two cached scans must read
one file size and search once. Both must return the actual expected match.
Removing the file cache makes these assertions fail. No runtime code, threshold,
retry, skip or cache invalidation guarantee is weakened.

Focused check: 1 passed in 1.23 s. No cloud rerun or CI branch push was requested.
