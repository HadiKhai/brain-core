"""Worker message formatting; host delivery remains the caller's action."""
from pathlib import Path
import re

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


def worker_message_guard(inst, sender_thread, tool, args):
    """Guard a known worker-to-coordinator send, never guess an unknown caller."""
    if (not isinstance(sender_thread, str) or not sender_thread
            or not isinstance(tool, str) or tool.split('__')[-1] != 't3_thread_send'
            or not isinstance(args, dict) or not isinstance(args.get('threadId'), str)):
        return None
    coordinators, owners = set(), []
    for node in inst.nodes():
        reference = node['local'].get('coordinator')
        if isinstance(reference, str) and reference:
            coordinators.update(_m_jobs.thread_reference_ids(reference))
        bindings = node['local'].get('workers', {})
        rows = _m_jobs.read_jobs(node)
        if rows is None or not isinstance(bindings, dict):
            continue
        keys = {_m_jobs.job_key(row) for row in rows}
        for key, bound in bindings.items():
            if key in keys and isinstance(bound, str) and sender_thread in _m_jobs.thread_reference_ids(bound):
                owners.append((node['id'], key))
    if sender_thread in coordinators or len(owners) != 1:
        return None
    target = _m_jobs.thread_reference_ids(args['threadId'])
    if len(target) != 1 or not target.intersection(coordinators):
        return None  # peer coordination and unknown targets are not parent chatter
    message = args.get('message')
    if isinstance(message, str) and re.match(r'^\s*(start|done|blocked|question)\s*:\s*\S', message, re.I):
        return None
    return ('Worker messages to a coordinator require start:, done:, blocked: or question:. '
            'Put progress in the job report or node inbox without a wake.')


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
    if n['id'] == 'root' and a.kind != 'question':
        print('INBOX ONLY: record landing/park/FYI with brain notify; do not host-send this update')
    else:
        print("NOT SENT: use the host send")
