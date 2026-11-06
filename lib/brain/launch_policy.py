"""Declared launch policy, provider admission and current-thread pointers, checked when acting.

Every value is instance data (the nearest node.md first, within its owner boundary, then
instance.md); the core ships no provider, model or account names. An undeclared key does
not restrict:

  launch_allow_<role>: <instanceId>/<model>, ...   role: coordinator or worker
  launch_effort_<role>: <effort>, ...
  launch_forbid: <instanceId or model>, ...
  launch_cap_<instanceId>: <n>    admitted non-coordinator launches on that provider at once
  thread_pointers: <file>, ...    (instance.md) node files whose line 1 names a role's current thread

Agent tool calls are refused in the PreToolUse hook, before the host call. Core launch
paths call the same checks before their own host calls. The role of a launch is read only
from structure the core writes (a rotation marker or a leading `Role:` field); a launch
without one is a worker launch, the default role. Its text is never interpreted.
"""
import contextlib
import fcntl
import json
import os
import re

from . import common as _m_common

_m_jobs = _m_common.LazyModule(__package__, 'jobs')
_m_knowledge = _m_common.LazyModule(__package__, 'knowledge')
_m_spend = _m_common.LazyModule(__package__, 'spend')


LAUNCH_ROLES = ('coordinator', 'worker')
LAUNCH_TOOLS = ('t3_thread_launch', 'delegate_task')
ADMISSION_FILE = '.brain/launch-admissions.json'
POINTER_FILE = '.brain/thread-pointers.json'
STATE_MAX_BYTES = 256 * 1024


def tool_base(name):
    """`mcp__t3-code__t3_thread_send` and runtime-dotted names share the host tool's own name."""
    if not isinstance(name, str):
        return None
    return name.rsplit('__', 1)[-1].rsplit('.', 1)[-1]


def policy_value(inst, n, key):
    for _, cfg in _m_knowledge.routing_node_configs(n):
        if cfg.get(key):
            return cfg[key]
    return inst.cfg.get(key)


def launch_role(text):
    for line in (text or '').splitlines()[:40]:
        if line.startswith('native-rotation: '):
            return 'coordinator'
        if line.startswith('Role: '):
            return 'coordinator' if line[len('Role: '):].startswith('coordinator') else 'worker'
    return 'worker'


def selection_effort(selection):
    options = selection.get('options') if isinstance(selection, dict) else None
    if isinstance(options, list):
        options = {o.get('id'): o.get('value') for o in options if isinstance(o, dict)}
    if not isinstance(options, dict):
        return None
    return options.get('reasoningEffort', options.get('effort'))


def selection_identity(selection):
    if not isinstance(selection, dict):
        return None, None
    instance = selection.get('instanceId', selection.get('providerInstanceId'))
    model = selection.get('model')
    return (instance if isinstance(instance, str) and instance else None,
            model if isinstance(model, str) and model else None)


def selection_reasons(inst, n, role, selection):
    """Refusals for one explicit launch selection; empty when the declared policy allows it."""
    allowed = _m_common.lst(policy_value(inst, n, 'launch_allow_' + role))
    efforts = _m_common.lst(policy_value(inst, n, 'launch_effort_' + role))
    forbidden = set(_m_common.lst(policy_value(inst, n, 'launch_forbid')))
    if not (allowed or efforts or forbidden):
        return []
    instance, model = selection_identity(selection)
    if not instance or not model:
        return [f'{role} launch needs an explicit provider instance and model; an inherited model cannot be checked']
    reasons = []
    if forbidden.intersection((instance, model)):
        reasons.append(f'{instance}/{model} is forbidden for every launch (launch_forbid)')
    if allowed and instance + '/' + model not in allowed:
        reasons.append(f'{instance}/{model} is not an allowed {role} model (launch_allow_{role}: {", ".join(allowed)})')
    effort = selection_effort(selection)
    if efforts and effort not in efforts:
        reasons.append(f'{role} effort {effort or "unset"} is not allowed (launch_effort_{role}: {", ".join(efforts)})')
    return reasons


def require_selection(inst, n, role, selection):
    reasons = selection_reasons(inst, n, role, selection)
    if reasons:
        raise ValueError('launch policy refused: ' + '; '.join(reasons))


def coordinator_selection(inst, n, observed=None):
    """An explicit coordinator selection: the declared one first, else the predecessor's observed one."""
    allowed = _m_common.lst(policy_value(inst, n, 'launch_allow_coordinator'))
    efforts = _m_common.lst(policy_value(inst, n, 'launch_effort_coordinator'))
    if allowed:
        if '/' not in allowed[0]:
            raise ValueError('launch_allow_coordinator entries are <instanceId>/<model>')
        instance, model = allowed[0].split('/', 1)
        selection = {'instanceId': instance, 'model': model}
    else:
        instance, model = selection_identity(observed)
        if not instance or not model:
            raise ValueError('coordinator launch needs an explicit model selection; none declared or observed')
        selection = {'instanceId': instance, 'model': model}
    if efforts:
        selection['options'] = {'reasoningEffort': efforts[0]}
    require_selection(inst, n, 'coordinator', selection)
    return selection


@contextlib.contextmanager
def _state_store(inst, name, empty):
    path = inst.root / name
    _m_common.safe_target(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path) + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a+') as lock:
        if os.fstat(lock.fileno()).st_nlink != 1:
            raise ValueError('unsafe launch policy lock')
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = dict(empty)
        if path.exists():
            if path.stat().st_size > STATE_MAX_BYTES:
                raise ValueError('launch policy state exceeds bound')
            data = json.loads(path.read_text())
            if not isinstance(data, dict) or data.get('version') != 1:
                raise ValueError('invalid launch policy state')
        before = json.dumps(data, sort_keys=True)
        yield data
        after = json.dumps(data, sort_keys=True)
        if after != before:
            if len(after.encode()) > STATE_MAX_BYTES:
                raise ValueError('launch policy state exceeds bound')
            _m_common.atomic_write(path, after + '\n', private=True)


def _open_job(inst, key):
    for node in inst.nodes():
        for row in _m_jobs.read_jobs(node) or []:
            if not row.get('Ended') and _m_jobs.job_key(row) == key:
                return node, row
    return None, None


def _slot_live(inst, slot):
    node, row = _open_job(inst, slot.get('job'))
    if row is None:
        return False
    handback = _m_jobs.read_handback(node, row)
    return not (isinstance(handback, dict) and handback.get('state'))


def admission_reason(inst, n, role, selection, text, kind):
    """Admit one launch on a capped provider, or say why not. Coordinators are never counted.

    Only an ordered deliverable is admitted: the brief carries the job mark of an open
    ledger job. A slot is held per job and kind until that job ends or hands back, so a
    relaunch of the same job reuses its slot and the cap counts live work, not attempts.
    """
    instance, _ = selection_identity(selection)
    cap = policy_value(inst, n, 'launch_cap_' + instance) if instance else None
    if not cap or role == 'coordinator':
        return None
    try:
        limit = int(cap)
        if limit < 0:
            raise ValueError
    except ValueError:
        return f'launch_cap_{instance} must be a non-negative integer'
    found = _m_spend.JOB_MARK_RE.search((text or '').encode())
    key = found.group(1).decode() if found else None
    owner, row = _open_job(inst, key) if key else (None, None)
    if row is None:
        return (f'{instance} admits only ordered deliverables: the brief must carry the job mark '
                f'(brain-job: <key>) of an open ledger job')
    slot_id = key + ':' + kind
    with _state_store(inst, ADMISSION_FILE, {'version': 1, 'slots': {}}) as data:
        slots = data.setdefault('slots', {})
        for sid in [s for s, slot in slots.items() if not _slot_live(inst, slot)]:
            del slots[sid]
        if slot_id in slots:
            return None
        held = sorted(s for s, slot in slots.items() if slot.get('instance') == instance)
        if len(held) >= limit:
            names = ', '.join(slots[s]['node'] + ':' + slots[s]['branch'] for s in held)
            return f'{instance} cap {limit} is full ({names}); wait for one to end or hand back'
        slots[slot_id] = {'instance': instance, 'job': key, 'kind': kind, 'node': owner['id'],
                          'branch': row['Job'], 'at': inst.now()}
    return None


def require_admission(inst, n, role, selection, text, kind='thread'):
    reason = admission_reason(inst, n, role, selection, text, kind)
    if reason:
        raise ValueError('launch policy refused: ' + reason)


def pointer_thread(line):
    """Thread id from a pointer line `<Label>: <thread link or id>`; None if it isn't one."""
    match = re.fullmatch(r'[A-Za-z][\w -]{0,40}:[ \t]+(.+)', line.strip())
    if not match:
        return None
    value = match.group(1).strip()
    links = re.findall(r't3-thread://v1/([^\s)\]<>`/]+)\)?', value) + \
        re.findall(r't3-thread://v2/[^\s)\]<>`/]+/([^\s)\]<>`/]+)\)?', value)
    if len(links) == 1:
        return links[0]
    if links or any(c.isspace() for c in value):
        return None
    return value.strip('`') or None


def pointer_send_reason(inst, arguments):
    """Refuse a send to a thread a pointer file no longer names; learn each pointer change."""
    files = _m_common.lst(inst.cfg.get('thread_pointers'))
    target = arguments.get('threadId') if isinstance(arguments, dict) else None
    if not files or not isinstance(target, str) or not target.strip():
        return None
    targets = _m_jobs.thread_reference_ids(target) | {target.strip()}
    reason = None
    with _state_store(inst, POINTER_FILE, {'version': 1, 'pointers': {}}) as data:
        pointers = data.setdefault('pointers', {})
        for node in inst.nodes():
            for name in files:
                path = node['dir'] / name
                if path.is_symlink() or not path.is_file():
                    continue
                with path.open() as stream:
                    first = stream.readline(4096).rstrip('\n')
                label = node['id'] + '/' + name
                record = pointers.setdefault(label, {'current': None, 'line': None, 'retired': []})
                current = pointer_thread(first)
                if current and current != record['current']:
                    if record['current']:
                        record['retired'] = [r for r in record['retired'] if r['thread'] != current][-49:] + [
                            {'thread': record['current'], 'at': inst.now()}]
                    record.update(current=current, line=first)
                if targets & {current}:
                    continue
                if not current and targets & {record['current'], *[r['thread'] for r in record['retired']]}:
                    reason = reason or (f'{label} line 1 does not name one thread; fix the pointer, '
                                        'then send to the thread it names')
                retired = [r for r in record['retired'] if r['thread'] in targets]
                if current and retired:
                    reason = reason or (f'thread {target.strip()} was replaced at {retired[-1]["at"]}; '
                                        f'{label} line 1 names the current one: {first.strip()} — '
                                        'send this message there instead')
    return reason


def pretool_launch_reason(inst, n, data):
    """The PreToolUse refusal for a launch, delegation or send that breaks declared policy.

    Unreadable admission or pointer state refuses the call (fail closed): an unproven
    admission is not one. State is only read when a cap or pointer file is declared.
    """
    try:
        return _pretool_launch_reason(inst, n, data)
    except (OSError, ValueError, KeyError, TypeError) as error:
        return 'Launch policy state unavailable (' + str(error)[:200] + '); repair it before this call'


def _pretool_launch_reason(inst, n, data):
    tool = tool_base(data.get('tool_name'))
    args = data.get('tool_input')
    if not isinstance(args, dict):
        return None
    if tool == 't3_thread_send':
        return pointer_send_reason(inst, args)
    if tool not in LAUNCH_TOOLS:
        return None
    if tool == 't3_thread_launch':
        selection, text, kind = args.get('modelSelection'), args.get('message'), 'thread'
    else:
        target = args.get('target') if isinstance(args.get('target'), dict) else {}
        selection = {k: target[k] for k in ('providerInstanceId', 'model', 'options') if k in target}
        text, kind = args.get('task'), 'delegate'
    if not isinstance(text, str):
        text = ''
    role = launch_role(text)
    reasons = selection_reasons(inst, n, role, selection)
    if not reasons:
        reason = admission_reason(inst, n, role, selection, text, kind)
        reasons = [reason] if reason else []
    return ('Launch policy refused this ' + tool + ': ' + '; '.join(reasons)) if reasons else None
