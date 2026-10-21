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


def ui_worktree(inst, home, branch, w):
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
        if w.get("base"):
            c = _m_common.git("rev-list", "--count", f"{w['base']}..{branch}", cwd=home).stdout.strip()
            d["ahead"] = int(c) if c.isdigit() else None
        t = _m_common.git("log", "-1", "--format=%ct", branch, cwd=home).stdout.strip()
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


def ui_state(inst, problems=None, scan=None, spend_budget=2.0):
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
            wts = [{k: hide(v) for k, v in ui_worktree(inst, home, b, w).items()}
                   for b, w in sorted(loc.get("worktrees", {}).items())]
            for wt, branch in zip(wts, sorted(loc.get("worktrees", {}))):
                state = _m_verify.ready_state(inst, n, branch)
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
        "inbox": _m_status.inbox_items(inst, nodes)[0],
        "commits": commits[:300], "hours": UI_HOURS, "activity": {"start": start, "counts": activity},
        "commits_24h": sum(1 for c in commits if c["t"] > time.time() - 86400), "commits_total": len(commits),
        "specialists": specialists,
        "items": {"count": len(items), "all": library, "recent": [safe({"id": i["fm"].get("id"), "title": i["fm"].get("title"),
                                                   "source": i["fm"].get("source_node"),
                                                   "at": str(i["fm"].get("learned_at", ""))}) for i in items[:6]]},
        "schedules": [{"id": x["id"], "when": x.get("when", ""), "node": x.get("node", ""),
                       "here": x["id"] in inst.local.get("schedules", {})} for x in _m_schedules.read_schedules(inst)],
        "machine": machine,
        "spend": _m_spend.spend_machine(inst, budget=spend_budget),  # the live view counts a little at each refresh
        "usage": _m_usage.usage_ui(inst),
        "usage_other_subscriptions": _m_usage.usage_other_count(inst),
        "problems": problems,
    }


def cmd_ui(a):
    """A live view of this brain in a browser on this machine: read-only, listening on 127.0.0.1 only."""
    import hmac
    import http.server
    import secrets
    import threading
    import urllib.parse
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
    page = (_m_common.CORE / "ui" / "index.html").read_bytes()
    lock, cond = threading.Lock(), threading.Condition()
    box = {"json": b"", "version": 0, "problems": None, "checked": 0.0}

    def refresh():
        import time
        while True:
            try:
                i = _m_instance.Instance(inst.root)
                if time.time() - box["checked"] > 30:  # the full check is slower; once every 30 s is enough
                    box["problems"], box["checked"] = len(_m_check.collect_problems(i)[0]), time.time()
                st = ui_state(i, box["problems"], scan)
                body = json.dumps(st, sort_keys=True).encode()
                plain = json.dumps({**st, "brain": {**st["brain"], "now": ""}, "machine": {}}, sort_keys=True)
                with cond:
                    if plain != box.get("plain") or time.time() - box.get("pushed", 0) > 30:  # machine figures, too
                        box["pushed"] = time.time()
                        box.update(json=body, plain=plain, version=box["version"] + 1)
                        cond.notify_all()
                    else:
                        box["json"] = body
            except Exception as e:  # keep serving the last good state
                box["error"] = str(e)
            time.sleep(a.every)

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
                return self.send(200, page, "text/html; charset=utf-8")  # the page holds no data; the API does
            if url.path.startswith("/api/") and not hmac.compare_digest(given, key):
                return self.send(403, b"this run's key is missing: open the address brain ui printed", "text/plain")
            if url.path == "/api/state":
                return self.send(200, box["json"], "application/json")
            if url.path == "/api/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                seen = -1
                try:
                    while True:
                        with cond:
                            if box["version"] == seen:
                                cond.wait(timeout=15)
                            v, body = box["version"], box["json"]
                        if v != seen:
                            self.wfile.write(b"data: " + body + b"\n\n")
                            seen = v
                        else:
                            self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
            self.send(404, b"not found", "text/plain")

    box["problems"] = len(_m_check.collect_problems(inst)[0])
    import time
    box["checked"] = time.time()
    st = ui_state(inst, box["problems"], scan)
    box.update(json=json.dumps(st, sort_keys=True).encode(), version=1)
    threading.Thread(target=refresh, daemon=True).start()
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
