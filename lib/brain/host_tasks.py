"""Registered delegated tasks have a file-backed completion doorbell fallback."""
import contextlib
import fcntl
import json
import os
import re
import time

from . import common as _m_common
from . import instance as _m_instance

_m_briefs = _m_common.LazyModule(__package__, 'briefs')
_m_host_observations = _m_common.LazyModule(__package__, 'host_observations')
_m_hosts = _m_common.LazyModule(__package__, 'hosts')
_m_host_instances = _m_common.LazyModule(__package__, 'host_instances')
_m_launch_policy = _m_common.LazyModule(__package__, 'launch_policy')
_m_nodes = _m_common.LazyModule(__package__, 'nodes')
_m_rpc = _m_common.LazyModule(__package__, 'host_rpc')
_m_transitions = _m_common.LazyModule(__package__, 'transitions')


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
            _m_common.atomic_write(inst.root / '.brain/host-tasks.json', after + '\n', private=True)


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
    selection = {k: target[k] for k in ('providerInstanceId', 'model', 'options') if k in target}
    _m_launch_policy.require_selection(inst, n, 'worker', selection)
    prior = host_task_read(inst)['tasks'].get(identity)
    if prior and prior.get('input_digest') != digest:
        raise ValueError('delegation key already names different work')
    if prior and prior.get('taskId'):
        return dict(prior)
    if prior and (prior.get('recovery') or prior.get('route_failure')):
        return delegation_recovery(inst, n, identity, payload, rpc)
    with host_task_store(inst) as data:
        row = data['tasks'].get(identity)
        if row and row.get('input_digest') != digest:
            raise ValueError('delegation key already names different work')
        if row and (row.get('taskId') or row.get('recovery') or row.get('route_failure')):
            return dict(row)
        data['tasks'][identity] = {'node': n['id'], 'parent_thread': parent,
                                  'clientRequestId': payload['clientRequestId'], 'input_digest': digest,
                                  'created_at': inst.now(), 'taskId': None}
    if inst.__class__(inst.root).node(n['id'])['local'].get('coordinator') != parent:
        raise ValueError('parent binding changed before delegation')
    try:
        result = rpc('delegate_task', payload)  # no automatic mutation retry, no instance writer lock
    except _m_rpc.HostRPCRejected as error:
        # Only the host's own refusal is a confirmed route failure; unknown outcomes still raise.
        with host_task_store(inst) as data:
            data['tasks'][identity]['route_failure'] = {'at': inst.now(), 'error': str(error)[:200]}
        return delegation_recovery(inst, n, identity, payload, rpc)
    if not isinstance(result, dict) or not isinstance(result.get('taskId'), str) or not result['taskId']:
        raise ValueError('delegation outcome unknown; retry explicitly with the same stable key')
    with host_task_store(inst) as data:
        row = data['tasks'][identity]
        row.update(taskId=result['taskId'], childThreadId=result.get('childThreadId'))
        return dict(row)


def delegation_recovery(inst, n, identity, payload, rpc):
    """After a confirmed route failure, run the same work once as a visible thread on the same model.

    Its checks run before the intent is recorded, so a refusal there leaves the confirmed
    failure retryable. Intent is recorded before the launch; an unknown
    launch outcome is never repeated, the provider and model are never switched, and the
    returned thread's identity is checked.
    """
    target = payload['target']
    marker = 'delegate-recovery: ' + identity
    selection = {'instanceId': target['providerInstanceId'], 'model': target['model'],
                 **({'options': target['options']} if 'options' in target else {})}

    def recorded(row):
        recovery = row.get('recovery')
        if recovery and recovery.get('phase') == 'launched':
            return dict(row)
        if recovery and recovery.get('phase') == 'mismatch':
            raise ValueError('route recovery thread ran a different provider or model; not adopted, no repeat')
        if recovery:
            raise ValueError('route recovery launch outcome unknown; inspect the host for ' + marker + ', no repeat')
        if not row.get('route_failure'):
            raise ValueError('route recovery requires a confirmed delegation failure')
        return None

    done = recorded(host_task_read(inst)['tasks'][identity])
    if done:
        return done
    current = inst.__class__(inst.root).node(n['id'])['local']
    project = current.get('home_project') or current.get('host_project')
    if current.get('coordinator') != n['local'].get('coordinator') or not project:
        raise ValueError('route recovery needs the same bound parent and a recorded home project')
    with host_task_store(inst) as data:
        row = data['tasks'][identity]
        done = recorded(row)
        if not done:
            row['recovery'] = {'phase': 'launching', 'marker': marker, 'at': inst.now()}
    if done:
        return done
    try:
        response = rpc('t3_thread_launch', {'title': n['id'] + ' delegated work (route recovery)',
            'projectId': project, 'workspaceStrategy': {'type': 'root'},
            'modelSelection': selection, 'message': marker + '\n\n' + payload['task']})
    except Exception:
        raise ValueError('route recovery launch outcome unknown; intent retained, no repeat') from None
    thread = response.get('threadId') if isinstance(response, dict) else None
    if not isinstance(thread, str) or not thread:
        raise ValueError('route recovery launch outcome unknown; intent retained, no repeat')
    observed = rpc('t3_thread_read', {'threadId': thread, 'limit': 1})
    observed = observed.get('thread') if isinstance(observed, dict) else None
    same = (isinstance(observed, dict) and observed.get('threadId') == thread and
            observed.get('providerInstanceId') == selection['instanceId'] and observed.get('model') == selection['model'])
    with host_task_store(inst) as data:
        row = data['tasks'][identity]
        row['recovery'].update(phase='launched' if same else 'mismatch', thread=thread)
        result = dict(row)
    if not same:
        raise ValueError('route recovery thread does not run the requested provider and model; it was not adopted')
    return result


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
    _m_common.atomic_write(path, json.dumps(snapshot) + '\n', private=True)
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
        print(result.get('taskId') or 'route failed; same work running as visible thread ' + result['recovery']['thread'])
    except (Exception, SystemExit):
        _m_common.die('delegation unavailable or outcome unknown; retain the stable key for explicit retry')
