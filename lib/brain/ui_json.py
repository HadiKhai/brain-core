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
    shared = (stamp(root / 'instance.md'), stamp(root / '.brain/wake/lifecycle.json'),
              json.dumps({k: v for k, v in inst.local.items() if k != 'nodes'}, sort_keys=True, default=str),
              os.path.realpath(root / '.brain/core-current'), runs is None)
    nodes, repos, homes = inst.nodes(), {}, {}
    for n in nodes:
        paths = [n['home_path']] if n['external'] and n['home_path'] else []
        paths += [Path(w['path']) for w in ui.ui_live_worktrees(n).values() if w.get('path')]
        homes[n['id']] = []
        for home in paths:
            own, common = ui_json_git_dirs(home)
            homes[n['id']].append((home, own, common))
            if common is not None:
                repos.setdefault(str(common), None)
    for common in repos:  # one tip listing per repository, shared by its worktrees
        result = _m_common.git('for-each-ref', '--format=%(refname)%00%(objectname)', 'refs/heads', 'refs/remotes',
                               cwd=common)
        repos[common] = (result.returncode, result.stdout)
    out = {}
    for n in nodes:
        files = []
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
        git = []
        for home, own, common in homes[n['id']]:
            git.append((str(home), stamp(home), str(common), repos.get(str(common)),
                        *(stamp(own / name) for name in (('HEAD', 'index', 'packed-refs', 'config') if own else ())),
                        *(stamp(path) for pattern in patterns for path in sorted(home.glob(pattern)))))
        mine = [r for r in runs or [] if isinstance(r, dict) and r.get('node') == n['id']]
        out[n['id']] = ui_json_fingerprint([shared, json.dumps(n['local'], sort_keys=True, default=str),
                                            files, git, mine])
    return out


def ui_json_views(inst, model, privacy, keys, cache):
    """Load persisted node views whose stamp still matches; others rebuild."""
    record = model.read('node-views')
    if not isinstance(record, dict) or record.get('binding') != model.binding(privacy):
        return {}, set()
    hits = set()
    for nid, entry in (record.get('nodes') or {}).items():
        if (isinstance(entry, dict) and nid in keys and entry.get('key') == keys[nid]
                and isinstance(entry.get('row'), dict) and isinstance(entry.get('commits'), list)):
            cache.nodes[nid] = (keys[nid], entry['row'], entry['commits'])
            hits.add(nid)
    return record, hits


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
        state['nodes'][index] = ui.ui_row_meters(inst, {**row, 'jobs': {**row['jobs'], 'open': opened}}, now)
    return True


def ui_json_build(inst, scan, code, cache=True):
    from . import check, status, ui
    before = ui_json_privacy_key(inst, code)
    model = _m_ui_readmodel.ReadModel(inst, code)
    # Node views persist under their own node stamp; a changed stamp rebuilds that view only.
    keys = ui_json_stamps(inst)
    views = ui.UIStateCache()
    record, hits = ui_json_views(inst, model, before, keys, views)
    # Integrity reads every node: its count reuses only an all-nodes stamp match.
    everything = ui_json_fingerprint(sorted(keys.items()))
    problems_entry = record.get('problems') if cache else None
    due_entries = record.get('due') if cache and isinstance(record.get('due'), dict) else {}
    due_memo = {nid: entry['items'] for nid, entry in due_entries.items()
                if nid in keys and ui_json_clocked(entry, keys[nid]) and isinstance(entry.get('items'), dict)}
    due_seen = {}
    # The full guard/check and recorded readiness observations stay unchanged.
    # No exclusive instance lock surrounds this computation or cache publication.
    with _m_ui_readmodel.cache_writes(cache), status.due_memo(due_memo, due_seen):
        if ui_json_clocked(problems_entry, everything) and type(problems_entry.get('count')) is int:
            problems, problems_at = problems_entry['count'], problems_entry['at']
        else:
            problems, problems_at = len(check.collect_problems(inst)[0]), time.time()
        state = ui.ui_state(inst, problems, scan, spend_budget=None, cache=views, node_keys=keys)
        if hits and not ui_json_view_usage(inst, state, hits):
            views.nodes.clear()
            state = ui.ui_state(inst, problems, scan, spend_budget=None, cache=views, node_keys=keys)
    current = ui.UIInstance(inst.root)
    if before != ui_json_privacy_key(current, code):
        return None  # do not publish text observed under an obsolete visibility policy
    if cache:
        # Publish only what was observed under a stamp that still holds after the build.
        after = ui_json_stamps(current)
        held = {nid for nid, key in keys.items() if after.get(nid) == key}
        due = {nid: {'key': keys[nid], 'at': due_entries[nid]['at'] if nid in due_memo else time.time(),
                     'items': {**due_memo.get(nid, {}), **due_seen.get(nid, {})}}
               for nid in set(due_memo) | set(due_seen) if nid in held}
        model.write('node-views', {
            'binding': model.binding(before),
            'nodes': {nid: {'key': key, 'row': row, 'commits': commits}
                      for nid, (key, row, commits) in views.nodes.items() if nid in held and key == keys[nid]},
            'due': due,
            'problems': ({'key': everything, 'at': problems_at, 'count': problems} if held == set(keys) else None)})
    state['brain'].pop('filling_in', None)
    state.pop('loading', None)  # snapshot metadata only; progress belongs to /api/loading
    record = {'version': 1, 'privacy': before, 'built_at': time.time(), 'state': state,
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
