# Bounded machine admission

[Current startup race fix and repetition evidence](startup-race.md).

[Review fixes, red/green evidence and current rule table](review-fixes.md) address all five
findings from PR #4 at `f5fce8c73bc951ac2f799eb35dc1c45348c136da`.

`brain machine admit --disk-floor N --until HH:MM [--reason TEXT]` records a bounded disk
admission. The deadline is today in the selected instance's timezone; seconds and absolute ISO
offset timestamps are also accepted. A floor above the effective base reserve is refused.
`brain machine admit list` shows active admission and audit history; `cancel` records early cancellation.

The latest admission alone can apply. Policy reads expire it without a timer and the first observer
appends an expiry record with observation time, OS actor, reason, deadline and original event index.
Journal writers/readers serialize that event; repeated reads do not duplicate it. Expiry is rechecked
after resource sampling and queue lock acquisition, and again immediately before granting work.
The base machine policy is never rewritten. Holds and dashboard data share the same effective
reserve calculation, including the existing small-machine scaling. Other machine limits still apply.

- [Reproducible real CLI proof](prove.py) and [redacted output](proof.txt): actual disk sampling,
  UTC host and Beirut instance, tightening refusal, hold/admit/expired hold, one expiry event,
  unchanged base-policy hash. All state and policy are disposable.
- Earlier fix head `fc1ef9eb`: **207 affected tests passed in 56.30 seconds**, four workers. All five requested regression
  tests failed before the fixes; the sixth selection-boundary regression failed before its fix.
- Lane inventory and `git diff --check` passed. No local full suite. The earlier review-fix head was submitted to cloud CI; the current startup
  fix is pushed to the PR branch only, with no CI-branch push by explicit order. Its 30-run receipt
  and 19-test class result are in the current startup report linked above.
- UI source/bundle is unchanged in the follow-up. Original UI proof at the prior PR head: 47 unit
  tests, TypeScript/Vite build, browser assertions and visual reading at 1440 and 390 px, with no
  horizontal page overflow. [Desktop screenshot](dashboard-1440.png), [phone screenshot](dashboard-390.png),
  [browser proof script](check-dashboard.mjs).

The current report retains the earlier diagnostic failures and their limitations. Fresh independent
re-review and cloud full-suite completion remain coordinator gates before landing. No subagents,
force push, merge, release, tag, upgrade or installed-core change. Paid AI/provider spend: USD 0.
