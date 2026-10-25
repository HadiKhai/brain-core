"""Check mechanics for Brain."""
import os
import re
import sys
import time
from pathlib import Path

from . import common as _m_common
from . import guard as _m_guard
from . import hosts as _m_hosts
from . import instance as _m_instance
from . import jobs as _m_jobs
from . import knowledge as _m_knowledge
from . import nodes as _m_nodes
from . import schedules as _m_schedules
from . import status as _m_status


def decision_warnings(inst, n):
    """Validate decision metadata; a citation is not independent verification of a fact."""
    path = n['dir'] / 'decisions.md'
    if not path.is_file():
        return []
    cutoff_text = n['fm'].get('decision_scope_since', inst.cfg.get('decision_scope_since'))
    cutoff = inst.parse_time(cutoff_text) if cutoff_text else None
    if cutoff_text and not cutoff:
        return [f"{n['id']}: invalid decision_scope_since timestamp"]
    text = re.sub(r'(?ms)^```.*?^```[^\n]*', '', path.read_text())
    headings = list(re.finditer(r'^## (\d{4}-\d{2}-\d{2})[^\n]*', text, re.M))
    out = []
    for index, heading in enumerate(headings):
        date = inst.parse_time(heading.group(1) + 'T00:00:00+00:00')
        if cutoff and date and date.date() < cutoff.date():
            continue
        body = text[heading.end():headings[index + 1].start() if index + 1 < len(headings) else len(text)]
        label = f"{n['id']}: decisions.md:{text.count(chr(10), 0, heading.start()) + 1}"
        if not re.search(r'^Applies to:[ \t]*(\S[^\n]*)', body, re.M | re.I):
            out.append(label + ': decision has no Applies to: scope')
        external = (re.search(r'^(?:External|Vendor|Provider|Policy) fact:', body, re.M | re.I) or
                    re.search(r'\b(?:vendor|provider|upstream)\b[^.\n]{0,120}\b(?:requires?|allows?|limits?|supports?|expires?|retains?|policy)\b',
                              heading.group() + body, re.I))
        source = re.search(r'^Source:[ \t]*(\S[^\n]*)', body, re.M | re.I)
        valid = False
        if source:
            import urllib.parse
            value = source.group(1).strip()
            url = re.search(r'https?://[^\s<>)]+', value)
            try:
                valid = bool(url and urllib.parse.urlsplit(url.group()).netloc)
            except ValueError:
                valid = False
            valid = valid or bool(re.fullmatch(r'probe:[ \t]+\S.+', value, re.I))
        if external and not valid:
            out.append(label + ': external fact has no valid Source: URL or probe: command')
    return out


def collect_problems(inst):
    """Returns (problems, warnings, fixable) for the axioms that can be checked mechanically."""
    problems, warnings, fixable = [], [], []
    p = problems.append
    fallback = inst.local.get("fallback", [])
    if not isinstance(fallback, list) or any(not isinstance(x, str) or not x.strip() for x in fallback) \
            or (isinstance(fallback, list) and all(isinstance(x, str) for x in fallback)
                and len(fallback) != len(set(fallback))):
        p(".brain/local.json: fallback must be an ordered list of distinct host provider instance ids")
    if not inst.principals:
        p("instance.md: no principals")
    for link in _m_common.outward_links(inst.root):
        p(f"{link.relative_to(inst.root.resolve()) if inst.root.resolve() in link.parents else link}: a link leading "
          "out of the brain (symbolic, or a hard link to a file elsewhere), where brain writes: remove it (commands "
          "that change anything refuse to run until then)")
    for rt in inst.runtimes:
        if not (_m_common.CORE / "adapters" / "runtimes" / f"{rt}.env").exists():
            p(f"runtime '{rt}' has no adapter in core/adapters/runtimes")
    host = inst.cfg.get("host")
    if host and not (_m_common.CORE / "adapters" / "hosts" / f"{host}.md").exists():
        p(f"host '{host}' has no adapter in core/adapters/hosts")
    if ensure_gitignore_needed(inst.root):
        p(".gitignore doesn't ignore .brain/ or private verify logs/input maps (required exclusions missing)")
        fixable.append("gitignore")
    label_keys = _m_common.lst(inst.cfg.get("label_keys"))
    tdir = _m_common.CORE / "templates" / "node"
    template_files = [f.relative_to(tdir) for f in tdir.rglob("*") if f.is_file()]
    nodes = inst.nodes()
    for n in nodes:
        fm, nid = n["fm"], n["id"]
        from . import freshness as _m_freshness
        index = _m_freshness.freshness_state(inst, n)
        if index and index['state'] != 'fresh':
            p(f"{nid}: upstream index {index['state']}; regenerate it explicitly, then brain index {nid} --record")
        try:
            _m_status.parallel_job_limit(n)
        except ValueError as error:
            p(f'{nid}: {error}')
        if not n["archived"]:
            status_file = n["dir"] / "status.md"
            status_text = status_file.read_text() if status_file.exists() else ""
            if (status_text.count(_m_status.FACTS_START) != 1 or status_text.count(_m_status.FACTS_END) != 1
                    or not _m_status.FACTS_RE.search(status_text)):
                p(f"{nid}: status.md missing or malformed generated facts block; brain status {nid} --refresh")
                if _m_status.FACTS_START not in status_text and _m_status.FACTS_END not in status_text:
                    fixable.append("facts")
            judgement = _m_status.FACTS_RE.sub("", status_text)
            now_match = re.search(r"(?ims)^## Now\s*\n(.*?)(?=^## |\Z)", judgement)
            now_lines = len([l for l in re.sub(r"<!--.*?-->", "", now_match.group(1), flags=re.S).splitlines() if l.strip()]) if now_match else 0
            if now_lines > _m_status.NOW_LINE_LIMIT:
                warnings.append(f"{nid}: Now has {now_lines} nonblank lines (limit {_m_status.NOW_LINE_LIMIT}); move history to Recent changes or decisions")
            incomplete = [b for b in _m_status.blocked_entries(inst, n) if b["missing"]]
            if incomplete:
                warnings.append(f"{nid}: {len(incomplete)} blocked item(s) missing valid since:, recommendation: or default:; "
                                "add '; since: <brain now timestamp>; recommendation: <action>; default: <fallback>'")
            warnings.extend(_m_status.code_setup_warnings(inst, n))
            warnings.extend(_m_guard.redact(w, _m_guard.never_terms(n)) for w in _m_status.dead_note_paths(inst, n))
        warnings.extend(decision_warnings(inst, n))
        future, dated = _m_status.future_entries(inst, n)
        problems.extend(future)
        warnings.extend(dated)
        for r in _m_jobs.read_jobs(n) or []:
            if r["Outcome"] == "merged" and r.get("Lesson check") == "required" and not r.get("Lessons"):
                warnings.append(_m_guard.redact(f"{nid}: merged job {r['Job']} has no lesson outcome; "
                                       "brain jobs --lessons", _m_guard.never_terms(n)))
        for f in _m_common.NODE_FIELDS:
            if f not in fm:
                p(f"{nid}: node.md is missing '{f}'")
        for k in _m_common.LOCAL_KEYS:
            if k in fm:
                p(f"{nid}: '{k}' is machine-specific; it belongs in .brain/local.json (axiom 23)")
                fixable.append("local")
        if n["external"] and fm.get("home") != "external":
            p(f"{nid}: home path in node.md is machine-specific (axiom 23)")
            fixable.append("local")
        if fm.get("id") not in (nid, None):
            p(f"{nid}: node.md says id '{fm.get('id')}'")
        if not n["owners"]:
            p(f"{nid}: no owners")
        for o in n["owners"]:
            if o not in inst.principals:
                p(f"{nid}: unknown principal '{o}'")
            elif o in inst.revoked and not n["archived"]:
                p(f"{nid}: owned by revoked principal '{o}'; archive it or change owners")
        if fm.get("runtime") and fm["runtime"] not in inst.runtimes:
            p(f"{nid}: runtime '{fm['runtime']}' not in instance runtimes")
        for l in _m_common.lst(fm.get("labels")):
            if ":" not in l:
                p(f"{nid}: label '{l}' isn't key:value")
            elif label_keys and l.split(":", 1)[0] not in label_keys:
                warnings.append(f"{nid}: label key '{l.split(':', 1)[0]}' isn't in instance label_keys")
        for rt in inst.runtimes:
            ad = inst.adapter(rt)
            hl = ad.get("HOOKS_LINK")
            if hl and ad.get("HOOKS_SOURCE") and (ad.get("HOOKS_SCOPE") != "root" or nid == "root") \
                    and not (n["dir"] / hl).is_symlink():
                p(f"{nid}: {hl} (the {rt} stop hook) is missing")
                fixable.append("links")
            link = ad.get("INSTRUCTIONS_LINK")
            if link and (n["dir"] / "AGENTS.md").exists():
                l = n["dir"] / link
                if not (l.is_symlink() and os.readlink(l) == "AGENTS.md"):
                    p(f"{nid}: {link} must be a link to AGENTS.md (axiom 22)")
                    fixable.append("links")
        if n["id"] == "root" and not (n["dir"] / "backlog.md").exists():
            p("root: missing backlog.md")
            fixable.append("backlog")
        if n["id"] != "root":
            for rel in template_files:
                if rel.name != ".gitkeep" and not (n["dir"] / rel).exists():
                    p(f"{nid}: missing {rel}")
                    fixable.append("templates")
            ag = n["dir"] / "AGENTS.md"
            if ag.exists():
                has = (_m_common.CORE / "templates" / "sealed-view.md").read_text().strip() in ag.read_text()
                if has != n["sealed"]:
                    p(f"{nid}: AGENTS.md sealed-view section out of step with its boundary")
                    fixable.append("views")
        if n["sealed"] and not n["archived"]:
            hp = n["home_path"]
            if not n["external"]:
                p(f"{nid}: sealed but its home is in the instance; give it one with 'brain seal' (axiom 8)")
            elif hp is None:
                p(f"{nid}: home not set on this machine: brain set {nid} home=<path>")
            elif not hp.exists():
                p(f"{nid}: home {_m_common.tilde(hp)} not found on this machine")
            elif inst.root.resolve() in [hp.resolve(), *hp.resolve().parents]:
                p(f"{nid}: sealed home is inside the instance (axiom 8)")
            else:
                terrs = []
                for pat, f in _m_guard.scan_traces(inst, hp, terrs):
                    p(_m_guard.redact(f"{nid}: trace of '{pat}' in its home (axiom 6): {f}", _m_guard.never_terms(n)))
                for e in terrs:
                    p(_m_guard.redact(f"{nid}: couldn't check its home for traces: {e}", _m_guard.never_terms(n)))
        if not n["archived"]:
            found, errs = _m_guard.scan_terms(n)
            for label, where in found:
                p(f"{nid}: {where} contains {label} (see: brain guard {nid})")
            for e in errs:
                p(f"{nid}: couldn't check for never-name words: {e}")
            for record in _m_knowledge.limit_records(inst, n):
                if record['state'] == 'invalid-expiry':
                    warnings.append(f"{nid}: a limit has invalid Expires: metadata; it remains active")
            for lim in _m_knowledge.limits_of(n):
                missing = [k for k in ("Why:", "Lift when:") if k.lower() not in lim.lower()]
                if missing:
                    warnings.append(f"{nid}: a limit in brief.md has no {' or '.join(missing)} "
                                    f"(\"{lim.splitlines()[0][:60]}\")")
        wbl = n["local"].get("workbench")
        if wbl:
            wbp = Path(wbl).expanduser().resolve()
            for inside, what in ((inst.root.resolve(), "the instance"),
                                 (n["home_path"].resolve() if n["home_path"] else None, "its home")):
                if inside and inside in [wbp, *wbp.parents]:
                    p(f"{nid}: workbench is inside {what} (axioms 6 and 8)")
        for br, w in n["local"].get("worktrees", {}).items():
            if not Path(w["path"]).exists():
                warnings.append(f"{nid}: worktree for {br} is gone; brain wt prune {nid}")
    for it in inst.items():
        fm, name = it["fm"], it["path"].relative_to(inst.root)
        for f in _m_common.ITEM_FIELDS:
            if not fm.get(f):
                p(f"{name}: missing '{f}' (axiom 10)")
        for o in _m_common.lst(fm.get("scope")) + _m_common.lst(fm.get("source_owners")):
            if o != "*" and o not in inst.principals:
                p(f"{name}: unknown principal '{o}'")
        if fm.get("status") not in ("active", "quarantined", "proposed", "rejected"):
            p(f"{name}: status must be active, proposed, rejected or quarantined")
        if fm.get("status") in ("proposed", "rejected") or fm.get("status_before_quarantine") in ("proposed", "rejected"):
            if not fm.get("source_repo") or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", fm.get("source_commit", "")):
                p(f"{name}: proposed learning needs source_repo and a full source_commit")
        if fm.get("source_node") and not _m_instance.resolve_ref(nodes, fm["source_node"]):
            p(f"{name}: source_node '{fm['source_node']}' is no node, former id or merged node (axiom 19)")
        for ref in _m_common.lst(fm.get("source_nodes")):
            if not _m_instance.resolve_ref(nodes, ref):
                p(f"{name}: source_nodes names '{ref}', no node, former id or merged node (axiom 19)")
    for n in nodes:
        if n["archived"]:
            continue
        vague = [b for b in _m_status.status_section(n, "blocked")
                 if not _m_status.empty_blocked(b) and "clears when" not in b.lower()]
        if vague:
            warnings.append(f"{n['id']}: {len(vague)} Blocked "
                            + ("entry doesn't say what clears it" if len(vague) == 1 else "entries don't say what clears them")
                            + ": write '<what> — waiting on <who or what>; clears when <condition>'")
    for s in _m_knowledge.find_specialists(inst):
        if not s["fm"].get("scope"):
            p(f"{s['dir'].relative_to(inst.root)}/role.md: missing scope")
    lesson_folders = [n["dir"] for n in nodes] + [s["dir"] for s in _m_knowledge.find_specialists(inst)]
    for folder in lesson_folders:
        for l in _m_knowledge.parse_lessons(folder / "lessons.md"):
            if l.get("malformed"):
                warnings.append(f"{folder.relative_to(inst.root)}/lessons.md: malformed lesson tags; repair before reuse")
            if not _m_instance.resolve_ref(nodes, l["from"]):
                warnings.append(f"{folder.relative_to(inst.root)}/lessons.md: lesson from unknown node '{l['from']}'")
    schedules = _m_schedules.read_schedules(inst)
    for x in schedules:
        for f in _m_schedules.SCHEDULE_FIELDS:
            if not x.get(f):
                p(f"schedules.md: '{x['id']}' is missing '{f}'")
        target = _m_instance.resolve_ref(nodes, x.get("node"))
        if x.get("node") and not target:
            p(f"schedules.md: '{x['id']}' names unknown node '{x['node']}'")
        provider = _m_schedules.schedule_provider(inst, x)
        pin = target["fm"].get("runtime") if target else None
        if provider and pin and provider != pin:
            warnings.append(_m_guard.redact(
                f"schedule '{x['id']}' provider '{provider}' differs from node '{target['id']}' runtime pin '{pin}'",
                _m_guard.never_terms(target)))
    for sid in inst.local.get("schedules", {}):
        if not any(x["id"] == sid for x in schedules):
            warnings.append(f"this machine runs schedule '{sid}', which schedules.md no longer lists: remove it "
                            f"in the host, then brain schedule unbind {sid}")
    if registry_stale(inst):
        p("registry.md is out of date")
        fixable.append("registry")
    return problems, warnings, sorted(set(fixable))


def ensure_gitignore_needed(root):
    gi = root / ".gitignore"
    lines = gi.read_text().splitlines() if gi.exists() else []
    return any(p not in lines for p in (".brain/", "**/job-handbacks/*-verify-*.log", "**/job-handbacks/*-inputs-*.map"))


def registry_stale(inst):
    f = inst.root / "registry.md"
    if not f.exists():
        return True
    return f.read_text() != _m_status.registry_text(inst)


def apply_fixes(inst, kinds):
    done = []
    if "gitignore" in kinds and _m_nodes.ensure_gitignore(inst.root):
        done.append(".gitignore ignores .brain/ and private verify logs/input maps")
    if "local" in kinds:
        for n in inst.nodes():
            fm, body = _m_common.read_fm(n["dir"] / "node.md")
            moved = []
            for k in _m_common.LOCAL_KEYS:
                if k in fm:
                    v = fm.pop(k)
                    if v and (v != "none" or k == "preview"):
                        inst.local_of(n["id"])[k] = v
                    moved.append(k)
            home = fm.get("home", "here")
            if home not in ("here", "none", "external", ""):
                _m_nodes.bind_home(inst, n["id"], fm, home)
                moved.append("home")
            if moved:
                _m_common.write_fm(n["dir"] / "node.md", fm, body)
                done.append(f"{n['id']}: {', '.join(moved)} -> .brain/local.json")
        inst.save_local()
    if "links" in kinds:
        done += [f"linked {l.relative_to(inst.root)}" for l in _m_hosts.link_runtimes(inst)]
    if "backlog" in kinds:
        path = inst.root / "backlog.md"
        _m_common.atomic_write(path, "")
        _m_common.touch(path)
        done.append("root: added backlog.md")
    if "templates" in kinds:
        for n in inst.nodes():
            if n["id"] == "root":
                continue
            tdir = _m_common.CORE / "templates" / "node"
            for f in tdir.rglob("*"):
                rel = f.relative_to(tdir)
                if f.is_file() and rel.name != ".gitkeep" and not (n["dir"] / rel).exists():
                    (n["dir"] / rel).parent.mkdir(parents=True, exist_ok=True)
                    _m_common.safe_target(n["dir"] / rel)
                    (n["dir"] / rel).write_text(_m_common.fill(f.read_text(), {"ID": n["id"], "TITLE": n["fm"].get("title", "")}))
                    _m_common.touch(n["dir"] / rel)
                    done.append(f"{n['id']}: added {rel}")
                    if rel.name == "status.md":
                        _m_status.refresh_status(inst, n)
    if "facts" in kinds:
        for n in inst.nodes():
            if n["archived"]:
                continue
            f = n["dir"] / "status.md"
            text = f.read_text() if f.exists() else ""
            # Another node may have requested this fix. Never repair ambiguous markers.
            if _m_status.FACTS_START not in text and _m_status.FACTS_END not in text:
                _m_status.refresh_status(inst, n)
                done.append(f"{n['id']}: inserted generated facts block")
    if "views" in kinds:
        done += [f"{nid}: sealed-view section updated" for nid in _m_nodes.resync_views(inst)]
    if _m_status.write_registry(inst):
        done.append("registry.md regenerated")
    return done


def cmd_check(a):
    inst = _m_nodes.load()
    from . import semantic as _m_semantic
    print(_m_semantic.semantic_mode(inst))
    problems, warnings, fixable = collect_problems(inst)
    if a.fix and fixable:
        for d in apply_fixes(inst, fixable):
            print(f"fixed: {d}")
        inst.commit([], "check --fix")
        inst = _m_instance.Instance(inst.root)
        problems, warnings, fixable = collect_problems(inst)
    seen = set()
    for n in inst.nodes():
        target, command = _m_hosts.wake_target(inst, n)
        if n["archived"] or target["id"] in seen or not command.startswith(("http://", "https://")):
            continue
        seen.add(target["id"])
        directory = _m_hosts.wake_directory(inst, target)
        name = "probe-" + _m_hosts.wake_binding(command) + ".json"
        # Default/--fix only read cached results. Only explicit --live may POST;
        # --fix owns the instance lock and must never wait/send, even with --live.
        if a.fix or not getattr(a, "live", False):
            record = (_m_hosts.wake_read(directory, command, name)
                      or _m_hosts.wake_read(directory, command))
            status = record["status"] if record else "not probed; run brain check --live"
        else:
            status = _m_hosts.wake_probe(inst, target, force=getattr(a, "live", False))
            record = _m_hosts.wake_read(directory, command, name)
        age = f"result age {max(0, int(time.time() - record['at']))}s" if record else "not cached"
        result = "HTTP " + str(status) if type(status) is int else status
        line = f"{target['id']}: wake probe {result} ({age})"
        if type(status) is int and not 200 <= status < 300:
            problems.append(line)
        elif type(status) is int and 200 <= status < 300:
            print("✓ " + line)
        else:
            warnings.append(line)
        delivery = _m_hosts.wake_read(directory, command)
        if delivery and delivery["status"] != status and not (
                type(delivery["status"]) is int and 200 <= delivery["status"] < 300):
            diagnostic = _m_hosts.wake_diagnostic(inst, target)
            if diagnostic:
                if type(delivery["status"]) is int and not 200 <= delivery["status"] < 300:
                    problems.append(f"{target['id']}: {diagnostic}")
                else:
                    warnings.append(f"{target['id']}: {diagnostic}")
    for w in warnings:
        print(f"! {w}")
    if problems:
        print("\n".join(f"✗ {x}" for x in problems))
        if fixable:
            print("(some can be fixed with: brain check --fix)")
        raise _m_common.VerdictExit(1)
    print(f"✓ {len(inst.nodes())} nodes, {len(inst.items())} items: all checks pass")


# ---------- machine, upgrade, selftest ----------
