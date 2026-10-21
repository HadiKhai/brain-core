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
            m = re.match(r"^(when|node|brief):\s*(.*)$", line)
            if m:
                key = m.group(1)
                cur[key] = m.group(2).strip()
            elif key == "brief" and line.strip():
                cur["brief"] += " " + line.strip()
    return out


def cmd_schedule(a):
    inst = _m_nodes.load()
    bound = inst.local.setdefault("schedules", {})
    if a.action == "add":
        if not (a.id and a.when and a.brief):
            _m_common.die("brain schedule add <id> --when \"daily 09:00\" --node <node> --brief \"...\"")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", a.id):
            _m_common.die("schedule ids are lowercase words with dashes")
        if any(x["id"] == a.id for x in read_schedules(inst)):
            _m_common.die(f"schedule '{a.id}' exists")
        inst.node(a.node)
        f = inst.root / "schedules.md"
        head = "" if f.exists() else ("# Schedules\n\nRecurring work. Shared by every machine; each schedule runs on "
                                      "one machine only (`brain schedule list` shows which).\n")
        _m_common.safe_target(f)
        with open(f, "a") as fh:
            fh.write(f"{head}\n## {a.id}\n\nwhen: {a.when}\nnode: {a.node}\nbrief: {a.brief.strip()}\n")
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
        else:
            bound.pop(a.id, None)
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
