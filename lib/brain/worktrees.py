"""Worktrees mechanics for Brain."""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import common as _m_common
from . import guard as _m_guard
from . import instance as _m_instance
from . import jobs as _m_jobs
from . import knowledge as _m_knowledge
from . import nodes as _m_nodes
from . import status as _m_status
from . import verify as _m_verify


WT_PORT_BASE = 4100
SETUP_FILE = "worktree-setup.sh"  # in a node folder: prepares each new worktree (env files, installs)


def job_where(n, branch):
    """Resolve a brief's registered job worktree, or its usual node/home directory."""
    w = n["local"].get("worktrees", {}).get(branch) if branch else None
    if branch and not w:
        _m_common.die(_m_guard.redact(f"no worktree for {branch} in {n['id']} (brain wt add {n['id']} <branch>)", _m_guard.never_terms(n)))
    if w and not Path(w["path"]).exists():
        _m_common.die(f"the worktree for {branch} is gone ({_m_common.tilde(w['path'])}); do not message its old thread; "
                      "start a fresh thread in a fresh worktree for follow-on work")
    where = Path(w["path"]) if w else (n["home_path"] if n["external"] else n["dir"])
    return where, w


def job_run_line(where, w, branch, runtime, model=None, tilde_home=True):
    """Format the shared job location and ports, retaining each caller's home-path style."""
    head = f"Run in: {_m_common.tilde(where) if w or tilde_home else where}"
    if branch:
        head += f"  ·  branch {branch}"
    head += f"  ·  runtime: {runtime}"
    if model:
        head += f"  ·  model: {model}"
    if w:
        head += f"  ·  {port_text(w['port'], w.get('ports', 1))}"
    return head


def workbench(inst, n, create=False):
    """Where AI operations on a node happen: scripts/, reports/ and, for a git home, worktrees/ and scratch/.
    A node whose home is here keeps them in its folder. An external home gets a workbench beside it, so
    worktrees never sit inside the instance (axiom 8) and nothing lands in the home (axiom 6)."""
    if not n["external"]:
        wb = n["dir"]
    elif n["local"].get("workbench"):
        wb = Path(n["local"]["workbench"]).expanduser()
    elif n["home_path"]:
        wb = n["home_path"].parent / f"{n['home_path'].name}-work"
    else:
        _m_common.die(f"{n['id']}: home not set on this machine: brain set {n['id']} home=<path>")
    if create:
        subdirs = ["scripts", "reports"] + (["worktrees", "scratch"] if n["external"] else [])
        for d in subdirs:
            (wb / d).mkdir(parents=True, exist_ok=True)
        if n["external"]:
            gi = wb / ".gitignore"
            if not gi.exists():
                gi.write_text("worktrees/\nscratch/\n.DS_Store\n")
            inst.local_of(n["id"])["workbench"] = str(wb)
            inst.save_local()
    return wb


def cmd_workbench(a):
    inst = _m_nodes.load()
    n = inst.node(a.id)
    wb = workbench(inst, n, create=True)
    if a.path:
        print(wb)
        return
    print(f"{n['id']}: workbench {_m_common.tilde(wb)}")
    print("  scripts/    reusable scripts (screenshots, seeding, audits); one line at the top says what each does")
    print("  reports/    one folder per report: <date>-<topic>/index.md plus its files (brain report)")
    if n["external"]:
        print("  worktrees/  one per job, made by 'brain wt add' (this machine only)")
        print("  scratch/    throwaway (this machine only)")
        print(f"  Not in the home and not in the instance. To keep scripts and reports on other machines, make it a\n"
              f"  private git repository of its own (worktrees/ and scratch/ are already ignored).")


def git_home(inst, nid):
    n = inst.node(nid)
    h = n["home_path"]
    if not n["external"] or not h or not (h / ".git").exists():
        _m_common.die(f"{nid}: worktrees need an external home that is a git repository on this machine")
    return n, h


def default_base(home):
    r = _m_common.git("symbolic-ref", "--short", "refs/remotes/origin/HEAD", cwd=home)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else "HEAD"


def port_count(n):
    """`ports: N` in node.md: how many consecutive ports each worktree of this node needs (default 1)."""
    v = n["fm"].get("ports", "1")
    if not v.isdigit() or int(v) < 1:
        _m_common.die(f"{n['id']}: ports must be a whole number of ports per worktree, not '{v}'")
    return int(v)


def ports_taken(inst):
    """Every port a worktree holds on this machine, in this brain and every other one here."""
    taken = set()
    locals_ = [inst.local] + [_m_nodes.local_of_root(r) for _, r in _m_instance.brains_here() if r.resolve() != inst.root.resolve()]
    for loc in locals_:
        for v in loc.get("nodes", {}).values():
            for w in v.get("worktrees", {}).values():
                if w.get("port"):
                    taken.update(range(w["port"], w["port"] + w.get("ports", 1)))
    return taken


def free_port(inst, count=1):
    """The first block of `count` ports, from WT_PORT_BASE, that no worktree holds and nothing listens on."""
    import socket as so
    taken = ports_taken(inst)

    def listening(port):
        with so.socket(so.AF_INET, so.SOCK_STREAM) as sk:
            return sk.connect_ex(("127.0.0.1", port)) == 0

    port = WT_PORT_BASE
    while any(q in taken or listening(q) for q in range(port, port + count)):
        port += 1
    return port


def port_text(port, count):
    return f"port {port}" if count == 1 else f"ports {port}-{port + count - 1}"


def reuse_submodules(home, path, seen=None):
    """Reuse cached module objects with independent checkout metadata, without fetching.

    Git's submodule setup rewrites core.worktree, so a linked worktree would alter
    the original checkout's config. Share objects only; uncached modules stay in setup.
    """
    if not (path / ".gitmodules").is_file():
        return []
    common = _m_common.git("rev-parse", "--git-common-dir", cwd=home)
    if common.returncode:
        raise ValueError("cannot locate the cached submodule directory")
    common_dir = (home / common.stdout.strip()).resolve()
    seen = set() if seen is None else seen
    identity = (str(common_dir), _m_common.git("rev-parse", "HEAD", cwd=path).stdout.strip())
    if identity in seen:
        raise ValueError("recursive submodule cache cycle; worktree retained")
    seen = seen | {identity}
    config = _m_common.git("config", "-z", "-f", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$", cwd=path)
    if config.returncode not in (0, 1):
        raise ValueError("cannot read submodule paths")
    entries = _m_common.git("ls-files", "--stage", "-z", cwd=path)
    if entries.returncode:
        raise ValueError("cannot read submodule commits")
    links = {entry.split("\t", 1)[1]: entry.split()[1] for entry in entries.stdout.split("\0")
             if entry.startswith("160000 ") and "\t" in entry}
    reused = []
    for record in config.stdout.split("\0"):
        if not record:
            continue
        key, relative = record.split("\n", 1)
        name = key[len("submodule."):-len(".path")]
        store = (common_dir / "modules" / name).resolve()
        target = (path / relative).resolve()
        if (not name or Path(name).is_absolute() or ".." in Path(name).parts
                or common_dir / "modules" not in store.parents or path.resolve() not in target.parents):
            raise ValueError("unsafe cached submodule path; worktree retained")
        commit = links.get(relative)
        if not commit or not store.is_dir():
            continue
        if _m_common.git("--git-dir", str(store), "cat-file", "-e", commit + "^{commit}", cwd=home).returncode:
            continue  # setup may fetch an uncached commit explicitly
        result = _m_common.git("clone", "--shared", "--no-checkout", "--", str(store), str(target), cwd=path)
        if result.returncode:
            raise ValueError("cached submodule checkout failed: " + result.stderr.strip())
        origin = _m_common.git("--git-dir", str(store), "config", "--get", "remote.origin.url", cwd=home)
        commands = [("checkout", "--detach", commit)]
        if origin.returncode == 0 and origin.stdout.strip():
            commands.append(("remote", "set-url", "origin", origin.stdout.strip()))
        for command in commands:
            result = _m_common.git(*command, cwd=target)
            if result.returncode:
                raise ValueError("cached submodule setup failed: " + result.stderr.strip())
        result = _m_common.git("submodule", "absorbgitdirs", "--", relative, cwd=path)
        if result.returncode:
            raise ValueError("cached submodule metadata setup failed: " + result.stderr.strip())
        reused.append(relative)
        reused.extend(relative + "/" + child for child in reuse_submodules(store, target, seen))
    return reused


def cmd_wt_add(a):
    inst = _m_nodes.load()
    n, home = git_home(inst, a.id)
    if _m_guard.find_terms(a.branch, _m_guard.never_terms(n)):
        _m_common.die(f"that branch name contains {', '.join(_m_guard.find_terms(a.branch, _m_guard.never_terms(n)))}: pick another")
    for warning in _m_status.code_setup_warnings(inst, n):
        print("! " + warning)
    wb = workbench(inst, n, create=True)
    path = wb / "worktrees" / a.branch.replace("/", "-")
    for owner, record in n["local"].get("worktrees", {}).items():
        if owner != a.branch and Path(record["path"]).resolve() == path.resolve():
            _m_common.die(f"{_m_common.tilde(path)} belongs to job {owner}; create a fresh worktree")
    if path.exists() or path.is_symlink():
        _m_common.die(f"{_m_common.tilde(path)} already exists; create a fresh worktree")
    # Record the same default/pin selection as the first worker brief, before setup.
    route = argparse.Namespace(branch=a.branch, model=None)
    runtime, _ = _m_knowledge.worker_route(inst, n, route)
    exists = _m_common.git("rev-parse", "--verify", "--quiet", f"refs/heads/{a.branch}", cwd=home).returncode == 0
    base = a.base or default_base(home)
    args = ["worktree", "add", str(path), a.branch] if exists else ["worktree", "add", "-b", a.branch, str(path), base]
    r = _m_common.git(*args, cwd=home)
    if r.returncode:
        _m_common.die(_m_guard.redact(f"git {' '.join(args)}: {r.stderr.strip()}", _m_guard.never_terms(n)))
    count = port_count(n)
    port = free_port(inst, count)
    inst = _m_instance.Instance(inst.root)  # git may have touched nothing here, but reload bindings to be safe
    start = _m_common.git("rev-parse", "HEAD", cwd=path).stdout.strip()
    inst.local_of(n["id"]).setdefault("worktrees", {})[a.branch] = {
        "path": str(path), "port": port, **({"ports": count} if count > 1 else {}), "base": base, "start": start,
        "created": inst.now()}
    inst.local_of(n["id"]).setdefault("workbench", str(wb))
    inst.save_local()
    _m_jobs.job_started(inst, n, a.branch, runtime)
    print(f"worktree: {_m_common.tilde(path)}\n  branch {a.branch}{'' if exists else ' (new, from ' + base + ')'}  ·  "
          f"{port_text(port, count)}")
    setup = n["dir"] / SETUP_FILE
    from . import events
    if setup.exists() and not a.no_setup:
        events.checkpoint_setup()
    _m_instance.unlock()  # the setup can take minutes and touches only the worktree
    try:
        for module in reuse_submodules(home, path):
            print(_m_guard.redact("  reused submodule: " + module, _m_guard.never_terms(n)))
    except (OSError, ValueError) as error:
        _m_common.die(_m_guard.redact(str(error) + "; worktree retained; resolve before starting a worker",
                                   _m_guard.never_terms(n)))
    if setup.exists() and not a.no_setup:
        env = _m_verify.verify_env(n, a.branch, {"path": str(path), "port": port, "ports": count})
        env["WORKBENCH"] = str(wb)
        print("  setup: running; waiting for completion", flush=True)
        started = time.monotonic()
        r = subprocess.run(["sh", str(setup)], cwd=path, env=env, capture_output=True, text=True)
        duration = time.monotonic() - started
        out = (r.stdout + r.stderr).strip()
        message = f"  setup: {'ok' if r.returncode == 0 else 'FAILED (exit ' + str(r.returncode) + ')'} in {duration:.2f}s"
        if out:
            message += "\n" + "\n".join("    " + l for l in out.splitlines()[-15:])
        print(_m_verify.VerifyScrubber(_m_guard.never_terms(n)).feed(message, final=True), flush=True)
        if r.returncode:
            print("  worktree retained with its setup output; resolve the setup failure before starting a worker")
            sys.exit(r.returncode if r.returncode > 0 else 1)
    elif not setup.exists():
        print(f"  no {SETUP_FILE} in the node folder: nothing copied in (env files, installs)")
    print(f"  launch a worker there (host adapter: an existing worktree at this path, branch {a.branch})")


def integration_target(n):
    """Cached integration ref in PR mode, its local branch in local mode; never fetch."""
    ref = _m_verify.integration_ref(n["home_path"])
    return (ref if n["fm"].get("merge", "local") == "pr"
            else ref.replace("refs/remotes/origin/", "refs/heads/", 1))


def worktree_state(home, w, branch, n=None):
    """(uncommitted files or None if gone, merged?, commits ahead of integration). A branch that never moved past
    where it started isn't merged: it's new."""
    path = w["path"]
    base = integration_target(n or {"home_path": home, "fm": {}})
    dirty = len(_m_common.git("status", "--porcelain", cwd=path).stdout.splitlines()) if Path(path).exists() else None
    tip = _m_common.git("rev-parse", "--verify", "--quiet", "refs/heads/" + branch, cwd=home).stdout.strip()
    moved = bool(tip) and tip != w.get("start")
    merged = moved and bool(base) and _m_common.git("merge-base", "--is-ancestor", "refs/heads/" + branch, base, cwd=home).returncode == 0
    if moved and base and not merged and shutil.which("gh") and "github.com" in _m_nodes.remote_url(home):
        try:
            target = base.removeprefix("refs/heads/").removeprefix("refs/remotes/origin/")
            r = subprocess.run(["gh", "pr", "list", "--head", branch, "--base", target,
                                "--state", "merged", "--json", "number"],
                               cwd=home, capture_output=True, text=True, timeout=30)
            merged = r.returncode == 0 and json.loads(r.stdout or "[]") != []  # squash merges
        except (subprocess.TimeoutExpired, ValueError):
            pass
    ahead = _m_common.git("rev-list", "--count", f"{base}..refs/heads/{branch}", cwd=home).stdout.strip() if base and tip else ""
    return dirty, merged, ahead


def cmd_wt_list(a):
    inst = _m_nodes.load()
    for n in inst.nodes():
        if a.id and n["id"] != a.id:
            continue
        wts = n["local"].get("worktrees", {})
        if not wts:
            continue
        print(n["id"])
        for br, w in sorted(wts.items()):
            dirty, merged, ahead = worktree_state(n["home_path"], w, br, n)
            state = "missing" if dirty is None else (f"{dirty} uncommitted" if dirty else "clean")
            print(f"  {br:<28} {port_text(w.get('port'), w.get('ports', 1))}  {state}  "
                  f"{'merged' if merged else (ahead + ' commit(s) ahead') if ahead else ''}  {_m_common.tilde(w['path'])}")


def job_commits(home, w, branch):
    """Commits made on the job's branch since its worktree was created (still right after a merge)."""
    if not w or not w.get("start"):
        return ""
    c = _m_common.git("rev-list", "--count", f"{w['start']}..refs/heads/{branch}", cwd=home).stdout.strip()
    return c if c.isdigit() else ""


def removal_refusal(n, branch, w):
    """File-derived live-work checks; force never overrides ownership or liveness."""
    path = Path(w["path"]).resolve()
    owners = {branch}
    owners.update(br for br, record in n["local"].get("worktrees", {}).items()
                  if Path(record["path"]).resolve() == path)
    if path.exists():
        actual = _m_common.git("symbolic-ref", "--quiet", "HEAD", cwd=path)
        head = _m_common.git("rev-parse", "--verify", "HEAD", cwd=path)
        tip = _m_common.git("rev-parse", "--verify", "refs/heads/" + branch, cwd=n["home_path"])
        if actual.returncode or actual.stdout.strip() != "refs/heads/" + branch:
            return "checked-out branch differs from recorded job (or HEAD is detached)"
        if head.returncode or tip.returncode or head.stdout.strip() != tip.stdout.strip():
            return "checked-out HEAD differs from recorded job"
    rows = _m_jobs.read_jobs(n)
    if rows is None:
        return "job ledger cannot be read safely"
    # Cleanup closes its own current row; every other claimant is live work.
    own = _m_jobs.open_job(rows, branch)
    for row in rows:
        if row is own or row.get("Ended"):
            continue
        if row["Job"] in owners:
            return "open ledger job " + row["Job"]
    for row in _m_status.active_work(n, include_closed=True):
        recorded_branch = row[0].replace("`", "").split()
        if recorded_branch and recorded_branch[0] in owners and len(row) > 1 and row[1].strip() not in ("", "-", "—"):
            if _m_status.active_work_closed(row):
                continue
            state = row[3].strip() if len(row) > 3 else "missing"
            reason = ("live bound thread in Active work for " + row[0] +
                      f" (state: {state or 'missing'}; reconcile the thread and row before cleanup)")
            return _m_guard.redact(reason, _m_guard.never_terms(n))
    return ""


def remove_worktree(inst, n, branch, force=False):
    w = n["local"].get("worktrees", {}).get(branch)
    if not w:
        _m_common.die(f"{n['id']}: no worktree for '{branch}' made by brain on this machine")
    reason = removal_refusal(n, branch, w)
    if reason:
        _m_common.die(f"{branch}: {reason}; worktree retained")
    if Path(w["path"]).exists():
        r = _m_common.git("worktree", "remove", *(["--force"] if force else []), w["path"], cwd=n["home_path"])
        if r.returncode:
            _m_common.die(f"{branch}: {r.stderr.strip()} (uncommitted work? --force discards it)")
    inst.local_of(n["id"])["worktrees"].pop(branch)
    inst.save_local()


def cmd_wt_rm(a):
    inst = _m_nodes.load()
    n, home = git_home(inst, a.id)
    w = n["local"].get("worktrees", {}).get(a.branch)
    reason = removal_refusal(n, a.branch, w) if w else ""
    if reason:
        _m_common.die(f"{a.branch}: {reason}; worktree retained")
    state = worktree_state(home, w, a.branch, n) if w else (None, False, "")
    _m_jobs.print_job_handback(n, a.branch)
    _m_jobs.print_worktree_threads(n, a.branch)
    remove_worktree(inst, n, a.branch, a.force)
    _m_jobs.job_ended(inst, n, a.branch, "merged" if state[1] else "abandoned", job_commits(home, w, a.branch), a.cause or "",
              worktree=(w or {}).get("path"))
    print(f"removed the worktree for {a.branch} (the branch itself stays)")


def cmd_wt_prune(a):
    """Remove clean worktrees merged into the integration branch. Never touches uncommitted work."""
    inst = _m_nodes.load()
    n, home = git_home(inst, a.id)
    if not a.dry_run and _m_common.git("remote", cwd=home).stdout.strip() and not a.offline:
        _m_common.git("fetch", "--all", "--prune", "--quiet", cwd=home)
    removed, kept = [], []
    for br, w in sorted(n["local"].get("worktrees", {}).items()):
        reason = removal_refusal(n, br, w)
        if reason:
            kept.append(f"{br} ({reason})")
            continue
        if not integration_target(n):
            kept.append(f"{br} (origin default unavailable; run git remote set-head origin -a)")
            continue
        dirty, merged, ahead = worktree_state(home, w, br, n)
        if dirty is None or (merged and dirty == 0):
            if dirty is not None:
                entries = _m_common.git("ls-files", "--stage", "-z", cwd=w["path"])
                if entries.returncode:
                    kept.append(f"{br} (submodule inventory unreadable; worktree retained)")
                    continue
                modules = [entry.split("\t", 1)[1] for entry in entries.stdout.split("\0")
                           if entry.startswith("160000 ") and "\t" in entry]
                if modules:
                    # Git refuses removing populated submodules; their gitdirs can belong
                    # to another checkout. Report them without deinitializing or deleting.
                    kept.append(f"{br} (submodules retained: {', '.join(modules)})")
                    continue
            _m_jobs.print_job_handback(n, br)
            _m_jobs.print_worktree_threads(n, br, dry_run=a.dry_run)
            if not a.dry_run:
                remove_worktree(inst, inst.node(n["id"]), br)
                _m_jobs.job_ended(inst, n, br, "merged" if merged else "abandoned", job_commits(home, w, br),
                          "" if merged else "worktree was gone", worktree=w.get("path"))
            removed.append(br)
        else:
            kept.append(f"{br} ({'uncommitted work' if dirty else 'not merged'})")
    if not a.dry_run:
        _m_common.git("worktree", "prune", cwd=home)
    print(f"{'would remove' if a.dry_run else 'removed'}: {', '.join(removed) or 'none'}")
    if kept:
        print(f"kept: {', '.join(kept)}")
