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


def ui_json_stamps(inst):
    """Per-node metadata stamps: node files, ledger, bindings and branch tips; never home listings.

    One for-each-ref per repository gives every branch tip by SHA; each checkout's
    HEAD/index metadata covers commits, checkouts and staging inside it.
    """
    from . import ui
    stamp = ui.UIInputs.stamp
    root = inst.root
    try:
        runs = json.loads((root / '.brain/worker-runs.json').read_text()).get('runs', [])
        runs = runs if isinstance(runs, list) else []
    except (OSError, ValueError, AttributeError, UnicodeError):
        runs = None
    # Wake lifecycle receipts change by the second; reused views re-read job and round
    # usage from them (ui_json_view_usage) and lifecycle reconciliation always re-runs.
    shared = (stamp(root / 'instance.md'), stamp(root / '.brain/communications.json'),
              stamp(root / '.brain/inbox-seen.json'),
              tuple(stamp(path) for path in sorted((root / '.brain/teaching').glob('*.json'))),
              json.dumps({k: v for k, v in inst.local.items() if k != 'nodes'}, sort_keys=True, default=str),
              os.path.realpath(root / '.brain/core-current'), runs is None)
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
            result = subprocess.run(['git', 'for-each-ref', '--format=%(refname)%00%(objectname)', 'refs/heads',
                                     'refs/remotes'], cwd=common, capture_output=True, text=True,
                                    env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'}, timeout=30)
        except (OSError, subprocess.SubprocessError):
            result = subprocess.CompletedProcess([], None, '', '')
        repos[common] = (result.returncode, dict(line.split('\0', 1) for line in result.stdout.splitlines()
                                                 if '\0' in line))
    out, worktrees = {}, {}
    # A node's tasks may depend on any node's tasks (task_flow.blockers): every view reads them.
    tasks = []
    for n in nodes:
        for directory, dirs, names in os.walk(n['dir'] / 'tasks'):
            dirs.sort()
            tasks.extend(stamp(Path(directory) / name) for name in sorted(names))
    for n in nodes:
        files = []
        for directory, dirs, names in os.walk(root / '.brain/host-questions' / n['id']):  # pending questions
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
            tips = tips if refs is None else {ref: tips.get(ref) for ref in refs}
            return (str(home), stamp(home), str(common), code, sorted(tips.items()),
                    *(stamp(own / name) for name in (('HEAD', 'index', 'packed-refs', 'config') if own else ())),
                    *(stamp(path) for pattern in patterns for path in sorted(home.glob(pattern))))
        git = [checkout(home) for home, _, _ in homes[n['id']]]
        mine = [r for r in runs or [] if isinstance(r, dict) and r.get('node') == n['id']]
        out[n['id']] = ui_json_fingerprint([shared, json.dumps(n['local'], sort_keys=True, default=str),
                                            files, git, mine, tasks])
        # A worktree row and its readiness depend on its own checkout, its job and
        # integration refs and the node's policy files, not on status or inbox edits.
        policy = [stamp(n['dir'] / name) for name in ('node.md', 'never-name.txt', _m_jobs.JOBS_FILE)]
        parent = n['parent']
        while parent:
            policy.extend(stamp(parent['dir'] / name) for name in ('node.md', 'never-name.txt'))
            parent = parent['parent']
        for directory, dirs, names in os.walk(n['dir'] / 'job-trains'):
            dirs.sort()
            policy.extend(stamp(Path(directory) / name) for name in sorted(names))
        ready = [row for row in files if any(f'/{tree}/' in row[0] for tree in ('scratch/ready', 'scratch/rehearse'))]
        jobs = _m_jobs.read_jobs(n) or []
        node_home = n['home_path'] if n['external'] and n['home_path'] else None
        for branch, w in n['local'].get('worktrees', {}).items():
            if not w.get('path'):
                continue
            refs = ['refs/heads/' + branch, 'refs/heads/main', 'refs/heads/master', 'refs/remotes/origin/HEAD',
                    'refs/remotes/origin/main', 'refs/remotes/origin/master']
            handbacks = [stamp(_m_jobs.handback_file(n, row)) for row in jobs if row.get('Job') == branch]
            worktrees[(n['id'], branch)] = ui_json_fingerprint([
                shared, policy, ready, handbacks,
                json.dumps({k: n['local'].get(k) for k in ('home', 'workbench')}, sort_keys=True, default=str),
                json.dumps(w, sort_keys=True, default=str), checkout(Path(w['path']), refs),
                checkout(node_home, refs) if node_home else None])
    return out, worktrees


def ui_json_job(job):
    return json.dumps(list(job))


def ui_json_views(inst, model, privacy, keys, worktree_keys, cache):
    """Load persisted node views, worktree rows and readiness whose stamp still matches."""
    from . import verify
    cache.worktree_keys = worktree_keys
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
    for name, entry in (record.get('worktrees') or {}).items():
        job = jobs.get(name)
        if job and isinstance(entry, dict) and entry.get('key') == worktree_keys[job] and isinstance(entry.get('row'), dict):
            cache.worktrees[job] = (worktree_keys[job], entry['row'])
    for name, entry in (record.get('ready') or {}).items():
        job = jobs.get(name)
        if (job and isinstance(entry, dict) and entry.get('key') == worktree_keys[job]
                and isinstance(entry.get('data'), dict) and isinstance(entry.get('policy'), list)):
            # Policy validators re-run on every hit (verify.ready_state); Git/input facts are reused.
            cache.ready[job] = (worktree_keys[job], verify.ReadyStateResult(
                entry['data'], entry['policy'], entry.get('head') or ''))
    return record, hits


def ui_json_plain(value):
    try:
        text = json.dumps(value, sort_keys=True)
        return json.loads(text) == value
    except (TypeError, ValueError):
        return False


# Due items and problems also compare against the clock (overdue thresholds, stale
# status): their stamp hit is reusable for at most this long.
UI_JSON_CLOCKED_SECONDS = 300


def ui_json_clocked(entry, key):
    return (isinstance(entry, dict) and entry.get('key') == key and type(entry.get('at')) in (int, float)
            and 0 <= time.time() - entry['at'] < UI_JSON_CLOCKED_SECONDS)


def ui_json_view_usage(inst, state, hits):
    """Job usage reads transcripts, outside node fingerprints: re-read it for reused views."""
    from . import ui
    now = inst.now_dt()
    nodes = {n['id']: n for n in inst.nodes()}
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
            if len(match) != 1:
                return False  # the ledger moved under its fingerprint: rebuild, never guess
            used = _m_spend.live_usage(inst, n, match[0])
            opened.append({**shown, 'usage': _m_spend.spend_summary(used, terms) if used and used['sessions'] else None,
                           'round_usage': ui.ui_round_usage(inst, n, match[0], used)})
        # Clock-derived fields are recomputed: daily buckets and blocked escalation move with time.
        jobs = {**row['jobs'], 'open': opened, 'throughput': _m_jobs.daily_throughput(
            inst, n, [r for r in (_m_jobs.read_jobs(n) or []) if r.get('Outcome') != 'external'])}
        blocked = [_m_ui_status.ui_blocked_metadata(inst, n, t) for t in _m_status.status_section(n, 'blocked')]
        state['nodes'][index] = ui.ui_row_meters(inst, {**row, 'jobs': jobs, 'blocked_metadata': blocked}, now)
    return True


def ui_json_build(inst, scan, code, cache=True):
    from . import check, status, ui, verify
    before = ui_json_privacy_key(inst, code)
    model = _m_ui_readmodel.ReadModel(inst, code)
    started = time.time()  # the snapshot is dated by when its oldest reading began
    # Node views persist under their own node stamp; a changed stamp rebuilds that view only.
    keys, worktree_keys = ui_json_stamps(inst)
    views = ui.UIStateCache()
    views.worktree_keys = worktree_keys
    record, hits = ui_json_views(inst, model, before, keys, worktree_keys, views) if cache else ({}, set())
    problems_entry = record.get('problems')
    due_entries = record.get('due') if isinstance(record.get('due'), dict) else {}
    due_memo = {nid: entry['items'] for nid, entry in due_entries.items()
                if nid in keys and ui_json_clocked(entry, keys[nid]) and isinstance(entry.get('items'), dict)}
    due_at = {nid: due_entries[nid]['at'] for nid in due_memo}
    problems = problems_key = None
    # The full guard/check and recorded readiness observations stay unchanged.
    # No exclusive instance lock surrounds this computation or cache publication.
    for attempt in range(2):
        # Integrity reads every node: its count reuses only an all-nodes stamp match.
        everything = ui_json_fingerprint(sorted(keys.items()))
        due_seen = {}
        with _m_ui_readmodel.cache_writes(cache), status.due_memo(due_memo, due_seen):
            if ui_json_clocked(problems_entry, everything) and type(problems_entry.get('count')) is int:
                problems, problems_at, problems_key = problems_entry['count'], problems_entry['at'], everything
            elif problems is None:  # a second pass reuses this build's count but never publishes it
                problems, problems_at, problems_key = len(check.collect_problems(inst)[0]), time.time(), everything
            state = ui.ui_state(inst, problems, scan, spend_budget=None, cache=views, node_keys=keys)
            if hits and not ui_json_view_usage(inst, state, hits):
                views.nodes.clear()
                state = ui.ui_state(inst, problems, scan, spend_budget=None, cache=views, node_keys=keys)
        current = ui.UIInstance(inst.root)
        if before != ui_json_privacy_key(current, code):
            return None  # do not publish text observed under an obsolete visibility policy
        after, worktrees_after = ui_json_stamps(current)
        held = {nid for nid, key in keys.items() if after.get(nid) == key}
        jobs_held = {job for job, key in worktree_keys.items() if worktrees_after.get(job) == key}
        for nid in held:
            if nid in due_seen:
                due_memo[nid] = {**due_memo.get(nid, {}), **due_seen[nid]}
                due_at.setdefault(nid, time.time())
        if held == set(keys) and jobs_held == set(worktree_keys):
            break
        # Something moved while observed: rebuild only the moved views under their new stamps,
        # once. Held views, worktree rows and readiness are reused from this build.
        moved = set(keys) - held
        due_memo = {nid: items for nid, items in due_memo.items() if nid in held}
        due_at = {nid: at for nid, at in due_at.items() if nid in held}
        keys, worktree_keys = {**keys, **{nid: after[nid] for nid in after}}, worktrees_after
        views.worktree_keys = worktree_keys
        hits = {nid for nid in hits if nid in held}
        for nid in moved:
            views.nodes.pop(nid, None)
    if cache:
        # Publish only what was observed under a stamp that still held after its pass.
        due = {nid: {'key': keys[nid], 'at': due_at.get(nid, time.time()), 'items': due_memo[nid]}
               for nid in due_memo if nid in held}
        model.write('node-views', {
            'binding': model.binding(before),
            'nodes': {nid: {'key': key, 'row': row, 'commits': commits}
                      for nid, (key, row, commits) in views.nodes.items() if nid in held and key == keys[nid]},
            'worktrees': {ui_json_job(job): {'key': key, 'row': row} for job, (key, row) in views.worktrees.items()
                          if job in jobs_held and key == worktree_keys[job] and ui_json_plain(row)},
            'ready': {ui_json_job(job): {'key': key, 'data': dict(result),
                                         'policy': list(getattr(result, 'policy_reasons', ())),
                                         'head': getattr(result, 'policy_head', '')}
                      for job, (key, result) in views.ready.items()
                      if job in jobs_held and key == worktree_keys[job] and isinstance(result, verify.ReadyStateResult)
                      and not (result.get('packet') or {}).get('full_disposition') and ui_json_plain(dict(result))},
            'due': due,
            'problems': ({'key': everything, 'at': problems_at, 'count': problems}
                         if held == set(keys) and problems_key == everything else None)})
    state['brain'].pop('filling_in', None)
    state.pop('loading', None)  # snapshot metadata only; progress belongs to /api/loading
    record = {'version': 1, 'privacy': before, 'built_at': started, 'state': state,
              'generation': secrets.token_hex(16),
              'selection': hashlib.sha256(json.dumps(scan, sort_keys=True).encode()).hexdigest()}
    if cache:
        model = _m_ui_readmodel.ReadModel(inst, code)
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
