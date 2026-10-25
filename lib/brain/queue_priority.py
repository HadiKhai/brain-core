"""Explicit node-scoped reordering of queued native verify reservations."""

from . import common as _m_common
from . import handback_queue as _m_handback_queue
from . import machine as _m_machine
from . import nodes as _m_nodes


def move_verify_before(inst, n, token, before):
    if not token or token == before:
        raise ValueError('queue --move and --before need two distinct queued tokens')
    scope = _m_machine.machine_scope(inst, n['id'])
    with _m_machine.machine_queue() as queue:
        matches = [[row for row in queue.rows if row['token'] == key] for key in (token, before)]
        if any(len(found) != 1 for found in matches):
            raise ValueError('queue target missing or ambiguous; no reorder')
        moving, anchor = [found[0] for found in matches]
        if any(row['state'] != 'queued' or row['kind'] != 'verify' or row.get('scope') != scope
               for row in (moving, anchor)):
            raise ValueError('queue reorder requires queued verify tokens belonging to this node')
        queue.rows.remove(moving)
        queue.rows.insert(queue.rows.index(anchor), moving)


def cmd_gate_queue(a):
    if not getattr(a, 'move', None):
        if getattr(a, 'before', None):
            _m_common.die('--before requires --move')
        if a.action == 'run' and a.id:
            return _m_handback_queue.cmd_queue(a)
        _m_common.die('queue requires run NODE --once or --node NODE --move TOKEN --before TOKEN')
    if a.action or a.id or a.once or a.request_id or not a.before or not a.node:
        _m_common.die('queue reorder requires only --node NODE --move TOKEN --before TOKEN')
    inst = _m_nodes.load()
    try:
        move_verify_before(inst, inst.node(a.node), a.move, a.before)
    except (OSError, ValueError) as error:
        _m_common.die(str(error))
    print('queue: reordered queued verify token within this node; priority bands remain authoritative')
