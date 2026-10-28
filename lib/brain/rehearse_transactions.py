"""Read-only transaction evidence for isolated rehearsal commands.

This observer is copied from the supervising core into the probe bootstrap. It
never changes journal publication or supplies event permissions. Its evidence is
private; the comparator consumes exact bytes and fails closed on unknown shapes.
"""
import datetime
import copy
import contextvars
import contextlib
import functools
import hashlib
import importlib
import json
import os
import re
import stat
import sys
import tempfile
import time
import traceback
from pathlib import Path


CAPTURE_BYTES = 32 * 1024 * 1024


class EvidenceError(ValueError):
    pass


def require(condition):
    if not condition:
        raise EvidenceError('incomplete transaction evidence')


def digest(value):
    return hashlib.sha256(value).hexdigest()


def capture_timing(root):
    """Exact private telemetry operands; unknown shapes grant no write authority."""
    return capture_private(root, 'timings', ('events.jsonl',))


def capture_private(root, folder, files):
    """Capture bounded private regular files through retained directory descriptors."""
    result = {}
    try:
        brain = os.open(str(Path(root) / '.brain'), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return result
    try:
        try:
            directory = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=brain)
        except FileNotFoundError:
            return result
        try:
            info = os.fstat(directory)
            require(stat.S_IMODE(info.st_mode) == 0o700 and info.st_uid == os.getuid())
            entries = sorted(os.listdir(directory))
            result[folder] = dict(directory=True, mode=info.st_mode, uid=info.st_uid,
                                     dev=info.st_dev, ino=info.st_ino, entries=entries)
            for name in files:
                if name not in entries:
                    continue
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                try:
                    before = os.fstat(fd)
                    require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and
                            stat.S_IMODE(before.st_mode) == 0o600 and before.st_uid == os.getuid()
                            and before.st_size <= 32 * 1024 * 1024)
                    payload = os.pread(fd, 32 * 1024 * 1024 + 1, 0)
                    def stable(st):
                        return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns,
                                st.st_ctime_ns, st.st_mode, st.st_nlink, st.st_uid)
                    require(len(payload) == before.st_size and stable(before) == stable(os.fstat(fd)) and
                            stable(before) == stable(os.stat(name, dir_fd=directory, follow_symlinks=False)))
                    result[folder + '/' + name] = dict(hex=payload.hex(), dev=before.st_dev, ino=before.st_ino,
                        mtime=before.st_mtime_ns, mode=before.st_mode, uid=before.st_uid)
                finally:
                    os.close(fd)
            current = os.stat(folder, dir_fd=brain, follow_symlinks=False)
            require((info.st_dev, info.st_ino, info.st_mode, info.st_uid) ==
                    (current.st_dev, current.st_ino, current.st_mode, current.st_uid) and
                    result[folder]['entries'] == sorted(os.listdir(directory)))
        finally:
            os.close(directory)
        return result
    finally:
        os.close(brain)


def validate_timing(before, after, observations, owner_pid, command):
    """Grant only an actually observed native append, never a timing path exemption."""
    names = {'timings', 'timings/events.jsonl'}
    old, new = ({k: v for k, v in snapshot.items() if k in names} for snapshot in (before, after))
    rows = [row for row in observations if row.get('type') == 'telemetry']
    if old == new:
        require(not rows)
        return set()
    require(len(rows) == 1)
    row = rows[0]
    require(row['pid'] == owner_pid and row['before'] == old and row['after'] == new)
    require(set(new) == names and set(old) <= names)
    directory = new['timings']
    require(directory['directory'] is True and stat.S_IMODE(directory['mode']) == 0o700
            and directory['uid'] == os.getuid())
    if 'timings' in old:
        require(all(old['timings'][k] == directory[k] for k in ('directory', 'mode', 'uid', 'dev', 'ino')))
        require(directory['entries'] == sorted(set(old['timings']['entries']) | {'events.jsonl'}))
    else:
        require(directory['entries'] == ['events.jsonl'])
    current = new['timings/events.jsonl']
    require(stat.S_ISREG(current['mode']) and stat.S_IMODE(current['mode']) == 0o600
            and current['uid'] == os.getuid())
    if 'timings/events.jsonl' in old:
        prior = old['timings/events.jsonl']
        require(all(prior[k] == current[k] for k in ('dev', 'ino', 'mode', 'uid')))
    record = row['record']
    require(record['kind'] == 'timing' and record['actor'] == 'brain' and
            record['data']['command'] == command)
    payload = (json.dumps(record, ensure_ascii=False, separators=(',', ':'), sort_keys=True) + '\n').encode()
    require(len(payload) <= 16 * 1024)
    require(blob(new, 'timings/events.jsonl') == blob(old, 'timings/events.jsonl') + payload)
    return {'timings/events.jsonl'} | ({'timings'} if 'timings' not in old else set())


UI_CACHE_FILES = ('json-fill.lock', 'json-snapshot.json', 'json-fill-status.json')


def capture_read(root):
    return {**capture_timing(root), **capture_private(root, 'ui-cache', UI_CACHE_FILES)}


def validate_read(before, after, observations, owner_pid, command, diagnostics=None):
    """Prove only native read observations; no operational journal permission."""
    try:
        require(before is not None and after is not None and isinstance(observations, list))
        require(all(row['pid'] == owner_pid and row['type'] in ('telemetry', 'ui-cache')
                    for row in observations))
        proven = validate_timing(before, after, observations, owner_pid, command)
        names = {'ui-cache', *('ui-cache/' + name for name in UI_CACHE_FILES)}
        old, new = ({k: v for k, v in snap.items() if k in names} for snap in (before, after))
        state, ui_proven = old, set()
        for row in (o for o in observations if o['type'] == 'ui-cache'):
            require(command == 'ui' and row['before'] == state)
            current, name = row['after'], row['name']
            require(name in UI_CACHE_FILES)
            key = 'ui-cache/' + name
            require(set(current) == set(state) | {'ui-cache', key})
            require(all(current[k] == v for k, v in state.items() if k not in ('ui-cache', key)))
            directory = current['ui-cache']
            require(directory['directory'] is True and stat.S_IMODE(directory['mode']) == 0o700
                    and directory['uid'] == os.getuid())
            if 'ui-cache' in state:
                prior = state['ui-cache']
                require(all(prior[k] == directory[k] for k in ('dev', 'ino', 'mode', 'uid')))
                require(directory['entries'] == sorted(set(prior['entries']) | {name}))
            else:
                require(directory['entries'] == [name])
            file = current[key]
            require(stat.S_ISREG(file['mode']) and stat.S_IMODE(file['mode']) == 0o600
                    and file['uid'] == os.getuid())
            require(blob(current, key) == bytes.fromhex(row['hex']))
            if name == 'json-fill.lock':
                require(row['hex'] == '')
                if key in state:
                    require(state[key] == file)
            else:
                require(isinstance(json.loads(bytes.fromhex(row['hex'])), dict))
            ui_proven.add(key)
            if 'ui-cache' not in state:
                ui_proven.add('ui-cache')
            state = current
        require(state == new)
        changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
        if 'ui-cache' in before and 'ui-cache' in after:
            changed.discard('ui-cache')
        require(changed <= proven | ui_proven)
        if diagnostics is not None:
            diagnostics.update(checkpoint='read-complete', exception_category=None)
        return {'brain/.brain/' + name for name in changed}, 'proven'
    except (EvidenceError, OSError, ValueError, TypeError, KeyError, AttributeError):
        if diagnostics is not None:
            diagnostics.update(checkpoint='read-observations', exception_category='EvidenceError')
        return set(), 'unproven'


def capture(root, diagnostics=None, stream_dir=None):
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
            if name == 'event-objects':
                objects = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                try:
                    entries = {}
                    for child in sorted(os.listdir(objects)):
                        require(re.fullmatch('[0-9a-f]{64}\\.json', child) is not None)
                        fd = os.open(child, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=objects)
                        try:
                            st = os.fstat(fd)
                            require(stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid()
                                    and st.st_nlink == 1 and st.st_size <= CAPTURE_BYTES)
                            with os.fdopen(os.dup(fd), 'rb') as source:
                                raw = source.read(CAPTURE_BYTES + 1)
                            require(len(raw) == st.st_size and digest(raw) == child[:-5])
                            obj = json_value(raw)
                            require(set(obj) == {'version', 'record_type', 'key', 'text'} and obj['version'] == 1
                                    and isinstance(obj['text'], str))
                            require(raw == (json.dumps(obj, ensure_ascii=False, sort_keys=True,
                                                       separators=(',', ':')) + '\n').encode())
                            attached = os.stat(child, dir_fd=objects, follow_symlinks=False)
                            require((st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns) ==
                                    (attached.st_dev, attached.st_ino, attached.st_size,
                                     attached.st_mtime_ns, attached.st_ctime_ns))
                            entries[child] = dict(record_type=obj['record_type'], key=obj['key'],
                                                  text_sha256=digest(obj['text'].encode()), mode=st.st_mode)
                        finally:
                            os.close(fd)
                    result[name] = dict(directory=True, mode=os.fstat(objects).st_mode, entries=entries)
                finally:
                    os.close(objects)
                continue
            if name == 'events-leases':
                leases = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                try:
                    result[name] = dict(directory=True, mode=os.fstat(leases).st_mode,
                                        entries=sorted(os.listdir(leases)))
                finally:
                    os.close(leases)
                continue
            if not (name in ('events.jsonl', 'events-index.json', 'events-compare.jsonl', 'events-compare-index.json',
                            'hook-events.jsonl', 'hook-events-offset', 'events-status-commits.json')
                    or re.fullmatch(r'events-\d{4}-\d{2}\.jsonl', name)):
                continue
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                st = os.fstat(fd)
                require(stat.S_ISREG(st.st_mode) and st.st_nlink == 1)
                streamed = None
                streamable = (name in ('events.jsonl', 'events-compare.jsonl', 'events-compare-index.json')
                              or re.fullmatch(r'events-\d{4}-\d{2}\.jsonl', name))
                if stream_dir is not None and streamable and st.st_size > min(1024 * 1024, CAPTURE_BYTES):
                    destination = Path(stream_dir)
                    require(destination.is_absolute() and destination.resolve() == destination
                            and destination.stat().st_uid == os.getuid()
                            and stat.S_IMODE(destination.stat().st_mode) == 0o700
                            and not any(p.is_symlink() for p in (destination, *destination.parents)))
                    out, filename = tempfile.mkstemp(prefix='history-', dir=destination)
                    h, total = hashlib.sha256(), 0
                    with os.fdopen(out, 'wb') as target:
                        while True:
                            chunk = os.read(fd, 65536)
                            if not chunk:
                                break
                            total += len(chunk)
                            require(total <= st.st_size)
                            h.update(chunk)
                            target.write(chunk)
                        target.flush()
                        os.fsync(target.fileno())
                    require(total == st.st_size)
                    value = os.stat(filename, follow_symlinks=False)
                    streamed = dict(path=filename, dev=value.st_dev, ino=value.st_ino,
                                    size=total, sha256=h.hexdigest())
                else:
                    require(st.st_size <= CAPTURE_BYTES)
                    with os.fdopen(os.dup(fd), 'rb') as stream:
                        payload = stream.read(CAPTURE_BYTES + 1)
                def stable(value):
                    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns,
                            value.st_ctime_ns, value.st_mode, value.st_nlink)
                require((streamed is not None or len(payload) == st.st_size) and stable(os.fstat(fd)) == stable(st))
                require(stable(os.stat(name, dir_fd=directory, follow_symlinks=False)) == stable(st))
                result[name] = dict(dev=st.st_dev, ino=st.st_ino, mtime=st.st_mtime_ns, mode=st.st_mode)
                result[name].update(dict(stream=streamed) if streamed is not None else dict(hex=payload.hex()))
            finally:
                os.close(fd)
        result.update(capture_timing(root))
        current = os.stat(str(Path(root) / '.brain'), follow_symlinks=False)
        require((initial.st_dev, initial.st_ino) == (current.st_dev, current.st_ino))
        result['status-archives'] = capture_archives(Path(root))
        return result
    except (OSError, ValueError) as error:
        if diagnostics is not None:
            diagnostics.update(checkpoint='capture', exception_category=type(error).__name__, partial_capture=result)
        return None
    finally:
        if directory is not None:
            os.close(directory)


def blob(snapshot, name):
    require(name not in snapshot or 'stream' not in snapshot[name])
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
        for line in _journal_lines(snapshot, name):
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
        row = snapshot[name]
        with _operand(snapshot, name) as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - 256))
            anchor = digest(stream.read(256))
        expected[str(row['dev']) + ':' + str(row['ino'])] = dict(
            offset=size, mtime=row['mtime'], anchor=anchor)
    require(saved == dict(version=2, positions=expected, state=state))


def timestamp(value):
    require(isinstance(value, str))
    match = re.fullmatch(r'(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)\s*(Z|[+-]\d{2}:?\d{2})', value)
    require(match is not None)
    zone = match[3]
    zone = '+00:00' if zone == 'Z' else zone[:3] + ':' + zone[-2:]
    return datetime.datetime.fromisoformat(match[1] + 'T' + match[2] + zone)


def check_comparisons(snapshot, now):
    if any('stream' in snapshot.get(n, {}) for n in ('events-compare.jsonl', 'events-compare-index.json')):
        return _stream_comparisons(snapshot, now)
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
    operation, and remains unexpected. Telemetry requires its separate exact
    native append observation; no general product path is authorized here.
    """
    from . import events
    checkpoint = 'capture-inputs'
    try:
        require(before is not None and after is not None and isinstance(observations, list))
        checkpoint = 'history-preservation'
        require(set(before) <= set(after))
        known = {'events-leases', 'events.jsonl', 'events-index.json', 'events-compare.jsonl', 'events-compare-index.json'}
        known.update(('event-objects', 'hook-events.jsonl', 'hook-events-offset'))
        known.add('events-status-commits.json')
        known.add('status-archives')
        known.update(('timings', 'timings/events.jsonl'))
        require(all(n in known or re.fullmatch(r'events-\d{4}-\d{2}\.jsonl', n) for n in set(before) | set(after)))
        for name in set(after) - set(before):
            if name != 'status-archives':
                require(after[name]['mode'] == (stat.S_IFDIR | 0o700 if after[name].get('directory')
                                               else stat.S_IFREG | events.common.publication_mode(private=True)))
        for snapshot in (before, after):
            if 'events-leases' in snapshot:
                require(snapshot['events-leases'].get('directory') is True and
                        snapshot['events-leases'].get('entries') == [])
        for name in before:
            if name in ('timings', 'timings/events.jsonl'):
                continue  # exact append and private metadata checked against the native observation below
            if name == 'status-archives':
                continue
            if name == 'event-objects':
                require(before[name]['mode'] == after[name]['mode'])
                require(all(after[name]['entries'].get(k) == v for k, v in before[name]['entries'].items()))
                continue
            if name == 'events-leases':
                require(before[name] == after[name])
                continue
            if name not in ('events-index.json', 'events-compare-index.json', 'hook-events-offset',
                            'events-status-commits.json'):
                require(_prefix_preserved(before, after, name))
            if name == 'hook-events.jsonl':
                require(before[name] == after[name])
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
        checkpoint = 'telemetry'
        timing_paths = validate_timing(before, after, observations, owner_pid, command)
        scopes, observed_events, observed_comparisons = {}, [], []
        witnessed_objects, cursor_ends = set(), []
        stamp_bytes = blob(before, 'events-status-commits.json')
        stamp_present = 'events-status-commits.json' in before
        archives = {}
        for observation in observations:
            checkpoint = 'lifecycle.' + str(observation.get('type', 'unknown'))
            require(observation['pid'] == owner_pid)
            if observation['type'] == 'telemetry':
                continue
            key, kind = observation['scope'], observation['type']
            if kind == 'enter':
                require(key not in scopes and observation['command'] == command)
                anchor = check_owner(observation['owner'], owner_pid, command, observation['node'], unleased=True)
                scopes[key] = dict(phase='enter', before=observation['resources'], ref=None, terminal=None,
                                   views={}, node=observation['node'], lock=anchor, coverage=None, covered=False,
                                   writers=[], reconciliation=None, projection=None)
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
            if kind in ('projection-enter', 'projection-exit'):
                require(scope['phase'] == 'finishing')
                require(check_owner(observation['owner'], owner_pid, command, scope['node']) == scope['lock'])
                require(observation['owner']['ref'] == scope['ref'] and observation['owner']['lease'] is True)
                if kind == 'projection-enter':
                    require(scope['projection'] is None)
                    packet = observation['packet']
                    require(bytes.fromhex(packet['cache'] or '') == stamp_bytes)
                    require((packet['cache'] is not None) == stamp_present)
                    path = packet['path'].removesuffix('status.md') + 'status-archive.md'
                    old = packet['archive']
                    summary = (dict(sha256=digest(bytes.fromhex(old)), mode=packet['modes']['status-archive.md'])
                               if old is not None else None)
                    if path in archives:
                        require(summary == archives[path])
                    else:
                        actual = resource_map(scope['before'])
                        require(actual.get(digest(path.encode()), {}).get('hash') ==
                                (digest(bytes.fromhex(old)) if old is not None else None))
                        require(before.get('status-archives', {}).get(path) == summary)
                    scope['projection'] = (observation['method'], packet)
                else:
                    require(scope['projection'] is not None)
                    method, packet = scope['projection']
                    require(method == observation['method'])
                    archive, stamp_bytes = projection_result(events, method, packet, observation['packet'], now)
                    stamp_present = observation['packet']['cache'] is not None
                    archives[packet['path'].removesuffix('status.md') + 'status-archive.md'] = (
                        dict(sha256=digest(bytes.fromhex(archive)), mode=observation['packet']['modes']['status-archive.md'])
                        if archive is not None else None)
                    scope['projection'] = None
                continue
            if kind in ('reconcile-enter', 'reconcile-resources', 'reconcile-exit'):
                require(scope['phase'] in ('started', 'finishing'))
                require(check_owner(observation['owner'], owner_pid, command, scope['node']) == scope['lock'])
                require(observation['owner']['ref'] == scope['ref'] and observation['owner']['lease'] is True)
                if kind == 'reconcile-enter':
                    require(scope['reconciliation'] is None)
                    scope['reconciliation'] = {}
                elif kind == 'reconcile-resources':
                    require(scope['reconciliation'] == {})
                    resource_map(observation['resources'])
                    scope['reconciliation'] = dict(resources=observation['resources'])
                else:
                    require(scope['reconciliation'] is not None)
                    if scope['reconciliation']:
                        require(scope['reconciliation']['resources'] == observation['resources'])
                    scope['reconciliation'] = None
                continue
            if kind == 'hook-resources':
                require(scope['writers'] and scope['writers'][-1]['kind'] == 'hooks')
                writer = scope['writers'][-1]
                require('resources' not in writer)
                require(check_owner(observation['owner'], owner_pid, command, scope['node']) == scope['lock'])
                require(observation['owner']['ref'] == scope['ref'] and observation['owner']['lease'] is True)
                resource_map(observation['resources'])
                writer['resources'] = observation['resources']
                continue
            if kind in ('writer-enter', 'writer-exit'):
                require(scope['phase'] in ('started', 'finishing'))
                require(check_owner(observation['owner'], owner_pid, command, scope['node']) == scope['lock'])
                require(observation['owner']['ref'] == scope['ref'] and observation['owner']['lease'] is True)
                if kind == 'writer-enter':
                    writer = copy.deepcopy(observation['writer'])
                    writer['seen'] = []
                    if writer['kind'] == 'hooks':
                        require(writer['saved'] == (cursor_ends[-1] if cursor_ends else
                                int(blob(before, 'hook-events-offset') or b'0')))
                        writer['expected'] = hook_fold_rows(before, state, writer)
                    else:
                        require(writer['kind'] in ('record', 'apply'))
                        require(re.fullmatch('[0-9a-f]{64}', writer['object']) is not None)
                        obj = after.get('event-objects', {}).get('entries', {}).get(writer['object'] + '.json')
                        require(obj is not None and obj['record_type'] == writer['record_type']
                                and obj['key'] == writer['key'] and obj['text_sha256'] == writer['after'])
                        if writer['object'] + '.json' not in before.get('event-objects', {}).get('entries', {}):
                            require(obj['mode'] == stat.S_IFREG | 0o600)
                        require(writer['record_type'] in ('jobs', 'handback'))
                        require((writer['record_type'] == 'jobs' and writer['key'] == 'ledger') or
                                (writer['record_type'] == 'handback' and re.fullmatch('[0-9a-f]{24}', writer['key'])))
                        suffix = (r'job-ledger\.md' if writer['record_type'] == 'jobs' else
                                  'job-handbacks/' + writer['key'] + r'\.json')
                        require(re.fullmatch(r'(?:nodes/[^/]+/)*' + suffix, writer['path']) is not None)
                    scope['writers'].append(writer)
                else:
                    require(scope['writers'])
                    writer = scope['writers'].pop()
                    require(observation['writer'] == {k: v for k, v in writer.items()
                                                      if k not in ('seen', 'expected', 'resources')})
                    if writer['kind'] == 'hooks':
                        require(writer['seen'] == writer['expected'])
                        if writer['expected']:
                            require(observation['resources'] == writer['resources'])
                        if writer.get('outbox'):
                            require(observation['cursor'] == writer['end'])
                            cursor_ends.append(writer['end'])
                    elif writer['kind'] == 'record':
                        require(len(writer['seen']) <= 1)
                    else:
                        require(len(writer['seen']) == (0 if writer['applied'] else 1))
                continue
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
                    require(scope['reconciliation'] is not None and
                            scope['reconciliation'].get('resources') == observation['resources'])
                    require(scope['phase'] in ('started', 'finishing') and scope['ref'] in live)
                    require(not ((set(state['opened']) - {scope['ref']}) & set(live)))
                    require(event['ref'] not in live and event['data'].get('flagged') is True)
                    require(event['data'].get('clear_faults') is True)
                    reconciled = resource_map(event['data'].get('resources'))
                    if not reconciled and scope['reconciliation'].get('baseline'):
                        # Later records in this same batch only retire dead refs.
                        # They cannot establish a baseline or adopt changed bytes.
                        require(event['data'].get('views') == {} and event['ref'] in state['opened'])
                        require(set(state['resources']) == set(actual))
                        require(all((state['resources'][k]['hash'], state['resources'][k]['scope']) ==
                                    (row['hash'], row['scope']) for k, row in actual.items()))
                    else:
                        require(hashes(event['data'].get('resources')) == hashes(observation['resources']))
                        require(all(reconciled[k]['scope'] == row['scope'] for k, row in actual.items()))
                        require(set(reconciled) == set(actual) | set(state['resources']))
                        require(all(row['hash'] is None for k, row in reconciled.items() if k not in actual))
                        require(all(scope['views'].get(k) == v for k, v in event['data'].get('views', {}).items()))
                        scope['reconciliation']['baseline'] = True
                    # A reconciliation may not hide a new or foreign operation.
                    require(event['ref'] not in state['opened'] or event['node'] == state['opened'][event['ref']]['node'])
                elif tag in ('record', 'record_applied', 'hook'):
                    require(scope['phase'] in ('started', 'finishing') and scope['ref'] in live
                            and observation['owned_lease'] is True and scope['writers'])
                    writer = scope['writers'][-1]
                    if tag == 'hook':
                        require(writer['kind'] == 'hooks')
                        require(observation['resources'] == writer['resources'])
                        index = len(writer['seen'])
                        require(index < len(writer['expected']))
                        expected = writer['expected'][index]
                        require(event['node'] == expected['node'] and event['ref'] == expected['ref'])
                        fresh = []
                        for row in expected['resources']:
                            fresh.append(actual.get(row['id']) or
                                         dict(id=row['id'], scope=row.get('scope', '*'), hash=None))
                        require(event['data'] == dict(version=1, resources=fresh,
                                at=expected['at'], outbox=expected['outbox']))
                        writer['seen'].append(expected)
                    else:
                        require(writer['kind'] == ('record' if tag == 'record' else 'apply'))
                        require(event['node'] == writer['node'] and not writer['seen'])
                        if tag == 'record':
                            require(event['data'] == {k: writer[k] for k in
                                    ('record_type', 'key', 'object', 'before', 'after', 'origin')} | {'version': 1})
                            witnessed_objects.add(writer['object'] + '.json')
                        else:
                            identity = writer['node'] + ':' + writer['record_type'] + ':' + writer['key']
                            entry = state['records'].get(identity)
                            require(entry is not None and entry['ref'] == writer['ref'] and not entry.get('applied'))
                            require(all(entry[k] == writer[k] for k in ('object', 'before', 'after')))
                            rid = digest(writer['path'].encode())
                            require(actual.get(rid, {}).get('hash') == writer['after'])
                            require(actual[rid]['scope'] == writer['node'])
                            rows = event['data'].get('resources')
                            require(isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0].get('path'), str))
                            require({k: v for k, v in rows[0].items() if k != 'path'} == actual[rid])
                            require({k: v for k, v in event['data'].items() if k != 'resources'} ==
                                    dict(version=1, record_ref=writer['ref'], record_type=writer['record_type'], key=writer['key']))
                        writer['seen'].append(event['ref'])
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
                require(not scope['writers'])
                require(scope['reconciliation'] is None)
                require(scope['projection'] is None)
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
        require(blob(after, 'events-status-commits.json') == stamp_bytes)
        require(('events-status-commits.json' in after) == stamp_present)
        for path, archive in archives.items():
            require(final_state['resources'].get(digest(path.encode()), {}).get('hash') ==
                    (archive['sha256'] if archive is not None else None))
            require(after.get('status-archives', {}).get(path) == archive)
        old_archives, new_archives = before.get('status-archives', {}), after.get('status-archives', {})
        require(all(old_archives.get(path) == new_archives.get(path) or path in archives
                    for path in set(old_archives) | set(new_archives)))
        old_objects = before.get('event-objects', {}).get('entries', {})
        new_objects = after.get('event-objects', {}).get('entries', {})
        require(set(new_objects) - set(old_objects) <= witnessed_objects)
        if cursor_ends:
            require(blob(after, 'hook-events-offset') == str(cursor_ends[-1]).encode())
        else:
            require(blob(before, 'hook-events-offset') == blob(after, 'hook-events-offset'))
        def comparable(snapshot, name):
            row = dict(snapshot.get(name, {}))
            if 'stream' in row:
                value = row.pop('stream')
                row.update(content_sha256=value['sha256'], size=value['size'])
            return row
        changed = {name for name in set(before) | set(after)
                   if comparable(before, name) != comparable(after, name)}
        if 'timings' in before and 'timings' in after:
            changed.discard('timings')  # Existing directory grants no write authority for entries.
        require(changed & {'timings', 'timings/events.jsonl'} == timing_paths)
        # The derived cache alone never constitutes an owned operation.
        require(added)
        if diagnostics is not None:
            diagnostics.update(checkpoint='complete', exception_category=None)
        proven = {'brain/.brain/' + name for name in changed if name not in ('event-objects', 'status-archives')}
        proven.update('brain/' + path for path in archives)
        proven.update('brain/.brain/event-objects/' + name for name in set(new_objects) - set(old_objects))
        if 'event-objects' not in before and 'event-objects' in after:
            proven.add('brain/.brain/event-objects')
        return proven, 'proven'
    except (EvidenceError, OSError, ValueError, TypeError, KeyError, AttributeError, IndexError) as error:
        if diagnostics is not None:
            diagnostics.update(checkpoint=checkpoint, exception_category=type(error).__name__,
                               rejection_stack=[dict(function=f.name, line=f.lineno)
                                                for f in traceback.extract_tb(error.__traceback__)])
        return set(), 'unproven'


def install_observer(events, root, trace, read=False):
    """Observe native lifecycle calls; keep receipts outside product storage."""
    stack = []
    active_ops = []
    sequence = [0]
    hook_operands = []
    reconcile_operands = []
    lock_sequence, lock_anchor = [0], [None]
    native_instance = events.instance
    timing_active = [False]

    def write_observation(record):
        with events.common.directory_target(Path(trace), private=True) as target:
            with os.fdopen(target.open(os.O_WRONLY | os.O_APPEND | os.O_CREAT), 'a') as stream:
                stream.write(json.dumps(record, sort_keys=True) + '\n')

    # Observe the exact operand at the native timing producer's append call.
    # This runs after the operational transaction, so it has no journal scope.
    timing_module = events.__package__ + '.timings'
    try:
        timings = importlib.import_module(timing_module)
    except ModuleNotFoundError as error:
        if error.name != timing_module:
            raise
        timings = None  # Older cores have no telemetry producer to observe.
    original_publish = timings.publish if timings is not None else None
    original_line = events.append_line

    def timing_publish(*args, **kwargs):
        previous = timing_active[0]
        timing_active[0] = True
        try:
            return original_publish(*args, **kwargs)
        finally:
            timing_active[0] = previous

    def timing_line(path, value):
        owned = timing_active[0] and Path(path) == Path(root) / '.brain/timings/events.jsonl'
        before = None
        if owned:
            try:
                before = capture_timing(root)
                record = copy.deepcopy(value)
            except (OSError, ValueError):
                pass
        result = original_line(path, value)
        if owned and before is not None:
            try:
                write_observation(dict(type='telemetry', pid=os.getpid(), before=before,
                                       after=capture_timing(root), record=record))
            except (OSError, ValueError):
                pass  # unavailable evidence grants nothing; preserve the native outcome
        return result

    if timings is not None:
        timings.publish, events.append_line = timing_publish, timing_line
    # Optional UI storage is observed at its native lock and atomic producers.
    # Unknown files and writes outside those producers receive no evidence.
    ui_name = events.__package__ + '.ui_json'
    try:
        ui_json = importlib.import_module(ui_name) if read else None
    except ModuleNotFoundError as error:
        if error.name != ui_name:
            raise
        ui_json = None
    if ui_json is not None:
        original_lock = ui_json.ui_json_fill_lock
        original_write = ui_json._m_ui_readmodel.ReadModel.write
        original_atomic = events.common.atomic_write
        ui_active = []
        def ui_capture():
            return capture_private(root, 'ui-cache', UI_CACHE_FILES)
        def ui_record(before, name, payload):
            write_observation(dict(type='ui-cache', pid=os.getpid(), name=name, hex=payload.hex(),
                                   before=before, after=ui_capture()))
        @contextlib.contextmanager
        def ui_lock(inst, blocking):
            before = ui_capture() if inst.root == Path(root) else None
            with original_lock(inst, blocking) as acquired:
                if acquired and before is not None:
                    ui_record(before, 'json-fill.lock', b'')
                yield acquired
        def ui_write(model, name, value):
            ui_active.append(model.folder / (name + '.json') if model.inst.root == Path(root)
                             and name in ('json-snapshot', 'json-fill-status') else None)
            try:
                return original_write(model, name, value)
            finally:
                ui_active.pop()
        @functools.wraps(original_atomic)
        def ui_atomic(path, text, *args, **kwargs):
            owned = bool(ui_active) and ui_active[-1] == Path(path)
            before = ui_capture() if owned else None
            result = original_atomic(path, text, *args, **kwargs)
            if before is not None:
                ui_record(before, Path(path).name, text.encode())
            return result
        ui_json.ui_json_fill_lock = ui_lock
        ui_json._m_ui_readmodel.ReadModel.write = ui_write
        events.common.atomic_write = ui_atomic
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
            write_observation(dict(type=kind, scope=stack[-1], pid=os.getpid(),
                                   monotonic_ns=time.monotonic_ns(), **data))
    def observe(kind, inst=None, **data):
        try:
            if inst is not None:
                reused = (hook_operands[-1] if hook_operands and data.get('record', {}).get('kind') == 'hook'
                          else None)
                if reconcile_operands and data.get('record', {}).get('kind') == 'reconcile':
                    if reconcile_operands[-1] is None:
                        reconcile_operands[-1] = resources(inst)
                        emit('reconcile-resources', resources=reconcile_operands[-1], owner=owner())
                    reused = reconcile_operands[-1]
                data.update(resources=copy.deepcopy(reused) if reused is not None else resources(inst),
                            live=live(inst), owned_lease=owned_lease())
            emit(kind, **data)
        except Exception:
            # Missing/invalid evidence fails closed in the supervisor. Preserve
            # the native call's own result and exception, including partial writes.
            try:
                emit('invalid')
            except Exception:
                pass
    def writer_emit(kind, **data):
        try:
            emit(kind, owner=owner(), **data)
        except Exception:
            observe('invalid')
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
    def record_writer(original, applying=False):
        def call(inst, n, *args, **kwargs):
            if not stack or inst.root.resolve() != Path(root).resolve():
                return original(inst, n, *args, **kwargs)
            try:
                if applying:
                    entry = args[0]
                    packet = {k: entry[k] for k in ('record_type', 'key', 'object', 'before', 'after', 'ref')}
                    packet.update(kind='apply', applied=bool(entry.get('applied')))
                else:
                    record_type, key, text = args[:3]
                    path = events.record_target(n, record_type, key)
                    raw = (json.dumps(dict(version=1, record_type=record_type, key=key, text=text),
                                      ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()
                    packet = dict(kind='record', record_type=record_type, key=key, object=digest(raw),
                                  before=digest(events.private_read(path)) if events._stamp(path) else None,
                                  after=digest(text.encode()), origin=kwargs.get('origin', args[3] if len(args) > 3 else 'observed'))
                packet.update(node=events.node_ref(n['id']),
                              path=events.record_target(n, packet['record_type'], packet['key']).relative_to(inst.root).as_posix())
            except Exception:
                observe('invalid')
                return original(inst, n, *args, **kwargs)
            writer_emit('writer-enter', writer=packet)
            result = original(inst, n, *args, **kwargs)
            writer_emit('writer-exit', writer=packet)
            return result
        return functools.wraps(original)(call)
    events.write_record = record_writer(events.write_record)
    events.apply_record = record_writer(events.apply_record, applying=True)
    original_fold = events.fold_hooks
    def fold_hooks(inst):
        if not stack or inst.root.resolve() != Path(root).resolve():
            return original_fold(inst)
        try:
            path = inst.root / '.brain/hook-events.jsonl'
            cursor = inst.root / '.brain/hook-events-offset'
            stamp = events._stamp(path)
            packet = dict(kind='hooks', outbox=stamp is not None,
                          saved=int(events.private_read(cursor)) if events._stamp(cursor) else 0,
                          end=stamp[2] if stamp else 0)
        except Exception:
            observe('invalid')
            return original_fold(inst)
        writer_emit('writer-enter', writer=packet)
        original_resources = events.resource_list
        hook_operands.append(None)
        def consumed(instance, values, *args, **kwargs):
            result = original_resources(instance, values, *args, **kwargs)
            try:
                if instance.root == inst.root:
                    require(hook_operands[-1] is None and kwargs.get('display') is False)
                    hook_operands[-1] = copy.deepcopy(result)
                    writer_emit('hook-resources', resources=result)
            except Exception:
                observe('invalid')
            return result
        events.resource_list = consumed
        try:
            result = original_fold(inst)
        finally:
            events.resource_list = original_resources
            hook_operands.pop()
        try:
            writer_emit('writer-exit', writer=packet, cursor=int(events.private_read(cursor)) if events._stamp(cursor) else 0,
                        resources=resources(inst))
        except Exception:
            observe('invalid')
        return result
    events.fold_hooks = fold_hooks
    original_reconcile = events.reconcile
    def reconcile(inst, *args, **kwargs):
        if not stack or inst.root.resolve() != Path(root).resolve():
            return original_reconcile(inst, *args, **kwargs)
        writer_emit('reconcile-enter')
        reconcile_operands.append(None)
        try:
            result = original_reconcile(inst, *args, **kwargs)
        finally:
            reconcile_operands.pop()
        try:
            writer_emit('reconcile-exit', resources=resources(inst))
        except Exception:
            observe('invalid')
        return result
    events.reconcile = reconcile
    def projection(original, method):
        def call(inst, n, *args, **kwargs):
            if not stack or inst.root.resolve() != Path(root).resolve():
                return original(inst, n, *args, **kwargs)
            try:
                before = projection_packet(events, inst, n)
                before['force'] = bool(kwargs.get('force', args[0] if args else False))
                writer_emit('projection-enter', method=method, packet=before)
            except Exception:
                observe('invalid')
            result = original(inst, n, *args, **kwargs)
            try:
                writer_emit('projection-exit', method=method, packet=projection_packet(events, inst, n))
            except Exception:
                observe('invalid')
            return result
        return functools.wraps(original)(call)
    events.cap_now = projection(events.cap_now, 'cap-now')
    events.status_commit = projection(events.status_commit, 'status-commit')
    original_comparison = events.comparison
    def comparison(inst, node, current=None, record=True, prepared=None, evidence=None):
        # Observe the native call's view without moving its computation or writes.
        captured = []
        previous_view = events.snapshot_view
        def observe_view(*args, **kwargs):
            result = previous_view(*args, **kwargs)
            captured.append(result)
            return result
        events.snapshot_view = observe_view
        try:
            options = {'prepared': prepared} if prepared is not None else {}
            if evidence is not None:
                options['evidence'] = evidence
            row = original_comparison(inst, node, current=current, record=record, **options)
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
    def detect_gaps(inst, preferred, record=True, actual=None):
        # Newer producers reuse a locked resource snapshot. Preserve that exact
        # operand; older installed cores still use the original signature.
        options = {'actual': actual} if actual is not None else {}
        if not record:
            return original_detect(inst, preferred, record=False, **options)
        if not stack or inst.root.resolve() != Path(root).resolve():
            return original_detect(inst, preferred, **options)
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
            result = original_detect(inst, preferred, **options)
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


@contextlib.contextmanager
def _operand(snapshot, name):
    import io
    row = snapshot.get(name, {})
    value = row.get('stream')
    if value is None:
        with io.BytesIO(blob(snapshot, name)) as stream:
            yield stream
        return
    path = Path(value['path'])
    require(not any(p.is_symlink() for p in (path, *path.parents)))
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        require(stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid() and st.st_nlink == 1
                and stat.S_IMODE(st.st_mode) == 0o600
                and (st.st_dev, st.st_ino, st.st_size) == (value['dev'], value['ino'], value['size']))
        h = hashlib.sha256()
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            h.update(chunk)
        require(h.hexdigest() == value['sha256'])
        os.lseek(fd, 0, 0)
        with os.fdopen(os.dup(fd), 'rb') as stream:
            yield stream
        current = os.fstat(fd)
        attached = path.lstat()
        require((current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns,
                 current.st_ctime_ns) == (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns))
        require((attached.st_dev, attached.st_ino) == (st.st_dev, st.st_ino))
    finally:
        os.close(fd)


def _prefix_preserved(before, after, name):
    with _operand(before, name) as old, _operand(after, name) as new:
        while True:
            chunk = old.read(65536)
            if not chunk:
                return True
            if new.read(len(chunk)) != chunk:
                return False


def _comparison_lines(snapshot):
    with _operand(snapshot, 'events-compare.jsonl') as stream:
        while True:
            line = stream.readline(CAPTURE_BYTES + 1)
            if not line:
                break
            require(len(line) <= CAPTURE_BYTES and line.endswith(b'\n'))
            yield line, json_value(line)


def _valid_comparison(row, identities):
    require(isinstance(row, dict))
    coverage = row.get('kind') == 'coverage'
    require(set(row) == ({'id', 'ts', 'node', 'kind', 'equal', 'resources', 'view_equal',
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


class _ComparisonRows:
    def __init__(self, snapshot, count):
        self.snapshot, self.count = snapshot, count

    def __len__(self):
        return self.count

    def __iter__(self):
        return (row for _, row in _comparison_lines(self.snapshot))

    def __getitem__(self, selected):
        require(isinstance(selected, slice) and selected.step is None)
        start, stop, _ = selected.indices(self.count)
        if start == 0:
            return _ComparisonPrefix(self, stop)
        rows = []
        for index, row in enumerate(self):
            if start <= index < stop:
                require(len(rows) < 10000)
                rows.append(row)
        require(len(rows) <= 10000)  # command delta is bounded; never materialize whole history
        return rows

    def __eq__(self, other):
        if len(self) != len(other):
            return False
        return all(a == b for a, b in zip(self, other))


class _ComparisonPrefix:
    def __init__(self, rows, count):
        self.rows, self.count = rows, count

    def __len__(self):
        return self.count

    def __iter__(self):
        import itertools
        return itertools.islice(iter(self.rows), self.count)

    def __eq__(self, other):
        import itertools
        return len(other) == self.count and all(a == b for a, b in
            zip(itertools.islice(iter(self.rows), self.count), iter(other)))


def _stream_comparisons(snapshot, now):
    """Full content/schema/prefix/index proof with bounded read buffers."""
    identities, count = set(), 0
    for _, row in _comparison_lines(snapshot):
        _valid_comparison(row, identities)
        count += 1
    if 'events-compare-index.json' not in snapshot:
        return _ComparisonRows(snapshot, count)
    # Large native caches are canonical JSON. Verify every byte against an
    # independent streamed reconstruction; unknown encodings remain refused.
    with _operand(snapshot, 'events-compare-index.json') as stream:
        header = stream.read(CAPTURE_BYTES)
    marker = b'"rows":['
    split = header.find(marker)
    require(split >= 0)
    saved = json_value(header[:split] + b'"rows":[],"version":1}')
    require(set(saved) == {'version', 'identity', 'offset', 'rows', 'first', 'mtime', 'anchor'})
    offset, mtime = saved['offset'], saved['mtime']
    require(type(offset) is int and offset >= 0 and type(mtime) is int and mtime >= 0)
    source = snapshot['events-compare.jsonl']
    require(mtime <= source['mtime'])
    first, position, anchor = {}, 0, b''
    parent = Path(snapshot['events-compare-index.json'].get('stream',
                  source.get('stream', {})).get('path', tempfile.gettempdir())).parent
    with tempfile.TemporaryFile(dir=parent) as recent:
        separator = b''
        for line, row in _comparison_lines(snapshot):
            if position == offset:
                break
            require(position + len(line) <= offset)
            position += len(line)
            anchor = (anchor + line)[-256:]
            if row['kind'] == 'comparison':
                node = row['node']
                key = node if re.fullmatch('n:[0-9a-f]{64}', node) else 'n:' + digest(node.encode())
                if key not in first or timestamp(row['ts']) < timestamp(first[key]['ts']):
                    first[key] = row
            if (now - timestamp(row['ts'])).total_seconds() <= 86400:
                recent.write(separator + json.dumps(row, separators=(',', ':'), sort_keys=True).encode())
                separator = b','
        require(position == offset)
        with _operand(snapshot, 'events-compare.jsonl') as stream:
            stream.seek(0, 2)
            length = stream.tell()
        require(offset <= length and (offset < length or mtime == source['mtime']))
        expected = dict(anchor=digest(anchor), first=first, identity=[source['dev'], source['ino']],
                        mtime=mtime, offset=offset)
        require(all(saved[k] == v for k, v in expected.items()))
        h = hashlib.sha256()
        h.update(json.dumps(expected, separators=(',', ':'), sort_keys=True).encode()[:-1] + b',"rows":[')
        recent.seek(0)
        for chunk in iter(lambda: recent.read(65536), b''):
            h.update(chunk)
        h.update(b'],"version":1}')
        cache = snapshot['events-compare-index.json']
        actual = cache['stream']['sha256'] if 'stream' in cache else digest(blob(snapshot, 'events-compare-index.json'))
        require(h.hexdigest() == actual)
    return _ComparisonRows(snapshot, count)


def _journal_lines(snapshot, name):
    with _operand(snapshot, name) as stream:
        while True:
            line = stream.readline(CAPTURE_BYTES + 1)
            if not line:
                return
            require(len(line) <= CAPTURE_BYTES and line.endswith(b'\n'))
            yield line


def hook_fold_rows(snapshot, state, writer):
    """Derive acknowledgements from the captured outbox, not from journal claims."""
    row = snapshot.get('hook-events.jsonl')
    require(writer['outbox'] == (row is not None))
    if row is None:
        require(writer['end'] == 0)
        return []
    payload = blob(snapshot, 'hook-events.jsonl')
    require(writer['end'] == len(payload) and 0 <= writer['saved'] <= len(payload))
    token = str(row['dev']) + ':' + str(row['ino'])
    offset = max(writer['saved'], state['hook_offsets'].get(token, 0))
    require(offset <= len(payload) and (offset == 0 or payload[offset - 1:offset] == b'\n'))
    result = []
    for line in payload[offset:].splitlines(keepends=True):
        offset += len(line)
        require(line.endswith(b'\n'))
        data = json_value(line)
        require(isinstance(data, dict) and all(isinstance(data.get(k), str) for k in ('node', 'ref', 'ts'))
                and isinstance(data.get('data'), dict))
        from . import events
        events._payload(data['data'])
        if offset <= state['hook_offsets'].get(token, 0) or data['ref'] in state['legacy_hooks']:
            continue
        result.append(dict(node=data['node'], ref=data['ref'], at=data['ts'],
                           resources=data['data'].get('resources', []), outbox=dict(file=token, end=offset)))
    return result


def projection_packet(events, inst, node):
    """Bounded private operands at the native projection call, including Git bases."""
    modes = {}
    def read(path):
        require(not any(p.is_symlink() for p in (path, *path.parents)))
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        try:
            st = os.fstat(fd)
            require(stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and st.st_size <= CAPTURE_BYTES)
            with os.fdopen(os.dup(fd), 'rb') as stream:
                value = stream.read(CAPTURE_BYTES + 1)
            current, attached = os.fstat(fd), path.lstat()
            def stable(s):
                return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns
            require(len(value) == st.st_size and stable(current) == stable(st) == stable(attached))
            modes[path.name] = st.st_mode
            return value.hex()
        finally:
            os.close(fd)
    path = node['dir'] / 'status.md'
    require(not any(p.is_symlink() for p in (path, *path.parents)))
    base = events._status_bytes(inst, node)
    return dict(node=events.node_ref(node['id']), node_id=node['id'],
                path=path.relative_to(inst.root).as_posix(), status=read(path),
                archive=read(node['dir'] / 'status-archive.md'),
                cache=read(inst.root / '.brain/events-status-commits.json'),
                base_code=base[0], base_status=base[1].hex(), base_archive=base[3].hex(), at=inst.now(), modes=modes)


def projection_result(events, method, before, after, now):
    """Independently derive the exact archive and ownership-stamp publication."""
    require(method in ('cap-now', 'status-commit'))
    require(re.fullmatch(r'(?:nodes/[^/]+/)*status\.md', before['path']) is not None)
    require(before['node'] == events.node_ref(before['node_id']))
    require(0 <= (now - timestamp(before['at'])).total_seconds() < 120)
    require(all(after[k] == before[k] for k in ('node', 'node_id', 'path', 'at')))
    raw_cache = bytes.fromhex(before['cache'] or '')
    saved = json_value(raw_cache) if raw_cache else {}
    require(isinstance(saved, dict))
    stamp = saved.get(before['node'], saved.get(before['node_id'], {}))
    if isinstance(stamp, str):
        stamp = {'at': stamp}
    require(isinstance(stamp, dict))
    old, archive = bytes.fromhex(before['status'] or ''), bytes.fromhex(before['archive'] or '')
    base, base_archive = bytes.fromhex(before['base_status']), bytes.fromhex(before['base_archive'])
    owned = (not before['base_code'] and before['status'] is not None
             and stamp.get('base_status') == digest(base)
             and stamp.get('owned_judgement') == digest(events.judgement(old.decode()).encode())
             and stamp.get('base_archive') == digest(base_archive) and stamp.get('owned_archive') == digest(archive))
    changed_cache, archive_hex = False, before['archive']
    if method == 'cap-now':
        text = old.decode()
        match = re.search(r'(?ms)^## Now[^\r\n]*\r?\n(.*?)(?=^## |\Z)', text)
        expected = text
        if before['status'] is not None and match:
            lines = match.group(1).splitlines(keepends=True)
            visible = re.sub(r'<!--.*?-->', lambda m: '\n' * m.group().count('\n'), match.group(1),
                             flags=re.S).splitlines()
            count, cut = 0, len(lines)
            for index, line in enumerate(visible):
                if line.strip():
                    count += 1
                    if count > 30:
                        cut = index
                        break
            if cut < len(lines):
                expected = text[:match.start(1)] + ''.join(lines[:cut]) + '\n' + text[match.end(1):]
                archive = (archive.decode() + '\n## ' + before['at'] + ': Now overflow\n\n' +
                           ''.join(lines[cut:]) + '\n').encode()
                archive_hex = archive.hex()
                ownership = (not before['base_code'] and
                             (owned or (events.judgement(base.decode()) == events.judgement(text)
                                        and base_archive == bytes.fromhex(before['archive'] or ''))))
                if ownership:
                    saved[before['node']] = {**stamp, 'base_status': digest(base), 'base_archive': digest(base_archive),
                        'owned_judgement': digest(events.judgement(expected).encode()), 'owned_archive': digest(archive)}
                    changed_cache = True
        require(after['status'] == (expected.encode().hex() if before['status'] is not None else None))
    else:
        require(after['status'] == before['status'])
        dirty = (bool(before['base_code']) or before['status'] is None or
                 (events.judgement(base.decode()) != events.judgement(old.decode()) and not owned))
        prior = timestamp(stamp['at']) if stamp.get('at') else None
        if not dirty and (before['force'] or not prior or (now - prior).total_seconds() >= 3600):
            saved[before['node']] = {'at': before['at']}
            changed_cache = True
    require(after['archive'] == archive_hex)
    if archive_hex is not None:
        require(after['modes']['status-archive.md'] == before['modes'].get(
            'status-archive.md', stat.S_IFREG | events.common.publication_mode()))
    expected_cache = json.dumps(saved, sort_keys=True).encode() if changed_cache else raw_cache
    require(bytes.fromhex(after['cache'] or '') == expected_cache)
    return archive_hex, expected_cache



def capture_archives(root):
    result = {}
    def visit(folder):
        require(not folder.is_symlink())
        path = folder / 'status-archive.md'
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            fd = None
        if fd is not None:
            try:
                st = os.fstat(fd)
                require(stat.S_ISREG(st.st_mode) and st.st_nlink == 1)
                h = hashlib.sha256()
                for chunk in iter(lambda: os.read(fd, 65536), b''):
                    h.update(chunk)
                current, attached = os.fstat(fd), path.lstat()
                def stable(s):
                    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_mode
                require(stable(st) == stable(current) == stable(attached))
                result[path.relative_to(root).as_posix()] = dict(sha256=h.hexdigest(), mode=st.st_mode)
            finally:
                os.close(fd)
        children = folder / 'nodes'
        if children.exists():
            require(not children.is_symlink())
            for child in children.iterdir():
                if child.is_dir() and (child / 'node.md').is_file():
                    visit(child)
    visit(root)
    return result
