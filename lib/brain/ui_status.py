"""Read-only age and waiting/failure evidence for cached dashboard snapshots."""
import json
import re
import hashlib

from . import common as _m_common

_m_guard = _m_common.LazyModule(__package__, 'guard')
_m_jobs = _m_common.LazyModule(__package__, 'jobs')
_m_nodes = _m_common.LazyModule(__package__, 'nodes')
_m_status = _m_common.LazyModule(__package__, 'status')


def ui_inbox_projection(inst, nodes, items):
    """Retain source lines and lifecycle facts without acknowledging or archiving files.

    Root is the operator rollup. Other inboxes address their coordinator unless
    the source explicitly names the operator as its recipient.
    """
    from . import communication, reminders
    try:
        questions = communication.communication_read(inst)['questions']
    except (OSError, ValueError):
        questions = {}
    try:
        lifecycle = reminders.lifecycle_read(inst)
    except (OSError, ValueError):
        lifecycle = reminders.lifecycle_empty()
    by_node = {n['id']: n for n in nodes if not n['archived']}
    out = []
    known = set()
    for item in items:
        node = by_node.get(item['node'])
        if node is None:
            continue
        item = dict(item)
        if item['kind'] == 'inbox':
            line = '- ' + item['text'].removeprefix('inbox: ')
            known.add((node['id'], item['id']))
            item['source'] = node['id'] + '/inbox.md'
            item.update(ui_request_metadata(inst, node, line, questions, lifecycle))
        out.append(item)
    for node in by_node.values():
        terms = _m_guard.never_terms(node)
        for name in ('inbox.md', 'inbox-archive.md'):
            path = node['dir'] / name
            if not path.is_file() or path.is_symlink():
                continue
            marker, retained = None, None
            for number, raw in enumerate(path.read_bytes().decode().splitlines(keepends=True), 1):
                line = raw.rstrip('\r\n')
                if name == 'inbox-archive.md' and line.startswith('<!-- inbox-archive:'):
                    marker = re.fullmatch(r'<!-- inbox-archive:([0-9a-f]{24}):([0-9a-f]{64}) -->', line)
                    retained = None
                    continue
                if name == 'inbox-archive.md' and line.startswith('<!-- episode '):
                    try:
                        retained = json.loads(line.removeprefix('<!-- episode ').removesuffix(' -->'))
                    except ValueError:
                        retained = None
                    continue
                if not line.startswith('- '):
                    continue
                verified = (marker is not None and isinstance(retained, dict) and retained.get('id') == marker[1]
                            and type(retained.get('answered')) is bool and marker[2] in
                            {hashlib.sha256(value.encode()).hexdigest() for value in (raw, line)})
                if name == 'inbox-archive.md':
                    identity = marker[1] if verified else _m_jobs.digest(['ui-archive-source', node['id'], number, line])
                else:
                    identity = reminders.request_episode_id(inst, node, line, lifecycle)
                if (node['id'], identity) in known:
                    marker, retained = None, None
                    continue
                known.add((node['id'], identity))
                metadata = ui_request_metadata(inst, node, line, questions, lifecycle)
                if verified:
                    metadata['answered'] = retained['answered']
                marker, retained = None, None
                # A source no longer in the open projection stays in the archive.
                metadata.update(open=False, source=node['id'] + '/' + name)
                out.append({'id': identity,
                            'node': node['id'], 'kind': 'inbox',
                            'text': _m_guard.redact(line[2:], terms), **metadata})
    return out


def ui_request_metadata(inst, node, line, questions, lifecycle):
    """Only explicit routing and structured choices can turn prose into an ask."""
    from . import reminders
    operator = str(inst.cfg.get('operator', 'operator'))
    body = re.sub(r'^- .*? from [^:]+:\s*', '', line)
    recipient = operator if node['id'] == 'root' else ''
    match = re.search(r'(?:^|;\s*)(?:to|recipient|waiting on)\s*:?\s*([^;]+)', body, re.I)
    if match:
        recipient = match[1].strip()
    item = {'recipient': recipient, 'question': bool(re.match(r'(?:question|decision):\s*\S', body, re.I))}
    timestamp = re.match(r'^- (.*?) from ', line)
    first_seen = inst.parse_time(timestamp[1]) if timestamp else None
    if first_seen:
        item['first_seen'] = first_seen.isoformat(timespec='seconds')
    marker = re.search(r'<!-- communication:([0-9a-f]+) -->', line)
    question = questions.get(marker[1]) if marker else None
    if isinstance(question, dict) and question.get('node') == node['id']:
        item.update({k: question[k] for k in ('options', 'recommendation', 'default', 'deadline', 'answer') if k in question})
        item['question'] = True
    else:
        fields = dict((k.lower(), v.strip()) for k, v in re.findall(
            r'(?:^|;\s*)(options|recommendation|deadline|expires_at|state):\s*([^;]+)', body, re.I))
        options = fields.pop('options', '')
        if options:
            item['options'] = [part.strip() for part in options.split('|') if part.strip()]
        item.update(fields)
    source_id = reminders.inbox_source_id(node, line)
    receipt = lifecycle.get('inbox', {}).get(node['id'], {}).get(source_id, {})
    if isinstance(receipt, dict) and receipt.get('answered') is True:
        item['answered'] = True
    terms = _m_guard.never_terms(node)
    def screened(value):
        if isinstance(value, str):
            return _m_guard.redact(value, terms)
        if isinstance(value, list):
            return [screened(v) for v in value]
        if isinstance(value, dict):
            return {k: screened(v) for k, v in value.items()}
        return value
    return screened(item)


def ui_work_metadata(inst, n):
    """Open ledger work, with faults bound to the current worker's latest valid run.

    The host snapshot's record order is retained: the last matching record wins.
    Error excerpts and host identities never leave this reader.
    """
    rows = _m_jobs.read_jobs(n) or []
    opened = {row['Job']: row for row in rows if row.get('Job') and not row.get('Ended')}
    workers = n['local'].get('workers', {})
    workers = workers if isinstance(workers, dict) else {}
    try:
        data = json.loads((inst.root / '.brain' / 'worker-runs.json').read_text())
        records = data.get('runs', []) if isinstance(data, dict) else []
        records = records if isinstance(records, list) else []
    except (OSError, ValueError, UnicodeError):
        records = []
    latest = {}
    keys = {_m_jobs.job_key(row) for row in opened.values()}
    for record in records:
        if not isinstance(record, dict) or record.get('node') != n['id']:
            continue
        key, thread, run, state = (record.get(name) for name in ('job_key', 'thread', 'run', 'status'))
        if (not isinstance(key, str) or key not in keys or not isinstance(thread, str) or not thread
                or workers.get(key) != thread or not isinstance(run, str) or not run.strip()
                or not isinstance(state, str) or not state.strip()):
            continue
        latest[key] = record
    now = inst.now_dt()
    out = {}
    for branch, row in opened.items():
        started = inst.parse_time(row.get('Started'))
        record = latest.get(_m_jobs.job_key(row), {})
        fault = record.get('status') == 'failed'
        category = ''
        if fault:
            error = str(record.get('error', '')).casefold()
            category = 'at capacity' if 'at capacity' in error else (
                'usage limit' if 'usage limit' in error else 'provider error')
        out[branch] = {
            'since': started.isoformat(timespec='seconds') if started else None,
            'age_hours': round(max(0, _m_status.elapsed(started, now) / 3600), 2) if started else None,
            'fault': fault, 'failure_category': category,
        }
    return out


def ui_blocked_metadata(inst, n, text, file_only=False):
    """Use explicit waiting fields; a blocked section alone does not identify its owner."""
    data = _m_status.blocked_metadata(inst, n, 'blocked', text, file_only=file_only)
    match = re.search(r'\bwaiting\s+on\s+([^;]+);\s*clears\s+when\s+([^;]+)', text, re.I)
    waiting, clears = (part.strip() for part in match.groups()) if match else ('', '')
    names = ['operator', 'you']
    operator = inst.cfg.get('operator')
    if isinstance(operator, str) and operator.strip():
        names.append(operator.strip())
    waiting_on_operator = bool(re.search(
        r'(?<!\w)(?:' + '|'.join(re.escape(name.casefold()) for name in names) + r')(?!\w)',
        waiting.casefold()))
    terms = _m_guard.never_terms(n)
    return {**data,
            'waiting_on': _m_nodes.import_text(waiting, terms),
            'clears_when': _m_nodes.import_text(clears, terms),
            'waiting_on_operator': waiting_on_operator}
