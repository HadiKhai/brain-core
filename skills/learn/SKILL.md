---
name: learn
description: Capture reusable knowledge (a method, style, asset, script, lesson or fact) from a node, or a correction to a specialist, as a library item or lesson with provenance and scope. Use when the operator says "keep this", "remember how we did this", or corrects a specialist, and when a job closes with lessons in its report.
---

# Learn (axiom 10)

1. **Pick the library.** Use the lowest node that every node likely to reuse the item descends
   from (`--at`, default the root). Never a sealed node's home: libraries live on this side.
2. **Write the item:**
   `core/bin/brain item add <item-id> --from <source node> --title "…" --kind <kind> --ref "<file@commit, URL, or thread + date>" [--at <node>] --body "…"`
   (or `--body-file`). It records the source, time and the source's owners, and sets the scope
   to those owners.
3. **Write the body** so it can be used without the source. A general lesson may be abstracted
   from a specific one; it keeps the same scope.
4. **Wider use** needs everyone in the scope to agree (axiom 11):
   `core/bin/brain item scope <item-id> <new scope> --consented-by <everyone in the current scope>`.
   Never claim consent the operator didn't report.
5. **A correction to a specialist** is a lesson:
   `core/bin/brain lesson <specialist> --from <node where it happened> "<the lesson>"`.
6. **Closing a job:** file its reusable lessons with step 2, then record the item ids:
   `core/bin/brain jobs <node> --branch <branch> --lessons <id1,id2>`.
   If there are none, use `--lessons "none: <reason>"` (say where node-only facts were kept).
   Do this before `wt prune` records the merge; it may also complete a missing outcome later.
   Old merged jobs need no backfill; newly recorded merges without an outcome warn in `check`.
7. Imported lessons use the import skill: `--proposed --repo <identity> --commit <hash>`.
   They remain unusable until a human or separate reviewer accepts them with
   `brain item accept <id> --reviewed-by <operator or non-revoked principal>`;
   `brain item reject <id>` retains an unusable rejected record. Restore accepts neither state.
8. Run `core/bin/brain check`.
