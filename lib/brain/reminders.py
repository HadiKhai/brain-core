"""Bounded, machine-local request and reminder lifecycle receipts."""
import contextlib
import fcntl
import json
import os
import re

from . import common as _m_common
from . import history as _m_history
from . import jobs as _m_jobs
from . import machine as _m_machine
from . import spend as _m_spend


LIFECYCLE_LIMIT = 2000
LIFECYCLE_BYTES = 2 * 1024 * 1024


def lifecycle_empty():
    return {'version': 1, 'inbox': {}, 'answers': [], 'reminders': {}, 'usage': {}}


def lifecycle_read(inst):
    path = inst.root / '.brain/lifecycle.json'
    if path.parent.resolve() != inst.root.resolve() / '.brain':
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
            not isinstance(data.get('answers'), list)):
        raise ValueError('invalid lifecycle state; retain it for repair')
    return data


@contextlib.contextmanager
def lifecycle_store(inst, readonly=False):
    """Serialize scans with updates; publishing pins the actual private directory."""
    if readonly:
        yield lifecycle_read(inst)
        return
    folder = inst.root / '.brain'
    if folder.resolve() != inst.root.resolve() / '.brain':
        raise ValueError('unsafe lifecycle state directory')
    folder.mkdir(exist_ok=True)
    path = folder / 'lifecycle.lock'
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a+') as lock:
        if (os.fstat(lock.fileno()).st_nlink != 1 or
                _m_history.history_fd_path(lock.fileno()) != str(path.absolute())):
            raise ValueError('unsafe lifecycle lock')
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = lifecycle_read(inst)
        before = json.dumps(data, sort_keys=True)
        yield data
        after = json.dumps(data, sort_keys=True)
        if len(after.encode()) > LIFECYCLE_BYTES:
            raise ValueError('lifecycle state exceeds its bound; retain history for review')
        if before != after:
            _m_machine.machine_publish(inst, folder / 'lifecycle.json', after + '\n')


def usage_restart_item(inst, n, row):
    """One protected restart need per job and successful rebrief baseline."""
    if row.get('Ended'):
        return None
    usage = _m_spend.live_usage(inst, n, row)
    if not usage:
        return None
    counts = {k: usage.get(k) for k in ('compactions', 'processed')}
    if any(type(v) is not int or v < 0 for v in counts.values()):
        return None
    key = _m_jobs.digest([n['id'], _m_jobs.job_key(row)])
    budget = (_m_spend.job_budget(n, row) or {}).get('tokens')
    with lifecycle_store(inst) as data:
        record = data['usage'].get(key, {})
        baseline = record.get('baseline', {'compactions': 0, 'processed': 0})
        if (not isinstance(baseline, dict) or any(type(baseline.get(k)) is not int or baseline[k] < 0
                                               for k in counts)):
            raise ValueError('invalid restart usage baseline')
        delta = {k: max(0, counts[k] - baseline[k]) for k in counts}
        if delta['compactions'] < 3 and (not budget or delta['processed'] < 2 * budget):
            return None
        episode = record.get('episode')
        if not episode:
            if len(data['usage']) >= LIFECYCLE_LIMIT and key not in data['usage']:
                raise ValueError('restart receipt bound reached')
            episode = os.urandom(12).hex()
            data['usage'][key] = {'job': _m_jobs.job_key(row), 'baseline': baseline,
                                  'episode': episode, 'at': inst.now()}
        budget_text = str(budget) if budget else 'not configured'
        return {'node': n['id'], 'kind': 'restart', 'job': _m_jobs.job_key(row), 'episode': episode,
                'text': (f"{row['Job']}: restart from brief: {delta['compactions']} compactions since brief "
                         f"(threshold 3); {delta['processed']} processed tokens since brief "
                         f"(token budget {budget_text}; threshold twice the budget). "
                         'Coordinator: regenerate the job brief and restart its worker; no automatic stop.')}


def acknowledge_usage_restart(inst, n, row):
    """Called only after a brief was delivered; initial briefs do not reset budgets."""
    if not row or row.get('Ended'):
        return
    key = _m_jobs.digest([n['id'], _m_jobs.job_key(row)])
    with lifecycle_store(inst) as data:
        record = data['usage'].get(key)
        if not isinstance(record, dict) or not record.get('episode'):
            return
        usage = _m_spend.live_usage(inst, n, row)
        if not usage or any(type(usage.get(k)) is not int or usage[k] < 0
                            for k in ('compactions', 'processed')):
            raise ValueError('restart usage unavailable; baseline not acknowledged')
        data['usage'][key] = {'job': _m_jobs.job_key(row), 'baseline': {
            k: usage[k] for k in ('compactions', 'processed')}, 'episode': None, 'at': inst.now()}


def inbox_source_id(n, line):
    return _m_jobs.digest([n['id'], 'inbox', ' '.join(('inbox: ' + line[2:]).split())])


def reconcile_requests(inst, n, lines, data):
    previous = data['inbox'].get(n['id'], {})
    if not isinstance(previous, dict):
        raise ValueError('invalid inbox lifecycle receipts')
    if len(lines) > LIFECYCLE_LIMIT:
        raise ValueError('inbox lifecycle bound reached; no requests discarded')
    answered_sources = {a.get('source_id') for a in data['answers'] if isinstance(a, dict)}
    current = {}
    for line in lines:
        key = inbox_source_id(n, line)
        record = previous.get(key)
        if record is None:
            record = {'id': os.urandom(12).hex() if key in answered_sources else key, 'answered': False}
        if (not isinstance(record, dict) or not re.fullmatch(r'[0-9a-f]{24}', str(record.get('id'))) or
                type(record.get('answered')) is not bool):
            raise ValueError('invalid inbox episode receipt')
        current[key] = record
    if current or n['id'] in data['inbox']:
        data['inbox'][n['id']] = current
    return current


def request_lines(inst, n, lines, readonly=False):
    """Keep source history intact; an observed recurrence gets a fresh episode."""
    try:
        with lifecycle_store(inst, readonly=readonly) as data:
            current = reconcile_requests(inst, n, lines, data)
            return [line for line in lines if not current[inbox_source_id(n, line)]['answered']]
    except (OSError, ValueError):
        return lines  # unknown receipts never hide a question


def request_episode_id(inst, n, line):
    key = inbox_source_id(n, line)
    try:
        record = lifecycle_read(inst)['inbox'].get(n['id'], {}).get(key, {})
        return record.get('id', key)
    except (OSError, ValueError, AttributeError):
        return key


def resolve_lifecycle_id(items, item_id):
    if not re.fullmatch(r'[0-9a-f]{6,24}', item_id or ''):
        _m_common.die('use a current item ID of at least six hexadecimal characters')
    matches = [i for i in items if i['id'].startswith(item_id)]
    if len(matches) != 1:
        _m_common.die('unknown or ambiguous item ID; inspect the current inbox or due output')
    return matches[0]


def record_answer(inst, n, line, item_id, text):
    if not text.strip() or len(text.encode()) > 8192:
        _m_common.die('answer must contain text and be at most 8192 bytes')
    with lifecycle_store(inst) as data:
        if len(data['answers']) >= LIFECYCLE_LIMIT:
            _m_common.die('answer history bound reached; retain it for coordinator review')
        key = inbox_source_id(n, line)
        current = data['inbox'].get(n['id'], {}).get(key)
        # Read-only lookups can address a first occurrence before a reporting scan.
        if current is None:
            source = n['dir'] / 'inbox.md'
            if not source.is_file() or line not in source.read_text().splitlines():
                _m_common.die('request source changed; inspect the current inbox')
            current = {'id': request_episode_id(inst, n, line), 'answered': False}
            data['inbox'].setdefault(n['id'], {})[key] = current
        if current.get('id') != item_id or current.get('answered'):
            _m_common.die('request episode changed or was already answered')
        current['answered'] = True
        data['answers'].append({'id': item_id, 'source_id': key, 'node': n['id'],
                                'source': line[2:], 'answer': text, 'at': inst.now()})


def reminder_key(item):
    return _m_jobs.digest([item['node'], item['kind'], item.get('job'),
                           item.get('episode') or ' '.join(item['text'].split())])


def reminder_reconcile(inst, n, items, data):
    previous = data['reminders'].get(n['id'], {})
    if not isinstance(previous, dict) or len(items) > LIFECYCLE_LIMIT:
        raise ValueError('invalid or oversized reminder lifecycle state')
    current, out = {}, []
    for item in items:
        key = reminder_key(item)
        record = previous.get(key) or {'id': os.urandom(12).hex(), 'at': inst.now()}
        if not isinstance(record, dict) or not re.fullmatch(r'[0-9a-f]{24}', str(record.get('id'))):
            raise ValueError('invalid reminder episode receipt')
        current[key] = record
        out.append({**item, 'id': record['id'], **({'handling': record['handling']} if 'handling' in record else {})})
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


def reminder_items(inst, n, items, include_handled=False):
    try:
        with lifecycle_store(inst) as data:
            out = reminder_reconcile(inst, n, items, data)
            return out if include_handled else [i for i in out if not reminder_handled(inst, i)]
    except (OSError, ValueError):
        return [{**i, 'id': reminder_key(i), 'lifecycle_error': 'receipt state unknown; no suppression'} for i in items]


def record_reminder(inst, n, items, item_id, action, reason, until=None, resume_condition=None):
    if not isinstance(reason, str) or not reason.strip() or len(reason.encode()) > 2048:
        _m_common.die('a nonempty reason of at most 2048 bytes is required')
    if action == 'ack' and (not isinstance(resume_condition, str) or not resume_condition.strip()
                            or len(resume_condition.encode()) > 2048):
        _m_common.die('acknowledgement requires a nonempty resume condition of at most 2048 bytes')
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
