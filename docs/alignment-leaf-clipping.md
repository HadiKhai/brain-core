# Text-node clipping correction

The previous audit constructed a Range for each option container. That range included an independently ellipsised secondary line, causing fitting parent containers to inherit its overflowing text bounds. The secondary picker line also had no explicit full-text access in the application source.

The audit now visits individual text nodes once, checks clipping along only that node's ancestor path, and requires a clipping box's own scroll extent to exceed its viewport before calling it clipped. A fitting container is not declared clipped. Full-text access is resolved from the text element through the option: title, tooltip trigger, aria-label, aria-labelledby and aria-describedby. A sibling tooltip cannot exempt the primary name. Attribute values must contain the actual text; IDs are resolved structurally.

The picker secondary line now uses the existing shared tooltip primitive, rendered directly as its small element so no layout wrapper is introduced. Its full secondary text is available on hover and explicitly labelled. The primary name still wraps and does not inherit that sibling's access.

The horizontal mutation now explicitly hides overflow on the primary title while removing its ellipsis. This models genuinely cut text, rather than treating readable or scrollable overflow as proof of lost content. The vertical line-clamp mutation remains unchanged. Neither negative assertion is removed or weakened.

The ci warning-proof scope now includes the complete mutation and structure specs as well as the browser warning proof. Added regression cases cover fitting containers around clipped secondary leaves, each supported ancestor access mechanism, sibling-access isolation, and a real picker tooltip. The PR run still executes the complete UI suite. Both execute the same new head in cloud CI. If this attempt fails for the same clipping cause, work stops for coordinator decision; no third correction or rerun is attempted.

All work remains in the assigned worktree and same PR; git commands use absolute git -C, commits are additive, no subagents or installed-core edits. No local dependencies or browsers were installed and no local full suite ran. Checks and reads are batched where independent. Fixtures are synthetic; diagnostic logic uses DOM, CSS, attributes and exact text containment, not semantic rules or model calls. Existing tolerances are retained. The final hand-back supplies measured test results and exact-head cloud URLs.
