---
name: hire
description: Run a specialist (tester, designer, analyst, editor...) on a job in a node, with its standards, lessons and relevant library items. Use when work needs expertise that a specialist definition covers.
---

# Hire

1. **Find the specialist.** Look in `specialists/<id>/role.md` in this node and its
   ancestors, and pick the nearest. The target node's own agents for the job come first
   (axiom 7). The specialist adds to them.
2. **Check the scope.** Every owner of the target node must be in the specialist's `scope`.
3. **Assemble the brief:**
   - the role, standards and checklist from `role.md`;
   - the lessons from `lessons.md` whose scope covers the target's owners;
   - the library items the target may use (`core/bin/brain usable`);
   - the task, the constraints, and who to report to.
4. **Run it** as a worker: host `launch` in the target node (in its home, for a sealed node),
   or host `delegate` for one-shot work. Use the runtime from `role.md` unless the operator
   says otherwise.
5. **Review.** When the operator corrects the result, run `learn` to add a lesson.
