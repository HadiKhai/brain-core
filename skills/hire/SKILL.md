---
name: hire
description: Run a specialist (tester, designer, analyst, editor...) on a job in a node, with its standards, lessons and relevant library items. Use when work needs expertise that a specialist definition covers.
---

# Hire

Before launching, run `brain usage`. Use only a subscription allowed for this brain and below
the stop line; follow the subscription and host instance in the brief. Unknown usage must be
stated; never run a polling loop in an agent.

1. **The node's own agents come first** (axiom 7). If the target (or its home) already has an
   agent or skill for the job, the specialist adds to it, it doesn't replace it.
2. **Pick the items** the job needs: `core/bin/brain item list --node <target>` shows what the
   target may use.
3. **Assemble the brief:**
   `core/bin/brain hire <specialist> <target> --task "<task, constraints, who to report to>" [--items a,b] [--out <file>] [--branch <branch>]`.
   Add `--model <id or display name>` when the job's model is known, so its scoped weekly
   limit is checked. It finds the nearest specialist at or above the target, refuses if its scope doesn't cover
   the target's owners, and includes only the lessons and items in scope.
   For an external-home job, pass its branch so the specialist gets the unified hand-back step.
4. **Run it** as a worker with that brief: host `launch` in the target node (in its home, for an
   external home), or host `delegate` for one-shot work. Use the runtime the brief names unless
   the operator says otherwise.
5. **Review.** When the operator corrects the result, record a lesson (the learn skill, step 5).

To create a specialist: `core/bin/brain specialist new <id> --title "…" [--at <node>] [--scope a,b]`,
then fill in its `role.md`.
