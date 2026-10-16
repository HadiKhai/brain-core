---
name: node-sync
description: Bring a node's files up to date with everything that happened in its coordinator's conversation since status.md was last updated. Use when told "sync", or before handing over.
---

# Sync

Run by a node's coordinator.

1. Read the `Updated:` line of `status.md`.
2. Go through everything since then. If your context was compacted, re-read your own thread
   with host `read`.
3. Add each new decision to the top of `decisions.md`, with why and the alternatives.
4. Rewrite `status.md`: Now, Next, Blocked / needs the operator, and Recent changes. Then
   update its first line.
5. Anything that concerns other nodes goes to the parent's `inbox.md` (axiom 13).
6. If the node mirrors its decisions to an outside source of truth (named in its rules), mirror
   them now.
7. Commit `<id>: sync`, and reply in 3-6 lines: what changed and what is waiting on the
   operator.
