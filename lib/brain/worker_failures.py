"""Sampler-owned same-thread recovery; file-only watch reads only its receipts."""
import fcntl
import json
import math
import os
import re

from . import common as _m_common
from . import host_instances as _m_host_instances
from . import host_rpc as _m_host_rpc
from . import instance as _m_instance
from . import jobs as _m_jobs
from . import machine as _m_machine


FAILURE_BACKOFF = (60, 180, 600)
FAILURE_CATEGORIES = ('at capacity', 'overloaded', 'rate limit', 'stream disconnected', 'work error')


def failure_category(text):
    """Only native error items are passed here, never arbitrary conversation prose."""
    text = text.lower() if isinstance(text, str) else ''
    for category, pattern in (
            ('at capacity', r'\bat capacity\b'),
            ('overloaded', r'\boverload(?:ed)?\b'),
            ('rate limit', r'\brate[ -]limit(?:ed|ing)?\b|\btoo many requests\b|\b429\b'),
            ('stream disconnected', r'\bstream (?:disconnected|disconnect|closed)|\bconnection (?:reset|closed)')):
        if re.search(pattern, text):
            return category
    return 'work error'


def failure_receipt_path(inst, record):
    return inst.root / '.brain/wake' / ('failure-' + _m_jobs.digest([record['thread'], record['run']]) + '.json')


def failure_receipt(inst, record):
    path = failure_receipt_path(inst, record)
    _m_common.safe_target(path)
    if not path.exists():
        return None
    if path.stat().st_size > 16384:
        raise ValueError('failure receipt exceeds bound')
    value = json.loads(path.read_text())
    if (not isinstance(value, dict) or value.get('version') != 1
            or any(value.get(k) != record[k] for k in ('node', 'job_key', 'thread', 'run'))
            or not isinstance(value.get('attempts'), list) or len(value['attempts']) > 3
            or type(value.get('since')) not in (int, float) or not math.isfinite(value['since'])
            or any(type(at) not in (int, float) or not math.isfinite(at) for at in value['attempts'])
            or value['attempts'] != sorted(value['attempts'])):
        raise ValueError('invalid failure receipt; retained for repair')
    return value


def failure_exhausted(inst, record):
    receipt = failure_receipt(inst, record)
    return receipt is not None and len(receipt['attempts']) == 3


def failure_owned(inst, record):
    """Revalidate open-job ownership, including a branch-attributed export gap."""
    n = inst.node(record['node'])
    row = next((r for r in _m_jobs.read_jobs(n) or []
                if not r.get('Ended') and _m_jobs.job_key(r) == record['job_key']), None)
    if n['archived'] or row is None or _m_jobs.job_signal(inst, n, row, strict=True):
        return None
    tid = record['thread']
    for node in inst.nodes():
        if tid in _m_jobs.thread_reference_ids(node['local'].get('coordinator', '')):
            return None
        for key, reference in node['local'].get('workers', {}).items():
            if (tid in _m_jobs.thread_reference_ids(reference)
                    and (node['id'], key) != (record['node'], record['job_key'])):
                return None
        if any(tid in _m_jobs.thread_reference_ids(ref) for refs in node['local'].get('retired_workers', {}).values()
               for ref in refs):
            return None
    bound = n['local'].get('workers', {}).get(record['job_key'])
    if record.get('discovered'):
        project = n['local'].get('home_project') or n['local'].get('host_project')
        if (record.get('ambiguous') or record.get('project') != project or record.get('branch') != row['Job']
                or bound and _m_jobs.thread_reference_ids(bound) != {tid}):
            return None
    elif not isinstance(bound, str) or _m_jobs.thread_reference_ids(bound) != {tid}:
        return None
    return n, row


def resume_failure(inst, record, rpc=None):
    """Claim before send: unknown outcomes consume a step and never replay it."""
    if record.get('category') not in FAILURE_CATEGORIES[:-1]:
        return
    if _m_common.outward_links(inst.root):
        raise ValueError('unsafe failure recovery state')
    path = failure_receipt_path(inst, record)
    path.parent.mkdir(parents=True, exist_ok=True)
    _m_common.safe_target(path.with_suffix('.lock'))
    fd = os.open(path.with_suffix('.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a+') as lock:
        if os.fstat(lock.fileno()).st_nlink != 1:
            raise ValueError('linked failure recovery lock')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        fresh = _m_instance.Instance(inst.root)
        owned = failure_owned(fresh, record)
        if owned is None:
            return
        receipt = failure_receipt(fresh, record)
        now = inst.now_dt().timestamp()
        if receipt is None:
            # Detection starts the backoff clock, rather than inferring provider timing.
            receipt = {**{k: record[k] for k in ('node', 'job_key', 'thread', 'run')},
                       'version': 1, 'since': now, 'attempts': []}
            _m_machine.machine_publish(inst, path, json.dumps(receipt, sort_keys=True) + '\n')
        step = len(receipt['attempts'])
        if step == 3 or now - (receipt['attempts'][-1] if step else receipt['since']) < FAILURE_BACKOFF[step]:
            return
        rpc = _m_host_instances.routed_host_rpc(fresh, record['node'], rpc or _m_host_rpc.call)
        detail = rpc('t3_thread_read', {'threadId': record['thread'], 'limit': 1, 'runLimit': 1})
        thread = detail.get('thread') if isinstance(detail, dict) else None
        if (not isinstance(thread, dict) or thread.get('threadId') != record['thread']
                or thread.get('latestRunId') != record['run'] or thread.get('status') != 'failed'
                or thread.get('activeRunId', 'unknown') is not None or thread.get('pendingRequestCount') != 0
                or thread.get('projectId') != record['project']):
            return
        if record.get('discovered') and thread.get('branch') != record['branch']:
            return
        pending = rpc('t3_pending_request_list', {'threadId': record['thread']})
        if not isinstance(pending, dict) or pending.get('requestIds') != []:
            return
        # Serialize the final ownership check and durable claim with local writers.
        with _m_instance.brain_lock(inst):
            if failure_owned(_m_instance.Instance(inst.root), record) is None:
                return
            receipt['attempts'].append(inst.now_dt().timestamp())
            _m_machine.machine_publish(inst, path, json.dumps(receipt, sort_keys=True) + '\n')
        response = rpc('t3_thread_send', {
            'threadId': record['thread'], 'mode': 'auto',
            'clientRequestId': 'failure-' + _m_jobs.digest([record['thread'], record['run'], step + 1]),
            'message': 'Resume the existing assigned job after the provider failure. Continue in this same thread, '
                       'with the existing provider, model, worktree and instructions. Do not switch runtimes.'})
        if not isinstance(response, dict) or response.get('threadId') != record['thread']:
            raise ValueError('failure resume outcome unknown; attempt retained without replay')


def resume_failures_if_enabled(inst, rpc=None):
    if inst.local_of('root').get('host_export') != 'on':
        return
    from . import host_observations as _m_host_observations
    for record in _m_host_observations.host_export_runs(inst):
        try:
            resume_failure(inst, record, rpc=rpc)
        except (OSError, ValueError, _m_host_rpc.HostRPCError):
            print('worker recovery unavailable; attempt evidence retained, no replay')
