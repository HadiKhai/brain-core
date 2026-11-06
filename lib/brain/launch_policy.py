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
import subprocess

from . import common as _m_common

_m_jobs = _m_common.LazyModule(__package__, 'jobs')
_m_knowledge = _m_common.LazyModule(__package__, 'knowledge')
_m_rotation = _m_common.LazyModule(__package__, 'rotation')
_m_spend = _m_common.LazyModule(__package__, 'spend')


LAUNCH_ROLES = ('coordinator', 'worker')
LAUNCH_TOOLS = ('t3_thread_launch', 'delegate_task')
LIVE_FILE = '.brain/live-threads.json'
POINTER_FILE = '.brain/thread-pointers.json'
STATE_MAX_BYTES = 256 * 1024
LIVE_STATUSES = ('preparing', 'queued', 'starting', 'running', 'waiting')
LIVE_SECONDS = 120


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


def coordinator_threads(inst):
    """Every thread bound as a coordinator, current or rotated out; these are never counted."""
    out = set()
    for node in inst.nodes():
        out |= _m_jobs.thread_reference_ids(node['local'].get('coordinator') or '') - {''}
        state = _m_rotation.read_rotation(inst, node) or {}
        for old in [state, *state.get('previous', [])]:
            if isinstance(old.get('coordinator'), str):
                out |= _m_jobs.thread_reference_ids(old['coordinator'])
    return out


def _live_rows(threads):
    rows = []
    for t in threads:
        if not isinstance(t, dict) or not isinstance(t.get('threadId'), str) or not t['threadId']:
            raise ValueError('host thread inventory has a thread without identity')
        if t.get('status') in LIVE_STATUSES:
            rows.append({'threadId': t['threadId'], 'providerInstanceId': t.get('providerInstanceId'),
                         'status': t['status'], 'title': str(t.get('title') or '')[:80]})
    return rows


def host_live_threads(rpc):
    """Exhaustive host inventory of live threads in every project, subagents included."""
    rows, seen, cursor, count = [], set(), None, 0
    for _ in range(20):
        args = {'allProjects': True, 'includeSubagents': True, 'statuses': list(LIVE_STATUSES), 'limit': 100}
        if cursor is not None:
            args['cursor'] = cursor
        page = rpc('t3_thread_list', args)
        if not isinstance(page, dict) or not isinstance(page.get('threads'), list) or type(page.get('total')) is not int:
            raise ValueError('incomplete host thread inventory; live capacity unknown')
        rows += _live_rows(page['threads'])
        count += len(page['threads'])
        cursor = page.get('nextCursor')
        if cursor is None:
            if count != page['total']:
                raise ValueError('host thread inventory count does not prove exhaustion')
            return rows
        if type(cursor) is not int or cursor in seen:
            raise ValueError('host thread inventory pagination did not advance')
        seen.add(cursor)
    raise ValueError('host thread inventory exceeds bound; live capacity unknown')


def observed_thread_list(arguments, response):
    """Live rows from one complete agent t3_thread_list call (PostToolUse), else None."""
    if not isinstance(arguments, dict) or not arguments.get('allProjects') or arguments.get('projectId'):
        return None
    if not arguments.get('includeSubagents') or arguments.get('cursor') not in (None, 0):
        return None
    statuses = arguments.get('statuses')
    if statuses is not None and not set(LIVE_STATUSES) <= set(statuses):
        return None
    if any(arguments.get(k) is not None for k in ('settled', 'snoozed', 'titleContains')):
        return None
    page = host_result(response)
    if (not isinstance(page, dict) or not isinstance(page.get('threads'), list) or page.get('nextCursor') is not None
            or type(page.get('total')) is not int or page['total'] != len(page['threads'])):
        return None
    return _live_rows(page['threads'])


def host_result(response):
    """A host tool result as the runtime reports it: structured content, one JSON text block, or the object."""
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except ValueError:
            return None
    if isinstance(response, list) and len(response) == 1 and isinstance(response[0], dict) and response[0].get('type') == 'text':
        return host_result(response[0].get('text'))
    if isinstance(response, dict):
        if isinstance(response.get('structuredContent'), dict):
            return response['structuredContent']
        if isinstance(response.get('content'), list):
            return host_result(response['content'])
    return response


def record_live_observation(inst, rows):
    with _state_store(inst, LIVE_FILE, {'version': 1}) as data:
        # Hook admissions before this count are in it; core admissions say when their launch returned.
        core = [a for a in data.get('admitted', []) if isinstance(a, dict) and a.get('via') == 'core']
        data.clear()
        data.update(version=1, observed_at=inst.now_dt().isoformat(), threads=rows, admitted=core)


def _reservations(inst, data, now):
    """Admissions at most LIVE_SECONDS old; older ones are dropped."""
    kept = []
    for a in data.get('admitted', []):
        at = inst.parse_time(a.get('at') or '') if isinstance(a, dict) else None
        if at and 0 <= (now - at).total_seconds() <= LIVE_SECONDS:
            kept.append(a)
    data['admitted'] = kept
    return kept


def admission_reason(inst, n, role, selection, text, kind, rpc=None, job=None):
    """The refusal for one launch on a capped provider, or None; see `admit`."""
    return admit(inst, n, role, selection, text, kind, rpc=rpc, job=job)[0]


def admit(inst, n, role, selection, text, kind, rpc=None, job=None):
    """Admit one launch on a capped provider: (refusal, None) or (None, reservation id or None).

    Capacity is live host threads on that provider (preparing to waiting, subagents
    included) minus bound or rotated-out coordinators, plus the one reservation list both
    paths share. Core paths count the host under the lock; the hook uses the latest complete
    agent observation if it is fresh. Every admission reserves a slot for LIVE_SECONDS: a
    core reservation stops counting in host counts once its launch returns (`admission_launched`),
    a hook one counts until a later observation (the hook never sees its launch return).
    Only an ordered deliverable is admitted: an open ledger job, named by the core or by the
    brief's job mark.
    """
    instance, _ = selection_identity(selection)
    cap = policy_value(inst, n, 'launch_cap_' + instance) if instance else None
    if not cap or role == 'coordinator':
        return None, None
    try:
        limit = int(cap)
        if limit < 0:
            raise ValueError
    except ValueError:
        return f'launch_cap_{instance} must be a non-negative integer', None
    if job is None:
        found = _m_spend.JOB_MARK_RE.search((text or '').encode())
        job = found.group(1).decode() if found else None
    owner, row = _open_job(inst, job) if job else (None, None)
    if row is None:
        return (f'{instance} admits only ordered deliverables: the brief must carry the job mark '
                f'(brain-job: <key>) of an open ledger job'), None
    excluded = coordinator_threads(inst)
    with _state_store(inst, LIVE_FILE, {'version': 1}) as data:
        now = inst.now_dt()
        admitted = _reservations(inst, data, now)
        if rpc is not None:
            threads = host_live_threads(rpc)
            # The host lists every launch that returned; the rest are still on their way.
            pending = [a for a in admitted if not a.get('launched_at')]
        else:
            at = inst.parse_time(data.get('observed_at') or '')
            age = (now - at).total_seconds() if at else None
            if age is None or not 0 <= age <= LIVE_SECONDS:
                return (f'{instance} cap {limit}: no fresh host count of live threads. Call t3_thread_list with '
                        f'allProjects, includeSubagents, statuses {list(LIVE_STATUSES)} and limit 100 (one complete '
                        f'page), then retry within {LIVE_SECONDS} s'), None
            threads = data.get('threads', [])
            # The observation holds only launches that returned before it was taken.
            pending = [a for a in admitted if not a.get('launched_at')
                       or (inst.parse_time(a['launched_at']) or now) >= at]
        live = sorted({t['threadId'] for t in threads if t.get('providerInstanceId') == instance} - excluded)
        pending = [a for a in pending if a.get('instance') == instance]
        if len(live) + len(pending) >= limit:
            names = ', '.join(live + ['admitted ' + a['branch'] for a in pending])
            return f'{instance} cap {limit} is full ({names}); wait for one to finish', None
        token = os.urandom(8).hex()
        admitted.append({'id': token, 'instance': instance, 'job': job, 'kind': kind, 'node': owner['id'],
                         'branch': row['Job'], 'via': 'hook' if rpc is None else 'core', 'at': now.isoformat()})
    return None, token


def require_admission(inst, n, role, selection, text, kind='thread', rpc=None, job=None):
    """Admit or raise; returns the reservation id (None when the provider is not capped)."""
    reason, token = admit(inst, n, role, selection, text, kind, rpc=rpc, job=job)
    if reason:
        raise ValueError('launch policy refused: ' + reason)
    return token


def _settle_admission(inst, token, launched):
    if not token:
        return
    with _state_store(inst, LIVE_FILE, {'version': 1}) as data:
        kept = []
        for a in data.get('admitted', []):
            if isinstance(a, dict) and a.get('id') == token:
                if not launched:
                    continue
                a = {**a, 'launched_at': inst.now_dt().isoformat()}
            kept.append(a)
        data['admitted'] = kept


def admission_launched(inst, token):
    """A core launch returned: host counts from now on list its thread."""
    _settle_admission(inst, token, True)


def admission_released(inst, token):
    """The host refused the launch: nothing started, the slot is free."""
    _settle_admission(inst, token, False)


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
    names = _m_common.lst(inst.cfg.get('thread_pointers'))
    for node in inst.nodes():
        for name in names:
            path = node['dir'] / name
            if not path.is_symlink() and path.is_file():
                yield node['id'] + '/' + name, path


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
        if len(header) == 3 and header[1] == b'blob':
            size = int(header[2])
            blob, pos = out[pos:pos + size], pos + size + 1
            thread = pointer_thread(blob.split(b'\n', 1)[0].decode(errors='replace'))
            if thread:
                record['history'][thread] = record['history'].get(thread) or sha
        record['commits'].append(sha)
    record['commits'] = record['commits'][-500:]


def _observe_pointers(inst, data, committed=True):
    pointers = data.setdefault('pointers', {})
    current = {}
    for label, path in pointer_files(inst):
        record = pointers.setdefault(label, {})
        record.setdefault('history', {})
        record.setdefault('commits', [])
        first = _first_line(path)
        thread = pointer_thread(first)
        if committed:
            _committed_pointers(inst, path, record)
        if thread:
            record['history'][thread] = record['history'].get(thread) or 'seen ' + inst.now()
        record.update(current=thread, line=first)
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
        if not record['current']:
            return f'{label} line 1 does not name one thread; fix the pointer, then send to the thread it names'
        return (f'thread {target.strip()} is no longer the one {label} names; line 1 names the current one: '
                f'{record["line"].strip()} — send this message there instead')
    return None


def pretool_launch_reason(inst, n, data):
    """The PreToolUse refusal for a launch, delegation or send that breaks declared policy.

    Unreadable admission or pointer state refuses the call (fail closed): an unproven
    admission is not one. State is only read when a cap or pointer file is declared.
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


def posttool_observe(inst, data):
    """PostToolUse: keep the latest complete host count of live threads for launch admission."""
    if tool_base(data.get('tool_name')) != 't3_thread_list':
        return
    rows = observed_thread_list(data.get('tool_input'), data.get('tool_response'))
    if rows is not None:
        record_live_observation(inst, rows)
