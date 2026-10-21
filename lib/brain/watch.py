"""File-only actionable-work watcher; no host queries, provider calls or model turns."""
import hashlib
import json
import os
import re
import shlex

from . import common as _m_common
from . import guard as _m_guard
from . import hosts as _m_hosts
from . import jobs as _m_jobs
from . import machine as _m_machine
from . import nodes as _m_nodes
from . import status as _m_status
from . import transitions as _m_transitions
from . import usage as _m_usage
from . import upgrade as _m_upgrade
from . import verify as _m_verify


def watch_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def watch_read(path, default):
    if not path.exists():
        return default
    # A damaged dedup file must never reset acknowledgement and repeat old wakes.
    data = json.loads(path.read_text())
    if not isinstance(data, type(default)):
        raise ValueError("invalid watcher state")
    return data


def watch_items(inst, n):
    """Due semantics, excluding refresh-only work. Never runs cmd_due's upgrade/network path."""
    rows = _m_jobs.read_jobs(n)
    worktrees = {b: {**w, **_m_status.local_job_state(n, b, w)}
                 for b, w in n['local'].get('worktrees', {}).items()}
    signals = {_m_jobs.job_key(r): _m_jobs.read_handback(n, r) for r in rows or []}
    for r in rows or []:
        if not r['Ended'] and not _m_jobs.handback_file(n, r).exists() and _m_verify.read_ready_packet(inst, n, r['Job'], r):
            signals[_m_jobs.job_key(r)] = {'state': 'done', 'report': '', 'legacy_ready': True}
    state = {'jobs': rows, 'worktrees': worktrees, 'handbacks': signals, 'inbox': _m_status.inbox_lines(n)}
    # Refresh evidence generates facts, Git logs and usage displays; none belongs on this path.
    items = _m_status.due_items(inst, n, evidence=(state, '', ''), include_stale=False,
                                include_ready=False) + _m_status.changes_due(inst, n)
    out = []
    for item in items:
        if item['kind'] == 'stale-status':
            continue
        if item['kind'] == 'inbox' and ('<!-- host-question:' in item['text'] or '<!-- handback:' in item['text']):
            continue  # Stop/handback already enqueued these question wakes
        if item['kind'] == 'handback' and item.get('state') in ('question', 'blocked'):
            continue  # direct question-wake owns these signals
        if item['kind'] == 'overdue-question':
            continue  # routed separately to root, once per source question
        if item['kind'] == 'unsettled-threads':
            for row in _m_jobs.unsettled_jobs(rows):
                out.append({'id': watch_digest([n['id'], item['kind'], _m_jobs.job_key(row)]),
                            'kind': item['kind'], 'text': 'Closed job needs host thread settlement'})
            continue
        if item['kind'] == 'core-changes':
            for entry in _m_upgrade.changes_unread(inst, n):
                out.append({'id': watch_digest([n['id'], item['kind'], entry['id']]),
                            'kind': item['kind'], 'text': 'Unread core change; run brain changes'})
            continue
        # Keep identity separate from changing display (readiness, age, counts).
        identity = item.get('job') or item['text']
        if item['kind'] == 'handback':
            identity = [identity, item.get('state')]
        text = _m_nodes.import_text(item['text'], _m_guard.never_terms(n))[:400]
        out.append({'id': watch_digest([n['id'], item['kind'], identity]),
                    'kind': item['kind'], 'text': text})
    return out


def watch_failures(inst, nodes, acknowledged):
    """Host adapter owns worker-runs.json; accept only currently bound, open jobs."""
    data = watch_read(inst.root / '.brain' / 'worker-runs.json', {})
    records = data.get('runs', [])
    if not isinstance(records, list):
        raise ValueError('worker runs must be a list')
    for n in nodes:
        rows = _m_jobs.read_jobs(n) or []
        workers = n['local'].get('workers', {})
        if not isinstance(workers, dict):
            continue
        for record in records:
            if not isinstance(record, dict) or record.get('node') != n['id'] or record.get('status') != 'failed':
                continue
            key = record.get('job_key')
            if not isinstance(key, str) or not isinstance(record.get('run'), str) or not record['run']:
                continue
            if not record.get('thread') or workers.get(key) != record['thread']:
                continue
            row = next((r for r in rows if not r['Ended'] and _m_jobs.job_key(r) == key), None)
            if row is None:
                continue
            marker = '<!-- worker-failure:' + watch_digest([key, record['thread'], record['run']]) + ' -->'
            if marker in acknowledged:
                continue
            # Only fixed categories persist; raw provider diagnostics can contain secrets.
            error = str(record.get('error', '')).lower()
            capacity = 'at capacity' in error
            category = 'at capacity' if capacity else ('usage limit' if 'usage limit' in error else 'provider error')
            retry = ''
            klass = (_m_jobs.job_class(n, row) if hasattr(_m_jobs, 'job_class')
                     else row.get('Class', '').strip().lower() or 'build')
            allowed = (_m_usage.class_allows_fallback(klass) if hasattr(_m_usage, 'class_allows_fallback')
                       else klass not in ('review', 'diagnose'))
            old = record.get('runtime')
            new = next((r for r in inst.runtimes if r != old and r in ('codex', 'claude')), None)
            if capacity and allowed and new and hasattr(_m_usage, 'class_route'):
                selected, subscription, reason = _m_usage.class_route(inst, klass, runtime=new, cached_only=True)
                new = selected if selected != old and selected in inst.runtimes and 'Stop starting jobs' not in reason else None
            if (capacity and allowed and old in ('codex', 'claude') and new
                    and not row.get('Capacity retry')
                    and not re.search(r' -> [^:;]+: at capacity(?:;|$)', row.get('Switches', ''))):
                switch = f'{old} -> {new}: at capacity'
                row['Switches'] = '; '.join(filter(None, [row.get('Switches'), _m_jobs.stamp(inst) + ' ' + switch]))
                row['Capacity retry'] = marker
                _m_jobs.write_jobs(inst, n, rows)
            # Recover the relaunch notice even after interruption following the ledger write.
            if capacity and old in ('codex', 'claude') and new and row.get('Capacity retry') == marker:
                command = 'brain brief ' + shlex.quote(n['id']) + ' --branch ' + shlex.quote(row['Job']) + ' --runtime ' + shlex.quote(new)
                retry = '; one retry recorded; coordinator: relaunch through host in existing worktree after `' + command + '`'
            text = _m_nodes.import_text(f"{row['Job']}: worker run failed ({category}){retry}", _m_guard.never_terms(n))
            _m_transitions.inbox_append(n['dir'], f'- {inst.now()} from watcher: {text} {marker}\n', marker)
            acknowledged.add(marker)


def watch_run(inst):
    path = inst.root / '.brain' / 'watch.json'
    state = watch_read(path, {})
    if path.exists() and (state.get('version') != 1 or not isinstance(state.get('seen'), list)
                  or not all(isinstance(x, str) for x in state['seen'])):
        raise ValueError('invalid watcher fingerprint; restore state before watching')
    seen = set(state.get('seen', []))
    nodes = [n for n in inst.nodes() if not n['archived']]
    failures = state.get('failures', [])
    if not isinstance(failures, list) or not all(isinstance(x, str) for x in failures):
        raise ValueError('invalid failure acknowledgement')
    failures = set(failures)
    watch_failures(inst, nodes, failures)
    groups = {}
    for n in nodes:
        # Uncoordinated nodes route actionable work to root; root needs no explicit binding.
        target, command = _m_hosts.wake_target(inst, n)
        group = groups.setdefault(target['id'], {'node': target, 'items': [], 'command': command})
        group['items'].extend(watch_items(inst, n))
        root = inst.node('root')
        for line in _m_status.overdue_questions(inst, n, _m_status.inbox_lines(n)):
            root_group = groups.setdefault('root', {'node': root, 'items': [],
                                            'command': _m_hosts.wake_target(inst, root)[1]})
            root_group['items'].append({'id': watch_digest(['overdue', n['id'], line]),
                                       'kind': 'overdue-question',
                                       'reason': 'overdue:' + hashlib.sha256(line.encode()).hexdigest(),
                                       'text': 'Unanswered question past its threshold; inspect brain due root'})
    current = []
    count = 0
    for group in groups.values():
        items = {i['id']: i for i in group['items']}
        current.extend(items.values())
        added = sorted(set(items) - seen)
        queued = False
        if added and group['command']:
            for key in added[:]:
                if 'reason' in items[key]:
                    if _m_hosts.request_wake(inst, group['node'], items[key]['reason']):
                        seen.add(key)
                        count += 1
                        queued = True
                    added.remove(key)
            # Include item identity in the opaque wake reason; equal counts aren't equal sets.
            reason = f'{len(added)} new items: ' + watch_digest(added)
            if added and _m_hosts.request_wake(inst, group['node'], reason):
                seen.update(added)  # acknowledgement means durable queue, not transport success
                count += len(added)
                queued = True
        target = group['node']
        directory = _m_hosts.wake_directory(inst, target)
        if not queued and group['command'] and directory.is_dir() and any(directory.glob('*.pending')):
            _m_hosts.wake_start(inst, target)  # retries queued failures without another model tick
    # Screened current excerpts are recomputed; historical acknowledgement stores only digests.
    _m_machine.machine_publish(inst, path, json.dumps({'version': 1, 'at': inst.now(),
                                'seen': sorted(seen), 'failures': sorted(failures), 'items': current}, sort_keys=True) + '\n')
    print(f'watch: {count} new item(s) queued')


def watch_once(inst):
    """Share the sampler's short, nonblocking instance-lock discipline."""
    import fcntl
    folder = inst.root / '.brain'
    if _m_common.outward_links(inst.root):
        _m_common.die('watch: unsafe instance links; no wake sent')
    _m_common.safe_target(folder / 'lock')
    fd = os.open(folder / 'lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'r+') as stream:
        if os.fstat(stream.fileno()).st_nlink != 1:
            _m_common.die('watch: instance lock is hard-linked')
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('watch: skipped; instance busy')
            return
        watch_run(inst)


def cmd_watch(a):
    try:
        watch_once(_m_nodes.load())
    except (OSError, ValueError):
        _m_common.die('watch: invalid or unreadable local state; retained for repair')
