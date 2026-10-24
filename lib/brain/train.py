"""Local merge trains. Staging is disposable; publication is a compare-and-swap fast-forward."""
import fcntl
import json
import os
import shlex
import shutil
from pathlib import Path

from . import common as _m_common
from . import docker_leftovers as _m_docker_leftovers
from . import guard as _m_guard
from . import instance as _m_instance
from . import jobs as _m_jobs
from . import land_checks as _m_land_checks
from . import nodes as _m_nodes
from . import spend as _m_spend
from . import status as _m_status
from . import verify as _m_verify
from . import worktrees as _m_worktrees


def ref(home, name):
    r = _m_common.git("rev-parse", "--verify", "--quiet", name + "^{commit}", cwd=home)
    if r.returncode:
        _m_common.die("train: missing commit " + name)
    return r.stdout.strip()


def ancestor(home, base, head):
    return _m_common.git("merge-base", "--is-ancestor", base, head, cwd=home).returncode == 0


def main_unoccupied(home):
    r = _m_common.git("worktree", "list", "--porcelain", "-z", cwd=home)
    if r.returncode or "branch refs/heads/main" in r.stdout.split("\0"):
        _m_common.die("train: main is checked out or worktrees unreadable; detach main in its coordinator-owned checkout first; never change the operator checkout")
    for token in r.stdout.split("\0"):
        if not token.startswith("worktree "):
            continue
        try:
            location = _m_common.git("rev-parse", "--absolute-git-dir", cwd=token[9:])
        except OSError:
            _m_common.die("train: worktrees unreadable; cannot establish main occupancy")
        if location.returncode:
            _m_common.die("train: worktrees unreadable; cannot establish main occupancy")
        for state in ("rebase-merge", "rebase-apply"):
            try:
                name = (Path(location.stdout.removesuffix("\n")) / state / "head-name").read_text().strip()
            except FileNotFoundError:
                continue
            except OSError:
                _m_common.die("train: rebase state unreadable; cannot establish main occupancy")
            if name == "refs/heads/main":
                _m_common.die("train: main is checked out in a rebase; leave the operator checkout untouched")


def save(path, data):
    _m_common.atomic_write(path, json.dumps(data, indent=2) + "\n")
    _m_common.touch(path)


def finish(inst, n, path, data):
    """Idempotent after a crash between main publication and ledger publication."""
    rows = _m_jobs.read_jobs(n)
    if rows is None:
        _m_common.die("train: ledger unreadable; retain journal and repair before continuing")
    reconciled = False
    for entry in data["accepted"]:
        row = next((r for r in rows if _m_jobs.job_key(r) == entry["key"]), None)
        if not row or (row.get("Ended") and row.get("Head commit") != entry["head"]):
            _m_common.die("train: ledger job changed; retain journal for coordinator reconciliation")
        reconciled = reconciled or bool(row.get("Ended"))
        started, ended = inst.parse_time(row["Started"]), inst.parse_time(data["ended"])
        if not str(row.get("Usage", "")).startswith("host"):
            row.update(entry.get("usage", {}))
        budget = _m_spend.job_budget(n, row)
        if budget:
            row["Budget"] = _m_spend.budget_text(budget)
        row.update({"Ended": data["ended"], "Outcome": "merged", "Batch": data["batch"],
                    "Commits": str(entry["commits"]), "Lesson check": "required",
                    "Base commit": entry["base"], "Head commit": entry["head"],
                    "Hours": f"{_m_status.elapsed(started, ended) / 3600:.1f}" if started and ended else ""})
    _m_jobs.write_jobs(inst, n, rows)  # one atomic ledger replacement, all jobs or none
    data["state"] = "reconciled" if reconciled else "complete"
    save(path, data)
    inst.commit([], f"{n['id']}: train merged")
    for entry in data["accepted"]:
        row = next(r for r in rows if _m_jobs.job_key(r) == entry["key"])
        _m_jobs.print_thread_checklist(n, row)


def journals(n):
    folder = n["dir"] / "job-trains"
    folder.mkdir(exist_ok=True)
    ignore = folder / ".gitignore"
    text = ignore.read_text() if ignore.exists() else ""
    if "*.log" not in text.splitlines():
        _m_common.atomic_write(ignore, text.rstrip() + "\n*.log\n")
        _m_common.touch(ignore)
    records = []
    for path in sorted(folder.glob("*.json")):
        try:
            data = json.loads(path.read_text())
            if data.get("version") != 1:
                raise ValueError()
        except (OSError, ValueError, AttributeError):
            _m_common.die("train: unreadable batch journal; coordinator must repair it")
        records.append((path, data))
    return folder, records


def entry_ready(inst, n, entry):
    row = _m_jobs.open_job(_m_jobs.read_jobs(n) or [], entry["branch"])
    if not row or _m_jobs.job_key(row) != entry["key"]:
        _m_common.die("train: queued job changed; no main update")
    state = _m_verify.ready_state(inst, n, entry["branch"], train=True)
    if not state["ready"] or _m_jobs.ready_fingerprint(n, row, state["packet"]) != entry["packet"]:
        _m_common.die("train: queued readiness changed; no main update: " + entry["branch"])


def cmd_train(a):
    inst = _m_nodes.load()
    n, home = _m_worktrees.git_home(inst, a.id)
    if n["fm"].get("merge", "local") == "pr":
        _m_common.die("train: merge=pr is not supported; use the per-job PR flow")
    verify = n["fm"].get("verify", "").strip()
    if not verify:
        _m_common.die("train: node has no full verify command")
    if _m_verify.integration_ref(home) not in ("refs/heads/main", "refs/remotes/origin/main"):
        _m_common.die("train: requires local main and a valid cached origin/main default (no fetch is performed)")
    main_unoccupied(home)
    # One train per repository, including invocations from other brains. No stale-lock timeout.
    common = Path(_m_common.git("rev-parse", "--git-common-dir", cwd=home).stdout.strip())
    if not common.is_absolute():
        common = home / common
    lock = common / "brain-train.lock"
    _m_common.safe_target(lock)
    try:
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError:
        _m_common.die("train: unsafe or unreadable repository lock")
    with os.fdopen(fd, "a+") as held:
        if lock.is_symlink() or os.fstat(held.fileno()).st_nlink != 1:
            _m_common.die("train: unsafe repository lock")
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _m_common.die("train: another train owns this repository")
        try:
            run_train(inst, n, home, verify, a)
        finally:
            # Persist ejections/interrupted execution evidence even when publication stops.
            try:
                inst.commit([], f"{n['id']}: train state")
            finally:
                _m_docker_leftovers.docker_leftover_nodes(inst, [n])


def run_train(inst, n, home, verify, a):
    folder, records = journals(n)
    main = ref(home, "refs/heads/main")
    for path, data in records:
        if data["state"] == "verified" and ancestor(home, data["tip"], main):
            finish(inst, n, path, data)
        elif data["state"] in ("building", "verified"):
            # Main wasn't published. Never reuse uncommitted execution evidence.
            data["state"] = "interrupted"
            save(path, data)
    rows = _m_jobs.read_jobs(n)
    if rows is None:
        _m_common.die("train: node ledger is not a Brain ledger")
    requested = a.jobs
    if requested:
        requested = [b for value in requested for b in value.split(",") if b]
        if not requested or len(set(requested)) != len(requested):
            _m_common.die("train: --jobs must name distinct open ready jobs")
    queue, refused = [], []
    for row in rows:
        if row.get("Ended") or (requested and row["Job"] not in requested):
            continue
        state = _m_verify.ready_state(inst, n, row["Job"], train=True)
        packet = state["packet"]
        fingerprint = _m_jobs.ready_fingerprint(n, row, packet)
        ejected = _m_jobs.train_ejection(n, row, packet)
        if not state["ready"] or ejected:
            refused.append((row["Job"], ejected if ejected else "; ".join(state["reasons"])))
            continue
        if ancestor(home, packet["head"], main):
            refused.append((row["Job"], "already integrated; coordinator must reconcile the open ledger"))
            continue
        signal = _m_jobs.read_handback(n, row) or {}
        queue.append({"branch": row["Job"], "key": _m_jobs.job_key(row), "head": packet["head"],
                      "base": packet.get("integration_base", packet["main"]), "packet": fingerprint,
                      "first_ready": signal.get("first_ready") or packet["checked_at"]})
    if requested:
        missing = set(requested) - {e["branch"] for e in queue}
        if missing:
            _m_common.die("train: requested jobs are not ready/open: " + ", ".join(sorted(missing)) +
                          " (" + "; ".join(b + ": " + why for b, why in refused) + ")")
        queue.sort(key=lambda e: requested.index(e["branch"]))
    else:
        queue.sort(key=lambda e: (inst.parse_time(e["first_ready"]) or inst.now_dt()).timestamp())
        for branch, why in refused:
            print(_m_guard.redact("train: not queued " + branch + ": " + why, _m_guard.never_terms(n)))
    if not queue:
        print("train: no ready jobs")
        return
    date = inst.now_dt().strftime("%Y-%m-%d")
    number = 1
    while (folder / f"{date}-{number}.json").exists() or _m_common.git("show-ref", "--verify", "--quiet", f"refs/heads/train/{date}-{number}", cwd=home).returncode == 0:
        number += 1
    batch = f"train/{date}-{number}"
    path = folder / f"{date}-{number}.json"
    data = {"version": 1, "batch": batch, "state": "building", "base": main,
            "queued": queue, "accepted": [], "ejected": [], "runs": []}
    save(path, data)
    wb = _m_worktrees.workbench(inst, n, create=True)
    stage = wb / "scratch" / batch.replace("/", "-")
    count = _m_worktrees.port_count(n)
    port = _m_worktrees.free_port(inst, count)
    w = {"path": str(stage), "port": port, "ports": count}
    env = _m_verify.verify_env(n, batch, w)
    env["WORKBENCH"] = str(wb)
    added = _m_common.git("worktree", "add", "-b", batch, str(stage), main, cwd=home)
    if added.returncode:
        _m_common.die("train: can't create staging worktree: " + _m_guard.redact(added.stderr, _m_guard.never_terms(n)))
    # Reserve ports through the existing allocator, without creating a ledger job for staging.
    inst.local_of(n["id"]).setdefault("worktrees", {})[batch] = {**w, "base": main, "start": main}
    inst.save_local()
    _m_instance.unlock()
    try:
        setup = n["dir"] / _m_worktrees.SETUP_FILE
        if setup.exists():
            result, log = _m_verify.run_verify(inst, n, batch, "sh " + shlex.quote(str(setup)), w, env)
            if result:
                _m_common.die("train: setup failed; see " + log)
        if dirty(stage) or ref(stage, "HEAD") != main:
            _m_common.die("train: setup changed tracked state; staging retained")

        def eject(entry, reason, log=None):
            item = {**entry, "reason": reason, "log": log}
            data["ejected"].append(item)
            save(path, data)
            print(_m_guard.redact(f"train: ejected {entry['branch']}: {reason}" + (f"; log {log}" if log else ""), _m_guard.never_terms(n)))

        def build(entries, conflicts=False):
            if dirty(stage):
                _m_common.die("train: staging dirty; refusing to reset it")
            _m_common.git("reset", "--hard", main, cwd=stage, check=True)
            accepted = []
            for entry in entries:
                before = ref(stage, "HEAD")
                merged = _m_common.git("merge", "--no-edit", entry["head"], cwd=stage)
                if merged.returncode:
                    files = _m_common.git("diff", "--name-only", "--diff-filter=U", "-z", cwd=stage).stdout.split("\0")
                    _m_common.git("merge", "--abort", cwd=stage)
                    if ref(stage, "HEAD") != before or dirty(stage):
                        _m_common.die("train: failed merge couldn't be aborted; staging retained")
                    if not conflicts or not any(files):
                        _m_common.die("train: merge failed unexpectedly; train branch retained: " + _m_guard.redact(merged.stderr, _m_guard.never_terms(n)))
                    eject(entry, "conflict in " + ", ".join(repr(f) for f in files if f))
                else:
                    accepted.append(entry)
            return accepted

        def check(entries):
            head = ref(stage, "HEAD")
            inputs = _m_verify.verify_inputs(n, w)
            result, log = _m_verify.run_verify(inst, n, batch, verify, w, env)
            if dirty(stage) or ref(stage, "HEAD") != head or _m_verify.verify_inputs(n, w) != inputs:
                _m_common.die("train: verify changed staging state; no main update")
            if result:
                # Pin red evidence before another run can rotate the batch's source log.
                target = folder / (path.stem + "-" + os.urandom(12).hex() + ".log")
                _m_common.safe_target(target)
                source_fd = os.open(log, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(source_fd, "rb") as source:
                    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
                    with os.fdopen(fd, "wb") as pinned:
                        shutil.copyfileobj(source, pinned)
                log = str(target)
            data["runs"].append({"jobs": [e["branch"] for e in entries], "head": head, "result": result, "log": log})
            save(path, data)
            return result, log

        verified_inputs = None
        baseline_proven = False
        accepted = build(queue, conflicts=True)
        while accepted:
            result, log = check(accepted)
            if not result:
                verified_inputs = _m_verify.verify_inputs(n, w)
                break
            # Prefix bisection attributes an interaction to the first job making its prefix red.
            if not baseline_proven:
                build([])
                baseline, baseline_log = check([])
                if baseline:
                    _m_common.die("train: baseline verify is red; no job blamed; log " + baseline_log)
                baseline_proven = True
            low, high = 0, len(accepted)
            failing_log = log
            while high - low > 1:
                mid = (low + high) // 2
                build(accepted[:mid])
                failed, prefix_log = check(accepted[:mid])
                if failed:
                    high, failing_log = mid, prefix_log
                else:
                    low = mid
            if high == len(accepted):
                # Only the original full run was red: rule out a transient before blame.
                build(accepted)
                confirmed, failing_log = check(accepted)
                if not confirmed:
                    verified_inputs = _m_verify.verify_inputs(n, w)
                    break
            eject(accepted[high - 1], "first failing prefix of batch", failing_log)
            accepted.pop(high - 1)
            build(accepted)
        if not accepted:
            data["state"] = "empty"
            save(path, data)
            print("train: no jobs released; main unchanged")
            return
        tip = ref(stage, "HEAD")
        # One configured check on the exact integration candidate, attributed to every accepted job.
        landing_rows = [_m_jobs.open_job(_m_jobs.read_jobs(n) or [], entry['branch']) for entry in accepted]
        if n['fm'].get('land_check', '').strip():
            try:
                receipt = _m_land_checks.run_check(inst, n, landing_rows[0], w, env, related=landing_rows[1:])
                if receipt and receipt['result'] != 'passed':
                    _m_common.die('train: land-check failed; no landing or push step')
            except (OSError, ValueError) as error:
                _m_common.die('train: ' + _m_guard.redact(str(error), _m_guard.never_terms(n)))
        # As in per-job closure, accounting is best-effort and never holds the Brain lock.
        for entry in accepted:
            row = _m_jobs.open_job(_m_jobs.read_jobs(n) or [], entry["branch"])
            if row and not str(row.get("Usage", "")).startswith("host"):
                try:
                    worker = n["local"]["worktrees"][entry["branch"]]["path"]
                    entry["usage"] = _m_spend.usage_cells(_m_spend.job_usage(inst, n, row, worker), _m_guard.never_terms(n))
                except (OSError, ValueError, RuntimeError):
                    entry["usage"] = {"Usage": "not counted"}
        with _m_instance.brain_lock(inst):
            fresh = _m_instance.Instance(inst.root)
            current = fresh.node(n["id"])
            if current["home_path"] != home or current["fm"] != n["fm"] or current["local"] != inst.node(n["id"])["local"]:
                _m_common.die("train: configuration changed; no main update")
            for entry in accepted:
                entry_ready(fresh, current, entry)
                entry["commits"] = int(_m_common.git("rev-list", "--count", entry["base"] + ".." + entry["head"], cwd=home).stdout)
            if dirty(stage) or ref(stage, "HEAD") != tip or _m_verify.verify_inputs(n, w) != verified_inputs:
                _m_common.die("train: staged verification evidence changed; no main update")
            main_unoccupied(home)
            if ref(home, "refs/heads/main") != main or not ancestor(home, main, tip):
                _m_common.die("train: main moved; no update")
            if current['fm'].get('land_check', '').strip():
                try:
                    for landing_row in landing_rows:
                        _m_land_checks.require_current(fresh, current, landing_row, w)
                except (OSError, ValueError) as error:
                    _m_common.die('train: ' + _m_guard.redact(str(error), _m_guard.never_terms(n)))
            # Consume receipts while their original job/base bindings are current. The
            # generated command pins the checked SHA, even after this local CAS lands it.
            try:
                from . import policy as _m_policy
                if (not current['fm'].get('land_check', '').strip() and not _m_policy.limit_snapshot(current)
                        and not (current['dir'] / 'publication-policy.json').exists()):
                    # Preserve the existing unconfigured advisory; "when ready" is not consent.
                    release_step = 'git -C ' + shlex.quote(str(home)) + ' push origin main'
                else:
                    release_step = _m_land_checks.push_step(fresh, current, landing_rows[0], w,
                                                          ref='main', home=home, expected_base=main)
                release_note = None
            except (OSError, ValueError) as error:
                release_step = None
                release_note = _m_guard.redact(str(error), _m_guard.never_terms(n))
            data.update(state="verified", tip=tip, accepted=accepted, ended=_m_jobs.stamp(inst))
            save(path, data)  # write-ahead intent makes crash recovery unambiguous
            result = _m_common.git("update-ref", "-m", batch, "refs/heads/main", tip, main, cwd=home)
            if result.returncode:
                _m_common.die("train: main changed during publication; journal retained")
            finish(fresh, current, path, data)
        print(f"train: {batch} merged {len(accepted)} job(s); {len(data['runs'])} full verify run(s)")
        print('Release when ready: ' + release_step if release_step else 'No push step: ' + release_note)
    finally:
        # Keep the train branch and journals. Failed/dirty staging stays for diagnosis.
        if not dirty(stage):
            _m_jobs.print_worktree_threads(n, batch)
            removed = _m_common.git("worktree", "remove", str(stage), cwd=home)
            if removed.returncode == 0:
                with _m_instance.brain_lock(inst):
                    fresh = _m_instance.Instance(inst.root)
                    fresh.local_of(n["id"]).get("worktrees", {}).pop(batch, None)
                    fresh.save_local()


def dirty(stage):
    r = _m_common.git("-c", "diff.ignoreSubmodules=none", "status", "--porcelain", "--ignore-submodules=none", "--untracked-files=all", cwd=stage)
    return r.returncode != 0 or bool(r.stdout.strip())
