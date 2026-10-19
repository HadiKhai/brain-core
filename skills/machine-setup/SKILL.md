---
name: machine-setup
description: Bind this machine to the instance - home paths for external homes, host projects and coordinator threads - after installing or cloning the instance on a new machine. Use when told "set up this machine", or when `brain` says a node has no coordinator on this machine.
---

# Machine setup (axiom 23)

Run by the thread the operator opened at the instance root. Host IDs and local paths belong to
one machine and live in `.brain/local.json`, never in git.

1. Run `core/bin/brain machine` to see what this machine is missing.
   Set the operator's provider fallback order with `brain set root fallback=<first>,<next>`.
   These are host provider instance ids from the host catalog, kept as a plain ordered
   `fallback` list in `.brain/local.json`; an empty value clears it. Workers that hit a usage
   limit resume on the next available instance in the same worktree (coordinator role).
2. **Homes.** For each node whose home is missing, ask the operator where it is on this machine.
   If it isn't there yet and `home_url` is known, offer to clone it (that's a network action:
   ask first). Then `core/bin/brain set <id> home=<path>`.
3. **The root.** This thread's own project and thread are the root's:
   `core/bin/brain set root host_project=<this project> coordinator=<this thread>`.
4. **Each other node**, parents before children, following the host adapter
   (`core/adapters/hosts/<host>.md`):
   - find or create its host project (folder = the node folder), then
     `core/bin/brain set <id> host_project=<id>`;
   - for an external home that is a git repo, find or create a host project for the home, then
     `core/bin/brain set <id> home_project=<id>`;
   - launch its coordinator with the brief in the node-new skill, step 5, and record it with
     `core/bin/brain set <id> coordinator=<thread>`. Pin it if the host can.
   Skip archived nodes.
5. **Schedules.** `core/bin/brain schedule list -v` shows the instance's recurring work. Each
   schedule should run on one machine only, or its brief arrives twice. For each one that isn't
   on this machine, ask the operator: "Should this machine run <id> (<when>)? It is probably
   already running on another machine." If yes, create it with host `schedule` (the brief from
   the list, targeting the node's coordinator) and record it:
   `core/bin/brain schedule bind <id> <host id>`. If this machine replaces another, tell the
   operator to remove it there (`brain schedule unbind <id>` on that machine).
6. **Worktree setup.** For each node with an external git home, check that its
   `worktree-setup.sh` (if any) works here: `core/bin/brain wt add <id> setup-check`, then
   `core/bin/brain wt rm <id> setup-check --force`. Ask before doing this in a home others own:
   a worktree is local, but it is still their repository.
7. **The global pointer.** Unless the operator declined it during install: if
   `core/bin/brain brains` shows other brains on this machine, or a runtime has several config
   folders here (one per account, say), ask the operator which folders this brain's pointer goes
   in, then `core/bin/brain link --global <runtime>=<folder> ...` (one per folder). A session
   started with that folder reaches this brain, and only folders named this way see it.
   Otherwise `core/bin/brain link --global`.
8. **Hooks.** If Codex is a runtime, ask the operator's OK, then run `core/bin/brain hook approve`:
   Codex runs the instance's stop hook only once the project is trusted and the hook approved.
   Run it again after a core upgrade that changes the hook.
9. Run `core/bin/brain check`, then give the operator one link per coordinator
   (`core/bin/brain` lists them).
