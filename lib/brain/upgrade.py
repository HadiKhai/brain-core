"""Upgrade mechanics for Brain."""
import json
import os
import re
import subprocess
import stat
import sys
import time
from pathlib import Path

from . import common as _m_common
from . import instance as _m_instance

_m_gate_lock = _m_common.LazyModule(__package__, 'gate_lock')
_m_guard = _m_common.LazyModule(__package__, 'guard')
_m_history = _m_common.LazyModule(__package__, 'history')
_m_jobs = _m_common.LazyModule(__package__, 'jobs')
_m_machine = _m_common.LazyModule(__package__, 'machine')
_m_nodes = _m_common.LazyModule(__package__, 'nodes')
_m_usage = _m_common.LazyModule(__package__, 'usage')
_m_core_releases = _m_common.LazyModule(__package__, 'core_releases')


def _version_git(*args, cwd):
    """Even read-only object lookups must never contact a promisor remote."""
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, errors="replace",
                          env={**os.environ, "GIT_NO_LAZY_FETCH": "1"})


def cached_origin_ref(core):
    ref = _version_git("symbolic-ref", "-q", "refs/remotes/origin/HEAD", cwd=core).stdout.strip()
    return ref if ref.startswith("refs/remotes/origin/") else "refs/remotes/origin/main"


def core_version(core, ref=None, now=None):
    """Installed identity and ancestry against local origin objects only; never refresh the cache."""
    top = _version_git("rev-parse", "--show-toplevel", cwd=core)
    if top.returncode or Path(top.stdout.strip()).resolve() != Path(core).resolve():
        return {"sha": None, "date": None, "ref": ref or "refs/remotes/origin/main", "behind": None,
                "status": "unknown", "cache_age_seconds": None, "reason": "installed core Git identity unavailable"}
    ref = ref or cached_origin_ref(core)
    if (not isinstance(ref, str) or not ref.startswith("refs/remotes/origin/") or
            _version_git("check-ref-format", ref, cwd=core).returncode):
        _m_common.die("--ref must name a cached refs/remotes/origin/<branch> ref")
    identity = _version_git("show", "-s", "--format=%H%n%cI", "HEAD", cwd=core)
    parts = identity.stdout.strip().splitlines() if identity.returncode == 0 else []
    result = {"sha": parts[0] if parts else None, "date": parts[1] if len(parts) > 1 else None,
              "ref": ref, "behind": None, "status": "unknown", "cache_age_seconds": None}
    # A reflog timestamp observes a local ref update, rather than the commit's author date.
    log = _version_git("reflog", "show", "-1", "--format=%gD", "--date=unix", ref, cwd=core)
    stamp = re.search(r"@\{(\d+)\}", log.stdout)
    if stamp:
        result["cache_age_seconds"] = max(0, int((time.time() if now is None else now) - int(stamp[1])))
    if not parts or _version_git("remote", "get-url", "origin", cwd=core).returncode:
        result["reason"] = "installed identity or origin unavailable"
        return result
    remote = _version_git("rev-parse", "--verify", ref + "^{commit}", cwd=core)
    if remote.returncode:
        result["reason"] = "cached ref unavailable"
        return result
    counts = _version_git("rev-list", "--left-right", "--count", "HEAD..." + ref, cwd=core)
    common = _version_git("merge-base", "HEAD", ref, cwd=core)
    if counts.returncode or common.returncode:
        result["reason"] = "ancestry unavailable"
        return result
    ahead, behind = map(int, counts.stdout.split())
    result["ahead"] = ahead
    if ahead and behind:
        result["status"] = "diverged"
    else:
        result.update(behind=behind, status="behind" if behind else "ahead" if ahead else "current")
    return result


def cmd_version(a):
    core = _m_common.CORE
    root = _m_nodes.try_find_root()
    if root and (root / "core").is_dir():
        core = root / "core"
    info = core_version(core, ref=a.ref)
    if a.json:
        print(json.dumps(info, indent=2))
        return
    print(f"core {info['sha'] or 'unknown'}; commit date: {info['date'] or 'unknown'}")
    age = info['cache_age_seconds']
    age = f"{age} seconds" if age is not None else "unknown"
    comparison = f"{info['behind']} commits behind" if info['behind'] is not None else info['status']
    print(f"cached {info['ref']}: {comparison}; cache age: {age}; no fetch")
    if info.get("reason"):
        print(info["reason"])


def upgrade_busy_message(root, busy):
    core = root / "core"
    ref = cached_origin_ref(core)
    before = _version_git("show", "HEAD:CHANGES.md", cwd=core)
    after = _version_git("show", ref + ":CHANGES.md", cwd=core)
    available = before.returncode == after.returncode == 0
    old_ids = {e["id"] for e in changes_entries(before.stdout)}
    fresh = [e for e in changes_entries(after.stdout) if e["id"] not in old_ids] if available else []
    lines = ["refused: core upgrade requires an idle brain; affected jobs/worktrees:"]
    lines += [f"- {node}: {reason}" for node, reason in busy]
    lines += [f"Locally cached changes from {ref} (no fetch; cache may be stale):"]
    lines += [f"- {e['id']}\n{e['body']}" for e in fresh] if fresh else [
        "- no new cached change entries" if available else "- change details unavailable locally"]
    lines.append("Retry brain upgrade when idle, after jobs finish and their worktrees are released.")
    return "\n".join(lines)


def _remote_key(url):
    url = (url or "").strip().rstrip("/")
    url = re.sub(r"^git@([^:]+):", r"https://\1/", url)
    url = re.sub(r"^ssh://git@", "https://", url)
    return url[:-4] if url.endswith(".git") else url


def own_node(inst, name=None):
    """The node whose gate an upgrade must wait for: --node, else the one whose home ships this core."""
    if name:
        return inst.node(name)["id"]
    core = _version_git("remote", "get-url", "origin", cwd=inst.root / "core").stdout
    owners = []
    for n in inst.nodes():
        home = n.get("home_path")
        if home and core.strip() and _remote_key(
                _version_git("remote", "get-url", "origin", cwd=home).stdout) == _remote_key(core):
            owners.append(n["id"])
    return owners[0] if len(owners) == 1 else None


def upgrade_busy(inst, idle, node=None):
    """Pinned releases keep foreign gates on their own core: only the own node's work refuses.
    A plain (unmigrated) core runs every process from the directory an upgrade rewrites, so it
    keeps the whole-brain idle guard, as does an own node nobody can name."""
    try:
        layout = _m_core_releases.pinned(inst.root)
    except (OSError, ValueError):
        return [("inventory", "unreadable core release links")]
    own = own_node(inst, node) if layout else None
    if own is None:
        return open_jobs_anywhere(inst, idle=idle)
    return open_jobs_anywhere(inst, idle=idle, own=own)


def _fetch_core_target(root):
    """Fetch the core's origin and name the commit `submodule update --remote` would check out."""
    core = root / "core"
    fetch = subprocess.run(["git", "-C", str(core), "-c", "protocol.file.allow=always", "fetch", "-q", "origin"],
                           capture_output=True, text=True)
    if fetch.returncode:
        _m_common.die(f"couldn't update core: {fetch.stderr.strip()}")
    # Git's precedence: the superproject's local config overrides .gitmodules; "." is its own branch.
    branch = (_m_common.git("config", "--get", "submodule.core.branch", cwd=root).stdout.strip() or
              _m_common.git("config", "-f", str(root / ".gitmodules"), "--get", "submodule.core.branch",
                            cwd=root).stdout.strip())
    if branch == ".":
        branch = _m_common.git("symbolic-ref", "-q", "--short", "HEAD", cwd=root).stdout.strip()
    ref = f"refs/remotes/origin/{branch}" if branch else cached_origin_ref(core)
    target = _version_git("rev-parse", "--verify", ref + "^{commit}", cwd=core)
    if target.returncode or not _m_core_releases.SHA.fullmatch(target.stdout.strip()):
        _m_common.die(f"couldn't update core: {ref} is unavailable after fetch")
    return target.stdout.strip()


def _commit_core(root, message):
    inst = _m_instance.Instance(root)
    inst.commit(["core", ".gitmodules"], message)


def cmd_upgrade(a):
    root = _m_nodes.find_root()
    gm = root / ".gitmodules"
    if not gm.exists() or "path = core" not in gm.read_text():
        _m_common.die("core/ isn't a git submodule here; reinstall with install.sh, or replace core/ by hand")
    idle = []
    try:
        inst = _m_instance.Instance(root)
        busy = upgrade_busy(inst, idle, getattr(a, "node", None))
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        busy = [("inventory", "unreadable job/worktree inventory")]
    if busy:
        _m_common.die(upgrade_busy_message(root, busy))
    if idle:
        print(f"{len(idle)} open jobs/worktrees idle; not affected", flush=True)
    if getattr(a, "rollback", False):
        return core_rollback(root)
    staged = _m_common.git("rev-parse", "HEAD", cwd=root / "core").stdout.strip()
    try:
        running = _m_core_releases.current(root)
    except (OSError, ValueError) as error:
        _m_common.die(f"couldn't read the installed core release: {error}")
    old = running or staged
    # Fetch and validate before the staged core (and its launcher) changes: a release that
    # fails to install never reaches core/bin/brain.
    new = _fetch_core_target(root)
    if old == new and staged == new:
        print(f"core is up to date ({new[:7]})")
        return
    if old != new:
        try:
            if running is None:
                # First install from a plain core: the release it ran becomes the rollback target.
                _m_core_releases.install(root, old)
            _m_core_releases.install(root, new)
            if running is None:
                _m_core_releases.swap(root, old)
            _m_core_releases.swap(root, new)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            _m_common.die(f"couldn't install core {new[:7]} ({error}); running processes keep core {old[:7]}; "
                          f"retry brain upgrade")
    r = _m_common.git("checkout", "-q", "--detach", new, cwd=root / "core")
    if r.returncode:
        print(f"! release {new[:7]} runs, but the staged core stays at {staged[:7]}: {r.stderr.strip()}")
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
    _commit_core(root, f"Upgrade core {old[:7]} -> {new[:7]}")
    # the new core's own link and check --fix commit whatever they change
    subprocess.run([sys.executable, newbin, "link"], env=env)
    subprocess.run([sys.executable, newbin, "check", "--fix"], env=env)
    migration = subprocess.run([sys.executable, newbin, "task", "migrate", "--all"], env=env)
    if migration.returncode:
        print("! task migration needs repair; see core/docs/tasks.md before starting jobs")
    _prune_releases(root)
    _m_usage.usage_restart(root)


def _prune_releases(root):
    try:
        removed, referenced = _m_core_releases.prune(root)
    except (OSError, ValueError) as error:
        print(f"! old core releases kept: {error}")
        return
    if removed:
        print(f"pruned {len(removed)} unreferenced core release(s)")
    if referenced:
        print(f"kept {len(referenced)} core release(s) still pinned by running processes")


def core_rollback(root):
    """Swap back to the previous release, then point the staged core and the gitlink at it."""
    try:
        old, back = _m_core_releases.rollback(root)
    except (OSError, ValueError) as error:
        _m_common.die(f"rollback: {error}")
    print(f"core {old[:7]} -> {back[:7]} (rolled back; running processes keep their core)", flush=True)
    r = _m_common.git("checkout", "-q", "--detach", back, cwd=root / "core")
    if r.returncode:
        print(f"! staged core still at {old[:7]}: {r.stderr.strip()}; the running release is {back[:7]}")
        return
    _commit_core(root, f"Roll back core {old[:7]} -> {back[:7]}")


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
            _m_common.atomic_write(cache_file, json.dumps(cache) + "\n", private=True)
        except OSError:
            pass
    remote = cache.get("head") if isinstance(cache.get("head"), str) else ""
    if not re.fullmatch(r"[0-9a-f]{40}", remote or "") or remote == local:
        return None
    if _m_common.git("merge-base", "--is-ancestor", remote, local, cwd=core).returncode == 0:
        return None  # this core already holds it
    return local, remote


def _ledger_inventory_readable(n):
    """A format marker without a well-formed table cannot establish an idle brain."""
    path = n["dir"] / _m_jobs.JOBS_FILE
    try:
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode):
            return False
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return True
    except (OSError, UnicodeError):
        return False
    header = None
    separator = False
    for line in lines:
        if header is None:
            if not line.startswith("| "):
                continue
            cells = [_m_jobs.uncell(c) for c in re.split(r"(?<!\\)\|", line.strip()[1:-1])]
            if not line.rstrip().endswith("|") or cells[:2] != ["Job", "Started"] or "Ended" not in cells:
                return False
            if len(cells) != len(set(cells)):
                return False
            header = cells
        elif line.strip():
            if not line.startswith("| ") or not line.rstrip().endswith("|"):
                return False
            cells = [_m_jobs.uncell(c) for c in re.split(r"(?<!\\)\|", line.strip()[1:-1])]
            if len(cells) != len(header):
                return False
            if not separator:
                if not all(re.fullmatch(r":?-{3,}:?", c) for c in cells):
                    return False
                separator = True
            elif not cells[header.index("Job")] or not cells[header.index("Started")]:
                return False
    return header is not None and separator


def _upgrade_admission_busy():
    """Observe existing admission leases without pruning or publishing the queue."""
    path = _m_machine.machine_dir() / "machine-care-queue.json"
    try:
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode):
            raise ValueError("unsafe admission inventory")
        rows = json.loads(path.read_text())
        if not isinstance(rows, list) or any(not _m_machine.machine_queue.valid(r) for r in rows):
            raise ValueError("invalid admission inventory")
        return [("machine", f"in-flight {r['kind']} admission") for r in rows
                if r["state"] == "running" and _m_machine.machine_queue.alive(r)]
    except FileNotFoundError:
        return []
    except (OSError, ValueError, TypeError, KeyError):
        return [("inventory", "unreadable admission inventory")]


def _upgrade_train_busy(n):
    """A retained flock file is idle; only a held flock (or an unknown read) refuses."""
    import fcntl
    home = n.get("home_path")
    if not home:
        return []
    try:
        home = Path(home)
        # Nodes without a Git home cannot own a repository train lock.
        try:
            (home / ".git").lstat()
        except FileNotFoundError:
            return []
        result = _version_git("rev-parse", "--git-common-dir", cwd=home)
        if result.returncode or not result.stdout.strip():
            raise ValueError("unreadable repository inventory")
        common = Path(result.stdout.strip())
        if not common.is_absolute():
            common = home / common
        lock = common / "brain-train.lock"
        try:
            fd = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return []
        with os.fdopen(fd) as held:
            info = os.fstat(held.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("unsafe train lock")
            try:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return [(n["id"], "in-flight train lock")]
        return []
    except (OSError, ValueError, TypeError):
        return [(n["id"], "unreadable train lock inventory")]


def _upgrade_node_gate_busy(inst, n):
    """Admission can be disabled; a node gate remains authoritative ownership."""
    import fcntl
    path = _m_gate_lock.gate_path(inst.root, n["id"])
    try:
        if path.parent.is_symlink():
            raise ValueError("unsafe gate directory")
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return []
        with os.fdopen(fd) as held:
            info = os.fstat(held.fileno())
            target = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                    not stat.S_ISREG(target.st_mode) or
                    (info.st_dev, info.st_ino) != (target.st_dev, target.st_ino)):
                raise ValueError("unsafe node gate")
            try:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                row = _m_gate_lock.gate_metadata(held.fileno())
                if _m_gate_lock.gate_stale_holder(row):
                    return [(n['id'], f"in-flight node gate (stale holder pid {row['pid']}; descriptor still held)")]
                if _m_gate_lock.gate_waiting(row, n['id']):
                    return []
                return [(n["id"], "in-flight node gate")]
            row = _m_gate_lock.gate_metadata(held.fileno())
            if _m_gate_lock.gate_abandoned(row):
                print(f"upgrade: stale node gate holder pid {row['pid']}; released lock is idle",
                      file=sys.stderr)
        return []
    except (OSError, ValueError, TypeError):
        return [(n["id"], "unreadable node gate inventory")]


def open_jobs_anywhere(inst, idle=None, own=None):
    """In-flight work and unknown inventory refuse; retained idle jobs are only a notice.

    Use the admission queue's existing PID/start-time liveness and repository flock,
    never lease age or the mere presence of a job/worktree. Reading changes no state.
    With `own` (pinned releases only), another node's in-flight work is a notice: it
    finishes on the release it started on.
    """
    try:
        busy = _upgrade_work_inventory(inst, idle, own)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return [("inventory", "unreadable job/worktree inventory")]
    return busy


def _upgrade_work_inventory(inst, idle, own=None):
    idle = [] if idle is None else idle
    out = _upgrade_admission_busy()
    if own is not None:  # admission rows name no node; their processes are pinned
        idle += [(node, f"{reason} keeps its pinned core") for node, reason in out]
        out = []
    for n in inst.nodes():
        if own is not None and n["id"] != own:
            foreign = []
            _upgrade_node_inventory(inst, n, foreign, idle)
            idle += [(node, f"{reason} keeps its pinned core") for node, reason in foreign]
        else:
            _upgrade_node_inventory(inst, n, out, idle)
    return out


def _upgrade_node_inventory(inst, n, out, idle):
    try:
        if not _ledger_inventory_readable(n):
            raise ValueError("unreadable job ledger")
        rows = _m_jobs.read_jobs(n)
        if rows is None:
            raise ValueError("unreadable job ledger")
    except (OSError, ValueError, TypeError, KeyError):
        out.append((n["id"], "unreadable job ledger"))
        rows = []
    idle += [(n["id"], r["Job"]) for r in rows if not r["Ended"]]
    out += _upgrade_train_busy(n)
    out += _upgrade_node_gate_busy(inst, n)
    worktrees = n["local"].get("worktrees", {})
    if not isinstance(worktrees, dict):
        out.append((n["id"], "unreadable worktree inventory"))
        return
    for branch, worktree in worktrees.items():
        if not isinstance(worktree, dict) or not isinstance(worktree.get("path"), str) or not worktree["path"]:
            out.append((n["id"], f"unreadable worktree {branch}"))
            continue
        try:
            Path(worktree["path"]).stat()
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            out.append((n["id"], f"unreadable worktree {branch}"))
        else:
            idle.append((n["id"], f"worktree {branch}"))


def auto_upgrade(inst):
    """With auto_upgrade=on, upgrade when the remote moved and no work is in flight.
    Returns a line saying what happened, or None."""
    if inst.local.get("auto_upgrade") != "on":
        return None
    update = core_update(inst)
    if not update:
        return None
    # Re-read inventory after taking the instance lock, then keep that lock for the
    # child upgrade (BRAIN_LOCK_HELD). Machine admission has its own ownership rules.
    with _m_instance.brain_lock(inst):
        fresh = _m_instance.Instance(inst.root)
        if upgrade_busy(fresh, []):
            return None  # retried on a later tick, once in-flight work stops
        r = subprocess.run([sys.executable, str(_m_common.CORE / "bin" / "brain"), "upgrade"], capture_output=True, text=True,
                           env={**os.environ, "BRAIN_ROOT": str(inst.root), "BRAIN_NO_REEXEC": "1"})
    lines = r.stdout.strip().splitlines()
    idle_notice = lines.pop(0) if lines and re.fullmatch(r"\d+ open jobs/worktrees idle; not affected", lines[0]) else ""
    first = (lines or [""])[0]
    if r.returncode:
        return f"core-upgrade: automatic upgrade failed ({(r.stderr.strip().splitlines() or ['?'])[-1]}); run brain upgrade"
    return f"core-upgraded: {first}" + (f"; {idle_notice}" if idle_notice else "") + "; read what changed: brain changes root"


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
