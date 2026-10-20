# Changes, for the agents that run a Brain

Newest first. Each entry says what changed in rules, skills and commands, and what to do after
upgrading. `brain changes <node>` shows the entries this node's coordinator hasn't read yet, and
`brain due <node>` lists them until `brain changes <node> --seen`. Entry headings are
`## <date>: <title>`; once released, a heading never changes.

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
