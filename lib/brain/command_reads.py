"""Short-lived, read-command snapshots. Never retained across mutations."""
import contextlib
import contextvars
import functools
import json

_VALUES = contextvars.ContextVar('brain_command_reads', default=None)


def memo(fn):
    @functools.wraps(fn)
    def read(*args, **kwargs):
        values = _VALUES.get()
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
            values[identity] = fn(*args, **kwargs)
        return values[identity]
    return read


@contextlib.contextmanager
def snapshot():
    from . import common, instance, nodes, spend, status, verify
    if _VALUES.get() is not None:
        yield
        return
    token = _VALUES.set({})
    discovery = spend._UI_DISCOVERY.set({})
    root = nodes.try_find_root()
    keys = {(n['id'], branch): True for n in instance.Instance(root).nodes()
            for branch in n['local'].get('worktrees', {})} if root else {}
    try:
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
