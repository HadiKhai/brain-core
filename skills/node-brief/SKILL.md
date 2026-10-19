---
name: node-brief
description: Brief the operator on everything under the current node - syncs stale children first, then summarizes what moved and what needs the operator. Use for a scheduled brief or "what's going on".
---

# Brief

Run by any coordinator, about its own subtree.

1. Run `core/bin/brain` (the dashboard: what needs the operator, work in flight) and
   `core/bin/brain status` for the `Updated:` lines. Run `core/bin/brain sync --all` to see what
   others did in external homes since the last sync.
2. For each child with a coordinator (`core/bin/brain show <child>`), compare its last activity
   (host `list`) with `Updated:`. A child with an external home is also stale if
   `core/bin/brain sync <child>` shows work since its last sync.
   - Send node-sync to every stale child first, then host `wait` on each.
   - Skip children that are busy or waiting on the operator, and say so.
3. Read each child's `status.md` and its new `decisions.md` entries. Process your own
   `inbox.md`.
4. Commit `<id>: brief`.
5. Brief the operator, in this order:
   - **Needs you**: `core/bin/brain inbox --new`; never relist reported items unless asked;
   - **Moved**: one line per node that changed, including work others did in shared homes;
   - **In flight**: open jobs and pull requests (the Active work rows);
   - **Quiet**: nodes with no change (names only);
   - **Across nodes**: anything that touches more than one node.
