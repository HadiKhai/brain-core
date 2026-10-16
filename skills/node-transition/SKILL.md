---
name: node-transition
description: Change the tree - relabel, move, change owners (transfer, sealing, unsealing), split, merge, pause, archive, or revoke a principal. Use whenever a node's labels, place, owners or lifecycle change, including "X is joining this project" or "this becomes its own company".
---

# Transition

Run by the coordinator of the node's parent. Only the operator starts an owner change
(axiom 3). The procedures are in `core/MODEL.md`.

| Change | Mechanics | Then |
| --- | --- | --- |
| Relabel | `core/bin/brain relabel <id> key:value …` | nothing else (axiom 18) |
| Move | `core/bin/brain move <id> <new-parent>` | update the host project's folder (host adapter). If the output says SEALED or UNSEALED, run that procedure. |
| New principal | `core/bin/brain principal add <id>` | before naming them as an owner |
| Owners, sealing | `core/bin/brain seal <id> --owners a,b --home <path> [--url <remote>] [--move <files>…]` | the rest of the sealing procedure, below |
| Owners, other | `core/bin/brain owners <id> <a,b>` | if the output says SEALED or UNSEALED, run that procedure |
| Split | `core/bin/brain split <id> <new-id> --title T [--move <files>…]` | write the new node's brief; start its coordinator (node-new, steps 4-5) |
| Merge | `core/bin/brain merge <src> <dst>` | fold `sources/merged-<src>/` into the destination's files; stop the source's coordinator |
| Pause / archive | `core/bin/brain relabel <id> state:paused`, or `core/bin/brain archive <id>` | the node's coordinator writes a final status, and its threads stop |
| Revoke a principal | `core/bin/brain revoke <principal>` | for each node it lists as still co-owned, ask the operator for new owners |

Items keep their provenance through every transition (axiom 19).

**Sealing**: `brain seal` does the mechanics (new home, files moved, trace check). You still:

1. Decide with the operator what belongs to the new owners (the `--move` list) and what stays
   private on this side. When unsure, it stays.
2. Write, in the home's own `AGENTS.md` and in its own conventions, every rule the node still
   needs from above (axioms 5 and 12). Name nothing above the boundary.
3. Run `core/bin/brain check`: it searches the home for upward traces (axiom 6).
4. Ask the principals in scope whether earlier items' scopes change (axiom 11). Until they
   decide, nothing changes.
5. Stay the node's coordinator on this side; the node-sync skill keeps you current with work
   others do in the home.

**Unsealing**: `core/bin/brain owners <id> ''`. Then move back what the parent's world should
hold, and point `home` back with `core/bin/brain set <id> home=here` if it now lives here.

Finish with `core/bin/brain check`, then commit.
