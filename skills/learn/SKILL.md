---
name: learn
description: Capture reusable knowledge (a method, style, asset, script, lesson or fact) from a node, or a correction to a specialist, as a library item or lesson with provenance and scope. Use when the operator says "keep this", "remember how we did this", or corrects a specialist, and when a job closes with proposed lessons in its unified hand-back's --lessons field.
---

# Learn (axiom 10)

1. **Pick the library.** Use the lowest node that every node likely to reuse the item descends
   from (`--at`, default the root). Never a sealed node's home: libraries live on this side.
2. **Write the item:**
   `core/bin/brain item add <item-id> --from <source node> --title "…" --kind <kind> --ref "<file@commit, URL, or thread + date>" [--at <node>] --body "…"`
   (or `--body-file`). It records the source, time and the source's owners, and sets the scope
   to those owners. Learned from several nodes, `--from a,b` records them all, places the item at
   their lowest common ancestor (axiom 13) and scopes it to the owners they all share: nothing
   reaches a node one of its sources may not. Add `--keywords` with the words someone meeting
   the problem would use; matching reads them with the title and body.
3. **Write the body** so it can be used without the source. A general lesson may be abstracted
   from a specific one; it keeps the same scope.
4. **Wider use** needs everyone in the scope to agree (axiom 11):
   `core/bin/brain item scope <item-id> <new scope> --consented-by <everyone in the current scope>`.
   Never claim consent the operator didn't report.
5. **A correction to a specialist or node** is a lesson. Read that target's `direction.md`
   first. Under `## Principles`, each principle is a `### <id>: <title>` block (lowercase
   letters, digits and hyphens, starting with a letter); `enforced_by: <check path>` names
   its check, or `enforced_by: wish` records that no check enforces it yet. Use
   `templates/direction.md` for the optional format (new specialists receive it).
   Pick the existing principle that the correction breaks:
   `core/bin/brain lesson <specialist> --from <node where it happened> --principle <id> "<the lesson>"`.
   Use `--at <library node>` when the specialist id is ambiguous. For a node correction:
   `core/bin/brain lesson <node> --node --from <source node> --principle <id> "<the lesson>"`.
   If none fits (including a missing direction file), use `--principle unclassified`, the
   default. Never invent a matching principle or accept a direction diff. The operator must
   approve direction changes. Each lesson retains its source, timestamp and source-owner
   scope along with the principle id and a snapshot of its `enforced_by` value (`wish` if
   absent). Old lessons read as unclassified; do not rewrite their provenance or scope.
6. **Closing a job:** read the worker hand-back's `lessons` field (`brain handback --lessons`),
   file its reusable proposals with step 2, then record the item ids. Only legacy reports
   need a Lessons section:
   `core/bin/brain jobs <node> --branch <branch> --lessons <id1,id2>`.
   If there are none, use `--lessons "none: <reason>"` (say where node-only facts were kept).
   Do this before `wt prune` records the merge; it may also complete a missing outcome later.
   Old merged jobs need no backfill; newly recorded merges without an outcome warn in `check`.
7. Imported lesson ids count as a job's lesson outcome in step 6 while still proposed.
   Recording them never accepts them. Imported lessons use the import skill: `--proposed --repo <identity> --commit <hash>`.
   They remain unusable until a human or separate reviewer accepts them with
   `brain item accept <id> --reviewed-by <operator or non-revoked principal>`;
   `brain item reject <id>` retains an unusable rejected record. Restore accepts neither state.
8. Brief, hire and review select relevant usable items automatically for the next job, and
   `brain recall <node> "<problem>"` finds them mid-task. Matching is by word stems, so give items
   descriptive titles, keywords and self-contained bodies.
9. **Reviewing proposals.** `brain proposals [<node>]` prints everything waiting, a node at a
   time, in full. Read it (or have a reviewer on another runtime read it and recommend), then
   accept or reject several at once: `brain item accept a b c --reviewed-by <principal>`,
   `brain item reject d e`, `brain specialist accept x y --at <node> --reviewed-by <principal>`.
   Edit a proposal's text before accepting it if it needs it. Only the operator or a principal accepts.
10. Run `core/bin/brain check`.
