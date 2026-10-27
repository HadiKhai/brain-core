"""Two-sided native coordinator rotation; live children retain their parent."""
import argparse
import contextlib
import fcntl
import io
import json
import os
import re
from pathlib import Path

from . import common as _m_common
from . import host_lifecycle as _m_host
from . import host_rpc as _m_rpc
from . import host_instances as _m_host_instances
from . import instance as _m_instance
from . import jobs as _m_jobs
from . import machine as _m_machine
from . import rotation as _m_rotation
from . import settlement as _m_settlement
from . import worker_lifecycle as _m_worker


def rotation_packet(inst, n):
    with _m_instance.brain_lock(inst), contextlib.redirect_stdout(io.StringIO()):
        _m_rotation.cmd_rotate(argparse.Namespace(id=n['id'], _instance=inst))
    return (n['dir'] / 'handover.md').read_text()


def rotation_registry_children(inst, old, inventory, rpc):
    if not (inst.root / '.brain/host-tasks.json').exists():
        return
    try:
        from . import host_tasks as _m_tasks
    except ImportError:
        raise ValueError('rotation waits for the registered delegated-task adapter') from None
    registry = _m_tasks.host_task_read(inst)
    for key, row in registry['tasks'].items():
        if row['parent_thread'] != old:
            continue
        child, completion = row.get('childThreadId'), row.get('completion')
        if (not row.get('taskId') or not isinstance(completion, dict)
                or completion.get('status') not in _m_tasks.HOST_TASK_TERMINAL
                or completion.get('parent_thread') != old or not completion.get('run')
                or completion.get('node') != row['node'] or completion.get('task_key') != key
                or not re.fullmatch('[0-9a-f]{64}', str(completion.get('id')))
                or not isinstance(child, str) or child not in inventory):
            raise ValueError('rotation waits for registered delegated children; old-parent rows retained')
        thread = _m_host.terminal_host_thread(rpc, child)
        if thread is None or thread.get('latestRunId') != completion['run']:
            raise ValueError('rotation waits for current terminal delegated-child evidence')


def rotation_schedules(projects, rpc):
    tasks = {}
    for project in projects:
        result = _m_host.host_object(rpc, 'list_scheduled_tasks', {'projectId': project})
        if not isinstance(result.get('tasks'), list):
            raise ValueError('rotation waits for complete native schedule inventory')
        for task in result['tasks']:
            if (not isinstance(task, dict) or not isinstance(task.get('scheduledTaskId'), str)
                    or not task['scheduledTaskId'] or 'boundThreadId' not in task or task.get('projectId') != project):
                raise ValueError('rotation schedule ownership is unknown')
            identity = task['scheduledTaskId']
            if identity in tasks and tasks[identity] != task:
                raise ValueError('rotation schedule changed during inventory')
            tasks[identity] = task
    return tasks


@contextlib.contextmanager
def rotation_action_store(inst, n):
    if _m_common.outward_links(inst.root):
        raise ValueError('rotation refused: unsafe instance links')
    folder = inst.root / '.brain/wake'
    if folder.resolve() != inst.root.resolve() / '.brain/wake':
        raise ValueError('unsafe rotation action state')
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ('rotation-action-' + _m_jobs.digest(n['id']) + '.json')
    _m_common.safe_target(path)
    fd = os.open(path.with_suffix('.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a+') as lock:
        if os.fstat(lock.fileno()).st_nlink != 1:
            raise ValueError('linked rotation lock')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        receipt = None
        if path.exists():
            if path.stat().st_size > 65536:
                raise ValueError('rotation action receipt exceeds bound')
            receipt = json.loads(path.read_text())
            if (not isinstance(receipt, dict) or receipt.get('node') != n['id']
                    or receipt.get('phase') not in ('launching', 'launched', 'bound', 'done')
                    or not re.fullmatch('[0-9a-f]{24}', str(receipt.get('ticket')))
                    or not isinstance(receipt.get('tasks'), list) or not isinstance(receipt.get('old'), str)):
                raise ValueError('invalid rotation action receipt; retain for repair')
        def publish(value):
            _m_machine.machine_publish(inst, path, json.dumps(value, sort_keys=True) + '\n')
        yield receipt, publish


def rotate_coordinator(inst, n, rpc=None):
    rpc = _m_host_instances.routed_host_rpc(inst, n['id'], rpc or _m_rpc.call)
    try:
        with rotation_action_store(inst, n) as (receipt, publish):
            fresh = _m_instance.Instance(inst.root)
            current = fresh.node(n['id'])
            old = current['local'].get('coordinator')
            if not isinstance(old, str) or not old:
                raise ValueError('rotation needs a recorded coordinator')
            # FIRST native operation: exhaustive nested inventory, before any
            # handover, schedule change, launch, interruption or settlement.
            projects, inventory, callers = _m_worker.restart_children(current, old, rpc)
            rotation_registry_children(fresh, old, inventory, rpc)
            old_thread = _m_worker.restart_thread(rpc, old)
            if receipt is not None and receipt['phase'] != 'done':
                if receipt['old'] != old:
                    raise ValueError('rotation binding changed; preserve in-flight receipt')
                if receipt['phase'] == 'launching':
                    matches = []
                    for tid in sorted(inventory):
                        if tid == old:
                            continue
                        value = _m_host.host_object(rpc, 't3_thread_read', {'threadId': tid, 'afterPosition': 0,
                                                                      'limit': 1, 'maxCharsPerItem': 50000})
                        items = value.get('items', [])
                        if items and items[0].get('createdBy') == 'user' and not items[0].get('textTruncated') and (
                                items[0].get('text', '').splitlines()[:1] == ['native-rotation: ' + receipt['ticket']]):
                            matches.append(tid)
                    if len(matches) != 1:
                        raise ValueError('rotation launch outcome unknown; no repeat launch permitted')
                    receipt.update(phase='launched', new=matches[0])
                    publish(receipt)
                return {'state': receipt['phase'], 'ticket': receipt['ticket'], 'thread': receipt.get('new')}
            if _m_rotation.rotation_due(fresh, current) is None:
                return {'state': 'idle'}
            if old not in callers:
                raise ValueError('automatic rotation must launch from the current coordinator host context')
            tasks = rotation_schedules(projects, rpc)
            owned = [key for key, task in tasks.items() if task['boundThreadId'] == old]
            wake = current['local'].get('wake') or (current['local'].get('wake_default') if current['id'] == 'root' else None)
            if wake and not any(tasks[key].get('webhookUrl') == wake for key in owned):
                raise ValueError('rotation cannot rebind an unrecognized wake transport; old coordinator retained')
            packet = rotation_packet(fresh, current)
            workspace = old_thread.get('worktreePath')
            if not isinstance(workspace, str) or not Path(workspace).is_dir() or Path(workspace).is_symlink():
                raise ValueError('rotation requires the observed existing coordinator workspace')
            ticket = os.urandom(12).hex()
            receipt = {'version': 1, 'node': n['id'], 'phase': 'launching', 'old': old, 'ticket': ticket,
                       'tasks': owned, 'projects': projects, 'at': inst.now(),
                       'old_sessions': list(current['local'].get('coordinator_sessions', [])),
                       'workspace': workspace}
            publish(receipt)
            latest = _m_instance.Instance(inst.root).node(n['id'])
            if latest['local'].get('coordinator') != old:
                raise ValueError('coordinator changed before launch; intent retained')
            _m_worker.restart_children(latest, old, rpc)
            message = ('native-rotation: ' + ticket + '\n\n' + packet + '\n\n'
                'Before coordinating, run `brain lifecycle accept-rotation ' + n['id'] + ' --ticket ' + ticket +
                '` from this new thread. Supply --session-id only with this runtime\'s actual session identity '
                'if its native environment does not provide one. This command rebinds schedules from this '
                'thread, preserves all open/parked work and retains the old coordinator until safely terminal. '
                'Never cancel or abandon delegated children to rotate.\n')
            response = _m_host.host_object(rpc, 't3_thread_launch', {'title': n['id'] + ' coordinator',
                'projectId': current['local'].get('host_project') or projects[0], 'message': message,
                'workspaceStrategy': {'type': 'existing_worktree', 'worktreePath': workspace}})
            new = response.get('threadId')
            if not isinstance(new, str) or not new or new in inventory:
                raise ValueError('rotation launch outcome unknown; no repeat launch permitted')
            receipt.update(phase='launched', new=new)
            publish(receipt)
            return {'state': 'launched', 'ticket': ticket, 'thread': new}
    except (OSError, ValueError, _m_rpc.HostRPCError, SystemExit) as error:
        return {'state': 'pending', 'reason': str(error)}


def accept_rotation(inst, n, ticket, session_id, rpc=None):
    rpc = _m_host_instances.routed_host_rpc(inst, n['id'], rpc or _m_rpc.call)
    try:
        if not isinstance(session_id, str) or not session_id.strip() or any(c.isspace() for c in session_id):
            raise ValueError('rotation needs the new runtime actual session identity; none invented')
        with rotation_action_store(inst, n) as (receipt, publish):
            if not receipt or receipt['ticket'] != ticket:
                raise ValueError('rotation requires the exact confirmed launch ticket')
            fresh = _m_instance.Instance(inst.root)
            current = fresh.node(n['id'])
            old = receipt['old']
            projects, inventory, callers = _m_worker.restart_children(current, old, rpc)
            rotation_registry_children(fresh, old, inventory, rpc)
            if receipt['phase'] == 'launching':
                if len(callers) != 1 or old in callers:
                    raise ValueError('unconfirmed rotation launch; no caller identity invented')
                candidate = next(iter(callers))
                value = _m_host.host_object(rpc, 't3_thread_read', {'threadId': candidate, 'afterPosition': 0,
                                                               'limit': 1, 'maxCharsPerItem': 50000})
                items = value.get('items', [])
                if (not items or items[0].get('createdBy') != 'user' or items[0].get('textTruncated') or
                        items[0].get('text', '').splitlines()[:1] != ['native-rotation: ' + ticket]):
                    raise ValueError('replacement first-prompt launch identity is not confirmed')
                receipt.update(phase='launched', new=candidate)
                publish(receipt)
            new = receipt['new']
            if callers != {new} or new not in inventory:
                raise ValueError('rotation acceptance must run from the exact replacement host thread')
            replacement = _m_worker.restart_thread(rpc, new)
            if replacement.get('worktreePath') != receipt['workspace']:
                raise ValueError('replacement coordinator workspace is not confirmed')
            if current['local'].get('coordinator') not in (old, new):
                raise ValueError('coordinator binding changed; do not overwrite it')
            tasks = rotation_schedules(projects, rpc)
            if any(key not in tasks or tasks[key]['boundThreadId'] not in (old, new) for key in receipt['tasks']):
                raise ValueError('rotation schedule binding changed; old evidence retained')
            if any(task['boundThreadId'] == old and key not in receipt['tasks'] for key, task in tasks.items()):
                raise ValueError('new old-parent schedule appeared; preserve it for reconciliation')
            for key in receipt['tasks']:
                if tasks[key]['boundThreadId'] == old:
                    reply = _m_host.host_object(rpc, 'update_scheduled_task', {'scheduledTaskId': key,
                                                                          'bindToCurrentThread': True})
                    if reply.get('scheduledTaskId') != key or reply.get('boundThreadId') != new:
                        raise ValueError('host did not confirm replacement schedule binding')
            observed = rotation_schedules(projects, rpc)
            if any(observed.get(key, {}).get('boundThreadId') != new for key in receipt['tasks']):
                raise ValueError('replacement schedules not durably observed')
            with _m_instance.brain_lock(inst):
                latest = _m_instance.Instance(inst.root)
                bound = latest.node(n['id'])
                if bound['local'].get('coordinator') not in (old, new):
                    raise ValueError('coordinator changed before replacement binding')
                _m_host_instances.set_owned_host_binding(latest, n['id'], 'coordinator',
                                                        bound['local'].get('coordinator'), new)
                sessions = bound['local'].setdefault('coordinator_sessions', [])
                if session_id not in sessions:
                    sessions.append(session_id)
                latest.save_local()
                state = _m_rotation.read_rotation(latest, bound)
                if state is None:
                    raise ValueError('unreadable rotation history; previous coordinator retained')
                previous = list(state.get('previous', []))
                if not any(value.get('coordinator') == old and value.get('ticket') == ticket for value in previous):
                    previous.append({'coordinator': old, 'coordinator_sessions': receipt['old_sessions'],
                                     'ticket': ticket, 'settled': False})
                state.update(coordinator=new, coordinator_sessions=list(sessions), previous=previous)
                state.pop('pending_session', None)
                _m_common.atomic_write(_m_rotation.rotation_file(latest, bound), json.dumps(state, sort_keys=True) + '\n', private=True)
                receipt['phase'] = 'bound'
                receipt['session_id'] = session_id
                publish(receipt)
            old_thread = _m_host.terminal_host_thread(rpc, old)
            if old_thread is None:
                # A replacement may stop only the exact old run, after checking
                # children again. Questions and shared coordinator ownership stay
                # pending; no cancellation is inferred from elapsed time.
                _m_worker.restart_children(current, old, rpc)
                old_thread = _m_worker.restart_thread(rpc, old)
                if any(old in _m_jobs.thread_reference_ids(node['local'].get('coordinator', ''))
                       for node in _m_instance.Instance(inst.root).nodes()):
                    raise ValueError('old thread remains a bound coordinator elsewhere; preserve it')
                run = old_thread.get('activeRunId')
                if isinstance(run, str) and run:
                    _m_host.host_object(rpc, 't3_thread_interrupt', {'threadId': old, 'runId': run,
                        'clientRequestId': 'rotation-stop-' + ticket, 'reason': 'replacement coordinator bound'})
                return {'state': 'bound', 'reason': 'old coordinator stopping or pending; retain it until confirmed terminal'}
            if not old_thread.get('settled'):
                reply = _m_host.host_object(rpc, 't3_thread_organize', {'threadId': old, 'action': 'settle'})
                if reply.get('settlesWhenTurnEnds'):
                    rpc('t3_thread_organize', {'threadId': old, 'action': 'unsettle'})
                    raise ValueError('host self-settlement undone; old coordinator retained')
            _, observed, _ = _m_host.lifecycle_inventory(current, rpc)
            if observed.get(old, {}).get('settled') is not True or _m_host.terminal_host_thread(rpc, old) is None:
                raise ValueError('host did not confirm old coordinator settlement')
            receipt['phase'] = 'done'
            publish(receipt)
            state = _m_rotation.read_rotation(latest, bound)
            for value in state['previous']:
                if value.get('coordinator') == old and value.get('ticket') == ticket:
                    value['settled'] = True
            _m_common.atomic_write(_m_rotation.rotation_file(latest, bound), json.dumps(state, sort_keys=True) + '\n', private=True)
            inst.local = latest.local
            return {'state': 'done', 'thread': new, 'previous': old}
    except (OSError, ValueError, _m_rpc.HostRPCError, SystemExit) as error:
        return {'state': 'pending', 'reason': str(error)}


def resume_rotation(inst, n, rpc=None):
    """Resume durable acceptance/settlement even after the new session is below threshold."""
    rpc = _m_host_instances.routed_host_rpc(inst, n['id'], rpc or _m_rpc.call)
    try:
        with rotation_action_store(inst, n) as (receipt, _):
            if receipt is None or receipt['phase'] == 'done':
                return None
            saved = dict(receipt)
        if saved['phase'] == 'bound' and saved.get('session_id'):
            return accept_rotation(inst, n, saved['ticket'], saved['session_id'], rpc=rpc)
        return rotate_coordinator(inst, n, rpc=rpc)
    except (OSError, ValueError, _m_rpc.HostRPCError, SystemExit) as error:
        return {'state': 'pending', 'reason': str(error)}
