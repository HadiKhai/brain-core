"""One bounded native restart step; uncertain host outcomes retain the obligation."""
import argparse
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path

from . import briefs as _m_briefs
from . import common as _m_common
from . import host_lifecycle as _m_host
from . import host_rpc as _m_rpc
from . import host_instances as _m_host_instances
from . import instance as _m_instance
from . import lifecycle_identity as _m_lifecycle_identity
from . import jobs as _m_jobs
from . import machine as _m_machine
from . import nodes as _m_nodes
from . import reminders as _m_reminders
from . import settlement as _m_settlement
from . import spend as _m_spend
from . import worktrees as _m_worktrees


def restart_brief(inst, n, row):
    # Native brief generation records replay inputs and routing. Reload while
    # owning its short publication lock so it cannot restore stale local state.
    with _m_instance.brain_lock(inst):
        fresh, current, live = restart_source(inst, n['id'], _m_jobs.job_key(row))
        if live is None:
            raise ValueError('job changed before native restart brief preparation')
        return _restart_brief(fresh, current, live)


def _restart_brief(inst, n, row):
    saved = _m_briefs.saved_brief_inputs(n, row)
    if not (saved.get('task') or saved.get('task_file')):
        raise ValueError('restart needs the recorded original task; no task invented')
    args = argparse.Namespace(id=n['id'], branch=row['Job'], small=False, import_study=False,
        task=None, task_file=None, context_file=None, why_separate=None, allow_unexplained=False,
        peer=None, runtime=None, model=None, job_class=None, effort=None, to=None, out=None,
        _instance=inst, _defer_restart_ack=True)
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        _m_briefs.cmd_brief(args)
    try:
        selection = json.loads(args.selection_line.removeprefix('modelSelection: '))
    except ValueError:
        raise ValueError('restart requires an explicit configured native model selection') from None
    if not isinstance(selection, dict) or not selection.get('instanceId') or not selection.get('model'):
        raise ValueError('restart requires an explicit configured native model selection')
    return output.getvalue(), selection


def captured_restart_usage(inst, n, row):
    usage = _m_spend.live_usage(inst, n, row)
    if (not isinstance(usage, dict) or type(usage.get('sessions')) is not int or usage['sessions'] < 1
            or not isinstance(usage.get('source'), str) or not usage['source'].startswith(('host', 'transcripts:'))
            or any(type(usage.get(k)) is not int or usage[k] < 0
                   for k in ('processed', 'compactions', 'active_minutes'))):
        raise ValueError('restart waits for a measured nonnegative captured usage snapshot')
    record = _m_reminders.lifecycle_read(inst)['usage'].get(_m_jobs.digest([n['id'], _m_jobs.job_key(row)]), {})
    baseline = record.get('baseline', {})
    if not isinstance(baseline, dict) or any(type(value) is not int or value < 0 or usage[k] < value
            for k, value in baseline.items() if k in ('processed', 'compactions', 'active_minutes')):
        raise ValueError('restart usage baseline is invalid or counters regressed')
    return dict(usage)


def restart_source(inst, nid, key):
    fresh = _m_instance.Instance(inst.root)
    n = fresh.node(nid)
    rows = _m_jobs.read_jobs(n)
    if rows is None:
        raise ValueError('restart waits for readable job ownership')
    matches = [row for row in rows if _m_jobs.job_key(row) == key]
    if len(matches) != 1 or matches[0].get('Ended'):
        return fresh, n, None
    row = matches[0]
    worktree = n['local'].get('worktrees', {}).get(row['Job'], {})
    if worktree and n.get('home_path'):
        base = _m_worktrees.integration_target(n)
        tip = _m_common.git('rev-parse', '--verify', '--quiet', 'refs/heads/' + row['Job'], cwd=n['home_path']).stdout.strip()
        if (tip and tip != worktree.get('start') and base and _m_common.git('merge-base', '--is-ancestor',
                'refs/heads/' + row['Job'], base, cwd=n['home_path']).returncode == 0):
            return fresh, n, None
    signal = _m_jobs.job_signal(fresh, n, row, strict=True)
    if signal and signal.get('restart') != 'compactions':
        return fresh, n, None  # terminal/blocked/question handbacks are not exhausted workers
    return fresh, n, row


def restart_workspace(n, row):
    workspace = n['local'].get('worktrees', {}).get(row['Job'], {}).get('path')
    if not workspace or not Path(workspace).is_dir() or Path(workspace).is_symlink():
        raise ValueError('restart worktree is absent or unsafe')
    branch = _m_common.git('symbolic-ref', '--quiet', '--short', 'HEAD', cwd=workspace)
    if branch.returncode or branch.stdout.strip() != row['Job']:
        raise ValueError('restart worktree branch changed')
    return workspace


def restart_bound(n, key, identity):
    reference = n['local'].get('workers', {}).get(key)
    return isinstance(reference, str) and _m_jobs.thread_reference_ids(reference) == {identity}


def restart_thread(rpc, identity):
    data = _m_host.host_object(rpc, 't3_thread_read', {'threadId': identity, 'limit': 1, 'runLimit': 1})
    thread = data.get('thread')
    if (not isinstance(thread, dict) or thread.get('threadId') != identity
            or thread.get('pendingRequestCount') != 0 or 'activeRunId' not in thread
            or thread.get('status') not in _m_settlement.SETTLEMENT_TERMINAL + ('running', 'starting', 'queued', 'preparing')):
        raise ValueError('restart waits for known thread activity without questions')
    pending = _m_host.host_object(rpc, 't3_pending_request_list', {'threadId': identity})
    if pending.get('requestIds') != []:
        raise ValueError('restart preserves pending host questions')
    return thread


def restart_children(n, identity, rpc):
    # Enumerate children FIRST. The native adapter has no reparent operation;
    # retain live/unknown children and wait rather than cancelling or abandoning.
    projects, inventory, callers = _m_host.lifecycle_inventory(n, rpc)
    if identity not in inventory:
        raise ValueError('restart thread missing from exhaustive host inventory')
    descendants, parents = set(), {identity}
    for _ in range(len(inventory) + 1):
        found = {tid for tid, thread in inventory.items() if thread.get('parentThreadId') in parents} - descendants
        if not found:
            break
        descendants.update(found)
        parents.update(found)
    for tid in sorted(descendants):
        if _m_host.terminal_host_thread(rpc, tid) is None:
            raise ValueError('restart waits for live or unknown delegated children; no supported reparent adapter')
    return projects, inventory, callers


def worker_host_rpc(inst, n, rpc):
    routed = _m_host_instances.routed_host_rpc(inst, n['id'], rpc)
    host = _m_lifecycle_identity.lifecycle_host_identity()
    def call(name, arguments):
        if not host or _m_lifecycle_identity.lifecycle_host_identity() != host:
            raise ValueError('worker host boot changed or unavailable; no native action taken')
        return routed(name, arguments)
    return call


def restart_worker(inst, n, job_key, rpc=None):
    """Execute at most one stop request or one launch; a later tick observes completion.

    The caller may schedule this explicit native maintenance entry. File-only
    due/watch/UI reads never execute host lifecycle actions.
    """
    rpc = worker_host_rpc(inst, n, rpc or _m_rpc.call)
    result = {'job': job_key, 'state': 'pending'}
    try:
        return _restart_worker(inst, n, job_key, rpc)
    except SystemExit:
        return {**result, 'reason': 'native brief refused restart; receipt retained'}
    except (OSError, ValueError, _m_rpc.HostRPCError) as error:
        return {**result, 'reason': str(error)}


def _restart_worker(inst, n, key, rpc):
    import re
    if not isinstance(key, str) or not re.fullmatch('[0-9a-f]{24}', key) or _m_common.outward_links(inst.root):
        raise ValueError('restart requires exact job ownership and safe local state')
    folder = inst.root / '.brain/wake'
    if folder.resolve() != inst.root.resolve() / '.brain/wake':
        raise ValueError('unsafe restart receipt directory')
    folder.mkdir(parents=True, exist_ok=True)
    identity = _m_jobs.digest([n['id'], key])
    path = folder / ('restart-' + identity + '.json')
    lock = path.with_suffix('.lock')
    _m_common.safe_target(path)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a+') as stream:
        if os.fstat(stream.fileno()).st_nlink != 1:
            raise ValueError('linked restart lock')
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'job': key, 'state': 'pending', 'reason': 'another native restart step owns this job'}
        def publish(receipt):
            _m_machine.machine_publish(inst, path, json.dumps(receipt, sort_keys=True) + '\n')
        fresh, current, row = restart_source(inst, n['id'], key)
        if row is None:
            return {'job': key, 'state': 'idle', 'reason': 'job is closed, unknown or has a terminal handback'}
        host = _m_lifecycle_identity.lifecycle_host_identity()
        if not host:
            raise ValueError('restart host identity unavailable; ownership unproven')
        receipt = json.loads(path.read_text()) if path.exists() and path.stat().st_size <= 262144 else None
        if path.exists() and (not isinstance(receipt, dict) or receipt.get('job') != key
                or receipt.get('phase') not in ('stopping', 'launching', 'launched', 'bound', 'done')):
            raise ValueError('restart receipt unreadable; retained for repair')
        if receipt is not None and receipt.get('host') != host:
            raise ValueError('restart receipt host changed or unknown; retain for reconciliation')
        if receipt is None or receipt['phase'] == 'done':
            need = _m_reminders.usage_restart_item(fresh, current, row)
            if need is None:
                return {'job': key, 'state': 'idle'}
            if receipt is not None:
                _m_machine.machine_publish(inst, path.with_name(path.stem + '-' + receipt['episode'] + '.done'),
                                           json.dumps(receipt, sort_keys=True) + '\n')
            old = current['local'].get('workers', {}).get(key)
            ids = _m_jobs.thread_reference_ids(old) if isinstance(old, str) else set()
            if len(ids) != 1:
                raise ValueError('restart needs one recorded worker thread')
            old = next(iter(ids))
            projects, _, callers = restart_children(current, old, rpc)
            if old in callers or old in _m_jobs.thread_reference_ids(current['local'].get('coordinator', '')):
                raise ValueError('restart cannot interrupt its caller or coordinator')
            captured_restart_usage(fresh, current, row)  # refuse before any stop if measurement is unknown
            restart_brief(fresh, current, row)  # validate resumable task and route before stopping
            thread = restart_thread(rpc, old)
            receipt = {'version': 1, 'job': key, 'host': host, 'episode': need['episode'], 'phase': 'stopping',
                       'old': old, 'run': thread['activeRunId'], 'at': inst.now()}
            publish(receipt)
        old = receipt['old']
        if (receipt['phase'] == 'launched' and restart_bound(current, key, receipt['new'])
                and old in current['local'].get('retired_workers', {}).get(key, [])):
            receipt['phase'] = 'bound'  # local publication succeeded before a receipt write failed
            publish(receipt)
        expected = receipt.get('new') if receipt['phase'] == 'bound' else old
        if not restart_bound(current, key, expected):
            raise ValueError('restart worker binding changed; no host action taken')
        if receipt['phase'] != 'bound':
            live_need = _m_reminders.lifecycle_read(fresh)['usage'].get(identity, {})
            if live_need.get('episode') != receipt['episode']:
                raise ValueError('restart obligation changed; retain the existing launch receipt')
        projects, inventory, callers = restart_children(current, old, rpc)
        if old in callers or old in _m_jobs.thread_reference_ids(current['local'].get('coordinator', '')):
            raise ValueError('restart cannot interrupt its caller or coordinator')
        thread = restart_thread(rpc, old)
        workspace = restart_workspace(current, row)
        if thread.get('worktreePath') != workspace or thread.get('branch') != row['Job']:
            raise ValueError('old worker workspace binding is unknown or changed')
        if thread['activeRunId'] is not None:
            if receipt['phase'] != 'stopping' or thread['activeRunId'] != receipt['run']:
                raise ValueError('restart found a different live run; preserve it')
            _, checked, live = restart_source(inst, n['id'], key)
            if live is None or not restart_bound(checked, key, old):
                raise ValueError('job ownership changed before stop request')
            _m_host.host_object(rpc, 't3_thread_interrupt', {'threadId': old, 'runId': receipt['run'],
                'reason': 'recorded job restart threshold', 'clientRequestId': 'restart-' + identity + '-' + receipt['episode']})
            return {'job': key, 'state': 'stopping'}
        if thread['status'] not in _m_settlement.SETTLEMENT_TERMINAL:
            raise ValueError('restart waits for confirmed old-worker termination')
        if receipt['phase'] == 'stopping':
            usage = captured_restart_usage(fresh, current, row)
            text, selection = restart_brief(fresh, current, row)
            marker = 'native-restart: ' + identity + '-' + receipt['episode']
            receipt.update(phase='launching', usage=usage, workspace=workspace, modelSelection=selection, marker=marker)
            # Launch has no idempotency key. Persist intent FIRST, never repeat an
            # unknown launch; exhaustive inventory can recover an exact marker.
            publish(receipt)
            _, checked, live = restart_source(inst, n['id'], key)
            if live is None or not restart_bound(checked, key, old) or restart_workspace(checked, live) != workspace:
                raise ValueError('job ownership changed before launch; intent retained without retry')
            restart_children(checked, old, rpc)
            if _m_host.terminal_host_thread(rpc, old) is None:
                raise ValueError('old worker changed before launch; no launch permitted')
            response = _m_host.host_object(rpc, 't3_thread_launch', {
                'title': row['Job'] + ' restart', 'projectId': current['local'].get('home_project') or projects[0],
                'workspaceStrategy': {'type': 'existing_worktree', 'worktreePath': workspace, 'branch': row['Job']},
                'modelSelection': selection, 'message': marker + '\n\n' + text})
            if not isinstance(response.get('threadId'), str) or not response['threadId'] or response['threadId'] in inventory:
                raise ValueError('restart launch outcome unknown; inspect native inventory before recovery')
            receipt.update(phase='launched', new=response['threadId'])
            publish(receipt)
        if receipt['phase'] == 'launching':
            matches = []
            for tid in sorted(inventory):
                if tid == old or tid in callers:
                    continue
                data = _m_host.host_object(rpc, 't3_thread_read', {'threadId': tid, 'afterPosition': 0, 'limit': 1,
                                                              'maxCharsPerItem': 50000})
                items = data.get('items', [])
                if items and items[0].get('createdBy') == 'user' and not items[0].get('textTruncated') and (
                        items[0].get('text', '').splitlines()[:1] == [receipt['marker']]):
                    matches.append(tid)
            if len(matches) != 1:
                raise ValueError('restart launch outcome unknown; no repeat launch permitted')
            receipt.update(phase='launched', new=matches[0])
            publish(receipt)
        replacement = restart_thread(rpc, receipt['new'])
        if (replacement.get('worktreePath') != receipt['workspace'] or replacement.get('branch') != row['Job']
                or replacement.get('providerInstanceId') != receipt['modelSelection']['instanceId']
                or replacement.get('model') != receipt['modelSelection']['model']
                or replacement.get('status') not in ('preparing', 'queued', 'starting', 'running', 'completed')):
            raise ValueError('replacement workspace/model binding not confirmed; old ownership retained')
        if receipt['phase'] == 'launched':
            with _m_instance.brain_lock(inst):
                latest, bound, live = restart_source(inst, n['id'], key)
                if live is None or not restart_bound(bound, key, old):
                    raise ValueError('job ownership changed before replacement binding')
                bound['local']['workers'][key] = receipt['new']
                bound['local'].setdefault('retired_workers', {}).setdefault(key, []).append(old)
                latest.save_local()
                receipt['phase'] = 'bound'
                publish(receipt)
        latest, bound, live = restart_source(inst, n['id'], key)
        if live is None or not restart_bound(bound, key, receipt['new']):
            raise ValueError('replacement binding changed before usage acknowledgement')
        _m_reminders.acknowledge_usage_restart(latest, bound, live, observed_usage=receipt['usage'])
        signal = _m_jobs.job_signal(latest, bound, live, strict=True)
        if signal and signal.get('restart') == 'compactions':
            bound['local']['worktrees'][live['Job']]['restart_ack'] = _m_jobs.digest([key, signal])
            latest.save_local()
        receipt['phase'] = 'done'
        publish(receipt)
        inst.local = latest.local
        return {'job': key, 'state': 'done', 'old_thread': old, 'thread': receipt['new']}


def launch_worker(inst, n, key, rpc=None):
    """Launch an unbound recorded job once, preserving its initial budget."""
    rpc = worker_host_rpc(inst, n, rpc or _m_rpc.call)
    try:
        import re
        if not isinstance(key, str) or not re.fullmatch('[0-9a-f]{24}', key) or _m_common.outward_links(inst.root):
            raise ValueError('launch requires exact job ownership and safe local state')
        folder = inst.root / '.brain/wake'
        if folder.resolve() != inst.root.resolve() / '.brain/wake':
            raise ValueError('unsafe launch receipt directory')
        folder.mkdir(parents=True, exist_ok=True)
        identity = _m_jobs.digest([n['id'], key])
        path = folder / ('launch-' + identity + '.json')
        _m_common.safe_target(path)
        fd = os.open(folder / ('restart-' + identity + '.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a+') as lock:
            if os.fstat(lock.fileno()).st_nlink != 1:
                raise ValueError('linked launch lock')
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fresh, current, row = restart_source(inst, n['id'], key)
            if row is None or current['local'].get('workers', {}).get(key):
                return {'job': key, 'state': 'idle'}
            from . import task_dispatch
            task_dispatch.validate_launch_job(fresh, current, row)
            workspace = restart_workspace(current, row)
            projects, inventory, callers = _m_host.lifecycle_inventory(current, rpc)
            def publish(value):
                _m_machine.machine_publish(inst, path, json.dumps(value, sort_keys=True) + '\n')
            host = _m_lifecycle_identity.lifecycle_host_identity()
            if not host:
                raise ValueError('launch host identity unavailable; ownership unproven')
            receipt = None
            if path.exists():
                if path.stat().st_size > 65536:
                    raise ValueError('launch receipt exceeds bound')
                receipt = json.loads(path.read_text())
                if (not isinstance(receipt, dict) or receipt.get('job') != key
                        or receipt.get('phase') not in ('launching', 'launched', 'done')
                        or receipt.get('workspace') != workspace or receipt.get('host') != host):
                    raise ValueError('invalid initial launch receipt; retained for repair')
                if receipt['phase'] == 'done':
                    raise ValueError('initial binding was removed after launch; no duplicate launch permitted')
            if receipt is None:
                # A missing binding is not proof that no outside worker exists.
                # Refuse unknown conversations already using this checkout.
                protected = set(callers)
                for node in fresh.nodes():
                    protected.update(_m_jobs.thread_reference_ids(node['local'].get('coordinator', '')))
                if len(inventory) > 256:
                    raise ValueError('launch inventory exceeds bounded ownership sweep')
                for tid in sorted(set(inventory) - protected):
                    value = _m_host.host_object(rpc, 't3_thread_read', {'threadId': tid, 'limit': 1})
                    thread = value.get('thread')
                    if not isinstance(thread, dict) or thread.get('threadId') != tid or 'worktreePath' not in thread:
                        raise ValueError('launch waits for known workspace ownership')
                    if thread['worktreePath'] == workspace:
                        raise ValueError('existing unbound conversation uses this workspace; adopt it explicitly')
                text, selection = restart_brief(fresh, current, row)
                marker = 'native-launch: ' + identity + '-' + os.urandom(12).hex()
                receipt = {'job': key, 'host': host, 'phase': 'launching', 'workspace': workspace,
                           'modelSelection': selection, 'marker': marker, 'at': inst.now()}
                publish(receipt)
                _, checked, live = restart_source(inst, n['id'], key)
                if live is None or checked['local'].get('workers', {}).get(key) or restart_workspace(checked, live) != workspace:
                    raise ValueError('job changed before initial launch; intent retained')
                task_dispatch.validate_launch_job(fresh.__class__(fresh.root), checked, live)
                response = _m_host.host_object(rpc, 't3_thread_launch', {'title': row['Job'],
                    'projectId': current['local'].get('home_project') or projects[0],
                    'workspaceStrategy': {'type': 'existing_worktree', 'worktreePath': workspace, 'branch': row['Job']},
                    'modelSelection': selection, 'message': marker + '\n\n' + text})
                new = response.get('threadId')
                if not isinstance(new, str) or not new or new in inventory:
                    raise ValueError('initial launch outcome unknown; no repeat permitted')
                receipt.update(phase='launched', new=new)
                publish(receipt)
            if receipt['phase'] == 'launching':
                matches = []
                for tid in sorted(set(inventory) - callers):
                    value = _m_host.host_object(rpc, 't3_thread_read', {'threadId': tid, 'afterPosition': 0,
                                                                   'limit': 1, 'maxCharsPerItem': 50000})
                    items = value.get('items', [])
                    if items and items[0].get('createdBy') == 'user' and not items[0].get('textTruncated') and (
                            items[0].get('text', '').splitlines()[:1] == [receipt['marker']]):
                        matches.append(tid)
                if len(matches) != 1:
                    raise ValueError('initial launch outcome unknown; no repeat permitted')
                receipt.update(phase='launched', new=matches[0])
                publish(receipt)
            replacement = restart_thread(rpc, receipt['new'])
            if (replacement.get('worktreePath') != workspace or replacement.get('branch') != row['Job']
                    or replacement.get('providerInstanceId') != receipt['modelSelection']['instanceId']
                    or replacement.get('model') != receipt['modelSelection']['model']
                    or replacement.get('status') not in ('preparing', 'queued', 'starting', 'running', 'completed')):
                raise ValueError('initial worker workspace/model binding is not confirmed')
            with _m_instance.brain_lock(inst):
                latest, bound, live = restart_source(inst, n['id'], key)
                if live is None or bound['local'].get('workers', {}).get(key) or restart_workspace(bound, live) != workspace:
                    raise ValueError('job changed before initial worker binding')
                bound['local'].setdefault('workers', {})[key] = receipt['new']
                latest.save_local()
                receipt['phase'] = 'done'
                publish(receipt)
                inst.local = latest.local
            return {'job': key, 'state': 'done', 'thread': receipt['new']}
    except (OSError, ValueError, _m_rpc.HostRPCError, SystemExit) as error:
        return {'job': key, 'state': 'pending', 'reason': str(error)}
