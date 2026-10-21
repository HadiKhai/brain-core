"""Worktrees mechanics for Brain."""
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
from . import nodes as _m_nodes
from . import status as _m_status
from . import verify as _m_verify


WT_PORT_BASE = 4100
SETUP_FILE = "worktree-setup.sh"  # in a node folder: prepares each new worktree (env files, installs)


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
    _m_jobs.job_started(inst, n, a.branch, n["fm"].get("runtime") or inst.cfg.get("default_runtime", ""))
    print(f"worktree: {_m_common.tilde(path)}\n  branch {a.branch}{'' if exists else ' (new, from ' + base + ')'}  ·  "
          f"{port_text(port, count)}")
    setup = n["dir"] / SETUP_FILE
    _m_instance.unlock()  # the setup can take minutes and touches only the worktree
    if setup.exists() and not a.no_setup:
        env = {**os.environ, "MAIN": str(home), "WORKTREE": str(path), "PORT": str(port), "PORTS": str(count),
               "PORT_LAST": str(port + count - 1), "BRANCH": a.branch, "NODE": n["id"], "WORKBENCH": str(wb)}
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


def worktree_state(home, w, branch, n=None):
    """(uncommitted files or None if gone, merged?, commits ahead of integration). A branch that never moved past
    where it started isn't merged: it's new."""
    path = w["path"]
    base = (_m_verify.integration_ref(home) if n and n["fm"].get("merge", "local") == "pr"
            else "refs/heads/main")
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
    ahead = _m_common.git("rev-list", "--count", f"{base}..{branch}", cwd=home).stdout.strip() if base and tip else ""
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
    c = _m_common.git("rev-list", "--count", f"{w['start']}..{branch}", cwd=home).stdout.strip()
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
    for row in _m_status.active_work(n):
        if row[0] in owners and len(row) > 1 and row[1].strip() not in ("", "-", "—"):
            return "live bound thread in Active work for " + row[0]
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
    state = worktree_state(home, w, a.branch, n) if w else (None, False, "")
    _m_jobs.print_job_handback(n, a.branch)
    remove_worktree(inst, n, a.branch, a.force)
    _m_jobs.job_ended(inst, n, a.branch, "merged" if state[1] else "abandoned", job_commits(home, w, a.branch), a.cause or "",
              worktree=(w or {}).get("path"))
    print(f"removed the worktree for {a.branch} (the branch itself stays)")


def cmd_wt_prune(a):
    """Remove clean worktrees merged into the integration branch. Never touches uncommitted work."""
    inst = _m_nodes.load()
    n, home = git_home(inst, a.id)
    if _m_common.git("remote", cwd=home).stdout.strip() and not a.offline:
        _m_common.git("fetch", "--all", "--prune", "--quiet", cwd=home)
    removed, kept = [], []
    for br, w in sorted(n["local"].get("worktrees", {}).items()):
        reason = removal_refusal(n, br, w)
        if reason:
            kept.append(f"{br} ({reason})")
            continue
        dirty, merged, ahead = worktree_state(home, w, br, n)
        if dirty is None or (merged and dirty == 0):
            _m_jobs.print_job_handback(n, br)
            if not a.dry_run:
                remove_worktree(inst, inst.node(n["id"]), br)
                _m_jobs.job_ended(inst, n, br, "merged" if merged else "abandoned", job_commits(home, w, br),
                          "" if merged else "worktree was gone", worktree=w.get("path"))
            removed.append(br)
        else:
            kept.append(f"{br} ({'uncommitted work' if dirty else 'not merged'})")
    _m_common.git("worktree", "prune", cwd=home)
    print(f"{'would remove' if a.dry_run else 'removed'}: {', '.join(removed) or 'none'}")
    if kept:
        print(f"kept: {', '.join(kept)}")
