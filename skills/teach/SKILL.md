---
name: teach
description: Give a node a capability that exists elsewhere (a role, checklist, script, convention) by writing it natively into that node. Use for "set up X in this project the way we do it" or "bring our tester to this node".
---

# Teach (axiom 12)

1. **Check that the node may use it:** `core/bin/brain usable <item> <node>`. If not, stop and
   ask the principals in its scope.
2. **Learn the target's conventions.** Look at where its rules, agents, skills, scripts and
   tests live, and at any generator that produces them. If it has one, change the generator's
   source, never the generated output.
3. **Write a native version.** Adapt the item to the target's stack and naming, for every
   runtime the target uses (axiom 22). Mention nothing above the target's boundary (axiom 6).
4. **Use the target's own change process.**
   - A node on this side of every boundary: commit.
   - A sealed node: a worker in its home makes a branch and a proposal (a PR or the like),
     and nothing is merged or pushed without consent (axiom 24).
5. **Record it.** Note in the teaching node's `status.md` what was taught, where and when.
   Once taught, the target owns the copy.
