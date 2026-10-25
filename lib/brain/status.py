"""Status mechanics for Brain."""
import datetime
import hashlib
import json
import re
import sys
import urllib.parse
from pathlib import Path

from . import briefs as _m_briefs
from . import check as _m_check
from . import common as _m_common
from . import docker_leftovers as _m_docker_leftovers
from . import guard as _m_guard
from . import history as _m_history
from . import hosts as _m_hosts
from . import instance as _m_instance
from . import jobs as _m_jobs
from . import knowledge as _m_knowledge
from . import machine as _m_machine
from . import nodes as _m_nodes
from . import proposals as _m_proposals
from . import schedules as _m_schedules
from . import rotation as _m_rotation
from . import reminders as _m_reminders
from . import spend as _m_spend
from . import transitions as _m_transitions
from . import upgrade as _m_upgrade
from . import usage as _m_usage
from . import verify as _m_verify
from . import worktrees as _m_worktrees


def status_line(n):
    s = n["dir"] / "status.md"
    text = s.read_text() if s.exists() else ""
    # Updated belongs to coordinator judgement, never to a generated facts refresh.
    text = FACTS_RE.sub("", text)
    m = re.search(r"^Updated: .+$", text, re.M)
    return m.group() if m else "(no Updated line)"


def updated_at(inst, n):
    m = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?(?: ?(?:Z|[+-]\d{2}:?\d{2}))?)", status_line(n))
    return inst.parse_time(m.group(1)) if m else None


def future_entries(inst, n):
    """Entry timestamps are evidence; planned dates in prose and examples are not."""
    problems, warnings = [], []
    now = inst.now_dt()
    date = r"\b\d{4}-\d{2}-\d{2}"
    clock = r"\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?"
    offset = r"[+-]\d{2}:\d{2}"
    # Attached offsets belong to ISO T timestamps; a space-form 09:00-10:30 is a range.
    pattern = date + rf"(?:T{clock}(?:Z|{offset})?| {clock}(?: ?Z| {offset})?)?"
    for f in sorted(n["dir"].glob("*.md")):
        if f.is_symlink() or not f.is_file():
            continue
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        text = re.sub(r"<!-- brain:facts v1 -->.*?<!-- /brain:facts -->|<!--.*?-->",
                      lambda m: "\n" * m.group().count("\n"), text, flags=re.S)
        fence = None
        for line_no, line in enumerate(text.splitlines(), 1):
            marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
            if marker:
                run, suffix = marker.groups()
                if fence is None:
                    fence = run
                elif run[0] == fence[0] and len(run) >= len(fence) and not suffix.strip():
                    fence = None
                continue
            if fence:
                continue
            m = re.match(r"^ {0,3}(?:Updated:\s*|#{1,6}\s+|[-*+]\s+)(" + pattern + r")(?=$|[\s:])", line)
            if not m:
                continue
            stamp = m.group(1)
            when = inst.parse_time(stamp if len(stamp) > 10 else stamp + " 00:00")
            if when and elapsed(now, when) > 5 * 60:
                updated = f.name == "status.md" and bool(re.match(r"^Updated:\s*$", line[:m.start(1)]))
                target = problems if updated else warnings
                target.append(f"{n['id']}: {f.name}:{line_no}: future "
                              + ("Updated: time; fix it with brain now" if updated else "dated entry"))
    return problems, warnings


def elapsed(a, b):
    """Seconds from a to b as real moments. Two times with the same zone object subtract by their wall clocks
    in Python, which is wrong across a daylight-saving change; in UTC it isn't."""
    utc = datetime.timezone.utc
    return (b.astimezone(utc) - a.astimezone(utc)).total_seconds()


def ago(inst, d):
    if not d:
        return "never"
    s = elapsed(d, inst.now_dt())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= size:
            return f"{int(s // size)}{unit} ago"
    return "just now"


def status_section(n, heading, physical=False):
    """Bullet lines under `## <heading>` in a node's status.md."""
    f = n["dir"] / "status.md"
    if not f.exists():
        return []
    out, inside = [], False
    for line in re.sub(r"<!--.*?-->", "", f.read_text(), flags=re.S).splitlines():
        if line.startswith("## "):
            inside = line[3:].strip().lower().startswith(heading.lower())
        elif inside and line.startswith("- ") and line[2:].strip():
            out.append(line[2:].strip())
        elif not physical and inside and out and line.startswith("  ") and line.strip():
            out[-1] += " " + line.strip()  # a wrapped bullet
    return out



NOW_LINE_LIMIT = 30
BLOCKED_HOURS = 48


def code_node(n):
    return bool(n["fm"].get("stack")) or any(
        l in ("type:code", "kind:code") for l in _m_common.lst(n["fm"].get("labels"))) or bool(
        n["external"] and n["home_path"] and (n["home_path"] / ".git").exists())


def code_setup_warnings(inst, n):
    if "kind:study" in _m_common.lst(n["fm"].get("labels")) or not code_node(n) or n["archived"]:
        return []
    out = []
    if not n["fm"].get("verify", "").strip():
        out.append(f"{n['id']}: code node needs verify=<command> before its first job; brain set {n['id']} 'verify=<command>'")
    bound = inst.local.get("schedules", {})
    ticks = [x for x in _m_schedules.read_schedules(inst) if x.get("node") == n["id"]]
    target, wake = _m_hosts.wake_target(inst, n)
    bound_wake = bool(n["local"].get("coordinator") and target["id"] == n["id"] and wake.strip())
    if not bound_wake and (not ticks or (n["local"].get("coordinator") and not any(x["id"] in bound for x in ticks))):
        out.append(f"{n['id']}: code node needs a coordinator tick before its first job; schedule a host interval, "
                   "brain schedule add, then brain schedule bind (adapters/hosts/t3.md)")
    return out


def empty_blocked(text):
    return bool(re.fullmatch(r"\(?\s*(?:none|nothing)(?:\s+(?:right now|blocked|currently|yet|at present))?[.!]?\s*\)?", text, re.I))


def blocked_entries(inst, n, file_only=False):
    """Stable source text, with age kept separate so a ticking clock cannot re-notify."""
    if n["archived"]:
        return []
    source = n["dir"] / "status.md"
    # The Instance lives for one CLI command/UI snapshot. No global or cross-snapshot cache:
    # edits, policy changes and advancing age are re-read on the next instance.
    stamp = source.stat() if source.exists() else None
    now = inst.now_dt()
    cache = getattr(inst, "_blocked_entries", {})
    key = (n["id"], stamp.st_mtime_ns if stamp else None, stamp.st_size if stamp else None, now.isoformat())
    # Cache source parsing/history separately from the clock-derived age.
    source_key = (*key[:3], file_only)
    parsed = cache.get(source_key)
    if parsed is not None:
        return [{**b,
                 "age_hours": round(max(0, elapsed(inst.parse_time(b["since"]), now) / 3600), 2) if b["since"] else None,
                 "escalated": bool(b["since"] and elapsed(inst.parse_time(b["since"]), now) > BLOCKED_HOURS * 3600)}
                for b in parsed]
    out = []
    first_lines = status_section(n, "blocked", physical=True)
    for index, text in enumerate(status_section(n, "blocked")):
        if empty_blocked(text):
            continue
        fields = dict((m.group(1).lower(), m.group(2).strip()) for m in re.finditer(
            r"(?:^|;\s*)(since|recommendation|default):\s*([^;]*)", text, re.I))
        since = inst.parse_time(fields.get("since", ""))
        # Legacy entries still age: use this bullet's introduction, never the status Updated line.
        if not since and not file_only:
            needle = first_lines[index]
            history = _m_common.git("log", "--reverse", "--format=%cI", "-S", needle, "--",
                          str(source.relative_to(inst.root)), cwd=inst.root).stdout.splitlines()
            since = inst.parse_time(history[0]) if history else datetime.datetime.fromtimestamp(
                source.stat().st_mtime, inst.zone())
        # File-only bootstrap cannot infer when a legacy bullet appeared.
        hours = max(0, elapsed(since, inst.now_dt()) / 3600) if since else None
        out.append({"text": text, "since": since.isoformat(timespec="seconds") if since else None,
                    "age_hours": round(hours, 2) if hours is not None else None,
                    "escalated": hours is not None and hours > BLOCKED_HOURS,
                    "recommendation": fields.get("recommendation", ""), "default": fields.get("default", ""),
                    "missing": [k for k in ("since", "recommendation", "default")
                                if not fields.get(k) or (k == "since" and not inst.parse_time(fields[k]))]})
    cache[source_key] = out
    inst._blocked_entries = cache
    return out


def blocked_metadata(inst, n, kind, text, file_only=False):
    if kind not in ("blocked", "blocked-overdue"):
        return {}
    return next(({k: _m_nodes.import_text(v, _m_guard.never_terms(n)) if isinstance(v, str) else v
                  for k, v in b.items() if k not in ("text", "missing")}
                 for b in blocked_entries(inst, n, file_only=file_only) if b["text"] == text), {})


def dead_note_paths(inst, n):
    """Explicit Markdown paths only; ambiguous package names/refs/prose stay judgement."""
    out = []
    extensions = set(_m_nodes.LANGUAGES) | {".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
                                   ".csv", ".tsv", ".sql", ".html", ".css", ".png", ".svg", ".pdf", ".lock"}
    filenames = {"README.md", "CHANGES.md", "AGENTS.md", "MODEL.md", "Dockerfile", "Makefile", "LICENSE"}
    node_files = {"node.md", "brief.md", "status.md", "knowledge.md", "decisions.md", "backlog.md",
                  "inbox.md", "direction.md", "job-ledger.md", "never-name.txt", "role.md",
                  "lessons.md", "handover.md", "connections.md", "AGENTS.md", "CLAUDE.md",
                  "worktree-setup.sh"}
    node_folders = {"sources", "library", "specialists", "job-contracts", "job-handbacks", "job-trains"}
    root_files = {"instance.md", "registry.md"}
    for name in ("brief.md", "status.md", "knowledge.md"):
        source = n["dir"] / name
        if not source.is_file():
            continue
        try:
            text = source.read_text()
        except (OSError, UnicodeError):
            out.append(f"{n['id']}: {name}: cannot check paths in unreadable Markdown")
            continue
        # Keep line numbers while hiding generated content and template comments.
        text = re.sub(r"<!-- brain:facts v1 -->.*?<!-- /brain:facts -->|<!--.*?-->",
                      lambda m: "\n" * m.group().count("\n"), text, flags=re.S)
        for number, line in enumerate(text.splitlines(), 1):
            candidates = [(v, False) for v in re.findall(r"`([^`\n]+)`", line)]
            candidates += [(v, True) for v in re.findall(r"\[[^\]]*\]\(([^)]+)\)", line)]
            for raw, link in candidates:
                value = raw
                if link:
                    match = re.match(r'^<([^>]+)>|^(\S+)', value)
                    value = (match.group(1) or match.group(2)) if match else ""
                value = value.split("#", 1)[0]
                value = re.sub(r":\d+(?::\d+)?$", "", value)
                if not value or re.search(r"[?*{}$]|<[^>]*>|://|^[a-zA-Z][a-zA-Z0-9+.-]*:", value):
                    continue
                if link:
                    value = urllib.parse.unquote(value)
                elif re.search(r"\s", value):
                    continue
                path = Path(value).expanduser()
                # A real filesystem root component keeps absolute Brain paths checkable.
                # Unknown slash-prefixed components are route examples, such as /api/v1.
                if not link and value.startswith("/") and not Path(*path.parts[:2]).exists():
                    continue
                explicit = value.startswith(("./", "../", "/", "~")) or value.endswith("/")
                file_shape = path.name in filenames or ("/" in value and path.suffix in extensions)
                if not link and not (explicit or file_shape):
                    continue
                # Links belong to the note. Inline project paths belong to its home, while
                # explicit Brain references must still warn even on an unbound machine.
                if link or value.startswith("../"):
                    bases = [n["dir"]]
                elif path.as_posix() in root_files:
                    bases = [inst.root]
                elif path.parts and path.parts[0] in {"core", "nodes", ".brain"}:
                    bases = [n["dir"], inst.root]
                elif path.as_posix() in node_files or (path.parts and path.parts[0] in node_folders):
                    bases = [n["dir"]]
                else:
                    bases = [n["home_path"] if n["external"] else n["dir"]]
                if bases == [None] and not path.is_absolute():
                    continue
                if not any((path if path.is_absolute() else base / path).exists() for base in bases):
                    out.append(f"{n['id']}: {name}:{number}: dead path {raw}; repair the reference or restore the file/folder")
    return sorted(set(out))


def active_work_closed(row):
    """A terminal label may have a note after a spaced dash or a colon."""
    return len(row) > 3 and bool(re.fullmatch(
        r"(?:merged|closed|done|abandoned)(?:(?:[ \t]+[—–-]|:)[ \t]+.+)?",
        row[3].strip().casefold()))


def active_work(n, include_closed=False):
    """Rows of the 'Active work' table in status.md (branch, thread, PR, state), minus the header."""
    f = n["dir"] / "status.md"
    if not f.exists():
        return []
    rows, inside = [], False
    for line in f.read_text().splitlines():
        if line.startswith("## "):
            inside = line[3:].strip().lower().startswith("active work")
        elif inside and line.startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if cells and not set("".join(cells)) <= set("-: ") and cells[0].lower() != "branch":
                rows.append(cells)
    return [r for r in rows if include_closed or not active_work_closed(r)]


def overdue_questions(inst, n, lines):
    try:
        minutes = max(1, float(n["fm"].get("question_minutes", 10)))
    except (ValueError, TypeError):
        minutes = 10
    for line in lines:
        if not ("<!-- host-question:" in line or "<!-- handback:" in line):
            continue
        match = re.match(r"- (.*?) from ", line)
        at = inst.parse_time(match[1]) if match else None
        if at and elapsed(at, inst.now_dt()) >= minutes * 60:
            yield line


def hook_question_queue(n):
    root = _m_instance.instance_above(n["dir"])
    return root / ".brain" / "host-questions" / urllib.parse.quote(n["id"], safe="")


def machine_transition_line(n, line):
    """Recognize only the retired autonomous writer, never a question or worker signal."""
    return (n['id'] == 'root' and not re.search(r'<!-- (?:host-question|handback|worker-failure):', line)
            and re.fullmatch(r'- .*? from brain: (?:hold heavy work: sustained .*|heavy work hold cleared) '
                             r'\[machine episode [0-9a-f]{24} (?:open|close)\]', line) is not None)


def inbox_lines(n, readonly=False, lifecycle_data=None):
    f = n["dir"] / "inbox.md"
    # Filter the historical record on read. Never rewrite or discard episode history.
    lines = [l for l in f.read_text().splitlines() if l.startswith("- ")
             and not machine_transition_line(n, l)] if f.exists() else []
    # A busy inbox never loses a stopped turn's question. The private queue is also an inbox
    # source until it can be appended; reading due/inbox retries delivery without waiting.
    queue = hook_question_queue(n)
    if not queue.exists() or _m_common.outward_links(_m_instance.instance_above(n["dir"])):
        root = _m_instance.instance_above(n['dir'])
        return _m_reminders.request_lines(_m_instance.Instance(root), n, lines, readonly=readonly,
                                          lifecycle_data=lifecycle_data)
    for pending in sorted(queue.glob("*.json")):
        try:
            value = json.loads(pending.read_text())
            line, marker = value["line"], value["marker"]
            if not isinstance(line, str) or not isinstance(marker, str):
                continue
            if not any(marker in old for old in lines):
                lines.append(line.rstrip("\n"))
            if not readonly and not _m_common.outward_links(_m_instance.instance_above(n["dir"])):
                _m_transitions.inbox_append(n["dir"], line, marker, nonblocking=True)
                pending.unlink()
        except (OSError, ValueError, KeyError, TypeError, SystemExit):
            continue
    root = _m_instance.instance_above(n['dir'])
    return _m_reminders.request_lines(_m_instance.Instance(root), n, lines, readonly=readonly,
                                      lifecycle_data=lifecycle_data)


# ---------- coordinator's mechanical queue and generated status ----------

FACTS_START = "<!-- brain:facts v1 -->"
FACTS_END = "<!-- /brain:facts -->"
FACTS_RE = re.compile(re.escape(FACTS_START) + r".*?" + re.escape(FACTS_END), re.S)


def facts_meta(n):
    f = n["dir"] / "status.md"
    block = FACTS_RE.search(f.read_text()) if f.exists() else None
    m = re.search(r"<!-- brain:facts-state (.*?) -->", block.group()) if block else None
    try:
        data = json.loads(m.group(1)) if m else {}
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def local_job_state(n, branch, w):
    """Only files and local git refs. Unlike worktree_state, never asks gh or a remote."""
    home = n["home_path"]
    exists = Path(w["path"]).is_dir()
    if not home or not home.exists():
        return {"exists": exists, "tip": "", "merged": False}
    tip = _m_common.git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", cwd=home).stdout.strip()
    target = _m_worktrees.integration_target(n)
    merged = bool(tip and tip != w.get("start") and target) and \
        _m_common.git("merge-base", "--is-ancestor", tip, target, cwd=home).returncode == 0
    return {"exists": exists, "tip": tip, "merged": merged}


def due_evidence(inst, n):
    rows = _m_jobs.read_jobs(n)
    wts = {b: {**w, **local_job_state(n, b, w)} for b, w in sorted(n["local"].get("worktrees", {}).items())}
    from . import job_attempts as _m_attempts
    projections = {_m_jobs.job_key(r): _m_attempts.job_projection(inst, n, r) for r in rows or []}
    signals = {key: value['signal'] for key, value in projections.items()}
    # Node's own commits only, excluding status (including its generated refresh commit) and children.
    names = ["brief.md", "decisions.md", "backlog.md", "node.md", "inbox.md", "sources", "AGENTS.md", _m_jobs.JOBS_FILE,
             "job-handbacks", "job-attempts", "handback-queue"]
    paths = [str((n["dir"] / name).relative_to(inst.root)) for name in names]
    last = _m_common.git("log", "-1", "--format=%H %cI", "--", *paths, cwd=inst.root).stdout.strip()
    state = {"jobs": rows, "worktrees": wts, "handbacks": signals, "job_projections": projections, "inbox": inbox_lines(n)}
    # Hash exactly the generated display, apart from its refresh time and metadata. Retain
    # the last refresh's merge baseline so its "since previous" snapshot stays comparable.
    lines, _ = status_facts(inst, n, state, facts_meta(n).get("merged_before", []))
    return state, _m_jobs.digest(lines), last


def status_lag(inst, n, evidence=None):
    state, fingerprint, last = evidence or due_evidence(inst, n)
    previous = facts_meta(n)
    if previous:
        return "facts differ from current files or git" if previous.get("fingerprint") != fingerprint else None
    if not (any(not r["Ended"] for r in state["jobs"] or []) or state["worktrees"]):
        return None
    u = updated_at(inst, n)
    if not u:
        return "open jobs have no status timestamp"
    times = [inst.parse_time(last.split(" ", 1)[1])] if " " in last else []
    times += [inst.parse_time(r.get("Ended") or r.get("Started")) for r in state["jobs"] or []]
    times += [inst.parse_time(s.get("at")) for s in state["handbacks"].values() if s]
    home = n["home_path"]
    if n["external"] and home and home.exists():
        t = _m_common.git("log", "-1", "--branches", "--format=%cI", cwd=home).stdout.strip()
        times.append(inst.parse_time(t))
    # Legacy Updated lines have minute precision. Don't make the status commit stale itself.
    if any(t and elapsed(u, t) >= 60 for t in times) or elapsed(u, inst.now_dt()) > _m_hosts.REMIND_MINUTES * 60:
        return "status predates job activity or is over an hour old with jobs open"
    return None


def parallel_job_limit(n):
    value = str(n['fm'].get('parallel_jobs', '1'))
    if not re.fullmatch(r'[1-9][0-9]*', value):
        raise ValueError('parallel_jobs must be a positive integer')
    return int(value)


def runnable_work(n, rows):
    """Explicit backlog tasks only; prose and other sections grant no work authority."""
    if rows is None or paused(n):
        return []  # an unreadable/legacy ledger cannot establish spare capacity
    try:
        limit = parallel_job_limit(n)
    except ValueError:
        return []  # brain check reports the invalid setting; never guess capacity
    open_rows = [r for r in rows if not r['Ended']]
    opened = {r['Job'] for r in open_rows}
    slots = limit - len(open_rows)
    path = n['dir'] / 'backlog.md'
    if slots <= 0 or not path.exists():
        return []
    tasks, section_depth, fence, parents = [], None, None, []
    text = re.sub(r'<!--.*?-->', '', path.read_text(), flags=re.S)
    for line in text.expandtabs(4).splitlines():
        marker = re.match(r'^ *(`{3,}|~{3,})(.*)$', line)
        if marker:
            run, suffix = marker.groups()
            if fence is None:
                fence = run
            elif run[0] == fence[0] and len(run) >= len(fence) and not suffix.strip():
                fence = None
            continue
        if fence:
            continue
        heading = re.fullmatch(r' {0,3}(#{1,6}) +(.+?) *#* *', line)
        if heading:
            depth, title = len(heading[1]), heading[2].strip().casefold()
            if title == 'build order':
                section_depth = depth
                parents = []
            elif section_depth is not None and depth <= section_depth:
                section_depth, parents = None, []
            continue
        match = re.fullmatch(r'( *)(?:[-+*]|[0-9]{1,9}[.)]) +(.+)', line)
        if section_depth is None or not match:
            continue
        indent, body = len(match[1]), match[2].strip()
        while parents and parents[-1][0] >= indent:
            parents.pop()
        checkbox = re.fullmatch(r'\[([ xX])\] +(.+)', body)
        task = checkbox[2].strip() if checkbox else body
        # Code spans delimit full branch identities, including legal punctuation.
        # In plain text whitespace/brackets delimit them; a final sentence period
        # is ignored. Use code spans when punctuation would otherwise be ambiguous.
        spans = re.findall(r'`+([^`]+)`+', task)
        plain = re.sub(r'`+[^`]+`+', '', task)
        branches = [span.strip() for span in spans if span.strip().startswith('job/')]
        branches += [b.rstrip('.') for b in re.findall(r"(?<![\w/.-])job/[^\s<>()\[\]`]+", plain)]
        blocked = bool((parents and parents[-1][1]) or
                       (checkbox and checkbox[1] != ' ') or
                       re.search(r'\(\s*(?:parked\s*:[^)]*|waits\s*:\s*operator\s*)\)', task, re.I) or
                       any(branch in opened for branch in branches))
        parents.append((indent, blocked))
        if not checkbox or blocked:
            continue
        tasks.append(task)
        if len(tasks) == slots:
            break
    return tasks


def due_items(inst, n, evidence=None, include_stale=True, include_ready=True, include_advice=True,
              include_handled=False):
    if n["archived"]:
        return []
    evidence = evidence or due_evidence(inst, n)
    state, _, _ = evidence
    items = []
    def add(kind, text, **extra):
        items.append({"node": n["id"], "kind": kind, "text": _m_guard.redact(text, _m_guard.never_terms(n)), **extra})
    if n['id'] == 'root':
        from . import coordinator_advisories as _m_coordinator_advisories
        items.extend(_m_coordinator_advisories.coordinator_advisories(inst))
    snapshot = _m_proposals.pending_rule_snapshot(inst, n)
    if snapshot:
        episode = _m_jobs.digest([n['id'], 'rule-activation-review', snapshot['digest']])
        add('rule-activation-review', f"activate the home's {snapshot['count']} imported rule(s)? "
            f"Coordinator review of complete snapshot {snapshot['digest']} in {snapshot['path']} required: brain proposals {n['id']}",
            episode=episode, snapshot=snapshot['digest'])
    runnable = runnable_work(n, state['jobs'])
    if runnable:
        add('idle-with-runnable-work', 'start local work from Build order: ' + '; '.join(runnable))
    rotation = _m_rotation.rotation_due(inst, n)
    if rotation:
        add("rotate", rotation)
    for blocked in blocked_entries(inst, n):
        if blocked["escalated"]:
            add("blocked-overdue", blocked["text"], **blocked_metadata(inst, n, "blocked-overdue", blocked["text"]))
    if missing_preview(n, state):
        add("missing-preview", "code node has merged jobs but no recorded preview; run a local preview and screenshot tour, "
            "then record preview=<URL/path> and preview_tour=<path> with brain set",
            episode=_m_jobs.digest([sorted(_m_jobs.job_key(r) for r in state['jobs'] or []
                                          if r.get('Ended') and r.get('Outcome') == 'merged'),
                                    sorted((b, w.get('tip')) for b, w in state['worktrees'].items() if w.get('merged'))]))
    if state["jobs"] is None:
        add("ledger", "job-ledger.md is a legacy file; reconcile jobs without overwriting it")
    unsettled = _m_jobs.unsettled_jobs(state["jobs"])
    if unsettled:
        add("unsettled-threads", f"{len(unsettled)} closed job(s) with unsettled threads; inspect brain jobs {n['id']}; "
            "find each job's threads through the host, check safety, settle, then record --branch <branch> --settled",
            count=len(unsettled))
    overlaps = _m_jobs.job_overlaps(n, state["jobs"] or [])
    for row in state["jobs"] or []:
        if _m_jobs.job_key(row) in overlaps:
            add("overlapping-inputs", row["Job"] + ": " + overlaps[_m_jobs.job_key(row)], job=_m_jobs.job_key(row))
        if row["Ended"]:
            if row["Outcome"] == "merged" and row.get("Lesson check") == "required" and not row.get("Lessons"):
                add("lesson-outcome", f"{row['Job']}: merged job needs a lesson outcome; learn proposed lessons "
                    f"and record item ids or none: <reason> with brain jobs {n['id']} --branch {row['Job']} --lessons; "
                    + _m_jobs.handback_detail(n, row), job=_m_jobs.job_key(row), condition='missing-lesson-outcome')
            continue
        branch = row["Job"]
        w = state["worktrees"].get(branch)
        signal = state["handbacks"][_m_jobs.job_key(row)]
        from . import job_attempts as _m_attempts
        projection = state.get('job_projections', {}).get(_m_jobs.job_key(row)) or _m_attempts.job_projection(inst, n, row)
        signal = projection['signal']
        if w and w["merged"]:
            add("merged-job", f"{branch}: merged in local git; record brain jobs {n['id']} --branch {branch} --lessons "
                "first, then close the ledger and prune; " + _m_jobs.handback_detail(n, row),
                job=_m_jobs.job_key(row), condition='close-merged-ledger', episode=w.get('tip'))
        elif (signal and signal.get("restart") == "compactions" and signal["state"] == "blocked"
              and not _m_briefs.restart_acknowledged(n, row, signal)):
            command = _m_briefs.restart_command(n, row)
            add("restart", f"{branch}: restart: compactions; resume with `{command}`",
                job=_m_jobs.job_key(row), state=signal["state"])
        elif signal and not _m_briefs.restart_acknowledged(n, row, signal):
            ready = _m_verify.ready_state(inst, n, branch, display=True) if include_ready and signal["state"] == "done" and not _m_briefs.import_job(branch) else None
            detail = ("ready to merge" if ready["ready"] else
                      "finished but not ready (" + "; ".join(ready["reasons"]) + ")") if ready else (signal["state"] + "; node study, no home ready evidence required"
                                           if _m_briefs.import_job(branch) else signal["state"])
            summary = signal.get('summary') or 'summary missing (legacy)'
            add("handback", f"{branch}: {detail}; {summary}; review report {signal.get('report', '')} and record the next step",
                job=_m_jobs.job_key(row), state=signal["state"], ready=bool(ready and ready["ready"]),
                reasons=ready["reasons"] if ready else signal.get('ready_reasons', []),
                episode=projection['episode_id'],
                need=_m_jobs.digest({k: signal.get(k) for k in
                                     ('state', 'summary', 'question', 'report', 'lessons', 'restart')}),
                protected=bool(signal.get('restart') or signal.get('question') or
                               any(f'from worker {branch}: blocked:' in line and '<!-- handback:' in line
                                   for line in state['inbox'])))
        elif not signal and projection['attempt'] and projection['attempt']['state'] != 'terminal':
            add('handback-attempt', f"{branch}: {projection['state']}; {projection['next_action']}; prior hand-back is historical",
                job=_m_jobs.job_key(row), state=projection['state'], episode=projection['attempt']['id'],
                informational=projection['state'] in ('queued', 'verifying'))
        elif not signal and _m_jobs.handback_file(n, row).exists():
            add("invalid-handback", f"{branch}: unreadable worker signal; ask the worker to write it again", job=_m_jobs.job_key(row))
        if not _m_briefs.import_job(branch) and (not w or not w["exists"]):
            add("missing-worktree", f"{branch}: open ledger job has no worktree on this machine", job=_m_jobs.job_key(row))
        if not any(i['kind'] == 'restart' and i.get('job') == _m_jobs.job_key(row) for i in items):
            try:
                restart = _m_reminders.usage_restart_item(inst, n, row)
            except (OSError, ValueError):
                restart = {'node': n['id'], 'kind': 'restart-repair', 'job': _m_jobs.job_key(row),
                           'text': branch + ': restart usage receipt unreadable; retain it for repair'}
            if restart:
                add(restart['kind'], restart['text'], **{k: v for k, v in restart.items() if k not in ('kind', 'text', 'node')})
    for branch, w in state["worktrees"].items():
        if not _m_jobs.open_job(state["jobs"] or [], branch):
            add("unrecorded-worktree", f"{branch}: worktree has no open ledger job; reconcile the records")
    for line in state["inbox"]:
        if machine_transition_line(n, line):
            continue  # legacy autonomous transitions are informational, never coordinator work
        else:
            add("inbox", line[2:], informational=_m_docker_leftovers.leftover_report_line(line))
    if include_advice:
        for advice in runaway_jobs(inst, n, structured=True):
            if isinstance(advice, str):  # legacy callers supplying display-only advice
                add("job", advice, informational=True)
            else:
                items.append(advice)
    if n['id'] == 'root':
        episode = _m_machine.machine_episode_due(inst)
        if episode:
            add(episode['kind'], episode['text'], **{k: v for k, v in episode.items() if k not in ('kind', 'text')})
    for line in overdue_questions(inst, n, state["inbox"]):
        add("overdue-question", "question unanswered past its threshold; root must follow up", escalate_to="root")
    lag = status_lag(inst, n, evidence) if include_stale else None
    if lag:
        add("stale-status", lag + "; update judgement sections, then brain status <node> --refresh")
    from . import watch as _m_watch
    items.extend(_m_watch.watch_job_stalls(inst, n))
    omitted = (['stale-status'] if not include_stale else []) + (['job'] if not include_advice else [])
    return _m_reminders.reminder_items(inst, n, items, include_handled=include_handled, omitted_kinds=omitted)


def changes_due(inst, n):
    """Unread core changes, for a node whose coordinator runs here (the root always)."""
    if n["id"] != "root" and not n["local"].get("coordinator"):
        return []
    unread = _m_upgrade.changes_unread(inst, n)
    if not unread:
        return []
    return [{"node": n["id"], "kind": "core-changes", "text": _m_nodes.import_text(
        f"core changed: {len(unread)} unread entr{'y' if len(unread) == 1 else 'ies'} ("
        + "; ".join(e["id"] for e in unread[:3]) + (" …" if len(unread) > 3 else "")
        + f"); read them: brain changes {n['id']}, then brain changes {n['id']} --seen", _m_guard.never_terms(n))}]


def machine_due_items(inst, nodes, state, items):
    """Waiting starts share one episode across due, watch and pending-wake validation."""
    try:
        holds = _m_machine.machine_hold_items(inst, state)
    except (OSError, ValueError):
        return [{'node': 'root', 'kind': 'machine-repair',
                 'text': 'Machine hold state unreadable; retain private wake state for repair'}]
    target = next((n for n in nodes if n['id'] == 'root'), nodes[0] if nodes else None)
    if target is None or target['archived']:
        return []
    scope = _m_machine.machine_scope(inst, target['id'])[1]
    hold = holds.get(scope)
    return ([{'node': target['id'], 'kind': 'machine', 'episode': hold['episode'],
              'scope': scope, 'text': _m_machine.machine_summary(state)}] if hold else [])


def cmd_due(a):
    inst = _m_nodes.load()
    nodes = [inst.node(a.id)] if a.id else inst.nodes()
    defer, ack = getattr(a, 'defer', None), getattr(a, 'ack', None)
    if defer or ack:
        if defer and ack:
            _m_common.die('choose one reminder operation')
        candidates = [i for n in nodes for i in due_items(inst, n, include_handled=True)]
        item_id = defer[0] if defer else ack
        target = _m_reminders.resolve_lifecycle_id(candidates, item_id)
        n = inst.node(target['node'])
        current = [i for i in candidates if i['node'] == n['id']]
        _m_reminders.record_reminder(inst, n, current, target['id'], 'defer' if defer else 'ack',
                                     getattr(a, 'reason', None), until=defer[1] if defer else getattr(a, 'until', None),
                                     resume_condition=getattr(a, 'resume', None))
        print(f"{n['id']}: reminder {target['id']} {'deferred' if defer else 'acknowledged'}; underlying state retained")
        return
    if getattr(a, 'since_wake', False):
        from . import watch as _m_watch
        try:
            items = _m_watch.watch_since_wake(inst, nodes)
        except (OSError, ValueError):
            _m_common.die('due: invalid or unreadable wake history; retained for repair')
        if a.quiet:
            sys.exit(1 if items else 0)
        if a.json:
            print(json.dumps(items, indent=2))
        else:
            for i in items:
                print(f"{i['node']}: {i['kind']}: {i['text']}")
        return
    root_tick = (a.id or "root") == "root" and not a.quiet
    if root_tick:
        _m_upgrade.core_update(inst)  # refresh the daily answer the inbox reports
    upgraded = _m_upgrade.auto_upgrade(inst) if root_tick else None
    if upgraded:  # the next tick runs the new core
        kind, text = upgraded.split(": ", 1)
        if a.json:
            print(json.dumps([{"node": "root", "kind": kind, "text": text}], indent=2))
        else:
            print(f"root: {upgraded}")
        return
    # Root's explicit tick also sees descendant escalations, without exposing their text.
    escalation_nodes = inst.nodes() if a.id == "root" else nodes
    from . import watch as _m_watch
    # Transport receipt expiry must not turn an unchanged escalation into new work.
    try:
        if not _m_common.outward_links(inst.root):
            _m_watch.watch_once(inst, kinds=('overdue-question',), quiet=True,
                                sources={n['id'] for n in escalation_nodes})
    except (OSError, ValueError):
        pass  # keep display available; invalid acknowledgements never replay the backlog
    for n in escalation_nodes:
        if n["archived"]:
            continue
        target, command = _m_hosts.wake_target(inst, n)
        if command and _m_hosts.wake_directory(inst, target).is_dir() and any(_m_hosts.wake_directory(inst, target).glob("*.pending")):
            try:
                _m_hosts.wake_start(inst, target)
            except OSError:
                pass
    _m_docker_leftovers.docker_leftover_nodes(inst, nodes)
    state = _m_machine.machine_state(inst)
    items = [item for n in nodes for item in due_items(inst, n, include_handled=getattr(a, 'handled', False)) + changes_due(inst, n)]
    wake_seen = set()
    for n in nodes:
        target, _ = _m_hosts.wake_target(inst, n)
        diagnostic = _m_hosts.wake_diagnostic(inst, n)
        if diagnostic and target["id"] not in wake_seen:
            wake_seen.add(target["id"])
            items.append({"node": target["id"], "kind": "wake", "text": diagnostic})
    if any(n['id'] == 'root' for n in nodes):
        from . import watch as _m_watch
        if _m_watch.watch_state_invalid(inst):
            items.append({'node': 'root', 'kind': 'watcher',
                          'text': 'watcher state invalid; restore .brain/watch.json before watching'})
    items += machine_due_items(inst, nodes, state, items)
    if not a.quiet and getattr(a, "machine", False):
        items = [i for i in items if i["kind"] != "machine"] + [
            {"node": a.id or "root", "kind": "machine", "text": _m_machine.machine_summary(state)}]
    if a.id == "root":
        # Retain the informational descendant backlog in the ordinary due view.
        items += [item for n in inst.nodes() if n["id"] != "root"
                  for item in due_items(inst, n, include_stale=False, include_advice=False)
                  if item["kind"] == "unsettled-threads"]
    if a.id == "root":
        for n in escalation_nodes:
            if n["id"] == "root" or n["archived"]:
                continue
            items += [item for item in due_items(inst, n, include_stale=False, include_advice=False)
                      if item["kind"] == "overdue-question"]
            if any("<!-- host-question:" in line or "<!-- handback:" in line for line in inbox_lines(n)):
                items.append({"node": n["id"], "kind": "question", "text":
                              "unanswered question in node inbox; follow up with its coordinator or answer for it"})
    # Ordinary ticks report each hold once. Explicit telemetry remains available at any time.
    items = [i for i in items if i['kind'] != 'machine' or not i.get('episode') or
             _m_machine.machine_hold_claim(inst, i['episode'], i['scope'])]
    if a.quiet:
        sys.exit(1 if any(not i.get('informational') for i in items) else 0)
    if a.json:
        print(json.dumps(items, indent=2))
    else:
        for item in items:
            print(f"{item['node']}: {item['kind']}: " + (f"[{item['id'][:8]}] " if item.get('id') else '') + item['text'] +
                  (f" (handled: {item['handling']['reason']}; resume: {item['handling'].get('resume_condition') or item['handling'].get('until')})"
                   if item.get('handling') and _m_reminders.reminder_handled(inst, item) else '') +
                  (f" (escalated: {item['age_hours']:g}h old, over 48h)" if item.get("escalated") else ""))


def missing_preview(n, state):
    code = code_node(n)
    merged = any(r["Ended"] and r["Outcome"] == "merged" for r in state["jobs"] or []) or \
        any(w["merged"] for w in state["worktrees"].values())
    recorded = [n['local'].get('preview', ''), n['fm'].get('preview_url', '')]
    return code and merged and not any(str(value).strip().casefold() not in ('', 'none') for value in recorded)


def display_time(inst, value):
    parsed = inst.parse_time(value)
    return _m_usage.usage_time(parsed.timestamp(), inst) if parsed else "unknown (legacy time)"


def status_facts(inst, n, state, known_merges):
    """The displayed facts only: no hidden git tips, file hashes or judgement timestamp."""
    merged = {_m_jobs.job_key(r): r for r in state["jobs"] or [] if r["Ended"] and r["Outcome"] == "merged"}
    for r in state["jobs"] or []:
        if not r["Ended"] and state["worktrees"].get(r["Job"], {}).get("merged"):
            merged[_m_jobs.job_key(r)] = {**r, "local_merge": True}
    def merge_time(r):
        t = inst.parse_time(r.get("Ended") or r.get("Started"))
        return t.timestamp() if t else 0
    new = sorted([r for key, r in merged.items() if key not in known_merges], key=merge_time, reverse=True)
    lines = ["Merged since previous refresh:"]
    lines += [_m_guard.redact(f"- {r['Job']}: merged in local git; ledger still open" if r.get("local_merge") else
                     f"- {display_time(inst, r['Ended'])}: {r['Job']} ({r['Commits'] or '?'} commits)", _m_guard.never_terms(n)) for r in new] or ["- None recorded."]
    lines += ["", "Open worktrees (this machine):"]
    lines += [_m_guard.redact(f"- {b}: {'present' if w['exists'] else 'missing'}" + ("; merged in local git" if w["merged"] else ""),
                     _m_guard.never_terms(n)) for b, w in state["worktrees"].items()] or ["- None."]
    lines += ["", "Running jobs (open in ledger; a hand-back means the worker stopped):"]
    from . import job_attempts as _m_attempts
    projections = [state.get('job_projections', {}).get(_m_jobs.job_key(r)) or _m_attempts.job_projection(inst, n, r)
                   for r in state['jobs'] or [] if not r['Ended']]
    lines += [_m_guard.redact(f"- {p['branch']}: {p['state']}; host activity unknown; " + ('runtime activity unknown; ' if p['state'] == 'open' else '') +
                     (p['summary'] or ('last hand-back (historical): ' + p['historical_summary'] if p['historical_summary'] else 'summary not recorded')) +
                     f"; started {display_time(inst, p['ledger']['Started'])}; {p['ledger']['Reviews'] or '0'} reviews", _m_guard.never_terms(n))
              for p in projections] or ["- None."]
    lines += ['', 'Active work (generated from ledger and worker records; host activity unknown):', '',
              '| Branch | State | Summary | Recorded queue | Next action |', '| --- | --- | --- | --- | --- |']
    for p in projections[:8]:
        queue = p['admission'] or p['queue']
        queued = (str(queue.get('position') or 'unknown') + '; owner ' + str(queue.get('owner') or 'unknown')) if queue else 'unknown'
        summary = p['summary'] or ('historical: ' + p['historical_summary'] if p['historical_summary'] else 'not recorded')
        values = (p['branch'], p['state'], summary, queued, p['next_action'])
        lines.append(_m_guard.redact('| ' + ' | '.join(_m_jobs.cell(v) for v in values) + ' |', _m_guard.never_terms(n)))
    if len(projections) > 8:
        lines.append(f'{len(projections) - 8} more open jobs; brain jobs --live lists all recorded rows.')
    if state["jobs"] is None:
        lines += ["- Legacy ledger: running jobs unknown."]
    lines += ["", f"Closed jobs with unsettled threads: {len(_m_jobs.unsettled_jobs(state['jobs']))}"]
    due = due_items(inst, n, (state, "", ""), include_stale=False, include_advice=False, include_handled=True)
    lessons = sorted([r for r in state["jobs"] or [] if r["Ended"] and r["Outcome"] == "merged"], key=merge_time, reverse=True)
    lines += ["", "Lesson outcomes (merged jobs):"]
    lines += [_m_guard.redact(f"- {r['Job']}: " + (r.get("Lessons") or
                     ("missing; coordinator must record outcome" if r.get("Lesson check") == "required" else
                      "legacy; outcome not required")), _m_guard.never_terms(n)) for r in lessons] or ["- None."]
    lines += ["", "Finished jobs awaiting coordinator:"]
    lines += ["- " + i["text"] + (" (acknowledged; reason: " + i['handling']['reason'] +
              '; resume: ' + (i['handling'].get('resume_condition') or i['handling'].get('until') or '') + ')'
              if _m_reminders.reminder_handled(inst, i) else '')
              for i in due if i["kind"] in ("handback", "restart")] or ["- None."]
    forecasts = _m_jobs.forecast_lines(state["jobs"] or [])
    if forecasts:
        lines += ["", "Forecasts (merged ledger durations):"] + ["- " + _m_guard.redact(s, _m_guard.never_terms(n)) for s in forecasts]
    if missing_preview(n, state):
        lines += ["", "Preview: missing — merged code jobs need a local preview and screenshot tour."]
    if n["fm"].get("preview_url"):
        lines += ["", _m_guard.redact("Published preview: " + n["fm"]["preview_url"], _m_guard.never_terms(n))]
    rules, roles, items = pending_imports(inst, n)
    if rules or roles or items:
        lines += ["", f"Imported, unreviewed (inactive): {rules} rule file(s), {roles} role(s), {items} proposed item(s)."]
    human = [i for i in attention_items(inst, [n]) if i[1] not in ("stale",)]
    lines += ["", f"Due: {sum(not i.get('informational') and not _m_reminders.reminder_handled(inst, i) for i in due)} · Needs the human (attention sources, excluding stale status): {len(human)}"]
    compact, count = [], 0
    for line in lines:
        if line.startswith("- "):
            count += 1
            if count <= 8:
                compact.append(line)
        else:
            if count > 8:
                compact.append(f"- +{count - 8} more")
            count = 0
            compact.append(line)
    if count > 8:
        compact.append(f"- +{count - 8} more")
    return compact, merged


def refresh_status(inst, n):
    f = n["dir"] / "status.md"
    text = ""
    if f.exists():
        with f.open(newline="") as stream:
            text = stream.read()
    if (text.count(FACTS_START) != text.count(FACTS_END) or text.count(FACTS_START) > 1 or
            (FACTS_START in text and not FACTS_RE.search(text))):
        _m_common.die("status.md has incomplete or duplicate facts markers; repair them before refreshing")
    previous = facts_meta(n)
    evidence = due_evidence(inst, n)
    state, fingerprint, _ = evidence
    lines, merged = status_facts(inst, n, state, previous.get("merged", []))
    fingerprint = _m_jobs.digest(lines)
    # Telemetry is an informational snapshot, never part of the stale-status wake fingerprint.
    lines += ["", "Machine (snapshot at refresh; live: brain due --machine):", "- " + _m_machine.machine_summary(_m_machine.machine_state(inst))]
    lines = [FACTS_START, "## Facts (generated by brain status --refresh)", "",
             "Facts refreshed: " + inst.now(), "", *lines, "",
             "<!-- brain:facts-state " + json.dumps({"fingerprint": fingerprint, "merged": list(merged),
                 "merged_before": previous.get("merged", [])}, sort_keys=True) + " -->", FACTS_END]
    block = "\n".join(lines)
    old = FACTS_RE.search(text)
    if old:
        suffix = text[old.end():]
        if suffix.startswith("\n\n"):
            suffix = suffix[2:]
        text = text[:old.start()] + suffix
    # Keep the title and coordinator Updated above facts, including legacy blocks at the top.
    body = re.search(r"(?m)^## ", text)
    headers = list(re.finditer(r"(?m)^(?:# |Updated:)[^\r\n]*(?:\r?\n|$)", text[:body.start()] if body else text))
    at = headers[-1].end() if headers else 0
    while at < len(text) and text[at] in "\r\n":
        at += 1
    text = text[:at] + block + "\n\n" + text[at:]
    _m_common.atomic_write(f, text)
    _m_common.touch(f)


def inbox_items(inst, nodes=None, reporter="root", readonly=False, requests_only=False, lifecycle_data=None):
    """The existing UI attention sources, with identities independent of line positions or other items.
    Read-only; until reported, first_seen comes from the source's git introduction (mtime if uncommitted)."""
    if lifecycle_data is None and (readonly or requests_only):
        try:
            lifecycle_data = _m_reminders.lifecycle_read(inst)
        except (OSError, ValueError):
            pass  # Display remains fail-open; mutations require a valid locked store.
    saved = inst.root / ".brain" / "inbox-seen.json"
    try:
        data = json.loads(saved.read_text()) if saved.exists() else {}
        reporters = data.get("reporters", {}) if isinstance(data, dict) and data.get("version") == 2 else {}
        reporters = reporters if isinstance(reporters, dict) else {}
    except (OSError, ValueError):
        reporters = {}
    reporters = {key: value for key, value in reporters.items() if isinstance(value, dict)}
    seen = {key: value for key, value in reporters.get(reporter, {}).items() if isinstance(value, dict)}
    by_id = {n["id"]: n for n in (nodes if nodes is not None else inst.nodes())}
    out, ids = [], set()
    # The live attention view retains advice; delivery queues filter it separately.
    if requests_only:
        attention = []
        for n in by_id.values():
            if n['archived'] or paused(n):
                continue
            attention += [(n['id'], 'blocked-overdue' if b['escalated'] else 'blocked', b['text'])
                          for b in blocked_entries(inst, n)]
            attention += [(n['id'], 'inbox', 'inbox: ' + line[2:])
                          for line in inbox_lines(n, readonly=True, lifecycle_data=lifecycle_data)]
    else:
        attention = [item for item in attention_items(inst, list(by_id.values()), readonly=readonly,
                                                     lifecycle_data=lifecycle_data) if item[1] != "job"]
    # Explicit node inboxes are coordinator views. Root's inbox is the human rollup;
    # root-owned job advice remains available in due, never in that human rollup.
    owner = by_id.get(reporter)
    advice_keys = {}
    if not requests_only and reporter != "root" and owner and not owner["archived"] and not paused(owner):
        for advice in runaway_jobs(inst, owner, structured=True, readonly=readonly):
            if isinstance(advice, str):
                attention.append((reporter, 'job', advice))
            else:
                attention.append((reporter, 'job', advice['text']))
                advice_keys[(reporter, advice['text'])] = _m_reminders.reminder_key(advice)
    for nid, kind, text in attention:
        identity = text if kind in ("blocked", "blocked-overdue", "inbox", "core-update", "job") else kind
        if kind == "machine-alert":
            identity = (_m_machine.machine_episode_due(inst) or {}).get("episode", kind)
        key = advice_keys.get((nid, text)) or _m_jobs.digest([nid, kind, " ".join(identity.split())])
        if kind == 'inbox':
            key = _m_reminders.request_episode_id(inst, by_id[nid], '- ' + text.removeprefix('inbox: '),
                                                   lifecycle_data=lifecycle_data)
        if key in ids:
            continue
        ids.add(key)
        n = by_id[nid]
        record = seen.get(key, {})
        first = record.get("first_seen") if isinstance(record, dict) else None
        if kind == 'inbox':
            match = re.match(r'inbox: (.*?) from ', text)
            stamp = inst.parse_time(match[1]) if match else None
            if stamp:
                first = stamp.isoformat(timespec='seconds')
        if not first:
            source = n["dir"] / ("inbox.md" if kind == "inbox" else "status.md" if kind in ("blocked", "blocked-overdue", "stale") else "node.md")
            if kind == "import-review":
                sources = [n["dir"] / "sources/import-proposals.md"]
                sources += [s["dir"] / "role.md" for s in _m_knowledge.find_specialists(inst)
                            if s["node"]["id"] == nid and s["fm"].get("status") == "proposed"]
                proposed = [i for i in inst.items() if i["fm"].get("status") == "proposed"]
                queue = proposal_nodes(inst, proposed)
                sources += [i["path"] for i in proposed if queue[i["path"]] == nid]
                source = next((p for p in sources if p.is_file()), source)
            needle = text.removeprefix("inbox: ") if kind == "inbox" else text
            history = _m_common.git("log", "--reverse", "--format=%cI", "-S", needle, "--",
                          str(source.relative_to(inst.root)), cwd=inst.root).stdout.splitlines()
            first = history[0] if history else datetime.datetime.fromtimestamp(
                source.stat().st_mtime if source.exists() else inst.now_dt().timestamp(), inst.zone()).isoformat(timespec="seconds")
        out.append({"id": key, "node": nid, "kind": kind, "text": _m_guard.redact(text, _m_guard.never_terms(n)),
                    "first_seen": first, "reported": bool(record.get("reported_at")) if isinstance(record, dict) else False,
                    'age_days': round(max(0, elapsed(inst.parse_time(first), inst.now_dt()) / 86400), 2)
                    if inst.parse_time(first) else None,
                    **({"informational": True} if kind == "job" or
                       (kind == "inbox" and _m_docker_leftovers.leftover_report_line('- ' + text.removeprefix('inbox: '))) else {}),
                    **blocked_metadata(inst, n, kind, text)})
    return sorted(out, key=lambda i: i['node']), reporters


def cmd_inbox(a):
    inst = _m_nodes.load()
    # Default to the calling node, including external homes/workbenches. Explicit root is instance-wide.
    here = _m_hosts.node_at(inst, Path.cwd())
    if not here:
        cwd = Path.cwd().resolve()
        places = [(Path(p).expanduser().resolve(), n) for n in inst.nodes()
                  for p in [n["home_path"], n["local"].get("workbench"),
                            *[w["path"] for w in n["local"].get("worktrees", {}).values()]] if p]
        here = next((n for p, n in sorted(places, key=lambda x: len(x[0].parts), reverse=True)
                     if p == cwd or p in cwd.parents), None)
    reporter = inst.node(a.id)["id"] if a.id else here["id"] if here else "root"
    nodes = [n for n in inst.nodes() if reporter == "root" or n["id"] == reporter or n["id"].startswith(reporter + "/")]
    weekly = getattr(a, 'weekly', False)
    if weekly and a.new:
        _m_common.die('--weekly is read-only and cannot combine with --new')
    items, reporters = inbox_items(inst, nodes, reporter, readonly=weekly, requests_only=weekly)
    if weekly:
        items = [i for i in items if i['kind'] == 'inbox' and i['age_days'] is not None and i['age_days'] >= 7]
    prefixes = shortest_ids([i["id"] for i in items])
    seen = reporters.get(reporter, {})
    if a.new:
        fresh = [i for i in items if not i["reported"]]
        now = inst.now_dt().isoformat(timespec="seconds")
        # Keep only unresolved ids: removing an item resolves it; a later recurrence can notify again.
        state = {i["id"]: {"first_seen": i["first_seen"], "reported_at": seen.get(i["id"], {}).get("reported_at") or now}
                 for i in items}
        items = fresh
    items.sort(key=lambda i: i['node'])
    output = json.dumps(items, indent=2) + "\n" if a.json else "".join(
        f"{item['node']}: [{prefixes[item['id']]}] {item['text']}" +
        (f" (age {item['age_days']:g}d)" if item.get('age_days') is not None else ' (age unknown)') +
        (f" (escalated: {item['age_hours']:g}h old, over 48h)" if item.get("escalated") else "") + "\n" for item in items)
    sys.stdout.write(output)
    if a.new:
        # Delivery comes first, under the same lock. A crash before saving repeats an item.
        sys.stdout.flush()
        f = inst.root / ".brain" / "inbox-seen.json"
        f.parent.mkdir(exist_ok=True)
        reporters[reporter] = state
        _m_common.atomic_write(f, json.dumps({"version": 2, "reporters": reporters}, indent=2) + "\n")


def answer_inbox(inst, item_id, text):
    try:
        with _m_reminders.lifecycle_store(inst) as data:
            items, _ = inbox_items(inst, readonly=True, requests_only=True, lifecycle_data=data)
            item = _m_reminders.resolve_lifecycle_id(items, item_id)
            if item['kind'] != 'inbox':
                _m_common.die('this item is not an answerable inbox request')
            n = inst.node(item['node'])
            _m_guard.no_terms_in(n, text, 'the recorded answer')
            line = next((line for line in inbox_lines(n, readonly=True, lifecycle_data=data)
                         if _m_reminders.request_episode_id(inst, n, line, lifecycle_data=data) == item['id']), None)
            if not line:
                _m_common.die('request episode changed; inspect the current inbox')
            _m_reminders.record_answer(inst, n, line, item['id'], text, lifecycle_data=data)
    except (OSError, ValueError):
        _m_common.die('answer receipt could not be recorded; retain lifecycle state for repair')
    return item['id']


def cmd_answer(a):
    key = answer_inbox(_m_nodes.load(), a.item_id, a.text)
    print(f'answer recorded for {key}; source and answer history retained')


def proposals_of(inst, nodes):
    """Per node: its unreviewed rule file, proposed specialists and proposed items, for one review sitting."""
    out, roles = [], _m_knowledge.find_specialists(inst)
    items = [i for i in inst.items() if i["fm"].get("status") == "proposed"]
    queue = proposal_nodes(inst, items)
    for n in nodes:
        rules = int(pending_imports(inst, n)[0])
        sp = [x for x in roles if x["node"]["id"] == n["id"] and x["fm"].get("status") == "proposed"]
        it = [i for i in items if queue[i["path"]] == n["id"]]
        if rules or sp or it:
            out.append((n, rules, sp, it))
    return out


def cmd_proposals(a):
    """Everything waiting for review, a node at a time, with full text: read it here, then accept or reject several
    at once. Nothing here accepts anything."""
    if getattr(a, 'approve_all', False):
        return _m_proposals.cmd_proposal_activation(a)
    if getattr(a, 'review_manifest', None):
        _m_common.die('--review-manifest requires --approve-all')
    inst = _m_nodes.load()
    top = inst.node(a.id or "root")
    nodes = [n for n in inst.nodes() if top["id"] == "root" or n["id"] == top["id"] or n["id"].startswith(top["id"] + "/")]
    if a.only:
        nodes = [n for n in nodes if n["id"] in set(_m_common.lst(a.only))]
    groups = proposals_of(inst, nodes)
    rows = []
    nodes_all, screens = inst.nodes(), []
    for n, rules, sp, it in groups:
        terms = _m_guard.never_terms(n)
        clean = lambda text, more=(): _m_nodes.import_text(text or "", terms + [t for t in more if t not in terms])
        src_terms = {x["dir"]: _m_knowledge.specialist_terms(inst, x, [n], nodes_all) for x in sp}
        src_terms.update({i["path"]: _m_knowledge.learning_terms(inst, i["fm"], [n], nodes_all) for i in it})
        group = list(terms)  # ids, paths and commands too: the holder's words and every proposal's sources'
        for more in src_terms.values():
            group += [t for t in more if t[0] not in {x[0] for x in group}]
        screens.append(group)
        rules_file = n["dir"] / "sources/import-proposals.md"
        rows.append({"node": n["id"], "owners": n["owners"],
                     "rules_snapshot": _m_proposals.pending_rule_snapshot(inst, n),
                     "rules": str(rules_file.relative_to(inst.root)) if rules else None,
                     "rules_body": clean(_m_common.read_fm(rules_file)[1].strip()) if rules else None,
                     "specialists": [{"id": x["id"], "title": clean(x["fm"].get("title"), src_terms[x["dir"]]),
                                      "scope": _m_common.lst(x["fm"].get("scope")),
                                      "from": _m_common.lst(x["fm"].get("source_nodes")) or [n["id"]],
                                      "path": str((x["dir"] / "role.md").relative_to(inst.root)),
                                      "body": clean(_m_common.read_fm(x["dir"] / "role.md")[1].strip(), src_terms[x["dir"]]),
                                      "lessons": clean((x["dir"] / "lessons.md").read_text().strip()
                                                       if (x["dir"] / "lessons.md").is_file() else "",
                                                       src_terms[x["dir"]])}
                                     for x in sp],
                     "items": [{"id": i["fm"].get("id", i["path"].stem), "kind": i["fm"].get("kind", ""),
                                "title": clean(i["fm"].get("title"), src_terms[i["path"]]),
                                "scope": _m_common.lst(i["fm"].get("scope")),
                                "from": _m_common.lst(i["fm"].get("source_nodes")) or [i["fm"].get("source_node", "")],
                                "ref": clean(i["fm"].get("source_ref"), src_terms[i["path"]]),
                                "keywords": clean(i["fm"].get("keywords"), src_terms[i["path"]]),
                                "provenance": clean(f"{i['fm'].get('source_repo', '')}@{i['fm'].get('source_commit', '')}",
                                                    src_terms[i["path"]]),
                                "path": str(i["path"].relative_to(inst.root)),
                                "body": clean(_m_common.read_fm(i["path"])[1].strip(), src_terms[i["path"]])} for i in it]})
    if a.json:
        print(json.dumps([_m_history.history_screen(row, group) for row, group in zip(rows, screens)], indent=1,
                         ensure_ascii=False))
        return
    if not rows:
        print("nothing waiting for review")
        return
    total = sum(len(r["items"]) + len(r["specialists"]) + bool(r["rules"]) for r in rows)
    out = [f"{total} proposal(s) waiting for review in {len(rows)} node(s). Read each, then accept or reject; "
           "nothing is usable until accepted.", ""]
    for r, group in zip(rows, screens):
        start = len(out)
        out += [f"## {r['node']}  (owners {', '.join(r['owners'])})", ""]
        if r["rules"]:
            out += [f"### rules: {r['rules']}", "Not yet reviewed; the import skill says how to record a review.", "",
                    r["rules_body"] or "(empty)", ""]
        for x in r["specialists"]:
            out += [f"### specialist `{x['id']}`: {x['title']}", f"scope {', '.join(x['scope'])} · from "
                    f"{', '.join(x['from'])} · {x['path']}", "", x["body"] or "(empty role)", ""]
            if x["lessons"]:
                out += ["Its lessons (hired with it):", "", x["lessons"], ""]
        for x in r["items"]:
            out += [f"### `{x['id']}` ({x['kind']}): {x['title']}",
                    f"scope {', '.join(x['scope'])} · from {', '.join(x['from'])} · ref {x['ref']} · {x['provenance']}"
                    + (f" · keywords {x['keywords']}" if x["keywords"] else ""), "", x["body"] or "(empty)", ""]
        ids = [x["id"] for x in r["items"]]
        if ids:
            out += [f"Accept: brain item accept {' '.join(ids)} --reviewed-by <you>  ·  reject: brain item reject <ids>"]
        if r["specialists"]:
            out += [f"Specialists: brain specialist accept {' '.join(x['id'] for x in r['specialists'])} "
                    f"--at {r['node']} --reviewed-by <you>"]
        out[start:] = _m_nodes.import_text("\n".join(out[start:]), group).split("\n") + [""]
    print("\n".join(out).rstrip() + "\n", end="")


def shortest_ids(ids):
    return {key: next((key[:size] for size in range(6, len(key) + 1)
                       if all(other == key or not other.startswith(key[:size]) for other in ids)), key)
            for key in ids}


def cmd_stale(a):
    """Freshness check: is a node's status.md older than its thread's last activity (any timezone, e.g. ...Z)?"""
    inst = _m_nodes.load()
    n = inst.node(a.id)
    u = updated_at(inst, n)
    try:
        t = datetime.datetime.fromisoformat(a.host_time.strip().replace("Z", "+00:00"))
    except ValueError:
        _m_common.die(f"not a time: {a.host_time} (ISO 8601, as the host gives it, e.g. 2026-10-19T03:31:00Z)")
    if t.tzinfo is None:
        _m_common.die("give the host time with its zone (Z or +hh:mm): without one it can't be compared")
    if not u:
        print(f"behind: {n['id']} has no Updated line; ask its coordinator to run the node-sync skill")
        return
    gap = elapsed(u, t)
    when = f"status {u.strftime('%Y-%m-%d %H:%M')} {inst.zone() or ''} = {u.astimezone(datetime.timezone.utc):%H:%MZ}"
    if gap > a.grace * 60:
        h, m = divmod(int(gap // 60), 60)
        print(f"behind by {h}h {m:02d}m ({when}; thread {t.astimezone(datetime.timezone.utc):%H:%MZ}): ask its "
              f"coordinator to run the node-sync skill for everything since {u.strftime('%Y-%m-%d %H:%M')}")
    else:
        print(f"current ({when}; thread {t.astimezone(datetime.timezone.utc):%H:%MZ})")


def cmd_now(a):
    inst = _m_nodes.load()
    print(inst.now_dt().isoformat(timespec="seconds") if a.iso else inst.now())


def cmd_tree(a):
    inst = _m_nodes.load()
    for n in inst.nodes():
        depth = 0 if n["id"] == "root" else n["id"].count("/") + 1
        fm = n["fm"]
        tag = " [sealed]" if n["sealed"] else ""
        labels = f"  {{{fm.get('labels')}}}" if fm.get("labels") else ""
        print(f"{'  ' * depth}{n['id']}: {fm.get('title', '')}  owners={','.join(n['owners'])}{tag}{labels}")


def cmd_status(a):
    inst = _m_nodes.load()
    if a.refresh and not a.id:
        _m_common.die("--refresh needs a node id: only its coordinator writes status.md")
    for n in ([inst.node(a.id)] if a.id else inst.nodes()):
        if a.refresh:
            refresh_status(inst, n)
        print(f"{n['id']:<28} {status_line(n)}")


def cmd_show(a):
    inst = _m_nodes.load()
    n = inst.node(a.id)
    fm, loc = n["fm"], n["local"]
    print(f"{n['id']}: {fm.get('title', '')}")
    par = n["parent"]
    seal = f"  [sealed: parent's owners are {','.join(par['owners'])}]" if n["sealed"] else ""
    print(f"  owners    {','.join(n['owners'])}{'' if _m_common.lst(fm.get('owners')) or not par else ' (inherited)'}{seal}")
    print(f"  parent    {par['id'] if par else '-'}")
    print(f"  labels    {fm.get('labels') or '-'}")
    print(f"  runtime   {fm.get('runtime', '-')}")
    if n["external"]:
        hp = n["home_path"]
        state = "missing on this machine" if not hp else ("" if hp.exists() else " (not found)")
        print(f"  home      {_m_common.tilde(hp) + state if hp else state}")
        print(f"  home_url  {fm.get('home_url', '-')}")
    else:
        print(f"  home      {fm.get('home', 'here')}")
    for k in _m_common.LOCAL_KEYS:
        v = loc.get(k) or fm.get(k)
        if v and v != "none":
            print(f"  {k:<13} {inst.thread_link(v) if k == 'coordinator' else v}")
    if loc.get("synced_at"):
        print(f"  synced    {loc['synced_at']}")
    for k in ("merged_from", "created"):
        if fm.get(k):
            print(f"  {k:<9} {fm[k]}")
    u = updated_at(inst, n)
    utc = f"  (= {u.astimezone(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%MZ')}, to compare with host times)" if u else ""
    print(f"  status    {status_line(n)}{utc}")
    lims = _m_knowledge.limits_of(n)
    if lims:
        print(f"  limits    {len(lims)} in brief.md")
    if _m_guard.never_terms(n):
        print(f"  never-name  {len(_m_guard.never_terms(n))} term(s) (brain guard {n['id']})")
    ib = inbox_lines(n)
    print(f"  inbox     {len(ib)} item(s)")
    kids = [m["id"] for m in inst.nodes() if m["parent"] and m["parent"]["id"] == n["id"]]
    if kids:
        print(f"  children  {', '.join(kids)}")
    print(f"  folder    {_m_common.tilde(n['dir'])}")


def paused(n):
    return "state:paused" in _m_common.lst(n["fm"].get("labels"))


def cmd_dashboard(a):
    root = _m_nodes.try_find_root()
    if not root:
        return False
    name = _m_nodes.register(root)
    inst = _m_instance.Instance(root)
    nodes = inst.nodes()
    others = [k for k, r in _m_instance.brains_here() if r.resolve() != root.resolve()]
    print(f"{nodes[0]['fm'].get('title', 'Brain')}{f' ({name})' if others else ''}  ·  {_m_common.tilde(root)}  ·  "
          f"machine {inst.local['machine']}\n")
    for n in nodes:
        depth = 0 if n["id"] == "root" else n["id"].count("/") + 1
        name = n["id"] if n["id"] == "root" else n["id"].rsplit("/", 1)[-1]
        tags = (" [sealed]" if n["sealed"] else "") + (" [archived]" if n["archived"] else "") + \
               (" [paused]" if paused(n) else "")
        ib, work = len(inbox_lines(n)), len(active_work(n))
        print(f"{'  ' * depth}{name:<{28 - 2 * depth}} updated {ago(inst, updated_at(inst, n)):<10}"
              f"{f'  inbox {ib}' if ib else ''}{f'  in flight {work}' if work else ''}{tags}")
    attention = [f"{nid}: {what}" for nid, kind, what in attention_items(inst, nodes)]
    problems, warnings, fixable = _m_check.collect_problems(inst)
    if problems:
        attention.append(f"{len(problems)} check problem(s): run 'brain check'"
                         + (" (some fixable: 'brain check --fix')" if fixable else ""))
    print("\nNeeds attention" if attention else "\nNothing needs attention.")
    for x in attention[:30]:
        print(f"  - {x if len(x) <= 140 else x[:139] + '…'}")
    coords = [(n["id"], n["local"].get("coordinator")) for n in nodes if n["local"].get("coordinator")]
    if coords:
        print("\nCoordinators")
        for nid, c in coords:
            print(f"  {nid:<28} {inst.thread_link(c)}")
    print("\nbrain help  ·  brain show <node>  ·  brain sync <node>  ·  brain check  ·  brain ui (in a browser)")
    if others and sys.stdout.isatty():  # for the person at the terminal; agents see only the brain they asked for
        print(f"other brains here: {', '.join(others)}  (brain -b <name>, brain --all, brain brains)")
    return True


def proposal_nodes(inst, items):
    """{item path: the node whose review queue holds it}: its source, or for one learned from several nodes,
    the node whose library holds it (their common ancestor)."""
    holders = {n["dir"].resolve(): n["id"] for n in inst.nodes()} if any(
        _m_common.lst(i["fm"].get("source_nodes")) for i in items) else {}
    return {i["path"]: holders.get(i["path"].parent.parent.resolve(), "root") if _m_common.lst(i["fm"].get("source_nodes"))
            else i["fm"].get("source_node") for i in items}


def pending_imports(inst, n):
    rules = n["dir"] / "sources/import-proposals.md"
    rules_pending = False
    if rules.is_file() and rules.read_text().strip():
        fm, _ = _m_common.read_fm(rules)
        reviewed = (fm.get("status") in ("reviewed", "rejected") and
                    fm.get("reviewed_by") in ({inst.cfg.get("operator")} | (set(inst.principals) - set(inst.revoked))) and
                    inst.parse_time(fm.get("reviewed_at", "")))
        rules_pending = not reviewed
        if _m_proposals.pending_rule_snapshot(inst, n) is None:
            rules_pending = False
    proposed = [i for i in inst.items() if i["fm"].get("status") == "proposed"]
    queue = proposal_nodes(inst, proposed)
    items = sum(1 for i in proposed if queue[i["path"]] == n["id"])
    roles = sum(1 for s in _m_knowledge.find_specialists(inst) if s["node"]["id"] == n["id"] and
                s["fm"].get("status") == "proposed")
    return int(rules_pending), roles, items


def runaway_jobs(inst, n, structured=False, readonly=False):
    """Open jobs past what this node's jobs usually take: twice the median in hours or tokens, over budget,
    compacting again and again, or mostly spent by sub-agents."""
    rows = _m_jobs.read_jobs(n) or []
    if not any(not r["Ended"] for r in rows):
        return []
    j, out = _m_jobs.jobs_summary(inst, n), []
    for r in j["open"]:
        u = _m_spend.live_usage(inst, n, r)
        why = {}
        if j["median_hours"] is not None and r["age_h"] > 2 * j["median_hours"]:
            why['median-age'] = f"open {r['age_h']} h, over twice the median of {j['median_hours']} h"
        if u and j["median_tokens"] and u["processed"] > 2 * j["median_tokens"]:
            why['median-tokens'] = f"{_m_spend.tokens_text(u['processed'])} tokens, over twice the median of {_m_spend.tokens_text(j['median_tokens'])}"
        b = _m_spend.job_budget(n, r)
        if b and u and b.get("tokens") and u["processed"] > b["tokens"]:
            why['budget-tokens'] = f"over its {_m_spend.tokens_text(b['tokens'])}-token budget"
        if b and b.get("hours") and r["age_h"] > b["hours"]:
            why['budget-hours'] = f"over its {b['hours']:g}-hour budget"
        if u and u["compactions"] >= 3:
            why['compaction-limit'] = f"{u['compactions']} compactions"
        if u and u["subagents"] and u["sub_share"] > 0.5:
            why['subagent-share'] = f"sub-agents spent {round(100 * u['sub_share'])}% of its tokens"
        if structured:
            out.extend(_m_reminders.latch_job_advice(inst, n, r, why, readonly=readonly))
        elif why:
            out.append(_m_guard.redact(f"job {r['Job']}: " + "; ".join(why.values()), _m_guard.never_terms(n)))
    return out


def attention_items(inst, nodes, readonly=False, lifecycle_data=None):
    """[(node id, kind, text)]: what waits on the operator, node by node."""
    out = []
    if (any(n["id"] == "root" and not n["archived"] and not paused(n) for n in nodes)
            and inst.local.get("auto_upgrade") != "on"):
        update = _m_upgrade.core_update(inst, check=False)  # the cached answer only: listing needs never wait on a network
        if update:
            out.append(("root", "core-update", f"core update available ({update[0][:7]} -> {update[1][:7]}): "
                        "brain upgrade, or brain set root auto_upgrade=on to upgrade whenever no job is open"))
    for n in nodes:
        if n["archived"] or paused(n):
            continue
        rules, roles, items = pending_imports(inst, n)
        if rules or roles or items:
            out.append((n["id"], "import-review", f"import review: {rules} rule file(s), {roles} role(s), "
                        f"{items} item(s) proposed; confirm or reject after reviewing source citations"))
        for b in blocked_entries(inst, n):
            out.append((n["id"], "blocked-overdue" if b["escalated"] else "blocked", b["text"]))
        out += [(n["id"], "inbox", "inbox: " + l[2:])
                for l in inbox_lines(n, readonly=readonly, lifecycle_data=lifecycle_data)]
        out += [(n["id"], "job", text) for text in runaway_jobs(inst, n)]
        if n["id"] == "root":
            episode = _m_machine.machine_episode_due(inst)
            if episode and episode["kind"] == "machine-alert":
                out.append((n["id"], "machine-alert", episode["text"]))
        u = updated_at(inst, n)
        days = int(elapsed(u, inst.now_dt()) // 86400) if u else 0
        if u and days >= _m_common.STALE_DAYS:
            out.append((n["id"], "stale", f"status not updated for {days} days"))
        if ("kind:study" not in _m_common.lst(n["fm"].get("labels"))
                and not (n["local"].get("coordinator") or n["fm"].get("coordinator", "none") != "none")):
            out.append((n["id"], "setup", "no coordinator on this machine (skill: machine-setup)"))
    return out


def write_registry(inst):
    text = registry_text(inst)
    changed = not (inst.root / "registry.md").exists() or (inst.root / "registry.md").read_text() != text
    if changed:
        _m_common.safe_target(inst.root / "registry.md")
        (inst.root / "registry.md").write_text(text)
        _m_common.touch(inst.root / "registry.md")
    return changed


def registry_text(inst):
    rows = ["# Registry", "",
            "Generated by `core/bin/brain registry` from every `node.md`. Do not edit; change nodes with "
            "`core/bin/brain`. Live status: `core/bin/brain status`; this machine's IDs: `core/bin/brain show <node>`.",
            "",
            "| Node | Title | Owners | Sealed | Labels | Runtime | Home |",
            "| --- | --- | --- | --- | --- | --- | --- |"]
    for n in inst.nodes():
        fm = n["fm"]
        home = fm.get("home", "here")
        if n["external"]:
            home = f"external{' (' + fm['home_url'] + ')' if fm.get('home_url', 'none') != 'none' else ''}"
        rows.append(f"| {n['id']} | {fm.get('title', '')} | {', '.join(n['owners'])} | "
                    f"{'yes' if n['sealed'] else ''} | {fm.get('labels', '')} | {fm.get('runtime', '')} | "
                    f"{home} |")
    return "\n".join(rows) + "\n"


def cmd_registry(a):
    inst = _m_nodes.load()
    write_registry(inst)
    print(inst.root / "registry.md")


# ---------- links ----------
