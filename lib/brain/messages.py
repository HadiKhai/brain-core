"""Advisory worker message protocol; host delivery remains the caller's action."""
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

from . import common as _m_common
from . import guard as _m_guard
from . import hosts as _m_hosts
from . import jobs as _m_jobs
from . import nodes as _m_nodes

MESSAGE_KINDS = ("start", "done", "blocked", "question")
MESSAGE_PROTOCOL = (
    "Send the coordinator only start, done, blocked or question; no progress notes, receipts, acknowledgements or hash relays. "
    "Use `brain msg <node> <kind> \"<one line>\"` to format a host message with branch and report, or the existing hand-back path. "
    "Questions and failure reports always go through; warnings never block a turn. "
    "Workers sharing a commit coordinate directly with the peer thread named in their brief; ask the coordinator for its id once if absent."
)


def format_worker_message(kind, text, branch, report):
    return f"{kind}: {' '.join(text.split())} [branch: {' '.join(branch.split())}; report: {' '.join(str(report).split())}]"


def cmd_msg(a):
    inst = _m_nodes.load()
    n = inst.node(a.id)
    branch = a.branch or _m_common.git("branch", "--show-current", cwd=Path.cwd()).stdout.strip()
    row = _m_jobs.open_job(_m_jobs.read_jobs(n) or [], branch)
    if not row:
        _m_common.die("msg needs an open job; use --branch to name it")
    report = a.report or (_m_jobs.read_handback(n, row) or {}).get("report") or _m_common.tilde(_m_jobs.handback_file(n, row))
    print(_m_guard.redact(format_worker_message(a.kind, a.message, branch, report), _m_guard.never_terms(n)))


def outgoing_calls(records):
    """Read structured Claude/Codex host calls, including literal code-mode sends.

    Code is never executed. Dynamic expressions are outside this advisory parser.
    """
    for rec in records:
        if not isinstance(rec, dict):
            continue
        msg = rec.get("message", {})
        blocks = msg.get("content", []) if isinstance(msg, dict) else []
        calls = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"] if isinstance(blocks, list) else []
        item = rec.get("payload", {}) if rec.get("type") == "response_item" else rec
        if isinstance(item, dict) and item.get("type") in ("function_call", "custom_tool_call"):
            calls.append(item)
        for call in calls:
            name = call.get("name", "")
            args = call.get("input", call.get("arguments", {}))
            if isinstance(args, str):
                if name.split(".")[-1] == "exec":
                    for match in re.finditer(r'(?:tools\.)?\w*t3_thread_send\s*\(\s*(\{.*?\})\s*\)', args, re.DOTALL):
                        fields = {}
                        for field in ("threadId", "message"):
                            value = re.search(r'(?:\b' + field + r'|"' + field + r'")\s*:\s*("(?:[^"\\]|\\.)*")', match[1])
                            if value:
                                fields[field] = json.loads(value[1])
                        if len(fields) == 2:
                            yield fields
                    continue
                try:
                    args = json.loads(args)
                except ValueError:
                    continue
            if name.endswith("t3_thread_send") and isinstance(args, dict):
                yield args


def warn_worker_messages(inst, n, data, cwd):
    """Warn once per observed bad send. Persist only digests in machine-private state."""
    coordinator = n["local"].get("coordinator")
    location = Path(cwd).resolve()
    worker = any(Path(w["path"]).resolve() == location or Path(w["path"]).resolve() in location.parents
                 for w in n["local"].get("worktrees", {}).values() if w.get("path"))
    if not worker or not coordinator or not data.get("transcript_path"):
        return
    fd = os.open(data["transcript_path"], os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        return
    with os.fdopen(fd, "rb") as fh:
        records = _m_hosts.hook_turn_records(fh)
    for call in outgoing_calls(records):
        text = call.get("message")
        if call.get("threadId") != coordinator or not isinstance(text, str):
            continue
        if re.match(r"^(?:start|done|blocked|question)\s*:", text.strip()):
            continue
        key = hashlib.sha256(json.dumps([data.get("session_id"), coordinator, text]).encode()).hexdigest()
        directory = Path.home() / ".local/state/brain/message-warnings"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(directory / key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        print("Worker message warning: send the coordinator only start, done, blocked or question. "
              "Questions and failure reports are always allowed; this warning never blocks a turn.", file=sys.stderr)
