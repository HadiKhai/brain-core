"""Knowledge mechanics for Brain."""
import json
import re
import shlex
import sys
from pathlib import Path

from . import briefs as _m_briefs
from . import common as _m_common
from . import guard as _m_guard
from . import history as _m_history
from . import instance as _m_instance
from . import jobs as _m_jobs
from . import nodes as _m_nodes
from . import spend as _m_spend
from . import usage as _m_usage
from . import worktrees as _m_worktrees


def usable(item_fm, owners):
    scope = _m_common.lst(item_fm.get("scope"))
    if item_fm.get("status", "active") != "active":
        return False, f"item is {item_fm.get('status')}"
    if "*" in scope:
        return True, "scope is *"
    missing = [o for o in owners if o not in scope]
    return (not missing), ("all owners in scope" if not missing else f"not in scope: {', '.join(missing)}")


def widening(old, new):
    old, new = set(old), set(new)
    return "*" not in old and ("*" in new or bool(new - old))


def cmd_usable(a):
    inst = _m_nodes.load()
    ok, why = usable(inst.item(a.item)["fm"], inst.node(a.node)["owners"])
    print(f"{'yes' if ok else 'no'}: {why}")
    sys.exit(0 if ok else 1)


def ancestors(n):
    """n, then its parent, up to the root."""
    out = []
    while n:
        out.append(n)
        n = n["parent"]
    return out


def common_ancestor(nodes):
    """The lowest node that sees all of them (axiom 13)."""
    shared = [m["id"] for m in ancestors(nodes[0])]
    for n in nodes[1:]:
        ids = {m["id"] for m in ancestors(n)}
        shared = [m for m in shared if m in ids]
    return shared[0]


def source_nodes(inst, spec):
    """--from a,b,c: the nodes an item or specialist was learned from, and the scope that knowledge allows:
    only principals in every source's owners, so nothing reaches a node that one of its sources may not."""
    ids = list(dict.fromkeys(_m_common.lst(spec)))
    if not ids:
        _m_common.die("--from names at least one node")
    nodes = [inst.node(i) for i in ids]
    scope = [o for o in nodes[0]["owners"] if all(o in n["owners"] for n in nodes[1:])]
    if not scope:
        _m_common.die("these nodes share no owner, so no node may use what is learned from all of them together; "
            "learn from each separately (axioms 10, 11)")
    return nodes, scope


def learning_terms(inst, fm, extra=(), nodes=None):
    """Never-name words a learning carries from where it came: every source node's (their ancestors' included),
    plus any extra nodes' (a target, the node holding it). Knowledge never takes a source's words anywhere."""
    nodes = inst.nodes() if nodes is None else nodes
    out = []
    for n in [_m_instance.resolve_ref(nodes, ref) for ref in [fm.get("source_node", "")] + _m_common.lst(fm.get("source_nodes"))] + list(extra):
        if n:
            out += [t for t in _m_guard.never_terms(n) if t not in out]
    return out


def specialist_text(sp):
    return "\n".join((sp["dir"] / f).read_text() for f in ("role.md", "lessons.md") if (sp["dir"] / f).is_file())


def specialist_terms(inst, sp, extra=(), nodes=None):
    """A specialist carries the words of every node it was learned from: its sources, and each lesson's node."""
    f = sp["dir"] / "lessons.md"
    froms = [l["from"] for l in parse_lessons(f)] if f.is_file() else []
    fm = _m_common.read_fm(sp["dir"] / "role.md")[0]
    return learning_terms(inst, {"source_nodes": ", ".join(_m_common.lst(fm.get("source_nodes")) + froms)}, extra, nodes)


def placed_at(inst, at, nodes):
    """Where a learning from several nodes lives: their lowest common ancestor unless --at names a higher one."""
    if not at:
        return inst.node(common_ancestor(nodes) if len(nodes) > 1 else "root")
    place = inst.node(at)
    if len(nodes) > 1 and any(place["id"] not in {m["id"] for m in ancestors(n)} for n in nodes):
        _m_common.die(f"--at {place['id']} can't see every source; use a node above all of them, "
            f"such as {common_ancestor(nodes)} (axiom 13)")
    return place


def cmd_item_add(a):
    inst = _m_nodes.load()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", a.id):
        _m_common.die("item ids are lowercase words with dashes")
    if any(i["fm"].get("id") == a.id for i in inst.items()):
        _m_common.die(f"item '{a.id}' already exists")
    sources, shared = source_nodes(inst, a.source)
    src = sources[0]
    at = placed_at(inst, a.at, sources)
    scope = _m_common.lst(a.scope) if a.scope else shared
    if widening(shared, scope):
        need = {o for n in sources for o in n["owners"]} - set(_m_common.lst(a.consented_by))
        if need:
            _m_common.die(f"scope {','.join(scope)} is wider than the source's owners; "
                f"needs --consented-by {','.join(sorted(need))} (axiom 11)")
    for o in scope:
        if o != "*" and o not in inst.principals:
            _m_common.die(f"unknown principal '{o}'")
    keywords = ", ".join(" ".join(k.split()) for k in (a.keywords or "").split(",") if k.strip())
    if a.proposed and any("\n" in v or "\r" in v for v in (a.title, a.kind, a.ref, a.keywords or "")):
        _m_common.die("proposed item metadata must stay on one line")
    owners = list(dict.fromkeys(o for n in sources for o in n["owners"]))
    values = {"ID": a.id, "TITLE": a.title, "KIND": a.kind, "SOURCE_NODE": src["id"],
              "SOURCE_REF": a.ref, "DATE": inst.now(), "SOURCE_OWNERS": ", ".join(owners),
              "SCOPE": ", ".join(scope), "STATUS": "proposed" if a.proposed else "active"}
    text = _m_common.fill((_m_common.CORE / "templates" / "item.md").read_text(), values)
    extra = (f"source_nodes: {', '.join(n['id'] for n in sources)}\n" if len(sources) > 1 else "") + \
            (f"keywords: {keywords}\n" if keywords else "")
    if extra:
        text = text.replace("\n---\n", "\n" + extra.rstrip("\n") + "\n---\n", 1)
    if a.body or a.body_file:
        body = a.body if a.body else Path(a.body_file).read_text()
        text = text.split("\n---\n", 1)[0] + "\n---\n\n" + body.strip() + "\n"
    if a.proposed:
        for n in sources:  # each source's never-name words, its ancestors' included
            _m_guard.no_terms_in(n, text, "the library item")
        body = text.split("\n---\n", 1)[-1]
        if _m_nodes.SECRET_META.search(text) or _m_nodes.SECRET_KEY.search(body):
            _m_common.die("unsafe proposed item content")
        if not a.repo or not a.commit or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", a.commit):
            _m_common.die("a proposed item needs --repo <non-local repo identity> and --commit <full hash>")
        if Path(a.repo).is_absolute() or a.repo.startswith(("~", ".", "file:")):
            _m_common.die("--repo must be a portable identity, not a local path")
        repo = _m_nodes.clean_remote(a.repo) if ":" in a.repo else a.repo
        if not repo:
            _m_common.die("--repo must be a portable identity, not a local path")
        metadata = repo + "\n" + a.commit
        for n in sources:
            _m_guard.no_terms_in(n, metadata, "the item provenance")
        if "\n" in repo or "\r" in repo or _m_nodes.SECRET_META.search(metadata):
            _m_common.die("unsafe item provenance")
        text = text.replace("\n---\n", f"\nsource_repo: {repo}\nsource_commit: {a.commit}\n---\n", 1)
    lib = at["dir"] / "library"
    lib.mkdir(exist_ok=True)
    path = lib / f"{a.id}.md"
    _m_common.safe_target(path)
    path.write_text(text)
    inst.commit([path], f"learn: {a.id}")
    print(f"{_m_common.tilde(path)}\n  from {', '.join(n['id'] for n in sources)} ({a.ref}); scope {', '.join(scope)}")


def cmd_item_show(a):
    inst = _m_nodes.load()
    item = inst.item(a.id)
    n = inst.node(a.node)
    ok, why = usable(item["fm"], n["owners"])
    if not ok:
        _m_common.die("item is not usable here: " + why)
    text = item["path"].read_text()
    if _m_guard.find_terms(text, learning_terms(inst, item["fm"], [n])):
        _m_common.die("the item holds a never-name word of this node or of where it was learned; it can't be shown here")
    print(text, end="")


def cmd_item_list(a):
    inst = _m_nodes.load()
    owners = inst.node(a.node)["owners"] if a.node else None
    for it in inst.items():
        fm = it["fm"]
        flag = ""
        if owners is not None:
            ok, _ = usable(fm, owners)
            flag = "  usable" if ok else "  -"
        print(f"{fm.get('id', it['path'].stem):<24} {fm.get('kind', ''):<10} scope={fm.get('scope', '')}"
              f"  from={fm.get('source_node', '')}  {fm.get('status', '')}{flag}")


def cmd_item_scope(a):
    inst = _m_nodes.load()
    it = inst.item(a.id)
    fm, body = _m_common.read_fm(it["path"])
    old = _m_common.lst(fm.get("scope"))
    new = _m_common.lst(a.scope)
    if widening(old, new):
        # Learned from several nodes, the scope is what their owners share; each source's owners still decide
        # whether their part of it reaches further.
        everyone = set(old) | (set(_m_common.lst(fm.get("source_owners"))) if _m_common.lst(fm.get("source_nodes")) else set())
        need = everyone - set(_m_common.lst(a.consented_by)) - {"*"}
        if need:
            _m_common.die(f"widening needs the consent of everyone in the current scope"
                f"{' and every source owner' if _m_common.lst(fm.get('source_nodes')) else ''}: --consented-by "
                f"{','.join(sorted(everyone - {'*'}))} (axiom 11)")
    fm["scope"] = ", ".join(new)
    _m_common.write_fm(it["path"], fm, body)
    inst.commit([it["path"]], f"scope: {a.id} -> {fm['scope']}")
    print(f"{a.id}: scope {', '.join(old)} -> {fm['scope']}")


def cmd_item_status(a, status):
    inst = _m_nodes.load()
    it = inst.item(a.id)
    fm, body = _m_common.read_fm(it["path"])
    if status == "quarantined":
        if fm.get("status") != "quarantined":
            fm["status_before_quarantine"] = fm.get("status", "active")
    else:
        if fm.get("status") in ("proposed", "rejected"):
            _m_common.die("restore cannot accept a proposed or rejected item")
        status = fm.pop("status_before_quarantine", "active")
        if status == "proposed" and set(_m_common.lst(fm.get("scope"))) & set(inst.revoked):
            _m_common.die("can't restore a proposal scoped to a revoked principal")
    fm["status"] = status
    if status == "quarantined":
        fm["quarantined"] = f"{inst.now()}, {a.reason or 'by the operator'}"
    else:
        fm.pop("quarantined", None)
    _m_common.write_fm(it["path"], fm, body)
    inst.commit([it["path"]], f"{status}: {a.id}")
    print(f"{a.id}: {status}")


def review_principal(inst, reviewer):
    if reviewer != inst.cfg.get("operator") and (reviewer not in inst.principals or reviewer in inst.revoked):
        _m_common.die("--reviewed-by must be the operator or a non-revoked principal in instance.md")
    return reviewer


def cmd_item_accept(a):
    """Accept one or several reviewed proposals: all are checked before any changes, then one commit."""
    inst = _m_nodes.load()
    review_principal(inst, a.reviewed_by)
    if _m_nodes.SECRET_META.search(a.reviewed_by):
        _m_common.die("unsafe review attribution")
    todo = []
    for iid in dict.fromkeys(a.id):
        it = inst.item(iid)
        fm, body = _m_common.read_fm(it["path"])
        if fm.get("status") != "proposed":
            _m_common.die(f"{iid}: only a proposed item can be accepted")
        terms = learning_terms(inst, fm, [n for n in inst.nodes() if n["dir"].resolve() == it["path"].parent.parent.resolve()])
        if _m_guard.find_terms(a.reviewed_by, terms):
            _m_common.die("the review attribution holds a never-name word")
        if _m_guard.find_terms(it["path"].read_text(), terms):
            _m_common.die(f"{iid}: holds a never-name word of a node it was learned from; edit it before accepting")
        todo.append((iid, it, fm, body))
    when = inst.now()
    for iid, it, fm, body in todo:
        fm["status"] = "active"
        fm["accepted_at"] = when
        fm["accepted_by"] = a.reviewed_by
        _m_common.write_fm(it["path"], fm, body)
    inst.commit([t[1]["path"] for t in todo], "accept: " + ", ".join(t[0] for t in todo))
    for iid, *_ in todo:
        print(f"{iid}: accepted")


def cmd_item_reject(a):
    inst = _m_nodes.load()
    todo = []
    for iid in dict.fromkeys(a.id):
        it = inst.item(iid)
        fm, body = _m_common.read_fm(it["path"])
        if fm.get("status") != "proposed":
            _m_common.die(f"{iid}: only a proposed item can be rejected")
        todo.append((iid, it, fm, body))
    when = inst.now()
    for iid, it, fm, body in todo:
        fm["status"] = "rejected"
        fm["rejected_at"] = when
        _m_common.write_fm(it["path"], fm, body)
    inst.commit([t[1]["path"] for t in todo], "reject: " + ", ".join(t[0] for t in todo))
    for iid, *_ in todo:
        print(f"{iid}: rejected")


# ---------- specialists ----------

def find_specialists(inst, sid=None):
    out = []
    for n in inst.nodes():
        sd = n["dir"] / "specialists"
        if sd.is_dir():
            for r in sorted(sd.glob("*/role.md")):
                if sid is None or r.parent.name == sid:
                    out.append({"node": n, "dir": r.parent, "id": r.parent.name, "fm": _m_common.read_fm(r)[0]})
    return out


def cmd_item_quarantine(a):
    cmd_item_status(a, "quarantined")


def cmd_item_restore(a):
    cmd_item_status(a, "active")


def cmd_specialist_new(a):
    inst = _m_nodes.load()
    at = inst.node(a.at or "root")
    d = at["dir"] / "specialists" / a.id
    if d.exists():
        _m_common.die(f"{d} already exists")
    runtime = a.runtime or inst.cfg.get("default_runtime", "")
    scope = _m_common.lst(a.scope) if a.scope else at["owners"]
    sources = []
    if a.source:  # a role drawn from how these nodes work: usable only where all their knowledge may go
        sources, shared = source_nodes(inst, a.source)
        at = placed_at(inst, a.at, sources)
        scope = _m_common.lst(a.scope) if a.scope else shared
        if "*" in scope or set(scope) - set(shared):
            _m_common.die(f"scope {', '.join(scope)} is wider than what {', '.join(n['id'] for n in sources)} share "
                f"({', '.join(shared)}): a specialist learned from them can't reach further (axiom 11)")
        d = at["dir"] / "specialists" / a.id
        if d.exists():
            _m_common.die(f"{d} already exists")
        for n in sources:
            _m_guard.no_terms_in(n, a.id + "\n" + a.title, "the specialist")
    vals = {"ID": a.id, "TITLE": a.title, "RUNTIME": runtime, "SCOPE": ", ".join(scope)}
    d.mkdir(parents=True)
    for f in ("role.md", "lessons.md"):
        (d / f).write_text(_m_common.fill((_m_common.CORE / "templates" / "specialist" / f).read_text(), vals))
    (d / "direction.md").write_text(_m_common.fill((_m_common.CORE / "templates/direction.md").read_text(), vals))
    if a.proposed or sources:
        fm, body = _m_common.read_fm(d / "role.md")
        if a.proposed:
            fm.update(status="proposed", imported="true")
        if sources:
            fm["source_nodes"] = ", ".join(n["id"] for n in sources)
        _m_common.write_fm(d / "role.md", fm, body)
    inst.commit([d], f"specialist: {a.id}")
    print(f"{_m_common.tilde(d)}/role.md  (scope {', '.join(scope)}): fill in purpose, standards, checklist")


def cmd_specialist_accept(a):
    inst = _m_nodes.load()
    review_principal(inst, a.reviewed_by)
    at = inst.node(a.at or "root")
    todo = []
    for sid in dict.fromkeys(a.id):
        path = at["dir"] / "specialists" / sid / "role.md"
        if not path.is_file():
            _m_common.die(f"no specialist '{sid}' at {at['id']}")
        fm, body = _m_common.read_fm(path)
        if fm.get("status") != "proposed":
            _m_common.die(f"{sid}: only a proposed specialist can be accepted")
        if _m_guard.find_terms(specialist_text({"dir": path.parent}) + "\n" + a.reviewed_by,
                      specialist_terms(inst, {"dir": path.parent}, [at])):
            _m_common.die(f"{sid}: holds a never-name word of where it was learned; edit it before accepting")
        todo.append((sid, path, fm, body))
    when = inst.now()
    for sid, path, fm, body in todo:
        fm.update(status="active", accepted_at=when, accepted_by=a.reviewed_by)
        _m_common.write_fm(path, fm, body)
    inst.commit([t[1] for t in todo], "accept specialist: " + ", ".join(t[0] for t in todo))
    for sid, *_ in todo:
        print(f"{sid}: accepted")


def direction_principles(path):
    """Read explicit ids only from the Principles section; never infer or activate rules.

    Each ### id: title block may name one enforced_by value. Keep that value as
    evidence at capture time, even if the direction later changes.
    """
    if not path.is_file():
        return {}
    out, active, current, fenced = {}, False, None, False
    text = re.sub(r"<!--.*?(?:-->|$)", "", path.read_text(), flags=re.S)
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced:
            continue
        if re.match(r"^#{1,2} ", line):
            active = line.strip() == "## Principles"
            current = None
        elif active and line.startswith("### "):
            m = re.fullmatch(r"### ([a-z][a-z0-9-]*): .+", line)
            current = m.group(1) if m else None
            if current:
                if current == "unclassified" or current in out:
                    _m_common.die("direction.md has a reserved or duplicate principle id")
                out[current] = {"enforced_by": "wish"}
        elif active and current and line.startswith("enforced_by:"):
            rule = out[current]
            if rule.get("explicit"):
                _m_common.die("direction.md has more than one enforced_by for a principle")
            value = line.split(":", 1)[1].strip()
            rule.update(enforced_by=value or "wish", explicit=True)
    return out


def parse_lessons(path):
    out = []
    if not path.is_file():
        return out
    for line in path.read_text().splitlines():
        m = re.match(r"^- (.+?) · from (.+?) · (.*)$", line)
        if m:
            tags, rest, malformed = {}, m.group(3), False
            if rest.startswith("tags "):
                try:
                    value, end = json.JSONDecoder().raw_decode(rest[5:])
                except (ValueError, TypeError):
                    malformed = True
                else:
                    if isinstance(value, dict):
                        tags = value
                    else:
                        malformed = True
                    rest = rest[5 + end:]
                    if not rest.startswith(" · "):
                        malformed = True
                    rest = rest[3:]
            if malformed:
                # Retain source policy even when a damaged record cannot safely be hired.
                out.append({"time": m.group(1), "from": m.group(2), "scope": [], "text": m.group(3),
                            "principle": "unclassified", "enforced_by": "wish", "line": line, "malformed": True})
                continue
            scoped = re.fullmatch(r"scope (.+?): (.+)", rest)
            if not scoped:
                continue
            principle = tags.get("principle", "unclassified")
            if not isinstance(principle, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", principle):
                principle = "unclassified"
            check = tags.get("enforced_by", "wish")
            if not isinstance(check, str) or principle == "unclassified":
                check = "wish"
            out.append({"time": m.group(1), "from": m.group(2), "scope": _m_common.lst(scoped.group(1)), "text": scoped.group(2),
                        "principle": principle, "enforced_by": check, "line": line})
    return out


def cmd_lesson(a):
    inst = _m_nodes.load()
    if a.node:
        if a.at:
            _m_common.die("--at selects a specialist's library; omit it with --node")
        target = inst.node(a.specialist)
        folder, sources = target["dir"], {}
    else:
        found = [s for s in find_specialists(inst, a.specialist) if not a.at or s["node"]["id"] == a.at]
        if not found:
            _m_common.die(f"no specialist '{a.specialist}'")
        if len(found) > 1:
            _m_common.die(f"several specialists named '{a.specialist}'; add --at <node>")
        target, folder, sources = found[0]["node"], found[0]["dir"], found[0]["fm"]
    src = inst.node(a.source)
    if a.node:
        current = src
        while current and set(current["owners"]) == set(target["owners"]):
            if current["id"] == target["id"]:
                break
            current = current["parent"]
        else:
            _m_common.die("a node lesson must come from that node or a descendant without an owner boundary")
    text = a.text.strip()
    if not text or len(text.splitlines()) != 1:
        _m_common.die("a lesson must be one nonempty line")
    principle, check = a.principle, "wish"
    if principle != "unclassified":
        rules = direction_principles(folder / "direction.md")
        if principle not in rules:
            _m_common.die("principle id is absent from the target's direction.md; use unclassified if none fits")
        check = rules[principle]["enforced_by"]
    tags = json.dumps({"principle": principle, "enforced_by": check}, ensure_ascii=False)
    path = folder / "lessons.md"
    line = f"- {inst.now()} · from {src['id']} · tags {tags} · scope {', '.join(src['owners'])}: {text}"
    policy_nodes = [src, target]
    if a.node and src["id"] == target["id"]:
        # A node's own notes may discuss its own terms, while inherited boundaries still apply.
        policy_nodes = [target["parent"]] if target["parent"] else []
    if _m_guard.find_terms(text + "\n" + tags, learning_terms(inst, sources, policy_nodes)):
        _m_common.die("lesson or its tags contain a never-name word of their source or target; reword before recording")
    lines = path.read_text().splitlines() if path.is_file() else ["# Lessons", ""]
    idx = next((i for i, l in enumerate(lines) if l.startswith("- ")), len(lines))
    if idx == len(lines) and lines and lines[-1].strip():
        lines.append("")
        idx += 1
    lines.insert(idx, line)
    _m_common.safe_target(path)
    path.write_text("\n".join(lines) + "\n")
    inst.commit([path], f"lesson: {a.specialist} from {src['id']}")
    print(line)


def cmd_hire(a):
    inst = _m_nodes.load()
    target = inst.node(a.node)
    chain, m = [], target
    while m:
        chain.append(m["id"])
        m = m["parent"]
    cands = sorted([s for s in find_specialists(inst, a.specialist) if s["node"]["id"] in chain],
                   key=lambda s: chain.index(s["node"]["id"]))
    if not cands:
        _m_common.die(f"no specialist '{a.specialist}' in {target['id']} or above it")
    sp = cands[0]
    if sp["fm"].get("status", "active") != "active":
        _m_common.die("imported specialist is unreviewed; have the operator or coordinator confirm it before hire")
    scope = _m_common.lst(sp["fm"].get("scope"))
    if "*" not in scope:
        missing = [o for o in target["owners"] if o not in scope]
        if missing:
            _m_common.die(f"specialist '{sp['id']}' isn't scoped for {', '.join(missing)} (axiom 10)")
    if _m_guard.find_terms(str(sp["dir"] / "role.md") + "\n" + (sp["dir"] / "role.md").read_text(), learning_terms(
            inst, {"source_nodes": sp["fm"].get("source_nodes")}, [sp["node"], target])):
        _m_common.die(f"specialist '{sp['id']}' holds a never-name word of where it was learned or of {target['id']}")
    nodes = inst.nodes()
    lessons = [l for l in parse_lessons(sp["dir"] / "lessons.md")
               if ("*" in l["scope"] or all(o in l["scope"] for o in target["owners"])) and
               not _m_guard.find_terms(l["text"], learning_terms(inst, {"source_node": l["from"],
                                                                "source_nodes": sp["fm"].get("source_nodes")},
                                                          [sp["node"], target], nodes))]
    items = [i for i in inst.items() if usable(i["fm"], target["owners"])[0] and
             not _m_guard.find_terms(i["path"].read_text(), learning_terms(inst, i["fm"], [target], nodes))]
    wanted = _m_common.lst(a.items)
    for w in wanted:
        if not any(i["fm"].get("id") == w for i in items):
            _m_common.die(f"item '{w}' doesn't exist or isn't usable in {target['id']}")
    if target["external"] and not (target["home_path"] and target["home_path"].exists()):
        _m_common.die(f"{target['id']}'s home isn't on this machine (brain set {target['id']} home=<path>)")
    where, w = _m_worktrees.job_where(target, a.branch)
    runtime, usage = worker_route(inst, target, a)
    head = _m_worktrees.job_run_line(where, w, a.branch, runtime, a.model, tilde_home=False)
    head = head.replace(f"  ·  runtime: {runtime}", f"  ·  runtime: {runtime}  ·  class: {a.job_class}", 1)
    out = [f"Role: worker, hired as specialist `{sp['id']}` ({sp['fm'].get('title', '')}).",
           head,
           "The rules, agents and skills where you run come first (axiom 7); this brief adds to them.",
           "", a.selection_line, "", _m_guard.redact(usage, _m_guard.never_terms(target)), ""]
    if target["sealed"]:
        out += ["This work happens in a project with its own owners. Leave nothing in it that names "
                "where this brief came from (axiom 6). Ask before pushing, publishing or anything "
                "irreversible (axiom 24).", ""]
    out += _m_briefs.separation_lines(a, target, a.branch)
    out += ["## Task", "", a.task.strip(), ""] + _m_jobs.contract_lines(inst, target, a.branch) + \
        ["## Specialist standards", "", _m_briefs.reference_line(sp["dir"] / "role.md", sp["fm"].get("title", sp["id"])), ""] + limits_lines(target)
    if lessons:
        out += ["## Lessons from earlier jobs", ""] + [f"- {l['text']}" for l in lessons] + [""]
    if wanted:
        out += ["## Knowledge for this job", ""]
        for i in items:
            if i["fm"].get("id") in wanted:
                reference = _m_briefs.reference_line(i["path"], i["fm"].get("title", i["fm"].get("id")))
                if _m_guard.find_terms(reference, learning_terms(inst, i["fm"], [target], nodes)):
                    _m_common.die("library reference holds a never-name word; cannot include it in the brief")
                out += [reference, ""]
    out += knowledge_lines(inst, target, a.task, sp["id"], exclude=wanted) + node_knowledge_lines(target) + \
        recall_hint(target)
    out += handback_lines(inst, target, branch=a.branch) + report_lines(job=bool(a.branch))
    text = _m_briefs.checked_brief(target, "\n".join(out) + "\n")
    _m_guard.no_terms_in(target, text, "the brief")
    _m_jobs.record_job_route(inst, target, a.branch, a.job_class, runtime, a.model, effort=a.effort)
    if a.job:
        rows = inst.local.setdefault("hires", [])
        record = {"specialist": sp["id"], "at": sp["node"]["id"], "node": target["id"],
                  "job": a.job, "task": a.task.strip(), "time": inst.now()}
        _m_guard.no_terms_in(target, json.dumps(record, ensure_ascii=False), "the hire record")
        if _m_nodes.SECRET_META.search(a.job) or _m_nodes.SECRET_LINE.search(a.task):
            _m_common.die("unsafe hire record")
        rows.append(record)
        inst.save_local()
    if a.out:
        _m_history.write_out(inst, target, a.out, text)
    else:
        print(text, end="")



# ---------- worker briefs ----------

KNOWLEDGE_STOP = {"the", "and", "for", "with", "from", "this", "that", "into", "use", "job", "node"}
KNOWLEDGE_SUFFIXES = ("ations", "ation", "ions", "ion", "ings", "ing", "ies", "ied", "ers", "er", "es", "ed", "ly", "s")


def knowledge_stem(word):
    """A light stem, so "throttling writes" meets "throttle a write": the same word in another form."""
    for suf in KNOWLEDGE_SUFFIXES:
        if word.endswith(suf) and len(word) - len(suf) >= 3:
            word = word[:-len(suf)] + ("y" if suf in ("ies", "ied") else "")
            break
    return word[:-1] if word.endswith("e") and len(word) > 4 else word


def knowledge_tokens(text):
    """{stem: the word as written}: matching uses stems, explanations show the words."""
    out = {}
    for t in re.findall(r"[\w]+", text.casefold()):
        if len(t) > 2 and t not in KNOWLEDGE_STOP:
            out.setdefault(knowledge_stem(t), t)
    return out


def knowledge_ranked(inst, n, contexts, exclude=(), states=("active",), required=None):
    """Explainable lexical ranking of items whose scope covers n's owners; only `states` count (usable items
    are exactly `brain usable`: active and in scope). With `required`, that context must match.
    [(score, id, title, why, item)], best first."""
    ranked = []
    nodes = inst.nodes()
    for item in inst.items():
        fm = item["fm"]
        if fm.get("id") in exclude or fm.get("status", "active") not in states:
            continue
        if not usable({**fm, "status": "active"}, n["owners"])[0]:
            continue
        if _m_guard.find_terms(item["path"].read_text(), learning_terms(inst, fm, [n], nodes)):
            continue
        body = _m_common.read_fm(item["path"])[1]
        words = knowledge_tokens(" ".join(str(fm.get(k, "")) for k in ("id", "title", "kind", "keywords")) + " " + body)
        score, why, hit = 0, [], set()
        for name, terms, weight in contexts:
            matches = sorted(terms[m] for m in words.keys() & terms.keys())
            if matches:
                score += weight * len(matches)
                why.append(name + ": " + ", ".join(matches[:4]))
                hit.add(name)
        if required and required not in hit:
            continue
        if fm.get("source_node") == n["id"]:
            score += 2
            why.append("same node")
        if score:
            key = fm.get("id", "")
            ranked.append((-score, key, " ".join(fm.get("title", key).split()), why, item))
    return sorted(ranked, key=lambda r: r[:2])


def knowledge_lines(inst, n, task, specialist="", exclude=()):
    """Up to five usable items for a brief, each with why it was picked."""
    contexts = [("task", knowledge_tokens(task), 4),
                ("labels/stack", knowledge_tokens(n["fm"].get("labels", "") + " " + n["fm"].get("stack", "")), 3),
                ("specialist", knowledge_tokens(specialist), 3)]
    lines = []
    for _, key, title, why, item in knowledge_ranked(inst, n, contexts, exclude):
        line = (f"- `{key}`: {title} — picked for {'; '.join(why)}. "
                f"Path: `{_m_common.tilde(item['path'])}`. Read full: `brain item show {key} --node {n['id']}`.")
        if not _m_guard.find_terms(line, learning_terms(inst, item["fm"], [n])):
            lines.append(line)
    if not lines:
        return []
    return ["## Relevant library items", ""] + lines[:5] + [""]


def node_knowledge_lines(n):
    """A pointer to the node's own technical notes (knowledge.md). A sealed node's worker runs in its home and
    gets what it needs in the task instead: nothing above a boundary is pointed at from inside it."""
    f = n["dir"] / "knowledge.md"
    if n["sealed"] or not f.is_file() or not f.read_text().strip():
        return []
    return [f"How this project is built and what earlier work learned: {_m_common.tilde(f)}. Read the parts your task touches.", ""]


def recall_hint(n):
    return [f"Stuck on something another project may have met? `brain recall {n['id']} \"<the problem>\"` lists "
            "what every project learned that this one may use.", ""]


def cmd_recall(a):
    """What every project learned that may help with this problem here: usable items first, then a count of
    unreviewed proposals that also match (they help only once accepted)."""
    inst = _m_nodes.load()
    n = inst.node(a.id)
    problem = " ".join(a.words)
    terms = knowledge_tokens(problem)
    if not terms:
        _m_common.die("describe the problem in a few words: brain recall <node> \"what goes wrong\"")
    contexts = [("problem", terms, 4),
                ("labels/stack", knowledge_tokens(n["fm"].get("labels", "") + " " + n["fm"].get("stack", "")), 1)]
    forbidden = _m_guard.never_terms(n)
    found = knowledge_ranked(inst, n, contexts, required="problem")[:a.limit]
    waiting = knowledge_ranked(inst, n, contexts, states=("proposed",), required="problem")
    if a.json:
        rows = [{"id": key, "title": _m_guard.redact(title, forbidden), "kind": item["fm"].get("kind", ""),
                 "from": item["fm"].get("source_node", ""), "score": -score, "why": why}
                for score, key, title, why, item in found]
        print(json.dumps(_m_history.history_screen({"node": n["id"], "items": rows, "proposed": [r[1] for r in waiting]},
                                        forbidden), indent=1, ensure_ascii=False))
        return
    if not found:
        print(f"{n['id']}: nothing learned yet matches that")
    for score, key, title, why, item in found:
        src = item["fm"].get("source_node", "")
        print(_m_guard.redact(f"- `{key}` ({item['fm'].get('kind', '')}, from {src}): {title} — {'; '.join(why)}. "
                     f"Read: brain item show {key} --node {n['id']}", forbidden))
    if waiting:
        print(f"{len(waiting)} unreviewed proposal(s) also match: {', '.join(r[1] for r in waiting[:10])}"
              f"{' …' if len(waiting) > 10 else ''}. They help only once accepted: brain proposals")


def limits_of(n):
    """The bullets under "## Limits" in a node's brief.md, each with its continuation lines."""
    f = n["dir"] / "brief.md"
    if not f.exists():
        return []
    m = re.search(r"^## Limits[^\n]*\n(.*?)(?=^## |\Z)", f.read_text(), re.M | re.S)
    if not m:
        return []
    out = []
    for line in re.sub(r"<!--.*?-->", "", m.group(1), flags=re.S).splitlines():
        if line.startswith("- "):
            out.append(line[2:].strip())
        elif out and line.startswith("  ") and line.strip():
            out[-1] += "\n" + line.strip()
    return out


def limits_lines(n):
    lims = limits_of(n)
    return (["## Limits", ""] + [f"- {l}" for l in lims] + [""]) if lims else []


def host_send(inst, n, to=None):
    to = to or n["local"].get("coordinator")
    if to:
        for warning in _m_jobs.stale_thread_warnings(n, to):
            print(_m_guard.redact(warning, _m_guard.never_terms(n)), file=sys.stderr)
    send = inst.host_adapter().get("SEND")
    return f"host `send` to thread `{to}`" + (f" ({send.replace('{id}', to)})" if send else "") if to else \
        "host `send` to whoever started you (their thread is in your launch message)"


def job_mark_lines(n, branch):
    """The line by which `brain jobs` finds the sessions this brief starts, to count the job's time and tokens."""
    row = _m_jobs.open_job(_m_jobs.read_jobs(n) or [], branch) if branch else None
    return [_m_spend.JOB_MARK.format(_m_jobs.job_key(row)) + " (lets `brain jobs` count this job's time and tokens; nothing to do)",
            ""] if row else []


def handback_lines(inst, n, to=None, who="worker", branch=None):
    """How a worker started by a coordinator asks, and says it's done. It never waits on the operator:
    nobody watches its thread, and a launched thread doesn't wake whoever launched it."""
    from . import messages as _m_messages
    how = host_send(inst, n, to)
    if who == "reviewer":  # a reviewer changes nothing, not even the inbox
        return job_mark_lines(n, branch) + ["## Questions and hand-back", "",
                "- **Never ask the operator, and never wait for an answer in this thread**: nobody is watching it.",
                f"- **A question you can't go on without:** {how} with the question, then end your turn with the "
                "question as your last line.",
                f"- **When you finish**, {how} with `done: <n> findings, <n> blockers` and put the findings in "
                "your final message. Write no file.", ""]
    inbox = "" if n["sealed"] else f" and append `- <time from brain now> from {who}: question: <it>` " \
                                     f"to `{_m_common.tilde(n['dir'] / 'inbox.md')}`"
    if branch:
        study = _m_briefs.import_job(branch)
        prepare = ("Finish the import output checks and commit this job's node files; leave the source home read-only. "
                   if study else "Finish reviews, commit, then `git rebase main`; resolve conflicts yourself. ")
        evidence = ("This node-folder study needs no home ready evidence. Record proposed item ids in --lessons; "
                    "recording them never accepts them. " if study else
                    "This one step runs node verify and checks local main. If verify is unset, ask the coordinator "
                    "to set it. If main moves, rebase and hand back again. Only the coordinator merges to main. ")
        return job_mark_lines(n, branch) + ["## Questions and hand-back", "",
                _m_messages.MESSAGE_PROTOCOL, "",
                "- **Never ask the operator, and never wait for an answer in this thread**: nobody is watching it.",
                "- **A choice** the brief or rules settle: make it and record it under Decisions I made.",
                "- **Before handing back**, " + prepare +
                f"Run `brain handback {shlex.quote(n['id'])} --branch {shlex.quote(branch)} "
                "--state done --reviews <completed rounds> --report <path> --lessons '<proposed lessons or none: reason>'`. "
                + evidence + "State and proposed lessons live in your one worker-owned job-handbacks/<key>.json. "
                "Do not edit the ledger" + (". " if study else " or status. ") +
                "Use `none: <reason>` when there is no reusable lesson.",
                "- **Blocked or need a choice:** use the same command with `--state blocked` or `--state question`, "
                "omit --reviews, and describe the blocker/question in --report. Questions also append to inbox; "
                "for a blocked human need add `--need-human '<need>'`. Legacy failed is also accepted.",
                "- End your turn after recording the hand-back. The coordinator reads it with brain due; "
                f"a host message is optional notification ({how}) and need only link the record. Do not duplicate Lessons "
                "in a message or report; --lessons carries your proposals.", ""]
    return ["## Questions and hand-back", "", _m_messages.MESSAGE_PROTOCOL, "",
            "- **Never ask the operator, and never wait for an answer in this thread**: nobody is watching it.",
            "- **A choice** the task, rules or brief settle: make it and list it under Decisions I made.",
            "- **Your report must have a Lessons section:** list each lesson, or `none: <reason>`.",
            f"- **A question you can't go on without:** {how} with the question{inbox}, then end your turn.",
            f"- **When you finish, fail or get blocked**, tell your coordinator straight away: {how} with "
            "`<kind>: <one line>. Report: <path>` using the message protocol above. Then end your turn.", ""]


def report_lines(job=False):
    return ["## Report", "",
            "The answer first; then the proof (commands, numbers, hashes); what you produced and where; what you "
            "did **not** run or check; Decisions I made; " +
            ("proposed lessons go in the hand-back's --lessons field; " if job else
             "Lessons: what the next job should know, or none: <reason>; ") + "and what is still open."]


def launch_selection(inst, n, a, runtime, subscription, row, kind):
    """Use configured model names, never invent a model or provider identity."""
    a.model = a.model or n["fm"].get("route_model_" + runtime) or inst.cfg.get("route_model_" + runtime) or "runtime default"
    a.effort = _m_jobs.job_effort(n, row, kind, getattr(a, "effort", None))
    instance = (subscription or {}).get("host_provider_instance_id") or runtime
    if a.model == "runtime default":
        a.selection_line = (f"modelSelection: unavailable; set route_model_{runtime} or --model "
                            f"(reasoningEffort: {a.effort}).")
    else:
        selection = {"instanceId": instance, "model": a.model, "options": {"reasoningEffort": a.effort}}
        a.selection_line = "modelSelection: " + json.dumps(selection)


def worker_route(inst, n, a):
    """Route all worker briefs with the same runtime pin and model usage limits."""
    row = _m_jobs.open_job(_m_jobs.read_jobs(n) or [], getattr(a, "branch", None))
    kind = _m_jobs.job_class(n, row, getattr(a, "job_class", None))
    pin = getattr(a, "runtime", None) or n["fm"].get("runtime")
    runtime, subscription, usage = _m_usage.class_route(
        inst, kind, runtime=pin, model=a.model,
        weekly_cap=n["fm"].get("route_weekly_cap", inst.cfg.get("route_weekly_cap", 60)))
    a.job_class = kind
    launch_selection(inst, n, a, runtime, subscription, row, kind)
    return runtime, _m_guard.redact(usage, _m_guard.never_terms(n))
