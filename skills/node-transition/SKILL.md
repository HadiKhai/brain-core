---
name: node-transition
description: Change the tree - relabel, move, change owners (transfer, sealing, unsealing), split, merge, pause, archive, or revoke a principal. Use whenever a node's labels, place, owners or lifecycle change.
---

# Transition

Run by the coordinator of the node's parent. Only the operator starts an owner change
(axiom 3). The procedures are in `core/MODEL.md`.

| Change | Mechanics | Then |
| --- | --- | --- |
| Relabel | `core/bin/brain relabel <id> key:value …` | nothing else (axiom 18) |
| Move | `core/bin/brain move <id> <new-parent>` | update the host project's folder (host adapter). If the output says SEALED or UNSEALED, run that procedure. |
| Owners | `core/bin/brain owners <id> <a,b>` | if the output says SEALED or UNSEALED, run that procedure |
| Pause / archive | `core/bin/brain relabel <id> state:paused`, or `core/bin/brain archive <id>` | the node's coordinator writes a final status, and its threads stop |
| Split / merge | `core/bin/brain new` for each resulting node, then move files | items keep their provenance (axiom 19) |
| Revoke a principal | find every item whose scope only that principal's nodes can use, and set `status: quarantined` | archive the nodes that principal alone owns (axiom 21) |

**Sealing**, in short:

1. Split the state by owner.
2. Copy in what the node still needs, natively.
3. Remove upward traces and verify with `core/bin/brain check`.
4. Ask the principals in scope about earlier items.
5. The coordinator stays on the parent's side.

Finish with `core/bin/brain check`, then commit.
