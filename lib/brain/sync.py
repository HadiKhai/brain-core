"""Sync mechanics for Brain."""
import datetime
import json
import re
import shutil
import subprocess
from pathlib import Path

from . import briefs as _m_briefs
from . import common as _m_common
from . import guard as _m_guard
from . import history as _m_history
from . import instance as _m_instance
from . import jobs as _m_jobs
from . import knowledge as _m_knowledge
from . import nodes as _m_nodes
from . import review as _m_review
from . import status as _m_status
from . import usage as _m_usage
from . import verify as _m_verify
from . import worktrees as _m_worktrees


def cmd_review(a):
    """Brief an independent reviewer, or record the completed host identity receipt."""
    inst = _m_nodes.load()
    n = inst.node(a.id)
    rows = _m_jobs.read_jobs(n)
    if rows is None:
        _m_common.die("cannot record review: unrecognized job ledger")
    row = _m_jobs.open_job(rows, a.branch)
    if getattr(a, 'receipt_file', None):
        try:
            data = json.loads(Path(a.receipt_file).expanduser().read_text())
            _m_review.record_review_receipt(inst, n, a.branch, data)
        except (OSError, ValueError, TypeError) as error:
            _m_common.die(str(error))
        print('review receipt recorded (coordinator-supplied host observation; no independent live host query)')
        return
    round_number = _m_review.review_policy(a, row)
    check_fix = getattr(a, "check_fix", False)
    built = a.built_by or (row or {}).get("Runtime") or n["fm"].get("runtime") or inst.cfg.get("default_runtime", "")
    try:
        fresh = _m_review.independence_policy(inst, n) == 'fresh-thread'
        exception = _m_review.runtime_exception(inst, n, a, built)
    except ValueError as error:
        _m_common.die(str(error))
    others = list(inst.runtimes) if fresh or exception else [rt for rt in inst.runtimes if rt != built]
    kind = _m_jobs.job_class(n, explicit=a.job_class, default="review")
    reviewer = a.by or n["fm"].get("review_runtime", inst.cfg.get("review_runtime")) or (built if fresh else ("claude" if "claude" in others else (others[0] if others else None)))
    if not reviewer:
        _m_common.die(f"only one runtime ({built}) in instance.md: nothing to cross-review with")
    if reviewer == built and not (fresh or exception):
        _m_common.die("the reviewer must run on a different runtime from the builder")
    pin = reviewer if (a.by or fresh or exception or n["fm"].get("review_runtime") or inst.cfg.get("review_runtime")) else (n["fm"].get("runtime") if n["fm"].get("runtime") in others else None)
    per_class = _m_knowledge.class_routing_config(inst, n)
    reviewer, _ = _m_usage.class_runtime(inst, kind, runtime=pin, runtimes=others)
    if not a.model:
        a.model = n['fm'].get('review_model', inst.cfg.get('review_model'))
    model_selection = _m_knowledge.class_model_selection(inst, n, a, reviewer, kind)
    reviewer, subscription, usage = _m_usage.class_route(inst, kind, runtime=pin,
        model=model_selection[0] if model_selection else a.model,
        weekly_cap=n["fm"].get("route_weekly_cap", inst.cfg.get("route_weekly_cap", 60)), runtimes=others)
    usage += (" Independent review requires a fresh non-builder thread; same runtime and model are allowed."
              if fresh or exception else " Independent review uses the other runtime from the builder.")
    _m_knowledge.launch_selection(inst, n, a, reviewer, subscription, row, kind, model_selection=model_selection)
    if per_class or " node.md " in a.model_source:
        usage += f" Routing sources: model={a.model_source}."
    where, base = None, a.base
    if n["external"]:
        if not (n["home_path"] and n["home_path"].exists()):
            _m_common.die(f"{n['id']}'s home isn't on this machine (brain set {n['id']} home=<path>): a reviewer can't run "
                "there, and never in the instance instead")
        w = n["local"].get("worktrees", {}).get(a.branch)
        if w and not Path(w["path"]).exists():
            _m_common.die(f"the worktree for {a.branch} is gone ({_m_common.tilde(w['path'])}); do not message its old thread; "
                          "start a fresh thread in a fresh worktree for follow-on work")
        where = Path(w["path"]) if w else n["home_path"]
        base = base or (w.get("base") if w else None) or _m_worktrees.default_base(n["home_path"])
        d = _m_common.git("diff", "--stat", f"{base}...{a.branch}", cwd=n["home_path"])
    else:
        where, base = n["dir"], base or "main"
        d = _m_common.git("diff", "--stat", f"{base}...{a.branch}", cwd=inst.root)
    if d.returncode:
        _m_common.die(f"can't diff {a.branch} against {base}: {d.stderr.strip()}")
    diffstat = d.stdout.strip()
    resolved = _m_common.git("rev-parse", "--verify", a.branch + "^{commit}", cwd=where)
    if resolved.returncode:
        _m_common.die("cannot resolve review branch")
    head = resolved.stdout.strip()
    fix_lines = _m_review.fix_check_lines(a, n, row, where, head) if check_fix else []
    base_ref = _m_common.git("rev-parse", "--verify", base + "^{commit}",
                             cwd=n["home_path"] if n["external"] else inst.root)
    if base_ref.returncode:
        _m_common.die("cannot resolve review base")
    mechanical = _m_review.review_mechanical(inst, n, a, row, where, base_ref.stdout.strip(), head)
    out = [f"Reviewer runtime: {reviewer}  (the change was built on {built}) · class: {kind}",
           f"Run in: {where}" + (f"  ·  model: {a.model}" if a.model else ""), "",
           a.selection_line, "", _m_guard.redact(usage, _m_guard.never_terms(n)), "",
           "Role: worker, reviewing a change you didn't write. Don't modify any file, commit or push.",
           "The rules, agents and skills where you run come first (axiom 7): if this project has its own "
           "review checklist or reviewer agents, apply them too.", ""]
    if fresh or exception:
        out += ["Review identity receipt required: after completion, record the actual builder and reviewer "
                "thread IDs, runtimes and models using `brain review " + n['id'] + " --branch " + a.branch +
                " --receipt-file <host-observation.json>`. Unknown, self or reused context cannot establish independence.", ""]
    if exception:
        out += ["Same-runtime exception expires: " + exception['until'],
                "Exception reason: " + json.dumps(exception['reason']), ""]
    if n["sealed"]:
        out += ["This project has its own owners. Write nothing into it, and keep your report free of any "
                "reference to where this brief came from (axiom 6).", ""]
    out += _m_briefs.separation_lines(a, n, a.branch, default="Independent fresh non-builder context." if fresh or exception else "Independent second-runtime review.")
    review_kind = "check-fix" if check_fix else "full"
    round_line = (f"Review round {round_number} ({review_kind})." if row else
                  f"Review round not recorded ({review_kind}): no open job; round cap not applied.")
    out += [round_line, ""] + mechanical
    inputs = [("Focus", a.focus or "")] if not check_fix else []
    if check_fix:
        prior = json.loads(_m_review.review_read(n, a.findings))
        inputs += [("Prior review context", finding["text"]) for finding in prior
                   if finding["severity"] in ("blocker", "major")]
        out += fix_lines + _m_review.rehearsal_lines()
        out += ["## Report", "", "For each prior blocker/major: fixed or unresolved, with evidence. "
                "Report regressions caused by the fix and exactly which checks you ran.", ""] + \
            _m_knowledge.handback_lines(inst, n, who="reviewer", branch=a.branch)
    else:
        out += ["## Change", "", f"Branch `{a.branch}` against `{base}`. See it with `git diff {base}...{a.branch}`"
                f" and `git log {base}..{a.branch}`.", ""]
        if diffstat:
            out += ["```", diffstat, "```", ""]
        if a.focus:
            out += ["## Focus", "", a.focus.strip(), ""]
        if round_number and round_number > 1:
            out += ["## Earlier rounds", "", f"This is review round {round_number} of this job. "
                    "Exception reason: " + (getattr(a, "reason", None) or "none") + ". "
                    "Check earlier fixes and the same kind of problem everywhere in the change.", ""]
        if getattr(a, "contract", None):
            contract_text = _m_review.review_read(n, a.contract)
            inputs.append(("Contract", contract_text))
            out += ["## The job's contract", "", contract_text, ""]
        else:
            inputs += _m_briefs.contract_inputs(n, a.branch)
            out += _m_jobs.contract_lines(inst, n, a.branch, who="reviewer")
        inputs.append(("Limits", "\n".join(_m_knowledge.limits_of(n))))
        out += _m_knowledge.knowledge_lines(inst, n, " ".join(filter(None, [a.branch, a.focus, diffstat])), "reviewer") + \
            _m_knowledge.node_knowledge_lines(n) + _m_knowledge.recall_hint(n)
        out += _m_knowledge.limits_lines(n)
        out += _m_review.rehearsal_lines()
        out += ["## What to check", "",
                "- Correctness: does it do what the commits and the task say, including edge cases?",
                "- Breakage: run the project's own tests, type checks and linters that cover the change; report results.",
                "- Security and data: secrets, auth, user input, destructive operations.",
                "- The project's rules (its AGENTS.md and conventions).",
                "- The builder finishes with one `brain handback` command carrying readiness, state and proposed lessons. "
                "Inspect that worker-owned record when present; only the builder writes or refreshes it after review.", "",
                "## Report", "",
                "A list of findings, most severe first. Each: severity (blocker, major, minor), `file:line`, "
                "the problem, and a concrete fix. Say which tests you ran and their results. If you find nothing "
                "that matters, say so plainly.", ""] + _m_knowledge.handback_lines(inst, n, who="reviewer", branch=a.branch)
    text = _m_briefs.checked_brief(n, "\n".join(out) + "\n", worker=False, inputs=inputs)
    _m_guard.no_terms_in(n, text, "the review brief")
    if a.out:
        _m_history.write_out(inst, n, a.out, text)
    _m_jobs.job_reviewed(inst, n, a.branch, full=not check_fix, head=head,
                         reason=getattr(a, "reason", None) or "", identity_required=fresh or bool(exception), exception=exception)
    _m_jobs.record_job_route(inst, n, a.branch, kind, reviewer, a.model, review=True, effort=a.effort)
    if not a.out:
        print(text, end="")


# ---------- sync ----------

def last_sync(inst, n):
    t = n["local"].get("synced_at")
    if t and inst.parse_time(t):
        return inst.parse_time(t), "last sync"
    t = _m_status.updated_at(inst, n)  # status_line skips the generated facts block, including on empty files
    if t:
        return t, "status.md Updated"
    return inst.now_dt() - datetime.timedelta(days=_m_common.STALE_DAYS), f"last {_m_common.STALE_DAYS} days"


def cmd_sync(a):
    if a.all and getattr(a, "reset_main", False):
        _m_common.die("--reset-main requires one node, not --all")
    inst = _m_nodes.load()
    if a.all:
        if a.id:
            _m_common.die("give a node or --all, not both")
        ids = [n["id"] for n in inst.nodes() if n["external"] and not n["archived"]]
        for i, nid in enumerate(ids):
            if i:
                print("\n" + "-" * 60 + "\n")
            if not (inst.node(nid)["home_path"] and inst.node(nid)["home_path"].exists()):
                print(f"# {nid}: home not on this machine (brain set {nid} home=<path>)")
                continue
            sync_one(inst, inst.node(nid), a)
        if not ids:
            print("no node has an external home")
        return
    if not a.id:
        _m_common.die("which node? (or --all)")
    sync_one(inst, inst.node(a.id), a)


def packet_endpoints(n, row, packet):
    if not isinstance(packet, dict):
        return None
    head, base = packet.get("head"), packet.get("integration_base", packet.get("main"))
    if (packet.get("node") != n["id"] or packet.get("branch") != row["Job"] or
            type(packet.get("verify_result")) is not int or packet.get("verify_result") != 0 or
            type(packet.get("review_rounds")) is not int or packet["review_rounds"] < 1):
        return None
    endpoints = contribution_endpoints(base, head)
    origin = packet.get("main")
    if not endpoints or not isinstance(origin, str) or not re.fullmatch(r"[0-9a-f]{40,64}", origin):
        return None
    if n["home_path"] and n["home_path"].exists() and _m_common.git(
            "merge-base", "--is-ancestor", origin, base, cwd=n["home_path"]).returncode:
        return None
    return endpoints


def contribution_endpoints(base, head):
    if (all(isinstance(h, str) and re.fullmatch(r"[0-9a-f]{40,64}", h) for h in (base, head))
            and base != head):
        return base, head
    return None


def integration_packet(inst, n, row):
    signal = _m_jobs.read_handback(n, row)
    if signal and signal.get("state") != "done":
        return None
    return _m_verify.read_ready_packet(inst, n, row["Job"], row)


def retain_integration(inst, n, row):
    """Publish verified identities in the coordinator's row before deleting worker evidence."""
    if _m_briefs.import_job(row["Job"]):  # study handbacks carry no readiness evidence
        return False
    if n["external"] and not (n["home_path"] or n["local"].get("workbench")):
        hb = _m_jobs.read_handback(n, row) or {}
        endpoints = packet_endpoints(n, row, hb.get("ready") if hb.get("state") == "done" else None)
    else:
        endpoints = packet_endpoints(n, row, integration_packet(inst, n, row))
    if endpoints:
        base, head = endpoints
        if (row.get("Base commit"), row.get("Head commit")) != (base, head):
            row.update({"Base commit": base, "Head commit": head})
            return True
    return False


def integration_owners(inst, n, rows):
    """Index contributions once; ledger endpoints survive worker evidence cleanup."""
    heads, squashes = {}, {}
    for row in rows:
        if row.get("Outcome") == "external" or row.get("Merge commit"):
            continue
        endpoints = packet_endpoints(n, row, integration_packet(inst, n, row))
        if not endpoints:
            endpoints = contribution_endpoints(row.get("Base commit"), row.get("Head commit"))
        if not endpoints:
            continue
        base, head = endpoints
        if _m_common.git("merge-base", "--is-ancestor", base, head, cwd=n["home_path"]).returncode:
            continue
        tree = _m_common.git("rev-parse", "--verify", "--quiet", head + "^{tree}", cwd=n["home_path"]).stdout.strip()
        base_tree = _m_common.git("rev-parse", "--verify", "--quiet", base + "^{tree}", cwd=n["home_path"]).stdout.strip()
        if tree and base_tree and tree != base_tree:
            heads.setdefault(head, []).append((row, base, head))
            squashes.setdefault((base, tree), []).append((row, base, head))
    return heads, squashes


def sync_external_merges(inst, n, since, reset=False):
    """Record observed default-branch integrations, without claiming they were reviewed jobs.

    First-parent single commits include squash merges and direct commits: Git alone cannot
    distinguish them. Full commit identities, not timestamps or subjects, deduplicate rows.
    """
    home = n["home_path"]
    ref = _m_verify.integration_ref(home)
    if not ref:
        print("external merges: not recorded; cached origin default is unavailable; run git remote set-head origin -a")
        return False
    if not ref.startswith("refs/remotes/origin/"):
        return True
    tip = _m_common.git("rev-parse", "--verify", "--quiet", ref + "^{commit}", cwd=home)
    if tip.returncode:
        print("external merges: not recorded; cached origin default is unavailable")
        return False
    tip = tip.stdout.strip()
    cursor = n["local"].get("synced_main", "")
    args = ["log", "--first-parent", "--reverse", "--format=%H %T %P%x09%cI", "--min-parents=1"]
    if cursor:
        if reset:
            common = _m_common.git("merge-base", cursor, tip, cwd=home)
            cursor = common.stdout.strip() if common.returncode == 0 else ""
        elif _m_common.git("merge-base", "--is-ancestor", cursor, tip, cwd=home).returncode:
            print("external merges: default history changed; inspect it, then explicitly use sync --reset-main to rescan")
            return False
    if cursor:
        args += [cursor + ".." + tip]
    else:
        args += ["--since=" + since.isoformat(timespec="seconds"), tip]
    result = _m_common.git(*args, cwd=home)
    if result.returncode:
        print("external merges: history unreadable; not recorded")
        return False
    with _m_instance.brain_lock(inst):
        rows = _m_jobs.ledger_rows(n)
        if rows is None:
            return False
        known = {r.get("Merge commit") for r in rows}
        # Retain whole owned ranges after the handback/worktree/branch has been cleaned up.
        for row in rows:
            base, head = row.get("Base commit", ""), row.get("Head commit", "")
            if row.get("Outcome") != "external" and contribution_endpoints(base, head):
                known.update(_m_common.git("rev-list", base + ".." + head, cwd=home).stdout.split())
        heads, squashes = integration_owners(inst, n, rows)
        records = []
        for line in result.stdout.splitlines():
            identity, when = line.split("\t", 1)
            commit, tree, *parents = identity.split()
            records.append((commit, tree, parents, when))
        claims, claimed = {}, set()
        for commit, tree, parents, when in records:
            if commit in known:
                continue
            candidates = list(heads.get(commit, []))
            for parent in parents[1:]:
                candidates += heads.get(parent, [])
            if len(parents) == 1:
                candidates += squashes.get((parents[0], tree), [])
            owners = {_m_jobs.job_key(r): (r, base, head) for r, base, head in candidates
                      if not r.get("Merge commit") and _m_jobs.job_key(r) not in claimed}
            if len(owners) == 1:
                key, claim = next(iter(owners.items()))
                claims[commit] = claim
                claimed.add(key)
        for row, base, head in claims.values():
            known.update(_m_common.git("rev-list", base + ".." + head, cwd=home).stdout.split())
        added = 0
        changed = False
        for commit, tree, parents, when in records:
            if commit in claims:
                row, base, head = claims[commit]
                row.update({"Merge commit": commit, "Base commit": base, "Head commit": head})
                known.add(commit)
                changed = True
            elif commit not in known:
                rows.append({"Job": "external:" + commit, "Started": when, "Ended": when,
                             "Outcome": "external", "Commits": "1", "Merge commit": commit,
                             "Cause": "Observed on origin default; merge, squash or direct commit; review/usage unknown"})
                known.add(commit)
                added += 1
        if added or changed:
            _m_jobs.write_jobs(inst, n, rows)
            inst.commit([], f"{n['id']}: record external integrations")
        # Advance only after ledger publication; replay after a crash is harmless.
        inst.local_of(n["id"])["synced_main"] = tip
        inst.save_local()
    print(f"external merges: {added} recorded")
    return True


def sync_one(inst, n, a):
    since, basis = last_sync(inst, n)
    iso = since.isoformat(timespec="seconds")
    print(f"# {n['id']}: changes since {since.strftime('%Y-%m-%d %H:%M')} ({basis})")
    home = n["home_path"]
    can_mark = True
    if n["external"]:
        if not home or not home.exists():
            _m_common.die(f"home isn't on this machine: brain set {n['id']} home=<path>")
        print(f"\nhome: {_m_common.tilde(home)}")
        if (home / ".git").exists():
            has_remote = bool(_m_common.git("remote", cwd=home).stdout.strip())
            if has_remote and not a.offline:
                r = _m_common.git("fetch", "--all", "--prune", "--quiet", cwd=home)
                print("fetched" if r.returncode == 0 else f"fetch failed: {r.stderr.strip()[:200]}")
            _m_verify.main_notice(n)
            can_mark = sync_external_merges(inst, n, since, getattr(a, "reset_main", False))
            br = _m_common.git("branch", "--show-current", cwd=home).stdout.strip() or "(detached)"
            ab = _m_common.git("rev-list", "--left-right", "--count", "HEAD...@{u}", cwd=home)
            track = ""
            if ab.returncode == 0:
                ahead, behind = ab.stdout.split()
                track = f", {ahead} ahead / {behind} behind upstream"
            print(f"branch: {br}{track}")
            # branches, remotes and tags only: hosts and tools keep private refs of their own
            log = _m_common.git("log", "--branches", "--remotes", "--tags", f"--since={iso}",
                      "--date=format:%Y-%m-%d %H:%M", "--format=%h %ad %an: %s%d", "-n", "60",
                      cwd=home).stdout.strip()
            print("\n## Commits (branches, remotes, tags)\n" + (log or "none"))
            st = _m_common.git("status", "--porcelain", cwd=home).stdout.rstrip().splitlines()
            shown = st[:a.limit]
            more = f"\n... and {len(st) - len(shown)} more" if len(st) > len(shown) else ""
            print(f"\n## Uncommitted in this checkout ({len(st)})\n" + ("\n".join(shown) + more if st else "none"))
            url = _m_nodes.remote_url(home)
            if has_remote and not a.offline and "github.com" in url and shutil.which("gh"):
                try:
                    r = subprocess.run(["gh", "pr", "list", "--state", "open", "-L", "30", "--json",
                                        "number,title,author,headRefName,updatedAt,isDraft,reviewDecision"],
                                       cwd=home, capture_output=True, text=True, timeout=30)
                    prs = json.loads(r.stdout) if r.returncode == 0 else None
                except (subprocess.TimeoutExpired, ValueError):
                    prs = None
                print("\n## Open pull requests")
                if prs is None:
                    print("(couldn't list them)")
                elif not prs:
                    print("none")
                for pr in prs or []:
                    tag = " draft" if pr.get("isDraft") else ""
                    rv = f" {pr['reviewDecision'].lower()}" if pr.get("reviewDecision") else ""
                    print(f"#{pr['number']} {pr['title']} ({pr['author']['login']}, {pr['headRefName']},"
                          f" updated {pr['updatedAt'][:16].replace('T', ' ')}{tag}{rv})")
        else:
            changed = [str(p.relative_to(home)) for p in home.rglob("*")
                       if p.is_file() and datetime.datetime.fromtimestamp(p.stat().st_mtime).astimezone() > since]
            print("\n## Files changed\n" + ("\n".join(sorted(changed)[:100]) or "none"))
    else:
        rel = n["dir"].relative_to(inst.root)
        log = _m_common.git("log", f"--since={iso}", "--date=format:%Y-%m-%d %H:%M", "--format=%h %ad %s",
                  "--", str(rel) if str(rel) != "." else ".", cwd=inst.root).stdout.strip()
        print("\n## Commits in this node\n" + (log or "none"))
    inbox = [l for l in (n["dir"] / "inbox.md").read_text().splitlines() if l.startswith("- ")] \
        if (n["dir"] / "inbox.md").exists() else []
    print("\n## Inbox\n" + ("\n".join(inbox) or "empty"))
    if a.mark and not can_mark:
        print("\nnot marked: external integrations could not be recorded; repair and sync again")
    elif a.mark:
        inst.local_of(n["id"])["synced_at"] = inst.now_dt().isoformat(timespec="seconds")
        inst.save_local()
        print(f"\nmarked synced at {inst.now()}")
    else:
        print(f"\n(fold this into status.md, then: brain sync {n['id']} --mark)")


# ---------- workbench, worktrees, reports ----------
