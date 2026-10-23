"""File-only actionable-work watcher; no host queries, provider calls or model turns."""
import hashlib
import json
import os
import re
import shlex
from pathlib import Path

from . import common as _m_common
from . import guard as _m_guard
from . import hosts as _m_hosts
from . import instance as _m_instance
from . import jobs as _m_jobs
from . import machine as _m_machine
from . import nodes as _m_nodes
from . import status as _m_status
from . import transitions as _m_transitions
from . import usage as _m_usage
from . import upgrade as _m_upgrade
from . import verify as _m_verify


WATCH_LOCAL_KINDS = ('unsettled-threads',)


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


def watch_text_identity(item):
    """Keep the need, not its timestamp metadata or generated display counters."""
    text = item['text']
    if item['kind'] == 'blocked-overdue':
        text = re.sub(r';\s*since:\s*[^;]*', '', text, flags=re.I)
    if item['kind'] == 'inbox':
        text = re.sub(r'^\d{4}-\d\d-\d\d[ T][0-9:.]+(?:Z|\s*[+-]\d\d:\d\d)?\s+', '', text)
    return ' '.join(text.split())


def watch_due_episodes(inst, scan):
    """Persist opaque receipts while ordinary due needs remain open.

    Readers and drains serialize the source scan and reconciliation together,
    so an older scan cannot erase a newer receipt. A scan closes absent needs.
    Machine and direct producer episodes already have their own receipts.
    """
    import fcntl
    path = inst.root / '.brain/wake/due-episodes.json'
    lock = path.with_suffix('.lock')
    if _m_common.outward_links(inst.root):
        raise ValueError('unsafe due episode state')
    path.parent.mkdir(exist_ok=True)
    _m_common.safe_target(lock)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a+') as stream:
        if os.fstat(stream.fileno()).st_nlink != 1:
            raise ValueError('linked due episode lock')
        fcntl.flock(stream, fcntl.LOCK_EX)
        groups = scan()
        entries = [i for group in groups.values() for i in group['items']
                   if 'reason' not in i and i['kind'] not in
                   ('machine-hold', 'machine-alert', 'machine-repair', 'job-ready', 'job-landed', 'rotate')]
        _m_common.safe_target(path)
        data = watch_read(path, {'version': 1, 'open': {}})
        if (data.get('version') != 1 or not isinstance(data.get('open'), dict)
                or any(not re.fullmatch(r'[0-9a-f]{64}', k)
                       or not isinstance(v, str) or not re.fullmatch(r'[0-9a-f]{64}', v)
                       for k, v in data['open'].items())):
            raise ValueError('invalid due episode state')
        # Preserve pre-upgrade acknowledgements rather than replaying the backlog.
        seen = set(watch_state(inst).get('seen', [])) if not path.exists() else set()
        current = {}
        for item in entries:
            key = item['id']
            current.setdefault(key, data['open'].get(key) or (key if key in seen else
                               watch_digest(['due-episode'])[:16] + os.urandom(24).hex()))
            item['id'] = current[key]
        if current != data['open'] or not path.exists():
            _m_machine.machine_publish(inst, path, json.dumps({'version': 1, 'open': current}, sort_keys=True) + '\n')
    return groups


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
                                include_ready=False, include_advice=False) + _m_status.changes_due(inst, n)
    out = []
    for item in items:
        if item['kind'] in ('stale-status', 'machine'):
            continue
        direct_reason = None
        handback_episode = None
        if item['kind'] == 'inbox':
            marker = re.search(r'<!-- (?:host-question|handback):[^>]+ -->', item['text'])
            direct_reason = marker.group() if marker else None
            episode = re.search(r'<!-- wake:([0-9a-f]{32}) -->', item['text'])
            if episode and marker:
                direct_reason = 'wake:' + episode[1]
        if item['kind'] == 'handback' and item.get('state') in ('question', 'blocked'):
            row = next(r for r in rows if _m_jobs.job_key(r) == item['job'])
            if any(f"from worker {row['Job']}: {item['state']}:" in line
                   and '<!-- handback:' in line for line in state['inbox']):
                continue  # the associated inbox item owns this same wake
            episode = (signals.get(item['job']) or {}).get('wake_id')
            if isinstance(episode, str) and re.fullmatch(r'[0-9a-f]{32}', episode):
                handback_episode = episode
            direct_reason = ('wake:' + handback_episode if handback_episode else
                             'handback:' + _m_jobs.digest([item['job'], item['state']]))
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
        identity = (item.get('episode') if item['kind'] == 'machine-alert' else
                    [item.get('job'), item.get('state'), watch_text_identity(item)] if item.get('job') else
                    watch_text_identity(item))
        if item['kind'] == 'rotate':
            sessions = n['local'].get('coordinator_sessions', [])
            category = ('repair' if item['text'].startswith('rotation sidecar') else
                        'session' if item['text'].startswith('append the new coordinator') else 'limit')
            identity = [n['local'].get('coordinator'), sessions[-1] if isinstance(sessions, list) and sessions else None,
                        category]
        if item['kind'] == 'handback':
            identity = [item.get('job') or watch_text_identity(item), item.get('state')]
            if item.get('state') == 'done':
                signal = signals.get(item.get('job')) or {}
                episode = signal.get('wake_id')
                handback_episode = (episode if isinstance(episode, str) and re.fullmatch(r'[0-9a-f]{32}', episode)
                                    else 'legacy')
            if handback_episode:
                identity.append(handback_episode)
        text = _m_nodes.import_text(item['text'], _m_guard.never_terms(n))[:400]
        entry = {'id': watch_digest([n['id'], item['kind'], identity]),
                 'kind': item['kind'], 'text': text}
        if direct_reason:
            entry.update(reason=direct_reason, source=n['id'])
        out.append(entry)
    return out


def watch_recorded_ready(inst, n, row, signal, strict=False):
    """Validate a stored success for notification, never run a readiness/verify path.

    Cached refs fence stale evidence. This notification is not merge authority and
    does not replace the coordinator's full readiness/input/rehearsal checks.
    """
    if _m_jobs.handback_file(n, row).exists():
        if not signal or signal.get('version') != 2 or signal.get('state') != 'done' or signal.get('ready_reasons') != []:
            return None
        packet = signal.get('ready')
    else:
        # Load only legacy stored evidence; a newer signal always wins. Strict
        # pending reads preserve unknown I/O failures instead of retiring notices.
        if not n['external'] or not n['home_path']:
            return None
        try:
            packet = json.loads(_m_verify.ready_path(inst, n, row['Job']).read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            if strict:
                raise
            return None
    if not isinstance(packet, dict):
        return None
    checked = inst.parse_time(packet.get('checked_at')) if isinstance(packet.get('checked_at'), str) else None
    started = inst.parse_time(row.get('Started'))
    sha = lambda value: isinstance(value, str) and re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value)
    if (type(packet.get('version')) is not int or packet['version'] != 1
            or packet.get('node') != n['id'] or packet.get('branch') != row['Job']
            or type(packet.get('verify_result')) is not int or packet['verify_result'] != 0
            or type(packet.get('review_rounds')) is not int or packet['review_rounds'] < 1
            or not checked or not started or checked < started or checked > inst.now_dt()
            or not n['fm'].get('verify', '').strip() or packet.get('verify_command') != n['fm']['verify'].strip()
            or not isinstance(packet.get('inputs'), str) or not re.fullmatch(r'[0-9a-f]{64}', packet['inputs'])
            or not all(sha(packet.get(k)) for k in ('head', 'main'))):
        return None
    lanes = packet.get('verify_lanes')
    if lanes is not None and (not isinstance(lanes, list) or not lanes or any(
            not isinstance(lane, dict) or lane.get('status') != 'passed'
            or type(lane.get('result')) is not int or lane['result'] != 0 for lane in lanes)):
        return None
    check = packet.get('verify_lanes_check')
    if check is not None and (not isinstance(check, dict) or type(check.get('result')) is not int or check['result'] != 0):
        return None
    home = n['home_path']
    w = n['local'].get('worktrees', {}).get(row['Job'])
    if not home or not home.exists() or not isinstance(w, dict) or not w.get('path') or not Path(w['path']).is_dir():
        return None
    def cached_git(*args, allowed=(0,)):
        result = _m_common.git(*args, cwd=home)
        if result.returncode not in allowed:
            raise OSError('cached Git evidence unreadable')
        return result
    if 'origin' in cached_git('remote').stdout.split():
        ref = cached_git('symbolic-ref', '--quiet', 'refs/remotes/origin/HEAD', allowed=(0, 1)).stdout.strip()
        if not ref.startswith('refs/remotes/origin/'):
            return None
    else:
        ref = 'refs/heads/main'
    target = ref if n['fm'].get('merge', 'local') == 'pr' else ref.replace('refs/remotes/origin/', 'refs/heads/', 1)
    def tip(name):
        return cached_git('rev-parse', '--verify', '--quiet', name + '^{commit}', allowed=(0, 1)).stdout.strip()
    head, main, base = tip('refs/heads/' + row['Job']), tip(ref), tip(target)
    if (packet['head'] != head or not main or not base
            or packet.get('integration_base', packet['main']) != base
            or packet['main'] != (main if 'integration_base' in packet else base)):
        return None
    if cached_git('merge-base', '--is-ancestor', main, base, allowed=(0, 1)).returncode:
        return None  # legacy local bases must still contain the current cached origin default
    if _m_jobs.train_ejection(n, row, packet, strict=strict):
        return None
    return packet


def watch_completion_scope(nid, row=None, kind=None):
    """Opaque node/job-kind prefixes keep unknown reads local without queue-schema changes."""
    node = watch_digest(['completion-node', nid])[:16]
    return node if row is None else node + watch_digest(['completion-job', _m_jobs.job_key(row), kind])[:16]


def watch_completion_items(inst, nodes=None, strict=False, unknown=None):
    """Opaque per-packet/per-ledger-transaction root notices, batched by watch_run."""
    out = []
    unknown = unknown if unknown is not None else set()
    for n in nodes if nodes is not None else inst.nodes():
        if n['archived']:
            continue
        try:
            rows = _m_jobs.read_jobs(n)
        except (Exception, SystemExit):
            rows = None
        if rows is None:
            if strict:
                unknown.add(watch_completion_scope(n['id']))
            continue
        for row in rows or []:
            kind, evidence = None, None
            try:
                if row['Ended']:
                    start, end = inst.parse_time(row['Started']), inst.parse_time(row['Ended'])
                    if row['Outcome'] == 'merged' and start and end and end >= start:
                        kind = 'job-landed'
                        evidence = 'landed'  # the job key owns this transaction, not bookkeeping
                else:
                    signal = _m_jobs.read_handback(n, row, strict=strict)
                    packet = watch_recorded_ready(inst, n, row, signal, strict=strict)
                    if packet:
                        episode = (signal or {}).get('wake_id')
                        kind, evidence = 'job-ready', (episode if isinstance(episode, str) and
                            re.fullmatch(r'[0-9a-f]{32}', episode) else 'legacy')
                if kind:
                    identity = watch_completion_scope(n['id'], row, kind) + watch_digest([n['id'], _m_jobs.job_key(row), kind, evidence])[:32]
                    out.append({'id': identity,
                                'node': 'root', 'kind': kind,
                                'text': ('A job has new recorded ready evidence; inspect coordinator due queues'
                                         if kind == 'job-ready' else
                                         'A job has landed in its ledger; inspect coordinator due queues')})
            except (Exception, SystemExit):
                # Reader exits and unexpected errors belong to this job, not the
                # whole inventory. KeyboardInterrupt still stops the process.
                if strict:
                    unknown.add(watch_completion_scope(n['id'], row, 'job-landed' if row['Ended'] else 'job-ready'))
    return out


def watch_route_allows_switch(reason):
    # TODO(router): replace prose compatibility with the requested structured eligibility flag.
    return 'Stop starting jobs' not in reason


def watch_retry(inst, n, row, record, marker):
    old = record.get('runtime')
    new = next((r for r in inst.runtimes if r != old and r in ('codex', 'claude')), None)
    # A node pin is an explicit operator choice, never silently overridden for recovery.
    pin = n['fm'].get('runtime')
    if old not in ('codex', 'claude') or not new or (pin and pin != new):
        return ''
    klass = _m_jobs.job_class(n, row)
    if not _m_usage.class_allows_fallback(klass):
        return ''
    # The retry mechanism is reachable only if class_allows_fallback is enabled.
    selected, subscription, reason = _m_usage.class_route(inst, klass, runtime=new, cached_only=True)
    if selected != new or not watch_route_allows_switch(reason):
        return ''
    if (not row.get('Capacity retry')
            and not re.search(r' -> [^:;]+: at capacity(?:;|$)', row.get('Switches', ''))):
        switch = f'{old} -> {new}: at capacity'
        row['Switches'] = '; '.join(filter(None, [row.get('Switches'), _m_jobs.stamp(inst) + ' ' + switch]))
        row['Capacity retry'] = marker
    if row.get('Capacity retry') != marker:
        return ''
    command = 'brain brief ' + shlex.quote(n['id']) + ' --branch ' + shlex.quote(row['Job']) + ' --runtime ' + shlex.quote(new)
    return '; one retry recorded; coordinator: relaunch through host in existing worktree after `' + command + '`'


def watch_failures(inst, nodes, acknowledged, warnings):
    """Untrusted host input cannot suppress unrelated actionable work."""
    try:
        data = watch_read(inst.root / '.brain' / 'worker-runs.json', {})
        records = data.get('runs', [])
        if not isinstance(records, list):
            raise ValueError('worker runs must be a list')
    except (OSError, ValueError):
        if 'worker-runs-invalid' not in warnings:
            print('watch: worker-runs invalid; failure processing skipped')
        warnings.add('worker-runs-invalid')
        return
    warnings.discard('worker-runs-invalid')
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
            error = str(record.get('error', '')).lower()
            capacity = 'at capacity' in error
            category = 'at capacity' if capacity else ('usage limit' if 'usage limit' in error else 'provider error')
            retry = ''
            previous = dict(row)
            try:
                if capacity:
                    retry = watch_retry(inst, n, row, record, marker)
            except (SystemExit, OSError, ValueError):
                row.clear()
                row.update(previous)  # invalid routing disables only this automatic switch
            if row != previous:
                _m_jobs.write_jobs(inst, n, rows)
            text = _m_nodes.import_text(f"{row['Job']}: worker run failed ({category}){retry}", _m_guard.never_terms(n))
            _m_transitions.inbox_append(n['dir'], f'- {inst.now()} from watcher: {text} {marker}\n', marker)
            # Persist the switch and its actionable notice together; other dirty files stay untouched.
            inst.commit([n['dir'] / _m_jobs.JOBS_FILE, n['dir'] / 'inbox.md'],
                        f"{n['id']}: " + ('runtime switch at capacity' if retry else 'worker failure notice'))
            acknowledged.add(marker)


def watch_state(inst):
    path = inst.root / '.brain' / 'watch.json'
    state = watch_read(path, {})
    if path.exists() and (state.get('version') != 1 or not isinstance(state.get('seen'), list)):
        raise ValueError('invalid watcher fingerprint')
    for key in ('seen', 'failures', 'warnings'):
        if not isinstance(state.get(key, []), list) or not all(isinstance(x, str) for x in state.get(key, [])):
            raise ValueError('invalid watcher acknowledgement')
    return state


def watch_state_invalid(inst):
    try:
        watch_state(inst)
        return False
    except (OSError, ValueError):
        return True


def watch_groups(inst, nodes=None):
    """The file-only wake queue, grouped by the coordinator who can act on it."""
    return watch_due_episodes(inst, lambda: watch_scan(inst, nodes))


def watch_scan(inst, nodes=None):
    """Collect source facts only while the due receipt transaction owns its lock."""
    nodes = nodes if nodes is not None else [n for n in inst.nodes() if not n['archived']]
    groups = {}
    for n in nodes:
        # Uncoordinated nodes route actionable work to root; root needs no explicit binding.
        target, command = _m_hosts.wake_target(inst, n)
        group = groups.setdefault(target['id'], {'node': target, 'items': [], 'command': command})
        group['items'].extend({**i, 'node': n['id']} for i in watch_items(inst, n)
                              if target['id'] == n['id'] or i['kind'] not in
                              WATCH_LOCAL_KINDS)
        root = inst.node('root')
        for line in _m_status.overdue_questions(inst, n, _m_status.inbox_lines(n)):
            root_group = groups.setdefault('root', {'node': root, 'items': [],
                                            'command': _m_hosts.wake_target(inst, root)[1]})
            root_group['items'].append({'id': watch_digest(['overdue', n['id'], line]),
                                       'kind': 'overdue-question', 'node': n['id'],
                                       'reason': 'overdue:' + hashlib.sha256(line.encode()).hexdigest(),
                                       'text': 'Unanswered question past its threshold; inspect brain due root'})
    completions = watch_completion_items(inst, nodes)
    if completions:
        root = inst.node('root')
        group = groups.setdefault('root', {'node': root, 'items': [],
                                          'command': _m_hosts.wake_target(inst, root)[1]})
        group['items'].extend(completions)
    state = _m_machine.machine_state(inst)
    for group in groups.values():
        for item in _m_status.machine_due_items(inst, [group['node']], state, []):
            if item['kind'] == 'machine-repair':
                group['items'].append({**item, 'id': watch_digest(['root', item['kind']])})
                continue
            group['items'].append({**item, 'kind': 'machine-hold',
                                   'id': watch_digest([item['node'], 'machine-hold', item['episode']])})
    return groups


def watch_since_wake(inst, nodes):
    groups = watch_groups(inst)
    out = []
    for n in nodes:
        target, _ = _m_hosts.wake_target(inst, n)
        # The coordinator is handling the latest wake: compare with the wake before it,
        # so the items that just woke it remain visible. Snapshots retain older due work.
        seen = {i['id'] for i in _m_hosts.wake_previous_items(_m_hosts.wake_directory(inst, target))}
        out.extend(i for i in groups.get(target['id'], {}).get('items', []) if i['id'] not in seen
                   and (target['id'] == n['id'] or i['node'] == n['id']))
    return list({i['id']: i for i in out}.values())


def watch_run(inst):
    path = inst.root / '.brain' / 'watch.json'
    state = watch_state(inst)  # our own acknowledgement history always fails closed
    seen = set(state.get('seen', []))
    nodes = [n for n in inst.nodes() if not n['archived']]
    failures = set(state.get('failures', []))
    warnings = set(state.get('warnings', []))
    watch_failures(inst, nodes, failures, warnings)
    groups = watch_groups(inst, nodes)
    current_ids = {i['id'] for group in groups.values() for i in group['items']}
    for group in groups.values():
        for pending in _m_hosts.wake_directory(inst, group['node']).glob('*.pending'):
            _m_hosts.wake_retire_due_pending(pending, current_ids)
    current = []
    count = 0
    for group in groups.values():
        items = {i['id']: i for i in group['items']}
        current.extend(items.values())
        added = sorted(set(items) - seen)
        queued = False
        if added and group['command']:
            for key in added[:]:
                if key not in added:
                    continue
                item = items[key]
                if item['kind'] == 'machine-hold':
                    # Publish simultaneous escalation with its hold before detaching a drain.
                    bundle = [key] + [k for k in added if items[k]['kind'] == 'machine-alert']
                    accepted = _m_machine.machine_hold_claim(
                        inst, item['episode'], item['scope'],
                        deliver=lambda: _m_hosts.request_wake(inst, group['node'], 'machine-hold:' + item['episode'],
                                                             items=[items[k] for k in bundle]))
                    if accepted:
                        seen.update(bundle)
                        count += len(bundle)
                        queued = True
                        for k in bundle[1:]:
                            added.remove(k)
                    added.remove(key)
                elif 'reason' in items[key]:
                    origin = inst.node(items[key].get('source', group['node']['id']))
                    if (_m_hosts.wake_recorded(inst, origin, items[key]['reason'])
                            or _m_hosts.request_wake(inst, origin, items[key]['reason'], items=[items[key]])):
                        seen.add(key)
                        count += 1
                        queued = True
                    added.remove(key)
            # Include item identity in the opaque wake reason; equal counts aren't equal sets.
            reason = f'{len(added)} new items: ' + watch_digest(added)
            if added and _m_hosts.request_wake(inst, group['node'], reason, items=[items[k] for k in added]):
                seen.update(added)  # acknowledgement means durable queue, not transport success
                count += len(added)
                queued = True
        target = group['node']
        directory = _m_hosts.wake_directory(inst, target)
        if not queued and group['command'] and directory.is_dir() and any(directory.glob('*.pending')):
            _m_hosts.wake_start(inst, target)  # retries queued failures without another model tick
    # Screened current excerpts are recomputed; historical acknowledgement stores only digests.
    _m_machine.machine_publish(inst, path, json.dumps({'version': 1, 'at': inst.now(),
                                'seen': sorted(seen), 'failures': sorted(failures), 'warnings': sorted(warnings), 'items': current}, sort_keys=True) + '\n')
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
        stream.seek(0)
        stream.truncate()
        stream.write(str(os.getpid()))
        stream.flush()
        _m_instance._LOCK['fh'], _m_instance._LOCK['depth'] = stream, 1
        os.environ['BRAIN_LOCK_HELD'] = f'{inst.root.resolve()}|{os.getpid()}'
        try:
            watch_run(inst)
        finally:
            _m_instance._LOCK['depth'] = 0
            _m_instance.unlock()


def cmd_watch(a):
    try:
        watch_once(_m_nodes.load())
    except (OSError, ValueError):
        _m_common.die('watch: invalid or unreadable local state; retained for repair')
