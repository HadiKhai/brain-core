"""Jobs mechanics for Brain."""
import hashlib
import json
import math
import os
import re
import shlex
import sys
import time
from pathlib import Path

from . import briefs as _m_briefs
from . import common as _m_common
from . import docker_leftovers as _m_docker_leftovers
from . import guard as _m_guard
from . import hosts as _m_hosts
from . import instance as _m_instance
from . import machine as _m_machine
from . import nodes as _m_nodes
from . import review as _m_review
from . import spend as _m_spend
from . import status as _m_status
from . import sync as _m_sync
from . import transitions as _m_transitions
from . import ui as _m_ui
from . import tasks as _m_tasks
from . import usage as _m_usage
from . import verify as _m_verify


JOBS_FILE = "job-ledger.md"  # in a node folder: one row per job, written by `brain wt` and `brain review`
JOBS_MARK = "<!-- brain:job-ledger v3 -->"  # older cores refuse this format instead of dropping new columns
JOBS_LEGACY_MARKS = ("<!-- brain:job-ledger v2 -->", "<!-- brain:job-ledger v1 -->")
JOB_COLS = ["Job", "Started", "Ended", "Hours", "Outcome", "Commits", "Reviews", "Runtime", "Cause"]
JOB_COLS += ["Class", "Model", "Effort"]
JOB_COLS += ["Files", "Lines", "Forecast", "Actual", "Meter %", "Verify s", "Verify reused", "Verify runs", "Verify reuses", "Queue s"]
JOB_COLS += ["Lessons", "Lesson check", "Switches", "Settled"]
JOB_COLS += ["Land check"]
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
        for field, key in (("Verify runs", "verify_runs"), ("Verify reuses", "verify_reused")):
            if type(hb.get(key)) is int and hb[key] >= 0:
                row[field] = str(hb[key])
        queued = forecast_number(hb.get("admission_wait_seconds_total"))
        if queued is not None:
            row["Queue s"] = str(queued)
        duration = hb.get("verify_duration")
        if isinstance(duration, dict) and type(duration.get("reused")) is bool:
            seconds = forecast_number(duration.get("elapsed_seconds"))
            if seconds is not None:
                row["Verify s"] = str(seconds)
                row["Verify reused"] = str(duration["reused"]).lower()
        actual = clean_actual(hb.get("actual"))
        if hb.get("state") == "done" and actual:
            row["Actual"] = json.dumps(actual, sort_keys=True)
            size = actual.get("size") if isinstance(actual.get("size"), dict) else {}
            for key in ("files", "lines"):
                if type(size.get(key)) is int and size[key] >= 0:
                    row[key.title()] = str(size[key])
            # Closing supplies final accounting; hand-back is the earlier snapshot.
            if row.get("Ended"):
                refreshed = clean_actual(actual_cost(inst, n, row, _m_spend.row_usage(row)))
                actual = {**actual, **{k: v for k, v in refreshed.items() if v is not None}}
                row["Actual"] = json.dumps(actual, sort_keys=True)
    f = n["dir"] / JOBS_FILE
    head = (f"# Jobs: {n['id']}\n\n{JOBS_MARK}\nOne row per job, kept by `brain wt add`, `brain review`, "
            "`brain wt rm` and `brain wt prune`; times carry their UTC offset; tokens are exact counts from this "
            "machine's transcripts. `brain jobs` sums it up.\n\n")
    # Columns this core doesn't know came from a newer one: kept, in their order, so no row ever loses one.
    cols = JOB_COLS + list(dict.fromkeys(k for r in rows for k in r if k not in JOB_COLS))
    out = [head + "| " + " | ".join(cell(c) for c in cols) + " |", "| " + " | ".join("---" for _ in cols) + " |"]
    out += ["| " + " | ".join(cell(r.get(c, "")) for c in cols) + " |" for r in rows]
    from . import events
    events.write_record(inst, n, "jobs", "ledger", "\n".join(out) + "\n")
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


def read_handback(n, row, strict=False):
    f = handback_file(n, row)
    try:
        data = json.loads(f.read_text())
        if (isinstance(data, dict) and data.get("job") == row["Job"] and
                data.get("started") == row["Started"] and data.get("state") in ("done", "failed", "blocked", "question") and
                data.get("version") in (None, 2, 3) and (data.get("version") not in (2, 3) or
                (isinstance(data.get("lessons"), str) and isinstance(data.get("ready_reasons", []), list) and
                 (data.get("ready") is None or isinstance(data["ready"], dict)))) and
                (data.get("version") != 3 or (one_line(data.get("summary")) and
                 (data["state"] != "question" or one_line(data.get("question")))))):
            return data
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        if strict:
            raise  # pending revalidation must distinguish unknown reads from confirmed stale state
    return None


def job_signal(inst, n, row, strict=False):
    """Effective worker signal, including supported legacy completion evidence.

    Any newer hand-back file takes precedence, including an unreadable one.
    Consumers decide whether an acknowledged compaction restart is still blocking.
    """
    from . import job_attempts as _m_attempts
    attempt = _m_attempts.current_attempt(n, row, strict=strict)
    signal = read_handback(n, row, strict=strict)
    if attempt and (attempt['state'] != 'terminal' or not signal or signal.get('attempt_id') != attempt['id']):
        return None
    if (not row['Ended'] and not handback_file(n, row).exists()
            and _m_verify.read_ready_packet(inst, n, row['Job'], row)):
        return {'state': 'done', 'report': '', 'legacy_ready': True}
    return signal


def ready_fingerprint(n, row, packet):
    """A new reviewed hand-back permits retry without changing a worker-owned file externally."""
    signal = read_handback(n, row) or {}
    return digest([packet, signal.get("verify_runs"), signal.get("verify_reused"), signal.get("at")])


def train_ejection(n, row, packet, strict=False):
    """Shared admission guard for both ordinary readiness and train retries."""
    if not row or not packet:
        return None
    key, fingerprint = job_key(row), ready_fingerprint(n, row, packet)
    for path in (n["dir"] / "job-trains").glob("*.json"):
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("ejected"), list):
                if strict:
                    raise ValueError("train journal unreadable")
                return "train journal unreadable; coordinator must repair it"
            for entry in data["ejected"]:
                if not isinstance(entry, dict):
                    if strict:
                        raise ValueError("train journal unreadable")
                    return "train journal unreadable; coordinator must repair it"
                if entry.get("key") == key and entry.get("packet") == fingerprint:
                    return "previously ejected from train; worker must hand back again"
        except (OSError, ValueError):
            if strict:
                raise
            return "train journal unreadable; coordinator must repair it"
    return None


def one_line(value):
    return isinstance(value, str) and bool(value.strip()) and not any(c in value for c in '\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029')


def validate_handback_text(a):
    summary, question = getattr(a, 'summary', None), getattr(a, 'question', None)
    if not one_line(summary):
        _m_common.die('--summary needs a nonempty one-line summary')
    if a.state == 'question' and not one_line(question):
        _m_common.die('--state question requires a nonempty one-line --question')
    if question is not None and not one_line(question):
        _m_common.die('--question needs a nonempty one-line question')
    return summary.strip(), question.strip() if question is not None else None


def validate_handback_options(a):
    """Reject invalid completion flags before attempts, queueing or heavy work."""
    validate_handback_text(a)
    reviews = getattr(a, 'reviews', None)
    checking = a.state == 'done' and (reviews is not None or getattr(a, 'record', False))
    if checking and (type(reviews) is not int or reviews < 1):
        _m_common.die('--record requires --reviews N completed review rounds (at least one)')
    if getattr(a, 'restart', None) and a.state != 'blocked':
        _m_common.die('--restart requires --state blocked')
    if getattr(a, 'reverify', False) and not checking:
        _m_common.die('--reverify requires --state done --reviews N')
    if getattr(a, 'full_receipt', None) and (not checking or getattr(a, 'reverify', False)):
        _m_common.die('--full-receipt requires --state done --reviews N and excludes --reverify')
    lessons = getattr(a, 'lessons', None)
    if lessons is not None and (not lessons.strip() or lessons.strip().lower() in ('none', 'none:')):
        _m_common.die('--lessons needs proposed lessons, or none: <reason>')
    if getattr(a, 'report', None) is not None and not one_line(a.report):
        _m_common.die('--report requires a nonempty one-line path')


def cmd_handback(a):
    from . import job_attempts as _m_attempts
    from . import handback_queue as _m_queue
    validate_handback_options(a)
    inst = _m_nodes.load()
    n = inst.node(a.id)
    row = open_job(read_jobs(n) or [], a.branch)
    if not row:
        _m_common.die('no open job for handback')
    _m_guard.no_terms_in(n, a.summary, 'handback summary')
    if getattr(a, 'question', None):
        _m_guard.no_terms_in(n, a.question, 'handback question')
    if getattr(a, 'enqueue', False):
        try:
            request = _m_queue.enqueue_handback(inst, n, row, a)
        except (OSError, ValueError) as error:
            _m_common.die(_m_guard.redact(str(error), _m_guard.never_terms(n)))
        print(request['id'])
        return
    identity = getattr(a, '_attempt_id', None) if getattr(a, '_queue_request', None) else None
    try:
        if identity:
            attempt = _m_attempts.current_attempt(n, row)
            if not attempt or attempt['id'] != identity or not _m_attempts.attempt_matches(n, row, attempt):
                _m_common.die('queued attempt changed; no handback published')
        else:
            attempt = _m_attempts.begin_attempt(inst, n, row)
            identity = attempt['id']
    except (OSError, ValueError) as error:
        _m_common.die(_m_guard.redact(str(error), _m_guard.never_terms(n)))
    a._attempt_id = identity
    context_token = _m_attempts.ATTEMPT_CONTEXT.set((inst, n, row, identity))
    try:
        return publish_handback(a)
    finally:
        # Terminal publication retires before returning (including a not-ready exit).
        # Exceptions retire only this attempt, never a concurrent replacement.
        try:
            _m_attempts.finish_attempt(inst, n, row, identity, 'interrupted')
        except (OSError, ValueError):
            pass  # unreadable/ambiguous evidence stays unknown; never overwrite it
        finally:
            _m_attempts.ATTEMPT_CONTEXT.reset(context_token)


def publish_handback(a):
    """Worker-owned completion, proposed lessons and readiness live in one job-scoped file."""
    inst = _m_nodes.load()
    n = inst.node(a.id)
    row = open_job(read_jobs(n) or [], a.branch)
    if not row:
        _m_common.die(_m_guard.redact(f"no open job for {a.branch} in {n['id']}", _m_guard.never_terms(n)))
    if a.state == "done":
        _m_verify.main_notice(n)
    if getattr(a, "restart", None) and a.state != "blocked":
        _m_common.die("--restart requires --state blocked")
    previous = read_handback(n, row) or {}
    lessons = (a.lessons if a.lessons is not None else previous.get("lessons", "")).strip()
    if a.lessons is not None and (not lessons or lessons == "none:" or lessons.lower() == "none"):
        _m_common.die("--lessons needs proposed lessons, or none: <reason>")
    packet, reasons = None, []
    study = _m_briefs.import_job(row["Job"])
    if a.state == "done" and not study and previous.get("state", "done") == "done":
        packet = _m_verify.read_ready_packet(inst, n, a.branch, row)
        reasons = _m_verify.current_ready_reasons(previous.get("ready_reasons", []))
    if getattr(a, "reverify", False) and not (a.state == "done" and not study and
                                              (a.reviews is not None or getattr(a, "record", False))):
        _m_common.die("--reverify forces a verify run: it goes with --state done --reviews N")
    fresh = reused = 0
    duration = None
    if a.state == "done" and not study and (a.reviews is not None or getattr(a, "record", False)):
        verify_started = time.monotonic()
        waits = []
        timing_token = _m_machine.ADMISSION_TIMING.set(waits)
        try:
            packet, reasons = _m_verify.record_ready(inst, n, a.branch, a.reviews, packet,
                                           getattr(a, "reverify", False), full_receipt=getattr(a, "full_receipt", None))
        finally:
            _m_machine.ADMISSION_TIMING.reset(timing_token)
        duration = record_verify_duration(inst, n, row, packet, time.monotonic() - verify_started)
        duration["queue_seconds"] = 0.0 if duration["reused"] else sum(waits)
        if packet:
            reused, fresh = (1, 0) if packet.get("verify_reused") else (0, 1)
    actual = None
    if a.state == "done" and not study:
        actual = actual_cost(inst, n, row, _m_spend.live_usage(inst, n, row))
        actual["size"] = job_size(n, row, packet)
    # Verification released the global lock. Re-read under it before publishing so a
    # concurrently closed/restarted job never receives this worker's old result.
    with _m_instance.brain_lock(inst):
        inst = _m_instance.Instance(inst.root)
        n = inst.node(a.id)
        current = open_job(read_jobs(n) or [], a.branch)
        if not current or job_key(current) != job_key(row):
            _m_common.die("job changed during verification; hand back to its coordinator")
        from . import job_attempts as _m_attempts
        attempt = _m_attempts.current_attempt(n, current)
        binding_matches = attempt and _m_attempts.attempt_matches(n, current, attempt)
        if (not binding_matches and attempt and packet is None and reasons and not study
                and a.state == 'done' and (a.reviews is not None or getattr(a, 'record', False))):
            # A native failed input check carries diagnostics, never readiness.
            # Permit only input drift; refs/config/job/attempt ownership stay fenced.
            diagnostic_binding = {**attempt['binding'],
                                  'inputs': _m_attempts.attempt_binding(n, current)['inputs']}
            binding_matches = _m_attempts.attempt_matches(n, current,
                {**attempt, 'binding': diagnostic_binding})
        if (not attempt or attempt['id'] != a._attempt_id or attempt['state'] not in _m_attempts.ATTEMPT_ACTIVE
                or not binding_matches):
            detail = _m_guard.redact('; '.join(reasons), _m_guard.never_terms(n))
            _m_common.die('head, base, inputs or attempt changed; no stale handback published' + ('; ' + detail if detail else ''))
        request = getattr(a, '_queue_request', None)
        if request:
            from . import handback_queue as _m_queue
            claim = _m_queue.read_queue_request(n, request['id'])
            if claim.get('state') != 'running' or claim.get('lease') != request['lease']:
                _m_common.die('handback request lease changed; no duplicate publication')
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
        summary, question = validate_handback_text(a)
        data = {"version": 3, "job": row["Job"], "started": row["Started"], "state": a.state,
                "summary": summary, "question": question, "attempt_id": a._attempt_id,
                "sessions": list(dict.fromkeys(sessions + _m_spend.spend_session_ids()))[-20:],
                "at": inst.now(), "report": _m_common.tilde(a.report) if a.report is not None else previous.get("report", ""),
                "lessons": lessons, "ready": packet, "ready_reasons": reasons,
                "verify_runs": count("verify_runs") + fresh, "verify_reused": count("verify_reused") + reused}
        if duration is not None:
            data["verify_duration"] = duration
            prior_wait = forecast_number(previous.get("admission_wait_seconds_total"))
            if prior_wait is not None or not previous.get("verify_runs"):
                data["admission_wait_seconds_total"] = (prior_wait or 0) + duration["queue_seconds"]
        elif previous.get("verify_duration") is not None:
            data["verify_duration"] = previous["verify_duration"]
            if previous.get("admission_wait_seconds_total") is not None:
                data["admission_wait_seconds_total"] = previous["admission_wait_seconds_total"]
        if actual is not None:
            data.update(estimate=saved_forecast(row.get("Forecast")), actual=clean_actual(actual))
        if a.state == "done":
            data["wake_id"] = os.urandom(16).hex()  # each publication is a fresh completion, even after a rebase
        need = getattr(a, "need_human", None)
        inbox_question = a.state == "question" or (a.state == "blocked" and need)
        if inbox_question:
            message = " ".join((question or need or ("See report " + data["report"] if data["report"] else
                                            "Coordinator choice needed; see worker hand-back")).split())
            marker = "<!-- handback:" + digest([job_key(row), a.state, message]) + " -->"
        if a.state in ("blocked", "question"):
            # Re-read under the lock: concurrent hand-backs must share the open episode.
            episode = read_handback(n, current) or {}
            old_id = episode.get("wake_id")
            wake_id = None
            if episode.get("state") == a.state and isinstance(old_id, str) and re.fullmatch(r"[0-9a-f]{32}", old_id):
                if inbox_question:
                    inbox = n["dir"] / "inbox.md"
                    lines = inbox.read_text().splitlines() if inbox.exists() else []
                    owner = next((re.search(r"<!-- wake:([0-9a-f]{32}) -->", line)
                                  for line in lines if marker in line), None)
                    if owner:
                        wake_id = owner[1]  # The matching inbox line owns this episode's identity.
                else:
                    target, _ = _m_hosts.wake_target(inst, n)
                    directory = _m_hosts.wake_directory(inst, target)
                    key = _m_hosts.wake_request_key(inst, n, "wake:" + old_id)
                    if (directory / (key + ".pending")).exists() and not (directory / (key + ".sent")).exists():
                        wake_id = old_id
            data["wake_id"] = wake_id or os.urandom(16).hex()
        if getattr(a, "restart", None):
            data["restart"] = a.restart
            data["restart_id"] = os.urandom(16).hex()
        first = previous.get("first_ready") if isinstance(previous.get("first_ready"), str) else None
        if not first and packet and packet.get("verify_result") == 0 and not reasons:
            first = inst.now_dt().isoformat()
        if first:
            data["first_ready"] = first
        if study:
            data.update(ready_required=False, review_rounds=a.reviews)
        f.parent.mkdir(exist_ok=True)
        from . import events
        events.write_record(inst, n, "handback", job_key(row), json.dumps(data, indent=2) + "\n")
        _m_common.touch(f)
        _m_attempts.finish_attempt(inst, n, current, a._attempt_id, 'terminal')
        request = getattr(a, '_queue_request', None)
        if request:
            from . import handback_queue as _m_queue
            _m_queue.queue_terminal(inst, n, request['id'], request['lease'], 'terminal')
        question_created = False
        if inbox_question:
            wake_id = data["wake_id"]
            inbox = n["dir"] / "inbox.md"
            bullet = f"- {inst.now()} from worker {a.branch}: {a.state}: {message} <!-- wake:{wake_id} --> {marker}\n"
            _m_guard.no_terms_in(n, bullet, "the inbox question")
            question_created = _m_transitions.inbox_append(n["dir"], bullet, marker)
            _m_common.touch(inbox)
        _m_tasks.handback_event(inst, n, row, data)
        inst.commit([], f"{n['id']}: worker hand-back")
    if question_created:
        _m_hosts.request_wake(inst, n, 'wake:' + wake_id)
    elif a.state == "blocked" and not need:
        _m_hosts.request_wake(inst, n, "wake:" + data["wake_id"])
    _m_instance.unlock()  # accounting follows durable publication and does not mutate shared state
    try:
        current = open_job(read_jobs(n) or [], a.branch)
        if current:
            started, used, budget = inst.parse_time(current["Started"]), _m_spend.live_usage(inst, n, current), _m_spend.job_budget(n, current)
            if (used and used["sessions"]) or budget:  # the worker sees its spend against its budget
                print(_m_guard.redact(_m_spend.usage_line(used, budget, _m_status.elapsed(started, inst.now_dt()) / 3600 if started else None,
                                      round_usage=_m_spend.job_round_usage(inst, n, current, used)),
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
            raise _m_common.VerdictExit(1)
    else:
        from . import messages as _m_messages
        kind = "blocked" if a.state == "failed" else a.state
        print(_m_guard.redact(_m_messages.format_worker_message(
            kind, "coordinator can see it with brain due", a.branch, data["report"] or _m_common.tilde(f)),
            _m_guard.never_terms(n)))


JOB_CLASSES = ("build", "diagnose", "review", "ui", "migration", "docs", "mechanical")
JOB_EFFORTS = ("low", "medium", "high", "xhigh")
CLASS_EFFORTS = {"build": "high", "ui": "high", "migration": "high", "docs": "medium",
                 "mechanical": "medium", "review": "high", "diagnose": "xhigh"}


def job_class(n, row=None, explicit=None, default="build"):
    """Resolve declared metadata only; never inspect tasks or message text."""
    def parse(value):
        words = (value or "").strip().split()
        return words[0].strip("`").lower() if words else ""
    value, source = parse(explicit), "--class"
    if not value and row:
        f = contract_file(n, row)
        sections = _m_ui.md_sections(f.read_text()) if f.is_file() else {}
        value = parse(next((v for k, v in sections.items() if k.lower() == "class"), ""))
        source = str(f) + " ## Class"
    if not value:
        stored = parse((row or {}).get("Class"))
        value = stored if stored in JOB_CLASSES else default
        source = "default class"
    if value not in JOB_CLASSES:
        _m_common.die(_m_guard.redact(source, _m_guard.never_terms(n)) +
                      ": job class must be one of: " + ", ".join(JOB_CLASSES))
    return value


def job_effort(n, row, kind, explicit=None):
    """An explicit launch option or contract declaration overrides the class effort."""
    value, source = explicit, "--effort"
    if not value and row:
        path = contract_file(n, row)
        sections = _m_ui.md_sections(path.read_text()) if path.is_file() else {}
        value = next((v.strip().split()[0].strip("`").lower() for k, v in sections.items()
                      if k.lower() == "effort" and v.strip()), None)
        source = str(path) + " ## Effort"
    value = value or CLASS_EFFORTS[kind]
    if value not in JOB_EFFORTS:
        _m_common.die(_m_guard.redact(source, _m_guard.never_terms(n)) +
                      ": effort must be one of: " + ", ".join(JOB_EFFORTS))
    return value


def record_job_route(inst, n, branch, kind, runtime, model, review=False, effort=None):
    """Record a worker's planned route without changing observed Models accounting."""
    if not branch:
        return
    with _m_instance.brain_lock(inst):
        rows = ledger_rows(n)
        row = open_job(rows or [], branch)
        fields = {"Class": kind, "Runtime": runtime, "Model": model}
        if effort:
            fields["Effort"] = effort
        if review:
            fields = {"Review " + k.lower(): v for k, v in fields.items()}
        if row and any(row.get(k) != v for k, v in fields.items()):
            row.update(fields)
            write_jobs(inst, n, rows)
            inst.commit([], f"{n['id']}: job route")


def job_started(inst, n, branch, runtime, task_id=None):
    with _m_instance.brain_lock(inst):
        rows = ledger_rows(n)
        if rows is not None and not open_job(rows, branch):
            try:
                _m_tasks.prepare_start(n, task_id, branch)
            except ValueError as error:
                _m_common.die(str(error))
            rows.append({"Job": branch, "Started": inst.now_dt().isoformat(), "Reviews": "0", "Runtime": runtime})
            if task_id:
                rows[-1]["Task"] = task_id
            write_jobs(inst, n, rows)
            _m_tasks.job_started(inst, n, rows[-1])
            inst.commit([], f"{n['id']}: job started")


def job_reviewed(inst, n, branch, *, full=True, head="", reason="", identity_required=False, exception=None):
    with _m_instance.brain_lock(inst):
        rows = ledger_rows(n)
        r = open_job(rows or [], branch)
        if rows is None:
            _m_common.die("cannot record review: unrecognized job ledger")
        if not r:
            return  # a review must never manufacture a job that no worktree command can close
        prior = _m_review.review_count(r.get("Reviews"))
        full_prior = _m_review.review_count(r.get("Full reviews") or r.get("Reviews"))
        r["Reviews"] = str(prior + 1)
        r["Full reviews"] = str(full_prior + int(full))
        r["Review head"] = head
        r["Review kind"] = "full" if full else "check-fix"
        r["Review reason"] = " ".join(reason.splitlines())
        if identity_required:
            r['Review identity required'] = 'true'
        if exception:
            history = json.loads(r.get('Review exceptions') or '[]')
            history.append({**exception, 'round': prior + 1, 'head': head})
            r['Review exceptions'] = json.dumps(history)
        write_jobs(inst, n, rows)
        inst.commit([], f"{n['id']}: job reviewed")


def publication_refusal(n, row):
    """An ambiguous train must not become success through closure or cleanup."""
    for path in (n['dir'] / 'job-trains').glob('*.json'):
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                raise ValueError()
        except (OSError, ValueError):
            return 'train journal unreadable; coordinator must reconcile before closure or cleanup'
        if any(entry.get('key') == job_key(row) for entry in data.get('accepted', [])):
            if (data.get('state') in ('publishing', 'publication_failed') or
                    (data.get('state') == 'verified' and data.get('publication_succeeded') is not True)):
                return 'train publication unresolved; reconcile retained journal before closure or cleanup'
    return ''


def published_train(n, row):
    """An explicit success receipt and integrated tip authorize train recovery."""
    refusal = publication_refusal(n, row)
    if refusal:
        _m_common.die(refusal)
    home = n.get("home_path")
    if not home:
        return None
    for path in (n["dir"] / "job-trains").glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            _m_common.die("train journal unreadable; coordinator must repair it before closing a job")
        if (data.get("version") != 1 or data.get("state") != "verified"
                or data.get('publication_succeeded') is not True):
            continue
        tip = data.get("tip")
        if not isinstance(tip, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", tip):
            continue
        if _m_common.git("merge-base", "--is-ancestor", tip, "refs/heads/main", cwd=home).returncode:
            continue
        for entry in data.get("accepted", []):
            if entry.get("key") == job_key(row) and entry.get('branch') == row['Job']:
                return data["batch"], entry
    return None


def prepare_job_usage(inst, n, branch, worktree=None):
    early = open_job(read_jobs(n) or [], branch)
    usage = None
    if early and not str(early.get("Usage", "")).startswith("host"):  # what the host reported wins
        with _m_instance.released(inst, n, extra=[_m_usage.usage_file("subscriptions.json"), handback_file(n, early)]):
            try:
                usage = _m_spend.usage_cells(_m_spend.job_usage(inst, n, early, worktree), _m_guard.never_terms(n))
            except (OSError, ValueError, RuntimeError) as e:
                usage = {"Usage": f"not counted: {type(e).__name__}"}  # a close never fails for its accounting
    # Check before returning to a caller that may now delete the worktree.
    current = open_job(read_jobs(n) or [], branch)
    if early and (not current or job_key(early) != job_key(current)):
        _m_common.die("job changed during accounting; retry from fresh state")
    return early, usage


def job_ended(inst, n, branch, outcome, commits="", cause="", worktree=None, prepared=None):
    # Destructive callers prepare accounting before removal. Revalidate job identity
    # and preserve host-reported usage after the slow scan has released state.
    with _m_instance.brain_lock(inst):
        early, usage = prepared if prepared is not None else prepare_job_usage(inst, n, branch, worktree)
        n = inst.node(n["id"])
        rows = ledger_rows(n)
        r = open_job(rows or [], branch)
        if not r:
            return
        if not early or job_key(early) != job_key(r):
            _m_common.die("job changed during accounting; retry from fresh state")
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
        if usage and r.get("Usage") == early.get("Usage") and not str(r.get("Usage", "")).startswith("host"):
            r.update(usage)
        write_jobs(inst, n, rows)
        if not n["external"] or n["local"].get("workbench") or n["home_path"]:
            _m_verify.ready_path(inst, n, branch).unlink(missing_ok=True)
        _m_tasks.job_closed(inst, n, r)
        cleanup_handback(inst, n, r, rows)
        inst.commit([], f"{n['id']}: job {outcome}")
        print_thread_checklist(n, r)
    _m_docker_leftovers.docker_leftover_nodes(inst, [n])


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
    missing = 'summary: ' + signal['summary'] if signal.get('summary') else 'summary missing (legacy)' if signal else 'summary not recorded'
    if signal.get('state') == 'question':
        missing += '; question: ' + signal['question'] if signal.get('question') else '; question missing (legacy)'
    return missing + f"; lessons: {signal.get('lessons') or 'not recorded'}; report: {signal.get('report') or 'not recorded'}"


def print_job_handback(n, branch):
    row = open_job(read_jobs(n) or [], branch)
    if row:
        print(_m_guard.redact(f"{branch}: {handback_detail(n, row)}", _m_guard.never_terms(n)), flush=True)


def closed_job(row):
    """Only explicit terminal ledger outcomes qualify; a host idle state is never closure."""
    return bool(row.get("Ended") and row.get("Outcome") in ("merged", "abandoned", "studied", "closed"))


def unsettled_jobs(rows):
    return [r for r in rows or [] if closed_job(r) and not r.get("Settled")]


def bound_worker_threads(n, branch):
    """Recorded references only, not proof that a host thread is safe to settle."""
    refs = []
    workers = n["local"].get("workers", {})
    if isinstance(workers, dict):
        for row in read_jobs(n) or []:
            key = job_key(row)
            if row["Job"] == branch and workers.get(key):
                refs.append((workers[key], "worker:" + key))
            retired = n["local"].get("retired_workers", {}).get(key, [])
            if row["Job"] == branch and isinstance(retired, list):
                refs.extend((thread, "retired-worker:" + key) for thread in retired if isinstance(thread, str) and thread)
    for row in _m_status.active_work(n, include_closed=True):
        name = row[0].replace("`", "").split()
        if name and name[0] == branch and len(row) > 1:
            thread = row[1].strip()
            if thread not in ("", "-", "—"):
                refs.append((thread, "Active work"))
    return list(dict.fromkeys(refs))


def thread_reference_ids(reference):
    """Match supported thread displays without changing their listing text."""
    value = reference.strip()
    links = set(re.findall(r"t3-thread://v1/([^\s)\]<>`]+)", value))
    if links:
        return links
    raw = re.fullmatch(r"(?:`([^`\s]+)`|([^\s`]+))(?:\s+\([^)]*\))*", value)
    return {raw.group(1) or raw.group(2)} if raw else {value}


def stale_thread_warnings(n, thread=None):
    """Keep stale references useful for host inspection, but warn before sending."""
    if not n["external"]:
        return []
    rows = read_jobs(n) or []
    branches = {r["Job"] for r in rows}
    for row in _m_status.active_work(n, include_closed=True):
        name = row[0].replace("`", "").split()
        if name:
            branches.add(name[0])
    warnings = []
    targets = thread_reference_ids(thread) if thread is not None else None
    for branch in sorted(branches):
        if _m_briefs.import_job(branch):
            continue  # Import studies run in the node, never in a job worktree.
        jobs = [r for r in rows if r["Job"] == branch]
        if thread is None and jobs and all(closed_job(r) and r.get("Settled") for r in jobs):
            continue
        refs = bound_worker_threads(n, branch)
        if targets is not None:
            refs = [(ref, source) for ref, source in refs if thread_reference_ids(ref) & targets]
        if not refs:
            continue
        w = n["local"].get("worktrees", {}).get(branch)
        if w and Path(w["path"]).is_dir():
            if len(jobs) == 1:
                continue
            current = [row for row in jobs if not row.get("Ended")]
            if len(current) == 1:
                current_source = "worker:" + job_key(current[0])
                sources = {source for _, source in refs if source.startswith("worker:")}
                current_ids = set().union(*(thread_reference_ids(ref) for ref, source in refs if source == current_source))
                all_ids = set().union(*(thread_reference_ids(ref) for ref, _ in refs))
                if (sum(job_key(row) == job_key(current[0]) for row in jobs) == 1 and
                        sources == {current_source} and len(current_ids) == 1 and all_ids == current_ids):
                    continue
            state = ("has historical or ambiguous job ownership; do not send until its original workspace "
                     "is confirmed through the host. The current branch worktree does not establish this target's workspace")
        else:
            state = ("is gone; do not send to the old thread" if w else
                     "is not registered on this machine; confirm its host workspace before sending. "
                     "If it was removed, do not send to the old thread")
        warnings.append(f"warning: job {branch}: bound worker thread workspace {state}. "
                        "Start a fresh thread in a fresh worktree for follow-on work after removal.")
    return warnings


def print_worktree_threads(n, branch, dry_run=False):
    lines = [f"{branch}: bound worker threads before worktree removal:"]
    refs = bound_worker_threads(n, branch)
    lines += [f"  {thread} ({source})" for thread, source in refs] or ["  none recorded; inspect the host for unrecorded job threads"]
    if refs:
        if dry_run:
            lines += ["  Required order before removal: message the thread before pruning: tell it the job is done and request any final hand-back.",
                      "  Then confirm it has stopped and reconcile pending requests; settle it through the host before removal."]
        else:
            lines += ["  Required order before this command: worker told the job is done and final hand-back requested; stopped and pending requests reconciled; then safely settled through the host.",
                      "  This command removes the worktree now. Do not message these threads after removal.",
                      "  If those steps were missed, inspect and safely settle through the host only; use a fresh thread in a fresh worktree for follow-on work."]
        lines.append("  Inspect ownership first; never settle coordinators or other open jobs. Already settled threads need no message.")
    if dry_run:
        lines.append("  dry-run: listing only; no messages, settlement or removal performed")
    print(_m_guard.redact("\n".join(lines), _m_guard.never_terms(n)), flush=True)


def print_thread_checklist(n, row):
    if not closed_job(row):
        return
    matches = [r for r in _m_status.active_work(n, include_closed=True) if r[0] == row["Job"]]
    branch_jobs = [r for r in read_jobs(n) or [] if r["Job"] == row["Job"]]
    ambiguous = (len(branch_jobs) != 1 or len(matches) != 1 or
                 not _m_status.active_work_closed(matches[0]))
    worker = ("ownership ambiguous or not recorded; confirm by job marker; no thread selected"
              if ambiguous else matches[0][1])
    text = (f"Threads to settle for {row['Job']} (brain-job: {job_key(row)}):\n"
            + "  Worker from Active work: " + worker + "\n"
            + "  While its workspace still exists, message the thread before pruning: tell it the job is done, then settle it safely.\n"
            + "  If its worktree is already gone, do not message it; inspect and safely settle through the host only, and use a fresh thread in a fresh worktree for follow-on work.\n"
            + "  Find delegated/sub-agent descendants by parentThreadId and reviewers by the brain-job marker.\n"
            + "  Settle only when the ledger says merged or closed (abandoned/studied count as closed), "
            "no thread is running or waiting, and no pending requests remain. "
            "Never settle coordinators or threads belonging to open jobs. "
            "Check every candidate through the host; unknown state means leave unsettled.\n"
            + f"  After every job thread is safely settled: brain jobs {n['id']} --branch {row['Job']} --job-key {job_key(row)} --settled --attestation-file <fresh-export.json>")
    print(_m_guard.redact(text, _m_guard.never_terms(n)))


def read_land_check(n, row):
    try:
        receipt = json.loads(row.get('Land check') or 'null')
        return receipt if isinstance(receipt, dict) and receipt.get('version') == 1 else None
    except (ValueError, TypeError):
        return None


def record_land_check(inst, n, key, receipt):
    with _m_instance.brain_lock(inst):
        rows = ledger_rows(n)
        matches = [r for r in rows or [] if job_key(r) == key and not r.get('Ended')]
        if len(matches) != 1:
            raise ValueError('land-check job changed or closed')
        row = matches[0]
        if (not isinstance(receipt, dict) or receipt.get('version') != 1 or receipt.get('node') != n['id']
                or receipt.get('job_key') != key or receipt.get('branch') != row['Job']
                or receipt.get('result') not in ('passed', 'failed') or type(receipt.get('returncode')) is not int
                or (receipt['result'] == 'passed') != (receipt['returncode'] == 0)
                or any(not isinstance(receipt.get(k), str) or not receipt[k] for k in
                       ('head', 'command', 'config_digest', 'checked_at'))):
            raise ValueError('invalid land-check receipt')
        checked = inst.parse_time(receipt['checked_at'])
        if not checked or not 0 <= (inst.now_dt() - checked).total_seconds() <= 300:
            raise ValueError('stale land-check receipt')
        row['Land check'] = json.dumps(receipt, sort_keys=True, separators=(',', ':'))
        _m_guard.no_terms_in(n, row['Land check'], 'land-check receipt')
        write_jobs(inst, n, rows)
        inst.commit([], f"{n['id']}: land-check evidence")


def manual_landing(n, row):
    home = n.get('home_path')
    if not home:
        return None
    def ref(name):
        return _m_common.git('rev-parse', '--verify', '--quiet', name + '^{commit}', cwd=home).stdout.strip()
    head, main = ref('refs/heads/' + row['Job']), ref('refs/heads/main')
    start = n['local'].get('worktrees', {}).get(row['Job'], {}).get('start')
    if (head and main and head != start
            and _m_common.git('merge-base', '--is-ancestor', head, main, cwd=home).returncode == 0):
        return head, main
    return None


def close_ledger_job(inst, n, branch, reason=None):
    with _m_instance.brain_lock(inst):
        rows = ledger_rows(n)
        row = open_job(rows or [], branch)
        if not row:
            _m_common.die('no open ledger job to close')
        refusal = publication_refusal(n, row)
        if refusal:
            _m_common.die(refusal)
        landed = manual_landing(n, row)
        if landed and not reason:
            row.update({'Outcome': 'merged', 'Head commit': landed[0], 'Main commit': landed[1],
                        'Lesson check': 'required', 'Cause': 'manual landing confirmed in local main'})
        else:
            if not one_line(reason):
                _m_common.die('--close needs a merged branch or a nonempty one-line --reason for parked/closed work')
            _m_guard.no_terms_in(n, reason, 'job closure reason')
            row.update(Outcome='closed', Cause=reason.strip())
        row['Ended'] = stamp(inst)
        start, end = inst.parse_time(row['Started']), inst.parse_time(row['Ended'])
        row['Hours'] = f'{_m_status.elapsed(start, end) / 3600:.1f}' if start and end else ''
        _m_tasks.job_closed(inst, n, row)
        write_jobs(inst, n, rows)
        inst.commit([], f"{n['id']}: ledger job closed")
        print_thread_checklist(n, row)


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
            "forecasts": job_forecasts(rows), "throughput": daily_throughput(inst, n, rows)}


def daily_throughput(inst, n, rows, days=14):
    """Read-only calendar-day rollup, refreshed with every jobs/dashboard snapshot."""
    import datetime
    now = inst.now_dt()
    buckets = {}
    for offset in range(days):
        date = now.date() - datetime.timedelta(days=offset)
        start = datetime.datetime.combine(date, datetime.time.min, tzinfo=now.tzinfo)
        seconds = (min(start + datetime.timedelta(days=1), now) - start).total_seconds()
        buckets[date.isoformat()] = dict(date=date.isoformat(), window_seconds=seconds, merged=0,
            measured_merges=0, processed_tokens=0, verify_runs=0, verify_reused=0, reruns=0,
            verify_measured_jobs=0, queue_seconds=0, queue_measured_jobs=0)
    for row in rows:
        end = inst.parse_time(row.get("Ended", ""))
        if not end or end > now or row.get("Outcome") != "merged":
            continue
        bucket = buckets.get(end.astimezone(now.tzinfo).date().isoformat())
        if bucket is None:
            continue
        bucket["merged"] += 1
        usage = _m_spend.row_usage(row)
        source = str((usage or {}).get("source") or "")
        if usage and (source == "host" or source.startswith(("host:", "transcripts:"))):
            bucket["processed_tokens"] += usage["processed"]
            bucket["measured_merges"] += 1
        runs, reused = row.get("Verify runs", ""), row.get("Verify reuses", "")
        if str(runs).isdigit() and str(reused).isdigit():
            bucket["verify_runs"] += int(runs)
            bucket["verify_reused"] += int(reused)
            bucket["reruns"] += max(0, int(runs) - 1)
            bucket["verify_measured_jobs"] += 1
        queued = forecast_number(row.get("Queue s"))
        if queued is not None:
            bucket["queue_seconds"] += queued
            bucket["queue_measured_jobs"] += 1
    for bucket in buckets.values():
        seconds, merged = bucket["window_seconds"], bucket["merged"]
        bucket["merges_per_hour"] = merged * 3600 / seconds if seconds > 0 else None
        bucket["minutes_per_merge"] = seconds / 60 / merged if merged else None
        measured = bucket["measured_merges"]
        bucket["tokens_per_merge"] = bucket["processed_tokens"] / measured if measured else None
        if not bucket["queue_measured_jobs"]:
            bucket["queue_seconds"] = None
    return list(buckets.values())


def job_kind(branch, rows=()):
    declared = next((r.get("Class") for r in reversed(rows) if r["Job"] == branch and r.get("Class")), None)
    if declared:
        return declared
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


def forecast_number(value):
    try:
        number = float(value)
        return number if not isinstance(value, bool) and math.isfinite(number) and number >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def forecast_record(value):
    try:
        data = json.loads(value or "null")
        return data if isinstance(data, dict) and data.get("version") == 1 else None
    except (TypeError, ValueError):
        return None


def saved_forecast(value):
    data = forecast_record(value)
    if not data or not isinstance(data.get("class"), str) or not isinstance(data.get("runtime"), str):
        return None
    if (type(data.get("samples")) is not int or data["samples"] < 0 or
            type(data.get("measured")) is not int or data["measured"] < 0 or
            type(data.get("enough_data")) is not bool or data.get("selection") not in ("nearest 5 sizes", "all class sizes") or
            not isinstance(data.get("conversion"), dict)):
        return None
    for key in ("wall_h", "active_h", "tokens", "weekly_percent"):
        if key in data and (not isinstance(data[key], dict) or
                            any(not _m_usage.usage_number(data[key].get(k)) or data[key][k] < 0
                                for k in ("median", "min", "max"))):
            return None
    if data["enough_data"] and "wall_h" not in data:
        return None
    if "tokens" in data and ("active_h" not in data or
                             not all(k in data["conversion"] for k in ("processed_per_percent", "fresh_per_percent", "source"))):
        return None
    for key in ("processed_per_percent", "fresh_per_percent"):
        value = data["conversion"].get(key)
        if value is not None and (not _m_usage.usage_number(value) or value <= 0):
            return None
    return data


def job_size(n, row, packet=None):
    """Committed net change from the job's Git base; NUL records never expose paths."""
    w = n.get("local", {}).get("worktrees", {}).get(row["Job"], {})
    path = w.get("path")
    if not path or not Path(path).is_dir():
        return None
    head = _m_common.git("rev-parse", "--verify", "HEAD", cwd=path)
    branch = _m_common.git("symbolic-ref", "--short", "HEAD", cwd=path)
    if head.returncode or branch.returncode or branch.stdout.strip() != row["Job"]:
        return None
    base_ref = None
    if isinstance(packet, dict) and packet.get("head") == head.stdout.strip():
        base_ref = packet.get("integration_base", packet.get("main"))
    if not base_ref:
        remote = _m_verify.integration_ref(path)
        if remote:
            base_ref = _m_verify.integration_parent(n, remote) if n.get("home_path") else remote
    base = _m_common.git("merge-base", base_ref or w.get("start") or "refs/heads/main", head.stdout.strip(), cwd=path)
    if base.returncode:
        return None
    diff = _m_common.git("diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--numstat", "-z",
                         base.stdout.strip(), head.stdout.strip(), "--", cwd=path)
    if diff.returncode:
        return None
    files = lines = binary = 0
    for record in diff.stdout.split("\0"):
        if not record:
            continue
        added, deleted, _ = record.split("\t", 2)
        files += 1
        if added == "-" or deleted == "-":
            binary += 1
        else:
            lines += int(added) + int(deleted)
    return {"files": files, "lines": lines, "binary_files": binary,
            "base": base.stdout.strip(), "head": head.stdout.strip()}


def planned_size(n, row):
    """Optional contract ## Size: N files, M lines. Never infer scope from prose."""
    if not row:
        return None
    path = contract_file(n, row)
    sections = _m_ui.md_sections(path.read_text()) if path.is_file() else {}
    body = next((v for k, v in sections.items() if k.lower() == "size"), "")
    files, lines = re.search(r"\b(\d+)\s+files?\b", body), re.search(r"\b(\d+)\s+lines?\b", body)
    if files and lines:
        return {"files": int(files[1]), "lines": int(lines[1])}
    return None


def measured_job_usage(row):
    usage = _m_spend.row_usage(row)
    source = str((usage or {}).get("source") or "")
    return usage if source == "host" or source.startswith(("host:", "transcripts:")) else None


def meter_conversion(inst, n, runtime, rows):
    """Approximation per runtime. Only explicit paired ledger observations calibrate it.

    Meter % is an operator/host-attributed job delta, not an account-wide before/after
    subtraction: overlapping jobs make such subtraction unsuitable for calibration.
    """
    import statistics
    defaults = {"claude": 68000000, "codex": 51000000}
    def setting(name, default):
        value = forecast_number(n["fm"].get(name, inst.cfg.get(name, default)))
        return value if value and value > 0 else default
    processed = setting("forecast_processed_per_percent_" + runtime, defaults.get(runtime))
    fresh = setting("forecast_fresh_per_percent_" + runtime, 1100000)
    fresh_source = "configured/default"
    pairs = []
    for row in rows:
        share, u = forecast_number(row.get("Meter %")), measured_job_usage(row)
        if (row.get("Ended") and row.get("Outcome") == "merged" and row.get("Runtime") == runtime and
                share and share <= 100 and u and u["processed"] > 0):
            try:
                pair = (u["processed"] / share, u["fresh"] / share)
                if all(forecast_number(v) is not None for v in pair) and pair[0] > 0:
                    pairs.append(pair)
            except OverflowError:
                pass
    if len(pairs) >= 3:
        processed = statistics.median(p[0] for p in pairs)
        fresh_samples = [p[1] for p in pairs if p[1] > 0]
        if len(fresh_samples) >= 3:
            fresh = statistics.median(fresh_samples)
            fresh_source = "ledger"
    return {"processed_per_percent": processed, "fresh_per_percent": fresh,
            "source": "ledger" if len(pairs) >= 3 else "configured/default", "fresh_source": fresh_source,
            "samples": len(pairs)}


def estimate_job(inst, n, rows, kind, runtime, size=None, model=None):
    """Same explicit class (legacy branch inference as fallback), nearest five sizes.

    Rank by the sum of log size ratios so files and lines have equal influence.
    Unknown sizes never pretend to be zero. Without a planned size use all class samples.
    """
    import statistics
    samples = []
    for row in rows:
        hours = forecast_number(row.get("Hours"))
        if (not row.get("Ended") or row.get("Outcome") != "merged" or hours is None or
                (row.get("Class") or job_kind(row["Job"], rows)) != kind or
                (model and (row.get("Model") != model or row.get("Runtime") != runtime))):
            continue
        distance = 0
        if size is not None:
            values = [forecast_number(row.get(k.title())) for k in ("files", "lines")]
            if any(v is None for v in values):
                continue
            distance = sum(abs(math.log1p(v) - math.log1p(size[k])) for k, v in zip(("files", "lines"), values))
        samples.append((distance, row, hours))
    def nearest(values):
        return sorted(values, key=lambda x: (x[0], x[1].get("Started", "")))[:5] if size is not None else values
    measured_samples = nearest([s for s in samples if s[1].get("Runtime") == runtime])
    samples = nearest(samples)
    def spread(values):
        return {"median": statistics.median(values), "min": min(values), "max": max(values)}
    out = {"version": 1, "class": kind, "runtime": runtime, "model": model, "size": size, "samples": len(samples),
           "enough_data": len(samples) >= 3, "selection": "nearest 5 sizes" if size else "all class sizes",
           "conversion": meter_conversion(inst, n, runtime, rows)}
    if out["enough_data"]:
        out["wall_h"] = spread([h for _, _, h in samples])
    measured = []
    for _, row, _ in measured_samples:
        u = measured_job_usage(row)
        active = forecast_number(row.get("Active h"))
        if u and u["processed"] > 0 and active is not None:
            measured.append((active, u["processed"]))
    out["measured"] = len(measured)
    if len(measured) >= 3:
        out["active_h"], out["tokens"] = spread([a for a, _ in measured]), spread([t for _, t in measured])
        factor = out["conversion"]["processed_per_percent"]
        if factor:
            out["weekly_percent"] = {k: v / factor for k, v in out["tokens"].items()}
    return out


def estimate_line(estimate):
    if not estimate:
        return "Forecast: unavailable; no saved launch estimate."
    out = f"Forecast {estimate['class']}: {estimate['samples']} similar merged jobs ({estimate['selection']}); "
    if estimate.get("model"):
        out += f"model {estimate['model']}; "
    if not estimate["enough_data"]:
        return out + "too little data (need 3 merged jobs). Advisory, never a limit or gate."
    def show(key, label, render):
        s = estimate[key]
        return f"{label} {render(s['median'])} (min–max {render(s['min'])}–{render(s['max'])})"
    out += show("wall_h", "wall", lambda x: f"{round(x, 2):g} h")
    if "tokens" in estimate:
        out += "; " + show("active_h", "active", lambda x: f"{round(x, 2):g} h")
        out += "; " + show("tokens", "processed tokens", _m_spend.tokens_text)
        if "weekly_percent" in estimate:
            out += "; " + show("weekly_percent", f"approx. {estimate['runtime']} weekly share", lambda x: f"{x:.3g}%")
        c = estimate["conversion"]
        if c["processed_per_percent"]:
            out += (f"; 1% ≈ {_m_spend.tokens_text(c['processed_per_percent'])} processed / "
                    f"{_m_spend.tokens_text(c['fresh_per_percent'])} fresh ({c['source']} processed, "
                    f"{c.get('fresh_source', 'configured/default')} fresh)")
    else:
        out += f"; active hours/tokens/weekly share: too little data ({estimate['measured']} measured for {estimate['runtime']}; need 3)"
    return out + ". Advisory, never a limit or gate."


def launch_forecast(inst, n, row, kind, runtime, model=None, rows=None):
    """Both launch paths retain one estimate until the selected route changes."""
    model = model if model and model != "runtime default" else None
    saved = saved_forecast((row or {}).get("Forecast"))
    if saved and (saved["class"], saved["runtime"], saved.get("model")) == (kind, runtime, model):
        return saved
    return estimate_job(inst, n, (read_jobs(n) or []) if rows is None else rows, kind, runtime, planned_size(n, row), model=model)


def record_job_forecast(inst, n, row, estimate):
    if not row:
        return
    with _m_instance.brain_lock(inst):
        rows = ledger_rows(n)
        current = open_job(rows or [], row["Job"])
        saved = saved_forecast((current or {}).get("Forecast"))
        if current and job_key(current) == job_key(row) and (not saved or
                (saved["class"], saved["runtime"], saved.get("model")) !=
                (estimate["class"], estimate["runtime"], estimate.get("model"))):
            current["Forecast"] = json.dumps(estimate, sort_keys=True)
            write_jobs(inst, n, rows)
            inst.commit([], f"{n['id']}: launch forecast")


def actual_cost(inst, n, row, usage):
    start = inst.parse_time(row.get("Started", ""))
    end = inst.parse_time(row.get("Ended", "")) or inst.now_dt()
    out = {"version": 1, "at": end.isoformat(),
           "wall_h": max(0, _m_status.elapsed(start, end) / 3600) if start else None,
           "active_h": None, "tokens": None, "weekly_percent": None}
    source = str((usage or {}).get("source") or "")
    if usage and (source == "host" or source.startswith(("host:", "transcripts:"))):
        out.update(active_h=usage.get("active_h", usage.get("active_minutes", 0) / 60),
                   tokens=usage["processed"], usage_source=usage["source"], usage_at=end.isoformat())
        saved = saved_forecast(row.get("Forecast")) or {}
        conversion = saved.get("conversion") or meter_conversion(inst, n, row.get("Runtime", ""), [])
        factor = conversion.get("processed_per_percent")
        if factor:
            out["weekly_percent"] = usage["processed"] / factor
    return out


def actual_line(actual):
    actual = clean_actual(actual) or {}
    out = "Actual at hand-back/close: "
    out += "; ".join(label + " " + ((_m_spend.tokens_text(v) if key == "tokens" else f"{round(v, 2):g}") + unit
                                      if (v := forecast_number(actual.get(key))) is not None else "unknown")
                     for key, label, unit in (("wall_h", "wall", " h"), ("active_h", "active", " h"),
                                               ("tokens", "processed tokens", ""),
                                               ("weekly_percent", "approx. weekly share", "%")))
    size = actual.get("size")
    if isinstance(size, dict):
        out += f"; Git size {size.get('files', '?')} files, {size.get('lines', '?')} added/deleted lines"
    return out


def clean_actual(data):
    """Persist/display only known numeric facts and Git identities from worker evidence."""
    if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
        return None
    out = {"version": 1}
    for key in ("wall_h", "active_h", "tokens", "weekly_percent"):
        out[key] = forecast_number(data.get(key))
    tokens = data.get("tokens")
    out["tokens"] = tokens if type(tokens) is int and tokens >= 0 else None
    for key in ("at", "usage_at"):
        if isinstance(data.get(key), str) and _m_usage.usage_stamp(data[key]):
            out[key] = data[key]
    source = str(data.get("usage_source") or "")
    if source == "host" or source.startswith("host:"):
        out["usage_source"] = "host"
    elif source.startswith("transcripts:"):
        out["usage_source"] = "transcripts: measured"
    size = data.get("size")
    out["size"] = None
    if isinstance(size, dict):
        size_out = {key: size[key] for key in ("files", "lines", "binary_files")
                    if type(size.get(key)) is int and size[key] >= 0}
        for key in ("base", "head"):
            if isinstance(size.get(key), str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", size[key]):
                size_out[key] = size[key]
        if all(key in size_out for key in ("files", "lines")):
            out["size"] = size_out
    return out


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
            u = measured_job_usage(row)
            sample = (hours, u if u and u["processed"] else None)
            groups.setdefault("unclassified", []).append(sample)  # the explicit all-jobs fallback
            kind = row.get("Class") or job_kind(row["Job"], rows)
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


def wave_allowance(inst, n, rows, planned, subscription, now=None, max_age_seconds=120):
    """Compare a complete wave forecast with fresh account-weekly headroom; never an admission gate."""
    now = time.time() if now is None else now
    window = subscription.get("windows", {}).get("seven_day", {})
    read_at = window.get("read_at", subscription.get("read_at"))
    used = forecast_number(window.get("used_percent"))
    reset = forecast_number(window.get("resets_at"))
    if (not _m_usage.usage_number(read_at) or not 0 <= now - read_at <= max_age_seconds or
            not read_at or window.get("source", subscription.get("source")) == "unknown" or
            window.get("absent") or window.get("expired") or reset is None or reset <= now or
            used is None or used > 100):
        return None
    stop = forecast_number(subscription.get("stop_percent", 85))
    if stop is None or not 0 < stop <= 100:
        return None
    values, missing = [], 0
    for row in planned:
        kind = row.get("Class") or job_kind(row["Job"], rows)
        runtime = row.get("Runtime") or subscription.get("runtime")
        estimate = launch_forecast(inst, n, row, kind, runtime, row.get("Model"), rows=rows)
        share = estimate.get("weekly_percent")
        if not isinstance(share, dict):
            missing += 1
        else:
            values.append(share)
    total = sum(value["median"] for value in values) if values and not missing else None
    upper = sum(value["max"] for value in values) if values and not missing else None
    headroom = max(0, stop - used)
    return {"jobs": len(planned), "missing_jobs": missing, "forecast_percent": total,
            "upper_percent": upper, "headroom_percent": headroom,
            "fits": total <= headroom if total is not None else None,
            "range_fits": upper <= headroom if upper is not None else None,
            "basis": "whole-job approximate account-weekly share; elapsed work not subtracted"}


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
                   "elapsed time not subtracted.")
        wave = wave_allowance(inst, n, rows, these, selected, max_age_seconds=policy["max_age_seconds"])
        if not wave or wave["forecast_percent"] is None:
            out.append(f"current route {selected['label']}: weekly allowance fit unknown (fresh weekly reading and three measured samples per job required).")
        else:
            out.append(f"current route {selected['label']}: {wave['jobs']} open/planned whole-job forecast "
                       f"{wave['forecast_percent']:.3g}% weekly share (observed-max sum {wave['upper_percent']:.3g}%), "
                       f"{wave['headroom_percent']:.3g}% headroom to the recorded stop line; median "
                       f"{'fits' if wave['fits'] else 'exceeds'} headroom"
                       + ("; observed range may exceed it" if not wave["range_fits"] else "")
                       + ". Approximate, account-wide; elapsed work not subtracted; five-hour/model-specific fit unknown; advisory only.")
    return out


def cmd_jobs(a):
    if getattr(a, 'thread', None) is not None:
        if (not (a.id and a.branch and a.start) or any((a.close, a.settled, a.all_closed,
                getattr(a, 'live', False), a.lessons is not None, a.switch is not None,
                a.contract_file, a.usage_file, a.job_key, getattr(a, 'reason', None),
                getattr(a, 'attestation_file', None)))):
            _m_common.die('adoption refused: --thread requires --start, a node and --branch, without other updates')
        from . import job_adoption as _m_adoption
        return _m_adoption.cmd_adopt_job(a)
    inst = _m_nodes.load()
    if getattr(a, 'attestation_file', None) and not a.settled:
        _m_common.die('--attestation-file requires --settled')
    if getattr(a, 'reason', None) and not a.close:
        _m_common.die('--reason requires --close')
    if a.close and a.id and a.branch and not _m_briefs.import_job(a.branch):
        if any((a.start, a.settled, a.all_closed, getattr(a,'live',False), a.lessons is not None, a.switch is not None, a.contract_file, a.usage_file, a.job_key)):
            _m_common.die('--close cannot combine with other job updates')
        close_ledger_job(inst, inst.node(a.id), a.branch, getattr(a, 'reason', None))
        return
    if getattr(a, 'live', False):
        if a.branch and not a.id or a.job_key and not (a.id and a.branch):
            _m_common.die('--live branch/job-key selection needs a node and --branch')
        if any((a.start, a.close, a.settled, a.all_closed, a.lessons is not None, a.switch is not None, a.contract_file, a.usage_file)):
            _m_common.die('--live is a read-only projection')
        from . import job_attempts as _m_attempts
        for n in inst.nodes():
            if a.id and n['id'] != a.id:
                continue
            for row in read_jobs(n) or []:
                if a.branch and row['Job'] != a.branch or a.job_key and job_key(row) != a.job_key:
                    continue
                projection = _m_attempts.job_projection(inst, n, row)
                print(_m_guard.redact(json.dumps({'node': n['id'], **projection}, ensure_ascii=False), _m_guard.never_terms(n)))
        return
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
        evidence_file = getattr(a, 'attestation_file', None)
        from . import settlement as _m_settlement
        def attest(targets):
            if not evidence_file:
                _m_common.die('--settled requires --attestation-file with fresh exhaustive per-thread evidence')
            try:
                evidence = json.loads(Path(evidence_file).read_text())
            except (OSError, ValueError):
                _m_common.die('unreadable settlement attestation; no ledger rows changed')
            try:
                identities = _m_settlement.validate_settlement_attestation(inst, n, targets, evidence)
            except ValueError as error:
                _m_common.die(str(error))
            # Preserve the supplied observations separately from the coordinator ledger.
            folder = n['dir'] / 'job-settlements'
            serialized = json.dumps(evidence, sort_keys=True) + '\n'
            _m_guard.no_terms_in(n, serialized, 'settlement attestation')
            _m_common.safe_target(folder / 'evidence.json')
            folder.mkdir(exist_ok=True)
            for target in targets:
                path = folder / (job_key(target) + '.json')
                _m_common.atomic_write(path, serialized)
                _m_common.touch(path)
            for identity in identities:
                print('t3_thread_organize ' + json.dumps({'action': 'settle', 'threadId': identity}, sort_keys=True))
        with _m_instance.brain_lock(inst):
            rows = ledger_rows(n)
            if a.all_closed:
                if rows is None:
                    _m_common.die("--all-closed requires a recognized job ledger")
                targets = unsettled_jobs(rows)
                if any(open_job(rows, r["Job"]) for r in targets):
                    _m_common.die("--all-closed refused: a branch to stamp has an open job; no rows changed")
                attest(targets)
                if targets:
                    settled_at = stamp(inst)
                    for row in targets:
                        row["Settled"] = settled_at
                    write_jobs(inst, n, rows)
                    inst.commit([], f"{n['id']}: closed job thread sweep recorded")
                print(f"{n['id']}: {len(targets)} closed job(s) recorded settled (coordinator-supplied evidence; no independent live host verification)")
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
            attest([row])
            if not row.get("Settled"):
                row["Settled"] = stamp(inst)
                write_jobs(inst, n, rows)
                inst.commit([], f"{n['id']}: job threads settled")
        print(_m_guard.redact(f"{n['id']}: threads for {a.branch} recorded settled (coordinator-supplied evidence; no independent live host verification)", _m_guard.never_terms(n)))
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
        _m_tasks.job_closed(inst, n, r)
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
            round_usage = _m_spend.job_round_usage(inst, n, r, u)
            for line in job_advice(inst, n, r) + [_m_spend.usage_line(u, _m_spend.job_budget(n, r), r["age_h"], round_usage=round_usage)] + \
                    _m_spend.usage_advice(u, j["median_tokens"], round_usage=round_usage):
                print(_m_guard.redact("        " + line, _m_guard.never_terms(n)))
            if r.get("Forecast"):
                print(_m_guard.redact("        " + estimate_line(saved_forecast(r["Forecast"])), _m_guard.never_terms(n)))
                hb = read_handback(n, r) or {}
                if isinstance(hb.get("actual"), dict):
                    print(_m_guard.redact("        " + actual_line(hb["actual"]), _m_guard.never_terms(n)))
        for c in j["causes"]:
            print(f"  cause {c}")
        for line in forecast_lines(read_jobs(n) or [], a.branch) + job_budget_lines(inst, n, a.branch):
            print(_m_guard.redact("  " + line, _m_guard.never_terms(n)))
        for r in read_jobs(n) or []:
            if a.branch and r["Job"] == a.branch and (not a.job_key or job_key(r) == a.job_key):
                if r.get("Ended"):
                    hb = read_handback(n, r) or {}
                    line = verify_duration_line(inst, n, hb.get("verify_duration"))
                    if line:
                        print(_m_guard.redact(line, _m_guard.never_terms(n)))
                if r.get("Ended") and r.get("Forecast"):
                    print(_m_guard.redact(estimate_line(saved_forecast(r["Forecast"])), _m_guard.never_terms(n)))
                    actual = forecast_record(r.get("Actual"))
                    if actual:
                        print(_m_guard.redact(actual_line(actual), _m_guard.never_terms(n)))
                print(_m_guard.redact(json.dumps(r, ensure_ascii=False), _m_guard.never_terms(n)))
                if closed_job(r) and not r.get("Settled"):
                    print_thread_checklist(n, r)


REVIEW_CAP = 2  # one full review and one blocker-only fix check; exceptions require a reason


def record_verify_duration(inst, n, row, packet, elapsed_seconds):
    """Measure readiness wall time, including preflight/admission; reused proof costs no new execution."""
    seconds = forecast_number(elapsed_seconds)
    if seconds is None:
        return None
    reused = isinstance(packet, dict) and packet.get("verify_reused") is True
    threshold = forecast_number(n["fm"].get("verify_warn_seconds", inst.cfg.get("verify_warn_seconds", 600)))
    threshold = threshold if threshold is not None and threshold > 0 else 600
    seconds = 0.0 if reused else seconds
    return {"elapsed_seconds": seconds, "reused": reused, "slow": seconds > threshold,
            "warning_seconds": threshold, "at": inst.now_dt().isoformat(),
            "basis": "readiness wall time including admission and preflight"}


def verify_duration_line(inst, n, duration):
    if not isinstance(duration, dict):
        return None
    seconds = forecast_number(duration.get("elapsed_seconds"))
    if seconds is None or type(duration.get("reused")) is not bool:
        return None
    threshold = forecast_number(n["fm"].get("verify_warn_seconds", inst.cfg.get("verify_warn_seconds", 600)))
    threshold = threshold if threshold is not None and threshold > 0 else 600
    return (f"verify duration: {seconds:.3f}s (readiness wall time incl. admission/preflight)"
            + ("; reused proof, no new execution" if duration["reused"] else "")
            + (f"; slow verify: exceeds {threshold:g}s" if seconds > threshold else ""))


def job_advice(inst, n, row):
    """What the records say about an open job's process: advice from observed events, never a gate."""
    out, hb = [], read_handback(n, row) or {}
    duration_line = verify_duration_line(inst, n, hb.get("verify_duration"))
    if duration_line:
        out.append(duration_line)
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
