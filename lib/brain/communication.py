"""Typed local notifications and decisions; never execute a selected action."""
import contextlib
import fcntl
import json
import os

from . import common as _m_common
from . import guard as _m_guard
from . import hosts as _m_hosts
from . import jobs as _m_jobs
from . import nodes as _m_nodes
from . import transitions as _m_transitions


COMMUNICATION_KINDS = ('question', 'landing', 'park', 'fyi')


def communication_read(inst):
    path = inst.root / '.brain/communications.json'
    _m_common.safe_target(path)
    if not path.exists():
        return {'version': 1, 'questions': {}, 'notices': {}}
    if path.stat().st_size > 1024 * 1024:
        raise ValueError('communication state exceeds bound')
    value = json.loads(path.read_text())
    if (not isinstance(value, dict) or value.get('version') != 1 or
            not isinstance(value.get('questions'), dict) or not isinstance(value.get('notices'), dict)):
        raise ValueError('invalid communication state; retained for repair')
    return value


@contextlib.contextmanager
def communication_store(inst):
    directory = inst.root / '.brain'
    directory.mkdir(exist_ok=True)
    path = directory / 'communications.lock'
    _m_common.safe_target(path)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a+') as lock:
        if os.fstat(lock.fileno()).st_nlink != 1:
            raise OSError('linked communication lock')
        fcntl.flock(lock, fcntl.LOCK_EX)
        value = communication_read(inst)
        before = json.dumps(value, sort_keys=True)
        yield value
        after = json.dumps(value, sort_keys=True)
        if len(after.encode()) > 1024 * 1024:
            raise ValueError('communication state exceeds bound')
        if after != before:
            _m_common.atomic_write(directory / 'communications.json', after + '\n')


def communication_question(inst, spec):
    if not isinstance(spec, dict) or not isinstance(spec.get('options'), list) or not 2 <= len(spec['options']) <= 10:
        raise ValueError('question requires two to ten typed options')
    ids = set()
    for option in spec['options']:
        if (not isinstance(option, dict) or not isinstance(option.get('id'), str) or not option['id'] or
                option['id'] in ids or not isinstance(option.get('label'), str) or not option['label'].strip() or
                option.get('action') not in ('routine', 'outward', 'destructive')):
            raise ValueError('question options need distinct ids, labels and explicit action categories')
        ids.add(option['id'])
    deadline = inst.parse_time(spec.get('deadline', '')) if isinstance(spec.get('deadline'), str) else None
    if (deadline is None or spec.get('recommendation') not in ids or
            spec.get('default') != spec.get('recommendation')):
        raise ValueError('question needs an absolute deadline and a default equal to its recommendation')
    return {'options': spec['options'], 'recommendation': spec['recommendation'],
            'default': spec['default'], 'deadline': deadline.isoformat()}


def communication_record(inst, n, kind, text, key, question=None):
    if kind not in COMMUNICATION_KINDS or not isinstance(key, str) or not key.strip() or not isinstance(text, str) or not text.strip():
        raise ValueError('notification requires a known kind, stable key and text')
    typed = communication_question(inst, question) if kind == 'question' else {}
    terms = _m_guard.never_terms(n)
    text = _m_nodes.import_text(text.strip(), terms)
    if typed:
        typed = {**typed, 'options': [{**o, 'label': _m_nodes.import_text(o['label'], terms)} for o in typed['options']]}
    identity = _m_hosts.wake_binding(json.dumps([n['id'], kind, key]))
    record = {'id': identity, 'node': n['id'], 'kind': kind, 'text': text, 'at': inst.now(), **typed}
    with communication_store(inst) as data:
        records = data['questions'] if typed else data['notices']
        previous = records.get(identity)
        if previous:
            fields = ('text', 'options', 'recommendation', 'default', 'deadline') if typed else ('text',)
            if any(previous.get(k) != record.get(k) for k in fields):
                raise ValueError('stable notification key already names different content')
            record = previous
        else:
            records[identity] = record
    marker = '<!-- communication:' + identity + ' -->'
    # Receipt first: interrupted inbox publication is recoverable by the next same-key call.
    _m_transitions.inbox_append(n['dir'], '- ' + inst.now() + ' from notification: ' + kind + ': ' + text + ' ' + marker + '\n', marker)
    if typed and 'answer' not in record:
        _m_hosts.request_wake(inst, n, 'question:' + identity,
                              items=[{'id': identity, 'kind': 'question'}])
    elif n['id'] != 'root':
        # Coordinator doorbells may include results; root's channel carries questions only.
        target, _ = _m_hosts.wake_target(inst, n)
        if target['id'] != 'root':
            _m_hosts.request_wake(inst, n, 'notification:' + identity,
                                  items=[{'id': identity, 'kind': 'notification'}], explicit=True)
    return record


def communication_reconcile(inst):
    """Record a routine choice at its deadline. No command/action executes here."""
    with communication_store(inst) as data:
        for q in data['questions'].values():
            spec = communication_question(inst, q)
            if ('answer' not in q and inst.now_dt() >= inst.parse_time(spec['deadline']) and
                    all(o['action'] == 'routine' for o in spec['options'])):
                q.update(answer=spec['default'], answered_by='deadline-default', answered_at=inst.now())


def communication_items(inst, nodes=None):
    allowed = {n['id'] for n in (nodes if nodes is not None else inst.nodes()) if not n['archived']}
    out = []
    for q in communication_read(inst)['questions'].values():
        communication_question(inst, q)
        if q.get('node') in allowed and 'answer' not in q:
            options = '; '.join(o['id'] + ': ' + o['label'] for o in q['options'])
            out.append({**q, 'text': q['text'] + ' Options: ' + options +
                        '. Recommendation/default: ' + q['recommendation'] + '. Deadline: ' + q['deadline']})
    return out


def communication_answer(inst, identity, choice):
    with communication_store(inst) as data:
        q = data['questions'].get(identity)
        if not q or choice not in {o['id'] for o in q['options']}:
            raise ValueError('unknown question or option')
        if 'answer' in q and q['answer'] != choice:
            raise ValueError('question already answered')
        q.update(answer=choice, answered_by='explicit-answer', answered_at=inst.now())


def cmd_notify(a):
    try:
        inst = _m_nodes.load()
        spec = json.loads(open(a.question_file).read()) if a.question_file else None
        record = communication_record(inst, inst.node(a.id), a.kind, a.text, a.key, spec)
        print(record['id'])
    except (OSError, ValueError) as error:
        _m_common.die(str(error))


def cmd_question_answer(a):
    try:
        communication_answer(_m_nodes.load(), a.id, a.choice)
        print('question answered')
    except ValueError as error:
        _m_common.die(str(error))
