"""Read-only transaction evidence for isolated rehearsal commands.

This observer is copied from the supervising core into the probe bootstrap. It
never changes journal publication or supplies event permissions. Its evidence is
private; the comparator consumes exact bytes and fails closed on unknown shapes.
"""
import datetime
import copy
import contextvars
import functools
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import traceback
from pathlib import Path


class EvidenceError(ValueError):
    pass


def require(condition):
    if not condition:
        raise EvidenceError('incomplete transaction evidence')


def digest(value):
    return hashlib.sha256(value).hexdigest()


def capture(root, diagnostics=None):
    """Capture exact journal operands without following aliases or changing them."""
    result = {}
    directory = None
    try:
        try:
            directory = os.open(str(Path(root) / '.brain'), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return result
        initial = os.fstat(directory)
        for name in sorted(os.listdir(directory)):
            if name == 'events-leases':
                leases = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                try:
                    result[name] = dict(directory=True, mode=os.fstat(leases).st_mode,
                                        entries=sorted(os.listdir(leases)))
                finally:
                    os.close(leases)
                continue
            if not (name in ('events.jsonl', 'events-index.json', 'events-compare.jsonl', 'events-compare-index.json')
                    or re.fullmatch(r'events-\d{4}-\d{2}\.jsonl', name)):
                continue
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                st = os.fstat(fd)
                require(stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and st.st_size <= 32 * 1024 * 1024)
                with os.fdopen(os.dup(fd), 'rb') as stream:
                    payload = stream.read(32 * 1024 * 1024 + 1)
                def stable(value):
                    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns,
                            value.st_ctime_ns, value.st_mode, value.st_nlink)
                require(len(payload) == st.st_size and stable(os.fstat(fd)) == stable(st))
                require(stable(os.stat(name, dir_fd=directory, follow_symlinks=False)) == stable(st))
                result[name] = dict(hex=payload.hex(), dev=st.st_dev, ino=st.st_ino,
                                    mtime=st.st_mtime_ns, mode=st.st_mode)
            finally:
                os.close(fd)
        current = os.stat(str(Path(root) / '.brain'), follow_symlinks=False)
        require((initial.st_dev, initial.st_ino) == (current.st_dev, current.st_ino))
        return result
    except (OSError, ValueError) as error:
        if diagnostics is not None:
            diagnostics.update(checkpoint='capture', exception_category=type(error).__name__, partial_capture=result)
        return None
    finally:
        if directory is not None:
            os.close(directory)


def blob(snapshot, name):
    return bytes.fromhex(snapshot[name]['hex']) if name in snapshot else b''


def json_value(payload):
    def unique(pairs):
        result = {}
        for k, v in pairs:
            require(k not in result)
            result[k] = v
        return result
    return json.loads(payload, object_pairs_hook=unique)


def lines(payload):
    require(not payload or payload.endswith(b'\n'))
    return [json_value(line) for line in payload.splitlines()]


def journal_names(snapshot):
    return sorted(n for n in snapshot if re.fullmatch(r'events-\d{4}-\d{2}\.jsonl', n)) + (
        ['events.jsonl'] if 'events.jsonl' in snapshot else [])


def resource_map(rows):
    require(isinstance(rows, list))
    result = {}
    for row in rows:
        require(isinstance(row, dict) and re.fullmatch('[0-9a-f]{64}', row.get('id', '')) is not None)
        require(row['id'] not in result and (row.get('hash') is None or
                re.fullmatch('[0-9a-f]{64}', row['hash']) is not None))
        require(isinstance(row.get('scope'), str))
        result[row['id']] = row
    return result


def hashes(rows):
    return {k: v['hash'] for k, v in resource_map(rows).items() if v['hash'] is not None}


def replay(snapshot, events):
    state = events._blank()
    seen = set()
    opened = {}
    begun = set()
    records = []
    for name in journal_names(snapshot):
        payload = blob(snapshot, name)
        for line in payload.splitlines(keepends=True):
            value = json_value(line)
            event = events._event(line)
            require(event == value)
            identity = digest(line)
            require(identity not in seen)
            seen.add(identity)
            if event['kind'] == 'begin':
                require(event['ref'] not in begun)
                begun.add(event['ref'])
                opened[event['ref']] = event
            elif event['kind'] in ('commit', 'abort'):
                begin = opened.pop(event['ref'], None)
                require(begin is not None and begin['node'] == event['node'] and
                        begin['data'].get('command') == event['data'].get('command'))
            elif event['kind'] == 'reconcile':
                opened.pop(event['ref'], None)
            events._apply(state, event)
            records.append(event)
    return state, records


def check_index(snapshot, state, required=False):
    payload = blob(snapshot, 'events-index.json')
    require(bool(payload) or not required)
    if not payload:
        return
    saved = json_value(payload)
    expected = {}
    for name in journal_names(snapshot):
        row, body = snapshot[name], blob(snapshot, name)
        expected[str(row['dev']) + ':' + str(row['ino'])] = dict(
            offset=len(body), mtime=row['mtime'], anchor=digest(body[-256:]))
    require(saved == dict(version=2, positions=expected, state=state))


def timestamp(value):
    require(isinstance(value, str))
    match = re.fullmatch(r'(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)\s*(Z|[+-]\d{2}:?\d{2})', value)
    require(match is not None)
    zone = match[3]
    zone = '+00:00' if zone == 'Z' else zone[:3] + ':' + zone[-2:]
    return datetime.datetime.fromisoformat(match[1] + 'T' + match[2] + zone)


def check_comparisons(snapshot, now):
    payload = blob(snapshot, 'events-compare.jsonl')
    rows = lines(payload)
    identities = set()
    for row in rows:
        require(isinstance(row, dict))
        coverage = row.get('kind') == 'coverage'
        require(set(row) == ({
            'id', 'ts', 'node', 'kind', 'equal', 'resources', 'view_equal',
            'interrupted', 'faults'} | (set() if coverage else {'out_of_scope'})))
        require(re.fullmatch('[0-9a-f]{32}', row['id']) is not None and row['id'] not in identities)
        identities.add(row['id'])
        require(type(row['equal']) is bool)
        require((coverage and row['view_equal'] is None) or
                (row['kind'] == 'comparison' and type(row['view_equal']) is bool))
        require(isinstance(row['node'], str))
        timestamp(row['ts'])
        for key in ('resources', 'interrupted', 'faults', *(() if coverage else ('out_of_scope',))):
            require(isinstance(row[key], list) and all(isinstance(x, str) for x in row[key]))
    cache = blob(snapshot, 'events-compare-index.json')
    if cache:
        saved = json_value(cache)
        offset = saved.get('offset')
        require(type(offset) is int and 0 <= offset <= len(payload))
        prefix = payload[:offset]
        prefix_rows = lines(prefix)
        first = {}
        for row in prefix_rows:
            if row['kind'] != 'comparison':
                continue
            node = row['node']
            key = node if re.fullmatch('n:[0-9a-f]{64}', node) else 'n:' + digest(node.encode())
            if key not in first or timestamp(row['ts']) < timestamp(first[key]['ts']):
                first[key] = row
        recent = [r for r in prefix_rows if (now - timestamp(r['ts'])).total_seconds() <= 86400]
        source = snapshot['events-compare.jsonl']
        mtime = saved.get('mtime')
        require(type(mtime) is int and 0 <= mtime <= source['mtime'])
        require(offset < len(payload) or mtime == source['mtime'])
        require(saved == dict(version=1, identity=[source['dev'], source['ino']], offset=offset,
                              rows=recent, first=first, mtime=mtime, anchor=digest(prefix[-256:])))
    return rows


def check_owner(receipt, owner_pid, command, node, unleased=False):
    """A witnessed native lock acquisition, never a later begin's ownership."""
    require(isinstance(receipt, dict) and receipt['pid'] == owner_pid and
            receipt['command'] == command and receipt['node'] == node)
    lock = receipt['lock']
    require(isinstance(lock, dict) and lock['pid'] == owner_pid and
            type(lock['serial']) is int and lock['serial'] > 0 and
            type(lock['depth']) is int and lock['depth'] > 0 and
            type(lock['dev']) is int and lock['dev'] >= 0 and
            type(lock['ino']) is int and lock['ino'] > 0)
    if unleased:
        require(receipt['lease'] is None and receipt['ref'] is None)
    return {k: lock[k] for k in ('pid', 'serial', 'dev', 'ino')}


def coverage_fields(events, state, operand):
    require(operand['recorded'] == state['resources'] and operand['opened'] == state['opened'])
    actual = resource_map(operand['actual'])
    recorded = state['resources']
    changed = sorted(k for k in recorded.keys() | actual.keys()
                     if recorded.get(k, {}).get('hash') != actual.get(k, {}).get('hash'))
    nodes = operand['nodes']
    require(isinstance(nodes, list) and all(isinstance(n, str) for n in nodes) and len(set(nodes)) == len(nodes))
    affected = {operand['preferred']}
    require(operand['preferred'] in nodes)
    for key in changed:
        scope = (actual.get(key) or recorded[key]).get('scope', '*')
        affected.update(n for n in nodes if scope in ('*', events.node_ref(n)))
    leases = operand['leases']
    require(set(leases) == set(state['opened']) and all(type(v) is bool for v in leases.values()))
    dead = [ref for ref in state['opened'] if not leases[ref]]
    # The observer captures the raw journal map, including each damage reason.
    # Coverage rows separately publish its keys in native replay order.
    require(isinstance(operand['faults'], dict) and operand['faults'] == state['faults'])
    faults = list(state['faults'])
    return [dict(node=events.node_ref(n), kind='coverage', equal=not changed and not dead and not faults,
                 resources=changed, view_equal=None, interrupted=dead, faults=faults)
            for n in sorted(affected)], bool(changed or dead or faults)


def comparison_fields(events, state, actual, node, view, live, outbox):
    actual = resource_map(actual)
    before = state['resources']
    def belongs(row):
        return row.get('scope', '*') in ('*', node)
    changed = sorted(k for k in before.keys() | actual.keys()
                     if ((k in before and belongs(before[k])) or (k in actual and belongs(actual[k])))
                     and before.get(k, {}).get('hash') != actual.get(k, {}).get('hash'))
    previous = state['views'].get(node)
    equal = previous is not None and events.normalize(view) == events.normalize(previous)
    interrupted = [ref for ref in state['opened'] if ref not in live]
    outside = [] if previous is None or view == previous or not equal else ['external_git_or_clock']
    faults = list(state['faults'])
    return dict(node=node, kind='comparison', equal=not changed and equal and not interrupted and not outbox and not faults,
                resources=changed, view_equal=equal, interrupted=interrupted, out_of_scope=outside, faults=faults)


_validation_retainer = contextvars.ContextVar('rehearsal_validation_retainer', default=None)


def validate(before, after, observations, owner_pid, command, now, diagnostics=None):
    """Retain an opted-in call before its caller can assert or tear down."""
    owner = _validation_retainer.get()
    if owner is None or not owner.destination:
        return _validate(before, after, observations, owner_pid, command, now, diagnostics)
    if diagnostics is None:
        diagnostics = {}
    caller = sys._getframe(1)
    call = owner.validation_call(before, after, observations, owner_pid, command, now,
                                 diagnostics, dict(file=caller.f_code.co_filename,
                                                   line=caller.f_lineno,
                                                   function=caller.f_code.co_name))
    del caller
    result = 'UNKNOWN'
    try:
        result = _validate(before, after, observations, owner_pid, command, now, diagnostics)
        return result
    except BaseException as error:
        call.exception(error)
        raise
    finally:
        call.checkpoint(stage='validation-call-finally', diagnostics=diagnostics,
                        status='CAPTURED',
                        validator_result=(dict(proven_paths=sorted(result[0]), verdict=result[1])
                                          if result != 'UNKNOWN' else 'UNKNOWN'))
        if call.incomplete:
            owner.incomplete = True


def _validate(before, after, observations, owner_pid, command, now, diagnostics=None):
    """Return only proven changed operands. Unknown evidence grants nothing.

    A failed command with partial writes is recognized as an unresolved native
    operation, and remains unexpected. No product path is authorized here.
    """
    from . import events
    checkpoint = 'capture-inputs'
    try:
        require(before is not None and after is not None and isinstance(observations, list))
        checkpoint = 'history-preservation'
        require(set(before) <= set(after))
        known = {'events-leases', 'events.jsonl', 'events-index.json', 'events-compare.jsonl', 'events-compare-index.json'}
        require(all(n in known or re.fullmatch(r'events-\d{4}-\d{2}\.jsonl', n) for n in set(before) | set(after)))
        for snapshot in (before, after):
            if 'events-leases' in snapshot:
                require(snapshot['events-leases'].get('directory') is True and
                        snapshot['events-leases'].get('entries') == [])
        for name in before:
            if name == 'events-leases':
                require(before[name] == after[name])
                continue
            if name not in ('events-index.json', 'events-compare-index.json'):
                require(blob(after, name).startswith(blob(before, name)))
            require(before[name]['mode'] == after[name]['mode'])
        # Rotation changes names and identities: refuse until separately witnessed.
        require(journal_names(before) == journal_names(after) or
                set(journal_names(after)) - set(journal_names(before)) <= {'events.jsonl'})
        checkpoint = 'replay-before'
        state, old_records = replay(before, events)
        checkpoint = 'index-before'
        check_index(before, state)
        checkpoint = 'replay-after'
        final_state, final_records = replay(after, events)
        require(final_records[:len(old_records)] == old_records)
        added = final_records[len(old_records):]
        checkpoint = 'comparison-before'
        old_comparisons = check_comparisons(before, now)
        checkpoint = 'comparison-after'
        new_comparisons = check_comparisons(after, now)
        require(new_comparisons[:len(old_comparisons)] == old_comparisons)
        scopes, observed_events, observed_comparisons = {}, [], []
        for observation in observations:
            checkpoint = 'lifecycle.' + str(observation.get('type', 'unknown'))
            require(observation['pid'] == owner_pid)
            key, kind = observation['scope'], observation['type']
            if kind == 'enter':
                require(key not in scopes and observation['command'] == command)
                anchor = check_owner(observation['owner'], owner_pid, command, observation['node'], unleased=True)
                scopes[key] = dict(phase='enter', before=observation['resources'], ref=None, terminal=None,
                                   views={}, node=observation['node'], lock=anchor, coverage=None, covered=False)
                continue
            require(key in scopes)
            scope = scopes[key]
            if kind in ('coverage-enter', 'coverage-row', 'coverage-exit'):
                require(scope['phase'] == 'enter' and scope['ref'] is None)
                require(check_owner(observation['owner'], owner_pid, command, scope['node'], unleased=True) == scope['lock'])
                if kind == 'coverage-enter':
                    require(scope['coverage'] is None and not scope['covered'])
                    scope['coverage'] = []
                elif kind == 'coverage-row':
                    require(scope['coverage'] is not None)
                    scope['coverage'].append(observation['record'])
                    observed_comparisons.append(observation['record'])
                else:
                    require(scope['coverage'] is not None)
                    expected, result = coverage_fields(events, state, observation['operands'])
                    require(type(observation['result']) is bool and observation['result'] == result)
                    require([{k: v for k, v in r.items() if k not in ('id', 'ts')}
                             for r in scope['coverage']] == expected)
                    require(observation['operands']['preferred'] == observation['preferred'])
                    require(events.node_ref(observation['preferred']) == scope['node'])
                    scope.update(coverage=None, covered=True)
                continue
            require(scope['coverage'] is None)
            if kind == 'event':
                event = observation['record']
                require(event['actor'] == 'brain' and event['data'].get('version') == 1)
                tag = event['kind']
                actual = resource_map(observation['resources'])
                live = observation['live']
                if tag == 'begin':
                    require(scope['ref'] is None and re.fullmatch('[0-9a-f]{32}', event['ref']) is not None)
                    require(scope['phase'] == 'enter' and event['node'] == scope['node'])
                    require(scope['covered'] == bool(old_records or observed_events))
                    require(event['data'].get('command') == command and event['ref'] in live and observation['owned_lease'] is True)
                    scope['before'] = observation['resources']
                    baseline = event['data'].get('baseline', [])
                    require(hashes(baseline) == (hashes(scope['before']) if not old_records and not observed_events else {}))
                    scope.update(ref=event['ref'], node=event['node'], phase='started')
                elif tag in ('commit', 'abort'):
                    require(scope['phase'] == 'finishing' and scope['terminal'] is None)
                    require(event['ref'] == scope['ref'] and event['node'] == scope['node'] and
                            event['ref'] in live and observation['owned_lease'] is True)
                    require(event['data'].get('command') == command)
                    previous = hashes(scope['before'])
                    current = {k: r['hash'] for k, r in actual.items()}
                    changed = {k: current.get(k) for k in previous.keys() | current.keys()
                               if previous.get(k) != current.get(k)
                               and state['resources'].get(k, {}).get('hash') != current.get(k)}
                    if tag == 'abort':
                        require(not scope['success'] and not changed and 'resources' not in event['data'])
                    else:
                        require(scope['success'] and type(event['data'].get('exit')) is int)
                        require({k: r['hash'] for k, r in resource_map(event['data'].get('resources')).items()} == changed)
                        for identity, row in resource_map(event['data'].get('resources')).items():
                            expected = actual.get(identity) or state['resources'].get(identity)
                            require(expected is not None and row['scope'] == expected['scope'])
                        require(type(event['data'].get('maintenance_ok')) is bool)
                    require(all(scope['views'].get(k) == v for k, v in event['data'].get('views', {}).items()))
                    scope['terminal'] = tag
                elif tag == 'reconcile':
                    require(scope['phase'] in ('started', 'finishing') and scope['ref'] in live)
                    require(not ((set(state['opened']) - {scope['ref']}) & set(live)))
                    require(event['ref'] not in live and event['data'].get('flagged') is True)
                    require(event['data'].get('clear_faults') is True)
                    reconciled = resource_map(event['data'].get('resources'))
                    require(hashes(event['data'].get('resources')) == hashes(observation['resources']))
                    require(all(reconciled[k]['scope'] == row['scope'] for k, row in actual.items()))
                    require(set(reconciled) == set(actual) | set(state['resources']))
                    require(all(row['hash'] is None for k, row in reconciled.items() if k not in actual))
                    require(all(scope['views'].get(k) == v for k, v in event['data'].get('views', {}).items()))
                    # A reconciliation may not hide a new or foreign operation.
                    require(event['ref'] not in state['opened'] or event['node'] == state['opened'][event['ref']]['node'])
                else:
                    raise EvidenceError('unowned event kind')
                events._apply(state, event)
                observed_events.append(event)
            elif kind == 'view':
                scope['views'][observation['node']] = observation['view']
            elif kind == 'comparison':
                require(scope['phase'] in ('started', 'finishing'))
                row = observation['record']
                require(scope['ref'] in observation['live'])
                expected = comparison_fields(events, state, observation['resources'], row['node'],
                                             observation['view'], observation['live'], observation['outbox'])
                require({k: v for k, v in row.items() if k not in ('id', 'ts')} == expected)
                observed_comparisons.append(row)
            elif kind == 'finish':
                require(scope['phase'] == 'started' and scope['ref'] in observation['live'] and observation['owned_lease'] is True)
                require(type(observation['success']) is bool)
                scope.update(phase='finishing', success=observation['success'])
            elif kind == 'close':
                require(scope['phase'] == 'finishing' and scope['terminal'] is not None)
                require(scope['ref'] not in observation['live'] and observation['lease_absent'] is True)
                scope['phase'] = 'closed'
            elif kind == 'exit':
                require(scope['phase'] == 'closed')
                require(hashes(observation['resources']) == {k: r['hash'] for k, r in state['resources'].items()})
                scope['phase'] = 'exited'
            else:
                raise EvidenceError('unknown lifecycle')
        checkpoint = 'lifecycle-completeness'
        require(scopes and all(s['phase'] == 'exited' for s in scopes.values()))
        checkpoint = 'lifecycle-record-accounting'
        require(observed_events == added and observed_comparisons == new_comparisons[len(old_comparisons):])
        checkpoint = 'replay-final'
        require(state == final_state and not state['opened'] and not state['faults'] and not state['invalid'])
        checkpoint = 'index-after'
        check_index(after, final_state, required=bool(added))
        changed = {name for name in set(before) | set(after) if before.get(name) != after.get(name)}
        # The derived cache alone never constitutes an owned operation.
        require(added)
        if diagnostics is not None:
            diagnostics.update(checkpoint='complete', exception_category=None)
        return {'brain/.brain/' + name for name in changed}, 'proven'
    except (EvidenceError, OSError, ValueError, TypeError, KeyError, AttributeError, IndexError) as error:
        if diagnostics is not None:
            diagnostics.update(checkpoint=checkpoint, exception_category=type(error).__name__,
                               rejection_stack=[dict(function=f.name, line=f.lineno)
                                                for f in traceback.extract_tb(error.__traceback__)])
        return set(), 'unproven'


def install_observer(events, root, trace):
    """Observe native lifecycle calls; keep receipts outside product storage."""
    stack = []
    active_ops = []
    sequence = [0]
    lock_sequence, lock_anchor = [0], [None]
    native_instance = events.instance
    def owner():
        op = active_ops[-1]
        fh = native_instance._LOCK['fh']
        require(fh is not None and not fh.closed and lock_anchor[0] is not None)
        fd = os.fstat(fh.fileno())
        path = os.stat(Path(root) / '.brain/lock', follow_symlinks=False)
        require(stat.S_ISREG(fd.st_mode) and fd.st_nlink == 1 and
                (fd.st_dev, fd.st_ino) == (path.st_dev, path.st_ino) == lock_anchor[0][1:] and
                os.pread(fh.fileno(), 128, 0).decode().strip() == str(os.getpid()) and
                os.environ.get('BRAIN_LOCK_HELD') == str(Path(root).resolve()) + '|' + str(os.getpid()))
        return dict(pid=os.getpid(), command=op.command, node=events.node_ref(op.node),
                    ref=getattr(op, 'ref', None), lease=None if op.lease is None else owned_lease(),
                    lock=dict(pid=os.getpid(), serial=lock_anchor[0][0], dev=fd.st_dev, ino=fd.st_ino,
                              depth=native_instance._LOCK['depth']))
    def resources(inst):
        return events.resource_list(inst, events.resources(inst), display=False)
    def live(inst):
        refs = set(events.journal(inst).state['opened']) | {getattr(op, 'ref', '') for op in active_ops}
        return sorted(ref for ref in refs if ref and events.lease_live(inst, ref))
    def owned_lease():
        if not active_ops:
            return False
        op = active_ops[-1]
        if getattr(op, 'lease', None) is None:
            return False
        fd = os.fstat(op.lease)
        path = os.stat(events.lease_path(op.inst, op.ref), follow_symlinks=False)
        return (stat.S_ISREG(fd.st_mode) and fd.st_nlink == 1 and
                (fd.st_dev, fd.st_ino) == (path.st_dev, path.st_ino) and events.lease_live(op.inst, op.ref))
    def emit(kind, **data):
        if stack:
            with Path(trace).open('a') as stream:
                stream.write(json.dumps(dict(type=kind, scope=stack[-1], pid=os.getpid(), **data), sort_keys=True) + '\n')
    def observe(kind, inst=None, **data):
        try:
            if inst is not None:
                data.update(resources=resources(inst), live=live(inst), owned_lease=owned_lease())
            emit(kind, **data)
        except Exception:
            # Missing/invalid evidence fails closed in the supervisor. Preserve
            # the native call's own result and exception, including partial writes.
            try:
                emit('invalid')
            except Exception:
                pass
    def wrap(cls, name, factory):
        original = getattr(cls, name)
        setattr(cls, name, functools.wraps(original)(factory(original)))
    def lock_enter(original):
        def call(lock):
            result = original(lock)
            if lock.key == str(Path(root).resolve()):
                try:
                    fh = native_instance._LOCK['fh']
                    if fh is not None:
                        st = os.fstat(fh.fileno())
                        if lock_anchor[0] is None:
                            lock_sequence[0] += 1
                            lock_anchor[0] = (lock_sequence[0], st.st_dev, st.st_ino)
                except Exception:
                    lock_anchor[0] = None
            return result
        return call
    def lock_exit(original):
        def call(lock, *args):
            try:
                return original(lock, *args)
            finally:
                if lock.key == str(Path(root).resolve()) and native_instance._LOCK['fh'] is None:
                    lock_anchor[0] = None
        return call
    wrap(native_instance.brain_lock, '__enter__', lock_enter)
    wrap(native_instance.brain_lock, '__exit__', lock_exit)
    def enter(original):
        def call(op):
            if op.inst.root.resolve() != Path(root).resolve():
                return original(op)
            sequence[0] += 1
            stack.append(str(sequence[0]))
            active_ops.append(op)
            try:
                observe('enter', op.inst, command=op.command, node=events.node_ref(op.node), owner=owner())
            except Exception:
                observe('invalid')
            try:
                return original(op)
            except BaseException:
                stack.pop()
                active_ops.pop()
                raise
        return call
    def append(original):
        def call(store, *args, **kwargs):
            record = original(store, *args, **kwargs)
            observe('event', store.inst, record=record)
            return record
        return call
    def finish(original):
        def call(op, success, exit_code=0):
            observe('finish', op.inst, success=success)
            return original(op, success, exit_code)
        return call
    def close(original):
        def call(op):
            result = original(op)
            observe('close', op.inst, lease_absent=not events.lease_path(op.inst, op.ref).exists())
            return result
        return call
    def leave(original):
        def call(op, *args):
            owned = bool(stack) and op.inst.root.resolve() == Path(root).resolve()
            try:
                return original(op, *args)
            finally:
                if owned:
                    observe('exit', op.inst)
                    stack.pop()
                    active_ops.pop()
        return call
    wrap(events.Command, '__enter__', enter)
    wrap(events.Command, 'finish', finish)
    wrap(events.Command, 'close_lease', close)
    wrap(events.Command, '__exit__', leave)
    wrap(events.Journal, 'append', append)
    original_view = events.snapshot_view
    def snapshot_view(inst, node, *args, **kwargs):
        result = original_view(inst, node, *args, **kwargs)
        observe('view', node=events.node_ref(node['id']), view=result)
        return result
    events.snapshot_view = snapshot_view
    original_comparison = events.comparison
    def comparison(inst, node, current=None, record=True):
        # Observe the native call's view without moving its computation or writes.
        captured = []
        previous_view = events.snapshot_view
        def observe_view(*args, **kwargs):
            result = previous_view(*args, **kwargs)
            captured.append(result)
            return result
        events.snapshot_view = observe_view
        try:
            row = original_comparison(inst, node, current=current, record=record)
        finally:
            events.snapshot_view = previous_view
        if record:
            try:
                view = current if current is not None else captured[-1]
                observe('comparison', inst, record=row, view=view, outbox=bool(events.outbox_pending(inst)))
            except Exception:
                observe('invalid')
        return row
    events.comparison = comparison
    original_detect = events.detect_gaps
    def detect_gaps(inst, preferred):
        if not stack or inst.root.resolve() != Path(root).resolve():
            return original_detect(inst, preferred)
        # Capture operands at the native calls that consume them. No second
        # resource scan may be substituted after publication or begin.
        saved = {name: getattr(events, name) for name in ('replay', 'resource_list', 'lease_live', 'append_line')}
        operand = dict(preferred=preferred, leases={})
        try:
            operand.update(nodes=[n['id'] for n in inst.nodes()],
                           faults=copy.deepcopy(events.journal(inst).state['faults']))
        except Exception:
            observe('invalid')
        counts = dict(replay=0, actual=0)
        def replay_call(*args, **kwargs):
            result = saved['replay'](*args, **kwargs)
            try:
                counts['replay'] += 1
                operand.update(recorded=copy.deepcopy(result[0]), opened=copy.deepcopy(result[2]))
            except Exception:
                observe('invalid')
            return result
        def resource_call(*args, **kwargs):
            result = saved['resource_list'](*args, **kwargs)
            try:
                counts['actual'] += 1
                operand['actual'] = copy.deepcopy(result)
            except Exception:
                observe('invalid')
            return result
        def lease_call(instance, ref):
            result = saved['lease_live'](instance, ref)
            if ref in operand['leases']:
                observe('invalid')
            operand['leases'][ref] = result
            return result
        def append_call(path, value):
            result = saved['append_line'](path, value)
            try:
                require(Path(path) == inst.root / '.brain/events-compare.jsonl')
                emit('coverage-row', record=copy.deepcopy(value), owner=owner())
            except Exception:
                observe('invalid')
            return result
        try:
            try:
                emit('coverage-enter', preferred=preferred, owner=owner())
            except Exception:
                observe('invalid')
            events.replay, events.resource_list = replay_call, resource_call
            events.lease_live, events.append_line = lease_call, append_call
            result = original_detect(inst, preferred)
            try:
                require(counts == dict(replay=1, actual=1))
                emit('coverage-exit', preferred=preferred, operands=operand, result=result, owner=owner())
            except Exception:
                observe('invalid')
            return result
        except BaseException:
            observe('invalid')
            raise
        finally:
            for name, value in saved.items():
                setattr(events, name, value)
    events.detect_gaps = detect_gaps


def _persist_fixture_evidence(lane, raw, row, failure=None):
    """Opt-in private retention before fixture assertions and teardown."""
    destination = os.environ.get('BRAIN_REHEARSAL_EVIDENCE_DIR')
    if not destination:
        return
    directory = Path(destination)
    for parent in (directory, *directory.parents):
        if parent.is_symlink() or (parent / '.git').exists():
            raise ValueError('private evidence destination refused')
    owner = directory.stat()
    if owner.st_uid != os.getuid() or stat.S_IMODE(owner.st_mode) != 0o700:
        raise ValueError('private evidence destination refused')
    source = Path(__file__).resolve().parents[2]
    require(source not in directory.resolve().parents and lane.resolve() not in directory.resolve().parents)
    packet = dict(version=1, command=[os.sys.executable, *os.sys.argv], raw=raw, row=row,
                  fixture_exception=failure, source_hashes={}, bootstrap_hashes={},
                  native_receipt_linkage='post-run receipt manifest binds this immutable artifact hash')
    for name in ('lib/brain/rehearse.py', 'lib/brain/rehearse_transactions.py',
                 'lib/brain/events.py', 'lib/brain/communication.py', 'lib/brain/instance.py',
                 'lib/brain/cli.py', 'tests/test_rehearse.py', 'tests/scenario.py', 'tests/core_loader.py'):
        packet['source_hashes'][name] = digest((source / name).read_bytes())
    bootstrap = lane / 'bootstrap'
    if bootstrap.exists():
        for path in sorted(bootstrap.glob('*.py')):
            packet['bootstrap_hashes'][path.name] = digest(path.read_bytes())
    audit = lane / 'audit.py'
    packet['audit_hash'] = digest(audit.read_bytes()) if audit.exists() else None
    packet['native_core_hashes'] = {}
    for name in ('bin/brain', 'lib/brain/events.py', 'lib/brain/rehearse.py', 'lib/brain/rehearse_transactions.py'):
        native = lane / 'brain/core' / name
        packet['native_core_hashes'][name] = digest(native.read_bytes()) if native.exists() else None
    trace = lane / 'home/.local/state/brain/rehearsal-transactions.jsonl'
    packet['ordered_trace_hex'] = trace.read_bytes().hex() if trace.exists() else None
    # This separate fallback is labelled; it never replaces a missing validator input.
    packet['failure_boundary_capture'] = capture(lane / 'brain')
    body = (json.dumps(packet, sort_keys=True) + '\n').encode()
    fd, filename = tempfile.mkstemp(prefix='transaction-proof-', suffix='.json', dir=directory)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())
    diagnostic = raw[-1].get('transaction_evidence', {}).get('diagnostics', {}) if raw else {}
    checkpoint = re.sub('[^a-zA-Z0-9_.-]', '_', str(diagnostic.get('checkpoint', 'fixture-incomplete')))
    category = re.sub('[^a-zA-Z0-9_.-]', '_', str(diagnostic.get('exception_category', 'none')))
    print('transaction-proof checkpoint=' + checkpoint + ' category=' + category +
          ' artifact=' + Path(filename).name + ' sha256=' + digest(body))
