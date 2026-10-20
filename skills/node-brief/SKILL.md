---
name: node-brief
description: Brief the operator on everything under the current node - signals stale children to their coordinators, then summarizes what moved and what needs the operator. Use for a scheduled brief or "what's going on".
---

# Brief

Run by any coordinator, about its own subtree. The root reports to the human; other
coordinators route unresolved needs to their parent, never report other subtrees.

1. Run `core/bin/brain` (the dashboard: what needs the operator, work in flight) and
   `core/bin/brain status` for the `Updated:` lines. Each coordinator checks its own external
   home in its node-sync; a parent brief never runs child sync or marks a child synced.
2. For each child with a coordinator (`core/bin/brain show <child>`), compare its last activity
   (host `list`) with `Updated:`. Send only a behind signal when the thread is newer, asking
   that coordinator to run its own node-sync. Do not wait for sync during the daily brief;
   report from the current files and label the stale snapshot as behind.
   - Avoid duplicate nudges to a child already syncing or busy; it owns the next sync.
   - Missing coordinator bindings are setup gaps, not evidence that a node is current.
3. Read each child's `status.md` and its new `decisions.md` entries. Process your own
   `inbox.md`.
4. Commit `<id>: brief`.
5. Brief the operator, in this order:
   - **Needs you**: `core/bin/brain inbox <id> --new` (root uses `root`); never relist reported items unless asked;
   - **Moved**: one line per node that changed, including work others did in shared homes;
   - **In flight**: open jobs and pull requests (the Active work rows);
   - **Quiet**: nodes with no change (names only);
   - **Across nodes**: anything that touches more than one node.
