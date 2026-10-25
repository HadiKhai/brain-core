"""brain: mechanics for a Brain instance (see core/MODEL.md).

Generic: everything specific is read from the instance (instance.md, node.md files) and from
this machine's bindings (.brain/local.json, never committed).
Judgment work (what goes in a brief, how to split state) lives in core/skills, not here.
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

from . import briefs as _m_briefs
from . import check as _m_check
from . import common as _m_common
from . import connect as _m_connect
from . import guard as _m_guard
from . import gate_lock as _m_gate_lock
from . import history as _m_history
from . import hosts as _m_hosts
from . import instance as _m_instance
from . import introspection as _m_introspection
from . import jobs as _m_jobs
from . import handback_queue as _m_handback_queue
from . import dispatcher as _m_dispatcher
from . import diagnostics as _m_diagnostics
from . import knowledge as _m_knowledge
from . import land_checks as _m_land_checks
from . import messages as _m_messages
from . import machine as _m_machine
from . import freshness as _m_freshness
from . import nodes as _m_nodes
from . import policy as _m_policy
from . import reports as _m_reports
from . import rehearse as _m_rehearse
from . import rotation as _m_rotation
from . import scorecard as _m_scorecard
from . import schedules as _m_schedules
from . import settings_batch as _m_settings_batch
from . import spend as _m_spend
from . import status as _m_status
from . import sync as _m_sync
from . import train as _m_train
from . import transitions as _m_transitions
from . import ui as _m_ui
from . import upgrade as _m_upgrade
from . import usage as _m_usage
from . import verify as _m_verify
from . import worktrees as _m_worktrees
from . import watch as _m_watch


def cmd_selftest(a):
    tests = _m_common.CORE / "tests"
    args = [sys.executable, "-m", "unittest", "discover", "-s", str(tests), "-t", str(tests)]
    if a.verbose:
        args.append("-v")
    if a.k:
        args += ["-k", a.k]
    env = {**os.environ, "BRAIN_CORE": str(_m_common.CORE), "BRAIN_NO_REEXEC": "1"}
    sys.exit(subprocess.run(args, env=env).returncode)


# ---------- main ----------

def take_brain_flag():
    """`-b <name>` / `--brain <name>` anywhere on the command line: run against that brain (sets BRAIN_ROOT,
    which the instance's own core then sees too)."""
    args, out, name, i = sys.argv[1:], [], None, 0
    while i < len(args):
        x = args[i]
        if x == "--":
            out.extend(args[i:])
            break
        if x in ("-b", "--brain") and i + 1 < len(args):
            name, i = args[i + 1], i + 2
            continue
        if x.startswith("--brain="):
            name = x.split("=", 1)[1]
        else:
            out.append(x)
        i += 1
    if name is None:
        return
    brains = dict(_m_instance.brains_here())
    if name not in brains:
        _m_common.die(f"no brain '{name}' on this machine ({', '.join(brains) or 'none yet'}; see: brain brains)")
    os.environ["BRAIN_ROOT"] = str(brains[name])
    sys.argv[1:] = out


def dashboard_all():
    """Every brain's dashboard, each by its own core."""
    for i, (name, root) in enumerate(_m_instance.brains_here()):
        print(f"{'' if i == 0 else chr(10)}===== {name} =====", flush=True)
        subprocess.run([sys.executable, str(root / "core" / "bin" / "brain")],
                       env={**os.environ, "BRAIN_ROOT": str(root), "BRAIN_NO_REEXEC": "1"})


def reexec_into_instance_core():
    """`brain` on PATH points at one core; for another instance, use that instance's own core."""
    if os.environ.get("BRAIN_NO_REEXEC") or (len(sys.argv) > 1 and sys.argv[1] in ("init", "selftest")):
        return
    if len(sys.argv) > 1 and sys.argv[1] == "ui" and (_m_common.CORE / ".git").is_file():
        return  # a core worktree (a dashboard in development) serves its own build
    root = _m_nodes.try_find_root()
    if not root:
        return
    other = root / "core" / "bin" / "brain"
    if other.exists() and other.resolve() != _m_common.ENTRY_POINT.resolve():
        os.environ["BRAIN_NO_REEXEC"] = "1"
        os.execv(sys.executable, [sys.executable, str(other), *sys.argv[1:]])


# Commands that change nothing in the instance: they never wait for the lock.
READ_ONLY = {"cmd_show", "cmd_tree", "cmd_status", "cmd_now", "cmd_jobs", "cmd_stale", "cmd_ui", "cmd_guard",
             "cmd_due", "cmd_inbox", "cmd_index", "cmd_scorecard", "cmd_introspect",
             "cmd_brief", "cmd_hire", "cmd_reports", "cmd_brains", "cmd_use", "cmd_machine", "cmd_usable",
             "cmd_item_list", "cmd_item_show", "cmd_wt_list", "cmd_selftest", "cmd_init", "cmd_hook", "cmd_hook_approve", "cmd_help",
             "cmd_dashboard", "cmd_ready", "cmd_usage", "cmd_scan", "cmd_scrub", "cmd_history",
             "cmd_gate_lock", "cmd_version", "cmd_connect", "cmd_recall", "cmd_proposals", "cmd_changes", "cmd_heavy", "cmd_rehearse", "cmd_policy"}


def writes(a):
    """Does this command, with these options, change anything in the instance?"""
    name = a.f.__name__
    if name == 'cmd_proposals':
        return bool(getattr(a, 'approve_all', False))
    if name == 'cmd_land_check':
        return False  # the explicit runner takes short owned locks around execution
    if name == 'cmd_dispatch':
        return False
    if name == "cmd_watch":
        return False  # takes a short nonblocking instance lock itself
    if name == "cmd_wake":
        # Requests publish with O_EXCL; drains serialize on their own per-node lock.
        # Neither may queue behind an instance writer, or hold it during a wait/send.
        return False
    if name == "cmd_machine":
        return a.action == "sample" or a.yes
    if name == "cmd_status":
        return a.refresh
    if name == "cmd_inbox":
        return a.new
    if name == 'cmd_index':
        return a.record
    if name == 'cmd_due':
        return bool(getattr(a, 'defer', None) or getattr(a, 'ack', None))
    if name == "cmd_changes":
        return a.seen
    if name == "cmd_connect":
        return a.action in (None, "plan") or bool(a.save)
    if name in ("cmd_brief", "cmd_hire", "cmd_history"):
        return bool(getattr(a, "out", None) or getattr(a, "job", None) or
                    (name == "cmd_brief" and getattr(a, "branch", None)))  # Branch briefs also record replay inputs and acknowledge restarts.
    if name == "cmd_check":
        return bool(a.fix)
    if name == "cmd_jobs":
        return a.start or a.close or a.lessons is not None or a.switch is not None or bool(a.contract_file) \
            or bool(a.usage_file)
    if name == "cmd_ready":
        return a.record
    if name in ("cmd_principal", "cmd_schedule"):
        return a.action not in ("list", None)
    return name not in READ_ONLY


def main():
    take_brain_flag()
    reexec_into_instance_core()
    ap = argparse.ArgumentParser(prog="brain", description=__doc__.splitlines()[0])
    ap.add_argument("-b", "--brain", metavar="NAME", help="use this brain (when this machine has several; "
                    "default: the one whose folders you are in, else `brain use`)")
    ap.add_argument("-a", "--all", action="store_true", help="with no command: every brain's dashboard")
    sub = ap.add_subparsers(dest="cmd", metavar="<command>")

    def cmd(name, fn, h, *args):
        s = sub.add_parser(name, help=h, description=h)
        for spec in args:
            names, kw = spec if isinstance(spec, tuple) else ((spec,), {})
            s.add_argument(*names, **kw)
        s.set_defaults(f=fn)
        return s

    opt = lambda *n, **kw: (n, kw)  # noqa: E731

    cmd("rehearse", _m_rehearse.cmd_rehearse, "compare installed and candidate cores on isolated brain copies",
        "id", opt("--branch", required=True))

    cmd("init", _m_nodes.cmd_init, "create a new instance from this core", "dir",
        opt("--operator", required=True), opt("--title", required=True), opt("--runtimes", default="claude,codex"),
        opt("--host", default="t3"), opt("--tz", default="UTC"),
        opt("--name", help="short name for `brain -b <name>` (default: the folder's name)"))
    cmd("usage", _m_usage.cmd_usage, "account-wide subscription usage (agents read the machine cache)",
        opt("action", choices=["install", "uninstall"], nargs="?"), opt("--json", action="store_true"),
        opt("--refresh", action="store_true"), opt("--watch", action="store_true"),
        opt("--every", type=float, default=300, help="poll interval in seconds"),
        opt("--launch-agent", help="install: plist path elsewhere (writes only; does not load it)"),
        opt("--no-load", action="store_true", help="install: write the LaunchAgent without loading it"))
    cmd("new", _m_nodes.cmd_new, "create a node", "id", opt("--title", required=True), opt("--owners", default=""),
        opt("--labels", default=""), opt("--runtime"), opt("--home", help="a path outside the instance"),
        opt("--home-url", dest="home_url"))
    cmd("scan", _m_nodes.cmd_scan, "discover git repos without writing (given folders are depth 0)",
        opt("dirs", nargs="+"), opt("--json", action="store_true"), opt("--depth", type=int, default=4))
    cmd("adopt", _m_nodes.cmd_adopt, "make existing git repositories node homes, then print study commands",
        opt("paths", nargs="+"), opt("--parent", default="root"), opt("--id"), opt("--title"))
    cmd("brief", _m_briefs.cmd_brief, "a worker brief: where it runs, the task, the node's limits, how to ask and hand back",
        "id", opt("--import", dest="import_study", action="store_true", help="study an existing home"),
        opt("--small", action="store_true", help="minutes, low risk: no job ceremony"),
        opt("--branch", help="the job's worktree (brain wt add)"), opt("--task"),
        opt("--task-file", dest="task_file"),
        opt("--context-file", action="append", help="embed shared findings; repeatable; recorded as job inputs on this machine"),
        opt("--why-separate", help="why this concern needs its own agent (or contract ## Why separate)"),
        opt("--allow-unexplained", action="store_true", help="allow an urgent launch without a separation reason"),
        opt("--class", dest="job_class", choices=_m_jobs.JOB_CLASSES),
        opt("--effort", choices=_m_jobs.JOB_EFFORTS),
        opt("--runtime"), opt("--model", help="job model for scoped usage limits"),
        opt("--to", help="the thread the worker reports to (default: the node's coordinator)"),
        opt("--peer", help="shared-commit peer thread; coordinate directly with it"), opt("--out"))
    cmd("scrub", _m_nodes.cmd_scrub, "filter source data without revealing never-name terms; reads paths or stdin",
        "id", opt("paths", nargs="*"), opt("--check", action="store_true"),
        opt("--metadata", action="store_true", help="file names and identities, not source content"))
    cmd("history", _m_history.cmd_history, "past AI conversations about a node's home, in batches readers never compact "
        "away: extract (default), plan, check, or brief <batches>", "id",
        opt("action", nargs="?", choices=["plan", "check", "brief"]), opt("batches", nargs="*"),
        opt("--batch-tokens", dest="batch_tokens", type=int, help="largest batch (one reader's input; default 60k, "
            "or less when a runtime compacted earlier here)"),
        opt("--slice-tokens", dest="slice_tokens", type=int, help="largest slice (one read; default 20k)"),
        opt("--source", action="append", help="only this subscription (repeatable; default: every allowed one)"),
        opt("--fresh", action="store_true", help="discard an earlier extraction and its notes"),
        opt("--technical", action="store_true", help="extract for a technical re-read: readers note only how it's "
            "built, design rules, failures and fixes, conventions, tools and kinds of work"),
        opt("--merge", help="brief: a merger for batches b001-b006"),
        opt("--final", action="store_true", help="brief: fold the merged notes into the node's files"),
        opt("--runtime"), opt("--model", help="reader model for scoped usage limits"),
        opt("--to", help="the thread readers report to (default: the node's coordinator)"), opt("--out"),
        opt("--also", action="append", metavar="FOLDER", help="also sessions run in this folder (repeatable): one "
            "that worked on this node from elsewhere, such as a scratch thread"),
        opt("--json", action="store_true"))
    cmd("connect", _m_connect.cmd_connect, "what nodes share: plan (default) digests of each node below <id>, then check, "
        "brief <node>... for digest readers, brief --final for the connector", "id",
        opt("action", nargs="?", choices=["plan", "check", "brief"]), opt("nodes", nargs="*"),
        opt("--only", help="plan: only these nodes (comma-separated)"),
        opt("--fresh", action="store_true", help="discard an earlier plan and its digests"),
        opt("--final", action="store_true", help="brief: compare the digests and propose"),
        opt("--runtime"), opt("--model", help="reader model for scoped usage limits"),
        opt("--to", help="the thread readers report to (default: the node's coordinator)"),
        opt("--save", action="store_true", help="brief: save it in .brain/connect/<node>/briefs/ and print its path"),
        opt("--json", action="store_true"))
    cmd("recall", _m_knowledge.cmd_recall, "what every project learned that may help with a problem here: "
        "brain recall <node> \"what goes wrong\"", "id", opt("words", nargs="+"),
        opt("--limit", type=int, default=10), opt("--json", action="store_true"))
    cmd("proposals", _m_status.cmd_proposals, "everything waiting for review, node by node, with full text and the commands "
        "to accept or reject several at once", opt("id", nargs="?"),
        opt("--only", help="only these nodes (comma-separated)"), opt("--json", action="store_true"),
        opt('--approve-all', action='store_true', help='activate one complete independently reviewed rules snapshot'),
        opt('--review-manifest', help='complete coordinator-supplied snapshot review JSON'))
    cmd('policy', _m_policy.cmd_policy, 'inspect explicit reviewed publication policy; never publishes', 'id',
        opt('--action', choices=['push', 'pr']), opt('--json', action='store_true', help='print a current limits mapping template'))
    cmd('land-check', _m_land_checks.cmd_land_check, 'record/check exact-candidate evidence; only prints supported push steps', 'id',
        opt('--branch', required=True), opt('--run', action='store_true', help='run the configured check explicitly'),
        opt('--push-step', action='store_true', help='print a policy-allowed SHA-pinned push step with current evidence'))
    cmd("guard", _m_guard.cmd_guard, f"check a home and its worktrees for the terms in {_m_guard.NEVER_FILE} (and, if sealed, "
        "upward traces); exits 1 if any", "id", opt("paths", nargs="*"))
    cmd("spend", _m_spend.cmd_spend, "time and tokens from this machine's transcripts: a node's past sessions (opt-in, "
        "aggregates only, never committed), or every session here", opt("id", nargs="?"),
        opt("--since", help="from this date (YYYY-MM-DD)"), opt("--machine", action="store_true",
                                                               help="every session on this machine, per runtime"),
        opt("--also", action="append", metavar="FOLDER", help="also sessions run in this folder (repeatable)"),
        opt("--json", action="store_true"))
    cmd("ui", _m_ui.cmd_ui, "a live view of this brain in your browser: projects, work in flight, worktrees, activity "
        "(read-only by default, this machine only)", opt("--port", type=int, default=_m_ui.UI_PORT),
        opt("--every", type=float, default=3.0, help="seconds between refreshes"),
        opt("--write", action="store_true", help="allow explicit keyed local proposal reviews as the configured operator"),
        opt("--scan", nargs="+", action="extend", default=[], help="discover import candidates in DIR... (read-only)"),
        opt("--open", action="store_true", help="open it in the browser"),
        opt("--json", action="store_true", help="print the state once, as JSON"))
    cmd("stale", _m_status.cmd_stale, "freshness check: is <id>'s status.md behind a time the host gave (e.g. a thread's "
        "updatedAt, in UTC)?", "id", "host_time", opt("--grace", type=int, default=5, help="minutes allowed"))
    cmd("jobs", _m_jobs.cmd_jobs, "jobs per node: open ones and their age, median hours and review rounds, causes",
        opt("id", nargs="?"), opt("--branch"), opt("--job-key", help="select one ledger job when a branch was reused"), opt("--lessons", help="learned item ids, or none: <reason>"),
        opt("--switch", help="record provider switch: old -> new: reason"),
        opt("--start", action="store_true", help="coordinator: start an import:<snapshot> study"),
        opt("--close", action="store_true", help="coordinator: close a processed study, manually landed job, or parked job with --reason"),
        opt("--reason", help="one-line reason to explicitly close parked work"),
        opt("--live", action="store_true", help="read-only JSON rows joining ledger, hand-back, attempts and recorded queues; host activity unknown"),
        opt("--settled", action="store_true", help="coordinator: record all closed job threads safely settled through the host"),
        opt("--all-closed", action="store_true", help="with --settled: attest a complete host sweep for all closed ledger jobs"),
        opt("--attestation-file", help="fresh version 1 coordinator export with exhaustive per-job/thread/project coverage"),
        opt("--contract-file", dest="contract_file", help="coordinator: the job's contract (" +
            ", ".join(f"## {f}" for f in _m_jobs.CONTRACT_FIELDS) + "; an optional ## Budget, e.g. 20M tokens, 3 h)"),
        opt("--usage-file", dest="usage_file", help="coordinator: the job's usage as the host reports it (JSON); "
            "wins over what transcripts show"))
    cmd("rotate", _m_rotation.cmd_rotate, "prepare a file-based coordinator handover; prints host steps", "id")
    cmd("due", _m_status.cmd_due, "coordinator actions and machine holds; empty means no output",
        opt("--since-wake", action="store_true", help="new file-only wake items compared with the wake before the latest delivery"),
        opt("--machine", action="store_true", help="also show informational machine readings, even without node actions"),
        opt('--defer', nargs=2, metavar=('ITEM_ID', 'UNTIL'), help='defer an eligible reminder until an absolute timestamp'),
        opt('--ack', metavar='ITEM_ID', help='acknowledge a current parked/blocked handback episode'),
        opt('--reason', help='required recorded reason for --defer or --ack'),
        opt('--resume', help='required resume condition for --ack'),
        opt('--until', help='optional absolute resume time for --ack'),
        opt('--handled', action='store_true', help='also show current handled reminders and their reasons'),
        opt("id", nargs="?"), opt("--json", action="store_true"),
        opt("--quiet", action="store_true", help="no output; exit 1 for node actions, including its held work, 0 otherwise"))
    cmd("inbox", _m_status.cmd_inbox, "needs in a node's subtree (default: calling node); --new remembers per reporter",
        opt("id", nargs="?"), opt("--json", action="store_true"), opt("--new", action="store_true"),
        opt('--weekly', action='store_true', help='read-only sweep of unresolved requests aged at least seven days'))
    cmd('answer', _m_status.cmd_answer, 'record text for exactly one current inbox episode, retaining source/history',
        'item_id', 'text')
    cmd('index', _m_freshness.cmd_index, 'configured upstream index freshness; never runs a generator', 'id',
        opt('--record', action='store_true', help='explicitly record current input/output fingerprints after regeneration'))
    cmd("msg", _m_messages.cmd_msg, "format start/done/blocked/question for a coordinator (does not send)",
        "id", opt("kind", choices=_m_messages.MESSAGE_KINDS), "message", opt("--branch"), opt("--report"))
    cmd("handback", _m_jobs.cmd_handback, "done/blocked/question hand-back; worker messages are start/done/blocked/question",
        "id", opt("--branch", required=True), opt("--state", choices=["done", "failed", "blocked", "question"], required=True),
        opt("--summary", required=True, help="nonempty one-line worker result"),
        opt("--question", help="one-line choice, required for question state"),
        opt("--enqueue", action="store_true", help="durably queue this request and return its ID; explicit foreground runner required"),
        opt("--report"), opt("--lessons", help="proposed lessons, or none: <reason>"),
        opt("--restart", choices=["compactions"], help="request a fresh brief resuming this job (blocked only)"),
        opt("--need-human", help="question or blocked need to append to the node inbox"),
        opt("--reviews", type=int, help="completed review rounds; runs verify for done (or reuses a green run that "
            "still proves this commit)"),
        opt("--reverify", action="store_true", help="run verify even when an earlier green run still proves it"))
    cmd("queue", _m_handback_queue.cmd_queue, "explicit foreground hand-back request runner; no service or recovery",
        opt("action", choices=['run']), 'id', opt('--once', action='store_true'), opt('--request-id'))
    cmd("dispatch", _m_dispatcher.cmd_dispatch, "bounded read-only plan of explicit root/operator asks; empty scan is silent",
        opt('--limit', type=int, default=50), opt('--node-limit', type=int, default=200))
    cmd("introspect", _m_introspection.cmd_introspect, "recorded causal job progress and communication metrics",
        "id", opt("--branch"), opt("--hours", type=float, default=24), opt("--limit", type=int, default=50))
    cmd("diagnose", _m_diagnostics.cmd_diagnose, "source-first diagnosis and bounded recorded probe setup permits",
        "id", opt("action", choices=["plan", "outcome"]), opt("--branch", required=True),
        opt("--question", required=True), opt("--source", action="append"),
        opt("--finding", choices=["source", "ambiguous", "runtime"]), opt("--reason"),
        opt("--attempt"), opt("--state", choices=["setup-failed", "observed"]))
    cmd("scorecard", _m_scorecard.cmd_scorecard, "score a supplied evidence window; missing evidence stays unknown",
        "id", opt("--file", required=True, help="version 1 measurement manifest"))
    cmd("show", _m_status.cmd_show, "everything about one node, including this machine's bindings", "id")
    cmd("set", _m_nodes.cmd_set, "set fields: brain set <id> key=value ... (host IDs and home paths stay on this machine)",
        "id", opt("pairs", nargs="+"))
    s = sub.add_parser("settings", help="apply an explicit validated settings batch")
    settings_sub = s.add_subparsers(dest="settings_action", required=True)
    t = settings_sub.add_parser("batch", help="JSON array of set/schedule-add operations; one bookkeeping commit")
    t.add_argument("file", help="UTF-8 JSON batch file")
    t.set_defaults(f=_m_settings_batch.cmd_settings_batch)
    cmd("relabel", _m_nodes.cmd_relabel, "brain relabel <id> key:value ... (key:- removes)", "id", opt("labels", nargs="+"))
    cmd("owners", _m_nodes.cmd_owners, "change owners: brain owners <id> a,b  ('' = same as parent)", "id", "owners")
    cmd("seal", _m_guard.cmd_seal, "give a node new owners and an external home (the sealing procedure's mechanics)",
        "id", opt("--owners", required=True), opt("--home", required=True), opt("--url"),
        opt("--move", nargs="*", help="files or folders (relative to the node) that belong to the new owners"),
        opt("--no-git", action="store_true", dest="no_git"))
    cmd("move", _m_transitions.cmd_move, "brain move <id> <new-parent-id>", "id", "parent")
    cmd("split", _m_transitions.cmd_split, "brain split <id> <new-id> --title T [--move files...]", "id", "new_id",
        opt("--title", required=True), opt("--owners"), opt("--labels"), opt("--runtime"), opt("--move", nargs="*"))
    cmd("merge", _m_transitions.cmd_merge, "brain merge <src> <dst>: fold src into dst (same owners only)", "src", "dst")
    cmd("archive", _m_transitions.cmd_archive, "brain archive <id>", "id")
    cmd("principal", _m_transitions.cmd_principal, "brain principal add <id> | list", opt("action", choices=["add", "list"]),
        opt("id", nargs="?"))
    cmd("revoke", _m_transitions.cmd_revoke, "end the relationship with a principal: quarantine and archive (axiom 21)", "principal")

    s = sub.add_parser("item", help="library items: add, list, scope, quarantine, restore")
    isub = s.add_subparsers(dest="icmd", metavar="<action>")
    t = isub.add_parser("add", help="brain item add <id> --from <node>[,<node>...] --title T --kind K --ref R")
    for n_, kw in [("id", {}), ("--from", {"dest": "source", "required": True}), ("--title", {"required": True}),
                   ("--kind", {"required": True}), ("--ref", {"required": True}), ("--at", {}), ("--scope", {}),
                   ("--consented-by", {"dest": "consented_by", "default": ""}), ("--body", {}),
                   ("--body-file", {"dest": "body_file"}), ("--proposed", {"action": "store_true"}),
                   ("--repo", {}), ("--commit", {}), ("--keywords", {"help": "words someone with this problem would use"})]:
        t.add_argument(n_, **kw)
    t.set_defaults(f=_m_knowledge.cmd_item_add)
    t = isub.add_parser("list", help="brain item list [--node N]  (shows what N may use)")
    t.add_argument("--node")
    t.set_defaults(f=_m_knowledge.cmd_item_list)
    t = isub.add_parser("show", help="read a usable item for a target node")
    t.add_argument("id"); t.add_argument("--node", required=True)
    t.set_defaults(f=_m_knowledge.cmd_item_show)
    t = isub.add_parser("scope", help="brain item scope <id> a,b [--consented-by ...]")
    t.add_argument("id"); t.add_argument("scope"); t.add_argument("--consented-by", dest="consented_by", default="")
    t.set_defaults(f=_m_knowledge.cmd_item_scope)
    for st, name in (("quarantined", "quarantine"), ("active", "restore")):
        t = isub.add_parser(name, help=f"brain item {name} <id>")
        t.add_argument("id"); t.add_argument("--reason")
        t.set_defaults(f=_m_knowledge.cmd_item_quarantine if st == "quarantined" else _m_knowledge.cmd_item_restore)
    t = isub.add_parser("accept", help="accept reviewed proposals (restore never accepts)")
    t.add_argument("id", nargs="+"); t.add_argument("--reviewed-by", required=True, dest="reviewed_by")
    t.set_defaults(f=_m_knowledge.cmd_item_accept)
    t = isub.add_parser("reject", help="reject proposals permanently (unusable)")
    t.add_argument("id", nargs="+")
    t.set_defaults(f=_m_knowledge.cmd_item_reject)
    cmd("usable", _m_knowledge.cmd_usable, "may <node> use <item>? (axiom 10)", "item", "node")

    s = sub.add_parser("specialist", help="specialists: new")
    ssub = s.add_subparsers(dest="scmd", metavar="<action>")
    t = ssub.add_parser("new", help="brain specialist new <id> --title T [--at node] [--runtime r] [--scope a,b]")
    t.add_argument("id"); t.add_argument("--title", required=True); t.add_argument("--at")
    t.add_argument("--runtime"); t.add_argument("--scope")
    t.add_argument("--proposed", action="store_true", help="imported and unreviewed; cannot be hired")
    t.add_argument("--from", dest="source", help="nodes whose work it is drawn from (comma-separated): it lives at "
                   "their common ancestor, scoped to the owners they share")
    t.set_defaults(f=_m_knowledge.cmd_specialist_new)
    t = ssub.add_parser("accept", help="activate reviewed proposed specialists")
    t.add_argument("id", nargs="+"); t.add_argument("--at")
    t.add_argument("--reviewed-by", required=True, dest="reviewed_by")
    t.set_defaults(f=_m_knowledge.cmd_specialist_accept)
    cmd("lesson", _m_knowledge.cmd_lesson, "brain lesson <specialist|node> --from <node> \"text\"", "specialist",
        opt("--from", dest="source", required=True), opt("--at"),
        opt("--node", action="store_true", help="record a correction to a node instead of a specialist"),
        opt("--principle", default="unclassified", help="id in the target's direction.md; default: unclassified"), "text")
    cmd("hire", _m_knowledge.cmd_hire, "print a specialist's brief for a job: brain hire <specialist> <node> --task \"...\"",
        "specialist", "node", opt("--task", required=True), opt("--items", default=""),
        opt("--runtime"), opt("--class", dest="job_class", choices=_m_jobs.JOB_CLASSES),
        opt("--effort", choices=_m_jobs.JOB_EFFORTS),
        opt("--model", help="job model for scoped usage limits"), opt("--out"), opt("--branch"),
        opt("--why-separate", help="why this specialist needs a separate agent; defaults to the contract"),
        opt("--allow-unexplained", action="store_true", help="allow an urgent launch without a reason"),
        opt("--job", help="record this hire for a branch or job id on this machine"))

    cmd("workbench", _m_worktrees.cmd_workbench, "a node's workbench: scripts, reports, worktrees (created if missing)", "id",
        opt("--path", action="store_true"))
    s = sub.add_parser("wt", help="worktrees of an external git home: add, list, rm, prune")
    wsub = s.add_subparsers(dest="wcmd", metavar="<action>")
    t = wsub.add_parser("add", help="brain wt add <node> <branch> [--base REF]: new worktree + setup + port")
    t.add_argument("id"); t.add_argument("branch"); t.add_argument("--base")
    t.add_argument("--no-setup", action="store_true", dest="no_setup")
    t.set_defaults(f=_m_worktrees.cmd_wt_add)
    t = wsub.add_parser("list", help="brain wt list [node]")
    t.add_argument("id", nargs="?")
    t.set_defaults(f=_m_worktrees.cmd_wt_list)
    t = wsub.add_parser("rm", help="brain wt rm <node> <branch> [--force] [--cause 'why it was dropped']")
    t.add_argument("id"); t.add_argument("branch"); t.add_argument("--force", action="store_true")
    t.add_argument("--cause", help="why the job ended without a merge; kept in the node's job-ledger.md")
    t.set_defaults(f=_m_worktrees.cmd_wt_rm)
    t = wsub.add_parser("prune", help="brain wt prune <node>: remove clean worktrees whose branch is merged")
    t.add_argument("id"); t.add_argument("--dry-run", action="store_true", dest="dry_run")
    t.add_argument("--offline", action="store_true")
    t.set_defaults(f=_m_worktrees.cmd_wt_prune)
    cmd("report", _m_reports.cmd_report, "start a report: brain report <node> \"topic\" (prints its folder)", "id", "topic")
    cmd("reports", _m_reports.cmd_reports, "every report, newest first: brain reports [node]", opt("id", nargs="?"))
    cmd("review", _m_sync.cmd_review, "brief for a review by another runtime: brain review <node> --branch B",
        "id", opt("--branch", required=True), opt("--base"), opt("--built-by", dest="built_by"),
        opt("--by", "--runtime", help="reviewer runtime (default: one the builder didn't use)"), opt("--focus"),
        opt("--class", dest="job_class", choices=_m_jobs.JOB_CLASSES),
        opt("--effort", choices=_m_jobs.JOB_EFFORTS),
        opt("--why-separate", help="reason for a separate reviewer; defaults to independent second-runtime review"),
        opt("--allow-unexplained", action="store_true", help="allow an urgent launch without a reason"),
        opt("--model", help="job model for scoped usage limits"), opt("--out"),
        opt("--reason", choices=("security", "schema", "privacy"), help="exception for another full round or round 3+"),
        opt("--check-fix", action="store_true", help="only check prior blockers/majors and fix regressions"),
        opt("--findings", help="prior findings JSON: list of objects with severity and text"),
        opt("--fix-base", help="reviewed commit before the fixes; defaults to previous review head"),
        opt("--contract", help="contract file; defaults to the job's recorded contract"))
    cmd("train", _m_train.cmd_train, "batch ready jobs onto local main, verify once, and print the release command", "id",
        opt("--jobs", nargs="+", help="explicit job order (space or comma separated); all must be ready"))
    cmd("ready", _m_verify.cmd_ready, "check a worker's ready packet against the branch and cached origin default (local main without origin)", "id",
        opt("--branch", required=True), opt("--record", action="store_true", help="worker: run node verify and record"),
        opt("--summary", help="one-line hand-back result (required for --record with schema v3)"),
        opt("--question", help="one-line coordinator choice for a question hand-back"),
        opt("--reviews", type=int, help="completed review rounds, required with --record"),
        opt("--report"), opt("--lessons", help="proposed lessons, or none: <reason>"),
        opt("--reverify", action="store_true", help="with --record: run verify even if a green run still proves it"))
    cmd("schedule", _m_schedules.cmd_schedule, "recurring work: brain schedule list | add <id> --when W --node N --brief B | "
        "bind <id> <host id> | unbind <id>", opt("action", choices=["list", "add", "bind", "unbind"], nargs="?",
                                                  default="list"),
        opt("id", nargs="?"), opt("host_id", nargs="?"), opt("--when"), opt("--node", default="root"), opt("--brief"),
        opt("-v", "--verbose", action="store_true"))
    cmd("sync", _m_sync.cmd_sync, "what changed in a node (and its home) since the last sync; --all: every external home",
        opt("id", nargs="?"), opt("--all", action="store_true"),
        opt("--offline", action="store_true"), opt("--mark", action="store_true"),
        opt("--reset-main", action="store_true", help="rescan from the common ancestor, or last-sync window for unrelated history; one node only"),
        opt("--limit", type=int, default=20, help="uncommitted files to list"))
    s = sub.add_parser("hook", help="runtime hooks: stop (called by the runtime), approve (trust them in Codex)")
    hsub = s.add_subparsers(dest="hcmd", metavar="<action>")
    t = hsub.add_parser("stop", help="called by a runtime when a turn ends; reads the hook's JSON on stdin")
    t.set_defaults(f=_m_hosts.cmd_hook, event="stop")
    t = hsub.add_parser("approve", help="trust this instance and its project hooks in Codex (writes its config)")
    t.set_defaults(f=_m_hosts.cmd_hook_approve)
    cmd("wake", _m_hosts.cmd_wake, "wake a node coordinator (or root); inbox remains the record", "id",
        opt("reason", nargs="?"), opt("--drain", action="store_true", help=argparse.SUPPRESS))
    cmd("watch", _m_watch.cmd_watch, "queue wakes only for new actionable file items",
        opt("--once", action="store_true", required=True))
    cmd("now", _m_status.cmd_now, "the current time in the instance's timezone, as every record writes it (axiom 17)",
        opt("--iso", action="store_true"))
    cmd("tree", _m_status.cmd_tree, "show the node tree")
    cmd("status", _m_status.cmd_status, "status timestamps; --refresh regenerates only one node's facts block",
        opt("id", nargs="?"), opt("--refresh", action="store_true"))
    cmd("registry", _m_status.cmd_registry, "regenerate registry.md")
    cmd("link", _m_hosts.cmd_link, "create runtime links (axiom 22); --command puts 'brain' on your PATH; --global adds a "
        "pointer to this Brain in each runtime's user-wide instructions",
        opt("--command", action="store_true"), opt("--bin-dir", dest="bin_dir", default="~/.local/bin"),
        opt("--global", action="store_true", dest="glob"), opt("--remove-global", action="store_true", dest="unglobal"),
        opt("targets", nargs="*", metavar="RUNTIME=FOLDER",
            help="with --global: the config folders that get this brain's pointer, e.g. claude=~/.claude-work"))
    cmd("check", _m_check.cmd_check, "verify what can be checked mechanically; --fix repairs the safe things",
        opt("--fix", action="store_true"),
        opt("--live", action="store_true", help="send real webhook probes; default check only reads cached results"))
    cmd("gate-lock", _m_gate_lock.cmd_gate_lock, "serialize a command for one node",
        opt("--node", help="node id (default: unambiguous current folder)"),
        opt("--label"), opt("command", nargs=argparse.REMAINDER))
    cmd("heavy", _m_machine.cmd_heavy, "wait until the machine admits new heavy work",
        opt("--node", help="coordinator whose work is waiting (default root)"))
    cmd("machine", _m_machine.cmd_machine, "machine state, sampler and bindings",
        opt("action", nargs="?", choices=["sample", "install", "uninstall", 'wake-setup']), opt('id', nargs='?'),
        opt("--yes", action="store_true", help="install/remove the sampler with operator consent"),
        opt("--missing-homes", action="store_true", dest="missing_homes"))
    cmd("brains", _m_hosts.cmd_brains, "the brains on this machine: brain brains [forget <name>]",
        opt("action", nargs="?", choices=["list", "forget"], default="list"), opt("name", nargs="?"))
    cmd("use", _m_hosts.cmd_use, "set this machine's default brain: brain use <name>", "name")
    cmd("version", _m_upgrade.cmd_version, "installed core SHA/date and cached origin ancestry; never fetches",
        opt("--ref", help="cached origin ref (default origin/HEAD or refs/remotes/origin/main)"),
        opt("--json", action="store_true"))
    cmd("upgrade", _m_upgrade.cmd_upgrade, "update core/ to the latest version, then link and check")
    cmd("changes", _m_upgrade.cmd_changes, "what changed in core for agents: unread entries (default), --all, --seen",
        opt("id", nargs="?"), opt("--seen", action="store_true", help="mark every entry read on this machine"),
        opt("--all", action="store_true"), opt("--json", action="store_true"))
    cmd("selftest", cmd_selftest, "run the core's scenario tests in throwaway folders",
        opt("-v", "--verbose", action="store_true"), opt("-k"))
    def cmd_help(a):
        ap.print_help()
    sub.add_parser("help", help="show this help").set_defaults(f=cmd_help)

    a, extra = ap.parse_known_args()
    # argparse's trailing nargs='*' otherwise rejects `scrub node --check file`.
    if extra:
        if getattr(a, "f", None) == _m_nodes.cmd_scrub and all(not p.startswith("-") for p in extra):
            a.paths.extend(extra)
        else:
            ap.error("unrecognized arguments: " + " ".join(extra))
    if not getattr(a, "f", None):
        if a.cmd in ("item", "specialist", "wt", "hook"):
            sub.choices[a.cmd].print_help()
            return
        if a.all:
            dashboard_all()
        elif not _m_status.cmd_dashboard(a):
            ap.print_help()
        return
    if a.f == _m_nodes.cmd_scan:
        try:
            a.f(a)
            sys.stdout.flush()
        except BrokenPipeError:
            # Read-only discovery may be piped to head; writes must report delivery failure.
            with open(os.devnull, "w") as sink:
                os.dup2(sink.fileno(), sys.stdout.fileno())
        return
    if a.f == _m_worktrees.cmd_wt_add:
        inst = _m_nodes.load()
        node = inst.node(a.id)
        token = _m_machine.machine_wait(inst=inst, node=node["id"])
        _m_machine.machine_release(token)
    if a.f == _m_machine.cmd_machine and a.action == "sample":
        a.f(a)  # sampler and watcher each take a short nonblocking instance lock
        return
    root = _m_nodes.try_find_root() if writes(a) else None
    if root:
        out = _m_common.outward_links(root)
        if out:  # checked before anything is written: no command half-done through a link
            _m_common.die("these links (symbolic or hard) lead out of the brain, and brain writes only inside it: "
                + ", ".join(str(p.relative_to(Path(root).resolve())) if Path(root).resolve() in p.parents else str(p)
                            for p in out[:5]) + ("…" if len(out) > 5 else "") + "; remove them (brain check lists them)")
        with _m_instance.brain_lock(root):
            a.f(a)
    else:
        a.f(a)
