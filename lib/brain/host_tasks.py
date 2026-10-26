"""Registered delegated tasks have a file-backed completion doorbell fallback."""
import contextlib
import fcntl
import json
import os
import re
import time

from . import briefs as _m_briefs
from . import common as _m_common
from . import host_observations as _m_host_observations
from . import hosts as _m_hosts
from . import host_instances as _m_host_instances
from . import instance as _m_instance
from . import nodes as _m_nodes
from . import transitions as _m_transitions


HOST_TASK_TERMINAL = ('completed', 'failed', 'cancelled', 'interrupted')


def host_task_read(inst):
    path = inst.root / '.brain/host-tasks.json'
    _m_common.safe_target(path)
    if not path.exists():
        return {'version': 1, 'tasks': {}}
    if path.stat().st_size > 1024 * 1024:
        raise ValueError('delegated task registry exceeds bound')
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('tasks'), dict):
        raise ValueError('invalid delegated task registry')
    for key, row in data['tasks'].items():
        if (not isinstance(row, dict) or any(not isinstance(row.get(k), str) or not row[k]
                for k in ('node', 'parent_thread', 'clientRequestId')) or
                row.get('taskId') is not None and not isinstance(row['taskId'], str)):
            raise ValueError('invalid delegated task registration')
    return data


@contextlib.contextmanager
def host_task_store(inst):
    path = inst.root / '.brain/host-tasks.lock'
    _m_common.safe_target(path)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a+') as lock:
        if os.fstat(lock.fileno()).st_nlink != 1:
            raise ValueError('unsafe delegated task lock')
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = host_task_read(inst)
        before = json.dumps(data, sort_keys=True)
        yield data
        after = json.dumps(data, sort_keys=True)
        if len(after.encode()) > 1024 * 1024:
            raise ValueError('delegated task registry exceeds bound')
        if after != before:
            _m_common.atomic_write(inst.root / '.brain/host-tasks.json', after + '\n')


def host_delegate(inst, n, task, target, key, rpc=None):
    """Explicit delegation; persist a stable host idempotency key before the call."""
    rpc = rpc or _m_host_observations.host_native_call
    rpc = _m_host_instances.routed_host_rpc(inst, n['id'], rpc)
    parent = n['local'].get('coordinator')
    if (not isinstance(parent, str) or not parent or not isinstance(key, str) or not key or
            not isinstance(task, str) or not task.strip() or not isinstance(target, dict) or
            any(not isinstance(target.get(k), str) or not target[k] for k in ('providerInstanceId', 'model'))):
        raise ValueError('delegation needs bound parent, stable key, task and explicit target')
    context = rpc('t3_thread_list', {'limit': 1})
    if not isinstance(context, dict) or context.get('currentThreadId') != parent:
        raise ValueError('delegation must run from the bound parent host context')
    identity = _m_hosts.wake_binding(json.dumps([n['id'], parent, key]))
    payload = {'task': _m_briefs.automated_brief(task), 'target': target,
               'mode': 'async', 'clientRequestId': 'brain-delegate-' + identity}
    digest = _m_hosts.wake_binding(json.dumps(payload, sort_keys=True))
    with host_task_store(inst) as data:
        row = data['tasks'].get(identity)
        if row and row.get('input_digest') != digest:
            raise ValueError('delegation key already names different work')
        if row and row.get('taskId'):
            return dict(row)
        data['tasks'][identity] = {'node': n['id'], 'parent_thread': parent,
                                  'clientRequestId': payload['clientRequestId'], 'input_digest': digest,
                                  'created_at': inst.now(), 'taskId': None}
    if inst.__class__(inst.root).node(n['id'])['local'].get('coordinator') != parent:
        raise ValueError('parent binding changed before delegation')
    result = rpc('delegate_task', payload)  # no automatic mutation retry, no instance writer lock
    if not isinstance(result, dict) or not isinstance(result.get('taskId'), str) or not result['taskId']:
        raise ValueError('delegation outcome unknown; retry explicitly with the same stable key')
    with host_task_store(inst) as data:
        row = data['tasks'][identity]
        row.update(taskId=result['taskId'], childThreadId=result.get('childThreadId'))
        return dict(row)


def host_task_refresh(inst, rpc=None):
    rpc = rpc or _m_host_observations.host_native_call
    for node in inst.nodes():
        if node['local'].get('host_instance') is not None:
            _m_host_instances.require_local_host(_m_instance.Instance(inst.root), node['id'])
    context = rpc('t3_thread_list', {'limit': 1})
    parent = context.get('currentThreadId') if isinstance(context, dict) else None
    if not isinstance(parent, str) or not parent:
        raise ValueError('delegated export requires an identified parent host context')
    registry = host_task_read(inst)
    statuses = []
    deadline = time.monotonic() + 60
    for key, row in registry['tasks'].items():
        n = inst.node(row['node'])
        _m_host_instances.require_local_host(_m_instance.Instance(inst.root), n['id'])
        if (not row.get('taskId') or row['parent_thread'] != parent or
                n['local'].get('coordinator') != row['parent_thread']):
            continue
        if row.get('completion'):
            statuses.append(row['completion'])
            continue
        if time.monotonic() >= deadline or len(statuses) >= 128:
            raise ValueError('delegated export exceeds bound; registrations retained')
        value = rpc('task_status', {'taskId': row['taskId']})
        if (not isinstance(value, dict) or value.get('taskId') != row['taskId'] or
                type(value.get('hasPendingChildRuns')) is not bool or
                value.get('workState') not in ('working', 'waiting_for_children', 'result_available')):
            raise ValueError('incomplete delegated task status; previous snapshot retained')
        status = value.get('latestTerminalStatus') or value.get('status')
        run = value.get('latestTerminalRunId') or value.get('childRunId')
        if status in HOST_TASK_TERMINAL and value['workState'] == 'result_available' and not value['hasPendingChildRuns']:
            if not isinstance(run, str) or not run:
                raise ValueError('terminal delegated task lacks immutable run identity')
            statuses.append({'id': _m_hosts.wake_binding(json.dumps([row['taskId'], run, status])),
                             'node': row['node'], 'parent_thread': row['parent_thread'],
                             'kind': 'delegated-completion', 'task_key': key,
                             'status': status, 'run': run})
    if host_task_read(inst) != registry:
        raise ValueError('delegated registry changed during observation')
    fresh = inst.__class__(inst.root)
    if any(fresh.node(r['node'])['local'].get('coordinator') != r['parent_thread'] for r in statuses):
        raise ValueError('delegated parent binding changed during observation')
    with host_task_store(inst) as current:
        if current != registry:
            raise ValueError('delegated registry changed before publication')
        for item in statuses:
            current['tasks'][item['task_key']]['completion'] = item
    snapshot = {'version': 1, 'parent_thread': parent, 'observed_at': inst.now_dt().isoformat(), 'items': statuses}
    path = inst.root / '.brain' / ('host-task-status-' + _m_hosts.wake_binding(parent) + '.json')
    _m_common.atomic_write(path, json.dumps(snapshot) + '\n')
    for item in statuses:
        n = inst.node(item['node'])
        marker = '<!-- host-task:' + item['id'] + ' -->'
        _m_transitions.inbox_append(n['dir'], '- ' + inst.now() + ' from host: Delegated task finished (' +
                                    item['status'] + '); inspect registered task result. ' + marker + '\n', marker)
        # Root result channels are inbox-only. Other parent coordinators get a doorbell.
        if _m_hosts.wake_target(inst, n)[0]['id'] != 'root':
            _m_hosts.request_wake(inst, n, 'host-task:' + item['id'], items=[item])
    return snapshot


def host_task_items(inst, nodes=None):
    """Terminal observations are immutable; their durable receipts do not expire."""
    allowed = {n['id']: n for n in (nodes if nodes is not None else inst.nodes()) if not n['archived']}
    out = []
    for row in host_task_read(inst)['tasks'].values():
        r = row.get('completion')
        if r is None:
            continue
        if (not isinstance(r, dict) or r.get('status') not in HOST_TASK_TERMINAL or
                not isinstance(r.get('id'), str) or not re.fullmatch('[0-9a-f]{64}', r['id']) or
                not isinstance(r.get('run'), str) or r.get('node') != row['node'] or
                r.get('parent_thread') != row['parent_thread']):
            raise ValueError('invalid delegated completion receipt')
        if r['node'] in allowed and allowed[r['node']]['local'].get('coordinator') == r['parent_thread']:
            out.append({**r, 'text': 'Delegated task finished (' + r['status'] + '); inspect registered task result'})
    return out


def host_task_refresh_if_enabled(inst):
    if inst.local_of('root').get('host_export') != 'on':
        return
    try:
        host_task_refresh(inst)
    except (Exception, SystemExit):
        print('delegated host state unknown; registered tasks retained for retry')


def cmd_delegate(a):
    try:
        inst = _m_nodes.load()
        task = open(a.task_file).read()
        target = json.loads(open(a.target_file).read())
        result = host_delegate(inst, inst.node(a.id), task, target, a.key)
        print(result['taskId'])
    except (Exception, SystemExit):
        _m_common.die('delegation unavailable or outcome unknown; retain the stable key for explicit retry')
