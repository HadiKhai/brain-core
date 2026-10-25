"""Schedules mechanics for Brain."""
import re

from . import common as _m_common
from . import nodes as _m_nodes


SCHEDULE_FIELDS = ["when", "node", "brief"]


def read_schedules(inst):
    """schedules.md: `## <id>` sections with `when:`, `node:` and `brief:` (the brief may run on). Shared state:
    every machine sees every schedule. Which machine runs one is that machine's binding (axiom 23)."""
    f = inst.root / "schedules.md"
    out, cur, key = [], None, None
    if not f.exists():
        return out
    for line in f.read_text().splitlines():
        if line.startswith("## "):
            cur = {"id": line[3:].strip()}
            out.append(cur)
            key = None
        elif cur is not None:
            m = re.match(r"^(when|node|provider|brief):\s*(.*)$", line)
            if m:
                key = m.group(1)
                cur[key] = m.group(2).strip()
            elif key == "brief" and line.strip():
                cur["brief"] += " " + line.strip()
    return out


def prepare_schedule(inst, sid, when, node, brief, existing=None, provider=None):
    """Validate an addition and return its text, without creating files or bindings."""
    if not all(isinstance(x, str) and x.strip() for x in (sid, when, node, brief)):
        _m_common.die("schedule add needs id, when, node and brief strings")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", sid):
        _m_common.die("schedule ids are lowercase words with dashes")
    existing = {x["id"] for x in read_schedules(inst)} if existing is None else existing
    if sid in existing:
        _m_common.die(f"schedule '{sid}' exists")
    inst.node(node)
    if provider is not None and provider not in inst.runtimes:
        _m_common.die("provider must be an instance runtime: " + ", ".join(inst.runtimes))
    declared = f"provider: {provider}\n" if provider else ""
    return f"\n## {sid}\n\nwhen: {when}\nnode: {node}\n{declared}brief: {brief.strip()}\n"


SCHEDULE_PREAMBLE = ("# Schedules\n\nRecurring work. Shared by every machine; each schedule runs on "
                     "one machine only (`brain schedule list` shows which).\n")


def schedule_provider(inst, schedule):
    """An explicit host runtime, never inferred from task ids or the node's pin.

    A machine's binding overrides the shared declaration. Keep legacy string task
    ids intact so existing schedule, machine and rotation readers still work.
    """
    sid = schedule["id"]
    if inst.local.get("schedules", {}).get(sid):
        provider = inst.local.get("schedule_providers", {}).get(sid)
        if provider:
            return provider
    return schedule.get("provider")


def cmd_schedule(a):
    inst = _m_nodes.load()
    bound = inst.local.setdefault("schedules", {})
    provider = getattr(a, "provider", None)
    if provider is not None:
        if a.action not in ("add", "bind"):
            _m_common.die("--provider is only used with schedule add or bind")
        if provider not in inst.runtimes:
            _m_common.die("provider must be an instance runtime: " + ", ".join(inst.runtimes))
    if a.action == "add":
        addition = prepare_schedule(inst, a.id, a.when, a.node, a.brief, provider=provider)
        f = inst.root / "schedules.md"
        head = "" if f.exists() else SCHEDULE_PREAMBLE
        _m_common.safe_target(f)
        with open(f, "a") as fh:
            fh.write(head + addition)
        _m_common.touch(f)
        inst.commit([f], f"schedule: {a.id}")
        print(f"{a.id}: added; create it in the host, then: brain schedule bind {a.id} <host id>")
        return
    if a.action in ("bind", "unbind"):
        if not any(x["id"] == a.id for x in read_schedules(inst)):
            _m_common.die(f"no schedule '{a.id}' in schedules.md")
        if a.action == "bind":
            if not a.host_id:
                _m_common.die("brain schedule bind <id> <host schedule id>")
            bound[a.id] = a.host_id
            if provider:
                inst.local.setdefault("schedule_providers", {})[a.id] = provider
            else:
                inst.local.get("schedule_providers", {}).pop(a.id, None)
        else:
            bound.pop(a.id, None)
            inst.local.get("schedule_providers", {}).pop(a.id, None)
        inst.save_local()
        print(f"{a.id}: {'runs on this machine (' + a.host_id + ')' if a.action == 'bind' else 'not on this machine'}")
        return
    for x in read_schedules(inst):
        here = bound.get(x["id"])
        print(f"{x['id']:<20} {x.get('when', '?'):<16} node {x.get('node', '?'):<16} "
              f"{'on this machine: ' + here if here else 'not on this machine'}")
        if a.verbose:
            print(f"  brief: {x.get('brief', '')}")


# ---------- views ----------
