# Order-gate paired trial (R59: every number here is computed by code)

Generic cases (`cases.py`, no private text) run through the hook's own `pretool_reason`
with real AI calls (claude-haiku-5-5), 3 repeats per case, 3 at a time (R73), held window 1.

- `before.json`: base lib (main 2d843bd4, release-7.5.55).
- `after-91e6c7eb.json`: PR head 91e6c7eb (first review round).
- `after.json`: the review-fix head (this commit's lib).
- `summary.txt`: `python3 -I summary.py . ''` over `before.json` and `after.json`;
  per case, passes out of 3 (errors count as not passed), group totals, calls and spend
  summed from each CLI result's `total_cost_usd`.

Rerun: `python3 -I run.py --lib <checkout>/lib --out after.json --reps 3 --workers 3 --held 1`
(base arm without `--held`).
