<div align="center">

# 🧠 Brain

**A brain for your AI agents.**<br>
One tree of projects, a coordinator for each, workers that report back instead of guessing,<br>
and every decision written down, so nothing lives only in a chat that's about to scroll away.

Runs on **Claude Code** and **Codex** (or both at once), in **T3 Code** or any host that can start and message threads.<br>
Python 3.9 standard library. Nothing to install but one command.

[Why](#why) · [How it works](#how-it-works) · [A job, start to finish](#a-job-start-to-finish) · [See it](#see-it) · [Usage](#usage-follows-the-account) · [Import](#bring-an-existing-project) · [Install](#install) · [Commands](#commands) · [The model](#the-model) · [Tests](#tests)

<br>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/img/ui-dark.png">
  <img alt="brain ui: projects, what needs you, work in flight with ports, activity per hour, library" src="docs/img/ui-light.png">
</picture>

<sub>`brain ui` on a demo brain. Every name, project and number in these images is made up.</sub>

</div>

---

## Why

One agent in one repo for one afternoon works fine on its own. The trouble starts when the work is
bigger than one conversation:

| | A bare agent session | With a Brain |
| --- | --- | --- |
| **Memory** | The context window; long runs get compacted and details drift | State lives in files. Any thread can be replaced and picks up exactly where the last one stopped |
| **Decisions** | Somewhere in the chat. "What did I say about X?" becomes a guess | `decisions.md`, each with a timestamp, the reason, the alternatives and your exact words |
| **Many agents** | Nothing stops two of them from taking the same port or the same migration number | One worktree and a block of ports per job, one coordinator per project, a lock on shared files |
| **Many projects** | One repo per session; nothing sees across them | A tree. The root sees every project; each project sees only itself and what's below it |
| **Questions** | A worker asks on a screen nobody is watching | Workers hand questions back to their coordinator; only what the rules don't settle reaches you |
| **Quality** | The model that wrote the change reviews it, if anyone does | A different runtime reviews every change before you're asked to approve it |
| **Learning** | Lost when the session ends | Lessons from each finished job become library items the next job gets in its brief |
| **Consent** | Whatever you repeat in each session | Written rules: no push, deploy, send or anything irreversible without your explicit OK |

For a five-minute fix, open the repository itself or use a small brief. The Brain’s rules apply
in its nodes; a project’s own rules govern its home.

---

## How it works

```mermaid
flowchart TB
    you(["🧑 You"])
    root["<b>Root coordinator</b><br/>sees every project<br/>routes work<br/>reports new needs to you"]
    subgraph projects["Projects: a coordinator each"]
        direction TB
        web["<b>Studio website</b>"]
        app["<b>Field app</b>"]
        client["<b>Northwind portal</b> 🔒<br/>sealed: shared with a client"]
    end
    subgraph jobs["Website jobs: a worktree and ports each"]
        direction TB
        w1["worker<br/>job/hero-redesign"]
        w2["worker<br/>job/blog-search"]
        rev["reviewer<br/>on the other runtime"]
    end
    files[("<b>Files, in git</b><br/>judgement · generated facts<br/>inbox · handbacks<br/>job ledger · lessons")]
    you <--> root
    root --> projects
    web <-->|"brief out<br/>done · blocked · question back"| jobs
    projects -.->|"write as they go"| files
```

- **Nodes** form one tree of any depth: a company, a product, a client, a paper, a side project.
  Each node is a folder with a few files, and optionally its own code repository (its *home*).
- **A coordinator** is one long-lived conversation per node. You can talk to it directly, or to the
  root, which asks the coordinators below it.
- **Workers** do one job each, in their own git worktree with their own ports. They never ask you
  anything: a question goes back to the coordinator, which answers from the rules and the decisions
  already made, and only brings you what they don't settle.
- **Everything is a file.** Threads are disposable; when one ends (or hits a usage limit), the next
  one reads the files and carries on. A resume brief preserves the job’s commits and unfinished work.

### The files of a node

```
nodes/website/
├── node.md          id, owners, labels, runtime, where its home is
├── AGENTS.md        rules for this node (CLAUDE.md links to it)
├── brief.md         what it is, why, who, scope, Limits (each with "Why:" and "Lift when:")
├── status.md        coordinator judgement + generated facts (separate freshness times)
├── decisions.md     each decision: time, what, Why, Alternatives, and Said: "your exact words"
├── inbox.md         anyone may append a line; only the coordinator clears it
├── job-ledger.md    hours, commits, reviews, outcome, lessons and runtime switches
├── job-handbacks/   one worker-owned record per job: state, readiness, proposed lessons
├── never-name.txt   (optional) words this project's code must never contain
├── sources/         material the node works from
└── nodes/           its children, if any
```

One writer per file: the coordinator owns its node's files; everyone else appends to the inbox.
Machine-specific things (thread IDs, local paths, ports) live in `.brain/local.json`, which git
ignores, so the same brain works on every machine that clones it.

---

## A job, start to finish

```mermaid
flowchart TB
    subgraph prepare["Worker prepares"]
        direction LR
        brief["Brief<br/>task + usable lessons"]
        worker["Build<br/>other-runtime review"]
        handback["Handback<br/>verify + ready packet<br/>report + proposed lessons"]
        brief --> worker --> handback
    end
    subgraph close["Coordinator closes"]
        direction LR
        ready["Ready<br/>branch + current main"]
        merge["Merge<br/>with required consent"]
        lessons["Record lessons<br/>close job + prune"]
        ready --> merge --> lessons
    end
    prepare --> close
    close -.->|rank usable items| next["Next brief"]
```

The worker builds in its worktree, gets review on another runtime, fixes findings, commits and
rebases on local `main`. One command finishes its part:

```sh
brain handback website --branch job/blog-search --state done --reviews 2 \
  --report reports/blog-search/index.md --lessons "Use title matches before recency."
```

It runs the node's configured verification, checks local main and stores the worker's state,
ready packet and proposed lessons together. For a blocker or question, use `--state blocked`
or `--state question`, omit `--reviews`, and describe it in the report. Questions reach the
inbox; a blocked human need adds `--need-human "<need>"`.

The coordinator runs `brain ready website --branch job/blog-search` immediately before merging.
It compares the worker’s packet with the branch and current local main; it does not rerun tests.
A moved main goes back to the worker. **Only the coordinator merges**, with required consent.
The packet records worker assertions, not independent proof.

At close, the coordinator saves reusable lessons with their source and scope, then records the
item ids with `brain jobs website --branch job/blog-search --lessons title-first` (or
`--lessons "none: <reason>"`). Briefs, hires and reviews automatically rank up to five usable
items by task, node and specialist. Proposed, rejected and quarantined items never enter a brief.

A clear, low-risk task taking minutes and changing no shared state can use
`brain brief website --small --task "Check the draft's links"`: one-line handback, no worktree,
ledger, review or ready packet. Growing scope returns to the full job flow.

### It wakes itself

A recurring host tick wakes each coordinator to run `brain due website` and check host pending
requests. It handles finished jobs, questions, missing lessons and previews. An empty tick ends
silently and changes no files. The host supplies the wake-up; the CLI supplies evidence.

`brain status website --refresh` generates the facts block in `status.md` from jobs, worktrees,
merges and attention counts. The coordinator writes the judgement around it. Refreshing facts
never makes old judgement look fresh.

`brain inbox root` is the human's one needs-you list. Items have stable ids;
`brain inbox root --new` reports each once per machine and reporter until resolved. Child
coordinators handle their own subtree and route unresolved needs upward. **The root reports to
the human.**

<details>
<summary><b>Inside a generated worker brief</b> (excerpt; <a href="docs/img/brief.txt">full demo capture</a>)</summary>

```markdown
## Task

Rank results by title match first, then recency. Done when the search page shows the 10 best results in under 100 ms.

## Relevant library items

- `flag-before-ship`: Ship UI changes behind a flag, measure, then switch — picked for task: then; same node. Read full: `brain item show flag-before-ship --node website`.
- `review-two-runtimes`: A second runtime reviews every change before you see it — picked for same node. Read full: `brain item show review-two-runtimes --node website`.
```

The full brief adds worktree, ports, subscription, host instance, resume state, limits and handback.
</details>

---

## See it

### In the terminal: `brain`

```text
Sam's Brain (sam)  ·  ~/Brain  ·  machine studio-mac

root                         updated 30m ago
  mobile-app                 updated 1h ago      in flight 1
  newsletter                 updated 1d ago
  northwind                  updated 3h ago      in flight 1 [sealed]
  research                   updated 20h ago
    pricing                  updated 6h ago
  website                    updated 12m ago     inbox 1  in flight 3

Needs attention
  - northwind: Northwind's API key for the staging billing service.
  - research/pricing: Pick the interview list (shortlist in sources/).
  - website: import review: 0 rule file(s), 0 role(s), 1 item(s) proposed; confirm or reject after reviewing source citations
  - website: Approve the new pricing copy on the landing page.
  - website: inbox: 2026-10-19 16:31 +01:00 from worker: question: keep the old hero as a fallback for Safari 15? (job/hero-redesign)
```

Demo captures: [due](docs/img/due.txt) · [inbox](docs/img/inbox.txt) · [usage](docs/img/usage.txt).

### Live in the browser: `brain ui --open`

A read-only page on `127.0.0.1` that updates within seconds of any file changing. It shows
Inbox and Due on the overview, the project tree, jobs, ports, activity, schedules and decisions.
The **Learnings**, **Agents**, **Usage** and **Import checklist** tabs show reusable items and
review state, specialists and hires, account windows, and discovered repositories. Click a
project for its notes, limits, decisions and job history.

<img alt="A project's notes in brain ui: needs you, inbox, now, next, recent changes, limits, decisions with the operator's words, jobs" src="docs/img/ui-project.png">

The page is for this machine only: it listens on `127.0.0.1`, refuses other hosts, and its data
needs a key that is printed with the address. Desktop notifications are opt-in, once per new
inbox item; saved ids survive reloads.

<details>
<summary><b>Four views</b> (demo data; follows your light or dark theme)</summary>

| Learnings | Agents |
| --- | --- |
| <picture><source media="(prefers-color-scheme: dark)" srcset="docs/img/ui-learnings-dark.png"><img alt="Learnings: active and proposed items, source citations and scope" src="docs/img/ui-learnings-light.png"></picture> | <picture><source media="(prefers-color-scheme: dark)" srcset="docs/img/ui-agents-dark.png"><img alt="Agents: specialist roles, lessons and hires" src="docs/img/ui-agents-light.png"></picture> |
| **Usage** | **Import checklist** |
| <picture><source media="(prefers-color-scheme: dark)" srcset="docs/img/ui-usage-dark.png"><img alt="Usage: account-wide windows, stop line, age and reset times" src="docs/img/ui-usage-light.png"></picture> | <picture><source media="(prefers-color-scheme: dark)" srcset="docs/img/ui-imports-dark.png"><img alt="Import checklist: repository candidates and review steps" src="docs/img/ui-imports-light.png"></picture> |

</details>

### Measured, not guessed: `brain jobs`

```text
website: 7 finished (6 merged, 1 abandoned); median 2.8 h and 1.5 review round(s) per merged job
  open  job/hero-redesign              7.5 h  2 review(s)   (over twice the median)
  open  job/blog-search                3.1 h  0 review(s)
  open  job/i18n                       1.2 h  0 review(s)
  cause superseded by the redesign
  blog: 1 similar jobs, too little data (need 3 merged jobs)
  all jobs on this node: 6 jobs, median 2.8 h, spread 1.1–5.6 h (observed min–max)
```

Forecasts use this node’s merged jobs: sample count, median and observed min–max spread.
Fewer than three samples means “too little data.” `brain jobs website --branch job/blog-search`
can include a planned job. Comparisons with reset windows describe serial job time, not token
cost, concurrent throughput or whether the remaining allowance will fit.

Code projects should show a running local preview and a screenshot tour early. Record them with
`brain set website preview=http://localhost:4100 preview_tour=reports/preview/index.md`.
These paths stay machine-local. `due` reminds after merges if the preview is missing; old work
gets a reminder, not a failing check.

---

## Boundaries and privacy

Owners decide what a node can see. A node whose owners differ from its parent's is **sealed**:
its home is its own repository, and nothing inside it may name or depend on anything above it.

```mermaid
flowchart TB
    root["<b>Sam's Brain</b><br/>owners: sam"]
    web["Studio website<br/>owners: sam (inherited)"]
    res["Research<br/>owners: sam"]
    nw["<b>Northwind portal</b> 🔒<br/>owners: sam, northwind"]
    repo[("northwind-portal repo<br/>shared with the client")]
    root --> web & res & nw
    nw ---|"home"| repo
    root -. "sees down" .-> nw
    repo --x|"never names the brain,<br/>its other clients or its paths"| root
```

- **Sight goes down, never up.** A coordinator reads its descendants. A sealed project inherits
  nothing from above; context reaches it only in the briefs of its workers.
- **Words a project must never contain.** List them in the node's `never-name.txt` (a former
  client, a codename). `brain guard` and `brain check` look for them in the project's files, names,
  staged changes, submodules, branch names and commit messages, and never print the word itself.
  `brain brief` refuses a brief that contains one, and `brain wt add` refuses such a branch name.
- **Consent for what can't be undone.** Push, deploy, publish, send or delete needs your explicit
  OK, and the owners' for a shared project.
- **Knowledge has a scope.** A lesson learned in a client project is usable only where every owner
  of the using project is in its scope. Widening it needs everyone's consent.

---

## Several brains, several machines

```mermaid
flowchart TB
    account[("Studio subscription<br/>server-wide 5h + 7d usage")]
    backup[("Overflow subscription<br/>its own 5h + 7d usage")]
    other["Sam's second machine"] -->|uses Studio too| account
    subgraph machine["Sam's studio machine"]
        poller["One 5-minute poller"]
        cache[("Machine usage cache")]
        route["Allowed brain + runtime pin<br/>stop line + pace + headroom"]
        personal["Personal brain worker"]
        work["Work brain worker"]
        poller --> cache --> route
        route -->|allowed subscription| personal
        route -->|allowed subscription| work
    end
    account -->|account-wide reading| poller
    backup -->|account-wide reading| poller
```

- **One command, any number of brains.** `brain` picks the brain whose folders you're in (its
  instance, or a project's code, workbench or worktree), else the one whose pointer is in the
  session's config folder, else the default. `brain -b work` picks one by name.
- **They never share a file.** A work brain can live only on the work laptop, with no remote at
  all; a personal one can follow you across machines with `install.sh --from <its git URL>`.
- **Per machine:** thread IDs, local paths, worktrees and ports stay in `.brain/local.json`.

### Usage follows the account

Machine-level `subscriptions.json` declares runtimes, config folders, host instances and allowed
brains, outside every brain. One `brain usage --watch --every 300` service polls every five minutes;
agents read the cache with `brain usage`. On macOS, `brain usage install` writes a LaunchAgent
and prints how to load it. See [machine setup](skills/machine-setup/SKILL.md#subscriptions-on-this-machine).

**Server readings are account-wide.** The same subscription on several machines has one
combined usage reading, not a fresh allowance per machine. Brain shows five-hour and seven-day
windows, reading age and resets. Stale or missing readings stay unknown, never spare capacity.

Before launch, routing filters by allowed brain and runtime, stops at the configured line
(default 85% of either applicable window), then favors weekly pacing and headroom. The machine's
fallback order breaks ties. Briefs name the subscription and host instance. A node's runtime
pin stays in force: at its limit, the brief names an alternative and waits for an explicit
`--runtime` choice. Pass `--model` for model-scoped limits. Without declared subscriptions,
existing runtime choices stay unchanged.

If a running AI hits a usage limit, the coordinator stops it, records the switch with
`brain jobs website --branch job/blog-search --switch "claude -> codex: usage limit"`, and
regenerates the brief with `--runtime codex`. The replacement continues in the same worktree
with the existing commits, handback and ledger state. If no eligible subscription remains,
it waits for a reset.

## Bring an existing project

Start with `brain scan ~/code --json`: read-only discovery. Choose the main repositories to
adopt, confirm the brain, parent and owners, then run `brain adopt ~/code/garden-notes`.
**Work and personal brains never mix.** Scan flags homes already assigned to another brain;
never adopt worktrees or nested repositories as separate projects.

Adopt records an import study job. `brain brief garden-notes --import` prepares a worker to
study local history and agent setup without changing the source repo or running its scripts.
`brain scrub garden-notes <path>` filters source evidence without revealing forbidden terms.
The study records cited decisions, unknowns and proposals, then finishes through `handback`.

Imported rules land in `sources/import-proposals.md` as **imported and unreviewed**; roles and
lessons start proposed and unusable. After actual review, accept a lesson with
`brain item accept <id> --reviewed-by sam`, or reject it with `brain item reject <id>`. Workers
never accept their own proposals. The coordinator reviews rules and roles before activating them.
Recording proposed ids in the ledger never accepts them.

---

## Install

A new brain (asks for your id, the runtimes you use and your timezone):

```sh
curl -fsSL https://raw.githubusercontent.com/HadiKhai/brain-core/main/install.sh | sh
```

The same brain on another machine:

```sh
curl -fsSL https://raw.githubusercontent.com/HadiKhai/brain-core/main/install.sh | sh -s -- --from <your brain's git URL>
```

A second brain on the same machine, with its own config folder:

```sh
curl -fsSL https://raw.githubusercontent.com/HadiKhai/brain-core/main/install.sh | sh -s -- --dir ~/WorkBrain --name work --global claude=~/.claude-work
```

The installer puts `brain` in `~/.local/bin` (on your PATH) and a short pointer in each runtime's
user-wide instructions, so an agent started anywhere knows where the brain is (`--no-global`
skips that). Then open your host at the brain's folder and say **"set up this machine"**: it creates
the projects and a coordinator thread for each node.

Then talk to it:

> *"New project: a pricing study for the studio, under research."*<br>
> *"What's waiting on me?"*<br>
> *"Website: add search to the blog. Ship behind a flag."*<br>
> *"Keep this as a lesson: list trusted dependencies explicitly."*

---

## Commands

You mostly talk to coordinators, and they run `brain` for you. By hand:

<details open>
<summary><b>See</b></summary>

| Command | Does |
| --- | --- |
| `brain` | dashboard: the tree, what needs you, coordinators |
| `brain ui [--open]` | the same, live in your browser (read-only, this machine only) |
| `brain show <node>` | everything about one node, with this machine's IDs and the status time in UTC |
| `brain jobs [node] [--branch B]` | job history and forecasts: samples, medians, spread, causes |
| `brain due [node]` | coordinator actions from files and local git; empty means silence |
| `brain inbox [node] [--new]` | one needs-you list; stable ids, only-new reporting per machine and reporter |
| `brain status <node> --refresh` | regenerate facts without advancing judgement freshness |
| `brain usage` | allowed subscriptions, account-wide windows, age and resets |
| `brain stale <node> <host time>` | is a node's status behind its thread? Compares moments, whatever the timezones |
| `brain sync <node>` / `--all` | what happened in a node and its home since the last sync |
| `brain check [--fix]` | verify the axioms that can be checked mechanically; repair the safe things |
| `brain machine` | this machine's bindings |
</details>

<details>
<summary><b>Shape the tree</b></summary>

| Command | Does |
| --- | --- |
| `brain new <id> --title T [--owners a,b] [--home path]` | create a node (ids nest with `/`) |
| `brain set <id> key=value` | fields; host IDs and home paths stay on this machine |
| `brain relabel`, `move`, `archive` | re-file a node |
| `brain split`, `merge` | reshape nodes, keeping provenance |
| `brain principal add <id>` | a new person, group or organization |
| `brain owners <id> a,b` | change owners |
| `brain seal <id> --owners a,b --home path` | give a node its own owners and home |
| `brain revoke <principal>` | end a relationship: quarantine its items and archive its nodes |
</details>

<details>
<summary><b>Run work</b></summary>

| Command | Does |
| --- | --- |
| `brain wt add/list/rm/prune` | one worktree per job beside the project's repo, with setup and a block of ports |
| `brain brief <id> [--branch B] --task-file F` | task, limits, routing, resume state and ranked usable lessons |
| `brain brief <id> --small --task "..."` | a minutes-long, low-risk task without job ceremony |
| `brain handback <id> --branch B --state done --reviews N --report P --lessons "..."` | one worker finishing step: verify, readiness, state and proposed lessons; also accepts blocked/question |
| `brain ready <id> --branch B` | compare the worker’s packet with branch and current local main before merge |
| `brain jobs <id> --branch B --lessons "..."` | coordinator records learned item ids or `none: <reason>` at close |
| `brain review <id> --branch B` | a brief for a review by a runtime the builder didn't use |
| `brain guard <id> [path ...]` | the project holds none of the words in `never-name.txt` |
| `brain workbench <id>`, `brain report <id> "topic"`, `brain reports` | scripts and reports, in a predictable place |
| `brain schedule list/add/bind` | recurring work, shared; each machine runs what it binds |
| `brain now` | the time, in the brain's timezone, with its offset |
</details>

<details>
<summary><b>Knowledge</b></summary>

| Command | Does |
| --- | --- |
| `brain item add/list/show/scope/quarantine/restore` | the library, provenance and scopes |
| `brain item accept <id> --reviewed-by sam` / `brain item reject <id>` | review proposed learning; rejected items stay unusable |
| `brain usable <item> <node>` | may this node use this item? |
| `brain specialist new`, `brain lesson`, `brain hire` | reusable experts, their lessons, a brief that brings both |
</details>

<details>
<summary><b>Import, this machine and its brains</b></summary>

| Command | Does |
| --- | --- |
| `brain scan <folders> --json` | discover main repo candidates without writing |
| `brain adopt <paths> [--parent <node>]` | adopt selected homes and record import study jobs |
| `brain brief <node> --import` | study history into cited node state and inactive proposals |
| `brain scrub <node> [paths ...] [--check]` | filter evidence, or check outputs without exposing forbidden terms |
| `brain usage --watch --every 300` | one machine poller; agents only read |
| `brain link --global [runtime=folder ...]` | a pointer to this brain in runtimes' user-wide instructions |
| `brain brains`, `brain use <name>`, `brain -b <name> …`, `brain --all` | several brains: list, default, pick, every dashboard |
| `brain upgrade` | update `core/` to the latest version, then link and check |
| `brain hook stop` / `brain hook approve` | the stop hook both runtimes run; trust it in Codex |
</details>

---

## What's in this repository

This repository is the **core**: it holds no one's data. Each brain is its own git repository and
uses the core as a submodule at `core/`, binding it in its `instance.md`.

| Path | What it is |
| --- | --- |
| [`MODEL.md`](MODEL.md) | primitives, the 24 axioms, procedures, and the runtime and host contracts. The source of truth |
| [`roles/`](roles) | the coordinator, worker and specialist protocols |
| [`skills/`](skills) | judgment procedures: import, jobs, review, learning, node changes and machine setup |
| [`templates/`](templates) | node, root, item, specialist and new-home templates |
| [`adapters/runtimes/`](adapters/runtimes) | one small file per agent runtime (Claude Code, Codex), with their stop hooks |
| [`adapters/hosts/`](adapters/hosts) | one mapping per host (T3 Code), plus a small `.env` the tool reads |
| [`bin/brain`](bin/brain) | the tool. Python 3.9+, standard library only |
| [`ui/index.html`](ui/index.html) | the live page `brain ui` serves. One file, no build step |
| [`install.sh`](install.sh) | one-line install: a new brain, another machine, or a second brain |
| [`tests/`](tests) | `brain selftest` and `live.py` |
| [`docs/`](docs) | these images, and the script that makes them from a demo brain |

---

## The model

Everything above follows from a small model: **principals** own **nodes** in one **tree**;
**boundaries** appear where owners change; **threads** run **roles** on **runtimes** inside a **host**;
**items** carry their provenance and scope. [`MODEL.md`](MODEL.md) has it in full.

<details>
<summary><b>The 24 axioms, in one line each</b></summary>

| # | Axiom |
| --- | --- |
| 1 | One tree, any depth; the root is a node like any other |
| 2 | Rules belong to nodes; labels carry none |
| 3 | Boundaries are derived from owners, never declared |
| 4 | Sight goes down |
| 5 | Inheritance goes down and stops at a boundary |
| 6 | No upward traces inside a sealed node |
| 7 | Nearest rules win |
| 8 | Layout follows sight: folders never load rules across a boundary |
| 9 | Context crosses a boundary only in briefs |
| 10 | Learning up is a citation, with provenance and a scope |
| 11 | Only the named owners can widen a scope |
| 12 | Teaching down is a native copy, through the receiver's own process |
| 13 | Relations live at the nearest common ancestor |
| 14 | Coordinators are recursive and optional |
| 15 | One writer per file; workers prepare, coordinators merge |
| 16 | State lives in files; handbacks, facts, ticks and resume survive threads |
| 17 | Full timestamps; reject status over five minutes ahead, warn on other future entries |
| 18 | Relabeling changes only labels |
| 19 | Transitions preserve provenance |
| 20 | Boundaries are recomputed after every transition |
| 21 | Revocation quarantines |
| 22 | Runtime neutrality: every axiom is tested on every runtime in use |
| 23 | Host and machine neutrality |
| 24 | Consent for what can't be taken back |
</details>

---

## Tests

Two layers: mechanical checks and real-agent scenarios. A passing unit suite does not prove
live billing reads, host launches or reviewer quality.

**`brain selftest`**: 214 tests in throwaway folders: the tree and its transitions, sealing
and traces, several brains on one machine, worktrees and ports, the job ledger, the guard (binary
files, Unicode, unreadable folders, submodules), locks under 16 parallel commands, the stop hook,
the live page and its key, timezones and future timestamps, handbacks and facts, inbox ids,
usage routing, imports, lessons, crash recovery, install and upgrade.

**`tests/live.py`**: the same use cases with real model calls, on **every** runtime in use (axiom 22),
judged by the files the agent leaves behind:

| Scenario | What a real agent must do |
| --- | --- |
| `parity`, `coordinator_writes_as_it_goes`, `worker_asks_the_writer` | load neutral rules; write timestamped decisions; respect file ownership |
| `learning_is_a_citation`, `lessons_become_items`, `limits_carry_their_reason` | preserve sources and scopes; learn at close; explain limits |
| `no_upward_traces`, `second_runtime_review` | respect sealed homes; review another runtime's change |
| `stop_hook_fires`, `notes_current_after_an_edit`, `freshness_across_timezones` | keep notes current and compare absolute times |
| `new_node_in_one_sentence`, `a_session_reaches_its_own_brain`, `worker_hands_back_a_question` | create a node; select the right brain; return an unresolved choice |
| `nine_` | capture close lessons; resume committed or dirty work; repair future status times |
| `usage_` | route before launch and respect a runtime pin |
| `autonomy_` | unified handback, finished-job wake, silent empty wake, subtree inbox and generated facts |
| `import_` | study history into cited decisions and unreviewed rules, roles and lessons |
| `r2_` | reuse a relevant lesson and finish a small job without ceremony |
| `recover_` | replace a coordinator, resume a worker on another runtime, route two jobs before launch |

```sh
brain selftest
python3 core/tests/live.py                     # every runtime in instance.md
LIVE_CODEX_BIN=/path/to/codex python3 core/tests/live.py --runtimes codex
```

The runner prints each executable and version, and stops calling a runtime that becomes
unavailable (a usage limit, say) instead of failing every remaining scenario. For the live page,
`tests/ui-checks.js` runs in a browser against a throwaway brain: timezones, notifications and
navigation. See [recovery rehearsals](docs/recovery.md) for real-agent fixtures, SIGKILL checks
and their limits. They test routing artifacts and file recovery, not real subscription exhaustion
or independent review quality.

---

## Changing the core

The core stays generic: if a change needs a specific name, it belongs in a brain, not here.
After any change:

1. `brain selftest`
2. `python3 core/tests/live.py` on every runtime
3. `python3 docs/make-screenshots.py` if the page changed; use `--text-only` for CLI captures (fake data only)
4. commit here; brains pick it up with `brain upgrade`

