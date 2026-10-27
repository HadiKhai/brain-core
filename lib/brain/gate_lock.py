"""Node-local process gates, independent of configurable machine admission."""
import contextlib
import contextvars
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import signal
import subprocess
import sys
import threading
import time

from . import common as _m_common
from . import instance as _m_instance

_m_nodes = _m_common.LazyModule(__package__, 'nodes')
_m_machine = _m_common.LazyModule(__package__, 'machine')
_m_core_releases = _m_common.LazyModule(__package__, 'core_releases')


GATE_ENV = ('BRAIN_GATE_FD', 'BRAIN_GATE_PATH', 'BRAIN_GATE_NODE')
ACTIVE_GATE = contextvars.ContextVar('brain_node_gate', default=None)
_PHASE_LOCK = threading.RLock()


def gate_metadata(fd):
    try:
        row = json.loads(os.pread(fd, 4096, 0))
        return row if isinstance(row, dict) else {}
    except (OSError, ValueError):
        return {}


def gate_stale_holder(row):
    pid = row.get('pid')
    if type(pid) is not int or pid <= 0:
        return False
    stamp = _m_machine.process_stamp(pid)
    return stamp is None or (stamp != 'unknown' and row.get('owner_stamp') not in (None, 'unknown', stamp))


def gate_abandoned(row):
    """A dead holder that never marked its own close may have crashed."""
    return row.get('phase') != 'released' and gate_stale_holder(row)


def gate_waiting(row, node):
    who = row.get('phase_owner')
    return (row.get('node') == node and row.get('phase') == 'waiting-for-admission'
            and type(row.get('pid')) is int and row['pid'] > 0
            and isinstance(row.get('owner_stamp'), str) and row['owner_stamp'] not in ('', 'unknown')
            and _m_machine.process_stamp(row['pid']) == row['owner_stamp']
            and isinstance(who, list) and len(who) == 2 and type(who[0]) is int and who[0] > 0
            and isinstance(who[1], str) and who[1] not in ('', 'unknown')
            and _m_machine.process_stamp(who[0]) == who[1])


def gate_write_metadata(fd, row):
    data = (json.dumps(row) + '\n').encode()
    if len(data) > 4096:
        _m_common.die('gate-lock: holder metadata too large')
    if os.pwrite(fd, data, 0) != len(data):
        _m_common.die('gate-lock: incomplete holder metadata')
    os.ftruncate(fd, len(data))


@contextlib.contextmanager
def gate_state_lock(inst):
    """Release a newly acquired physical lock even inside a suspended CLI scope."""
    with _PHASE_LOCK:
        already_held = _m_instance._LOCK['fh'] is not None
        try:
            # Gates are identified by root alone, like gate_path.
            with _m_instance.brain_lock(inst.root):
                yield
        finally:
            if not already_held:
                _m_instance.unlock()


@contextlib.contextmanager
def gate_admission(inst, node, admitted='running'):
    """Only the first admission can be idle; later lane waits retain running ownership.

    The gate-lock wrapper admits before its command exists, so it passes
    `admitted='starting'` and leaves the command its own first admission."""
    active = ACTIVE_GATE.get()
    env = active[2] if active is not None and active[:2] == (inst.root, node) else os.environ
    fd = gate_inherited_fd(inst.root, env, node)
    if fd is None:
        yield
        return
    # Serialize phase changes with upgrade's instance lock. Never hold it while
    # waiting for admission. A running phase is sticky across nested commands.
    with gate_state_lock(inst):
        row = gate_metadata(fd)
        if row.get('phase') == 'starting':
            row.update(phase='waiting-for-admission',
                       phase_owner=[os.getpid(), _m_machine.process_stamp(os.getpid())])
            gate_write_metadata(fd, row)
        elif row.get('phase') == 'waiting-for-admission':
            # Two inherited admission callers cannot both vouch for an idle gate.
            row.update(phase='running')
            gate_write_metadata(fd, row)
    try:
        yield
    finally:
        with gate_state_lock(inst):
            row = gate_metadata(fd)
            if row.get('phase') == 'waiting-for-admission':
                row.update(phase=admitted)
                row.pop('phase_owner', None)
                gate_write_metadata(fd, row)


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
            holder = gate_metadata(fd)
            print('waiting for gate held by ' + str(holder.get('label', 'unknown')) +
                  ' since ' + str(holder.get('started', 'unknown')), file=sys.stderr, flush=True)
            if gate_stale_holder(holder):
                print('gate-lock: stale holder pid ' + str(holder['pid']) +
                      '; descriptor still held; waiting', file=sys.stderr, flush=True)
            fcntl.flock(fd, fcntl.LOCK_EX)
        target = path.lstat()
        if (not stat.S_ISREG(target.st_mode) or
                (opened.st_dev, opened.st_ino) != (target.st_dev, target.st_ino) or
                os.fstat(fd).st_nlink != 1):
            _m_common.die('gate-lock: lock path changed while waiting')
        with gate_state_lock(inst):
            holder = gate_metadata(fd)
            if gate_abandoned(holder):
                print('gate-lock: stale holder pid ' + str(holder['pid']) +
                      '; reclaiming released gate', file=sys.stderr, flush=True)
            os.fchmod(fd, 0o600)
            gate_write_metadata(fd, {'node': node, 'label': label, 'started': inst.now(),
                                    'pid': os.getpid(), 'owner_stamp': _m_machine.process_stamp(os.getpid()),
                                    'phase': 'starting'})
        return fd, True
    except BaseException:
        os.close(fd)
        raise


def gate_environment(inst, node, env, fd):
    env = {**env, 'BRAIN_GATE_FD': str(fd), 'BRAIN_GATE_PATH': str(gate_path(inst.root, node)),
           'BRAIN_GATE_NODE': node}
    if _m_common.STABLE_ENTRY_POINT != _m_common.ENTRY_POINT:
        env['BRAIN_CORE_RELEASE'] = str(_m_common.CORE)  # nested commands stay on this gate's release
    return env


def gate_close(fd):
    """Mark an owned close before dropping our reference, never unlock a child."""
    try:
        row = gate_metadata(fd)
        if row.get('pid') == os.getpid():
            row.update(phase='released')
            row.pop('phase_owner', None)
            gate_write_metadata(fd, row)
    except (OSError, SystemExit):
        pass  # Best effort: failed publication must never leak the descriptor.
    finally:
        os.close(fd)


@contextlib.contextmanager
def node_gate(inst, n, env, label):
    fd, owned = gate_acquire(inst, n['id'], env, label)
    try:
        env = gate_environment(inst, n['id'], env, fd)
        active = ACTIVE_GATE.set((inst.root, n['id'], env))
        try:
            yield env
        finally:
            ACTIVE_GATE.reset(active)
    finally:
        if owned:
            gate_close(fd)


def gate_wrapper_admission(inst, node, env):
    """Wait for heavy admission holding the gate, in the phase upgrade treats as idle.

    The lock comes first so same-node waiters keep their flock order; admitting
    first would let a later arrival overtake an earlier one once load clears."""
    active = ACTIVE_GATE.set((inst.root, node, env))
    try:
        with contextlib.redirect_stdout(sys.stderr):  # the command owns stdout
            with _m_machine.machine_run('heavy', inst, node) as token:
                _m_machine.machine_wait(inst=inst, node=node, token=token, gate_phase='starting')
    finally:
        ACTIVE_GATE.reset(active)


def cmd_gate_lock(a):
    command = a.command[1:] if a.command[:1] == ['--'] else a.command
    if not command:
        _m_common.die('gate-lock requires -- CMD [ARGS...]')
    inst = _m_nodes.load()
    n = gate_node(inst, getattr(a, 'node', None))
    fd, owned = gate_acquire(inst, n['id'], os.environ, a.label or command[0])
    child = None
    pending = []
    delayed = {}
    handlers = {}

    def forward(signum, frame):
        if child is None:
            pending.append(signum)
        else:
            try:
                child.send_signal(signum)
            except ProcessLookupError:
                pass

    def defer(signum, frame):
        # A terminal signal already reaches the child in our process group.
        # Give it time to exit before forwarding a wrapper-only signal once.
        delayed.setdefault(signum, time.monotonic() + 2.0)

    try:
        os.set_inheritable(fd, True)
        _m_core_releases.hold_pin(_m_common.CORE)  # the command retains this release through supervision/exec
        env = gate_environment(inst, n['id'], os.environ, fd)
        if owned:  # nested wrappers retain the outer owner's admission
            gate_wrapper_admission(inst, n['id'], env)
        sys.stdout.flush()
        sys.stderr.flush()
        if not owned:
            # The outer owner supervises the complete chain. Preserve exec for
            # nested wrappers so they cannot outlive their actual command.
            for name in ('SIGPIPE', 'SIGXFZ', 'SIGXFSZ'):
                if hasattr(signal, name):
                    signal.signal(getattr(signal, name), signal.SIG_DFL)
            os.execvpe(command[0], command, env)
        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGQUIT, signal.SIGTERM,
                       signal.SIGUSR1, signal.SIGUSR2, signal.SIGALRM, signal.SIGVTALRM,
                       signal.SIGPROF, signal.SIGWINCH):
            if signal.getsignal(signum) == signal.SIG_IGN:
                continue  # Preserve nohup and background-job dispositions across exec.
            handler = defer if signum in (signal.SIGINT, signal.SIGQUIT) else forward
            handlers[signum] = signal.signal(signum, handler)
        child = subprocess.Popen(command, env=env, close_fds=False)
        for signum in pending:
            forward(signum, None)
        while True:
            try:
                result = child.wait(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                for signum, deadline in list(delayed.items()):
                    if time.monotonic() >= deadline:
                        del delayed[signum]
                        forward(signum, None)
    except OSError as e:
        print('gate-lock: ' + str(e), file=sys.stderr)
        raise SystemExit(127 if e.errno == errno.ENOENT else 126)
    finally:
        if owned:
            gate_close(fd)
        else:
            os.close(fd)  # Only reached if nested exec failed.
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
    if result < 0:
        signum = -result
        if signum != signal.SIGKILL:
            signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
    raise SystemExit(result)
