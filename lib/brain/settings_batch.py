"""Explicit settings batches: validate in memory, then publish named paths once."""
import json
import re
import uuid
from pathlib import Path

from . import common as _m_common
from . import instance as _m_instance
from . import nodes as _m_nodes
from . import schedules as _m_schedules
from . import status as _m_status


def plan_settings_batch(inst, operations):
    """No files, commits, bindings or host operations are published during planning."""
    if not isinstance(operations, list) or not operations:
        _m_common.die("settings batch needs a nonempty JSON array of operations")
    nodes = inst.nodes()
    # All consumers, including validation hooks and registry rendering, see earlier operations.
    inst.nodes = lambda: nodes
    planned = {}
    schedule_path = inst.root / "schedules.md"
    schedule_text = schedule_path.read_text() if schedule_path.exists() else _m_schedules.SCHEDULE_PREAMBLE
    schedule_ids = {row["id"] for row in _m_schedules.read_schedules(inst)}
    messages = []
    for operation in operations:
        if not isinstance(operation, dict):
            _m_common.die("each settings batch operation must be an object")
        kind = operation.get("op")
        allowed = {"op", "node", "pairs"} if kind == "set" else {"op", "node", "id", "when", "brief"}
        if kind not in ("set", "schedule-add") or set(operation) - allowed:
            _m_common.die("settings batch supports only set and schedule-add, with their documented fields")
        nid = operation.get("node", "root")
        if not isinstance(nid, str):
            _m_common.die("batch node must be a node id string")
        n = inst.node(nid)
        if kind == "set":
            pairs = operation.get("pairs")
            if not isinstance(pairs, list) or not pairs:
                _m_common.die("batch set needs a nonempty pairs array")
            for pair in pairs:
                if (not isinstance(pair, str) or "=" not in pair or "\n" in pair or "\r" in pair or
                        not re.fullmatch(r"[A-Za-z0-9_:-]+", pair.split("=", 1)[0])):
                    _m_common.die("batch settings need single-line key=value strings with valid field names")
            fm, body = _m_nodes.prepare_settings(inst, n, pairs, messages)
            if fm != n["fm"]:
                planned[n["dir"] / "node.md"] = "---\n" + "\n".join(f"{k}: {v}" for k, v in fm.items()) + "\n---\n" + body
                n["fm"] = fm
                n["external"] = fm.get("home", "here") not in ("here", "none", "")
            n["local"] = inst.local_of(nid)
            n["info"] = {**n["fm"], **n["local"]}
            home = n["fm"].get("home", "here")
            if home == "here":
                n["home_path"] = n["dir"]
            elif home in ("none", ""):
                n["home_path"] = None
            elif home == "external":
                n["home_path"] = Path(n["local"]["home"]).expanduser() if n["local"].get("home") else None
            else:
                n["home_path"] = Path(home).expanduser()
        else:
            if any(isinstance(operation.get(key), str) and any(c in operation[key] for c in "\r\n")
                   for key in ("id", "when", "brief")):
                _m_common.die("batch schedule fields must be single-line strings")
            sid = operation.get("id")
            schedule_text += _m_schedules.prepare_schedule(
                inst, sid, operation.get("when"), nid, operation.get("brief"), schedule_ids)
            schedule_ids.add(sid)
            planned[schedule_path] = schedule_text
    if any(path.name == "node.md" for path in planned):
        planned[inst.root / "registry.md"] = _m_status.registry_text(inst)
    local = json.dumps(inst.local, indent=2, sort_keys=True) + "\n"
    if not inst.local_path.exists() or inst.local_path.read_text() != local:
        planned[inst.local_path] = local
    return planned, messages


def publish_settings_batch(inst, planned, count):
    """Retain a private recovery packet on publication failures; never repair operator data."""
    planned = {path: text for path, text in planned.items() if not path.exists() or path.read_text() != text}
    if not planned:
        return None
    shared = [path for path in planned if ".brain" not in path.relative_to(inst.root).parts]
    for path in planned:
        _m_common.safe_target(path)
        if path.is_symlink():
            _m_common.die("settings batch targets must be regular files, not symlinks")
    if shared:
        rels = [str(path.relative_to(inst.root)) for path in shared]
        dirty = _m_common.git("status", "--porcelain=v1", "--untracked-files=all", "--", *rels, cwd=inst.root)
        if dirty.returncode or dirty.stdout:
            _m_common.die("settings batch targets contain existing edits; commit or move those edits before retrying")
    before = {str(path.relative_to(inst.root)): path.read_text() if path.exists() else None for path in planned}
    after = {str(path.relative_to(inst.root)): text for path, text in planned.items()}
    # Nothing to publish is not a bookkeeping commit.
    if all(before[relative] == text for relative, text in after.items()):
        return None
    folder = inst.root / ".brain" / "settings-batches"
    _m_common.safe_target(folder / "pending.json")
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    packet = folder / (uuid.uuid4().hex + ".json")
    record = {"state": "pending", "base": _m_common.git("rev-parse", "HEAD", cwd=inst.root).stdout.strip(),
              "before": before, "after": after, "operations": count}
    try:
        _m_common.atomic_write(packet, json.dumps(record, indent=2) + "\n")
        for path, text in planned.items():
            path.parent.mkdir(exist_ok=True)
            _m_common.atomic_write(path, text)
        inst.commit_settings_batch(shared, f"settings: batch {count} operations")
        record.update(state="committed" if shared else "applied-local", head=_m_common.git("rev-parse", "HEAD", cwd=inst.root).stdout.strip())
        _m_common.atomic_write(packet, json.dumps(record, indent=2) + "\n")
    except (OSError, SystemExit) as error:
        # The pending record is itself sufficient if updating it fails.
        record["state"] = "failed"
        try:
            _m_common.atomic_write(packet, json.dumps(record, indent=2) + "\n")
        except (OSError, SystemExit):
            pass
        _m_common.die(f"settings batch publication failed; ask the coordinator before recovery. "
                      f"Inspect pending/failure record {packet} and current Git state; no automatic repair. {error}")
    return packet


def cmd_settings_batch(a):
    try:
        operations = json.loads(Path(a.file).expanduser().read_text())
    except (OSError, ValueError) as error:
        _m_common.die(f"cannot read settings batch JSON: {error}")
    inst = _m_instance.Instance(_m_nodes.find_root())
    with _m_instance.brain_lock(inst):
        planned, messages = plan_settings_batch(inst, operations)
        packet = publish_settings_batch(inst, planned, len(operations))
    for message in messages:
        print(message)
    print(f"settings batch: {len(operations)} operations applied" + (f"; receipt {packet}" if packet else "; unchanged"))
