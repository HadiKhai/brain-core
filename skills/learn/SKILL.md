---
name: learn
description: Capture reusable knowledge (a method, style, asset, script, lesson or fact) from a node, or a correction to a specialist, as a library item or lesson with provenance and scope. Use when the operator says "keep this", "remember how we did this", or corrects a specialist.
---

# Learn (axiom 10)

1. **Pick the library.** Use the lowest node that every node likely to reuse the item
   descends from; usually it is the one running this skill.
2. **Write the item.** Copy `core/templates/item.md` to `<that node>/library/<item-id>.md`, and
   fill in:
   - `source_node`: where the item came from;
   - `source_ref`: the exact file and commit, URL, or thread with its date;
   - `learned_at`: now;
   - `source_owners`: the effective owners of the source node (`core/bin/brain tree`);
   - `scope`: the same as `source_owners`. Never wider, unless every principal in it consents
     (axiom 11).
3. **Write the body** so it can be used without the source. A general lesson may be abstracted
   from a specific one; the abstraction keeps the same scope unless widened per axiom 11.
4. **A correction to a specialist** goes to that specialist's `lessons.md` instead:
   `- <timestamp> · from <node> · scope <owners of that node>: <lesson>`.
5. Run `core/bin/brain check`, then commit `learn: <item-id>`.
