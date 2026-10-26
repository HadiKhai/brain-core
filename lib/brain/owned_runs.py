"""Explicit local reconciliation of recorded queue children, never host threads.

Only a gated child registered before exec is eligible. No process groups, PID
search, descendant inference, SIGKILL escalation, or verification-success replay.
"""
import fcntl
import json
import math
import os
import stat
import subprocess
from pathlib import Path
import sys
import time

from . import common as _m_common
from . import handback_queue as _m_queue
from . import instance as _m_instance
from . import job_attempts as _m_attempts
from . import jobs as _m_jobs
from . import lifecycle_identity as _m_identity
from . import machine as _m_machine


TAKEOVER_WINDOW_SECONDS = 86400


def owned_process_parent(pid):
    try:
        if sys.platform.startswith('linux'):
            return int(Path('/proc/%d/stat' % pid).read_text().rsplit(') ', 1)[1].split()[1])
        result = subprocess.run(['ps', '-p', str(pid), '-o', 'ppid='], capture_output=True,
                                text=True, timeout=2, env={**os.environ, 'LC_ALL': 'C'})
        return int(result.stdout.strip()) if result.returncode == 0 else None
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def queue_child(pid):
    """Publish a direct, still-gated verifier identity before releasing its exec."""
    context = _m_queue.QUEUE_LEASE.get()
    if context is None:
        return
    _, inst, n, request = context
    if type(pid) is not int or pid <= 0 or pid == os.getpid():
        raise ValueError('invalid owned queue child')
    stamp = _m_machine.process_stamp(pid)
    if stamp in (None, 'unknown') or owned_process_parent(pid) != os.getpid():
        raise ValueError('owned queue child identity unavailable; exec remains gated')
    with _m_instance.brain_lock(inst):
        data = _m_queue.queue_publication_context(n, request)
        info = os.fstat(_m_queue.queue_owned_context(n, data))
        run = data['run']
        run.setdefault('owner', list(data['owner']))
        run.setdefault('lease_identity', [info.st_dev, info.st_ino])
        run.setdefault('takeover_until', time.time() + TAKEOVER_WINDOW_SECONDS)
        children = run.setdefault('processes', [])
        if any(child[0] == pid for child in children):
            raise ValueError('owned queue child already recorded')
        children.append([pid, stamp])
        _m_queue.write_queue_request(n, data)
    _m_instance.unlock()


def write_takeover(n, identity, audit):
    path = _m_queue.queue_path(n, identity).with_suffix('.takeover.json')
    _m_common.safe_target(path)
    _m_common.atomic_write(path, json.dumps(audit, sort_keys=True) + '\n', private=True)


def _takeover_check(inst, n, expected, lease_fd, lock_fd):
    held = os.fstat(lock_fd)
    named = os.stat(_m_queue.queue_path(n, expected['id']).with_suffix('.takeover.lock'), follow_symlinks=False)
    if held.st_nlink != 1 or (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino):
        raise ValueError('takeover owner lock changed')
    data = _m_queue.read_queue_request(n, expected['id'])
    if data != expected:
        raise ValueError('owned run record changed before takeover')
    run, owner = data.get('run', {}), data.get('owner')
    host = _m_identity.lifecycle_host_identity()
    if not host or run.get('host') != host:
        raise ValueError('owned runner host is foreign or unknown')
    if (data['state'] != 'running' or type(run.get('version')) is not int or run.get('version') != 1
            or run.get('id') != data.get('lease') or run.get('phase') != 'dispatched'):
        raise ValueError('owned runner lease or phase changed')
    info = os.fstat(lease_fd)
    current = os.stat(_m_queue.queue_path(n, data['id']).with_suffix('.lease'), follow_symlinks=False)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or run.get('lease_identity') != [info.st_dev, info.st_ino]
            or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)):
        raise ValueError('owned runner lease inode changed')
    until = run.get('takeover_until')
    if type(until) not in (int, float) or not math.isfinite(until) or until <= time.time():
        raise ValueError('owned runner takeover lease expired or unknown')
    if (run.get('owner') != owner or not isinstance(owner, list) or len(owner) != 2 or type(owner[0]) is not int or owner[0] <= 0
            or not isinstance(owner[1], str) or owner[1] in ('', 'unknown')
            or _m_machine.process_stamp(owner[0]) is not None):
        raise ValueError('owned runner owner is live, reused or unknown')
    children = run.get('processes')
    if (not isinstance(children, list) or not children or len(children) > 1024
            or any(not isinstance(c, list) or len(c) != 2 or type(c[0]) is not int or c[0] <= 0
                   or c[0] in (owner[0], os.getpid()) or not isinstance(c[1], str)
                   or c[1] in ('', 'unknown') for c in children)
            or len({c[0] for c in children}) != len(children)):
        raise ValueError('owned process inventory invalid')
    for pid, stamp in children:
        observed = _m_machine.process_stamp(pid)
        if observed is not None:
            if observed != stamp:
                raise ValueError('owned process identity is foreign, reused or unknown')
            raise ValueError('automatic stop refused: live recorded process has no lifetime-bound identity handle')
    fresh = _m_instance.Instance(inst.root)
    current_node = fresh.node(n['id'])
    row = _m_jobs.open_job(_m_jobs.read_jobs(current_node) or [], data['args'].get('branch'))
    attempt = _m_attempts.current_attempt(current_node, row) if row else None
    if (not row or data.get('node') != n['id'] or data['args'].get('id') != n['id']
            or _m_jobs.job_key(row) != data['job_key'] or not attempt
            or attempt['id'] != data['attempt_id'] or attempt.get('request_id') != data['id']
            or attempt['state'] not in _m_attempts.ATTEMPT_ACTIVE
            or data['binding'] != attempt['binding'] or not _m_attempts.attempt_matches(current_node, row, attempt)):
        raise ValueError('owned run job, attempt or binding changed')
    return current_node, row


def takeover_owned_run(inst, n, identity):
    """Return True only when this exact dispatched request was interrupted safely.

    Only already-absent recorded processes can be reconciled. A live process
    refuses: PID/start observations do not provide a lifetime-bound signal handle.
    """
    path = _m_queue.queue_path(n, identity)
    lock_path = path.with_suffix('.takeover.lock')
    _m_common.safe_target(lock_path)
    lock = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    lease_fd = None
    audit = None
    try:
        info = os.fstat(lock)
        current = os.stat(lock_path, follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)):
            raise ValueError('unsafe takeover owner lock')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('takeover already has an owner')
        with _m_instance.brain_lock(inst):
            data = _m_queue.read_queue_request(n, identity)
            if data['state'] != 'running' or 'processes' not in data.get('run', {}):
                return False  # original legacy/pre-dispatch recovery rules still apply
            audit_path = path.with_suffix('.takeover.json')
            if audit_path.exists():
                _m_common.safe_target(audit_path)
                old = json.loads(audit_path.read_text())
                if old.get('state') == 'intent' or old.get('signalled'):
                    raise ValueError('previous partial takeover requires inspection; no repeated signal')
            audit = {'version': 1, 'request': identity, 'lease': data.get('lease'),
                     'owner': data.get('owner'), 'run': data.get('run'),
                     'controller': [os.getpid(), _m_machine.process_stamp(os.getpid())],
                     'state': 'intent', 'at': inst.now_dt().isoformat(), 'signalled': []}
            write_takeover(n, identity, audit)
            # Intent is durable before even the first ownership probe/action.
            lease_fd = os.open(path.with_suffix('.lease'), os.O_RDWR | os.O_NOFOLLOW)
            _takeover_check(inst, n, data, lease_fd, lock)
            current_node, row = _takeover_check(inst, n, data, lease_fd, lock)
            try:
                fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('descendant lease still held; partial takeover retained')
            current_node, row = _takeover_check(inst, n, data, lease_fd, lock)
            # No success publication or external operation is replayed. Separate
            # durable writes retain an intent if interruption itself is interrupted.
            data.update(state='interrupted', finished_at=inst.now_dt().isoformat())
            _m_queue.write_queue_request(current_node, data)
            if not _m_attempts.finish_attempt(inst, current_node, row, data['attempt_id'], 'interrupted'):
                raise ValueError('owned attempt changed during interruption; inspect intent')
            audit.update(state='settled', reason='recorded processes absent and original lease acquired')
            write_takeover(n, identity, audit)
            return True
    except (OSError, ValueError) as error:
        if audit is not None:
            audit.update(state='refused', reason=str(error))
            write_takeover(n, identity, audit)
        raise
    finally:
        if lease_fd is not None:
            os.close(lease_fd)
        os.close(lock)
        _m_instance.unlock()
