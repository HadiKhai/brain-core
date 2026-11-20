# Advisory visual measurements

Visual measurement checks retain their existing tolerances and report evidence instead of failing tests. The distinction is declared by the check type: `visualExpect` for direct measurements, and `kind: "measurement"` for shared audit results. Functional checks continue to use ordinary assertions. The serving application and its policy configuration are unchanged.

A warning records the test, current URL, viewport, expected and measured values, and a screenshot. The Playwright HTML report contains the attachments. The reporter writes `test-results/visual-warnings.json`, emits GitHub warning annotations on the PR check, and appends the warning list to the Actions summary. The `ui-evidence` artifact retains the HTML report, values and screenshots, including when functional tests fail. Screenshot capture problems are reported separately and never turn a measurement into a failure.

## Spec inventory

All existing `ui/tests/*.spec.ts` files were inspected. Measurement-bearing specs:

| Spec | Measurements |
| --- | --- |
| alignment.spec.ts | Shared audit, page width, text overflow, phone gutters, inset mutations |
| after74-dashboard.spec.ts | Scroll position and document width |
| extremes.spec.ts | Overflow, row/field sizing, fold position, option width, page height, shared audit |
| first-paint.spec.ts | Loading notice horizontal bounds |
| interaction.spec.ts | Font size, dock position, icon bounds, focus styling, overflow, line height, scrim, opacity, dot centring |
| r9-graph.spec.ts | Label collisions, overflow styling, footer bounds and position |
| r9-panels.spec.ts | Panel footer placement |
| r9-tables.spec.ts | Footer bounds and position, card height |
| round-usage.spec.ts | Summary height and column bounds |
| tasks.spec.ts | Visual tab baseline, overflow, card sizing, viewport bounds, line height, radius, navigation labels |
| ui-fitness.spec.ts | Declared spacing, scrollbar styling, surface/row sizing, overview height and option bounds |
| ui-fitness.mutations.spec.ts | The same shared audit measurements under deliberate mutations |

`visual-warning.spec.ts` adds a browser proof: a deliberate 14px header translation must create an annotation, expected/actual values and a PNG attachment, with no recorded test error.

Hard checks remain for actual clicks, keyboard actions, paging, routes, access, source data and feedback. The task selected/unselected background equality assertion remains hard. Cut status/option text without full text on hover remains hard; ellipsis alone does not make a cut value readable. The distant-task test also checks actual pointer actionability, rather than treating a viewport-edge measurement as proof that a control is unreachable.

## Local validation

`node --test ui/tests/visual-report.test.mjs`: four passing tests in 114.711 ms. The actual `tests/ui-fitness.js` source runs against a small synthetic DOM double, returning two 14px alignment misses (2 versus 16, and 30 versus 16). Both are reported while the test passes. Negative controls verify missing field labels, unreadable cut status text, and non-assertion errors still fail. Reporter coverage checks annotation, value attachment, summary persistence, and screenshot attachment handling. See `alignment-warning-proof.txt` for retained output and duration.

This is logic-level proof, not browser geometry or a real screenshot. The assigned worktree has no node_modules, so no browser or dependency installation was attempted. Existing dashboard-oracle and first-paint unit files could not start because TypeScript is absent; the independent license file passed its three checks. The cloud UI workflow installs the pinned lockfile dependencies and Chromium, builds the UI, then runs unit and browser tests, including the real screenshot proof.

Syntax checks passed for all 23 TypeScript spec/helper files and both browser audit scripts; `git diff --check` was clean.

No local full Python suite, installed-core change, merge, tag or upgrade was performed. The existing Full suite workflow runs separately from the same head on the ci branch. Its URL and observed state, the PR URL and exact head are supplied in the hand-back.
