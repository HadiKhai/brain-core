"""Declared launch policy and current-thread pointers, checked when acting.

Every value is instance data (the nearest node.md first, within its owner boundary, then
instance.md); the core ships no provider, model or account names. An undeclared key does
not restrict:

  launch_allow_<role>: <instanceId>/<model>, ...   role: coordinator or worker
  launch_effort_<role>: <effort>, ...
  launch_forbid: <instanceId or model>, ...
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
import subprocess

from . import common as _m_common

_m_jobs = _m_common.LazyModule(__package__, 'jobs')
_m_knowledge = _m_common.LazyModule(__package__, 'knowledge')
_m_launch_receipts = _m_common.LazyModule(__package__, 'launch_receipts')


LAUNCH_ROLES = ('coordinator', 'worker')
LAUNCH_TOOLS = ('t3_thread_launch', 'delegate_task')
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


def policy_declared(inst, n, role):
    return any(_m_common.lst(policy_value(inst, n, key)) for key in
               ('launch_allow_' + role, 'launch_effort_' + role, 'launch_forbid'))


def observed_reasons(inst, n, role, requested, thread, always=False):
    """Refusals for the thread a host returned, before it is bound, started or adopted.

    The host's thread observation must show the requested provider instance and model and
    pass the declared policy itself. Options the host reports must match the requested
    ones; a host that reports none leaves the checked request as the only effort on record.
    Without declared policy nothing is checked unless `always` is set.
    """
    if not (always or policy_declared(inst, n, role)):
        return []
    if not isinstance(thread, dict):
        return ['the launched thread could not be observed']
    instance, model = selection_identity(thread)
    if not instance or not model:
        return ['the launched thread reports no provider instance and model']
    want = selection_identity(requested)
    reasons = []
    if want != (None, None) and (instance, model) != want:
        reasons.append(f'the launched thread runs {instance}/{model}, not the requested {want[0]}/{want[1]}')
    observed = {'instanceId': instance, 'model': model}
    reported = thread.get('options')
    if reported is not None:
        try:
            reported = _m_launch_receipts.receipt_options(reported)
            asked = _m_launch_receipts.receipt_options(requested.get('options') or {}) if requested else {}
        except ValueError:
            return reasons + ['the launched thread reports unreadable options']
        different = sorted(k for k in asked if reported.get(k) != asked[k])
        if different:
            reasons.append('the launched thread reports other ' + ', '.join(different) + ' than requested')
        observed['options'] = reported
    elif isinstance(requested, dict) and 'options' in requested:
        observed['options'] = requested['options']
    return reasons + selection_reasons(inst, n, role, observed)


def require_observed(inst, n, role, requested, thread, refused):
    reasons = observed_reasons(inst, n, role, requested, thread)
    if reasons:
        raise ValueError('launch policy refused the launched thread (' + '; '.join(reasons) + '); ' + refused)


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


def pointer_thread(line):
    """Thread id from a pointer line `<Label>: <thread link or id>`; None if it isn't one."""
    match = re.fullmatch(r'[A-Za-z][\w -]{0,40}:[ \t]+(.+)', line.strip())
    if not match:
        return None
    value = match.group(1).strip()
    links = re.findall(r't3-thread://v1/([^\s)\]<>`/]+)', value) + \
        re.findall(r't3-thread://v2/[^\s)\]<>`/]+/([^\s)\]<>`/]+)', value)
    if len(links) == 1:
        return links[0]
    if links or any(c.isspace() for c in value):
        return None
    return value.strip('`') or None


def pointer_files(inst):
    """Every declared pointer as (label, path, usable); a missing or unsafe file is still listed."""
    names = _m_common.lst(inst.cfg.get('thread_pointers'))
    for node in inst.nodes():
        for name in names:
            path = node['dir'] / name
            yield node['id'] + '/' + name, path, not path.is_symlink() and path.is_file()


def _first_line(path):
    with path.open() as stream:
        return stream.readline(4096).rstrip('\n')


def _committed_pointers(inst, path, record):
    """Every line-1 thread the file named in a commit, read once per commit (`git cat-file --batch`)."""
    rel = os.path.relpath(path, inst.root)
    log = _m_common.git('log', '--format=%H', '--', rel, cwd=inst.root)
    shas = [s for s in (log.stdout.split() if log.returncode == 0 else []) if s not in record['commits']]
    if not shas:
        return
    batch = subprocess.run(['git', 'cat-file', '--batch'], cwd=inst.root, capture_output=True, timeout=20,
                           input=''.join(f'{sha}:{rel}\n' for sha in shas).encode())
    out, pos = batch.stdout, 0
    for sha in shas:
        end = out.index(b'\n', pos)
        header = out[pos:end].split()
        pos = end + 1
        if len(header) == 3:
            size = int(header[2])
            blob, pos = out[pos:pos + size], pos + size + 1
            thread = pointer_thread(blob.split(b'\n', 1)[0].decode(errors='replace')) if header[1] == b'blob' else None
            if thread:
                record['history'][thread] = record['history'].get(thread) or sha
        record['commits'].append(sha)
    record['commits'] = record['commits'][-500:]


def _observe_pointers(inst, data, committed=True):
    pointers = data.setdefault('pointers', {})
    current = {}
    for label, path, usable in pointer_files(inst):
        record = pointers.get(label)
        if record is None and not (usable or committed):
            continue  # never seen and not readable now: the send-time history read covers it
        record = record if record is not None else {}
        record.setdefault('history', {})
        record.setdefault('commits', [])
        if committed:
            _committed_pointers(inst, path, record)
        first = _first_line(path) if usable else None
        thread = pointer_thread(first) if usable else None
        if thread:
            record['history'][thread] = record['history'].get(thread) or 'seen ' + inst.now()
        if not (usable or record['history']):
            continue
        # A missing or unsafe file keeps its known history: its current target is unresolved.
        record.update(current=thread, line=first, missing=not usable)
        pointers[label] = record
        current[label] = record
    return current


def learn_pointers(inst):
    """Before any agent tool call, record what each pointer's line 1 names now.

    Every replacement path (Edit, Write, Codex apply_patch, a shell command, a script) runs
    as a tool call, so the line it replaces is on record, under the lock, before it runs.
    Committed history is read at send time, not here, to keep each call cheap.
    """
    if _m_common.lst(inst.cfg.get('thread_pointers')):
        with _state_store(inst, POINTER_FILE, {'version': 1, 'pointers': {}}) as data:
            _observe_pointers(inst, data, committed=False)


def pointer_send_reason(inst, arguments):
    """Refuse a send to a thread a pointer file named before but no longer names.

    Former pointers come from the file's committed history, plus each line 1 observed
    before any agent tool call or a send, so the first send after a replacement is refused too.
    """
    target = arguments.get('threadId') if isinstance(arguments, dict) else None
    if not _m_common.lst(inst.cfg.get('thread_pointers')) or not isinstance(target, str) or not target.strip():
        return None
    targets = _m_jobs.thread_reference_ids(target) | {target.strip()}
    with _state_store(inst, POINTER_FILE, {'version': 1, 'pointers': {}}) as data:
        records = _observe_pointers(inst, data)
    for label, record in records.items():
        if targets & {record['current']} or not targets & set(record['history']):
            continue
        if record.get('missing'):
            return (f'{label} is missing or not a regular file, and it named thread {target.strip()} before; '
                    'restore the pointer, then send to the thread its line 1 names')
        if not record['current']:
            return f'{label} line 1 does not name one thread; fix the pointer, then send to the thread it names'
        return (f'thread {target.strip()} is no longer the one {label} names; line 1 names the current one: '
                f'{record["line"].strip()} — send this message there instead')
    return None


def pretool_launch_reason(inst, n, data):
    """The PreToolUse refusal for a launch, delegation or send that breaks declared policy.

    Unreadable pointer state refuses the call (fail closed). State is only read when a
    pointer file is declared.
    """
    try:
        return _pretool_launch_reason(inst, n, data)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        return 'Launch policy state unavailable (' + str(error)[:200] + '); repair it before this call'


def _pretool_launch_reason(inst, n, data):
    tool = tool_base(data.get('tool_name'))
    args = data.get('tool_input')
    if tool != 't3_thread_send':
        try:
            learn_pointers(inst)
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            pass  # learning a pointer never blocks a call; the committed history still covers it
    if not isinstance(args, dict):
        return None
    if tool == 't3_thread_send':
        return pointer_send_reason(inst, args)
    if tool not in LAUNCH_TOOLS:
        return None
    if tool == 't3_thread_launch':
        selection, text = args.get('modelSelection'), args.get('message')
    else:
        target = args.get('target') if isinstance(args.get('target'), dict) else {}
        selection = {k: target[k] for k in ('providerInstanceId', 'model', 'options') if k in target}
        text = args.get('task')
    if not isinstance(text, str):
        text = ''
    reasons = selection_reasons(inst, n, launch_role(text), selection)
    return ('Launch policy refused this ' + tool + ': ' + '; '.join(reasons)) if reasons else None
