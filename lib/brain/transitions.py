"""Transitions mechanics for Brain."""
import re
import shutil
import subprocess
from pathlib import Path

from . import common as _m_common
from . import hosts as _m_hosts
from . import nodes as _m_nodes
from . import status as _m_status


def do_move(inst, nid, parent_id):
    n = inst.node(nid)
    newp = inst.node(parent_id)
    if parent_id == nid or parent_id.startswith(nid + "/"):
        _m_common.die("can't move a node under itself")
    leaf = nid.rsplit("/", 1)[-1]
    new_id = leaf if parent_id == "root" else f"{parent_id}/{leaf}"
    dest = inst.node_dir(new_id)
    if dest.exists():
        _m_common.die(f"{new_id} already exists")
    # A move is not a transfer (axiom 3): pin inherited owners if the new parent's differ.
    fm, body = _m_common.read_fm(n["dir"] / "node.md")
    if not _m_common.lst(fm.get("owners")) and set(n["owners"]) != set(newp["owners"]):
        fm["owners"] = ", ".join(n["owners"])
        _m_common.write_fm(n["dir"] / "node.md", fm, body)
        print(f"  {nid}: owners kept as {fm['owners']} (use 'brain owners' to transfer)")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if (inst.root / ".git").exists():
        subprocess.run(["git", "-C", str(inst.root), "mv", str(n["dir"]), str(dest)], check=True)
    else:
        shutil.move(str(n["dir"]), str(dest))
    _m_common.touch(n["dir"], dest)
    for m in inst.nodes():  # rewrite ids in the moved subtree, and this machine's bindings
        if m["id"] == new_id or m["id"].startswith(new_id + "/"):
            old_id = nid + m["id"][len(new_id):]
            fm, body = _m_common.read_fm(m["dir"] / "node.md")
            fm["id"] = m["id"]
            fm["former_ids"] = ", ".join(_m_common.lst(fm.get("former_ids")) + [old_id])  # items still cite old_id
            _m_common.write_fm(m["dir"] / "node.md", fm, body)
            ag = m["dir"] / "AGENTS.md"
            if ag.exists():
                _m_common.safe_target(ag)
                ag.write_text(ag.read_text().replace(f"Node `{old_id}`", f"Node `{m['id']}`"))
                _m_common.touch(ag)
            if old_id in inst.local["nodes"]:
                inst.local["nodes"][m["id"]] = inst.local["nodes"].pop(old_id)
    inst.save_local()
    return new_id


def cmd_move(a):
    inst = _m_nodes.load()
    before = _m_nodes.snapshot(inst)
    new_id = do_move(inst, a.id, a.parent)
    after = {(a.id + k[len(new_id):] if k == new_id or k.startswith(new_id + "/") else k): v
             for k, v in _m_nodes.snapshot(inst).items()}
    print(f"moved {a.id} -> {new_id}")
    _m_nodes._boundary_report(before, after)
    _m_nodes.resync_views(inst)
    print("  host: update the node's host project folder (see the host adapter)")
    _m_status.write_registry(inst)
    inst.commit([], f"Move {a.id} -> {new_id}")


def git_mv(inst, src, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    tracked = _m_common.git("ls-files", "--error-unmatch", str(src), cwd=inst.root).returncode == 0
    if tracked:
        subprocess.run(["git", "-C", str(inst.root), "mv", str(src), str(dest)], check=True)
    else:
        shutil.move(str(src), str(dest))
    _m_common.touch(src, dest)


def append_inbox(n_dir, who, text, inst):
    inbox_append(n_dir, f"- {inst.now()} from {who}: {text}\n")
    _m_common.touch(n_dir / "inbox.md")


def inbox_append(n_dir, line, marker=None, nonblocking=False):
    """One append, shared by CLI writers and hooks. Hooks never wait for an inbox writer."""
    import fcntl
    target = n_dir / "inbox.md"
    _m_common.safe_target(target)
    with open(target, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        if marker:
            fh.seek(0)
            if marker in fh.read():
                return False
        fh.write(line)
        fh.flush()
        return True


def cmd_split(a):
    inst = _m_nodes.load()
    before = _m_nodes.snapshot(inst)
    src = inst.node(a.id)
    if src["id"] == "root":
        _m_common.die("split a child, not the root")
    d, parent_id, eff, sealed = _m_nodes.create_node(inst, a.new_id, a.title, a.owners or "", a.labels or "",
                                            a.runtime or src["fm"].get("runtime"))
    moved = []
    for rel in a.move or []:
        s = src["dir"] / rel
        if Path(rel).parts[0] in ("node.md", "AGENTS.md", "nodes") or s.is_symlink():
            _m_common.die(f"'{rel}' is the node's record; use 'brain move' for child nodes")
        if not s.exists():
            _m_common.die(f"no '{rel}' in {src['dir']}")
        git_mv(inst, s, d / rel)
        moved.append(rel)
    note = f"split: '{a.new_id}' created from '{a.id}'" + (f", moved {', '.join(moved)}" if moved else "")
    append_inbox(src["dir"], "brain", note, inst)
    append_inbox(d, "brain", note + "; write this node's brief.md", inst)
    _m_hosts.link_runtimes(inst)
    _m_status.write_registry(inst)
    inst.commit([], f"Split {a.new_id} from {a.id}")
    print(f"split: {a.id} -> {a.id} + {a.new_id}{'  (sealed)' if sealed else ''}")
    _m_nodes._boundary_report(before, _m_nodes.snapshot(inst))
    print("  items keep their provenance (axiom 19)")


def cmd_merge(a):
    inst = _m_nodes.load()
    src, dst = inst.node(a.src), inst.node(a.dst)
    if "root" in (src["id"], dst["id"]) or src["id"] == dst["id"]:
        _m_common.die("merge two different non-root nodes")
    if dst["id"].startswith(src["id"] + "/"):
        _m_common.die(f"{dst['id']} is inside {src['id']}")
    if set(src["owners"]) != set(dst["owners"]):
        _m_common.die(f"owners differ ({','.join(src['owners'])} vs {','.join(dst['owners'])}); "
            f"change owners first so no boundary is crossed silently")
    before = _m_nodes.snapshot(inst)
    for c in [m for m in inst.nodes() if m["parent"] and m["parent"]["id"] == src["id"]]:
        new = do_move(inst, c["id"], dst["id"])
        print(f"  child {c['id']} -> {new}")
    src = inst.node(a.src)
    leaf = src["id"].rsplit("/", 1)[-1]
    target = dst["dir"] / "sources" / f"merged-{leaf}"
    for entry in sorted(src["dir"].iterdir()):
        if entry.name in ("node.md", "nodes") or entry.is_symlink() or entry.name == ".DS_Store":
            continue
        git_mv(inst, entry, target / entry.name)
    fm, body = _m_common.read_fm(dst["dir"] / "node.md")
    fm["merged_from"] = ", ".join(_m_common.lst(fm.get("merged_from")) + [src["id"]])
    _m_common.write_fm(dst["dir"] / "node.md", fm, body)
    old_local = inst.local["nodes"].pop(src["id"], {})
    inst.save_local()
    shutil.rmtree(src["dir"])
    _m_common.touch(src["dir"])
    append_inbox(dst["dir"], "brain", f"merged '{src['id']}' into this node: fold "
                 f"sources/merged-{leaf}/ (its brief, status, decisions, rules) into this node's files", inst)
    _m_nodes.resync_views(inst)
    _m_status.write_registry(inst)
    inst.commit([], f"Merge {src['id']} into {dst['id']}")
    print(f"merged {src['id']} into {dst['id']}; its files are in {_m_common.tilde(target)}")
    _m_nodes._boundary_report(before, _m_nodes.snapshot(inst))
    if old_local.get("coordinator"):
        print(f"  stop its coordinator thread: {inst.thread_link(old_local['coordinator'])}")
    print("  items keep source_node; node.md merged_from resolves it (axiom 19)")


def cmd_archive(a):
    a.labels = ["state:archived"]
    _m_nodes.cmd_relabel(a)
    inst = _m_nodes.load()
    c = inst.node(a.id)["local"].get("coordinator")
    if c:
        print(f"  final status, then stop its coordinator thread: {inst.thread_link(c)}")


# ---------- principals ----------

def cmd_principal(a):
    inst = _m_nodes.load()
    if a.action == "list":
        for p in inst.principals:
            tag = " (operator)" if p == inst.cfg.get("operator") else (" (revoked)" if p in inst.revoked else "")
            print(f"{p}{tag}")
        return
    if not a.id or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", a.id):
        _m_common.die("principal ids are lowercase words with dashes")
    if a.id in inst.principals:
        _m_common.die(f"'{a.id}' is already a principal")
    cfg, body = _m_common.read_fm(inst.root / "instance.md")
    cfg["principals"] = ", ".join(inst.principals + [a.id])
    _m_common.write_fm(inst.root / "instance.md", cfg, body)
    inst.commit([inst.root / "instance.md"], f"Add principal {a.id}")
    print(f"principals: {cfg['principals']}")


# ---------- revoke (axiom 21) ----------

def cmd_revoke(a):
    inst = _m_nodes.load()
    p = a.principal
    if p not in inst.principals:
        _m_common.die(f"unknown principal '{p}'")
    if p == inst.cfg.get("operator"):
        _m_common.die("the operator can't be revoked")
    if p in inst.revoked:
        _m_common.die(f"'{p}' is already revoked")
    quarantined, archived, shared = [], [], []
    for it in inst.items():
        scope = set(_m_common.lst(it["fm"].get("scope")))
        if "*" not in scope and scope and not (scope - {p}) and it["fm"].get("status") != "quarantined":
            fm, body = _m_common.read_fm(it["path"])
            fm["status_before_quarantine"] = fm.get("status", "active")
            fm["status"] = "quarantined"
            fm["quarantined"] = f"{inst.now()}, revoked {p}"
            _m_common.write_fm(it["path"], fm, body)
            quarantined.append(fm.get("id"))
    for n in inst.nodes():
        if p in n["owners"] and n["parent"] and not n["archived"]:
            if set(n["owners"]) == {p}:
                fm, body = _m_common.read_fm(n["dir"] / "node.md")
                labels = [l for l in _m_common.lst(fm.get("labels")) if not l.startswith("state:")] + ["state:archived"]
                fm["labels"] = ", ".join(labels)
                _m_common.write_fm(n["dir"] / "node.md", fm, body)
                archived.append(n["id"])
            else:
                shared.append(n["id"])
    cfg, body = _m_common.read_fm(inst.root / "instance.md")
    cfg["revoked"] = ", ".join(_m_common.lst(cfg.get("revoked")) + [p])
    _m_common.write_fm(inst.root / "instance.md", cfg, body)
    _m_status.write_registry(inst)
    inst.commit([], f"Revoke {p}")
    print(f"revoked {p}")
    print(f"  quarantined items: {', '.join(quarantined) or 'none'}")
    print(f"  archived nodes: {', '.join(archived) or 'none'}  (each: final status, stop its threads)")
    if shared:
        print(f"  still co-owned with {p}: {', '.join(shared)} -> decide new owners with 'brain owners'")


# ---------- items and scope (axioms 10, 11) ----------
