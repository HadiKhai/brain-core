#!/usr/bin/env python3
"""brain: mechanics for a Brain instance (see core/MODEL.md).

Generic: everything specific is read from the instance (instance.md, node.md files) and from
this machine's bindings (.brain/local.json, never committed).
Judgment work (what goes in a brief, how to split state) lives in core/skills, not here.
"""
import argparse
import contextlib
import contextvars
import datetime
import fnmatch
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
import unicodedata
from pathlib import Path

from . import command_reads as _m_command_reads


CORE = Path(__file__).resolve().parents[2]
ENTRY_POINT = CORE / "bin" / "brain"
NODE_FIELDS = ["id", "title", "owners", "labels", "home", "runtime", "created"]
LOCAL_KEYS = ["coordinator", "host_project", "home_project", "host_instance", "preview", "preview_tour", "review_urls"]  # per machine (axiom 23)
ITEM_FIELDS = ["id", "title", "kind", "source_node", "source_ref", "learned_at",
               "source_owners", "scope", "status"]
STALE_DAYS = 7
TOUCHED = set()  # every path this command wrote; commits include these and nothing else (axiom 15)


def touch(*paths):
    for x in paths:
        TOUCHED.add(Path(x).absolute())


# ---------- files ----------

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.S)


def read_fm(path):
    text = Path(path).read_text()
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fm = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip()
    return fm, m.group(2)


def write_fm(path, fm, body):
    lines = "\n".join(f"{k}: {v}" for k, v in fm.items())
    atomic_write(path, f"---\n{lines}\n---\n{body}")
    touch(path)


def safe_target(path):
    """A file brain writes inside an instance stays inside it: neither a folder on the way nor the file itself may
    be a link leading out (a node's job-handbacks/ linked into a home would put job records there)."""
    p = Path(os.path.abspath(path))
    root = next((d for d in p.parents if (d / "instance.md").is_file() and (d / "node.md").is_file()), None)
    if root is None:
        return
    top = root.resolve()
    parent = p.parent.resolve()
    for q in [p.parent] + ([p] if p.is_symlink() else []):
        real = parent if q == p.parent else q.resolve()
        if real != top and top not in real.parents:
            die(f"{p} leads out of the brain through a link; brain writes only inside it (remove the link)")
    try:
        shared = p.is_file() and p.stat().st_nlink > 1  # the file itself, or the one a link leads to
    except OSError:
        shared = False
    if shared:  # a hard link: writing it would change the same file wherever else it is linked
        die(f"{p} is hard-linked to another file; brain won't write a file shared with another place (copy it)")
    return parent / p.name  # publish through the parent checked inside containment


def hard_linked(entry):
    """A regular file with another name somewhere (a hard link): writing it in place changes it there too."""
    try:
        return entry.is_file(follow_symlinks=False) and entry.stat(follow_symlinks=False).st_nlink > 1
    except OSError:
        return False


def shared_file(path):
    """A regular file with more than one name (a hard link), reached by its real path."""
    try:
        return path.is_file() and path.stat().st_nlink > 1
    except OSError:
        return False


@_m_command_reads.memo
def outward_links(root):
    """Links in what brain commands write that lead out of the brain: its own files, .brain/, and in the root and
    every node folder their files, library, specialists, job records, sources and reports, and the nodes below.
    A brain holds none: one would put brain files in a home or anywhere else. A link that stays inside the brain
    (core/'s skills and settings, CLAUDE.md to AGENTS.md) is fine, and a folder it leads to is looked at as the
    folder it stands for."""
    top = Path(root).resolve()
    bad, seen = [], set()

    def real_of(p):
        try:
            real = p.resolve()
        except (OSError, RuntimeError):
            return None
        return real if real == top or top in real.parents else None

    def look(folder, deep=False, kind="node"):
        try:
            key = Path(folder).resolve()
        except (OSError, RuntimeError):
            return
        if (key, deep, kind) in seen:
            return  # a folder reached twice, through links that loop
        seen.add((key, deep, kind))
        try:
            entries = list(os.scandir(folder))
        except OSError:
            return
        for e in entries:
            p = Path(e.path)
            if e.is_symlink():
                real = real_of(p)
                if real is None or shared_file(real):
                    bad.append(p)  # leads out, or to a file hard-linked elsewhere
                    continue
                if not real.is_dir():
                    continue
            elif not e.is_dir(follow_symlinks=False):
                if hard_linked(e):
                    bad.append(p)  # the same file is also somewhere else: writing it changes that too
                continue
            if kind == "entries":
                continue  # sources/ and reports/: only what lies directly in them is written
            if kind == "nodes":
                look(p)  # each node folder below nodes/
            elif deep or e.name in (".brain", "library", "specialists", "job-handbacks", "job-contracts", "job-trains"):
                look(p, deep=True)
            elif e.name in ("sources", "reports"):
                look(p, deep=True)  # merges move files in, reports write index files below
            elif e.name == "nodes" and kind == "node":
                look(p, kind="nodes")
    look(top)
    git_dir = top / ".git"
    if git_dir.is_file():  # a linked worktree or submodule: .git names the real folder
        m = re.match(r"gitdir: (.+)", git_dir.read_text().strip())
        git_dir = (top / m[1]).resolve() if m else git_dir
    try:
        entries = list(os.scandir(git_dir)) if git_dir.is_dir() else []
    except OSError:
        entries = []
    for e in entries:  # what a commit writes: its message, index, HEAD, refs and logs at the top
        if hard_linked(e):
            bad.append(Path(e.path))
        elif e.is_symlink():
            p = Path(e.path)
            try:
                real = p.resolve()
            except (OSError, RuntimeError):
                real = None
            if real is None or shared_file(real) or not (real == git_dir.resolve() or git_dir.resolve() in real.parents
                                                         or real == top or top in real.parents):
                bad.append(p)
    return bad


def sync_directory(path):
    directory = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def durable_makedirs(path, mode=0o700):
    """Create directories and persist ancestor entries before publishing children.

    Re-sync existing entries too: a prior interrupted call may have created them
    without completing its parent sync. An internal alias selects its checked
    physical directory once; subsequent operations never follow that alias.
    """
    with durable_directory_target(Path(path) / '.persistence-target', mode):
        pass


class DirectoryTarget:
    """A checked physical parent held until its last read, publication or cleanup.

    Paths are labels, never the authority for a later filesystem operation. Each
    physical component is opened without following links, relative to its held
    predecessor. This is not a namespace lock against arbitrary external renames.
    """
    def __init__(self, path, *, create=False, private=False, _checked_physical=False):
        path = Path(path)
        self.path = path if _checked_physical else safe_target(path) or path.parent.resolve() / path.name
        self.create, self.private = create, private
        self.chain = []
        self.fd = None

    def __enter__(self):
        flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | os.O_NOFOLLOW
        try:
            self.fd = os.open(self.path.anchor, flags)
            self.chain.append((None, self.path.anchor, self.fd))
            for name in self.path.parent.parts[1:]:
                parent = self.fd
                try:
                    child = os.open(name, flags, dir_fd=parent)
                except FileNotFoundError:
                    if not self.create:
                        raise
                    self.check()
                    try:
                        os.mkdir(name, 0o700 if self.private else 0o777, dir_fd=parent)
                        os.fsync(parent)
                    except FileExistsError:
                        pass
                    child = os.open(name, flags, dir_fd=parent)
                self.fd = child
                self.chain.append((parent, name, child))
            self.check()
            self.stat()  # refuse linked/shared targets before any mutation
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *exc):
        for _, _, fd in reversed(self.chain):
            os.close(fd)
        self.chain = []
        self.fd = None

    def check(self):
        from . import history
        for parent, name, child in self.chain[1:]:
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            held = os.fstat(child)
            if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
                raise OSError('publication directory moved')
        # Also detect a moved ancestor, including the instance itself.
        if self.chain:
            expected = Path(self.path.anchor).joinpath(*(name for _, name, _ in self.chain[1:]))
            if history.history_fd_path(self.fd) != str(expected):
                raise OSError('publication directory moved')

    def durable_child(self, name, mode=0o700):
        """Hold a child and persist its entry before any deeper mutation."""
        if name in ('', '.', '..') or Path(name).name != name:
            raise ValueError('directory persistence requires one relative name')
        self.check()
        parent = self.fd
        try:
            os.mkdir(name, mode, dir_fd=parent)
        except FileExistsError:
            pass
        child = os.open(name, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | os.O_NOFOLLOW,
                        dir_fd=parent)
        self.fd = child
        self.chain.append((parent, name, child))
        self.check()
        os.fsync(parent)
        self.check()

    def stat(self, name=None):
        self.check()
        if name is not None and (name in ('', '.', '..') or Path(name).name != name):
            raise ValueError('publication requires one directory-relative filename')
        try:
            st = os.stat(name or self.path.name, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(st.st_mode):
            if self.private:
                die('event storage must not be linked')
        elif not stat.S_ISREG(st.st_mode):
            raise OSError('publication target is not a regular file')
        elif st.st_nlink != 1:
            die('publication target is hard-linked to another file')
        return st

    def open(self, flags=os.O_RDONLY, mode=0o600):
        before = self.stat()
        # Never truncate before validating the opened inode.
        fd = os.open(self.path.name, (flags & ~os.O_TRUNC) | os.O_NOFOLLOW, mode, dir_fd=self.fd)
        try:
            self.check_file(fd)
            after = os.fstat(fd)
            if before and (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise OSError('publication file changed')
            if self.private and flags & (os.O_WRONLY | os.O_RDWR):
                os.fchmod(fd, 0o600)
            if flags & os.O_TRUNC:
                os.ftruncate(fd, 0)
            return fd
        except BaseException:
            try:
                if flags & os.O_CREAT and flags & os.O_EXCL:
                    st = os.fstat(fd)
                    self.unlink_owned(self.path.name, (st.st_dev, st.st_ino))
            finally:
                os.close(fd)
            raise

    def check_file(self, fd):
        self.check()
        held, current = os.fstat(fd), self.stat()
        if (not stat.S_ISREG(held.st_mode) or held.st_nlink != 1 or current is None
                or (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino)):
            raise OSError('publication file moved or is not a private regular file')

    def read_bytes(self):
        with os.fdopen(self.open(), 'rb') as stream:
            before = self.generation(os.fstat(stream.fileno()))
            data = stream.read()
            self.check_file(stream.fileno())
            if self.generation(os.fstat(stream.fileno())) != before:
                raise OSError('publication file changed while reading')
            return data

    @staticmethod
    def generation(st):
        return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_mode, st.st_nlink) if st else None

    def unlink_owned(self, name, identity):
        # Cleanup is only of our captured inode in our original directory. A
        # replaced name belongs to somebody else, even when it looks like a temp.
        if name in ('', '.', '..') or Path(name).name != name:
            raise ValueError('cleanup requires one directory-relative filename')
        try:
            st = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if (st.st_dev, st.st_ino) != identity:
            return False
        os.unlink(name, dir_fd=self.fd)
        os.fsync(self.fd)
        return True

    def atomic_write(self, text, *, expected=Ellipsis):
        recovered = self.recover_mode(expected=expected)
        prior = self.stat()
        if self.generation(prior) != recovered:
            raise OSError('publication target changed')
        name = '.' + self.path.name + '.' + os.urandom(12).hex()
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        # Retain the staging descriptor through replacement/cleanup, so even
        # inode reuse cannot make an unknown replacement look like our temp.
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as stream:
            owned = os.fstat(stream.fileno())
            identity = (owned.st_dev, owned.st_ino)
            published = False
            record = None
            try:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
                current = self.stat()
                if self.generation(current) != self.generation(prior):
                    raise OSError('publication target changed')
                staging = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
                if (staging.st_dev, staging.st_ino) != identity or staging.st_nlink != 1:
                    raise OSError('publication temporary file changed')
                # Content is written and synced privately. Prepare and persist
                # the intended mode before rename so a killed writer cannot
                # leave the published destination with the staging mode.
                mode = (stat.S_IMODE(prior.st_mode)
                        if prior and stat.S_ISREG(prior.st_mode) else 0o644)
                mode = 0o600 if self.private else mode
                record = self.mode_record(stream.fileno(), text, mode)
                os.fchmod(stream.fileno(), mode)
                os.fsync(stream.fileno())
                if self.generation(self.stat()) != self.generation(prior):
                    raise OSError('publication target changed')
                staging = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
                if (staging.st_dev, staging.st_ino) != identity or staging.st_nlink != 1:
                    raise OSError('publication temporary file changed')
                os.replace(name, self.path.name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
                published = True
                os.fsync(self.fd)
                self.unlink_owned(record[0], record[1])
            except BaseException as primary:
                if not published:
                    cleanup = None
                    try:
                        os.fchmod(stream.fileno(), 0o600)
                    except OSError as error:
                        cleanup = error
                    try:
                        self.unlink_owned(name, identity)
                    except OSError as error:
                        cleanup = error
                    if record is not None:
                        try:
                            self.unlink_owned(record[0], record[1])
                        except OSError as error:
                            cleanup = error
                    if cleanup is not None:
                        # A failed cleanup must not replace the write/replace
                        # failure that tells callers whether publication ran.
                        raise primary from cleanup
                raise
            finally:
                if record is not None:
                    os.close(record[2])

    def mode_prefix(self, st):
        key = hashlib.sha256(os.fsencode(self.path.name)).hexdigest()
        return '.intended-mode-' + key + '-' + format(st.st_dev, 'x') + '-' + format(st.st_ino, 'x') + '-'

    def mode_record(self, fd, text, mode):
        """Persist intent before chmod/rename, bound to this exact content inode."""
        self.check()
        st = os.fstat(fd)
        name = self.mode_prefix(st) + os.urandom(12).hex()
        record_fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        held = os.fstat(record_fd)
        identity = (held.st_dev, held.st_ino)
        try:
            fcntl.flock(record_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.fchmod(record_fd, 0o600)
            if stat.S_IMODE(os.fstat(record_fd).st_mode) != 0o600:
                raise OSError('intended-mode record is not private')
            data = json.dumps(dict(version=1, destination=self.path.name, device=st.st_dev,
                                   inode=st.st_ino, size=st.st_size, mtime_ns=st.st_mtime_ns,
                                   sha256=hashlib.sha256(text.encode('utf-8')).hexdigest(), mode=mode)).encode()
            with os.fdopen(os.dup(record_fd), 'wb') as record:
                record.write(data)
                record.flush()
                os.fsync(record.fileno())
            os.fsync(self.fd)
            return name, identity, record_fd
        except BaseException as primary:
            try:
                self.unlink_owned(name, identity)
            except OSError as cleanup:
                raise primary from cleanup
            finally:
                os.close(record_fd)
            raise

    def recover_mode(self, *, expected=Ellipsis):
        """Repair only a published inode; abandoned staging never gets promoted.

        Call under the caller's writer lock. Ordinary reads do not recover or
        mutate permissions. Records for other inodes remain for reconciliation.
        """
        current = self.stat()
        original = self.generation(current)
        if expected is not Ellipsis and original != expected:
            raise OSError('publication target changed')
        if current is None or not stat.S_ISREG(current.st_mode):
            return original
        prefix = self.mode_prefix(current)
        for name in os.listdir(self.fd):
            if not re.fullmatch(re.escape(prefix) + r'[0-9a-f]{24}', name):
                continue
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue  # the publishing writer still owns its intent
                held = os.fstat(fd)
                if (not stat.S_ISREG(held.st_mode) or held.st_nlink != 1
                        or held.st_uid != os.geteuid() or stat.S_IMODE(held.st_mode) != 0o600
                        or held.st_size > 2048):
                    raise OSError('unsafe intended-mode record')
                try:
                    record = json.loads(os.read(fd, 2049))
                except (ValueError, UnicodeError) as error:
                    raise OSError('invalid intended-mode record') from error
                if (not isinstance(record, dict) or record.get('version') != 1
                        or record.get('destination') != self.path.name
                        or record.get('device') != current.st_dev or record.get('inode') != current.st_ino
                        or record.get('size') != current.st_size or record.get('mtime_ns') != current.st_mtime_ns
                        or type(record.get('mode')) is not int or not 0 <= record['mode'] <= 0o7777):
                    raise OSError('intended-mode record does not match publication')
                with os.fdopen(self.open(), 'rb') as stream:
                    opened = os.fstat(stream.fileno())
                    before = self.generation(opened)
                    digest = hashlib.sha256()
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                        digest.update(chunk)
                    self.check_file(stream.fileno())
                    if (self.generation(os.fstat(stream.fileno())) != before
                            or before != self.generation(current) or opened.st_uid != os.geteuid()
                            or digest.hexdigest() != record.get('sha256')):
                        raise OSError('published content changed before mode recovery')
                    named = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
                    if (self.generation(os.fstat(fd)) != self.generation(held)
                            or self.generation(named) != self.generation(held)):
                        raise OSError('intended-mode record changed during recovery')
                    mode = 0o600 if self.private else record['mode']
                    if stat.S_IMODE(opened.st_mode) != mode:
                        os.fchmod(stream.fileno(), mode)
                    prepared = os.fstat(stream.fileno())
                    # Only our chmod may change mode/ctime. Do not adopt an
                    # independent write as the caller's expected generation.
                    if (self.generation(prepared)[:4] != before[:4]
                            or prepared.st_nlink != opened.st_nlink
                            or prepared.st_uid != opened.st_uid
                            or stat.S_IMODE(prepared.st_mode) != mode):
                        raise OSError('published content changed during mode recovery')
                    os.fsync(stream.fileno())
                    os.fsync(self.fd)
                    stream.seek(0)
                    recovered_digest = hashlib.sha256()
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                        recovered_digest.update(chunk)
                    self.check_file(stream.fileno())
                    if (self.generation(os.fstat(stream.fileno())) != self.generation(prepared)
                            or recovered_digest.hexdigest() != record['sha256']):
                        raise OSError('published content changed during mode recovery')
                    current = prepared
                self.unlink_owned(name, (held.st_dev, held.st_ino))
            finally:
                os.close(fd)
        return self.generation(current)



def directory_target(path, *, create=False, private=False):
    return DirectoryTarget(path, create=create, private=private)


@contextlib.contextmanager
def durable_directory_target(path, mode=0o700, *, private=False):
    """Keep one checked physical authority through persistence and publication."""
    path = Path(os.path.abspath(path))
    path = safe_target(path) or path.parent.resolve() / path.name
    parent = path.parent
    root = next((p for p in [parent, *parent.parents] if (p / 'instance.md').is_file()
                 and (p / 'node.md').is_file()), path.anchor)
    root = Path(root)
    # The resolved name is already checked. Never resolve its physical components
    # again: O_NOFOLLOW descent must refuse replacement with another internal alias.
    with DirectoryTarget(root / '.persistence-target', private=private, _checked_physical=True) as target:
        for part in parent.relative_to(root).parts:
            target.durable_child(part, mode)
        target.path = path
        target.stat()
        yield target


def atomic_write(path, text, *, private=False, create=False):
    """Sync file data, replace atomically, then sync its parent directory.

    Pre-publication failures preserve the old file. A parent-sync error is reported
    after the complete new file is visible; callers must not assume rollback.
    This is one-file persistence ordering, not a transaction across records.
    """
    with directory_target(path, create=create, private=private) as target:
        target.atomic_write(text)


def lst(value):
    return [x.strip() for x in (value or "").split(",") if x.strip() and x.strip() != "none"]


def fill(text, values):
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", v)
    return text


_GIT_READS = contextvars.ContextVar('brain_git_reads', default=None)


@contextlib.contextmanager
def git_read_snapshot():
    """Share repeated display reads within one build, never across threads or builds."""
    if _GIT_READS.get() is not None:
        yield
        return
    token = _GIT_READS.set({})
    try:
        yield
    finally:
        _GIT_READS.reset(token)


def branch_refs(cwd):
    """One branch enumeration per repository in a read snapshot."""
    reads = _GIT_READS.get()
    where = str(Path(cwd or Path.cwd()).absolute())
    repository = reads.get((where, 'repository'), where) if reads is not None else where
    key = (repository, 'branch-ref-snapshot')
    if reads is not None and key in reads:
        return reads[key]
    result = git('for-each-ref', '--format=%(refname)%00%(objectname)%00%(objecttype)%00%(committerdate:unix)',
                 'refs/heads/', 'refs/remotes/', cwd=cwd)
    fields = [line.split('\0') for line in result.stdout.splitlines()]
    refs = ({parts[0]: parts[1] for parts in fields if parts[2] == 'commit'}
            if not result.returncode and all(len(parts) == 4 for parts in fields) else None)
    if reads is not None:
        reads[key] = refs
        if refs is not None:
            for name, tip, kind, stamp in fields:
                if kind == 'commit' and stamp.isdigit():
                    reads[(repository, 'commit-time', name)] = stamp
        trees = git('worktree', 'list', '--porcelain', '-z', cwd=cwd)
        if not trees.returncode:
            path = branch = head = None
            for field in trees.stdout.split('\0'):
                if field.startswith('worktree '):
                    path, branch, head = field[len('worktree '):], None, None
                elif field.startswith('HEAD '):
                    head = field[len('HEAD '):]
                elif field.startswith('branch refs/heads/'):
                    branch = field[len('branch refs/heads/'):]
                elif not field and path is not None:
                    tree = str(Path(path).absolute())
                    reads[(tree, 'repository')] = repository
                    reads[(tree, 'worktree-branch')] = branch
                    if head and re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', head) and set(head) != {'0'}:
                        reads[(tree, 'worktree-head')] = head
                    path = None
    return refs


def commit_range_summary(cwd, base, head):
    """Count reachable commits and flag unfinished titles from one command-local graph."""
    reads = _GIT_READS.get()
    if reads is not None:
        refs = branch_refs(cwd)
        base, head = (refs or {}).get(base, base), (refs or {}).get(head, head)
        if (refs is not None and head in refs.values()
                and re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', base)):
            where = str(Path(cwd or Path.cwd()).absolute())
            repository = reads.get((where, 'repository'), where)
            key = (repository, 'title-graph', base)
            if key not in reads:
                try:
                    # Pin the roots we enumerated: refs may move while the read
                    # runs, and an old tip must not become an invented empty range.
                    batch = git('log', '--format=%H%x00%P%x00%s', *sorted(set(refs.values())),
                                '--not', base, '--', cwd=cwd)
                except OSError:
                    batch = None  # e.g. an argument limit; individual reads still work
                graph = {}
                valid = batch is not None and not batch.returncode
                for line in batch.stdout.splitlines() if batch is not None else []:
                    parts = line.split('\0')
                    if len(parts) != 3 or not all(re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', oid)
                            for oid in [parts[0], *parts[1].split()]):
                        valid = False
                        break
                    graph[parts[0]] = (parts[1].split(), bool(re.match(r'(?:wip|fixup!|squash!)', parts[2], re.I)))
                reads[key] = graph if valid else None
            graph = reads[key]
            if graph is not None:
                summary_key = (*key, head)
                if summary_key in reads:
                    return reads[summary_key]
                pending, seen = [head], set()
                unfinished, contains = False, head == base
                while pending:
                    tip = pending.pop()
                    if tip in seen or tip not in graph:
                        continue
                    seen.add(tip)
                    parents, bad_title = graph[tip]
                    unfinished = unfinished or bad_title
                    contains = contains or base in parents
                    pending.extend(parents)
                reads[summary_key] = len(seen), unfinished, contains
                return reads[summary_key]
    return None


def unfinished_commit_titles(cwd, base, head):
    """Batch clean ranges, retaining Git's exact ordering for any findings/errors."""
    summary = commit_range_summary(cwd, base, head)
    if summary is not None and not summary[1]:
        return []
    result = git('log', '--format=%s', base + '..' + head, cwd=cwd)
    return ([title for title in result.stdout.splitlines() if re.match(r'(?:wip|fixup!|squash!)', title, re.I)]
            if not result.returncode else None)


def git(*args, cwd=None, check=False, env=None):
    reads = _GIT_READS.get()
    key = (str(Path(cwd or Path.cwd()).absolute()), args)
    query = args
    while len(query) >= 2 and query[0] == '-c':
        query = query[2:]
    read_only = (query and (query[0] in
                 ('rev-parse', 'log', 'status', 'diff', 'ls-files', 'merge-base', 'rev-list', 'for-each-ref')
                 or query == ('remote',)
                 or (query[0] == 'symbolic-ref' and len([x for x in query[1:] if not x.startswith('-')]) == 1
                     and all(not x.startswith('-') or x in ('--short', '--quiet', '-q') for x in query[1:]))))
    cacheable = (reads is not None and read_only and env is None
                 and not any(str(a).startswith('--output') for a in args))
    result = reads.get(key) if cacheable else None
    tree_key = (key[0], 'worktree-branch')
    if (result is None and cacheable and query in (('symbolic-ref', '--short', 'HEAD'),
                                                  ('symbolic-ref', '--quiet', '--short', 'HEAD'))
            and tree_key in reads):
        branch = reads[tree_key]
        result = subprocess.CompletedProcess(['git', *args], 0 if branch else 1,
                                             (branch + '\n') if branch else '', '')
        reads[key] = result
    # Display snapshots enumerate refs once per repository instead of spawning
    # rev-parse for every ledger row. Writers never enter this optional scope.
    if (result is None and cacheable and len(args) == 4 and args[:3] == ('rev-parse', '--verify', '--quiet')
            and re.fullmatch(r'refs/(?:heads|remotes)/[^~^: ?*\[\]\\]+(?:\^\{commit\})?', args[3])):
        refs = branch_refs(cwd)
        if refs is not None:
            tip = refs.get(args[3].removesuffix('^{commit}'))
            result = subprocess.CompletedProcess(['git', *args], 0 if tip else 1, (tip + '\n') if tip else '', '')
            reads[key] = result
    if result is None and cacheable:
        repository = reads.get((key[0], 'repository'), key[0])
        if args in (('rev-parse', '--verify', 'HEAD'), ('rev-parse', '--verify', '--quiet', 'HEAD')):
            head = reads.get((key[0], 'worktree-head'))
            if head:
                result = subprocess.CompletedProcess(['git', *args], 0, head + '\n', '')
        elif len(args) == 4 and args[:3] == ('log', '-1', '--format=%ct'):
            branch_refs(cwd)
            stamp = reads.get((repository, 'commit-time', args[3]))
            if stamp is not None:
                signature_key = (key[0], 'log-show-signature')
                if signature_key not in reads:
                    config = git('config', '--bool', '--get', 'log.showSignature', cwd=cwd)
                    reads[signature_key] = (config.returncode == 1 or
                                            config.returncode == 0 and config.stdout.strip() == 'false')
                if reads[signature_key]:
                    result = subprocess.CompletedProcess(['git', *args], 0, stamp + '\n', '')
        elif len(args) == 3 and args[:2] == ('rev-list', '--count') and args[2].count('..') == 1:
            base, head = args[2].split('..')
            summary = commit_range_summary(cwd, base, head)
            if summary is not None:
                result = subprocess.CompletedProcess(['git', *args], 0, str(summary[0]) + '\n', '')
        elif len(args) == 4 and args[:2] == ('merge-base', '--is-ancestor'):
            summary = commit_range_summary(cwd, *args[2:])
            if summary is not None:
                result = subprocess.CompletedProcess(['git', *args], 0 if summary[2] else 1, '', '')
        if result is not None:
            reads[key] = result
    if result is None:
        result = subprocess.run(["git", *(["-C", str(cwd)] if cwd else []), *args],
                                capture_output=True, text=True, errors="replace", check=False, env=env)
        if cacheable:
            reads[key] = result
    if check:
        result.check_returncode()
    return result


class VerdictExit(SystemExit):
    """A completed command returning a negative verdict rather than a failed mutation."""


def die(msg):
    sys.exit(f"brain: {msg}")


def tilde(p):
    p, h = str(p), str(Path.home())
    return "~" + p[len(h):] if p == h or p.startswith(h + "/") else p


def tilde_shell(p):
    path = tilde(p)
    return "~/" + shlex.quote(path[2:]) if path.startswith("~/") else "~" if path == "~" else shlex.quote(path)


def read_env(f):
    out = {}
    if f.exists():
        for line in f.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


# ---------- instance ----------

def holds_files(folder):
    try:
        return any(f.is_file() or f.is_symlink() for f in folder.rglob("*"))
    except OSError:
        return True  # can't tell: let git say
