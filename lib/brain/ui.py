"""Ui mechanics for Brain."""
import argparse
import contextlib
import json
import math
import os
import re
import shutil
import shlex
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

from . import check as _m_check
from . import common as _m_common
from . import guard as _m_guard
from . import instance as _m_instance
from . import jobs as _m_jobs
from . import knowledge as _m_knowledge
from . import machine as _m_machine
from . import nodes as _m_nodes
from . import rehearse as _m_rehearse
from . import schedules as _m_schedules
from . import spend as _m_spend
from . import status as _m_status
from . import usage as _m_usage
from . import ui_review as _m_ui_review
from . import ui_status as _m_ui_status
from . import ui_server as _m_ui_server
from . import verify as _m_verify
from . import worktrees as _m_worktrees


UI_PORT = 4747
UI_TELEMETRY_SECONDS = 30
UI_HOURS = 48  # the activity grid's span
LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")


def md_sections(text):
    """{heading: body} for the `## ` sections of a markdown file, in order."""
    out, cur = {}, None
    for line in text.splitlines():
        if line.startswith("## "):
            cur = line[3:].strip()
            out[cur] = []
        elif cur is not None:
            out[cur].append(line)
    return {k: "\n".join(v).strip() for k, v in out.items()}


def ui_decisions(inst, n, limit=6):
    f = n["dir"] / "decisions.md"
    if not f.exists():
        return []
    out, text = [], f.read_text()
    for m in re.finditer(r"^#{2,3} (\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?: ?(?:Z|[+-]\d{2}:?\d{2}))?): (.+)$", text, re.M):
        rest = text[m.end():].split("\n#", 1)[0]
        said = re.search(r"^Said: (.+)$", rest, re.M)
        d = inst.parse_time(m.group(1))
        out.append({"time": m.group(1), "t": int(d.timestamp()) if d else None, "title": m.group(2).strip(),
                    "said": said.group(1).strip() if said else ""})
    return sorted(out, key=lambda d: d["t"] or 0, reverse=True)[:limit]


def ui_listening(w):
    import socket
    found = []
    for port in range(w.get('port') or 0, (w.get('port') or 0) + w.get('ports', 1)) if w.get('port') else []:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
            connection.settimeout(0.05)
            if connection.connect_ex(('127.0.0.1', port)) == 0:
                found.append(port)
    return found


def ui_meters(inst, state, cache):
    """Clocks and socket liveness do not invalidate repository/readiness caches."""
    nodes = []
    now = inst.now_dt()
    for n in state['nodes']:
        worktrees = [{**w, 'listening': ui_listening(w) if w.get('live', True) else []} for w in n['worktrees']]
        jobs = {**n['jobs'], 'open': [{**r, 'age_h': round(_m_status.elapsed(inst.parse_time(r['Started']), now) / 3600, 1)}
                                    for r in n['jobs']['open']]}
        def age(row):
            since = inst.parse_time(row.get('since'))
            return {**row, 'age_hours': round(max(0, _m_status.elapsed(since, now) / 3600), 2)} if since else row
        refreshed = {**n, 'worktrees': worktrees, 'jobs': jobs, 'work': [age(r) for r in n['work']],
                     'blocked_metadata': [age(r) for r in n.get('blocked_metadata', [])],
                     'updated_ago': _m_status.ago(inst, inst.parse_time(n['updated']))}
        nodes.append(refreshed)
        if n['id'] in cache.nodes:
            key, _, commits = cache.nodes[n['id']]
            cache.nodes[n['id']] = (key, refreshed, commits)
        for w in worktrees:
            job = (n['id'], w['branch'])
            if job in cache.worktrees:
                key, old = cache.worktrees[job]
                cache.worktrees[job] = (key, {**old, 'listening': w['listening']})
    return {**state, 'nodes': nodes, 'brain': {**state['brain'], 'now': now.isoformat(timespec='seconds')},
            'machine': _m_machine.machine_state(inst)}


def ui_worktree(inst, home, branch, w, n=None):
    path = Path(w["path"])
    d = {"branch": branch, "path": _m_common.tilde(path), "port": w.get("port"), "ports": w.get("ports", 1),
         "exists": path.exists(), "listening": ui_listening(w), "dirty": None, "ahead": None, "last": None, "wrote": None}
    if d["exists"]:
        changed = _m_common.git("status", "--porcelain", cwd=path).stdout.splitlines()
        d["dirty"] = len(changed)
        times = []
        for line in changed[:200]:  # the newest uncommitted write: an agent at work right now
            try:
                times.append((path / line[3:].split(" -> ")[-1].strip('"')).stat().st_mtime)
            except OSError:
                pass
        d["wrote"] = int(max(times)) if times else None
        target = _m_worktrees.integration_target(n or {"home_path": home, "fm": {}})
        if target:
            c = _m_common.git("rev-list", "--count", f"{target}..refs/heads/{branch}", cwd=home).stdout.strip()
            d["ahead"] = int(c) if c.isdigit() else None
        t = _m_common.git("log", "-1", "--format=%ct", "refs/heads/" + branch, cwd=home).stdout.strip()
        d["last"] = int(t) if t.isdigit() else None
    return d


def ui_live_worktrees(n):
    """Repository inspection belongs to current work, including ledger repair rows."""
    rows = _m_jobs.read_jobs(n) or []
    branches = {row['Job'] for row in rows if not row.get('Ended')}
    closed = {row['Job'] for row in rows if row.get('Ended')} - branches
    branches.update(row[0].strip().strip('`') for row in _m_status.active_work(n)
                    if row and row[0].strip().strip('`') not in closed)
    return {branch: w for branch, w in n['local'].get('worktrees', {}).items() if branch in branches}


def cached_worktree(inst, home, branch, w, n, cache, live=None):
    if not (branch in ui_live_worktrees(n) if live is None else live):
        return {'branch': branch, 'path': _m_common.tilde(Path(w['path'])), 'port': w.get('port'),
                'ports': w.get('ports', 1), 'exists': Path(w['path']).exists(), 'live': False,
                'listening': [], 'dirty': None, 'ahead': None, 'last': None, 'wrote': None}
    if cache is None:
        return {**ui_worktree(inst, home, branch, w, n), 'live': True}
    job = (n['id'], branch)
    key = cache.worktree_keys.get(job)
    old = cache.worktrees.get(job)
    if old is None or old[0] != key:
        cache.worktrees[job] = (key, {**ui_worktree(inst, home, branch, w, n), 'live': True})
    return cache.worktrees[job][1]


def ui_commits(cwd, since, all_refs=False):
    # branches, remotes and tags only: hosts keep private refs (checkpoints) of their own
    args = ["log", f"--since={since}", "--format=%ct%x00%h%x00%s"] + \
        (["--branches", "--remotes", "--tags"] if all_refs else [])
    out = []
    for line in _m_common.git(*args, cwd=cwd).stdout.splitlines():
        parts = line.split("\x00")
        if len(parts) == 3 and parts[0].isdigit():
            out.append({"t": int(parts[0]), "sha": parts[1], "subject": parts[2]})
    return out



CPU_READING_CACHE = {}

def machine_utilization():
    """Bounded stdlib-only probes; iowait is not CPU busy time on Linux."""
    cached = CPU_READING_CACHE.get(sys.platform)
    if cached and 0 <= time.monotonic() - cached[0] < 5:
        return dict(cached[1])
    result = {}
    try:
        if sys.platform == 'darwin':
            top = subprocess.run(['top', '-l', '2', '-s', '1', '-n', '0'], capture_output=True,
                                 text=True, timeout=3, env={**os.environ, 'LC_ALL': 'C'})
            matches = re.findall(r'CPU usage:.*?([\d.]+)% idle', top.stdout)
            if top.returncode == 0 and len(matches) >= 2:
                result['cpu_idle_pct'] = float(matches[-1])
            pressure = subprocess.run(['memory_pressure', '-Q'], capture_output=True,
                                      text=True, timeout=1, env={**os.environ, 'LC_ALL': 'C'})
            match = re.search(r'System-wide memory free percentage:\s*(\d+)%', pressure.stdout)
            if pressure.returncode == 0 and match:
                result['memory_pressure_pct'] = 100 - float(match[1])
        elif sys.platform.startswith('linux'):
            def counters():
                values = list(map(int, Path('/proc/stat').read_text().splitlines()[0].split()[1:]))
                # guest time is already included in user/nice; iowait leaves CPUs idle.
                return sum(values[:8]), values[3] + (values[4] if len(values) > 4 else 0)
            first = counters()
            time.sleep(0.05)
            second = counters()
            total, idle = second[0] - first[0], second[1] - first[1]
            if total > 0 and 0 <= idle <= total:
                result['cpu_idle_pct'] = round(100 * idle / total, 1)
            try:
                psi = Path('/proc/pressure/memory').read_text()
                match = re.search(r'^some avg10=([\d.]+)', psi, re.M)
                if match:
                    result['memory_pressure_pct'] = float(match[1])
            except OSError:
                pass
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        pass
    # Bad or unavailable probes stay unknown, rather than becoming admission evidence.
    result = {k: v for k, v in result.items() if math.isfinite(v) and 0 <= v <= 100}
    CPU_READING_CACHE[sys.platform] = (time.monotonic(), dict(result))
    return result


def machine_load():
    d = {"cpus": os.cpu_count() or 1, "thermal": "unknown"}
    try:
        d["load"] = [round(x, 2) for x in os.getloadavg()]
    except OSError:
        pass
    try:
        d["load_per_cpu"] = [x / d["cpus"] for x in os.getloadavg()]
    except OSError:
        pass
    try:
        du = shutil.disk_usage(str(Path.home()))
        d["disk_free_gb"], d["disk_total_gb"] = round(du.free / 1e9), round(du.total / 1e9)
    except OSError:
        pass
    try:
        if sys.platform == "darwin":
            vm = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=0.2).stdout
            page = int(re.search(r"page size of (\d+)", vm).group(1))
            pages = {k: int(v) for k, v in re.findall(r"Pages (free|inactive):\s+(\d+)", vm)}
            total = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True,
                                       timeout=0.2).stdout)
            d["mem_free_gb"], d["mem_total_gb"] = round(sum(pages.values()) * page / 1e9, 1), round(total / 1e9)
        elif Path("/proc/meminfo").exists():
            mi = dict(re.findall(r"(\w+):\s+(\d+)", Path("/proc/meminfo").read_text()))
            d["mem_free_gb"], d["mem_total_gb"] = round(int(mi.get("MemAvailable", int(mi["MemFree"]) + int(mi["Inactive"]))) * 1024 / 1e9, 1), round(int(mi["MemTotal"]) * 1024 / 1e9)
    except (OSError, AttributeError, ValueError, KeyError, subprocess.SubprocessError):
        pass
    try:
        if sys.platform == "darwin":
            therm = subprocess.run(["pmset", "-g", "therm"], capture_output=True, text=True, timeout=0.2)
            limits = [int(v) for v in re.findall(r"(?:CPU_Speed_Limit|CPU_Scheduler_Limit)\s*=\s*(\d+)", therm.stdout)]
            levels = [int(v) for v in re.findall(r"(?:Thermal|Performance)_Warning_Level\s*=\s*(\d+)", therm.stdout)]
            if therm.returncode == 0 and (limits or levels or "No thermal warning" in therm.stdout):
                d["thermal"] = "pressure" if any(v < 100 for v in limits) or any(levels) else "normal"
        elif sys.platform.startswith("linux"):
            states = []
            for p in Path("/sys/class/thermal").glob("cooling_device*/cur_state"):
                kind = (p.parent / "type").read_text().strip().lower()
                if kind in ("processor", "intel_powerclamp", "cpufreq"):
                    states.append(int(p.read_text()))
            if states:
                d["thermal"] = "pressure" if any(states) else "normal"
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    if "mem_free_gb" in d:
        d["memory_pressure"] = "low-free-memory" if d["mem_free_gb"] < 8 else "normal"
    d.update(machine_utilization())
    return d


class UIInstance(_m_instance.Instance):
    """One read-only refresh shares parsed node/library records across its derived views."""
    def __init__(self, root):
        super().__init__(root)
        self._nodes, self._items = None, None

    def nodes(self):
        if self._nodes is None:
            self._nodes = super().nodes()
        return self._nodes

    def items(self):
        if self._items is None:
            self._items = super().items()
        return self._items


def ui_state(inst, problems=None, scan=None, spend_budget=2.0, machine_spend=None, cache=None, node_keys=None):
    readiness = _m_verify.display_cache(cache.ready, cache.worktree_keys) if cache is not None else contextlib.nullcontext()
    usage = _m_spend.live_usage_snapshot(cache.usage, cache.usage_revision) if cache is not None else contextlib.nullcontext()
    jobs = _m_status.local_job_snapshot(cache.local_jobs, cache.worktree_keys) if cache is not None else _m_status.local_job_snapshot()
    with _m_common.git_read_snapshot(), _m_spend.spend_read_only(), readiness, usage, jobs:
        return _ui_state(inst, problems, scan, spend_budget, machine_spend, cache, node_keys)


def ui_node(inst, n, since, cache=None, partial=False):
    """The expensive repository/readiness part, reusable independently for each node."""
    home_commits = []
    fm, loc = n["fm"], n["local"]
    st = n["dir"] / "status.md"
    secs = md_sections(st.read_text()) if st.exists() else {}
    u = _m_status.updated_at(inst, n)
    work = []
    work_metadata = _m_ui_status.ui_work_metadata(inst, n)
    for r in _m_status.active_work(n):
        m = LINK_RE.search(r[1] if len(r) > 1 else "")
        work.append({"branch": r[0], "thread": m.group(1) if m else (r[1] if len(r) > 1 else ""),
                     "link": m.group(2) if m else "", "runtime": (re.search(r"\((Claude|Codex|[A-Za-z]+)\)\s*$",
                     r[1]) or [None, ""])[1] if len(r) > 1 else "", "pr": r[2] if len(r) > 2 else "",
                     "state": r[3] if len(r) > 3 else "", **work_metadata.get(r[0], {})})
    wts = []
    terms = _m_guard.never_terms(n)
    hide = lambda t: _m_guard.redact(t, terms) if terms and isinstance(t, str) else t  # noqa: E731
    if not partial and n["external"] and n["home_path"] and n["home_path"].exists():
        home = n["home_path"]
        live = ui_live_worktrees(n)
        wts = [{k: hide(v) for k, v in cached_worktree(inst, home, b, w, n, cache, live=b in live).items()}
               for b, w in sorted(loc.get("worktrees", {}).items())]
        for wt, branch in zip(wts, sorted(loc.get("worktrees", {}))):
            if not wt.get('live', True):
                continue
            state = _m_verify.ready_state(inst, n, branch, display=True)
            wt["ready"] = {"ready": state["ready"], "reasons": [hide(x) for x in state["reasons"]],
                           "packet": {k: hide(v) for k, v in state["packet"].items() if k not in ("input_files", "input_files_ref")}
                           if state["packet"] else None}
        for c in ui_commits(home, since, all_refs=True):  # what comes from the home is shown as guard shows it
            home_commits.append({**c, "subject": hide(c["subject"]), "node": n["id"], "where": "home"})
    work = [{k: hide(v) for k, v in j.items()} for j in work]
    js = _m_jobs.jobs_summary(inst, n)
    for r in js["open"]:
        r.update(work_metadata.get(r["Job"], {}))
        used = None if partial else _m_spend.live_usage(inst, n, r)
        r["usage"] = _m_spend.spend_summary(used, terms) if used and used["sessions"] else None
        r["budget"] = _m_spend.job_budget(n, r)
    try:
        back = json.loads(_m_spend.spend_node_file(inst, n).read_text())
        total = back.get("total") if isinstance(back.get("total"), dict) else None
        if total:  # screened again: never-name words may have been added since it was written
            total = {**total, "models": _m_spend.spend_models(total.get("models") or {}, terms)}
        lookback = {"at": back.get("at"), "since": back.get("since"), "total": total,
                    "weeks": {rt: v.get("weeks", {}) for rt, v in (back.get("days") or {}).items()},
                    "sessions": len(back.get("sessions") or [])}
    except (OSError, ValueError, AttributeError):
        lookback = None
    try:
        wb = _m_worktrees.workbench(inst, n)
        reps = sorted((wb / "reports").glob("*/index.md"), reverse=True)[:5] if (wb / "reports").is_dir() else []
    except SystemExit:
        reps = []
    row = {
        "id": n["id"], "title": fm.get("title", ""), "parent": n["parent"]["id"] if n["parent"] else None,
        "owners": n["owners"], "labels": _m_common.lst(fm.get("labels")), "runtime": fm.get("runtime", ""),
        "preview": hide(loc.get("preview", "")), "preview_tour": hide(loc.get("preview_tour", "")),
        "preview_url": hide(fm.get("preview_url", "")),
        "sealed": n["sealed"], "archived": n["archived"], "paused": _m_status.paused(n), "external": n["external"],
        "home": _m_common.tilde(n["home_path"]) if n["home_path"] else "",
        "coordinator": inst.thread_link(loc["coordinator"]) if loc.get("coordinator") else "",
        "updated": u.isoformat() if u else None, "updated_ago": _m_status.ago(inst, u),
        "status": {k: v for k, v in secs.items() if v and k.lower() != "active work"},
        "work": work, "worktrees": wts, "inbox": [l[2:] for l in _m_status.inbox_lines(n, readonly=partial)],
        "blocked": _m_status.status_section(n, "blocked"),
        "blocked_metadata": [_m_ui_status.ui_blocked_metadata(inst, n, t, file_only=partial) for t in _m_status.status_section(n, "blocked")], "decisions": ui_decisions(inst, n), "limits": _m_knowledge.limits_of(n),
        "never_name": len(terms), "spend": lookback,
        "jobs": {**js, "rows": [{k: hide(v) for k, v in r.items()} for r in (_m_jobs.read_jobs(n) or [])
                              if r.get("Outcome") != "external"],
                                           "open": [{k: hide(v) for k, v in r.items()} for r in js["open"]],
                                           "forecasts": [{k: hide(v) for k, v in r.items()} for r in js["forecasts"]]},
        "reports": [{"name": d.parent.name, "title": next((l[2:] for l in d.read_text().splitlines()
                                                          if l.startswith("# ")), d.parent.name),
                     "path": _m_common.tilde(d)} for d in reps]}
    return row, home_commits


def ui_initial_state(inst, scan=None):
    """A file-only first reading; repository, readiness and usage fill in afterwards."""
    nodes = inst.nodes()
    return {
        'review': {'enabled': False}, 'due': [], 'scan': scan or [],
        'usage_policy': {'stop_percent': None, 'max_age_seconds': 120}, 'usage_routing_policy': {},
        'brain': {'name': _m_nodes.brain_name(inst.root), 'title': nodes[0]['fm'].get('title', 'Brain'),
                  'root': _m_common.tilde(inst.root), 'machine': inst.local.get('machine', ''),
                  'tz': str(inst.zone() or ''), 'fallback': inst.local.get('fallback', []),
                  'now': inst.now_dt().isoformat(timespec='seconds'), 'others': [], 'filling_in': True},
        'nodes': [ui_node(inst, n, '', partial=True)[0] for n in nodes],
        'attention': [], 'inbox': [], 'commits': [], 'hours': UI_HOURS,
        'activity': {'start': (int(time.time()) // 3600 - UI_HOURS + 1) * 3600, 'counts': {}},
        'commits_24h': 0, 'commits_total': 0, 'specialists': [],
        'items': {'count': 0, 'all': [], 'recent': []}, 'schedules': [],
        'machine': {}, 'spend': None, 'usage': [], 'usage_other_subscriptions': 0, 'problems': None,
    }


def ui_loading(box, now=None):
    return {'filling_in': box['partial'],
            'age_seconds': max(0, int((time.time() if now is None else now) - box['built_at'])),
            'refreshing': box['building'] or box.get('request', 0) > box.get('checked', 0),
            'failed': bool(box.get('error')), 'version': box.get('version', 0)}


class UIStateCache:
    """Process-local display cache. Explicit CLI checks remain immediate and uncached."""
    def __init__(self):
        self.nodes, self.due = {}, {}
        self.ready, self.worktrees, self.worktree_keys = {}, {}, {}
        self.local_jobs = {}
        self.usage, self.usage_revision = {}, None
        self.problems, self.problem_key, self.problem_at = None, None, float('-inf')

    def problems_for(self, inst, signature):
        # Integrity includes full home scans; batch changes while observed, at most once / 5 min.
        signature = tuple(row for row in signature if row[0] != 'clock')
        if self.problems is None or (signature != self.problem_key and time.monotonic() - self.problem_at >= 300):
            self.problems = len(_m_check.collect_problems(inst)[0])
            self.problem_key, self.problem_at = signature, time.monotonic()
        return self.problems


def _ui_state(inst, problems=None, scan=None, spend_budget=2.0, machine_spend=None, cache=None, node_keys=None):
    """Everything the page shows, from the same files `brain` reads. Read-only."""
    now = inst.now_dt()
    nodes = inst.nodes()
    ids = sorted((n["id"] for n in nodes), key=len, reverse=True)
    since = f"{UI_HOURS} hours ago"
    commits = []
    for c in ui_commits(inst.root, since):
        nid = next((i for i in ids if c["subject"].startswith(i + ":")), "root")
        commits.append({**c, "node": nid, "where": "brain"})
    out_nodes = []
    for n in nodes:
        key = node_keys.get(n['id']) if node_keys is not None else None
        old = cache.nodes.get(n['id']) if cache is not None else None
        if old is None or old[0] != key:
            row, home_commits = ui_node(inst, n, since, cache)
            if cache is not None:
                cache.nodes[n['id']] = (key, row, home_commits)
        else:
            _, row, home_commits = old
        u = _m_status.updated_at(inst, n)
        open_rows = [{**r, 'age_h': round(_m_status.elapsed(inst.parse_time(r['Started']), now) / 3600, 1)}
                     for r in row['jobs']['open']]
        out_nodes.append({**row, 'updated_ago': _m_status.ago(inst, u), 'jobs': {**row['jobs'], 'open': open_rows}})
        commits.extend(c for c in home_commits if c["t"] >= time.time() - UI_HOURS * 3600)
    commits.sort(key=lambda c: c["t"], reverse=True)
    start = (int(time.time()) // 3600 - (UI_HOURS - 1)) * 3600
    activity = {}
    for c in commits:
        h = (c["t"] - start) // 3600
        if 0 <= h < UI_HOURS:
            activity.setdefault(c["node"], [0] * UI_HOURS)[h] += 1
    items = sorted(inst.items(), key=lambda i: str(i["fm"].get("learned_at", "")), reverse=True)
    # Imported knowledge stays inspectable even while it is awaiting review.
    terms = list({t for n in nodes for t in _m_guard.never_terms(n)})
    def safe(value, field=""):
        if isinstance(value, str):
            return _m_nodes.import_text(value, terms, metadata=field not in ("body", "text"))
        if isinstance(value, dict):
            return {k: safe(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [safe(v, field) for v in value]
        return value
    library = []
    for i in items:
        fm = i["fm"]
        library.append(safe({"id": fm.get("id"), "title": fm.get("title"), "kind": fm.get("kind"),
                             "status": fm.get("status", "active"),
                             "review_node": next((n["id"] for n in nodes if n["dir"].resolve() == i["path"].parent.parent.resolve()), ""),
                             "state": "accepted" if fm.get("status", "active") == "active" else fm.get("status"),
                             "scope": _m_common.lst(fm.get("scope")), "path": str(i["path"].relative_to(inst.root)),
                             "body": _m_common.read_fm(i["path"])[1],
                             "provenance": {"node": fm.get("source_node"), "nodes": _m_common.lst(fm.get("source_nodes")),
                                            "ref": fm.get("source_ref"),
                                            "repo": fm.get("source_repo"), "commit": fm.get("source_commit"),
                                            "time": fm.get("learned_at"), "owners": _m_common.lst(fm.get("source_owners"))},
                             "accepted_at": fm.get("accepted_at"), "accepted_by": fm.get("accepted_by")}))
    specialists = []
    for sp in _m_knowledge.find_specialists(inst):
        lesson_path = sp["dir"] / "lessons.md"
        role_body = _m_common.read_fm(sp["dir"] / "role.md")[1]
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", role_body) if p.strip() and not p.strip().startswith("#")]
        role_sections = {k.lower(): v for k, v in md_sections(role_body).items()}
        specialists.append(safe({"id": sp["id"], "node": sp["node"]["id"],
                                 "title": sp["fm"].get("title"), "runtime": sp["fm"].get("runtime"),
                                 "role_summary": paragraphs[0] if paragraphs else "", "role_body": role_body,
                                 "purpose": role_sections.get("purpose", ""), "when_to_hire": role_sections.get("when to hire", ""),
                                 "hire_command": "brain hire " + shlex.quote(sp["id"]) + " " + shlex.quote(sp["node"]["id"]) + " --task 'Describe the task'",
                                 "scope": _m_common.lst(sp["fm"].get("scope")), "status": sp["fm"].get("status", "active"),
                                 "imported": sp["fm"].get("imported") == "true",
                                 "lessons": _m_knowledge.parse_lessons(lesson_path) if lesson_path.exists() else [],
                                 "jobs": [r for r in inst.local.get("hires", [])
                                          if r.get("specialist") == sp["id"] and r.get("at") == sp["node"]["id"]]}))
    try:
        policy = _m_usage.usage_rows(inst)[1]
        usage_policy = {k: policy[k] for k in ("stop_percent", "max_age_seconds")}
        usage_routing_policy = {k: policy[k] for k in ("pacing_margin", "autodetected")}
    except (OSError, ValueError):
        usage_policy = {"stop_percent": None, "max_age_seconds": 120}
        usage_routing_policy = {}
    machine = _m_machine.machine_state(inst)
    due = []
    for n in nodes:
        key = (node_keys.get(n['id']), int(time.time() // 300)) if node_keys is not None else None
        old = cache.due.get(n['id']) if cache is not None else None
        if old is None or old[0] != key:
            items_due = _m_status.due_items(inst, n)
            if cache is not None:
                cache.due[n['id']] = (key, items_due)
        else:
            items_due = old[1]
        due.extend(dict(item) for item in items_due)
    branches = {(n["id"], _m_jobs.job_key(r)): r["Job"] for n in nodes for r in (_m_jobs.read_jobs(n) or [])}
    for item in due:  # the job's own name, for a card that otherwise reads "this job"
        if item.get("job") and (item["node"], item["job"]) in branches:
            item["branch"] = branches[(item["node"], item["job"])]
    machine_due = _m_status.machine_due_items(inst, nodes, machine, due)
    inbox = []
    for item in _m_status.inbox_items(inst, nodes)[0]:
        extra = {}
        if item['kind'] in ('blocked', 'blocked-overdue'):
            node = next(n for n in nodes if n['id'] == item['node'])
            terms = _m_guard.never_terms(node)
            original = next((t for t in _m_status.status_section(node, 'blocked')
                             if _m_guard.redact(t, terms) == item['text']), item['text'])
            extra = _m_ui_status.ui_blocked_metadata(inst, node, original)
        inbox.append({**item, **extra, "from": inbox_sender(item)})
    def need_key(item):
        text = item.get("text", "")
        if item.get("kind") == "inbox":
            text = text.removeprefix("inbox: ")
        return item.get("node"), item.get("kind"), " ".join(text.split())
    inbox_ids = {need_key(item): item["id"] for item in inbox}
    for item in due + machine_due:
        # Shared inbox cards retain their action ID; other needs keep lifecycle IDs.
        item["id"] = (inbox_ids.get(need_key(item)) or item.get("id")
                      or _m_jobs.digest(["ui-due", *need_key(item)]))
    return {
        "review": {"enabled": False},
        "due": safe(due + machine_due),
        "scan": safe(scan or []),
        "usage_policy": usage_policy,
        "usage_routing_policy": usage_routing_policy,
        "brain": {"name": _m_nodes.brain_name(inst.root), "title": nodes[0]["fm"].get("title", "Brain"),
                  "root": _m_common.tilde(inst.root), "machine": inst.local.get("machine", ""), "tz": str(inst.zone() or ""),
                  "fallback": inst.local.get("fallback", []),
                  "now": now.isoformat(timespec="seconds"), "others": [k for k, r in _m_instance.brains_here()
                                                                       if r.resolve() != inst.root.resolve()]},
        "nodes": out_nodes,
        "attention": [safe({"node": a, "kind": k, "text": t,
                            **(_m_ui_status.ui_blocked_metadata(inst, next(n for n in nodes if n["id"] == a), t) if k in ("blocked", "blocked-overdue") else {})})
                      for a, k, t in _m_status.attention_items(inst, nodes)],
        "inbox": inbox,
        "commits": commits[:300], "hours": UI_HOURS, "activity": {"start": start, "counts": activity},
        "commits_24h": sum(1 for c in commits if c["t"] > time.time() - 86400), "commits_total": len(commits),
        "specialists": specialists,
        "items": {"count": len(items), "all": library, "recent": [safe({"id": i["fm"].get("id"), "title": i["fm"].get("title"),
                                                   "source": i["fm"].get("source_node"),
                                                   "at": str(i["fm"].get("learned_at", ""))}) for i in items[:6]]},
        "schedules": [{"id": x["id"], "when": x.get("when", ""), "node": x.get("node", ""),
                       "here": x["id"] in inst.local.get("schedules", {})} for x in _m_schedules.read_schedules(inst)],
        "machine": machine,
        "spend": machine_spend if machine_spend is not None else _m_spend.spend_machine(inst, budget=spend_budget),
        "usage": _m_usage.usage_ui(inst),
        "usage_other_subscriptions": _m_usage.usage_other_count(inst),
        "problems": problems,
    }


def inbox_sender(item):
    """Who wrote an inbox line, from its record: only inbox lines carry a sender ("<time> from <who>: ...")."""
    if item.get("kind") != "inbox":
        return ""
    m = re.match(r"^(?:inbox:\s*)?\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?(?: ?(?:Z|[+-]\d{2}:?\d{2}))? from ([^:\n]{1,60}):",
                 item.get("text") or "")
    return m.group(1).strip() if m else ""


def search_fold(text):
    """Accent-, case- and Arabic-letter-form-insensitive text for matching."""
    import unicodedata
    text = unicodedata.normalize("NFKD", str(text or ""))
    text = "".join(c for c in text if not unicodedata.combining(c) and c != "\u0640")
    for forms, plain in (("أإآٱ", "ا"), ("ى", "ي"), ("ة", "ه"), ("ؤ", "و"), ("ئ", "ي")):
        for f in forms:
            text = text.replace(f, plain)
    return " ".join(text.casefold().split())


def search_rank(name, query):
    """Exact, then the name's start, then a later word's start, then earlier matches, then any match; None if none."""
    n, q = search_fold(name), search_fold(query)
    if not q:
        return (0, 0)
    if n == q:
        return (0, 0)
    if n.startswith(q):
        return (1, 0)
    starts = [m.start() for m in re.finditer(r"(?:^|(?<=[\s\-_/(·.,:]))" + re.escape(q), n)]
    if starts:
        return (2, starts[0])
    at = n.find(q)
    return (3, at) if at >= 0 else None


def search_records(state):
    """Every record a reference field can point at, with one secondary line that tells similar names apart."""
    nodes = {n["id"]: n for n in state.get("nodes", [])}
    title = lambda nid: (nodes.get(nid) or {}).get("title") or "Workspace"  # noqa: E731
    out = []
    for n in state.get("nodes", []):
        if n.get("archived"):
            continue
        parent = n.get("parent")
        out.append({"kind": "node", "id": n["id"], "title": n.get("title") or n["id"],
                    "secondary": (title(parent) + " · " if parent else "Workspace root · ") + (n.get("updated_ago") or "update time not recorded"),
                    "recent": n.get("updated") or "", "used": len((n.get("jobs") or {}).get("rows") or [])})
    for sp in state.get("specialists", []):
        out.append({"kind": "specialist", "id": f"{sp.get('node')}/{sp.get('id')}", "title": sp.get("title") or sp.get("id"),
                    "secondary": " · ".join(x for x in (sp.get("runtime"), title(sp.get("node")),
                                                         f"{len(sp.get('jobs') or [])} hires") if x),
                    "recent": max((str(j.get("time") or "") for j in sp.get("jobs") or []), default=""),
                    "used": len(sp.get("jobs") or [])})
    for i in (state.get("items") or {}).get("all", []):
        prov = i.get("provenance") or {}
        out.append({"kind": "learning", "id": i.get("id"), "title": i.get("title") or i.get("id"),
                    "secondary": " · ".join(x for x in ((i.get("kind") or "Item").capitalize(), title(prov.get("node")),
                                                         "proposed" if i.get("status") == "proposed" else "") if x),
                    "recent": str(prov.get("time") or ""), "used": 0})
    for n in state.get("nodes", []):
        for r in (n.get("jobs") or {}).get("rows") or []:
            out.append({"kind": "job", "id": f"{n['id']}/{r.get('Job')}/{r.get('Started')}", "title": r.get("Job") or "job",
                        "secondary": " · ".join(x for x in (n.get("title"), r.get("Outcome") or "open", r.get("Runtime")) if x),
                        "recent": str(r.get("Started") or ""), "used": 0})
    return out


def ui_search(state, kind="all", query="", offset=0, limit=20):
    """One page of ranked records for the dashboard's reference pickers: read-only, from the served state."""
    records = [r for r in search_records(state) if kind in ("all", r["kind"])]
    query = str(query or "")[:200]
    if query.strip():
        ranked = [(rank, r) for r in records for rank in [search_rank(r["title"], query)] if rank is not None]
        ranked.sort(key=lambda x: (x[0], search_fold(x[1]["title"]), x[1]["id"]))
        records = [r for _, r in ranked]
    else:  # nothing typed yet: the most relevant first (recent, then most used, then alphabetical)
        records.sort(key=lambda r: search_fold(r["title"]))
        records.sort(key=lambda r: r["used"], reverse=True)
        records.sort(key=lambda r: r["recent"], reverse=True)
    offset, limit = max(0, int(offset)), max(1, min(100, int(limit)))
    page = [{k: v for k, v in r.items() if k not in ("recent", "used")} for r in records[offset:offset + limit]]
    return {"items": page, "total": len(records), "offset": offset,
            "next": offset + limit if offset + limit < len(records) else None}


def core_revision():
    """Which core serves the dashboard: its path and commit, so a preview names its source."""
    head = _m_common.git("rev-parse", "--short", "HEAD", cwd=_m_common.CORE).stdout.strip() if (_m_common.CORE / ".git").exists() else ""
    dirty = _m_common.git("status", "--porcelain", "--untracked-files=no", cwd=_m_common.CORE).stdout.strip() if head else ""
    return f"{_m_common.tilde(_m_common.CORE)} @ {head or 'no git commit'}{' (uncommitted changes)' if dirty else ''}"


class UISpend:
    """Continue unfinished aggregation without rebuilding the dashboard or writing caches."""
    BUDGET = 0.05
    PUBLISH_EVERY = 60

    def __init__(self, inst):
        from collections import deque
        self.inst = inst
        self.queue = deque()
        self.pending = set()
        self.stamps, self.aggs = {}, {}
        self.allowed, self.forbidden = set(), set()
        self.permissions = None
        self.published = float('-inf')
        self.dirty = True
        self.revision = 0
        self.background = False
        self.view = self.view_key()
        self.value = {'complete': False, 'files': 0, 'read': 0, 'runtimes': {}, 'since_week': None}

    def view_key(self):
        return (frozenset(_m_spend.brain_terms(self.inst)), self.inst.now_dt().date(), int(time.time() // 300))

    def refresh_view(self):
        key = self.view_key()
        if key != self.view:
            self.view = key
            self.dirty, self.published = True, float('-inf')
            return True
        return False

    def discover(self):
        sources, allowed, forbidden = _m_spend.spend_sources(self.inst)
        permissions = (frozenset(allowed), frozenset(forbidden))
        if permissions != self.permissions:
            self.aggs.clear()
            self.stamps.clear()
            self.queue.clear()
            self.pending.clear()
            self.dirty, self.published = True, float('-inf')
        self.permissions = permissions
        self.allowed, self.forbidden = allowed, forbidden
        files = {}
        for runtime, cfg in sources:
            for name in (('projects',) if runtime == 'claude' else ('sessions', 'archived_sessions')):
                for path in _m_spend.spend_walk(cfg / name):
                    if runtime == 'claude' and path.parent.name != 'subagents' and path.parent.parent != (cfg / name).resolve():
                        continue
                    stamp = UIInputs.stamp(path)
                    files[path] = stamp
                    if self.stamps.get(path) != stamp and path not in self.pending:
                        self.queue.append((runtime, path))
                        self.pending.add(path)
        removed = self.stamps.keys() - files.keys()
        if removed:
            self.published = float('-inf')
        for path in removed:
            self.aggs.pop(path, None)
        if files != self.stamps:
            self.revision += 1
            self.dirty = True
        self.stamps = files

    def advance(self):
        # Passive tabs reserve CPU for metadata and genuine workspace changes;
        # unfinished files still advance on every slice and publish completion.
        deadline = time.monotonic() + self.BUDGET * (0.5 if self.background else 1)
        with _m_spend.spend_read_only():
            while self.queue and time.monotonic() < deadline:
                runtime, path = self.queue.popleft()
                self.pending.discard(path)
                if path not in self.stamps:
                    continue
                agg = _m_spend.spend_file(self.inst, path, runtime, self.allowed, self.forbidden,
                                          budget=max(0.000001, deadline - time.monotonic()))
                if agg:
                    self.aggs[path] = agg
                    self.dirty = True
                    if not agg.get('complete', True):
                        self.queue.appendleft((runtime, path))
                        self.pending.add(path)
                else:
                    self.aggs.pop(path, None)
        done = not self.queue
        if self.dirty and (time.monotonic() - self.published >= self.PUBLISH_EVERY or done != self.value['complete']):
            aggs = list(self.aggs.values())
            self.value = {'complete': done, 'files': len(self.stamps), 'read': len(aggs),
                          'runtimes': _m_spend.spend_days(self.inst, aggs),
                          'since_week': _m_spend.spend_summary(_m_spend.spend_sum(aggs, time.time() - 7 * 86400),
                                                             _m_spend.brain_terms(self.inst)) if done else None}
            self.published, self.dirty = time.monotonic(), False
            return True
        return False


class UIInputs:
    """Metadata only: no transcript parsing, Git status, readiness hashes or cache writes per poll."""
    def __init__(self, inst):
        self.root = inst.root
        self.files = set()
        self.worker_path = self.root / '.brain/worker-runs.json'
        self.worker_stamp, self.worker_context_stamp = None, None
        self.worker_records, self.worker_context, self.worker_nodes = [], {}, {}
        self.worker_rows = ()
        self.slow_files = set()
        self.slow_at, self.slow_rows = float('-inf'), []
        self.trees = []
        self.git_dirs = set()
        self.source_trees = []
        self.node_paths = {}
        self.listings = {}
        self.home_owners = {}
        self.home_indexes = {}
        self.home_dirs = {}
        self.home_git = {}
        self.verify_globs = []
        self.verify_paths = {}
        self.report_dirs = set()
        self.core_required = any(_m_rehearse.required(n) for n in inst.nodes())
        self.core_revision, self.core_at = None, None
        for n in inst.nodes():
            loc = n['local']
            live = ui_live_worktrees(n)
            homes = [n['home_path']] if n['external'] and n['home_path'] else []
            homes += [Path(w['path']) for w in live.values() if w.get('path')]
            watched = self.node_paths.setdefault(n['id'], set())
            for w in live.values():
                if w.get('path'):
                    self.verify_globs.append((n['id'], Path(w['path']),
                                              _m_common.lst(n['fm'].get('verify_inputs')) or
                                              _m_common.lst(_m_verify.VERIFY_INPUTS)))
            for home in homes:
                self.home_owners.setdefault(home, set()).add(n['id'])
                home_paths = {home}
                self.home_indexes.setdefault(home, set())
                self.home_git.setdefault(home, set())
                # Git's index supplies tracked and non-ignored files; directory metadata detects additions.
                result = _m_common.git('ls-files', '-z', '--cached', '--others', '--exclude-standard', cwd=home)
                watched.add(home)
                self.slow_files.add(home)
                for name in result.stdout.split('\0'):
                    if not name:
                        continue
                    path = home / name
                    home_paths.add(path)
                    watched.add(path)
                    self.slow_files.add(path)
                    while path.parent != home and path.parent != path:
                        path = path.parent
                        home_paths.add(path)
                        watched.add(path)
                        self.slow_files.add(path)
                self.listings[home] = home_paths
                for flag in ('--git-dir', '--git-common-dir'):
                    raw = _m_common.git('rev-parse', flag, cwd=home).stdout.strip()
                    if raw:
                        directory = (home / raw).resolve()
                        self.git_dirs.add(directory)
                        self.home_indexes[home].add(directory / 'index')
                        self.home_git[home].add(directory)
                        watched.update(directory / name for name in ('HEAD', 'index', 'packed-refs', 'logs/HEAD'))
                        watched.add(directory / 'refs')
            try:
                reports = _m_worktrees.workbench(inst, n) / 'reports'
                self.report_dirs.add(reports)
                self.files.add(reports)
                self.files.update(reports.glob('*/index.md'))
                self.trees.extend(reports.parent / 'scratch' / name for name in ('ready', 'rehearse'))
            except SystemExit:
                pass
        for directory in self.git_dirs | {self.root / '.git'}:
            self.files.update(directory / name for name in ('HEAD', 'index', 'packed-refs', 'logs/HEAD'))
            self.trees.append(directory / 'refs')
        sources, _, _ = _m_spend.spend_sources(inst)
        for runtime, cfg in sources:
            self.source_trees.extend(cfg / name for name in (('projects',) if runtime == 'claude' else ('sessions', 'archived_sessions')))
        self.files.update(_m_usage.usage_file(name) for name in ('usage.json', 'subscriptions.json', 'brains.json'))
        self.topology = self.topology_key(inst)
        self.home_dirs = {home: {home} | {p.parent for p in paths} for home, paths in self.listings.items()}
        self.listing_keys = {home: self.listing_key(home) for home in self.listings}
        self.home_names = {home: tuple(map(str, paths)) for home, paths in self.listings.items()}
        self.home_keys = {}

    def listing_key(self, home):
        return tuple(self.stamp(path) for path in sorted(self.home_dirs[home] | self.home_indexes[home]))

    def update_listings(self):
        for home in self.listings:
            if self.listing_key(home) == self.listing_keys[home]:
                continue
            result = _m_common.git('ls-files', '-z', '--cached', '--others', '--exclude-standard', cwd=home)
            paths = {home}
            for name in result.stdout.split('\0'):
                if not name:
                    continue
                path = home / name
                paths.add(path)
                while path.parent != home and path.parent != path:
                    path = path.parent
                    paths.add(path)
            before = self.listings[home]
            for nid in self.home_owners[home]:
                self.node_paths[nid].difference_update(before - paths)
                self.node_paths[nid].update(paths)
            self.listings[home] = paths
            self.home_names[home] = tuple(map(str, paths))
            self.home_dirs[home] = {home} | {p.parent for p in paths}
            self.listing_keys[home] = self.listing_key(home)
        self.slow_files = set().union(*self.listings.values()) if self.listings else set()

    @staticmethod
    def permission_key():
        return tuple(UIInputs.stamp(_m_usage.usage_file(name)) for name in ('subscriptions.json', 'brains.json'))

    @staticmethod
    def topology_key(inst):
        # Coordinator/preview/status edits do not change the watched repositories.
        return (tuple((n['id'], str(n['home_path']), str(n['local'].get('workbench')), n['fm'].get('verify_inputs'), n['fm'].get('rehearse'), tuple(sorted(ui_live_worktrees(n))), tuple(sorted(
                    (b, str(w.get('path'))) for b, w in n['local'].get('worktrees', {}).items())))
                      for n in inst.nodes()),
                tuple(UIInputs.stamp(_m_usage.usage_file(name)) for name in ('subscriptions.json', 'brains.json')),
                tuple(UIInputs.stamp(Path(w['path']) / '.git') for n in inst.nodes()
                      for w in ui_live_worktrees(n).values()
                      if w.get('path') and (Path(w['path']) / '.git').is_file()))

    def node_keys(self, inst, signature):
        rows = dict((row[0], row) for row in self.fast_rows)
        global_rows = (self.stamp(self.root / 'instance.md'),
                       json.dumps({k: v for k, v in inst.local.items() if k != 'nodes'}, sort_keys=True))
        out = {}
        for n in inst.nodes():
            prefix = str(n['dir']) + '/'
            children = prefix + 'nodes/'
            owned = self.node_paths.get(n['id'], set())
            prefixes = [str(path) + '/' for path in owned if path.name == 'refs']
            try:
                wb = _m_worktrees.workbench(inst, n)
                prefixes += [str(wb / name) + '/' for name in ('reports', 'scratch/ready', 'scratch/rehearse')]
                report_stamp = self.stamp(wb / 'reports')
            except SystemExit:
                report_stamp = ('reports', None)
            key = [row for path, row in rows.items() if
                   (path.startswith(prefix) and not path.startswith(children)) or
                   any(path.startswith(p) for p in prefixes)]
            homes = tuple((str(home), self.home_keys.get(home)) for home, owners in self.home_owners.items() if n['id'] in owners)
            key.extend((rows.get(str(path)) or self.stamp(path)) for path in owned if path not in self.slow_files)
            key.extend([report_stamp, self.stamp(_m_spend.spend_node_file(inst, n))])
            parent = n['parent']
            while parent:
                key.extend(self.stamp(parent['dir'] / name) for name in ('node.md', 'never-name.txt'))
                parent = parent['parent']
            out[n['id']] = (global_rows, json.dumps(n['local'], sort_keys=True), homes, tuple(sorted(set(key))),
                            self.worker_nodes.get(n['id'], ()),
                            self.core_revision if _m_rehearse.required(n) else None)
        return out

    def worktree_keys(self, inst, signature):
        rows = {row[0]: row for row in self.fast_rows}
        result = {}
        for n in inst.nodes():
            prefix = str(n['dir']) + '/'
            semantic = [self.stamp(n['dir'] / name) for name in ('node.md', 'never-name.txt', _m_jobs.JOBS_FILE)]
            if _m_rehearse.required(n):
                semantic.append(('installed-core', self.core_revision))
            semantic.append(self.stamp(self.root / 'instance.md'))
            semantic.append(('bindings', json.dumps({k: n['local'].get(k) for k in ('home', 'workbench', 'worktrees')}, sort_keys=True)))
            trains = str(n['dir'] / 'job-trains') + '/'
            semantic.extend(row for path, row in rows.items() if path.startswith(trains))
            parent = n['parent']
            while parent:
                semantic.extend(self.stamp(parent['dir'] / name) for name in ('node.md', 'never-name.txt'))
                parent = parent['parent']
            try:
                wb = _m_worktrees.workbench(inst, n)
                prefixes = [str(wb / name) + '/' for name in ('scratch/ready', 'scratch/rehearse')]
                semantic.extend(row for path, row in rows.items() if any(path.startswith(p) for p in prefixes))
            except SystemExit:
                pass
            jobs = _m_jobs.read_jobs(n) or []
            for branch, w in n['local'].get('worktrees', {}).items():
                job_rows = [row for row in jobs if row['Job'] == branch]
                records = [self.stamp(_m_jobs.handback_file(n, row)) for row in job_rows]
                home = Path(w['path'])
                git_rows = []
                for directory in self.home_git.get(home, set()) | self.home_git.get(n['home_path'], set()):
                    git_rows.extend(self.stamp(directory / name) for name in ('HEAD', 'index', 'packed-refs', 'config'))
                    # Read only the job and integration refs, not unrelated workers' branch tips.
                    refs = ['refs/heads/' + branch, 'refs/heads/main', 'refs/remotes/origin/HEAD']
                    symbolic = directory / 'refs/remotes/origin/HEAD'
                    try:
                        target = symbolic.read_text().strip().removeprefix('ref: ')
                        if target.startswith('refs/remotes/origin/') and '..' not in Path(target).parts:
                            refs.extend([target, target.replace('refs/remotes/origin/', 'refs/heads/', 1)])
                    except OSError:
                        pass
                    git_rows.extend(self.stamp(directory / ref) for ref in refs)
                extras = tuple(self.stamp(path) for path in sorted(self.verify_paths.get(home, set())))
                result[(n['id'], branch)] = (tuple(semantic), tuple(records), self.stamp(home), self.home_keys.get(home),
                                             tuple(git_rows), extras)
        return result

    @staticmethod
    def stamp(path):
        try:
            st = path.stat()
            return (str(path), st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
        except OSError:
            return (str(path), None)

    def source_signature(self):
        paths = set()
        for root in self.source_trees:
            paths.add(root)
            for top, dirs, files in os.walk(root):
                paths.update(Path(top) / name for name in [*dirs, *files])
        return tuple(sorted(self.stamp(path) for path in paths))

    def worker_signature(self, rows):
        """Cache host reads by metadata, but invalidate displays only for current worker events."""
        local_path = str(self.root / '.brain/local.json')
        context_stamp = tuple(sorted(row for row in rows if row[0] == local_path
                                     or row[0].endswith('/node.md')
                                     or row[0].endswith('/' + _m_jobs.JOBS_FILE)))
        context_changed = context_stamp != self.worker_context_stamp
        if context_changed:
            self.worker_context = {}
            for n in _m_instance.Instance(self.root).nodes():
                workers = n['local'].get('workers', {})
                if not isinstance(workers, dict):
                    continue
                opened = {row['Job']: row for row in (_m_jobs.read_jobs(n) or [])
                          if row.get('Job') and not row.get('Ended')}
                self.worker_context[n['id']] = {_m_jobs.job_key(row): workers.get(_m_jobs.job_key(row))
                                                for row in opened.values()}
            self.worker_context_stamp = context_stamp
        stamp = self.stamp(self.worker_path)
        if stamp == self.worker_stamp and not context_changed:
            return self.worker_rows
        if stamp != self.worker_stamp:
            try:
                data = json.loads(self.worker_path.read_text())
                records = data.get('runs', []) if isinstance(data, dict) else []
                self.worker_records = records if isinstance(records, list) else []
            except (OSError, ValueError, UnicodeError):
                self.worker_records = []
            self.worker_stamp = stamp
        latest = {}
        for record in self.worker_records:
            if not isinstance(record, dict):
                continue
            node, key, thread, run, state = (record.get(name) for name in
                                             ('node', 'job_key', 'thread', 'run', 'status'))
            if (not isinstance(node, str) or not isinstance(key, str)
                    or not isinstance(thread, str) or not thread
                    or self.worker_context.get(node, {}).get(key) != thread
                    or not isinstance(run, str) or not run.strip()
                    or not isinstance(state, str) or not state.strip()):
                continue
            category = ''
            if state == 'failed':
                error = str(record.get('error', '')).casefold()
                category = 'at capacity' if 'at capacity' in error else (
                    'usage limit' if 'usage limit' in error else 'provider error')
            latest[(node, key)] = (key, thread, run, state, category)
        self.worker_nodes = {}
        for (node, _), record in sorted(latest.items()):
            self.worker_nodes.setdefault(node, []).append(record)
        self.worker_nodes = {node: tuple(records) for node, records in self.worker_nodes.items()}
        self.worker_rows = tuple(sorted(self.worker_nodes.items()))
        return self.worker_rows

    def signature(self):
        # Readiness also depends on explicitly declared ignored inputs (.env by default).
        # Discover by name/metadata here; only changed readiness reads their contents.
        for nid, home, patterns in self.verify_globs:
            extra = {path for pattern in patterns for path in home.glob(pattern)
                     if '.git' not in path.relative_to(home).parts and path.is_file()}
            self.node_paths[nid].difference_update(self.verify_paths.get(home, set()) - extra)
            self.node_paths[nid].update(extra)
            self.verify_paths[home] = extra
        paths = set(self.files)
        for reports in self.report_dirs:
            paths.update(reports.glob('*/index.md'))
        for extra in self.verify_paths.values():
            paths.update(extra)
        # Brain source and recorded aggregates matter. The installed core, object store,
        # machine sampler, locks and transcript-cache output are not dashboard inputs.
        for root in [self.root, *self.trees]:
            paths.add(root)
            for top, dirs, files in os.walk(root):
                path = Path(top)
                if path == self.root:
                    dirs[:] = [d for d in dirs if d not in ('core', '.git')]
                if path == self.root / '.brain':
                    dirs[:] = [d for d in dirs if d != 'spend']
                    files = [f for f in files if f not in ('machine.json', 'lock')]
                dirs.sort()
                paths.update(path / name for name in files)
                paths.update(path / name for name in dirs if path / name != self.root / '.brain')
        # Explicit look-back commands publish node aggregates; dashboard polls do not.
        paths.update((self.root / '.brain/spend/nodes').glob('*.json'))
        paths.discard(self.worker_path)
        rows = [self.stamp(path) for path in paths]
        if time.monotonic() - self.slow_at >= 15:
            self.update_listings()
            self.slow_rows = [self.stamp(path) for path in self.slow_files - paths -
                              {self.worker_path, self.root / '.brain'}]
            self.slow_at = time.monotonic()
        if self.core_required:
            clock = int(time.time() // 300)
            if (self.root / 'core/.git').exists() or clock != self.core_at:
                try:
                    self.core_revision = _m_rehearse.installed_core_commit(self.root)
                except _m_rehearse.RehearsalError:
                    self.core_revision = None
                self.core_at = clock
            rows.append(('installed-core', self.core_revision))
        rows.append(('worker-runs', self.worker_signature(rows)))
        self.fast_rows = tuple(rows)
        rows.extend(self.slow_rows)
        indexed = {row[0]: row for row in rows}
        self.home_keys = {home: frozenset(indexed.get(name, (name, None)) for name in names)
                          for home, names in self.home_names.items()}
        rows.append(('clock', int(time.time() // 300)))  # age/due boundaries, not a per-poll rebuild
        return tuple(sorted(rows))


def cmd_ui(a):
    """A live view of this brain in a browser on this machine: read-only, listening on 127.0.0.1 only."""
    import hmac
    import http.server
    import secrets
    import threading
    import urllib.parse
    os.environ['GIT_OPTIONAL_LOCKS'] = '0'  # a view must not refresh Git indexes on disk
    inst = UIInstance(_m_nodes.load().root)
    # Scan only explicit startup directories, once. Reuse the CLI's read-only discovery.
    scan = []
    if a.scan:
        import contextlib
        import io
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            _m_nodes.cmd_scan(argparse.Namespace(dirs=a.scan, depth=4, json=True))
        scan = json.loads(output.getvalue())
    reviews = _m_ui_review.Reviews(inst.root, enabled=getattr(a, "write", False))
    key = secrets.token_urlsafe(18)  # other accounts on this machine can reach 127.0.0.1 too; they don't get this
    if a.json:
        with _m_common.git_read_snapshot():
            print(json.dumps(ui_state(inst, len(_m_check.collect_problems(inst)[0]), scan, spend_budget=None), indent=1))
        return
    version = _m_ui_server.ui_code_version()
    if a.open:
        existing = _m_ui_server.ui_reuse_server(inst, a.port, version, reviews.enabled)
        if existing:
            import webbrowser
            print(f'brain ui core: {_m_common.tilde(_m_common.CORE)} @ source {version[:12]}', flush=True)
            print(f'brain ui: {existing}  (reusing this version on port {a.port})', flush=True)
            webbrowser.open(existing)
            return
    assets = _m_common.CORE / "ui" / "dist"
    started = (assets / "index.html").read_bytes()

    def page():
        # Read the shell at each request: after a rebuild its asset names must match the files on disk.
        try:
            return (assets / "index.html").read_bytes()
        except OSError:
            return started
    cond = threading.Condition()
    interval = max(0.05, a.every)
    box = {'json': b'', 'version': 0, 'clients': 0, 'last_poll': float('-inf'),
           'telemetry': time.monotonic(), 'request': 0, 'checked': 0,
           'partial': True, 'built_at': time.time(), 'building': True, 'error': '', 'stopped': False}

    def touch(wait=False):
        with cond:
            box['last_poll'] = time.monotonic()
            box['request'] += 1
            requested = box['request']
            cond.notify_all()
            # An idle poll must receive the newly checked snapshot, not the previous poll's data.
            if wait or not box['partial']:
                cond.wait_for(lambda: not box['partial'] and box['checked'] >= requested, timeout=4)

    def publish(st, complete=False):
        st = {**st, "review": {"enabled": reviews.enabled}}
        with cond:
            box.update(state=st, json=json.dumps(st, sort_keys=True).encode(), version=box['version'] + 1)
            if complete:
                box.update(partial=False, built_at=time.time(), building=False, error='')
            cond.notify_all()

    def refresh(inputs, signature, spend, cache, source_signature, permissions):
        metadata_at = float('-inf')
        live_at, live_revision = time.monotonic(), spend.revision
        while True:
            with cond:
                # Idle/background clients poll each minute. Keep their small aggregation slices
                # moving between polls; a closed/no-client server sleeps after the lease expires.
                while not box['stopped'] and not box['clients'] and time.monotonic() - box['last_poll'] > 75:
                    cond.wait()
                if box['stopped']:
                    return
                requested = box['request']
                spend.background = not box['clients']
            try:
                cadence = interval if box['clients'] else max(interval, 60)
                if requested > box['checked'] or time.monotonic() - metadata_at >= cadence:
                    changed = inputs.signature()
                    if changed != signature:
                        with cond:
                            box['building'] = True
                        i = UIInstance(inst.root)
                        if inputs.topology != inputs.topology_key(i):
                            inputs = UIInputs(i)
                            changed = inputs.signature()
                        problems = cache.problems_for(i, changed)
                        spend.inst = i
                        if spend.refresh_view():
                            spend.advance()
                        access = inputs.permission_key()
                        if access != permissions:
                            spend.discover()
                            spend.advance()
                            cache.usage.clear()
                            cache.nodes.clear()
                            cache.due.clear()
                            cache.usage_revision = (spend.revision, spend.permissions)
                            permissions = access
                        cache.worktree_keys = inputs.worktree_keys(i, changed)
                        st = ui_state(i, problems, scan, machine_spend=spend.value, cache=cache,
                                      node_keys=inputs.node_keys(i, changed))
                        signature = changed
                        publish(st, complete=True)
                    sources = inputs.source_signature()
                    if sources != source_signature:
                        spend.discover()
                        source_signature = sources
                    metadata_at = time.monotonic()
                if spend.advance():
                    publish({**box['state'], 'spend': spend.value})
                if spend.revision != live_revision and time.monotonic() - live_at >= 60:
                    i = UIInstance(inst.root)
                    nodes = {n['id']: n for n in i.nodes()}
                    updated = []
                    cache.usage_revision = (spend.revision, spend.permissions)
                    with _m_spend.spend_read_only(), _m_spend.live_usage_snapshot(cache.usage, cache.usage_revision):
                        for n in box['state']['nodes']:
                            real = nodes.get(n['id'])
                            if not real:
                                updated.append(n)
                                continue
                            rows = []
                            for row in n['jobs']['open']:
                                used = _m_spend.live_usage(i, real, row)
                                rows.append({**row, 'usage': _m_spend.spend_summary(used, _m_guard.never_terms(real))
                                             if used and used['sessions'] else None})
                            refreshed = {**n, 'jobs': {**n['jobs'], 'open': rows}}
                            updated.append(refreshed)
                            if n['id'] in cache.nodes:
                                key, _, commits = cache.nodes[n['id']]
                                cache.nodes[n['id']] = (key, refreshed, commits)
                    publish({**box['state'], 'nodes': updated})
                    live_at, live_revision = time.monotonic(), spend.revision
                if time.monotonic() - box['telemetry'] >= UI_TELEMETRY_SECONDS:
                    publish(ui_meters(spend.inst, box['state'], cache))
                    box['telemetry'] = time.monotonic()
            except Exception as e:
                with cond:
                    box.update(error=str(e), building=False)
            finally:
                with cond:
                    box['checked'] = requested
                    cond.notify_all()
            with cond:
                if box['request'] == box['checked']:
                    cond.wait(timeout=interval)

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, code, body, ctype, headers=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if not _m_ui_review.authorized_write(self.headers, self.client_address[0],
                                                 self.server.server_address[1], key):
                return self.send(403, b"Local same-origin review key required", "text/plain")
            url = urllib.parse.urlsplit(self.path)
            if url.path == '/api/server/stop':
                self.send(200, b'{}', 'application/json')
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if url.path not in ("/api/review/prepare", "/api/review/apply"):
                return self.send(404, b"not found", "text/plain")
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 256000:
                    raise ValueError("Review request is too large or empty")
                self.connection.settimeout(10)
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, dict):
                    raise ValueError("A JSON object is required")
                result = (reviews.prepare(payload) if url.path.endswith("/prepare") else reviews.apply(payload))
            except (ValueError, OSError, SystemExit) as error:
                # Core validation messages may carry private source text: do not echo it across HTTP.
                message = str(error) if isinstance(error, _m_ui_review.ReviewError) else "Review failed validation; no confirmation is retained. Refresh and review again."
                return self.send(409, json.dumps({"error": message}).encode(), "application/json")
            if url.path.endswith("/apply"):
                touch(wait=True)
            return self.send(200, json.dumps(result).encode(), "application/json")

        def do_GET(self):
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            if host not in ("127.0.0.1", "localhost", "[::1]"):  # no other site can read it through this browser
                return self.send(403, b"forbidden", "text/plain")
            url = urllib.parse.urlsplit(self.path)
            given = urllib.parse.parse_qs(url.query).get("k", [""])[0]
            if url.path in ("/", "/index.html"):
                return self.send(200, page(), "text/html; charset=utf-8")  # the page holds no data; the API does
            if url.path.startswith("/assets/"):
                # Serve only committed build assets, never arbitrary paths or symlinks.
                name = urllib.parse.unquote(url.path[len("/assets/"):])
                path = assets / "assets" / name
                try:
                    if "/" in name or "\\" in name or name.startswith(".") or path.is_symlink() or not path.is_file():
                        return self.send(404, b"not found", "text/plain")
                    body = path.read_bytes()
                except (ValueError, OSError):
                    return self.send(404, b"not found", "text/plain")
                import mimetypes
                return self.send(200, body, mimetypes.guess_type(str(path))[0] or "application/octet-stream")
            if url.path.startswith("/api/") and not hmac.compare_digest(given, key):
                return self.send(403, b"this run's key is missing: open the address brain ui printed", "text/plain")
            if url.path == '/api/server':
                return self.send(200, json.dumps(identity).encode(), 'application/json')
            if url.path == '/api/loading':
                with cond:
                    body = json.dumps(ui_loading(box)).encode()
                return self.send(200, body, 'application/json')
            if url.path == "/api/review":
                try:
                    result = reviews.catalog()
                except (ValueError, OSError, SystemExit):
                    return self.send(409, b'{"error":"Review catalog is unavailable"}', "application/json")
                return self.send(200, json.dumps(result).encode(), "application/json")
            if url.path == "/api/state":
                touch()
                with cond:
                    body, version = box["json"], str(box["version"])
                return self.send(200, body, "application/json", {"X-Brain-UI-Version": version})
            if url.path == "/api/search":
                q = urllib.parse.parse_qs(url.query)
                one = lambda k, d: (q.get(k) or [d])[0]  # noqa: E731
                try:
                    found = ui_search(box["state"], one("kind", "all"), one("q", ""), int(one("offset", "0")),
                                      int(one("limit", "20")))
                except ValueError:
                    return self.send(400, b"offset and limit are numbers", "text/plain")
                return self.send(200, json.dumps(found).encode(), "application/json")
            if url.path == "/api/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                with cond:
                    box['clients'] += 1
                    cond.notify_all()
                import select
                import socket
                seen, pinged = -1, time.monotonic()
                try:
                    while True:
                        with cond:
                            if box["version"] == seen:
                                cond.wait(timeout=1)
                            v, body = box["version"], box["json"]
                        if select.select([self.connection], [], [], 0)[0] and not self.connection.recv(1, socket.MSG_PEEK):
                            return
                        if v == seen and time.monotonic() - pinged < 15:
                            continue
                        if v != seen:
                            self.wfile.write(b"data: " + body + b"\nid: " + str(v).encode() + b"\n\n")
                            seen = v
                        else:
                            self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        pinged = time.monotonic()
                except (BrokenPipeError, ConnectionResetError):
                    return
                finally:
                    with cond:
                        box['clients'] -= 1
                        cond.notify_all()
            self.send(404, b"not found", "text/plain")

    st = ui_initial_state(inst, scan)
    publish(st)

    def initialize():
        # One worker owns initialization and ongoing refresh. A failed attempt sleeps
        # until another state request, rather than abandoning a usable partial view.
        while True:
            with cond:
                if box['stopped']:
                    return
                requested = box['request']
                box['building'] = True
            try:
                with _m_common.git_read_snapshot():
                    current = UIInstance(inst.root)
                    spend = UISpend(current)
                    spend.discover()
                    spend.advance()
                    inputs = UIInputs(current)
                    signature = inputs.signature()
                    cache = UIStateCache()
                    cache.worktree_keys = inputs.worktree_keys(current, signature)
                    cache.usage_revision = (spend.revision, spend.permissions)
                    full = ui_state(current, cache.problems_for(current, signature), scan, machine_spend=spend.value,
                                    cache=cache, node_keys=inputs.node_keys(current, signature))
                    source_signature = inputs.source_signature()
                    permissions = inputs.permission_key()
                with cond:
                    if box['stopped']:
                        return
                    publish(full, complete=True)
                    box['checked'] = requested
            except (Exception, SystemExit) as error:
                with cond:
                    box.update(error=str(error), building=False, checked=requested)
                    cond.notify_all()
                    retry_at = time.monotonic() + interval
                    cond.wait_for(lambda: box['stopped'] or box['request'] > requested)
                    # Requests during an attempt coalesce into the next attempt. A
                    # burst cannot turn persistent failures into a hot retry loop.
                    while not box['stopped'] and time.monotonic() < retry_at:
                        cond.wait(timeout=retry_at - time.monotonic())
                continue
            refresh(inputs, signature, spend, cache, source_signature, permissions)
            return
    port = a.port
    srv = None
    deadline = time.monotonic() + (2 if a.open else 0)
    while True:
        try:
            srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
            break
        except OSError:
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
    if not srv:
        _m_common.die(f"port {port} is busy; no other port was opened (leave unrelated services running)")
    srv.daemon_threads = True
    port = srv.server_address[1]
    identity = {'pid': os.getpid(), 'version': version, 'root': str(inst.root.resolve()), 'write': reviews.enabled}
    _m_ui_server.ui_server_record(inst, port, {**identity, 'key': key})
    threading.Thread(target=initialize, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/?k={key}"
    print(f"brain ui core: {_m_common.tilde(_m_common.CORE)} @ source {version[:12]}", flush=True)
    mode = "local proposal reviews enabled" if reviews.enabled else "read-only"
    print(f"{st['brain']['title']} ({st['brain']['name']}): {url}  ({mode}, this machine only; Ctrl-C stops it)",
          flush=True)
    if a.open:
        import webbrowser
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        with cond:
            box['stopped'] = True
            cond.notify_all()
        srv.server_close()


# ---------- check ----------
