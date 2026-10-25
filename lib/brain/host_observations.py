"""Bound native host exports. Failed/partial reads retain the last complete snapshot."""
import fcntl
import os
import json
import re
import time

from . import common as _m_common
from . import hosts as _m_hosts
from . import jobs as _m_jobs
from . import nodes as _m_nodes


HOST_SNAPSHOT_SECONDS = 180
HOST_EXPORT_CLOCK = time
HOST_STATUSES = ('idle', 'preparing', 'queued', 'starting', 'running', 'waiting',
                 'completed', 'interrupted', 'failed', 'cancelled', 'rolled_back')


def host_bindings(inst):
    bindings = []
    for n in inst.nodes():
        if n['archived']:
            continue
        local = n['local']
        if local.get('coordinator'):
            bindings.append({'node': n['id'], 'thread': local['coordinator'], 'role': 'coordinator',
                             'project': local.get('host_project')})
        workers = local.get('workers', {})
        if not isinstance(workers, dict):
            raise ValueError('invalid bound workers')
        for row in _m_jobs.read_jobs(n) or []:
            key = _m_jobs.job_key(row)
            if not row['Ended'] and workers.get(key):
                bindings.append({'node': n['id'], 'thread': workers[key], 'role': 'worker', 'job_key': key,
                                 'project': local.get('home_project') or local.get('host_project')})
    if len(bindings) > 128 or any(not isinstance(b['thread'], str) or not b['thread'] or
                                not isinstance(b['project'], str) or not b['project'] for b in bindings):
        raise ValueError('host export needs explicit project/thread bindings within its bound')
    return sorted(bindings, key=lambda b: (b['node'], b['role'], b['thread']))


def host_native_call(name, arguments):
    # Shared bridge is supplied by the lifecycle module; never start a second client.
    from . import host_rpc
    return host_rpc.call(name, arguments)


def host_export(inst, rpc=None):
    path = inst.root / '.brain/host-export.lock'
    _m_common.safe_target(path)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a+') as lock:
        if os.fstat(lock.fileno()).st_nlink != 1:
            raise ValueError('unsafe host export lock')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('host export already active')
        return _host_export(inst, rpc)


def _host_export(inst, rpc=None):
    rpc = rpc or host_native_call
    if inst.cfg.get('host') != 't3' or _m_common.outward_links(inst.root):
        raise ValueError('host export requires a safe t3 instance')
    bindings = host_bindings(inst)
    observed, threads = inst.now_dt().isoformat(), {}
    deadline = HOST_EXPORT_CLOCK.monotonic() + 60
    def call(name, arguments):
        if HOST_EXPORT_CLOCK.monotonic() >= deadline:
            raise ValueError('host export budget exceeded; previous snapshot retained')
        value = rpc(name, arguments)
        if not isinstance(value, dict):
            raise ValueError('invalid native host response')
        return value
    for project in sorted({b['project'] for b in bindings}):
        cursor, cursors = None, set()
        for _ in range(10):
            value = call('t3_thread_list', {'projectId': project, 'includeSubagents': True,
                                          'limit': 100, **({'cursor': cursor} if cursor is not None else {})})
            rows = value.get('threads')
            if not isinstance(rows, list):
                raise ValueError('invalid thread export')
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get('threadId'), str):
                    raise ValueError('invalid thread identity')
                threads[(project, row['threadId'])] = row
            cursor = value.get('nextCursor')
            if cursor is None:
                break
            if type(cursor) is not int or cursor < 0 or cursor in cursors:
                raise ValueError('invalid thread pagination')
            cursors.add(cursor)
        else:
            raise ValueError('thread export exceeds page bound')
    records = []
    for binding in bindings:
        row = threads.get((binding['project'], binding['thread']))
        if row is None or row.get('status') not in HOST_STATUSES or not (
                row.get('latestRunId') is None or isinstance(row.get('latestRunId'), str)):
            raise ValueError('bound host thread unknown; previous snapshot retained')
        pending = call('t3_pending_request_list', {'threadId': binding['thread']}).get('requestIds')
        if not isinstance(pending, list) or len(pending) > 200 or any(not isinstance(p, str) or not p for p in pending):
            raise ValueError('invalid pending request export')
        records.append({**binding, 'status': row['status'], 'run': row.get('latestRunId'),
                        'pending': sorted({_m_hosts.wake_binding(json.dumps([binding['thread'], p])) for p in pending})})
    # Rebinding while reads were in flight cannot publish stale ownership as fresh.
    if host_bindings(inst.__class__(inst.root)) != bindings:
        raise ValueError('host bindings changed during export; previous snapshot retained')
    snapshot = {'version': 1, 'observed_at': observed, 'bindings': _m_jobs.digest(bindings), 'records': records}
    path = inst.root / '.brain/host-observations.json'
    _m_common.safe_target(path)
    _m_common.atomic_write(path, json.dumps(snapshot, sort_keys=True) + '\n')
    return snapshot


def host_snapshot(inst):
    path = inst.root / '.brain/host-observations.json'
    _m_common.safe_target(path)
    if not path.exists() or path.stat().st_size > 1024 * 1024:
        return None
    value = json.loads(path.read_text())
    at = inst.parse_time(value.get('observed_at', '')) if isinstance(value, dict) else None
    if (not at or value.get('version') != 1 or not 0 <= (inst.now_dt() - at).total_seconds() <= HOST_SNAPSHOT_SECONDS
            or value.get('bindings') != _m_jobs.digest(host_bindings(inst)) or not isinstance(value.get('records'), list)):
        return None
    bindings = host_bindings(inst)
    identities = {(b['node'], b['thread'], b['role']): b for b in bindings}
    if len(value['records']) != len(bindings):
        raise ValueError('incomplete host observation records')
    observed = set()
    for record in value['records']:
        if (not isinstance(record, dict) or record.get('status') not in HOST_STATUSES or
                not isinstance(record.get('pending'), list) or
                any(not isinstance(key, str) or not re.fullmatch('[0-9a-f]{64}', key) for key in record['pending'])):
            raise ValueError('invalid host observation record')
        key = (record.get('node'), record.get('thread'), record.get('role'))
        binding = identities.get(key)
        if not binding or key in observed or any(record.get(k) != v for k, v in binding.items()):
            raise ValueError('host observation ownership mismatch')
        observed.add(key)
    return value


def host_export_runs(inst):
    value = host_snapshot(inst)
    return [{**r, 'error': 'host run failed'} for r in value['records']
            if r['role'] == 'worker' and r['status'] == 'failed' and r['run']] if value else []


def host_observation_items(inst, nodes=None):
    value = host_snapshot(inst)
    allowed = {n['id'] for n in (nodes if nodes is not None else inst.nodes())}
    return [{'id': _m_hosts.wake_binding(json.dumps(['host-pending', r['node'], r['thread'], r['pending']])),
             'node': r['node'], 'kind': 'question',
             'text': str(len(r['pending'])) + ' pending host question(s); inspect the bound ' + r['role'] + ' thread'}
            for r in value['records'] if r['node'] in allowed and r['pending']] if value else []


def host_export_if_enabled(inst):
    if inst.local_of('root').get('host_export') != 'on':
        return
    try:
        path = inst.root / '.brain/host-observations.json'
        if path.exists() and 0 <= time.time() - path.stat().st_mtime < 60:
            return
        host_export(inst)
    except (Exception, SystemExit):
        print('host export unavailable; previous snapshot retained, host activity unknown')


def cmd_host_export(a):
    try:
        result = host_export(_m_nodes.load())
        print('host export: ' + str(len(result['records'])) + ' bound thread(s) observed')
    except (Exception, SystemExit):
        _m_common.die('host export unavailable or incomplete; previous snapshot retained')
