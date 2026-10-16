---
name: check
description: Verify the instance against the model - structure, owners, runtime links, sealed homes, library provenance. Use after any structural change, and before telling the operator something is set up.
---

# Check

1. Run `core/bin/brain check`. It verifies what can be checked mechanically:
   - fields, owners and principals;
   - runtime links (axiom 22);
   - sealed homes outside the instance (axiom 8), with no traces (axiom 6);
   - item provenance and scope (axiom 10).
2. `core/bin/brain check --fix` repairs the safe things (links, registry, sealed-view
   sections, missing template files, machine IDs left in `node.md`). Fix the rest, or explain
   to the operator why something can't be fixed.
3. Also review what it can't check:
   - Do workers' briefs carry only usable items (axiom 9)?
   - Is each file written by its one writer (axiom 15)?
   - Are the `status.md` files fresh (the freshness check)?
