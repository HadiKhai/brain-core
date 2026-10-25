"""Explicit host provisioning through the host's supported native MCP transport.

Bindings and interrupted-operation markers are local to this machine. No host
lifecycle operation is performed unless the caller requests a provisioning action.
"""
import argparse
import hashlib
import json
import os
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
            raise ValueError('host binding changed during provisioning; retain the host result for reconciliation')
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
            raise ValueError('coordinator changed during host setup; retain the host task for reconciliation')
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
    return result


def cmd_host(a):
    inst = _m_nodes.load()
    try:
        if a.action == 'wake-setup':
            provision_wake(inst, a.id)
            print('host wake task bound to this coordinator; capability saved privately')
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
