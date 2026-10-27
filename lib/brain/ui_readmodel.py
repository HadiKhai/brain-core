"""Detached dashboard observations and versioned private derived data.

A projection is context-local, instance-bound and never an action receipt.
Private caches are optional; their absence cannot prevent serving a partial view.
"""
import contextlib
import copy
from contextvars import ContextVar
import hashlib
import functools
import inspect
import json
import os
import stat

from . import common
from . import history


_PROJECTION = ContextVar('dashboard_projection', default=None)
_USAGE_CACHE = ContextVar('dashboard_usage_cache', default=None)
_CACHE_WRITES = ContextVar('dashboard_cache_writes', default=True)


@contextlib.contextmanager
def cache_writes(enabled):
    token = _CACHE_WRITES.set(bool(enabled) and _CACHE_WRITES.get())
    try:
        yield
    finally:
        _CACHE_WRITES.reset(token)


def projection(root):
    current = _PROJECTION.get()
    return current if current is not None and current.root == root.resolve() else None


class Observation:
    def __init__(self, root):
        self.root = root.resolve()
        self.records = {}

    def store(self, path, reader, select=None):
        key = str(path)
        if key not in self.records:
            self.records[key] = reader()
        value = self.records[key]
        return copy.deepcopy(select(value) if select is not None else value)


@contextlib.contextmanager
def observe(inst):
    current = projection(inst.root)
    token = _PROJECTION.set(current or Observation(inst.root))
    try:
        yield _PROJECTION.get()
    finally:
        _PROJECTION.reset(token)


def episode(inst, parts, size=24):
    """A prospective display ID; recorded receipts always take precedence."""
    if projection(inst.root):
        return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:size]
    return os.urandom(size // 2).hex()


class ReadModel:
    VERSION = 1
    MAX_BYTES = 32 * 1024 * 1024

    def __init__(self, inst, code):
        self.inst, self.code = inst, code
        self.folder = inst.root / '.brain/ui-cache'
        self.writable = {'private', 'create'} <= set(inspect.signature(common.atomic_write).parameters)

    def read(self, name):
        path = self.folder / (name + '.json')
        try:
            # Never follow a replaced directory or file to read private display data.
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd) as stream:
                info = os.fstat(stream.fileno())
                expected = self.inst.root.resolve() / '.brain/ui-cache' / path.name
                if (history.history_fd_path(stream.fileno()) != str(expected) or
                        not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                        info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > self.MAX_BYTES):
                    return None
                text = stream.read(self.MAX_BYTES + 1)
                return json.loads(text) if len(text.encode()) <= self.MAX_BYTES else None
        except (OSError, ValueError):
            return None

    def write(self, name, value):
        if not self.writable or not _CACHE_WRITES.get():
            return False
        text = json.dumps(value, separators=(',', ':'), sort_keys=True)
        if len(text.encode()) > self.MAX_BYTES:
            return False
        try:
            common.atomic_write(self.folder / (name + '.json'), text, private=True, create=True)
            return True
        except (OSError, ValueError, SystemExit):
            return False  # derived-data failure never discards a usable reading

    def binding(self, signature):
        return hashlib.sha256(json.dumps([self.VERSION, self.code, str(self.inst.root.resolve()), signature],
                                         sort_keys=True, default=str).encode()).hexdigest()

    def metadata(self, signature):
        record = self.read('metadata')
        if (isinstance(record, dict) and record.get('binding') == self.binding(signature)
                and isinstance(record.get('state'), dict)
                and isinstance(record['state'].get('brain'), dict)
                and isinstance(record['state'].get('nodes'), list)):
            return record['state']
        return None

    def save_metadata(self, signature, state):
        # Usage, meters and server permissions are independently refreshed.
        state = {k: v for k, v in state.items() if k not in ('spend', 'machine', 'review')}
        return self.write('metadata', {'binding': self.binding(signature), 'state': state})

    @contextlib.contextmanager
    def usage_scope(self):
        from . import spend
        token = _USAGE_CACHE.set(self)
        try:
            with spend.spend_read_only():
                yield
        finally:
            _USAGE_CACHE.reset(token)


def usage_cache():
    return _USAGE_CACHE.get()


def read_view(function):
    @functools.wraps(function)
    def build(inst, *args, **kwargs):
        from . import spend
        with observe(inst), spend.spend_read_only(), ReadModel(inst, '').usage_scope():
            return function(inst, *args, **kwargs)
    return build
