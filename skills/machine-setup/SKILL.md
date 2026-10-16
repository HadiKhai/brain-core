---
name: machine-setup
description: Bind this machine to the instance - home paths for external homes, host projects and coordinator threads - after installing or cloning the instance on a new machine. Use when told "set up this machine", or when `brain` says a node has no coordinator on this machine.
---

# Machine setup (axiom 23)

Run by the thread the operator opened at the instance root. Host IDs and local paths belong to
one machine and live in `.brain/local.json`, never in git.

1. Run `core/bin/brain machine` to see what this machine is missing.
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
5. Run `core/bin/brain check`, then give the operator one link per coordinator
   (`core/bin/brain` lists them).
