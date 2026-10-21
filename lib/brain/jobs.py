"""Jobs mechanics for Brain."""
import hashlib
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path

from . import briefs as _m_briefs
from . import common as _m_common
from . import guard as _m_guard
from . import hosts as _m_hosts
from . import instance as _m_instance
from . import nodes as _m_nodes
from . import spend as _m_spend
from . import status as _m_status
from . import sync as _m_sync
from . import transitions as _m_transitions
from . import ui as _m_ui
from . import usage as _m_usage
from . import verify as _m_verify


JOBS_FILE = "job-ledger.md"  # in a node folder: one row per job, written by `brain wt` and `brain review`
JOBS_MARK = "<!-- brain:job-ledger v3 -->"  # older cores refuse this format instead of dropping new columns
JOBS_LEGACY_MARKS = ("<!-- brain:job-ledger v2 -->", "<!-- brain:job-ledger v1 -->")
JOB_COLS = ["Job", "Started", "Ended", "Hours", "Outcome", "Commits", "Reviews", "Runtime", "Cause"]
JOB_COLS += ["Lessons", "Lesson check", "Switches", "Settled"]
JOB_COLS += ["Active h", "Agent h", "Fresh in", "Cache read", "Cache write", "Output", "Reasoning", "Tool calls",
             "Compactions", "Sub-agents", "Sub share", "Models", "Budget", "Usage"]


def cell(v):
    return str(v).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def uncell(v):
    return re.sub(r"\\(.)", r"\1", v.strip())


def read_jobs(n):
    """Rows of the node's ledger; None if a file of that name exists but isn't one (brain never overwrites it)."""
    f = n["dir"] / JOBS_FILE
    if not f.exists():
        return []
    text = f.read_text()
    if JOBS_MARK not in text and not any(m in text for m in JOBS_LEGACY_MARKS):
        return None
    rows, columns = [], JOB_COLS
    for line in text.splitlines():
        if line.startswith("| "):
            cells = [uncell(c) for c in re.split(r"(?<!\\)\|", line.strip()[1:-1])]
            if cells[:2] == ["Job", "Started"]:
                columns = cells  # older ledgers have fewer columns
                continue
            if set("".join(cells)) <= set("-: "):
                continue
            rows.append({**dict.fromkeys(JOB_COLS, ""), **dict(zip(columns, cells))})
    return rows


def write_jobs(inst, n, rows):
    # Retained worker evidence repairs old rows too; absent/invalid evidence never
    # changes history. Readiness freshness is unrelated to completed review rounds.
    for row in rows:
        hb = read_handback(n, row) or {}
        ready = hb.get("ready")
        rounds = ready.get("review_rounds") if isinstance(ready, dict) else hb.get("review_rounds")
        if hb.get("state") == "done" and type(rounds) is int and rounds >= 0:
            current = int(row.get("Reviews") or 0) if str(row.get("Reviews") or "0").isdigit() else 0
            # A coordinator can start another review after this hand-back. Retained
            # evidence repairs undercounts, but cannot undo an open job's new rounds.
            row["Reviews"] = str(rounds if row.get("Ended") else max(current, rounds))
    f = n["dir"] / JOBS_FILE
    head = (f"# Jobs: {n['id']}\n\n{JOBS_MARK}\nOne row per job, kept by `brain wt add`, `brain review`, "
            "`brain wt rm` and `brain wt prune`; times carry their UTC offset; tokens are exact counts from this "
            "machine's transcripts. `brain jobs` sums it up.\n\n")
    # Columns this core doesn't know came from a newer one: kept, in their order, so no row ever loses one.
    cols = JOB_COLS + list(dict.fromkeys(k for r in rows for k in r if k not in JOB_COLS))
    out = [head + "| " + " | ".join(cell(c) for c in cols) + " |", "| " + " | ".join("---" for _ in cols) + " |"]
    out += ["| " + " | ".join(cell(r.get(c, "")) for c in cols) + " |" for r in rows]
    _m_common.atomic_write(f, "\n".join(out) + "\n")
    _m_common.touch(f)


def ledger_rows(n):
    rows = read_jobs(n)
    if rows is None:
        print(f"note: {_m_common.tilde(n['dir'] / JOBS_FILE)} isn't a job ledger written by brain; this job isn't recorded",
              file=sys.stderr)
    return rows


def stamp(inst):
    return inst.now_dt().isoformat(timespec="minutes")


def open_job(rows, branch):
    return next((r for r in reversed(rows) if r["Job"] == branch and not r["Ended"]), None)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:24]


def job_key(row):
    return digest([row["Job"], row["Started"]])


def handback_file(n, row):
    return n["dir"] / "job-handbacks" / (job_key(row) + ".json")


def read_handback(n, row):
    f = handback_file(n, row)
    try:
        data = json.loads(f.read_text())
        if (isinstance(data, dict) and data.get("job") == row["Job"] and
                data.get("started") == row["Started"] and data.get("state") in ("done", "failed", "blocked", "question") and
                data.get("version") in (None, 2) and (data.get("version") != 2 or
                (isinstance(data.get("lessons"), str) and isinstance(data.get("ready_reasons", []), list) and
                 (data.get("ready") is None or isinstance(data["ready"], dict))))):
            return data
    except (OSError, ValueError):
        pass
    return None


def ready_fingerprint(n, row, packet):
    """A new reviewed hand-back permits retry without changing a worker-owned file externally."""
    signal = read_handback(n, row) or {}
    return digest([packet, signal.get("verify_runs"), signal.get("verify_reused"), signal.get("at")])


def train_ejection(n, row, packet):
    """Shared admission guard for both ordinary readiness and train retries."""
    if not row or not packet:
        return None
    key, fingerprint = job_key(row), ready_fingerprint(n, row, packet)
    for path in (n["dir"] / "job-trains").glob("*.json"):
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("ejected"), list):
                return "train journal unreadable; coordinator must repair it"
            for entry in data["ejected"]:
                if not isinstance(entry, dict):
                    return "train journal unreadable; coordinator must repair it"
                if entry.get("key") == key and entry.get("packet") == fingerprint:
                    return "previously ejected from train; worker must hand back again"
        except (OSError, ValueError):
            return "train journal unreadable; coordinator must repair it"
    return None


def cmd_handback(a):
    """Worker-owned completion, proposed lessons and readiness live in one job-scoped file."""
    inst = _m_nodes.load()
    n = inst.node(a.id)
    row = open_job(read_jobs(n) or [], a.branch)
    if not row:
        _m_common.die(_m_guard.redact(f"no open job for {a.branch} in {n['id']}", _m_guard.never_terms(n)))
    if a.state == "done":
        _m_verify.main_notice(n)
    previous = read_handback(n, row) or {}
    lessons = (a.lessons if a.lessons is not None else previous.get("lessons", "")).strip()
    if a.lessons is not None and (not lessons or lessons == "none:" or lessons.lower() == "none"):
        _m_common.die("--lessons needs proposed lessons, or none: <reason>")
    packet, reasons = None, []
    study = _m_briefs.import_job(row["Job"])
    if a.state == "done" and not study and previous.get("state", "done") == "done":
        packet = _m_verify.read_ready_packet(inst, n, a.branch, row)
        reasons = previous.get("ready_reasons", [])
    if getattr(a, "reverify", False) and not (a.state == "done" and not study and
                                              (a.reviews is not None or getattr(a, "record", False))):
        _m_common.die("--reverify forces a verify run: it goes with --state done --reviews N")
    fresh = reused = 0
    if a.state == "done" and not study and (a.reviews is not None or getattr(a, "record", False)):
        packet, reasons = _m_verify.record_ready(inst, n, a.branch, a.reviews, packet,
                                       getattr(a, "reverify", False))
        if packet:
            reused, fresh = (1, 0) if packet.get("verify_reused") else (0, 1)
    # Verification released the global lock. Re-read under it before publishing so a
    # concurrently closed/restarted job never receives this worker's old result.
    with _m_instance.brain_lock(inst):
        inst = _m_instance.Instance(inst.root)
        n = inst.node(a.id)
        current = open_job(read_jobs(n) or [], a.branch)
        if not current or job_key(current) != job_key(row):
            _m_common.die("job changed during verification; hand back to its coordinator")
        if packet and packet.get("version") == 1 and "integration_base" not in packet:
            preflight, head, main = _m_verify.ready_preflight(n, a.branch)
            if not preflight and packet.get("head") == head and _m_verify.packet_main_matches(n, packet, main):
                packet = {**packet, "main": main, "integration_base": packet["main"]}
        f = handback_file(n, row)
        count = lambda k: previous.get(k) if type(previous.get(k)) is int else 0
        sessions = [i for i in previous.get("sessions", []) if isinstance(i, str) and _m_spend.SESSION_ID.fullmatch(i)] \
            if isinstance(previous.get("sessions"), list) else []
        if isinstance(packet, dict):
            packet.pop("input_files", None)  # never recommit an older experimental inline fingerprint map
        data = {"version": 2, "job": row["Job"], "started": row["Started"], "state": a.state,
                "sessions": list(dict.fromkeys(sessions + _m_spend.spend_session_ids()))[-20:],
                "at": inst.now(), "report": _m_common.tilde(a.report) if a.report is not None else previous.get("report", ""),
                "lessons": lessons, "ready": packet, "ready_reasons": reasons,
                "verify_runs": count("verify_runs") + fresh, "verify_reused": count("verify_reused") + reused}
        first = previous.get("first_ready") if isinstance(previous.get("first_ready"), str) else None
        if not first and packet and packet.get("verify_result") == 0 and not reasons:
            first = inst.now_dt().isoformat()
        if first:
            data["first_ready"] = first
        if study:
            data.update(ready_required=False, review_rounds=a.reviews)
        f.parent.mkdir(exist_ok=True)
        _m_common.atomic_write(f, json.dumps(data, indent=2) + "\n")
        _m_common.touch(f)
        need = getattr(a, "need_human", None)
        question_created = False
        if a.state == "question" or (a.state == "blocked" and need):
            message = " ".join((need or ("See report " + data["report"] if data["report"] else
                                            "Coordinator choice needed; see worker hand-back")).split())
            marker = "<!-- handback:" + digest([job_key(row), a.state, message]) + " -->"
            inbox = n["dir"] / "inbox.md"
            bullet = f"- {inst.now()} from worker {a.branch}: {a.state}: {message} {marker}\n"
            _m_guard.no_terms_in(n, bullet, "the inbox question")
            question_created = _m_transitions.inbox_append(n["dir"], bullet, marker)
            _m_common.touch(inbox)
        inst.commit([], f"{n['id']}: worker hand-back")
    if question_created:
        _m_hosts.request_wake(inst, n, "handback:" + os.urandom(16).hex())
    elif a.state == "blocked" and not need:
        _m_hosts.request_wake(inst, n, "handback:" + os.urandom(16).hex())
    try:
        current = open_job(read_jobs(n) or [], a.branch)
        if current:
            started, used, budget = inst.parse_time(current["Started"]), _m_spend.live_usage(inst, n, current), _m_spend.job_budget(n, current)
            if (used and used["sessions"]) or budget:  # the worker sees its spend against its budget
                print(_m_guard.redact(_m_spend.usage_line(used, budget, _m_status.elapsed(started, inst.now_dt()) / 3600 if started else None),
                             _m_guard.never_terms(n)))
    except (OSError, ValueError, RuntimeError):
        pass  # accounting never blocks a hand-back
    checked = not study and a.state == "done" and (a.reviews is not None or getattr(a, "record", False))
    if checked:
        state = _m_verify.ready_state(inst, n, a.branch)
        why = reasons + state["reasons"]
        print(_m_verify.VerifyScrubber(_m_guard.never_terms(n)).feed(
            "ready" if not why else "not ready: " + "; ".join(dict.fromkeys(why)), final=True))
        if why:
            sys.exit(1)
    else:
        from . import messages as _m_messages
        kind = "blocked" if a.state == "failed" else a.state
        print(_m_guard.redact(_m_messages.format_worker_message(
            kind, "coordinator can see it with brain due", a.branch, data["report"] or _m_common.tilde(f)),
            _m_guard.never_terms(n)))


def job_started(inst, n, branch, runtime):
    with _m_instance.brain_lock(inst):
        rows = ledger_rows(n)
        if rows is not None and not open_job(rows, branch):
            rows.append({"Job": branch, "Started": inst.now_dt().isoformat(), "Reviews": "0", "Runtime": runtime})
            write_jobs(inst, n, rows)
            inst.commit([], f"{n['id']}: job started")


def job_reviewed(inst, n, branch):
    with _m_instance.brain_lock(inst):
        rows = ledger_rows(n)
        r = open_job(rows or [], branch)
        if r:
            r["Reviews"] = str(int(r.get("Reviews") or 0) + 1)
            write_jobs(inst, n, rows)
            inst.commit([], f"{n['id']}: job reviewed")


def published_train(n, row):
    """Publication intent is integration evidence only when its tip actually reached main."""
    home = n.get("home_path")
    if not home:
        return None
    for path in (n["dir"] / "job-trains").glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            _m_common.die("train journal unreadable; coordinator must repair it before closing a job")
        if data.get("version") != 1 or data.get("state") != "verified":
            continue
        tip = data.get("tip")
        if not isinstance(tip, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", tip):
            continue
        if _m_common.git("merge-base", "--is-ancestor", tip, "refs/heads/main", cwd=home).returncode:
            continue
        for entry in data.get("accepted", []):
            if entry.get("key") == job_key(row):
                return data["batch"], entry
    return None


def job_ended(inst, n, branch, outcome, commits="", cause="", worktree=None):
    # Transcripts are read before the lock is taken: a long first read never holds up other commands.
    early = open_job(read_jobs(n) or [], branch)
    usage = None
    if early and not str(early.get("Usage", "")).startswith("host"):  # what the host reported wins
        try:
            usage = _m_spend.usage_cells(_m_spend.job_usage(inst, n, early, worktree), _m_guard.never_terms(n))
        except (OSError, ValueError, RuntimeError) as e:
            usage = {"Usage": f"not counted: {type(e).__name__}"}  # a close never fails for its accounting
    with _m_instance.brain_lock(inst):
        rows = ledger_rows(n)
        r = open_job(rows or [], branch)
        if not r:
            return
        _m_sync.retain_integration(inst, n, r)
        published = published_train(n, r)
        if published:
            batch, entry = published
            outcome, commits = "merged", entry["commits"]
            r.update({"Batch": batch, "Base commit": entry["base"], "Head commit": entry["head"]})
        r.update(Ended=stamp(inst), Outcome=outcome, Commits=str(commits), Cause=cause)
        if outcome == "merged":
            r["Lesson check"] = "required"  # old, already-ended rows never acquire this marker
        a, b = inst.parse_time(r["Started"]), inst.parse_time(r["Ended"])
        r["Hours"] = f"{_m_status.elapsed(a, b) / 3600:.1f}" if a and b else ""
        budget = _m_spend.job_budget(n, r)
        if budget:
            r["Budget"] = _m_spend.budget_text(budget)
        if usage and early and job_key(early) == job_key(r) and not str(r.get("Usage", "")).startswith("host"):
            r.update(usage)
        write_jobs(inst, n, rows)
        if not n["external"] or n["local"].get("workbench") or n["home_path"]:
            _m_verify.ready_path(inst, n, branch).unlink(missing_ok=True)
        cleanup_handback(inst, n, r, rows)
        inst.commit([], f"{n['id']}: job {outcome}")
        print_thread_checklist(n, r)


def cleanup_handback(inst, n, row, rows):
    if row.get("Ended") and (row.get("Outcome") != "merged" or row.get("Lessons")):
        if _m_sync.retain_integration(inst, n, row):
            write_jobs(inst, n, rows)
        for record in (handback_file(n, row), contract_file(n, row)):
            record.unlink(missing_ok=True)
            _m_common.touch(record)


CONTRACT_FIELDS = ("Outcome", "Authorized", "Discretion", "Stop when", "Needs consent", "Done means")


def contract_file(n, row):
    """The coordinator's contract for one job: what the worker may do without asking (one writer: the coordinator)."""
    return n["dir"] / "job-contracts" / (job_key(row) + ".md")


def contract_problems(text):
    """Fields missing or empty in a contract: each `## <field>` needs at least one line under it."""
    sections = {k.lower(): v for k, v in _m_ui.md_sections(text).items()}
    return [f for f in CONTRACT_FIELDS if not sections.get(f.lower(), "").strip()]


def job_input_path(n, value):
    """Canonicalize declarations and recorded context with the same alias policy."""
    import posixpath
    path = (n.get("home_path") or n["dir"]) / Path(value).expanduser()
    try:
        return str(path.resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return posixpath.normpath(str(path))


def job_inputs(n, row):
    """Explicit inputs only: prose and Git refs are not evidence of shared reads."""
    f = contract_file(n, row)
    text = f.read_text() if f.is_file() else ""
    sections = {k.lower(): v for k, v in _m_ui.md_sections(text).items()}
    values = [re.sub(r"^(?:[-*+]|[0-9]+[.)])\s+", "", line.strip()).strip().strip("`")
              for line in sections.get("inputs", "").splitlines() if line.strip()]
    values += n["local"].get("worktrees", {}).get(row["Job"], {}).get("context_inputs", [])
    return {job_input_path(n, v) for v in values
            if v and v.casefold() not in ("none", "n/a", "not applicable", "-")}


def inputs_overlap(left, right, root=""):
    """Only * and ? are patterns; brackets and the home prefix stay literal."""
    def matches(path, pattern):
        prefix = root if root and pattern.startswith(root.rstrip("/") + "/") else ""
        tail = pattern[len(prefix):]
        if "*" not in tail and "?" not in tail:
            return False
        regex = re.escape(prefix) + re.escape(tail).replace(r"\*", ".*").replace(r"\?", ".")
        return re.fullmatch(regex, path) is not None
    return any(p == q or p.startswith(q.rstrip("/") + "/") or q.startswith(p.rstrip("/") + "/")
               or matches(p, q) or matches(q, p) for p in left for q in right)


def job_overlaps(n, rows, include_resolved=False):
    """Shared context resolves the advice; jobs may still show that softer fact."""
    def context(row):
        return {job_input_path(n, v) for v in n["local"].get("worktrees", {}).get(row["Job"], {}).get("context_inputs", [])}
    opened = [(r, job_inputs(n, r), context(r)) for r in rows if not r["Ended"]]
    out = {}
    for row, inputs, shared in opened:
        peers, resolved = set(), set()
        for peer, other, peer_shared in opened:
            if job_key(peer) == job_key(row) or not inputs_overlap(inputs, other, root=job_input_path(n, ".")):
                continue
            if shared & peer_shared:
                resolved.add(peer["Job"])
            else:
                peers.add(peer["Job"])
        lines = []
        if peers:
            lines.append("overlapping inputs with " + ", ".join(sorted(peers)) +
                         ": merge related jobs or share a context brief")
        if include_resolved and resolved:
            lines.append("shares context with " + ", ".join(sorted(resolved)) +
                         "; overlap advice resolved; merge if one concern")
        if lines:
            out[job_key(row)] = "; ".join(lines)
    return out


def contract_lines(inst, n, branch, who="worker"):
    row = open_job(read_jobs(n) or [], branch) if branch else None
    f = contract_file(n, row) if row else None
    if not f or not f.is_file():
        return []
    text = _m_guard.redact(f.read_text().strip(), _m_guard.never_terms(n))
    if who == "reviewer":
        return ["## The job's contract", "", "Check the change against its outcome and what done means:", "", text, ""]
    budget = _m_spend.contract_budget(text)
    return ["## Contract for this job", "", text, "",
            "Within this contract, act: don't ask for what it authorizes, and use your discretion where it gives it. "
            "Record results and decisions in the report. Follow the coordinator message protocol below. "
            "Use the hand-back path for a stop condition or something it says needs consent.", ""] + (
        [f"Its budget is {_m_spend.budget_text(budget)}. `brain jobs {shlex.quote(n['id'])} --branch {shlex.quote(branch)}` "
         "shows what this job has spent so far; check it before long steps, and say in your report how it went "
         "against the budget (`brain handback` prints it).", ""] if budget else [])


def handback_detail(n, row):
    signal = read_handback(n, row) or {}
    return f"lessons: {signal.get('lessons') or 'not recorded'}; report: {signal.get('report') or 'not recorded'}"


def print_job_handback(n, branch):
    row = open_job(read_jobs(n) or [], branch)
    if row:
        print(_m_guard.redact(f"{branch}: {handback_detail(n, row)}", _m_guard.never_terms(n)), flush=True)


def closed_job(row):
    """Only explicit terminal ledger outcomes qualify; a host idle state is never closure."""
    return bool(row.get("Ended") and row.get("Outcome") in ("merged", "abandoned", "studied", "closed"))


def unsettled_jobs(rows):
    return [r for r in rows or [] if closed_job(r) and not r.get("Settled")]


def print_thread_checklist(n, row):
    if not closed_job(row):
        return
    matches = [r for r in _m_status.active_work(n, include_closed=True) if r[0] == row["Job"]]
    branch_jobs = [r for r in read_jobs(n) or [] if r["Job"] == row["Job"]]
    ambiguous = (len(branch_jobs) != 1 or len(matches) != 1 or
                 len(matches[0]) < 4 or matches[0][3].lower() not in ("merged", "closed", "done", "abandoned"))
    worker = ("ownership ambiguous or not recorded; confirm by job marker; no thread selected"
              if ambiguous else matches[0][1])
    text = (f"Threads to settle for {row['Job']} (brain-job: {job_key(row)}):\n"
            + "  Worker from Active work: " + worker + "\n"
            + "  Find delegated/sub-agent descendants by parentThreadId and reviewers by the brain-job marker.\n"
            + "  Settle only when the ledger says merged or closed (abandoned/studied count as closed), "
            "no thread is running or waiting, and no pending requests remain. "
            "Never settle coordinators or threads belonging to open jobs. "
            "Check every candidate through the host; unknown state means leave unsettled.\n"
            + f"  After every job thread is safely settled: brain jobs {n['id']} --branch {row['Job']} --job-key {job_key(row)} --settled")
    print(_m_guard.redact(text, _m_guard.never_terms(n)))


def jobs_summary(inst, n):
    """Finished and open jobs of one node, with medians, for `brain jobs` and the live view."""
    import statistics
    all_rows = read_jobs(n) or []
    rows = [r for r in all_rows if r.get("Outcome") != "external"]
    done = [r for r in rows if r["Ended"]]
    hours = [float(r["Hours"]) for r in done if r["Outcome"] == "merged" and r["Hours"]]
    reviews = [int(r["Reviews"] or 0) for r in done if r["Outcome"] == "merged"]
    measured = [u for u in (_m_spend.row_usage(r) for r in done if r["Outcome"] == "merged") if u and u["processed"]]
    def med(xs):
        if not xs:
            return None
        v = round(statistics.median(xs), 1)
        return int(v) if v == int(v) and all(isinstance(x, int) for x in xs) else v
    now = inst.now_dt()
    open_ = [{**r, "age_h": round(_m_status.elapsed(inst.parse_time(r["Started"]), now) / 3600, 1)}
             for r in rows if not r["Ended"] and inst.parse_time(r["Started"])]
    return {"rows": rows[-40:], "open": open_, "finished": len(done),
            "external": len(all_rows) - len(rows),
            "merged": sum(1 for r in done if r["Outcome"] == "merged"),
            "abandoned": sum(1 for r in done if r["Outcome"] == "abandoned"),
            "unsettled": len(unsettled_jobs(rows)),
            "median_hours": med(hours), "median_reviews": med(reviews),
            "median_tokens": int(statistics.median([u["processed"] for u in measured])) if measured else None,
            "median_active_h": med([u["active_h"] for u in measured]),
            "measured": len(measured),
            "causes": [r["Cause"] for r in done if r["Cause"]][-10:],
            "forecasts": job_forecasts(rows)}


def job_kind(branch, rows=()):
    # Generic job/ says nothing about the task. Repeated slug families do.
    if branch.startswith("import:"):
        return "import"
    prefix, _, name = branch.lower().partition("/")
    if prefix != "job":
        return prefix if name else "unclassified"
    token = name.split("-", 1)[0]
    repeats = sum(r["Job"].lower().startswith("job/" + token + "-") or
                  r["Job"].lower() == "job/" + token for r in rows)
    return token if token and not token.isdigit() and repeats >= 2 else "unclassified"


def job_forecasts(rows):
    """Per kind of job, what similar merged jobs took: wall hours, and where measured, active hours and tokens."""
    import statistics
    groups = {}
    for row in rows:
        if row.get("Ended") and row.get("Outcome") == "merged":
            try:
                hours = float(row.get("Hours", ""))
            except ValueError:
                continue
            if not 0 <= hours < float("inf"):
                continue
            u = _m_spend.row_usage(row)
            sample = (hours, u if u and u["processed"] else None)
            groups.setdefault("unclassified", []).append(sample)  # the explicit all-jobs fallback
            kind = job_kind(row["Job"], rows)
            if kind != "unclassified":
                groups.setdefault(kind, []).append(sample)
    out = []
    for kind, xs in sorted(groups.items()):
        hours, used = [h for h, _ in xs], [u for _, u in xs if u]
        f = {"kind": kind, "samples": len(xs), "median_h": round(statistics.median(hours), 1),
             "min_h": min(hours), "max_h": max(hours), "enough_data": len(xs) >= 3, "measured": len(used)}
        if len(used) >= 3:
            tokens = [u["processed"] for u in used]
            f.update(median_active_h=round(statistics.median([u["active_h"] for u in used]), 1),
                     median_tokens=int(statistics.median(tokens)), min_tokens=min(tokens), max_tokens=max(tokens),
                     median_fresh_out=int(statistics.median([u["fresh"] + u["output"] for u in used])))
        out.append(f)
    return out


def forecast_lines(rows, planned=None):
    forecasts = {f["kind"]: f for f in job_forecasts(rows)}
    kinds = {job_kind(r["Job"], rows) for r in rows}
    if planned:
        kinds.add(job_kind(planned, rows))
    out = []
    for kind in sorted(kinds):
        f = forecasts.get(kind, {"samples": 0, "enough_data": False})
        label = f"{kind}: {f['samples']} similar jobs" if kind != "unclassified" else f"all jobs on this node: {f['samples']} jobs"
        out.append(label + ", " +
                   (f"median {f['median_h']:g} h, spread {f['min_h']:g}–{f['max_h']:g} h (observed min–max)"
                    if f["enough_data"] else "too little data (need 3 merged jobs)")
                   + (f"; of which {f['median_active_h']:g} h active; {_m_spend.tokens_text(f['median_tokens'])} tokens processed "
                      f"(spread {_m_spend.tokens_text(f['min_tokens'])}–{_m_spend.tokens_text(f['max_tokens'])}), of them "
                      f"{_m_spend.tokens_text(f['median_fresh_out'])} fresh input and output; from {f['measured']} measured jobs"
                      if f.get("median_tokens") else ""))
    return out


def job_budget_lines(inst, n, planned=None):
    """Compare ledger wall time with fresh routed reset windows, never infer token allowance."""
    rows = read_jobs(n) or []
    jobs = [r for r in rows if not r["Ended"]]
    if planned and not any(r["Job"] == planned for r in rows):
        jobs.append({"Job": planned, "Runtime": n["fm"].get("runtime", "")})
    forecasts = {f["kind"]: f for f in job_forecasts(rows) if f["enough_data"]}
    if not jobs or any(job_kind(r["Job"], rows) not in forecasts for r in jobs):
        return []
    out = []
    for runtime in sorted({r.get("Runtime") or n["fm"].get("runtime") or inst.cfg.get("default_runtime", "") for r in jobs}):
        selected, _ = _m_usage.usage_route(inst, [runtime] if runtime else inst.runtimes, preferred=runtime, cached_only=True)
        if not selected:
            continue
        try:
            _, policy = _m_usage.usage_rows(inst)
        except (OSError, ValueError):
            continue
        windows = [w for name, w in selected["windows"].items() if name in _m_usage.USAGE_WINDOWS and
                   not w.get("absent") and not w["expired"] and w.get("resets_at") and
                   w.get("source", selected["source"]) != "unknown" and
                   w.get("read_at", selected["read_at"]) and
                   0 <= time.time() - w.get("read_at", selected["read_at"]) <= policy["max_age_seconds"]]
        if not windows:
            continue
        these = [r for r in jobs if (r.get("Runtime") or n["fm"].get("runtime") or inst.cfg.get("default_runtime", "")) == runtime]
        estimate = sum(forecasts[job_kind(r["Job"], rows)]["median_h"] for r in these)
        remaining = min((w["resets_at"] - time.time()) / 3600 for w in windows)
        out.append(f"current route {selected['label']}: {len(these)} open/planned job(s), typical serial full-job time {estimate:g} h "
                   f"{'fits' if estimate <= remaining else 'exceeds'} {remaining:.1f} h until the next recorded reset; "
                   "elapsed time not subtracted; allowance fit unknown (the subscription says how full it is, not "
                   "how many tokens a percent is).")
    return out


def cmd_jobs(a):
    inst = _m_nodes.load()
    if a.job_key and not (a.id and a.branch):
        _m_common.die("--job-key needs a node and --branch")
    if a.job_key and any((a.start, a.close, a.lessons is not None, a.switch is not None, a.contract_file, a.usage_file)):
        _m_common.die("--job-key supports inspection or --settled only")
    if a.all_closed and not (a.settled and a.id):
        _m_common.die("--all-closed needs a node and --settled")
    if a.all_closed and (a.branch or a.job_key):
        _m_common.die("--all-closed cannot combine with --branch or --job-key")
    if a.settled:
        if not a.id or (not a.branch and not a.all_closed):
            _m_common.die("--settled needs a node and --branch")
        if any((a.start, a.close, a.lessons is not None, a.switch is not None, a.contract_file, a.usage_file)):
            _m_common.die("--settled cannot combine with other job updates")
        n = inst.node(a.id)
        with _m_instance.brain_lock(inst):
            rows = ledger_rows(n)
            if a.all_closed:
                if rows is None:
                    _m_common.die("--all-closed requires a recognized job ledger")
                targets = unsettled_jobs(rows)
                if any(open_job(rows, r["Job"]) for r in targets):
                    _m_common.die("--all-closed refused: a branch to stamp has an open job; no rows changed")
                if targets:
                    settled_at = stamp(inst)
                    for row in targets:
                        row["Settled"] = settled_at
                    write_jobs(inst, n, rows)
                    inst.commit([], f"{n['id']}: closed job thread sweep recorded")
                print(f"{n['id']}: {len(targets)} closed job(s) recorded settled (coordinator's complete host sweep)")
                return
            matches = [r for r in rows or [] if r["Job"] == a.branch and
                       (not a.job_key or job_key(r) == a.job_key)]
            if len(matches) > 1 and not a.job_key:
                _m_common.die("branch has multiple jobs; select the printed --job-key")
            row = matches[-1] if matches else None
            if a.job_key and not row:
                _m_common.die("no job with that --job-key on that branch")
            if not row or not closed_job(row):
                _m_common.die("--settled requires a merged or closed ledger job")
            if open_job(rows, a.branch):
                _m_common.die("--settled refused: another job on that branch is open; leave unsettled until it closes")
            if not row.get("Settled"):
                row["Settled"] = stamp(inst)
                write_jobs(inst, n, rows)
                inst.commit([], f"{n['id']}: job threads settled")
        print(_m_guard.redact(f"{n['id']}: threads for {a.branch} recorded settled (coordinator's host check)", _m_guard.never_terms(n)))
        return
    if a.branch and not a.id:
        _m_common.die("--branch needs a node: brain jobs <node> --branch <branch>")
    if a.contract_file:
        if not a.id or not a.branch:
            _m_common.die("a contract needs a node and --branch: brain jobs <node> --branch <branch> --contract-file <file>")
        n = inst.node(a.id)
        row = open_job(read_jobs(n) or [], a.branch)
        if not row:
            _m_common.die(_m_guard.redact(f"no open job for {a.branch} in {n['id']}", _m_guard.never_terms(n)))
        text = Path(a.contract_file).read_text().strip() + "\n"
        missing = contract_problems(text)
        if missing:
            _m_common.die("the contract needs a line under each of: " + ", ".join(f"## {f}" for f in missing))
        _m_guard.no_terms_in(n, text, "the contract")
        if _m_nodes.SECRET_LINE.search(text):
            _m_common.die("unsafe contract text")
        f = contract_file(n, row)
        f.parent.mkdir(exist_ok=True)
        _m_common.atomic_write(f, text)
        inst.commit([f], f"{n['id']}: contract for {a.branch}")
        print(_m_guard.redact(f"{n['id']}: contract for {a.branch} recorded; briefs for this job carry it", _m_guard.never_terms(n)))
        return
    if a.usage_file:
        if not a.id or not a.branch:
            _m_common.die("host usage needs a node and --branch: brain jobs <node> --branch <branch> --usage-file <file>")
        n = inst.node(a.id)
        cells = _m_spend.host_usage_cells(a.usage_file, n)
        with _m_instance.brain_lock(inst):
            rows = ledger_rows(n)
            r = next((r for r in reversed(rows or []) if r["Job"] == a.branch), None)
            if not r:
                _m_common.die("no job with that branch in the ledger")
            r.update(cells)
            write_jobs(inst, n, rows)
            inst.commit([], f"{n['id']}: job usage from the host")
        print(_m_guard.redact(f"{n['id']}: usage for {a.branch} recorded as the host reports it", _m_guard.never_terms(n)))
        return
    if a.start or a.close or a.lessons is not None or a.switch is not None:
        if not a.id or not a.branch:
            _m_common.die("job updates need a node and --branch")
        if a.start or a.close:
            n = inst.node(a.id)
            if not _m_briefs.import_job(a.branch):
                _m_common.die("--start and --close apply only to import:<snapshot> studies")
            if a.start:
                if a.close or not n["external"] or not n["home_path"] or a.branch != _m_briefs.import_job_id(n["home_path"]):
                    _m_common.die("--start needs the current import snapshot and cannot combine with --close")
                job_started(inst, n, a.branch, n["fm"].get("runtime", ""))
        n = inst.node(a.id)
        rows = ledger_rows(n)
        r = next((r for r in reversed(rows or []) if r["Job"] == a.branch), None)
        if not r:
            _m_common.die("no job with that branch in the ledger")
        if a.lessons is not None:
            outcome = a.lessons.strip()
            if outcome.startswith("none:"):
                if not outcome[5:].strip():
                    _m_common.die("none needs a reason")
            else:
                ids = _m_common.lst(outcome)
                # A proposed import lesson is a filed outcome, even before review permits its use.
                items = {it["fm"].get("id"): it for it in inst.items()}
                if not ids or any(i not in items or items[i]["fm"].get("source_node") != n["id"] for i in ids):
                    _m_common.die("lessons must be item ids filed via learn from this node, or none: <reason>")
                outcome = ", ".join(ids)
            r["Lessons"] = outcome
        if a.switch is not None:
            switch = a.switch.strip()
            if not re.fullmatch(r"[^\s:;]+[ \t]+->[ \t]+[^\s:;]+:[ \t]*\S.*", switch):
                _m_common.die("--switch needs old -> new: reason")
            r["Switches"] = "; ".join(filter(None, [r.get("Switches"), f"{stamp(inst)} {switch}"]))
        write_jobs(inst, n, rows)
        cleanup_handback(inst, n, r, rows)
        inst.commit([], f"{n['id']}: job outcome")
        if a.close:
            if not open_job(rows or [], a.branch):
                _m_common.die("no open import study to close")
            job_ended(inst, n, a.branch, "studied", cause="coordinator processed import hand-back")
        print(_m_guard.redact(f"{n['id']}: updated job {a.branch}", _m_guard.never_terms(n)))
        return
    if a.job_key and not any(r["Job"] == a.branch and job_key(r) == a.job_key
                             for r in read_jobs(inst.node(a.id)) or []):
        _m_common.die("no job with that --job-key on that branch")
    for n in inst.nodes():
        if a.id and n["id"] != a.id:
            continue
        j = jobs_summary(inst, n)
        if not j["rows"] and not a.branch:
            continue
        print(f"{n['id']}: {j['finished']} finished ({j['merged']} merged, {j['abandoned']} abandoned); {j['unsettled']} closed job(s) with unsettled threads"
              + (f"; median {j['median_hours']} h and {j['median_reviews']} review round(s) per merged job"
                 if j["median_hours"] is not None else "")
              + (f"; median {_m_spend.tokens_text(j['median_tokens'])} tokens and {j['median_active_h']} active h "
                 f"({_m_spend.count_of(j['measured'], 'measured job')})" if j["median_tokens"] else ""))
        if not a.branch:
            backlog = unsettled_jobs(read_jobs(n))
            for r in backlog[:10]:
                print(_m_guard.redact(f"  unsettled {r['Job']} (brain-job: {job_key(r)}); inspect --branch {r['Job']} --job-key {job_key(r)}",
                             _m_guard.never_terms(n)))
            if len(backlog) > 10:
                print(f"  … and {len(backlog) - 10} more unsettled job(s)")
        overlaps = job_overlaps(n, j["open"], include_resolved=True)
        for r in j["open"]:
            u = _m_spend.live_usage(inst, n, r)
            late = j["median_hours"] is not None and r["age_h"] > 2 * j["median_hours"]
            heavy = bool(u and j["median_tokens"] and u["processed"] > 2 * j["median_tokens"])
            print(_m_guard.redact(f"  open  {r['Job']:<28} {r['age_h']:>5} h  {r['Reviews'] or 0} review(s)"
                         + (f"  {_m_spend.tokens_text(u['processed'])} tokens" if u and u["sessions"] else "")
                         + ("   (over twice the median" + (" in hours and tokens)" if heavy else ")") if late else
                            "   (over twice the median in tokens)" if heavy else ""), _m_guard.never_terms(n)))
            if job_key(r) in overlaps:
                print(_m_guard.redact("        " + r["Job"] + ": " + overlaps[job_key(r)], _m_guard.never_terms(n)))
            for line in job_advice(inst, n, r) + [_m_spend.usage_line(u, _m_spend.job_budget(n, r), r["age_h"])] + \
                    _m_spend.usage_advice(u, j["median_tokens"]):
                print(_m_guard.redact("        " + line, _m_guard.never_terms(n)))
        for c in j["causes"]:
            print(f"  cause {c}")
        for line in forecast_lines(read_jobs(n) or [], a.branch) + job_budget_lines(inst, n, a.branch):
            print(_m_guard.redact("  " + line, _m_guard.never_terms(n)))
        for r in read_jobs(n) or []:
            if a.branch and r["Job"] == a.branch and (not a.job_key or job_key(r) == a.job_key):
                print(_m_guard.redact(json.dumps(r, ensure_ascii=False), _m_guard.never_terms(n)))
                if closed_job(r) and not r.get("Settled"):
                    print_thread_checklist(n, r)


REVIEW_CAP = 3  # rounds, as the review skill says


def job_advice(inst, n, row):
    """What the records say about an open job's process: advice from observed events, never a gate."""
    out, hb = [], read_handback(n, row) or {}
    reviews = int(row.get("Reviews") or 0) if str(row.get("Reviews") or "0").isdigit() else 0
    if reviews > REVIEW_CAP:
        out.append(f"advice: {reviews} review rounds, over the cap of {REVIEW_CAP}: audit each finding's whole class "
                   "before another round")
    runs, reused = (hb.get(k) if type(hb.get(k)) is int else 0 for k in ("verify_runs", "verify_reused"))
    if runs or reused:
        out.append(f"verify: {runs} run(s), {reused} reused" + (
            "; several full runs: set verify_skip for paths that can't affect it, or batch the fixes" if runs > 2 else ""))
    started, first = inst.parse_time(row.get("Started", "")), inst.parse_time(hb.get("first_ready") or "")
    if started and first:
        out.append(f"first ready after {round(_m_status.elapsed(started, first) / 3600, 1)} h")
    elif started and not contract_file(n, row).is_file():
        out.append("no contract: brain jobs <node> --branch <branch> --contract-file <file> says what the worker may do "
                   "without asking")
    return out
