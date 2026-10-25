"""Instance-wide process gate, independent of machine admission."""
import errno
import fcntl
import json
import os
import stat
import signal
import sys

from . import common as _m_common
from . import nodes as _m_nodes


def gate_inherited_fd(root, env):
    path = root / '.brain/gate.lock'
    inherited = env.get('BRAIN_GATE_FD', '')
    fd = None
    try:
        candidate = int(inherited)
        held = os.fstat(candidate)
        target = path.stat()
        if (candidate >= 3 and held.st_nlink == 1 and stat.S_ISREG(held.st_mode) and
                (held.st_dev, held.st_ino) == (target.st_dev, target.st_ino)):
            fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fd = candidate
    except (OSError, ValueError):
        pass
    if fd is None and env.get('BRAIN_GATE_PATH') == str(path):
        _m_common.die('gate-lock: inherited descriptor closed; keep one outer wrapper around the complete gate')
    return fd


def cmd_gate_lock(a):
    command = a.command[1:] if a.command[:1] == ['--'] else a.command
    if not command:
        _m_common.die('gate-lock requires -- CMD [ARGS...]')
    inst = _m_nodes.load()
    path = inst.root / '.brain/gate.lock'
    if _m_common.outward_links(inst.root):
        _m_common.die('gate-lock: unsafe instance links')
    path.parent.mkdir(parents=True, exist_ok=True)
    _m_common.safe_target(path)
    # Exec preserves the lock for the command's entire lifetime and forwards its
    # exit status/signals without a supervising shell. Nested wrappers share it.
    fd = gate_inherited_fd(inst.root, os.environ)
    if fd is None:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        opened = os.fstat(fd)
        if opened.st_nlink != 1 or not stat.S_ISREG(opened.st_mode):
            os.close(fd)
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
        metadata = json.dumps({'label': a.label or command[0], 'started': inst.now(), 'pid': os.getpid()}) + '\n'
        os.ftruncate(fd, 0)
        os.pwrite(fd, metadata.encode(), 0)
    os.set_inheritable(fd, True)
    env = {**os.environ, 'BRAIN_GATE_FD': str(fd), 'BRAIN_GATE_PATH': str(path)}
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
