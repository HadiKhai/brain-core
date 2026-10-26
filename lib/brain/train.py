"""Local merge trains. Git owns fast-forwards; Brain never rolls publication back."""
import fcntl
import json
import importlib
import hashlib
import time
import os
import shlex
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from . import briefs as _m_briefs
from . import common as _m_common
from . import docker_leftovers as _m_docker_leftovers
from . import guard as _m_guard
from . import instance as _m_instance
from . import jobs as _m_jobs
from . import land_checks as _m_land_checks
from . import nodes as _m_nodes
from . import spend as _m_spend
from . import status as _m_status
from . import tasks as _m_tasks
from . import verify as _m_verify
from . import worktrees as _m_worktrees


def ref(home, name):
    r = _m_common.git("rev-parse", "--verify", "--quiet", name + "^{commit}", cwd=home)
    if r.returncode:
        _m_common.die("train: missing commit " + name)
    return r.stdout.strip()


def ancestor(home, base, head):
    return _m_common.git("merge-base", "--is-ancestor", base, head, cwd=home).returncode == 0


def main_checkouts(home):
    r = _m_common.git("worktree", "list", "--porcelain", "-z", cwd=home)
    if r.returncode:
        _m_common.die("train: worktrees unreadable; cannot establish main occupancy")
    checkouts = []
    for record in r.stdout.split("\0\0"):
        fields = record.split("\0")
        if "branch refs/heads/main" in fields:
            path = next((f[9:] for f in fields if f.startswith("worktree ")), "")
            head = next((f[5:] for f in fields if f.startswith("HEAD ")), "")
            if not path or not head:
                _m_common.die("train: worktrees unreadable; cannot establish main occupancy")
            checkouts.append((path, head))
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
    if len(checkouts) > 1:
        _m_common.die("train: main is checked out in multiple worktrees; detach extra checkouts before retrying")
    return checkouts


def checkout_clean_at(path, base, tip):
    """Read-only checks, including index flags that can hide independent edits."""
    location = _m_common.git("rev-parse", "--absolute-git-dir", cwd=path)
    if location.returncode:
        return False
    gitdir = Path(location.stdout.removesuffix("\n"))
    if any((gitdir / state).exists() for state in (
            "index.lock", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG", "sequencer")):
        return False
    flags = _m_common.git("ls-files", "-v", "-z", cwd=path)
    others = _m_common.git("ls-files", "--others", "--exclude-standard", "-z", cwd=path)
    if flags.returncode or others.returncode or others.stdout or any(
            entry and not entry.startswith("H ") for entry in flags.stdout.split("\0")):
        return False
    return all(_m_common.git(*args, cwd=path).returncode == 0 for args in (
        ("diff-index", "--cached", "--quiet", "--ignore-submodules=none", base, "--"),
        ("diff-files", "--quiet", "--ignore-submodules=none", "--")))


def landing_failure(home, path, tip, reason):
    """Observe and report; never repair a ref, index, or checkout after failure."""
    current = _m_common.git("rev-parse", "--verify", "refs/heads/main", cwd=home)
    message = "train: " + reason + "; no success recorded; main=" + (
        current.stdout.strip() if not current.returncode else "unreadable: " + current.stderr.strip())
    if path is not None:
        branch = _m_common.git("symbolic-ref", "-q", "HEAD", cwd=path)
        head = _m_common.git("rev-parse", "--verify", "HEAD", cwd=path)
        status = _m_common.git("--no-optional-locks", "status", "--porcelain=v1",
                               "--untracked-files=all", "--ignore-submodules=none", cwd=path)
        command = "git -C " + shlex.quote(str(path))
        message += ("\ncheckout=" + str(path) + "; HEAD=" + head.stdout.strip() +
                    "; branch=" + branch.stdout.strip() + "\ngit status (porcelain):\n" +
                    (status.stdout if not status.returncode else status.stderr) +
                    "\nRecovery: preserve any local edits first; inspect with " + command +
                    " status; then " + command + " merge --ff-only --no-overwrite-ignore --no-autostash " + tip +
                    "; reconcile the retained train journal before rerunning train or pushing.")
    else:
        message += "\nRecovery: inspect main and the retained train journal; reconcile any other writer's publication before rerunning train. Intended tip=" + tip
    _m_common.die(message)


def landing_preflight(home, main, tip):
    checkouts = main_checkouts(home)
    for path, head in checkouts:
        if head != main or not checkout_clean_at(path, main, tip):
            landing_failure(home, path, tip, "operator checkout is dirty or changed")
    return checkouts


def owned_fast_forward(home, main, tip, action):
    """Git's ref log must record this invocation's exact base-to-tip update."""
    location = _m_common.git('rev-parse', '--git-path', 'logs/refs/heads/main', cwd=home)
    if location.returncode:
        return False
    path = Path(location.stdout.removesuffix('\n'))
    if not path.is_absolute():
        path = Path(home) / path
    try:
        with path.open('rb') as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - 4096))
            lines = stream.read().splitlines()
        record, message = lines[-1].rsplit(b'\t', 1)
        old, new, _ = record.split(b' ', 2)
    except (OSError, IndexError, ValueError):
        return False
    return (old == main.encode('ascii') and new == tip.encode('ascii') and
            message == (action + ': Fast-forward').encode('ascii'))


def publish_main(home, main, tip, batch):
    """Git owns checked-out fast-forwards; otherwise publish by one CAS. No undo."""
    checkouts = landing_preflight(home, main, tip)
    path = checkouts[0][0] if checkouts else None
    if ref(home, "refs/heads/main") != main or not ancestor(home, main, tip):
        landing_failure(home, path, tip, "main moved; no update")
    boundary = landing_preflight(home, main, tip)
    if boundary != checkouts:
        landing_failure(home, boundary[0][0] if boundary else path, tip, 'main occupancy changed before publication')
    action = 'brain-land-' + uuid.uuid4().hex
    try:
        if path is None:
            result = _m_common.git("update-ref", "-m", batch, "refs/heads/main", tip, main, cwd=home)
        else:
            # Git must protect ignored files at the actual checkout boundary too.
            # Explicitly disable configured autostash: late edits must cause refusal.
            result = _m_common.git("merge", "--ff-only", "--no-overwrite-ignore", "--no-autostash", tip,
                                   cwd=path, env={**os.environ, 'GIT_REFLOG_ACTION': action})
    except (OSError, subprocess.SubprocessError) as error:
        landing_failure(home, path, tip, "publication interrupted: " + str(error))
    if result.returncode:
        reason = "Git fast-forward failed" if path else "main changed during publication"
        landing_failure(home, path, tip, reason + ": " + result.stdout + result.stderr)
    if ref(home, "refs/heads/main") != tip:
        landing_failure(home, path, tip, "main changed after publication")
    actual = main_checkouts(home)
    if ([p for p, _ in actual] != [p for p, _ in checkouts]
            or any(head != tip for _, head in actual)):
        landing_failure(home, actual[0][0] if actual else path, tip, 'main occupancy changed during publication')
    if path is not None and not owned_fast_forward(home, main, tip, action):
        landing_failure(home, path, tip, 'publication ownership unknown; expected Git fast-forward receipt absent')
    if path is not None:
        branch = _m_common.git("symbolic-ref", "-q", "HEAD", cwd=path)
        if branch.returncode or branch.stdout.strip() != "refs/heads/main" or not checkout_clean_at(path, tip, tip):
            landing_failure(home, path, tip, "checkout changed during fast-forward")


def conflicting_jobs(n, rows, accepted, tip):
    """Coordinator advice only; leave unlanded workers' evidence and gates alone."""
    if rows is None:
        _m_common.die("train: ledger unreadable; no main update")
    landed = {entry["branch"] for entry in accepted}
    conflicts = []
    for row in rows:
        branch = row["Job"]
        if row.get("Ended") or branch in landed or _m_briefs.import_job(branch):
            continue
        head = ref(n["home_path"], "refs/heads/" + branch)
        result = _m_common.git("merge-tree", "--write-tree", tip, head, cwd=n["home_path"])
        if result.returncode == 1:
            conflicts.append({"branch": branch, "key": _m_jobs.job_key(row), "head": head})
        elif result.returncode:
            _m_common.die("train: merge cleanliness is unknown; no main update")
    return conflicts


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
        matches = [r for r in rows if _m_jobs.job_key(r) == entry['key']]
        row = matches[0] if len(matches) == 1 else None
        if (not row or row['Job'] != entry['branch'] or
                (row.get("Ended") and row.get("Head commit") != entry["head"])):
            _m_common.die("train: ledger job changed; retain journal for coordinator reconciliation")
        if data.get('publication_succeeded') is not True:
            _m_common.die('train: publication ownership unknown; no ledger rows changed')
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
    _m_tasks.train_finished(inst, n, rows, data)
    data["state"] = "reconciled" if reconciled else "complete"
    save(path, data)
    inst.commit([], f"{n['id']}: train merged")
    for entry in data["accepted"]:
        row = next(r for r in rows if _m_jobs.job_key(r) == entry["key"])
        _m_jobs.print_thread_checklist(n, row)
    if data['accepted']:
        print('train: settlement pending; inspect and settle through the host, then record '
              'fresh evidence with brain jobs ' + n['id'] +
              ' --settled --all-closed --attestation-file <fresh-export.json>. '
              'Printed instructions perform no host settlement.')


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
    landing_preflight(home, ref(home, "refs/heads/main"), ref(home, "refs/heads/main"))
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
        primary = None
        try:
            run_train(inst, n, home, verify, a)
        except BaseException as error:
            primary = error
            raise
        finally:
            # Attempt all finalizers, preserving any publication/execution failure.
            errors = []
            for action in (lambda: inst.commit([], f"{n['id']}: train state"),
                           lambda: _m_verify.verify_cleanup(inst, n),
                           lambda: _m_docker_leftovers.docker_leftover_nodes(inst, [n])):
                try:
                    action()
                except BaseException as error:
                    errors.append(error)
            if errors:
                if primary is None:
                    raise errors[0]
                primary.cleanup_errors = [type(error).__name__ + ': ' + str(error) for error in errors]
                print('train: cleanup incomplete; primary failure retained', file=sys.stderr)


def reconcile_manual_landings(inst, n):
    """Close only completed exact-head jobs whose commits are already in local main."""
    closed = []
    with _m_instance.brain_lock(inst):
        current = _m_instance.Instance(inst.root).node(n['id'])
        for row in _m_jobs.read_jobs(current) or []:
            if row.get('Ended') or _m_briefs.import_job(row['Job']):
                continue
            if _m_jobs.publication_refusal(current, row) or not _m_jobs.manual_landing(current, row):
                continue
            state = _m_verify.ready_state(inst, current, row['Job'], train=True)
            if not state['ready']:
                continue
            _m_jobs.close_ledger_job(inst, current, row['Job'], preview=False)
            closed.append(_m_jobs.job_key(row))
            print('train: reconciled manual landing ' + row['Job'])
    if closed and 'preview_upgrade' in n['local']:
        from . import preview_upgrade as _m_preview_upgrade
        result = _m_preview_upgrade.landed_preview(inst, n, _m_preview_upgrade.preview_landed_head(n))
        print('train: reconciled preview ' + result['state'])
    return closed


def post_publication(inst, n, path, data):
    """Host settlement runs outside the instance lock; failure cannot undo landing."""
    if data.get('publication_succeeded') is True and 'preview_upgrade' in n['local']:
        from . import preview_upgrade as _m_preview_upgrade
        preview = _m_preview_upgrade.landed_preview(inst, n, data['tip'])
        with _m_instance.brain_lock(inst):
            current = json.loads(path.read_text())
            if current.get('tip') != data.get('tip') or current.get('accepted') != data.get('accepted'):
                raise ValueError('train journal changed; preview receipt retained separately')
            current['preview_upgrade'] = preview
            save(path, current)
        print('train: preview ' + preview['state'])
    if not data.get('settle_requested') or data.get('publication_succeeded') is not True:
        return None
    keys = [entry['key'] for entry in data['accepted']]
    if not keys:
        return None
    if set((data.get('settlement') or {}).get('settled', [])) == set(keys):
        return data['settlement']
    _m_instance.unlock()
    try:
        module = importlib.import_module('.host_lifecycle', __package__)
        result = module.settle_landed_jobs(inst, n, keys)
        if (not isinstance(result, dict) or not all(isinstance(result.get(k), list) for k in ('settled', 'pending', 'errors'))
                or any(key not in keys for key in result['settled'])):
            raise ValueError('invalid settlement result')
    except (Exception, SystemExit) as error:
        result = dict(settled=[], pending=[dict(job=key, reason='settlement unconfirmed') for key in keys],
                      errors=[type(error).__name__])
    with _m_instance.brain_lock(inst):
        current = json.loads(path.read_text())
        if current.get('tip') != data.get('tip') or current.get('accepted') != data.get('accepted'):
            raise ValueError('train journal changed; settlement result retained by native lifecycle')
        current['settlement'] = result
        save(path, current)
    print('train: landed ' + data['tip'] + '; settlement ' +
          ('complete' if not result['pending'] and not result['errors'] else 'pending'))
    return result


def cmd_land(a):
    """One coordinator command reconciles, gates, publishes and safely settles."""
    inst = _m_nodes.load()
    n, _ = _m_worktrees.git_home(inst, a.id)
    reconcile_manual_landings(inst, n)
    a.settle = True
    return cmd_train(a)


def train_configuration(n):
    """Keep execution inputs while excluding thread routing from publication proof."""
    routing = {'workers', 'coordinator', 'coordinator_sessions'}
    return _m_jobs.digest([str(n['home_path']),
                          {k: v for k, v in n['fm'].items() if k not in routing},
                          {k: v for k, v in n['local'].items() if k not in routing}])


def run_train(inst, n, home, verify, a):
    folder, records = journals(n)
    main = ref(home, "refs/heads/main")
    for path, data in records:
        if data["state"] in ("publishing", "publication_failed"):
            checkouts = main_checkouts(home)
            landing_failure(home, checkouts[0][0] if checkouts else None, data["tip"],
                            "unresolved publication; reconcile retained journal " + str(path))
        if data["state"] == "verified" and ancestor(home, data["tip"], main):
            if data.get("publication_succeeded") is not True:
                _m_common.die("train: publication ownership unknown; reconcile retained journal before retrying")
            landing_preflight(home, main, main)
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
        for old_path, old_data in records:
            if old_data.get('state') in ('complete', 'reconciled'):
                if getattr(a, 'settle', False):
                    old_data['settle_requested'] = True
                post_publication(inst, n, old_path, old_data)
        print("train: no ready jobs")
        return
    date = inst.now_dt().strftime("%Y-%m-%d")
    number = 1
    while (folder / f"{date}-{number}.json").exists() or _m_common.git("show-ref", "--verify", "--quiet", f"refs/heads/train/{date}-{number}", cwd=home).returncode == 0:
        number += 1
    batch = f"train/{date}-{number}"
    path = folder / f"{date}-{number}.json"
    data = {"version": 1, "batch": batch, "state": "building", "base": main,
            "queued": queue, "accepted": [], "ejected": [], "runs": [],
            "settle_requested": getattr(a, 'settle', False),
            "origin_base": _m_common.git('rev-parse', '--verify', '--quiet',
                                          'refs/remotes/origin/main^{commit}', cwd=home).stdout.strip()}
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
    expected_configuration = train_configuration(n)
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
            started = time.monotonic()
            result, log = _m_verify.run_verify(inst, n, batch, verify, w, env)
            duration = time.monotonic() - started
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
            data["runs"].append({"jobs": [e["branch"] for e in entries], "head": head, "result": result,
                                 "log": log, "seconds": duration, "command": verify,
                                 "log_digest": hashlib.sha256(Path(log).read_bytes()).hexdigest()})
            save(path, data)
            if result == 0:
                for entry in entries:
                    _m_verify.record_full_fact(inst, n, entry['branch'], head, main,
                                              _m_jobs.digest([verify, inputs]))
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
            if train_configuration(current) != expected_configuration:
                _m_common.die("train: configuration changed; no main update")
            for entry in accepted:
                entry_ready(fresh, current, entry)
                entry["commits"] = int(_m_common.git("rev-list", "--count", entry["base"] + ".." + entry["head"], cwd=home).stdout)
            if dirty(stage) or ref(stage, "HEAD") != tip or _m_verify.verify_inputs(n, w) != verified_inputs:
                _m_common.die("train: staged verification evidence changed; no main update")
            landing_preflight(home, main, tip)
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
            conflicts = conflicting_jobs(current, _m_jobs.read_jobs(current), accepted, tip)
            data.update(state="publishing", tip=tip, accepted=accepted, rebase=conflicts, ended=_m_jobs.stamp(inst))
            save(path, data)  # An interrupted publication always requires operator reconciliation.
            try:
                publish_main(home, main, tip, batch)
            except BaseException:
                data["state"] = "publication_failed"
                save(path, data)
                raise
            data.update(state="verified", publication_succeeded=True)
            save(path, data)
            finish(fresh, current, path, data)
            for entry in conflicts:
                print(_m_guard.redact("train: rebase required (conflicts with landed main): " + entry["branch"], _m_guard.never_terms(current)))
        post_publication(fresh, current, path, data)
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
