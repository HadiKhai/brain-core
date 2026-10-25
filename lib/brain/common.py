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


CORE = Path(__file__).resolve().parents[2]
ENTRY_POINT = CORE / "bin" / "brain"
NODE_FIELDS = ["id", "title", "owners", "labels", "home", "runtime", "created"]
LOCAL_KEYS = ["coordinator", "host_project", "home_project", "preview", "preview_tour"]  # per machine (axiom 23)
ITEM_FIELDS = ["id", "title", "kind", "source_node", "source_ref", "learned_at",
               "source_owners", "scope", "status"]
STALE_DAYS = 7
TOUCHED = set()  # every path this command wrote; commits include these and nothing else (axiom 15)


def touch(*paths):
    for x in paths:
        TOUCHED.add(Path(x).absolute())


# ---------- files ----------

def read_fm(path):
    text = Path(path).read_text()
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    if not m:
        return {}, text
    fm = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip()
    return fm, m.group(2)


def write_fm(path, fm, body):
    safe_target(path)
    lines = "\n".join(f"{k}: {v}" for k, v in fm.items())
    Path(path).write_text(f"---\n{lines}\n---\n{body}")
    touch(path)


def safe_target(path):
    """A file brain writes inside an instance stays inside it: neither a folder on the way nor the file itself may
    be a link leading out (a node's job-handbacks/ linked into a home would put job records there)."""
    p = Path(os.path.abspath(path))
    root = next((d for d in p.parents if (d / "instance.md").is_file() and (d / "node.md").is_file()), None)
    if root is None:
        return
    top = root.resolve()
    for q in [p.parent] + ([p] if p.is_symlink() else []):
        real = q.resolve()
        if real != top and top not in real.parents:
            die(f"{p} leads out of the brain through a link; brain writes only inside it (remove the link)")
    try:
        shared = p.is_file() and p.stat().st_nlink > 1  # the file itself, or the one a link leads to
    except OSError:
        shared = False
    if shared:  # a hard link: writing it would change the same file wherever else it is linked
        die(f"{p} is hard-linked to another file; brain won't write a file shared with another place (copy it)")


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


def atomic_write(path, text):
    """Replace a whole file from a same-folder temporary; failures leave the old file intact."""
    path = Path(path)
    safe_target(path)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=path.parent,
                                         prefix="." + path.name + ".", delete=False) as stream:
            tmp = Path(stream.name)
            if path.exists():
                os.fchmod(stream.fileno(), stat.S_IMODE(path.stat().st_mode))
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if tmp and tmp.exists():
            tmp.unlink()


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
