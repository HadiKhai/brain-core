"""Count Python-owned child launches and time real Git calls without profiling."""
import json
import os
from pathlib import Path
import subprocess
import threading
import time

CURRENT = [os.environ.get('BRAIN_PANEL_TEST', '<collection>')]
_installed = False
_local = threading.local()
_streams = {}


def emit(event, **fields):
    pid = os.getpid()
    if pid not in _streams:
        path = Path(os.environ['BRAIN_PANEL_METRICS']) / ('process-%d.jsonl' % pid)
        _streams[pid] = path.open('x', buffering=1)
    _streams[pid].write(json.dumps(dict(event=event, pid=pid, test=CURRENT[0], **fields)) + '\n')


def words(args):
    return [os.fsdecode(x) for x in args] if isinstance(args, (list, tuple)) else [os.fsdecode(args)]


def is_git(args):
    return bool(args) and Path(args[0]).name == 'git'


def install():
    global _installed
    if _installed:
        return
    _installed = True
    original_init = subprocess.Popen.__init__
    original_run = subprocess.run
    original_wait = subprocess.Popen.wait
    original_communicate = subprocess.Popen.communicate
    probe = str(Path(__file__).resolve().parent)

    def init(child, args, *pos, **kwargs):
        command = words(args)
        # Scenario constructs its own environment. Propagate attribution only
        # at the child boundary, so tags cannot invalidate fixture cache keys.
        if len(pos) > 9:
            raise RuntimeError('panel instrumentation needs a keyword child environment')
        env = dict(kwargs.get('env') if kwargs.get('env') is not None else os.environ)
        env.update(BRAIN_PANEL_METRICS=os.environ['BRAIN_PANEL_METRICS'],
                   BRAIN_PANEL_TEST=CURRENT[0])
        # Shells and shebang tools must pass the same probe to their Python
        # descendants. Deduplicate it so recursion cannot change fingerprints.
        paths = [x for x in env.get('PYTHONPATH', '').split(os.pathsep) if x and x != probe]
        env['PYTHONPATH'] = os.pathsep.join([probe, *paths])
        kwargs['env'] = env
        child._panel_started = time.monotonic()
        child._panel_wall = time.time()
        child._panel_git = is_git(command) and not getattr(_local, 'git_run', False)
        child._panel_done = False
        try:
            result = original_init(child, args, *pos, **kwargs)
        except BaseException:
            emit('spawn_failed', command=command)
            raise
        emit('spawn', command=command, child_pid=child.pid,
             git=is_git(command), brain=any(x.endswith('/bin/brain') for x in command))
        return result

    def finished(child):
        if child._panel_git and not child._panel_done and child.returncode is not None:
            child._panel_done = True
            end = time.monotonic()
            emit('git_time', seconds=end - child._panel_started,
                 start=child._panel_started, end=end,
                 wall_start=child._panel_wall,
                 wall_end=child._panel_wall + end - child._panel_started,
                 kind='direct_popen_lifetime', returncode=child.returncode)

    def wait(child, *args, **kwargs):
        try:
            return original_wait(child, *args, **kwargs)
        finally:
            finished(child)

    def communicate(child, *args, **kwargs):
        try:
            return original_communicate(child, *args, **kwargs)
        finally:
            finished(child)

    def run(*args, **kwargs):
        command = words(args[0] if args else kwargs['args'])
        if not is_git(command):
            return original_run(*args, **kwargs)
        previous = getattr(_local, 'git_run', False)
        _local.git_run = True
        start = time.monotonic()
        wall_start = time.time()
        try:
            return original_run(*args, **kwargs)
        finally:
            _local.git_run = previous
            end = time.monotonic()
            emit('git_time', seconds=end - start, start=start, end=end,
                 wall_start=wall_start, wall_end=wall_start + end - start,
                 kind='subprocess_run', command=command)

    subprocess.Popen.__init__ = init
    subprocess.Popen.wait = wait
    subprocess.Popen.communicate = communicate
    subprocess.run = run
