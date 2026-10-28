"""Recorded coordinator backlog and active-run advisories; never query a host."""
import contextlib
import datetime
import fcntl
import hashlib
import json
import math
import os
import re
import stat

from . import common as _m_common

_m_hosts = _m_common.LazyModule(__package__, 'hosts')


COORDINATOR_OBSERVATION_MAX_AGE = 180
COORDINATOR_TRANSPORT_GRACE = 60  # drain start and delivery slack past one rate window


def coordinator_time(value):
    if not isinstance(value, str):
        raise ValueError('timestamp required')
    result = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('timezone required')
    return result.timestamp()


def coordinator_read(path):
    """No linked, oversized or nonregular snapshot inputs."""
    if any(p.is_symlink() for p in [path, *path.parents]):
        raise ValueError('linked coordinator evidence')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1024 * 1024:
        raise ValueError('unsafe coordinator evidence')
    return json.loads(path.read_text()), info


def coordinator_host_runs(inst, now):
    """An exporter must provide fresh, exhaustive per-thread queue observations."""
    try:
        data, _ = coordinator_read(inst.root / '.brain/coordinator-runs.json')
        observed = coordinator_time(data.get('observed_at'))
        if (type(data.get('version')) is not int or data['version'] != 1
                or not 0 <= now - observed <= COORDINATOR_OBSERVATION_MAX_AGE
                or not isinstance(data.get('runs'), list) or len(data['runs']) > 2000):
            return {}
        records = {}
        for rec in data['runs']:
            if (not isinstance(rec, dict) or any(not isinstance(rec.get(k), str) or not rec[k]
                    for k in ('node', 'thread', 'run', 'status')) or rec['node'] in records
                    or not isinstance(rec.get('queued_wakes'), list) or len(rec['queued_wakes']) > 10000):
                return {}
            started = coordinator_time(rec.get('started_at'))
            if started > observed:
                return {}
            queued = {}
            for wake in rec['queued_wakes']:
                if not isinstance(wake, dict) or not isinstance(wake.get('id'), str) or not wake['id']:
                    return {}
                at = coordinator_time(wake.get('queued_at'))
                if at > observed or wake['id'] in queued:
                    return {}
                queued[wake['id']] = at
            records[rec['node']] = {**rec, 'started': started, 'queued': queued}
        return records
    except (OSError, ValueError, TypeError, AttributeError, OverflowError):
        return {}  # unavailable evidence never establishes active host work


def coordinator_native_queue(inst, n, now):
    queued = {}
    directory = _m_hosts.wake_directory(inst, n)
    for path in sorted(directory.glob('*.pending')):
        try:
            if not re.fullmatch('[0-9a-f]{64}', path.stem):
                continue
            data, info = coordinator_read(path)
            refs = _m_hosts.wake_item_refs(data['items'])
            at = info.st_mtime
            if not math.isfinite(at) or at > now:
                continue
            # A coalesced file can represent several independent queued items.
            for identity in [i['id'] for i in refs] or [path.stem]:
                queued[identity] = min(at, queued.get(identity, at))
        except (OSError, ValueError, TypeError, KeyError):
            continue  # malformed queue bytes remain untouched for transport recovery
    return queued


def coordinator_transport(view, now, held=False):
    """Doorbells inside the delivery rate window are in transport, not queued at the coordinator.

    One drain delivers every pending doorbell together, so they are all in transport
    while the oldest is younger than the window; past it, none is waiting on the window.
    A delivered but unconsumed turn (held) waits on the coordinator itself.
    """
    if (view['count'] and not held
            and now - view['oldest_at'] <= _m_hosts.WAKE_INTERVAL + COORDINATOR_TRANSPORT_GRACE):
        return {**view, 'count': 0, 'oldest_at': None, 'episode': None, 'transport': view['count']}
    return {**view, 'transport': 0}


def coordinator_queue_view(inst, n, now, host_record=None):
    """Use the transport owner's bound turn projection when that hook is installed.

    The episode is the condition's start, so later arrivals never replace it.
    """
    if host_record and host_record.get('queued') is not None and host_record['thread'] == n['local'].get('coordinator'):
        queued = host_record['queued']
        at = min(queued.values()) if queued else None
        return {'count': len(queued), 'oldest_at': at, 'episode': [at], 'source': 'fresh_host_snapshot',
                'transport': 0}  # the host's own queue wins over brain's transport
    reader = getattr(_m_hosts, 'wake_turn_queue', None)
    if reader:
        unknown = {'count': None, 'oldest_at': None, 'episode': None, 'source': 'coalesced_queue_unknown',
                   'transport': None}
        try:
            for _ in range(3):  # a concurrent enqueue or delivery makes one read unknown; re-read before giving up
                value = reader(inst, n)
                if value.get('known') is True:
                    break
            count, at = value.get('item_count'), value.get('oldest_at')
            if value.get('known') is not True or type(count) is not int or count < 0:
                return unknown
            if count and (type(at) not in (int, float) or not math.isfinite(at) or at > now):
                return unknown
            if not count and at is not None:
                return unknown
            return coordinator_transport({'count': count, 'oldest_at': at, 'episode': [at],
                                          'source': 'coalesced_native_queue'}, now, value.get('unconsumed_wake') is True)
        except (OSError, ValueError, TypeError, AttributeError):
            return unknown
    queued = coordinator_native_queue(inst, n, now)
    at = min(queued.values()) if queued else None
    return coordinator_transport({'count': len(queued), 'oldest_at': at, 'episode': [at],
                                  'source': 'native_pending_only'}, now)


def coordinator_queue_severity(count, age):
    """Re-fire only when materially worse: each doubling past four items, and once at 30 minutes."""
    return [max(0, count.bit_length() - 3), age >= 1800]


def coordinator_condition_valid(record):
    severity = record.get('severity') if isinstance(record, dict) else None
    return (isinstance(record, dict) and 'severity' in record and isinstance(record.get('thread'), str)
            and type(record.get('start')) in (int, float) and math.isfinite(record['start'])
            and (severity is None or isinstance(severity, list) and len(severity) == 2
                 and type(severity[0]) is int and severity[0] >= 0 and type(severity[1]) is bool))


@contextlib.contextmanager
def coordinator_conditions(inst):
    """Each continuing backlog's start and highest notified severity, under one lock.

    Yields (recorded, kept): the caller fills kept, which replaces the record when it
    differs. Unavailable state yields None, and episodes fall back to the observation.
    """
    path = inst.root / '.brain/coordinator-queue.json'
    try:
        _m_common.safe_target(path)
        fd = os.open(path.with_suffix('.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except (OSError, ValueError):
        yield None, {}
        return
    with os.fdopen(fd, 'a+') as stream:
        recorded = {}
        try:
            if os.fstat(stream.fileno()).st_nlink != 1:
                raise ValueError('linked coordinator queue lock')
            fcntl.flock(stream, fcntl.LOCK_EX)
            if path.exists():
                data, _ = coordinator_read(path)
                if data.get('version') == 1 and isinstance(data.get('conditions'), dict):
                    recorded = {k: v for k, v in data['conditions'].items() if coordinator_condition_valid(v)}
        except (OSError, ValueError, TypeError, AttributeError):
            yield None, {}
            return
        kept = {}
        yield recorded, kept
        if kept != recorded and (kept or path.exists()):
            try:
                _m_common.atomic_write(path, json.dumps({'version': 1, 'conditions': kept}, sort_keys=True) + '\n',
                                       private=True)
            except OSError:
                pass  # the next scan re-derives the condition from its observation


def coordinator_advisories(inst):
    with coordinator_conditions(inst) as (recorded, kept):
        return coordinator_advisory_items(inst, recorded or {}, kept)


def coordinator_advisory_items(inst, recorded, kept):
    now = inst.now_dt().timestamp()
    host = coordinator_host_runs(inst, now)
    from . import host_observations
    host.update(host_observations.host_coordinator_runs(inst))
    items = []
    for n in inst.nodes():
        if n['archived'] or not n['local'].get('coordinator'):
            continue
        thread = n['local']['coordinator']
        rec = host.get(n['id'])
        rec = rec if rec and rec['thread'] == thread else None
        queue = coordinator_queue_view(inst, n, now, rec)
        condition = recorded.get(n['id'])
        condition = condition if condition and condition['thread'] == thread else None
        if queue['count'] is None and condition:
            kept[n['id']] = condition  # unknown evidence neither ends nor asserts the backlog
        if not queue['count']:
            continue
        # A backlog continues through delivery and new arrivals; its start never moves.
        condition = condition or {'thread': thread, 'start': queue['oldest_at'], 'severity': None}
        age = now - min(condition['start'], queue['oldest_at'])
        def add(kind, identity, text):
            episode = hashlib.sha256(json.dumps([n['id'], thread, kind, identity], sort_keys=True).encode()).hexdigest()
            items.append({'node': 'root', 'kind': kind, 'subject_node': n['id'],
                          'episode': episode, 'text': text})
        if condition['severity'] is not None or queue['count'] > 3 or age > 600:
            # Once raised, the advisory stays until the backlog clears, at its highest severity.
            level, aged = coordinator_queue_severity(queue['count'], age)
            previous = condition['severity'] or [0, False]
            condition = {**condition, 'severity': [max(level, previous[0]), aged or previous[1]]}
            add('coordinator-queue', [condition['start'], condition['severity']],
                f"{n['id']}: coordinator has {queue['count']} queued item(s); oldest waiting "
                f"{int(age / 60)} min. Drain all queued work in one turn.")
        kept[n['id']] = condition
        try:
            busy_minutes = float(n['fm'].get('coordinator_stall_minutes', 20))
            if not math.isfinite(busy_minutes) or busy_minutes < 1:
                busy_minutes = 20
        except (ValueError, TypeError):
            busy_minutes = 20
        if rec and rec['status'] == 'active' and now - rec['started'] >= busy_minutes * 60:
            add('coordinator-busy', [rec['run'], rec['started']],
                f"{n['id']}: coordinator active for {int((now - rec['started']) / 60)} min "
                'with wakes queued behind this run; inspect its current work.')
    return items
