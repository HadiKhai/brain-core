"""Rebuildable SQLite aggregates, published through checked directory authority.

SQLite works on a private temporary copy. Atomic publication avoids SQLite
sidecars and never gives its pathname API authority over the instance tree.
Concurrent readers may discard a cache update; source logs remain authoritative.
"""
import contextlib
import contextvars
import json
import os
import sqlite3
import tempfile
from pathlib import Path

from . import common

_CURRENT = contextvars.ContextVar('brain_usage_databases', default=None)


class UsageCache:
    def __init__(self, inst, readonly=False):
        self.target = self.temp = self.db = None
        self.dirty = False
        self.readonly = readonly
        try:
            self.target = common.directory_target(inst.root / '.brain/usage.sqlite',
                                                  create=not readonly, private=True)
            self.target.__enter__()
            self.generation = self.target.generation(self.target.stat())
            self.temp = tempfile.TemporaryDirectory(prefix='brain-usage-')
            self.path = Path(self.temp.name) / 'usage.sqlite'
            if self.target.stat():
                if self.target.stat().st_size > 64 * 1024 * 1024:
                    raise ValueError('usage cache exceeds its bound')
                self.path.write_bytes(self.target.read_bytes())
            try:
                self.open()
            except sqlite3.DatabaseError:
                if self.db:
                    self.db.close()
                self.path.unlink(missing_ok=True)
                self.open()
        except (OSError, ValueError, sqlite3.Error, SystemExit):
            self.close(publish=False)

    def open(self):
        self.db = sqlite3.connect(str(self.path))
        self.db.execute('PRAGMA journal_mode=MEMORY')
        self.db.execute('PRAGMA secure_delete=ON')
        self.db.execute('CREATE TABLE IF NOT EXISTS aggregates (key TEXT PRIMARY KEY, value TEXT NOT NULL)')

    def get(self, key):
        try:
            row = self.db.execute('SELECT value FROM aggregates WHERE key=?', (key,)).fetchone() if self.db else None
            return json.loads(row[0]) if row else None
        except (sqlite3.Error, ValueError, TypeError):
            return None

    def put(self, key, value):
        if self.db is None or self.readonly:
            return
        try:
            self.db.execute('INSERT OR REPLACE INTO aggregates VALUES (?, ?)',
                            (key, json.dumps(value, separators=(',', ':'))))
            self.dirty = True
        except (sqlite3.Error, ValueError, TypeError):
            pass

    def publish(self):
        self.db.commit()
        data = self.path.read_bytes()
        target = self.target
        target.check()
        if target.generation(target.stat()) != self.generation:
            return  # another reader published first; never overwrite its generation
        name = '.usage-' + os.urandom(12).hex()
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=target.fd)
        with os.fdopen(fd, 'wb') as stream:
            st = os.fstat(stream.fileno())
            identity = (st.st_dev, st.st_ino)
            try:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
                staged = os.stat(name, dir_fd=target.fd, follow_symlinks=False)
                if (staged.st_dev, staged.st_ino) != identity or staged.st_nlink != 1:
                    raise OSError("cache staging file changed")
                target.check()
                if target.generation(target.stat()) != self.generation:
                    return
                os.replace(name, target.path.name, src_dir_fd=target.fd, dst_dir_fd=target.fd)
                os.fsync(target.fd)
            finally:
                target.unlink_owned(name, identity)

    def close(self, publish=True):
        try:
            if publish and self.db and self.dirty and not self.readonly:
                self.publish()
        except (OSError, ValueError, sqlite3.Error, SystemExit):
            pass  # optional derived state cannot make an accounting read fail
        finally:
            if self.db:
                try:
                    self.db.close()
                except sqlite3.Error:
                    pass
                self.db = None
            if self.temp:
                try:
                    self.temp.cleanup()
                except OSError:
                    pass
                self.temp = None
            if self.target:
                try:
                    self.target.__exit__(None, None, None)
                except OSError:
                    pass
                self.target = None


@contextlib.contextmanager
def batch():
    if _CURRENT.get() is not None:
        yield
        return
    caches = {}
    token = _CURRENT.set(caches)
    try:
        yield
    finally:
        _CURRENT.reset(token)
        for cache in caches.values():
            cache.close()


@contextlib.contextmanager
def database(inst, readonly=False):
    caches = _CURRENT.get()
    if caches is None:
        cache = UsageCache(inst, readonly)
        try:
            yield cache
        finally:
            cache.close()
    else:
        key = (str(inst.root), readonly)
        if key not in caches:
            caches[key] = UsageCache(inst, readonly)
        yield caches[key]
