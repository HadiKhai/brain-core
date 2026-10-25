"""Explicit host provisioning through the host's supported native MCP transport.

Bindings and interrupted-operation markers are local to this machine. No host
lifecycle operation is performed unless the caller requests a provisioning action.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess

from . import common as _m_common
from . import hosts as _m_hosts
from . import instance as _m_instance
from . import knowledge as _m_knowledge
from . import nodes as _m_nodes
from . import schedules as _m_schedules


AUTOMATED = 'Automated turn: no operator present; cancellations are host events.'


def host_call(inst, tool, arguments):
    if inst.cfg.get('host') != 't3':
        raise ValueError('host provisioning requires a supported adapter (t3)')
    executable = os.environ.get('T3_ACP_MCP_NODE')
    if not executable:
        raise ValueError('native host MCP transport unavailable; use the host adapter workflow')
    command = [executable]
    if os.environ.get('T3_ACP_MCP_ENTRYPOINT'):
        command.append(os.environ['T3_ACP_MCP_ENTRYPOINT'])
    command += ['acp-mcp-call', tool, json.dumps(arguments, separators=(',', ':'))]
    result = subprocess.run(command, cwd=inst.root, env={**os.environ, 'ELECTRON_RUN_AS_NODE':'1'},
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError('native host call failed: ' + tool)
    value = json.loads(result.stdout)
    if not isinstance(value, dict) or value.get('isError'):
        raise ValueError('invalid host response: ' + tool)
    if isinstance(value.get('structuredContent'), dict):
        return value['structuredContent']
    content = value.get('content')
    if isinstance(content, list):
        for part in content:
            if part.get('type') == 'text':
                parsed = json.loads(part['text'])
                if isinstance(parsed, dict):
                    return parsed
        raise ValueError('host response has no result: ' + tool)
    return value


def _bind(inst, nid, **values):
    with _m_instance.brain_lock(inst):
        inst.local = inst._load_local()
        inst.node(nid)
        local = inst.local_of(nid)
        if any(local.get(key) not in (None, '', value) for key, value in values.items()):
            pending = local.setdefault('host_setup_pending', {'operation':'coordinator', 'started':inst.now()})
            pending.setdefault('returned_bindings', {}).update(values)
            inst.save_local()
            raise ValueError('host binding changed during provisioning; host result retained for reconciliation')
        local.update(values)
        inst.save_local()


def provision_coordinator(inst, nid, selection):
    n = inst.node(nid)
    if not selection.get('model') or not selection.get('instanceId') or selection['model'] == 'runtime default':
        raise ValueError('coordinator requires a configured model and provider instance')
    if n['sealed'] and not n.get('home_path'):
        raise ValueError('sealed node needs an external home before host provisioning')
    with _m_instance.brain_lock(inst):
        inst.local = inst._load_local()
        local = inst.local_of(nid)
        coordinator = local.get('coordinator')
        if not coordinator:
            if local.get('host_setup_pending'):
                raise ValueError('host setup pending; inspect the host and bind the resulting coordinator before retrying')
            local['host_setup_pending'] = {'started':inst.now(), 'operation':'coordinator'}
            inst.save_local()
    if not coordinator:
        local = inst.local_of(nid)
        if not local.get('host_project'):
            project = host_call(inst, 't3_project_create', {'title':n['fm'].get('title', nid),
                                'workspaceRoot':str(n['dir'].resolve()), 'createWorkspaceRootIfMissing':False})
            if not isinstance(project.get('id'), str) or not project['id']:
                raise ValueError('host project creation returned no id; inspect the pending operation')
            _bind(inst, nid, host_project=project['id'])
        if n['sealed'] and n.get('external') and not inst.local_of(nid).get('home_project'):
            project = host_call(inst, 't3_project_create', {'title':n['fm'].get('title', nid) + ' home',
                                'workspaceRoot':str(n['home_path'].resolve()), 'createWorkspaceRootIfMissing':False})
            if not isinstance(project.get('id'), str) or not project['id']:
                raise ValueError('home project creation returned no id; inspect the pending operation')
            _bind(inst, nid, home_project=project['id'])
        message = (AUTOMATED + '\nRole: coordinator of node ' + nid + '. Read the instance instructions, '
                   'core/roles/coordinator.md and every file in this node. Continue the request in brief.md. '
                   'Run brain host wake-setup ' + nid + ' from this coordinator to provision its wake binding.')
        launched = host_call(inst, 't3_thread_launch', {'title':n['fm'].get('title', nid) + ' · coordinator',
            'projectId':inst.local_of(nid)['host_project'], 'workspaceStrategy':{'type':'root'},
            'modelSelection':selection, 'message':message})
        coordinator = launched.get('threadId')
        if not isinstance(coordinator, str) or not coordinator:
            raise ValueError('host launch returned no thread id; inspect the pending operation')
        _bind(inst, nid, coordinator=coordinator)
    host_call(inst, 't3_thread_organize', {'threadId':coordinator, 'action':'pin'})
    with _m_instance.brain_lock(inst):
        inst.local = inst._load_local()
        if inst.local_of(nid).get('coordinator') != coordinator:
            raise ValueError('coordinator changed during pinning; reconcile the completed host action')
        inst.local_of(nid).pop('host_setup_pending', None)
        inst.save_local()
    return coordinator


def _coordinator_context(inst, n):
    coordinator = n['local'].get('coordinator')
    if not coordinator or host_call(inst, 't3_thread_configuration', {}).get('threadId') != coordinator:
        raise ValueError('run this host action inside the bound coordinator thread')
    return coordinator


def provision_wake(inst, nid):
    n = inst.node(nid)
    coordinator = _coordinator_context(inst, n)
    plan = _m_hosts.wake_setup_plan(inst, n)
    if plan.get('gap'):
        raise ValueError(plan['gap'])
    key = 'wake-' + hashlib.sha256(nid.encode()).hexdigest()
    with _m_instance.brain_lock(inst):
        schedules = _m_schedules.read_schedules(inst)
        existing = next((r for r in schedules if r['id'] == key), None)
        if existing and (existing.get('node') != nid or existing.get('when') != 'webhook'):
            raise ValueError('wake schedule definition conflicts; reconcile it before host setup')
        if not existing:
            path = inst.root / 'schedules.md'
            text = path.read_text() if path.exists() else _m_schedules.SCHEDULE_PREAMBLE
            text += _m_schedules.prepare_schedule(inst, key, 'webhook', nid, AUTOMATED + ' Run brain due ' + nid + '.')
            _m_common.atomic_write(path, text)
            inst.commit([path], 'Add portable coordinator wake schedule')
    identity = hashlib.sha256((nid + '\0' + coordinator).encode()).hexdigest()
    arguments = {**plan['host_call'], 'clientRequestId':'wake-' + identity}
    arguments['prompt'] = AUTOMATED + '\n' + arguments['prompt']
    result = host_call(inst, 'schedule_task', arguments)
    sid = result.get('scheduledTaskId')
    if result.get('boundThreadId') != coordinator or not isinstance(sid, str) or not sid:
        raise ValueError('host schedule did not confirm the bound coordinator; no wake binding written')
    with _m_instance.brain_lock(inst):
        inst.local = inst._load_local()
        # A binding rotation while the host request ran must not publish a stale URL.
        if inst.local_of(nid).get('coordinator') != coordinator:
            inst.local.setdefault('host_unbound_tasks', {})[identity] = {'task':sid, 'coordinator':coordinator}
            inst.save_local()
            raise ValueError('coordinator changed during host setup; host task retained for reconciliation')
        inst.local.setdefault('schedules', {})[key] = sid
        inst.local.setdefault('host_wake_tasks', {})[coordinator] = sid
        inst.save_local()
        url = result.get('webhookUrl')
        if not isinstance(url, str) or not url.startswith('https://'):
            raise ValueError('enable T3 Connect remote access; host task retained without a wake URL')
        inst.local_of(nid)['wake'] = url
        inst.save_local()
    return sid


def watch_change(inst, nid, url):
    n = inst.node(nid)
    coordinator = _coordinator_context(inst, n)
    result = host_call(inst, 'watch_pull_request', {'url':url, 'threadId':coordinator})
    if result.get('watching') is not True:
        raise ValueError('host did not confirm pull-request watch')
    with _m_instance.brain_lock(inst):
        inst.local = inst._load_local()
        if inst.local_of(nid).get('coordinator') != coordinator:
            key = hashlib.sha256((coordinator + '\0' + url).encode()).hexdigest()
            inst.local.setdefault('host_unbound_watches', {})[key] = {'coordinator':coordinator, 'url':url}
            inst.save_local()
            raise ValueError('coordinator changed during watch setup; watch retained for reconciliation')
    return result


def schedule_trigger(when):
    if when == 'hourly':
        return {'type':'interval', 'everyMs':3600000}
    interval = re.fullmatch(r'every ([1-9][0-9]*) minutes', when)
    if interval:
        return {'type':'interval', 'everyMs':int(interval[1]) * 60000}
    clock = re.fullmatch(r'(daily|weekdays) ([0-2][0-9]:[0-5][0-9])', when)
    if clock and int(clock[2][:2]) < 24:
        result = {'type':'fixed_time', 'timeOfDay':clock[2]}
        if clock[1] == 'weekdays':
            result['weekdays'] = [1, 2, 3, 4, 5]
        return result
    raise ValueError('host schedule supports hourly, every N minutes, daily HH:MM or weekdays HH:MM')


def provision_schedule(inst, nid, sid, provider=None):
    n = inst.node(nid)
    coordinator = _coordinator_context(inst, n)
    record = next((row for row in _m_schedules.read_schedules(inst) if row['id'] == sid), None)
    target = _m_instance.resolve_ref(inst.nodes(), record.get('node')) if record else None
    if not record or not target or target['id'] != nid or not record.get('brief'):
        raise ValueError('schedule must exist and target this node')
    if provider is not None and provider not in inst.runtimes:
        raise ValueError('provider must be an instance runtime')
    trigger = schedule_trigger(record.get('when', ''))
    definition = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
    with _m_instance.brain_lock(inst):
        inst.local = inst._load_local()
        bound = inst.local.get('schedules', {}).get(sid)
        receipt = inst.local.get('host_schedule_receipts', {}).get(sid, {})
        if bound:
            if receipt == {'definition':definition, 'coordinator':coordinator, 'task':bound, 'provider':provider}:
                return bound
            raise ValueError('existing host schedule differs or has no receipt; reconcile it before provisioning')
    prompt = record['brief']
    if not prompt.startswith(AUTOMATED):
        prompt = AUTOMATED + '\n' + prompt
    key = hashlib.sha256((nid + '\0' + sid + '\0' + coordinator + '\0' + definition).encode()).hexdigest()
    result = host_call(inst, 'schedule_task', {'title':sid, 'prompt':prompt, 'schedule':trigger,
                       'bindToCurrentThread':True, 'clientRequestId':'schedule-' + key})
    task = result.get('scheduledTaskId')
    if result.get('boundThreadId') != coordinator or not isinstance(task, str) or not task:
        raise ValueError('host schedule did not confirm the bound coordinator')
    with _m_instance.brain_lock(inst):
        inst.local = inst._load_local()
        current = next((row for row in _m_schedules.read_schedules(inst) if row['id'] == sid), None)
        if (current != record or inst.local_of(nid).get('coordinator') != coordinator or
                inst.local.get('schedules', {}).get(sid) not in (None, '', task)):
            inst.local.setdefault('host_unbound_tasks', {})[key] = {'task':task, 'coordinator':coordinator}
            inst.save_local()
            raise ValueError('schedule changed during host setup; returned host task retained for reconciliation')
        inst.local.setdefault('schedules', {})[sid] = task
        inst.local.setdefault('host_schedule_receipts', {})[sid] = {
            'definition':definition, 'coordinator':coordinator, 'task':task, 'provider':provider}
        if provider:
            inst.local.setdefault('schedule_providers', {})[sid] = provider
        else:
            inst.local.get('schedule_providers', {}).pop(sid, None)
        inst.save_local()
    return task


def cmd_host(a):
    inst = _m_nodes.load()
    try:
        if inst.cfg.get('host') != 't3' or not os.environ.get('T3_ACP_MCP_NODE'):
            raise ValueError('native host MCP transport unavailable; use the host adapter workflow')
        if a.action == 'wake-setup':
            provision_wake(inst, a.id)
            print('host wake task bound to this coordinator; capability saved privately')
        elif a.action == 'schedule':
            if not a.schedule_id:
                raise ValueError('host schedule requires --schedule-id')
            provision_schedule(inst, a.id, a.schedule_id, a.provider)
            print('host recurring task bound to this coordinator')
        elif a.action == 'watch':
            if not a.url:
                raise ValueError('host watch requires --url')
            watch_change(inst, a.id, a.url)
            print('host pull-request watch active')
        else:
            n = inst.node(a.id)
            route = argparse.Namespace(runtime=n['fm'].get('runtime'), model=a.model, branch=None,
                                       job_class='build', effort=None)
            _m_knowledge.worker_route(inst, n, route)
            selection = json.loads(route.selection_line.partition(': ')[2]) if route.model != 'runtime default' else {}
            coordinator = provision_coordinator(inst, a.id, selection)
            print('coordinator: ' + inst.thread_link(coordinator))
    except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired) as error:
        _m_common.die(str(error) if isinstance(error, ValueError) else 'host operation interrupted; inspect pending host state')
