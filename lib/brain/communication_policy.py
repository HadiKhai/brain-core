"""Opt-in quiet notification delivery and recoverable, inbox-only morning briefs."""
import hashlib
import re

from . import communication as _m_communication
from . import transitions as _m_transitions


def quiet_now(inst):
    policy = inst.local_of('root').get('message_policy')
    if policy is None:
        return False
    if not isinstance(policy, dict) or set(policy) != {'quiet_start', 'quiet_end'}:
        raise ValueError('message_policy requires quiet_start and quiet_end')
    minutes = []
    for key in ('quiet_start', 'quiet_end'):
        value = policy[key]
        if not isinstance(value, str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', value):
            raise ValueError('quiet hours require HH:MM')
        hour, minute = map(int, value.split(':'))
        minutes.append(hour * 60 + minute)
    if minutes[0] == minutes[1] or inst.zone() is None:
        raise ValueError('quiet hours require distinct boundaries and a known instance timezone')
    now = inst.now_dt().astimezone(inst.zone())
    minute = now.hour * 60 + now.minute
    start, end = minutes
    return start <= minute < end if start < end else minute >= start or minute < end


def morning_brief(inst, n):
    """Publish one immutable daily snapshot in this node's inbox, never a wake.

    The inbox is the only supported delivery boundary here. This does not attest
    operator delivery through a host UI, messaging application or email service.
    Unpublished snapshots recover first; later notices stay available next time.
    """
    if inst.local_of('root').get('message_policy') is None:
        raise ValueError('morning brief requires an explicit message_policy')
    if quiet_now(inst):
        return {'state': 'quiet'}
    day = inst.now_dt().astimezone(inst.zone()).date().isoformat()
    with _m_communication.communication_store(inst) as data:
        briefs = data.setdefault('morning_briefs', {})
        pending = sorted((k, r) for k, r in briefs.items() if r['node'] == n['id'] and not r.get('published'))
        key = hashlib.sha256((n['id'] + '\0' + day).encode()).hexdigest()
        if pending:
            key, record = pending[0]
        elif key in briefs:
            return {'state': 'already-published', 'day': day}
        else:
            covered = {ident for b in briefs.values() for ident in b['items']}
            items = sorted(r['id'] for r in data['notices'].values()
                           if r['node'] == n['id'] and r.get('deferred') and r['id'] not in covered)
            if not items:
                return {'state': 'empty', 'day': day}
            text = 'Morning brief ' + day + ':\n' + ''.join('  - ' + data['notices'][i]['text'].replace('\n', ' ') + '\n' for i in items)
            record = {'node': n['id'], 'day': day, 'items': items, 'text': text}
            briefs[key] = record
        record = dict(record)
    marker = '<!-- morning-brief:' + key + ' -->'
    _m_transitions.inbox_append(n['dir'], '- ' + record['text'] + '  ' + marker + '\n', marker)
    with _m_communication.communication_store(inst) as data:
        data['morning_briefs'][key]['published'] = inst.now()
    return {'state': 'published', 'day': record['day'], 'items': record['items']}


def morning_items(inst, nodes):
    """Read-only due visibility, deliberately excluded from the wake collection."""
    try:
        unavailable = inst.local_of('root').get('message_policy') is None or quiet_now(inst)
    except ValueError:
        return []  # an invalid notice policy must never interrupt question collection
    if unavailable:
        return []
    data = _m_communication.communication_read(inst)
    briefs = data.get('morning_briefs', {})
    published = {i for b in briefs.values() if b.get('published') for i in b['items']}
    result = []
    for n in nodes:
        count = sum(r['node'] == n['id'] and r.get('deferred', False) and r['id'] not in published
                    for r in data['notices'].values())
        if count:
            result.append({'node': n['id'], 'kind': 'morning-brief',
                           'text': str(count) + ' retained quiet-hours notice(s); run brain morning-brief ' + n['id']})
    return result


def operator_brief(inst):
    """Aggregate retained notices into one root inbox snapshot per delivery day.

    This explicit local publication has no host transport. A recovery reserves
    the current day before appending, so an old snapshot and a fresh snapshot
    cannot both be published that day. Node inbox briefs remain independent.
    """
    if inst.local_of('root').get('message_policy') is None:
        raise ValueError('operator brief requires an explicit message_policy')
    if quiet_now(inst):
        return {'state': 'quiet'}
    day = inst.now_dt().astimezone(inst.zone()).date().isoformat()
    root = inst.node('root')
    allowed = {n['id'] for n in inst.nodes() if not n['archived']}
    with _m_communication.communication_store(inst) as data:
        briefs = data.setdefault('operator_briefs', {})
        today = [(k, r) for k, r in briefs.items() if r['delivery_day'] == day]
        pending = sorted((k, r) for k, r in briefs.items() if not r.get('published'))
        if today:
            key, record = today[0]
            if record.get('published'):
                return {'state': 'already-published', 'day': day}
        elif pending:
            key, record = pending[0]
            record['delivery_day'] = day
        else:
            covered = {i for b in briefs.values() for i in b['items']}
            notices = sorted((r for r in data['notices'].values()
                              if r['node'] in allowed and r.get('deferred') and r['id'] not in covered),
                             key=lambda r: (r['node'], r['id']))
            if not notices:
                return {'state': 'empty', 'day': day}
            key = hashlib.sha256(('operator\0' + day).encode()).hexdigest()
            text = 'Morning brief ' + day + ':\n' + ''.join(
                '  - ' + r['node'] + ': ' + r['text'].replace('\n', ' ') + '\n' for r in notices)
            record = {'day': day, 'delivery_day': day, 'items': [r['id'] for r in notices], 'text': text}
            briefs[key] = record
        record = dict(record)
    marker = '<!-- operator-brief:' + key + ' -->'
    _m_transitions.inbox_append(root['dir'], '- ' + record['text'] + '  ' + marker + '\n', marker)
    with _m_communication.communication_store(inst) as data:
        data['operator_briefs'][key]['published'] = inst.now()
    return {'state': 'published', 'day': record['day'], 'delivery_day': record['delivery_day'],
            'items': record['items']}


def cmd_morning_brief(a):
    from . import common, nodes
    import json
    try:
        inst = nodes.load()
        if getattr(a, 'operator', False):
            if a.id != 'root':
                raise ValueError('operator brief must target root')
            result = operator_brief(inst)
        else:
            result = morning_brief(inst, inst.node(a.id))
        print(json.dumps(result))
    except (OSError, ValueError) as error:
        common.die(str(error))
