"""Compare installed and candidate cores without granting either live write access.

The macOS sandbox is a prerequisite, not an optional best-effort safeguard. Other
platforms refuse until they have an equivalent OS-enforced write/network boundary.
Published evidence is redacted. Optional raw diffs stay in a private local sidecar.
"""
import collections
import contextlib
import contextvars
import datetime
import difflib
import errno
import hashlib
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
import traceback

from . import common as _m_common
from . import guard as _m_guard
from . import hosts as _m_hosts
from . import instance as _m_instance
from . import jobs as _m_jobs
from . import nodes as _m_nodes
from . import rehearse_external as _m_rehearse_external
from . import transitions as _m_transitions
from . import rehearse_transactions as _m_transactions
from . import upgrade as _m_upgrade
from . import verify as _m_verify


class RehearsalError(Exception):
    def __init__(self, message, code='unclassified-harness-error'):
        super().__init__(message)
        self.code = code


_retention_children = contextvars.ContextVar('rehearsal_retention_children', default=None)


class NativeRetention:
    """Opt-in diagnostics only; unavailable operands never imply validation."""
    def __init__(self, owned, stage, **fields):
        self.owned = Path(owned)
        self.destination = os.environ.get('BRAIN_REHEARSAL_EVIDENCE_DIR')
        self.incomplete = False
        self.packet = dict(version=1, status='INCOMPLETE', stage=stage,
                           owned=str(owned), invocation=[sys.executable, *sys.argv],
                           retention_errors=[],
                           native_receipt_linkage='post-run manifest binds artifact hashes to native receipt',
                           limits='sequential/non-atomic; context-local threads; uncatchable termination and storage failure may prevent retention',
                           **fields)
        children = _retention_children.get()
        if self.destination and children is not None:
            children.append(self)
        self.checkpoint()

    def checkpoint(self, **fields):
        if not self.destination:
            return True
        self.packet.update(fields)
        if self.incomplete:
            self.packet['status'] = 'INCOMPLETE'
        try:
            source = Path(__file__).resolve().parents[2]
            if 'source_binding' not in self.packet:
                def git(*args):
                    return subprocess.run(['git', '-C', str(source), *args], check=True,
                                          capture_output=True, timeout=5).stdout
                names = ('bin/brain', 'lib/brain/rehearse.py', 'lib/brain/rehearse_transactions.py',
                         'lib/brain/events.py', 'lib/brain/instance.py', 'tests/scenario.py',
                         'tests/test_rehearse.py', 'tests/nightly_rehearse.py')
                self.packet['source_binding'] = dict(
                    head=git('rev-parse', 'HEAD').decode().strip(),
                    tree=git('rev-parse', 'HEAD^{tree}').decode().strip(),
                    diff_sha256=hashlib.sha256(git('diff', '--binary', 'HEAD')).hexdigest(),
                    source_hashes={name: hashlib.sha256((source / name).read_bytes()).hexdigest()
                                   for name in names})
            observer = self.packet.get('observer_path')
            if isinstance(observer, str):
                self.packet['observer_sha256'] = hashlib.sha256(Path(observer).read_bytes()).hexdigest()
            root = self.packet.get('root')
            if isinstance(root, str) and root != 'UNKNOWN' and 'native_core_hashes' not in self.packet:
                core = Path(root) / 'core'
                if core.exists():
                    self.packet['native_core_hashes'] = {
                        name: hashlib.sha256((core / name).read_bytes()).hexdigest()
                        if (core / name).is_file() else 'UNKNOWN'
                        for name in ('bin/brain', 'lib/brain/events.py',
                                     'lib/brain/rehearse_transactions.py')}
            directory = Path(self.destination)
            for parent in (directory, *directory.parents):
                if parent.is_symlink() or (parent / '.git').exists() or (
                        (parent / 'HEAD').is_file() and (parent / 'objects').is_dir()):
                    raise ValueError('private evidence destination refused')
            permissions = directory.stat()
            if (not directory.is_absolute() or permissions.st_uid != os.getuid()
                    or stat.S_IMODE(permissions.st_mode) != 0o700
                    or directory.resolve() == self.owned.resolve()
                    or self.owned.resolve() in directory.resolve().parents
                    or source == directory.resolve() or source in directory.resolve().parents):
                raise ValueError('private evidence destination refused')
            body = (json.dumps(self.packet, sort_keys=True) + '\n').encode()
            fd, filename = tempfile.mkstemp(prefix='native-retention-', suffix='.json', dir=directory)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            dfd = os.open(str(directory), os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
            self.artifact = dict(path=filename, sha256=hashlib.sha256(body).hexdigest())
        except BaseException as error:
            # A diagnostic error must never replace an assertion/native exception.
            self.incomplete = True
            self.packet.update(status='INCOMPLETE', preserved_owned=str(self.owned))
            self.packet['retention_errors'].append(dict(category=type(error).__name__,
                                                        stage=self.packet['stage']))
            try:
                fd, filename = tempfile.mkstemp(prefix='unsaved-native-retention-',
                                                suffix='.json', dir=self.owned)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write((json.dumps(self.packet, sort_keys=True) + '\n').encode())
                    stream.flush()
                    os.fsync(stream.fileno())
                dfd = os.open(str(self.owned), os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except BaseException:
                pass  # The owned directory remains the sole recovery source.
            print('native retention=INCOMPLETE category=' + type(error).__name__, flush=True)
        return not self.incomplete

    def exception(self, error):
        return self.checkpoint(exception=dict(category=type(error).__name__,
            stack=[dict(function=f.name, line=f.lineno)
                   for f in traceback.extract_tb(error.__traceback__)]))

    def validation_call(self, before, after, observations, owner_pid, command, now,
                        diagnostics, caller):
        return NativeRetention(self.owned, 'validation-call', before=before, after=after,
            observations=observations, owner_pid=owner_pid, validator_command=command,
            validation_instant=now.isoformat(), diagnostics=diagnostics,
            validator_result='UNKNOWN', caller=caller,
            context={key: self.packet[key] for key in
                     ('nodeid', 'fixture', 'root', 'trace_path', 'args', 'kind')
                     if key in self.packet})

    def bind_command_sources(self, core, bootstrap):
        if not self.destination:
            return
        try:
            self.checkpoint(
                bootstrap_hashes={path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                  for path in bootstrap.iterdir() if path.is_file()},
                native_core_hashes={name: hashlib.sha256((core / name).read_bytes()).hexdigest()
                                   if (core / name).is_file() else 'UNKNOWN'
                                   for name in ('bin/brain', 'lib/brain/events.py',
                                                'lib/brain/rehearse_transactions.py')})
        except BaseException as error:
            self.incomplete = True
            self.exception(error)


@contextlib.contextmanager
def retained_temporary_directory(copies=None):
    """Native retention is the sole final cleanup authority for owned copies."""
    temporary = None if copies is not None else tempfile.TemporaryDirectory(prefix='brain-rehearse-')
    owned = copies.bulk if copies is not None else Path(temporary.name)
    retention = NativeRetention(owned, 'before-native-setup')
    children = []
    token = _retention_children.set(children)
    primary_error = None
    try:
        yield str(owned)
    except BaseException as error:
        primary_error = error
        try:
            retention.exception(error)
        except BaseException as diagnostic_error:
            if copies is None:
                raise
            retention.incomplete = True
            if copies is not None:
                copies.retain_unknown()
            raise error from diagnostic_error
        raise
    finally:
        try:
            if any(child.incomplete and (child.owned == owned or owned in child.owned.parents)
                   for child in children):
                retention.incomplete = True
                if copies is not None:
                    copies.retain_unknown()
            _retention_children.reset(token)
            saved = retention.checkpoint(stage='before-native-delete') is True
            if copies is None:
                if retention.incomplete or not saved:
                    temporary._finalizer.detach()
                else:
                    temporary.cleanup()
            else:
                approved = (saved and not retention.incomplete and copies.retirement_ready
                            and copies.safe() and copies.processes_ended())
                if approved:
                    # Keep the exact inode recoverable until terminal publication has
                    # succeeded. No post-delete publication can erase the last copy.
                    staged = copies.root / ('retiring-' + copies.run)
                    moved = False
                    try:
                        if staged.exists() or staged.is_symlink():
                            raise OSError('owned retirement destination occupied')
                        copies.bulk.rename(staged)
                        moved = True
                        copies.receipt['state'] = 'removed'
                        copies.publish()
                        valid = (copies.identity(copies.root) == copies.identities[0]
                                 and copies.identity(staged) == copies.identities[1]
                                 and not copies.bulk.exists() and not copies.bulk.is_symlink()
                                 and not (copies.root / 'cleanup.json').is_symlink()
                                 and json.loads((copies.root / 'cleanup.json').read_text()) == copies.receipt
                                 and copies.processes_ended())
                        if not valid:
                            raise OSError('owned retirement identity changed')
                        shutil.rmtree(staged)
                    except BaseException as error:
                        retention.incomplete = True
                        copies.retain_unknown()
                        copies.receipt['state'] = 'retained-cleanup-error'
                        try:
                            if (moved and not copies.bulk.exists() and not copies.bulk.is_symlink()
                                    and copies.identity(copies.root) == copies.identities[0]
                                    and staged.exists() and copies.identity(staged) == copies.identities[1]):
                                staged.rename(copies.bulk)
                        except BaseException as rollback_error:
                            raise error from rollback_error
                        raise
                else:
                    retention.incomplete = True
                    copies.retain_unknown()
                    copies.receipt.update(state='retained', refusal='native-retention-or-ownership-incomplete')
                    retention.checkpoint(bulk_cleanup=dict(copies.receipt))
        except BaseException as cleanup_error:
            if copies is not None:
                copies.retain_unknown()
            if copies is not None and primary_error is not None:
                raise primary_error from cleanup_error
            raise


@contextlib.contextmanager
def rehearsal_phase(phase):
    try:
        yield
    except (RehearsalError, OSError, subprocess.SubprocessError) as error:
        # Only fixed phase/command labels enter public diagnostics, never argv.
        if not hasattr(error, 'rehearsal_phase'):
            error.rehearsal_phase = phase
        raise


def refusal_reason(error):
    if isinstance(error, RehearsalError):
        return error.code
    if isinstance(error, OSError):
        return type(error).__name__ + ':' + errno.errorcode.get(error.errno, 'UNKNOWN')
    if isinstance(error, subprocess.CalledProcessError):
        return 'CalledProcessError:exit-' + str(error.returncode)
    return type(error).__name__


def node_instance(n):
    root = n
    while root.get('parent'):
        root = root['parent']
    return _m_instance.Instance(root['dir'])


def installed_core_commit(root):
    core = root / 'core'
    if (core / '.git').exists():
        result = _m_common.git('rev-parse', '--verify', 'HEAD', cwd=core)
        if result.returncode or not re.fullmatch(r'[0-9a-f]{40,64}', result.stdout.strip()):
            raise RehearsalError('installed core commit unavailable', code='installed-core-commit-unavailable')
        return result.stdout.strip()
    # Fixture and manually installed cores have no commit. Still bind their bytes.
    return 'unversioned:' + hashlib.sha256(json.dumps(core_identity(root), sort_keys=True).encode()).hexdigest()


def core_identity(root):
    """Canonical internal aliases for provenance; mutation audits stay literal."""
    core = root / 'core'
    entries = snapshot(core)
    for rel, value in entries.items():
        if value.startswith('link:'):
            try:
                target = (core / rel).resolve()
            except (OSError, RuntimeError):
                raise RehearsalError('installed core link identity unavailable', code='installed-core-link-identity-unavailable') from None
            if target == root or root in target.parents:
                entries[rel] = 'link:brain:' + target.relative_to(root).as_posix()
    return entries


def evidence_path(n, branch):
    # Evidence stays in the workbench, leaving even instance Git byte-identical.
    from . import worktrees as _m_worktrees
    key = hashlib.sha256(branch.encode()).hexdigest()
    inst = node_instance(n)
    return _m_worktrees.workbench(inst, n) / 'scratch' / 'rehearse' / (key + '.json')


def read_evidence(n, branch, head, main=None, integration_base=None):
    if not n['external'] or not n.get('home_path') or not head:
        return None
    try:
        p = json.loads(evidence_path(n, branch).read_text())
        if (isinstance(p, dict) and p.get('version') == 1 and p.get('node') == n['id'] and p.get('branch') == branch
                and p.get('head') == head and p.get('passed') is True
                and p.get('installed_core_commit') == installed_core_commit(node_instance(n).root)
                and p.get('main') == (main if main is not None else _m_verify.ready_refs(n, branch)[1])
                and p.get('integration_base') == (integration_base if integration_base is not None
                                                  else _m_verify.integration_parent(n, p.get('main')))):
            return p
    except (OSError, ValueError, TypeError, RehearsalError):
        pass
    return None


def required(n):
    return n['fm'].get('rehearse') == 'required'


def isolated_probe_instance(inst, directory):
    """The shared transport helper's cache belongs in a private instance."""
    shutil.copy2(inst.root / 'instance.md', directory / 'instance.md')
    for node in inst.nodes():
        dest = directory / node['dir'].relative_to(inst.root)
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(node['dir'] / 'node.md', dest / 'node.md')
    _m_common.atomic_write(directory / '.brain/local.json', json.dumps(inst.local), private=True, create=True)
    return _m_instance.Instance(directory)


def probe_delivery(inst, nodes, probe_inst=None):
    """One authorized real POST per distinct safe binding; never publish capabilities."""
    statuses = []
    if _m_common.outward_links(inst.root):
        raise RehearsalError('outward-linked brain cannot probe delivery', code='outward-linked-brain-cannot-probe-delivery')
    if probe_inst is not None and probe_inst.root.resolve() == inst.root.resolve():
        raise RehearsalError('delivery cache requires a private instance', code='delivery-cache-requires-a-private-instance')
    seen = set()
    for node in nodes:
        if not node['local'].get('coordinator') and node['id'] != 'root':
            continue
        _, address = _m_hosts.wake_target(inst, node)
        if not address.startswith(('https://', 'http://')) or address in seen:
            continue  # shell transports are not webhooks; never execute them here
        seen.add(address)
        try:
            if probe_inst is None:
                with tempfile.TemporaryDirectory(prefix='brain-rehearse-probe-') as td:
                    private = isolated_probe_instance(inst, Path(td))
                    status = _m_hosts.wake_probe(private, private.node(node['id']), force=True)
            else:
                status = _m_hosts.wake_probe(probe_inst, probe_inst.node(node['id']), force=True)
        except Exception:
            status = 0
        statuses.append({'coordinator': len(statuses), 'status': status if type(status) is int else 0})
    return statuses


def delivery_regressions(statuses):
    return ['wake:' + str(p['coordinator']) + ': HTTP ' + str(p['status'])
            for p in statuses if not 200 <= p['status'] < 300]


def snapshot(root, cache=None):
    """Hash regular files and link text, never traverse links or read their targets."""
    out = {}
    for dp, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            p = Path(dp) / name
            rel = p.relative_to(root).as_posix()
            if p.is_symlink() or p.is_dir() or p.is_file():
                out[rel] = snapshot_entry(p, cache)
    return out


def snapshot_entry(p, cache=None):
    """Read one entry; callers must keep its parent directories link-free."""
    if p.is_symlink():
        return 'link:' + os.readlink(p)
    if p.is_dir():
        return 'dir:' + str(p.stat().st_mode)
    if not p.is_file():
        return 'missing'
    st = p.stat()
    identity = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_mode)
    if cache is not None and cache.get(str(p), (None,))[0] == identity:
        return cache[str(p)][1]
    h = hashlib.sha256()
    with p.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    value = str(st.st_mode) + ':' + h.hexdigest()
    if cache is not None:
        cache[str(p)] = (identity, value)
    return value


def validate_git_metadata(root):
    """Refuse linked Git administration before asking Git to read it."""
    metadata = root / '.git'
    if root.is_symlink() or metadata.is_symlink():
        raise RehearsalError('linked Git metadata refused', code='linked-git-metadata-refused')
    if not metadata.exists():
        raise RehearsalError('source stability metadata unavailable', code='source-stability-metadata-unavailable')
    # Inspect gitfile/commondir paths before Git can canonicalize linked parents
    # or read linked config/index/refs. Both paths may be relative to their owner.
    own = metadata
    if metadata.is_file():
        pointer = metadata.read_text()
        if not pointer.startswith('gitdir: '):
            raise RehearsalError('source stability metadata unavailable', code='source-stability-metadata-unavailable')
        own = root / pointer[8:].strip()
    if any(p.is_symlink() for p in (own, *own.parents)):
        raise RehearsalError('linked Git metadata refused', code='linked-git-metadata-refused')
    if (own / 'commondir').is_symlink():
        raise RehearsalError('linked Git metadata refused', code='linked-git-metadata-refused')
    common = own / (own / 'commondir').read_text().strip() if (own / 'commondir').is_file() else own
    # Git itself reads these paths. Refuse links before asking it for index/refs;
    # private ref namespaces are neither read nor traversed by this check.
    for base in {own, common}:
        for parent in (base, *base.parents):
            if parent.is_symlink():
                raise RehearsalError('linked Git metadata refused', code='linked-git-metadata-refused')
        for rel in ('config', 'config.worktree', 'index', 'HEAD', 'packed-refs', 'refs',
                    'refs/heads', 'refs/tags'):
            path = base / rel
            if path.is_symlink():
                raise RehearsalError('linked Git metadata refused', code='linked-git-metadata-refused')
            if rel in ('refs/heads', 'refs/tags') and path.is_dir():
                # Only inspect link types: transient ref locks may disappear and
                # must not be opened or hashed while an unrelated writer runs.
                for dp, dirs, files in os.walk(path, followlinks=False):
                    if any((Path(dp) / name).is_symlink() for name in dirs + files):
                        raise RehearsalError('linked Git metadata refused', code='linked-git-metadata-refused')
    return own


def stability_snapshot(root):
    """Bind tracked bytes and refs; retained for callers inspecting fixed trees."""
    own = validate_git_metadata(root)
    def git(*args):
        r = _m_common.git(*args, cwd=root)
        if r.returncode:
            raise RehearsalError('source stability metadata unavailable', code='source-stability-metadata-unavailable')
        return r.stdout
    out = {}
    listing = subprocess.run(['git', '-C', str(root), 'ls-files', '--cached', '-z'], capture_output=True)
    if listing.returncode:
        raise RehearsalError('source stability metadata unavailable', code='source-stability-metadata-unavailable')
    tracked = {os.fsdecode(rel) for rel in listing.stdout.split(b'\0') if rel}
    for rel in tracked:
        path = root
        for part in Path(rel).parts:
            path = path / part
            if path.is_symlink() or not path.is_dir():
                break  # do not read a tracked descendant through a replaced parent
        key = path.relative_to(root).as_posix()
        out[key] = snapshot_entry(path)
        if not path.is_symlink() and path.is_dir() and key == rel:
            out[key] = stability_snapshot(path) if (path / '.git').exists() else snapshot(path)
    # A manually installed core may be outside the parent's index, but its bytes
    # are still executable rehearsal inputs. Preserve coverage for that layout.
    core = root / 'core'
    if core.exists() and not any(rel == 'core' or rel.startswith('core/') for rel in tracked):
        out['core'] = (snapshot_entry(core) if core.is_symlink() else
                       stability_snapshot(core) if (core / '.git').exists() else snapshot(core))
    out['.git:refs'] = git('for-each-ref', '--format=%(refname) %(objectname) %(symref)',
                           'refs/heads', 'refs/tags')
    out['.git:HEAD'] = (own / 'HEAD').read_text()
    return out


def copy_tree(source, dest):
    if dest.exists() or dest.is_symlink():
        raise FileExistsError(str(dest))
    if sys.platform == 'darwin':
        # Unsupported cloning can leave a partial tree. Only clean up the tree
        # inside this operation's own temporary directory, never its destination.
        with _m_rehearse_external.OwnedCopies(prefix='.rehearse-copy-', parent=dest.parent) as copies:
            cloned = copies.bulk / 'tree'
            r = _m_rehearse_external.owned_run(['/bin/cp', '-c', '-p', '-R', '-P', str(source), str(cloned)],
                                             capture_output=True)
            if not r.returncode:
                cloned.rename(dest)
    if not dest.exists():
        # Independent inodes, including for source hard links; never hardlink.
        shutil.copytree(source, dest, symlinks=True)
    relocate_links(source, dest)
    capture_gitfiles(source, dest)


def copy_brain(source, dest, private_root):
    """Drop inode-bound indexes only from this newly copied private Brain."""
    private_root, dest = Path(private_root), Path(dest)
    owner = private_root.lstat()
    if (private_root.resolve() != private_root or not stat.S_ISDIR(owner.st_mode)
            or owner.st_uid != os.getuid() or stat.S_IMODE(owner.st_mode) != 0o700
            or private_root not in dest.parents or dest.parent.resolve() != dest.parent):
        raise RehearsalError('unowned Brain clone', code='unowned-brain-clone')
    copy_tree(source, dest)  # refuses an existing destination; never edits source
    clone = dest.lstat()
    if (dest.resolve() != dest or not stat.S_ISDIR(clone.st_mode)
            or clone.st_uid != os.getuid()):
        raise RehearsalError('ambiguous Brain clone', code='unowned-brain-clone')
    directory = dest / '.brain'
    if not directory.exists() and not directory.is_symlink():
        return
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        initial = os.fstat(fd)
        if initial.st_uid != os.getuid():
            raise RehearsalError('foreign Brain clone metadata', code='unowned-brain-clone')
        caches = {}
        for name in ('events-index.json', 'events-compare-index.json'):
            try:
                entry = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if (not stat.S_ISREG(entry.st_mode) or entry.st_uid != os.getuid()
                    or entry.st_nlink != 1):
                raise RehearsalError('ambiguous Brain clone cache', code='unowned-brain-clone')
            caches[name] = entry
        current = directory.lstat()
        if (current.st_dev, current.st_ino) != (initial.st_dev, initial.st_ino):
            raise RehearsalError('replaced Brain clone metadata', code='unowned-brain-clone')
        for name, entry in caches.items():
            current = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if current != entry:
                raise RehearsalError('replaced Brain clone cache', code='unowned-brain-clone')
            os.unlink(name, dir_fd=fd)
    finally:
        os.close(fd)


def capture_gitfiles(source, dest):
    """Capture external administration before this copy operation returns.

    Administration already copied with the tree (or another common directory)
    must be reused, not fetched again from a moving live submodule/worktree.
    """
    mappings = [(Path(os.path.abspath(source)), dest)]
    def captured(path):
        path = Path(os.path.abspath(path))
        for original, private in sorted(mappings, key=lambda pair: len(str(pair[0])), reverse=True):
            try:
                return private / path.relative_to(original)
            except ValueError:
                pass
        return None
    for dp, dirs, files in os.walk(dest, followlinks=False):
        dirs[:] = [name for name in dirs if name != '.git']
        metadata = Path(dp) / '.git'
        if '.git' not in files or metadata.is_symlink():
            continue
        original_repo = source / Path(dp).relative_to(dest)
        own = Path(os.path.abspath(validate_git_metadata(original_repo)))
        common = (Path(os.path.abspath(own / (own / 'commondir').read_text().strip()))
                  if (own / 'commondir').is_file() else own)
        private_common = captured(common)
        metadata.unlink()
        if private_common is not None:
            if not private_common.is_dir():
                raise RehearsalError('captured Git administration unavailable', code='captured-git-administration-unavailable')
            copy_tree(private_common, metadata)
        else:
            copy_tree(common, metadata)
            mappings.append((common, metadata))
        private_own = captured(own)
        for name in ('HEAD', 'index'):
            origin = (private_own if private_own is not None else own) / name
            if origin.is_file() and origin != metadata / name:
                shutil.copy2(origin, metadata / name)
        # The copied administration's old relative worktree binding is invalid
        # at its new location. Remove it before any Git discovery in this copy.
        copied_locks = []
        for mp, md, mf in os.walk(metadata, followlinks=False):
            if any((Path(mp) / name).is_symlink() for name in md + mf):
                raise RehearsalError('linked Git metadata refused', code='linked-git-metadata-refused')
            copied_locks.extend(Path(mp) / name for name in mf if name.endswith('.lock'))
        for lock in copied_locks:
            lock.unlink()
        (metadata / 'config.worktree').unlink(missing_ok=True)
        unset = _m_common.git('--git-dir=' + str(metadata), '--work-tree=' + str(Path(dp)),
                              'config', '--no-includes', '--file', str(metadata / 'config'),
                              '--unset-all', 'core.worktree', cwd=Path(dp))
        if unset.returncode not in (0, 5):
            raise RehearsalError('captured Git worktree binding unavailable', code='captured-git-worktree-binding-unavailable')


def relocate_links(source, dest):
    """Keep absolute internal aliases inside every relocated copy's own tree."""
    for dp, dirs, files in os.walk(dest, followlinks=False):
        for name in dirs + files:
            link = Path(dp) / name
            if not link.is_symlink():
                continue
            target = Path(os.readlink(link))
            if not target.is_absolute():
                continue
            try:
                suffix = target.relative_to(source)
            except ValueError:
                continue
            # Preserve alias/.. semantics: do not resolve or normalize the suffix
            # through either the live tree or copied links while rewriting it.
            relative = Path(os.path.relpath(dest, link.parent)) / suffix
            link.unlink()
            link.symlink_to(relative)


def validate_frozen_links(root):
    """No surviving alias may observe an external/live target, even read-only."""
    for dp, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            link = Path(dp) / name
            if link.is_symlink():
                try:
                    target = link.resolve()
                except (OSError, RuntimeError):
                    raise RehearsalError('cyclic or unavailable frozen link refused', code='cyclic-or-unavailable-frozen-link-refused') from None
                if target != root and root not in target.parents:
                    raise RehearsalError('outward-linked frozen brain refused', code='outward-linked-frozen-brain-refused')


def materialize_git(source, dest):
    """Detach copied submodules/worktrees from their original Git administration."""
    if not (source / '.git').exists():
        return
    validate_git_metadata(source)
    def resolve(arg):
        r = _m_common.git('rev-parse', '--path-format=absolute', arg, cwd=source)
        if r.returncode:
            raise RehearsalError('Git metadata could not be copied safely', code='git-metadata-could-not-be-copied-safely')
        return Path(r.stdout.strip())
    own, common = resolve('--git-dir'), resolve('--git-common-dir')
    target = dest / '.git'
    if not (own == common and target.is_dir() and not target.is_symlink()):
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)
        copy_tree(common, target)
    # Preparation itself is outside the child sandbox. Never follow copied Git
    # metadata links while detaching it from the original repository.
    for dp, dirs, files in os.walk(target, followlinks=False):
        for name in dirs + files:
            if (Path(dp) / name).is_symlink():
                raise RehearsalError('linked Git metadata refused', code='linked-git-metadata-refused')
        # Source writers do not exist in the detached copy. Their copied lock
        # files would otherwise prevent safe config rewriting and later probes.
        for name in files:
            if name.endswith('.lock'):
                (Path(dp) / name).unlink()
    for name in ('HEAD', 'index'):
        if (own / name).is_file() and own / name != target / name:
            shutil.copy2(own / name, target / name)
    # A copied submodule config can still name its old relative worktree. Bind
    # both directories explicitly while reading it, before replacing that config.
    config = _m_common.git('--git-dir=' + str(target), '--work-tree=' + str(dest),
                           'config', '--no-includes', '--file', str(target / 'config'),
                           '--null', '--list', cwd=dest)
    if config.returncode:
        raise RehearsalError('Git format configuration unreadable', code='git-format-configuration-unreadable')
    formats = []
    for entry in config.stdout.split('\0'):
        key, _, value = entry.partition('\n')
        if key == 'core.repositoryformatversion' or key.startswith('extensions.'):
            formats.append((key, value))
    for name in ('commondir', 'gitdir', 'worktrees', 'hooks', 'config.worktree'):
        p = target / name
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        elif p.exists() or p.is_symlink():
            p.unlink()
    # Never execute repository hooks or load include/configured external helpers.
    (target / 'config').write_text('[core]\n\trepositoryformatversion = 0\n\tbare = false\n\thooksPath = /dev/null\n')
    for key, value in formats:
        result = _m_common.git('config', '--file', str(target / 'config'), '--replace-all', key, value, cwd=dest)
        if result.returncode:
            raise RehearsalError('Git format configuration could not be preserved', code='git-format-configuration-could-not-be-preserved')

    validate_git_objects(dest)
    listing = subprocess.run(['git', '-C', str(source), 'ls-files', '--stage', '-z'], capture_output=True)
    if listing.returncode:
        raise RehearsalError('Git metadata could not be copied safely', code='git-metadata-could-not-be-copied-safely')
    gitlinks = {os.fsdecode(entry.partition(b'\t')[2]) for entry in listing.stdout.split(b'\0')
                if entry.startswith(b'160000 ') and entry.partition(b'\t')[2]}
    for rel in gitlinks:
        child = source / rel
        if not child.is_symlink() and (child / '.git').exists():
            materialize_git(child, dest / rel)
    # Also detach a manual, untracked core before its copy is used as a baseline.
    if ((source / 'core/.git').exists() and not (source / 'core').is_symlink()
            and 'core' not in gitlinks):
        materialize_git(source / 'core', dest / 'core')


def validate_git_objects(root):
    """Reject incomplete copied object stores without depending on private refs."""
    env = {k: v for k, v in os.environ.items() if k in ('PATH', 'LANG', 'LC_ALL', 'TZ')}
    env.update(HOME=str(root), GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1',
               GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0', GIT_NO_REPLACE_OBJECTS='1',
               GIT_NO_LAZY_FETCH='1')
    r = subprocess.run(['git', '-C', str(root), 'rev-list', '--quiet', '--objects',
                        '--missing=error', 'HEAD', '--branches', '--tags'],
                       env=env, capture_output=True)
    if r.returncode:
        raise RehearsalError('live brain moved while copying; retry with a stable source', code='live-brain-moved-while-copying-retry-with-a-stable-source')


OUTPUT_NORMALIZATION_PATTERNS = {
    'timestamps': r'\b\d{4}-\d\d-\d\d[ T]\d\d:\d\d(?::\d\d(?:\.\d+)?)?(?:Z| ?[+-]\d\d:\d\d)?',
    'warning-byte-sizes': r'(?<![\w.+-])\d+(?:\.\d+)?(?=[ \t]*(?:bytes?|[KMGT]i?B)\b)',
}

# These modules read wall time as data (cache ages, activity, policy windows).
# Instance lock deadlines retain real time; all monotonic/elapsed timers do too.
FROZEN_WALL_CLOCK_MODULES = ('check', 'hosts', 'jobs', 'machine', 'spend', 'ui', 'upgrade', 'usage')

# Only these live numeric observations and the exact spans below are volatile.
# Presence, types, policy thresholds, reason membership and other values compare.
UI_VOLATILE_NUMBERS = (
    ('machine', 'load', 0), ('machine', 'load', 1), ('machine', 'load', 2),
    ('machine', 'load_per_cpu', 0), ('machine', 'load_per_cpu', 1), ('machine', 'load_per_cpu', 2),
    ('machine', 'mem_free_gb'), ('machine', 'disk_free_gb'),
)
_VOLATILE_NUMBER = object()
UI_VOLATILE_MACHINE_SPANS = (
    ('load5-observation', r'^(5-minute load/CPU )(\d+(?:\.\d+)?)( > \d+(?:\.\d+)?)$'),
    ('free-memory-observation', r'^(free memory )(\d+(?:\.\d+)?)( GB < \d+(?:\.\d+)? GB'
     r'(?:; threshold exceeds machine; adjust machine-care\.json)?)$'),
    ('free-disk-observation', r'^(free disk )(\d+(?:\.\d+)?)( GB < \d+(?:\.\d+)? GB'
     r'(?:; threshold exceeds machine; adjust machine-care\.json)?)$'),
)


def semantic_output(text, roots=()):
    """Preserve operands; standardize copy roots, JSON and outer whitespace.

    Publication's timestamp/size substitutions must never erase policy operands.
    The caller redacts protected terms before keeping this private representation.
    """
    for root in sorted(map(str, roots), key=len, reverse=True):
        text = text.replace(root, '<copy>')
    try:
        return json.dumps(json.loads(text), sort_keys=True, ensure_ascii=False)
    except ValueError:
        return text.strip()


def semantic_ui(value, trace=None, path=()):
    if (path in UI_VOLATILE_NUMBERS and type(value) in (int, float) and value >= 0
            and (type(value) is int or math.isfinite(value))):
        if trace is not None:
            name = 'ui.' + '.'.join(map(str, path))
            trace[name] = hashlib.sha256(json.dumps(value).encode()).hexdigest()
        return _VOLATILE_NUMBER
    if (len(path) == 3 and path[0] == 'machine' and path[1] in ('held', 'sample_reasons')
            and type(path[2]) is int and isinstance(value, str)):
        for name, pattern in UI_VOLATILE_MACHINE_SPANS:
            match = re.fullmatch(pattern, value)
            if match:
                if trace is not None:
                    trace['ui.' + '.'.join(map(str, path)) + '.' + name] = hashlib.sha256(match[2].encode()).hexdigest()
                # A structural token cannot collide with any unrecognized raw
                # string (including a candidate that prints our placeholder).
                return ('str', ('observation', name, match[1], match[3]))
    if isinstance(value, dict):
        return {key: semantic_ui(item, trace, path + (key,)) for key, item in value.items()}
    if isinstance(value, list):
        return [semantic_ui(item, trace, path + (i,)) for i, item in enumerate(value)]
    return (type(value).__name__, value)


def semantic_brief_warning(line, trace=None):
    # This is the renderer's measured-size diagnostic, not arbitrary limits or
    # quotas. The configured brief_max_kb, section names and instructions survive.
    ending = '. Shorten task/context or use path references; retain limits, guard and hand-back instructions.'
    pattern = (r'^((?:warning: |brain: )?)brief is (\d+) bytes (\(brief_max_kb=[\d.]+\); largest sections: )'
               r'([^\n]+)' + re.escape(ending) + r'$')
    match = re.fullmatch(pattern, line)
    if not match:
        return line
    sections = match[4].split('; ')
    if any(not re.fullmatch(r'.+: \d+ bytes', section) for section in sections):
        return line
    if trace is not None:
        counts = [match[2], *[section.rsplit(': ', 1)[1] for section in sections]]
        previous = trace.get('brief-rendered-byte-counts', '')
        trace['brief-rendered-byte-counts'] = hashlib.sha256((previous + json.dumps(counts)).encode()).hexdigest()
    # Keep the normalized form structurally distinct from unrecognized text,
    # including a candidate that literally prints publication placeholders.
    return ('brief-rendered-byte-counts', match[1], match[3],
            tuple(section.rsplit(': ', 1)[0] for section in sections), ending)


def comparison_text(row):
    return row.get('semantic_text', row['text'])


def comparison_stdout(row):
    return row.get('semantic_stdout', comparison_text(row))


def comparison_digest(value):
    # semantic_ui uses a structural sentinel for volatile numbers. Its encoding
    # cannot collide with a raw scalar, which carries an explicit type tag.
    def structural_token(item):
        if item is _VOLATILE_NUMBER:
            return ['volatile-number']
        raise TypeError('unsupported comparison value')
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, default=structural_token)
    return hashlib.sha256(encoded.encode()).hexdigest()


def stderr_comparison(row):
    return row.get('stderr_digest', comparison_digest(''))


def normalize(text, roots=(), trace=None):
    def record(name, values):
        if trace is not None and values:
            trace[name] = hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()
    paths = []
    for root in sorted(map(str, roots), key=len, reverse=True):
        paths.extend([root] * text.count(root))
        text = text.replace(root, '<copy>')
    record('copy-paths', paths)
    record('timestamps', re.findall(OUTPUT_NORMALIZATION_PATTERNS['timestamps'], text))
    text = re.sub(OUTPUT_NORMALIZATION_PATTERNS['timestamps'], '<time>', text)
    # Path substitutions happen after brief measurement. Different copy paths
    # therefore change byte totals even when the warning itself is unchanged.
    record('warning-byte-sizes', [token for line in text.splitlines() if is_warning(line)
           for token in re.findall(OUTPUT_NORMALIZATION_PATTERNS['warning-byte-sizes'], line, re.I)])
    text = ''.join(normalize_warning(line) if is_warning(line) else line
                   for line in text.splitlines(keepends=True))
    # JSON object ordering must not create a diff; list ordering remains significant.
    try:
        canonical = json.dumps(json.loads(text), sort_keys=True, ensure_ascii=False)
        record('json-encoding', text)
        return canonical
    except ValueError:
        return text.strip()


def is_warning(line):
    return re.search(r'warn|limit|stop|consent|✗|!', line, re.I)


def normalize_warning(line):
    # Replace only the number, preserving its unit, spacing and line endings.
    return re.sub(OUTPUT_NORMALIZATION_PATTERNS['warning-byte-sizes'],
                  '<size>', line, flags=re.I)


def expected_change_notices(inst, nodes, baseline, candidate):
    """Attribute only exact notices made solely from candidate CHANGES entries.

    Stable identities handle rewritten existing notices. New notices need this
    additional provenance check; a blanket exemption for core-changes would hide
    scheduler regressions. Keep rendering aligned with status.changes_due.
    """
    def entries(core):
        path = core / 'CHANGES.md'
        return _m_upgrade.changes_entries(path.read_text()) if path.is_file() else []
    old = {entry['id']: entry for entry in entries(baseline)}
    fresh = entries(candidate)
    changed = {entry['id'] for entry in fresh if old.get(entry['id']) != entry}
    expected = {}
    terms = [term for node in nodes for term in _m_guard.never_terms(node)]
    for i, node in enumerate(nodes):
        if node['id'] != 'root' and not node['local'].get('coordinator'):
            continue
        seen = inst.local.get('changes_seen', {}).get(node['id'])
        if seen is None:
            seen = _m_upgrade.changes_baseline(inst)
        if seen is None:
            continue  # A never-upgraded brain starts with no unread entries.
        unread = [entry for entry in fresh if entry['id'] not in set(seen)]
        if not unread or any(entry['id'] not in changed for entry in unread):
            continue
        text = (f"core changed: {len(unread)} unread entr{'y' if len(unread) == 1 else 'ies'} ("
                + '; '.join(entry['id'] for entry in unread[:3]) + (' …' if len(unread) > 3 else '')
                + f"); read them: brain changes {node['id']}, then brain changes {node['id']} --seen")
        item = dict(node=node['id'], kind='core-changes', text=_m_nodes.import_text(text, _m_guard.never_terms(node)))
        expected['due:' + str(i)] = json.loads(redact_output(semantic_output(json.dumps(item)), terms))
    return expected


def redaction_spans(text, terms):
    """Locate folded matches, merging overlaps before replacing content.

    guard.redact can blank an entire output for full Unicode folds. Map folded
    offsets back to source characters instead, and merge all overlapping matches
    before replacing them so term order cannot expose or partially mask a match.
    """
    folded, offsets = [], []
    for i, char in enumerate(text):
        part = char.casefold()
        folded.append(part)
        offsets.extend([i] * len(part))
    folded = ''.join(folded)
    spans = []
    for term, _ in terms:
        needle = term.casefold()
        if not needle:
            continue
        start = folded.find(needle)
        while start >= 0:
            spans.append((offsets[start], offsets[start + len(needle) - 1] + 1))
            start = folded.find(needle, start + 1)
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def redact_text(text, terms):
    parts, cursor = [], 0
    for start, end in redaction_spans(text, terms):
        parts.extend((text[cursor:start], '[…]'))
        cursor = end
    return ''.join(parts) + text[cursor:]


def redact_channel_boundary(stdout, stderr, terms):
    # Separating channels must not expose a term split across their boundary.
    # Each channel's remaining JSON strings are decoded/redacted independently.
    boundary = len(stdout)
    for start, end in redaction_spans(stdout + stderr, terms):
        if start < boundary < end:
            return stdout[:start] + '[…]', '[…]' + stderr[end - boundary:]
    return stdout, stderr


def redact_output(text, terms):
    # Match decoded JSON strings too, including names with quotes/backslashes.
    # Replace individual tokens rather than rebuilding dicts: protected keys
    # may redact to the same spelling, but must not collapse distinct fields.
    try:
        json.loads(text)
    except ValueError:
        pass
    else:
        text = re.sub(r'"(?:[^"\\]|\\.)*"',
                      lambda m: json.dumps(redact_text(json.loads(m[0]), terms), ensure_ascii=False), text)
    return redact_text(text, terms)


def check_error_items(row):
    """Count error lines within JSON leaves, preserving their item identity."""
    errors = collections.Counter()
    def visit(value, path=()):
        if isinstance(value, dict):
            for key, item in value.items():
                visit(item, path + (key,))
        elif isinstance(value, list):
            for item in value:
                identity = tuple((key, json.dumps(item[key], sort_keys=True))
                                 for key in ('node', 'kind', 'id', 'branch') if key in item) if isinstance(item, dict) else ()
                visit(item, path + (identity,))
        elif isinstance(value, str):
            errors.update((path, line) for line in value.splitlines() if '✗' in line)
    stdout = comparison_stdout(row)
    try:
        visit(json.loads(stdout))
    except ValueError:
        visit(stdout)
    visit(row.get('semantic_stderr', row.get('stderr', '')), ('stderr',))
    return errors


def added_check_errors(old, new):
    return [line for (_, line) in (check_error_items(new) - check_error_items(old)).elements()]


def baseline_conditions(key, row):
    conditions = (['timeout'] if row['timeout'] else []) + (['unexpected writes'] if row['unexpected'] else [])
    # JSON needs stdout; a textual failure can legitimately use stderr alone.
    text = comparison_stdout(row) if key == 'ui' or key.startswith('due:') else comparison_text(row)
    if (key in ('ui', 'check', 'fix') or key.startswith(('due:', 'ready:', 'brief:'))) and not text.strip():
        conditions.append('empty output')
    if key == 'ui':
        if row['code'] != 0:
            conditions.append('abnormal exit')
        summary = row['ui_comparison'] if 'ui_comparison' in row else ui_comparison(text)
        if not summary['valid']:
            conditions.append('invalid output')
    return conditions


def diagnostic_line(line, terms, identifiers=()):
    # Publication masks private identifiers and whole path tokens, including
    # normalized copy paths, before protected terms can obscure path markers.
    # Keep this rendering separate from comparison so it cannot hide a delta.
    line = re.sub(r'''(?<!\w)(["'])(?=[^"'\n]*[/\\])[^"'\n]*\1''', '<path>', line)
    # There is no safe delimiter between an unquoted path containing spaces
    # and the surrounding message. Refuse to publish any part of that line.
    if re.search(r'[/\\].*\s+\S', line):
        return '✗ <redacted path diagnostic>'
    line = re.sub(r'''[^\s"'(),;]*[/\\][^\s"'(),;]*''', '<path>', line)
    line = re.sub(r'''[^\s"'(),;<>]*\.[A-Za-z][\w.-]*''', '<path>', line)
    # Invalid references (unknown nodes, principals, schedules) need not occur
    # in the snapshot's id lists. Check reports these values in quotes.
    line = re.sub(r'''(?<!\w)(["'])[^"'\n]*\1''', '[…]', line)
    for identifier in sorted(set(identifiers), key=len, reverse=True):
        line = re.sub(r'(?<![\w.-])' + re.escape(identifier) + r'(?![\w.-])',
                      '[…]', line, flags=re.I)
    return redact_text(line, terms)


def ui_readiness(value):
    """Compare the parsed readiness contract, including reasons on blocked jobs."""
    rows = {}
    nodes = value.get('nodes', [])
    if not isinstance(nodes, list):
        raise ValueError('invalid UI nodes')
    identities = set()
    for node in nodes:
        if (not isinstance(node, dict) or not isinstance(node.get('id'), str) or not node['id']
                or not isinstance(node.get('worktrees'), list) or node['id'] in identities):
            raise ValueError('invalid UI node')
        identities.add(node['id'])
        for worktree in node.get('worktrees', []):
            if not isinstance(worktree, dict):
                raise ValueError('invalid UI worktree')
            ready = worktree.get('ready')
            if ready is not None and not isinstance(ready, dict):
                raise ValueError('invalid UI readiness')
            if ready is not None and (type(ready.get('ready')) is not bool
                    or not isinstance(ready.get('reasons'), list)
                    or any(not isinstance(reason, str) for reason in ready['reasons'])):
                raise ValueError('invalid UI readiness values')
            identity = json.dumps([node.get('id'), worktree.get('branch')], sort_keys=True)
            if identity in rows:
                raise ValueError('duplicate UI worktree')
            rows[identity] = ({k: ready.get(k) for k in ('ready', 'reasons')}
                              if ready is not None else None)
    return rows


def ui_comparison(text, trace=None):
    """Validate original identities; retain only content-free comparison data."""
    try:
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError('invalid UI object')
        readiness = ui_readiness(value)
        semantic = semantic_ui(value, trace)
    except (ValueError, TypeError):
        return dict(valid=False)
    return dict(valid=True, keys=comparison_digest(sorted(value)),
                readiness=comparison_digest(readiness), semantic=comparison_digest(semantic))


def compare(before, after, expected_changes=None):
    """Return content-free regression labels from private comparison views."""
    flags = []
    for key, old in before.items():
        new = after[key]
        # Incompleteness cannot provide an error/readiness/semantic baseline.
        # Candidate safety failures still fail independently of that baseline.
        if new['unexpected']:
            flags.append(key + ': unexpected writes')
        if key == 'hook' and new['seconds'] > 1:
            flags.append('hook: latency exceeds 1 s')
        if new['timeout']:
            flags.append(key + ': timeout')
        if baseline_conditions(key, old):
            flags.append(key + ': baseline incomplete')
            if key == 'ui' and new['code'] == 0:
                current = new['ui_comparison'] if 'ui_comparison' in new else ui_comparison(comparison_stdout(new))
                if not current['valid']:
                    flags.append('ui: invalid output')
            continue
        if new['code'] != 0 and (old['code'] == 0 or new['code'] != old['code']):
            flags.append(key + ': new command failure')
        if added_check_errors(old, new):
            flags.append(key + ': new check errors')
        if stderr_comparison(old) != stderr_comparison(new):
            flags.append(key + ': stderr changed')
        ready = lambda x: x['code'] == 0 and x['text'].splitlines()[-1:] == ['ready']
        if key.startswith('ready:') and ready(old) and not ready(new):
            flags.append(key + ': ready became not ready')
        if (key.startswith('ready:') and comparison_text(old) != comparison_text(new)
                and comparison_text(old).startswith('not ready:')
                and comparison_text(new).startswith('not ready:')):
            flags.append(key + ': ready reason changed')
        if key.startswith('due:') and not identical_baseline_failure(old, new):
            try:
                def items(value):
                    parsed = json.loads(comparison_stdout(value))
                    if not isinstance(parsed, list) or any(not isinstance(item, dict) for item in parsed):
                        raise ValueError('invalid due items')
                    return parsed
                identity = lambda item: json.dumps([item.get(k) for k in ('node', 'kind', 'id')], sort_keys=True)
                previous, current = items(old), items(new)
                added = (collections.Counter(map(identity, current))
                         - collections.Counter(map(identity, previous)))
                expected = (expected_changes or {}).get(key)
                if (expected is not None and expected in current and added[identity(expected)]
                        and all(identity(item) != identity(expected) for item in previous)):
                    added[identity(expected)] -= 1
                if +added:
                    flags.append(key + ': new due items')
            except (ValueError, TypeError):
                flags.append(key + ': invalid due output')
        if key.startswith('brief:'):
            warnings = lambda x: collections.Counter(normalize_warning(l) for l in x['text'].splitlines()
                                                      if is_warning(l))
            if warnings(old) - warnings(new):
                flags.append(key + ': brief warning disappeared')
            semantic_warnings = lambda row: collections.Counter(semantic_brief_warning(line)
                for line in comparison_text(row).splitlines() if is_warning(line))
            if (semantic_warnings(old) - semantic_warnings(new)
                    and key + ': brief warning disappeared' not in flags):
                flags.append(key + ': brief warning disappeared')
        if key == 'ui' and old['code'] == new['code'] == 0:
            previous, current = (row['ui_comparison'] if 'ui_comparison' in row else
                                 ui_comparison(comparison_stdout(row)) for row in (old, new))
            if not previous['valid'] or not current['valid']:
                flags.append('ui: invalid output')
            else:
                if previous['keys'] != current['keys']:
                    flags.append('ui: output keys changed')
                if previous['readiness'] != current['readiness']:
                    flags.append('ui: readiness changed')
                if previous['semantic'] != current['semantic']:
                    flags.append('ui: semantic output changed')
    return flags


def identical_baseline_failure(old, new):
    # Publication formatting cannot turn changed operands into a shared failure.
    return (old['code'] != 0 and old['code'] == new['code'] and comparison_text(old) == comparison_text(new)
            and stderr_comparison(old) == stderr_comparison(new)
            and not any(r['timeout'] or r['unexpected'] for r in (old, new)))


def sandbox_prefix(folder):
    exe = Path('/usr/bin/sandbox-exec')
    if sys.platform != 'darwin' or not exe.is_file():
        raise RehearsalError('rehearsal requires an OS write/network sandbox (currently macOS sandbox-exec)', code='rehearsal-requires-an-os-write-network-sandbox-currently-macos-sandbox-exec')
    # Refuse a shadow executable as well as avoiding its execution. The caller's
    # PATH is also inherited by Git/Python helpers, so don't silently trust it.
    found = shutil.which('sandbox-exec')
    if found is not None and Path(found).resolve() != exe:
        raise RehearsalError('shadowed sandbox executable refused', code='shadowed-sandbox-executable-refused')
    # Sandbox string literals use JSON-compatible escaping. File writes are allowed
    # only to this command's copy; even another lane's copy is read-only.
    profile = ('(version 1)(allow default)(deny network*)(deny mach-lookup)(deny appleevent-send)'
               '(deny signal (target others))(deny file-write*)'
               '(allow file-write* (subpath ' + json.dumps(str(folder.resolve())) + '))'
               '(allow file-write* (literal "/dev/null"))')
    return [str(exe), '-p', profile]


def sandbox_self_check(folder):
    """Prove denied writes and network, not merely that a launcher exits zero."""
    import socket
    with tempfile.TemporaryDirectory(prefix='brain-rehearse-denied-') as other:
        target = Path(other) / 'canary'
        target.write_bytes(b'unchanged')
        with socket.socket() as server:
            server.bind(('127.0.0.1', 0))
            server.listen(1)
            script = '''import pathlib, socket, sys
denied = 0
try:
    pathlib.Path(sys.argv[1]).write_bytes(b'changed')
except OSError:
    denied += 1
try:
    socket.create_connection(('127.0.0.1', int(sys.argv[2])), timeout=1).close()
except OSError:
    denied += 1
sys.exit(0 if denied == 2 else 1)
'''
            result = subprocess.run([*sandbox_prefix(folder), sys.executable, '-I', '-B', '-c', script,
                                     str(target), str(server.getsockname()[1])], capture_output=True, timeout=5)
            if result.returncode or target.read_bytes() != b'unchanged':
                raise RehearsalError('sandbox enforcement self-check failed', code='sandbox-enforcement-self-check-failed')


def allowed_write(rel, kind):
    if (rel.startswith('home/.local/state/brain/') or rel.startswith('tmp/')
            or rel.startswith('home/Library/Caches/com.apple.DeveloperTools/')
            or rel in ('home/.local', 'home/.local/state', 'home/.local/state/brain',
                       'home/brains.json', 'home/write-attempt', 'home/rehearsal-clock-bound',
                       'home/Library', 'home/Library/Caches',
                       'home/Library/Caches/com.apple.DeveloperTools')):
        return True
    if not rel.startswith('brain/'):
        return False
    rel = rel[6:]
    if (rel in ('.brain/lock', '.brain/machine.json', '.brain/core-remote.json', '.brain/hooks.json',
                '.brain/hooks.log', '.brain/inbox-seen.json', '.brain/wake', '.brain/host-questions', '.brain/spend',
                '.brain/docker-leftovers.json', '.brain/docker-leftovers.lock')
            or rel.startswith(('.brain/wake/', '.brain/host-questions/', '.brain/spend/'))):
        return True
    if rel == '.brain/local.json':
        return kind == 'fix'  # the supported migration moves machine bindings here
    if rel in ('.brain',):
        return True
    note = re.fullmatch(r'(?:nodes/[^/]+/)*([^/]+)', rel)
    if kind == 'fix':
        return (rel.startswith('.git/') or rel in ('.gitignore', 'registry.md')
                or (note and note[1] in ('node.md', 'status.md', 'brief.md', 'AGENTS.md', 'CLAUDE.md',
                                         'inbox.md', 'decisions.md'))
                or bool(re.fullmatch(r'(?:nodes/[^/]+/)*(?:\.claude|\.codex|\.agents)(?:/(?:settings.json|hooks.json|skills))?', rel)))
    if kind == 'refresh':
        return rel.startswith('.git/') or (note and note[1] == 'status.md')
    return False


def bind_git_executable(env):
    # Apple's /usr/bin/git is a developer-tool launcher. Its first use under a
    # sandbox can run xcodebuild and wait on denied system services. Resolve the
    # actual selected Git before admission; probes and helpers still sandbox it.
    if sys.platform == 'darwin' and shutil.which('git', path=env.get('PATH')) == '/usr/bin/git':
        r = subprocess.run(['/usr/bin/xcrun', '--find', 'git'], capture_output=True, text=True, timeout=5)
        git = Path(r.stdout.strip())
        if r.returncode or not git.is_absolute() or not git.is_file():
            raise RehearsalError('Git executable unavailable', code='git-executable-unavailable')
        env['PATH'] = str(git.parent) + os.pathsep + env.get('PATH', '')


def group_members(pgid):
    """Observe a denied group without spawning a helper that might need stopping."""
    import ctypes
    try:
        lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
        query = lib.proc_listpgrppids
        query.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        query.restype = ctypes.c_int
        members = (ctypes.c_int * 65536)()
        ctypes.set_errno(0)
        count = query(pgid, members, ctypes.sizeof(members))  # returns PID count, not bytes
        complete = 0 <= count < len(members) and not ctypes.get_errno()
        return sorted(pid for pid in members[:max(0, min(count, len(members)))] if pid > 0), complete
    except (OSError, AttributeError):
        return [], False


def report_leftover(inst, n, row):
    """Publish immediately: a later refusal or repeat must not erase this stop request."""
    members = ','.join(map(str, row['pids'])) or 'none observed'
    if not row['members_complete']:
        members += ' (scan incomplete)'
    text = ('rehearsal process leftover: pgid=' + str(row['pgid']) + '; PIDs=' + members +
            '; EPERM; no further signals sent; operator stop: ' + row['stop_command'])
    print('diagnostic: ' + text, file=sys.stderr, flush=True)
    _m_transitions.inbox_append(n['dir'], '- ' + inst.now() + ' from rehearse: ' + text + '\n')


def run_command(core, lane, args, kind, roots, terms=(), cache=None, raw_output=None, on_leftover=None):
    retention = NativeRetention(lane, 'before-command-setup',
        args=args, kind=kind, root=str(lane / 'brain'), before='UNKNOWN', after='UNKNOWN', observations='UNKNOWN',
        validation_instant='UNKNOWN', validator_result='UNKNOWN', raw_trace_hex='UNKNOWN')
    token = _m_transactions._validation_retainer.set(retention)
    try:
        return _run_command(core, lane, args, kind, roots, terms, cache, raw_output,
                            on_leftover, retention)
    except BaseException as error:
        retention.exception(error)
        raise
    finally:
        trace_path = retention.packet.get('trace_path')
        if retention.destination and trace_path is not None:
            try:
                retention.checkpoint(command_finally_trace_hex=Path(trace_path).read_bytes().hex())
            except BaseException as error:
                retention.incomplete = True
                retention.exception(error)
        retention.checkpoint(stage='command-finally')
        _m_transactions._validation_retainer.reset(token)


def _run_command(core, lane, args, kind, roots, terms, cache, raw_output, on_leftover, retention):
    deadline = ui_probe_deadline(args)  # Validate before starting anything.
    brain = lane / 'brain'
    home = lane / 'home'
    home.mkdir(exist_ok=True)
    tmp = lane / 'tmp'
    tmp.mkdir(exist_ok=True)
    unrelated = lane / 'unrelated'
    unrelated.mkdir(exist_ok=True)
    if kind == 'hook':
        (home / 'brains.json').write_text(json.dumps({
            'brains': {'rehearsal': {'root': str(brain)}}, 'default': 'rehearsal'}))
    bootstrap = lane / 'bootstrap'
    bootstrap.mkdir(exist_ok=True)
    clock_file = lane / 'rehearse-clock'
    clock = read_clock(clock_file) if clock_file.exists() else None
    # Bind input clocks, never thresholds, lock deadlines or elapsed timers.
    # Helpers inherit the same startup hook and instant as the probe itself.
    (bootstrap / 'rehearsal_clock.py').write_text('''import datetime, importlib, importlib.util, os, sys, time
from pathlib import Path
def pin():
    sys.path.insert(0, str(Path(os.environ['BRAIN_ROOT']) / 'core/lib'))
    from brain.instance import Instance
    instant = datetime.datetime.fromisoformat(os.environ['BRAIN_REHEARSAL_NOW'])
    def now_dt(self):
        return instant.astimezone(self.zone())
    Instance.now_dt = now_dt
    class WallClock:
        def time(self):
            return instant.timestamp()
        def __getattr__(self, name):
            return getattr(time, name)
    wall = WallClock()
    for name in __CLOCK_MODULES__:
        qualified = 'brain.' + name
        if importlib.util.find_spec(qualified) is not None:
            module = importlib.import_module(qualified)
            if getattr(module, 'time', None) is time:
                module.time = wall
'''.replace('__CLOCK_MODULES__', repr(FROZEN_WALL_CLOCK_MODULES)))
    # Copy the supervising implementation, never a candidate-supplied validator.
    (bootstrap / 'rehearsal_transactions.py').write_bytes(Path(_m_transactions.__file__).read_bytes())
    # Every Python helper inherits this supervisor. Detached wake drains remain
    # in the command's group, and each Python parent reaps its children on exit.
    (bootstrap / 'sitecustomize.py').write_text('''import atexit, os, subprocess
children = []
original = subprocess.Popen
class SupervisedPopen(original):
    def __init__(self, *args, **kwargs):
        kwargs['start_new_session'] = False
        super().__init__(*args, **kwargs)
        children.append(self)
subprocess.Popen = SupervisedPopen
def reap():
    for child in reversed(children):
        # The outer group supervisor alone owns signaling. In particular an
        # EPERM there must not be followed by a child-side atexit kill.
        if child.poll() is not None:
            child.wait()
atexit.register(reap)
try:
    import rehearsal_audit
    if os.environ.get('BRAIN_REHEARSAL_TRANSACTIONS'):
        import sys
        from pathlib import Path
        library = Path(os.environ['BRAIN_ROOT']) / 'core/lib'
        if (library / 'brain/events.py').is_file():
            sys.path.insert(0, str(library))
            from brain import events
            from rehearsal_transactions import install_observer
            install_observer(events, os.environ['BRAIN_ROOT'], os.environ['BRAIN_REHEARSAL_TRANSACTIONS'])
    if os.environ.get('BRAIN_REHEARSAL_NOW'):
        from rehearsal_clock import pin
        pin()
except BaseException:
    # Sticky across helpers: an ignored helper failure must still refuse evidence.
    try:
        fd = os.open(os.environ['BRAIN_REHEARSAL_FAILURE'], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    except OSError:
        pass
    os._exit(70)
''')
    audit = lane / 'audit.py'
    violation = home / 'write-attempt'
    violation.unlink(missing_ok=True)
    clock_bound = home / 'rehearsal-clock-bound'
    clock_bound.unlink(missing_ok=True)
    startup_failed = home / 'rehearsal-startup-failed'
    startup_failed.unlink(missing_ok=True)
    # OS sandboxing is the enforcement boundary. The audit hook additionally
    # observes Python attempts even when candidate code catches a denied write.
    (bootstrap / 'rehearsal_audit.py').write_text('''import json, os, pathlib, sys
boundary = pathlib.Path(os.environ['BRAIN_REHEARSAL_BOUNDARY']).resolve()
marker = pathlib.Path(os.environ['BRAIN_REHEARSAL_MARKER'])
busy = False
recorded_open = None
pathlib_open_code = pathlib.Path.open.__code__
def audit(event, args):
    global busy, recorded_open
    if busy:
        return
    paths = []
    if event == 'open':
        mode, flags = args[1:3]
        if (isinstance(mode, str) and any(c in mode for c in 'wax+')) or (isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)):
            paths = args[:1]
    elif event in ('os.remove', 'os.rmdir', 'os.mkdir', 'os.chmod', 'os.chown', 'os.truncate', 'os.utime'):
        paths = args[:1]
    elif event in ('os.rename', 'os.link'):
        paths = args[:2]
    elif event == 'os.symlink':
        paths = args[1:2]
    busy = True
    try:
        for name in paths:
            if not isinstance(name, (str, bytes)):
                continue
            p = pathlib.Path(os.fsdecode(name)).resolve()
            if p != pathlib.Path('/dev/null') and boundary != p and boundary not in p.parents:
                # Python 3.9 pathlib passes an opener to io.open. Its nested
                # os.open callback represents the same operation, after the
                # high-level callback has already recorded the violation.
                # Consume only that exact parent-frame/path/flags pair once.
                caller = sys._getframe(1)
                if (event == 'open' and args[1] is None and caller.f_back is not None
                        and caller.f_back.f_code is pathlib_open_code
                        and recorded_open == (str(p), args[2], id(caller.f_back))):
                    recorded_open = None
                    del caller
                    continue
                with marker.open('a') as stream:
                    stream.write(json.dumps(dict(event=event, path=str(p))) + '\\n')
                recorded_open = ((str(p), args[2], id(caller))
                                 if event == 'open' and isinstance(args[1], str)
                                 and caller.f_code is pathlib_open_code else None)
                del caller
    finally:
        busy = False
sys.addaudithook(audit)
''')
    audit.write_text('''import os, pathlib, runpy, sys
entry = sys.argv[3]
sys.argv = sys.argv[3:]
# The startup hook installs supervision and auditing before lane-owned imports.
import rehearsal_audit
if os.environ.get('BRAIN_REHEARSAL_NOW'):
    from rehearsal_clock import pin
    pin()
    (pathlib.Path(os.environ['HOME']) / 'rehearsal-clock-bound').write_text(os.environ['BRAIN_REHEARSAL_NOW'])
runpy.run_path(entry, run_name='__main__')
''')
    # Deliberately build an allowlisted environment. No inherited Brain lock,
    # provider credentials, registries, Python startup hooks or Git overrides.
    env = {k: v for k, v in os.environ.items() if k in ('PATH', 'LANG', 'LC_ALL', 'TZ')}
    env.update(HOME=str(home), TMPDIR=str(tmp), BRAIN_CONFIG=str(home / 'brains.json'),
               PYTHONPATH=str(bootstrap), BRAIN_REHEARSAL_BOUNDARY=str(lane),
               BRAIN_REHEARSAL_MARKER=str(violation),
               BRAIN_REHEARSAL_FAILURE=str(startup_failed),
               XDG_CONFIG_HOME=str(home / '.config'), BRAIN_ROOT=str(brain),
               BRAIN_NO_REEXEC='1', BRAIN_NO_UPDATE_CHECK='1', PYTHONDONTWRITEBYTECODE='1',
               BRAIN_USAGE_TEST_NO_KEYCHAIN='1', GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1',
               GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0',
               GIT_AUTHOR_NAME='Rehearsal', GIT_AUTHOR_EMAIL='rehearsal@example.invalid',
               GIT_COMMITTER_NAME='Rehearsal', GIT_COMMITTER_EMAIL='rehearsal@example.invalid')
    if clock is not None:
        bind_git_executable(env)
        env['BRAIN_REHEARSAL_NOW'] = clock
        epoch = int(datetime.datetime.fromisoformat(clock).timestamp())
        env['GIT_TEST_DATE_NOW'] = str(epoch)
        # Git parses relative --since dates itself. Prove this executable honors
        # the reference instant; unsupported Git must refuse, not compare drift.
        check = subprocess.run([*sandbox_prefix(lane), 'git', '-C', str(brain),
                                'rev-parse', '--since=24 hours ago'],
                               env=env, capture_output=True, text=True, timeout=5)
        if check.returncode or check.stdout.strip() != '--max-age=' + str(epoch - 86400):
            raise RehearsalError('Git frozen clock binding failed', code='git-frozen-clock-binding-failed')
    trace = home / '.local/state/brain/rehearsal-transactions.jsonl'
    transaction_before = None
    capture_before_metadata = {}
    if kind in ('fix', 'refresh'):
        _m_common.atomic_write(trace, '', private=True, create=True)
        env['BRAIN_REHEARSAL_TRANSACTIONS'] = str(trace)
        retention.bind_command_sources(core, bootstrap)
        retention.checkpoint(stage='capture-before', capture_before_metadata=capture_before_metadata,
                             trace_path=str(trace))
        try:
            transaction_before = _m_transactions.capture(brain, capture_before_metadata)
        finally:
            retention.checkpoint(before=transaction_before if sys.exc_info()[0] is None else 'UNKNOWN',
                                 capture_before_metadata=capture_before_metadata)
    before = snapshot(lane, cache)
    start = time.monotonic()
    p = subprocess.Popen([*sandbox_prefix(lane), sys.executable, '-B', str(audit), str(lane), str(violation),
                          str(core / 'bin/brain'), *args],
                         cwd=unrelated if kind == 'hook' else brain, env=env, stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors='replace', start_new_session=True)
    owner = _m_rehearse_external.ACTIVE_COPIES.get()
    if owner is not None:
        owner.processes.append(p)
    timed_out = False
    leftovers = []
    signal_audit = []
    def stop_group():
        if leftovers:
            return False  # EPERM is terminal for signaling this probe's group.
        audit = dict(pid=p.pid, pgid=p.pid, signal=int(signal.SIGKILL),
                     at=datetime.datetime.now(datetime.timezone.utc).isoformat(), result='attempted')
        signal_audit.append(audit)
        if owner is not None:
            owner.signals.append(audit)
        try:
            os.killpg(p.pid, signal.SIGKILL)
            audit['result'] = 'sent'
        except ProcessLookupError:
            audit['result'] = 'absent'
        except PermissionError as error:
            audit.update(result='denied', errno=error.errno)
            if error.errno != errno.EPERM:
                raise
            row = dict(pgid=p.pid, pids=[], members_complete=False, stop_command='kill -TERM -' + str(p.pid))
            row['pids'], row['members_complete'] = group_members(p.pid)
            leftovers.append(row)
            if on_leftover is not None:
                on_leftover(row)
            return False
        return True
    try:
        out, err = p.communicate(json.dumps({'cwd': str(unrelated)}) if kind == 'hook' else '', timeout=deadline)
    except subprocess.TimeoutExpired as error:
        timed_out = True
        if stop_group():
            out, err = p.communicate()
        else:
            # The denied process may still hold the pipes. Never wait for its EOF.
            out, err = error.output or '', error.stderr or ''
            out = out.decode(errors='replace') if isinstance(out, bytes) else out
            err = err.decode(errors='replace') if isinstance(err, bytes) else err
    finally:
        # Stop remaining processes in this command's group. Any detached helper
        # still inherits the OS sandbox and cannot write to the live machine.
        if stop_group():
            p.wait()
        elif owner is not None:
            owner.unknown = True
        retention.packet.update(stdout=locals().get('out', 'UNKNOWN'),
                                stderr=locals().get('err', 'UNKNOWN'), owner_pid=p.pid,
                                code=p.returncode, timeout=timed_out, signal_audit=signal_audit)
    elapsed = time.monotonic() - start
    retention.checkpoint(stage='command-boundary')
    if startup_failed.exists() or startup_failed.is_symlink() or (clock is not None and
                                  (not clock_bound.is_file() or clock_bound.read_text() != clock)):
        raise RehearsalError('probe frozen clock binding failed', code='probe-frozen-clock-binding-failed')
    # Normalize JSON escapes before redaction as well: an escaped spelling must
    # not become a protected literal after the privacy filter has run.
    normalization_tokens = {}
    normalized = normalize(out + err, roots, trace=normalization_tokens)
    text = redact_output(normalized, terms)
    semantic = semantic_output(out + err, roots)
    semantic_text = redact_output(semantic, terms)
    stdout_semantic, stderr_semantic = semantic_output(out, roots), semantic_output(err, roots)
    # Compute UI validity and semantic identities before publication redaction
    # merges protected names. Neither raw JSON nor identities leave this process.
    ui_summary = ui_comparison(stdout_semantic, normalization_tokens) if args[:1] == ['ui'] else None
    stderr_value = ([semantic_brief_warning(line) for line in stderr_semantic.splitlines()]
                    if args[:1] == ['brief'] else stderr_semantic)
    stderr_digest = comparison_digest(stderr_value)
    safe_out, safe_err = redact_channel_boundary(out, err, terms)
    stdout = redact_output(normalize(safe_out, roots), terms)
    stderr = redact_output(normalize(safe_err, roots), terms)
    semantic_stdout = redact_output(semantic_output(safe_out, roots), terms)
    semantic_stderr = redact_output(semantic_output(safe_err, roots), terms)
    if args[:1] == ['brief']:
        for line in semantic.splitlines():
            semantic_brief_warning(line, normalization_tokens)
    # Capture safe renderings before protected-term redaction can erase a path
    # marker and strand its whitespace-separated private suffix. Only these
    # values, never the lookup keys, may be published as error diagnostics.
    error_diagnostics = {redact_output(line, terms): diagnostic_line(normalize(line), terms)
                         for line in [*normalized.splitlines(), *semantic.splitlines()] if '✗' in line}
    after = snapshot(lane, cache)
    changed = {k for k in before.keys() | after.keys() if before.get(k) != after.get(k)}
    transaction_after, observations, proven = None, [], set()
    transaction_verdict = 'unproven'
    diagnostics, capture_after_metadata = {}, {}
    validation_now, raw_trace_hex = None, None
    if kind in ('fix', 'refresh'):
        retention.checkpoint(stage='capture-after')
        try:
            transaction_after = _m_transactions.capture(brain, capture_after_metadata)
        finally:
            retention.checkpoint(after=transaction_after if sys.exc_info()[0] is None else 'UNKNOWN',
                                 capture_after_metadata=capture_after_metadata)
        try:
            trace_bytes = trace.read_bytes()
            raw_trace_hex = trace_bytes.hex()
            observations = _m_transactions.lines(trace_bytes)
        except (OSError, ValueError) as error:
            observations = None
            diagnostics.update(checkpoint='trace-parse', exception_category=type(error).__name__)
        finally:
            retention.checkpoint(stage='trace-parse-finally', raw_trace_hex=raw_trace_hex,
                                 observations=observations, diagnostics=diagnostics)
        if not timed_out and not leftovers:
            validation_now = (datetime.datetime.fromisoformat(clock) if clock is not None else
                              datetime.datetime.now().astimezone())
            retention.checkpoint(stage='validate', validation_instant=validation_now.isoformat(),
                                 validator_command='status' if kind == 'refresh' else 'check')
            try:
                proven, transaction_verdict = _m_transactions.validate(
                    transaction_before, transaction_after, observations, p.pid,
                    'status' if kind == 'refresh' else 'check',
                    validation_now, diagnostics=diagnostics)
                retention.checkpoint(validator_result=dict(proven_paths=sorted(proven),
                                                           verdict=transaction_verdict))
            finally:
                retention.checkpoint(stage='validator-finally', diagnostics=diagnostics)
        else:
            diagnostics.update(checkpoint='supervision-bypass', exception_category='Timeout' if timed_out else 'Leftovers')
    unexpected_paths = sorted(k for k in changed if not allowed_write(k, kind) and k not in proven)
    denied_attempts = [json.loads(line) for line in violation.read_text().splitlines()] if violation.exists() else []
    if raw_output is not None:
        raw_output.append(dict(stdout=out, stderr=err, code=p.returncode, timeout=timed_out,
            writes=[dict(path=k, operation=('created' if k not in before else
                                          'removed' if k not in after else 'modified'),
                         unexpected=k in unexpected_paths) for k in sorted(changed)],
            denied_attempts=denied_attempts,
            transaction_evidence=dict(before=transaction_before, after=transaction_after,
                observations=observations, owner_pid=p.pid, verdict=transaction_verdict,
                validator_command='status' if kind == 'refresh' else 'check',
                validation_instant=validation_now.isoformat() if validation_now is not None else None,
                diagnostics=diagnostics, capture_before_metadata=capture_before_metadata,
                capture_after_metadata=capture_after_metadata, raw_trace_hex=raw_trace_hex)))
    retention.checkpoint(stage='command-result', raw_output=raw_output,
                         status='CAPTURED')
    result = {'text': text, 'semantic_text': semantic_text,
            'stdout': stdout, 'stderr': stderr, 'semantic_stdout': semantic_stdout,
            'semantic_stderr': semantic_stderr, 'stderr_digest': stderr_digest,
            'error_diagnostics': error_diagnostics, 'normalization_tokens': normalization_tokens,
            'code': p.returncode, 'timeout': timed_out,
            'leftovers': leftovers, 'signal_audit': signal_audit,
            'seconds': elapsed, 'unexpected': len(unexpected_paths) + int(violation.exists()),
            # Raw paths stay in the private sidecar; fingerprints let a local
            # operator attribute a known path without publishing private names.
            'unexpected_path_digests': [hashlib.sha256(os.fsencode(k)).hexdigest() for k in unexpected_paths],
            'denied_attempts': len(denied_attempts), 'transaction_evidence': transaction_verdict}
    if ui_summary is not None:
        result['ui_comparison'] = ui_summary
    return result


def read_clock(path):
    try:
        text = path.read_text()
        instant = datetime.datetime.fromisoformat(text)
        if instant.utcoffset() is None:
            raise ValueError('naive clock')
        return instant.isoformat()
    except (OSError, ValueError):
        raise RehearsalError('frozen rehearsal clock unavailable', code='frozen-rehearsal-clock-unavailable') from None


def ui_probe_deadline(args):
    if args[:1] != ['ui']:
        return 60
    try:
        value = float(os.environ.get('BRAIN_REHEARSE_UI_TIMEOUT', '60'))
        if not math.isfinite(value) or value <= 0:
            raise ValueError('invalid deadline')
        return value
    except ValueError:
        raise RehearsalError('UI probe deadline must be a finite positive number of seconds',
                             code='invalid-ui-probe-deadline') from None


def freeze_machine_inputs(inst, clock):
    """Use the normal sample reader in both copies, including sustained policy."""
    from . import machine as _m_machine
    from . import ui as _m_ui
    instant = datetime.datetime.fromisoformat(clock).timestamp()
    data = _m_machine.machine_samples(inst, fresh=False)
    if data and 0 <= instant - data['samples'][-1]['at'] <= 180:
        return  # Keep valid history and episode recovery semantics intact.
    data = dict(samples=[dict(at=instant, reading=_m_ui.machine_load())],
                episode=False, heat_reasons=[])
    path = inst.root / '.brain/machine.json'
    _m_common.atomic_write(path, json.dumps(data), private=True, create=True)


def external_inputs(inst):
    """Record live observation operands; never publish their paths or contents.

    The instance copy freezes local.json. External Git refs/index/config, tracked
    and untracked worktree metadata, ignored verification inputs, and workbench
    evidence remain live. Metadata includes ctime to detect rewrites/restores,
    even when Git's dirty path list and file size do not change. No object stores,
    ignored dependency trees or private host refs are scanned.
    """
    from . import worktrees as _m_worktrees
    records, repositories, seen = {}, [], set()
    def stamp(path):
        path = Path(path)
        def identity(st):
            return (st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        try:
            value = identity(path.lstat())
            if path.is_symlink():
                value += (os.readlink(path), identity(path.stat()))
            records[str(path)] = value
        except FileNotFoundError:
            records[str(path)] = None
    def linked_directory(path):
        if path.is_symlink() and path.is_dir():
            raise RehearsalError('linked external observation directory refused',
                                 code='linked-external-observation-directory-refused')
    def tree(root):
        linked_directory(root)
        stamp(root)
        if not root.is_dir():
            return
        def failed(error):
            raise error
        for dp, dirs, files in os.walk(root, followlinks=False, onerror=failed):
            for name in dirs:
                linked_directory(Path(dp) / name)
            for name in dirs + files:
                stamp(Path(dp) / name)
    bindings = getattr(inst, 'local', {})
    bound_roots = set()
    for node in inst.nodes():
        if not node.get('external'):
            continue
        loc = node.get('local', {})
        if node.get('home_path'):
            root = Path(node['home_path']).expanduser()
            repositories.append(root)
            bound_roots.add(root)
        for worktree in loc.get('worktrees', {}).values():
            if worktree.get('path'):
                path = Path(worktree['path']).expanduser()
                repositories.append(path)
                bound_roots.add(path)
                records['verify:' + str(path)] = _m_verify.verify_input_files(node, {'path': str(path)})
        wb = _m_worktrees.workbench(inst, node)
        bound_roots.add(wb)
        reports = wb / 'reports'
        linked_directory(reports)
        stamp(reports)
        if reports.is_dir():
            for directory in reports.iterdir():
                if directory.is_dir():
                    linked_directory(directory)
                    stamp(directory)
                    stamp(directory / 'index.md')
        for name in ('scratch/ready', 'scratch/rehearse'):
            tree(wb / name)
    def other_bindings(value):
        if isinstance(value, dict):
            for item in value.values():
                other_bindings(item)
        elif isinstance(value, list):
            for item in value:
                other_bindings(item)
        elif isinstance(value, str) and value.startswith(('/', '~/')):
            path = Path(value).expanduser()
            # Known repositories/workbenches have the narrower read sets above.
            # Other absolute bindings must not silently remain live/unrecorded.
            if path not in bound_roots and path != inst.root:
                tree(path)
    other_bindings(bindings)
    while repositories:
        root = repositories.pop()
        if root in seen:
            continue
        seen.add(root)
        stamp(root)
        if not root.exists():
            continue
        if not (root / '.git').exists():
            tree(root)
            continue
        own = validate_git_metadata(root)
        common = own / (own / 'commondir').read_text().strip() if (own / 'commondir').is_file() else own
        for directory in {own, common}:
            for name in ('HEAD', 'index', 'config', 'config.worktree', 'commondir', 'packed-refs'):
                stamp(directory / name)
            for name in ('heads', 'remotes', 'tags'):
                tree(directory / 'refs' / name)
        def git(*args):
            value = _m_common.git('-c', 'core.fsmonitor=false', *args, cwd=root)
            if value.returncode:
                raise RehearsalError('external observation inputs unavailable', code='external-inputs-unavailable')
            return value.stdout
        records['refs:' + str(root)] = git('for-each-ref', '--format=%(refname) %(objectname) %(symref)',
                                          'refs/heads', 'refs/remotes', 'refs/tags')
        listing = git('ls-files', '-z', '--cached', '--others', '--exclude-standard')
        for rel in set(listing.split('\0')) - {''}:
            path = root / rel
            stamp(path)
            parent = path.parent
            while parent != root and root in parent.parents:
                stamp(parent)
                parent = parent.parent
            if path.is_dir() and not path.is_symlink() and (path / '.git').exists():
                repositories.append(path)
    return comparison_digest([bindings, records])


def run_lane(source, core, folder, nodes, commands, on_leftover=None):
    retention = NativeRetention(folder, 'before-lane-setup')
    folder.mkdir()
    copy_brain(source, folder / 'brain', folder.parent)
    materialize_git(source, folder / 'brain')
    # A core submodule's .git file would otherwise point back to live metadata.
    old_core = folder / 'brain/core'
    if old_core.is_symlink():
        old_core.unlink()
    elif old_core.exists():
        shutil.rmtree(old_core)
    copy_tree(core, old_core)
    validate_frozen_links(folder / 'brain')
    clock = read_clock(source.parent / 'rehearse-clock')
    (folder / 'rehearse-clock').write_text(clock)
    results = {}
    cache = {}
    mutations = {}
    terms = [t for node in nodes for t in _m_guard.never_terms(node)]
    raw_outputs = {}
    for key, args, kind in commands:
        if kind in ('fix', 'refresh'):
            # Mutating probes get independent copies, never mask errors in later probes.
            lane = folder / ('mutation-' + kind)
            if kind not in mutations:
                lane.mkdir()
                copy_brain(folder / 'brain', lane / 'brain', folder.parent)
                (lane / 'rehearse-clock').write_text(clock)
                mutations[kind] = lane
        else:
            lane = folder
        raw = []
        try:
            with rehearsal_phase(('baseline' if folder.name == 'before' else 'candidate') + ':' + key):
                results[key] = run_command(lane / 'brain/core', lane, args, kind,
                                           [lane / 'brain', folder / 'brain', source, core, folder, lane], terms, cache,
                                           raw_output=raw, on_leftover=on_leftover)
            raw_outputs[key] = raw[0]
        except BaseException as error:
            retention.exception(error)
            raise
        finally:
            retention.checkpoint(stage='lane-command-finally', command=key,
                                 raw_output=raw, raw_outputs=raw_outputs, results=results)
    # This intermediate never leaves the rehearsal's private temporary directory.
    with (folder / 'probe-outputs.json').open('x') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(raw_outputs, stream)
    return results


def write_local_diff(inst, n, directory, before, after, metadata=None):
    """Retain raw diagnostics outside every repository; never return their path."""
    directory = Path(directory)
    for path in (directory, *directory.parents):
        if path.is_symlink():
            raise RehearsalError('linked local diagnostic path refused', code='linked-local-diagnostic-path-refused')
        if ((path / '.git').exists() or (path / '.git').is_symlink()
                or ((path / 'HEAD').is_file() and (path / 'objects').is_dir())):
            raise RehearsalError('local diagnostics must stay outside repositories', code='local-diagnostics-must-stay-outside-repositories')
    for root in (inst.root, n['home_path'], Path(__file__).resolve().parents[2]):
        root = root.resolve()
        if directory.resolve() == root or root in directory.resolve().parents:
            raise RehearsalError('local diagnostics must stay outside brain and core home', code='local-diagnostics-must-stay-outside-brain-and-core-home')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    permissions = directory.stat()
    if permissions.st_uid != os.getuid() or stat.S_IMODE(permissions.st_mode) != 0o700:
        raise RehearsalError('local diagnostic directory is not private', code='local-diagnostic-directory-is-not-private')
    fd, _ = tempfile.mkstemp(prefix='rehearse-', suffix='.diff', dir=directory)
    with os.fdopen(fd, 'w') as stream:
        stream.write('# Local rehearsal diff\n' + json.dumps(metadata or {}, sort_keys=True) + '\n')
        for key in sorted(before.keys() | after.keys()):
            # Equal writes on both sides are still safety failures. A unified
            # output diff alone would erase their attribution entirely.
            for label, rows in (('installed', before), ('candidate', after)):
                row = rows.get(key) or {}
                stream.write(json.dumps(dict(lane=label, command=key,
                    writes=row.get('writes', []), denied_attempts=row.get('denied_attempts', []),
                    transaction_evidence=row.get('transaction_evidence')),
                    sort_keys=True) + '\n')
            def lines(value):
                if value is None:
                    return ['missing command\n']
                rendered = ['exit=' + str(value['code']) + ' timeout=' + str(value['timeout']) + '\n']
                for channel in ('stdout', 'stderr'):
                    rendered.append(channel + ':\n')
                    parts = value[channel].splitlines(keepends=True)
                    if parts and not parts[-1].endswith('\n'):
                        parts[-1] += '\n'
                        parts.append('\\ No newline at end of ' + channel + '\n')
                    rendered.extend(parts)
                return rendered
            stream.writelines(difflib.unified_diff(lines(before.get(key)), lines(after.get(key)),
                                                  fromfile='installed/' + key, tofile='candidate/' + key))


def rehearsal_commands(nodes):
    commands, jobs = [], []
    for i, node in enumerate(nodes):
        commands.append(('due:' + str(i), ['due', node['id'], '--json'], 'read'))
        commands.append(('brief:' + str(i), ['brief', node['id']], 'read'))
        for j, row in enumerate(_m_jobs.read_jobs(node) or []):
            jobs.append(row['Job'])
            if not row.get('Ended'):
                commands.append(('ready:' + str(i) + ':' + str(j),
                                 ['ready', node['id'], '--branch', row['Job']], 'read'))
        commands.append(('refresh:' + str(i), ['status', node['id'], '--refresh'], 'refresh'))
    commands += [('check', ['check'], 'read'), ('fix', ['check', '--fix'], 'fix'),
                 ('ui', ['ui', '--json'], 'read'), ('hook', ['hook', 'stop'], 'hook')]
    return commands, jobs


def rehearse(inst, n, branch, local_diff_dir=None):
    if not n['external']:
        raise RehearsalError('rehearsal needs an external core home and workbench', code='rehearsal-needs-an-external-core-home-and-workbench')
    head, main = _m_verify.ready_refs(n, branch)
    integration_base = _m_verify.integration_parent(n, main)
    if not head or not main:
        raise RehearsalError('candidate branch or cached integration main missing', code='candidate-branch-or-cached-integration-main-missing')
    # Probe admission before any candidate is executed. The sandbox protects live
    # homes even when local.json points to them; they are read-only observations.
    with _m_rehearse_external.OwnedCopies() as copies:
        folder = copies.bulk
        copies.record(head=head, main=main, integration_base=integration_base)
        sandbox_self_check(folder)
        frozen = folder / 'source'
        (folder / 'rehearse-clock').write_text(inst.now_dt().isoformat())
        frozen_now = read_clock(folder / 'rehearse-clock')
        copy_brain(inst.root, frozen, folder)
        validate_frozen_links(frozen)
        materialize_git(frozen, frozen)
        # The copy is common fixed input, not an atomic historical snapshot.
        # Live writers are irrelevant after preparation; audit only private data.
        baseline_commit = installed_core_commit(frozen)
        copies.record(installed_core_commit=baseline_commit)
        # Keep core-relative links into the captured brain valid until the core
        # is installed into a lane. No extra standalone baseline copy is needed.
        baseline = frozen / 'core'
        candidate = folder / 'candidate'
        # Clone transports only Git objects; do not run setup, hooks or a checkout
        # in the original home. No hard links and no origin survives in the copy.
        prep_home = folder / 'prepare-home'
        prep_home.mkdir()
        prep_env = {k: v for k, v in os.environ.items() if k in ('PATH', 'LANG', 'LC_ALL', 'TZ')}
        prep_env.update(HOME=str(prep_home), TMPDIR=str(folder), GIT_CONFIG_GLOBAL='/dev/null',
                        GIT_CONFIG_NOSYSTEM='1', GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0')
        bind_git_executable(prep_env)
        prefix = sandbox_prefix(folder)
        r = _m_rehearse_external.owned_run([*prefix, 'git', '-c', 'core.hooksPath=/dev/null', 'clone', '--no-local', '--no-hardlinks',
                            '--no-checkout', '--quiet', str(n['home_path']), str(candidate)],
                           env=prep_env, capture_output=True)
        if r.returncode:
            raise RehearsalError('candidate snapshot failed', code='candidate-snapshot-failed')
        r = _m_rehearse_external.owned_run([*prefix, 'git', '-C', str(candidate), '-c', 'core.hooksPath=/dev/null',
                            'checkout', '--detach', '--quiet', head], env=prep_env, capture_output=True)
        if r.returncode or not (candidate / 'bin/brain').is_file():
            raise RehearsalError('candidate is not a core checkout', code='candidate-is-not-a-core-checkout')
        validate_frozen_links(candidate)
        _m_rehearse_external.owned_run([*prefix, 'git', '-C', str(candidate), 'remote', 'remove', 'origin'],
                       env=prep_env, capture_output=True, check=True)
        frozen_inst = _m_instance.Instance(frozen)
        from . import rehearse_external
        captured_inst = rehearse_external.freeze_declared(frozen_inst, folder / 'external')
        if captured_inst is not frozen_inst:
            # This root is the owned source copy, never the live caller's root.
            _m_common.atomic_write(frozen / '.brain/local.json', json.dumps(captured_inst.local), private=True)
            frozen_inst = captured_inst
        freeze_machine_inputs(frozen_inst, frozen_now)
        nodes = frozen_inst.nodes()
        diagnostic_terms = []
        diagnostic_ids = [n['id'], branch]
        for node in nodes:
            diagnostic_terms.extend(_m_guard.never_terms(node))
            diagnostic_ids.extend(value for value in
                                  [node['id'], node['fm'].get('id'), *node.get('owners', [])] if value)
        commands, job_ids = rehearsal_commands(nodes)
        diagnostic_ids.extend(job_ids)
        # Seed the same real delivery result into both command copies. Otherwise
        # a new check's cache miss would POST inside the network-denying sandbox.
        delivery_source = folder / 'delivery-source'
        delivery_source.mkdir()
        delivery_inst = isolated_probe_instance(frozen_inst, delivery_source)
        delivery = probe_delivery(delivery_inst, delivery_inst.nodes(), frozen_inst)
        expected = expected_change_notices(frozen_inst, nodes, baseline, candidate)
        original = snapshot(frozen)
        captured_external = snapshot(folder / 'external')
        # Equal-length paths prevent measured sizes from changing warning
        # thresholds, top-section selection, and section order before normalization.
        leftovers = []
        def on_leftover(row):
            leftovers.append(row)
            report_leftover(inst, n, row)
        observations = [external_inputs(frozen_inst)]
        with rehearsal_phase('baseline'):
            old = run_lane(frozen, baseline, folder / 'before', nodes, commands, on_leftover=on_leftover)
        observations.append(external_inputs(frozen_inst))
        with rehearsal_phase('candidate'):
            new = run_lane(frozen, candidate, folder / 'after_', nodes, commands, on_leftover=on_leftover)
        observations.append(external_inputs(frozen_inst))
        flags = compare(old, new, expected)
        drift_comparisons = []
        if len(set(observations)) != 1:
            # No changed external operand can grant a pass or prove causality.
            # Keep independently observed safety/incomplete failures as failures.
            safety = (': unexpected writes', ': baseline incomplete', ': timeout', ': latency exceeds 1 s')
            drift_comparisons = [flag for flag in flags if not flag.endswith(safety)]
            flags = [flag for flag in flags if flag.endswith(safety)]
            flags.append('DRIFT: external inputs changed; comparison inconclusive')
        if leftovers:
            flags.append('rehearsal process cleanup incomplete')
        if snapshot(frozen) != original:
            flags.append('frozen brain changed during rehearsal; evidence refused')
        if snapshot(folder / 'external') != captured_external:
            flags.append('captured external inputs changed during rehearsal; evidence refused')
        warnings, diagnostics = [], []
        for key, result in old.items():
            if key in ('check', 'fix') and not baseline_conditions(key, result) and not drift_comparisons:
                added = added_check_errors(result, new[key])
                if added:
                    diagnostics.append(dict(command=key, reason='new check errors',
                        added_errors=[diagnostic_line(new[key].get('error_diagnostics', {}).get(line, line),
                                                     diagnostic_terms, diagnostic_ids) for line in added]))
            if identical_baseline_failure(result, new[key]):
                warnings.append(key + ': identical baseline failure')
            conditions = baseline_conditions(key, result)
            if conditions:
                diagnostics.append(dict(command=key, reason='baseline incomplete', conditions=conditions,
                                        exit_codes=dict(baseline=result['code'], candidate=new[key]['code'])))
        if (_m_verify.ready_refs(n, branch) != (head, main)
                or _m_verify.integration_parent(n, main) != integration_base):
            flags.append('candidate or integration main moved during rehearsal')
        flags.extend(delivery_regressions(delivery))
        normalized_differences = []
        for key in old:
            before_tokens, after_tokens = (row.get('normalization_tokens', {}) for row in (old[key], new[key]))
            patterns = sorted(name for name in before_tokens.keys() | after_tokens.keys()
                              if before_tokens.get(name) != after_tokens.get(name)
                              and (name != 'json-encoding' or old[key]['text'] == new[key]['text']))
            if patterns:
                normalized_differences.append(dict(command=key, patterns=patterns))
        if local_diff_dir is not None:
            raw = [json.loads((folder / name / 'probe-outputs.json').read_text())
                   for name in ('before', 'after_')]
            write_local_diff(inst, n, local_diff_dir, *raw,
                             metadata=dict(branch=branch, head=head, frozen_now=frozen_now))
        result = {'version': 1, 'node': n['id'], 'branch': branch, 'head': head, 'main': main,
                'installed_core_commit': baseline_commit,
                'frozen_now': frozen_now,
                'integration_base': integration_base,
                'checked_at': _m_jobs.stamp(frozen_inst), 'passed': not flags, 'regressions': flags,
                'warnings': warnings, 'diagnostics': diagnostics,
                'write_diagnostics': [dict(lane=label, command=key, unexpected=row['unexpected'],
                    path_digests=row.get('unexpected_path_digests', []),
                    denied_attempts=row.get('denied_attempts', 0))
                    for label, rows in (('baseline', old), ('candidate', new))
                    for key, row in rows.items() if row['unexpected']],
                'drift_comparisons': drift_comparisons,
                'leftovers': leftovers,
                'signal_audit': [entry for lane_results in (old, new) for command_result in lane_results.values()
                                 for entry in command_result.get('signal_audit', [])],
                'normalized_differences': normalized_differences,
                'commands': len(commands), 'changed_outputs': sum(old[k]['text'] != new[k]['text'] for k in old),
                'hook_seconds': new['hook']['seconds'],
                'delivery': delivery,
                'output_digests': {k: hashlib.sha256(new[k]['text'].encode()).hexdigest() for k in new}}
        copies.record(comparison=dict(passed=result['passed'], commands=result['commands'],
                                      regressions=list(flags), output_digests=result['output_digests']))
    result['bulk_cleanup'] = dict(run=copies.run, state=copies.receipt['state'],
                                  processes_ended=copies.receipt['processes_ended'])
    if copies.receipt['state'] != 'removed':
        result['warnings'].append('owned bulk copies retained; cleanup ownership or process proof incomplete')
        result['regressions'].append('owned bulk cleanup incomplete')
        result['passed'] = False
    return result


def cmd_rehearse(a):
    # Do not register or rewrite machine bindings while observing the live brain.
    phase = 'instance'
    try:
        inst = _m_instance.Instance(_m_nodes.find_root())
        n = inst.node(a.id)
        phase = 'evidence'
        if not n['external'] or not n.get('home_path'):
            raise RehearsalError('external candidate home required', code='external-candidate-home-required')
        path = evidence_path(n, a.branch)
        # Refuse linked evidence directories, including links into a live home.
        for parent in [path, *path.parents]:
            if parent.is_symlink():
                raise RehearsalError('linked evidence path refused', code='linked-evidence-path-refused')
        for protected in (inst.root.resolve(), n['home_path'].resolve()):
            if path.resolve() == protected or protected in path.resolve().parents:
                raise RehearsalError('evidence must stay outside live brain and home', code='evidence-must-stay-outside-live-brain-and-home')
        path.unlink(missing_ok=True)  # a failed repeat must not resurrect an earlier pass
        phase = 'rehearsal'
        result = rehearse(inst, n, a.branch, local_diff_dir=path.parent / 'local-only')
        if _m_guard.find_terms(json.dumps(result), [t for node in inst.nodes() for t in _m_guard.never_terms(node)]):
            raise RehearsalError('protected evidence refused', code='protected-evidence-refused')
        phase = 'publication'
        path.parent.mkdir(parents=True, exist_ok=True)
        _m_common.atomic_write(path, json.dumps(result, indent=2) + '\n', private=True)
    except (RehearsalError, OSError, subprocess.SubprocessError, ValueError) as error:
        # Exception messages and command output can contain protected source content.
        _m_common.die('rehearsal refused or incomplete; phase=' + getattr(error, 'rehearsal_phase', phase)
                      + '; reason=' + refusal_reason(error) + '; no passing evidence recorded')
    print('rehearse: ' + ('passed' if result['passed'] else 'failed') +
          '; commands=' + str(result['commands']) + '; regressions=' + str(len(result['regressions'])))
    for flag in result['regressions']:
        print('- ' + flag)
    for flag in result.get('drift_comparisons', []):
        print('inconclusive: ' + flag)
    for diagnostic in result.get('diagnostics', []):
        print('diagnostic: ' + json.dumps(diagnostic, ensure_ascii=False))
    for warning in result.get('warnings', []):
        print('warning: ' + warning)
    if not result['passed']:
        sys.exit(1)
