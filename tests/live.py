#!/usr/bin/env python3
"""Live runtime tests (axiom 22): the same use cases, run by every runtime, judged by the files they leave.

Each scenario builds a throwaway instance, starts a real agent in it with a short brief, then checks the
result mechanically. Costs one model call per scenario per runtime.

    python3 core/tests/live.py [--runtimes claude,codex] [-k name] [--keep] [--jobs 4]

Environment: LIVE_CLAUDE_MODEL (default claude-sonnet-5-5), LIVE_CODEX_EFFORT (default medium).
LIVE_CLAUDE_BIN and LIVE_CODEX_BIN select an executable explicitly; otherwise PATH is used.
"""
import argparse
import concurrent.futures as cf
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

CORE = Path(__file__).resolve().parent.parent
BRAIN = CORE / "bin" / "brain"
SKILLS = sorted(p.name for p in (CORE / "skills").iterdir() if (p / "SKILL.md").exists())
GIT_ENV = {"GIT_AUTHOR_NAME": "live", "GIT_AUTHOR_EMAIL": "live@test", "GIT_COMMITTER_NAME": "live",
           "GIT_COMMITTER_EMAIL": "live@test", "BRAIN_NO_REEXEC": "1"}


def brains_config(folder):
    """Each run lists its brains in its own file, never in this machine's real list."""
    return {"BRAIN_CONFIG": str(Path(folder) / "brains.json")}


def runtime_bin(runtime):
    requested = os.environ.get(f"LIVE_{runtime.upper()}_BIN", runtime)
    found = shutil.which(requested)
    if not found:
        raise RuntimeError(f"live: executable not found for {runtime}: {requested}")
    return found


def runtime_path(runtime):
    # Hook approval starts the CLI too: use the same executable directory as the scenario agent.
    return f"{Path(runtime_bin(runtime)).parent}:{os.environ.get('PATH', '')}"


class RuntimeUnavailable(RuntimeError):
    """The CLI failed before it could provide a usable scenario result."""


def sh(*args, cwd=None, check=True, env=None):
    r = subprocess.run(list(args), cwd=cwd, capture_output=True, text=True, env={**os.environ, **GIT_ENV, **(env or {})})
    if check and r.returncode:
        raise RuntimeError(f"{' '.join(map(str, args))}: {r.stderr or r.stdout}")
    return r.stdout


def brain(root, *args, check=True):
    return sh(sys.executable, str(BRAIN), *args, cwd=root, check=check, env=brains_config(Path(root).parent))


class Run:
    """One scenario on one runtime: a fresh instance, an agent call, then checks."""

    def __init__(self, scenario, runtime, base):
        self.scenario, self.runtime = scenario, runtime
        self.tmp = Path(tempfile.mkdtemp(prefix=f"live-{scenario.__name__}-{runtime}-", dir=base)).resolve()
        self.root = self.tmp / "alice"
        self.failures, self.output, self.blocked = [], "", False

    def instance(self):
        sh(sys.executable, str(BRAIN), "init", str(self.root), "--operator", "alice", "--title", "Alice",
           "--runtimes", "claude,codex", "--tz", "UTC", cwd=self.tmp, env=brains_config(self.tmp))
        brain(self.root, "new", "a", "--title", "Atlas", "--runtime", self.runtime)
        return self.root

    def home_repo(self, name, files):
        h = self.tmp / name
        h.mkdir()
        sh("git", "init", "-q", "-b", "main", cwd=h)
        for k, v in files.items():
            (h / k).parent.mkdir(parents=True, exist_ok=True)
            (h / k).write_text(v)
        sh("git", "add", "-A", cwd=h)
        sh("git", "commit", "-qm", "init", cwd=h)
        return h

    def agent(self, cwd, prompt, writable=None, tools=True, user_instructions=None):
        """Run the runtime non-interactively in `cwd`. `writable`: extra folder it may write to.
        `user_instructions`: stands in for the user-wide instruction file (claude: instead of the real one;
        codex: its throwaway config folder already holds whatever was linked there)."""
        if self.runtime == "claude":
            # the prompt goes first: --add-dir takes several values and would swallow it
            cmd = [runtime_bin("claude"), "-p", prompt, "--model", os.environ.get("LIVE_CLAUDE_MODEL", "claude-sonnet-5-5")]
            if tools:
                cmd += ["--dangerously-skip-permissions"]  # throwaway folders only
                if writable:
                    cmd += ["--add-dir", str(writable)]
            if user_instructions is not None:
                cmd += ["--setting-sources", "project,local", "--append-system-prompt", user_instructions]
            env = {}
        else:
            codex_home = self.codex_home(cwd)
            cmd = [runtime_bin("codex"), "exec", "--skip-git-repo-check"]
            # full access, as a host thread has: workspace-write keeps .git read-only, so no commits
            cmd += ["--dangerously-bypass-approvals-and-sandbox"] if tools else ["-s", "read-only"]
            cmd.append(prompt)
            env = {"CODEX_HOME": str(codex_home)}
        # `brain` on PATH is the core under test, and its list of brains is this run's
        bin_ = self.tmp / "bin"
        if not bin_.exists():
            bin_.mkdir()
            (bin_ / "brain").symlink_to(BRAIN)
        env = {**env, **brains_config(self.tmp), "PATH": f"{bin_}:{runtime_path(self.runtime)}"}
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=600,
                           env={**os.environ, **GIT_ENV, **env}, stdin=subprocess.DEVNULL)
        self.output = (r.stdout or "") + ("\n[stderr]\n" + r.stderr[-3000:] if r.returncode else "")
        if r.returncode:
            raise RuntimeUnavailable(f"{self.runtime} exited {r.returncode}: {self.output.strip()[-500:]}")
        return r.stdout or ""

    def codex_home(self, cwd=None):
        """A throwaway Codex config: the operator's login, this run's folders trusted."""
        codex_home = self.tmp / "codex-home"
        if not codex_home.exists():
                codex_home.mkdir()
                real = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
                for f in ("auth.json", "config.toml"):
                    if (real / f).exists():
                        shutil.copy(real / f, codex_home / f)
                cfg = codex_home / "config.toml"
                text = cfg.read_text() if cfg.exists() else ""
                text = re.sub(r"(?m)^model_reasoning_effort\s*=.*\n?", "", text)
                trust = "".join(f'\n[projects."{p}"]\ntrust_level = "trusted"\n'
                                for p in {str(self.tmp), str(self.root), str(cwd or self.root)})
                effort = os.environ.get("LIVE_CODEX_EFFORT", "medium")
                cfg.write_text(f'model_reasoning_effort = "{effort}"\n' + text + trust)
        return codex_home

    def approve_hooks(self):
        if self.runtime == "codex":
            sh(sys.executable, str(BRAIN), "hook", "approve", cwd=self.root,
               env={"CODEX_HOME": str(self.codex_home(self.root / "nodes" / "a")),
                    "PATH": runtime_path("codex"), **brains_config(self.tmp)})

    def check(self, cond, msg):
        if not cond:
            self.failures.append(msg)

    def check_time(self, text, what):
        """Axiom 17: the instance's timezone (UTC here), not the machine's clock."""
        m = re.search(TS, text)
        now = brain(self.root, "now").strip()[:16]  # "YYYY-MM-DD HH:MM", then its offset
        if not m:
            return self.failures.append(f"{what}: no timestamp")
        fmt = "%Y-%m-%d %H:%M"
        import datetime as dt
        gap = abs((dt.datetime.strptime(m.group(0), fmt) - dt.datetime.strptime(now, fmt)).total_seconds())
        self.check(gap <= 15 * 60, f"{what}: {m.group(0)} isn't the instance's time ({now})")

    def brain_check(self):
        r = subprocess.run([sys.executable, str(BRAIN), "check"], cwd=self.root, capture_output=True, text=True,
                           env={**os.environ, **GIT_ENV, **brains_config(self.tmp)})
        self.check(r.returncode == 0, f"brain check fails: {(r.stdout + r.stderr).strip()[-400:]}")


TS = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}"
TS_ANY = TS + r"(:\d{2})?( ?(?:[A-Z]{2,5}|Z|[+-]\d{2}:?\d{2}))?"  # seconds and a zone are fine too (axiom 17)


# ---------- scenarios ----------

def parity(run):
    """Axiom 22: the same instruction chain, role and skills on every runtime."""
    run.instance()
    out = run.agent(run.root / "nodes" / "a", (
        "Without running any tools, answer from the instructions and skills already in your context. Reply with "
        "one line of JSON and nothing else: {\"node\": <the node id your node folder's instructions name>, "
        "\"role\": <your role when your brief names none>, \"skills\": [<the exact name of every skill available "
        "to you from this folder's instructions or skill directory, sorted>]}"), tools=False)
    m = re.search(r"\{.*\}", out, re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except ValueError:
        data = {}
    run.check(data.get("node") == "a", f"node: {data.get('node')!r}")
    run.check(str(data.get("role", "")).lower() == "worker", f"role: {data.get('role')!r}")
    seen = sorted(s for s in data.get("skills", []) if s in SKILLS)
    run.check(seen == SKILLS, f"skills missing: {sorted(set(SKILLS) - set(seen))}")


def coordinator_writes_as_it_goes(run):
    """A2/A3, axioms 16-17: a decision and a blocker land in the node's files, timestamped and committed."""
    root = run.instance()
    a = root / "nodes" / "a"
    st0 = a / "status.md"
    lines = st0.read_text().splitlines()
    lines[0] = "Updated: 2020-01-01 00:00 by a coordinator (claude)"
    st0.write_text("\n".join(lines) + "\n")
    sh("git", "commit", "-qam", "a: old status", cwd=root)
    before = lines[0]
    run.agent(a, (
        "Role: coordinator of node `a`. The operator says: \"We decided to use Postgres instead of SQLite, "
        "because we need concurrent writes. Also, I'm still waiting on a price quote from the hosting company "
        "before we can launch.\" Record this the way your role requires, then reply in one line."), writable=root)
    dec = (a / "decisions.md").read_text()
    run.check(re.search(rf"## {TS_ANY}: .*Postgres", dec), "decisions.md has no timestamped Postgres entry")
    run.check("Why:" in dec.split("Postgres", 1)[-1][:800], "the decision has no Why:")
    st = (a / "status.md").read_text()
    first = st.splitlines()[0]
    run.check(first != before and re.match(rf"Updated: {TS_ANY} by a coordinator", first), f"Updated line: {first!r}")
    run.check_time(first, "Updated line")
    m = re.search(rf"## ({TS_ANY}): .*Postgres", dec)
    if m:
        run.check_time(m.group(1), "decision")
    blocked = st.split("## Blocked", 1)[-1].split("\n## ", 1)[0].lower()
    run.check("quote" in blocked, "the quote isn't under Blocked / needs the operator")
    dirty = sh("git", "status", "--porcelain", "--", "nodes/a", cwd=root).strip()
    run.check(not dirty, f"not committed: {dirty}")
    run.check("quote" in brain(root).lower(), "the dashboard doesn't show the blocker")
    run.brain_check()


def worker_asks_the_writer(run):
    """Axiom 15: a worker doesn't edit coordinator files; it appends to the inbox."""
    root = run.instance()
    a = root / "nodes" / "a"
    keep = {f: (a / f).read_text() for f in ("status.md", "decisions.md", "brief.md")}
    run.agent(a, "Let the coordinator of this node know that the printer's invoice is overdue. Then reply in one line.",
              writable=root)
    inbox = (a / "inbox.md").read_text()
    run.check(re.search(r"^- .*invoice", inbox, re.M | re.I), "no inbox line about the invoice")
    for f, text in keep.items():
        run.check((a / f).read_text() == text, f"worker edited {f}")


def learning_is_a_citation(run):
    """G3, axiom 10: a learned item records provenance and starts scoped to the source's owners."""
    root = run.instance()
    run.agent(root / "nodes" / "a", (
        "Role: coordinator of node `a`. Save this as a reusable method in the library, learned from this node, "
        "with brief.md as the reference: \"Test an auth guard both ways: one request that must pass and one "
        "that must be refused.\" Then reply in one line."), writable=root)
    items = [p for p in root.rglob("library/*.md") if "core" not in p.relative_to(root).parts]
    run.check(items, "no library item")
    for p in items:
        t = p.read_text()
        run.check("source_node: a" in t, f"{p.name}: source_node isn't a")
        run.check(re.search(r"scope: alice\b", t), f"{p.name}: scope isn't alice")
        run.check(re.search(rf"learned_at: {TS}", t), f"{p.name}: no learned_at timestamp")
    run.brain_check()


def new_node_in_one_sentence(run):
    """B1: a new node from one sentence, through the tool, with its brief written."""
    root = run.instance()
    run.agent(root, (
        "Role: coordinator of the root node. The operator says: \"Start a new project, garden: growing "
        "tomatoes on the balcony this summer.\" There is no host in this test: skip host registration and "
        "don't start a coordinator. Then reply in one line."))
    g = root / "nodes" / "garden"
    run.check((g / "node.md").exists(), "nodes/garden/node.md missing")
    if g.exists():
        run.check("tomato" in (g / "brief.md").read_text().lower(), "brief.md doesn't mention tomatoes")
    run.brain_check()


def no_upward_traces(run):
    """Axiom 6, scoreboard 12: a worker in a sealed home leaves nothing that names the instance."""
    root = run.instance()
    brain(root, "principal", "add", "team")
    home = run.home_repo("conv", {"conv.py": "def convert(amount, rate):\n    return amount * rate\n"})
    brain(root, "new", "conv", "--title", "Converter", "--owners", "team", "--home", str(home))
    run.agent(home, (
        f"Role: worker. Background, for you only: the operator tracks this project in their notes at "
        f"{root}/nodes/conv (don't go there). Task: write a README.md for this project: a small Python "
        f"currency converter. Commit it. Then reply in one line."))
    run.check((home / "README.md").exists(), "no README.md")
    hits = sh("git", "grep", "-I", "-i", "-n", "-e", "brain", "-e", str(run.tmp), "-e", "alice", "HEAD",
              cwd=home, check=False)
    run.check(not hits.strip(), f"traces in the home: {hits.strip()[:300]}")
    run.brain_check()


def second_runtime_review(run):
    """C3: a reviewer on this runtime finds a planted bug in a change built elsewhere, and changes nothing."""
    root = run.instance()
    brain(root, "principal", "add", "team")
    home = run.home_repo("app", {"auth.py": "def allowed(user):\n    return user.get('role') == 'admin'\n"})
    brain(root, "new", "app", "--title", "App", "--owners", "team", "--home", str(home))
    brain(root, "wt", "add", "app", "relax-auth", "--base", "main")
    wt = run.tmp / "app-work" / "worktrees" / "relax-auth"
    (wt / "auth.py").write_text("def allowed(user):\n    # admins and editors\n    return user.get('role') == 'admin' or True\n")
    sh("git", "commit", "-qam", "Let editors in", cwd=wt)
    head = sh("git", "rev-parse", "HEAD", cwd=wt)
    other = "codex" if run.runtime == "claude" else "claude"
    brief = brain(root, "review", "app", "--branch", "relax-auth", "--built-by", other)
    run.check(f"Reviewer runtime: {run.runtime}" in brief, "brain review picked the wrong runtime")
    out = run.agent(wt, brief)
    low = out.lower()
    run.check("auth.py" in low and ("or true" in low or "always" in low), "the always-true check wasn't flagged")
    run.check(re.search(r"blocker|critical|major|high", low), "not rated as serious")
    run.check(sh("git", "rev-parse", "HEAD", cwd=wt) == head and not sh("git", "status", "--porcelain", cwd=wt).strip(),
              "the reviewer changed the worktree")


def stop_hook_fires(run):
    """The stop hook runs in this runtime: notes left behind make the turn go on (axioms 15-17)."""
    root = run.instance()
    run.approve_hooks()
    a = root / "nodes" / "a"
    time.sleep(1.1)
    (a / "brief.md").write_text((a / "brief.md").read_text() + "\nAudience: teenagers.\n")
    run.agent(a, "Reply with just: OK", writable=root)
    log = root / ".brain" / "hooks.log"
    run.check(log.exists() and "stop a " in log.read_text(), "the stop hook didn't run")


def notes_current_after_an_edit(run):
    """A3: a coordinator that changes the node leaves status.md current and everything committed."""
    root = run.instance()
    run.approve_hooks()
    a = root / "nodes" / "a"
    st = a / "status.md"
    lines = st.read_text().splitlines()
    lines[0] = "Updated: 2020-01-01 00:00 by a coordinator (claude)"
    st.write_text("\n".join(lines) + "\n")
    sh("git", "commit", "-qam", "a: old status", cwd=root)
    run.agent(a, "Role: coordinator of node `a`. Add this line at the end of brief.md: \"Audience: teenagers.\" "
                 "That's the task. Reply with one word.", writable=root)
    run.check("teenagers" in (a / "brief.md").read_text(), "brief.md not edited")
    first = st.read_text().splitlines()[0]
    run.check(not first.startswith("Updated: 2020"), f"status.md not updated: {first!r}")
    run.check_time(first, "Updated line")
    dirty = sh("git", "status", "--porcelain", "--", "nodes/a", cwd=root).strip()
    run.check(not dirty, f"not committed: {dirty}")


def blocked(node_dir, item):
    st = node_dir / "status.md"
    st.write_text(st.read_text().replace("## Blocked / needs the operator\n",
                                         f"## Blocked / needs the operator\n\n- {item}\n"))


def a_session_reaches_its_own_brain(run):
    """Two brains on one machine (personal, the default; and work). A session set up for work (the work
    pointer in its runtime's config folder) answers from the work brain and never shows the other."""
    me = run.instance()
    work = run.tmp / "work"
    sh(sys.executable, str(BRAIN), "init", str(work), "--operator", "alice", "--title", "Work", "--name", "work",
       "--runtimes", "claude,codex", "--tz", "UTC", cwd=run.tmp, env=brains_config(run.tmp))
    brain(work, "new", "orion", "--title", "Orion launch")
    blocked(me / "nodes" / "a", "Book the dentist for Tuesday")
    blocked(work / "nodes" / "orion", "Legal hasn't signed off the Orion contract")
    for root in (me, work):
        sh("git", "commit", "-qam", "blocked", cwd=root)
    elsewhere = run.tmp / "elsewhere"
    elsewhere.mkdir()
    brain(me, "link", "--global", f"claude={run.tmp / 'claude-me'}", f"codex={run.tmp / 'codex-me'}")
    brain(work, "link", "--global", f"claude={run.tmp / 'claude-work'}",
          f"codex={run.codex_home(elsewhere) if run.runtime == 'codex' else run.tmp / 'codex-work'}")
    pointer = (run.tmp / "claude-work" / "CLAUDE.md").read_text()
    out = run.agent(elsewhere, "What's blocked and waiting on me? One line, from the Brain.",
                    user_instructions=pointer if run.runtime == "claude" else None)
    low = out.lower()
    run.check("legal" in low or "orion" in low, "didn't answer from the work brain")
    run.check("dentist" not in low, "showed the personal brain")



def worker_hands_back_a_question(run):
    """A worker started by a coordinator never asks the operator: a question it can't settle goes back to the
    coordinator (the inbox here, as there is no host to send to), and it doesn't guess."""
    root = run.instance()
    home = run.home_repo("app", {"config.json": '{"timeout": 30}\n'})
    brain(root, "set", "a", f"home={home}", "coordinator=thr-a")
    brief = brain(root, "brief", "a", "--task", (
        "Set the request timeout in config.json to the value the operator chose for production. That value "
        "isn't written down anywhere you can see. Commit on main when done."))
    out = run.agent(home, brief, writable=root)
    run.check(json.loads((home / "config.json").read_text()) == {"timeout": 30}, "the worker guessed a value")
    inbox = (root / "nodes" / "a" / "inbox.md").read_text()
    run.check(re.search(r"^- .*question.*timeout|^- .*timeout.*\?", inbox, re.M | re.I),
              "no question about the timeout in the inbox")
    run.check("timeout" in out.lower() and "?" in out, "the turn doesn't end with the question")


def lessons_become_items(run):
    """Closing a job: a reusable lesson from the report becomes a library item; one that only fits this node
    stays in its files."""
    root = run.instance()
    a = root / "nodes" / "a"
    st = a / "status.md"
    st.write_text(st.read_text().replace("| --- | --- | --- | --- |\n", "| --- | --- | --- | --- |\n| job/deps | thr-x | - | in review |\n", 1))
    d = Path(brain(root, "report", "a", "job deps").strip())
    (d / "index.md").write_text(
        "# job deps\n\n## Answer\n\nDependencies pinned; install scripts allowed only for esbuild.\n\n"
        "## Lessons\n\n- Bun treats an empty `trustedDependencies` list as unset, so every package's install "
        "scripts run; list the allowed packages explicitly.\n- This node's staging server is staging.atlas.test.\n")
    sh("git", "add", "-A", cwd=root)
    sh("git", "commit", "-qm", "job deps", cwd=root)
    run.agent(a, (
        f"Role: coordinator of node `a`. job/deps is merged; its worker's report is at {d / 'index.md'}. "
        f"Update your notes, then reply in one line."), writable=root)
    items = [p.read_text() for p in root.rglob("library/*.md") if "core" not in p.relative_to(root).parts]
    hit = [t for t in items if "trusteddependencies" in t.lower()]
    run.check(hit, "the reusable lesson isn't a library item")
    for t in hit:
        run.check("source_node: a" in t, "the item doesn't cite node a")
    run.check(not any("staging.atlas.test" in t for t in items), "a node-only fact became a library item")
    run.check(re.search(r"job/deps.*merged", st.read_text()), "the Active work row isn't marked merged")
    run.brain_check()


def limits_carry_their_reason(run):
    """A limit is written with why it exists and when it stops applying, so it doesn't outlive its cause."""
    root = run.instance()
    a = root / "nodes" / "a"
    run.agent(a, (
        "Role: coordinator of node `a`. The operator says: \"Don't run more than 2 worktrees at once for now, "
        "the disk is almost full.\" Record this the way your role requires, then reply in one line."), writable=root)
    brief = (a / "brief.md").read_text()
    lims = brief.split("## Limits", 1)[-1].split("\n## ", 1)[0] if "## Limits" in brief else ""
    lim = next((l for l in lims.splitlines() if l.startswith("- ") and "2" in l), "")
    run.check(lim, "no limit of 2 worktrees under Limits in brief.md")
    run.check(re.search(r"why:", lim, re.I) and re.search(r"lift when:", lim, re.I),
              f"the limit has no Why: or Lift when: ({lim!r})")
    run.check("disk" in lim.lower(), "the reason (the disk) isn't recorded")
    run.brain_check()


def freshness_across_timezones(run):
    """#10, one clock: a child's status written 90 minutes ago in Beirut time (its digits look later than the
    thread's UTC time from 30 minutes ago) is behind that thread. Every clue agrees: the status line, its
    commit date, its change log. The coordinator must not call it current."""
    import datetime as dt
    from zoneinfo import ZoneInfo
    root = run.instance()
    inst = root / "instance.md"
    inst.write_text(inst.read_text().replace("timezone: UTC", "timezone: Asia/Beirut"))
    now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
    wrote = (now - dt.timedelta(minutes=90)).astimezone(ZoneInfo("Asia/Beirut"))
    thread = now - dt.timedelta(minutes=30)
    st = root / "nodes" / "a" / "status.md"
    text = st.read_text()
    lines = text.splitlines()
    off = "" if os.environ.get("LIVE_LEGACY_TIME") else " +03:00"  # the control: times as older cores wrote them
    lines[0] = f"Updated: {wrote:%Y-%m-%d %H:%M}{off} by a coordinator (claude)"  # as `brain now` writes it
    body = "\n".join(lines) + "\n"
    body = re.sub(r"(## Recent changes[^\n]*\n)(?:\n?- [^\n]*\n)*", rf"\1\n- {wrote:%Y-%m-%d %H:%M}{off}: started\n", body)
    st.write_text(body)
    rs = root / "status.md"  # the root's own notes are current, so its stop hook stays quiet
    rl = rs.read_text().splitlines()
    rl[0] = f"Updated: {brain(root, 'now').strip()} by root coordinator (claude)"
    rs.write_text("\n".join(rl) + "\n")
    sh("git", "add", "-A", cwd=root)
    when = wrote.isoformat()
    sh("git", "commit", "-qm", "a: status", cwd=root, env={"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when})
    out = run.agent(root, (
        f"Role: coordinator of node `root`. Freshness check for child `a`: the host says its coordinator thread "
        f"was last active at {thread:%Y-%m-%dT%H:%M:%S}Z. Is a's status.md current or behind? Answer in one line, "
        "starting with the word current or behind."), writable=root)
    first = out.strip().lower()
    run.check(first.startswith("behind") or "behind" in first[:200], f"called it current: {out.strip()[:200]!r}")

def nine_job_close_lessons(run):
    """A coordinator closes a real merged job and records learned item ids in its ledger."""
    root = run.instance()
    home = run.home_repo("app", {"app.py": "print('hello')\n"})
    brain(root, "set", "a", f"home={home}")
    brain(root, "wt", "add", "a", "job/deps")
    wt = run.tmp / "app-work/worktrees/job-deps"
    sh("git", "commit", "--allow-empty", "-qm", "pin dependencies", cwd=wt)
    sh("git", "merge", "-q", "job/deps", cwd=home)
    report = Path(brain(root, "report", "a", "deps").strip()) / "index.md"
    report.write_text("# Done\n\nDependencies pinned.\n\n## Lessons\n\n"
                      "- Bun treats empty trustedDependencies as unset; explicitly list allowed install scripts.\n")
    run.agent(root / "nodes/a", f"Role: coordinator of node `a`. job/deps is merged. Close the job using "
              f"its report at {report}, update your notes and clean up the worktree. Reply in one line.", writable=root)
    ledger = (root / "nodes/a/job-ledger.md").read_text()
    items = [p for p in root.rglob("library/*.md") if "core" not in p.relative_to(root).parts]
    ids = [re.search(r"(?m)^id: (.+)$", p.read_text()).group(1) for p in items
           if "trusteddependencies" in p.read_text().lower()]
    run.check(ids, "reusable lesson not filed via learn")
    row = next((line for line in ledger.splitlines() if line.startswith("| job/deps |")), "")
    run.check("merged" in row, "job not recorded merged")
    run.check(any(f"| {i} |" in row for i in ids), "ledger has no learned item id")
    run.check(not wt.exists(), "merged worktree not pruned")
    run.brain_check()


def _nine_worker_resume(run, committed):
    root = run.instance()
    home = run.home_repo("app", {
        "calc.py": "def add(a, b):\n    raise NotImplementedError\n\ndef multiply(a, b):\n    raise NotImplementedError\n",
        "test_calc.py": "from calc import add, multiply\nassert add(2, 3) == 5\nassert multiply(2, 3) == 6\n"})
    brain(root, "set", "a", f"home={home}", "verify=python3 -B test_calc.py")
    brain(root, "wt", "add", "a", "job/calc")
    main_before = sh("git", "rev-parse", "main", cwd=home)
    wt = run.tmp / "app-work/worktrees/job-calc"
    partial = "def add(a, b):\n    # The first worker completed addition.\n    return a + b\n"
    (wt / "calc.py").write_text(partial + "\ndef multiply(a, b):\n    raise NotImplementedError\n")
    if committed:
        sh("git", "commit", "-qam", "addition complete; multiplication remains", cwd=wt)
    prior = sh("git", "rev-parse", "HEAD", cwd=wt).strip()
    # A completed review is fixture state; this scenario tests resumption, not reviewer quality.
    brain(root, "review", "a", "--branch", "job/calc", "--built-by", "codex" if run.runtime == "claude" else "claude")
    brain(root, "jobs", "a", "--branch", "job/calc", "--switch", f"other -> {run.runtime}: usage limit")
    report = Path(brain(root, "report", "a", "calc").strip()) / "index.md"
    task = (f"Finish calc.py so test_calc.py passes. Addition was completed before the previous runtime "
            f"hit its usage limit; multiplication remains. One review round is recorded and no findings "
            f"remain open. No additional review is needed in this exercise. Commit on the job branch. "
            f"Write your report to {report}. There is no host in this exercise: your final response is "
            f"the hand-back; do not try to send a host message.")
    brief = brain(root, "brief", "a", "--branch", "job/calc", "--runtime", run.runtime, "--task", task)
    coordinator = {name: (root / "nodes/a" / name).read_text()
                   for name in ("status.md", "decisions.md", "brief.md", "node.md", "job-ledger.md")}
    run.agent(wt, brief, writable=root)
    ancestors = sh("git", "rev-list", "HEAD", cwd=wt).splitlines()
    run.check(prior in ancestors, "replacement reset the partial branch")
    run.check(sh("git", "rev-parse", "HEAD", cwd=wt).strip() != prior, "no finishing commit")
    run.check(sh("git", "rev-parse", "main", cwd=home) == main_before, "worker changed main")
    run.check((wt / "calc.py").read_text().startswith(partial), "completed addition was rewritten")
    verify = subprocess.run([sys.executable, "-B", "test_calc.py"], cwd=wt, capture_output=True)
    run.check(verify.returncode == 0, "replacement did not finish multiplication")
    ready = brain(root, "ready", "a", "--branch", "job/calc", check=False).strip()
    run.check(ready == "ready", f"no valid ready packet: {ready}")
    text = report.read_text()
    lessons = re.search(r"(?ims)^## Lessons\s*\n(.*?)(?=^## |\Z)", text)
    run.check(lessons and lessons.group(1).strip() and "<!--" not in lessons.group(1), "Lessons section left empty")
    if lessons and re.search(r"\bnone\b", lessons.group(1), re.I):
        run.check(bool(re.search(r"none\s*:\s*\S", lessons.group(1), re.I)), "none has no reason")
    for name, before in coordinator.items():
        run.check((root / "nodes/a" / name).read_text() == before, f"worker edited coordinator's {name}")


def nine_worker_resume_ready(run):
    """A replacement runtime continues partial commits, writes Lessons and leaves a current ready packet."""
    _nine_worker_resume(run, committed=True)


def nine_worker_resume_uncommitted(run):
    """A runtime switch before the first commit preserves and finishes uncommitted work."""
    _nine_worker_resume(run, committed=False)


def nine_future_updated_repaired(run):
    """A future Updated value makes the stop hook ask the coordinator to repair it with brain now."""
    root = run.instance()
    run.approve_hooks()
    node = root / "nodes/a"
    st = node / "status.md"
    lines = st.read_text().splitlines()
    lines[0] = "Updated: 2999-01-01 12:00 +00:00 by a coordinator (claude)"
    st.write_text("\n".join(lines) + "\n")
    sh("git", "commit", "-qam", "a: incorrect clock", cwd=root)
    run.agent(node, "Role: coordinator of node `a`. Check your notes, then reply with one word.", writable=root)
    run.check_time(st.read_text().splitlines()[0], "repaired Updated line")
    log = root / ".brain/hooks.log"
    run.check(log.exists() and "future Updated:" in log.read_text(), "hook did not flag future Updated")
    run.check(not sh("git", "status", "--porcelain", "--", "nodes/a", cwd=root).strip(), "repair not committed")
    run.brain_check()


def usage_routes_before_launch(run):
    """A coordinator keeps work off exhausted and other-brain accounts; evidence stays in files."""
    root = run.instance()
    # A synthetic read-only app-server makes refreshes deterministic even if the agent takes >2 min.
    bin_ = run.tmp / "bin"
    bin_.mkdir(exist_ok=True)
    (bin_ / "brain").symlink_to(BRAIN)
    stub = bin_ / "codex"
    stub.write_text("#!" + sys.executable + "\n" + r'''import json,os,sys,time
from pathlib import Path
for line in sys.stdin:
    msg=json.loads(line)
    if 'id' not in msg: continue
    cfg=Path(os.environ['CODEX_HOME'])
    if msg['method']=='initialize': result={}
    elif msg['method']=='account/read': result={'account':{'accountId':cfg.name}}
    elif msg['method']=='account/rateLimits/read':
        used=95 if cfg.name=='full' else 0 if cfg.name=='work' else 30
        result={'rateLimits':{'primary':{'usedPercent':used,'windowDurationMins':10080,
                                       'resetsAt':int(time.time()+3*86400)},'secondary':None}}
    else: sys.exit(3)
    print(json.dumps({'id':msg['id'],'result':result}),flush=True)
''')
    stub.chmod(0o755)
    subs = []
    for name in ("full", "work", "room"):
        cfg = run.tmp / name
        cfg.mkdir()
        subs.append({"id": name, "runtime": "codex", "config_dir": str(cfg),
                     "host_provider_instance_id": "provider-" + name,
                     "brains": ["work-only"] if name == "work" else ["alice"]})
    (run.tmp / "subscriptions.json").write_text(json.dumps({"subscriptions": subs}))
    # Seed only through the command under test, with the stub on PATH.
    sh(sys.executable, str(BRAIN), "usage", "--refresh", cwd=root,
       env={**brains_config(run.tmp), "PATH": f"{bin_}:{os.environ.get('PATH', '')}"})
    run.agent(root / "nodes/a", (
        "Role: coordinator of node `a`. Prepare a worker to audit this node's brief for clarity. "
        "The host's launch tools are unavailable in this test, so prepare the launch without starting a thread. "
        "Write launch-plan.json in your node with runtime, subscription_id, provider_instance_id, and reason. "
        "Save the machine eligibility evidence you used as usage-before-launch.json in your node, and the "
        "worker's assembled brief as worker-brief.md. Follow your coordinator rules and commit your files. "
        "Reply in one line."), writable=root)
    node = root / "nodes/a"
    try:
        plan = json.loads((node / "launch-plan.json").read_text())
        usage = json.loads((node / "usage-before-launch.json").read_text())
    except (OSError, ValueError) as e:
        run.check(False, f"launch evidence missing or invalid: {e}")
        return
    run.check(plan.get("subscription_id") == "room", f"wrong subscription: {plan}")
    run.check(plan.get("provider_instance_id") == "provider-room", f"wrong host instance: {plan}")
    run.check(plan.get("runtime") == "codex", f"wrong worker runtime: {plan}")
    run.check(usage.get("scope") == "account-wide", "no account-wide brain usage evidence")
    rows = {s.get("id"): s for s in usage.get("subscriptions", [])}
    run.check(rows.get("full", {}).get("windows", {}).get("seven_day", {}).get("used_percent") == 95,
              "exhausted account not checked")
    run.check(rows.get("work", {}).get("brains") == ["work-only"], "other brain's account not checked")
    brief = (node / "worker-brief.md").read_text() if (node / "worker-brief.md").exists() else ""
    run.check("provider-room" in brief, "worker brief lacks the chosen provider")
    run.check("provider-full" not in brief and "provider-work" not in brief, "worker brief chose ineligible provider")
    run.brain_check()


SCENARIOS = [parity, coordinator_writes_as_it_goes, worker_asks_the_writer, learning_is_a_citation,
             new_node_in_one_sentence, no_upward_traces, second_runtime_review, stop_hook_fires,
             notes_current_after_an_edit, a_session_reaches_its_own_brain, worker_hands_back_a_question,
             lessons_become_items, limits_carry_their_reason, freshness_across_timezones,
             nine_job_close_lessons, nine_worker_resume_ready, nine_worker_resume_uncommitted,
             nine_future_updated_repaired, usage_routes_before_launch]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--runtimes", default="claude,codex")
    ap.add_argument("-k", help="only scenarios whose name contains this")
    ap.add_argument("--keep", action="store_true", help="keep the throwaway folders")
    ap.add_argument("--jobs", type=int, default=4)
    a = ap.parse_args()
    runtimes = [r for r in a.runtimes.split(",") if r]
    if not runtimes or any(rt not in ("claude", "codex") for rt in runtimes):
        ap.error("--runtimes must name claude, codex, or both")
    for rt in runtimes:
        try:
            executable = runtime_bin(rt)
            version = sh(executable, "--version").strip()
        except RuntimeError as e:
            sys.exit(str(e))
        print(f"runtime {rt}: {executable} ({version})", flush=True)
    base = Path(tempfile.mkdtemp(prefix="brain-live-")).resolve()
    jobs = [(s, rt) for s in SCENARIOS if not a.k or a.k in s.__name__ for rt in runtimes]
    results = {}
    unavailable, runtime_lock = {}, threading.Lock()

    def go(job):
        s, rt = job
        run = Run(s, rt, base)
        t0 = time.time()
        with runtime_lock:
            reason = unavailable.get(rt)
        if reason:
            run.blocked = True
            run.failures.append(f"not started: {rt} unavailable earlier in this run")
            return job, run, time.time() - t0
        try:
            s(run)
        except RuntimeUnavailable as e:
            run.blocked = True
            run.failures.append(str(e))
            with runtime_lock:
                unavailable[rt] = str(e)
        except Exception as e:  # a crash is a failure, not an abort
            run.failures.append(f"crashed: {e}")
        return job, run, time.time() - t0

    with cf.ThreadPoolExecutor(a.jobs) as ex:
        for (s, rt), run, secs in ex.map(go, jobs):
            results[(s.__name__, rt)] = run
            mark = "!" if run.blocked else "✓" if not run.failures else "✗"
            print(f"{mark} {s.__name__:<32} {rt:<7} {secs:5.0f}s", flush=True)
            for f in run.failures:
                print(f"    - {f}")
            if run.failures and run.output:
                print("    agent said: " + run.output.strip().replace("\n", " ")[-500:])
    names = [s.__name__ for s in SCENARIOS if (s.__name__, runtimes[0]) in results]
    print("\n" + f"{'scenario':<32}" + "".join(f"{rt:<9}" for rt in runtimes))
    for n in names:
        print(f"{n:<32}" + "".join(f"{('BLOCKED' if results[(n, rt)].blocked else 'FAIL' if results[(n, rt)].failures else 'pass'):<9}" for rt in runtimes))
    failed = sum(1 for r in results.values() if r.failures)
    if a.keep or failed:
        print(f"\nfolders: {base}")
    else:
        shutil.rmtree(base, ignore_errors=True)
    # claude keeps a transcript folder per working folder; these were throwaway
    slug = re.sub(r"[^A-Za-z0-9]", "-", base.name)
    for d in (Path.home() / ".claude" / "projects").glob(f"*{slug}*"):
        shutil.rmtree(d, ignore_errors=True)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
