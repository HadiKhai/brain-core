"""Explicit native-host lifecycle execution, fenced by current recorded ownership."""
import json
import re

from . import common as _m_common
from . import instance as _m_instance

_m_rpc = _m_common.LazyModule(__package__, 'host_rpc')
_m_host_instances = _m_common.LazyModule(__package__, 'host_instances')
_m_jobs = _m_common.LazyModule(__package__, 'jobs')
_m_nodes = _m_common.LazyModule(__package__, 'nodes')
_m_settlement = _m_common.LazyModule(__package__, 'settlement')


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


def settle_closed_jobs(inst, n, rpc=None, job_keys=None, revalidate=False, tell=False, workspace=None):
    rpc = _m_host_instances.routed_host_rpc(inst, n['id'], rpc or _m_rpc.call)
    if _m_common.outward_links(inst.root):
        raise ValueError('lifecycle refused: unsafe instance links')
    rows = _m_jobs.read_jobs(n)
    if rows is None:
        raise ValueError('lifecycle refused: unreadable job ledger')
    targets = _m_jobs.unsettled_jobs(rows)
    if revalidate and job_keys is None:
        raise ValueError('settlement revalidation requires exact closed job keys')
    if job_keys is not None:
        requested = set(job_keys)
        if any(not isinstance(key, str) or not re.fullmatch('[0-9a-f]{24}', key) for key in requested):
            raise ValueError('settlement requires exact ledger job keys')
        closed = {_m_jobs.job_key(row) for row in rows if _m_jobs.closed_job(row)}
        if not requested.issubset(closed):
            raise ValueError('settlement refused: requested job is open or unknown')
        targets = [row for row in targets if _m_jobs.job_key(row) in requested]
        if revalidate:
            targets = [row for row in rows if _m_jobs.job_key(row) in requested and _m_jobs.closed_job(row)]
    result = {'node': n['id'], 'settled_threads': [], 'settled_jobs': [], 'retained': [], 'unknown_threads': []}
    if not targets:
        return result
    projects, inventory, callers = lifecycle_inventory(n, rpc)
    protected = lifecycle_protected(inst) | callers
    known = {_m_jobs.job_key(row): row for row in rows}
    ownership = {key: _m_jobs.bound_worker_threads(n, row['Job']) for key, row in known.items()}
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
                    if identity in owner:
                        missing.add(owner[identity])
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
        confirmed = []
        try:
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
                thread = terminal_host_thread(rpc, identity)
                # Host reads can outlive a local rebind. Reload AFTER observation,
                # and require the exact closed-job binding before any mutation.
                fresh = _m_instance.Instance(inst.root)
                current_node = fresh.node(n['id'])
                current = next((r for r in _m_jobs.read_jobs(current_node) or [] if _m_jobs.job_key(r) == key), None)
                if not current or not _m_jobs.closed_job(current) or identity in lifecycle_protected(fresh) | callers or thread is None:
                    break
                if _m_jobs.bound_worker_threads(current_node, current['Job']) != ownership[key]:
                    break
                if thread.get('settled') is not True:
                    if tell:
                        from . import cleanup_notices as _m_cleanup_notices
                        fresh_node = fresh.node(n['id'])
                        if (fresh_node['local'].get('worktrees', {}).get(current['Job'], {}).get('path') != workspace
                                or _m_jobs.bound_worker_threads(fresh_node, current['Job']) != ownership[key]
                                or not _m_cleanup_notices.cleanup_notice(fresh, fresh_node, key, identity,
                                                                        workspace, rpc)):
                            break
                        if terminal_host_thread(rpc, identity) is None:
                            break
                        fresh = _m_instance.Instance(inst.root)
                        current_node = fresh.node(n['id'])
                        current = next((r for r in _m_jobs.read_jobs(current_node) or [] if _m_jobs.job_key(r) == key), None)
                        if (not current or not _m_jobs.closed_job(current)
                                or _m_jobs.bound_worker_threads(current_node, current['Job']) != ownership[key]
                                or identity in lifecycle_protected(fresh) | callers):
                            break
                    reply = host_object(rpc, 't3_thread_organize', {'action': 'settle', 'threadId': identity})
                    if reply.get('settlesWhenTurnEnds'):
                        rpc('t3_thread_organize', {'action': 'unsettle', 'threadId': identity})
                        raise ValueError('host attempted to settle the caller; immediately unsettled, no job stamped')
                    checked = terminal_host_thread(rpc, identity)
                    if checked is None or checked.get('settled') is not True:
                        break
                confirmed.append(identity)
                result['settled_threads'].append(identity)
            if len(confirmed) != len(group):
                result['retained'].append({'job': key, 'reason': 'ownership or activity changed before settlement'})
                continue
            _, after, _ = lifecycle_inventory(n, rpc)
            if (set(after) != set(inventory) or any(
                    after[item].get('parentThreadId') != inventory[item].get('parentThreadId') for item in after)):
                result['retained'].append({'job': key, 'reason': 'host thread ownership inventory changed'})
                continue
            states = {identity: terminal_host_thread(rpc, identity) for identity in group}
            if any(after.get(identity, {}).get('settled') is not True or states[identity] is None for identity in group):
                result['retained'].append({'job': key, 'reason': 'host did not confirm settled terminal threads'})
                continue
            if result['unknown_threads']:
                result['retained'].append({'job': key, 'reason': 'unattributed project threads prevent exhaustive job attestation'})
                continue
            with _m_instance.brain_lock(inst):
                fresh_owner = _m_instance.Instance(inst.root)
                fresh_node = fresh_owner.node(n['id'])
                if _m_jobs.bound_worker_threads(fresh_node, row['Job']) != ownership[key]:
                    raise ValueError('worker ownership changed before settlement publication; no job stamped')
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
                _m_common.atomic_write(path, json.dumps(evidence, sort_keys=True) + '\n', private=True)
                current['Settled'] = inst.now()
                _m_jobs.write_jobs(inst, n, current_rows)
                inst.commit([path], f"{n['id']}: native host settlement confirmed")
                result['settled_jobs'].append(key)
        except (OSError, _m_rpc.HostRPCError):
            result['settled_threads'] = list(dict.fromkeys(result['settled_threads'] + confirmed))
            result['retained'].append({'job': _m_jobs.job_key(row),
                'reason': 'partial lifecycle outcome unknown; no job completion inferred'})
    return result


def worktree_thread_hook(inst, n, cleanup, rpc=None, tell=False):
    """C06 callback: True only after exact closed jobs are freshly host-settled.

    The caller still owns path removal and its final branch/live-owner recheck.
    This function never removes a workspace or cancels a host request.
    """
    try:
        fresh = _m_instance.Instance(inst.root)
        current = fresh.node(n['id'])
        if (not isinstance(cleanup, dict) or cleanup.get('version') != 1 or cleanup.get('node') != n['id']
                or not isinstance(cleanup.get('branch'), str) or not isinstance(cleanup.get('jobs'), list)
                or not cleanup['jobs'] or not isinstance(cleanup.get('threads'), list)):
            return False
        branch = cleanup['branch']
        workspace = current['local'].get('worktrees', {}).get(branch, {}).get('path')
        if not workspace or cleanup.get('workspace') != workspace:
            return False
        rows = _m_jobs.read_jobs(current)
        if rows is None:
            return False
        expected = {_m_jobs.job_key(row): row for row in rows if row['Job'] == branch}
        if {item.get('key') for item in cleanup['jobs'] if isinstance(item, dict)} != set(expected):
            return False
        for item in cleanup['jobs']:
            row = expected.get(item.get('key'))
            if (not row or not _m_jobs.closed_job(row) or item.get('closed') is not True
                    or item.get('started') != row.get('Started') or item.get('ended') != row.get('Ended')):
                return False
        references = set()
        worker_bindings = {key: current['local'].get('workers', {}).get(key) for key in expected}
        retired_bindings = {key: list(current['local'].get('retired_workers', {}).get(key, [])) for key in expected}
        for reference, _ in _m_jobs.bound_worker_threads(current, branch):
            references.update(_m_jobs.thread_reference_ids(reference))
        supplied = {item.get('id') for item in cleanup['threads'] if isinstance(item, dict)}
        if not supplied or supplied != references:
            return False
        _m_instance.unlock()
        observation = settle_closed_jobs(fresh, current, rpc=rpc, job_keys=list(expected), revalidate=True,
                                        tell=tell and cleanup.get('workspace_exists') is True
                                        and cleanup.get('phase') == 'before-remove', workspace=workspace)
        if set(observation['settled_jobs']) != set(expected) or observation['retained']:
            return False
        with _m_instance.brain_lock(inst):
            latest = _m_instance.Instance(inst.root)
            bound = latest.node(n['id'])
            rows = _m_jobs.read_jobs(bound) or []
            if bound['local'].get('worktrees', {}).get(branch, {}).get('path') != workspace:
                return False
            if any(bound['local'].get('workers', {}).get(key) != worker_bindings[key] or
                   bound['local'].get('retired_workers', {}).get(key, []) != retired_bindings[key] for key in expected):
                return False
            for key in expected:
                row = next((row for row in rows if _m_jobs.job_key(row) == key), None)
                if not row or not _m_jobs.closed_job(row) or not row.get('Settled'):
                    return False
            for key in expected:
                reference = bound['local'].get('workers', {}).pop(key, None)
                if reference:
                    retired = bound['local'].setdefault('retired_workers', {}).setdefault(key, [])
                    if reference not in retired:
                        retired.append(reference)
            latest.save_local()
            inst.local = latest.local
        return True
    except (OSError, ValueError, TypeError, AttributeError, _m_rpc.HostRPCError):
        return False


def settle_landed_jobs(inst, n, job_keys):
    """Train finish hook: settle only accepted closed jobs; retain uncertain work."""
    result = {'settled': [], 'pending': [], 'errors': []}
    if (not isinstance(job_keys, (list, tuple, set)) or any(
            not isinstance(key, str) or not re.fullmatch('[0-9a-f]{24}', key) for key in job_keys)):
        result['errors'].append('settlement requires a collection of exact ledger job keys')
        return result
    requested = list(dict.fromkeys(job_keys))
    inst = _m_instance.Instance(inst.root)
    n = inst.node(n['id'])
    bindings = {key: n['local'].get('workers', {}).get(key) for key in requested}
    verified = []
    try:
        observation = settle_closed_jobs(inst, n, job_keys=requested, revalidate=True)
        verified = observation['settled_jobs']
        result['pending'] = observation['retained']
    except (OSError, ValueError, _m_rpc.HostRPCError) as error:
        result['errors'].append(str(error))
    rows = _m_jobs.read_jobs(n)
    for key in requested:
        row = next((r for r in rows or [] if _m_jobs.job_key(r) == key), None)
        if key in verified and row and _m_jobs.closed_job(row) and row.get('Settled'):
            with _m_instance.brain_lock(inst):
                latest = _m_instance.Instance(inst.root)
                current = latest.node(n['id'])
                row = next((r for r in _m_jobs.read_jobs(current) or [] if _m_jobs.job_key(r) == key), None)
                if (not row or not _m_jobs.closed_job(row) or not row.get('Settled')
                        or current['local'].get('workers', {}).get(key) != bindings[key]):
                    result['pending'].append({'job': key, 'reason': 'ownership changed before worker unbinding'})
                    continue
                reference = current['local'].get('workers', {}).pop(key, None)
                if reference:
                    retired = current['local'].setdefault('retired_workers', {}).setdefault(key, [])
                    if reference not in retired:
                        retired.append(reference)
                latest.save_local()
                result['settled'].append(key)
        elif not any(p.get('job') == key for p in result['pending']):
            result['pending'].append({'job': key, 'reason': 'native settlement unconfirmed'})
    return result


def cmd_lifecycle(a):
    inst = _m_nodes.load()
    try:
        n = inst.node(a.id)
        if a.action in ('restart', 'launch'):
            from . import worker_lifecycle as _m_worker
            result = (_m_worker.launch_worker if a.action == 'launch' else _m_worker.restart_worker)(inst, n, a.job_key)
        elif a.action in ('rotate', 'accept-rotation'):
            from . import coordinator_lifecycle as _m_coordinator
            if a.action == 'rotate':
                result = _m_coordinator.rotate_coordinator(inst, n)
            else:
                import os
                session = a.session_id or os.environ.get('CODEX_THREAD_ID') or os.environ.get('CLAUDE_CODE_SESSION_ID')
                result = _m_coordinator.accept_rotation(inst, n, a.ticket, session)
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
    from . import coordinator_lifecycle as _m_coordinator
    result = {'restarts': [], 'settlement': None}
    rows = _m_jobs.read_jobs(n)
    if rows is None:
        raise ValueError('lifecycle waits for readable ownership')
    for row in rows:
        if not row.get('Ended'):
            key = _m_jobs.job_key(row)
            from . import briefs as _m_briefs
            if not n['local'].get('workers', {}).get(key) and _m_briefs.saved_brief_inputs(n, row):
                result['restarts'].append(_m_worker.launch_worker(inst, n, key, rpc=rpc))
            else:
                result['restarts'].append(_m_worker.restart_worker(inst, n, key, rpc=rpc))
    result['settlement'] = settle_closed_jobs(inst, n, rpc=rpc)
    # Only rotate measured coordinators; ordinary file-only watcher reads retain
    # their existing contract and never call this native maintenance entry.
    from . import rotation as _m_rotation
    pending_rotation = _m_coordinator.resume_rotation(inst, n, rpc=rpc)
    if pending_rotation is not None:
        result['rotation'] = pending_rotation
    elif _m_rotation.rotation_due(inst, n) is not None:
        result['rotation'] = _m_coordinator.rotate_coordinator(inst, n, rpc=rpc)
    return result
