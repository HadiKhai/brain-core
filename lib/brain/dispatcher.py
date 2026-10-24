"""Bounded read-only root/operator ask plans. Printing never acknowledges delivery."""
import json

from . import common as _m_common
from . import nodes as _m_nodes
from . import status as _m_status


def dispatch_plan(inst, limit=50):
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError('dispatch limit must be between 1 and 200')
    # Only explicit requests, not advice/status/age/machine readings. Reuse the
    # shared inbox source projection and its stable episode IDs, without --new.
    asks = [item for item in _m_status.inbox_items(inst, reporter='root')
            if item['kind'] in ('inbox', 'blocked', 'blocked-overdue') and not item.get('informational')]
    asks = sorted({(item['node'], item['id']): item for item in asks}.values(), key=lambda i: (i['node'], i['id']))
    if not asks:
        return None
    return {'version': 1, 'bounded': True, 'partial': len(asks) > limit, 'limit': limit,
            'evidence': 'recorded files only; host activity unknown',
            'items': [{'episode_id': item['id'], 'target_node': item['node'], 'kind': item['kind'],
                       'text': item['text']} for item in asks[:limit]]}


def cmd_dispatch(a):
    try:
        plan = dispatch_plan(_m_nodes.load(), a.limit)
    except ValueError as error:
        _m_common.die(str(error))
    if plan:
        print(json.dumps(plan, ensure_ascii=False, sort_keys=True))
