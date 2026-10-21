---
name: review
description: Have a change reviewed by an agent on a different runtime than the one that built it, and get the findings addressed, before the operator is asked to look. Use after a worker finishes a change, before opening or marking a pull request ready.
---

# Second-runtime review

Run by the coordinator that started the builder. A reviewer on another runtime catches what the
builder's runtime tends to miss.

Before launching, run `brain usage`. Use only a subscription allowed for this brain and below
the stop line; follow the subscription and host instance in the brief. Unknown usage must be
stated; never run a polling loop in an agent.

1. **The node's own reviewers first** (axiom 7). If the home has its own review agents or
   checklist, the builder runs them; this review adds to them.
2. **Brief.** `brain review` supplies a separation reason from its independent
   second-runtime review role; `--why-separate` can state a more specific reason.
   `core/bin/brain review <id> --branch <branch> --built-by <builder runtime>
   [--focus "<what worries you>"]`. It picks a reviewer runtime the builder didn't use and says
   where to run. Add `--model <id or display name>` for model-scoped weekly limits.
   When reviewing verify failures, inspect the printed private log, each failing lane's own tail,
   and the overall fallback. `[lane NAME]` prefixes identify interleaved test groups or named steps;
   `FAIL`, `FAILED`, `FAILURE`, `ERROR`, or `exit N` (nonzero) identifies a failing lane. Missing
   lane markers do not mean a group passed. Reused evidence is an earlier green run, not a fresh
   execution; the command, refs, ignored inputs and job ports must still match the reuse rules.
3. **Run it.** Use one reviewer for the concern, not one per file or test step. State why this
   separate agent is needed (independent second-runtime review), and include shared findings
   and earlier rounds in the supplied brief so it need not gather them again. Host `delegate` on the reviewer runtime, in the job's worktree (or `launch` a
   worker there if the host's delegate can't pick a folder). The reviewer changes nothing.
4. **Address.** Send the findings to the builder (host `send`). Blockers and majors get fixed or
   answered with a reason. When a round finds a kind of problem an earlier round already found,
   the builder audits that whole class before the next round, not only the new sites. Repeat 2-4
   until the reviewer has no blockers, at most three rounds: a later brief tells its reviewer
   which round it is and asks it to look for each earlier finding's whole class.
   The builder then records readiness, state and proposed lessons with one `brain handback`
   command. Reviewers never write or refresh the builder's job record.
5. **Report.** In the Active work row and in what you tell the operator: reviewed by `<runtime>`,
   what was found, what was fixed, and anything the builder and reviewer still disagree on. Give
   the operator both sides of a disagreement; don't settle it silently.
