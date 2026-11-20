# Review corrections for advisory visual measurements

The review identified two remaining geometry failures, an over-broad unreadable-text condition, and menu screenshots captured after dismissal. These are corrected together on the existing branch and PR, without changing the application.

- Back navigation still must reach the expected route and visible content. Its exact restored page/list scroll position is now advisory. Initial scrolling and the existing wheel-behaviour test remain functional checks.
- Nested scroll depth and overscroll CSS are advisory observations. Only the test that actually wheels an inner scroll area and observes the parent failing to move produces a functional scrolling failure.
- Menu overflow without clipping is advisory. Text clipping is determined from rendered text bounds and computed overflow on the element and its ancestors. Hidden/clip overflow and ellipsis can establish clipping; a scroll owner that can expose its content does not establish unreadability. Genuinely clipped text with no full-text access remains hard.
- Menu warnings are reported immediately while each menu is open. Evidence includes numeric content sizes, viewport sizes, text bounds, menu bounds and option bounds. Dismissal follows attachment capture.

The cloud browser proof includes deliberate 14px alignment and 1px restoration misses, readable visible overflow, hidden/clip/ellipsis/ancestor clipping negative controls, and nested scroll geometry beside a real successful wheel interaction. The full PR UI check still runs every browser spec. The ci branch push runs the warning-proof scope separately (also selectable by workflow dispatch) to provide bounded browser evidence; it does not replace the full UI or Python gates.

## Process evidence

The complete order and relevant source were reread before editing. All changes remain in the assigned worktree. One builder continues the same PR; no subagents, force push, merge, tag, release or installed-core edits. Every git command uses git -C with the assigned absolute path. All requested findings remain in scope; no ordered work was cut or deferred.

No AI calls or prompt changes are involved. The split is a generic property of diagnostic check types, with no instance rule names or policy embedded in the implementation. Fixtures contain synthetic text only; no private data is sent to cloud CI. Existing numeric tolerances are retained, and proof offsets are declared mutation inputs. Independent reads/checks are batched; the complete UI and Python gates run independently in cloud CI. No local full suite or dependency/browser installation is performed. Local validation consists of the affected warning-logic tests, syntax checks and a diff check. The final hand-back records their measured counts/durations and the exact cloud run URLs, states and summary evidence.
