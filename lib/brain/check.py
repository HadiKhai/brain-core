"""Check mechanics for Brain."""
import os
import re
import sys
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
        for l in _m_knowledge.parse_lessons(s["dir"] / "lessons.md") if (s["dir"] / "lessons.md").exists() else []:
            if not _m_instance.resolve_ref(nodes, l["from"]):
                warnings.append(f"{s['dir'].relative_to(inst.root)}/lessons.md: lesson from unknown node '{l['from']}'")
    for x in _m_schedules.read_schedules(inst):
        for f in _m_schedules.SCHEDULE_FIELDS:
            if not x.get(f):
                p(f"schedules.md: '{x['id']}' is missing '{f}'")
        if x.get("node") and not _m_instance.resolve_ref(nodes, x["node"]):
            p(f"schedules.md: '{x['id']}' names unknown node '{x['node']}'")
    for sid in inst.local.get("schedules", {}):
        if not any(x["id"] == sid for x in _m_schedules.read_schedules(inst)):
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
    problems, warnings, fixable = collect_problems(inst)
    if a.fix and fixable:
        for d in apply_fixes(inst, fixable):
            print(f"fixed: {d}")
        inst.commit([], "check --fix")
        inst = _m_instance.Instance(inst.root)
        problems, warnings, fixable = collect_problems(inst)
    for w in warnings:
        print(f"! {w}")
    if problems:
        print("\n".join(f"✗ {x}" for x in problems))
        if fixable:
            print("(some can be fixed with: brain check --fix)")
        sys.exit(1)
    print(f"✓ {len(inst.nodes())} nodes, {len(inst.items())} items: all checks pass")


# ---------- machine, upgrade, selftest ----------
