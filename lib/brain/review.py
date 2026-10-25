"""Bounded review rounds and privacy-safe mechanical evidence for review briefs."""
import json
import os
import signal
import re
import subprocess
from pathlib import Path

from . import common as _m_common
from . import guard as _m_guard
from . import jobs as _m_jobs
from . import nodes as _m_nodes
from . import ui as _m_ui


def review_count(value):
    if value in (None, ""):
        return 0
    if not re.fullmatch(r"[0-9]+", str(value)):
        _m_common.die("invalid review count in job ledger; repair the count before reviewing")
    return int(value)


def review_policy(a, row):
    rounds = review_count(row.get("Reviews")) if row else 0
    full = review_count(row.get("Full reviews") or row.get("Reviews")) if row else 0
    check = getattr(a, "check_fix", False)
    reason = getattr(a, "reason", None)
    if row and (rounds >= 2 or (full >= 1 and not check)) and not reason:
        _m_common.die(f"review round {rounds + 1} refused: a second full round or round 3+ requires "
                      "--reason security|schema|privacy; use --check-fix for the blocker-only fix check")
    if check and not rounds and not getattr(a, "fix_base", None):
        _m_common.die("--check-fix needs a prior review round or an explicit --fix-base")
    if not check and (getattr(a, "findings", None) or getattr(a, "fix_base", None)):
        _m_common.die("--findings and --fix-base require --check-fix")
    return rounds + 1 if row else None


def review_read(n, path):
    if Path(path).name == _m_guard.NEVER_FILE:
        _m_common.die("review never reads the filter vocabulary as evidence")
    try:
        text = Path(path).read_text()
    except (OSError, UnicodeError):
        _m_common.die("cannot read review evidence; no content emitted")
    _m_guard.no_terms_in(n, text, "review evidence")
    return text


def fix_check_lines(a, n, row, where, head):
    if not getattr(a, "findings", None):
        _m_common.die("--check-fix requires --findings <prior-findings.json> (severity and text per finding)")
    try:
        findings = json.loads(review_read(n, a.findings))
    except ValueError:
        _m_common.die("prior findings must be a JSON list of objects with severity and text")
    if not isinstance(findings, list) or any(not isinstance(f, dict) or
            f.get("severity") not in ("blocker", "major", "minor") or not isinstance(f.get("text"), str)
            or not f["text"].strip() for f in findings):
        _m_common.die("prior findings must be a JSON list of objects with severity and text")
    base = getattr(a, "fix_base", None) or (row or {}).get("Review head")
    ref = _m_common.git("rev-parse", "--verify", str(base or "") + "^{commit}", cwd=where)
    if ref.returncode or _m_common.git("merge-base", "--is-ancestor", ref.stdout.strip(), head, cwd=where).returncode:
        _m_common.die("--check-fix needs an ancestor fix base (previous review head or --fix-base)")
    if ref.stdout.strip() == head:
        _m_common.die("--check-fix refuses an empty fix range; commit the fix first")
    blockers = [f for f in findings if f["severity"] in ("blocker", "major")]
    # JSON string encoding prevents findings from introducing new Markdown sections/instructions.
    return ["## Fix check", "", f"Fix range: `{ref.stdout.strip()}..{head}`.",
            "Check only: is each prior blocker/major fixed, and did the fix break anything?",
            "Do not reopen the full review or report unrelated minors.", "", "Prior blockers/majors:",
            *[f"- {f['severity']}: {json.dumps(f['text'], ensure_ascii=False)}" for f in blockers],
            *([] if blockers else ["- None recorded; check fix regressions only."]), ""]


def review_blob(n, repo, head, path):
    """Read repository-root-relative evidence from the immutable candidate, never the checkout."""
    rel = Path(path)
    if rel.is_absolute() or ".." in rel.parts or rel.name == _m_guard.NEVER_FILE:
        return None
    result = _m_common.git("show", f"{head}:{rel.as_posix()}", cwd=repo)
    if result.returncode:
        return None
    _m_guard.no_terms_in(n, result.stdout, "committed review evidence")
    return result.stdout


def contract_audit(n, path, repo, head):
    if not path or not Path(path).is_file():
        return ["Contract audit: GAP — no job contract supplied."]
    text = review_read(n, path)
    sections = {k.lower(): v for k, v in _m_ui.md_sections(text).items()}
    done = sections.get("done means", "").strip()
    bullets = re.split(r"(?m)^[ \t]*(?:[-*+]|[0-9]+[.)])[ \t]+", done)
    bullets = [b.strip() for b in bullets if b.strip()]
    if not bullets:
        return ["Contract audit: GAP — no Done means bullets."]
    out = [f"Contract audit: {len(bullets)} Done means item(s); test annotations are references, not execution proof."]
    for i, bullet in enumerate(bullets, 1):
        tests = re.findall(r"\btest:\s*`?([^\s`,;)]+)", bullet, re.I)
        valid = []
        for test in tests:
            file, sep, name = test.partition("::")
            source = review_blob(n, repo, head, file)
            if source is not None and sep and re.search(r"\bdef\s+" + re.escape(name.split("::")[-1]) + r"\s*\(", source):
                valid.append(test)
        if valid:
            out.append(f"- Done item {i}: named test " + ", ".join(valid))
        elif re.search(r"\bgap:\s*\S", bullet, re.I):
            out.append(f"- Done item {i}: stated GAP (untested).")
        else:
            out.append(f"- Done item {i}: GAP — no existing named test; add test: path::test_name or gap: explanation.")
    return out


FITNESS_TIMEOUT = 30


def review_fitness(command, where):
    try:
        proc = subprocess.Popen(command, shell=True, cwd=where, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return "Fitness gate: FAIL (could not start)."
    try:
        code = proc.wait(timeout=FITNESS_TIMEOUT)
        return f"Fitness gate: {'PASS' if code == 0 else 'FAIL'} (exit {code})."
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        return f"Fitness gate: FAIL (could not finish within {FITNESS_TIMEOUT} seconds; process group stopped)."


def review_mechanical(inst, n, a, row, where, base, head):
    path = getattr(a, "contract", None) or (_m_jobs.contract_file(n, row) if row else None)
    repo = n["home_path"] if n["external"] else inst.root
    audit = contract_audit(n, path, repo, head)
    diff = _m_common.git("diff", "--name-only", "--diff-filter=ACMRT", "-z", f"{base}...{head}", cwd=repo)
    if diff.returncode:
        _m_common.die("cannot enumerate review seed files")
    seeds, unsafe = 0, 0
    principals = inst.principals
    for name in filter(None, diff.stdout.split("\0")):
        rel = Path(name)
        if not any(p in ("tests", "fixtures", "seeds", "test") for p in rel.parts):
            continue
        text = review_blob(n, repo, head, name)
        if text is None:
            _m_common.die("cannot read committed review seed; no brief emitted")
        seeds += 1
        names = any(re.search(r"(?<!\w)" + re.escape(p) + r"(?!\w)", text, re.I)
                    for p in principals if len(p) >= 3)
        paths = bool(re.search(r"/(?:Users|home)/[^\s/'\"<>]+|[A-Z]:\\Users\\", text))
        expected = text[:-1] if text.endswith("\n") else text
        unsafe += bool(names or paths or _m_nodes.import_text(text, _m_guard.never_terms(n)) != expected)
    lint = f"Seed-text lint: {'FLAG' if unsafe else 'clean'} — {seeds} changed test/fixture file(s), {unsafe} flagged. " \
           "Checks configured principal names, user-home paths, scrub patterns and guard terms; other real names need reviewer judgement."
    command = n["local"].get("fitness") or n["fm"].get("fitness")
    fitness = "Fitness gate: not configured (fitness=<command>)."
    if command:
        worktree = n["local"].get("worktrees", {}).get(a.branch)
        current = _m_common.git("rev-parse", "HEAD", cwd=where) if worktree else None
        dirty = _m_common.git("status", "--porcelain", cwd=where) if worktree else None
        if not worktree:
            fitness = "Fitness gate: skipped: no worktree for the reviewed branch."
        elif current.returncode or current.stdout.strip() != head or dirty.returncode or dirty.stdout.strip():
            fitness = "Fitness gate: skipped: worktree differs from the reviewed head."
        else:
            fitness = review_fitness(command, where)
    if getattr(a, "check_fix", False):
        # A fix check gets the results, never a restatement of the full contract.
        audit = [f"Contract audit: {sum('GAP' in line for line in audit)} gap line(s); inspect the prior full brief for scope."]
    return ["## Mechanical checks", "", *audit, fitness, lint, ""]


def rehearsal_lines():
    return ["## Rehearsal", "", "Rehearsal is an optional diagnostic, never a readiness or landing gate. "
            "Do not start one automatically. When explicitly requested, run `brain rehearse` on the "
            "committed candidate with machine admission and its sandbox protections. Inspect the report "
            "and preserve raw failures; report exactly what ran or state that it was not run. "
            "Keep private data out of the report.", ""]


# Review identities are observed host data supplied by a coordinator, not a live host query.
def independence_policy(inst, n):
    value = n['fm'].get('review_independence', inst.cfg.get('review_independence', 'cross-runtime'))
    if value not in ('cross-runtime', 'fresh-thread'):
        raise ValueError('review_independence must be cross-runtime or fresh-thread')
    return value


def runtime_exception(inst, n, a, built):
    """Retain explicit expiring legacy exceptions without weakening thread independence."""
    if a.by != built or independence_policy(inst, n) == 'fresh-thread':
        return None
    reason = (getattr(a, 'same_runtime_reason', None) or '').strip()
    until = n['fm'].get('same_runtime_review_until', inst.cfg.get('same_runtime_review_until', ''))
    expiry = inst.parse_time(until) if re.search(r'(?:Z|[+-]\d{2}:?\d{2})$', until) else None
    if not reason or not expiry:
        _m_common.die('same-runtime review needs fresh-thread policy or an expiring exception and --same-runtime-reason')
    if expiry <= inst.now_dt():
        _m_common.die('same-runtime review exception expired')
    return {'runtime': built, 'reason': reason, 'until': until}


def review_receipt_path(n, row, head):
    import hashlib
    if not re.fullmatch(r'[0-9a-f]{40,64}', head or ''):
        raise ValueError('invalid review head')
    root = n
    while root.get('parent'):
        root = root['parent']
    return root['dir'] / '.brain/review-receipts' / hashlib.sha256(n['id'].encode()).hexdigest() / _m_jobs.job_key(row) / (head + '.json')


def validate_review_receipt(inst, n, row, head, data, fresh=False):
    def refuse():
        raise ValueError('review receipt needs exact code, known builder and distinct fresh non-builder thread identities')
    if (not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] != 1 or
            data.get('node') != n['id'] or data.get('job_key') != _m_jobs.job_key(row) or
            data.get('branch') != row['Job'] or data.get('head') != head):
        refuse()
    observed = inst.parse_time(data.get('observed_at')) if isinstance(data.get('observed_at'), str) else None
    started = inst.parse_time(row.get('Started'))
    if not observed or observed > inst.now_dt() or (started and observed < started):
        refuse()
    if fresh and (inst.now_dt() - observed).total_seconds() > 300:
        refuse()
    builder, reviewer = data.get('builder'), data.get('reviewer')
    for identity in (builder, reviewer):
        if not isinstance(identity, dict) or any(not isinstance(identity.get(k), str) or not identity[k].strip()
                or any(c.isspace() for c in identity[k]) for k in ('threadId', 'runtime', 'model')):
            refuse()
        if identity['runtime'] not in inst.runtimes:
            refuse()
    known = set()
    for ref, _ in _m_jobs.bound_worker_threads(n, row['Job']):
        known.update(_m_jobs.thread_reference_ids(ref))
    if (builder['threadId'] not in known or reviewer['threadId'] in known or
            reviewer['threadId'] == builder['threadId'] or builder['runtime'] != row.get('Runtime') or
            builder['model'] != row.get('Model') or reviewer.get('fresh_context') is not True or
            reviewer.get('context_reused') is not False or reviewer.get('built_change') is not False):
        refuse()
    if builder['runtime'] == reviewer['runtime'] and independence_policy(inst, n) != 'fresh-thread':
        try:
            history = json.loads(row.get('Review exceptions') or '[]')
            permitted = any(isinstance(entry, dict) and entry.get('head') == head and
                            entry.get('runtime') == reviewer['runtime'] and entry.get('reason') and
                            isinstance(entry.get('until'), str) and inst.parse_time(entry['until']) and
                            observed < inst.parse_time(entry['until']) for entry in history)
        except (ValueError, TypeError):
            permitted = False
        if not permitted:
            refuse()
    for key in ('runtime', 'model'):
        expected = row.get('Review ' + key) or n['fm'].get('review_' + key, inst.cfg.get('review_' + key))
        if expected and expected != reviewer[key]:
            refuse()
    if data.get('verdict') not in ('pass', 'blocked') or not isinstance(data.get('findings'), list):
        refuse()
    for finding in data['findings']:
        if (not isinstance(finding, dict) or finding.get('severity') not in ('blocker', 'major', 'minor') or
                any(not isinstance(finding.get(k), str) or not finding[k].strip() for k in ('id', 'class', 'text')) or
                finding.get('state') not in ('open', 'closed') or not isinstance(finding.get('paths'), list) or
                not finding['paths']):
            refuse()
    if data['verdict'] == 'pass' and any(f['state'] == 'open' and f['severity'] in ('blocker', 'major') for f in data['findings']):
        refuse()
    return data


def record_review_receipt(inst, n, branch, data):
    from . import instance as _m_instance
    with _m_instance.brain_lock(inst):
        row = _m_jobs.open_job(_m_jobs.read_jobs(n) or [], branch)
        if not row:
            raise ValueError('review receipt needs an open job')
        repo = n.get('home_path') if n.get('external') else inst.root
        head = _m_common.git('rev-parse', '--verify', 'refs/heads/' + branch + '^{commit}', cwd=repo).stdout.strip()
        validate_review_receipt(inst, n, row, head, data, fresh=True)
        serialized = json.dumps(data, sort_keys=True) + '\n'
        _m_guard.no_terms_in(n, serialized, 'review identity receipt')
        path = review_receipt_path(n, row, head)
        _m_common.safe_target(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        import hashlib
        history = path.parent / (head + '-' + hashlib.sha256(data['reviewer']['threadId'].encode()).hexdigest() + '.json')
        _m_common.safe_target(history)
        if history.exists() and history.read_text() != serialized:
            raise ValueError('reviewer thread already supplied a different receipt; use a fresh review context')
        from . import review_findings as _m_review_findings
        _m_review_findings.record_review_findings(inst, n, branch, head, data['findings'])
        _m_common.atomic_write(history, serialized)
        _m_common.atomic_write(path, serialized)
        rows = _m_jobs.read_jobs(n)
        current = _m_jobs.open_job(rows or [], branch)
        current['Review identity required'] = 'true'
        current['Review identity receipt'] = str(path.relative_to(inst.root))
        _m_jobs.write_jobs(inst, n, rows)
        inst.commit([], f"{n['id']}: review identity receipt")
    return path


def validate_ready_reviews(inst, n, branch, head, packet=None):
    row = _m_jobs.open_job(_m_jobs.read_jobs(n) or [], branch)
    try:
        required = independence_policy(inst, n) == 'fresh-thread' or (row or {}).get('Review identity required') == 'true'
    except ValueError as error:
        return [str(error)]
    if not required:
        return []
    if not row:
        return ['independent review receipt requires an open job']
    try:
        path = review_receipt_path(n, row, head)
        data = json.loads(path.read_text())
        validate_review_receipt(inst, n, row, head, data)
        if data['verdict'] != 'pass':
            raise ValueError('review is blocked')
    except (OSError, ValueError, TypeError, KeyError):
        return ['missing, stale, blocked or non-independent exact-head review receipt']
    return []


def review_evidence(inst, n, branch=None):
    """Read-only receipts, including failed review classes, for advisory consumers."""
    out = []
    for row in _m_jobs.read_jobs(n) or []:
        if branch and row['Job'] != branch:
            continue
        folder = review_receipt_path(n, row, '0' * 40).parent
        for path in sorted(folder.glob('*-*.json')):
            try:
                data = json.loads(path.read_text())
                if not isinstance(data, dict):
                    continue
                validate_review_receipt(inst, n, row, data.get('head'), data)
                out.append(data)
            except (OSError, ValueError, TypeError, KeyError):
                continue
    return out
