"""Lesson recurrence notices and explicit receipts; never changes direction or lessons."""
import hashlib
import json
import re
from collections import Counter, defaultdict

from . import common as _m_common
from . import instance as _m_instance

_m_guard = _m_common.LazyModule(__package__, 'guard')
_m_knowledge = _m_common.LazyModule(__package__, 'knowledge')


def synthesis_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def synthesis_receipt(folder):
    path = folder / "synthesis.json"
    if not path.exists():
        return {"version": 1, "seen": []}
    value = json.loads(path.read_text())
    if (not isinstance(value, dict) or value.get("version") != 1
            or not isinstance(value.get("seen"), list)
            or any(not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v) for v in value["seen"])):
        raise ValueError("invalid synthesis receipt")
    return value


def synthesis_targets(inst, n):
    yield {"dir": n["dir"], "id": n["id"], "type": "node", "fm": {}}
    # Read this library only: one damaged role in another node must not stop due.
    for path in sorted((n["dir"] / "specialists").glob("*/role.md")):
        target = {"dir": path.parent, "id": path.parent.name, "type": "specialist"}
        try:
            fm = _m_common.read_fm(path)[0]
        except (OSError, ValueError):
            yield {**target, "error": "unreadable specialist role; repair role.md before distilling"}
            continue
        if _m_knowledge.usable(fm, n["owners"])[0]:
            yield {**target, "fm": fm}


def synthesis_sets(inst, n):
    """Group only lessons visible to this holder; hashes retain duplicate occurrences.

    Receipts consume an exact pending set. Three/five fresh corrections are needed
    for another notice, without rewriting historical provenance or accepting rules.
    """
    nodes = inst.nodes()
    out = []
    for target in synthesis_targets(inst, n):
        folder = target["dir"]
        if "error" in target:
            out.append({"kind": "synthesis-state", "text": target["error"]})
            continue
        try:
            receipt = synthesis_receipt(folder)
        except (OSError, ValueError):
            out.append({"kind": "synthesis-state", "text": "unreadable synthesis receipt; repair synthesis.json before distilling"})
            continue
        seen = set(receipt["seen"])
        groups, occurrences = defaultdict(list), Counter()
        try:
            lesson_path = folder / "lessons.md"
            if lesson_path.exists() and not lesson_path.is_file():
                raise ValueError("lessons.md is not a file")
            lessons = _m_knowledge.parse_lessons(lesson_path)
            terms = _m_knowledge.learning_terms(inst, target["fm"], [n], nodes)
        except (OSError, ValueError):
            out.append({"kind": "synthesis-state", "text": "unreadable lessons or source policy; repair evidence before distilling"})
            continue
        for lesson in lessons:
            source = _m_instance.resolve_ref(nodes, lesson["from"])
            if source is None or lesson.get("malformed"):
                continue
            if "*" not in lesson["scope"] and not set(n["owners"]) <= set(lesson["scope"]):
                continue
            if target["type"] == "node":
                current = source
                while current and set(current["owners"]) == set(n["owners"]):
                    if current["id"] == n["id"]:
                        break
                    current = current["parent"]
                else:
                    continue
            try:
                policy = terms + _m_guard.never_terms(source)
                if target["type"] == "node" and source["id"] == n["id"]:
                    # A node may discuss its own terms in its own lessons, as at capture.
                    policy = _m_guard.never_terms(n["parent"]) if n["parent"] else []
            except (OSError, ValueError):
                out.append({"kind": "synthesis-state", "text": "unreadable lesson source policy; repair evidence before distilling"})
                groups.clear()
                break
            payload = "\n".join(lesson[k] for k in ("text", "principle", "enforced_by"))
            if _m_guard.find_terms(payload, policy):
                continue
            fingerprint = synthesis_digest(lesson["line"])
            occurrences[fingerprint] += 1
            token = synthesis_digest([fingerprint, occurrences[fingerprint]])
            if token not in seen:
                groups[lesson["principle"]].append(token)
        for principle, tokens in sorted(groups.items()):
            threshold = 5 if principle == "unclassified" else 3
            if len(tokens) < threshold:
                continue
            set_id = synthesis_digest([n["id"], target["type"], target["id"], principle, sorted(tokens)])
            label = _m_guard.redact(target["id"], terms)
            rule = _m_guard.redact(principle, terms)
            out.append({"kind": "synthesis", "set": set_id, "count": len(tokens),
                        "text": f"synthesis due for {target['type']} {label}: {len(tokens)} lessons for {rule}; "
                                f"use distill, then brain due {n['id']} --synthesis-seen {set_id}",
                        "folder": folder, "tokens": tokens})
    return out


def synthesis_due(inst, n):
    return [{"node": n["id"], **{k: v for k, v in item.items() if k not in ("folder", "tokens")}}
            for item in synthesis_sets(inst, n)]


def synthesis_seen(inst, n, set_id):
    if n["archived"]:
        _m_common.die("archived nodes cannot acknowledge synthesis")
    item = next((s for s in synthesis_sets(inst, n) if s.get("set") == set_id), None)
    if item is None:
        _m_common.die("synthesis set is absent or changed; read brain due again before acknowledging")
    folder = item["folder"]
    receipt = synthesis_receipt(folder)
    receipt["seen"] = sorted(set(receipt["seen"]) | set(item["tokens"]))
    receipt["at"] = inst.now()
    path = folder / "synthesis.json"
    _m_common.atomic_write(path, json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    inst.commit([path], "record synthesis receipt")
    print("synthesis set recorded; direction and lesson records unchanged")
