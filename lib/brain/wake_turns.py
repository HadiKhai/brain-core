"""Optional consumption-aware wake receipts, independent of display history."""
import contextlib
import fcntl
import json
import math
import os
import re

from . import common as _m_common
from . import hosts as _m_hosts


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
    target, command = _m_hosts.wake_target(inst, n)
    return _m_hosts.wake_binding(str(target['local'].get('coordinator', '')) + '\0' + command)


def wake_turn_enabled(n):
    return n['local'].get('wake_coalesce') == 'on'


def wake_turn_read(path, binding):
    if not path.exists():
        return None
    value = json.loads(path.read_text())
    if (not isinstance(value, dict) or value.get('version') != 1 or
            not isinstance(value.get('binding'), str) or not isinstance(value.get('token'), str) or
            type(value.get('at')) not in (int, float) or not math.isfinite(value['at']) or
            type(value.get('sent')) is not bool or type(value.get('consumed')) is not bool or
            type(value.get('reminded')) is not bool or not isinstance(value.get('paths'), list) or
            any(not isinstance(p, str) or not re.fullmatch(r'[0-9a-f]{64}\.pending', p) for p in value['paths']) or
            not isinstance(value.get('items'), list)):
        raise ValueError('invalid wake turn')
    refs = _m_hosts.wake_item_refs(value['items'])
    if any(not isinstance(i['id'], str) or not re.fullmatch(r'[0-9a-f]{64}', i['id'])
           or not isinstance(i['kind'], str) for i in refs):
        raise ValueError('invalid wake turn identities')
    return value if value['binding'] == binding else None


def wake_turn_write(path, value):
    _m_common.atomic_write(path, json.dumps(value, sort_keys=True) + '\n')


def wake_turn_prepare(inst, n, pending, refs):
    """Return (action, token). A damaged/unwritable receipt never suppresses work."""
    if not wake_turn_enabled(n):
        return 'legacy', None
    directory = _m_hosts.wake_directory(inst, n)
    try:
        with wake_turn_lock(directory) as path:
            binding = wake_turn_binding(inst, n)
            value = wake_turn_read(path, binding)
            if value and value['sent'] and not value['consumed']:
                value['items'] = _m_hosts.wake_item_refs(value['items'] + refs)
                value['paths'] = sorted(set(value['paths']) | {p.name for p in pending})
                reminder = not value['reminded'] and _m_hosts.WAKE_CLOCK.time() - value['at'] >= 1800
                if reminder:
                    value['reminded'] = True  # at most one reminder attempt per consumption episode
                wake_turn_write(path, value)
                return ('reminder' if reminder else 'hold'), value['token']
            if not pending:
                return 'hold', None
            token = os.urandom(16).hex()
            value = {'version': 1, 'binding': binding, 'token': token, 'sent': False,
                     'consumed': False, 'reminded': False, 'at': _m_hosts.WAKE_CLOCK.time(),
                     'items': refs, 'paths': [p.name for p in pending]}
            wake_turn_write(path, value)
            return 'send', token
    except (OSError, ValueError, KeyError, TypeError):
        return 'legacy', None


def wake_turn_finish(inst, n, token, sent):
    if token is None:
        return
    try:
        with wake_turn_lock(_m_hosts.wake_directory(inst, n)) as path:
            value = wake_turn_read(path, wake_turn_binding(inst, n))
            if value and value['token'] == token:
                if not sent or value['consumed']:
                    path.unlink(missing_ok=True)
                else:
                    value['sent'] = True
                    wake_turn_write(path, value)
    except (OSError, ValueError, KeyError, TypeError):
        pass  # successful transport is never repeated to repair bookkeeping


def wake_turn_consume(inst, n, consume=True, shown=None):
    """Consume only this binding's captured reasons; later arrivals stay queued."""
    if not wake_turn_enabled(n):
        return None
    directory = _m_hosts.wake_directory(inst, n)
    try:
        with wake_turn_lock(directory) as path:
            value = wake_turn_read(path, wake_turn_binding(inst, n))
            if not value or value['consumed']:
                return None
            if shown is not None:
                if shown['token'] != value['token']:
                    return None
                value = {**value, 'items': shown['items'], 'paths': shown['paths']}
            # A due command can arrive before a rate-limited drainer gathers a burst.
            # Capture only complete queue records owned by this exact binding.
            for pending in ([] if shown is not None else directory.glob('*.pending')):
                try:
                    metadata, refs = _m_hosts.wake_pending_value(pending)
                    if metadata.get('binding') == value['binding']:
                        value['items'] = _m_hosts.wake_item_refs(value['items'] + refs)
                        value['paths'] = sorted(set(value['paths']) | {pending.name})
                except (OSError, ValueError, KeyError, TypeError):
                    pass  # unknown queue bytes remain deliverable, never acknowledged
            if not consume:
                return value
            value['consumed'] = True
            wake_turn_write(path, value)  # fence transport completion before acknowledging its queue
            for ref in value['items']:
                if ref['kind'] in _m_hosts.WAKE_TERMINAL_KINDS:
                    _m_hosts.wake_accept_write(_m_hosts.wake_terminal_receipt(inst, ref['id']), 'delivered\n')
            _m_hosts.wake_ack_shown(inst, n, value['items'])
            for name in value['paths']:
                pending = directory / name
                _m_common.safe_target(pending)
                _m_common.atomic_write(pending.with_suffix('.sent'), 'consumed\n')
                pending.unlink(missing_ok=True)
            return value
    except (OSError, ValueError, KeyError, TypeError):
        return None


def wake_turn_stuck(inst, n):
    if not wake_turn_enabled(n):
        return None
    try:
        value = wake_turn_read(_m_hosts.wake_directory(inst, n) / 'turn.json', wake_turn_binding(inst, n))
        if value and value['sent'] and not value['consumed'] and _m_hosts.WAKE_CLOCK.time() - value['at'] >= 1800:
            return 'coordinator wake unconsumed for 30 minutes; inspect coordinator activity'
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None
