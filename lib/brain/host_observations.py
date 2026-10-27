"""Bound native host exports. Failed/partial reads retain the last complete snapshot."""
import fcntl
import os
import json
import re
import time

from . import common as _m_common
from . import hosts as _m_hosts
from . import host_instances as _m_host_instances
from . import jobs as _m_jobs
from . import nodes as _m_nodes
from . import worker_failures as _m_failures


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


def host_failure_jobs(inst):
    """Project/branch ownership for workers launched outside the binding path."""
    result = []
    for n in inst.nodes():
        project = n['local'].get('home_project') or n['local'].get('host_project')
        if n['archived'] or not project:
            continue
        for row in _m_jobs.read_jobs(n) or []:
            if not row['Ended']:
                result.append({'node': n['id'], 'job_key': _m_jobs.job_key(row),
                               'project': project, 'branch': row['Job']})
    if len(result) > 128:
        raise ValueError('failure export exceeds job bound')
    return sorted(result, key=lambda r: (r['node'], r['job_key']))


def host_failure_detail(call, identity):
    detail = call('t3_thread_read', {'threadId': identity, 'limit': 1, 'runLimit': 1})
    thread = detail.get('thread')
    if not isinstance(thread, dict) or thread.get('threadId') != identity:
        raise ValueError('invalid failed thread detail')
    return detail


def host_failure_category(call, detail, run):
    """Native error items expose failure.message as text; never classify prose.

    The host read schema exposes no run error field. Read a bounded activity tail
    using itemCount; long/incomplete/unknown tails safely become work notices.
    """
    thread = detail['thread']
    count = thread.get('itemCount')
    if type(count) is not int or count < 0:
        return 'work error'
    tail = call('t3_thread_read', {'threadId': thread['threadId'], 'view': 'activity',
                **({'afterPosition': count - 101} if count > 100 else {}), 'limit': 100, 'runLimit': 1,
                'maxCharsPerItem': 4096})
    current = tail.get('thread')
    if (not isinstance(current, dict) or current.get('threadId') != thread['threadId']
            or current.get('latestRunId') != run or current.get('status') != 'failed'):
        raise ValueError('failed activity changed during export; previous snapshot retained')
    if tail.get('hasMore') is not False:
        return 'work error'
    errors = [item for item in tail.get('items', []) if isinstance(item, dict)
              and item.get('type') == 'error' and item.get('sourceThreadId') == thread['threadId']
              and item.get('runId') == run and item.get('visibility') == 'local']
    if not errors or any(item.get('textTruncated') is not False for item in errors):
        return 'work error'
    categories = [_m_failures.failure_category(item.get('text')) for item in errors]
    return categories[-1] if all(c != 'work error' for c in categories) else 'work error'


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
    jobs = host_failure_jobs(inst)
    guards = [_m_host_instances.routed_host_rpc(inst, nid, lambda *_: None)
              for nid in {b['node'] for b in bindings + jobs}]
    observed, threads = inst.now_dt().isoformat(), {}
    deadline = HOST_EXPORT_CLOCK.monotonic() + 60
    def call(name, arguments):
        if HOST_EXPORT_CLOCK.monotonic() >= deadline:
            raise ValueError('host export budget exceeded; previous snapshot retained')
        for guard in guards:
            guard(name, arguments)
        value = rpc(name, arguments)
        if not isinstance(value, dict):
            raise ValueError('invalid native host response')
        return value
    for project in sorted({b['project'] for b in bindings + jobs}):
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
    # Branch is available on thread detail, not the list schema. Inspect only
    # failed threads in projects with open jobs, with the same bounded deadline.
    details, discovered = {}, []
    for (project, identity), row in threads.items():
        candidates = [j for j in jobs if j['project'] == project]
        if row.get('status') != 'failed' or not candidates:
            continue
        if len(details) >= 128:
            raise ValueError('failed thread export exceeds bound')
        detail = host_failure_detail(call, identity)
        details[(project, identity)] = detail
        thread = detail['thread']
        matches = [j for j in candidates if j['branch'] == thread.get('branch')]
        if len(matches) == 1 and not any(b['project'] == project and b['thread'] == identity for b in bindings):
            if thread.get('projectId') != project or thread.get('latestRunId') != row.get('latestRunId'):
                raise ValueError('failed discovery changed during export')
            discovered.append({**matches[0], 'thread': identity, 'role': 'worker', 'discovered': True})
    for binding in discovered:
        binding['ambiguous'] = sum((d['node'], d['job_key']) == (binding['node'], binding['job_key'])
                                   for d in discovered) > 1
    records = []
    for binding in bindings + discovered:
        row = threads.get((binding['project'], binding['thread']))
        if row is None or row.get('status') not in HOST_STATUSES or not (
                row.get('latestRunId') is None or isinstance(row.get('latestRunId'), str)):
            raise ValueError('bound host thread unknown; previous snapshot retained')
        pending = call('t3_pending_request_list', {'threadId': binding['thread']}).get('requestIds')
        if not isinstance(pending, list) or len(pending) > 200 or any(not isinstance(p, str) or not p for p in pending):
            raise ValueError('invalid pending request export')
        active_run, started_at = None, None
        if binding['role'] == 'coordinator':
            # These are documented native fields. updatedAt/requestedAt measure
            # different events and cannot stand in for an actual running start.
            detail = call('t3_thread_read', {'threadId': binding['thread'], 'limit': 1,
                                           'maxCharsPerItem': 1, 'runLimit': 50})
            thread, runs = detail.get('thread'), detail.get('recentRuns')
            if not isinstance(thread, dict) or thread.get('threadId') != binding['thread'] or not isinstance(runs, list):
                raise ValueError('invalid native coordinator run evidence')
            active = thread.get('activeRunId')
            matching = [r for r in runs if isinstance(r, dict) and r.get('runId') == active]
            if (isinstance(active, str) and active and thread.get('status') == row['status'] == 'running'
                    and thread.get('latestRunId') == row.get('latestRunId') == active and len(matching) == 1
                    and matching[0].get('status') == 'running'):
                at = matching[0].get('startedAt')
                parsed = inst.parse_time(at) if isinstance(at, str) else None
                # Native timestamps include their zone. Never infer one for host evidence.
                if parsed and re.search(r'(Z|[+-][0-9]{2}:[0-9]{2})$', at) and parsed <= inst.parse_time(observed):
                    active_run, started_at = active, at
        category = None
        if binding['role'] == 'worker' and row['status'] == 'failed' and row.get('latestRunId'):
            detail = details.get((binding['project'], binding['thread'])) or host_failure_detail(call, binding['thread'])
            if (detail['thread'].get('latestRunId') != row['latestRunId']
                    or detail['thread'].get('status') != 'failed'):
                raise ValueError('failed run changed during export; previous snapshot retained')
            category = host_failure_category(call, detail, row['latestRunId'])
        records.append({**binding, 'status': row['status'], 'run': row.get('latestRunId'), 'category': category,
                        'active_run': active_run, 'started_at': started_at,
                        'pending': sorted({_m_hosts.wake_binding(json.dumps([binding['thread'], p])) for p in pending})})
    # Rebinding while reads were in flight cannot publish stale ownership as fresh.
    fresh = inst.__class__(inst.root)
    if host_bindings(fresh) != bindings or host_failure_jobs(fresh) != jobs:
        raise ValueError('host bindings changed during export; previous snapshot retained')
    snapshot = {'version': 1, 'observed_at': observed, 'bindings': _m_jobs.digest(bindings),
                'jobs': _m_jobs.digest(jobs), 'records': records}
    path = inst.root / '.brain/host-observations.json'
    _m_common.safe_target(path)
    _m_common.atomic_write(path, json.dumps(snapshot, sort_keys=True) + '\n', private=True)
    return snapshot


def host_snapshot(inst):
    path = inst.root / '.brain/host-observations.json'
    _m_common.safe_target(path)
    if not path.exists() or path.stat().st_size > 1024 * 1024:
        return None
    value = json.loads(path.read_text())
    at = inst.parse_time(value.get('observed_at', '')) if isinstance(value, dict) else None
    if (not at or value.get('version') != 1 or not 0 <= (inst.now_dt() - at).total_seconds() <= HOST_SNAPSHOT_SECONDS
            or value.get('bindings') != _m_jobs.digest(host_bindings(inst))
            or value.get('jobs') != _m_jobs.digest(host_failure_jobs(inst)) or not isinstance(value.get('records'), list)):
        return None
    bindings = host_bindings(inst)
    identities = {(b['node'], b['thread'], b['role']): b for b in bindings}
    if len(value['records']) < len(bindings) or len(value['records']) > len(bindings) + 128:
        raise ValueError('incomplete host observation records')
    observed = set()
    for record in value['records']:
        if (not isinstance(record, dict) or record.get('status') not in HOST_STATUSES or
                record.get('category') not in (None,) + _m_failures.FAILURE_CATEGORIES or
                not isinstance(record.get('pending'), list) or
                any(not isinstance(key, str) or not re.fullmatch('[0-9a-f]{64}', key) for key in record['pending'])):
            raise ValueError('invalid host observation record')
        key = (record.get('node'), record.get('thread'), record.get('role'))
        binding = identities.get(key)
        if record.get('discovered') is True:
            candidates = [j for j in host_failure_jobs(inst) if all(record.get(k) == v for k, v in j.items())]
            if (binding or len(candidates) != 1 or record.get('role') != 'worker'
                    or record.get('status') != 'failed' or not isinstance(record.get('thread'), str)
                    or not record['thread']):
                raise ValueError('invalid discovered failure ownership')
            ambiguous = sum(r.get('discovered') is True and (r.get('node'), r.get('job_key')) ==
                            (record['node'], record['job_key']) for r in value['records']) > 1
            if record.get('ambiguous') is not ambiguous:
                raise ValueError('invalid discovered failure ambiguity')
            binding = {**candidates[0], 'thread': record['thread'], 'role': 'worker', 'discovered': True,
                       'ambiguous': ambiguous}
        if not binding or key in observed or any(record.get(k) != v for k, v in binding.items()):
            raise ValueError('host observation ownership mismatch')
        observed.add(key)
    if not set(identities).issubset(observed):
        raise ValueError('incomplete host observation records')
    return value


def host_export_runs(inst):
    value = host_snapshot(inst)
    return [{**r, 'source': 'native-host', 'error': r.get('category') or 'host run failed'} for r in value['records']
            if r['role'] == 'worker' and r['status'] == 'failed' and r['run']] if value else []


def host_coordinator_runs(inst):
    """Fresh bound native starts; queue age/count belongs to the native wake owner."""
    try:
        value = host_snapshot(inst)
        result = {}
        for r in value['records'] if value else []:
            at = r.get('started_at')
            started = inst.parse_time(at) if isinstance(at, str) else None
            if (r['role'] == 'coordinator' and r['status'] == 'running' and
                    isinstance(r.get('active_run'), str) and r['active_run'] == r.get('run')
                    and started and re.search(r'(Z|[+-][0-9]{2}:[0-9]{2})$', at)
                    and started <= inst.parse_time(value['observed_at'])):
                result[r['node']] = {**r, 'run': r['active_run'], 'status': 'active',
                                     'started': started.timestamp(), 'queued': None}
        return result
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return {}


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
