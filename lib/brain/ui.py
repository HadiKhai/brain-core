"""Ui mechanics for Brain."""
import argparse
import json
import os
import re
import shutil
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
from . import schedules as _m_schedules
from . import spend as _m_spend
from . import status as _m_status
from . import usage as _m_usage
from . import verify as _m_verify
from . import worktrees as _m_worktrees


UI_PORT = 4747
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


def ui_worktree(inst, home, branch, w, n=None):
    import socket as so
    path = Path(w["path"])
    d = {"branch": branch, "path": _m_common.tilde(path), "port": w.get("port"), "ports": w.get("ports", 1),
         "exists": path.exists(), "listening": [], "dirty": None, "ahead": None, "last": None, "wrote": None}
    for q in range(w.get("port") or 0, (w.get("port") or 0) + w.get("ports", 1)) if w.get("port") else []:
        with so.socket(so.AF_INET, so.SOCK_STREAM) as sk:
            sk.settimeout(0.05)
            if sk.connect_ex(("127.0.0.1", q)) == 0:
                d["listening"].append(q)
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
            d["mem_free_gb"], d["mem_total_gb"] = round((int(mi["MemFree"]) + int(mi["Inactive"])) * 1024 / 1e9, 1), round(int(mi["MemTotal"]) * 1024 / 1e9)
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
    return d


def ui_state(inst, problems=None, scan=None, spend_budget=2.0, machine_spend=None):
    with _m_spend.spend_read_only():
        return _ui_state(inst, problems, scan, spend_budget, machine_spend)


def _ui_state(inst, problems=None, scan=None, spend_budget=2.0, machine_spend=None):
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
        fm, loc = n["fm"], n["local"]
        st = n["dir"] / "status.md"
        secs = md_sections(st.read_text()) if st.exists() else {}
        u = _m_status.updated_at(inst, n)
        work = []
        for r in _m_status.active_work(n):
            m = LINK_RE.search(r[1] if len(r) > 1 else "")
            work.append({"branch": r[0], "thread": m.group(1) if m else (r[1] if len(r) > 1 else ""),
                         "link": m.group(2) if m else "", "runtime": (re.search(r"\((Claude|Codex|[A-Za-z]+)\)\s*$",
                         r[1]) or [None, ""])[1] if len(r) > 1 else "", "pr": r[2] if len(r) > 2 else "",
                         "state": r[3] if len(r) > 3 else ""})
        wts = []
        terms = _m_guard.never_terms(n)
        hide = lambda t: _m_guard.redact(t, terms) if terms and isinstance(t, str) else t  # noqa: E731
        if n["external"] and n["home_path"] and n["home_path"].exists():
            home = n["home_path"]
            wts = [{k: hide(v) for k, v in ui_worktree(inst, home, b, w, n).items()}
                   for b, w in sorted(loc.get("worktrees", {}).items())]
            for wt, branch in zip(wts, sorted(loc.get("worktrees", {}))):
                state = _m_verify.ready_state(inst, n, branch, display=True)
                wt["ready"] = {"ready": state["ready"], "reasons": [hide(x) for x in state["reasons"]],
                               "packet": {k: hide(v) for k, v in state["packet"].items() if k not in ("input_files", "input_files_ref")}
                               if state["packet"] else None}
            for c in ui_commits(home, since, all_refs=True):  # what comes from the home is shown as guard shows it
                commits.append({**c, "subject": hide(c["subject"]), "node": n["id"], "where": "home"})
        work = [{k: hide(v) for k, v in j.items()} for j in work]
        js = _m_jobs.jobs_summary(inst, n)
        for r in js["open"]:
            used = _m_spend.live_usage(inst, n, r)
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
        out_nodes.append({
            "id": n["id"], "title": fm.get("title", ""), "parent": n["parent"]["id"] if n["parent"] else None,
            "owners": n["owners"], "labels": _m_common.lst(fm.get("labels")), "runtime": fm.get("runtime", ""),
            "preview": hide(loc.get("preview", "")), "preview_tour": hide(loc.get("preview_tour", "")),
            "preview_url": hide(fm.get("preview_url", "")),
            "sealed": n["sealed"], "archived": n["archived"], "paused": _m_status.paused(n), "external": n["external"],
            "home": _m_common.tilde(n["home_path"]) if n["home_path"] else "",
            "coordinator": inst.thread_link(loc["coordinator"]) if loc.get("coordinator") else "",
            "updated": u.isoformat() if u else None, "updated_ago": _m_status.ago(inst, u),
            "status": {k: v for k, v in secs.items() if v and k.lower() != "active work"},
            "work": work, "worktrees": wts, "inbox": [l[2:] for l in _m_status.inbox_lines(n)],
            "blocked": _m_status.status_section(n, "blocked"), "decisions": ui_decisions(inst, n), "limits": _m_knowledge.limits_of(n),
            "never_name": len(terms), "spend": lookback,
            "jobs": {**js, "rows": [{k: hide(v) for k, v in r.items()} for r in (_m_jobs.read_jobs(n) or [])
                                  if r.get("Outcome") != "external"],
                                               "open": [{k: hide(v) for k, v in r.items()} for r in js["open"]],
                                               "forecasts": [{k: hide(v) for k, v in r.items()} for r in js["forecasts"]]},
            "reports": [{"name": d.parent.name, "title": next((l[2:] for l in d.read_text().splitlines()
                                                              if l.startswith("# ")), d.parent.name),
                         "path": _m_common.tilde(d)} for d in reps]})
    commits.sort(key=lambda c: c["t"], reverse=True)
    import time
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
        specialists.append(safe({"id": sp["id"], "node": sp["node"]["id"],
                                 "title": sp["fm"].get("title"), "runtime": sp["fm"].get("runtime"),
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
    due = [item for n in nodes for item in _m_status.due_items(inst, n)]
    branches = {(n["id"], _m_jobs.job_key(r)): r["Job"] for n in nodes for r in (_m_jobs.read_jobs(n) or [])}
    for item in due:  # the job's own name, for a card that otherwise reads "this job"
        if item.get("job") and (item["node"], item["job"]) in branches:
            item["branch"] = branches[(item["node"], item["job"])]
    machine_due = _m_status.machine_due_items(inst, nodes, machine, due)
    return {
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
                            **_m_status.blocked_metadata(inst, next(n for n in nodes if n["id"] == a), k, t)})
                      for a, k, t in _m_status.attention_items(inst, nodes)],
        "inbox": [{**i, "from": inbox_sender(i)} for i in _m_status.inbox_items(inst, nodes)[0]],
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
        self.value = {'complete': False, 'files': 0, 'read': 0, 'runtimes': {}, 'since_week': None}

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
        deadline = time.monotonic() + self.BUDGET
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
        self.slow_files = set()
        self.slow_at, self.slow_rows = float('-inf'), []
        self.trees = []
        self.git_dirs = set()
        self.source_trees = []
        for n in inst.nodes():
            loc = n['local']
            homes = [n['home_path']] if n['home_path'] else []
            homes += [Path(w['path']) for w in loc.get('worktrees', {}).values() if w.get('path')]
            for home in homes:
                # Git's index supplies tracked and non-ignored files; directory metadata detects additions.
                result = _m_common.git('ls-files', '-z', '--cached', '--others', '--exclude-standard', cwd=home)
                self.slow_files.add(home)
                for name in result.stdout.split('\0'):
                    if not name:
                        continue
                    path = home / name
                    self.slow_files.add(path)
                    while path.parent != home and path.parent != path:
                        path = path.parent
                        self.slow_files.add(path)
                for flag in ('--git-dir', '--git-common-dir'):
                    raw = _m_common.git('rev-parse', flag, cwd=home).stdout.strip()
                    if raw:
                        self.git_dirs.add((home / raw).resolve())
            try:
                reports = _m_worktrees.workbench(inst, n) / 'reports'
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

    def signature(self):
        paths = set(self.files)
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
        rows = [self.stamp(path) for path in paths]
        if time.monotonic() - self.slow_at >= 15:
            self.slow_rows = [self.stamp(path) for path in self.slow_files - paths]
            self.slow_at = time.monotonic()
        rows.extend(self.slow_rows)
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
    inst = _m_nodes.load()
    # Scan only explicit startup directories, once. Reuse the CLI's read-only discovery.
    scan = []
    if a.scan:
        import contextlib
        import io
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            _m_nodes.cmd_scan(argparse.Namespace(dirs=a.scan, depth=4, json=True))
        scan = json.loads(output.getvalue())
    key = secrets.token_urlsafe(18)  # other accounts on this machine can reach 127.0.0.1 too; they don't get this
    if a.json:
        print(json.dumps(ui_state(inst, len(_m_check.collect_problems(inst)[0]), scan, spend_budget=None), indent=1))
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
           'telemetry': time.monotonic(), 'request': 0, 'checked': 0}

    def touch():
        with cond:
            box['last_poll'] = time.monotonic()
            box['request'] += 1
            requested = box['request']
            cond.notify_all()
            # An idle poll must receive the newly checked snapshot, not the previous poll's data.
            cond.wait_for(lambda: box['checked'] >= requested, timeout=30)

    def publish(st):
        with cond:
            box.update(state=st, json=json.dumps(st, sort_keys=True).encode(), version=box['version'] + 1)
            cond.notify_all()

    def refresh(inputs, signature, spend):
        source_signature = inputs.source_signature()
        metadata_at = float('-inf')
        live_at, live_revision = time.monotonic(), spend.revision
        while True:
            with cond:
                # Idle/background clients poll each minute. Keep their small aggregation slices
                # moving between polls; a closed/no-client server sleeps after the lease expires.
                while not box['clients'] and time.monotonic() - box['last_poll'] > 75:
                    cond.wait()
                requested = box['request']
            try:
                cadence = interval if box['clients'] else max(interval, 15)
                if requested > box['checked'] or time.monotonic() - metadata_at >= cadence:
                    changed = inputs.signature()
                    if changed != signature:
                        i = _m_instance.Instance(inst.root)
                        problems = len(_m_check.collect_problems(i)[0])
                        spend.inst = i
                        spend.discover()
                        spend.dirty, spend.published = True, float('-inf')
                        spend.advance()
                        st = ui_state(i, problems, scan, machine_spend=spend.value)
                        signature, inputs = changed, UIInputs(i)
                        publish(st)
                    sources = inputs.source_signature()
                    if sources != source_signature:
                        spend.discover()
                        source_signature = sources
                    metadata_at = time.monotonic()
                if spend.advance():
                    publish({**box['state'], 'spend': spend.value})
                if spend.revision != live_revision and time.monotonic() - live_at >= 60:
                    i = _m_instance.Instance(inst.root)
                    nodes = {n['id']: n for n in i.nodes()}
                    updated = []
                    with _m_spend.spend_read_only():
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
                            updated.append({**n, 'jobs': {**n['jobs'], 'open': rows}})
                    publish({**box['state'], 'nodes': updated})
                    live_at, live_revision = time.monotonic(), spend.revision
                if time.monotonic() - box['telemetry'] >= 30:
                    publish({**box['state'], 'brain': {**box['state']['brain'], 'now': inst.now_dt().isoformat(timespec='seconds')},
                             'machine': _m_machine.machine_state(inst)})
                    box['telemetry'] = time.monotonic()
            except Exception as e:
                box['error'] = str(e)
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

        def send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

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
            if url.path == "/api/state":
                touch()
                return self.send(200, box["json"], "application/json")
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
                            self.wfile.write(b"data: " + body + b"\n\n")
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

    spend = UISpend(inst)
    spend.discover()
    spend.advance()
    st = ui_state(inst, len(_m_check.collect_problems(inst)[0]), scan, machine_spend=spend.value)
    box.update(json=json.dumps(st, sort_keys=True).encode(), version=1, state=st)
    inputs = UIInputs(inst)
    threading.Thread(target=refresh, args=(inputs, inputs.signature(), spend), daemon=True).start()
    port = a.port
    srv = None
    for p_ in range(port, port + 20):
        try:
            srv = http.server.ThreadingHTTPServer(("127.0.0.1", p_), H)
            break
        except OSError:
            continue
    if not srv:
        _m_common.die(f"no free port from {port}")
    srv.daemon_threads = True
    url = f"http://127.0.0.1:{srv.server_address[1]}/?k={key}"
    print(f"brain ui core: {core_revision()}", flush=True)
    print(f"{st['brain']['title']} ({st['brain']['name']}): {url}  (read-only, this machine only; Ctrl-C stops it)",
          flush=True)
    if a.open:
        import webbrowser
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


# ---------- check ----------
