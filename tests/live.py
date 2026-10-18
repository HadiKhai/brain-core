#!/usr/bin/env python3
"""Live runtime tests (axiom 22): the same use cases, run by every runtime, judged by the files they leave.

Each scenario builds a throwaway instance, starts a real agent in it with a short brief, then checks the
result mechanically. Costs one model call per scenario per runtime.

    python3 core/tests/live.py [--runtimes claude,codex] [-k name] [--keep] [--jobs 4]

Environment: LIVE_CLAUDE_MODEL (default claude-sonnet-5-5), LIVE_CODEX_EFFORT (default medium).
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
        self.failures, self.output = [], ""

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
            cmd = ["claude", "-p", prompt, "--model", os.environ.get("LIVE_CLAUDE_MODEL", "claude-sonnet-5-5")]
            if tools:
                cmd += ["--dangerously-skip-permissions"]  # throwaway folders only
                if writable:
                    cmd += ["--add-dir", str(writable)]
            if user_instructions is not None:
                cmd += ["--setting-sources", "project,local", "--append-system-prompt", user_instructions]
            env = {}
        else:
            codex_home = self.codex_home(cwd)
            cmd = ["codex", "exec", "--skip-git-repo-check"]
            # full access, as a host thread has: workspace-write keeps .git read-only, so no commits
            cmd += ["--dangerously-bypass-approvals-and-sandbox"] if tools else ["-s", "read-only"]
            cmd.append(prompt)
            env = {"CODEX_HOME": str(codex_home)}
        # `brain` on PATH is the core under test, and its list of brains is this run's
        bin_ = self.tmp / "bin"
        if not bin_.exists():
            bin_.mkdir()
            (bin_ / "brain").symlink_to(BRAIN)
        env = {**env, **brains_config(self.tmp), "PATH": f"{bin_}:{os.environ.get('PATH', '')}"}
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=600,
                           env={**os.environ, **GIT_ENV, **env}, stdin=subprocess.DEVNULL)
        self.output = (r.stdout or "") + ("\n[stderr]\n" + r.stderr[-3000:] if r.returncode else "")
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
               env={"CODEX_HOME": str(self.codex_home(self.root / "nodes" / "a")), **brains_config(self.tmp)})

    def check(self, cond, msg):
        if not cond:
            self.failures.append(msg)

    def check_time(self, text, what):
        """Axiom 17: the instance's timezone (UTC here), not the machine's clock."""
        m = re.search(TS, text)
        now = brain(self.root, "now").strip()
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
TS_ANY = TS + r"(:\d{2})?( ?[A-Z]{2,5}|[+-]\d{2}:?\d{2})?"  # seconds and a zone are fine too (axiom 17)


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


SCENARIOS = [parity, coordinator_writes_as_it_goes, worker_asks_the_writer, learning_is_a_citation,
             new_node_in_one_sentence, no_upward_traces, second_runtime_review, stop_hook_fires,
             notes_current_after_an_edit, a_session_reaches_its_own_brain]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--runtimes", default="claude,codex")
    ap.add_argument("-k", help="only scenarios whose name contains this")
    ap.add_argument("--keep", action="store_true", help="keep the throwaway folders")
    ap.add_argument("--jobs", type=int, default=4)
    a = ap.parse_args()
    runtimes = [r for r in a.runtimes.split(",") if r]
    for rt in runtimes:
        if not shutil.which(rt):
            sys.exit(f"live: '{rt}' isn't installed")
    base = Path(tempfile.mkdtemp(prefix="brain-live-")).resolve()
    jobs = [(s, rt) for s in SCENARIOS if not a.k or a.k in s.__name__ for rt in runtimes]
    results = {}

    def go(job):
        s, rt = job
        run = Run(s, rt, base)
        t0 = time.time()
        try:
            s(run)
        except Exception as e:  # a crash is a failure, not an abort
            run.failures.append(f"crashed: {e}")
        return job, run, time.time() - t0

    with cf.ThreadPoolExecutor(a.jobs) as ex:
        for (s, rt), run, secs in ex.map(go, jobs):
            results[(s.__name__, rt)] = run
            mark = "✓" if not run.failures else "✗"
            print(f"{mark} {s.__name__:<32} {rt:<7} {secs:5.0f}s", flush=True)
            for f in run.failures:
                print(f"    - {f}")
            if run.failures and run.output:
                print("    agent said: " + run.output.strip().replace("\n", " ")[-500:])
    names = [s.__name__ for s in SCENARIOS if (s.__name__, runtimes[0]) in results]
    print("\n" + f"{'scenario':<32}" + "".join(f"{rt:<9}" for rt in runtimes))
    for n in names:
        print(f"{n:<32}" + "".join(f"{('pass' if not results[(n, rt)].failures else 'FAIL'):<9}" for rt in runtimes))
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
