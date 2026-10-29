"""Worktrees mechanics for Brain."""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import common as _m_common
from . import instance as _m_instance

_m_guard = _m_common.LazyModule(__package__, 'guard')
_m_jobs = _m_common.LazyModule(__package__, 'jobs')
_m_tasks = _m_common.LazyModule(__package__, 'tasks')
_m_knowledge = _m_common.LazyModule(__package__, 'knowledge')
_m_machine = _m_common.LazyModule(__package__, 'machine')
_m_nodes = _m_common.LazyModule(__package__, 'nodes')
_m_status = _m_common.LazyModule(__package__, 'status')
_m_verify = _m_common.LazyModule(__package__, 'verify')


WT_PORT_BASE = 4100
SETUP_FILE = "worktree-setup.sh"  # in a node folder: prepares each new worktree (env files, installs)


def rebind_job(inst, node, branch):
    """Recover missing ledger ownership from a registered, still-linked checkout."""
    from . import job_adoption
    with _m_instance.brain_lock(inst):
        inst = _m_instance.Instance(inst.root)
        n, home = git_home(inst, node)
        _m_guard.no_terms_in(n, branch, 'rebound branch')
        if _m_common.git('check-ref-format', '--branch', branch, cwd=home).returncode:
            _m_common.die('rebind refused: invalid branch')
        try:
            path, _ = job_adoption.adoption_checkout(home, branch)
        except ValueError as error:
            _m_common.die(str(error))
        binding = n['local'].get('worktrees', {}).get(branch)
        if not binding or Path(binding['path']).resolve() != path:
            _m_common.die('rebind refused: branch needs its registered worktree binding')
        for other in inst.nodes():
            for name, record in other['local'].get('worktrees', {}).items():
                if Path(record['path']).resolve() == path and (other['id'] != node or name != branch):
                    _m_common.die('rebind refused: checkout also belongs to another job')
        rows = _m_jobs.ledger_rows(n)
        if rows is None:
            _m_common.die('rebind refused: unrecognized job ledger')
        row = _m_jobs.open_job(rows, branch)
        if row:
            print('already bound: ' + branch + ' (' + _m_jobs.job_key(row) + ')')
            return
        # Never reopen historical completion or reuse its verification proof.
        # A surviving active task attempt can restore the original immutable
        # identity, including its contract and worker-owned handback.
        tasks = _m_tasks.read_records(n)
        linked = [task for task in tasks.values() if task.get('branch') == branch
                  and task.get('status') in ('in-progress', 'in-review', 'testing')]
        if len(linked) > 1 or tasks and not linked:
            _m_common.die('rebind refused: no unique active task for this branch')
        if not linked and any(old['Job'] == branch for old in rows):
            _m_common.die('rebind refused: historical job binding exists; use a new branch')
        row = {'Job': branch, 'Started': inst.now_dt().isoformat(), 'Reviews': '0',
               'Runtime': n['fm'].get('runtime', ''), 'Rebound': inst.now()}
        if linked:
            task = linked[0]
            attempts = [attempt for attempt in task.get('attempts', [])
                        if attempt.get('job') == task.get('job') and attempt.get('state') == 'open'
                        and attempt.get('branch') == branch]
            if (len(attempts) != 1 or not inst.parse_time(attempts[0].get('started'))
                    or _m_jobs.job_identity(branch, attempts[0]['started']) != task.get('job')):
                _m_common.die('rebind refused: active task attempt identity is missing or ambiguous')
            row.update(Started=attempts[0]['started'], Task=task['id'])
        if any(_m_jobs.job_key(old) == _m_jobs.job_key(row) for old in rows):
            _m_common.die('rebind refused: historical job identity must stay closed')
        rows.append(row)
        _m_jobs.write_jobs(inst, n, rows)
        inst.commit([], f"{n['id']}: missing ledger job rebound")
        print('rebound: ' + branch + ' (' + _m_jobs.job_key(row) + ')')


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
    if not _m_tasks.provider_pull_on_wake(inst, n):
        _m_common.die("external task sync is unknown; no worktree started")
    try:
        _m_tasks.prepare_start(n, getattr(a, "task_id", None), a.branch)
    except ValueError as error:
        _m_common.die(str(error))
    # Even a setup-free start must not publish a job under native resource pressure.
    # Healthy light creation needs no reservation; held starts use the managed
    # admission lifecycle, including its real terminal outcome and owned release.
    policy = _m_machine.machine_policy()
    if _m_machine.machine_reasons(_m_machine.machine_reading(inst), policy):
        with _m_instance.released(inst, n), \
                _m_machine.machine_run('heavy', inst, n['id'], a.branch) as token:
            _m_machine.machine_wait('heavy', inst, n['id'], job=a.branch, token=token)
        n = inst.node(a.id)  # released admission reloads validated local bindings
        # Admission may outlive a task or capacity change; validate again before Git.
        try:
            _m_tasks.prepare_start(n, getattr(a, 'task_id', None), a.branch)
        except ValueError as error:
            _m_common.die(str(error))
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
    _m_jobs.job_started(inst, n, a.branch, runtime, task_id=getattr(a, "task_id", None))
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
        with _m_verify.verification_resources(inst, n) as resources, tempfile.TemporaryFile() as output:
            started = time.monotonic()
            try:
                code = _m_machine.machine_execute(["sh", str(setup)], inst, n['id'], a.branch,
                                                  cwd=path, env=env, stdout=output, stderr=output)
            except OSError as error:
                code = 125
                output.write(('heavy run: ' + str(error) + '\n').encode())
            duration = time.monotonic() - started
            resources['failed'] = bool(code)
            output.seek(0)
            out = output.read().decode('utf-8', errors='replace').strip()
        message = f"  setup: {'ok' if code == 0 else 'FAILED (exit ' + str(code) + ')'} in {duration:.2f}s"
        if out:
            message += "\n" + "\n".join("    " + l for l in out.splitlines()[-15:])
        print(_m_verify.VerifyScrubber(_m_guard.never_terms(n)).feed(message, final=True), flush=True)
        if code:
            print("  worktree retained with its setup output; resolve the setup failure before starting a worker")
            sys.exit(code if code > 0 else 1)
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


LIST_PR_TIMEOUT = 3

def list_worktree_states(n, pr_cache):
    """One ref/graph/PR observation per repo; only working indexes need separate reads.

    This display snapshot is never used as cleanup or publication evidence.
    """
    home = n['home_path']
    wts = n['local'].get('worktrees', {})
    refs = _m_common.branch_refs(home) or {}
    base = integration_target(n)
    tips = {br: refs.get('refs/heads/' + br, '') for br in wts}
    graph = {}
    valid_graph = False
    if base and refs.get(base) and any(tips.values()):
        result = _m_common.git('rev-list', '--parents', *sorted(set(tips.values()) - {''}),
                               '--not', refs[base], cwd=home)
        valid_graph = result.returncode == 0
        if valid_graph:
            graph = {parts[0]: parts[1:] for line in result.stdout.splitlines()
                     if (parts := line.split())}

    def ahead(tip):
        seen, pending = set(), [tip]
        while pending:
            sha = pending.pop()
            if sha in graph and sha not in seen:
                seen.add(sha)
                pending.extend(graph[sha])
        return str(len(seen)) if valid_graph and tip else ''

    counts = {br: ahead(tip) for br, tip in tips.items()}
    moved = {br for br, tip in tips.items() if tip and tip != wts[br].get('start')}
    merged = {br for br in moved if valid_graph and counts[br] == '0'}
    key = None
    if moved - merged and base and shutil.which('gh') and 'github.com' in _m_nodes.remote_url(home):
        location = _m_common.git('rev-parse', '--git-common-dir', cwd=home)
        key = str((Path(home) / location.stdout.strip()).resolve()) if location.returncode == 0 else str(Path(home).resolve())

    def merged_prs():
        # Fetch every page: a default result limit would lose old squash
        # merges. One CLI invocation serves every registered branch/base.
        # None records an unanswered lookup, which listing reports.
        try:
            result = subprocess.run(['gh', 'pr', 'list', '--state', 'merged', '--limit', '1000000',
                                     '--json', 'headRefName,baseRefName'],
                                    cwd=home, capture_output=True, text=True, timeout=LIST_PR_TIMEOUT)
            if result.returncode == 0:
                return {(pr['headRefName'], pr['baseRefName']) for pr in json.loads(result.stdout or '[]')}
        except (subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
            pass
        return None

    def dirty(path):
        return (len(_m_common.git('status', '--porcelain', cwd=path).stdout.splitlines())
                if Path(path).exists() else None)

    # The PR lookup is network-bound; overlap it with the bounded index reads.
    with ThreadPoolExecutor(max_workers=1) as lookup, ThreadPoolExecutor(max_workers=8) as pool:
        prs = lookup.submit(merged_prs) if key is not None and key not in pr_cache else None
        values = dict(zip(wts, pool.map(dirty, (w['path'] for w in wts.values()))))
        if prs is not None:
            pr_cache[key] = prs.result()
    if key is not None and pr_cache[key]:
        target = base.removeprefix('refs/heads/').removeprefix('refs/remotes/origin/')
        merged.update(br for br in moved if (br, target) in pr_cache[key])
    return {br: (values[br], br in merged, counts[br]) for br in wts}


def cmd_wt_list(a):
    inst = _m_nodes.load()
    with _m_common.git_read_snapshot():
        _list_worktrees(inst, a)


def _list_worktrees(inst, a):
    pr_cache = {}
    for n in inst.nodes():
        if a.id and n["id"] != a.id:
            continue
        wts = n["local"].get("worktrees", {})
        if not wts:
            continue
        states = list_worktree_states(n, pr_cache)
        print(n["id"])
        for br, w in sorted(wts.items()):
            dirty, merged, ahead = states[br]
            state = "missing" if dirty is None else (f"{dirty} uncommitted" if dirty else "clean")
            print(f"  {br:<28} {port_text(w.get('port'), w.get('ports', 1))}  {state}  "
                  f"{'merged' if merged else (ahead + ' commit(s) ahead') if ahead else ''}  {_m_common.tilde(w['path'])}")
    if None in pr_cache.values():
        print(f"brain: GitHub merged-PR lookup unavailable within {LIST_PR_TIMEOUT} s; "
              "squash-merged branches may show as commits ahead", file=sys.stderr)



def job_commits(home, w, branch):
    """Commits made on the job's branch since its worktree was created (still right after a merge)."""
    if not w or not w.get("start"):
        return ""
    c = _m_common.git("rev-list", "--count", f"{w['start']}..refs/heads/{branch}", cwd=home).stdout.strip()
    return c if c.isdigit() else ""


def cleanup_generation(home, w, branch, n):
    """Fresh local cleanup evidence, deliberately outside the display Git cache.

    This detects changes across released scans; it is not a lease against Git
    writers after the final observation. Git's removal and live-work checks stay
    authoritative. No network lookup belongs in the final locked observation.
    """
    def query(*args, cwd=home, accepted=(0,)):
        result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                                env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
        if result.returncode not in accepted:
            raise ValueError("cleanup Git evidence unavailable")
        return result

    remote = query("remote").stdout.split()
    base = (query("symbolic-ref", "refs/remotes/origin/HEAD").stdout.strip().decode()
            if b"origin" in remote else "refs/heads/main")
    if n["fm"].get("merge", "local") != "pr":
        base = base.replace("refs/remotes/origin/", "refs/heads/", 1)
    tip = query("rev-parse", "--verify", "refs/heads/" + branch).stdout.strip()
    target = query("rev-parse", "--verify", base).stdout.strip()
    ancestor = query("merge-base", "--is-ancestor", tip.decode(), target.decode(), accepted=(0, 1)).returncode == 0
    path = Path(w["path"])
    identity, checkout, head, status = None, None, None, None
    if path.exists():
        stat = path.stat()
        identity = (stat.st_dev, stat.st_ino)
        checkout = query("symbolic-ref", "--quiet", "HEAD", cwd=path).stdout.strip()
        head = query("rev-parse", "--verify", "HEAD", cwd=path).stdout.strip()
        status = query("status", "--porcelain=v1", "-z", "--untracked-files=all", cwd=path).stdout
    return {"base": base, "target": target, "tip": tip, "ancestor": ancestor,
            "identity": identity, "checkout": checkout, "head": head, "status": status}


def cleanup_recheck(home, w, branch, n, generation, state):
    """Retain on a changed generation; preserve squash evidence only for its exact refs."""
    try:
        current = cleanup_generation(home, w, branch, n)
    except (OSError, ValueError):
        current = None
    if current != generation:
        _m_common.die(f"{branch}: cleanup inputs changed or unreadable; worktree retained; retry from fresh state")
    # A positive remote squash lookup remains evidence only for unchanged inputs.
    merged = current["tip"].decode() != w.get("start") and (current["ancestor"] or state[1])
    return (None if current["status"] is None else len(current["status"].split(b"\0")) - 1,
            merged, state[2])


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
    if own:
        refusal = _m_jobs.publication_refusal(n, own)
        if refusal:
            return refusal
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


def worktree_cleanup_result(n, branch, removed=False, record=None):
    """Portable facts for a host-owned tell/settle hook, never host authorization.

    Keep every recorded incarnation and reference visible. The host must verify
    liveness, coordinator protection, pending requests and current ownership.
    A missing workspace forbids messaging its old thread.
    """
    record = record or n['local'].get('worktrees', {}).get(branch) or {}
    exists = bool(record.get('path') and Path(record['path']).is_dir())
    rows = [r for r in (_m_jobs.read_jobs(n) or []) if r['Job'] == branch]
    threads = [{'id': identity, 'reference': reference, 'source': source}
               for reference, source in _m_jobs.bound_worker_threads(n, branch)
               for identity in sorted(_m_jobs.thread_reference_ids(reference))]
    return {'version': 1, 'node': n['id'], 'branch': branch,
            'phase': 'removed' if removed else 'before-remove',
            'workspace': record.get('path'), 'workspace_exists': exists,
            'jobs': [{'key': _m_jobs.job_key(row), 'closed': _m_jobs.closed_job(row),
                      'started': row.get('Started'), 'ended': row.get('Ended')} for row in rows],
            'threads': threads, 'requires_host_validation': True,
            'host_actions': ['inspect', 'tell-if-safe', 'settle-if-safe'] if exists and not removed
                            else ['inspect', 'settle-if-safe']}


def remove_worktree(inst, n, branch, force=False, thread_hook=None):
    w = n["local"].get("worktrees", {}).get(branch)
    if not w:
        _m_common.die(f"{n['id']}: no worktree for '{branch}' made by brain on this machine")
    reason = removal_refusal(n, branch, w)
    if reason:
        _m_common.die(f"{branch}: {reason}; worktree retained")
    if thread_hook is not None:
        if thread_hook(worktree_cleanup_result(n, branch, record=w)) is not True:
            _m_common.die(f"{branch}: thread cleanup not confirmed; worktree retained")
        fresh = _m_instance.Instance(inst.root)
        n = fresh.node(n['id'])
        if n['local'].get('worktrees', {}).get(branch) != w:
            _m_common.die(f"{branch}: worktree binding changed; worktree retained")
        inst.local = fresh.local
        # Host work can take time. Recheck current branch and live job ownership
        # after the callback; the original preflight grants no lasting permission.
        reason = removal_refusal(n, branch, w)
        if reason:
            _m_common.die(f"{branch}: {reason}; worktree retained")
    if Path(w["path"]).exists():
        r = _m_common.git("worktree", "remove", *(["--force"] if force else []), w["path"], cwd=n["home_path"])
        if r.returncode:
            _m_common.die(f"{branch}: {r.stderr.strip()} (uncommitted work? --force discards it)")
    inst.local_of(n["id"])["worktrees"].pop(branch)
    inst.save_local()
    return worktree_cleanup_result(n, branch, removed=True, record=w)


def cmd_wt_rm(a):
    inst = _m_nodes.load()
    n, home = git_home(inst, a.id)
    w = n["local"].get("worktrees", {}).get(a.branch)
    reason = removal_refusal(n, a.branch, w) if w else ""
    if reason:
        _m_common.die(f"{a.branch}: {reason}; worktree retained")
    if not w:
        _m_common.die(f"{n['id']}: no worktree for '{a.branch}' made by brain on this machine")
    with _m_instance.released(inst, n):
        try:
            generation = cleanup_generation(home, w, a.branch, n)
        except (OSError, ValueError):
            _m_common.die(f"{a.branch}: cleanup inputs unreadable; worktree retained")
        state = worktree_state(home, w, a.branch, n)
        cleanup_recheck(home, w, a.branch, n, generation, state)
    prepared = _m_jobs.prepare_job_usage(inst, n, a.branch, (w or {}).get("path"))
    _m_jobs.print_job_handback(n, a.branch)
    _m_jobs.print_worktree_threads(n, a.branch)
    state = cleanup_recheck(home, w, a.branch, n, generation, state)
    remove_worktree(inst, n, a.branch, a.force, thread_hook=cleanup_thread_callback(inst, n, a))
    _m_jobs.job_ended(inst, n, a.branch, "merged" if state[1] else "abandoned", job_commits(home, w, a.branch), a.cause or "",
              worktree=(w or {}).get("path"), prepared=prepared)
    print(f"removed the worktree for {a.branch} (the branch itself stays)")


def prune_process_refusal(path):
    """Retain a checkout while any process has a file or cwd open inside it."""
    if not Path(path).exists():
        return ''
    binary = shutil.which('lsof')
    if not binary:
        return 'process inventory unavailable'
    try:
        result = subprocess.run([binary, '-nP', '-Fpn', '+D', str(path)],
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return 'process inventory unavailable'
    # Even an incomplete inventory proves the checkout busy when it found a PID.
    if any(line.startswith('p') for line in result.stdout.splitlines()):
        return 'running processes use this worktree'
    if result.returncode != 1 or result.stdout.strip() or result.stderr.strip():
        return 'process inventory unavailable'
    return ''


def cmd_wt_prune(a):
    """Remove clean worktrees merged into the integration branch. Never touches uncommitted work."""
    inst = _m_nodes.load()
    n, home = git_home(inst, a.id)
    if not a.dry_run and _m_common.git("remote", cwd=home).stdout.strip() and not a.offline:
        with _m_instance.released(inst, n):
            _m_common.git("fetch", "--all", "--prune", "--quiet", cwd=home)
    removed, kept = [], []
    for br, w in sorted(n["local"].get("worktrees", {}).items()):
        if any(_m_common.fnmatch.fnmatchcase(br, pattern) or
               _m_common.fnmatch.fnmatchcase(str(w.get('path', '')), pattern)
               for pattern in getattr(a, 'exclude', [])):
            kept.append(f"{br} (excluded)")
            continue
        reason = removal_refusal(n, br, w)
        if reason:
            kept.append(f"{br} ({reason})")
            continue
        if not integration_target(n):
            kept.append(f"{br} (origin default unavailable; run git remote set-head origin -a)")
            continue
        with _m_instance.released(inst, n):
            try:
                generation = cleanup_generation(home, w, br, n)
            except (OSError, ValueError):
                kept.append(f"{br} (cleanup inputs unreadable; worktree retained)")
                continue
            dirty, merged, ahead = worktree_state(home, w, br, n)
            cleanup_recheck(home, w, br, n, generation, (dirty, merged, ahead))
        if dirty is None or (merged and dirty == 0):
            with _m_instance.released(inst, n):
                reason = prune_process_refusal(w['path'])
            if reason:
                kept.append(f"{br} ({reason})")
                continue
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
                prepared = _m_jobs.prepare_job_usage(inst, inst.node(n["id"]), br, w.get("path"))
                with _m_instance.released(inst, n):
                    reason = prune_process_refusal(w['path'])
                if reason:
                    kept.append(f"{br} ({reason})")
                    continue
                dirty, merged, ahead = cleanup_recheck(home, w, br, n, generation, (dirty, merged, ahead))
                remove_worktree(inst, inst.node(n["id"]), br, thread_hook=cleanup_thread_callback(inst, n, a))
                _m_jobs.job_ended(inst, n, br, "merged" if merged else "abandoned", job_commits(home, w, br),
                          "" if merged else "worktree was gone", worktree=w.get("path"), prepared=prepared)
            removed.append(br)
        else:
            kept.append(f"{br} ({'uncommitted work' if dirty else 'not merged'})")
    if not a.dry_run:
        _m_common.git("worktree", "prune", cwd=home)
    print(f"{'would remove' if a.dry_run else 'removed'}: {', '.join(removed) or 'none'}")
    if kept:
        print(f"kept: {', '.join(kept)}")


def cleanup_thread_callback(inst, n, args):
    if not getattr(args, 'settle_threads', False):
        return None
    from . import host_lifecycle as _m_host_lifecycle
    return lambda facts: _m_host_lifecycle.worktree_thread_hook(inst, n, facts, tell=True)
