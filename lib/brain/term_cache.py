"""Private, rebuildable never-name verdicts. No term, label or pathname is stored.

Only an explicit check --fix publishes. Read commands can consume a warm cache
without changing their input tree. Cache failures always fall back to scanning.
"""
import hashlib
import json
import os
import re
import stat
import sys
import unicodedata
from pathlib import Path

from . import common as _m_common

SCANNER_VERSION = 1
MAX_BYTES = 64 << 20


def hex_key(value, lengths=(64,)):
    return isinstance(value, str) and len(value) in lengths and re.fullmatch('[0-9a-f]+', value) is not None


def valid_cache(value):
    """Reject the entire artifact on unknown fields or nonprivate schema values."""
    def fields(item, names):
        return isinstance(item, dict) and item.keys() == set(names)
    if not fields(value, ('version', 'nodes')) or not hex_key(value['version']) or not isinstance(value['nodes'], dict):
        return False
    for node_key, node in value['nodes'].items():
        if (not hex_key(node_key) or not fields(node, ('words', 'contexts'))
                or not hex_key(node['words']) or not isinstance(node['contexts'], dict)):
            return False
        for context_key, context in node['contexts'].items():
            if (not hex_key(context_key) or not fields(context, ('blobs', 'files', 'stores'))
                    or any(not isinstance(context[key], dict) for key in ('blobs', 'files', 'stores'))):
                return False
            blobs = context['blobs']
            if any(not hex_key(oid, (40, 64)) or type(clean) is not bool for oid, clean in blobs.items()):
                return False
            for path_key, record in context['files'].items():
                if (not hex_key(path_key) or not isinstance(record, list) or len(record) != 2
                        or not isinstance(record[0], list) or len(record[0]) != 9
                        or any(type(number) is not int or not -(1 << 127) <= number < (1 << 127)
                               for number in record[0])
                        or not hex_key(record[1], (40, 64)) or record[1] not in blobs):
                    return False
            for store_key, records in context['stores'].items():
                if (not hex_key(store_key) or not isinstance(records, dict)
                        or any(not hex_key(oid, (40, 64)) or oid not in blobs or not hex_key(stamp)
                               for oid, stamp in records.items())):
                    return False
    return True


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()


def identity(value):
    return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns,
            value.st_ctime_ns, value.st_mode, value.st_uid, value.st_gid, value.st_nlink]


def accessible(path, mode):
    if os.access in os.supports_effective_ids:
        return os.access(path, mode, effective_ids=True)
    if os.getuid() != os.geteuid() or os.getgid() != os.getegid():
        return False
    return os.access(path, mode)


def scanner_version():
    # Implementation changes invalidate even when the explicit format stays put.
    return digest([SCANNER_VERSION, unicodedata.unidata_version, list(sys.version_info[:3]),
                   hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                   hashlib.sha256(Path(__file__).with_name('guard.py').read_bytes()).hexdigest(),
                   hashlib.sha256(Path(__file__).with_name('common.py').read_bytes()).hexdigest(),
                   os.geteuid(), os.getegid()])


class VerdictCache:
    def __init__(self, root, writable=False):
        self.target = None
        self.data = {'version': None, 'nodes': {}}
        self.dirty = False
        self.writable = writable
        try:
            self.data['version'] = scanner_version()
            # DirectoryTarget retains checked physical authority through publish.
            path = root / '.brain/term-scan-cache.json'
            self.target = _m_common.directory_target(path, create=writable, private=True)
            self.target.__enter__()
            st = self.target.stat()
            self.generation = self.target.generation(st)
            if st:
                if st.st_uid != os.geteuid() or st.st_mode & 0o077 or st.st_size > MAX_BYTES:
                    raise ValueError('nonprivate or oversized term cache')
                try:
                    old = json.loads(self.target.read_bytes())
                except (ValueError, TypeError, RecursionError):
                    old = None
                if valid_cache(old) and old['version'] == self.data['version']:
                    self.data = old
                else:
                    self.dirty = True
            if not writable:
                self.close()
        except (OSError, ValueError, TypeError, RecursionError, SystemExit):
            self.close(publish=False)

    def policy(self, node, words, principals, kind):
        key, word_hash = digest(str(node)), digest(words)
        record = self.data['nodes'].get(key)
        if (not isinstance(record, dict) or record.get('words') != word_hash
                or not isinstance(record.get('contexts'), dict)):
            record = {'words': word_hash, 'contexts': {}}
            self.data['nodes'][key] = record
            self.dirty = True
        contexts = record.setdefault('contexts', {})
        context = digest([principals if kind else [], kind])
        value = contexts.get(context)
        if not isinstance(value, dict) or any(not isinstance(value.get(k), dict) for k in ('blobs', 'files', 'stores')):
            value = {'blobs': {}, 'files': {}, 'stores': {}}
            contexts[context] = value
            self.dirty = True
        return Policy(self, value)

    def forget(self, node):
        if self.data['nodes'].pop(digest(str(node)), None) is not None:
            self.dirty = True

    def close(self, publish=True):
        target, self.target = self.target, None
        if target is None:
            return
        try:
            if publish and self.writable and self.dirty:
                data = json.dumps(self.data, separators=(',', ':'))
                if len(data) <= MAX_BYTES:
                    target.atomic_write(data + '\n', expected=self.generation)
        except (OSError, ValueError, TypeError, SystemExit):
            pass  # derived data never prevents a complete source scan
        finally:
            target.__exit__(None, None, None)


class Policy:
    def __init__(self, owner, data):
        self.owner, self.data = owner, data

    def file_clean(self, path, st):
        old = self.data['files'].get(self.file_key(path))
        return (isinstance(old, list) and len(old) == 2 and isinstance(old[1], str) and old[0] == identity(st)
                and self.data['blobs'].get(old[1]) is True)

    def remember_file(self, path, st, data, clean, oid=None):
        # Canonical SHA-1 Git blob identity, also for dirty/untracked files.
        oid = oid or hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        self.data['blobs'][oid] = bool(clean)
        self.data['files'][self.file_key(path)] = [identity(st), oid]
        self.owner.dirty = True

    @staticmethod
    def file_key(path):
        path = os.fspath(path)
        if not os.path.isabs(path):
            path = os.path.abspath(path)
        return hashlib.sha256(os.fsencode(path)).hexdigest()

    def blob_clean(self, oid, storage):
        stamp = storage.blob_stamp(oid) if storage is not None else None
        if stamp is None:
            return False
        old = self.data['stores'].get(storage.key)
        reusable = (isinstance(old, dict) and old.get(oid) == stamp
                    and self.data['blobs'].get(oid) is True)
        storage.reused |= reusable
        return reusable

    def remember_blob(self, oid, clean, storage):
        self.data['blobs'][oid] = bool(clean)
        stamp = storage.blob_stamp(oid) if storage is not None else None
        if stamp is not None:
            old = self.data['stores'].get(storage.key)
            if not isinstance(old, dict):
                old = {}
                self.data['stores'][storage.key] = old
            old[oid] = stamp
        self.owner.dirty = True


class ObjectStorage:
    """Bind a successful complete blob read to unchanged, readable Git storage.

    A header-only cat-file probe misses corrupt compressed bodies. Fingerprint
    the object files themselves instead, and refuse alternate/replaced stores.
    """
    def __init__(self, directory):
        self.path = Path(directory)
        self.key = digest(str(self.path))
        self.reused = False
        self.stamp = self.fingerprint()
        self.blobs = {}

    def fingerprint(self):
        try:
            overrides = ('GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_REPLACE_REF_BASE',
                         'GIT_CONFIG_PARAMETERS', 'GIT_CONFIG_COUNT', 'GIT_SHALLOW_FILE')
            if any(os.environ.get(key) for key in overrides):
                return None
            objects = self.path / 'objects'
            if os.path.lexists(self.path / 'reftable'):
                return None  # replacement refs are not represented by refs/replace
            if os.path.lexists(objects / 'info/alternates') or os.path.lexists(self.path / 'refs/replace'):
                return None
            packed = self.path / 'packed-refs'
            if os.path.lexists(packed):
                if packed.is_symlink() or not packed.is_file() or b'refs/replace/' in packed.read_bytes():
                    return None
            rows = []
            # Parent access/ACL changes invalidate the proof too.
            for path in [self.path, *self.path.parents]:
                st = path.stat()
                if not accessible(path, os.R_OK | os.X_OK):
                    return None
                # Directory entry churn in an ancestor is irrelevant; access is
                # checked afresh and object-directory churn is bound below.
                rows.append([str(path), [st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_gid]])
            for name in ('config', 'packed-refs'):
                path = self.path / name
                rows.append([name, identity(path.stat()) if path.exists() else None])
            def walk(directory):
                before = directory.stat()
                if directory.is_symlink() or not accessible(directory, os.R_OK | os.X_OK):
                    raise OSError('unreadable object directory')
                rows.append([str(directory.relative_to(self.path)), identity(before)])
                with os.scandir(directory) as entries:
                    for entry in sorted(entries, key=lambda e: e.name):
                        st = entry.stat(follow_symlinks=False)
                        if stat.S_ISDIR(st.st_mode):
                            walk(Path(entry.path))
                        elif stat.S_ISREG(st.st_mode) and accessible(entry.path, os.R_OK):
                            rows.append([str(Path(entry.path).relative_to(self.path)), identity(st)])
                        else:
                            raise OSError('unreadable or linked object storage')
                if identity(directory.stat()) != identity(before):
                    raise OSError('object directory changed')
            # Loose objects added by another commit do not change existing
            # bodies. Bind each loose body separately, and packed bodies to the
            # pack/index generation that proved their successful read.
            rows.append(['objects', self.directory_identity(objects)])
            if os.path.lexists(objects / 'pack'):
                walk(objects / 'pack')
            else:
                rows.append(['pack', None])
            return digest(rows)
        except (OSError, ValueError):
            return None

    @staticmethod
    def directory_identity(path):
        st = path.stat()
        if path.is_symlink() or not accessible(path, os.R_OK | os.X_OK):
            raise OSError('unreadable object directory')
        return [st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_gid]

    def source_stamp(self, oid):
        if self.stamp is None:
            return None
        try:
            directory = self.path / 'objects' / oid[:2]
            body = directory / oid[2:]
            try:
                st = body.lstat()
            except FileNotFoundError:
                return digest(['packed', self.stamp])
            if not stat.S_ISREG(st.st_mode) or not accessible(body, os.R_OK):
                return None
            return digest(['loose', self.stamp, self.directory_identity(directory), identity(st)])
        except OSError:
            return None

    def blob_stamp(self, oid):
        if oid not in self.blobs:
            self.blobs[oid] = self.source_stamp(oid)
        return self.blobs[oid]

    def unchanged(self):
        return (self.stamp is not None and self.fingerprint() == self.stamp
                and all(self.source_stamp(oid) == old for oid, old in self.blobs.items()))
