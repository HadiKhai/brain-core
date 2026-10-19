# Role: specialist

A specialist is a reusable role definition, for example a tester, designer, analyst or
editor. It lives in a node's `specialists/<id>/` folder, and that node's descendants can hire
it.

- `role.md`: what the specialist is for, its standards and checklist, its preferred runtime,
  and its `scope` (axiom 10).
- `lessons.md`: corrections it has received. Each lesson has a timestamp, the node it came
  from, and a scope.

## Hiring (the `hire` skill)

A hired specialist runs as a **worker** in the target node. Its brief is assembled from:

1. its `role.md`;
2. the lessons and library items the target node may use (`core/bin/brain usable`);
3. the task.

Imported roles start `proposed` and cannot be hired until the operator/coordinator confirms
their review with `brain specialist accept`. Source roles are summarized, never copied as authority.

The target node's own rules still win (axiom 7). The specialist adds to them.

## Getting better

When the operator corrects a specialist's work, the hirer records the correction with the
`learn` skill as a lesson in `lessons.md`. Its scope is the owners of the node where the
correction happened (axiom 10). The next hire in any node within that scope receives it.
