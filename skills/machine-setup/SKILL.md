---
name: machine-setup
description: Bind this machine to the instance - home paths for external homes, host projects and coordinator threads - after installing or cloning the instance on a new machine. Use when told "set up this machine", or when `brain` says a node has no coordinator on this machine.
---

# Machine setup (axiom 23)

Run by the thread the operator opened at the instance root. Host IDs and local paths belong to
one machine and live in `.brain/local.json`, never in git.

1. Run `core/bin/brain machine` to see what this machine is missing.
   Set the operator's provider fallback order with `brain set root fallback=<first>,<next>`.
   These are host provider instance ids from the host catalog, kept as a plain ordered
   `fallback` list in `.brain/local.json`; an empty value clears it. Workers that hit a usage
   limit resume on the next available instance in the same worktree (coordinator role).
2. **Homes.** For each node whose home is missing, ask the operator where it is on this machine.
   If it isn't there yet and `home_url` is known, offer to clone it (that's a network action:
   ask first). Then `core/bin/brain set <id> home=<path>`.
3. **The root.** This thread's own project and thread are the root's:
   `core/bin/brain set root host_project=<this project> coordinator=<this thread>`.
4. **Each other node**, parents before children, following the host adapter
   (`core/adapters/hosts/<host>.md`):
   - find or create its host project (folder = the node folder), then
     `core/bin/brain set <id> host_project=<id>`;
   - for an external home that is a git repo, find or create a host project for the home, then
     `core/bin/brain set <id> home_project=<id>`;
   - launch its coordinator with the brief in the node-new skill, step 5, and record it with
     `core/bin/brain set <id> coordinator=<thread>`. Pin it if the host can.
   Skip archived nodes.
5. **Schedules.** `core/bin/brain schedule list -v` shows the instance's recurring work. Each
   schedule should run on one machine only, or its brief arrives twice. For each one that isn't
   on this machine, ask the operator: "Should this machine run <id> (<when>)? It is probably
   already running on another machine." If yes, create it with host `schedule` (the brief from
   the list, targeting the node's coordinator) and record it:
   `core/bin/brain schedule bind <id> <host id>`. If this machine replaces another, tell the
   operator to remove it there (`brain schedule unbind <id>` on that machine).
6. **Worktree setup.** For each node with an external git home, check that its
   `worktree-setup.sh` (if any) works here: `core/bin/brain wt add <id> setup-check`, then
   `core/bin/brain wt rm <id> setup-check --force`. Ask before doing this in a home others own:
   a worktree is local, but it is still their repository.
7. **The global pointer.** Unless the operator declined it during install: if
   `core/bin/brain brains` shows other brains on this machine, or a runtime has several config
   folders here (one per account, say), ask the operator which folders this brain's pointer goes
   in, then `core/bin/brain link --global <runtime>=<folder> ...` (one per folder). A session
   started with that folder reaches this brain, and only folders named this way see it.
   Otherwise `core/bin/brain link --global`.
8. **Subscriptions.** The install only detects one account per runtime (its default folder).
   List the host's provider instances (`orchestrator_capabilities` on T3). If a runtime has
   several (a work and a personal Claude, say), or the operator mentions another account, ask
   which config folder each one uses and which brains may use it. Write `subscriptions.json`
   ([below](#subscriptions-on-this-machine)), one entry per account, bound to its host instance.
   Then `core/bin/brain usage --refresh`: each account should show its own reading. Two
   entries showing one reading mean both folders are logged in to the same account.
9. **Hooks.** If Codex is a runtime, ask the operator's OK, then run `core/bin/brain hook approve`:
   Codex runs the instance's stop hook only once the project is trusted and the hook approved.
   Run it again after a core upgrade that changes the hook.
10. Run `core/bin/brain check`, then give the operator one link per coordinator
    (`core/bin/brain` lists them).

## Subscriptions on this machine

`brain usage` reads account-wide limits from the machine cache. Declare subscriptions in
`subscriptions.json` beside the registry (`BRAIN_CONFIG` names that registry file; normally
`~/.config/brain/brains.json`). Keep the file outside every brain. Discover T3 provider instance
IDs with `orchestrator_capabilities`, never infer them from a runtime's name:

```json
{
  "stop_percent": 85,
  "pacing_margin": 10,
  "subscriptions": [
    {"id": "personal-codex", "runtime": "codex", "label": "Personal",
     "config_dir": "~/.codex", "host_provider_instance_id": "<from capabilities>",
     "brains": ["personal"]},
    {"id": "work-claude", "runtime": "claude", "label": "Work",
     "config_dir": "~/.claude-work", "host_provider_instance_id": "<from capabilities>",
     "brains": ["work"]}
  ]
}
```

Names in `brains` are machine registry names (`brain brains`); `[]` allows none, `["*"]`
explicitly allows every brain. Without a file, installed runtimes each have one subscription,
using `CLAUDE_CONFIG_DIR` / `CODEX_HOME` when set, otherwise `~/.claude` / `~/.codex`, allowed
for every brain; the output says so. These defaults are informational until subscriptions are
declared: existing runtime choices stay unchanged. Bind host instances explicitly before
launching; defaults cannot infer them. Brain usage output lists only subscriptions that brain
may use, plus a count of others; the UI never exposes config directories.
Duplicate account IDs share one reading while each entry keeps its own brain allow-list.
Account keys are hashed; account names, tokens and credit balances are not cached.
An explicitly absent Codex five-hour window shows `none`; missing readings stay `unknown`.
`pacing_margin` is percentage points ahead of the fraction of the week elapsed, default 10.
The fullest window determines headroom; `.brain/local.json`'s ordered `fallback` host instance
IDs break ties; otherwise equal readings keep the preferred runtime. A node's `runtime` pins
`brain brief` just as a specialist's runtime constrains `hire`. At its stop line, the brief keeps
the pin, says to stop, and names an eligible alternative; `--runtime` explicitly overrides it.
Review always uses a runtime different from the builder. Pass `--model <id or display name>`
to brief/hire/review to apply matching model-scoped weekly limits; other model limits are shown
separately and ignored for routing. Unknown model means no scoped limit can be applied.

Run `brain usage --refresh` once to check access. Codex uses read-only app-server account
lookups; on macOS Claude reads its stored OAuth credential through `/usr/bin/security`, with
stdout captured only in memory, no stdin or terminal, and a two-second timeout. The CLI has no
per-read flag to suppress Keychain UI; a denied or timed-out read retries after ten minutes,
with at most one failed attempt per service per refresh cycle. Non-macOS reads
`$CLAUDE_CONFIG_DIR/.credentials.json` (default `~/.claude`). Failures fall back to timestamped
logs or unknown. Merge observations per window: retain newer cached windows and their original
source and time during an outage. A failed inline refresh never relabels or freshens good
readings, so an old low log value cannot clear a stop line. Tokens never belong in this file,
command arguments, reports or the cache.
Local logs do not reliably measure this machine's share of the account-wide allowance.

Keep one `brain usage --watch --every 300` process per machine, outside agent threads.
`install.sh` starts it (skip with `--no-usage`). On macOS, `brain usage install` writes
`~/Library/LaunchAgents/org.brain.usage.plist` and loads it; running it again is safe (unchanged:
nothing happens; changed: the agent is reloaded) and it refuses to replace a plist that isn't its
own. `brain usage uninstall` stops and removes it; `brain upgrade` restarts it on the new core.
`--no-load` or `--launch-agent <path>` only writes the plist. Elsewhere, run the watch loop as a
background service yourself.
The LaunchAgent restarts failed exits, throttled to thirty seconds, and writes stdout/stderr
to `~/Library/Logs/brain-usage.log`; a duplicate poller exits successfully without restarting.
One bad refresh logs a generic failure and the watch loop continues.
Agents read `usage.json`; freshness is twice its stored poll interval (two minutes for old
caches). While a live poller holds the watch lock, brief/hire/review never poll inline, even
when stale. The UI reads without calling runtimes. Expired observations are unknown until
refreshed; fallback skips transcript files older than eight days.
