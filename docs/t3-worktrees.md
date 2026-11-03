# T3 worktrees of settled threads

T3 creates a git worktree for a thread started with a `worktree` workspace (for
example each run of a scheduled task that starts a fresh thread) under
`~/.t3/worktrees/<project folder>/`. It records the path on the thread and never
removes the folder, not when the thread settles and not when it is deleted.

`brain t3-worktrees` judges every folder there:

| verdict | when |
| --- | --- |
| `remove` | the worktree belongs to the instance repository, every host thread that records it is settled with no active run, no process has its working directory inside it, `git status` (untracked and ignored files and submodules included) is empty apart from core's own `.brain/hooks.json` and `.brain/hooks.log`, no index entry is marked assume-unchanged or skip-worktree, HEAD and every commit only its admin entry points at (every file in it that can name a commit, such as `ORIG_HEAD` or `MERGE_AUTOSTASH`, private refs, every reflog; an unknown folder there keeps the worktree) are on another branch, remote branch or tag, no merge, rebase, cherry-pick, revert or bisect is in progress, and no submodule or nested repository is checked out |
| `keep` | anything else, with the reason: kept by setting, unsettled, running, a process inside, changes, ignored files, index flags hiding edits, commits only here, a submodule checkout, no host record, the time budget, or state that could not be read |
| `other` | a worktree of another repository (a node home, another checkout): counted, never changed |

Thread state comes from T3's own store, opened read-only. If it cannot be read,
or the running processes cannot be listed, everything is kept.

```
brain t3-worktrees                       # dry run: every worktree, its size, the total that would be freed
                                         # (10 seconds; --seconds N for more; unchecked ones say so)
brain t3-worktrees --others              # also measure other repositories' worktrees (slower)
brain t3-worktrees --thread <id> --apply # remove one settled thread's worktree
brain t3-worktrees --apply               # remove every removable worktree
```

Sizes are clone-aware on APFS: data shared with a file outside the set is not
counted as freed.

Removal is `git worktree remove`, never forced, so Git's admin entry goes with
the folder, the branch and its commits stay, and Git's own refusal of changed,
untracked, locked or submodule worktrees still holds. Just before each removal
the thread state, running processes and the exact commit check are read again.
Git's removal still deletes ignored files, so the worktree is first moved with
`git worktree move` to a hidden sibling (`.<name>.removing`) that its old path
no longer reaches; only if no process has anything open inside it and every
content check still passes, and a last read of the thread state and the keep
setting still allows it, is it removed; otherwise it is moved back and kept.
Git also refuses a worktree whose admin entry has an empty `modules` folder;
only that empty folder is removed and the same move or removal repeats. A
`.<name>.removing` folder left by an interrupted pass is listed as kept; move it
back with `git worktree move`. After removal T3 keeps the thread's record, including
the old path; do not resume such a thread, start a fresh one.

Settings on the root node:

```
t3_worktree_keep: t3-0000aaaa,t3-0000bbbb   # folder names (or absolute paths) never removed;
                                             # if it cannot be read, nothing is removed
t3_worktree_reap: on                         # due ticks remove what they can in 10 seconds
```

Recording a settlement spends at most 2 seconds per attested thread; what is
left over waits for the next pass or the sweep.

`brain jobs <node> --branch <branch> --settled --attestation-file <export.json> --thread <id>` removes the attested threads'
clean T3 worktrees right after recording the settlement and lists those it kept.
