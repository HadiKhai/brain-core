"""Fast detached JSON observations with an explicitly dated full snapshot cache."""
import contextlib
import datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import secrets
import sys
import time

from . import common as _m_common

_m_guard = _m_common.LazyModule(__package__, 'guard')
_m_jobs = _m_common.LazyModule(__package__, 'jobs')
_m_machine = _m_common.LazyModule(__package__, 'machine')
_m_job_attempts = _m_common.LazyModule(__package__, 'job_attempts')
_m_nodes = _m_common.LazyModule(__package__, 'nodes')
_m_reports = _m_common.LazyModule(__package__, 'reports')
_m_spend = _m_common.LazyModule(__package__, 'spend')
_m_status = _m_common.LazyModule(__package__, 'status')
_m_ui_status = _m_common.LazyModule(__package__, 'ui_status')
_m_ui_readmodel = _m_common.LazyModule(__package__, 'ui_readmodel')
_m_usage = _m_common.LazyModule(__package__, 'usage')
_m_verify = _m_common.LazyModule(__package__, 'verify')
_m_worktrees = _m_common.LazyModule(__package__, 'worktrees')


UI_JSON_WAIT = 1  # static capability for cross-version rehearsal callers


def ui_json_complete(record):
    from . import ui_fill
    try:
        if type(record.get('built_at')) not in (int, float):
            return False
        datetime.datetime.fromtimestamp(record['built_at'], datetime.timezone.utc)
    except (AttributeError, ValueError, OverflowError, OSError):
        return False
    state = record.get('state') if isinstance(record, dict) else None
    return (isinstance(state, dict) and isinstance(state.get('brain'), dict)
            and isinstance(state.get('nodes'), list) and type(state.get('problems')) is int
            and not state['brain'].get('filling_in') and ui_fill.complete(state)
            and 'loading' not in state
            and (state.get('spend') is None or isinstance(state.get('spend'), dict))
            and type(record.get('built_at')) in (int, float)
            and math.isfinite(record['built_at']) and record['built_at'] >= 0)


def ui_json_last_complete(inst, code, scan):
    try:
        record = ui_json_load(inst, code, scan)
        return record if ui_json_complete(record) else None
    except (OSError, ValueError, TypeError, OverflowError, SystemExit):
        return None


def ui_json_save_complete(inst, code, scan, state, privacy):
    """Only a complete observation under its original privacy fence is reusable."""
    record = {'version': 1, 'privacy': privacy, 'built_at': time.time(), 'state': state,
              'generation': secrets.token_hex(16),
              'selection': hashlib.sha256(json.dumps(scan, sort_keys=True).encode()).hexdigest()}
    if not ui_json_complete(record) or privacy != ui_json_privacy_key(inst, code):
        return False
    return _m_ui_readmodel.ReadModel(inst, code).write('json-snapshot', record)

def ui_json_privacy_key(inst, code):
    # A stale observation may be dated; changed visibility or source permission
    # never permits replaying old display text. Persist only this opaque digest.
    privacy = _m_spend.spend_privacy_signature(inst)
    declarations = []
    for name in ('subscriptions.json', 'brains.json'):
        path = _m_usage.usage_file(name)
        try:
            declarations.append((name, path.read_bytes().hex()))
        except FileNotFoundError:
            declarations.append((name, None))
    value = [str(inst.root.resolve()), code, inst.cfg, inst.principals, inst.revoked,
             [(n['id'], n['owners'], n['sealed'], str(n['home_path']), _m_guard.never_terms(n)) for n in inst.nodes()],
             privacy, declarations]
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


@contextlib.contextmanager
def ui_json_fill_lock(inst, blocking):
    path = inst.root / '.brain/ui-cache/json-fill.lock'
    with _m_common.directory_target(path, create=True, private=True) as target:
        fd = target.open(os.O_CREAT | os.O_RDWR)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                yield False
                return
            target.check_file(fd)
            yield True
        finally:
            os.close(fd)


def ui_json_load(inst, code, scan):
    model = _m_ui_readmodel.ReadModel(inst, code)
    record = model.read('json-snapshot')
    if (not isinstance(record, dict) or record.get('version') != 1
            or record.get('privacy') != ui_json_privacy_key(inst, code)
            or record.get('selection') != hashlib.sha256(json.dumps(scan, sort_keys=True).encode()).hexdigest()
            or not isinstance(record.get('built_at'), (int, float))
            or not isinstance(record.get('state'), dict)
            or not isinstance(record['state'].get('brain'), dict)
            or not isinstance(record['state'].get('nodes'), list)):
        return None
    return record


def ui_json_source(state, record=None, refreshing=False, failed=False, origin=None, inst=None):
    now = time.time()
    built = record['built_at'] if record else None
    age = max(0, now - built) if built is not None else None
    state = {**state, 'brain': {**state['brain']}, 'review': {'enabled': False}}
    if inst is not None:
        state.update(_m_usage.usage_ui_state(inst))
    state['brain']['filling_in'] = record is None
    state['loading'] = {'filling_in': record is None, 'age_seconds': age,
                        'refreshing': refreshing, 'failed': failed, 'version': 1}
    state['source'] = {'kind': origin or ('cached-full' if record else 'initial-partial'),
                       'built_at': datetime.datetime.fromtimestamp(built, datetime.timezone.utc).isoformat() if built else None,
                       'age_seconds': age, 'observation_only': True}
    if record is None:
        state['problems'] = 'unknown'
        state['readiness'] = 'unknown'
        for node in state.get('nodes', []):
            node['readiness'] = 'unknown'
    else:
        state['readiness'] = 'recorded snapshot; use brain ready for current readiness'
    return state


def ui_json_fill_failed(inst, code):
    status = _m_ui_readmodel.ReadModel(inst, code).read('json-fill-status')
    return isinstance(status, dict) and status.get('state') == 'failed'


def ui_json_request_fill(inst, scan):
    try:
        with ui_json_fill_lock(inst, blocking=False) as acquired:
            if not acquired:
                return True
        script = ('import sys; sys.dont_write_bytecode=True; sys.path.insert(0,sys.argv[1]); '
                  'from brain.ui_json import ui_json_background; ui_json_background(sys.argv[2],sys.argv[3])')
        subprocess.Popen([sys.executable, '-B', '-c', script, str(_m_common.CORE / 'lib'),
                          str(inst.root), json.dumps(scan)], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                         start_new_session=True, env={**os.environ, 'BRAIN_ROOT': str(inst.root), 'BRAIN_NO_REEXEC': '1'})
        return True
    except (OSError, ValueError, SystemExit):
        return False


def ui_json_fingerprint(value):
    """A stable digest of metadata stamps; sets are ordered, never str()-ed."""
    def canonical(item):
        if isinstance(item, (set, frozenset)):
            return sorted((canonical(v) for v in item), key=lambda v: json.dumps(v, sort_keys=True))
        if isinstance(item, (list, tuple)):
            return [canonical(v) for v in item]
        if isinstance(item, dict):
            return {str(k): canonical(v) for k, v in item.items()}
        return item if item is None or isinstance(item, (str, int, float, bool)) else str(item)
    return hashlib.sha256(json.dumps(canonical(value), sort_keys=True).encode()).hexdigest()


def ui_json_git_dirs(path):
    """A checkout's own and common Git directories, read from files without running Git."""
    dot = path / '.git'
    if dot.is_dir():
        return dot, dot
    try:
        text = dot.read_text()
    except OSError:
        return None, None
    if not text.startswith('gitdir:'):
        return None, None
    own = Path(text[len('gitdir:'):].strip())
    own = own if own.is_absolute() else (path / own).resolve()
    try:
        common = (own / (own / 'commondir').read_text().strip()).resolve()
    except OSError:
        common = own
    return own, common


def ui_json_future(inst, value, now):
    when = inst.parse_time(value) if isinstance(value, str) else None
    return bool(when and when > now)


def ui_json_policy_stamp(inst, path, now):
    """Policy clocks change only when a future receipt observation becomes current."""
    from . import ui
    stamp = ui.UIInputs.stamp(path)
    try:
        data = json.loads(path.read_text())
        future = [ui_json_future(inst, data.get(field), now) for field in ('observed_at', 'checked_at')]
    except (OSError, ValueError, AttributeError, UnicodeError):
        future = None
    return stamp, future


def ui_json_stamps(inst, problem_keys=None):
    """Per-node metadata stamps: node files, ledger, bindings and branch tips; never home listings.

    One for-each-ref per repository gives every branch tip by SHA; each checkout's
    HEAD/index metadata covers commits, checkouts and staging inside it.
    """
    from . import ui
    stamp = ui.UIInputs.stamp
    now = inst.now_dt()
    root = inst.root
    try:
        runs = json.loads((root / '.brain/worker-runs.json').read_text()).get('runs', [])
        runs = runs if isinstance(runs, list) else []
    except (OSError, ValueError, AttributeError, UnicodeError):
        runs = None
    # Wake lifecycle receipts change by the second; reused views re-read job and round
    # usage from them (ui_json_view_usage) and lifecycle reconciliation always re-runs.
    # Inbox-seen markers churn the same way and only the uncached inbox_items reads them.
    shared = (stamp(root / 'instance.md'), stamp(root / '.brain/communications.json'),
              tuple(stamp(path) for path in sorted((root / '.brain/teaching').glob('*.json'))),
              json.dumps({k: v for k, v in inst.local.items() if k != 'nodes'}, sort_keys=True, default=str),
              os.path.realpath(root / '.brain/core-current'), runs is None)
    # Task readiness in node views shows the native machine hold: its state, never the sampler's
    # churn. Worktree readiness never reads it, so heat episodes leave worktree rows cached.
    hold = bool((_m_machine.machine_samples(inst) or {}).get('episode'))
    nodes, repos, homes = inst.nodes(), {}, {}
    for n in nodes:
        paths = [n['home_path']] if n['external'] and n['home_path'] else []
        paths += [Path(w['path']) for w in ui.ui_live_worktrees(n).values() if w.get('path')]
        homes[n['id']] = []
        for home in paths:
            homes[n['id']].append((home, *ui_json_git_dirs(home)))
        for home in [*paths, *(Path(w['path']) for w in n['local'].get('worktrees', {}).values() if w.get('path'))]:
            common = ui_json_git_dirs(home)[1]
            if common is not None:
                repos.setdefault(str(common), None)
    for common in repos:  # one tip listing per repository, shared by its worktrees
        # Never through the command's memoized Git snapshot: the post-build stamp must see moved refs.
        try:
            result = subprocess.run(['git', 'for-each-ref', '--format=%(refname)%00%(objectname)%00%(symref)',
                                     'refs/heads', 'refs/remotes', 'refs/tags'],  # commit views read tags too
                                    cwd=common, capture_output=True, text=True,
                                    env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'}, timeout=30)
        except (OSError, subprocess.SubprocessError):
            result = subprocess.CompletedProcess([], None, '', '')
        repos[common] = (result.returncode, {ref: rest for ref, *rest in (line.split('\0') for line in
                                                                         result.stdout.splitlines() if '\0' in line)})
    out, worktrees = {}, {}
    # A node's tasks may depend on any node's tasks (task_flow.blockers): every view reads them.
    tasks = []
    for n in nodes:
        for directory, dirs, names in os.walk(n['dir'] / 'tasks'):
            dirs.sort()
            tasks.extend(stamp(Path(directory) / name) for name in sorted(names))
    for n in nodes:
        # Parsed metadata, own and inherited: a stamp taken after a stale parse must not match it.
        meta, parent = [], n
        while parent:
            meta.append(json.dumps({k: parent.get(k) for k in ('id', 'fm', 'external', 'home_path', 'archived')},
                                   sort_keys=True, default=str))
            parent = parent['parent']
        files = [meta]
        for directory, dirs, names in os.walk(_m_status.hook_question_queue(n)):  # pending questions
            dirs.sort()
            files.extend(stamp(Path(directory) / name) for name in sorted(names))
        for directory, dirs, names in os.walk(n['dir']):
            if Path(directory) == n['dir']:  # a child node has its own views
                dirs[:] = [d for d in dirs if d != 'nodes' and not (n['dir'] == root and (
                    d.startswith('.') or d in ('core', 'cores')))]  # the instance's own state is `shared`
            dirs[:] = sorted(d for d in dirs if d != '.git')
            files.extend(stamp(Path(directory) / name) for name in sorted(names))
        parent = n['parent']
        while parent:
            files.extend(stamp(parent['dir'] / name) for name in ('node.md', 'never-name.txt'))
            parent = parent['parent']
        files.append(stamp(_m_spend.spend_node_file(inst, n)))
        files.append(stamp(_m_reports.preview_receipt_file(inst, n)))
        try:
            wb = _m_worktrees.workbench(inst, n)
            files.append(stamp(wb / 'reports'))
            files.extend(stamp(path) for path in sorted((wb / 'reports').glob('*/index.md')))
            for tree in ('scratch/ready', 'scratch/rehearse'):
                for directory, dirs, names in os.walk(wb / tree):
                    dirs.sort()
                    files.extend(stamp(Path(directory) / name) for name in sorted(names))
        except SystemExit:
            files.append(('workbench', None))
        patterns = _m_common.lst(n['fm'].get('verify_inputs')) or _m_common.lst(_m_verify.VERIFY_INPUTS)
        def checkout(home, refs=None):
            own, common = ui_json_git_dirs(home)
            code, tips = repos.get(str(common), (None, {}))
            if refs is not None:
                # The integration branch is origin/HEAD's target, or its local counterpart (merge=local).
                target = (tips.get('refs/remotes/origin/HEAD') or ['', ''])[-1]
                if target:
                    refs = [*refs, target, target.replace('refs/remotes/origin/', 'refs/heads/', 1)]
                tips = {ref: tips.get(ref) for ref in refs}
            return (str(home), stamp(home), str(common), code, sorted(tips.items()),
                    *(stamp(own / name) for name in (('HEAD', 'index', 'packed-refs', 'config') if own else ())),
                    *(stamp(path) for pattern in patterns for path in sorted(home.glob(pattern))))
        git = [checkout(home) for home, _, _ in homes[n['id']]]
        if problem_keys is not None:
            # Integrity has no cross-node task blockers or worker/machine meters.
            problem_keys[n['id']] = ui_json_fingerprint([
                stamp(root / 'instance.md'), os.path.realpath(root / '.brain/core-current'), meta, files, git,
                json.dumps(n['local'], sort_keys=True, default=str)])
        mine = [r for r in runs or [] if isinstance(r, dict) and r.get('node') == n['id']]
        out[n['id']] = ui_json_fingerprint([shared, json.dumps(n['local'], sort_keys=True, default=str),
                                            files, git, mine, tasks, hold])
        # A worktree row and its readiness depend on its own checkout, its job and
        # integration refs and the node's policy files, not on status or inbox edits.
        policy = [meta, *(stamp(n['dir'] / name) for name in ('node.md', 'never-name.txt'))]
        parent = n['parent']
        while parent:
            policy.extend(stamp(parent['dir'] / name) for name in ('node.md', 'never-name.txt'))
            parent = parent['parent']
        for directory, dirs, names in os.walk(n['dir'] / 'job-trains'):
            dirs.sort()
            policy.extend(stamp(Path(directory) / name) for name in sorted(names))
        ready = [row for row in files if any(f'/{tree}/' in row[0] for tree in ('scratch/ready', 'scratch/rehearse'))]
        jobs = _m_jobs.read_jobs(n) or []
        namespace = hashlib.sha256(n['id'].encode()).hexdigest()
        # A post-build stamp must see moved thread bindings even inside command-read memoization.
        active = getattr(_m_status.active_work, '__wrapped__', _m_status.active_work)(n, include_closed=True)
        node_home = n['home_path'] if n['external'] and n['home_path'] else None
        for branch, w in n['local'].get('worktrees', {}).items():
            if not w.get('path'):
                continue
            refs = ['refs/heads/' + branch, 'refs/heads/main', 'refs/heads/master', 'refs/remotes/origin/HEAD',
                    'refs/remotes/origin/main', 'refs/remotes/origin/master']
            handbacks = [(stamp(_m_jobs.handback_file(n, row)), stamp(_m_job_attempts.attempt_path(n, row)),
                          stamp(_m_jobs.contract_file(n, row)))
                         for row in jobs if row.get('Job') == branch]  # replacement attempts move readiness
            receipts = []
            for row in jobs:
                if row.get('Job') != branch:
                    continue
                job_key = _m_jobs.job_key(row)
                receipts.append(stamp(root / '.brain/review-findings' / namespace / (job_key + '.json')))
                for tree in ('review-receipts', 'preview-receipts'):
                    folder = root / '.brain' / tree / namespace / job_key
                    receipts.append(stamp(folder))
                    receipts.extend(ui_json_policy_stamp(inst, path, now) for path in sorted(folder.glob('*.json')))
            worktrees[(n['id'], branch)] = ui_json_fingerprint([
                shared, policy, ready, handbacks, receipts, _m_jobs.bound_worker_threads(n, branch),
                [row for row in jobs if row.get('Job') == branch],
                ui_json_future(inst, w.get('created'), now),
                [row for row in active if row and row[0].replace('`', '').split()[:1] == [branch]],
                json.dumps({k: n['local'].get(k) for k in ('home', 'workbench', 'preview', 'review_urls')},
                           sort_keys=True, default=str),
                json.dumps(w, sort_keys=True, default=str), checkout(Path(w['path']), refs),
                checkout(node_home, refs) if node_home else None])
    return out, worktrees


def ui_json_job(job):
    return json.dumps(list(job))


def ui_json_views(inst, model, privacy, keys, worktree_keys, cache):
    """Load persisted node views, worktree rows and readiness whose stamp still matches."""
    from . import verify
    cache.worktree_keys = worktree_keys
    cache.policy_stamped = True
    record = model.read('node-views')
    if not isinstance(record, dict) or record.get('binding') != model.binding(privacy):
        return {}, set()
    hits = set()
    for nid, entry in (record.get('nodes') or {}).items():
        if (isinstance(entry, dict) and nid in keys and entry.get('key') == keys[nid]
                and isinstance(entry.get('row'), dict) and isinstance(entry.get('commits'), list)):
            cache.nodes[nid] = (keys[nid], entry['row'], entry['commits'])
            hits.add(nid)
    jobs = {ui_json_job(job): job for job in worktree_keys}
    # Dashboard worktree rows and readiness are served under their stamp with their own read time
    # shown; unstaged edits are outside the stamp, so rows are revalidated in the background, never
    # inside the build (D-20261028-ui-bounded-staleness). Acting commands never read this cache.
    fresh = lambda entry: type(entry.get('at')) in (int, float) and entry['at'] <= time.time()  # noqa: E731
    for name, entry in (record.get('worktrees') or {}).items():
        job = jobs.get(name)
        if (job and isinstance(entry, dict) and entry.get('key') == worktree_keys[job]
                and isinstance(entry.get('row'), dict) and fresh(entry)):
            cache.worktrees[job] = (worktree_keys[job], entry['row'])
            cache.checked[job] = entry['at']
    for name, entry in (record.get('ready') or {}).items():
        job = jobs.get(name)
        if (job and isinstance(entry, dict) and entry.get('key') == worktree_keys[job] and fresh(entry)
                and job in cache.checked and entry['at'] == cache.checked[job]
                and isinstance(entry.get('data'), dict) and isinstance(entry.get('policy'), list)):
            # The stamp includes receipt/policy inputs; the row's age bounds unstamped edits.
            cache.ready[job] = (worktree_keys[job], verify.ReadyStateResult(
                entry['data'], entry['policy'], entry.get('head') or ''))
    return record, hits


def ui_json_plain(value):
    try:
        text = json.dumps(value, sort_keys=True)
        return json.loads(text) == value
    except (TypeError, ValueError):
        return False


UI_JSON_WORKTREE_SECONDS = 60  # the dashboard's staleness bound for worktree rows
UI_JSON_REVALIDATE_SECONDS = 30  # background re-reads begin well before rows reach it


# Due items and problems also compare against the clock (overdue thresholds, stale
# status): their stamp hit is reusable for at most this long.
UI_JSON_CLOCKED_SECONDS = 300


def ui_json_clocked(entry, key):
    return (isinstance(entry, dict) and entry.get('key') == key and type(entry.get('at')) in (int, float)
            and 0 <= time.time() - entry['at'] < UI_JSON_CLOCKED_SECONDS)


def ui_json_problems(inst, keys, entries):
    """Keep only hit node counts; shared integrity rules and acting checks stay live."""
    from . import check
    counts, oldest = {}, time.time()
    with _m_guard.term_scan_cache():
        total = len(check.collect_global_problems(inst)[0])
        for n in inst.nodes():
            nid, key = n['id'], keys[n['id']]
            entry = entries.get(nid)
            if not (ui_json_clocked(entry, key) and type(entry.get('count')) is int and entry['count'] >= 0):
                at = time.time()
                entry = {'key': key, 'at': at, 'count': len(check.collect_node_problems(inst, n)[0])}
            counts[nid] = entry
            total += entry['count']
            oldest = min(oldest, entry['at'])
    return total, counts, oldest


def ui_json_view_usage(inst, state, hits, views):
    """Refresh what a reused view shows outside its stamp: job usage (transcripts), round usage,
    lifecycle-filtered inbox, clock-derived fields and its bounded-age worktree rows."""
    from . import inbox_notices, ui, verify
    now = inst.now_dt()
    nodes = {n['id']: n for n in inst.nodes()}
    with verify.display_cache(views.ready, views.worktree_keys, views.policy_stamped), \
            _m_status.local_job_snapshot(views.local_jobs, views.worktree_keys):
        for index, row in enumerate(state['nodes']):
            n = nodes.get(row['id'])
            if row['id'] not in hits or n is None:
                continue
            terms = _m_guard.never_terms(n)
            hide = lambda t: _m_guard.redact(t, terms) if terms and isinstance(t, str) else t  # noqa: E731
            ledger = [r for r in (_m_jobs.read_jobs(n) or []) if not r.get('Ended')]
            opened = []
            for shown in row['jobs']['open']:
                match = [r for r in ledger if all(hide(r.get(k, '')) == shown.get(k, '') for k in _m_jobs.JOB_COLS)]
                if len(match) != 1:  # the ledger moved mid-pass: show the row as its view read it
                    opened.append(shown)
                    continue
                used = _m_spend.live_usage(inst, n, match[0])
                opened.append({**shown, 'usage': _m_spend.spend_summary(used, terms) if used and used['sessions'] else None,
                               'round_usage': ui.ui_round_usage(inst, n, match[0], used)})
            # Clock-derived fields are recomputed: daily buckets and blocked escalation move with time.
            jobs = {**row['jobs'], 'open': opened, 'throughput': _m_jobs.daily_throughput(
                inst, n, [r for r in (_m_jobs.read_jobs(n) or []) if r.get('Outcome') != 'external'])}
            blocked = [_m_ui_status.ui_blocked_metadata(inst, n, t) for t in _m_status.status_section(n, 'blocked')]
            inbox = [line[2:] for line in _m_status.inbox_lines(n)
                     if not inbox_notices.informational_notice(line)]  # answered questions leave by lifecycle
            worktrees = (ui.ui_worktree_rows(inst, n, views, hide)
                         if n['external'] and n['home_path'] and n['home_path'].exists() else row['worktrees'])
            state['nodes'][index] = ui.ui_row_meters(inst, {**row, 'jobs': jobs, 'blocked_metadata': blocked,
                                                            'inbox': inbox, 'worktrees': worktrees}, now)


def ui_json_revalidate_lock(inst):
    """One background worktree re-read at a time; a busy lock means one is already running."""
    @contextlib.contextmanager
    def held():
        folder = inst.root / '.brain/ui-cache'
        folder.mkdir(parents=True, exist_ok=True)
        fd = os.open(folder / 'revalidate.lock', os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            yield True
        finally:
            os.close(fd)
    return held()


@contextlib.contextmanager
def ui_json_publish_lock(inst):
    """A short lock around each writer's read-merge-write of the persisted views."""
    folder = inst.root / '.brain/ui-cache'
    folder.mkdir(parents=True, exist_ok=True)
    fd = os.open(folder / 'publish.lock', os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def ui_json_keep_newer(record, stored):
    """Keep a worktree reading another writer published later; a row and its readiness move together."""
    if not isinstance(stored, dict) or stored.get('binding') != record.get('binding'):
        return record
    rows, ready = record.setdefault('worktrees', {}), record.setdefault('ready', {})
    for name, entry in (stored.get('worktrees') or {}).items():
        mine = rows.get(name)
        if (isinstance(entry, dict) and type(entry.get('at')) in (int, float)
                and (mine is None or entry['at'] > mine['at'])):
            rows[name] = entry
            pair = (stored.get('ready') or {}).get(name)
            if isinstance(pair, dict) and pair.get('at') == entry['at']:
                ready[name] = pair
            else:
                ready.pop(name, None)
    return record


def ui_json_request_revalidate(inst):
    try:
        script = ('import sys; sys.dont_write_bytecode=True; sys.path.insert(0,sys.argv[1]); '
                  'from brain.ui_json import ui_json_revalidate; ui_json_revalidate(sys.argv[2])')
        subprocess.Popen([sys.executable, '-B', '-c', script, str(_m_common.CORE / 'lib'), str(inst.root)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True, start_new_session=True,
                         env={**os.environ, 'BRAIN_ROOT': str(inst.root), 'BRAIN_NO_REEXEC': '1'})
        return True
    except (OSError, ValueError):
        return False


def ui_json_revalidate(root):
    """Background re-read of the dashboard's live worktree rows and readiness, outside any build."""
    from . import ui, ui_server, verify
    os.environ.update(BRAIN_ROOT=root, BRAIN_NO_REEXEC='1', GIT_OPTIONAL_LOCKS='0')
    inst = ui.UIInstance(Path(root))
    code = ui_server.ui_code_version()
    try:
        with ui_json_revalidate_lock(inst) as acquired:
            if not acquired:
                return
            privacy = ui_json_privacy_key(inst, code)
            _, keys = ui_json_stamps(inst)
            at, rows, ready = {}, {}, {}
            with _m_common.git_read_snapshot(), _m_status.local_job_snapshot():
                for n in inst.nodes():
                    live = ui.ui_live_worktrees(n)
                    for branch, w in n['local'].get('worktrees', {}).items():
                        job = (n['id'], branch)
                        if branch in live and job in keys:
                            at[job] = time.time()
                            rows[job] = {**ui.ui_worktree(inst, n['home_path'], branch, w, n), 'live': True}
                            ready[job] = verify.ready_state(inst, n, branch, display=True)
            current = ui.UIInstance(Path(root))
            _, after = ui_json_stamps(current)
            if privacy != ui_json_privacy_key(current, code):
                return
            model = _m_ui_readmodel.ReadModel(current, code)
            mine = {'binding': model.binding(privacy), 'worktrees': {}, 'ready': {}}
            for job, row in rows.items():
                result = ready[job]
                if (after.get(job) == keys[job] and ui_json_plain(row) and isinstance(result, verify.ReadyStateResult)
                        and not (result.get('packet') or {}).get('full_disposition') and ui_json_plain(dict(result))):
                    name = ui_json_job(job)
                    mine['worktrees'][name] = {'key': keys[job], 'at': at[job], 'row': row}
                    mine['ready'][name] = {'key': keys[job], 'at': at[job], 'data': dict(result),
                                           'policy': list(result.policy_reasons), 'head': result.policy_head}
            with ui_json_publish_lock(current):
                record = model.read('node-views')
                if not isinstance(record, dict) or record.get('binding') != mine['binding']:
                    return
                rows_now, ready_now = record.setdefault('worktrees', {}), record.setdefault('ready', {})
                for name, entry in mine['worktrees'].items():  # only readings newer than what is stored
                    if not isinstance(rows_now.get(name), dict) or entry['at'] > rows_now[name].get('at', 0):
                        rows_now[name], ready_now[name] = entry, mine['ready'][name]
                model.write('node-views', record)
    except (Exception, SystemExit):
        return  # an optional re-read; the next build requests another


def ui_json_build(inst, scan, code, cache=True):
    """One pass like main: delivered as observed; only views whose stamp held are cached."""
    from . import check, status, ui, verify
    before = ui_json_privacy_key(inst, code)
    model = _m_ui_readmodel.ReadModel(inst, code)
    started = time.time()  # the snapshot is dated by when its oldest reading began
    problem_keys = {}
    keys, worktree_keys = ui_json_stamps(inst, problem_keys)
    views = ui.UIStateCache()
    views.worktree_keys = worktree_keys
    record, hits = ui_json_views(inst, model, before, keys, worktree_keys, views) if cache else ({}, set())
    problems_entries = record.get('problems') if isinstance(record.get('problems'), dict) else {}
    # Due items also depend on wake lifecycle receipts (answered questions, acknowledgements).
    lifecycle = ui.UIInputs.stamp(inst.root / '.brain/wake/lifecycle.json')
    due_keys = {nid: ui_json_fingerprint([key, lifecycle]) for nid, key in keys.items()}
    due_entries = record.get('due') if isinstance(record.get('due'), dict) else {}
    due_memo = {nid: entry['items'] for nid, entry in due_entries.items()
                if nid in keys and ui_json_clocked(entry, due_keys[nid]) and isinstance(entry.get('items'), dict)}
    due_seen = {}
    # The full guard/check and recorded readiness observations stay unchanged.
    # No exclusive instance lock surrounds this computation or cache publication.
    with _m_ui_readmodel.cache_writes(cache), status.due_memo(due_memo, due_seen):
        problems, problem_counts, problems_at = ui_json_problems(inst, problem_keys, problems_entries)
        state = ui.ui_state(inst, problems, scan, spend_budget=None, cache=views, node_keys=keys)
        state['problems_checked_at'] = int(problems_at)
        state['problems_checked_age_seconds'] = max(0, int(time.time() - problems_at))
        if hits:
            ui_json_view_usage(inst, state, hits, views)
    current = ui.UIInstance(inst.root)
    if before != ui_json_privacy_key(current, code):
        return None  # do not publish text observed under an obsolete visibility policy
    if cache:
        # A view enters the cache only if its stamp held across the pass; delivery never waits on it.
        problems_after = {}
        after, worktrees_after = ui_json_stamps(current, problems_after)
        held = {nid for nid, key in keys.items() if after.get(nid) == key}
        jobs_held = {job for job, key in worktree_keys.items()
                     if worktrees_after.get(job) == key and job in views.checked}
        views_record = {
            'binding': model.binding(before),
            'nodes': {nid: {'key': key, 'row': row, 'commits': commits}
                      for nid, (key, row, commits) in views.nodes.items() if nid in held and key == keys[nid]},
            'worktrees': {ui_json_job(job): {'key': key, 'at': views.checked[job], 'row': row}
                          for job, (key, row) in views.worktrees.items()
                          if job in jobs_held and key == worktree_keys[job] and ui_json_plain(row)},
            'ready': {ui_json_job(job): {'key': key, 'at': views.checked[job], 'data': dict(result),
                                         'policy': list(getattr(result, 'policy_reasons', ())),
                                         'head': getattr(result, 'policy_head', '')}
                      for job, (key, result) in views.ready.items()
                      if job in jobs_held and key == worktree_keys[job] and job in views.worktrees
                      and isinstance(result, verify.ReadyStateResult)
                      and not (result.get('packet') or {}).get('full_disposition') and ui_json_plain(dict(result))},
            'due': {nid: {'key': due_keys[nid], 'at': due_entries[nid]['at'] if nid in due_memo else started,
                          'items': {**due_memo.get(nid, {}), **due_seen.get(nid, {})}}
                    for nid in set(due_memo) | set(due_seen) if nid in held},
            'problems': {nid: entry for nid, entry in problem_counts.items()
                         if problems_after.get(nid) == problem_keys[nid]}}
        try:
            with ui_json_publish_lock(current):  # a background revalidation may have published newer rows
                model.write('node-views', ui_json_keep_newer(views_record, model.read('node-views')))
        except OSError:
            pass  # an optional cache; the observation is still delivered
        if any(time.time() - at >= UI_JSON_REVALIDATE_SECONDS for at in views.checked.values()):
            ui_json_request_revalidate(current)
    for row in state['nodes']:  # each live worktree row carries its own read time and age
        for w in row.get('worktrees', []):
            at = views.checked.get((row['id'], w.get('branch')))
            if w.get('live', True) and at is not None:
                w['checked_at'] = int(at)
                w['checked_age_seconds'] = max(0, int(time.time() - at))
    state['brain'].pop('filling_in', None)
    state.pop('loading', None)  # snapshot metadata only; progress belongs to /api/loading
    record = {'version': 1, 'privacy': before, 'built_at': started, 'state': state,
              'generation': secrets.token_hex(16),
              'selection': hashlib.sha256(json.dumps(scan, sort_keys=True).encode()).hexdigest()}
    if cache:
        if model.write('json-snapshot', record):
            model.write('json-fill-status', {'version': 1, 'state': 'complete', 'at': time.time()})
    return record  # --wait can deliver a fresh observation even if the optional cache is unwritable


def ui_json_read(inst, scan, wait=False, cache=True):
    from . import ui, ui_server
    code = ui_server.ui_code_version()
    if not cache:
        record = ui_json_build(inst, scan, code, cache=False)
        if record is None:
            _m_common.die('UI visibility changed during fill; retry')
        return ui_json_source(record['state'], record, origin='fresh-full', inst=inst)
    if wait:
        stack, cache = contextlib.ExitStack(), True
        previous = _m_ui_readmodel.ReadModel(inst, code).read('json-snapshot')
        generation = previous.get('generation') if isinstance(previous, dict) else None
        try:
            try:
                acquired = stack.enter_context(ui_json_fill_lock(inst, blocking=False))
            except (OSError, ValueError, SystemExit):
                cache = False  # refused optional storage never prevents a source read
            if cache and acquired:
                latest = ui_json_last_complete(ui.UIInstance(inst.root), code, scan)
                if latest is not None and latest.get('generation') and latest['generation'] != generation:
                    return ui_json_source(latest['state'], latest, origin='fresh-full', inst=inst)
            if cache and not acquired:
                stack.close()
                # Join the owner once. Never queue a redundant full rebuild.
                stack.enter_context(ui_json_fill_lock(inst, blocking=True))
                joined = ui_json_last_complete(ui.UIInstance(inst.root), code, scan)
                if joined is None or not joined.get('generation') or joined['generation'] == generation:
                    _m_common.die('UI fill did not publish a validated complete snapshot; retry --wait')
                return ui_json_source(joined['state'], joined, origin='fresh-full', inst=inst)
            # Build failures remain failures; only optional lock/storage errors fall back.
            record = ui_json_build(inst, scan, code, cache=cache)
        finally:
            try:
                stack.close()
            except (OSError, ValueError, SystemExit):
                pass
        if record is not None:
            return ui_json_source(record['state'], record, origin='fresh-full', inst=inst)
        _m_common.die('UI visibility changed during fill; retry --wait')
    else:
        try:
            record = ui_json_load(inst, code, scan)
        except (OSError, ValueError, SystemExit):
            record = None
        if record is not None:
            stale = time.time() - record['built_at'] >= 60
            refreshing = ui_json_request_fill(inst, scan) if stale else False
            return ui_json_source(record['state'], record, refreshing, ui_json_fill_failed(inst, code), inst=inst)
    partial = ui.ui_initial_state(inst, scan)
    terms = list({term for n in inst.nodes() for term in _m_guard.never_terms(n)})
    def screen(value):
        if isinstance(value, str):
            return _m_nodes.import_text(value, terms, metadata=True)
        if isinstance(value, list):
            return [screen(v) for v in value]
        if isinstance(value, dict):
            return {k: screen(v) for k, v in value.items()}
        return value
    refreshing = ui_json_request_fill(inst, scan)
    result = ui_json_source(screen(partial), refreshing=refreshing, inst=inst)
    result['loading']['failed'] = not refreshing or ui_json_fill_failed(inst, code)
    return result


def ui_json_background(root, raw_scan):
    from . import command_reads, timings, ui, ui_server, usage_cache
    os.environ.update(BRAIN_ROOT=root, BRAIN_NO_REEXEC='1', GIT_OPTIONAL_LOCKS='0')
    sys.argv = ['brain', 'ui']
    inst = ui.UIInstance(Path(root))
    model = _m_ui_readmodel.ReadModel(inst, ui_server.ui_code_version())
    def status(state):
        model.write('json-fill-status', {'version': 1, 'state': state, 'at': time.time()})
    try:
        with ui_json_fill_lock(inst, blocking=False) as acquired:
            if not acquired:
                return
            status('working')
            with timings.command(), usage_cache.batch(), command_reads.snapshot():
                timings.select_command('ui')
                record = ui_json_build(inst, json.loads(raw_scan), ui_server.ui_code_version())
            status('complete' if record is not None else 'failed')
    except (Exception, SystemExit):
        # Preserve prior observations and expose the failed fill, never a clean
        # result. A later read may retry without discarding source evidence.
        status('failed')
        return
