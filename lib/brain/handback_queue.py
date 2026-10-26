"""Durable requests and an explicitly caller-owned foreground handback runner.

Recovery is explicit: quiescent pre-dispatch work, exact terminal publication,
or recorded local verifier processes through owned_runs. Unknown descendants
retain the lease. No daemon, process-group signalling or legacy-run adoption.
"""
import argparse
import contextlib
import contextvars
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
from . import lifecycle_identity as _m_lifecycle_identity


QUEUE_ARGS = ('id', 'branch', 'state', 'summary', 'question', 'lessons', 'report', 'reviews',
              'need_human', 'restart', 'reverify', 'full_receipt')
QUEUE_LEASE = contextvars.ContextVar('brain_queue_lease', default=None)


def queue_owned_context(n, request, fd=None):
    """A copied lease token is not a foreground ownership capability."""
    context = QUEUE_LEASE.get()
    if context is None:
        raise ValueError('queue mutation requires the caller-owned lease descriptor')
    held, _, bound, claimed = context
    if (bound['dir'] != n['dir'] or claimed['id'] != request['id']
            or claimed['lease'] != request.get('lease') or fd is not None and held != fd
            or request.get('owner') != [os.getpid(), _m_machine.process_stamp(os.getpid())]):
        raise ValueError('queue foreground ownership changed')
    info = os.fstat(held)
    path = queue_path(n, request['id']).with_suffix('.lease')
    current = os.stat(path, follow_symlinks=False)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)):
        raise ValueError('queue lease descriptor replaced or unlinked')
    return held


def queue_descendant_fd():
    """Keep the caller-owned lease alive through native verifier exec boundaries."""
    context = QUEUE_LEASE.get()
    if context is None:
        return None
    fd, inst, n, request = context
    data = read_queue_request(n, request['id'])
    host = _m_lifecycle_identity.lifecycle_host_identity()
    if (not host or data.get('run', {}).get('host') != host
            or data.get('lease') != request['lease'] or data.get('state') != 'running'):
        raise ValueError('queue descendant lease changed')
    return queue_owned_context(n, data, fd)


def queue_dispatch(inst, n, request):
    """Fence before any code that may launch descendants, including admission."""
    with _m_instance.brain_lock(inst):
        data = read_queue_request(n, request['id'])
        host = _m_lifecycle_identity.lifecycle_host_identity()
        if (not host or data.get('run', {}).get('host') != host
                or data.get('lease') != request['lease'] or data['state'] != 'running'):
            raise ValueError('queue dispatch lease changed')
        queue_owned_context(n, data)
        data['run']['phase'] = 'dispatched'
        write_queue_request(n, data)
    _m_instance.unlock()


def recover_request(inst, n, data):
    """Called only under the original exclusive lease FD and instance lock."""
    owner, run = data.get('owner'), data.get('run')
    if (not isinstance(owner, list) or len(owner) != 2 or type(owner[0]) is not int
            or owner[0] <= 0 or not isinstance(owner[1], str) or owner[1] in ('', 'unknown')
            or not isinstance(run, dict) or run.get('id') != data.get('lease')
            or type(run.get('version')) is not int or run.get('version') != 1):
        raise ValueError('legacy or unknown runner ownership; preserve lease')
    host = _m_lifecycle_identity.lifecycle_host_identity()
    if not host or run.get('host') != host:
        raise ValueError('runner host is foreign or unknown; preserve lease without probing its PID')
    stamp = _m_machine.process_stamp(owner[0])
    if stamp is not None:
        raise ValueError('runner is live or unknown; preserve lease')
    row = _m_jobs.open_job(_m_jobs.read_jobs(n) or [], data['args'].get('branch'))
    attempt = _m_attempts.current_attempt(n, row) if row else None
    if (not row or data.get('node') != n['id'] or data['args'].get('id') != n['id']
            or _m_jobs.job_key(row) != data['job_key'] or not attempt
            or attempt['id'] != data['attempt_id'] or data['binding'] != attempt['binding']
            or not _m_attempts.attempt_matches(n, row, attempt)):
        raise ValueError('request job, attempt or binding changed; preserve lease')
    published = _m_jobs.read_handback(n, row) or {}
    if (published.get('attempt_id') == data['attempt_id']
            and published.get('queue_publication') == {'request': data['id'], 'lease': data['lease']}):
        _m_attempts.finish_attempt(inst, n, row, data['attempt_id'], 'terminal')
        data.update(state='terminal', finished_at=inst.now_dt().isoformat())
        write_queue_request(n, data)
        return False
    if run.get('phase') != 'claimed' or attempt['state'] != 'queued':
        raise ValueError('dispatched descendant shutdown unproven; preserve lease')
    data.setdefault('recovered', []).append({'lease': data['lease'], 'owner': owner, 'run': run})
    return True


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
def claim_handback(inst, n, identity, recover=False):
    """A stable lock FD plus durable run phase fences explicit crash takeover."""
    path = queue_path(n, identity).with_suffix('.lease')
    host = _m_lifecycle_identity.lifecycle_host_identity()
    if not host:
        raise ValueError('runner host identity unavailable; no lease acquired')
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
            if data['state'] == 'running' and recover:
                if not recover_request(inst, n, data):
                    yield None
                    return
            elif data['state'] != 'queued':
                raise ValueError('request is not queued; retained running leases need ownership recovery')
            data.update(state='running', lease=os.urandom(16).hex(),
                        owner=[os.getpid(), _m_machine.process_stamp(os.getpid())])
            if data['owner'][1] in (None, 'unknown'):
                raise ValueError('cannot identify foreground runner')
            data['run'] = {'version': 1, 'id': data['lease'], 'phase': 'claimed', 'host': host}
            write_queue_request(n, data)
        _m_instance.unlock()  # the foreground lease, not brain_lock, owns the wait
        context = QUEUE_LEASE.set((fd, inst, n, data))
        try:
            yield data
        finally:
            QUEUE_LEASE.reset(context)
    finally:
        os.close(fd)


def queue_terminal(inst, n, identity, lease, state):
    if state not in ('terminal', 'interrupted', 'invalidated'):
        raise ValueError('invalid terminal request state')
    with _m_instance.brain_lock(inst):
        data = read_queue_request(n, identity)
        host = _m_lifecycle_identity.lifecycle_host_identity()
        if (not host or data.get('run', {}).get('host') != host
                or data['state'] != 'running' or data['lease'] != lease):
            return False
        try:
            queue_owned_context(n, data)
        except (OSError, ValueError):
            return False
        data.update(state=state, finished_at=inst.now_dt().isoformat())
        write_queue_request(n, data)
        return True


def queue_publication_context(n, request):
    """Re-read the exact dispatched claim under the caller's instance writer lock.

    A token or copied request is insufficient: the current process must still
    hold the original lease inode. Refusal leaves reconciliation evidence intact.
    """
    data = read_queue_request(n, request['id'])
    queue_owned_context(n, data)
    claimed = QUEUE_LEASE.get()[3]
    fields = ('id', 'node', 'job_key', 'attempt_id', 'binding', 'args', 'lease', 'owner')
    run, host = data.get('run', {}), _m_lifecycle_identity.lifecycle_host_identity()
    if (any(data.get(key) != request.get(key) or data.get(key) != claimed.get(key) for key in fields)
            or data['state'] != 'running' or data.get('node') != n['id']
            or data['args'].get('id') != n['id'] or not host
            or type(run.get('version')) is not int or run.get('version') != 1
            or run.get('id') != data['lease'] or run.get('phase') != 'dispatched'
            or run.get('host') != host or run.get('host') != claimed.get('run', {}).get('host')):
        raise ValueError('queue publication ownership changed; preserve running evidence')
    return data


def queue_projection(n, identity, strict=False):
    try:
        data = read_queue_request(n, identity)
        waiting = sorted((read_queue_request(n, p.stem) for p in queue_path(n, identity).parent.glob('*.json')
                          if re.fullmatch('[0-9a-f]{32}', p.stem)),
                         key=lambda r: (r['at'], r['id']))
        waiting = [r['id'] for r in waiting if r['state'] == 'queued']
        return {'request_id': identity, 'state': data['state'], 'owner': data['owner'],
                'position': waiting.index(identity) + 1 if identity in waiting else None,
                'recorded_at': data['at'], 'owner_activity': 'unknown'}
    except (OSError, ValueError, KeyError):
        if strict:
            raise
        return {'request_id': identity, 'state': 'unknown', 'position': None, 'owner': None}


def run_queue_once(inst, n, identity=None, recover=False):
    _m_instance.unlock()  # only short state transactions hold the instance lock
    if recover and identity is None:
        raise ValueError('recovery requires an explicit request ID')
    if identity is None:
        waiting = sorted((read_queue_request(n, p.stem) for p in (n['dir'] / 'handback-queue').glob('*.json')
                          if re.fullmatch('[0-9a-f]{32}', p.stem)),
                         key=lambda r: (r['at'], r['id']))
        identity = next((r['id'] for r in waiting if r['state'] == 'queued'), None)
    if identity is None:
        return None
    if recover:
        from . import owned_runs
        if owned_runs.takeover_owned_run(inst, n, identity):
            return identity
    with claim_handback(inst, n, identity, recover=recover) as request:
        if request is None:
            return identity  # publication already exists; never replay its wake
        queue_dispatch(inst, n, request)
        with _m_instance.brain_lock(inst):
            inst = _m_instance.Instance(inst.root)
            n = inst.node(n['id'])
            claim = queue_publication_context(n, request)
            row = _m_jobs.open_job(_m_jobs.read_jobs(n) or [], claim['args'].get('branch'))
            attempt = _m_attempts.current_attempt(n, row) if row else None
            if (not row or _m_jobs.job_key(row) != claim['job_key'] or not attempt
                    or attempt['id'] != claim['attempt_id'] or attempt['state'] != 'queued'
                    or attempt.get('request_id') != identity or claim['binding'] != attempt['binding']
                    or not _m_attempts.attempt_matches(n, row, attempt)):
                if not queue_terminal(inst, n, identity, claim['lease'], 'invalidated'):
                    raise ValueError('queue invalidation refused; preserve running evidence')
                if row:
                    _m_attempts.finish_attempt(inst, n, row, claim['attempt_id'], 'invalidated')
                return identity
            _m_attempts.update_attempt(inst, n, row, attempt['id'], 'queued', owner=claim['owner'])
        _m_instance.unlock()
        args = argparse.Namespace(**{key: request['args'].get(key) for key in QUEUE_ARGS})
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
        result = run_queue_once(inst, inst.node(a.id), a.request_id, recover=getattr(a, 'recover', False))
    except (OSError, ValueError) as error:
        _m_common.die(str(error))
    if result:
        print(result)
