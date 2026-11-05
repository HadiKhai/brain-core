# Compact instance rule indexes

Keep startup instructions short by indexing complete rules retained in an instance's
library. Each trigger is one line under `## Instance rules`, for example:

```markdown
- R01 Before publishing.
- R02 Before deleting real data.
```

The complete text uses matching, unique markers immediately before each rule:

```markdown
<!-- rule:R01 -->
- **Publishing:** get the operator's explicit permission first.
```

Tell agents where to find the full rules and to read the matching rule before
acting. Preserve current overrides when the library contains historical orders.
Triggers identify when to read; they do not replace the complete requirements.

Run the standalone check with paths to the proposed files and their saved bases:

```sh
python3 tools/check_rule_index.py proposed/AGENTS.md proposed/library/instance-rules.md \
  --baseline-index base/AGENTS.md --baseline-rules base/library/instance-rules.md \
  --summary-map summary-map.json
```

It checks matching rule IDs, duplicate or empty entries, the index's 4,000 Unicode
character budget, retention of all original full-rule lines and original index
requirements, and the unchanged operator block. A reviewer must also check that
each trigger identifies the right rule and that instruction pointers remain.
The optional summary map assigns each original requirement line to its rule IDs;
the check then requires that wording inside every assigned rule's block. Put
current requirements and explicit override references beside historical text so
an agent reading only that block cannot revive an obsolete instruction.
This is a retention check, not a semantic equivalence proof or token measurement.

Keep instance-specific text and settings patches outside the public core
repository. Bind patches to base hashes and preserve runtime hook symlinks. For
Claude sessions, project-local `.claude/settings.local.json` can hold plugin
overrides without replacing `.claude/settings.json` or changing user settings.
Apply that profile only to coordinator working folders; external home worktrees
keep their own settings. Check resolved catalogs in a fresh session after applying
it, and measure the first call's input plus cache-read plus cache-creation tokens.
