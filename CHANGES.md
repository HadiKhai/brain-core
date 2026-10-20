# Changes, for the agents that run a Brain

Newest first. Each entry says what changed in rules, skills and commands, and what to do after
upgrading. `brain changes <node>` shows the entries this node's coordinator hasn't read yet, and
`brain due <node>` lists them until `brain changes <node> --seen`. Entry headings are
`## <date>: <title>`; once released, a heading never changes.

## 2026-10-21: Group agents by concern and share context

For agents:
- Gather common context once, group related investigation and implementation in one job,
  and state why each separate agent is needed. `brain brief --context-file <file>` embeds
  shared findings (repeatable); `--why-separate "<reason>"` or contract `## Why separate`
  carries the reason. Hire uses the same option and contract fallback; review defaults to
  its independent second-runtime role when no reason is supplied. Old worker/hire calls warn; an explicitly empty reason refuses unless
  `--allow-unexplained` is set for urgent work.
- Optional contract `## Inputs` lists paths, one per line. Declared inputs and context files
  recorded by branch briefs on this machine let `brain jobs` and `brain due` flag each
  overlapping open job once, with advice to merge related work or share a context brief.
  Sharing a context file resolves the pair and clears its due advice; jobs retains a softer
  shared-context line. Directory descendants and globs against literal paths count; common
  Markdown bullet styles work. Put paths on their own lines without notes. The check reads
  local records only and never gates a launch. Old contracts still work.

After upgrading:
- Coordinators: read the updated coordinator role and home-job skill. Add inputs and a
  separation reason when briefing new work; old jobs need no migration.

## 2026-10-20: Runtime questions reach the inbox

For agents:
- Stop hooks capture unanswered Claude/Codex question-tool calls from the supplied transcript,
  screen them and append deduplicated node inbox lines. `brain inbox` and `brain due` see them.
  Hook failures stay fail-open; live host pending requests still need checking.
- Root's T3 tick checks every descendant coordinator's pending requests using its `brain show`
  binding. Scheduled tasks retain the provider they were created under.

After upgrading:
- Re-read `roles/coordinator.md`, `skills/machine-setup/SKILL.md` and `adapters/hosts/t3.md`.
  Refresh root's tick; verify hooks with made-up questions. Reapprove changed Codex hooks.
  With the operator's consent, install the runtime's user-level Stop hook once per machine
  and keep `brain` on PATH. Never install Brain hooks in a home or worktree. Claude uses its
  user settings; Codex uses user-level hooks when supported, otherwise host pending checks.
- Transcript scanning now walks backward in chunks to the last operator message, capped at
  four MiB per stop; questions beyond that cap rely on host pending checks.


## 2026-10-20: History from folders named as the node's

For agents:
- `brain history <node> --also <folder>` (repeatable) also reads sessions that ran in that very
  folder (not in folders below it): work done on the node from somewhere other than its home or
  worktrees, such as a scratch thread. `brain spend <node> --also <folder>` counts them too, and a
  sub-agent only when its own folder counts. A folder holding the home folder or a brain, inside a
  brain or another repository, or holding another project's home, is refused.
- `brain history` and `brain spend` no longer count a session by the remote it recorded when its
  folder still exists and git reads it as another repository; the recorded remote speaks only for
  folders that are gone or aren't repositories. A session inside a brain's folders never counts.

After upgrading:
- Nothing, unless a node's history was made mostly outside its home: then ask the operator which
  folders to name, and extract again with `--fresh --also <folder>`.

## 2026-10-20: Time and tokens per job

For agents:
- When a job closes, its ledger row records what it cost on this machine: tokens by kind (fresh
  input, cache reads and writes, output, reasoning), active and agent hours, tool calls,
  compactions, sub-agents and their share, models. They come from the runtimes' transcripts of
  the job's own sessions: those in its worktree, those given a brief naming it (briefs now carry
  a `brain-job:` line; reviewers are found by it), and those its hand-back recorded. Aggregates
  only. Older ledgers are read and rewritten in the new format; columns a newer core adds are kept.
- `brain jobs` shows medians of tokens and active hours, each open job's spend so far, and advice
  when a job is over twice the median, compacts three times or more, or sub-agents spend most of
  it. Forecasts give active hours and tokens of similar merged jobs. The live view shows the same,
  flags such jobs under attention, and totals this machine per runtime per day and week.
- A contract may set `## Budget` (e.g. `30M tokens, 6 h`). Briefs carry it; `brain handback`
  prints the spend against it; `brain jobs` and the live view flag a job over it.
- `brain spend <node>` looks back over every past session about a node's home (opt-in, like
  `brain history`); `brain spend --machine` totals every session here.
- Codex's compressed sessions (`.jsonl.zst`) are read through `zstd`, by `brain history` too:
  earlier extractions missed them.
- A host that reports usage per thread: `brain jobs <node> --branch <b> --usage-file <json>`.

After upgrading:
- Ask the operator whether to look back: `brain spend <node>` for each node with an external
  home, so the live view has history from the start.
- If earlier `brain history` studies ran on a machine with compressed Codex sessions, extract
  again (`brain history <node> --fresh`, add `--technical` for a technical re-read): they were
  missed.
- Set a `## Budget` in contracts once `brain jobs` shows measured medians for that kind of job.

## 2026-10-20: Briefs to files, merges, history batch sizes

For agents:
- `--out` on `brain brief`, `brain review`, `brain hire` and `brain history … brief` writes a new
  private file, never through a link, and refuses the node's home, its checkouts and the instance.
  Write briefs to a folder of your own outside them (or let the command print the brief).
- `brain history <node>` sizes batches so the runtime that compacted soonest here still holds one
  (60k at most); `--batch-tokens` still sets them yourself.
- `brain connect <node> check` rejects a digest citing more than 30 code files of one home.
- `brain check` gives one Blocked warning per node, with a count. `.brain/hooks.log` says when
  the stop hook held a turn for an upgrade notice.
- `brain merge` no longer fails its commit when the merged node has empty folders.
- A brain holding a link that leads out of it, where brain writes (its own files, `.brain/`, library,
  specialists, a node folder or its job, library, source and report folders), refuses every command
  that changes something until the link is gone; `brain check` lists such links.

After upgrading:
- Run `brain check`. If it lists links leading out of the brain, remove them (or move what they
  point to into the brain).

## 2026-10-20: Upgrades reach running coordinators

For agents:
- A coordinator thread loads its rules when its session begins. While its node has unread change
  entries, the stop hook holds its turn once (a tick's too) and asks it to run `brain changes
  <node>`, re-read `core/roles/coordinator.md` and the skills the entries name, do their "After
  upgrading", then `brain changes <node> --seen`.

After upgrading:
- Nothing beyond what the entries below ask.

## 2026-10-20: Less ceremony per job

For agents:
- `brain changes <node>` shows what changed in core since you last read it; `brain due <node>`
  lists unread entries until `brain changes <node> --seen`. Read them at the start of a session.
- The root tick checks the core's remote daily and reports an available upgrade once. With
  `brain set root auto_upgrade=on` (per machine) it upgrades by itself whenever no job is open.
- `brain handback --reviews N` reuses a green verify run that still proves the commit: nothing
  changed since, or only paths in the node's `verify_skip` globs, and the untracked files verify
  reads (`verify_inputs`, `.env,.env.*` by default) and the job's ports are the same.
  `--reverify` forces a run. A verify that changes what it checks records nothing.
  Run the checks your change affects during the job; leave the full verify to handback.
- A job has a contract: `brain jobs <node> --branch <b> --contract-file <f>` with Outcome,
  Authorized, Discretion, Stop when, Needs consent and Done means. Briefs, hires and reviews
  carry it. Act within it without asking; report starts, results and decisions, not
  acknowledgements; hand back only on a stop condition or for what needs consent.
- Review briefs say which round it is and, from round 2, ask the reviewer to audit each earlier
  finding's whole class; from round 3 they ask for blockers only. `brain jobs` advises when a job
  passes three rounds, runs verify often, or has no contract, and says how long it took to be
  first ready.
- Blocked entries say what they wait on and what clears them:
  `- <what> — waiting on <who or what>; clears when <condition>` (`brain check` warns otherwise).

After upgrading:
- For each code node, set `verify_skip` for paths its verify can't be affected by (docs, notes),
  and `verify_inputs` if verify reads untracked files other than `.env` files.
- Write a contract for each open job, and for each new one before briefing its worker.
- Decide with the operator whether this machine should upgrade core by itself.

## 2026-10-20: Cross-project learning

For agents:
- History readers note the technical side too: how it's built, design rules, failures and fixes,
  code conventions, tools, kinds of work. The final pass writes the node's `knowledge.md` and
  proposes `method`, `style`, `fact` and `lesson` items with `--keywords`, and specialists.
- `brain item add --from a,b` and `brain specialist new --from a,b` learn from several nodes: the
  item lives at their lowest common ancestor, scoped to the owners they share. Widening it needs
  every source owner. Learning carries its sources' never-name words: holding one, it is offered,
  shown, hired or accepted nowhere until edited.
- `brain connect <node>` compares the nodes below it (the `connect` skill): plan, one digest reader
  per node, `check`, then one connector proposing shared items and specialists and writing
  `connections.md`.
- `brain proposals [<node>]` shows everything waiting for review in full; `brain item accept a b c`,
  `brain item reject a b` and `brain specialist accept a b --at <node>` take several ids at once.
- `brain recall <node> "<problem>"` lists what every project learned that this one may use. Briefs,
  hires and reviews point workers at it, and at `knowledge.md` (not in sealed nodes).

After upgrading:
- For nodes already studied with `brain history`, a technical re-read: `brain history <node>
  --technical --fresh`, then readers, check and the final pass as the history skill says.
- Once several nodes are studied: the `connect` skill at the root.
- Review what waits: `brain proposals`.

## 2026-10-19: Every past AI conversation about a node

For agents:
- `brain history <node>` extracts every Claude and Codex conversation about a node's home, from
  every subscription this brain may use, into batches a reader finishes before it compacts. The
  `history` skill: extract, readers per batch, `check`, merge, final pass.

After upgrading:
- Nothing required. Offer the study to the operator for nodes whose import study is done.
