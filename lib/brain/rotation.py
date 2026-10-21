"""File-based coordinator rotation. Host actions always remain with the coordinator."""
import json
import re
import urllib.parse

from . import common as _m_common
from . import guard as _m_guard
from . import jobs as _m_jobs
from . import nodes as _m_nodes
from . import schedules as _m_schedules
from . import spend as _m_spend


def rotation_due(inst, n):
    if not n["local"].get("coordinator"):
        return None
    measured = _m_spend.coordinator_measure(inst, n)
    if not measured:
        return None
    def limit(key, default):
        try:
            return max(1, int(n["fm"].get(key, default)))
        except (ValueError, TypeError):
            return default
    context, turns = measured["context"], measured["turns"]
    if (context is not None and context >= limit("rotate_tokens", 150000)) or turns >= limit("rotate_turns", 50):
        return (f"coordinator context {context if context is not None else 'unknown'} tokens, {turns} turns; "
                f"run brain rotate {n['id']} and follow the host handover steps")
    return None


def cmd_rotate(a):
    inst = _m_nodes.load()
    n = inst.node(a.id)
    if _m_common.outward_links(inst.root):
        _m_common.die("rotation refused: unsafe instance links; repair before preparing a handover")
    rows = _m_jobs.read_jobs(n)
    if rows is None:
        _m_common.die("rotation refused: unreadable job ledger; reconcile it before rotating")
    local_file = inst.root / ".brain" / "rotation" / (urllib.parse.quote(n["id"], safe="") + ".json")
    bindings = {"coordinator": n["local"].get("coordinator", ""),
                "coordinator_sessions": n["local"].get("coordinator_sessions", []),
                "worktrees": {}, "schedules": {}}
    lines = [f"# Coordinator handover: {n['id']}", "", f"Prepared: {inst.now()}", "",
             "Start from these files. Read node.md, brief.md and the coordinator role before acting.",
             "The old thread must stay available until host tasks and pending requests have been reconciled.", ""]
    for name in ("status.md", "decisions.md", "backlog.md", "inbox.md"):
        path = n["dir"] / name
        try:
            body = path.read_text() if path.exists() else "(not created)"
        except (OSError, UnicodeError):
            _m_common.die(f"rotation refused: cannot read {name}")
        lines += [f"## {name}", "", body, ""]
    lines += ["## Open jobs and hand-backs", ""]
    for row in rows:
        if row["Ended"]:
            continue
        lines += ["### " + row["Job"], "", json.dumps(row, sort_keys=True), ""]
        worktree = n["local"].get("worktrees", {}).get(row["Job"], {})
        bindings["worktrees"][row["Job"]] = {k: worktree[k] for k in ("path", "port", "runtime") if k in worktree}
        path = _m_jobs.handback_file(n, row)
        signal = _m_jobs.read_handback(n, row)
        if path.exists() and signal is None:
            _m_common.die("rotation refused: unreadable open-job hand-back")
        lines += [json.dumps(signal, sort_keys=True) if signal else "No hand-back recorded.", ""]
    lines += ["## Schedules", ""]
    for schedule in _m_schedules.read_schedules(inst):
        if schedule.get("node") == n["id"]:
            lines += [json.dumps(schedule, sort_keys=True), ""]
            bindings["schedules"][schedule["id"]] = inst.local.get("schedules", {}).get(schedule["id"])
    # Wake capabilities never enter the packet or sidecar. Pending reason files contain no text.
    directory = inst.root / ".brain" / "wake" / urllib.parse.quote(n["id"], safe="")
    pending = (sorted(p.stem for p in directory.glob("*.pending") if re.fullmatch(r"[0-9a-f]{64}", p.stem))
               if directory.is_dir() else [])
    bindings["pending_wakes"] = pending
    lines += ["## Wakes and host reconciliation", "",
              "Previous coordinator, worktree and host-task bindings are in the ignored machine-local sidecar "
              f"`.brain/rotation/{local_file.name}`. Inspect it before changing bindings.",
              f"Pending wakes: {len(pending)} (digests in the local sidecar).",
              "Wake transport configured: " + ("yes" if n["local"].get("wake") or
                                               inst.local_of("root").get("wake_default") else "no") + ".",
              "Inspect machine-local wake bindings without copying capability URLs into notes.",
              "Enumerate host schedules, webhook tasks and pending requests on the old coordinator and all open workers; "
              "the CLI cannot see unrecorded host state. Reconcile each before stopping the old thread.", ""]
    path = n["dir"] / "handover.md"
    _m_common.safe_target(path)
    _m_common.safe_target(local_file)
    local_file.parent.mkdir(parents=True, exist_ok=True)
    _m_common.atomic_write(local_file, json.dumps(bindings, indent=2) + "\n")
    _m_common.atomic_write(path, _m_guard.redact("\n".join(lines), _m_guard.never_terms(n)))
    inst.commit([path], f"{n['id']}: prepare coordinator handover")
    print(f"handover: {_m_common.tilde(path)}")
    print("1. Launch a fresh coordinator thread in the node's project with this handover.")
    print(f"2. brain set {n['id']} coordinator=<new>; append its runtime session id to coordinator_sessions=<old ids>,<new session id>.")
    print("3. Rebind every schedule and wake task; reconcile host pending requests and open workers.")
    print("4. Tell the old thread to stop, confirm it has stopped, then settle it through the host.")
