"""Explicit machine-local external snapshots for optional isolated comparisons."""
import copy
import contextvars
import json
import os
import signal
import stat
import subprocess
import tempfile
import threading
import sys
import uuid
from pathlib import Path


ACTIVE_COPIES = contextvars.ContextVar('rehearsal_owned_copies', default=None)


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
        atomic_write(self.root / 'cleanup.json', json.dumps(self.receipt, sort_keys=True) + '\n')

    def record(self, **facts):
        if not self.safe():
            self.unknown = True
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
        self.token = ACTIVE_COPIES.set(self)
        from .rehearse import retained_temporary_directory
        self.native_scope = retained_temporary_directory(copies=self)
        self.native_scope.__enter__()
        return self

    def __exit__(self, kind, error, traceback):
        try:
            try:
                self.retire(kind)
            except BaseException:
                self.native_scope.__exit__(*sys.exc_info())
                raise
            return self.native_scope.__exit__(kind, error, traceback)
        finally:
            if self.previous_term is not None:
                signal.signal(signal.SIGTERM, self.previous_term)

    def retire(self, kind):
        ACTIVE_COPIES.reset(self.token)
        owned = self.safe()
        # Bulk owns evidence only. Native retention alone decides deletion.
        self.receipt.update(terminal='success' if kind is None else kind.__name__,
                            signal_audit=self.signals,
                            processes=[dict(pid=p.pid, returncode=p.poll()) for p in self.processes])
        ended = self.processes_ended()
        self.receipt.update(state='retained', processes_ended=ended)
        parent = ACTIVE_COPIES.get()
        try:
            if owned:
                self.publish()
                self.retirement_ready = ended and self.safe()
            if not self.retirement_ready:
                self.unknown = True
                if parent is not None:
                    parent.unknown = True
        except BaseException:
            self.unknown = True
            if parent is not None:
                parent.unknown = True
            raise
        return False


def owned_run(args, *, capture_output=False, text=False, env=None, timeout=None, check=False):
    """Supervise only processes started for the current copy operation."""
    owner = ACTIVE_COPIES.get()
    if owner is None:
        return subprocess.run(args, capture_output=capture_output, text=text, env=env,
                              timeout=timeout, check=check)
    process = subprocess.Popen(args, env=env, text=text, start_new_session=True,
                               stdout=subprocess.PIPE if capture_output else None,
                               stderr=subprocess.PIPE if capture_output else None)
    owner.processes.append(process)
    try:
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
            owner.unknown = True
        if not owner.unknown:
            process.wait()
    result = subprocess.CompletedProcess(args, process.returncode, out, err)
    if owner.unknown:
        from .rehearse import RehearsalError
        raise RehearsalError('owned copy process cleanup unknown', code='owned-copy-process-cleanup-unknown')
    if check:
        result.check_returncode()
    return result


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
