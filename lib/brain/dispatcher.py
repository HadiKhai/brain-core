"""Bounded file-only question plans and idempotent native doorbell dispatch."""
import json
import fcntl
import os

from . import common as _m_common

_m_nodes = _m_common.LazyModule(__package__, 'nodes')
_m_status = _m_common.LazyModule(__package__, 'status')


def dispatch_plan(inst, limit=50, node_limit=200):
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError('dispatch limit must be between 1 and 200')
    # Only explicit requests, not advice/status/age/machine readings. Reuse the
    # shared inbox source projection and its stable episode IDs, without --new.
    if type(node_limit) is not int or not 1 <= node_limit <= 200:
        raise ValueError('dispatch node limit must be between 1 and 200')
    nodes = sorted(inst.nodes(), key=lambda n: n['id'])
    items, _ = _m_status.inbox_items(inst, nodes=nodes[:node_limit], reporter='root', readonly=True, requests_only=True)
    asks = [item for item in items
            if item['kind'] in ('inbox', 'blocked', 'blocked-overdue') and not item.get('informational')]
    asks = sorted({(item['node'], item['id']): item for item in asks}.values(), key=lambda i: (i['node'], i['id']))
    if not asks:
        return None
    return {'version': 1, 'bounded': True, 'partial': len(asks) > limit or len(nodes) > node_limit, 'limit': limit,
            'node_limit': node_limit, 'scanned_nodes': min(len(nodes), node_limit),
            'evidence': 'recorded files only; host activity unknown',
            'items': [{'episode_id': item['id'], 'target_node': item['node'], 'kind': item['kind'],
                       'text': item['text']} for item in asks[:limit]]}


def cmd_dispatch(a):
    try:
        inst = _m_nodes.load()
        if getattr(a, 'run', False):
            result = dispatch_run(inst, a.limit, a.node_limit)
            if result['accepted'] or result['partial']:
                print(json.dumps(result, sort_keys=True))
            return
        plan = dispatch_plan(inst, a.limit, a.node_limit)
    except ValueError as error:
        _m_common.die(str(error))
    if plan:
        print(json.dumps(plan, ensure_ascii=False, sort_keys=True))


def dispatch_run(inst, limit=50, node_limit=200):
    from . import communication as _m_communication
    from . import host_observations as _m_host_observations
    from . import hosts as _m_hosts
    from . import watch as _m_watch
    if type(limit) is not int or not 1 <= limit <= 200 or type(node_limit) is not int or not 1 <= node_limit <= 200:
        raise ValueError('dispatch limits must be between one and two hundred')
    _m_communication.communication_reconcile(inst)
    nodes = sorted(inst.nodes(), key=lambda n: n['id'])
    selected = nodes[:node_limit]
    inbox, _ = _m_status.inbox_items(inst, selected, 'root', readonly=True, requests_only=True)
    asks = [{'id': i['id'], 'node': i['node'], 'kind': 'question', 'text': i['text'].removeprefix('inbox: ')}
            for i in inbox if i['kind'] == 'inbox' and '<!-- host-question:' in i['text']]
    asks += _m_communication.communication_items(inst, selected)
    try:
        asks += _m_host_observations.host_observation_items(inst, selected)
    except (OSError, ValueError, TypeError):
        pass  # unknown host state cannot suppress file-backed questions
    asks = sorted({(i['node'], i['id']): i for i in asks}.values(), key=lambda i: (i['node'], i['id']))
    result = {'accepted': 0, 'partial': len(nodes) > node_limit or len(asks) > limit}
    if not asks:
        return result
    groups = _m_watch.watch_groups(inst)
    def text_key(text):
        return ' '.join(text.removeprefix('inbox: ').split())[:400]
    actual = {(i['node'], text_key(i['text'])): i for g in groups.values() for i in g['items']}
    directory = inst.root / '.brain'
    path, lock_path = directory / 'dispatch-receipts.json', directory / 'dispatch.lock'
    _m_common.safe_target(path)
    _m_common.safe_target(lock_path)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a+') as lock:
        if os.fstat(lock.fileno()).st_nlink != 1:
            raise ValueError('unsafe dispatcher lock')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {**result, 'busy': True}
        if path.exists() and path.stat().st_size > 1024 * 1024:
            raise ValueError('dispatch receipts exceed bound')
        data = json.loads(path.read_text()) if path.exists() else {'version': 1, 'accepted': []}
        if (not isinstance(data, dict) or data.get('version') != 1 or
                not isinstance(data.get('accepted'), list) or not all(isinstance(x, str) for x in data['accepted'])):
            raise ValueError('invalid dispatcher receipts; retained for repair')
        accepted = set(data['accepted'])
        before = set(accepted)
        for q in asks[:limit]:
            source = inst.node(q['node'])
            target, command = _m_hosts.wake_target(inst, source)
            if not command:
                continue
            receipt = _m_hosts.wake_binding(json.dumps([q['node'], q['id'], target['id'],
                                                       target['local'].get('coordinator'), _m_hosts.wake_binding(command)]))
            if receipt in accepted:
                continue
            item = actual.get((q['node'], text_key(q['text'])))
            ref = ({'id': item['id'], 'kind': item['kind'], 'question': True} if item else
                   {'id': _m_hosts.wake_binding(json.dumps(['dispatch-question', q['node'], q['id']])), 'kind': 'question'})
            if _m_hosts.request_wake(inst, source, 'dispatch-question:' + receipt, items=[ref]):
                accepted.add(receipt)
                result['accepted'] += 1
        if accepted != before:
            body = json.dumps({'version': 1, 'accepted': sorted(accepted)})
            if len(body.encode()) > 1024 * 1024:
                raise ValueError('dispatch receipts exceed bound; queue remains durable')
            _m_common.atomic_write(path, body + '\n', private=True)
    return result


def dispatch_if_enabled(inst):
    if inst.local_of('root').get('dispatcher') != 'on':
        return
    try:
        dispatch_run(inst)
    except (OSError, ValueError, TypeError):
        print('dispatcher state unavailable; questions retained in source files')
