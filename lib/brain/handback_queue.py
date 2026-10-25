"""Durable requests and an explicitly caller-owned foreground handback runner.

No daemon, cancellation, takeover or descendant crash recovery. An abandoned
running lease stays unknown until the ownership redesign supplies safe recovery.
"""
import argparse
import contextlib
import fcntl
import json
import os
import re
import stat

from . import common as _m_common
from . import instance as _m_instance
from . import job_attempts as _m_attempts
from . import jobs as _m_jobs
from . import machine as _m_machine
from . import nodes as _m_nodes


QUEUE_ARGS = ('id', 'branch', 'state', 'summary', 'question', 'lessons', 'report', 'reviews',
              'need_human', 'restart', 'reverify', 'full_receipt')


def queue_path(n, identity):
    if not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{32}', identity):
        raise ValueError('invalid handback request ID')
    return n['dir'] / 'handback-queue' / (identity + '.json')


def read_queue_request(n, identity):
    path = queue_path(n, identity)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('linked handback request evidence')
    data = json.loads(path.read_text())
    if (not isinstance(data, dict) or type(data.get('version')) is not int or data.get('version') != 1 or data.get('id') != identity
            or data.get('state') not in ('queued', 'running', 'terminal', 'interrupted', 'invalidated')
            or not isinstance(data.get('args'), dict) or not isinstance(data.get('binding'), dict)
            or not isinstance(data.get('at'), str)
            or not re.fullmatch('[0-9a-f]{24}', str(data.get('job_key', '')))
            or not re.fullmatch('[0-9a-f]{32}', str(data.get('attempt_id', '')))):
        raise ValueError('invalid handback request; preserve for inspection')
    return data


def write_queue_request(n, data):
    from . import events
    with events.node_write(n["dir"], "handback_queue"):
        path = queue_path(n, data['id'])
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError('linked handback request evidence')
        _m_common.safe_target(path)
        path.parent.mkdir(exist_ok=True)
        _m_common.atomic_write(path, json.dumps(data, sort_keys=True) + '\n')


def enqueue_handback(inst, n, row, args):
    _m_jobs.validate_handback_options(args, row=row)
    with _m_instance.brain_lock(inst):
        identity = os.urandom(16).hex()
        attempt = _m_attempts.begin_attempt(inst, n, row, request_id=identity)
        data = {'version': 1, 'id': identity, 'node': n['id'], 'job_key': _m_jobs.job_key(row),
                'attempt_id': attempt['id'], 'binding': attempt['binding'], 'state': 'queued',
                'at': inst.now_dt().isoformat(), 'owner': None, 'lease': None,
                'args': {key: getattr(args, key, None) for key in QUEUE_ARGS}}
        try:
            write_queue_request(n, data)
        except BaseException:
            _m_attempts.finish_attempt(inst, n, row, attempt['id'], 'interrupted')
            raise
        return data


@contextlib.contextmanager
def claim_handback(inst, n, identity):
    """The lock FD is the live lease. Persistent running state forbids crash takeover."""
    path = queue_path(n, identity).with_suffix('.lease')
    _m_common.safe_target(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_nlink != 1:
            raise ValueError('unsafe handback lease')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('handback request already has an owner')
        with _m_instance.brain_lock(inst):
            data = read_queue_request(n, identity)
            if data['state'] != 'queued':
                raise ValueError('request is not queued; retained running leases need ownership recovery')
            data.update(state='running', lease=os.urandom(16).hex(),
                        owner=[os.getpid(), _m_machine.process_stamp(os.getpid())])
            if data['owner'][1] in (None, 'unknown'):
                raise ValueError('cannot identify foreground runner')
            write_queue_request(n, data)
        _m_instance.unlock()  # the foreground lease, not brain_lock, owns the wait
        yield data
    finally:
        os.close(fd)


def queue_terminal(inst, n, identity, lease, state):
    if state not in ('terminal', 'interrupted', 'invalidated'):
        raise ValueError('invalid terminal request state')
    with _m_instance.brain_lock(inst):
        data = read_queue_request(n, identity)
        if data['state'] != 'running' or data['lease'] != lease:
            return False
        data.update(state=state, finished_at=inst.now_dt().isoformat())
        write_queue_request(n, data)
        return True


def queue_projection(n, identity, strict=False):
    try:
        data = read_queue_request(n, identity)
        waiting = sorted((read_queue_request(n, p.stem) for p in queue_path(n, identity).parent.glob('*.json')),
                         key=lambda r: (r['at'], r['id']))
        waiting = [r['id'] for r in waiting if r['state'] == 'queued']
        return {'request_id': identity, 'state': data['state'], 'owner': data['owner'],
                'position': waiting.index(identity) + 1 if identity in waiting else None,
                'recorded_at': data['at'], 'owner_activity': 'unknown'}
    except (OSError, ValueError, KeyError):
        if strict:
            raise
        return {'request_id': identity, 'state': 'unknown', 'position': None, 'owner': None}


def run_queue_once(inst, n, identity=None):
    _m_instance.unlock()  # only short state transactions hold the instance lock
    if identity is None:
        waiting = sorted((read_queue_request(n, p.stem) for p in (n['dir'] / 'handback-queue').glob('*.json')),
                         key=lambda r: (r['at'], r['id']))
        identity = next((r['id'] for r in waiting if r['state'] == 'queued'), None)
    if identity is None:
        return None
    with claim_handback(inst, n, identity) as request:
        row = _m_jobs.open_job(_m_jobs.read_jobs(n) or [], request['args'].get('branch'))
        attempt = _m_attempts.current_attempt(n, row) if row else None
        if (request.get('node') != n['id'] or request['args'].get('id') != n['id']
                or not row or _m_jobs.job_key(row) != request['job_key'] or not attempt
                or attempt['id'] != request['attempt_id'] or attempt['state'] != 'queued'
                or not _m_attempts.attempt_matches(n, row, attempt)):
            queue_terminal(inst, n, identity, request['lease'], 'invalidated')
            if row:
                _m_attempts.finish_attempt(inst, n, row, request['attempt_id'], 'invalidated')
            return identity
        args = argparse.Namespace(**{key: request['args'].get(key) for key in QUEUE_ARGS})
        _m_attempts.update_attempt(inst, n, row, attempt['id'], 'queued', owner=request['owner'])
        args._attempt_id, args._queue_request, args.enqueue = attempt['id'], request, False
        # This is the native synchronous writer/verifier, including its own FIFO
        # reservation. Exceptions retain evidence; there is no takeover/signalling.
        _m_jobs.cmd_handback(args)
        return identity


def cmd_queue(a):
    if not a.once:
        _m_common.die('queue run requires --once; no background runner is installed')
    inst = _m_nodes.load()
    try:
        result = run_queue_once(inst, inst.node(a.id), a.request_id)
    except (OSError, ValueError) as error:
        _m_common.die(str(error))
    if result:
        print(result)
