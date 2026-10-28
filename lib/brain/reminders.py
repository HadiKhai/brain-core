"""Bounded, machine-local request and reminder lifecycle receipts."""
import contextlib
import fcntl
import json
import pickle
import os
import re

from . import common as _m_common
from . import command_reads as _m_command_reads

_m_briefs = _m_common.LazyModule(__package__, 'briefs')
_m_briefs = _m_common.LazyModule(__package__, 'briefs')
_m_guard = _m_common.LazyModule(__package__, 'guard')
_m_history = _m_common.LazyModule(__package__, 'history')
_m_jobs = _m_common.LazyModule(__package__, 'jobs')
_m_attempts = _m_common.LazyModule(__package__, 'job_attempts')
_m_status = _m_common.LazyModule(__package__, 'status')
_m_machine = _m_common.LazyModule(__package__, 'machine')
_m_spend = _m_common.LazyModule(__package__, 'spend')


_USAGE_NOT_OBSERVED = object()

LIFECYCLE_LIMIT = 2000
LIFECYCLE_BYTES = 2 * 1024 * 1024
ADVICE_CONDITIONS = ('median-age', 'median-tokens', 'budget-tokens', 'budget-hours',
                     'compaction-limit', 'subagent-share', 'usage-repair')


def lifecycle_empty():
    return {'version': 1, 'inbox': {}, 'inbox_retired': {}, 'answers': [],
            'reminders': {}, 'reminders_retired': {}, 'usage': {}}


def lifecycle_read(inst):
    path = inst.root / '.brain/wake/lifecycle.json'
    if path.parent.resolve() != inst.root.resolve() / '.brain/wake':
        raise ValueError('unsafe lifecycle state directory')
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return lifecycle_empty()
    with os.fdopen(fd) as stream:
        if (os.fstat(stream.fileno()).st_nlink != 1 or
                _m_history.history_fd_path(stream.fileno()) != str(path.absolute())):
            raise ValueError('unsafe lifecycle state')
        text = stream.read(LIFECYCLE_BYTES + 1)
    if len(text) > LIFECYCLE_BYTES:
        raise ValueError('lifecycle state exceeds its bound')
    data = json.loads(text)
    if (not isinstance(data, dict) or data.get('version') != 1 or
            any(not isinstance(data.get(k), dict) for k in ('inbox', 'reminders', 'usage')) or
            not isinstance(data.get('answers'), list) or not isinstance(data.get('inbox_retired', {}), dict) or
            not isinstance(data.get('reminders_retired', {}), dict)):
        raise ValueError('invalid lifecycle state; retain it for repair')
    return data


@contextlib.contextmanager
def lifecycle_store(inst, readonly=False, blocking=True, *, usage_key=None):
    """Serialize scans with updates; publishing pins the actual private directory."""
    from . import ui_readmodel
    view = ui_readmodel.projection(inst.root)
    if readonly or view is not None:
        data = (view.store(inst.root / '.brain/wake/lifecycle.json', lambda: lifecycle_read(inst))
                if view else lifecycle_read(inst))
        yield data
        return
    folder = inst.root / '.brain/wake'
    if folder.resolve() != inst.root.resolve() / '.brain/wake':
        raise ValueError('unsafe lifecycle state directory')
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / 'lifecycle.lock'
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a+') as lock:
        if (os.fstat(lock.fileno()).st_nlink != 1 or
                _m_history.history_fd_path(lock.fileno()) != str(path.absolute())):
            raise ValueError('unsafe lifecycle lock')
        fcntl.flock(lock, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        data = lifecycle_read(inst)
        # This is only an in-memory change detector; no pickle is read or kept.
        # Most observations do not change receipts, so avoid encoding their
        # entire JSON ledger twice on every job/node observation.
        # Restart observations can mutate only their own usage record. Comparing
        # it avoids serializing all inbox/reminder history for every open job.
        def observed():
            return data if usage_key is None else data['usage'].get(usage_key)
        before = pickle.dumps(observed(), protocol=4)
        yield data
        if before != pickle.dumps(observed(), protocol=4):
            after = json.dumps(data, sort_keys=True)
            if len(after.encode()) > LIFECYCLE_BYTES:
                raise ValueError('lifecycle state exceeds its bound; retain history for review')
            _m_machine.machine_publish(inst, folder / 'lifecycle.json', after + '\n')


@contextlib.contextmanager
def usage_restart_store(inst, key):
    """Detach one projected restart record, retaining the real ledger bound."""
    from . import ui_readmodel
    view = ui_readmodel.projection(inst.root)
    if view is not None:
        record, present, count = view.store(inst.root / '.brain/wake/lifecycle.json',
            lambda: lifecycle_read(inst),
            select=lambda data: (data['usage'].get(key, {}), key in data['usage'], len(data['usage'])))
        yield {'usage': {key: record} if present else {}}, count
    else:
        with lifecycle_store(inst, usage_key=key) as data:
            yield data, len(data['usage'])


@_m_command_reads.memo
def usage_restart_item(inst, n, row):
    """One protected restart need per job and successful rebrief baseline."""
    if row.get('Ended'):
        return None
    usage = _m_spend.live_usage(inst, n, row)
    counts = {k: (usage or {}).get(k) for k in ('compactions', 'processed')}
    counts = {k: v if type(v) is int and v >= 0 else None for k, v in counts.items()}
    key = _m_jobs.digest([n['id'], _m_jobs.job_key(row)])
    with usage_restart_store(inst, key) as (data, usage_size):
        record = data['usage'].get(key, {})
        if not isinstance(record, dict):
            raise ValueError('invalid restart usage receipt')
        baseline = record.get('baseline', {'compactions': 0, 'processed': 0})
        if (not isinstance(baseline, dict) or any(type(baseline.get(k)) is not int or baseline[k] < 0
                                               for k in counts)):
            raise ValueError('invalid restart usage baseline')
        if any(value is not None and value < baseline[k] for k, value in counts.items()):
            raise ValueError('restart usage counters regressed; retain the baseline for review')
        delta = {k: value - baseline[k] if value is not None else None for k, value in counts.items()}
        episode = record.get('episode')
        if episode is not None and not re.fullmatch(r'[0-9a-f]{24}', str(episode)):
            raise ValueError('invalid restart episode')
        # Budgets are soft. Retire legacy token-only episodes when known
        # compaction evidence rules out the restart threshold. Unknown legacy
        # evidence stays pending; new episodes record their compaction trigger.
        crossed = delta['compactions'] is not None and delta['compactions'] >= 3
        if episode and record.get('trigger') != 'compactions' and delta['compactions'] is not None:
            if not crossed:
                # With no episode, old accounting treats `at` as a delivered
                # restart time. Retiring an alert must preserve only a real
                # round time, never promote the alert's observation time.
                data['usage'][key] = {**record, 'episode': None, 'at': record.get('round_at')}
                return None
            data['usage'][key] = {**record, 'trigger': 'compactions'}
        if not episode and not crossed:
            return None
        if not episode:
            if usage_size >= LIFECYCLE_LIMIT and key not in data['usage']:
                raise ValueError('restart receipt bound reached')
            from . import ui_readmodel
            episode = ui_readmodel.episode(inst, [key, baseline, record])
            data['usage'][key] = {'job': _m_jobs.job_key(row), 'baseline': baseline,
                                  'episode': episode, 'trigger': 'compactions', 'at': inst.now(),
                                  'round_at': record.get('round_at') or (record.get('at') if record and not record.get('episode') else None)}
        shown = {k: value if value is not None else 'unknown' for k, value in delta.items()}
        command = _m_briefs.restart_command(n, row)
        return {'node': n['id'], 'kind': 'job-restart', 'job': _m_jobs.job_key(row), 'episode': episode,
                'blocking': True, 'protected': True,
                'text': (f"BLOCKING: restart {row['Job']} from a brief ({shown['compactions']} compactions); "
                         f"{shown['processed']} processed tokens since brief (recorded only; budgets are soft). "
                         f"Owning coordinator must restart its worker with `{command}` before continuing the job.")}


def acknowledge_usage_restart(inst, n, row, *, observed_usage=_USAGE_NOT_OBSERVED):
    """Acknowledge delivered restart usage; ordinary briefs do not reset budgets.

    Native launchers pass their captured pre-launch live_usage dict after binding
    the replacement. Explicit snapshots require known processed, compaction and
    active-minute counters and never trigger a later lifetime-counter read.
    """
    if not row or row.get('Ended'):
        return
    captured = observed_usage is not _USAGE_NOT_OBSERVED
    if captured:
        if (not isinstance(observed_usage, dict) or
                type(observed_usage.get('sessions')) is not int or observed_usage['sessions'] < 1 or
                not isinstance(observed_usage.get('source'), str) or
                not observed_usage['source'].startswith(('host', 'transcripts:')) or any(
                type(observed_usage.get(k)) is not int or observed_usage[k] < 0
                for k in ('compactions', 'processed', 'active_minutes'))):
            raise ValueError('restart usage unavailable; captured baseline not acknowledged')
        observed_usage = dict(observed_usage)
    key = _m_jobs.digest([n['id'], _m_jobs.job_key(row)])
    with lifecycle_store(inst) as data:
        advice = data.get('advice', {})
        if key in advice:
            advice[key] = {'episode': os.urandom(12).hex(), 'conditions': {}}
        record = data['usage'].get(key)
        signal = _m_jobs.job_signal(inst, n, row)
        manual_restart = bool(signal and signal.get('restart') == 'compactions' and
                              not _m_briefs.restart_acknowledged(n, row, signal))
        if not (isinstance(record, dict) and record.get('episode')) and not manual_restart:
            return
        usage = observed_usage if captured else _m_spend.live_usage(inst, n, row)
        if not usage or any(type(usage.get(k)) is not int or usage[k] < 0
                            for k in ('compactions', 'processed')):
            raise ValueError('restart usage unavailable; baseline not acknowledged')
        if captured and isinstance(record, dict):
            previous = record.get('baseline', {})
            if not isinstance(previous, dict) or any(type(value) is not int or value < 0
                    for k, value in previous.items() if k in ('compactions', 'processed', 'active_minutes')):
                raise ValueError('invalid restart usage baseline')
            if any(usage[k] < previous[k] for k in ('compactions', 'processed', 'active_minutes') if k in previous):
                raise ValueError('restart usage counters regressed; baseline not acknowledged')
        baseline = {k: usage[k] for k in ('compactions', 'processed')}
        active = usage.get('active_minutes')
        if type(active) is int and active >= 0:
            baseline['active_minutes'] = active
        data['usage'][key] = {'job': _m_jobs.job_key(row), 'baseline': baseline,
                              'episode': None, 'at': inst.now(), 'round_at': inst.now()}


def inbox_source_id(n, line):
    return _m_jobs.digest([n['id'], 'inbox', ' '.join(('inbox: ' + line[2:]).split())])


def next_request_id(n, key, data):
    """Deterministic between read-only projection and mutation, distinct after retirement."""
    retired = data.get('inbox_retired', {}).get(n['id'], {})
    if not isinstance(retired, dict):
        raise ValueError('invalid retired inbox episodes')
    previous = retired.get(key)
    if previous is None:
        previous = next((a.get('id') for a in reversed(data['answers']) if isinstance(a, dict)
                         and a.get('source_id') == key and a.get('node') == n['id']), None)
    if previous is not None and not re.fullmatch(r'[0-9a-f]{24}', str(previous)):
        raise ValueError('invalid previous inbox episode')
    return _m_jobs.digest([key, 'recurrence', previous]) if previous else key


def validate_inbox_episode(n, key, record, answers):
    if (not isinstance(record, dict) or not isinstance(record.get('id'), str) or
            not re.fullmatch(r'[0-9a-f]{24}', record['id']) or type(record.get('answered')) is not bool):
        raise ValueError('invalid inbox episode receipt')
    if record['answered'] and not any(isinstance(a, dict) and a.get('id') == record['id']
                                      and a.get('source_id') == key and a.get('node') == n['id']
                                      and isinstance(a.get('answer'), str) and a['answer'].strip()
                                      for a in answers):
        raise ValueError('answered receipt has no retained answer')


def reconcile_requests(inst, n, lines, data):
    previous = data['inbox'].get(n['id'], {})
    if not isinstance(previous, dict):
        raise ValueError('invalid inbox lifecycle receipts')
    if len(lines) > LIFECYCLE_LIMIT:
        raise ValueError('inbox lifecycle bound reached; no requests discarded')
    current = {}
    for line in lines:
        key = inbox_source_id(n, line)
        record = previous.get(key)
        if record is None:
            record = {'id': next_request_id(n, key, data), 'answered': False}
        validate_inbox_episode(n, key, record, data['answers'])
        current[key] = record
    absent = set(previous) - set(current)
    closed = data.get('inbox_retired', {}).get(n['id'], {})
    if not isinstance(closed, dict):
        raise ValueError('invalid retired inbox episodes')
    absent_known = set(closed) - set(current)
    if absent or absent_known:
        retired = data.setdefault('inbox_retired', {}).setdefault(n['id'], {})
        if not isinstance(retired, dict) or len(set(retired) | absent) > LIFECYCLE_LIMIT:
            raise ValueError('retired inbox episode bound reached; retain history for review')
        for key in absent:
            record = previous[key]
            if not isinstance(record, dict) or not re.fullmatch(r'[0-9a-f]{24}', str(record.get('id'))):
                raise ValueError('invalid retired inbox episode')
            retired[key] = record['id']
        # A read-only projection may have exposed the next generation without
        # persisting it. A later observed absence must invalidate that ID too.
        # Advance only closed generations; unchanged open requests keep their IDs.
        for key in absent_known - absent:
            if not re.fullmatch(r'[0-9a-f]{24}', str(retired[key])):
                raise ValueError('invalid retired inbox episode')
            retired[key] = _m_jobs.digest([key, 'observed-absence', retired[key]])
    if current or n['id'] in data['inbox']:
        data['inbox'][n['id']] = current
    return current


def request_lines(inst, n, lines, readonly=False, lifecycle_data=None):
    """Keep source history intact; an observed recurrence gets a fresh episode."""
    from . import inbox_notices
    if lifecycle_data is not None:
        current = reconcile_requests(inst, n, lines, lifecycle_data)
        return [line for line in lines if inbox_notices.notice_pending(line, current[inbox_source_id(n, line)])]
    try:
        with lifecycle_store(inst, readonly=readonly) as data:
            current = reconcile_requests(inst, n, lines, data)
            return [line for line in lines if inbox_notices.notice_pending(line, current[inbox_source_id(n, line)])]
    except (OSError, ValueError):
        return lines  # unknown receipts never hide a question


def request_episode_id(inst, n, line, lifecycle_data=None):
    key = inbox_source_id(n, line)
    try:
        data = lifecycle_data if lifecycle_data is not None else lifecycle_read(inst)
        record = data['inbox'].get(n['id'], {}).get(key, {})
        return record['id'] if record else next_request_id(n, key, data)
    except (OSError, ValueError, AttributeError):
        return key


def resolve_lifecycle_id(items, item_id):
    if not re.fullmatch(r'[0-9a-f]{6,24}', item_id or ''):
        _m_common.die('use a current item ID of at least six hexadecimal characters')
    matches = [i for i in items if i['id'].startswith(item_id)]
    if len(matches) != 1:
        _m_common.die('unknown or ambiguous item ID; inspect the current inbox or due output')
    return matches[0]


def record_answer(inst, n, line, item_id, text, lifecycle_data=None):
    if not text.strip() or len(text.encode()) > 8192:
        _m_common.die('answer must contain text and be at most 8192 bytes')
    _m_guard.no_terms_in(n, text, 'the recorded answer')
    if lifecycle_data is None:
        with lifecycle_store(inst) as data:
            return record_answer(inst, n, line, item_id, text, lifecycle_data=data)
    data = lifecycle_data
    if len(data['answers']) >= LIFECYCLE_LIMIT:
        _m_common.die('answer history bound reached; retain it for coordinator review')
    key = inbox_source_id(n, line)
    source = n['dir'] / 'inbox.md'
    if not source.is_file() or line not in source.read_text().splitlines():
        _m_common.die('request source changed; inspect the current inbox')
    current = data['inbox'].get(n['id'], {}).get(key)
    if current is None:
        current = {'id': next_request_id(n, key, data), 'answered': False}
        data['inbox'].setdefault(n['id'], {})[key] = current
    if current.get('id') != item_id or current.get('answered'):
        _m_common.die('request episode changed or was already answered')
    current['answered'] = True
    data['answers'].append({'id': item_id, 'source_id': key, 'node': n['id'],
                            'source': line[2:], 'answer': text, 'at': inst.now()})


def semantic_item_key(item):
    """Only producer-supplied semantics; display numbers and prose never enter these keys."""
    if item.get('job') and item.get('condition'):
        return [item['node'], item['job'], item['condition'], item.get('state'), item.get('episode')]
    return None


def latch_job_advice(inst, n, row, conditions, readonly=False):
    """Keep each first crossing open until a successful rebrief, even if medians move."""
    key = _m_jobs.digest([n['id'], _m_jobs.job_key(row)])
    with lifecycle_store(inst, readonly=readonly) as data:
        advice = data.setdefault('advice', {})
        if not isinstance(advice, dict) or (key not in advice and len(advice) >= LIFECYCLE_LIMIT):
            raise ValueError('invalid or oversized job advice state')
        record = advice.get(key)
        if record is None:
            if not conditions:
                return []
            record = {'episode': _m_jobs.digest([key, 'initial-advice-baseline']), 'conditions': {}}
        if (not isinstance(record, dict) or not re.fullmatch(r'[0-9a-f]{24}', str(record.get('episode'))) or
                not isinstance(record.get('conditions'), dict) or
                any(code not in ADVICE_CONDITIONS or not isinstance(text, str)
                    for code, text in record['conditions'].items())):
            raise ValueError('invalid job advice baseline')
        for code, text in conditions.items():
            record['conditions'].setdefault(code, text)
        advice[key] = record
        projection = _m_attempts.job_projection(inst, n, row)
        signal = projection['signal'] or {}
        attempt = projection['attempt'] or {}
        w = n['local'].get('worktrees', {}).get(row['Job'])
        local = _m_status.local_job_state(n, row['Job'], w) if w else {}
        # Recorded episode evidence only: never attempt timestamps, owners,
        # queue positions, usage counters, display prose or moving thresholds.
        context = {'state': projection['state'], 'attempt': attempt.get('id'),
                   'publication': signal.get('wake_id'), 'restart': signal.get('restart_id'),
                   'merged': bool(local.get('merged')),
                   'merged_tip': local.get('tip') if local.get('merged') else None}
        previous = record.get('context_episode')
        if previous is not None and not re.fullmatch(r'[0-9a-f]{24}', str(previous)):
            raise ValueError('invalid job advice context episode')
        if record.get('context') != context:
            # Chaining also distinguishes A -> B -> A in owning inboxes, even
            # when no due scan reconciled the intermediate B reminder.
            record['context_episode'] = _m_jobs.digest([record['episode'], previous, context])
            record['context'] = context
        state = projection['state']
        return [{'node': n['id'], 'kind': 'job', 'job': _m_jobs.job_key(row), 'condition': code,
                 'state': state, 'episode': record['context_episode'], 'informational': True,
                 'text': _m_guard.redact(f"job {row['Job']}: " + (conditions[code] if code in conditions else
                          'crossed since brief: ' + record['conditions'][code]), _m_guard.never_terms(n))}
                for code in ADVICE_CONDITIONS if code in record['conditions']]


def reminder_key(item):
    semantic = semantic_item_key(item)
    if semantic is not None:
        return _m_jobs.digest(semantic)
    need = item.get('need')
    if need is None and item['kind'] in ('handback', 'missing-preview'):
        need = ' '.join(item['text'].split())
    return _m_jobs.digest([item['node'], item['kind'], item.get('job'),
                           item.get('episode') or ' '.join(item['text'].split()), need])


def reminder_reconcile(inst, n, items, data, omitted_kinds=()):
    previous = data['reminders'].get(n['id'], {})
    retired = data.get('reminders_retired', {}).get(n['id'], {})
    if not isinstance(previous, dict) or len(items) > LIFECYCLE_LIMIT:
        raise ValueError('invalid or oversized reminder lifecycle state')
    if (not isinstance(retired, dict) or len(retired) > LIFECYCLE_LIMIT or
            any(not re.fullmatch(r'[0-9a-f]{24}', str(value)) for value in retired.values())):
        raise ValueError('invalid or oversized retired reminder state')
    current, out = {}, []
    if omitted_kinds:
        for key, record in previous.items():
            if not isinstance(record, dict) or not re.fullmatch(r'[0-9a-f]{24}', str(record.get('id'))):
                raise ValueError('invalid reminder episode receipt')
            # Older receipts have no kind. A partial scan cannot prove them closed;
            # the next complete scan migrates or retires them without losing identity.
            if record.get('kind') in omitted_kinds or 'kind' not in record:
                current[key] = record
    for item in items:
        key = reminder_key(item)
        # Identical frozen inputs must expose the same IDs in independent copies.
        # Keep legacy IDs while open, then derive each successor from its retired ID.
        identity = _m_jobs.digest([key, 'recurrence', retired[key]]) if key in retired else key
        record = previous[key] if key in previous else {'id': identity, 'at': inst.now()}
        if not isinstance(record, dict) or not re.fullmatch(r'[0-9a-f]{24}', str(record.get('id'))):
            raise ValueError('invalid reminder episode receipt')
        record = {**record, 'kind': item['kind']}
        current[key] = record
        handling = record.get('handling')
        if handling is not None and not isinstance(handling, dict):
            raise ValueError('invalid reminder handling receipt')
        safe = {k: _m_guard.redact(v, _m_guard.never_terms(n)) if isinstance(v, str) else v
                for k, v in handling.items()} if handling else None
        out.append({**item, 'id': record['id'], **({'handling': safe} if safe else {})})
    if len(current) > LIFECYCLE_LIMIT:
        raise ValueError('reminder lifecycle bound reached; retain receipts for review')
    absent = set(previous) - set(current)
    if absent:
        if len(set(retired) | absent) > LIFECYCLE_LIMIT:
            raise ValueError('retired reminder bound reached; retain receipts for review')
        closed = dict(retired)
        for key in absent:
            record = previous[key]
            if not isinstance(record, dict) or not re.fullmatch(r'[0-9a-f]{24}', str(record.get('id'))):
                raise ValueError('invalid retired reminder episode')
            closed[key] = record['id']
        data.setdefault('reminders_retired', {})[n['id']] = closed
    if current or n['id'] in data['reminders']:
        data['reminders'][n['id']] = current
    return out


def reminder_eligible(item, action):
    if action == 'defer':
        return item['kind'] == 'missing-preview'
    return (action == 'ack' and item['kind'] == 'handback' and item.get('state') == 'blocked'
            and not item.get('protected') and not item.get('reasons'))


def reminder_handled(inst, item):
    handling = item.get('handling')
    if not isinstance(handling, dict) or not reminder_eligible(item, handling.get('action')):
        return False
    if not isinstance(handling.get('reason'), str) or not handling['reason'].strip():
        return False
    if handling['action'] == 'ack' and not handling.get('resume_condition'):
        return False
    until = handling.get('until')
    expiry = inst.parse_time(until) if isinstance(until, str) else None
    return (expiry > inst.now_dt()) if expiry else handling['action'] == 'ack' and until is None


def reminder_items(inst, n, items, include_handled=False, omitted_kinds=()):
    try:
        with lifecycle_store(inst) as data:
            out = reminder_reconcile(inst, n, items, data, omitted_kinds=omitted_kinds)
            return out if include_handled else [i for i in out if not reminder_handled(inst, i)]
    except (OSError, ValueError):
        return [{**i, 'id': reminder_key(i), 'lifecycle_error': 'receipt state unknown; no suppression'} for i in items]


def record_reminder(inst, n, items, item_id, action, reason, until=None, resume_condition=None):
    if not isinstance(reason, str) or not reason.strip() or len(reason.encode()) > 2048:
        _m_common.die('a nonempty reason of at most 2048 bytes is required')
    if action == 'ack' and (not isinstance(resume_condition, str) or not resume_condition.strip()
                            or len(resume_condition.encode()) > 2048):
        _m_common.die('acknowledgement requires a nonempty resume condition of at most 2048 bytes')
    _m_guard.no_terms_in(n, reason + ' ' + (resume_condition or ''), 'the reminder handling reason')
    if action == 'defer' or until is not None:
        expiry = inst.parse_time(until)
        if not expiry or not re.search(r'(?:Z|[+-]\d\d:?\d\d)\s*$', until or '') or expiry <= inst.now_dt():
            _m_common.die('until must be an absolute future timestamp with a UTC offset')
    # Validate before opening a writable transaction, so refusals change no receipts.
    with lifecycle_store(inst, readonly=True) as data:
        current = reminder_reconcile(inst, n, items, data)
        item = resolve_lifecycle_id(current, item_id)
        if not reminder_eligible(item, action):
            _m_common.die('this protected need cannot be acknowledged or deferred')
    with lifecycle_store(inst) as data:
        current = reminder_reconcile(inst, n, items, data)
        item = resolve_lifecycle_id(current, item_id)
        if not reminder_eligible(item, action):
            _m_common.die('this protected need cannot be acknowledged or deferred')
        data['reminders'][n['id']][reminder_key(item)]['handling'] = {
            'action': action, 'reason': reason, 'resume_condition': resume_condition,
            'until': until, 'at': inst.now()}
