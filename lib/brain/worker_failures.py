"""Sampler-owned same-thread recovery; file-only watch reads only its receipts."""
import contextlib
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
    return inst.root / '.brain/wake' / ('failure-' + _m_jobs.digest(record['thread']) + '-' +
                                         _m_jobs.digest(record['run']) + '.json')


def failure_lock_path(inst, record):
    return inst.root / '.brain/wake' / ('failure-' + _m_jobs.digest(record['thread']) + '.lock')


def failure_read(path):
    _m_common.safe_target(path)
    if path.stat().st_size > 16384:
        raise ValueError('failure receipt exceeds bound')
    value = json.loads(path.read_text())
    if (not isinstance(value, dict) or value.get('version') != 2
            or any(not isinstance(value.get(k), str) or not value[k]
                   for k in ('node', 'job_key', 'thread', 'run', 'project'))
            or value.get('category') not in FAILURE_CATEGORIES[:-1]
            or not isinstance(value.get('attempts'), list) or len(value['attempts']) > 3
            or type(value.get('since')) not in (int, float) or not math.isfinite(value['since'])
            or type(value.get('pending')) is not bool):
        raise ValueError('invalid failure receipt; retained for repair')
    known, claimed, at = {value['run']}, set(), value['since']
    for step, attempt in enumerate(value['attempts']):
        if (not isinstance(attempt, dict) or not isinstance(attempt.get('run'), str)
                or attempt['run'] not in known
                or attempt['run'] in claimed or type(attempt.get('at')) not in (int, float)
                or not math.isfinite(attempt['at']) or attempt['at'] - at < FAILURE_BACKOFF[step]
                or not (attempt.get('continuation') is None or
                        isinstance(attempt['continuation'], str) and attempt['continuation'])):
            raise ValueError('invalid failure attempt; retained for repair')
        claimed.add(attempt['run'])
        if attempt.get('continuation'):
            known.add(attempt['continuation'])
        at = attempt['at']
    if value['pending'] and not value['attempts']:
        raise ValueError('invalid pending failure claim')
    return value


def failure_paths(inst, thread=None):
    prefix = 'failure-' + (_m_jobs.digest(thread) + '-' if thread is not None else '')
    paths = sorted((inst.root / '.brain/wake').glob(prefix + '*.json'))
    if len(paths) > 1024:
        raise ValueError('failure receipt inventory exceeds bound')
    return paths


def failure_receipt(inst, record):
    matches, uncertain = [], []
    for path in failure_paths(inst, record['thread']):
        value = failure_read(path)
        if any(value[k] != record[k] for k in ('node', 'job_key', 'thread')):
            continue
        known = {value['run']} | {a['run'] for a in value['attempts']} | {
            a['continuation'] for a in value['attempts'] if a.get('continuation')}
        if record['run'] in known:
            matches.append(value)
        elif value['pending']:
            uncertain.append(value)  # a lost response cannot authorize a new episode/reset
    candidates = matches or uncertain
    if len(candidates) > 1:
        raise ValueError('ambiguous failure episode; retained for repair')
    return candidates[0] if candidates else None


def failure_publish(inst, value):
    # Creation and rename both sync their directories before an action is allowed.
    path = failure_receipt_path(inst, value)
    with _m_common.durable_directory_target(path, private=True) as target:
        target.atomic_write(json.dumps(value, sort_keys=True) + '\n')


@contextlib.contextmanager
def failure_instance_lock(inst):
    """No ordinary lock wait in the minute sampler; hold ownership through send."""
    if _m_instance._LOCK['fh'] is not None:
        raise ValueError('failure recovery requires an unlocked sampler')
    path = inst.root / '.brain/lock'
    with _m_common.directory_target(path, create=True, private=True) as target:
        fd = target.open(os.O_RDWR | os.O_CREAT)
    with os.fdopen(fd, 'r+') as stream:
        if os.fstat(stream.fileno()).st_nlink != 1:
            raise ValueError('linked failure instance lock')
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        stream.seek(0)
        stream.truncate()
        stream.write(str(os.getpid()))
        stream.flush()
        _m_instance._LOCK['fh'], _m_instance._LOCK['depth'] = stream, 1
        os.environ['BRAIN_LOCK_HELD'] = str(inst.root.resolve()) + '|' + str(os.getpid())
        try:
            yield True
        finally:
            _m_instance._LOCK['depth'] = 0
            _m_instance.unlock()


def failure_notice_records(inst):
    """Third claims remain actionable even after export observes a running run.

    An abandoned pending claim also needs attention: its delivery/continuation is
    unknown, so it must never restart the retry sequence or replay the send.
    """
    result = []
    for path in failure_paths(inst):
        try:
            value = failure_read(path)
            if len(value['attempts']) != 3 and not value['pending']:
                continue
            lock = failure_lock_path(inst, value)
            _m_common.safe_target(lock)
            fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'a+') as stream:
                if os.fstat(stream.fileno()).st_nlink != 1:
                    raise ValueError('linked failure lock')
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue  # still in flight; the next file-only watch reconciles it
                value = failure_read(path)
                if len(value['attempts']) == 3 or value['pending']:
                    result.append({**value, 'source': 'recovery-receipt', 'status': 'failed',
                                   'error': value['category']})
        except (OSError, ValueError):
            continue  # damaged receipts remain; current failures still get ordinary notices
    return result


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
    """One durable claim per failed thread/run, across a three-attempt episode."""
    if record.get('category') not in FAILURE_CATEGORIES[:-1]:
        return
    if _m_common.outward_links(inst.root):
        raise ValueError('unsafe failure recovery state')
    lock_path = failure_lock_path(inst, record)
    with _m_common.durable_directory_target(lock_path, private=True) as target:
        fd = target.open(os.O_CREAT | os.O_RDWR)
    with os.fdopen(fd, 'a+') as lock:
        if os.fstat(lock.fileno()).st_nlink != 1:
            raise ValueError('linked failure recovery lock')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        fresh = _m_instance.Instance(inst.root)
        if failure_owned(fresh, record) is None:
            return
        receipt = failure_receipt(fresh, record)
        now = inst.now_dt().timestamp()
        if receipt is None:
            receipt = {**{k: record[k] for k in ('node', 'job_key', 'thread', 'run', 'project', 'category')},
                       **{k: record[k] for k in ('branch', 'discovered', 'ambiguous') if k in record},
                       'version': 2, 'since': now, 'attempts': [], 'pending': False}
            failure_publish(inst, receipt)
        step = len(receipt['attempts'])
        if (step == 3 or receipt['pending'] or any(a['run'] == record['run'] for a in receipt['attempts'])
                or now - (receipt['attempts'][-1]['at'] if step else receipt['since']) < FAILURE_BACKOFF[step]):
            return
        rpc = _m_host_instances.routed_host_rpc(fresh, record['node'], rpc or _m_host_rpc.call)
        pending = rpc('t3_pending_request_list', {'threadId': record['thread']})
        if not isinstance(pending, dict) or pending.get('requestIds') != []:
            return
        with failure_instance_lock(inst) as acquired:
            if not acquired or failure_owned(_m_instance.Instance(inst.root), record) is None:
                return
            # This is the final host check, after the pending-request RPC and lock.
            detail = rpc('t3_thread_read', {'threadId': record['thread'], 'limit': 1, 'runLimit': 1})
            thread = detail.get('thread') if isinstance(detail, dict) else None
            if (not isinstance(thread, dict) or thread.get('threadId') != record['thread']
                    or thread.get('latestRunId') != record['run'] or thread.get('status') != 'failed'
                    or thread.get('activeRunId', 'unknown') is not None or thread.get('pendingRequestCount') != 0
                    or thread.get('projectId') != record['project']):
                return
            if record.get('discovered') and thread.get('branch') != record['branch']:
                return
            if failure_owned(_m_instance.Instance(inst.root), record) is None:
                return
            receipt['attempts'].append({'run': record['run'], 'at': inst.now_dt().timestamp(), 'continuation': None})
            receipt['pending'] = True
            # The third claim is itself the durable escalation obligation. Watch
            # reconciles it independently of the next host snapshot or process.
            failure_publish(inst, receipt)
            response = rpc('t3_thread_send', {
                'threadId': record['thread'], 'mode': 'steer',
                'clientRequestId': 'failure-' + _m_jobs.digest([record['thread'], record['run']]),
                'message': 'resume your task from your state note; the host failed your last turn on ' + record['category']})
            if (not isinstance(response, dict) or response.get('threadId') != record['thread']
                    or not isinstance(response.get('runId'), str) or not response['runId']
                    or response['runId'] == record['run']):
                raise ValueError('failure resume outcome unknown; claim retained without replay')
            receipt['attempts'][-1]['continuation'] = response['runId']
            receipt['pending'] = False
            failure_publish(inst, receipt)


def resume_failures_if_enabled(inst, rpc=None):
    if inst.local_of('root').get('host_export') != 'on':
        return
    from . import host_observations as _m_host_observations
    for record in _m_host_observations.host_export_runs(inst):
        try:
            resume_failure(inst, record, rpc=rpc)
        except (OSError, ValueError, _m_host_rpc.HostRPCError):
            print('worker recovery unavailable; claim retained, no replay')
