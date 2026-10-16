---
name: node-sync
description: Bring a node's files up to date with everything that happened since status.md was last updated - in its coordinator's conversation, and in its external home if it has one. Use when told "sync", before handing over, or before answering about a node whose home others work in.
---

# Sync

Run by a node's coordinator.

1. Read the `Updated:` line of `status.md`.
2. Go through your conversation since then. If your context was compacted, re-read your own
   thread with host `read`.
3. If the node's home is external, run `core/bin/brain sync <id>`. It shows the commits on every
   branch, uncommitted work and open pull requests since the last sync: work other owners did
   without you. Read what matters in the home itself. Never write there from here (axiom 6).
4. Add each new decision to the top of `decisions.md`, with why and the alternatives. Decisions
   others recorded in the home are cited, not copied: "<what> (home: <file or PR>)".
5. Rewrite `status.md`: Now, Next, Blocked / needs the operator, and Recent changes. Then
   update its first line.
6. Anything that concerns other nodes goes to the parent's `inbox.md` (axiom 13).
7. If the node mirrors its decisions to an outside source of truth (named in its rules), mirror
   them now.
8. Commit `<id>: sync`, then `core/bin/brain sync <id> --mark`, and reply in 3-6 lines: what
   changed and what is waiting on the operator.
