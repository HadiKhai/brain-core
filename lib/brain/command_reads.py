"""Short-lived read snapshots, with explicit content-bound refresh reuse."""
import contextlib
import contextvars
import functools
import json
import copy

_VALUES = contextvars.ContextVar('brain_command_reads', default=None)
_SCOPED = contextvars.ContextVar('brain_scoped_reads', default=None)
_CONTENT = contextvars.ContextVar('brain_content_reads', default=None)


def memo(fn):
    @functools.wraps(fn)
    def read(*args, **kwargs):
        values = _VALUES.get()
        scoped = _SCOPED.get()
        if scoped is not None and fn in scoped:
            values = scoped[fn]
        if values is None:
            return fn(*args, **kwargs)
        # Instance/node identities are stable within one command; callers supply
        # the differing display options (merge baseline and metadata mode) too.
        def key(arg):
            if hasattr(arg, 'root'):
                return str(arg.root)
            if isinstance(arg, dict) and 'dir' in arg:
                return str(arg['dir'])
            return json.dumps(arg, sort_keys=True, default=str)
        identity = (fn, tuple(key(a) for a in args), tuple((k, key(v)) for k, v in sorted(kwargs.items())))
        if identity not in values:
            content = _CONTENT.get()
            if content is not None and fn in content[0]:
                version = content[0][fn](*args, **kwargs)
                cache_key = (identity, version)
                if cache_key not in content[1]:
                    # The reader parses exactly the captured bytes, rather than
                    # racing a second physical read under an older content key.
                    content[1][cache_key] = copy.deepcopy(fn(*args, **kwargs, _content=version))
                values[identity] = copy.deepcopy(content[1][cache_key])
            else:
                values[identity] = fn(*args, **kwargs)
        return values[identity]
    return read


@contextlib.contextmanager
def content_scope(read, fingerprint, values):
    """Reuse a pure reader accepting _content, only inside an explicit refresh."""
    token = _CONTENT.set(({getattr(read, '__wrapped__', read): fingerprint}, values))
    try:
        yield
    finally:
        _CONTENT.reset(token)


@contextlib.contextmanager
def scope(*reads):
    """Memoize only named observations; writer guards remain uncached."""
    values = dict(_SCOPED.get() or {})
    for read in reads:
        values.setdefault(getattr(read, '__wrapped__', read), {})
    token = _SCOPED.set(values)
    try:
        yield
    finally:
        _SCOPED.reset(token)


def active():
    return _VALUES.get() is not None


@contextlib.contextmanager
def snapshot():
    from . import common, instance, nodes, spend, status, verify
    if _VALUES.get() is not None:
        yield
        return
    token = _VALUES.set({})
    discovery = spend._UI_DISCOVERY.set({})
    try:
        root = nodes.try_find_root()
        keys = {(n['id'], branch): True for n in instance.Instance(root).nodes()
                for branch in n['local'].get('worktrees', {})} if root else {}
        with common.git_read_snapshot(), status.local_job_snapshot(), verify.display_cache({}, keys):
            yield
    finally:
        spend._UI_DISCOVERY.reset(discovery)
        _VALUES.reset(token)


def history_dates(inst, source, needle):
    """Introduction dates are immutable at a pinned Git head; keep only their dates."""
    from . import common, jobs, spend, usage_cache
    relative = source.relative_to(inst.root).as_posix()
    def recount():
        result = common.git('log', '--reverse', '--format=%cI', '-S', needle, '--', relative, cwd=inst.root)
        return result.stdout.splitlines() if not result.returncode else []
    if _VALUES.get() is None:
        return recount()
    head = common.git('rev-parse', '--verify', '--quiet', 'HEAD', cwd=inst.root)
    if head.returncode:
        return recount()
    key = 'history:' + jobs.digest([head.stdout.strip(), relative, needle])
    with usage_cache.database(inst, readonly=spend._SPEND_READ_ONLY.get()) as cache:
        dates = cache.get(key)
        if isinstance(dates, list) and all(isinstance(t, str) and inst.parse_time(t) for t in dates):
            return dates
        dates = recount()
        cache.put(key, dates)
        return dates
