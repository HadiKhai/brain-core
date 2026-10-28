"""Versioned core releases: install beside the staged core, swap one symlink, prune unreferenced.

`core/` stays the Git submodule (the staged source that `git submodule update` rewrites).
Each installed release is a copy in `.brain/cores/<sha>` with checked-hash bytecode, plus a
`<sha>.pin` file. `.brain/core-current` is a relative symlink to the running release;
`.brain/core-previous` names the rollback target. The launcher resolves core-current once
(or BRAIN_CORE_RELEASE, which a gate exports to its command)
and holds a shared flock on the release's pin for its whole life, so a swap never changes
the code a running process (or gate) imports, and prune skips any release still pinned.
"""
import fcntl
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import common as _m_common

CURRENT = "core-current"
PREVIOUS = "core-previous"
SHA = re.compile(r"[0-9a-f]{40}")
# Bytecode belongs in the release's own __pycache__, beside the sources it hashes.
BYTECODE_ENV = ("PYTHONPYCACHEPREFIX", "PYTHONDONTWRITEBYTECODE")


def cores_dir(root):
    return Path(root) / ".brain" / "cores"


def pin_path(release):
    return Path(str(release) + ".pin")


def _link_target(root, name):
    link = Path(root) / ".brain" / name
    if not link.is_symlink():
        return None
    target = os.readlink(link)
    sha = Path(target).name
    if not SHA.fullmatch(sha) or Path(target).parent != Path("cores"):
        raise ValueError(f"{link} names no installed release")
    return sha


def current(root):
    """The release sha core-current names; None for a plain (not yet migrated) core."""
    return _link_target(root, CURRENT)


def previous(root):
    return _link_target(root, PREVIOUS)


def pinned(root):
    return current(root) is not None


def hold_pin(release):
    """A shared pin on `release` that survives exec (a gate's command); None outside a release."""
    try:
        fd = os.open(str(pin_path(release)), os.O_RDONLY)
    except OSError:
        return None
    fcntl.flock(fd, fcntl.LOCK_SH)
    os.set_inheritable(fd, True)
    return fd


def install(root, sha, python=None):
    """Export the staged core's commit `sha` into `.brain/cores/<sha>` with checked-hash bytecode."""
    root = Path(root)
    if not SHA.fullmatch(sha or ""):
        raise ValueError("release needs a full commit sha")
    release = cores_dir(root) / sha
    if release.is_dir() and pin_path(release).is_file():
        return release  # already installed; an installed release is never rewritten
    cores_dir(root).mkdir(parents=True, exist_ok=True)
    partial = cores_dir(root) / f".partial-{sha}-{os.getpid()}"
    shutil.rmtree(partial, ignore_errors=True)
    try:
        # The committed tree only: local edits in the staged core never enter a sha's release.
        partial.mkdir()
        archive = subprocess.run(["git", "-C", str(root / "core"), "archive", "--format=tar", sha],
                                 capture_output=True)
        if archive.returncode:
            raise ValueError(f"no committed core {sha[:7]}: {archive.stderr.decode(errors='replace').strip()}")
        untar = subprocess.run(["tar", "-x", "-C", str(partial)], input=archive.stdout, capture_output=True)
        if untar.returncode:
            raise ValueError(f"couldn't unpack core {sha[:7]}: {untar.stderr.decode(errors='replace').strip()}")
        if release.exists():
            shutil.rmtree(release)  # an earlier attempt that never reached its pin file
        os.rename(partial, release)
        # Compile at the final path: a pycache prefix keys bytecode by source path. The same
        # interpreter as the launcher validates each .pyc by source hash, never mtime.
        python = python or sys.executable
        compiled = subprocess.run([python, "-m", "compileall", "-q", "-f", "--invalidation-mode", "checked-hash",
                                   str(release / "lib" / "brain")], capture_output=True, text=True,
                                  env={k: v for k, v in os.environ.items() if k not in BYTECODE_ENV})
        if compiled.returncode:
            raise ValueError(f"bytecode compile failed: {(compiled.stdout + compiled.stderr).strip()[-300:]}")
        # Load the release's own launcher, which imports brain.cli, without running a command.
        smoke = subprocess.run([python, "-B", "-c", "import runpy, sys; runpy.run_path(sys.argv[1], run_name='smoke')",
                                str(release / "bin" / "brain")], capture_output=True, text=True, cwd=release,
                               env={k: v for k, v in os.environ.items() if k != "BRAIN_CORE_RELEASE"})
        if smoke.returncode:
            raise ValueError(f"release does not import: {smoke.stderr.strip()[-300:]}")
    except BaseException:
        shutil.rmtree(release, ignore_errors=True)  # no pin yet: nothing can run it
        raise
    finally:
        shutil.rmtree(partial, ignore_errors=True)
    with open(pin_path(release), "a"):
        pass
    _fsync_dir(cores_dir(root))
    return release


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _point(root, name, sha):
    """Atomically repoint .brain/<name> at cores/<sha>: rename a fresh symlink over the old one."""
    brain = Path(root) / ".brain"
    tmp = brain / f".{name}.{os.getpid()}"
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    os.symlink(os.path.join("cores", sha), tmp)
    os.replace(tmp, brain / name)
    _fsync_dir(brain)


def swap(root, sha):
    """Make `sha` current; the release it replaces becomes the rollback target. Returns that sha."""
    release = cores_dir(root) / sha
    if not (release.is_dir() and pin_path(release).is_file()):
        raise ValueError(f"release {sha[:7]} is not installed")
    old = current(root)
    if old == sha:
        return previous(root)
    # Activate first: an interruption before the second rename leaves `sha` running and the
    # older rollback target in place, never both links on the release being left.
    _point(root, CURRENT, sha)
    if old:
        _point(root, PREVIOUS, old)
    return old


def rollback(root):
    """Swap back to the previous release; the release left becomes the new rollback target."""
    old, back = current(root), previous(root)
    if not old or not back or back == old:
        raise ValueError("no previous pinned release to roll back to")
    swap(root, back)
    return old, back


def prune(root):
    """Remove releases other than current/previous whose pin no live process holds.
    Returns (removed, kept_referenced)."""
    keep = {current(root), previous(root)}
    removed, referenced = [], []
    for release in sorted(cores_dir(root).glob("*")):
        if not release.is_dir() or not SHA.fullmatch(release.name) or release.name in keep:
            continue
        pin = pin_path(release)
        try:
            fd = os.open(pin, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            continue  # never finished installing; a later install replaces it
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                referenced.append(release.name)
                continue
            # Holding the exclusive pin: a launcher that opened it waits, then sees the release gone.
            shutil.rmtree(release)
            pin.unlink()
            removed.append(release.name)
        finally:
            os.close(fd)
    return removed, referenced
