"""Reports mechanics for Brain."""
import re

from . import common as _m_common
from . import nodes as _m_nodes
from . import worktrees as _m_worktrees


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "report"


def cmd_report(a):
    inst = _m_nodes.load()
    n = inst.node(a.id)
    wb = _m_worktrees.workbench(inst, n, create=True)
    d = wb / "reports" / f"{inst.now_dt().strftime('%Y-%m-%d')}-{slug(a.topic)}"
    d.mkdir(parents=True, exist_ok=True)
    idx = d / "index.md"
    if not idx.exists():
        idx.write_text(
            f"# {a.topic}\n\nCreated: {inst.now()} for {n['id']}\n\n"
            "## Answer\n\n<!-- the conclusion, in a few lines -->\n\n"
            "## Findings\n\n<!-- each with its evidence: the command run, the numbers, the hash or file -->\n\n"
            "## Controls\n\n<!-- what would have failed if the finding were wrong, and whether it did -->\n\n"
            "## Not run\n\n<!-- what wasn't checked, and why; \"none\" only if truly none -->\n\n"
            "## Lessons\n\n<!-- what the next job should know: a pitfall, a tool's quirk, a method that worked; "
            "or \"none: <reason>\". The coordinator turns reusable ones into library items -->\n\n"
            "## Files\n\n## How to reproduce\n\n<!-- the script in scripts/ that made this, and how to run it -->\n")
    print(d)


def cmd_reports(a):
    inst = _m_nodes.load()
    for n in inst.nodes():
        if a.id and n["id"] != a.id:
            continue
        try:
            wb = _m_worktrees.workbench(inst, n)
        except SystemExit:
            continue
        for d in sorted((wb / "reports").glob("*/index.md"), reverse=True) if (wb / "reports").is_dir() else []:
            title = next((l[2:] for l in d.read_text().splitlines() if l.startswith("# ")), d.parent.name)
            print(f"{n['id']:<24} {d.parent.name[:10]}  {title}  ({_m_common.tilde(d.parent)})")


# ---------- schedules ----------
