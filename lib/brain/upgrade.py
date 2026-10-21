"""Upgrade mechanics for Brain."""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import common as _m_common
from . import guard as _m_guard
from . import history as _m_history
from . import instance as _m_instance
from . import jobs as _m_jobs
from . import nodes as _m_nodes
from . import usage as _m_usage


def cmd_upgrade(a):
    root = _m_nodes.find_root()
    gm = root / ".gitmodules"
    if not gm.exists() or "path = core" not in gm.read_text():
        _m_common.die("core/ isn't a git submodule here; reinstall with install.sh, or replace core/ by hand")
    old = _m_common.git("rev-parse", "HEAD", cwd=root / "core").stdout.strip()
    r = subprocess.run(["git", "-C", str(root), "-c", "protocol.file.allow=always", "submodule", "update",
                        "--remote", "core"], capture_output=True, text=True)
    if r.returncode:
        _m_common.die(f"couldn't update core: {r.stderr.strip()}")
    new = _m_common.git("rev-parse", "HEAD", cwd=root / "core").stdout.strip()
    if old == new:
        print(f"core is up to date ({new[:7]})")
        return
    print(f"core {old[:7]} -> {new[:7]}")
    print(_m_common.git("log", "--oneline", f"{old}..{new}", cwd=root / "core").stdout.rstrip(), flush=True)
    before = {e["id"] for e in changes_entries(_m_common.git("show", f"{old}:CHANGES.md", cwd=root / "core").stdout)}
    fresh = [e for e in changes_entries(_m_common.git("show", f"{new}:CHANGES.md", cwd=root / "core").stdout)
             if e["id"] not in before]
    if fresh:
        print("\nWhat changed for agents (each coordinator sees it in brain due until brain changes <node> --seen):")
        print("\n".join(f"- {e['id']}" for e in fresh), flush=True)
    newbin = str(root / "core" / "bin" / "brain")
    env = {**os.environ, "BRAIN_ROOT": str(root)}
    inst = _m_instance.Instance(root)
    inst.commit(["core", ".gitmodules"], f"Upgrade core {old[:7]} -> {new[:7]}")
    # the new core's own link and check --fix commit whatever they change
    subprocess.run([sys.executable, newbin, "link"], env=env)
    subprocess.run([sys.executable, newbin, "check", "--fix"], env=env)
    _m_usage.usage_restart(root)


# ---------- what changed in core, for the agents ----------

CHANGES_HEAD = re.compile(r"^## (\d{4}-\d{2}-\d{2}): (.+?)\s*$", re.M)
CORE_CHECK_EVERY = 86400  # seconds between asks of the core's remote


def changes_entries(text):
    """CHANGES.md as [{id, date, title, body}], newest first; an entry's id is its heading."""
    found = list(CHANGES_HEAD.finditer(text or ""))
    return [{"id": f"{m[1]}: {m[2]}", "date": m[1], "title": m[2],
             "body": text[m.end():found[i + 1].start() if i + 1 < len(found) else len(text)].strip()}
            for i, m in enumerate(found)]


def changes_baseline(inst):
    """Entry ids this brain's core held before its latest upgrade; None if it never upgraded."""
    log = _m_common.git("log", "-1", "--format=%s", "--grep=^Upgrade core ", cwd=inst.root).stdout.strip()
    m = re.match(r"Upgrade core ([0-9a-f]{7,40}) -> ", log)
    if not m:
        return None
    old = _m_common.git("show", f"{m[1]}:CHANGES.md", cwd=inst.root / "core")
    return {e["id"] for e in changes_entries(old.stdout)} if old.returncode == 0 else set()


def changes_unread(inst, n):
    """Entries this node's coordinator hasn't marked read on this machine. Before it ever marked any, what came
    with the latest upgrade; a brain that never upgraded starts with nothing unread."""
    f = _m_common.CORE / "CHANGES.md"
    entries = changes_entries(f.read_text()) if f.is_file() else []
    seen = inst.local.get("changes_seen", {}).get(n["id"])
    if seen is None:
        base = changes_baseline(inst)
        seen = [e["id"] for e in entries] if base is None else base
    return [e for e in entries if e["id"] not in set(seen)]


def core_update(inst, check=True):
    """(local, remote) when the core's remote has a commit this core lacks. Asked at most daily, five seconds at
    most, from the core's own remote; the answer is cached in .brain/core-remote.json. None when unknown."""
    core = inst.root / "core"
    if os.environ.get("BRAIN_NO_UPDATE_CHECK") or not (core / ".git").exists():
        return None
    local = _m_common.git("rev-parse", "HEAD", cwd=core).stdout.strip()
    cache_file = inst.root / ".brain" / "core-remote.json"
    try:
        cache = json.loads(cache_file.read_text())
    except (OSError, ValueError):
        cache = {}
    if check and (not isinstance(cache.get("checked"), (int, float)) or time.time() - cache["checked"] > CORE_CHECK_EVERY):
        try:
            r = subprocess.run(["git", "-C", str(core), "ls-remote", "origin", "HEAD"], capture_output=True,
                               text=True, timeout=5)
            head = r.stdout.split()[0] if r.returncode == 0 and r.stdout.split() else ""
        except (subprocess.TimeoutExpired, OSError):
            head = ""
        cache = {"checked": time.time(), "head": head}
        try:
            cache_file.parent.mkdir(exist_ok=True)
            _m_common.atomic_write(cache_file, json.dumps(cache) + "\n")
        except OSError:
            pass
    remote = cache.get("head") if isinstance(cache.get("head"), str) else ""
    if not re.fullmatch(r"[0-9a-f]{40}", remote or "") or remote == local:
        return None
    if _m_common.git("merge-base", "--is-ancestor", remote, local, cwd=core).returncode == 0:
        return None  # this core already holds it
    return local, remote


def open_jobs_anywhere(inst):
    """Why this brain isn't idle: open ledger jobs, ledgers that can't be read, and job worktrees on this machine.
    What can't be known counts as busy."""
    out = []
    for n in inst.nodes():
        rows = _m_jobs.read_jobs(n)
        if rows is None:
            out.append((n["id"], "unreadable job ledger"))
        out += [(n["id"], r["Job"]) for r in rows or [] if not r["Ended"]]
        out += [(n["id"], f"worktree {b}") for b, w in n["local"].get("worktrees", {}).items()
                if isinstance(w, dict) and w.get("path") and Path(w["path"]).exists()]
    return out


def auto_upgrade(inst):
    """With auto_upgrade=on on this machine, upgrade when the core's remote moved and no job is open anywhere: a
    worker's rules never change under it. Returns a line saying what happened, or None."""
    if inst.local.get("auto_upgrade") != "on":
        return None
    update = core_update(inst)
    if not update:
        return None
    # Idle is checked, and the upgrade runs, under one hold of the instance lock: no job can start in between.
    # The upgrade is this process's child, so it reuses the lock (BRAIN_LOCK_HELD).
    with _m_instance.brain_lock(inst):
        if open_jobs_anywhere(_m_instance.Instance(inst.root)):
            return None  # retried on a later tick, once the jobs close
        r = subprocess.run([sys.executable, str(_m_common.CORE / "bin" / "brain"), "upgrade"], capture_output=True, text=True,
                           env={**os.environ, "BRAIN_ROOT": str(inst.root), "BRAIN_NO_REEXEC": "1"})
    first = (r.stdout.strip().splitlines() or [""])[0]
    if r.returncode:
        return f"core-upgrade: automatic upgrade failed ({(r.stderr.strip().splitlines() or ['?'])[-1]}); run brain upgrade"
    return f"core-upgraded: {first}; read what changed: brain changes root"


def cmd_changes(a):
    """What changed in core for this node's coordinator: unread entries, or all with --all; --seen marks them read."""
    inst = _m_nodes.load()
    n = inst.node(a.id or "root")
    f = _m_common.CORE / "CHANGES.md"
    entries = changes_entries(f.read_text()) if f.is_file() else []
    if a.seen:
        inst.local.setdefault("changes_seen", {})[n["id"]] = [e["id"] for e in entries]
        inst.save_local()
        print(f"{n['id']}: {len(entries)} change entr{'y' if len(entries) == 1 else 'ies'} marked read on this machine")
        return
    show = entries if a.all else changes_unread(inst, n)
    terms = _m_guard.never_terms(n)
    if a.json:
        print(json.dumps(_m_history.history_screen(show, terms), indent=1, ensure_ascii=False))
        return
    update = core_update(inst)
    if update:
        print(f"core update available ({update[0][:7]} -> {update[1][:7]}): brain upgrade"
              + ("" if inst.local.get("auto_upgrade") == "on" else
                 ", or brain set root auto_upgrade=on to upgrade whenever no job is open"))
    if not show:
        print(f"{n['id']}: nothing new in core since you last read it" if not a.all else "no CHANGES.md")
        return
    for e in show:
        print(_m_nodes.import_text(f"## {e['id']}\n\n{e['body']}", terms) + "\n")
    if not a.all:
        print(f"Done reading? brain changes {n['id']} --seen")
