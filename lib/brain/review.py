"""Bounded review rounds and privacy-safe mechanical evidence for review briefs."""
import json
import re
import subprocess
from pathlib import Path

from . import common as _m_common
from . import guard as _m_guard
from . import jobs as _m_jobs
from . import nodes as _m_nodes
from . import ui as _m_ui


def review_policy(a, row):
    rounds = int(row.get("Reviews") or 0) if row else 0
    full = int(row.get("Full reviews", row.get("Reviews")) or 0) if row else 0
    check = getattr(a, "check_fix", False)
    reason = getattr(a, "reason", None)
    if (rounds >= 2 or (full >= 1 and not check)) and not reason:
        _m_common.die(f"review round {rounds + 1} refused: a second full round or round 3+ requires "
                      "--reason security|schema|privacy; use --check-fix for the blocker-only fix check")
    if check and not rounds:
        _m_common.die("--check-fix needs a prior review round")
    if not check and (getattr(a, "findings", None) or getattr(a, "fix_base", None)):
        _m_common.die("--findings and --fix-base require --check-fix")
    return rounds + 1


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
    base = getattr(a, "fix_base", None) or row.get("Review head")
    ref = _m_common.git("rev-parse", "--verify", str(base or "") + "^{commit}", cwd=where)
    if ref.returncode or _m_common.git("merge-base", "--is-ancestor", ref.stdout.strip(), head, cwd=where).returncode:
        _m_common.die("--check-fix needs an ancestor fix base (previous review head or --fix-base)")
    blockers = [f for f in findings if f["severity"] in ("blocker", "major")]
    # JSON string encoding prevents findings from introducing new Markdown sections/instructions.
    return ["## Fix check", "", f"Fix range: `{ref.stdout.strip()}..{head}`.",
            "Check only: is each prior blocker/major fixed, and did the fix break anything?",
            "Do not reopen the full review or report unrelated minors.", "", "Prior blockers/majors:",
            *[f"- {f['severity']}: {json.dumps(f['text'], ensure_ascii=False)}" for f in blockers],
            *([] if blockers else ["- None recorded; check fix regressions only."]), ""]


def contract_audit(n, path, where):
    if not path or not Path(path).is_file():
        return ["Contract audit: GAP — no job contract supplied."]
    text = review_read(n, path)
    sections = {k.lower(): v for k, v in _m_ui.md_sections(text).items()}
    done = sections.get("done means", "").strip()
    bullets = re.split(r"(?m)^\s*[-*+]\s+", done)
    bullets = [b.strip() for b in bullets if b.strip()]
    if not bullets:
        return ["Contract audit: GAP — no Done means bullets."]
    out = [f"Contract audit: {len(bullets)} Done means item(s); test annotations are references, not execution proof."]
    for i, bullet in enumerate(bullets, 1):
        tests = re.findall(r"\btest:\s*`?([^\s`,;)]+)", bullet, re.I)
        valid = []
        for test in tests:
            file, sep, name = test.partition("::")
            candidate = (where / file).resolve()
            if not candidate.is_relative_to(where.resolve()) or not candidate.is_file():
                continue
            source = review_read(n, candidate)
            if sep and re.search(r"\bdef\s+" + re.escape(name.split("::")[-1]) + r"\s*\(", source):
                valid.append(test)
        if valid:
            out.append(f"- Done item {i}: named test " + ", ".join(valid))
        elif re.search(r"\bgap:\s*\S", bullet, re.I):
            out.append(f"- Done item {i}: stated GAP (untested).")
        else:
            out.append(f"- Done item {i}: GAP — no existing named test; add test: path::test_name or gap: explanation.")
    return out


def review_mechanical(inst, n, a, row, where, base):
    path = getattr(a, "contract", None) or (_m_jobs.contract_file(n, row) if row else None)
    audit = contract_audit(n, path, where)
    # Reuse the guard's complete scan, not only the diff; never include offending text in a brief.
    hits, errors = _m_guard.scan_terms(n, [where])
    if hits or errors:
        _m_common.die("review seed lint failed or incomplete: run brain guard; no review brief emitted")
    diff = _m_common.git("diff", "--name-only", "-z", f"{base}...{a.branch}", cwd=where)
    if diff.returncode:
        _m_common.die("cannot enumerate review seed files")
    seeds, unsafe = 0, 0
    principals = inst.principals
    for name in filter(None, diff.stdout.split("\0")):
        rel = Path(name)
        if not any(p in ("tests", "fixtures", "seeds", "test") for p in rel.parts):
            continue
        file = where / rel
        if not file.is_file():
            continue
        seeds += 1
        text = review_read(n, file)
        names = any(re.search(r"(?<!\w)" + re.escape(p) + r"(?!\w)", text, re.I)
                    for p in principals if len(p) >= 3)
        paths = bool(re.search(r"/(?:Users|home)/[^\s/'\"<>]+|[A-Z]:\\Users\\", text))
        unsafe += bool(names or paths or _m_nodes.import_text(text, _m_guard.never_terms(n)) != text.rstrip("\n"))
    lint = f"Seed-text lint: {'FLAG' if unsafe else 'clean'} — {seeds} changed test/fixture file(s), {unsafe} flagged. " \
           "Checks configured principal names, user-home paths, scrub patterns and guard terms; other real names need reviewer judgement."
    command = n["local"].get("fitness") or n["fm"].get("fitness")
    fitness = "Fitness gate: not configured (fitness=<command>)."
    if command:
        try:
            result = subprocess.run(command, shell=True, cwd=where, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=120)
            fitness = f"Fitness gate: {'PASS' if result.returncode == 0 else 'FAIL'} (exit {result.returncode})."
        except (OSError, subprocess.TimeoutExpired):
            fitness = "Fitness gate: FAIL (could not finish within 120 seconds)."
    if getattr(a, "check_fix", False):
        # A fix check gets the results, never a restatement of the full contract.
        audit = [f"Contract audit: {sum('GAP' in line for line in audit)} gap line(s); inspect the prior full brief for scope."]
    return ["## Mechanical checks", "", *audit, fitness, lint, ""]


def rehearsal_lines():
    return ["## Rehearsal", "", "Run the change against real-shaped data: use `brain rehearse` when available. "
            "Otherwise copy a brain (never use the original), and run installed versus candidate core with "
            "`BRAIN_ROOT=<copy> BRAIN_NO_REEXEC=1 BRAIN_NO_UPDATE_CHECK=1 python3 <worktree>/bin/brain due`, "
            "`check`, and `ready <node> --branch <job>` for open jobs. Report exactly what ran, new errors, "
            "ready flips and due noise. Keep private data out of the report.", ""]
