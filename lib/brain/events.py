"""Private, metadata-only write journal and status views (Python 3.9/stdlib)."""
import hashlib
import json
import os
import re
import uuid
import fcntl
import stat
import subprocess
import contextlib
from pathlib import Path

from . import common, guard, instance, nodes

VERSION = 1
# No command arguments, messages, note bodies, item bodies or schedule briefs enter the journal.
STATE_DIRS = {"nodes", "library", "specialists", "job-handbacks", "job-contracts", "job-attempts", "handback-queue"}
_CURRENT = []


def digest(value):
    return hashlib.sha256(value).hexdigest()


def node_ref(nid):
    """Opaque structural identity, separate from every scrubbed display field."""
    return nid if re.fullmatch(r'n:[0-9a-f]{64}', nid) else 'n:' + digest(nid.encode())


def same_node(value, nid):
    return value in (nid, node_ref(nid))  # old journals remain readable, without rewriting history


def screen(inst, value):
    """Scrub display leaves only; schema keys, identities, clocks and hashes are immutable."""
    terms = [t for n in inst.nodes() for t in guard.never_terms(n)]
    text_fields = {'facts', 'active', 'path', 'node_display'}
    def visit(v, text=False):
        if isinstance(v, str):
            return nodes.import_text(v, terms, metadata=True) if text else v
        if isinstance(v, list):
            return [visit(x, text) for x in v]
        if isinstance(v, dict):
            return {k: visit(x, text or k in text_fields) for k, x in v.items()}
        return v
    return visit(value)


def private_path(path):
    # Compatibility path preparation only; filesystem consumers hold authority.
    with common.directory_target(path, create=True, private=True) as target:
        return target.path


@contextlib.contextmanager
def private_open(path, mode='rb'):
    with common.directory_target(path, private=True) as target:
        with os.fdopen(target.open(), mode) as stream:
            yield stream
            target.check_file(stream.fileno())


def private_read(path):
    with common.directory_target(path, private=True) as target:
        return target.read_bytes()


def append_line(path, value):
    """One O_APPEND write, fsync'd; directory-relative and without following links."""
    line = (json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode()
    with common.directory_target(path, create=True, private=True) as target:
        fd = target.open(os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        try:
            target.check_file(fd)
            if os.write(fd, line) != len(line):
                raise OSError("short event append; reconciliation required")
            os.fsync(fd)
            os.fsync(target.fd)
        finally:
            os.close(fd)


class JournalError(ValueError):
    pass


_KINDS = {'begin', 'commit', 'abort', 'hook', 'reconcile', 'explain', 'damage', 'fact', 'record', 'record_applied'}
_JOURNALS = {}


def _payload(data):
    if not isinstance(data, dict):
        raise ValueError()
    for key in ('resources', 'baseline'):
        rows = data.get(key, [])
        if not isinstance(rows, list):
            raise ValueError()
        for row in rows:
            if (not isinstance(row, dict) or 'hash' not in row or not re.fullmatch(r'[0-9a-f]{64}', row.get('id', ''))
                    or (row.get('hash') is not None and not re.fullmatch(r'[0-9a-f]{64}', row['hash']))
                    or not isinstance(row.get('scope', '*'), str)):
                raise ValueError()
    views = data.get('views', {})
    if not isinstance(views, dict):
        raise ValueError()
    for key, view in views.items():
        if (not isinstance(key, str) or not isinstance(view, dict)
                or not isinstance(view.get('facts'), list) or not all(isinstance(x, str) for x in view['facts'])
                or not isinstance(view.get('active'), list)
                or any(not isinstance(row, list) or len(row) != 4 or not all(isinstance(x, str) for x in row)
                       for row in view['active'])):
            raise ValueError()
        for name in ('active_keys', 'merged', 'merged_before'):
            if not isinstance(view.get(name, []), list) or not all(isinstance(x, str) for x in view.get(name, [])):
                raise ValueError()
        if 'comparison' in view:
            _payload({'views': {key: view['comparison']}})
    if not isinstance(data.get('invalidate', []), list) or not all(isinstance(x, str) for x in data.get('invalidate', [])):
        raise ValueError()
    packet = data.get('outbox')
    if packet is not None and (not isinstance(packet, dict) or not isinstance(packet.get('file'), str)
                               or type(packet.get('end')) is not int or packet['end'] < 0):
        raise ValueError()


def _event(line):
    try:
        if not line.endswith(b'\n'):
            raise ValueError()
        value = json.loads(line)
        if (not isinstance(value, dict) or not isinstance(value.get('data'), dict)
                or any(not isinstance(value.get(k), str) for k in ('ts', 'node', 'kind', 'actor', 'ref'))
                or value['kind'] not in _KINDS):
            raise ValueError()
        if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2} .*', value['ts']):
            raise ValueError()
        _payload(value['data'])
        if value['kind'] in ('record', 'record_applied'):
            if (not re.fullmatch('n:[0-9a-f]{64}', value['node']) or
                    not re.fullmatch('[0-9a-f]{32}', value['ref'])):
                raise ValueError()
            validate_record(value['data'], applied=value['kind'] == 'record_applied')
        if value['kind'] == 'fact':
            validate_fact(value['data'])
        if value['kind'] == 'explain' and not isinstance(value['data'].get('comparison'), str):
            raise ValueError()
        return value
    except (ValueError, TypeError, UnicodeError):
        raise JournalError('damaged event record; a locked refresh can reconcile it') from None


def _paths(inst):
    with common.directory_target(inst.root / '.brain/events.jsonl', private=True) as target:
        names = os.listdir(target.fd)
        target.check()
        folder = target.path.parent
        return ([folder / name for name in sorted(names) if re.fullmatch(r'events-\d{4}-\d{2}\.jsonl', name)]
                + ([folder / 'events.jsonl'] if 'events.jsonl' in names else []))


def read(inst):
    """Explicit audit read. Historical structure/identities are never re-screened."""
    records = []
    for path in _paths(inst):
        with private_open(path) as stream:
            records.extend(_event(line) for line in stream)
    return records


def _blank():
    return {'resources': {}, 'views': {}, 'opened': {}, 'faults': {}, 'invalid': {},
            'hook_offsets': {}, 'legacy_hooks': {}, 'explained': {}, 'resolved': {}, 'records': {}, 'pending_landings': {}}


def _apply(state, e):
    data, kind = e['data'], e['kind']
    if kind == 'fact':
        if data['fact_type'] in ('landed', 'task_landed', 'job_landed'):
            state['pending_landings'][e['ref']] = e['node']
        elif data['fact_type'] == 'dependencies_reconciled':
            state['pending_landings'].pop(data.get('evidence', {}).get('ref'), None)
    if kind == 'record':
        identity = e['node'] + ':' + data['record_type'] + ':' + data['key']
        state['records'][identity] = {**data, 'ref': e['ref'], 'applied': False}
    if kind == 'record_applied':
        identity = e['node'] + ':' + data['record_type'] + ':' + data['key']
        entry = state['records'].get(identity)
        if entry and entry['ref'] == data['record_ref']:
            entry['applied'] = True
    if kind == 'begin':
        state['opened'][e['ref']] = e
        for r in data.get('baseline', []):
            state['resources'][r['id']] = r
    if kind in ('commit', 'reconcile', 'hook', 'record_applied'):
        changed = False
        for r in data.get('resources', []):
            changed |= state['resources'].get(r['id'], {}).get('hash') != r['hash']
            if r['hash'] is None:
                state['resources'].pop(r['id'], None)
            else:
                state['resources'][r['id']] = r
        if kind == 'hook' and changed:
            state['views'].pop(e['node'], None)
            state['invalid'][e['node']] = True
        for key, view in data.get('views', {}).items():
            state['views'][key] = view
            state['invalid'].pop(key, None)
        for key in data.get('invalidate', []):
            state['views'].pop(key, None)
            state['invalid'][key] = True
    if kind in ('commit', 'abort', 'reconcile'):
        state['opened'].pop(e['ref'], None)
    if kind == 'damage':
        state['faults'][e['ref']] = data.get('reason', 'journal_damage')
    if kind == 'reconcile' and data.get('clear_faults'):
        state['resolved'].update({ref: True for ref in state['faults']})
        state['faults'].clear()
    if kind == 'explain':
        state['explained'][data['comparison']] = e['ts']
    packet = data.get('outbox')
    if packet:
        state['hook_offsets'][packet['file']] = max(packet['end'], state['hook_offsets'].get(packet['file'], 0))
    elif kind == 'hook':
        state['legacy_hooks'][e['ref']] = True


def _stream_anchor(stream, offset):
    stream.seek(max(0, offset - 256))
    return digest(stream.read(min(offset, 256)))


def _anchor(path, offset):
    with private_open(path) as stream:
        return _stream_anchor(stream, offset)


def _stamp(path):
    try:
        with common.directory_target(path, private=True) as target:
            st = target.stat()
            return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns) if st else None
    except FileNotFoundError:
        return None


def quarantine(inst, name, payload):
    """Retain damaged bytes privately; never echo or embed them in journal records."""
    path = inst.root / '.brain' / (name + '.rejected.' + uuid.uuid4().hex)
    with common.directory_target(path, create=True, private=True) as target:
        with os.fdopen(target.open(os.O_WRONLY | os.O_CREAT | os.O_EXCL), 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
            target.check_file(stream.fileno())
        os.fsync(target.fd)
        return target.path


class Journal:
    """Validated materialized replay index; normal commands seek only new journal bytes."""
    def __init__(self, inst):
        self.inst = inst
        self.path = inst.root / '.brain/events-index.json'
        self.index_stamp = None
        self.positions = {}
        self.state = _blank()
        self.loaded = False
        self.dirty = False

    def load_index(self):
        try:
            with common.directory_target(self.path, private=True) as target:
                st = target.stat()
                stamp = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns) if st else None
                if self.loaded and stamp == self.index_stamp:
                    return
                self.loaded = True
                self.index_stamp = stamp
                data = json.loads(target.read_bytes())
            if (data['version'] != 2 or set(data['state']) != set(_blank())
                    or not all(isinstance(x, dict) for x in data['state'].values())
                    or not isinstance(data['positions'], dict)):
                raise ValueError()
            for pos in data['positions'].values():
                if (not isinstance(pos, dict) or type(pos.get('offset')) is not int or pos['offset'] < 0
                        or type(pos.get('mtime')) is not int or not isinstance(pos.get('anchor'), str)):
                    raise ValueError()
            _payload({'resources': list(data['state']['resources'].values()), 'views': data['state']['views']})
            if any(type(x) is not int or x < 0 for x in data['state']['hook_offsets'].values()):
                raise ValueError()
            for identity, entry in data['state']['records'].items():
                if (not isinstance(identity, str) or not re.fullmatch(r'n:[0-9a-f]{64}:(?:jobs:ledger|handback:[0-9a-f]{24})', identity)
                        or not isinstance(entry, dict) or type(entry.get('applied')) is not bool
                        or not re.fullmatch('[0-9a-f]{32}', entry.get('ref', ''))):
                    raise ValueError()
                validate_record(entry)
            for ref, owner in data['state']['pending_landings'].items():
                if not re.fullmatch('[0-9a-f]{32}', ref) or not re.fullmatch('n:[0-9a-f]{64}', owner):
                    raise ValueError()
            for e in data['state']['opened'].values():
                _event((json.dumps(e) + '\n').encode())
            self.positions, self.state = data['positions'], data['state']
        except (FileNotFoundError, ValueError, KeyError, TypeError):
            self.loaded = True
            self.index_stamp = None
            self.positions, self.state = {}, _blank()
        self.dirty = False

    def sync(self, persist=False):
        self.load_index()
        paths = _paths(self.inst)
        found = {}
        reset = False
        for path in paths:
            with private_open(path) as stream:
                st = os.fstat(stream.fileno())
                key = '%s:%s' % (st.st_dev, st.st_ino)
                size, mtime = st.st_size, st.st_mtime_ns
                found[key] = (path, st)
                old = self.positions.get(key)
                if old and (size < old['offset'] or (size == old['offset'] and mtime != old['mtime'])
                            or (size > old['offset'] and _stream_anchor(stream, old['offset']) != old['anchor'])):
                    reset = True
        if set(self.positions) - set(found):
            reset = True
        if reset:
            self.positions, self.state = {}, _blank()
            self.dirty = True
        for key, (path, st) in found.items():
            old = self.positions.get(key)
            offset = old['offset'] if old else 0
            if old and offset == st.st_size:
                continue
            # Bound descriptor use independently of the number of monthly files.
            # The second open must still name the exact observed generation.
            with private_open(path) as stream:
                generation = common.DirectoryTarget.generation(st)
                if common.DirectoryTarget.generation(os.fstat(stream.fileno())) != generation:
                    raise OSError('journal changed before replaying')
                stream.seek(offset)
                for line in stream:
                    _apply(self.state, _event(line))
                offset = stream.tell()
                anchor = _stream_anchor(stream, offset)
                if common.DirectoryTarget.generation(os.fstat(stream.fileno())) != generation:
                    raise OSError('journal changed while replaying')
            self.positions[key] = {'offset': offset, 'mtime': st.st_mtime_ns, 'anchor': anchor}
            self.dirty = True
        marker = self.inst.root / '.brain/events-repair-needed.json'
        if _stamp(marker):
            try:
                note = json.loads(private_read(marker))
                if note['ref'] not in self.state['resolved']:
                    self.state['faults'].setdefault(note['ref'], note['reason'])
            except (ValueError, KeyError, TypeError):
                raise JournalError('damaged recovery marker') from None
        if persist and self.dirty:
            self.save()
        return self

    def save(self):
        with common.directory_target(self.path, create=True, private=True) as target:
            target.atomic_write(json.dumps({'version': 2, 'positions': self.positions, 'state': self.state},
                                           separators=(',', ':'), sort_keys=True))
            st = target.stat()
            self.index_stamp = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
            self.dirty = False

    def repair(self):
        # Publish a durable flag before rewriting damaged advisory bytes.
        marker = self.inst.root / '.brain/events-repair-needed.json'
        note = {'ref': uuid.uuid4().hex, 'reason': 'journal_damage'}
        common.atomic_write(marker, json.dumps(note), private=True, create=True)
        for path in _paths(self.inst):
            good, bad = [], []
            with private_open(path) as stream:
                for line in stream:
                    try:
                        _event(line)
                        good.append(line)
                    except JournalError:
                        bad.append(line)
            if bad:
                quarantine(self.inst, 'events', b''.join(bad))
                common.atomic_write(path, b''.join(good).decode('utf-8'), private=True)
        # A cache is derived and disposable. A corrupt marker is replaced by the flag above.
        self.positions, self.state = {}, _blank()
        self.loaded, self.index_stamp, self.dirty = True, _stamp(self.path), True
        self.sync()
        self.save()
        self.append('root', 'damage', note['ref'], {'reason': note['reason'], 'flagged': True})

    def append(self, node, kind, ref, data, scrub=True):
        self.sync()
        data = dict(data)
        if 'views' in data:
            data['views'] = {node_ref(k): v for k, v in data['views'].items()}
        record = {'ts': self.inst.now(), 'node': node_ref(node), 'kind': kind, 'actor': 'brain',
                  'ref': ref, 'data': {'version': VERSION, **data}}
        if scrub:
            record = screen(self.inst, record)
        path = self.inst.root / '.brain/events.jsonl'
        with common.directory_target(path, create=True, private=True) as target:
            current = target.stat()
            if current and current.st_size:
                with os.fdopen(target.open(), 'rb') as stream:
                    first = _event(stream.readline())
                    target.check_file(stream.fileno())
                month = first['ts'][:7]
                if not re.fullmatch(r'[0-9]{4}-[0-9]{2}', month):
                    raise JournalError('invalid journal rotation month')
                if month != self.inst.now()[:7]:
                    name = 'events-' + month + '.jsonl'
                    if target.stat(name):
                        raise JournalError('journal rotation archive already exists')
                    target.check()
                    os.replace(target.path.name, name, src_dir_fd=target.fd, dst_dir_fd=target.fd)
                    os.fsync(target.fd)
            path = target.path
        append_line(path, record)
        self.sync(persist=True)
        return record

    def fault(self, reason, packet=None):
        ref = uuid.uuid4().hex
        self.append('root', 'damage', ref, {'reason': reason, 'flagged': True, **({'outbox': packet} if packet else {})}, scrub=False)


def journal(inst, repair=False):
    key = str(inst.root.resolve())
    store = _JOURNALS.setdefault(key, Journal(inst))
    store.inst = inst
    try:
        return store.sync(persist=repair)
    except JournalError:
        if not repair:
            raise
        store.repair()
        return store


def append(inst, node, kind, ref, data, scrub=True):
    return journal(inst).append(node, kind, ref, data, scrub=scrub)


def replay(inst, records=None):
    if records is None:
        state = journal(inst).state
    else:
        state = _blank()
        for e in records:
            _apply(state, e)
    views = {n['id']: state['views'].get(node_ref(n['id']), state['views'].get(n['id'])) for n in inst.nodes()}
    return state['resources'], {k: v for k, v in views.items() if v is not None}, state['opened']


def resources(inst):
    """Content hashes only. Git, external homes, reports, source imports and caches are outside this stream."""
    result = {}
    def walk(folder, deep=False):
        for p in sorted(folder.iterdir()):
            if p.is_dir() and not p.is_symlink():
                if p.name == "nodes":
                    for child in sorted(p.iterdir()):
                        if child.is_dir() and not child.is_symlink() and (child / "node.md").is_file():
                            walk(child)
                elif p.name in STATE_DIRS or deep:
                    walk(p, True)
            elif p.is_file() and p.suffix in (".md", ".json", ".txt", ".sh"):
                rel = p.relative_to(inst.root).as_posix()
                result[rel] = digest(p.read_bytes())
    walk(inst.root)
    for name in ("local.json", "inbox-seen.json"):
        path = inst.root / ".brain" / name
        if _stamp(path):
            result[".brain/" + name] = digest(private_read(path))
    # Ready packets are Brain-owned state in workbenches, not external Git operations.
    for n in inst.nodes():
        wb = n["local"].get("workbench")
        if wb:
            folder = Path(wb).expanduser() / "scratch/ready"
            if folder.is_dir():
                for path in folder.glob("*.json"):
                    result["@ready/" + n["id"] + "/" + path.name] = digest(path.read_bytes())
    return result


def resource_scope(inst, path):
    if path in ('.brain/local.json', '.brain/inbox-seen.json'):
        return '*'
    for n in sorted(inst.nodes(), key=lambda n: len(n['dir'].parts), reverse=True):
        prefix = n['dir'].relative_to(inst.root).as_posix()
        if path.startswith('@ready/' + n['id'] + '/') or prefix == '.' or path.startswith(prefix + '/'):
            return node_ref(n['id'])
    return node_ref('root')


def resource_list(inst, values, display=True):
    rows = [{'id': digest(k.encode()), 'scope': resource_scope(inst, k), 'hash': v,
             **({'path': k} if display else {})} for k, v in sorted(values.items())]
    return screen(inst, rows) if display else rows


def pending(records):
    opened = {}
    for e in records:
        if e["kind"] == "begin":
            opened[e["ref"]] = e
        elif e["kind"] in ("commit", "abort", "reconcile"):
            opened.pop(e["ref"], None)
    return opened


def fold_hooks(inst):
    path = inst.root / '.brain/hook-events.jsonl'
    st = _stamp(path)
    if st is None:
        return
    store = journal(inst)
    token = '%s:%s' % st[:2]
    cursor = inst.root / '.brain/hook-events-offset'
    try:
        saved = int(private_read(cursor)) if _stamp(cursor) else 0
        if saved < 0 or saved > st[2]:
            raise ValueError()
    except (ValueError, UnicodeError):
        quarantine(inst, 'hook-events-offset', private_read(cursor))
        store.fault('hook_cursor_damage')
        saved = 0
    # The journal ack is authoritative if a process died before saving the cursor.
    offset = max(saved, store.state['hook_offsets'].get(token, 0))
    if offset > st[2]:
        store.fault('hook_outbox_truncated')
        offset = 0
    current = None
    with private_open(path) as stream:
        stream.seek(offset)
        while True:
            line = stream.readline()
            if not line:
                break
            end = stream.tell()
            packet = {'file': token, 'end': end}
            try:
                data = json.loads(line)
                if (not line.endswith(b'\n') or not isinstance(data, dict)
                        or any(not isinstance(data.get(k), str) for k in ('node', 'ref', 'ts'))
                        or not isinstance(data.get('data'), dict)):
                    raise ValueError()
                _payload(data['data'])
            except (ValueError, TypeError, UnicodeError):
                quarantine(inst, 'hook-events', line)
                store.fault('hook_outbox_damage', packet)
                offset = end
                continue
            if end <= store.state['hook_offsets'].get(token, 0) or data['ref'] in store.state['legacy_hooks']:
                offset = end
                continue
            if current is None:
                current = {r['id']: r for r in resource_list(inst, resources(inst), display=False)}
            # A hook is an invalidation, not an old content assertion. Re-observe under this lock.
            fresh = []
            for old in data['data'].get('resources', []):
                row = current.get(old.get('id'))
                if row:
                    fresh.append(row)
                else:
                    fresh.append({'id': old['id'], 'scope': old.get('scope', '*'), 'hash': None})
            append(inst, data['node'], 'hook', data['ref'], {'resources': fresh, 'at': data['ts'], 'outbox': packet}, scrub=False)
            offset = end
    common.atomic_write(cursor, str(offset), private=True, create=True)


def hook_written(n_dir):
    root = instance.instance_above(n_dir)
    inst = instance.Instance(root)
    n = next(n for n in inst.nodes() if n["dir"] == n_dir)
    f = n_dir / "inbox.md"
    record = screen(inst, {"ts": inst.now(), "node": node_ref(n["id"]), "ref": uuid.uuid4().hex,
                           "data": {"resources": resource_list(inst, {f.relative_to(root).as_posix(): digest(f.read_bytes())})}})
    append_line(root / ".brain/hook-events.jsonl", record)


def lease_path(inst, ref, create=False):
    if not re.fullmatch(r"[0-9a-f]{32}", ref):
        common.die("invalid event operation reference")
    path = inst.root / ".brain/events-leases" / ref
    return private_path(path) if create else path


def lease_live(inst, ref):
    p = lease_path(inst, ref)
    try:
        with common.directory_target(p, private=True) as target:
            fd = target.open()
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
                target.check_file(fd)
                return False
            finally:
                os.close(fd)
    except FileNotFoundError:
        return False


@contextlib.contextmanager
def dead_lease(inst, ref):
    """Keep a dead lease locked through reconciliation and exact-inode cleanup."""
    with contextlib.ExitStack() as stack:
        try:
            target = stack.enter_context(common.directory_target(lease_path(inst, ref), private=True))
            fd = target.open()
        except FileNotFoundError:
            yield None
            return
        stack.callback(os.close, fd)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            common.die('another write is active; cannot reconcile its snapshot')
        target.check_file(fd)
        yield target, fd


class Command:
    """One command operation; a crash or partial failure leaves its begin unresolved."""
    def __init__(self, root, args=None, command=None, node="root"):
        self.inst = instance.Instance(root)
        self.args = args
        ids = {n["id"] for n in self.inst.nodes()}
        candidates = [getattr(args, "id", None), getattr(args, "node", None), getattr(args, "source", None)] if args else [node]
        self.node = next((target for target in candidates if isinstance(target, str) and target in ids), "root")
        self.close_job = bool(getattr(args, "close", False)) or (args is not None and args.f.__name__ in ("cmd_wt_prune", "cmd_wt_rm"))
        self.command = command or args.f.__name__.removeprefix("cmd_")
        if getattr(args, "project", False) and self.command == "status":
            self.command = "project_status"
        self.lease = None
        self.lease_target = None
        self.record_paths = set()
        self.study_handback = (self.command == 'handback' and
                               str(getattr(args, 'branch', '')).startswith('import:'))

    def start(self):
        self.ref = uuid.uuid4().hex
        self.before = resources(self.inst)
        self.lease_target = common.directory_target(lease_path(self.inst, self.ref), create=True, private=True)
        self.lease_target.__enter__()
        try:
            self.lease = self.lease_target.open(os.O_RDWR | os.O_CREAT | os.O_EXCL)
            fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.fsync(self.lease)
            os.fsync(self.lease_target.fd)
            data = {"command": self.command}
            if not journal(self.inst).positions:
                data["baseline"] = resource_list(self.inst, self.before)
            append(self.inst, self.node, "begin", self.ref, data)
        except BaseException:
            self.close_lease()
            raise

    def __enter__(self):
        nested = any(op.inst.root == self.inst.root for op in _CURRENT)
        # Refused writes must not first publish repairs or reconstructed sources.
        # Read the retained state without changing it before admitting a writer.
        if (not nested and self.command not in ("status", "set")
                and any(source(self.inst, n) == "events" for n in self.inst.nodes())
                and journal(self.inst).positions and detect_gaps(self.inst, self.node, record=False)):
            common.die("events source has unreconciled state; run status --refresh before writing")
        journal(self.inst, repair=True)
        if not nested:
            read_comparisons(self.inst, repair=True)
            if journal(self.inst).positions:
                detect_gaps(self.inst, self.node)
        self.start()
        _CURRENT.append(self)
        try:
            if not nested:
                # These are durable writers too: their journal records and hook
                # acknowledgements belong to this live, locked operation.
                fold_hooks(self.inst)
                recover_records(self.inst)
                backfill_records(self.inst)
            return self
        except BaseException as primary:
            # __exit__ is not called when __enter__ raises. Preserve the primary
            # failure and leave partial preparation unresolved for reconciliation.
            secondary = None
            try:
                self.finish(False)
            except BaseException as error:
                secondary = error
            try:
                self.close_lease()
            except BaseException as error:
                secondary = secondary or error
            finally:
                _CURRENT.pop()
            if secondary is not None:
                raise primary from secondary
            raise

    def close_lease(self):
        try:
            if self.lease is not None:
                st = os.fstat(self.lease)
                try:
                    self.lease_target.unlink_owned(self.lease_target.path.name, (st.st_dev, st.st_ino))
                finally:
                    os.close(self.lease)
        finally:
            self.lease = None
            if self.lease_target is not None:
                self.lease_target.__exit__(None, None, None)
                self.lease_target = None

    def changes(self, after):
        # While setup/verify releases the lock, other commands may complete. Their recorded
        # changes belong to their operations, not to this command's success or failure.
        recorded, _, _ = replay(self.inst)
        return {k: after.get(k) for k in self.before.keys() | after.keys()
                if self.before.get(k) != after.get(k)
                and recorded.get(digest(k.encode()), {}).get('hash') != after.get(k)}

    def finish(self, success, exit_code=0):
        self.inst = instance.Instance(self.inst.root)
        # Hooks own their durable writes even when this command failed. Attribute
        # them before deciding whether the command left partial writes behind.
        fold_hooks(self.inst)
        after = resources(self.inst)
        changed = self.changes(after)
        if success:
            maintenance_ok, views = True, {}
            try:
                views = maintain(self, changed)
            except (Exception, SystemExit):
                maintenance_ok = False
                print('brain: state saved; generated status maintenance needs repair', file=__import__('sys').stderr)
            # snapshot rendering may itself deliver queued hook questions.
            fold_hooks(self.inst)
            after = resources(self.inst)
            changed = self.changes(after)
            try:
                data = {'command': self.command, 'exit': exit_code, 'resources': resource_list(self.inst, changed),
                        'views': views, 'maintenance_ok': maintenance_ok}
                if not maintenance_ok:
                    data['invalidate'] = [node_ref(n['id']) for n in affected_nodes(self.inst, changed, self.node)]
                # Prepare all display text before publication; no display failure may strand begin.
                data = screen(self.inst, data)
            except (Exception, SystemExit):
                data = {'command': self.command, 'exit': exit_code,
                        'resources': resource_list(self.inst, changed, display=False),
                        'maintenance_ok': False,
                        'invalidate': [node_ref(n['id']) for n in affected_nodes(self.inst, changed, self.node)]}
            append(self.inst, self.node, 'commit', self.ref, data, scrub=False)
        elif not changed:
            append(self.inst, self.node, "abort", self.ref, {"command": self.command})
        # Persisted partial changes require reconciliation, never a false commit.

    def __exit__(self, typ, value, tb):
        try:
            with instance.brain_lock(self.inst):
                try:
                    verdict = isinstance(value, common.VerdictExit)
                    self.finish(typ is None or verdict or (typ is SystemExit and value.code in (None, 0)),
                                value.code if verdict else 0)
                finally:
                    self.close_lease()
        finally:
            _CURRENT.pop()
        return False


def checkpoint_setup():
    """wt add completed its own Brain writes; setup has a separate operation and may release the lock."""
    if _CURRENT:
        op = _CURRENT[-1]
        op.finish(True)
        op.close_lease()
        op.command = "wt_setup"
        op.start()


def cmd_backlog(a):
    inst = nodes.load()
    n = inst.node(a.id)
    if a.action == "add":
        text = " ".join(a.item.split())
        if not text:
            common.die("backlog item must not be empty")
        text = nodes.import_text(text, guard.never_terms(n))
        path = n["dir"] / "backlog.md"
        common.safe_target(path)
        with path.open("a") as stream:
            stream.write("- " + inst.now() + ": " + text + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        common.touch(path)
        inst.commit([path], n["id"] + ": backlog item")
        print(n["id"] + ": backlog item added")


# Views store generated metadata only. Human prose stays in snapshot files.
def snapshot_view(inst, n, known_merges=None):
    from . import jobs, status
    state = status.file_state(inst, n)
    known = status.facts_meta(n).get('merged_before', []) if known_merges is None else known_merges
    lines, merged = status.status_facts(inst, n, state, known, metadata_only=True)
    active, keys = [], []
    hints, retained = {}, set()
    for cells in status.active_work(n, include_closed=True):
        if len(cells) >= 2:
            cells = (cells + ['-'] * max(0, 3 - len(cells)) + ['unknown'])[:4]
            # Match the cleanup reader's supported backtick/annotation syntax.
            branch = cells[0].replace('`', '').split()
            if branch:
                hints.setdefault(branch[0], []).append(cells)
    all_rows = state['jobs'] or []
    for row in all_rows:
        if row['Ended'] and row not in jobs.unsettled_jobs(all_rows):
            continue
        signal = state['handbacks'].get(jobs.job_key(row)) or {}
        candidates = hints.get(row['Job'], [])
        unambiguous = len(candidates) == 1 and sum(r['Job'] == row['Job'] for r in all_rows) == 1
        refs = candidates[0][1:3] if unambiguous else ['-', '-']
        refs = [v if v and len(v) <= 1024 and not re.search(r'[\r\n\x00]', v) else '-' for v in refs]
        job_state = row['Outcome'] if row['Ended'] else signal.get('state') or 'open'
        if job_state not in ('open', 'done', 'failed', 'blocked', 'question', 'merged', 'abandoned', 'closed', 'studied', 'external'):
            job_state = 'unknown'
        if unambiguous and candidates[0][1].strip() not in ('', '-', '—'):
            # Ledger completion is not a terminal transition of the source owner.
            # Keep its state verbatim, including unknown and annotated labels.
            job_state = candidates[0][3]
            retained.add(id(candidates[0]))
        active.append([row['Job'], *refs, job_state])
        keys.append(jobs.job_key(row))
    for candidates in hints.values():
        for cells in candidates:
            if (id(cells) not in retained and cells[1].strip() not in ('', '-', '—')
                    and not status.active_work_closed(cells)):
                # Ambiguous, missing or settled ledger rows cannot erase ownership.
                active.append(cells)
                keys.append('')
    return screen(inst, {'facts': lines, 'active': active, 'active_keys': keys, 'merged': list(merged), 'merged_before': known,
                         'comparison': {'facts': brain_facts(lines), 'active': active}})


def render(view):
    from . import jobs
    table = ['## Active work', '', '| Branch | Thread | PR | State |', '| --- | --- | --- | --- |']
    table += ['| ' + ' | '.join(jobs.cell(v) for v in row) + ' |' for row in view['active']]
    return '\n'.join(view['facts'] + ['', *table]) + '\n'


def brain_facts(lines):
    result, section = [], ''
    ignored = {'Merged since previous refresh:', 'Open worktrees (this machine):',
               'Finished jobs awaiting coordinator:'}
    for line in lines:
        if line and not line.startswith('- '):
            section = line
        if section in ignored or line.startswith(('Due:', 'Preview: missing')):
            continue
        result.append(line)
    return [line for line in result if line.strip()]


def normalize(view):
    if view is None:
        return None
    return view.get('comparison', {'facts': brain_facts(view['facts']), 'active': view['active']})


def events_view(inst, n):
    try:
        store = journal(inst)
    except JournalError:
        common.die('events view unavailable: journal damage; run a locked status --refresh')
    if store.state['faults']:
        common.die('events view unavailable: flagged stream damage needs a locked status --refresh')
    if outbox_pending(inst):
        common.die('events view unavailable: hook metadata awaits a locked refresh')
    _, views, opened = replay(inst)
    if opened:
        common.die('events view unavailable: interrupted or active write; run a locked status --refresh to reconcile')
    if n['id'] not in views:
        common.die('events view unavailable: no recorded view; run status --refresh')
    return views[n['id']]


def source(inst, n):
    return n['fm'].get('status_source', inst.node('root')['fm'].get('status_source', inst.cfg.get('status_source', 'files')))


def comparison(inst, n, current=None, record=True):
    state, views, opened = replay(inst)
    actual = resource_list(inst, resources(inst))
    actual = {r['id']: r for r in actual}
    def belongs(r):
        return r.get('scope', '*') in ('*', node_ref(n['id']))
    changed = [key for key in state.keys() | actual.keys()
               if ((key in state and belongs(state[key])) or (key in actual and belongs(actual[key])))
               and (state.get(key, {}).get('hash') != actual.get(key, {}).get('hash'))]
    view = current if current is not None else snapshot_view(inst, n)
    view_equal = n['id'] in views and normalize(view) == normalize(views[n['id']])
    # The current command's begin is active; it is not an interrupted operation.
    interrupted = [ref for ref in opened if not lease_live(inst, ref)]
    faults = list(journal(inst).state['faults'])
    out_of_scope = [] if n['id'] not in views or view == views[n['id']] or not view_equal else ['external_git_or_clock']
    result = {'id': uuid.uuid4().hex, 'ts': inst.now(), 'node': node_ref(n['id']), 'kind': 'comparison', 'equal': not changed and view_equal and not interrupted and not outbox_pending(inst) and not faults,
              'resources': sorted(changed), 'view_equal': view_equal, 'interrupted': interrupted,
              'out_of_scope': out_of_scope, 'faults': faults}
    if record:
        append_line(inst.root / '.brain/events-compare.jsonl', screen(inst, result))
    return result


def reconcile(inst, node=None, snapshot=False):
    state, _, opened = replay(inst)
    dead = [ref for ref in opened if not lease_live(inst, ref)]
    actual = resource_list(inst, resources(inst))
    actual_map = {r["id"]: r for r in actual}
    gap = any(state.get(k, {}).get("hash") != actual_map.get(k, {}).get("hash")
              for k in state.keys() | actual_map.keys())
    if not dead and not gap and not journal(inst).state["faults"] and not journal(inst).state["invalid"]:
        return
    ours = {op.ref for op in _CURRENT if op.inst.root == inst.root}
    foreign = [e for ref, e in opened.items() if ref not in ours and lease_live(inst, ref)]
    if foreign:
        # Snapshot rendering does not own an active writer's event baseline.
        # Defer only a healthy live operation, never a separate partial write or
        # damaged replay. Explicit/event-source reconciliation stays strict.
        healthy = not dead and not journal(inst).state['faults'] and not journal(inst).state['invalid']
        if snapshot and node is not None and healthy:
            return
        # A node refresh may proceed beside a released writer, but must not
        # adopt that writer's pending bytes or close its operation. Explicit
        # whole-instance reconciliation still refuses every live foreign lease.
        scopes = {e['node'] for e in foreign}
        owners = [n['id'] for n in inst.nodes() if node_ref(n['id']) in scopes]
        scopes.update(node_ref(n['id']) for n in inst.nodes()
                      if any(owner == 'root' or n['id'].startswith(owner + '/') for owner in owners))
        changed = [actual_map.get(k) or state[k] for k in state.keys() | actual_map.keys()
                   if state.get(k, {}).get('hash') != actual_map.get(k, {}).get('hash')]
        if (node is not None and node_ref(node['id']) not in scopes and healthy
                and all(r.get('scope') in scopes for r in changed)):
            return
        common.die("another write is active; cannot reconcile its snapshot")
    with contextlib.ExitStack() as stack:
        leases = {ref: stack.enter_context(dead_lease(inst, ref)) for ref in dead}
        marker = stack.enter_context(common.directory_target(inst.root / '.brain/events-repair-needed.json', private=True))
        marker_st = marker.stat()
        # One locked snapshot feeds both comparison and reconciliation. Rendering
        # every node twice repeats the same expensive Git/accounting reads and
        # can observe two different external states within one repair.
        views = {n['id']: snapshot_view(inst, n) for n in inst.nodes() if not n['archived']}
        # Keep evidence of the gap before installing a new baseline.
        for n in inst.nodes():
            comparison(inst, n, current=views.get(n['id']))
        current = resource_list(inst, resources(inst))
        existing = {r['id'] for r in current}
        for ref in dead or [uuid.uuid4().hex]:
            lease = leases.get(ref)
            if lease:
                lease[0].check_file(lease[1])
            append(inst, opened.get(ref, {}).get("node", "root"), 'reconcile', ref,
                   {'resources': current + [{**r, 'hash': None}
                       for key, r in journal(inst).state['resources'].items() if key not in existing],
                    'views': views, 'flagged': True, 'clear_faults': True})
            if lease:
                st = os.fstat(lease[1])
                lease[0].unlink_owned(lease[0].path.name, (st.st_dev, st.st_ino))
        if marker_st:
            marker.unlink_owned(marker.path.name, (marker_st.st_dev, marker_st.st_ino))


def read_comparisons(inst, repair=False):
    """Tail-index comparison evidence; retain one day plus the first real comparison per node."""
    path = inst.root / '.brain/events-compare.jsonl'
    cache = inst.root / '.brain/events-compare-index.json'
    stamp = _stamp(path)
    if stamp is None:
        return []
    try:
        saved = json.loads(private_read(cache))
        if (saved['version'] != 1 or not isinstance(saved['rows'], list) or not isinstance(saved['first'], dict)
                or saved['identity'] != list(stamp[:2]) or stamp[2] < saved['offset']
                or (stamp[2] == saved['offset'] and stamp[3] != saved['mtime'])
                or _anchor(path, saved['offset']) != saved['anchor']):
            raise ValueError()
    except (OSError, ValueError, TypeError, KeyError):
        saved = {'version': 1, 'identity': list(stamp[:2]), 'offset': 0, 'rows': [], 'first': {}}
    now = inst.now_dt()
    def valid(row):
        return (isinstance(row, dict) and isinstance(row.get('node'), str)
                and isinstance(row.get('ts'), str) and inst.parse_time(row['ts']) is not None
                and type(row.get('equal')) is bool)
    if not all(valid(r) for r in saved['rows']) or not all(valid(r) for r in saved['first'].values()):
        saved.update(offset=0, rows=[], first={})
    rows, first = saved['rows'], saved['first']
    with private_open(path) as stream:
        stream.seek(saved['offset'])
        for line in stream:
            try:
                row = json.loads(line)
                if not line.endswith(b'\n') or not valid(row):
                    raise ValueError()
            except (ValueError, TypeError, UnicodeError):
                if not repair:
                    common.die('invalid comparison history; run a locked status --refresh before switching views')
                quarantine(inst, 'events-compare', line)
                journal(inst).fault('comparison_history_damage')
                continue
            rows.append(row)
            if row.get('kind') == 'comparison':
                key = node_ref(row['node'])
                if key not in first or inst.parse_time(row['ts']) < inst.parse_time(first[key]['ts']):
                    first[key] = row
        saved['offset'] = stream.tell()
    # Old coverage rows do not establish a day, and old differences cannot poison a future clean day.
    rows = [r for r in rows if (now - inst.parse_time(r['ts'])).total_seconds() <= 86400]
    saved.update(rows=rows, first=first, mtime=stamp[3], anchor=_anchor(path, saved['offset']))
    if repair:
        common.atomic_write(cache, json.dumps(saved, separators=(',', ':'), sort_keys=True), private=True, create=True)
    return rows + [r for r in first.values() if r not in rows]


def switch_gate(inst, n):
    comparison_now = comparison(inst, n, record=False)
    if not comparison_now['equal']:
        common.die('status_source=events requires a matching snapshot and no interrupted writes; refresh and compare first')
    rows = [r for r in read_comparisons(inst) if same_node(r['node'], n['id'])]
    now = inst.now_dt()
    times = [(r, inst.parse_time(r['ts'])) for r in rows]
    real = [(r, t) for r, t in times if r.get('kind') == 'comparison']
    if not real or not any(t and (now - t).total_seconds() >= 86400 for _, t in real):
        common.die('status_source=events requires at least one day of comparison history')
    recent = [r for r, t in times if t and 0 <= (now - t).total_seconds() <= 86400]
    explained = explained_comparisons(inst)
    if not any(r.get('kind') == 'comparison' for r in recent) or any(not r['equal'] and r.get('id') not in explained for r in recent):
        common.die('status_source=events requires a comparison day with zero unexplained differences')
    # Root is instance-wide: all existing nodes need their own clean comparison day.
    if n['id'] == 'root':
        for child in inst.nodes():
            if child['id'] != 'root' and not child['archived']:
                switch_gate(inst, child)


def cmd_events(a):
    inst = nodes.load()
    n = inst.node(a.id)
    if getattr(a, 'metrics', False):
        from . import delivery_metrics
        print(json.dumps(delivery_metrics.delivery_measurements(inst, n), sort_keys=True, indent=2))
        return
    if getattr(a, 'rebuild_records', False):
        count = len(recover_records(inst, n, rebuild=True))
        print(n['id'] + ': rebuilt ' + str(count) + ' event record(s)')
        return
    if getattr(a, 'record', None):
        try:
            fields = json.loads(a.data)
            if not isinstance(fields, dict):
                raise ValueError('fact data must be an object')
            event = record_fact(inst, n, a.record, a.job_key, **fields)
        except (ValueError, TypeError):
            common.die('invalid fact metadata; use controlled phase/state/classification and evidence fields')
        print(json.dumps(event, sort_keys=True) if a.json else n['id'] + ': fact recorded ' + event['ref'])
        return
    if getattr(a, 'explain', None):
        rows = read_comparisons(inst)
        if not any(r.get('id') == a.explain and same_node(r['node'], n['id']) and not r['equal'] for r in rows):
            common.die('explain needs an existing difference id for this node')
        if not a.reason or not a.reason.strip():
            common.die('explain needs --reason; record the full reason in decisions.md')
        append(inst, n['id'], 'explain', uuid.uuid4().hex,
               {'comparison': a.explain, 'reason_hash': digest(a.reason.encode())})
        print(n['id'] + ': difference explained; reason fingerprint recorded')
        return
    if a.compare:
        now = inst.now_dt()
        rows = [r for r in read_comparisons(inst) if same_node(r['node'], n['id']) and inst.parse_time(r['ts'])
                and 0 <= (now - inst.parse_time(r['ts'])).total_seconds() <= 86400]
        current = comparison(inst, n, record=False)
        result = {'node': n['id'], 'checks': len(rows) + 1, 'differences': sum(not r['equal'] for r in rows) + int(not current['equal']),
                  'unexplained': [r.get('id') for r in rows if not r['equal'] and r.get('id') not in explained_comparisons(inst)],
                  'current': current}
        if a.json:
            print(json.dumps(screen(inst, result), sort_keys=True))
        else:
            print('%s: %d comparison(s), %d difference(s) in the last day; current: %s' %
                  (n['id'], result['checks'], result['differences'], 'equal' if result['current']['equal'] else 'different'))
        return
    records = [e for e in read(inst) if same_node(e['node'], n['id'])]
    for e in records:
        print(json.dumps(e, ensure_ascii=False, sort_keys=True))


def events_view_without_current(inst, n):
    active = {op.ref for op in _CURRENT}
    _, views, opened = replay(inst)
    if set(opened) - active:
        common.die('events view unavailable: another interrupted or active write needs reconciliation')
    if n['id'] not in views:
        common.die('events view unavailable: no recorded view; refresh while files are authoritative')
    return views[n['id']]


def cap_now(inst, n):
    path = n['dir'] / 'status.md'
    if not path.exists():
        return
    with path.open(newline='') as stream:
        text = stream.read()
    match = re.search(r'(?ms)^## Now[^\r\n]*\r?\n(.*?)(?=^## |\Z)', text)
    if not match:
        return
    lines = match.group(1).splitlines(keepends=True)
    visible = re.sub(r'<!--.*?-->', lambda m: '\n' * m.group().count('\n'), match.group(1), flags=re.S).splitlines()
    count, cut = 0, len(lines)
    for i, line in enumerate(visible):
        if line.strip():
            count += 1
            if count > 30:
                cut = i
                break
    if cut == len(lines):
        return
    owned = ownership_before(inst, n)
    archive = n['dir'] / 'status-archive.md'
    previous = archive.read_text() if archive.exists() else ''
    common.atomic_write(archive, previous + '\n## ' + inst.now() + ': Now overflow\n\n' + ''.join(lines[cut:]) + '\n')
    common.atomic_write(path, text[:match.start(1)] + ''.join(lines[:cut]) + '\n' + text[match.end(1):])
    common.touch(path, archive)
    own_transformation(inst, n, owned)


def active_table(inst, n, view):
    path = n['dir'] / 'status.md'
    with path.open(newline='') as stream:
        text = stream.read()
    table = render({'facts': [], 'active': view['active']}).lstrip('\n')
    match = re.search(r'(?ms)^## Active work[^\r\n]*\r?\n.*?(?=^## |\Z)', text)
    if match:
        old = match.group()
        rows = list(re.finditer(r'(?m)^[ \t]*\|[^\r\n]*(?:\r?\n|$)', old))
        new_rows = table.splitlines()[2:]
        if [r.group().rstrip('\r\n') for r in rows] == new_rows:
            return
        newline = '\r\n' if '\r\n' in old else '\n'
        rendered = newline.join(new_rows) + newline
        if rows:
            replacement = old[:rows[0].start()] + rendered + old[rows[-1].end():]
        else:
            replacement = old + newline + rendered
        text = text[:match.start()] + replacement + text[match.end():]
    elif view['active']:
        text = text + '\n' + table + '\n'
    else:
        return
    common.atomic_write(path, text)
    common.touch(path)


def close_active_work(inst, n, row):
    """An explicit job-close transition may close one unambiguous source row."""
    from . import jobs, status
    rows = status.active_work(n, include_closed=True)
    matches = [cells for cells in rows
               if cells and cells[0].replace('`', '').split()[:1] == [row['Job']]]
    if (len(matches) == 1 and len(matches[0]) >= 4
            and sum(r['Job'] == row['Job'] for r in jobs.read_jobs(n) or []) == 1):
        matches[0][3] = row['Outcome']
        active_table(inst, n, {'active': rows})


def judgement(text):
    from . import status
    text = status.FACTS_RE.sub('', text)
    return re.sub(r'(?ms)^## Active work[^\r\n]*\r?\n.*?(?=^## |\Z)', '', text)


def _status_bytes(inst, n):
    path = n['dir'] / 'status.md'
    rel = path.relative_to(inst.root).as_posix()
    old = subprocess.run(['git', 'show', 'HEAD:' + rel], cwd=inst.root, capture_output=True)
    archive = n['dir'] / 'status-archive.md'
    archive_old = subprocess.run(['git', 'show', 'HEAD:' + archive.relative_to(inst.root).as_posix()],
                                 cwd=inst.root, capture_output=True)
    return (old.returncode, old.stdout, path.read_bytes() if path.exists() else None,
            archive_old.stdout if archive_old.returncode == 0 else b'', archive.read_bytes() if archive.exists() else b'')


def _status_stamps(inst):
    path = inst.root / '.brain/events-status-commits.json'
    try:
        value = json.loads(private_read(path))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _stamp_for(inst, n):
    value = _status_stamps(inst).get(node_ref(n['id']), _status_stamps(inst).get(n['id'], {}))
    return {'at': value} if isinstance(value, str) else value


def _owned_judgement(inst, n, blobs=None):
    code, old, current, archive_old, archive_current = blobs or _status_bytes(inst, n)
    stamp = _stamp_for(inst, n)
    return (not code and current is not None and stamp.get('base_status') == digest(old)
            and stamp.get('owned_judgement') == digest(judgement(current.decode()).encode())
            and stamp.get('base_archive') == digest(archive_old)
            and stamp.get('owned_archive') == digest(archive_current))


def status_judgement_dirty(inst, n):
    blobs = _status_bytes(inst, n)
    code, old, current, _, _ = blobs
    return (bool(code) or current is None or
            (judgement(old.decode()) != judgement(current.decode()) and not _owned_judgement(inst, n, blobs)))


def ownership_before(inst, n):
    blobs = _status_bytes(inst, n)
    code, old, current, archive_old, archive_current = blobs
    if code or current is None:
        return None
    owned = _owned_judgement(inst, n, blobs)
    if not owned and (judgement(old.decode()) != judgement(current.decode()) or archive_old != archive_current):
        return None
    return {'base_status': digest(old), 'base_archive': digest(archive_old)}


def own_transformation(inst, n, before):
    if before is None:
        return
    path, archive = n['dir'] / 'status.md', n['dir'] / 'status-archive.md'
    saved = _status_stamps(inst)
    saved[node_ref(n['id'])] = {**_stamp_for(inst, n), **before,
                               'owned_judgement': digest(judgement(path.read_bytes().decode()).encode()),
                               'owned_archive': digest(archive.read_bytes() if archive.exists() else b'')}
    common.atomic_write(inst.root / '.brain/events-status-commits.json', json.dumps(saved, sort_keys=True), private=True, create=True)


def status_commit(inst, n, force=False, message=None):
    if status_judgement_dirty(inst, n):
        return
    path = n['dir'] / 'status.md'
    rel = path.relative_to(inst.root).as_posix()
    saved = _status_stamps(inst)
    prior = inst.parse_time(_stamp_for(inst, n).get('at', ''))
    if not force and prior and (inst.now_dt() - prior).total_seconds() < 3600:
        return
    want = [rel]
    archive = n['dir'] / 'status-archive.md'
    blobs = _status_bytes(inst, n)
    if archive.exists() and (blobs[3] == blobs[4] or _owned_judgement(inst, n, blobs)):
        want.append(archive.relative_to(inst.root).as_posix())
    inst._commit(inst.root, want, message or n['id'] + ': refresh status facts')
    saved[node_ref(n['id'])] = {'at': inst.now()}
    common.atomic_write(inst.root / '.brain/events-status-commits.json', json.dumps(saved, sort_keys=True), private=True, create=True)


def affected_nodes(inst, changed, preferred):
    selected = {preferred}
    for n in inst.nodes():
        prefix = n['dir'].relative_to(inst.root).as_posix()
        prefix = '' if prefix == '.' else prefix + '/'
        if any(k.startswith(prefix) and '/nodes/' not in k[len(prefix):]
               and not (not prefix and k.startswith('nodes/')) for k in changed):
            selected.add(n['id'])
    return [n for n in inst.nodes() if n['id'] in selected and not n['archived']]


def maintain(op, changed):
    if op.command == "project_status":
        return getattr(op, "projected_views", {})
    from . import status, freshness
    dispatch_landings(op.inst)
    indexed = freshness.maintain_indexes(op.inst, op.inst.nodes())
    if (op.command == 'task' and getattr(op.args, '_open_task_added', False)
            and not changed and not op.record_paths and not indexed
            and not any(n['fm'].get('upstream_generator') == 'links' for n in op.inst.nodes())):
        # Task files are outside this ledger/status projection. Preserve the
        # native command journal without re-reading every job's live usage.
        return {}
    changed = {**{path: None for path in op.record_paths}, **changed}
    views = {}
    # Cleanup records current metadata for replay, but never rewrites the status
    # document or its archive. An explicit refresh still materializes the view.
    local_only = (op.command == 'set' and changed and
                  set(changed) <= {'.brain/local.json', '.brain/inbox-seen.json'})
    # Connect plans and saved briefs live outside shared state. Keep their
    # journal/lease lifecycle and replay views without ticking shared status.
    # A connect operation that actually changes tracked state still projects it.
    private_connect = (op.command == 'connect' and not changed
                       and not op.changes(resources(op.inst)))
    # Shared creation keeps its body writes and current replay metadata, but
    # leaves tracked status documents to the existing explicit refresh command.
    # Import starts opt in only after the body validates the current snapshot.
    explicit_refresh = (op.command in ('new', 'adopt') or
                        (op.command == 'jobs' and
                         getattr(op.args, '_validated_import_start', False)))
    materialize = (op.command not in ('wt_prune', 'wt_rm') and not op.study_handback
                   and not local_only and not private_connect and not explicit_refresh)
    _, recorded_views, _ = replay(op.inst)
    selected = {n['id']: n for n in affected_nodes(op.inst, changed, op.node)}
    for n in op.inst.nodes():
        if not n['archived'] and node_ref(n['id']) in journal(op.inst).state['invalid']:
            selected[n['id']] = n
    for n in selected.values():
        if materialize:
            cap_now(op.inst, n)
        if op.command == 'status' or (op.command == 'check' and status.facts_meta(n)):
            # A requested refresh already consumed its merge baseline; retain that display.
            known = status.facts_meta(n).get('merged_before', [])
        else:
            known = status.facts_meta(n).get('merged', [])
        if op.command in ('handback', 'task', 'review', 'preview_receipt'):
            # Status projection can scan worker transcripts. It grants no action
            # authority, and must not hold up writers after terminal publication.
            # A concurrent source change refuses this generated view; the normal
            # maintenance-failure path invalidates it for an explicit refresh.
            def projection_inputs():
                paths = [op.inst.root / path for path in resources(op.inst)
                         if not path.startswith('@ready/')]
                for node in op.inst.nodes():
                    paths.extend(node['dir'] / name for name in ('tasks', 'requirements'))
                    if node.get('external') and node.get('home_path'):
                        paths.extend(instance.git_state_paths(node['home_path']))
                paths.append(op.inst.root / '.brain/communications.json')
                return paths
            with instance.released(op.inst, list(op.inst.nodes()), extra=projection_inputs):
                view = snapshot_view(op.inst, n, known)
        else:
            view = snapshot_view(op.inst, n, known)
        # During comparison, files supply new facts. After switching, a write materializes its
        # own new metadata projection; a refresh alone must use the committed event view.
        if source(op.inst, n) == 'events' and op.command == 'status':
            view = events_view_without_current(op.inst, n)
        views[n['id']] = view
        if materialize:
            status.refresh_status(op.inst, n, view)
            active_table(op.inst, n, view)
            ledger = (n["dir"] / "job-ledger.md").relative_to(op.inst.root).as_posix()
            force = op.command in ('new', 'merge', 'changes') or (op.close_job and ledger in changed)
            message = None
            if op.command == 'merge':
                message = common.git('log', '-1', '--format=%s', cwd=op.inst.root).stdout.strip()
            status_commit(op.inst, n, force, message=message)
    return views


def detect_gaps(inst, preferred, record=True):
    """Cheap pre-write coverage check: one resource scan, no Git or status rendering."""
    recorded, views, opened = replay(inst)
    actual = {r['id']: r for r in resource_list(inst, resources(inst))}
    changed = [k for k in recorded.keys() | actual.keys()
               if recorded.get(k, {}).get('hash') != actual.get(k, {}).get('hash')]
    affected = {preferred}
    for key in changed:
        scope = (actual.get(key) or recorded[key]).get('scope', '*')
        for n in inst.nodes():
            if scope in ('*', node_ref(n['id'])):
                affected.add(n['id'])
    dead = [ref for ref in opened if not lease_live(inst, ref)]
    faults = list(journal(inst).state['faults'])
    for nid in sorted(affected):
        result = {'id': uuid.uuid4().hex, 'ts': inst.now(), 'node': node_ref(nid), 'kind': 'coverage', 'equal': not changed and not dead and not faults,
                  'resources': sorted(changed), 'view_equal': None, 'interrupted': dead, 'faults': faults}
        if record:
            append_line(inst.root / '.brain/events-compare.jsonl', screen(inst, result))
    return bool(changed or dead or faults)


def explained_comparisons(inst):
    return set(journal(inst).state['explained'])


def outbox_pending(inst):
    path = inst.root / '.brain/hook-events.jsonl'
    cursor = inst.root / '.brain/hook-events-offset'
    try:
        offset = int(private_read(cursor)) if _stamp(cursor) else 0
        stamp = _stamp(path)
        return offset < 0 or (stamp is not None and stamp[2] != offset)
    except (ValueError, OSError, UnicodeError):
        return True


FACT_PHASES = ('request', 'review', 'execution', 'coordination', 'admission', 'acknowledgement', 'finished')
FACT_STATES = ('working', 'waiting', 'done')
_FACT_FIELDS = {'phase', 'state', 'failure_class', 'reason_code', 'head', 'base', 'inputs',
                'product', 'depends_on', 'evidence', 'origin', 'at'}


def validate_fact(data):
    """Facts are controlled metadata, not free-form descriptions or transcript guesses."""
    allowed = _FACT_FIELDS | {'version', 'fact_type', 'job_key'}
    if set(data) - allowed or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', data.get('fact_type', '')):
        raise ValueError('invalid fact type or fields')
    key = data.get('job_key')
    if key is not None and not re.fullmatch(r'[0-9a-f]{24}', key):
        raise ValueError('invalid fact job key')
    for field, values in (('phase', FACT_PHASES), ('state', FACT_STATES),
                          ('origin', ('observed', 'reconstructed'))):
        if field in data and data[field] not in values:
            raise ValueError('invalid fact ' + field)
    for field in ('failure_class', 'reason_code'):
        if field in data and (not isinstance(data[field], str) or
                              not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', data[field])):
            raise ValueError('fact classifications must be controlled identifiers')
    for field in ('head', 'base', 'inputs'):
        if field in data and (not isinstance(data[field], str) or not re.fullmatch(r'[0-9a-f]{7,64}', data[field])):
            raise ValueError('fact evidence must be a hexadecimal fingerprint')
    if 'product' in data and type(data['product']) is not bool:
        raise ValueError('fact product flag must be boolean')
    if 'depends_on' in data:
        values = data['depends_on']
        if (not isinstance(values, list) or len(values) > 64 or len(set(values)) != len(values)
                or any(not isinstance(x, str) or not re.fullmatch(r'[0-9a-f]{24}', x) for x in values)
                or key in values):
            raise ValueError('invalid fact dependencies')
    if 'evidence' in data:
        value = data['evidence']
        if (not isinstance(value, dict) or set(value) - {'head', 'base', 'inputs', 'ref'}
                or any(not isinstance(x, str) or not re.fullmatch(r'[A-Za-z0-9_.:/#-]{1,256}', x)
                       for x in value.values())):
            raise ValueError('fact evidence must contain bounded references')
    if 'at' in data:
        from datetime import datetime
        try:
            at = datetime.fromisoformat(data['at'])
            if at.tzinfo is None:
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError('fact time needs an absolute ISO timestamp') from None


def record_fact(inst, n, fact_type, job_key=None, **fields):
    """Publish one observed typed fact; callers supply evidence, never inferred prose.

    The caller may already own the instance lock. Reconstructed imports must label
    origin explicitly; their timestamps are not treated as measured observations.
    """
    data = {'fact_type': fact_type, 'job_key': job_key, 'origin': 'observed',
            'at': inst.now_dt().isoformat(timespec='microseconds'), **fields}
    validate_fact(data)
    at = inst.parse_time(data['at'])
    if not at or at > inst.now_dt():
        raise ValueError('fact timestamp cannot be in the future')
    with instance.brain_lock(inst):
        journal(inst, repair=True)
        event = append(inst, n['id'], 'fact', uuid.uuid4().hex, data)
        try:
            dispatch_landings(inst)
        except (Exception, SystemExit):
            print('brain: fact saved; dependency reconciliation pending', file=__import__('sys').stderr)
        return event


def facts(inst, n, job_key=None):
    """Ordered explicit audit read, scoped to the opaque node and optional exact job key."""
    return [e for e in read(inst) if e['kind'] == 'fact' and same_node(e['node'], n['id'])
            and (job_key is None or e['data'].get('job_key') == job_key)]


_RECORD_TYPES = ('jobs', 'handback')


def record_target(n, record_type, key):
    if record_type == 'jobs' and key == 'ledger':
        return n['dir'] / 'job-ledger.md'
    if record_type == 'handback' and re.fullmatch(r'[0-9a-f]{24}', key):
        return n['dir'] / 'job-handbacks' / (key + '.json')
    raise ValueError('invalid event record identity')


def record_identity(n, record_type, key):
    record_target(n, record_type, key)
    return node_ref(n['id']) + ':' + record_type + ':' + key


def record_object(inst, data):
    sha = data['object']
    if not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{64}', sha):
        raise ValueError('invalid record object')
    path = inst.root / '.brain/event-objects' / (sha + '.json')
    raw = private_read(path)
    if digest(raw) != sha:
        raise ValueError('event record object is damaged')
    obj = json.loads(raw)
    if (not isinstance(obj, dict) or obj.get('version') != 1 or not isinstance(obj.get('text'), str)
            or obj.get('record_type') != data['record_type'] or obj.get('key') != data['key']):
        raise ValueError('invalid event record object')
    return obj


def record_read(inst, n, record_type, key='ledger'):
    entry = journal(inst).state['records'].get(record_identity(n, record_type, key))
    return record_object(inst, entry)['text'] if entry else None


def write_record(inst, n, record_type, key, text, origin='observed'):
    """Journal an immutable source object before materializing its conventional file.

    Prose remains private in objects; the journal contains only identity and hashes.
    Acknowledgements distinguish unfinished writes from later handwritten edits.
    """
    path = record_target(n, record_type, key)
    common.safe_target(path)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('linked record target')
    if origin not in ('observed', 'reconstructed') or not isinstance(text, str):
        raise ValueError('invalid record source')
    with instance.brain_lock(inst):
        store = journal(inst, repair=True)
        identity = record_identity(n, record_type, key)
        previous = store.state['records'].get(identity)
        if previous and record_object(inst, previous)['text'] == text and _stamp(path) and private_read(path).decode() == text:
            return previous['ref']
        before = digest(private_read(path)) if _stamp(path) else None
        obj = {'version': 1, 'record_type': record_type, 'key': key, 'text': text}
        raw = (json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()
        sha = digest(raw)
        path_object = inst.root / '.brain/event-objects' / (sha + '.json')
        with common.directory_target(path_object, private=True, create=True) as target:
            if target.stat():
                if target.read_bytes() != raw:
                    raise ValueError('immutable event object collision')
            else:
                target.atomic_write(raw.decode())
            os.fsync(target.fd)
        data = {'record_type': record_type, 'key': key, 'object': sha, 'before': before,
                'after': digest(text.encode()), 'origin': origin}
        event = append(inst, n['id'], 'record', uuid.uuid4().hex, data)
        for op in _CURRENT:
            if op.inst.root == inst.root:
                op.record_paths.add(path.relative_to(inst.root).as_posix())
        apply_record(inst, n, {**data, 'ref': event['ref']})
        return event['ref']


def apply_record(inst, n, entry, rebuild=False):
    path = record_target(n, entry['record_type'], entry['key'])
    common.safe_target(path)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('linked record target')
    text = record_object(inst, entry)['text']
    with common.directory_target(path, private=True, create=True) as target:
        st = target.stat()
        current = digest(target.read_bytes()) if st else None
        if current != entry['after']:
            if current != entry['before'] and not (rebuild and current is None):
                raise ValueError('record target changed independently; reconcile without overwriting it')
            target.atomic_write(text, expected=target.generation(st))
            common.touch(path)
    if not entry.get('applied'):
        append(inst, n['id'], 'record_applied', uuid.uuid4().hex,
               {'record_ref': entry['ref'], 'record_type': entry['record_type'], 'key': entry['key'],
                'resources': resource_list(inst, {path.relative_to(inst.root).as_posix(): entry['after']})})


def recover_records(inst, n=None, rebuild=False):
    """Retry pending materializations; explicit rebuild also restores missing projections."""
    changed = []
    nodes_by_ref = {node_ref(node['id']): node for node in ([n] if n else inst.nodes())}
    for identity, entry in list(journal(inst).state['records'].items()):
        owner = nodes_by_ref.get(identity.split(':jobs:')[0].split(':handback:')[0])
        if owner and (rebuild or not entry.get('applied')):
            apply_record(inst, owner, entry, rebuild=rebuild)
            changed.append(entry['ref'])
    return changed


def backfill_records(inst):
    """Label existing snapshots as reconstructed sources, never invented past events."""
    from . import jobs
    store = journal(inst)
    for n in inst.nodes():
        ledger = n['dir'] / jobs.JOBS_FILE
        if ledger.exists() and jobs.read_jobs(n) is not None and record_identity(n, 'jobs', 'ledger') not in store.state['records']:
            write_record(inst, n, 'jobs', 'ledger', ledger.read_text(), origin='reconstructed')
        for row in jobs.read_jobs(n) or []:
            path = jobs.handback_file(n, row)
            key = jobs.job_key(row)
            if path.exists() and record_identity(n, 'handback', key) not in store.state['records']:
                write_record(inst, n, 'handback', key, path.read_text(), origin='reconstructed')


_DISPATCHING_LANDINGS = False


def dispatch_landings(inst):
    """Idempotent task dependency application; pending event ids survive a failed call."""
    global _DISPATCHING_LANDINGS
    if _DISPATCHING_LANDINGS or not journal(inst).state['pending_landings']:
        return 0
    import importlib
    try:
        tasks = importlib.import_module('.tasks', __package__)
    except ModuleNotFoundError as error:
        if error.name != __package__ + '.tasks':
            raise
        return 0
    reconcile_dependencies = getattr(tasks, 'reconcile_dependencies', None)
    if reconcile_dependencies is None:
        return 0  # an older tasks capability keeps its durable pending events
    _DISPATCHING_LANDINGS = True
    try:
        owners = {node_ref(n['id']): n['id'] for n in inst.nodes()}
        count = 0
        for ref, owner in list(journal(inst).state['pending_landings'].items()):
            if owner not in owners:
                continue
            reconcile_dependencies(inst, changed_node=owners[owner], event_id=ref)
            append(inst, owners[owner], 'fact', uuid.uuid4().hex,
                   {'fact_type': 'dependencies_reconciled', 'job_key': None,
                    'at': inst.now_dt().isoformat(timespec='microseconds'), 'origin': 'observed',
                    'evidence': {'ref': ref}})
            count += 1
        return count
    finally:
        _DISPATCHING_LANDINGS = False


class node_write:
    """Short native state-writer scope, including callers bypassing CLI dispatch."""
    def __init__(self, directory, command):
        root = instance.instance_above(directory)
        if root is None:
            raise ValueError('native event writer is outside an instance')
        self.inst = instance.Instance(root)
        n = next((n for n in self.inst.nodes() if n['dir'] == directory), None)
        if n is None:
            raise ValueError('native event writer has no node')
        self.skip = any(op.inst.root == root for op in _CURRENT)
        self.lock = instance.brain_lock(self.inst)
        self.operation = Command(root, command=command, node=n['id'])

    def __enter__(self):
        self.lock.__enter__()
        if self.skip:
            return None
        try:
            return self.operation.__enter__()
        except BaseException:
            self.lock.__exit__(*__import__('sys').exc_info())
            raise

    def __exit__(self, *exc):
        try:
            return False if self.skip else self.operation.__exit__(*exc)
        finally:
            self.lock.__exit__(*exc)


def validate_record(data, applied=False):
    record_type, key = data.get('record_type'), data.get('key')
    if (record_type not in _RECORD_TYPES or not isinstance(key, str) or
            (record_type == 'jobs' and key != 'ledger') or
            (record_type == 'handback' and not re.fullmatch('[0-9a-f]{24}', key))):
        raise ValueError('invalid source record identity')
    if applied:
        if not re.fullmatch('[0-9a-f]{32}', data.get('record_ref', '')):
            raise ValueError('invalid applied source reference')
        return
    for field in ('object', 'after'):
        if not isinstance(data.get(field), str) or not re.fullmatch('[0-9a-f]{64}', data[field]):
            raise ValueError('invalid source record fingerprint')
    if data.get('before') is not None and not re.fullmatch('[0-9a-f]{64}', data['before']):
        raise ValueError('invalid previous source fingerprint')
    if data.get('origin') not in ('observed', 'reconstructed'):
        raise ValueError('invalid source record provenance')
