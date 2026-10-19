---
name: node-new
description: Create a child node of the current node with its folder, record, coordinator thread and host registration. Use when the operator asks to start, add or set up anything new (a project, area, research topic, client, sub-project).
---

# New node

Run by the coordinator of the parent node.

Before launching, run `brain usage`. Use only a subscription allowed for this brain and below
the stop line; follow the subscription and host instance in the brief. Unknown usage must be
stated; never run a polling loop in an agent.

1. **Collect the details.** Infer what you can and ask only for what's missing:
   - the id (lowercase, dashes);
   - the title;
   - the owners, only if they differ from this node's (a principal from `instance.md`; add a
     new principal there first if needed);
   - the labels (`key:value`, using `label_keys` from `instance.md`);
   - the runtime (one of `runtimes`);
   - the `home`, if the node's material lives outside the instance (required for a sealed
     node). For a node that already exists and is gaining owners, use the node-transition
     skill (`brain seal`) instead.
2. **Create it:** `core/bin/brain new <parent-id>/<id> --title "…" [--owners …] [--labels …] [--runtime …] [--home …]`
   (use just `<id>` when the parent is the root). Note whether it reports **sealed**.
3. **Write the brief.** Fill `brief.md` with what the operator said, and add any node-specific
   rules to its `AGENTS.md` under "Rules for this node". Leave unknown sections empty. This is
   the only time the parent writes in a child's files.
4. **Register with the host.** Follow the host adapter
   (`core/adapters/hosts/<host>.md`): create the node's host project and record it with
   `core/bin/brain set <id> host_project=…`. For a sealed node with a git home, also register
   the home and record `home_project=…`. These IDs stay on this machine (axiom 23).
5. **Start the coordinator.** Host `launch` in the node's project, with the node's runtime and
   this brief:
   > Role: coordinator of node `<id>`. Read the instance `AGENTS.md`, `core/roles/coordinator.md`
   > and every file in your node folder. Reply with a short summary of the node and what you
   > need from the operator. Do no other work yet.

   Record it with `core/bin/brain set <id> coordinator=<thread>`, and pin it if the host can.
6. **Check:** `core/bin/brain check`. Then give the operator a link to the coordinator.
