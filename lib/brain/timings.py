"""Command-effect-scoped measurements and bounded metadata-only slow profiles."""
import contextlib
import contextvars
import cProfile
import functools
import json
import math
import datetime
import pstats
import re
import time
import uuid
from pathlib import Path

SLOW_SECONDS = 5.0
MAX_RECORD_BYTES = 16 * 1024
_CURRENT = contextvars.ContextVar('brain_command_timing', default=None)
_LABEL = contextvars.ContextVar('brain_command_label', default='invalid')
_PERSIST = contextvars.ContextVar('brain_command_timing_persist', default=True)
_NODE = contextvars.ContextVar('brain_command_timing_node', default=None)


def select_node(ref):
    _NODE.set(ref)


def timing_node(inst, ref=None):
    candidates = inst.nodes()
    if isinstance(ref, str):
        match = next((n for n in candidates if n['id'] == ref), None)
        if match:
            return match
    cwd = Path.cwd().resolve()
    matches = []
    for n in candidates:
        places = [n['dir'], n['home_path'], n['local'].get('workbench'),
                  *[w.get('path') for w in n['local'].get('worktrees', {}).values()]]
        for place in places:
            path = Path(place).expanduser().resolve() if place else None
            if path and (path == cwd or path in cwd.parents):
                matches.append((len(path.parts), n))
    return max(matches, key=lambda row: row[0])[1] if matches else inst.node('root')


def select_command(name, persist=True):
    """Called only after the CLI has selected and validated a registered command."""
    _LABEL.set(name)
    _PERSIST.set(persist)


def record(name, seconds):
    current = _CURRENT.get()
    if current is not None:
        current[name] = current.get(name, 0.0) + max(0.0, seconds)


@contextlib.contextmanager
def phase(name):
    start = time.perf_counter()
    try:
        yield
    finally:
        record(name, time.perf_counter() - start)


def timed(name):
    def decorate(fn):
        @functools.wraps(fn)
        def run(*args, **kwargs):
            with phase(name):
                return fn(*args, **kwargs)
        return run
    return decorate


@contextlib.contextmanager
def command(persist=True):
    from . import spend, usage_cache
    start = time.perf_counter()
    phases = {}
    token = _CURRENT.set(phases)
    label_token = _LABEL.set('invalid')
    persist_token = _PERSIST.set(persist)
    node_token = _NODE.set(None)
    # Function totals are sufficient for top-N diagnostics. Omitting caller
    # edges and individual C builtins keeps ordinary read commands inexpensive.
    profiler = cProfile.Profile(subcalls=False, builtins=False)
    profiler.enable()
    success = False
    try:
        with usage_cache.batch(), spend.accounting_snapshot():
            yield
        success = True
    except SystemExit as error:
        success = error.code in (None, 0)
        raise
    finally:
        profiler.disable()
        elapsed = time.perf_counter() - start
        _CURRENT.reset(token)
        # Telemetry never changes the command's outcome, even on damaged storage.
        try:
            if _PERSIST.get():
                publish(elapsed, phases, profiler, success)
        except (Exception, SystemExit):
            pass
        finally:
            _LABEL.reset(label_token)
            _PERSIST.reset(persist_token)
            _NODE.reset(node_token)


def publish(elapsed, phases, profiler, success):
    from . import events, guard, instance, nodes
    root = nodes.try_find_root()
    if root is None:
        return
    inst = instance.Instance(root)
    # Only a registered command word, never its arguments or raw invocation.
    name = _LABEL.get()
    identity = uuid.uuid4().hex
    data = dict(command=name, elapsed_s=round(elapsed, 6), slow=elapsed > SLOW_SECONDS,
                lock_wait_s=round(phases.get('lock_wait', 0.0), 6),
                lock_hold_s=round(phases.get('lock_hold', 0.0), 6),
                success=success, phases={k[:64]: round(v, 6) for k, v in sorted(phases.items())[:32]})
    if elapsed > SLOW_SECONDS:
        # Filenames alone (no absolute source paths, arguments, locals or callers).
        rows = sorted(pstats.Stats(profiler).stats.items(), key=lambda row: row[1][3], reverse=True)[:30]
        top = [dict(function=Path(k[0]).name + ':' + str(k[1]) + ':' + k[2],
                    calls=v[1], self_s=round(v[2], 6), cumulative_s=round(v[3], 6)) for k, v in rows]
        terms = [t for n in inst.nodes() for t in guard.never_terms(n)]
        for row in top:
            row['function'] = nodes.import_text(row['function'], terms, metadata=True)[:256]
        profile = {'command': name, 'elapsed_s': data['elapsed_s'], 'top': top}
        data['profile'] = profile
    record = dict(ts=inst.now(), node=events.node_ref(timing_node(inst, _NODE.get())['id']),
                  kind='timing', actor='brain', ref=identity, data=data)
    def size():
        return len((json.dumps(record, ensure_ascii=False, separators=(',', ':'), sort_keys=True) + '\n').encode())
    while size() > MAX_RECORD_BYTES and data.get('profile', {}).get('top'):
        data['profile']['top'].pop()
        data['profile']['truncated'] = True
    if size() > MAX_RECORD_BYTES:
        return  # optional diagnostics never publish an oversized record
    events.append_line(inst.root / '.brain/timings/events.jsonl', record)


def cmd_timings(a):
    from . import common, events, nodes
    inst = nodes.load()
    def stamp(value):
        if isinstance(value, (float, int)):
            number = float(value)
        else:
            try:
                number = float(value)
            except (ValueError, TypeError):
                value = re.sub(r'\s+([+-]\d{2}:\d{2})$', r'\1', value.replace('Z', '+00:00'))
                parsed = datetime.datetime.fromisoformat(value)
                if parsed.tzinfo is None:
                    raise ValueError('timestamp needs a timezone')
                number = parsed.timestamp()
        if not math.isfinite(number):
            raise ValueError('timestamp must be finite')
        return number
    try:
        since = stamp(a.since) if a.since else None
        until = stamp(a.until) if a.until else None
        if since is not None and until is not None and since > until:
            raise ValueError('since is after until')
        if a.slowest is not None and a.slowest < 1:
            raise ValueError('slowest must be positive')
    except (ValueError, TypeError, OverflowError) as error:
        common.die('invalid timing filter: ' + str(error))
    node = events.node_ref(inst.node(a.node)['id']) if a.node else None
    records = []
    paths = [inst.root / '.brain/timings/events.jsonl']
    for path in paths:
        try:
            with events.private_open(path) as stream:
                for line in stream:
                    try:
                        row = json.loads(line)
                        data = row.get('data', {})
                        if (row.get('kind') == 'timing' and isinstance(data, dict)
                                and isinstance(data.get('elapsed_s'), (int, float))
                                and math.isfinite(data['elapsed_s']) and data['elapsed_s'] >= 0
                                and (not node or events.same_node(row.get('node'), a.node))
                                and (since is None or stamp(row.get('ts')) >= since)
                                and (until is None or stamp(row.get('ts')) <= until)
                                and (not a.slow or data.get('slow'))
                                and (not a.command or data.get('command') == a.command)):
                            item = dict(data, ts=row.get('ts'), node=row.get('node'))
                            legacy = item.get('profile')
                            if isinstance(legacy, str) and re.fullmatch(r'[0-9a-f]{32}\.json', legacy):
                                try:
                                    profile = json.loads(events.private_read(inst.root / '.brain/timings' / legacy))
                                    if isinstance(profile, dict) and isinstance(profile.get('top'), list):
                                        item['profile'] = profile
                                except (OSError, ValueError, SystemExit):
                                    pass  # retained legacy reference remains visible when unavailable
                            records.append(item)
                    except (ValueError, TypeError, AttributeError, OverflowError):
                        continue
        except (OSError, ValueError, SystemExit):
            continue
    if a.summary:
        values = sorted(row['elapsed_s'] for row in records)
        result = dict(count=len(values), p50_s=values[math.ceil(len(values) * .50) - 1] if values else None,
                      p95_s=values[math.ceil(len(values) * .95) - 1] if values else None)
        print(json.dumps(result, indent=2) if a.json else
              f"count={result['count']} p50_s={result['p50_s']} p95_s={result['p95_s']}")
        return
    records = (sorted(records, key=lambda row: row['elapsed_s'], reverse=True)[:a.slowest]
               if a.slowest else records[-50:])
    if a.json:
        print(json.dumps(records, indent=2))
    elif not records:
        print('No matching command timings.')
    else:
        for row in records:
            phases = ', '.join(k + '=' + format(v, '.3f') + 's' for k, v in row.get('phases', {}).items()
                               if isinstance(v, (int, float)))
            print(f"{row['ts']} {row.get('command', '?')}: {row['elapsed_s']:.3f}s" +
                  (' slow' if row.get('slow') else '') + ('; ' + phases if phases else '') +
                  (('; profile: ' + str(len(row['profile'].get('top', []))) + ' functions'
                    if isinstance(row.get('profile'), dict) else '; profile .brain/timings/' + row['profile'])
                   if row.get('profile') else ''))


@contextlib.contextmanager
def queue_phase(root, name, backlog_s=None):
    """Bounded transport telemetry without profiler, payloads or instance locks."""
    if name not in ('enqueue', 'apply', 'wait'):
        raise ValueError('unknown queue timing phase')
    start, success = time.perf_counter(), False
    capture = {'publish': True}
    try:
        yield capture
        success = True
    finally:
        try:
            from . import events
            seconds = max(0.0, time.perf_counter() - start)
            phases = {'queue_' + name: round(seconds, 6)}
            if backlog_s is not None:
                phases['queue_backlog'] = round(max(0.0, backlog_s), 6)
            if capture['publish']:
                events.append_line(Path(root) / '.brain/timings/events.jsonl',
                                   dict(ts=time.time(), node=events.node_ref('root'), kind='timing',
                                        actor='brain', ref=uuid.uuid4().hex,
                                        data=dict(command='queue', elapsed_s=round(seconds, 6),
                                                  slow=seconds > SLOW_SECONDS, success=success, phases=phases)))
        except (Exception, SystemExit):
            pass  # optional telemetry never changes durable acceptance/application
