"""Drain-local due read reuse, independent of wake delivery and consumption."""
import contextlib
from contextvars import ContextVar

from . import instance as _m_instance


_WAKE_READ_SCOPE = ContextVar('brain_wake_read_scope', default=None)


@contextlib.contextmanager
def wake_read_scope(inst):
    """One due inventory per drain; changed inputs retain work for the next run."""
    current = _WAKE_READ_SCOPE.get()
    if current is not None and current['root'] == inst.root:
        yield
        return
    token = _WAKE_READ_SCOPE.set({'root': inst.root, 'scanned': False})
    try:
        yield
    finally:
        _WAKE_READ_SCOPE.reset(token)


def wake_current_due_ids(inst):
    """Reuse known due identities only while their source and receipt fences hold.

    Binding, shown and terminal receipts still re-read on every pending file.
    A changed or unknown scan cannot retire work; it stays pending rather than
    rebuilding the entire tree for every remaining file in this drain.
    """
    from . import watch
    scope = _WAKE_READ_SCOPE.get()
    scope = scope if scope is not None and scope['root'] == inst.root else None
    fresh = _m_instance.Instance(inst.root)
    if scope is None or not scope['scanned']:
        if scope is not None:
            scope['scanned'] = True
            scope['ids'] = None
        groups = watch.watch_groups(fresh)
        if (getattr(groups, 'stale', False) or
                not all(g.get('episodes_known', True) for g in groups.values())):
            return None
        ids = {i['id'] for group in groups.values() for i in group['items']}
        if scope is None:
            return ids
        scope['layout'] = getattr(groups, 'source_layout', None) or {}
        scope['generation'] = getattr(groups, 'source_generation', None)
        scope['revision'] = getattr(groups, 'receipt_revision', None)
        scope['lifecycle'] = _m_instance.state_signature(fresh,
            extra=[fresh.root / '.brain/wake/lifecycle.json'])
        scope['ids'] = ids
    if scope['ids'] is None:
        return None
    if (scope['generation'] != watch.watch_source_signature(fresh, scope['layout']) or
            scope['revision'] != watch.watch_receipt_observed(fresh) or
            scope['lifecycle'] != _m_instance.state_signature(fresh,
                extra=[fresh.root / '.brain/wake/lifecycle.json'])):
        return None
    return scope['ids']

