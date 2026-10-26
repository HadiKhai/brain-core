"""Addressed host inventory and routing, kept exclusively in machine-local state.

An inventory entry describes bindings; it never registers a provider or grants
remote execution. The native bridge can address only the current machine.
"""
import copy
import json
import re

from . import instance as _m_instance


HOST_BINDING_FIELDS = frozenset(('host_project', 'home_project', 'coordinator'))
T3_HOST_CAPABILITIES = frozenset(('pin', 'watch_pull_request'))


def host_instance_inventory(inst):
    value = inst.local.get('host_instances', {})
    if not isinstance(value, dict):
        raise ValueError('invalid local host instance inventory')
    for alias, row in value.items():
        if (not isinstance(alias, str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', alias)
                or not isinstance(row, dict) or set(row) != {'machine', 'adapter', 'nodes'}
                or any(not isinstance(row[k], str) or not row[k].strip() for k in ('machine', 'adapter'))
                or not isinstance(row['nodes'], dict)):
            raise ValueError('invalid local host instance entry')
        for nid, bindings in row['nodes'].items():
            if (not isinstance(nid, str) or not nid or not isinstance(bindings, dict)
                    or set(bindings) - HOST_BINDING_FIELDS
                    or any(not isinstance(v, str) or not v.strip() for v in bindings.values())):
                raise ValueError('host inventory accepts project/coordinator bindings only')
    return copy.deepcopy(value)


def remember_host_instance(inst, alias, machine, adapter, bindings):
    """Add an immutable addressed descriptor, without changing active bindings."""
    entry = {'machine': machine, 'adapter': adapter, 'nodes': copy.deepcopy(bindings)}
    with _m_instance.brain_lock(inst):
        fresh = _m_instance.Instance(inst.root)
        inventory = host_instance_inventory(fresh)
        if alias in inventory and inventory[alias] != entry:
            raise ValueError('host instance already exists with conflicting ownership; reconcile explicitly')
        inventory[alias] = entry
        fresh.local['host_instances'] = inventory
        host_instance_inventory(fresh)
        fresh.save_local()
        inst.local = fresh.local


def host_route(inst, nid):
    local = inst.local_of(nid)
    alias = local.get('host_instance')
    if alias is None:
        return {'instance': None, 'state': 'local', 'adapter': inst.cfg.get('host'),
                'machine': inst.local['machine'],
                'bindings': {k: local[k] for k in HOST_BINDING_FIELDS if local.get(k)}}
    inventory = host_instance_inventory(inst)
    if not isinstance(alias, str) or alias not in inventory or nid not in inventory[alias]['nodes']:
        raise ValueError('selected host instance has no addressed node bindings')
    row = inventory[alias]
    return {'instance': alias, 'state': 'local' if row['machine'] == inst.local['machine'] else 'remote',
            'machine': row['machine'], 'adapter': row['adapter'], 'bindings': row['nodes'][nid]}


def require_local_host(inst, nid):
    route = host_route(inst, nid)
    if route['state'] != 'local':
        raise ValueError('remote host route: native execution must run on the addressed machine')
    if route['instance'] is not None:
        actual = {k: inst.local_of(nid)[k] for k in HOST_BINDING_FIELDS if inst.local_of(nid).get(k)}
        if route['adapter'] != inst.cfg.get('host') or route['bindings'] != actual:
            raise ValueError('selected host adapter/bindings changed; explicit reconciliation required')
    return route


def select_host_instance(inst, nid, alias):
    with _m_instance.brain_lock(inst):
        fresh = _m_instance.Instance(inst.root)
        fresh.node(nid)
        fresh.local_of(nid)['host_instance'] = alias
        route = host_route(fresh, nid)
        if route['state'] == 'local':
            require_local_host(fresh, nid)
        fresh.save_local()
        inst.local = fresh.local


def routed_host_rpc(inst, nid, rpc):
    """Fence each call against its route; validate current addressed bindings.

    Existing lifecycle owners may advance bindings within the same local
    descriptor. Legacy ownership checks remain with their existing consumers.
    """
    expected = require_local_host(_m_instance.Instance(inst.root), nid)
    expected['bindings'].pop('coordinator', None)
    def call(name, arguments):
        latest = require_local_host(_m_instance.Instance(inst.root), nid)
        latest['bindings'].pop('coordinator', None)
        if latest != expected:
            raise ValueError('host route or binding changed before native call')
        return rpc(name, arguments)
    return call


def set_owned_host_binding(inst, nid, key, before, after):
    """Caller owns the writer lock and its lifecycle transition evidence."""
    if key not in HOST_BINDING_FIELDS or not isinstance(after, str) or not after:
        raise ValueError('invalid owned host binding update')
    route = require_local_host(inst, nid)
    if inst.local_of(nid).get(key) != before:
        raise ValueError('host binding changed before owned transition')
    inst.local_of(nid)[key] = after
    if route['instance'] is not None:
        inst.local['host_instances'][route['instance']]['nodes'][nid][key] = after


def require_host_capability(inst, nid, capability):
    route = require_local_host(inst, nid)
    supported = T3_HOST_CAPABILITIES if route['adapter'] == 't3' else frozenset()
    if capability not in supported:
        state = 'unsupported' if route['adapter'] == 't3' else 'unknown'
        raise ValueError(state + ' host capability: ' + capability)


def cmd_host_instance(a):
    from . import nodes as _m_nodes
    inst = _m_nodes.load()
    try:
        if a.action == 'remember':
            if not a.name or not a.machine or not a.adapter or not a.file:
                raise ValueError('remember needs name, --machine, --adapter and --file with node bindings')
            from pathlib import Path
            remember_host_instance(inst, a.name, a.machine, a.adapter, json.loads(Path(a.file).read_text()))
        elif a.action == 'route':
            if not a.name:
                raise ValueError('route needs an addressed host instance name')
            select_host_instance(inst, a.node, a.name)
        elif a.action == 'plan':
            route = host_route(inst, a.node)
            print(json.dumps({'state': route['state'], 'addressed': route['instance'] is not None,
                              'execution': 'local-only', 'auto_settle_control':
                              'unsupported' if route['adapter'] == 't3' else 'unknown'}, sort_keys=True))
            return
        else:
            # Ordinary output intentionally omits machine names and host IDs.
            rows = host_instance_inventory(inst)
            print(json.dumps({'instances': len(rows), 'execution': 'local-only'}))
            return
    except (OSError, ValueError) as error:
        from . import common
        common.die(str(error))
    print('host instance binding saved privately')
