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
   without you. This explicit command fetches unless `--offline`; it never updates the operator's
   local branch or files. Integration main is cached `origin/HEAD` (local `main` without origin),
   and local default-branch lag is shown. If the default changes, explicitly refresh its symbolic
   ref with `git remote set-head origin -a`.
   Sync records newly observed first-parent default-branch commits as `external` ledger rows,
   keyed by full commit hash. This includes merge commits, squash merges and direct commits:
   Git alone cannot distinguish the latter two. A worker's recorded ready head identifies its
   merge even if the branch was deleted; a squash also needs that head's exact tree and recorded
   integration base as its parent. Empty/unverified job branches never claim an external integration.
   Job closure and lesson cleanup retain verified range endpoints in the ledger before deleting
   worker evidence, so delayed pushes and syncs still associate owned merges, squash and fast-forward
   work. Local mode records its actual local integration parent separately from the remote default,
   so an unrelated unpublished local change is never attributed to the worker. External rows
   assert no review, runtime usage or job duration. First sync uses
   the existing last-sync/status time window; later syncs use a local commit cursor, including
   without `--mark`. Repeats are deduplicated even if the cursor is lost. A rewritten history
   refuses cursor advancement. After inspecting rewritten history or a new unrelated default,
   explicitly run `brain sync <id> --reset-main` to rescan from the old/new common ancestor,
   or the last-sync time window for unrelated history, preserving the ledger and hash deduplication.
   Reset names one node; it cannot use `--all`. `--offline` avoids fetching during that rescan.
   `--mark` refuses to advance the sync time if integrations could not be recorded. Homes without origin retain
   their existing behavior. Read what matters in the home itself. Never write there from here (axiom 6).
4. Add each new decision to the top of `decisions.md`, with why and the alternatives. Decisions
   others recorded in the home are cited, not copied: "<what> (home: <file or PR>)".
5. Update `status.md` judgement sections: Now, Next, Active work (each row’s state from the
   pull requests and branches in step 3), Blocked / needs the operator, and Recent changes.
   Set the judgement's `Updated:` line outside the facts block from `core/bin/brain now`.
   Preserve the generated facts markers and their contents; run `core/bin/brain status <id>
   --refresh` to regenerate facts and their separate `Facts refreshed:` time. Refreshing facts
   alone must never make old judgement appear current.
6. Anything that concerns other nodes goes to the parent's `inbox.md` (axiom 13).
7. If the node mirrors its decisions to an outside source of truth (named in its rules), mirror
   them now.
8. Commit `<id>: sync`, then `core/bin/brain sync <id> --mark`, and reply in 3-6 lines: what
   changed and what is waiting on the operator.
