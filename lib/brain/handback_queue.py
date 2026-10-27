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
import hashlib
import subprocess
import sys
import time
import io

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

# v2 transport is instance-scoped. The v1 checked-handback runner below keeps
# its ownership/binding protocol; unchecked intents never enter that runner.
PATCH_FIELDS = frozenset(('title', 'description', 'kind', 'priority', 'order',
                          'estimate', 'spent', 'external_url', 'next_action'))
MAX_INTENT = 65536


def intent_bytes(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')) + '\n').encode()


def intent_hash(value):
    return hashlib.sha256(intent_bytes(value)).hexdigest()


def intent_path(inst, name):
    return inst.root / '.brain' / 'write-queue' / name


def intent_id(identity):
    if not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{32}', identity):
        raise ValueError('invalid write request ID')
    return identity


def intent_read(inst, name):
    from . import events
    return json.loads(events.private_read(intent_path(inst, name)))


def intent_store(inst, name, data, exclusive=False):
    path = intent_path(inst, name)
    raw = intent_bytes(data)
    with _m_common.directory_target(path, create=True, private=True) as target:
        if not exclusive:
            target.atomic_write(raw.decode())
            return
        fd = target.open(os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            if os.write(fd, raw) != len(raw):
                raise OSError('short write request; preserve evidence')
            os.fsync(fd)
            os.fsync(target.fd)
        finally:
            os.close(fd)


def submit_intent(inst, payload, identity=None, start=True):
    """Durable acceptance only. No instance lock, event operation or attempt."""
    from . import events
    identity = intent_id(identity or os.urandom(16).hex())
    data = dict(version=2, id=identity, payload=payload, hash=intent_hash(payload))
    if len(intent_bytes(data)) > MAX_INTENT:
        raise ValueError('write request exceeds 64 KiB')
    try:
        intent_store(inst, identity + '.request', data, exclusive=True)
    except FileExistsError:
        if intent_read(inst, identity + '.request') != data:
            raise ValueError('write request ID already has a different payload')
        # An earlier caller may have died after write but before fsync. A retry
        # must establish durability itself before acknowledging the same ID.
        with _m_common.directory_target(intent_path(inst, identity + '.request'), private=True) as target:
            fd = target.open()
            try:
                os.fsync(fd)
                os.fsync(target.fd)
            finally:
                os.close(fd)
    # The byte position of this one O_APPEND line establishes monotonic order.
    # Repeated identical IDs are transport retries, not additional applications.
    events.append_line(intent_path(inst, 'index'), dict(id=identity, hash=data['hash']))
    if start:
        try:
            start_intent_drainer(inst)
        except OSError:
            print('write accepted; applier could not start: run brain queue drain, then queue wait ' + identity,
                  file=sys.stderr)
    return data


def intent_index(inst):
    """Never skip a short/corrupt line or missing immutable payload."""
    from . import events
    try:
        with _m_common.directory_target(intent_path(inst, 'index'), private=True) as target:
            fd = target.open()
            try:
                # A stable prefix may grow while producers append. Replacement
                # is forbidden, growth is not a corrupt-snapshot condition.
                size = os.fstat(fd).st_size
                chunks, remaining = [], size
                while remaining:
                    chunk = os.read(fd, remaining)
                    if not chunk:
                        raise ValueError('blocked write queue: truncated index')
                    chunks.append(chunk)
                    remaining -= len(chunk)
                target.check_file(fd)
                raw = b''.join(chunks)
            finally:
                os.close(fd)
    except FileNotFoundError:
        return []
    rows, seen, offset = [], set(), 0
    for line in raw.splitlines(keepends=True):
        if not line.endswith(b'\n') or len(line) > 256:
            raise ValueError('blocked write queue: short or oversized index line')
        try:
            entry = json.loads(line)
            if not isinstance(entry, dict):
                raise ValueError('invalid index shape')
            identity = intent_id(entry['id'])
            data = intent_read(inst, identity + '.request')
            if (not isinstance(data, dict) or not isinstance(data.get('payload'), dict)
                    or set(data) != {'version', 'id', 'payload', 'hash'}
                    or set(entry) != {'id', 'hash'} or type(data.get('version')) is not int
                    or data.get('version') != 2 or data.get('id') != identity
                    or data.get('hash') != entry['hash'] or intent_hash(data['payload']) != entry['hash']):
                raise ValueError('request hash mismatch')
        except (KeyError, TypeError, ValueError, OSError) as error:
            raise ValueError('blocked write queue: invalid index/request evidence') from error
        if identity not in seen:
            rows.append((offset, data))
            seen.add(identity)
        offset += len(line)
    return rows


def intent_receipt(inst, identity):
    try:
        result = intent_read(inst, intent_id(identity) + '.receipt')
    except FileNotFoundError:
        return None
    request = intent_read(inst, identity + '.request')
    if (not isinstance(result, dict) or not isinstance(request, dict)
            or result.get('id') != identity or result.get('hash') != request.get('hash')
            or result.get('state') not in ('applied', 'rejected') or type(result.get('sequence')) is not int):
        raise ValueError('blocked write queue: invalid receipt')
    return result


def pending_intents(inst):
    pending = []
    for sequence, data in intent_index(inst):
        receipt = intent_receipt(inst, data['id'])
        if receipt:
            if receipt['sequence'] != sequence or pending:
                raise ValueError('blocked write queue: receipt order or index identity changed')
        else:
            pending.append((sequence, data))
    return pending


def intent_lease(inst):
    """Return a held independent lease, or None. Never wait behind a producer."""
    with _m_common.directory_target(intent_path(inst, 'applier.lease'), create=True, private=True) as target:
        fd = target.open(os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            target.check_file(fd)
            return fd
        except BlockingIOError:
            os.close(fd)
            return None
        except BaseException:
            os.close(fd)
            raise


def start_intent_drainer(inst):
    fd = intent_lease(inst)
    if fd is None:
        return
    try:
        env = {**os.environ, 'BRAIN_ROOT': str(inst.root), 'BRAIN_NO_REEXEC': '1'}
        env.pop('BRAIN_LOCK_HELD', None)
        subprocess.Popen([sys.executable, '-B', str(_m_common.ENTRY_POINT), 'queue', 'drain',
                          '--lease-fd', str(fd)], env=env, pass_fds=(fd,),
                         start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    finally:
        os.close(fd)


def fast_intent(a, root):
    """Route only the explicitly queueable public CLI forms, before outer lock."""
    from . import tasks, guard
    name = a.f.__name__
    payload = None
    inst = _m_instance.Instance(root)
    if name == 'cmd_inbox' and getattr(a, 'add', None):
        n = inst.node(a.id or 'root')
        text = _m_nodes.import_text(' '.join(a.add.split()), guard.never_terms(n))
        payload = dict(kind='inbox', node=n['id'], text=text, at=inst.now())
    elif name == 'cmd_task' and a.action == 'set':
        changes = tasks.command_changes(a)
        if changes and set(changes) <= PATCH_FIELDS:
            n = inst.node(a.node)
            guard.no_terms_in(n, json.dumps(changes), 'task patch')
            payload = dict(kind='task', node=n['id'], task=a.task_id, changes=changes)
    elif name == 'cmd_handback' and getattr(a, 'enqueue', False):
        n = inst.node(a.id)
        row = _m_jobs.open_job(_m_jobs.read_jobs(n) or [], a.branch)
        if not row:
            raise ValueError('no open job for handback')
        _m_jobs.validate_handback_options(a, row=row)
        from . import briefs
        checked = a.state == 'done' and not briefs.import_job(row['Job']) and (
            a.reviews is not None or getattr(a, 'record', False))
        if not checked:
            args = {key: getattr(a, key, None) for key in QUEUE_ARGS}
            guard.no_terms_in(n, json.dumps(args), 'handback request')
            payload = dict(kind='handback', node=n['id'], job_key=_m_jobs.job_key(row), args=args)
    if payload is None:
        return False
    if payload['kind'] == 'inbox' and getattr(a, 'request_id', None):
        try:
            previous = intent_read(inst, intent_id(a.request_id) + '.request')
            payload['at'] = previous['payload']['at']
        except FileNotFoundError:
            pass
    data = submit_intent(inst, payload, getattr(a, 'request_id', None))
    if getattr(a, 'json', False):
        print(json.dumps(dict(state='queued', request_id=data['id'])))
    else:
        print(data['id'])
    return True


def apply_intent(inst, data):
    from . import tasks, transitions, guard
    payload, identity = data['payload'], data['id']
    n = inst.node(payload['node'])
    guard.no_terms_in(n, json.dumps(payload), 'write request')
    if _m_common.outward_links(inst.root):
        raise ValueError('unsafe instance links; write request rejected')
    if payload['kind'] == 'inbox':
        marker = '<!-- write-request:' + identity + ' -->'
        line = '- ' + payload['at'] + ' from brain: ' + payload['text'] + ' ' + marker + '\n'
        try:
            prepared = intent_read(inst, identity + '.prepared')
        except FileNotFoundError:
            prepared = dict(hash=data['hash'], line=line)
            intent_store(inst, identity + '.prepared', prepared, exclusive=True)
        if prepared != dict(hash=data['hash'], line=line):
            raise RuntimeError('inbox preparation identity changed')
        # Archives retain the marker. A death after append must not resurrect it.
        archived = archived_intent_marker(n, marker)
        if not archived:
            transitions.inbox_append(n['dir'], line, marker)
        _m_common.touch(n['dir'] / 'inbox.md')
        _m_common.sync_directory(n['dir'])
        inst.commit([], n['id'] + ': inbox item')
    elif payload['kind'] == 'task':
        if not payload['changes'] or not set(payload['changes']) <= PATCH_FIELDS:
            raise ValueError('task gate fields cannot be queued')
        name = identity + '.prepared'
        try:
            prepared = intent_read(inst, name)
        except FileNotFoundError:
            before = tasks.read_records(n, ident=payload['task']).get(payload['task'])
            row, closed = tasks.prepare_mutation(inst, n, payload['task'], payload['changes'])
            if closed:
                raise ValueError('task completion cannot be queued')
            row['description'] = row.get('description', '').strip()
            prepared = dict(before=before, after=row, hash=data['hash'])
            intent_store(inst, name, prepared, exclusive=True)
        if prepared.get('hash') != data['hash']:
            raise ValueError('prepared request identity changed')
        current = tasks.read_records(n, ident=payload['task']).get(payload['task'])
        if current == prepared['before']:
            tasks.save_record(n, prepared['after'])
        elif current != prepared['after']:
            raise RuntimeError('task drift after preparation; retain blocked request')
        inst.commit([], n['id'] + ': task ' + payload['task'])
    elif payload['kind'] == 'handback':
        apply_handback_intent(inst, n, data)
    else:
        raise ValueError('unsupported write request kind')


def archived_intent_marker(n, marker):
    path = n['dir'] / 'inbox-archive.md'
    try:
        with _m_common.directory_target(path) as target:
            return marker in target.read_bytes().decode()
    except FileNotFoundError:
        return False


def apply_handback_intent(inst, n, request):
    """Unchecked native publication, with durable effects retained for replay."""
    from . import tasks, transitions, events, hosts
    payload = request['payload']
    row = _m_jobs.open_job(_m_jobs.read_jobs(n) or [], payload['args']['branch'])
    if not row or _m_jobs.job_key(row) != payload['job_key']:
        raise ValueError('job changed before queued message application')
    from . import briefs
    if (payload['args']['state'] == 'done' and not briefs.import_job(row['Job'])
            and payload['args'].get('reviews') is not None):
        raise ValueError('checked handback cannot use the write-intent applier')
    try:
        prepared = intent_read(inst, request['id'] + '.prepared')
    except FileNotFoundError:
        args = argparse.Namespace(**payload['args'], enqueue=False, _intent_request=request)
        _m_jobs.validate_handback_options(args, row=row)
        # The immutable intent is deliberately bound at application time, never
        # represented as a v1 submission-time verifier binding.
        _m_jobs.cmd_handback(args)
        return
    if prepared.get('hash') != request['hash'] or prepared.get('job_key') != payload['job_key']:
        raise RuntimeError('handback preparation identity changed')
    data = prepared['after']
    with _m_instance.brain_lock(inst):
        attempt = _m_attempts.current_attempt(n, row)
        if not attempt or attempt['id'] != data['attempt_id']:
            raise RuntimeError('handback attempt changed after preparation')
        current = _m_jobs.read_handback(n, row)
        if current == prepared['before']:
            if not _m_attempts.attempt_matches(n, row, attempt):
                raise RuntimeError('handback binding changed after preparation')
            events.write_record(inst, n, 'handback', _m_jobs.job_key(row), json.dumps(data, indent=2) + '\n')
        elif current != data:
            raise RuntimeError('handback drift after preparation; retain blocked request')
        _m_common.touch(_m_jobs.handback_file(n, row))
        _m_attempts.finish_attempt(inst, n, row, data['attempt_id'], 'terminal')
        if prepared['inbox_question']:
            bullet = ('- ' + data['at'] + ' from worker ' + prepared['branch'] + ': ' + data['state'] +
                      ': ' + prepared['message'] + ' <!-- wake:' + data['wake_id'] + ' --> ' + prepared['marker'] + '\n')
            marker = prepared['marker']
            archived = archived_intent_marker(n, marker)
            if not archived:
                transitions.inbox_append(n['dir'], bullet, marker)
            _m_common.touch(n['dir'] / 'inbox.md')
            _m_common.sync_directory(n['dir'])
        tasks.handback_event(inst, n, row, data)  # failure event uses the saved attempt ID
        inst.commit([], n['id'] + ': worker hand-back')
    _m_instance.unlock()
    if prepared['inbox_question'] or data['state'] == 'blocked' and not prepared['need']:
        hosts.request_wake(inst, n, 'wake:' + data['wake_id'])


def drain_intents(inst, lease_fd=None):
    from . import events
    fd = intent_lease(inst) if lease_fd is None else lease_fd
    if fd is None:
        return
    blocked = False
    try:
        with _m_common.directory_target(intent_path(inst, 'applier.lease'), private=True) as target:
            target.check_file(fd)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            pending = pending_intents(inst)
            if not pending:
                break
            sequence, data = pending[0]
            state, reason = 'applied', ''
            diagnostic = io.StringIO()
            with _m_instance.brain_lock(inst):
                current = _m_instance.Instance(inst.root)
                args = argparse.Namespace(cmd='queue', action='apply', id=data['payload']['node'], f=cmd_intent_queue)
                try:
                    with contextlib.redirect_stderr(diagnostic):
                        with events.Command(inst.root, args):
                            apply_intent(current, data)
                except (ValueError, SystemExit) as error:
                    # Rejection is terminal only when no prepared primary effect
                    # exists. Partial application always remains blocked/replayable.
                    if intent_path(inst, data['id'] + '.prepared').exists():
                        raise RuntimeError('prepared application needs recovery') from error
                    state, reason = 'rejected', diagnostic.getvalue().strip() or str(error)
                with _m_instance.brain_lock(inst):
                    intent_store(inst, data['id'] + '.receipt', dict(id=data['id'], hash=data['hash'],
                                 sequence=sequence, state=state, reason=reason), exclusive=True)
        path = intent_path(inst, 'blocked')
        with _m_common.directory_target(path, private=True) as target:
            info = target.stat()
            if info:
                target.unlink_owned(path.name, (info.st_dev, info.st_ino))
    except (OSError, ValueError, RuntimeError) as error:
        blocked = True
        intent_store(inst, 'blocked', dict(state='blocked', reason=str(error)))
        raise
    finally:
        os.close(fd)
        # Release then inspect: a producer that saw the old lease is covered;
        # a producer arriving after this inspection acquires the free lease itself.
        if not blocked and pending_intents(inst):
            start_intent_drainer(inst)


def cmd_intent_queue(a):
    inst = _m_instance.Instance(_m_nodes.find_root())
    if a.action == 'drain':
        drain_intents(inst, getattr(a, 'lease_fd', None))
        return
    identity = intent_id(a.id)
    deadline = time.monotonic() + a.timeout
    while True:
        receipt = intent_receipt(inst, identity)
        if receipt:
            print(json.dumps(receipt, sort_keys=True))
            if receipt['state'] != 'applied':
                raise SystemExit(1)
            return
        if intent_path(inst, 'blocked').exists():
            _m_common.die('write queue blocked; inspect .brain/write-queue/blocked')
        if time.monotonic() >= deadline:
            _m_common.die('write request pending; retain its ID and wait again')
        time.sleep(0.025)


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
        _m_common.atomic_write(path, json.dumps(data, sort_keys=True) + '\n', private=True)


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
