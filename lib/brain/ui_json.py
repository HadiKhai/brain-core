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
from . import guard as _m_guard
from . import nodes as _m_nodes
from . import spend as _m_spend
from . import ui_readmodel as _m_ui_readmodel
from . import usage as _m_usage


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
            and isinstance(state.get('loading'), dict) and not state['loading'].get('filling_in')
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
    sources, allowed, forbidden = _m_spend.spend_sources(inst)
    declarations = []
    for name in ('subscriptions.json', 'brains.json'):
        path = _m_usage.usage_file(name)
        try:
            declarations.append((name, path.read_bytes().hex()))
        except FileNotFoundError:
            declarations.append((name, None))
    value = [str(inst.root.resolve()), code, inst.cfg, inst.principals, inst.revoked,
             [(n['id'], n['owners'], n['sealed'], str(n['home_path']), _m_guard.never_terms(n)) for n in inst.nodes()],
             [(rt, str(cfg)) for rt, cfg in sources], sorted(map(str, allowed)), sorted(map(str, forbidden)), declarations]
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


def ui_json_source(state, record=None, refreshing=False, failed=False, origin=None):
    now = time.time()
    built = record['built_at'] if record else None
    age = max(0, now - built) if built is not None else None
    state = {**state, 'brain': {**state['brain']}, 'review': {'enabled': False}}
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


def ui_json_build(inst, scan, code, cache=True):
    from . import check, ui
    before = ui_json_privacy_key(inst, code)
    # The full guard/check and recorded readiness observations stay unchanged.
    # No exclusive instance lock surrounds this computation or cache publication.
    with _m_ui_readmodel.cache_writes(cache):
        state = ui.ui_state(inst, len(check.collect_problems(inst)[0]), scan, spend_budget=None)
    current = ui.UIInstance(inst.root)
    if before != ui_json_privacy_key(current, code):
        return None  # do not publish text observed under an obsolete visibility policy
    state['brain'].pop('filling_in', None)
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
        return ui_json_source(record['state'], record, origin='fresh-full')
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
                    return ui_json_source(latest['state'], latest, origin='fresh-full')
            if cache and not acquired:
                stack.close()
                # Join the owner once. Never queue a redundant full rebuild.
                stack.enter_context(ui_json_fill_lock(inst, blocking=True))
                joined = ui_json_last_complete(ui.UIInstance(inst.root), code, scan)
                if joined is None or not joined.get('generation') or joined['generation'] == generation:
                    _m_common.die('UI fill did not publish a validated complete snapshot; retry --wait')
                return ui_json_source(joined['state'], joined, origin='fresh-full')
            # Build failures remain failures; only optional lock/storage errors fall back.
            record = ui_json_build(inst, scan, code, cache=cache)
        finally:
            try:
                stack.close()
            except (OSError, ValueError, SystemExit):
                pass
        if record is not None:
            return ui_json_source(record['state'], record, origin='fresh-full')
        _m_common.die('UI visibility changed during fill; retry --wait')
    else:
        try:
            record = ui_json_load(inst, code, scan)
        except (OSError, ValueError, SystemExit):
            record = None
        if record is not None:
            stale = time.time() - record['built_at'] >= 60
            refreshing = ui_json_request_fill(inst, scan) if stale else False
            return ui_json_source(record['state'], record, refreshing, ui_json_fill_failed(inst, code))
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
    result = ui_json_source(screen(partial), refreshing=refreshing)
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
