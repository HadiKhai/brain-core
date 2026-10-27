"""Node-local process gates, independent of configurable machine admission."""
import contextlib
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import signal
import sys

from . import common as _m_common
from . import nodes as _m_nodes


GATE_ENV = ('BRAIN_GATE_FD', 'BRAIN_GATE_PATH', 'BRAIN_GATE_NODE')


def gate_path(root, node):
    # Only a resolved node id defines identity; display labels never form paths.
    return root / '.brain/gates' / (hashlib.sha256(node.encode()).hexdigest() + '.lock')


def gate_node(inst, ref=None):
    if ref is not None:
        return inst.node(ref)
    cwd = Path.cwd().resolve()
    matches = []
    for n in inst.nodes():
        local = n['local']
        places = [n['dir'], n['home_path'], local.get('workbench'),
                  *[w.get('path') for w in local.get('worktrees', {}).values()]]
        depths = [len(p.parts) for p in (Path(x).expanduser().resolve() for x in places if x)
                  if p == cwd or p in cwd.parents]
        if depths:
            matches.append((max(depths), n))
    if matches:
        depth = max(d for d, _ in matches)
        nearest = {n['id']: n for d, n in matches if d == depth}
        if len(nearest) == 1:
            return next(iter(nearest.values()))
    _m_common.die('gate-lock: cannot identify one node from this folder; use --node NODE')


def gate_inherited_fd(root, env, node):
    if not any(key in env for key in GATE_ENV):
        return None
    path = gate_path(root, node)
    if env.get('BRAIN_GATE_PATH') != str(path) or env.get('BRAIN_GATE_NODE') != node:
        _m_common.die('gate-lock: inherited node/path mismatch; use one node per outer wrapper')
    check_fd = None
    try:
        candidate = int(env.get('BRAIN_GATE_FD', ''))
        held = os.fstat(candidate)
        target = path.lstat()
        if (candidate < 3 or held.st_nlink != 1 or not stat.S_ISREG(held.st_mode) or
                not stat.S_ISREG(target.st_mode) or
                (held.st_dev, held.st_ino) != (target.st_dev, target.st_ino)):
            raise ValueError('stale descriptor')
        # An unlocked FD is not inherited ownership. An independent open must be
        # excluded, while the inherited open file description must own the lock.
        check_fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            fcntl.flock(check_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            raise ValueError('descriptor no longer locked')
        fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return candidate
    except (OSError, ValueError):
        _m_common.die('gate-lock: inherited descriptor closed, stale or unlocked; keep one outer wrapper around the complete gate')
    finally:
        if check_fd is not None:
            os.close(check_fd)


def gate_acquire(inst, node, env, label):
    path = gate_path(inst.root, node)
    _m_common.safe_target(path)
    inherited = gate_inherited_fd(inst.root, env, node)
    if inherited is not None:
        return inherited, False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        opened = os.fstat(fd)
        if opened.st_nlink != 1 or not stat.S_ISREG(opened.st_mode):
            _m_common.die('gate-lock: linked lock file')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                holder = json.loads(os.pread(fd, 4096, 0))
                if not isinstance(holder, dict):
                    holder = {}
            except (OSError, ValueError):
                holder = {}
            print('waiting for gate held by ' + str(holder.get('label', 'unknown')) +
                  ' since ' + str(holder.get('started', 'unknown')), file=sys.stderr, flush=True)
            fcntl.flock(fd, fcntl.LOCK_EX)
        target = path.lstat()
        if (not stat.S_ISREG(target.st_mode) or
                (opened.st_dev, opened.st_ino) != (target.st_dev, target.st_ino) or
                os.fstat(fd).st_nlink != 1):
            _m_common.die('gate-lock: lock path changed while waiting')
        metadata = json.dumps({'node': node, 'label': label, 'started': inst.now(), 'pid': os.getpid()}) + '\n'
        os.fchmod(fd, 0o600)
        os.ftruncate(fd, 0)
        os.pwrite(fd, metadata.encode(), 0)
        return fd, True
    except BaseException:
        os.close(fd)
        raise


def gate_environment(inst, node, env, fd):
    return {**env, 'BRAIN_GATE_FD': str(fd), 'BRAIN_GATE_PATH': str(gate_path(inst.root, node)),
            'BRAIN_GATE_NODE': node}


@contextlib.contextmanager
def node_gate(inst, n, env, label):
    fd, owned = gate_acquire(inst, n['id'], env, label)
    try:
        yield gate_environment(inst, n['id'], env, fd)
    finally:
        if owned:
            # Close, never unlock: a surviving child retains the same ownership.
            os.close(fd)


def cmd_gate_lock(a):
    command = a.command[1:] if a.command[:1] == ['--'] else a.command
    if not command:
        _m_common.die('gate-lock requires -- CMD [ARGS...]')
    inst = _m_nodes.load()
    n = gate_node(inst, getattr(a, 'node', None))
    fd, _ = gate_acquire(inst, n['id'], os.environ, a.label or command[0])
    os.set_inheritable(fd, True)
    env = gate_environment(inst, n['id'], os.environ, fd)
    sys.stdout.flush()
    sys.stderr.flush()
    for name in ('SIGPIPE', 'SIGXFZ', 'SIGXFSZ'):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), signal.SIG_DFL)
    try:
        os.execvpe(command[0], command, env)
    except OSError as e:
        os.close(fd)
        print('gate-lock: ' + str(e), file=sys.stderr)
        raise SystemExit(127 if e.errno == errno.ENOENT else 126)
