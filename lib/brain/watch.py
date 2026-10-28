"""File-only actionable-work watcher; no host queries, provider calls or model turns."""
import hashlib
import contextlib
import json
import math
import os
import re
import shlex
import stat
import time
from pathlib import Path

from . import common as _m_common
from . import timings as _m_timings
from . import instance as _m_instance

_m_briefs = _m_common.LazyModule(__package__, 'briefs')
_m_guard = _m_common.LazyModule(__package__, 'guard')
_m_hosts = _m_common.LazyModule(__package__, 'hosts')
_m_jobs = _m_common.LazyModule(__package__, 'jobs')
_m_machine = _m_common.LazyModule(__package__, 'machine')
_m_nodes = _m_common.LazyModule(__package__, 'nodes')
_m_reminders = _m_common.LazyModule(__package__, 'reminders')
_m_status = _m_common.LazyModule(__package__, 'status')
_m_transitions = _m_common.LazyModule(__package__, 'transitions')
_m_usage = _m_common.LazyModule(__package__, 'usage')
_m_upgrade = _m_common.LazyModule(__package__, 'upgrade')
_m_verify = _m_common.LazyModule(__package__, 'verify')


WATCH_LOCAL_KINDS = ('unsettled-threads',)
WATCH_PASSIVE_KINDS = ('task-aging', 'task-parked')


WATCH_DECISION_KINDS = ('coordinator-queue', 'coordinator-busy', 'delegated-completion', 'question', 'handback', 'job-stall', 'blocked-overdue', 'restart', 'job-restart', 'rotate',
                        'overdue-question', 'task-ready', 'task-unblocked', 'task-stalled', 'task-provider-pending')


def watch_decision_item(item):
    if item['kind'] == 'inbox':
        return bool(item.get('reason') or re.search(r'<!-- (?:host-question|handback):[^>]+ -->', item.get('text', '')))
    if item['kind'] == 'handback':
        return item.get('state') in ('question', 'blocked', 'done')
    return item['kind'] in WATCH_DECISION_KINDS


def watch_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def watch_read(path, default):
    if not path.exists():
        return default
    # Strict readers retain damaged evidence; delivery callers may fail open.
    data = json.loads(path.read_text())
    if not isinstance(data, type(default)):
        raise ValueError("invalid watcher state")
    return data


def watch_job_stalls(inst, n):
    """Observe silent frozen/queued jobs; delivery uses the ordinary due episode receipt.

    Only local source facts are read. The first observation starts the clock rather
    than guessing when an old free-form status label was applied. Verifier log
    activity also counts as progress, so a streaming verify is never called frozen.
    """
    import fcntl
    if n['archived']:
        return []
    try:
        minutes = float(n['fm'].get('job_stall_minutes', 15))
        if not math.isfinite(minutes) or minutes < 1:
            raise ValueError('invalid stall threshold')
    except (TypeError, ValueError):
        minutes = 15
    directory = inst.root / '.brain/wake'
    path = directory / ('job-stalls-' + watch_digest(n['id']) + '.json')
    lock = path.with_suffix('.lock')
    if not path.exists() and not any(len(r) > 3 and re.match(r'^(frozen|queued)\b', r[3].casefold())
                                     for r in _m_status.active_work(n)):
        return []  # do not create private watcher state for ordinary nodes, including init
    if _m_common.outward_links(inst.root):
        raise ValueError('unsafe job stall state')
    from . import ui_readmodel
    view = ui_readmodel.projection(inst.root)
    if view is None:
        directory.mkdir(parents=True, exist_ok=True)
        _m_common.safe_target(lock)
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with contextlib.nullcontext(None) if view else os.fdopen(fd, 'a+') as stream:
        if stream is not None:
            if os.fstat(stream.fileno()).st_nlink != 1:
                raise ValueError('linked job stall lock')
            fcntl.flock(stream, fcntl.LOCK_EX)
        _m_common.safe_target(path)
        data = watch_read(path, {'version': 1, 'jobs': {}})
        if (data.get('version') != 1 or not isinstance(data.get('jobs'), dict)
                or any(not re.fullmatch(r'[0-9a-f]{64}', key) or not isinstance(value, dict)
                       or not re.fullmatch(r'[0-9a-f]{64}', str(value.get('event', '')))
                       or type(value.get('since')) not in (int, float)
                       or not math.isfinite(value['since'])
                       for key, value in data['jobs'].items())):
            raise ValueError('invalid job stall state')
        # Read sources under the observation lock; stale readers cannot restore an old clock.
        states = {r[0]: r[3] for r in _m_status.active_work(n) if len(r) > 3}
        rows = _m_jobs.read_jobs(n)
        if rows is None:
            return []  # a legacy ledger has no attributable jobs; preserve prior observations
        current, items = {}, []
        now = inst.now_dt().timestamp()
        for row in rows:
            if row['Ended']:
                continue
            state = states.get(row['Job'], '').strip().casefold()
            match = re.match(r'^(frozen|queued)\b', state)
            if (not match or re.search(r'\bwait(?:ing)? (?:on|for) (?:a |the )?(?:human|operator)\b', state)
                    or re.search(r'\b(?:human|operator) (?:choice|decision|approval)\b', state)):
                continue
            handback = _m_jobs.job_signal(inst, n, row, strict=True)
            if handback:
                if not _m_briefs.restart_acknowledged(n, row, handback):
                    continue  # current terminal/blocking signals already have their own due path
            elif _m_jobs.handback_file(n, row).exists():
                continue  # an invalid file cannot resurrect legacy evidence or imply a resumed worker
            home = n.get('home_path')
            tip = ''
            if home and home.is_dir():
                result = _m_common.git('rev-parse', '--verify', '--quiet', 'refs/heads/' + row['Job'], cwd=home)
                if result.returncode not in (0, 1):
                    raise OSError('job stall Git evidence unreadable')
                tip = result.stdout.strip()
            key = _m_jobs.job_key(row)
            logs = []
            for log in (n['dir'] / 'job-handbacks').glob(key + '*-verify-*.log'):
                if re.fullmatch(re.escape(key) + r'(?:-[0-9a-f]{16})?-verify-\d+-[0-9a-f]{12}\.log', log.name):
                    stat = log.stat()
                    logs.append([log.name, stat.st_mtime_ns, stat.st_size])
            event = watch_digest([state, tip, row.get('Reviews'), row.get('Review head'),
                                  row.get('Review kind'), row.get('Switches'), sorted(logs), handback])
            identity = watch_digest([n['id'], key])
            previous = data['jobs'].get(identity)
            since = previous['since'] if previous and previous['event'] == event else now
            current[identity] = {'event': event, 'since': since}
            if now - since >= minutes * 60:
                items.append({'node': n['id'], 'kind': 'job-stall', 'job': key,
                              'state': match[1], 'episode': watch_digest([identity, event, since]),
                              'text': _m_nodes.import_text(
                                  f"{row['Job']} {match[1]} {minutes:g} min without an event; inspect the job",
                                  _m_guard.never_terms(n))})
        if view is None and (current != data['jobs'] or not path.exists()):
            _m_machine.machine_publish(inst, path, json.dumps({'version': 1, 'jobs': current}, sort_keys=True) + '\n')
    return items


def watch_text_identity(item):
    """Keep the need, not its timestamp metadata or generated display counters."""
    text = item['text']
    if item['kind'] == 'blocked-overdue':
        text = re.sub(r';\s*since:\s*[^;]*', '', text, flags=re.I)
    if item['kind'] == 'inbox':
        text = re.sub(r'^\d{4}-\d\d-\d\d[ T][0-9:.]+(?:Z|\s*[+-]\d\d:\d\d)?\s+', '', text)
    return ' '.join(text.split())


def watch_source_signature(inst, layout=None):
    """Restat sources once each, rehashing only changed source generations.

    Directory entries supply their own stat data; overlapping task/Git/resource
    inventories share it. Never use a directory's mtime to infer its children's
    contents: an in-place rewrite leaves that mtime unchanged.
    """
    from . import events
    inst._load_local()  # malformed binding evidence must still fail closed
    nodes = list(inst.nodes(fresh=True))
    paths = [inst.root / 'instance.md', inst.root / '.brain/local.json']
    paths += [n['dir'] / name for n in nodes for name in ('node.md', 'never-name.txt', 'tasks', 'requirements')]
    layout = {} if layout is None else layout
    homes = []
    for n in nodes:
        if n.get('external') and n.get('home_path'):
            home = n['home_path']
            paths.extend(_m_instance.git_state_paths(home, locations=layout.setdefault('git_locations', {})))
            # Fence the logical home and marker even when Git resolves both to
            # the same metadata directory. Never descend into the repository.
            homes.append([str(home), str(home.resolve()), str((home / '.git').resolve())])
    paths += [inst.root / '.brain' / name for name in (
        'communications.json', 'worker-runs.json', 'host-observations.json',
        'host-tasks.json', 'machine.json', 'rotation')]
    paths.append(_m_machine.machine_dir() / 'machine-care.json')
    paths.append(inst.root / '.brain/inbox-seen.json')
    generations = {}
    old = layout.get('sources', {})
    current = {}

    def observe(path, info=None):
        key = os.fspath(path)
        if key in generations:
            return
        try:
            info = info or os.lstat(key)
            stamp = (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
                     info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            if stat.S_ISLNK(info.st_mode):
                stamp += (os.readlink(key),)
                try:
                    target = os.stat(key)
                    stamp += (target.st_dev, target.st_ino, target.st_mode, target.st_nlink,
                              target.st_size, target.st_mtime_ns, target.st_ctime_ns)
                except (FileNotFoundError, NotADirectoryError):
                    stamp += (None,)
        except (FileNotFoundError, NotADirectoryError):
            stamp = None
        cached = old.get(key)
        digest = cached[1] if cached and cached[0] == stamp else watch_digest([key, stamp])
        generations[key] = digest
        current[key] = (stamp, digest)

    visited = set()
    def tree(folder):
        key = os.fspath(folder)
        if key in visited:
            return
        visited.add(key)
        observe(folder)
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    observe(entry.path, entry.stat(follow_symlinks=False))
                    if entry.is_dir(follow_symlinks=False):
                        tree(entry.path)
        except (FileNotFoundError, NotADirectoryError):
            pass

    def resources(folder, deep=False):
        with os.scandir(folder) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name == 'nodes':
                        with os.scandir(entry.path) as children:
                            for child in children:
                                if child.is_dir(follow_symlinks=False) and os.path.isfile(os.path.join(child.path, 'node.md')):
                                    resources(child.path)
                    elif deep or entry.name in events.STATE_DIRS:
                        resources(entry.path, True)
                elif entry.is_file() and os.path.splitext(entry.name)[1] in ('.md', '.json', '.txt', '.sh'):
                    observe(entry.path, entry.stat(follow_symlinks=False))

    # The ordinary resource set excludes transcripts, objects, reports and caches.
    resources(inst.root)
    for n in nodes:
        wb = n['local'].get('workbench')
        if wb:
            folder = Path(wb).expanduser() / 'scratch/ready'
            if folder.is_dir():
                for path in folder.glob('*.json'):
                    observe(path)
    for path in paths:
        observe(path)
        if path.is_dir() and not path.is_symlink():
            tree(path)
    layout['sources'] = current
    return watch_digest([homes, sorted(generations.items())])


def watch_untracked(groups, generation=None, *, complete=True):
    """Unknown episode state must not reuse a transport key and hide a new need."""
    for group in groups.values():
        group['episodes_known'] = False
        for item in group['items']:
            if (('reason' not in item or item['kind'] == 'overdue-question') and item['kind'] not in
                    ('machine-hold', 'machine-alert', 'machine-repair', 'job-ready', 'job-landed', 'rotate')):
                item['id'] = watch_digest([item['id'], os.urandom(24).hex()])
                if item['kind'] == 'overdue-question':
                    item['legacy_reason'] = item['reason']
                    item['reason'] = 'overdue:' + item['id']
    return WatchGroups(groups, None, generation, complete=complete)


class WatchGroups(dict):
    def __init__(self, values, revision, generation=None, stale=False, layout=None, retained=(), complete=True, retired=()):
        super().__init__(values)
        self.receipt_revision = revision
        self.source_generation = generation
        self.stale = stale
        self.source_layout = layout
        self.retained_ids = set(retained)
        self.complete = complete
        self.retired = frozenset(retired)


def watch_receipt_revision(inst):
    with _m_common.directory_target(inst.root / '.brain/wake/due-episodes.json') as target:
        with os.fdopen(target.open()) as stream:
            return target.generation(os.fstat(stream.fileno()))


def watch_receipt_observed(inst):
    """The receipt generation before a scan: None when absent, False when unreadable."""
    try:
        return watch_receipt_revision(inst)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, SystemExit):
        return False


def watch_queue_unknown(inst, entries):
    """Find recorded queue episodes omitted while their projection is unknown."""
    from . import coordinator_advisories as advisories
    path = inst.root / '.brain/coordinator-queue.json'
    try:
        _m_common.safe_target(path)
        recorded = advisories.coordinator_conditions_read(path)
    except (OSError, ValueError, TypeError, AttributeError):
        return set()
    if not recorded:
        return set()
    present = {i['id'] for i in entries}
    now = inst.now_dt().timestamp()
    host = advisories.coordinator_host_runs(inst, now)
    unknown = set()
    for n in inst.nodes():
        condition = recorded.get(n['id'])
        if (n['archived'] or not condition or condition['severity'] is None
                or condition['thread'] != n['local'].get('coordinator')):
            continue
        episode = watch_digest([n['id'], condition['thread'], 'coordinator-queue',
                                [condition['start'], condition['severity']]])
        key = watch_digest(['root', 'coordinator-queue', episode])
        if key not in present and advisories.coordinator_queue_view(inst, n, now, host.get(n['id']))['count'] is None:
            unknown.add(key)
    return unknown


def watch_due_episodes(inst, scan, serialized=False, blocking=True, layout=None, overdue_only=False):
    """Persist opaque receipts while ordinary due needs remain open.

    The source scan runs before the receipt lock; only reconciliation holds it.
    A scan reconciles only if the receipt it observed before scanning is still
    current, so overlapping scans serialize as if each ran alone; one that lost
    is stale and cannot erase a newer receipt. A scan closes absent needs.
    `serialized` holds the lock across the scan, as before, for a caller whose
    optimistic scan was already overtaken. Machine and direct producer
    episodes already have their own receipts. A caller's `layout` carries
    stamp-validated source generations into its rescan and publication fence.
    Background callers take nonblocking admission before scanning; busy
    admission leaves receipts untouched.
    """
    import fcntl
    serialized = serialized or not blocking
    path = inst.root / '.brain/wake/due-episodes.json'
    lock = path.with_suffix('.lock')
    if _m_common.outward_links(inst.root):
        raise ValueError('unsafe due episode state')
    path.parent.mkdir(exist_ok=True)
    _m_common.safe_target(lock)
    def acquire():
        stream = stack.enter_context(os.fdopen(os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), 'a+'))
        if os.fstat(stream.fileno()).st_nlink != 1:
            raise ValueError('linked due episode lock')
        with _m_timings.phase('episode_lock_wait'):
            fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        stack.enter_context(_m_timings.phase('episode_lock_hold'))
    with contextlib.ExitStack() as stack:
        if serialized:
            try:
                acquire()  # fallback/background: admit before scanning sources
            except BlockingIOError:
                return WatchGroups({}, None, stale=True, complete=not overdue_only)
        observed = watch_receipt_observed(inst)
        layout = {} if layout is None else layout
        generation = watch_source_signature(inst, layout)
        groups = scan()
        entries = [i for group in groups.values() for i in group['items']
                   if ('reason' not in i or i['kind'] == 'overdue-question') and i['kind'] not in
                   ('machine-hold', 'machine-alert', 'machine-repair', 'job-ready', 'job-landed', 'rotate')]
        queue_unknown = set() if overdue_only else watch_queue_unknown(inst, entries)
        if watch_source_signature(inst, layout) != generation:
            return WatchGroups({}, None, generation, stale=True, layout=layout, complete=not overdue_only)
        overdue_keys = [i['id'] for i in entries if i['kind'] == 'overdue-question']
        if not serialized:
            acquire()
        _m_common.safe_target(path)
        try:
            data = watch_read(path, {'version': 1, 'open': {}})
            if (data.get('version') != 1 or not isinstance(data.get('open'), dict)
                    or type(data.get('overdue_episodes', False)) is not bool
                    or not isinstance(data.get('queue_unknown', {}), dict)
                    or any(k not in data['open'] or type(v) is not int or not 1 <= v <= 3
                           for k, v in data.get('queue_unknown', {}).items())
                    or any(not re.fullmatch(r'[0-9a-f]{64}', k)
                           or not isinstance(v, str) or not re.fullmatch(r'[0-9a-f]{64}', v)
                           for k, v in data['open'].items())):
                raise ValueError('invalid due episode state')
            previous_overdue = data.get('overdue_keys')
            if previous_overdue is not None and (not isinstance(previous_overdue, list)
                    or any(not isinstance(k, str) or k not in data['open'] for k in previous_overdue)):
                raise ValueError('invalid overdue episode ownership')
            if overdue_only and previous_overdue is None:
                # A legacy/full writer may have replaced the ownership metadata
                # during this scan. Do not guess which opaque receipts to close.
                return WatchGroups({}, None, generation, stale=True, layout=layout, complete=False)
        except (OSError, ValueError):
            return watch_untracked(groups, generation, complete=not overdue_only)
        # Another reconciliation since this scan's observation may reflect newer sources.
        if observed is False or watch_receipt_observed(inst) != observed:
            return WatchGroups({}, None, generation, stale=True, layout=layout, complete=not overdue_only)
        # Preserve pre-upgrade acknowledgements rather than replaying the backlog.
        try:
            legacy_seen = set(watch_state(inst).get('seen', []))
        except (OSError, ValueError):
            legacy_seen = set()
        seen = legacy_seen if not path.exists() else set()
        overdue_seen = legacy_seen if not data.get('overdue_episodes') else set()
        # A narrow scan owns only overdue questions. Full scans still replace
        # the entire inventory, including after upgrading a legacy receipt.
        owned = set(previous_overdue or ())
        current = {k: v for k, v in data['open'].items() if k not in owned} if overdue_only else {}
        for item in entries:
            key = item['id']
            acknowledged = key in seen or (item['kind'] == 'overdue-question' and
                           item.get('legacy_id', key) in overdue_seen)
            current.setdefault(key, data['open'].get(key) or (item.get('legacy_id', key) if acknowledged else
                               watch_digest(['due-episode'])[:16] + os.urandom(24).hex()))
            item['id'] = current[key]
            if item['kind'] == 'overdue-question':
                item['legacy_reason'] = item['reason']
                item['reason'] = 'overdue:' + item['id']
        # A narrow overdue scan does not observe coordinator queues, so it
        # preserves their unknown-read counters without advancing their age.
        misses = (dict(data.get('queue_unknown', {})) if overdue_only else
                  {key: data.get('queue_unknown', {}).get(key, 0) + 1
                   for key in queue_unknown & data['open'].keys()})
        misses = {key: count for key, count in misses.items() if count <= 3}
        retained = {key: data['open'][key] for key in misses}
        current.update(retained)
        try:
            # Atomic replacement supplies a new generation even for an unchanged scan.
            _m_machine.machine_publish(inst, path, json.dumps(
                {'version': 1, 'open': current, 'overdue_episodes': True,
                 'overdue_keys': sorted(overdue_keys), 'queue_unknown': misses}, sort_keys=True) + '\n')
            revision = watch_receipt_revision(inst)
        except OSError:
            return watch_untracked(groups, generation, complete=not overdue_only)
    retired = {data['open'][key] for key in owned - set(overdue_keys)} if overdue_only else set()
    return WatchGroups(groups, revision, generation, layout=layout, retained=retained.values(), complete=not overdue_only, retired=retired)


def watch_items(inst, n):
    """Due semantics, excluding refresh-only work. Never runs cmd_due's upgrade/network path."""
    rows = _m_jobs.read_jobs(n)
    worktrees = {b: {**w, **_m_status.local_job_state(n, b, w)}
                 for b, w in n['local'].get('worktrees', {}).items()}
    signals = {_m_jobs.job_key(r): _m_jobs.job_signal(inst, n, r) for r in rows or []}
    state = {'jobs': rows, 'worktrees': worktrees, 'handbacks': signals, 'inbox': _m_status.inbox_lines(n)}
    visible = n['local'].get('wake_policy') == 'visible'
    own = _m_hosts.wake_target(inst, n)[0]['id'] == n['id']
    # The canonical file state keys the memoized source inventory exactly as the
    # coordinator rollup does, so a node shown with readiness is scanned once.
    evidence = _m_status.due_evidence(inst, n) if visible and own else (_m_status.file_state(inst, n), '', '')
    items = _m_status.due_items(inst, n, evidence=evidence, include_stale=visible and own,
                                include_ready=False, include_advice=visible and own) + _m_status.changes_due(inst, n)
    out = []
    for item in items:
        if (item['kind'] == 'machine' or item['kind'] == 'stale-status' and not visible) or (item['kind'] == 'inbox' and item.get('informational')):
            continue
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
        if item['kind'] in WATCH_PASSIVE_KINDS:
            continue  # parked work and age remain visible in due, without a doorbell
        entry = watch_item_entry(n, item, rows, signals, state['inbox'])
        if entry is not None:
            out.append(entry)
    return out


def watch_item_entry(n, item, rows, signals, inbox):
    """The canonical wake entry for one ordinary due item; None when its inbox line owns the wake.

    Shared by the scan and by a producer that has just published a direct
    episode, so a supplied wake ref is exactly the identity, kind, question
    flag and direct reason a later scan computes.
    """
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
               and '<!-- handback:' in line for line in inbox):
            return None  # the associated inbox item owns this same wake
        episode = (signals.get(item['job']) or {}).get('wake_id')
        if isinstance(episode, str) and re.fullmatch(r'[0-9a-f]{32}', episode):
            handback_episode = episode
        direct_reason = ('wake:' + handback_episode if handback_episode else
                         'handback:' + _m_jobs.digest([item['job'], item['state']]))
    # Keep identity separate from changing display (readiness, age, counts).
    identity = (item.get('episode') if item.get('episode') else
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
    semantic = _m_reminders.semantic_item_key(item)
    entry = {'id': watch_digest(semantic if semantic is not None else [n['id'], item['kind'], identity]),
             'kind': item['kind'], 'text': text}
    if item['kind'] == 'handback':
        entry['state'] = item.get('state')
    if direct_reason:
        entry.update(reason=direct_reason, source=n['id'])
    return entry


def watch_handback_wake_items(inst, n, branch, attempt_id, wake_id, line=None):
    """Typed refs for a just-published direct question/blocked handback, or None.

    The producer already knows its episode, so it never rediscovers it through
    an all-node scan. Only the current signal of this job, attempt and episode,
    in the form a scan of this node shows as direct, gets refs; a restart, a
    replaced or legacy signal, a merged worktree, an older blocked inbox line
    that owns the wake, a handled reminder or unknown state returns None,
    keeping request_wake's scan fallback.
    """
    import copy
    from . import inbox_notices
    from . import job_attempts as _m_attempts
    if n['archived'] or not isinstance(wake_id, str) or not re.fullmatch(r'[0-9a-f]{32}', wake_id):
        return None
    reason = 'wake:' + wake_id
    if line is not None:
        # The scan sees the durable line, never the bullet as written: an append
        # onto an unterminated line or a filtered request is a different form.
        if (inbox_notices.informational_notice(line)
                or line not in _m_status.inbox_lines(n, readonly=True)):
            return None
        item = {'node': n['id'], 'kind': 'inbox', 'text': _m_guard.redact(line[2:], _m_guard.never_terms(n))}
        entry = watch_item_entry(n, item, [], {}, [])
    else:
        row = _m_jobs.open_job(_m_jobs.read_jobs(n) or [], branch)
        projection = _m_attempts.job_projection(inst, n, row) if row else None
        signal = projection and projection['signal']  # what due reports, not merely the file
        if (not signal or signal.get('state') != 'blocked' or signal.get('restart')
                or signal.get('attempt_id') != attempt_id or signal.get('wake_id') != wake_id):
            return None
        w = n['local'].get('worktrees', {}).get(branch)
        if w and _m_status.local_job_state(n, branch, w).get('merged'):
            return None  # a merged job is reported as such, never as this handback
        inbox = n['dir'] / 'inbox.md'
        # Raw lines are a superset of the scan's filtered inbox: never miss an owning line.
        lines = [l for l in inbox.read_text().splitlines() if l.startswith('- ')] if inbox.exists() else []
        key = _m_jobs.job_key(row)
        need = _m_jobs.digest({k: signal.get(k) for k in ('state', 'summary', 'question', 'report', 'lessons', 'restart')})
        item = {'node': n['id'], 'kind': 'handback', 'job': key, 'state': 'blocked', 'text': branch,
                'reasons': signal.get('ready_reasons', []), 'episode': projection['episode_id'], 'need': need,
                'protected': bool(signal.get('question'))}
        # A coordinator may already have acknowledged this need; the scan then
        # omits it. Read the same reminder receipts without changing them.
        try:
            with _m_reminders.lifecycle_store(inst, readonly=True) as data:
                current = _m_reminders.reminder_reconcile(inst, n, [item], copy.deepcopy(data))
        except (OSError, ValueError):
            return None  # unknown suppression state: the scan decides
        if _m_reminders.reminder_handled(inst, current[0]):
            return None
        entry = watch_item_entry(n, item, [row], {key: signal}, lines)
    if entry is None or entry.get('reason') != reason or entry.get('source') != n['id']:
        return None
    return [entry]


def watch_recorded_ready(inst, n, row, signal, strict=False):
    """Validate a stored success for notification, never run a readiness/verify path.

    Cached refs fence stale evidence. This notification is not merge authority and
    does not replace the coordinator's full readiness/input checks.
    """
    from . import job_attempts as _m_attempts
    projection = _m_attempts.job_projection(inst, n, row, strict=strict)
    signal = projection['signal']
    if projection['attempt'] and not signal:
        return None
    if _m_jobs.handback_file(n, row).exists():
        if not signal or signal.get('version') not in (2, 3) or signal.get('state') != 'done' or signal.get('ready_reasons') != []:
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
                                'node': 'root', 'source': n['id'], 'kind': kind,
                                'text': ('A job has new recorded ready evidence; inspect coordinator due queues'
                                         if kind == 'job-ready' else
                                         'A job has landed in its ledger; inspect coordinator due queues')})
            except (Exception, SystemExit):
                # Reader exits and unexpected errors belong to this job, not the
                # whole inventory. KeyboardInterrupt still stops the process.
                if strict:
                    unknown.add(watch_completion_scope(n['id'], row, 'job-landed' if row['Ended'] else 'job-ready'))
    return out


def watch_completion_target(inst, item):
    source = inst.node(item.get('source', 'root'))
    target, _ = _m_hosts.wake_target(inst, source)
    return target if target['local'].get('wake_policy') == 'visible' else inst.node('root')


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
        warnings.discard('worker-runs-invalid')
    except (OSError, ValueError):
        if 'worker-runs-invalid' not in warnings:
            print('watch: worker-runs invalid; failure processing skipped')
        warnings.add('worker-runs-invalid')
        records = []
    from . import host_observations as _m_host_observations
    try:
        records += _m_host_observations.host_export_runs(inst)
    except (OSError, ValueError, TypeError):
        pass  # unknown host export never suppresses manual records or other source work
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


def watch_state(inst, path=None):
    path = path if path is not None else inst.root / '.brain' / 'watch.json'
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


def watch_groups(inst, nodes=None, episode_blocking=True, *, overdue_only=False):
    """File-only wake queue; foreground waits, background defers unknown scans."""
    from . import command_reads, communication
    started = time.perf_counter()
    communication.communication_reconcile(inst)  # defaults precede observation, including fallback scans
    # Direct wake drains and snapshots enter here without watch_once's read
    # scope. Batch their transcript/Git observations too. Each standalone scan
    # needs its own scope, including fallback after a failed first observation;
    # cached old reads must never acquire the fallback's newer source signature.
    # Existing receipts are opaque. One ordinary scan first records which keys
    # belong to overdue questions; later due invocations can reconcile just
    # those keys without closing other kinds or recomputing their source facts.
    narrow = False
    if overdue_only and nodes is None:
        try:
            data = watch_read(inst.root / '.brain/wake/due-episodes.json', {})
            narrow = isinstance(data.get('overdue_keys'), list)
        except (OSError, ValueError):
            pass
    def scan():
        from . import tasks
        with command_reads.snapshot(), (contextlib.nullcontext() if narrow else tasks.record_scan(inst)):
            return watch_scan(inst, nodes, overdue_only=True) if narrow else watch_scan(inst, nodes)
    layout = {}  # stamp-validated generations only; each scan still reads afresh
    try:
        groups = watch_due_episodes(inst, scan, blocking=episode_blocking, layout=layout, overdue_only=narrow)
        if groups.stale and episode_blocking and not command_reads.active():
            # Overtaken (or sources moved): rescan fresh inside the receipt lock,
            # which no reconciliation can overtake. Only this rare second scan
            # holds the lock long. Inside an enclosing read scope a rescan would
            # reuse old reads, so those callers keep stale as unknown.
            groups = watch_due_episodes(inst, scan, serialized=True, layout=layout, overdue_only=narrow)
    except (OSError, ValueError):
        if _m_common.outward_links(inst.root):
            raise  # fail open for receipt I/O, never for unsafe links
        _m_status.due_once_forget()  # the fallback's due inventory follows its own generation
        generation = watch_source_signature(inst, layout)
        groups = scan()
        if watch_source_signature(inst, layout) != generation:
            groups = WatchGroups({}, None, generation, stale=True, layout=layout, complete=not narrow)
        else:
            groups = watch_untracked(groups, generation, complete=not narrow)
    if groups.stale:
        _m_timings.record('episode_deferred', time.perf_counter() - started)
    return groups


def watch_inbox_rollup(inst, group):
    """Include every fresh printed inbox item without acknowledging the reporter.

    Reporter-scoped identities keep root's rollup independent of child delivery.
    Work already in the reporter's due queue retains its ordinary source episode.
    """
    reporter = group['node']['id']
    nodes = [n for n in inst.nodes() if reporter == 'root' or n['id'] == reporter
             or n['id'].startswith(reporter + '/')]
    items, _ = _m_status.inbox_items(inst, nodes, reporter, readonly=True)
    def key(item):
        return (item['node'], item['kind'], ' '.join(item['text'].split()))
    visible = {key(i) for i in group['items']}
    for item in items:
        if item['reported'] or item['kind'] in WATCH_PASSIVE_KINDS:
            continue
        n = inst.node(item['node'])
        text = item['text'].removeprefix('inbox: ') if item['kind'] == 'inbox' else item['text']
        entry = {'node': item['node'], 'kind': item['kind'],
                 'text': _m_nodes.import_text(text, _m_guard.never_terms(n))[:400],
                 'id': watch_digest(['inbox-rollup', reporter, item['id']])}
        if key(entry) not in visible:
            group['items'].append(entry)
            visible.add(key(entry))


def watch_scan(inst, nodes=None, *, overdue_only=False):
    """Collect facts with one inventory; receipt/publication fences stay fresh."""
    with inst.node_observation():
        return watch_scan_observed(inst, nodes, overdue_only=overdue_only)


def watch_scan_observed(inst, nodes=None, *, overdue_only=False):
    # A supplied selection carries IDs, not reusable records from an older scan.
    selected = None if nodes is None else {n['id'] for n in nodes}
    nodes = [n for n in inst.nodes() if not n['archived'] and (selected is None or n['id'] in selected)]
    groups = {}
    for n in nodes:
        # Uncoordinated nodes route actionable work to root; root needs no explicit binding.
        target, command = _m_hosts.wake_target(inst, n)
        group = groups.setdefault(target['id'], {'node': target, 'items': [], 'command': command})
        group['items'].extend({**i, 'node': n['id']} for i in ([] if overdue_only else watch_items(inst, n))
                              if target['id'] == n['id'] or i['kind'] not in
                              WATCH_LOCAL_KINDS)
        root = inst.node('root')
        for line in _m_status.overdue_questions(inst, n, _m_status.inbox_lines(n)):
            root_group = groups.setdefault('root', {'node': root, 'items': [],
                                            'command': _m_hosts.wake_target(inst, root)[1]})
            identity = watch_text_identity({'kind': 'inbox', 'text': line[2:]})
            root_group['items'].append({'id': watch_digest(['overdue', n['id'], identity]),
                                       'legacy_id': watch_digest(['overdue', n['id'], line]),
                                       'kind': 'overdue-question', 'node': n['id'],
                                       'reason': 'overdue:' + hashlib.sha256(line.encode()).hexdigest(),
                                       'text': 'Unanswered question past its threshold; inspect brain due root'})
    if overdue_only:
        return groups
    from . import communication as _m_communication
    from . import host_observations as _m_host_observations
    try:
        from . import host_tasks as _m_host_tasks
        host_items = (_m_host_observations.host_observation_items(inst, nodes) +
                      _m_host_tasks.host_task_items(inst, nodes))
    except (OSError, ValueError, TypeError):
        host_items = []
    for item in _m_communication.communication_items(inst, nodes) + host_items:
        n = inst.node(item['node'])
        target, command = _m_hosts.wake_target(inst, n)
        if target['id'] == 'root' and item['kind'] == 'delegated-completion':
            continue  # root result notifications remain in files/inbox
        group = groups.setdefault(target['id'], {'node': target, 'items': [], 'command': command})
        group['items'].append(item)
    completions = watch_completion_items(inst, nodes)
    for item in completions:
        target = watch_completion_target(inst, item)
        group = groups.setdefault(target['id'], {'node': target, 'items': [],
                                                'command': _m_hosts.wake_target(inst, target)[1]})
        group['items'].append({**item, 'node': item.get('source', target['id'])})
    for group in groups.values():
        if group['node']['local'].get('wake_policy') == 'visible':
            watch_inbox_rollup(inst, group)
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
        # so the items that just woke it remain visible. The baseline retains older deliveries.
        seen = {i['id'] for i in _m_hosts.wake_previous_items(_m_hosts.wake_directory(inst, target))}
        out.extend(i for i in groups.get(target['id'], {}).get('items', []) if i['id'] not in seen
                   and (target['id'] == n['id'] or i['node'] == n['id']))
    return list({i['id']: i for i in out}.values())


@contextlib.contextmanager
def watch_precomputed_publication(inst, groups):
    """Fence old scans without waiting for a long scan while holding the instance lock."""
    import fcntl
    revision = getattr(groups, 'receipt_revision', None)
    generation = getattr(groups, 'source_generation', None)
    if (getattr(groups, 'stale', False) or generation is not None and
            watch_source_signature(inst, getattr(groups, 'source_layout', None)) != generation):
        yield False
        return
    if revision is None:
        # Unknown receipt storage grants no retirement authority, but cannot
        # suppress current questions. Its separately rechecked source scan is
        # still eligible for fail-open delivery with fresh transport identities.
        yield generation is not None and all(not g.get('episodes_known', True) for g in groups.values())
        return
    stack = contextlib.ExitStack()
    try:
        target = stack.enter_context(_m_common.directory_target(inst.root / '.brain/wake/due-episodes.lock'))
        stream = stack.enter_context(os.fdopen(target.open(os.O_RDWR), 'r+'))
        with _m_timings.phase('episode_lock_wait'):
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stack.enter_context(_m_timings.phase('episode_lock_hold'))
        target.check_file(stream.fileno())
        current = watch_receipt_revision(inst) == revision
    except (OSError, ValueError, SystemExit):
        stack.close()
        yield False
        return
    with stack:
        yield current


def watch_run(inst, kinds=None, quiet=False, sources=None, groups=None):
    if groups is not None:
        with watch_precomputed_publication(inst, groups) as current:
            if current:
                return watch_publish(inst, kinds, quiet, sources, groups)
        return 'deferred'  # a newer/busy scan owns reconciliation
    return watch_publish(inst, kinds, quiet, sources)


def watch_publish(inst, kinds=None, quiet=False, sources=None, groups=None):
    if groups is not None and not getattr(groups, 'complete', True) and (
            kinds is None or set(kinds) != {'overdue-question'}):
        raise ValueError('partial watcher inventory can publish only overdue questions')
    path = inst.root / '.brain' / 'watch.json'
    try:
        state = watch_state(inst)
        state_valid = True
    except (OSError, ValueError):
        state, state_valid = {}, False
    seen = set(state.get('seen', []))
    due_seen_path = inst.root / '.brain/wake/due-seen.json'
    try:
        seen.update(watch_state(inst, due_seen_path).get('seen', []))
        due_seen_valid = True
    except (OSError, ValueError):
        due_seen_valid = False
    nodes = [n for n in inst.nodes() if not n['archived']]
    failures = set(state.get('failures', []))
    warnings = set(state.get('warnings', []))
    if kinds is None:
        watch_failures(inst, nodes, failures, warnings)
    groups = watch_groups(inst, nodes) if groups is None else groups
    current_ids = {i['id'] for group in groups.values() for i in group['items']}
    current_ids.update(getattr(groups, 'retained_ids', ()))
    complete = getattr(groups, 'complete', True)
    for group in groups.values():
        if group.get('episodes_known', True):
            for pending in _m_hosts.wake_directory(inst, group['node']).glob('*.pending'):
                retained = set()
                if not complete:
                    try:
                        _, refs = _m_hosts.wake_pending_value(pending)
                        retained = {i['id'] for i in refs if i['kind'] != 'overdue-question'}
                    except (OSError, ValueError, KeyError, TypeError):
                        continue  # unknown pending work stays deliverable
                _m_hosts.wake_retire_due_pending(pending, current_ids | retained)
    pending_ids = set()
    for pending in (inst.root / '.brain/wake').glob('*/*.pending'):
        try:
            refs = json.loads(pending.read_text()).get('items', [])
            if isinstance(refs, list):
                pending_ids.update(i['id'] for i in refs if isinstance(i, dict) and isinstance(i.get('id'), str))
        except (OSError, ValueError, AttributeError):
            pass  # unidentified pending requests stay durable; they cannot own a known acknowledgement
    if complete:
        seen.intersection_update(current_ids | pending_ids)
    else:
        seen.difference_update(getattr(groups, 'retired', ()) - pending_ids)
    current = []
    count = 0
    for group in groups.values():
        items = {i['id']: i for i in group['items']}
        current.extend(items.values())
        added = sorted(key for key in set(items) - seen
                       if ((watch_decision_item(items[key]) or group['node']['local'].get('wake_policy') == 'visible')
                           if kinds is None else items[key]['kind'] in kinds)
                       and (sources is None or items[key]['node'] in sources))
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
                                                             items=[items[k] for k in bundle], explicit=kinds is not None))
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
                            or _m_hosts.request_wake(inst, origin, items[key]['reason'], items=[items[key]], explicit=kinds is not None)):
                        seen.add(key)
                        count += 1
                        queued = True
                    added.remove(key)
            # Include item identity in the opaque wake reason; equal counts aren't equal sets.
            reason = f'{len(added)} new items: ' + watch_digest(added)
            if added and _m_hosts.request_wake(inst, group['node'], reason, items=[items[k] for k in added], explicit=kinds is not None):
                seen.update(added)  # acknowledgement means durable queue, not transport success
                count += len(added)
                queued = True
        target = group['node']
        directory = _m_hosts.wake_directory(inst, target)
        from . import wake_turns as _m_wake_turns
        if not queued and group['command'] and directory.is_dir() and (
                any(directory.glob('*.pending')) or _m_wake_turns.wake_turn_stuck(inst, target)):
            _m_hosts.wake_start(inst, target)  # retries queued failures without another model tick
    # Screened current excerpts are recomputed; historical acknowledgement stores only digests.
    # Corrupt files remain byte-for-byte available for repair. Healthy stores
    # retain only current episodes and durable pending requests, never closed history.
    try:
        if kinds is None and state_valid:
            _m_machine.machine_publish(inst, path, json.dumps({'version': 1, 'at': inst.now(),
                                        'seen': sorted(seen), 'failures': sorted(failures), 'warnings': sorted(warnings), 'items': current}, sort_keys=True) + '\n')
        if due_seen_valid:
            _m_machine.machine_publish(inst, due_seen_path, json.dumps({'version': 1, 'seen': sorted(seen)}) + '\n')
    except OSError:
        pass  # enqueue has succeeded even if acknowledgement persistence failed
    if not quiet:
        if not state_valid:
            print('watch: watcher state invalid; retained for repair (brain due root)')
        elif not due_seen_valid:
            print('watch: due acknowledgement state invalid; retained for repair')
        else:
            print(f'watch: {count} new item(s) queued')
    return 'published'


def watch_once(inst, kinds=None, quiet=False, sources=None, groups=None, episode_blocking=True):
    """Return published/deferred; only foreground callers wait for episode admission."""
    import fcntl
    from . import command_reads
    folder = inst.root / '.brain'
    # The scan reuses this link audit; the publication audits again just before its lock.
    with command_reads.scope(_m_common.outward_links):
        if _m_common.outward_links(inst.root):
            _m_common.die('watch: unsafe instance links; no wake sent')
        if _m_instance._LOCK['fh'] is not None:
            if not quiet:
                print('watch: skipped; instance busy')
            return 'deferred'
        generation = _m_instance.state_signature(inst, list(inst.nodes()))
        if groups is None:
            # Each scan opens its own read scope, so an overtaken scan can rescan fresh.
            with _m_timings.phase('watch_compute'):
                groups = watch_groups(inst, episode_blocking=episode_blocking)
        if getattr(groups, 'stale', False):
            if not quiet:
                print('watch: deferred; episode scan busy or overtaken')
            return 'deferred'
    with command_reads.scope(_m_common.outward_links):
        if _m_common.outward_links(inst.root):
            _m_common.die('watch: unsafe instance links; no wake sent')
        return watch_locked_publish(inst, folder, generation, kinds, quiet, sources, groups)


def watch_locked_publish(inst, folder, generation, kinds, quiet, sources, groups):
    """Publish under the short instance lock; per-file targets are still checked on write."""
    import fcntl
    _m_common.safe_target(folder / 'lock')
    fd = os.open(folder / 'lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'r+') as stream:
        if os.fstat(stream.fileno()).st_nlink != 1:
            _m_common.die('watch: instance lock is hard-linked')
        try:
            with _m_timings.phase('lock_wait'):
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if not quiet:
                print('watch: skipped; instance busy')
            return 'deferred'
        held = os.fstat(stream.fileno())
        current = os.stat(folder / 'lock', follow_symlinks=False)
        if (not stat.S_ISREG(held.st_mode) or held.st_nlink != 1
                or (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino)):
            _m_common.die('watch: instance lock changed')
        os.fchmod(stream.fileno(), 0o600)
        stream.seek(0)
        stream.truncate()
        stream.write(str(os.getpid()))
        stream.flush()
        _m_instance._LOCK['fh'], _m_instance._LOCK['depth'] = stream, 1
        _m_instance._LOCK['hold_started'] = time.perf_counter()
        os.environ['BRAIN_LOCK_HELD'] = f'{inst.root.resolve()}|{os.getpid()}'
        try:
            if _m_instance.state_signature(inst, list(inst.nodes())) != generation:
                return 'deferred'  # changed routing/configuration cannot authorize the old scan
            with _m_timings.phase('watch_write'):
                return watch_run(inst, kinds=kinds, quiet=quiet, sources=sources, groups=groups)
        finally:
            _m_instance._LOCK['depth'] = 0
            _m_instance.unlock()


def cmd_watch(a):
    try:
        watch_once(_m_nodes.load())
    except (OSError, ValueError):
        _m_common.die('watch: invalid or unreadable local state; retained for repair')
