"""Worker message formatting; host delivery remains the caller's action."""
from pathlib import Path
import re
import datetime

from . import common as _m_common
from .message_envelope import AGENT_MESSAGE_PREFIX, marked_message, unmarked_message

_m_guard = _m_common.LazyModule(__package__, 'guard')
_m_jobs = _m_common.LazyModule(__package__, 'jobs')
_m_nodes = _m_common.LazyModule(__package__, 'nodes')

def agent_message(text, inst=None):
    """Frame the exact body so a coalesced operator remainder cannot be hidden."""
    if inst is None:
        root = _m_nodes.try_find_root()
        if root:
            from .instance import Instance
            inst = Instance(root)
    now = inst.now_dt() if inst else datetime.datetime.now().astimezone()
    return marked_message(text, AGENT_MESSAGE_PREFIX, sent_at=now.isoformat(timespec='seconds'))


def send_delivery_mode(mode=None):
    """Only an explicit interruption choice may preempt a running recipient."""
    return mode if mode in ('steer', 'restart') else 'queue'


def stamped_send_input(inst, tool, args):
    """Stamp a declared send and queue routine traffic without reading its meaning."""
    if (not isinstance(tool, str) or tool.split('__')[-1].split('.')[-1] != 't3_thread_send'
            or not isinstance(args, dict) or not isinstance(args.get('message'), str)):
        return None
    return {**args, 'message': agent_message(args['message'], inst),
            'mode': send_delivery_mode(args.get('mode'))}


def cmd_agent_message(a):
    if a.message_file and a.message is not None:
        raise SystemExit("provide message or --message-file, not both")
    text = a.message
    if a.message_file:
        with a.message_file.open(encoding="utf-8", newline="") as stream:
            text = stream.read()
    if not isinstance(text, str) or not text:
        raise SystemExit("provide a nonempty message or --message-file")
    print(agent_message(text))


MESSAGE_KINDS = _m_common.MESSAGE_KINDS
MESSAGE_PROTOCOL = (
    "Send the coordinator only start, done, blocked or question; no progress notes, receipts, acknowledgements or hash relays. "
    "Use `brain msg <node> <kind> \"<one line>\"` to format a host message, adding branch and report when known, or use the existing hand-back path. "
    "Questions and failure reports always go through. "
    "Use host send mode queue for routine reports, hand-backs and questions; "
    "choose mode steer only to intentionally interrupt the recipient's running turn. "
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
    if isinstance(message, str):
        body = unmarked_message(message, AGENT_MESSAGE_PREFIX)
        if body is not None:
            message = body
        elif message.startswith(AGENT_MESSAGE_PREFIX + "\n\n"):
            message = message[len(AGENT_MESSAGE_PREFIX) + 2:]
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
    print(agent_message(_m_guard.redact(format_worker_message(a.kind, a.message, branch, report), _m_guard.never_terms(n)), inst))
    if n['id'] == 'root' and a.kind != 'question':
        print('INBOX ONLY: record landing/park/FYI with brain notify; do not host-send this update')
    else:
        print("NOT SENT: use the host send with mode queue")
