---
name: review
description: Have a change reviewed in a fresh context by an agent that did not build it, and get the findings addressed, before the operator is asked to look. Use after a worker finishes a change, before opening or marking a pull request ready.
---

# Independent review

Run by the coordinator that started the builder. Use a fresh reviewer context that
did not build the change. The runtime and model may match the builder when authorized;
record the reviewer's actual thread ID, runtime and model.

Before launching, run `brain usage`. Use only a subscription allowed for this brain and below
the stop line; follow the subscription and host instance in the brief. Unknown usage must be
stated; never run a polling loop in an agent.

1. **The node's own reviewers first** (axiom 7). If the home has its own review agents or
   checklist, the builder runs them; this review adds to them.
2. **Brief.** State the independent non-builder review role; `--why-separate` can
   state a more specific reason.
   `core/bin/brain review <id> --branch <branch> --built-by <builder runtime>
   [--focus "<what worries you>"]`. It picks a reviewer runtime the builder didn't use and says
   where to run. That is the current CLI selection behavior, not an independence
   requirement. If it cannot represent the authorized reviewer, the coordinator
   supplies a separate explicit brief and reports the actual external review; never
   invent native review records. Add `--model <id or display name>` for model-scoped weekly limits.
   When reviewing verify failures, inspect the printed private log, each failing lane's own tail,
   and the overall fallback. `[lane NAME]` prefixes identify interleaved test groups or named steps;
   `FAIL`, `FAILED`, `FAILURE`, `ERROR`, or `exit N` (nonzero) identifies a failing lane. Missing
   lane markers do not mean a group passed. Reused evidence is an earlier green run, not a fresh
   execution; the command, refs, ignored inputs and job ports must still match the reuse rules.
   With configured `verify_lanes` or `verify_lanes_from`, inspect the packet's `verify_lanes`
   entries and each private log, including dependency skips and reused results. An aggregate
   green alone is insufficient: the map/context and every job lane's input hash must still
   match. Nightly lanes belong to the node's full `verify` command used by trains/nightlies.
   Review the optional `verify_lanes_check` coverage gate as project-owned proof that every
   test belongs to exactly one lane.
   Input-change refusals identify only relative paths and added/removed/modified status. If verify
   modified its own inputs, fix setup or the writing runner before the first verify; exclude a
   file only when it cannot affect the result. Check setup completion, elapsed time and failure
   status, path/never-name screening, and that private per-file evidence never enters UI output.
   Mechanical checks run before each brief: Done means items need an existing named
   `test: tests/file.py::test_name` or a stated `gap: explanation` in the contract
   (`--contract <path>` overrides the recorded job contract). Missing evidence is flagged.
   Test references and seed content come from the reviewed commit, including for internal nodes.
   The optional node `fitness=<command>` gate runs in a registered, clean worktree at that head
   and reports its exit status; otherwise it reports skipped. Its 30-second timeout stops the
   process group. Reviews without an existing open job state that their rounds are unrecorded
   and uncapped; a review never creates an uncloseable ledger job. Seed lint
   reuses guard/scrub and flags configured names and home paths in changed fixtures/tests;
   never-name evidence refuses the brief without quoting it. Inspect all gaps and failures.

3. **Run it.** Use one reviewer for the concern, not one per file or test step. State why this
   separate agent is needed (fresh-context non-builder review), and include shared findings
   and earlier rounds in the supplied brief so it need not gather them again. Host `delegate` on the reviewer runtime, in the job's worktree (or `launch` a
   worker there if the host's delegate can't pick a folder). The reviewer changes nothing.
   Rehearsal is an optional diagnostic; do not start it automatically. If explicitly requested,
   inspect its report and preserve raw failures. Report exactly what ran, or state that it was not run.
4. **Address.** Send the findings to the builder (host `send`). Blockers and majors get fixed or
   answered with a reason. When a round finds a kind of problem an earlier round already found,
   the builder audits that whole class before the fix check, not only the new sites. Use one full
   round, then `brain review <id> --branch <branch> --check-fix --findings <prior.json>`.
   The JSON is a list of objects with `severity` (`blocker`, `major`, `minor`) and `text`.
   Only blockers/majors enter the fix brief. Its fix range starts at the previous review head;
   `--fix-base <commit>` overrides it. Ask only whether those findings are fixed and whether
   the fix broke anything. A second full round or any third brief requires
   `--reason <text>`, a nonempty one-line explanation recorded in the ledger. Jobs are capped at
   3 rounds; `--risk security|permissions|data-loss|concurrency` marks the change in the ledger
   for up to 5 rounds. Later rounds retain that mark and still require their own reason.
   At the cap, narrow the change or hand back for a coordinator decision.
   The builder then records readiness, state and proposed lessons with one `brain handback`
   command. Reviewers never write or refresh the builder's job record.
5. **Report.** In the Active work row and in what you tell the operator: reviewed by `<runtime>`,
   what was found, what was fixed, and anything the builder and reviewer still disagree on. Give
   the operator both sides of a disagreement; don't settle it silently.

For core changes, `brain rehearse <id> --branch <branch>` remains available when
explicitly requested. It executes the committed branch using throwaway copies
and requires machine admission and an OS write/network sandbox (currently macOS).
Missing, failed or stale rehearsal reports never block readiness or landing,
including on legacy nodes with `rehearse=required`. Other proof requirements still apply.

Rehearsal also sends one real constant-event POST per distinct bound coordinator webhook.
It uses an explicit User-Agent, never prints the URL, and fails on any non-2xx
status (including redirects). The temporary-copy command sandbox denies network;
this authorized delivery probe runs separately against the real bindings.
