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

_m_attempts = _m_common.LazyModule(__package__, 'job_attempts')
_m_jobs = _m_common.LazyModule(__package__, 'jobs')
_m_machine = _m_common.LazyModule(__package__, 'machine')
_m_nodes = _m_common.LazyModule(__package__, 'nodes')
_m_lifecycle_identity = _m_common.LazyModule(__package__, 'lifecycle_identity')
_m_spend = _m_common.LazyModule(__package__, 'spend')


QUEUE_ARGS = ('id', 'branch', 'state', 'summary', 'question', 'lessons', 'report', 'reviews',
              'need_human', 'restart', 'reverify', 'full_receipt', 'record')
QUEUE_LEASE = contextvars.ContextVar('brain_queue_lease', default=None)

# v2 transport binds handback verification at application. Existing v1 requests
# keep their submission-time binding and foreground ownership protocol.
PATCH_FIELDS = frozenset(('title', 'description', 'kind', 'priority', 'order',
                          'estimate', 'spent', 'external_url', 'next_action'))
MAX_INTENT = 65536
# A queued plain handback waits this long for its receipt, then reports the queued ID.
HANDBACK_WAIT = 4.0
# Native application output replayed from a receipt keeps only its tail.
MAX_REPLAY = 16384


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
        if name.endswith(('.receipt', '.prepared', '.verdict', '.dispatch')):
            # These are published only by the single applier lease owner.
            # Stage + fsync + rename: no empty final receipt/preparation can be
            # exposed by death between create and write. Refuse an existing
            # final object rather than replacing its immutable evidence.
            if target.stat() is not None:
                raise FileExistsError(str(path))
            target.atomic_write(raw.decode(), expected=None)
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
    if (not isinstance(request, dict) or set(request) != {'version', 'id', 'payload', 'hash'}
            or type(request.get('version')) is not int or request['version'] != 2
            or request.get('id') != identity or not isinstance(request.get('payload'), dict)
            or request.get('hash') != intent_hash(request['payload'])):
        raise ValueError('blocked write queue: invalid receipt request')
    if (not isinstance(result, dict)
            or result.get('id') != identity or result.get('hash') != request.get('hash')
            or result.get('state') not in ('applied', 'rejected') or type(result.get('sequence')) is not int
            or result.get('view') not in ((None, 'pending', 'current', 'invalid') if result['state'] == 'applied'
                                          else (None,))
            or ('nodes' in result) != (result.get('view') is not None)
            or any(not isinstance(result.get(key, ''), str) for key in ('output', 'diagnostic'))):
        raise ValueError('blocked write queue: invalid receipt')
    nodes = result.get('nodes', [])
    # Legacy receipts (no view) were maintained synchronously. A view-bearing
    # receipt must name what its repair projects; nothing pending names nothing.
    if (not isinstance(nodes, list) or not all(isinstance(nid, str) and nid for nid in nodes)
            or len(set(nodes)) != len(nodes) or (result.get('view') in ('pending', 'invalid') and not nodes)):
        raise ValueError('blocked write queue: invalid receipt')
    if checked_handback(request['payload']):
        validate_handback_verdict(result)
        if result['state'] == 'rejected' and result['ready']:
            raise ValueError('blocked write queue: rejected readiness')
        if result['state'] == 'applied':
            prepared = intent_read(inst, identity + '.prepared')
            native = handback_verdict(inst, request, prepared)
            if any(result.get(key) != native.get(key) for key in (
                    'ready', 'verdict', 'reasons', 'exit_code', 'attempt_id', 'binding')):
                raise ValueError('blocked write queue: receipt differs from native verdict')
    try:
        marker = intent_read(inst, identity + '.view')
    except FileNotFoundError:
        return result
    ref = marker.get('ref') if isinstance(marker, dict) else None
    if (not isinstance(marker, dict) or set(marker) != {'id', 'hash', 'view', 'ref'}
            or marker['id'] != identity or marker['hash'] != result['hash'] or result.get('view') is None
            or not isinstance(ref, str) or not (re.fullmatch('[0-9a-f]{32}', ref) or
                                                marker['view'] == 'invalid' and ref == '')
            or marker['view'] not in ('current', 'invalid')):
        raise ValueError('blocked write queue: invalid view marker')
    return {**result, 'view': marker['view']}


def pending_views(inst):
    """Applied receipts whose deferred batch projection has not succeeded yet."""
    return [data for _, data in intent_index(inst)
            if (intent_receipt(inst, data['id']) or {}).get('view') in ('pending', 'invalid')]


def checked_handback(payload):
    from . import briefs
    args = payload.get('args', {})
    return (payload.get('kind') == 'handback' and args.get('state') == 'done'
            and not briefs.import_job(args.get('branch', ''))
            and (args.get('reviews') is not None or args.get('record', False)))


def validate_handback_verdict(result):
    if not isinstance(result, dict):
        raise ValueError('blocked write queue: invalid handback verdict')
    ready, reasons = result.get('ready'), result.get('reasons')
    if (type(ready) is not bool or not isinstance(reasons, list)
            or any(not isinstance(reason, str) or not reason for reason in reasons)
            or ready != (not reasons) or result.get('verdict') != ('ready' if ready else 'not-ready')
            or type(result.get('exit_code')) is not int or result['exit_code'] != (0 if ready else 1)):
        raise ValueError('blocked write queue: invalid handback verdict')


def handback_verdict(inst, request, prepared):
    """Read a native verdict only; interrupted verification never implies ready."""
    try:
        saved = intent_read(inst, request['id'] + '.verdict')
        dispatch = intent_read(inst, request['id'] + '.dispatch')
    except FileNotFoundError as error:
        raise RuntimeError('checked handback verdict missing; retain interrupted request') from error
    validate_handback_verdict(saved)
    if (prepared.get('hash') != request['hash']
            or saved.get('hash') != request['hash'] or saved.get('id') != request['id']
            or saved.get('attempt_id') != prepared['after']['attempt_id']
            or saved.get('handback_hash') != intent_hash(prepared['after'])
            or not isinstance(dispatch, dict) or dispatch.get('id') != request['id']
            or dispatch.get('hash') != request['hash'] or dispatch.get('job_key') != prepared['job_key']
            or dispatch.get('attempt_id') != saved.get('attempt_id')
            or dispatch.get('binding') != saved.get('binding')
            or not isinstance(saved.get('binding'), dict)
            or any(not isinstance(saved['binding'].get(key), str) for key in ('head', 'base', 'inputs'))):
        raise RuntimeError('checked handback verdict binding changed')
    return saved


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


def intent_lease(inst, name='applier.lease'):
    """Return a held independent lease, or None. Never wait behind a producer."""
    with _m_common.directory_target(intent_path(inst, name), create=True, private=True) as target:
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
            current = tasks.read_records(n, ident=a.task_id).get(a.task_id)
            if current and current.get('status') in ('merged', 'done'):
                return False  # effective completion proof retains synchronous refusal
            guard.no_terms_in(n, json.dumps(changes), 'task patch')
            payload = dict(kind='task', node=n['id'], task=a.task_id, changes=changes)
    elif (name == 'cmd_handback' and not getattr(a, 'direct', False)
          or name == 'cmd_ready' and getattr(a, 'record', False) and getattr(a, 'enqueue', False)):
        if name == 'cmd_ready':
            a.state = 'done'
        n = inst.node(a.id)
        # Cheap submit-time refusals; application revalidates under the lock.
        row = _m_jobs.open_job(_m_jobs.read_jobs(n) or [], a.branch)
        if not row:
            raise ValueError('no open job for handback')
        _m_jobs.validate_handback_options(a, row=row)
        args = {key: getattr(a, key, None) for key in QUEUE_ARGS}
        if name == 'cmd_handback' and not getattr(a, 'enqueue', False) and not default_queue(inst, n, row, a, args):
            return False  # the plain form stays synchronous unless queueing is safe and needed
        guard.no_terms_in(n, json.dumps(args), 'handback request')
        # The applier may serve many callers; spend belongs to this one.
        payload = dict(kind='handback', node=n['id'], job_key=_m_jobs.job_key(row), args=args,
                       sessions=_m_spend.spend_session_ids())
        if name == 'cmd_handback' and not getattr(a, 'enqueue', False):
            payload['carry_ready'] = True  # the plain form keeps --direct's readiness carry-forward
    if payload is None:
        return False
    if payload['kind'] == 'inbox' and getattr(a, 'request_id', None):
        try:
            previous = intent_read(inst, intent_id(a.request_id) + '.request')
            payload['at'] = previous['payload']['at']
        except FileNotFoundError:
            pass
    since = time.time()
    data = submit_intent(inst, payload, getattr(a, 'request_id', None), start=False)
    started = True
    try:
        start_intent_drainer(inst)
    except OSError:
        started = False
        print('write accepted; applier could not start: run brain queue drain, then queue wait ' + data['id'],
              file=sys.stderr)
    if name == 'cmd_handback' and not getattr(a, 'enqueue', False):
        wait = HANDBACK_WAIT if getattr(a, 'wait', None) is None else a.wait
        return lambda: await_handback(inst, n, data['id'], wait, started, since)
    if getattr(a, 'json', False):
        print(json.dumps(dict(state='queued', request_id=data['id'])))
    else:
        print(data['id'])
    return True


def default_queue(inst, n, row, a, args):
    """Whether a plain handback admits through the queue instead of running here.

    Only an unchecked, non-study message queues: verification binds this
    caller's execution context (environment, stdin, lane binding), which no
    applier reproduces. Links and guarded terms keep their exact synchronous
    refusals. An uncontended handback runs here as before; one that would wait
    on the writer lock, a running applier or earlier requests queues behind them.
    """
    from . import briefs, guard
    checked = a.state == 'done' and (args.get('reviews') is not None or args.get('record'))
    if checked or briefs.import_job(row['Job']):
        if getattr(a, 'request_id', None):
            _m_common.die('--request-id retries a queued handback; a checked or study handback queues only with --enqueue')
        return False
    if getattr(a, 'request_id', None):
        return True  # a retry reuses its queued identity; never publish a second, different handback
    if _m_common.outward_links(inst.root) or guard.find_terms(json.dumps(args), guard.never_terms(n)):
        return False
    if _m_instance.brain_lock(inst).owner_is_running_us():
        return False  # nested under our own holder: its lock is ours, an applier would wait on it
    return writer_busy(inst) or applier_busy(inst) or queue_pending(inst)


def pending_job_request(inst, n, row):
    """An earlier queued handback for this job: a synchronous publication must not
    overtake it, or its later application would replace the newer result."""
    try:
        pending = pending_intents(inst)
    except ValueError:
        return None  # a broken queue applies nothing until repaired; its own block reports it
    key = _m_jobs.job_key(row)
    return next((data['id'] for _, data in pending if data['payload'].get('kind') == 'handback'
                 and data['payload'].get('node') == n['id'] and data['payload'].get('job_key') == key), None)


def refuse_overtaking(inst, n, row):
    earlier = pending_job_request(inst, n, row)
    if earlier:
        _m_common.die('queued handback ' + earlier + ' for this job is still pending; '
                      'brain queue wait ' + earlier + ', then hand back again')


def writer_busy(inst):
    """A nonblocking probe of the instance writer lock; never waits or holds it."""
    try:
        with _m_common.directory_target(inst.root / '.brain' / 'lock', private=True) as target:
            fd = target.open()
    except (OSError, ValueError):
        return False  # absent or unsafe: the synchronous path owns its exact refusal
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)


def applier_busy(inst):
    fd = intent_lease(inst)
    if fd is None:
        # Projection holds the applier lease too, but releases the writer lock.
        # Earlier requests still force FIFO via queue_pending/refuse_overtaking;
        # a plain handback need not wait for generated text alone.
        projection = intent_lease(inst, 'projection.lease')
        if projection is None:
            return False
        os.close(projection)
        return True
    os.close(fd)
    return False


def queue_pending(inst):
    """Earlier requests keep FIFO order: a direct publication must not overtake them."""
    if intent_path(inst, 'blocked').exists():
        return True
    try:
        return bool(pending_intents(inst))
    except ValueError:
        return True  # invalid queue evidence: queue behind it and report the block honestly


def await_handback(inst, n, identity, wait, started=True, since=0.0):
    """Bounded receipt wait for a plain handback. Never takes the instance lock.

    Applied: replay the native output and exit code. Rejected: the refusal
    reaches this caller. Still pending: the queued ID, never a false record.
    """
    from . import guard, timings
    terms = guard.never_terms(n)
    with timings.queue_phase(inst.root, 'wait'):
        deadline = time.monotonic() + wait
        while True:
            receipt = intent_receipt(inst, identity)
            if receipt or time.monotonic() >= deadline:
                break
            time.sleep(0.025)
    if receipt is None:
        try:
            # A marker older than this submission may belong to a block that a
            # later drain is already retrying; a fresh one is this queue now.
            blocked = intent_path(inst, 'blocked').stat().st_mtime >= since
        except FileNotFoundError:
            blocked = False
        if blocked:
            _m_common.die('write queue blocked; handback ' + identity + ' not recorded yet: inspect '
                          '.brain/write-queue/blocked, then brain queue wait ' + identity)
        if not started:
            _m_common.die('handback ' + identity + ' accepted, not recorded: run brain queue drain, '
                          'then brain queue wait ' + identity)
        print('queued; applies shortly: ' + identity + ' (brain queue wait ' + identity + ' shows its result)')
        return
    sys.stdout.write(receipt.get('output', ''))
    sys.stdout.flush()
    if receipt.get('diagnostic'):
        sys.stderr.write(receipt['diagnostic'])
    if receipt['state'] == 'rejected':
        reason = (receipt.get('reason') or 'refused').removeprefix('brain: ')
        _m_common.die(guard.redact('handback not recorded: ' + reason, terms))
    if receipt.get('exit_code', 0):
        raise _m_common.VerdictExit(receipt['exit_code'])


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
            prepared = dict(hash=data['hash'], line=line, inbox_after_hash=inbox_after_hash(n, line, marker))
            intent_store(inst, identity + '.prepared', prepared, exclusive=True)
        if prepared.get('hash') != data['hash'] or prepared.get('line') != line:
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
        existing = tasks.read_records(n, ident=payload['task']).get(payload['task'])
        if existing and existing.get('status') in ('merged', 'done'):
            raise ValueError('effective completion gate cannot be queued')
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
        # Prepared recovery never exempts current mutable graph/proof inputs.
        records = tasks.read_records(n)
        records[payload['task']] = prepared['after']
        tasks.validate(n, records, selected=payload['task'])
        if current == prepared['before']:
            tasks.save_record(n, prepared['after'])
        elif current != prepared['after']:
            raise RuntimeError('task drift after preparation; retain blocked request')
        inst.commit([], n['id'] + ': task ' + payload['task'])
    elif payload['kind'] == 'handback':
        return apply_handback_intent(inst, n, data)
    else:
        raise ValueError('unsupported write request kind')


def archived_intent_marker(n, marker):
    path = n['dir'] / 'inbox-archive.md'
    try:
        with _m_common.directory_target(path) as target:
            return marker in target.read_bytes().decode()
    except FileNotFoundError:
        return False


def inbox_after_hash(n, line, marker):
    path = n['dir'] / 'inbox.md'
    try:
        with _m_common.directory_target(path) as target:
            raw = target.read_bytes()
    except FileNotFoundError:
        raw = b''
    return hashlib.sha256(raw if marker.encode() in raw else raw + line.encode()).hexdigest()


def prepare_intent_operation(op, request):
    """Bind the native journal begin to this exact immutable request, durably."""
    binding = dict(write_request=request['id'], request_hash=request['hash'], before_hash=intent_hash(op.before))
    intent_store(op.inst, request['id'] + '.operation', dict(binding, ref=op.ref, before=op.before, node=op.node))
    return binding


def resume_intent_operation(op, request):
    """Resume only our dead native operation; never adopt unrelated source gaps."""
    from . import events
    try:
        saved = intent_read(op.inst, request['id'] + '.operation')
    except FileNotFoundError:
        return False
    except (ValueError, OSError) as error:
        raise RuntimeError('damaged write operation evidence') from error
    if (not isinstance(saved, dict) or not isinstance(saved.get('before'), dict)
            or not isinstance(saved.get('ref'), str) or not re.fullmatch('[0-9a-f]{32}', saved['ref'])
            or any(not isinstance(k, str) or not isinstance(v, str) or not re.fullmatch('[0-9a-f]{64}', v)
                   for k, v in saved['before'].items())):
        raise RuntimeError('invalid write operation evidence')
    binding = dict(write_request=request['id'], request_hash=request['hash'], before_hash=intent_hash(saved['before']))
    if any(saved.get(k) != v for k, v in binding.items()) or saved.get('node') != op.node:
        raise RuntimeError('write operation binding changed')
    store = events.journal(op.inst)
    recorded, _, opened = events.replay(op.inst)
    old = opened.get(saved['ref'])
    if old is None:
        return False  # no begin yet, or the prior operation completed before its receipt
    if (old['data'].get('command') != op.command or not events.same_node(old['node'], op.node)
            or any(old['data'].get(k) != v for k, v in binding.items())
            or set(opened) != {saved['ref']} or store.state['faults']):
        raise RuntimeError('unrelated or damaged event operation; retain blocked request')
    actual = events.resources(op.inst)
    allowed = intent_recovery_resources(op.inst, request, actual)
    observed = {events.digest(path.encode()): (path, value) for path, value in actual.items()}
    for key in recorded.keys() | observed.keys():
        current = observed.get(key)
        value = current[1] if current else None
        if recorded.get(key, {}).get('hash') != value and (
                current is None or (allowed.get(current[0]) != value
                                    and saved['before'].get(current[0]) != value)):
            raise RuntimeError('unrelated source gap; retain blocked write operation')
    target = _m_common.directory_target(events.lease_path(op.inst, saved['ref']), create=True, private=True)
    target.__enter__()
    fd = None
    try:
        fd = target.open(os.O_RDWR | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        target.check_file(fd)
    except BaseException:
        if fd is not None:
            os.close(fd)
        target.__exit__(None, None, None)
        raise
    op.ref, op.before, op.lease_target, op.lease = saved['ref'], saved['before'], target, fd
    events._CURRENT.append(op)
    return True


def intent_recovery_resources(inst, request, actual):
    """Only prepared native effects may bridge the journal's coverage check."""
    n, payload = inst.node(request['payload']['node']), request['payload']
    try:
        prepared = intent_read(inst, request['id'] + '.prepared')
    except FileNotFoundError:
        prepared = {}
    if prepared and prepared.get('hash') != request['hash']:
        raise RuntimeError('prepared operation payload changed')
    allowed = {}
    if prepared.get('inbox_after_hash'):
        allowed[(n['dir'] / 'inbox.md').relative_to(inst.root).as_posix()] = prepared['inbox_after_hash']
    if payload['kind'] == 'handback':
        row = _m_jobs.open_job(_m_jobs.read_jobs(n) or [], payload['args']['branch'])
        if not row or _m_jobs.job_key(row) != payload['job_key']:
            raise RuntimeError('interrupted handback job changed')
        attempt = _m_attempts.current_attempt(n, row)
        if attempt and attempt.get('write_request') == request['id'] and attempt['state'] in ('queued', 'terminal', 'interrupted'):
            path = _m_attempts.attempt_path(n, row).relative_to(inst.root).as_posix()
            allowed[path] = actual.get(path)
        if prepared and _m_jobs.read_handback(n, row) == prepared['after']:
            path = _m_jobs.handback_file(n, row).relative_to(inst.root).as_posix()
            allowed[path] = actual.get(path)
    return allowed


def apply_handback_intent(inst, n, request):
    """Native publication and checked verdicts; verification binds here."""
    from . import tasks, transitions, events, hosts
    payload = request['payload']
    row = _m_jobs.open_job(_m_jobs.read_jobs(n) or [], payload['args']['branch'])
    if not row or _m_jobs.job_key(row) != payload['job_key']:
        raise ValueError('job changed before queued message application')
    checked = checked_handback(payload)
    try:
        prepared = intent_read(inst, request['id'] + '.prepared')
    except FileNotFoundError:
        attempt = _m_attempts.current_attempt(n, row)
        if checked and (intent_path(inst, request['id'] + '.dispatch').exists()
                        or attempt and attempt.get('write_request') == request['id']):
            raise RuntimeError('interrupted checked handback; retain verifier ownership evidence')
        args = argparse.Namespace(**payload['args'], enqueue=False, _intent_request=request)
        _m_jobs.validate_handback_options(args, row=row)
        # The immutable intent is deliberately bound at application time, never
        # represented as a v1 submission-time verifier binding.
        try:
            _m_jobs.cmd_handback(args)
        except _m_common.VerdictExit:
            if not checked:
                raise
        except SystemExit as error:
            # A native refusal may have created and retired an attempt. Close
            # that journal operation as a completed negative verdict, rather
            # than leaving its known interruption as an unrelated source gap.
            attempt = _m_attempts.current_attempt(n, row)
            published = _m_jobs.read_handback(n, row) or {}
            if (checked and not intent_path(inst, request['id'] + '.prepared').exists()
                    and attempt and attempt.get('write_request') == request['id']
                    and attempt['state'] == 'interrupted' and published.get('write_request') != request['id']):
                print(str(error), file=sys.stderr)
                raise _m_common.VerdictExit(1) from error
            raise
        if checked:
            prepared = intent_read(inst, request['id'] + '.prepared')
            return handback_verdict(inst, request, prepared)
        return None
    if prepared.get('hash') != request['hash'] or prepared.get('job_key') != payload['job_key']:
        raise RuntimeError('handback preparation identity changed')
    data = prepared['after']
    verdict = handback_verdict(inst, request, prepared) if checked else None
    with _m_instance.brain_lock(inst):
        attempt = _m_attempts.current_attempt(n, row)
        if not attempt or attempt['id'] != data['attempt_id']:
            raise RuntimeError('handback attempt changed after preparation')
        if checked and (attempt['binding'] != verdict.get('binding')
                        or not _m_attempts.attempt_matches(n, row, attempt)):
            raise RuntimeError('checked handback binding changed after verdict')
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
    return verdict


BATCH_REQUESTS = 16
BATCH_SECONDS = 15
VIEW_COALESCE_SECONDS = 0.25


def open_view_batch(inst, node):
    """One maintenance operation for a drained batch, opened after its applies."""
    from . import events
    args = argparse.Namespace(cmd='queue', action='views', id=node, f=cmd_intent_queue)
    operation = events.Command(inst.root, args)
    operation.write_batch = True
    operation.__enter__()
    return operation


def mark_views(inst, waiting, view, ref):
    with _m_instance.brain_lock(inst):
        for data in waiting:
            intent_store(inst, data['id'] + '.view', dict(id=data['id'], hash=data['hash'], view=view, ref=ref))


RECONCILE_WAIT = 30


def reconcile_dead_batches(inst):
    """A drainer killed during batch maintenance leaves a dead operation with
    only generated bytes. Reconcile it before admitting the next request; a
    request must never be rejected for this recoverable state."""
    from . import events
    deadline = time.monotonic() + RECONCILE_WAIT
    while True:
        with _m_instance.brain_lock(inst):
            _, _, opened = events.replay(inst)
            if not any(e['data'].get('write_batch') and not events.lease_live(inst, ref)
                       for ref, e in opened.items()):
                return
            try:
                events.reconcile(inst)
                return
            except SystemExit:
                pass  # a live foreign writer; wait for it outside the writer lock
        if time.monotonic() >= deadline:
            raise RuntimeError('interrupted view batch needs reconciliation beside a live writer; '
                               'run status --refresh, then queue drain')
        time.sleep(0.25)


def maintain_view_batch(inst):
    """Expose projection-only ownership without granting any write authority."""
    fd = intent_lease(inst, 'projection.lease')
    if fd is None:
        return False
    try:
        return _maintain_view_batch(inst, fd)
    finally:
        os.close(fd)


def _maintain_view_batch(inst, projection_fd):
    """Publish receipt-node groups before repairing unrelated invalid views.

    Each marker follows its own fenced projection commit. The detached drainer
    retains its lease and continues maintenance after that receipt can be read.
    """
    from . import events, status
    waiting = pending_views(inst)
    groups = {}
    for data in waiting:
        nodes = tuple(sorted(intent_receipt(inst, data['id'])['nodes']))
        groups.setdefault(nodes, []).append(data)
    succeeded = True
    for nodes, receipts in groups.items():
        try:
            with intent_touched(), _m_instance.brain_lock(inst):
                operation = open_view_batch(inst, nodes[0])
                # Reconciliation can clear invalidation without rewriting the
                # status files. Always project every node named by a receipt.
                operation.project_nodes = nodes
                operation.__exit__(None, None, None)
                invalid = events.journal(_m_instance.Instance(inst.root)).state['invalid']
                ok = (getattr(operation, 'maintenance_ok', False) is True
                      and not any(events.node_ref(nid) in invalid for nid in nodes))
                mark_views(inst, receipts, 'current' if ok else 'invalid', operation.ref)
        except (Exception, SystemExit):
            mark_views(inst, receipts, 'invalid', '')
            ok = False
        succeeded = succeeded and ok
    if not succeeded:
        return False
    if waiting:
        # Receipt-local projection is complete. The applier remains busy with
        # unrelated recovery; standalone deferred projection keeps its marker.
        fcntl.flock(projection_fd, fcntl.LOCK_UN)
    # Markers are already durable. This remaining work must not hold their
    # queue wait barriers, and a failure must not revoke their successful pass.
    try:
        with intent_touched(), _m_instance.brain_lock(inst):
            current = _m_instance.Instance(inst.root)
            invalid = events.journal(current).state['invalid']
            nodes = [n['id'] for n in current.nodes() if not n['archived'] and
                     (events.node_ref(n['id']) in invalid or status.generated_stale(n))]
            if nodes:
                operation = open_view_batch(inst, nodes[0])
                operation.repair_nodes = nodes
                operation.__exit__(None, None, None)
                invalid = events.journal(_m_instance.Instance(inst.root)).state['invalid']
                return (getattr(operation, 'maintenance_ok', False) is True
                        and not any(events.node_ref(nid) in invalid for nid in nodes))
    except (Exception, SystemExit):
        return False  # unrelated view repair must not block the write queue
    return True


def apply_view_batch(inst, maintenance=None):
    """Apply one bounded FIFO batch, then maintain its views once.

    Returns whether requests may remain after the batch bound.
    """
    from . import events
    started, applied, more = time.monotonic(), 0, True
    try:
        # The index is append-only and requests are immutable. Select one FIFO
        # prefix per bounded batch instead of rereading all receipt history for
        # every item. Producers appended during this pass join the next batch.
        pending = iter(pending_intents(inst))
        while applied == 0 or applied < BATCH_REQUESTS and time.monotonic() - started < BATCH_SECONDS:
            item = next(pending, None)
            if item is None:
                more = bool(pending_intents(inst)) if applied else False
                break
            sequence, data = item
            state, reason = 'applied', ''
            verdict = None
            diagnostic, output = io.StringIO(), io.StringIO()
            operation = None
            with intent_touched(), _m_instance.brain_lock(inst):
                # Selection grants no authority to apply replaced payloads or
                # requests that acquired a terminal receipt in the meantime.
                if (intent_read(inst, data['id'] + '.request') != data
                        or intent_receipt(inst, data['id']) is not None):
                    raise ValueError('blocked write queue: selected request evidence changed')
                current = _m_instance.Instance(inst.root)
                args = argparse.Namespace(cmd='queue', action='apply', id=data['payload']['node'], f=cmd_intent_queue)
                try:
                    # The native output belongs to the request's caller, via its receipt.
                    with contextlib.redirect_stderr(diagnostic), contextlib.redirect_stdout(output):
                        if _m_common.outward_links(inst.root):
                            raise ValueError('unsafe instance links; write request rejected')
                        operation = events.Command(inst.root, args)
                        operation.write_request = data
                        operation.defer_views = True
                        with operation:
                            verdict = apply_intent(current, data)
                except (ValueError, SystemExit) as error:
                    # Rejection is terminal only when no prepared primary effect
                    # exists. Partial application always remains blocked/replayable.
                    if intent_path(inst, data['id'] + '.prepared').exists():
                        raise RuntimeError('prepared application needs recovery') from error
                    state, reason = 'rejected', diagnostic.getvalue().strip() or str(error)
                    if checked_handback(data['payload']):
                        verdict = dict(ready=False, verdict='not-ready', reasons=[reason], exit_code=1)
                if checked_handback(data['payload']):
                    validate_handback_verdict(verdict)
                result = ({key: verdict[key] for key in ('ready', 'verdict', 'reasons', 'exit_code',
                           'attempt_id', 'binding') if key in verdict} if verdict else {})
                if data['payload']['kind'] == 'handback':
                    result['output'] = output.getvalue()[-MAX_REPLAY:]
                    if state == 'applied':
                        result['diagnostic'] = diagnostic.getvalue()[-MAX_REPLAY:]
                        if not result['output']:
                            # Recovery of a prepared request reprints nothing natively.
                            result['output'] = native_result_line(current, data, verdict)
                receipt = dict(id=data['id'], hash=data['hash'], sequence=sequence, state=state,
                               reason=reason or '; '.join(result.get('reasons', [])), **result)
                if state == 'applied':
                    refs = set(getattr(operation, 'invalidated', ()))
                    receipt['nodes'] = sorted(n['id'] for n in current.nodes() if events.node_ref(n['id']) in refs)
                    # A write that invalidated no live view has nothing pending.
                    receipt['view'] = ('current' if not receipt['nodes'] or getattr(operation, 'maintenance_ok', False)
                                       else 'pending' if getattr(operation, 'views_deferred', False) else 'invalid')
                with _m_instance.brain_lock(inst):
                    intent_store(inst, data['id'] + '.receipt', receipt, exclusive=True)
            applied += 1
    except BaseException:
        # Keep the primary failure; the receipts already published stay honest.
        with contextlib.suppress(Exception, SystemExit):
            maintain_view_batch(inst)
        raise
    # Copy offsets: the journal advances its own position dictionaries in place.
    with _m_instance.brain_lock(inst):
        cursor = {key: pos['offset'] for key, pos in events.journal(inst).positions.items()}
    ok = maintain_view_batch(inst)
    if maintenance is not None:
        maintenance.append((ok, cursor))
    return more


def deferred_writes_since(inst, cursor):
    """Read only the journal tail, including files rotated during projection."""
    from . import events
    for path in events._paths(inst):
        with events.private_open(path) as stream:
            info = os.fstat(stream.fileno())
            stream.seek(cursor.get('%s:%s' % (info.st_dev, info.st_ino), 0))
            for line in stream:
                event = events._event(line)
                if event['kind'] == 'commit' and event['data'].get('deferred') and event['data'].get('invalidate'):
                    return True
    return False


def drain_intents(inst, lease_fd=None):
    fd = intent_lease(inst) if lease_fd is None else lease_fd
    if fd is None:
        return
    blocked = False
    maintenance = []
    try:
        with _m_common.directory_target(intent_path(inst, 'applier.lease'), private=True) as target:
            target.check_file(fd)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        reconcile_dead_batches(inst)
        # One fixed window per lease, shared by foreground writes and accepted
        # requests. Producers never sleep or wait for projection computation.
        time.sleep(VIEW_COALESCE_SECONDS)
        while apply_view_batch(inst, maintenance):
            pass
        path = intent_path(inst, 'blocked')
        with _m_common.directory_target(path, private=True) as target:
            info = target.stat()
            if info:
                target.unlink_owned(path.name, (info.st_dev, info.st_ino))
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError) as error:
        blocked = True
        intent_store(inst, 'blocked', dict(state='blocked', reason=str(error)))
        raise
    finally:
        primary = sys.exc_info()[0]
        os.close(fd)
        # Release then inspect: a producer that saw the old lease is covered;
        # a producer arriving after this inspection acquires the free lease itself.
        if not blocked:
            try:
                from . import events, status
                with _m_instance.brain_lock(inst):
                    current = _m_instance.Instance(inst.root)
                    invalid = events.journal(current).state['invalid']
                    # A failed batch gets another attempt only for a new source
                    # commit, never for its own invalidation or unchanged damage.
                    late_view = (maintenance and any(not n['archived'] and
                                                    (events.node_ref(n['id']) in invalid or status.generated_stale(n))
                                                    for n in current.nodes()) and
                                 (maintenance[-1][0] or deferred_writes_since(current, maintenance[-1][1])))
                if pending_intents(inst) or late_view:
                    start_intent_drainer(inst)
            except (Exception, SystemExit):
                if primary is None:
                    raise


def native_result_line(inst, request, verdict):
    """The final line the native handback prints, from durable evidence only."""
    from . import guard, messages, verify
    n = inst.node(request['payload']['node'])
    terms = guard.never_terms(n)
    prepared = intent_read(inst, request['id'] + '.prepared')
    after = prepared['after']
    # Only a checked hand-back verified now; an unchecked one may carry an earlier duration.
    duration = verdict and _m_jobs.verify_duration_line(inst, n, after.get('verify_duration'))
    prefix = guard.redact(duration, terms) + '\n' if duration else ''
    if verdict:
        text = 'ready' if verdict['ready'] else 'not ready: ' + '; '.join(dict.fromkeys(verdict['reasons']))
        return prefix + verify.VerifyScrubber(terms).feed(text, final=True) + '\n'
    report = after.get('report') or _m_common.tilde(n['dir'] / 'job-handbacks' / (prepared['job_key'] + '.json'))
    kind = 'blocked' if after['state'] == 'failed' else after['state']
    return guard.redact(messages.format_worker_message(
        kind, 'coordinator can see it with brain due', prepared['branch'], report), terms) + '\n'


@contextlib.contextmanager
def intent_touched():
    """Process-global native write tracking belongs to one request at a time."""
    prior = set(_m_common.TOUCHED)
    _m_common.TOUCHED.clear()
    try:
        yield
    finally:
        _m_common.TOUCHED.clear()
        _m_common.TOUCHED.update(prior)


def cmd_intent_queue(a):
    inst = _m_instance.Instance(_m_nodes.find_root())
    if a.action == 'drain':
        drain_intents(inst, getattr(a, 'lease_fd', None))
        return
    identity = intent_id(a.id)
    deadline = time.monotonic() + a.timeout
    kicked = False
    while True:
        receipt = intent_receipt(inst, identity)
        if receipt and receipt.get('view') == 'pending':
            if not kicked:
                kicked = True
                start_intent_drainer(inst)  # no-op while a drain owns the lease
        elif receipt:
            print(json.dumps(receipt, sort_keys=True))
            if receipt['state'] != 'applied' or receipt.get('exit_code', 0) != 0:
                raise SystemExit(1)
            return  # an invalid view is reported only by the receipt's own view field
        if receipt and time.monotonic() >= deadline:
            _m_common.die('write applied; generated view pending; retain its ID and wait again')
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
