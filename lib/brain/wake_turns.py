"""Binding-fenced consumption receipts; transport acceptance never proves a run."""
import contextlib
import fcntl
import json
import math
import os
import re

from . import common as _m_common
from . import instance as _m_instance

_m_hosts = _m_common.LazyModule(__package__, 'hosts')


@contextlib.contextmanager
def wake_turn_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'turn.lock'
    _m_common.safe_target(path)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a+') as stream:
        if os.fstat(stream.fileno()).st_nlink != 1:
            raise OSError('linked wake turn lock')
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield directory / 'turn.json'


def wake_turn_binding(inst, n):
    target, command = _m_hosts.wake_configured_route(inst, n)
    return _m_hosts.wake_binding(str(target['local'].get('coordinator', '')) + '\0' + command)


def wake_turn_enabled(n):
    return True  # consumption fencing is mandatory, including legacy routes


def wake_turn_session_binding(n):
    sessions = n['local'].get('coordinator_sessions', [])
    selected = sessions[-1] if isinstance(sessions, list) and sessions else ''
    return _m_hosts.wake_binding(str(n['local'].get('coordinator', '')) + '\0' + selected)


def wake_turn_caller(inst, n):
    """Only the bound coordinator or its selected runtime session can consume."""
    if 'T3CODE_THREAD_ID' in os.environ:
        # T3's Claude SDK environment binds this host id independently of the
        # provider runtime session. A foreign native id cannot fall back to one.
        return bool(os.environ['T3CODE_THREAD_ID']) and os.environ['T3CODE_THREAD_ID'] == n['local'].get('coordinator')
    callers = {os.environ[key] for key in ('CODEX_THREAD_ID', 'CLAUDE_CODE_SESSION_ID') if os.environ.get(key)}
    sessions = n['local'].get('coordinator_sessions', [])
    allowed = {n['local'].get('coordinator')}
    if isinstance(sessions, list) and sessions:
        bound = n['local'].get('coordinator_session_binding') == wake_turn_session_binding(n)
        if not bound and 'coordinator_session_binding' not in n['local']:
            from . import rotation
            state = rotation.read_rotation(inst, n)
            bound = bool(state and state.get('coordinator') == n['local'].get('coordinator') and
                         sessions[-1] in state.get('coordinator_sessions', []) and
                         not rotation.needs_session(state, n))
        if bound:
            allowed.add(sessions[-1])
    return bool(callers) and callers <= allowed


def wake_turn_outstanding(inst, n):
    try:
        value = wake_turn_read(_m_hosts.wake_directory(inst, n) / 'turn.json', wake_turn_binding(inst, n))
        return bool(value and not value['consumed'])
    except (OSError, ValueError, KeyError, TypeError):
        return False


def wake_terminal_claims(inst, n):
    """Unreadable peers hold only terminal work, never independent questions."""
    claimed, unknown = set(), False
    for other in inst.nodes():
        if other['id'] == n['id'] or other['archived']:
            continue
        try:
            receipt = wake_turn_read(_m_hosts.wake_directory(inst, other) / 'turn.json',
                                     wake_turn_binding(inst, other))
            if receipt and not receipt['consumed']:
                claimed.update(i['id'] for i in receipt['items'] if i['kind'] in _m_hosts.WAKE_TERMINAL_KINDS)
        except (OSError, ValueError, KeyError, TypeError):
            unknown = True
    return claimed, unknown


def wake_turn_read(path, binding):
    if not path.exists():
        return None
    value = json.loads(path.read_text())
    if (not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1 or
            not isinstance(value.get('binding'), str) or not re.fullmatch(r'[0-9a-f]{64}', value['binding']) or
            not isinstance(value.get('token'), str) or not re.fullmatch(r'[0-9a-f]{32}', value['token']) or
            type(value.get('at')) not in (int, float) or not math.isfinite(value['at']) or
            value['at'] < 0 or value['at'] > _m_hosts.WAKE_CLOCK.time() or
            type(value.get('sent')) is not bool or type(value.get('consumed')) is not bool or
            type(value.get('reminded')) is not bool or not isinstance(value.get('paths'), list) or
            any(not isinstance(p, str) or not re.fullmatch(r'[0-9a-f]{64}\.pending', p) for p in value['paths']) or
            not isinstance(value.get('items'), list)):
        raise ValueError('invalid wake turn')
    if len(value['paths']) != len(set(value['paths'])):
        raise ValueError('duplicate wake turn paths')
    if ('queue_known' in value and type(value['queue_known']) is not bool or
            'route_binding' in value and (not isinstance(value['route_binding'], str) or
                not re.fullmatch(r'[0-9a-f]{64}', value['route_binding'])) or
            'opaque_paths' in value and (not isinstance(value['opaque_paths'], list) or
                any(not isinstance(p, str) or p not in value['paths'] for p in value['opaque_paths']) or
                len(value['opaque_paths']) != len(set(value['opaque_paths'])))):
        raise ValueError('invalid wake queue projection metadata')
    if any(not isinstance(i, dict) or set(i) - {'id', 'kind', 'question'} or
           'question' in i and i['question'] is not True for i in value['items']):
        raise ValueError('invalid canonical wake turn item')
    refs = _m_hosts.wake_item_refs(value['items'])
    if len(refs) != len(value['items']) or any(not _m_hosts.wake_known_kind(i['kind']) for i in refs):
        raise ValueError('duplicate or unknown wake turn identity')
    if any(not isinstance(i['id'], str) or not re.fullmatch(r'[0-9a-f]{64}', i['id'])
           or not isinstance(i['kind'], str) for i in refs):
        raise ValueError('invalid wake turn identities')
    return value if value['binding'] == binding else None


def wake_turn_write(path, value):
    _m_common.atomic_write(path, json.dumps(value, sort_keys=True) + '\n', private=True)


def wake_turn_pending_summary(pending):
    opaque, known = [], True
    for path in pending:
        try:
            _, refs = _m_hosts.wake_pending_value(path)
            if not refs:
                opaque.append(path.name)
        except (OSError, ValueError, KeyError, TypeError):
            known = False
            opaque.append(path.name)
    return opaque, known


def wake_turn_prepare(inst, n, pending, refs):
    """Reserve before transport; unknown prior acceptance must never cause a duplicate."""
    if not wake_turn_enabled(n):
        return 'hold', None
    directory = _m_hosts.wake_directory(inst, n)
    try:
        with wake_turn_lock(directory) as path:
            binding = wake_turn_binding(inst, n)
            value = wake_turn_read(path, binding)
            if value is None and path.exists():
                retired_value = json.loads(path.read_text())
                command = _m_hosts.wake_configured_route(inst, n)[1]
                if (command.startswith(('https://', 'http://')) and
                        retired_value.get('route_binding') == _m_hosts.wake_binding(command) and
                        n['local'].get('wake_verified_binding') != binding):
                    return 'stale', None  # the same webhook may still target the retired coordinator
                # A valid retired binding is not authority for this coordinator.
                # Preserve its exact receipt before reserving the replacement.
                retired = path.with_name('turn-retired-' + os.urandom(8).hex() + '.json')
                _m_common.safe_target(retired)
                path.replace(retired)
            opaque, known = wake_turn_pending_summary(pending)
            if value and not value['consumed']:
                value['items'] = _m_hosts.wake_item_refs(value['items'] + refs)
                value['paths'] = sorted(set(value['paths']) | {p.name for p in pending})
                value['opaque_paths'] = sorted(set(value.get('opaque_paths', [])) | set(opaque))
                value['queue_known'] = value.get('queue_known', False) and known
                wake_turn_write(path, value)
                return 'hold', value['token']
            if not pending:
                return 'hold', None
            token = os.urandom(16).hex()
            value = {'version': 1, 'binding': binding, 'token': token, 'sent': False,
                     'route_binding': _m_hosts.wake_binding(_m_hosts.wake_configured_route(inst, n)[1]),
                     'consumed': False, 'reminded': False, 'at': _m_hosts.WAKE_CLOCK.time(),
                     'items': refs, 'paths': [p.name for p in pending], 'opaque_paths': opaque, 'queue_known': known}
            wake_turn_write(path, value)
            return 'send', token
    except (OSError, ValueError, KeyError, TypeError):
        return 'hold', None


def wake_turn_finish(inst, n, token, sent):
    if token is None:
        return
    try:
        with wake_turn_lock(_m_hosts.wake_directory(inst, n)) as path:
            value = wake_turn_read(path, wake_turn_binding(inst, n))
            if value and value['token'] == token:
                if sent is False or value['consumed']:
                    path.unlink(missing_ok=True)
                else:
                    value['sent'] = bool(sent)  # None is an unknown outcome; never authorize a retry.
                    wake_turn_write(path, value)
    except (OSError, ValueError, KeyError, TypeError):
        pass  # successful transport is never repeated to repair bookkeeping


def wake_turn_consume(inst, n, consume=True, shown=None, recover=False):
    """Consume only this binding's captured reasons; later arrivals stay queued."""
    if _m_hosts.wake_route_disabled(inst, n):
        return None
    if consume or recover:
        fresh = _m_instance.Instance(inst.root)
        current = fresh.node(n['id'])
        if (not wake_turn_caller(fresh, current) or wake_turn_binding(fresh, current) != wake_turn_binding(inst, n)
                or _m_hosts.wake_route_disabled(fresh, current)):
            return None
        inst, n = fresh, current
    directory = _m_hosts.wake_directory(inst, n)
    try:
        with wake_turn_lock(directory) as path:
            try:
                value = wake_turn_read(path, wake_turn_binding(inst, n))
            except (OSError, ValueError, KeyError, TypeError):
                if not recover:
                    raise
                # A real coordinator can reconstruct a display receipt from the
                # retained queue. This never sends or acknowledges anything.
                pending = _m_hosts.wake_ready_pending(inst, n, sorted(directory.glob('*.pending')), split=False)
                retired = path.with_name('turn-retired-' + os.urandom(8).hex() + '.json')
                _m_common.safe_target(retired)
                path.replace(retired)
                if not pending:
                    return None
                opaque, known = wake_turn_pending_summary(pending)
                value = {'version': 1, 'binding': wake_turn_binding(inst, n), 'token': os.urandom(16).hex(),
                         'sent': False, 'consumed': False, 'reminded': False, 'at': _m_hosts.WAKE_CLOCK.time(),
                         'items': _m_hosts.wake_delivery_items(inst, n, pending), 'paths': [p.name for p in pending],
                         'opaque_paths': opaque, 'queue_known': known}
                wake_turn_write(path, value)
            if not value or value['consumed']:
                return None
            if shown is not None:
                if shown['token'] != value['token']:
                    return None
                value = {**value, 'items': shown['items'], 'paths': shown['paths']}
            # A due command can arrive before a rate-limited drainer gathers a burst.
            # Capture only complete queue records owned by this exact binding.
            ready = _m_hosts.wake_ready_pending(inst, n, sorted(directory.glob('*.pending')), split=False)
            ready_names = {p.name for p in ready}
            blocked = [directory / name for name in value['paths'] if (directory / name).exists() and name not in ready_names]
            blocked_ids = set()
            for pending in blocked:
                try:
                    blocked_ids.update(i['id'] for i in _m_hosts.wake_pending_value(pending)[1])
                except (OSError, ValueError, KeyError, TypeError):
                    pass
            value = {**value, 'paths': [name for name in value['paths'] if name not in {p.name for p in blocked}],
                     'items': [i for i in value['items'] if i['id'] not in blocked_ids]}
            for pending in ([] if shown is not None else ready):
                try:
                    metadata, refs = _m_hosts.wake_pending_value(pending)
                    if metadata.get('binding') == value['binding']:
                        value['items'] = _m_hosts.wake_item_refs(value['items'] + refs)
                        value['paths'] = sorted(set(value['paths']) | {pending.name})
                except (OSError, ValueError, KeyError, TypeError):
                    pass  # unknown queue bytes remain deliverable, never acknowledged
            if not consume:
                return value
            for ref in value['items']:
                if ref['kind'] in _m_hosts.WAKE_TERMINAL_KINDS:
                    _m_hosts.wake_accept_write(_m_hosts.wake_terminal_receipt(inst, ref['id']), 'delivered\n')
            _m_hosts.wake_ack_shown(inst, n, value['items'])
            for name in value['paths']:
                pending = directory / name
                _m_common.safe_target(pending)
                _m_common.atomic_write(pending.with_suffix('.sent'), 'consumed\n', private=True)
                pending.unlink(missing_ok=True)
            # The turn lock fences transport completion throughout publication.
            # A failed acknowledgement leaves the episode unconsumed so due can
            # retry bookkeeping without authorizing a duplicate transport.
            value['consumed'] = True
            wake_turn_write(path, value)
            return value
    except (OSError, ValueError, KeyError, TypeError):
        return None


def wake_turn_stuck(inst, n):
    if not wake_turn_enabled(n):
        return None
    try:
        value = wake_turn_read(_m_hosts.wake_directory(inst, n) / 'turn.json', wake_turn_binding(inst, n))
        if value and not value['consumed'] and _m_hosts.WAKE_CLOCK.time() - value['at'] >= 120:
            return ('coordinator wake stalled: ' + ('transport accepted' if value['sent'] else 'transport outcome unknown') +
                    ', unconsumed for 120 seconds; '
                    'start state UNKNOWN; inspect coordinator activity; automatic retry unavailable; '
                    'run brain due ' + n['id'] + ' from its bound coordinator; without native host identity, '
                    'bind the actual current runtime session with brain set ' + n['id'] + ' coordinator_sessions=<actual-session-id>')
    except (OSError, ValueError, KeyError, TypeError):
        return ('coordinator wake receipt unavailable; start state UNKNOWN; pending work retained; '
                'bound coordinator: run brain due ' + n['id'] + ' to reconstruct its display receipt')
    return None


def wake_turn_queue(inst, n):
    """Read-only native reason count. Never infer host activity from delivery state."""
    unknown = {'known': False, 'item_count': None, 'oldest_at': None, 'unconsumed_wake': False}
    target, _ = _m_hosts.wake_target(inst, n)
    directory = _m_hosts.wake_directory(inst, target)
    try:
        path = directory / 'turn.json'
        _m_common.safe_target(path)
        before = path.read_bytes() if path.exists() else None
        turn = wake_turn_read(path, wake_turn_binding(inst, target)) if wake_turn_enabled(target) else None
        identities, times = set(), []
        unconsumed = bool(turn and not turn['consumed'])
        if unconsumed:
            if turn.get('queue_known') is not True or not isinstance(turn.get('opaque_paths'), list):
                return unknown
            identities.update('item:' + i['id'] for i in turn['items'])
            identities.update('opaque:' + name for name in turn['opaque_paths'])
            times.append(turn['at'])
        pending = sorted(directory.glob('*.pending'))
        manifest = [(p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in pending]
        for p in pending:
            metadata, refs = _m_hosts.wake_pending_value(p)
            if metadata.get('binding') not in (None, wake_turn_binding(inst, target)):
                continue
            identities.update('item:' + i['id'] for i in refs)
            if not refs:
                identities.add('opaque:' + p.name)
            times.append(p.stat().st_mtime)
        after = path.read_bytes() if path.exists() else None
        if before != after or manifest != [(p.name, p.stat().st_mtime_ns, p.stat().st_size)
                                           for p in sorted(directory.glob('*.pending'))]:
            return unknown
        return {'known': True, 'item_count': len(identities),
                'oldest_at': min(times) if identities else None,
                'unconsumed_wake': bool(unconsumed and turn['sent'])}
    except (OSError, ValueError, KeyError, TypeError):
        return unknown
