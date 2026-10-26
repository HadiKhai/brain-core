"""Explicit machine-local external snapshots for optional isolated comparisons."""
import copy
import contextvars
import json
import os
import signal
import socket
import stat
import subprocess
import tempfile
import threading
import sys
import time
import uuid
from pathlib import Path


ACTIVE_COPIES = contextvars.ContextVar('rehearsal_owned_copies', default=None)
# The recovery supervisor is infrastructure, separate from command launch seams.
# In particular a command's mocked/overridden Popen cannot stand in for its guardian.
_guardian_popen = subprocess.Popen


class OwnedCopies:
    """Exact-owned copy and process evidence for native retention cleanup.

    Never adopt a preexisting path or recover a capability from a receipt. The
    receipt is evidence, not deletion authority. Unknown processes retain bytes.
    """
    def __init__(self, parent=None, prefix='brain-rehearse-'):
        if parent is not None:
            parent = Path(parent).absolute()
            if any(p.is_symlink() for p in (parent, *parent.parents)):
                raise ValueError('linked owned-copy parent refused')
        self.root = Path(tempfile.mkdtemp(prefix=prefix, dir=parent)).resolve()
        self.bulk = self.root / 'bulk'
        self.bulk.mkdir(mode=0o700)
        self.pid = os.getpid()
        self.run = uuid.uuid4().hex
        self.identities = [self.identity(p) for p in (self.root, self.bulk)]
        self.processes = []
        self.signals = []
        self.unknown = False
        self.retirement_ready = False
        self.guardian = None
        self.receipt = dict(version=1, run=self.run, owner_pid=self.pid,
                            directories=self.identities, state='running')
        self.publish()

    @staticmethod
    def identity(path):
        value = path.lstat()
        if (not stat.S_ISDIR(value.st_mode) or value.st_uid != os.getuid()
                or stat.S_IMODE(value.st_mode) != 0o700):
            raise ValueError('owned copy is not a directory')
        return [value.st_dev, value.st_ino]

    def publish(self):
        from .common import atomic_write
        atomic_write(self.root / 'cleanup.json', json.dumps(self.receipt, sort_keys=True) + '\n', private=True)
        if self.guardian is not None:
            self.guardian_message(dict(type='receipt', receipt=self.receipt))

    def record(self, **facts):
        if not self.safe():
            self.retain_unknown()
            raise ValueError('owned copy identity changed')
        self.receipt.update(facts)
        self.publish()

    def safe(self):
        try:
            return (os.getpid() == self.pid and self.bulk.parent == self.root
                    and not any(p.is_symlink() for p in (self.root, *self.root.parents, self.bulk))
                    and self.identities == [self.identity(p) for p in (self.root, self.bulk)]
                    and not (self.root / 'cleanup.json').is_symlink()
                    and json.loads((self.root / 'cleanup.json').read_text()) == self.receipt)
        except (OSError, ValueError):
            return False

    def processes_ended(self):
        if self.unknown:
            return False
        for process in self.processes:
            if process.poll() is None:
                return False
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                continue
            except OSError:
                return False
            return False  # descendants or a reused group: retain, never signal here
        return True

    def __enter__(self):
        self.previous_term = None
        if ACTIVE_COPIES.get() is None and threading.current_thread() is threading.main_thread():
            self.previous_term = signal.getsignal(signal.SIGTERM)
            def cancel(signum, frame):
                raise KeyboardInterrupt
            signal.signal(signal.SIGTERM, cancel)
        self.parent_copies = ACTIVE_COPIES.get()
        token = None
        try:
            if self.parent_copies is None:
                self.start_guardian()
            self.token = token = ACTIVE_COPIES.set(self)
            from .rehearse import retained_temporary_directory
            self.native_scope = retained_temporary_directory(copies=self)
            self.native_scope.__enter__()
        except BaseException:
            self.retain_unknown()
            if token is not None:
                ACTIVE_COPIES.reset(token)
            self.stop_guardian()
            if self.previous_term is not None:
                signal.signal(signal.SIGTERM, self.previous_term)
            raise
        return self

    def __exit__(self, kind, error, traceback):
        cleanup_handlers = None
        if threading.current_thread() is threading.main_thread():
            # A cancellation starts unwinding once. A later signal must not
            # interrupt native retirement halfway through deleting owned bytes.
            cleanup_handlers = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT))
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            try:
                self.retire(kind)
            except BaseException as retirement_error:
                if error is None:
                    self.native_scope.__exit__(*sys.exc_info())
                    raise
                # Native finalization must see the original body object and
                # traceback, even when retirement publication already failed.
                try:
                    self.native_scope.__exit__(kind, error, traceback)
                except BaseException as native_error:
                    secondary = error.__cause__ if native_error is error else native_error
                else:
                    secondary = error.__cause__
                if secondary is not None and secondary is not retirement_error:
                    retirement_error.__cause__ = secondary
                raise error.with_traceback(traceback) from retirement_error
            return self.native_scope.__exit__(kind, error, traceback)
        finally:
            self.stop_guardian()
            if cleanup_handlers is not None:
                signal.signal(signal.SIGINT, cleanup_handlers[1])
                signal.signal(signal.SIGTERM, self.previous_term if self.previous_term is not None
                              else cleanup_handlers[0])

    def guardian_message(self, value):
        owner = self
        while owner.guardian is None and getattr(owner, 'parent_copies', None) is not None:
            owner = owner.parent_copies
        if owner.guardian is None:
            return
        try:
            channel, process = owner.guardian
            channel.sendall((json.dumps(value) + '\n').encode())
            if channel.recv(1) != b'.':
                raise OSError('cleanup guardian unavailable')
        except OSError:
            self.retain_unknown()
            raise

    def start_guardian(self):
        """Transfer this new allocation's capability, never adopt a disk receipt."""
        parent, child = socket.socketpair()
        parent.settimeout(5)
        program = ('import sys; sys.path.insert(0,sys.argv[1]); '
                   'from brain.rehearse_external import cleanup_guardian; '
                   'cleanup_guardian(int(sys.argv[2]))')
        try:
            process = _guardian_popen([sys.executable, '-B', '-c', program,
                str(Path(__file__).resolve().parents[1]), str(child.fileno())],
                pass_fds=(child.fileno(),), start_new_session=True,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.guardian = (parent, process)
            self.guardian_message(dict(type='allocation', root=str(self.root),
                pid=self.pid, run=self.run, identities=self.identities, receipt=self.receipt))
        except BaseException:
            parent.close()
            if self.guardian is not None:
                self.guardian[1].wait(timeout=5)
                self.guardian = None
            raise
        finally:
            child.close()

    def stop_guardian(self):
        if self.guardian is None:
            return
        channel, process = self.guardian
        try:
            # Normal exit has already gone through native retention, including
            # every refusal/error. EOF recovery must never undo that decision.
            self.guardian_message(dict(type='finished'))
        except OSError:
            pass  # A diagnostic supervisor must not replace a body exception.
        finally:
            channel.close()
            self.guardian = None
            try:
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def before_process(self):
        self.guardian_message(dict(type='launching'))

    def track_process(self, process):
        self.processes.append(process)
        self.guardian_message(dict(type='process', pid=process.pid))

    def retain_unknown(self):
        # Context links are captured on entry, never recovered from disk receipts.
        # Mark the entire enclosing chain before any fallible cleanup operation.
        owner = self
        while owner is not None:
            owner.unknown = True
            if owner.guardian is not None:
                # Best effort veto; a broken channel itself cannot authorize
                # recovery, whose positive handshake is required below.
                try:
                    channel, _ = owner.guardian
                    channel.sendall(b'{"type":"uncertain"}\n')
                    channel.recv(1)
                except OSError:
                    pass
            owner = getattr(owner, 'parent_copies', None)

    def retire(self, kind):
        ACTIVE_COPIES.reset(self.token)
        try:
            if self.unknown:
                self.retain_unknown()
            owned = self.safe()
            if not owned:
                self.retain_unknown()
            # Bulk owns evidence only. Native retention alone decides deletion.
            self.receipt.update(terminal='success' if kind is None else kind.__name__,
                                signal_audit=self.signals,
                                processes=[dict(pid=p.pid, returncode=p.poll()) for p in self.processes])
            ended = self.processes_ended()
            if not ended:
                self.retain_unknown()
            self.receipt.update(state='retained', processes_ended=ended)
            if owned:
                self.publish()
                self.retirement_ready = ended and self.safe()
            if not self.retirement_ready:
                self.retain_unknown()
        except BaseException:
            self.retain_unknown()
            raise
        return False


def owned_run(args, *, capture_output=False, text=False, env=None, timeout=None, check=False):
    """Supervise only processes started for the current copy operation."""
    owner = ACTIVE_COPIES.get()
    if owner is None:
        return subprocess.run(args, capture_output=capture_output, text=text, env=env,
                              timeout=timeout, check=check)
    owner.before_process()
    process = subprocess.Popen(args, env=env, text=text, start_new_session=True,
                               stdout=subprocess.PIPE if capture_output else None,
                               stderr=subprocess.PIPE if capture_output else None)
    try:
        owner.track_process(process)
        out, err = process.communicate(timeout=timeout)
    finally:
        audit = dict(pid=process.pid, pgid=process.pid, signal=int(signal.SIGKILL), result='attempted')
        owner.signals.append(audit)
        try:
            os.killpg(process.pid, signal.SIGKILL)
            audit['result'] = 'sent'
        except ProcessLookupError:
            audit['result'] = 'absent'
        except OSError:
            audit['result'] = 'denied-or-unknown'
            owner.retain_unknown()
        if audit['result'] in ('sent', 'absent'):
            process.wait()
    result = subprocess.CompletedProcess(args, process.returncode, out, err)
    if owner.unknown:
        from .rehearse import RehearsalError
        raise RehearsalError('owned copy process cleanup unknown', code='owned-copy-process-cleanup-unknown')
    if check:
        result.check_returncode()
    return result


def cleanup_guardian(fd):
    """Survive worker death; only an acknowledged in-memory allocation is ours.

    Launch gaps, uncertain retention, aliases, identity changes, denied signals
    and live/reused process groups retain bytes. No directory search or receipt
    can create a capability. Normal exit leaves native retention in charge.
    """
    from .machine import process_stamp
    channel = socket.socket(fileno=fd)
    allocation, witnessed, processes, pending, uncertain = None, None, [], False, False
    try:
        with channel.makefile('rb') as stream:
            for line in stream:
                value = json.loads(line)
                kind = value['type']
                if kind == 'allocation' and allocation is None:
                    allocation = value
                    witnessed = value['receipt']
                elif kind == 'receipt' and allocation is not None:
                    witnessed = value['receipt']
                elif kind == 'launching' and allocation is not None and not pending:
                    pending = True
                elif kind == 'process' and pending:
                    value['stamp'] = process_stamp(value['pid'])
                    processes.append(value)
                    pending = False
                elif kind == 'uncertain':
                    uncertain = True
                elif kind == 'finished':
                    channel.sendall(b'.')
                    return
                else:
                    return
                channel.sendall(b'.')
        if allocation is None or pending or uncertain:
            return
        # EOF alone is insufficient: a live worker could have lost its socket.
        if process_stamp(allocation['pid']) is not None:
            return
        for process in processes:
            pid, stamp = process['pid'], process['stamp']
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                continue
            if stamp in (None, 'unknown') or process_stamp(pid) != stamp:
                return
            os.killpg(pid, signal.SIGKILL)
            deadline = time.monotonic() + 5
            while True:
                try:
                    os.killpg(pid, 0)
                except ProcessLookupError:
                    break
                if time.monotonic() >= deadline:
                    return
                time.sleep(.02)
        root = Path(allocation['root'])
        bulk = root / 'bulk'
        staged = root / ('retiring-' + allocation['run'])
        if (any(p.is_symlink() for p in (root, *root.parents))
                or OwnedCopies.identity(root) != allocation['identities'][0]):
            return
        # Immutable receipt fields corroborate the transferred capability; its
        # contents never grant authority over another path or allocation.
        receipt = root / 'cleanup.json'
        entry = receipt.lstat()
        if (not stat.S_ISREG(entry.st_mode) or entry.st_uid != os.getuid() or entry.st_nlink != 1):
            return
        saved = json.loads(receipt.read_text())
        if (saved != witnessed or saved.get('owner_pid') != allocation['pid'] or saved.get('run') != allocation['run']
                or saved.get('directories') != allocation['identities']):
            return
        # The native scope may have already staged the same captured inode.
        # A kill during its rmtree must not strand the surviving owned bytes.
        if (not bulk.exists() and not bulk.is_symlink() and not staged.is_symlink()
                and OwnedCopies.identity(staged) == allocation['identities'][1]):
            staged.rename(bulk)
        if bulk.is_symlink() or OwnedCopies.identity(bulk) != allocation['identities'][1]:
            return
        # Reuse the native scope's same identity, process, checkpoint and staged
        # retirement predicates. Only the transferred owner PID changes here.
        copies = OwnedCopies.__new__(OwnedCopies)
        copies.root, copies.bulk = root, bulk
        copies.pid, copies.run = os.getpid(), allocation['run']
        copies.identities, copies.receipt = allocation['identities'], saved
        copies.processes, copies.signals = [], []
        copies.unknown, copies.retirement_ready = False, False
        copies.guardian, copies.parent_copies = None, None
        from .rehearse import retained_temporary_directory
        with retained_temporary_directory(copies=copies):
            if not copies.safe():
                return
            copies.receipt.update(terminal='owner-exit', state='retained', processes_ended=True)
            copies.publish()
            copies.retirement_ready = copies.safe()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return  # Unknown is never deletion permission.
    finally:
        channel.close()


def freeze_declared(inst, directory):
    """Copy declared roots once, then rebind only supported local path operands.

    A declaration is local.json's rehearse_external = {version: 1, roots: [...]}.
    Unknown bindings remain observable through the existing drift detector. Copies
    preserve dirty/untracked/ignored bytes; this is a fixed capture, not an atomic
    historical transaction with concurrent source writers. Return a real Instance
    with independent in-memory bindings, without publishing into the caller.
    """
    from . import rehearse
    declaration = getattr(inst, 'local', {}).get('rehearse_external')
    if declaration is None:
        return inst
    def refuse():
        raise rehearse.RehearsalError('invalid external snapshot declaration',
                                     code='invalid-external-snapshot-declaration')
    if (not isinstance(declaration, dict) or set(declaration) != {'version', 'roots'}
            or declaration['version'] != 1 or not isinstance(declaration['roots'], list)
            or not 1 <= len(declaration['roots']) <= 64):
        refuse()
    roots = []
    for value in declaration['roots']:
        if not isinstance(value, str) or not Path(value).expanduser().is_absolute():
            refuse()
        root = Path(value).expanduser()
        if '..' in root.parts or any(p.is_symlink() for p in (root, *root.parents)):
            refuse()
        root = root.resolve()
        if (not root.is_dir() or root == Path(root.anchor) or root == inst.root
                or root in inst.root.parents or inst.root in root.parents
                or any(root == old or root in old.parents or old in root.parents for old in roots)):
            refuse()
        roots.append(root)
    bindings = []
    for node in inst.nodes():
        local = node.get('local', {})
        bindings.extend(value for value in (local.get('home'), local.get('workbench')) if value)
        bindings.extend(w['path'] for w in local.get('worktrees', {}).values() if w.get('path'))
    # No arbitrary filesystem scan: every root must be an exact supported binding.
    supported = {Path(value).expanduser().absolute() for value in bindings}
    if any(root not in supported for root in roots):
        refuse()
    directory.mkdir()
    mappings = []
    for index, root in enumerate(roots):
        target = directory / str(index)
        rehearse.copy_tree(root, target)
        rehearse.validate_frozen_links(target)
        # Workbenches can contain nested repositories without being Git roots.
        # Sanitize every captured repository, including untracked nested ones.
        for top, dirs, files in os.walk(target, followlinks=False):
            dirs[:] = [name for name in dirs if name != '.git']
            repo = Path(top)
            if (repo / '.git').exists():
                rehearse.materialize_git(repo, repo)
        mappings.append((root, target))
    def remap(value):
        if not isinstance(value, str):
            return value
        path = Path(value).expanduser()
        if path.is_absolute():
            for root, target in mappings:
                try:
                    return str(target / path.relative_to(root))
                except ValueError:
                    pass
        return value
    local = copy.deepcopy(inst.local)
    for node in local.get('nodes', {}).values():
        for key in ('home', 'workbench'):
            if key in node:
                node[key] = remap(node[key])
        for tree in node.get('worktrees', {}).values():
            if 'path' in tree:
                tree['path'] = remap(tree['path'])
    # The declaration is preparation-only; it cannot become a live observation.
    local.pop('rehearse_external')
    # Capturing operands must never publish into the caller, which may be live.
    # The preparation owner alone persists these bindings into its private copy.
    from .instance import Instance
    captured = Instance(inst.root)
    captured.local = local
    return captured
