"""Explicit native-host lifecycle execution, fenced by current recorded ownership."""
import json
import re

from . import common as _m_common
from . import host_rpc as _m_rpc
from . import instance as _m_instance
from . import jobs as _m_jobs
from . import nodes as _m_nodes
from . import settlement as _m_settlement


def host_object(rpc, name, args):
    value = rpc(name, args)
    if not isinstance(value, dict):
        raise ValueError(name + ': unknown native host response')
    return value


def lifecycle_inventory(n, rpc):
    projects = sorted({n['local'][key] for key in ('home_project', 'host_project') if n['local'].get(key)})
    if not projects:
        raise ValueError('lifecycle requires recorded host projects; no ownership inferred')
    threads, callers = {}, set()
    for project in projects:
        cursor, seen, found, total = None, set(), set(), None
        for _ in range(100):
            args = {'projectId': project, 'includeSubagents': True, 'limit': 100}
            if cursor is not None:
                args['cursor'] = cursor
            data = host_object(rpc, 't3_thread_list', args)
            if (data.get('projectId') != project or not isinstance(data.get('threads'), list)
                    or type(data.get('total')) is not int or data['total'] < 0
                    or total is not None and total != data['total']):
                raise ValueError('incomplete or changing host project inventory')
            total = data['total']
            if isinstance(data.get('currentThreadId'), str):
                callers.add(data['currentThreadId'])
            for thread in data['threads']:
                identity = thread.get('threadId') if isinstance(thread, dict) else None
                if not isinstance(identity, str) or not identity or identity in found:
                    raise ValueError('ambiguous host thread inventory')
                found.add(identity)
                if identity in threads and threads[identity] != thread:
                    raise ValueError('host thread changed between project inventories')
                threads[identity] = thread
            cursor = data.get('nextCursor')
            if cursor is None:
                if len(found) != total:
                    raise ValueError('host inventory count does not prove exhaustion')
                break
            if type(cursor) is not int or cursor in seen:
                raise ValueError('host inventory pagination did not advance')
            seen.add(cursor)
        else:
            raise ValueError('host inventory exceeds the bounded sweep; no completion inferred')
    return projects, threads, callers


def lifecycle_protected(inst):
    protected = set()
    for node in inst.nodes():
        coordinator = node['local'].get('coordinator')
        if coordinator:
            protected.update(_m_jobs.thread_reference_ids(coordinator))
        rows = _m_jobs.read_jobs(node)
        if rows is None:
            raise ValueError('lifecycle refused: unreadable job ownership')
        for row in rows:
            if not row.get('Ended'):
                for ref, _ in _m_jobs.bound_worker_threads(node, row['Job']):
                    protected.update(_m_jobs.thread_reference_ids(ref))
    return protected


def launch_marker(rpc, identity):
    data = host_object(rpc, 't3_thread_read', {'threadId': identity, 'afterPosition': 0,
                                            'limit': 1, 'maxCharsPerItem': 50000})
    items = data.get('items')
    if not isinstance(items, list) or not items:
        return None
    first = items[0]
    if (first.get('createdBy') != 'user' or first.get('textTruncated')
            or not isinstance(first.get('text'), str)):
        return None
    keys, fenced = set(), False
    for line in first['text'].splitlines():
        if line.startswith(('```', '~~~')):
            fenced = not fenced
        match = re.fullmatch(r'brain-job: ([0-9a-f]{24})(?: \(lets `brain jobs` count.*\))?', line)
        if match and not fenced:
            keys.add(match[1])
    return next(iter(keys)) if len(keys) == 1 else None


def terminal_host_thread(rpc, identity):
    data = host_object(rpc, 't3_thread_read', {'threadId': identity, 'limit': 1, 'runLimit': 1})
    thread = data.get('thread')
    if (not isinstance(thread, dict) or thread.get('threadId') != identity
            or thread.get('status') not in _m_settlement.SETTLEMENT_TERMINAL
            or thread.get('activeRunId') is not None or thread.get('pendingRequestCount') != 0):
        return None
    pending = host_object(rpc, 't3_pending_request_list', {'threadId': identity})
    return thread if pending.get('requestIds') == [] else None


def settle_closed_jobs(inst, n, rpc=None, job_keys=None):
    rpc = rpc or _m_rpc.call
    if _m_common.outward_links(inst.root):
        raise ValueError('lifecycle refused: unsafe instance links')
    rows = _m_jobs.read_jobs(n)
    if rows is None:
        raise ValueError('lifecycle refused: unreadable job ledger')
    targets = _m_jobs.unsettled_jobs(rows)
    if job_keys is not None:
        requested = set(job_keys)
        if any(not isinstance(key, str) or not re.fullmatch('[0-9a-f]{24}', key) for key in requested):
            raise ValueError('settlement requires exact ledger job keys')
        closed = {_m_jobs.job_key(row) for row in rows if _m_jobs.closed_job(row)}
        if not requested.issubset(closed):
            raise ValueError('settlement refused: requested job is open or unknown')
        targets = [row for row in targets if _m_jobs.job_key(row) in requested]
    result = {'node': n['id'], 'settled_threads': [], 'settled_jobs': [], 'retained': [], 'unknown_threads': []}
    if not targets:
        return result
    projects, inventory, callers = lifecycle_inventory(n, rpc)
    protected = lifecycle_protected(inst) | callers
    known = {_m_jobs.job_key(row): row for row in rows}
    owner, missing = {}, set()
    for row in rows:
        key = _m_jobs.job_key(row)
        for reference, _ in _m_jobs.bound_worker_threads(n, row['Job']):
            ids = _m_jobs.thread_reference_ids(reference)
            if len(ids) != 1:
                missing.add(key)
            for identity in ids:
                if identity not in inventory or identity in owner and owner[identity] != key:
                    missing.add(key)
                owner[identity] = key
    # Follow all listed descendants before considering first-prompt markers.
    for _ in range(len(inventory) + 1):
        additions = {identity: owner[thread['parentThreadId']] for identity, thread in inventory.items()
                     if identity not in owner and thread.get('parentThreadId') in owner}
        if not additions:
            break
        owner.update(additions)
    for identity in sorted(set(inventory) - set(owner) - protected):
        marker = launch_marker(rpc, identity)
        if marker in known:
            owner[identity] = marker
        else:
            result['unknown_threads'].append(identity)
    for _ in range(len(inventory) + 1):
        additions = {identity: owner[thread['parentThreadId']] for identity, thread in inventory.items()
                     if identity not in owner and thread.get('parentThreadId') in owner}
        if not additions:
            break
        owner.update(additions)
    result['unknown_threads'] = [identity for identity in result['unknown_threads'] if identity not in owner]
    for row in targets:
        key = _m_jobs.job_key(row)
        group = sorted(identity for identity, claimed in owner.items() if claimed == key)
        if key in missing or not group or any(identity in protected for identity in group):
            result['retained'].append({'job': key, 'reason': 'missing, ambiguous or protected thread ownership'})
            continue
        if any(terminal_host_thread(rpc, identity) is None for identity in group):
            result['retained'].append({'job': key, 'reason': 'live, waiting, pending or unknown host state'})
            continue
        confirmed = []
        for identity in group:
            # Re-read files and host state immediately before each singular call.
            fresh = _m_instance.Instance(inst.root)
            current = next((r for r in _m_jobs.read_jobs(fresh.node(n['id'])) or [] if _m_jobs.job_key(r) == key), None)
            thread = terminal_host_thread(rpc, identity)
            if not current or not _m_jobs.closed_job(current) or identity in lifecycle_protected(fresh) | callers or thread is None:
                break
            if thread.get('settled') is not True:
                reply = host_object(rpc, 't3_thread_organize', {'action': 'settle', 'threadId': identity})
                if reply.get('settlesWhenTurnEnds'):
                    rpc('t3_thread_organize', {'action': 'unsettle', 'threadId': identity})
                    raise ValueError('host attempted to settle the caller; immediately unsettled, no job stamped')
            confirmed.append(identity)
        if len(confirmed) != len(group):
            result['retained'].append({'job': key, 'reason': 'ownership or activity changed before settlement'})
            continue
        _, after, _ = lifecycle_inventory(n, rpc)
        states = {identity: terminal_host_thread(rpc, identity) for identity in group}
        if any(after.get(identity, {}).get('settled') is not True or states[identity] is None for identity in group):
            result['retained'].append({'job': key, 'reason': 'host did not confirm settled terminal threads'})
            continue
        result['settled_threads'].extend(group)
        if result['unknown_threads']:
            result['retained'].append({'job': key, 'reason': 'unattributed project threads prevent exhaustive job attestation'})
            continue
        with _m_instance.brain_lock(inst):
            current_rows = _m_jobs.ledger_rows(n)
            current = next((r for r in current_rows or [] if _m_jobs.job_key(r) == key), None)
            if not current or not _m_jobs.closed_job(current):
                raise ValueError('job changed before settlement publication; host observations retained by caller')
            evidence = {'version': 1, 'node': n['id'], 'observed_at': inst.now_dt().isoformat(),
                        'source': 'native-host-rpc', 'project_ids': projects,
                        'coverage': {'project': True, 'nested': True, 'complete': True},
                        'jobs': [{'job_key': key, 'threads': [
                            {'threadId': identity, 'job_key': key, 'state': states[identity]['status'],
                             'settled': True, 'pending_requests': [],
                             **({'parentThreadId': after[identity]['parentThreadId']}
                                if after[identity].get('parentThreadId') in group else {}),
                             **({'external_parentThreadId': after[identity]['parentThreadId']}
                                if after[identity].get('parentThreadId') and after[identity]['parentThreadId'] not in group else {})}
                            for identity in group]}]}
            _m_settlement.validate_settlement_attestation(inst, n, [current], evidence)
            path = n['dir'] / 'job-settlements' / (key + '.json')
            _m_common.safe_target(path)
            path.parent.mkdir(exist_ok=True)
            _m_common.atomic_write(path, json.dumps(evidence, sort_keys=True) + '\n')
            current['Settled'] = inst.now()
            _m_jobs.write_jobs(inst, n, current_rows)
            inst.commit([path], f"{n['id']}: native host settlement confirmed")
            result['settled_jobs'].append(key)
    return result


def settle_landed_jobs(inst, n, job_keys):
    """Train finish hook: settle only accepted closed jobs; retain uncertain work."""
    result = {'settled': [], 'pending': [], 'errors': []}
    if (not isinstance(job_keys, (list, tuple, set)) or any(
            not isinstance(key, str) or not re.fullmatch('[0-9a-f]{24}', key) for key in job_keys)):
        result['errors'].append('settlement requires a collection of exact ledger job keys')
        return result
    requested = list(dict.fromkeys(job_keys))
    try:
        observation = settle_closed_jobs(inst, n, job_keys=requested)
        result['pending'] = observation['retained']
    except (OSError, ValueError, _m_rpc.HostRPCError) as error:
        result['errors'].append(str(error))
    rows = _m_jobs.read_jobs(n)
    for key in requested:
        row = next((r for r in rows or [] if _m_jobs.job_key(r) == key), None)
        if row and _m_jobs.closed_job(row) and row.get('Settled'):
            result['settled'].append(key)
        elif not any(p.get('job') == key for p in result['pending']):
            result['pending'].append({'job': key, 'reason': 'native settlement unconfirmed'})
    return result


def cmd_lifecycle(a):
    inst = _m_nodes.load()
    try:
        n = inst.node(a.id)
        if a.action == 'restart':
            from . import worker_lifecycle as _m_worker
            result = _m_worker.restart_worker(inst, n, a.job_key)
        elif a.action == 'maintain':
            result = maintain_lifecycle(inst, n)
        else:
            result = settle_closed_jobs(inst, n)
    except (OSError, ValueError, _m_rpc.HostRPCError) as error:
        _m_common.die(str(error))
    print(json.dumps(result, sort_keys=True))


def maintain_lifecycle(inst, n, rpc=None):
    """Native maintenance entry; each invocation advances bounded restart work."""
    from . import worker_lifecycle as _m_worker
    result = {'restarts': [], 'settlement': None}
    rows = _m_jobs.read_jobs(n)
    if rows is None:
        raise ValueError('lifecycle waits for readable ownership')
    for row in rows:
        if not row.get('Ended'):
            result['restarts'].append(_m_worker.restart_worker(inst, n, _m_jobs.job_key(row), rpc=rpc))
    result['settlement'] = settle_closed_jobs(inst, n, rpc=rpc)
    return result
