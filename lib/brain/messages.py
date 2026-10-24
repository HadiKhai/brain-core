"""Worker message formatting; host delivery remains the caller's action."""
from pathlib import Path

from . import common as _m_common
from . import guard as _m_guard
from . import jobs as _m_jobs
from . import nodes as _m_nodes

MESSAGE_KINDS = ("start", "done", "blocked", "question")
MESSAGE_PROTOCOL = (
    "Send the coordinator only start, done, blocked or question; no progress notes, receipts, acknowledgements or hash relays. "
    "Use `brain msg <node> <kind> \"<one line>\"` to format a host message, adding branch and report when known, or use the existing hand-back path. "
    "Questions and failure reports always go through. "
    "Workers sharing a commit coordinate directly with the peer thread named in their brief; ask the coordinator for its id once if absent."
)


def format_worker_message(kind, text, branch=None, report=None):
    context = []
    if branch:
        context.append("branch: " + " ".join(branch.split()))
    if report:
        context.append("report: " + " ".join(str(report).split()))
    return f"{kind}: {' '.join(text.split())}" + (" [" + "; ".join(context) + "]" if context else "")


def cmd_msg(a):
    inst = _m_nodes.load()
    n = inst.node(a.id)
    branch = a.branch or _m_common.git("branch", "--show-current", cwd=Path.cwd()).stdout.strip()
    row = _m_jobs.open_job(_m_jobs.read_jobs(n) or [], branch) if branch else None
    report = a.report
    if not report and row:
        report = (_m_jobs.read_handback(n, row) or {}).get("report") or _m_common.tilde(_m_jobs.handback_file(n, row))
    print(_m_guard.redact(format_worker_message(a.kind, a.message, branch, report), _m_guard.never_terms(n)))
    print("NOT SENT: use the host send")
