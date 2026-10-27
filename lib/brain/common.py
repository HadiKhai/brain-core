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
        prior = self.stat()
        if expected is not Ellipsis and self.generation(prior) != expected:
            raise OSError('publication target changed')
        name = '.' + self.path.name + '.' + os.urandom(12).hex()
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        # Retain the staging descriptor through replacement/cleanup, so even
        # inode reuse cannot make an unknown replacement look like our temp.
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as stream:
            owned = os.fstat(stream.fileno())
            identity = (owned.st_dev, owned.st_ino)
            published = False
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
                os.fchmod(stream.fileno(), 0o600 if self.private else mode)
                os.fsync(stream.fileno())
                if self.generation(self.stat()) != self.generation(prior):
                    raise OSError('publication target changed')
                staging = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
                if (staging.st_dev, staging.st_ino) != identity or staging.st_nlink != 1:
                    raise OSError('publication temporary file changed')
                os.replace(name, self.path.name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
                published = True
                os.fsync(self.fd)
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
                    if cleanup is not None:
                        # A failed cleanup must not replace the write/replace
                        # failure that tells callers whether publication ran.
                        raise primary from cleanup
                raise



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
    key = (str(Path(cwd or Path.cwd()).absolute()), 'branch-ref-snapshot')
    if reads is not None and key in reads:
        return reads[key]
    result = git('for-each-ref', '--format=%(refname)%00%(objectname)%00%(objecttype)',
                 'refs/heads/', 'refs/remotes/', cwd=cwd)
    refs = ({parts[0]: parts[1] for line in result.stdout.splitlines()
             for parts in [line.split('\0')] if len(parts) == 3 and parts[2] == 'commit'}
            if not result.returncode else None)
    if reads is not None:
        reads[key] = refs
        trees = git('worktree', 'list', '--porcelain', '-z', cwd=cwd)
        if not trees.returncode:
            path = branch = None
            for field in trees.stdout.split('\0'):
                if field.startswith('worktree '):
                    path, branch = field[len('worktree '):], None
                elif field.startswith('branch refs/heads/'):
                    branch = field[len('branch refs/heads/'):]
                elif not field and path is not None:
                    reads[(str(Path(path).absolute()), 'worktree-branch')] = branch
                    path = None
    return refs


@contextlib.contextmanager
def git_read_index(cwd, env, *, diff=False):
    """Isolate index refreshes while preserving Git's normal read output.

    Disabling diff.autoRefreshIndex changes name-only/raw output for stat-only
    changes. Git versions that ignore optional locks during diff must therefore
    refresh a disposable copy instead of the caller's index. Split indexes are
    flattened privately so reads cannot freshen their original backing files.
    """
    location = git('rev-parse', '--path-format=absolute', '--git-path', 'index', cwd=cwd, env=env)
    source = Path(location.stdout.removesuffix('\n')) if not location.returncode else None
    if source is None or not source.is_file():
        yield env  # no repository/index to refresh; preserve Git's own verdict
        return
    with source.open('rb') as stream:
        original = os.fstat(stream.fileno())
        contents = stream.read()
    split = b'link' in contents  # conservative: a filename may also contain it
    if not diff and not split:
        yield env
        return
    with tempfile.TemporaryDirectory(prefix='brain-diff-index-') as temporary:
        directory = Path(temporary)
        private = directory / 'index'
        private.write_bytes(contents)
        # The source descriptor binds bytes and mtime even across an atomic
        # index replacement. Git needs that mtime for racy-clean detection.
        os.utime(private, ns=(original.st_atime_ns, original.st_mtime_ns))
        child = {**env, 'GIT_INDEX_FILE': str(private)}
        if split:
            own = git('rev-parse', '--absolute-git-dir', cwd=cwd, env=env, check=True)
            own = Path(own.stdout.removesuffix('\n'))
            shared = [path for path in own.glob('sharedindex.*') if path.is_file()]
            if shared:
                # Flatten the copied split index before the actual read. Moving
                # the real query's GIT_DIR changes conditional configuration.
                # Conversion needs no objects/refs and uses only scratch config.
                form = git('rev-parse', '--show-object-format', cwd=cwd, env=env, check=True)
                (directory / 'objects').mkdir()
                (directory / 'refs').mkdir()
                (directory / 'HEAD').write_text('ref: refs/heads/scratch\n')
                (directory / 'config').write_text(
                    '[core]\nrepositoryformatversion = 1\nbare = false\n'
                    '[extensions]\nobjectFormat = ' + form.stdout.removesuffix('\n') + '\n')
                for backing in shared:
                    shutil.copy2(backing, directory / backing.name)
                conversion = {key: value for key, value in env.items() if not key.startswith('GIT_')}
                conversion.update(GIT_DIR=str(directory), GIT_INDEX_FILE=str(private),
                                  GIT_WORK_TREE=str(Path(cwd or Path.cwd()).absolute()),
                                  GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_SYSTEM=os.devnull,
                                  GIT_CONFIG_GLOBAL=os.devnull, GIT_OPTIONAL_LOCKS='0', GIT_NO_LAZY_FETCH='1')
                git('update-index', '--no-split-index', cwd=cwd, env=conversion, check=True)
                os.utime(private, ns=(original.st_atime_ns, original.st_mtime_ns))
        yield child


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
    if result is None:
        # Status/diff may refresh the index even though their callers only read.
        # Scope the override to this child, including explicitly supplied envs;
        # add/commit and other writers keep their original locking behaviour.
        lock_query = args
        while lock_query:
            if len(lock_query) >= 2 and lock_query[0] == '-c':
                lock_query = lock_query[2:]
            elif lock_query[0] == '--no-optional-locks':
                lock_query = lock_query[1:]
            else:
                break
        if lock_query and lock_query[0] in ('status', 'diff'):
            env = {**(os.environ if env is None else env), 'GIT_OPTIONAL_LOCKS': '0'}
        index = (git_read_index(cwd, env, diff=lock_query[0] == 'diff')
                 if lock_query and lock_query[0] in ('status', 'diff') else contextlib.nullcontext(env))
        with index as child_env:
            result = subprocess.run(["git", *(["-C", str(cwd)] if cwd else []), *args],
                                    capture_output=True, text=True, errors="replace", check=False, env=child_env)
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
