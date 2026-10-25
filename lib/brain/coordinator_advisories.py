"""Recorded coordinator backlog and active-run advisories; never query a host."""
import datetime
import hashlib
import json
import math
import re
import stat

from . import hosts as _m_hosts


COORDINATOR_OBSERVATION_MAX_AGE = 180


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


def coordinator_advisories(inst):
    now = inst.now_dt().timestamp()
    host = coordinator_host_runs(inst, now)
    items = []
    for n in inst.nodes():
        if n['archived'] or not n['local'].get('coordinator'):
            continue
        thread = n['local']['coordinator']
        rec = host.get(n['id'])
        rec = rec if rec and rec['thread'] == thread else None
        queued = rec['queued'] if rec else coordinator_native_queue(inst, n, now)
        if not queued:
            continue
        oldest = min(queued, key=lambda key: (queued[key], key))
        def add(kind, identity, text):
            episode = hashlib.sha256(json.dumps([n['id'], thread, kind, identity], sort_keys=True).encode()).hexdigest()
            items.append({'node': 'root', 'kind': kind, 'subject_node': n['id'],
                          'episode': episode, 'text': text})
        if len(queued) > 3 or now - queued[oldest] > 600:
            add('coordinator-queue', [oldest, queued[oldest]],
                f"{n['id']}: coordinator has {len(queued)} queued item(s); oldest waiting "
                f"{int((now - queued[oldest]) / 60)} min. Drain all queued work in one turn.")
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
