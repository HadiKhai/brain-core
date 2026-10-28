"""brain: mechanics for a Brain instance (see core/MODEL.md).

Generic: everything specific is read from the instance (instance.md, node.md files) and from
this machine's bindings (.brain/local.json, never committed).
Judgment work (what goes in a brief, how to split state) lives in core/skills, not here.
"""
import argparse
import importlib
import os
import subprocess
import sys
from pathlib import Path

from . import common as _m_common
from . import instance as _m_instance
from . import timings as _m_timings
from . import command_reads as _m_command_reads

# Parsing argv must not compile the whole core: handler modules load at dispatch.
_m_events = _m_common.LazyModule(__package__, 'events')
_m_nodes = _m_common.LazyModule(__package__, 'nodes')
_m_status = _m_common.LazyModule(__package__, 'status')
_m_status_refresh = _m_common.LazyModule(__package__, 'status_refresh')
_m_tasks = _m_common.LazyModule(__package__, 'tasks')


class _Command:
    """Resolve only the selected handler, through its real owning module.

    Do not retain a function here: fixture patches and repeated main() calls must
    see the current module attribute, using the active package's normal imports.
    """
    def __init__(self, module, name):
        self.module = module
        self.__name__ = name

    def resolve(self):
        module = importlib.import_module('.' + self.module, __package__)
        return getattr(module, self.__name__)

    def __call__(self, args):
        return self.resolve()(args)


def cmd_selftest(a):
    tests = _m_common.CORE / "tests"
    jobs = getattr(a, 'jobs', 1)
    if type(jobs) is not int or not 1 <= jobs <= 32:
        _m_common.die('selftest --jobs must be between 1 and 32')
    if jobs == 1:
        args = [sys.executable, "-m", "unittest", "discover", "-s", str(tests), "-t", str(tests)]
    else:
        args = [sys.executable, '-m', 'pytest', '-p', 'no:cacheprovider', '-n', str(jobs),
                '--max-worker-restart=0', '--dist=worksteal', str(tests / 'test_brain.py')]
    if a.verbose:
        args.append("-v")
    elif jobs > 1:
        args.append('-q')
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
    if len(sys.argv) > 1 and sys.argv[1] == 'ui' and (_m_common.CORE / '.git').is_file():
        return  # an explicitly selected development worktree executes its own build
    root = _m_nodes.try_find_root()
    if not root:
        return
    other = root / "core" / "bin" / "brain"
    # A staged core's launcher already runs that instance's pinned release (core_releases.py).
    if other.exists() and other.resolve() not in (_m_common.ENTRY_POINT.resolve(), Path(sys.argv[0]).resolve()):
        if (_m_common.CORE / '.git').is_file():
            options = argparse.ArgumentParser(add_help=False)
            options.add_argument('-a', '--all', action='store_true')
            options.add_argument('command', nargs='?')
            if options.parse_known_args(sys.argv[1:])[0].command == 'rehearse':
                _m_common.die(f'rehearse: refusing instance-core re-execution from worktree core '
                              f'{_m_common.CORE} into installed core {other.parent.parent}; '
                              'set BRAIN_NO_REEXEC=1 explicitly to rehearse with the worktree core')
        os.environ["BRAIN_NO_REEXEC"] = "1"
        os.execv(sys.executable, [sys.executable, str(other), *sys.argv[1:]])


# Commands that change nothing in the instance: they never wait for the lock.
READ_ONLY = {"cmd_show", "cmd_tree", "cmd_status", "cmd_now", "cmd_jobs", "cmd_settle", "cmd_stale", "cmd_ui", "cmd_guard",
             "cmd_due", "cmd_inbox", "cmd_timings", "cmd_time", "cmd_index", "cmd_scorecard", "cmd_introspect",
             "cmd_brief", "cmd_hire", "cmd_reports", "cmd_brains", "cmd_use", "cmd_machine", "cmd_usable",
             "cmd_item_list", "cmd_item_show", "cmd_wt_list", "cmd_selftest", "cmd_init", "cmd_hook", "cmd_hook_pretool", "cmd_hook_approve", "cmd_help",
             "cmd_dashboard", "cmd_ready", "cmd_usage", "cmd_scan", "cmd_scrub", "cmd_history",
             "cmd_digest", "cmd_model_fit", "cmd_launch_check", "cmd_events", "cmd_gate_lock", "cmd_version", "cmd_connect", "cmd_recall", "cmd_proposals", "cmd_changes", "cmd_heavy", "cmd_heavy_run", "cmd_rehearse", "cmd_policy", "cmd_preview", "cmd_preview_receipt_skeleton", "cmd_task_apply_plan"}


def writes(a):
    """Does this command, with these options, change anything in the instance?"""
    name = a.f.__name__
    if name == "cmd_spend" and getattr(a, "add_source", None):
        return False  # local source registration owns a short lock; no node/event writes
    if name == "cmd_host_instance":
        return a.action in ("remember", "route")
    if name == "cmd_events":
        return bool(a.explain or a.record or a.rebuild_records)
    if name == "cmd_task_apply_plan":
        return not a.dry_run
    if name == "cmd_wt_prune":
        return not a.dry_run
    if name == 'cmd_preview':
        return bool(a.record)
    if name == "cmd_task_flow":
        return a.action in ("attempt", "alias", "reconcile")
    if name == "cmd_task_provider":
        return True
    if name == "cmd_task_sources":
        return a.action == "rule-add"
    if name == "cmd_task_migrate":
        return True
    if name == 'cmd_records':
        return a.action == 'reconcile-records' and a.apply
    if name == "cmd_task_backfill":
        return a.apply
    if name == "cmd_requirement":
        return True
    if name == "cmd_task":
        return a.action in ("add", "set", "backfill", "sync", "event", "requirement-add")
    if name == 'cmd_proposals':
        return bool(getattr(a, 'approve_all', False))
    if name in ('cmd_full_run', 'cmd_spec_run', 'cmd_gate', 'cmd_full_isolate', 'cmd_full_quarantine'):
        return False  # native execution owns short locks and machine admission
    if name == 'cmd_land_check':
        return False  # the explicit runner takes short owned locks around execution
    if name == 'cmd_host':
        return False  # host calls use short local binding transactions
    if name == 'cmd_lifecycle':
        return False  # native RPC stays outside the instance lock; publication takes its own short locks
    if name in ('cmd_dispatch', 'cmd_host_export', 'cmd_delegate'):
        return False
    if name == "cmd_watch":
        return False  # takes a short nonblocking instance lock itself
    if name == "cmd_wake":
        # Requests publish with O_EXCL; drains serialize on their own per-node lock.
        # Neither may queue behind an instance writer, or hold it during a wait/send.
        return False
    if name == 'cmd_gate_queue' and a.action in ('wait', 'drain'):
        return False  # receipt reads / applier-owned per-record transactions
    if name == "cmd_machine":
        return a.action != "queue-service" and (a.action == "sample" or a.yes)
    if name == "cmd_status":
        return a.refresh or getattr(a, "project", False)
    if name == "cmd_inbox":
        return bool(getattr(a, "add", None))
    if name == 'cmd_index':
        return a.record or bool(getattr(a, "refresh", False))
    if name == "cmd_teach":
        return bool(a.file)
    if name == "cmd_feedback":
        return a.action != "list"
    if name == "cmd_enforcement":
        return a.sync_tasks
    if name == 'cmd_due':
        return bool(getattr(a, 'synthesis_seen', None) or getattr(a, 'defer', None) or getattr(a, 'ack', None))
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
        return a.start or a.close or bool(getattr(a, "settled", False)) or a.lessons is not None or a.switch is not None or bool(a.contract_file) \
            or bool(a.usage_file) or bool(getattr(a, 'rebind', None)) or bool(getattr(a, '_reconcile_landings', False))
    if name == "cmd_ready":
        return a.record
    if name in ("cmd_principal", "cmd_schedule"):
        return a.action not in ("list", None)
    return name not in READ_ONLY


def main():
    # Admission must not pay for the full command profiler/accounting lifecycle.
    # Source/application commands retain the normal measurement path below.
    take_brain_flag()
    reexec_into_instance_core()
    candidate = fast_write_arguments()
    if candidate is not None:
        root = _m_nodes.try_find_root()
        if root:
            from . import handback_queue
            try:
                with _m_timings.queue_phase(root, 'enqueue') as timing:
                    accepted = timing['publish'] = handback_queue.fast_intent(candidate, root)
                if accepted:
                    if callable(accepted):
                        accepted()  # the default handback's receipt wait is not admission time
                    return
            except (OSError, ValueError) as error:
                _m_common.die(str(error))
    # A long-lived applier must not retain command profiler/accounting caches.
    if sys.argv[1:] == ['machine', 'queue-service', 'run']:
        from . import write_queue_service
        write_queue_service.run_service(_m_instance.Instance(_m_nodes.find_root()))
        return
    with _m_timings.command(persist=False), _m_timings.phase('dispatch'):
        _main()


def command_read(a):
    """Commands whose declared read effect includes a private observation snapshot."""
    name = a.f.__name__
    return (name in ('cmd_due', 'cmd_inbox', 'cmd_status', 'cmd_jobs') or
            (name == 'cmd_ui' and getattr(a, 'json', False) and not getattr(a, 'scan', None))) and (
            not writes(a) or (name == 'cmd_inbox' and not getattr(a, 'add', None)))


def queueable_command(cmd, name):
    """The same public option schema for full dispatch and the admission parser."""
    opt = lambda *n, **kw: (n, kw)  # noqa: E731
    if name == 'inbox':
        return cmd("inbox", _Command("status", "cmd_inbox"), "needs in a node's subtree (default: calling node); --new remembers per reporter",
        opt("id", nargs="?"), opt("--json", action="store_true"), opt("--new", action="store_true"),
        opt('--add', help='append a screened inbox item'),
        opt('--request-id', help='retry --add with the same payload-bound ID; queue wait ID before dependent reads'),
        opt('--weekly', action='store_true', help='read-only sweep of unresolved requests aged at least seven days'))
    return cmd("handback", _Command("jobs", "cmd_handback"), "done/blocked/question hand-back; worker messages are start/done/blocked/question",
        "id", opt("--branch", required=True), opt("--state", choices=["done", "failed", "blocked", "question"], required=True),
        opt("--summary", required=True, help="nonempty one-line worker result"),
        opt("--question", help="one-line choice, required for question state"),
        opt("--enqueue", action="store_true", help="return a request ID; queue wait ID returns the native checked verdict"),
        opt("--direct", action="store_true", help="apply in this process instead of through the write queue"),
        opt("--wait", type=float, metavar="SECONDS",
            help="when a contended unchecked handback queues: wait this long for its receipt (default 4)"),
        opt('--request-id', help='retry a queued handback using the same payload-bound ID'),
        opt('--record', action='store_true', help='with done --reviews N: record native readiness'),
        opt("--report"), opt("--lessons", help="proposed lessons, or none: <reason>"),
        opt("--restart", choices=["compactions"], help="request a fresh brief resuming this job (blocked only)"),
        opt("--need-human", help="question or blocked need to append to the node inbox"),
        opt("--reviews", type=int, help="completed review rounds; runs verify for done (or reuses a green run that "
            "still proves this commit)"),
        opt("--full-receipt", help="adopt a native exact-candidate full-run receipt without rerunning"),
        opt("--reverify", action="store_true", help="run verify even when an earlier green run still proves it"))


def fast_write_arguments():
    """Avoid building unrelated command parsers on the measured producer path."""
    argv = sys.argv[1:]
    if not argv:
        return None
    name = argv[0]
    if name == 'task' and len(argv) > 1 and argv[1] == 'set':
        parser = argparse.ArgumentParser(prog='brain task set')
        _m_tasks.patch_parser(parser)
        parser.set_defaults(f=_m_tasks.cmd_task, action='set', cmd='task')
        return parser.parse_args(argv[2:])
    if not (name == 'inbox' and any(a == '--add' or a.startswith('--add=') for a in argv[1:])
            or name == 'handback' and '--direct' not in argv[1:]):
        return None
    def selected(command, fn, help_text, *specs):
        parser = argparse.ArgumentParser(prog='brain ' + command, description=help_text)
        for spec in specs:
            names, kw = spec if isinstance(spec, tuple) else ((spec,), {})
            parser.add_argument(*names, **kw)
        parser.set_defaults(f=fn, cmd=command)
        return parser
    return queueable_command(selected, name).parse_args(argv[1:])


def _main():
    take_brain_flag()
    reexec_into_instance_core()
    ap = argparse.ArgumentParser(prog="brain", description=__doc__.splitlines()[0])
    ap.add_argument("-b", "--brain", metavar="NAME", help="use this brain (when this machine has several; "
                    "default: the one whose folders you are in, else `brain use`)")
    ap.add_argument("-a", "--all", action="store_true", help="with no command: every brain's dashboard")
    sub = ap.add_subparsers(dest="cmd", metavar="<command>")

    _m_tasks.register(sub)

    def cmd(name, fn, h, *args):
        s = sub.add_parser(name, help=h, description=h)
        for spec in args:
            names, kw = spec if isinstance(spec, tuple) else ((spec,), {})
            s.add_argument(*names, **kw)
        s.set_defaults(f=fn)
        return s

    opt = lambda *n, **kw: (n, kw)  # noqa: E731

    cmd("rehearse", _Command("rehearse", "cmd_rehearse"), "compare installed and candidate cores on isolated brain copies",
        "id", opt("--branch", required=True))

    cmd("init", _Command("nodes", "cmd_init"), "create a new instance from this core", "dir",
        opt("--operator", required=True), opt("--title", required=True), opt("--runtimes", default="claude,codex"),
        opt("--host", default="t3"), opt("--tz", default="UTC"),
        opt("--name", help="short name for `brain -b <name>` (default: the folder's name)"))
    cmd("model-fit", _Command("model_fit", "cmd_model_fit"), "learn model recommendations from comparable recorded jobs", "id",
        opt("--class", dest="job_class", choices=_m_common.JOB_CLASSES, required=True),
        opt("--runtime", required=True), opt("--effort", choices=_m_common.JOB_EFFORTS, default="high"))
    cmd("launch-check", _Command("launch_receipts", "cmd_launch_check"), "compare routed selection with returned host receipt metadata", "id",
        opt("--selection-file", required=True), opt("--receipt-file", required=True))
    cmd("usage", _Command("usage", "cmd_usage"), "account-wide subscription usage (agents read the machine cache)",
        opt("action", choices=["install", "uninstall"], nargs="?"), opt("--json", action="store_true"),
        opt("--refresh", action="store_true"), opt("--watch", action="store_true"),
        opt("--every", type=float, default=300, help="poll interval in seconds"),
        opt("--launch-agent", help="install: plist path elsewhere (writes only; does not load it)"),
        opt("--no-load", action="store_true", help="install: write the LaunchAgent without loading it"))
    cmd("new", _Command("nodes", "cmd_new"), "create a node", "id", opt("--title", required=True), opt("--owners", default=""),
        opt("--labels", default=""), opt("--runtime"), opt("--home", help="a path outside the instance"),
        opt("--home-url", dest="home_url"), opt("--brief", help="initial node outcome"),
        opt("--coordinator", action="store_true", help="also provision the host project and coordinator"),
        opt("--model", help="coordinator model; otherwise use configured routing"))
    cmd("scan", _Command("nodes", "cmd_scan"), "discover git repos without writing (given folders are depth 0)",
        opt("dirs", nargs="+"), opt("--json", action="store_true"), opt("--depth", type=int, default=4))
    cmd("adopt", _Command("nodes", "cmd_adopt"), "make existing git repositories node homes, then print study commands",
        opt("paths", nargs="+"), opt("--parent", default="root"), opt("--id"), opt("--title"))
    cmd("brief", _Command("briefs", "cmd_brief"), "a worker brief: where it runs, the task, the node's limits, how to ask and hand back",
        "id", opt("--import", dest="import_study", action="store_true", help="study an existing home"),
        opt("--small", action="store_true", help="minutes, low risk: no job ceremony"),
        opt("--branch", help="the job's worktree (brain wt add)"), opt("--task"),
        opt("--task-file", dest="task_file"),
        opt("--context-file", action="append", help="embed shared findings; repeatable; recorded as job inputs on this machine"),
        opt("--why-separate", help="why this concern needs its own agent (or contract ## Why separate)"),
        opt("--allow-unexplained", action="store_true", help="allow an urgent launch without a separation reason"),
        opt("--class", dest="job_class", choices=_m_common.JOB_CLASSES),
        opt("--effort", choices=_m_common.JOB_EFFORTS),
        opt("--runtime"), opt("--model", help="job model for scoped usage limits"),
        opt("--to", help="the thread the worker reports to (default: the node's coordinator)"),
        opt("--peer", help="shared-commit peer thread; coordinate directly with it"), opt("--out"))
    cmd("scrub", _Command("nodes", "cmd_scrub"), "filter source data without revealing never-name terms; reads paths or stdin",
        "id", opt("paths", nargs="*"), opt("--check", action="store_true"),
        opt("--metadata", action="store_true", help="file names and identities, not source content"))
    cmd("history", _Command("history", "cmd_history"), "past AI conversations about a node's home, in batches readers never compact "
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
    cmd("connect", _Command("connect", "cmd_connect"), "what nodes share: plan (default) digests of each node below <id>, then check, "
        "brief <node>... for digest readers, brief --final for the connector", "id",
        opt("action", nargs="?", choices=["plan", "check", "brief"]), opt("nodes", nargs="*"),
        opt("--only", help="plan: only these nodes (comma-separated)"),
        opt("--fresh", action="store_true", help="discard an earlier plan and its digests"),
        opt("--final", action="store_true", help="brief: compare the digests and propose"),
        opt("--runtime"), opt("--model", help="reader model for scoped usage limits"),
        opt("--to", help="the thread readers report to (default: the node's coordinator)"),
        opt("--save", action="store_true", help="brief: save it in .brain/connect/<node>/briefs/ and print its path"),
        opt("--json", action="store_true"))
    cmd("recall", _Command("knowledge", "cmd_recall"), "what every project learned that may help with a problem here: "
        "brain recall <node> \"what goes wrong\"", opt("id", nargs="?", default="root"), opt("words", nargs="*"),
        opt("--limit", type=int, default=10), opt("--setup", action="store_true", help="prepare the optional local multilingual ONNX model"),
        opt("--json", action="store_true"))
    cmd("proposals", _Command("status", "cmd_proposals"), "everything waiting for review, node by node, with full text and the commands "
        "to accept or reject several at once", opt("id", nargs="?"),
        opt("--only", help="only these nodes (comma-separated)"), opt("--json", action="store_true"),
        opt("--brief", action="store_true", help="compact independent reviewer sheet with claim, citation and decision commands"),
        opt('--approve-all', action='store_true', help='activate one complete independently reviewed rules snapshot'),
        opt('--review-manifest', help='complete coordinator-supplied snapshot review JSON'))
    cmd('policy', _Command("policy", "cmd_policy"), 'inspect explicit reviewed publication policy; never publishes', 'id',
        opt('--action', choices=['push', 'pr']), opt('--json', action='store_true', help='print a current limits mapping template'))
    cmd('land-check', _Command("land_checks", "cmd_land_check"), 'record/check exact-candidate evidence; only prints supported push steps', 'id',
        opt('--branch', required=True), opt('--run', action='store_true', help='run the configured check explicitly'),
        opt('--push-step', action='store_true', help='print a policy-allowed SHA-pinned push step with current evidence'))
    cmd("guard", _Command("guard", "cmd_guard"), f"check a home and its worktrees for the terms in {_m_common.NEVER_FILE} (and, if sealed, "
        "upward traces); exits 1 if any", "id", opt("paths", nargs="*"))
    cmd("spend", _Command("spend", "cmd_spend"), "time and tokens from this machine's transcripts: a node's past sessions (opt-in, "
        "aggregates only, never committed), or every session here", opt("id", nargs="?"),
        opt("--since", help="from this date (YYYY-MM-DD)"), opt("--machine", action="store_true",
                                                               help="every session on this machine, per runtime"),
        opt("--also", action="append", metavar="FOLDER", help="also sessions run in this folder (repeatable)"),
        opt("--add-source", action="append", metavar="RUNTIME=FOLDER",
            help="register an already permitted subscription folder in local sources; no instruction-file edits"),
        opt("--json", action="store_true"))
    cmd('preview', _Command("reports", "cmd_preview"), 'record/check source-bound local screenshot-tour evidence', 'id',
        opt('--record', metavar='RECEIPT_JSON'), opt('--json', action='store_true'))
    cmd('preview-adapter', _Command("preview_stdio", "cmd_preview_adapter"), 'trust an explicit machine-private local preview driver', 'id',
        opt('--register', required=True, metavar='PRIVATE_JSON'))
    cmd('preview-upgrade', _Command("preview_stdio", "cmd_preview_upgrade"), 'reconcile the existing preview protocol for exact landed main', 'id',
        opt('--head', required=True))
    cmd("ui", _Command("ui", "cmd_ui"), "a live view of this brain in your browser: projects, work in flight, worktrees, activity "
        "(read-only by default, this machine only)", opt("--port", type=int, default=_m_common.UI_PORT),
        opt("--every", type=float, default=3.0, help="seconds between refreshes"),
        opt("--write", action="store_true", help="allow explicit keyed local proposal reviews as the configured operator"),
        opt("--scan", nargs="+", action="extend", default=[], help="discover import candidates in DIR... (read-only)"),
        opt("--open", action="store_true", help="open it in the browser"),
        opt("--json", action="store_true", help="print an initial or dated cached JSON observation"),
        opt("--wait", action="store_true", help="with --json: compute a fresh complete snapshot before returning"))
    cmd("stale", _Command("status", "cmd_stale"), "freshness check: is <id>'s status.md behind a time the host gave (e.g. a thread's "
        "updatedAt, in UTC)?", "id", "host_time", opt("--grace", type=int, default=5, help="minutes allowed"))
    cmd("jobs", _Command("jobs", "cmd_jobs"), "jobs per node: open ones and their age, median hours and review rounds, causes",
        opt("id", nargs="?"), opt("--branch"), opt("--job-key", help="select one ledger job when a branch was reused"), opt("--lessons", help="learned item ids, or none: <reason>"),
        opt("--switch", help="record provider switch: old -> new: reason"),
        opt("--start", action="store_true", help="coordinator: start an import study or adopt an existing --thread"),
        opt("--thread", action="append", help="with --start: adopt one worker; with --settled: repeat for every confirmed settled thread"),
        opt("--rebind", metavar="BRANCH", help="restore a missing ledger row for a registered existing worktree branch"),
        opt("--close", action="store_true", help="coordinator: close a processed study, manually landed job, or parked job with --reason"),
        opt("--reason", help="one-line reason to explicitly close parked work"),
        opt("--live", action="store_true", help="read-only JSON rows joining ledger, hand-back, attempts and recorded queues; host activity unknown"),
        opt("--settled", action="store_true", help="coordinator: record all closed job threads safely settled through the host"),
        opt("--all-closed", action="store_true", help="with --settled: attest a complete host sweep for all closed ledger jobs"),
        opt("--attestation-file", help="fresh version 1 coordinator export with exhaustive per-job/thread/project coverage"),
        opt("--contract-file", dest="contract_file", help="coordinator: the job's contract (" +
            ", ".join(f"## {f}" for f in _m_common.CONTRACT_FIELDS) + "; an optional ## Budget, e.g. 20M tokens, 3 h)"),
        opt("--usage-file", dest="usage_file", help="coordinator: the job's usage as the host reports it (JSON); "
            "wins over what transcripts show"))
    cmd("settle", _Command("settlement", "cmd_settle"), "print exact host calls for a closed job; never calls the host", "id",
        opt("--job", required=True, help="exact ledger job key printed by brain jobs"))
    cmd("lifecycle", _Command("host_lifecycle", "cmd_lifecycle"), "execute a native host lifecycle action with fresh ownership checks",
        opt("action", choices=("settle", "launch", "restart", "maintain", "rotate", "accept-rotation")), "id",
        opt("--job-key", help="restart only this exact open ledger job"),
        opt("--ticket", help="accept only this exact confirmed rotation launch"),
        opt("--session-id", help="replacement runtime actual session identity; never a host thread ID substitute"))
    cmd("rotate", _Command("rotation", "cmd_rotate"), "prepare a file-based coordinator handover; prints host steps", "id")
    cmd("due", _Command("status", "cmd_due"), "coordinator actions and machine holds; empty means no output",
        opt("--since-wake", action="store_true", help="new file-only wake items compared with the wake before the latest delivery"),
        opt("--synthesis-seen", metavar="SET", help="record an exact synthesis set after its proposal has been reviewed"),
        opt("--machine", action="store_true", help="also show informational machine readings, even without node actions"),
        opt('--defer', nargs=2, metavar=('ITEM_ID', 'UNTIL'), help='defer an eligible reminder until an absolute timestamp'),
        opt('--ack', metavar='ITEM_ID', help='acknowledge a current parked/blocked handback episode'),
        opt('--reason', help='required recorded reason for --defer or --ack'),
        opt('--resume', help='required resume condition for --ack'),
        opt('--until', help='optional absolute resume time for --ack'),
        opt('--handled', action='store_true', help='also show current handled reminders and their reasons'),
        opt("id", nargs="?"), opt("--json", action="store_true"),
        opt("--quiet", action="store_true", help="no output; exit 1 for node actions, including its held work, 0 otherwise"))
    queueable_command(cmd, 'inbox')
    cmd('inbox-seen', _Command('inbox_notices', 'cmd_inbox_seen'),
        'mark one owning node inbox episode handled, recording the action and retaining its source',
        'id', 'item_id', 'reason')
    cmd('answer', _Command("status", "cmd_answer"), 'record text for exactly one current inbox episode, retaining source/history',
        'item_id', 'text')
    cmd('index', _Command("freshness", "cmd_index"), 'configured upstream index freshness or opt-in native link generation', 'id',
        opt('--record', action='store_true', help='explicitly record current input/output fingerprints after regeneration'),
        opt('--refresh', action='store_true', help='regenerate an explicitly owned native link index'))
    cmd("delegate", _Command("host_tasks", "cmd_delegate"), "explicit registered native delegation with completion fallback",
        "id", opt("--task-file", required=True), opt("--target-file", required=True), opt("--key", required=True))
    cmd("host-export", _Command("host_observations", "cmd_host_export"), "one bounded native export; preserves previous snapshot on failure")
    cmd("notify", _Command("communication", "cmd_notify"), "record typed notification; root wakes only for questions",
        "id", opt("kind", choices=_m_common.COMMUNICATION_KINDS), "text",
        opt("--key", required=True, help="stable idempotency key"), opt("--question-file"))
    cmd("question-answer", _Command("communication", "cmd_question_answer"), "answer one typed question", "id", "choice")
    cmd('morning-brief', _Command("communication_policy", "cmd_morning_brief"), 'publish a retained quiet-hours inbox brief; never wakes', 'id',
        opt('--operator', action='store_true', help='root only: aggregate retained notices once per local delivery day'))
    cmd("msg", _Command("messages", "cmd_msg"), "format start/done/blocked/question for a coordinator (does not send)",
        "id", opt("kind", choices=_m_common.MESSAGE_KINDS), "message", opt("--branch"), opt("--report"))
    queueable_command(cmd, 'handback')
    cmd("queue", _Command("queue_priority", "cmd_gate_queue"), "run one hand-back or explicitly reorder queued native verify tokens",
        opt("action", choices=['run', 'wait', 'drain'], nargs='?'), opt('id', nargs='?'), opt('--once', action='store_true'),
        opt('--request-id'), opt('--node'), opt('--move'), opt('--before'),
        opt('--timeout', type=float, default=300, help='queue wait ID: receipt barrier before reading or depending on a queued write'),
        opt('--lease-fd', type=int, help=argparse.SUPPRESS),
        opt('--recover', action='store_true', help='recover an exact queue run only after recorded processes are absent; live or unknown processes stay retained'))
    cmd("dispatch", _Command("dispatcher", "cmd_dispatch"), "bounded read-only plan of explicit root/operator asks; empty scan is silent",
        opt('--limit', type=int, default=50), opt('--node-limit', type=int, default=200),
        opt('--run', action='store_true', help='enqueue only current questions through the durable coalesced wake path'))
    cmd("backlog", _Command("events", "cmd_backlog"), "append a dated backlog item", "id",
        opt("action", choices=["add"]), "item")
    cmd("introspect", _Command("introspection", "cmd_introspect"), "recorded causal job progress and communication metrics",
        "id", opt("--branch"), opt("--hours", type=float, default=24), opt("--limit", type=int, default=50))
    cmd("diagnose", _Command("diagnostics", "cmd_diagnose"), "source-first diagnosis and bounded recorded probe setup permits",
        "id", opt("action", choices=["plan", "outcome"]), opt("--branch", required=True),
        opt("--question", required=True), opt("--source", action="append"),
        opt("--finding", choices=["source", "ambiguous", "runtime"]), opt("--reason"),
        opt("--attempt"), opt("--state", choices=["setup-failed", "observed"]))
    cmd("scorecard", _Command("scorecard", "cmd_scorecard"), "score a supplied evidence window; missing evidence stays unknown",
        "id", opt("--file", required=True, help="version 1 measurement manifest"))
    cmd("show", _Command("status", "cmd_show"), "everything about one node, including this machine's bindings", "id")
    cmd("set", _Command("nodes", "cmd_set"), "set fields: brain set <id> key=value ... (host IDs and home paths stay on this machine)",
        "id", opt("pairs", nargs="+"))
    s = sub.add_parser("settings", help="apply an explicit validated settings batch")
    settings_sub = s.add_subparsers(dest="settings_action", required=True)
    t = settings_sub.add_parser("batch", help="JSON array of set/schedule-add operations; one bookkeeping commit")
    t.add_argument("file", help="UTF-8 JSON batch file")
    t.set_defaults(f=_Command("settings_batch", "cmd_settings_batch"))
    t = settings_sub.add_parser("recover", help="explicitly complete an owned interrupted metadata packet")
    t.add_argument("file", help="private settings-batch packet")
    t.set_defaults(f=_Command("metadata_recovery", "cmd_settings_recover"))
    cmd("host-instance", _Command("host_instances", "cmd_host_instance"), "private addressed host inventory; execution remains local",
        opt("action", choices=['list', 'remember', 'route', 'plan']), opt("name", nargs='?'),
        opt("--node", default='root'), opt("--machine"), opt("--adapter"), opt("--file"))
    cmd("relabel", _Command("nodes", "cmd_relabel"), "brain relabel <id> key:value ... (key:- removes)", "id", opt("labels", nargs="+"))
    cmd("owners", _Command("nodes", "cmd_owners"), "change owners: brain owners <id> a,b  ('' = same as parent)", "id", "owners")
    cmd("seal", _Command("guard", "cmd_seal"), "give a node new owners and an external home (the sealing procedure's mechanics)",
        "id", opt("--owners", required=True), opt("--home", required=True), opt("--url"),
        opt("--move", nargs="*", help="files or folders (relative to the node) that belong to the new owners"),
        opt("--no-git", action="store_true", dest="no_git"))
    cmd("move", _Command("transitions", "cmd_move"), "brain move <id> <new-parent-id>", "id", "parent")
    cmd("split", _Command("transitions", "cmd_split"), "brain split <id> <new-id> --title T [--move files...]", "id", "new_id",
        opt("--title", required=True), opt("--owners"), opt("--labels"), opt("--runtime"), opt("--move", nargs="*"))
    cmd("merge", _Command("transitions", "cmd_merge"), "brain merge <src> <dst>: fold src into dst (same owners only)", "src", "dst")
    cmd("archive", _Command("transitions", "cmd_archive"), "brain archive <id>", "id")
    cmd("principal", _Command("transitions", "cmd_principal"), "brain principal add <id> | list", opt("action", choices=["add", "list"]),
        opt("id", nargs="?"))
    cmd("revoke", _Command("transitions", "cmd_revoke"), "end the relationship with a principal: quarantine and archive (axiom 21)", "principal")

    s = sub.add_parser("item", help="library items: add, list, scope, quarantine, restore")
    isub = s.add_subparsers(dest="icmd", metavar="<action>")
    t = isub.add_parser("add", help="brain item add <id> --from <node>[,<node>...] --title T --kind K --ref R")
    for n_, kw in [("id", {}), ("--from", {"dest": "source", "required": True}), ("--title", {"required": True}),
                   ("--kind", {"required": True}), ("--ref", {"required": True}), ("--at", {}), ("--scope", {}),
                   ("--consented-by", {"dest": "consented_by", "default": ""}), ("--body", {}),
                   ("--body-file", {"dest": "body_file"}), ("--proposed", {"action": "store_true"}),
                   ("--repo", {}), ("--commit", {}), ("--keywords", {"help": "words someone with this problem would use"})]:
        t.add_argument(n_, **kw)
    t.set_defaults(f=_Command("knowledge", "cmd_item_add"))
    t = isub.add_parser("list", help="brain item list [--node N]  (shows what N may use)")
    t.add_argument("--node")
    t.set_defaults(f=_Command("knowledge", "cmd_item_list"))
    t = isub.add_parser("show", help="read a usable item for a target node")
    t.add_argument("id"); t.add_argument("--node", required=True)
    t.set_defaults(f=_Command("knowledge", "cmd_item_show"))
    t = isub.add_parser("scope", help="brain item scope <id> a,b [--consented-by ...]")
    t.add_argument("id"); t.add_argument("scope"); t.add_argument("--consented-by", dest="consented_by", default="")
    t.set_defaults(f=_Command("knowledge", "cmd_item_scope"))
    for st, name in (("quarantined", "quarantine"), ("active", "restore")):
        t = isub.add_parser(name, help=f"brain item {name} <id>")
        t.add_argument("id"); t.add_argument("--reason")
        t.set_defaults(f=_Command("knowledge", "cmd_item_quarantine") if st == "quarantined" else _Command("knowledge", "cmd_item_restore"))
    t = isub.add_parser("accept", help="accept reviewed proposals (restore never accepts)")
    t.add_argument("id", nargs="+"); t.add_argument("--reviewed-by", required=True, dest="reviewed_by")
    t.set_defaults(f=_Command("knowledge", "cmd_item_accept"))
    t = isub.add_parser("reject", help="reject proposals permanently (unusable)")
    t.add_argument("id", nargs="+")
    t.set_defaults(f=_Command("knowledge", "cmd_item_reject"))
    cmd("usable", _Command("knowledge", "cmd_usable"), "may <node> use <item>? (axiom 10)", "item", "node")

    s = sub.add_parser("specialist", help="specialists: new")
    ssub = s.add_subparsers(dest="scmd", metavar="<action>")
    t = ssub.add_parser("new", help="brain specialist new <id> --title T [--at node] [--runtime r] [--scope a,b]")
    t.add_argument("id"); t.add_argument("--title", required=True); t.add_argument("--at")
    t.add_argument("--runtime"); t.add_argument("--scope")
    t.add_argument("--proposed", action="store_true", help="imported and unreviewed; cannot be hired")
    t.add_argument("--from", dest="source", help="nodes whose work it is drawn from (comma-separated): it lives at "
                   "their common ancestor, scoped to the owners they share")
    t.set_defaults(f=_Command("knowledge", "cmd_specialist_new"))
    t = ssub.add_parser("accept", help="activate reviewed proposed specialists")
    t.add_argument("id", nargs="+"); t.add_argument("--at")
    t.add_argument("--reviewed-by", required=True, dest="reviewed_by")
    t.set_defaults(f=_Command("knowledge", "cmd_specialist_accept"))
    cmd("teach", _Command("teaching", "cmd_teach"), "prepare or apply a checked native adaptation of a usable item", "item", "node",
        opt("--branch"), opt("--file"), opt("--to"), opt("--check"), opt("--expected-sha256"),
        opt("--consented-by", default=""))
    cmd("feedback", _Command("feedback", "cmd_feedback"), "record corrections and close only with an executed green check", "id",
        opt("action", nargs="?", default="list", choices=["list", "add", "map", "close"]),
        opt("value", nargs="?"), opt("--principle"), opt("--job"), opt("--check"),
        opt("--before"), opt("--after"), opt("--json", action="store_true"))
    cmd("enforcement", _Command("learning", "cmd_enforcement"), "inspect enforcement and synchronize check tasks", "id",
        opt("--sync-tasks", action="store_true"), opt("--json", action="store_true"))
    cmd("lesson", _Command("knowledge", "cmd_lesson"), "brain lesson <specialist|node> --from <node> \"text\"", "specialist",
        opt("--from", dest="source", required=True), opt("--at"),
        opt("--node", action="store_true", help="record a correction to a node instead of a specialist"),
        opt("--principle", default="unclassified", help="id in the target's direction.md; default: unclassified"), "text")
    cmd("hire", _Command("knowledge", "cmd_hire"), "print a specialist's brief for a job: brain hire <specialist> <node> --task \"...\"",
        "specialist", "node", opt("--task", required=True), opt("--items", default=""),
        opt("--runtime"), opt("--class", dest="job_class", choices=_m_common.JOB_CLASSES),
        opt("--effort", choices=_m_common.JOB_EFFORTS),
        opt("--model", help="job model for scoped usage limits"), opt("--out"), opt("--branch"),
        opt("--why-separate", help="why this specialist needs a separate agent; defaults to the contract"),
        opt("--allow-unexplained", action="store_true", help="allow an urgent launch without a reason"),
        opt("--job", help="record this hire for a branch or job id on this machine"))

    cmd("workbench", _Command("worktrees", "cmd_workbench"), "a node's workbench: scripts, reports, worktrees (created if missing)", "id",
        opt("--path", action="store_true"))
    s = sub.add_parser("wt", help="worktrees of an external git home: add, list, rm, prune")
    wsub = s.add_subparsers(dest="wcmd", metavar="<action>")
    t = wsub.add_parser("add", help="brain wt add <node> <branch> [--base REF]: new worktree + setup + port")
    t.add_argument("id"); t.add_argument("branch"); t.add_argument("--base")
    t.add_argument("--no-setup", action="store_true", dest="no_setup")
    t.add_argument("--task-id", help="ready task to link to this job")
    t.set_defaults(f=_Command("worktrees", "cmd_wt_add"))
    t = wsub.add_parser('env', help='set machine-local per-worktree environment overrides; values are never printed')
    t.add_argument('id'); t.add_argument('branch'); t.add_argument('pairs', nargs='*')
    t.add_argument('--unset', action='append', default=[])
    t.set_defaults(f=_Command("job_env", "cmd_job_env"))
    t = wsub.add_parser("list", help="brain wt list [node]")
    t.add_argument("id", nargs="?")
    t.set_defaults(f=_Command("worktrees", "cmd_wt_list"))
    t = wsub.add_parser("rm", help="brain wt rm <node> <branch> [--force] [--cause 'why it was dropped']")
    t.add_argument("id"); t.add_argument("branch"); t.add_argument("--force", action="store_true")
    t.add_argument("--cause", help="why the job ended without a merge; kept in the node's job-ledger.md")
    t.add_argument('--settle-threads', action='store_true', help='tell and freshly settle exact closed-job threads before removal; retain while pending')
    t.set_defaults(f=_Command("worktrees", "cmd_wt_rm"))
    t = wsub.add_parser("prune", help="brain wt prune <node>: remove clean worktrees whose branch is merged")
    t.add_argument('--settle-threads', action='store_true', help='tell and freshly settle exact closed-job threads before removal; retain while pending')
    t.add_argument("id"); t.add_argument("--dry-run", action="store_true", dest="dry_run")
    t.add_argument("--offline", action="store_true")
    t.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                   help="retain branches or registered paths matching this glob (repeatable)")
    t.set_defaults(f=_Command("worktrees", "cmd_wt_prune"))
    cmd("report", _Command("reports", "cmd_report"), "start a report: brain report <node> \"topic\" (prints its folder)", "id", "topic")
    cmd("reports", _Command("reports", "cmd_reports"), "every report, newest first: brain reports [node]", opt("id", nargs="?"))
    cmd("review", _Command("sync", "cmd_review"), "brief for an independent review: brain review <node> --branch B",
        "id", opt("--branch", required=True), opt("--base"), opt("--built-by", dest="built_by"),
        opt("--by", "--runtime", help="reviewer runtime (default: configured independence policy)"), opt("--focus"),
        opt("--class", dest="job_class", choices=_m_common.JOB_CLASSES),
        opt("--effort", choices=_m_common.JOB_EFFORTS),
        opt("--why-separate", help="reason for a separate reviewer; defaults to the independence policy"),
        opt("--allow-unexplained", action="store_true", help="allow an urgent launch without a reason"),
        opt("--model", help="job model for scoped usage limits"), opt("--out"),
        opt("--reason", help="nonempty one-line explanation for another full round or round 3+"),
        opt("--risk", choices=("security", "permissions", "data-loss", "concurrency"),
            help="mark the change for up to 5 review rounds (otherwise 3); recorded in the ledger"),
        opt("--check-fix", action="store_true", help="only check prior blockers/majors and fix regressions"),
        opt("--findings", help="prior findings JSON: list of objects with severity and text"),
        opt("--fix-base", help="reviewed commit before the fixes; defaults to previous review head"),
        opt("--contract", help="contract file; defaults to the job's recorded contract"),
        opt("--receipt-file", help="completed review host-observation JSON; does not start another round"),
        opt("--same-runtime-reason", help="reason for a configured expiring same-runtime exception"))
    cmd("preview-receipt", _Command("preview_evidence", "cmd_preview_receipt"), "record rendered preview source samples", "id",
        opt("--branch", required=True), opt("--file", required=True))
    cmd("preview-receipt-skeleton", _Command("preview_evidence", "cmd_preview_receipt_skeleton"),
        "read current own-job receipt JSON; fill null browser/render and three-source fields before preview-receipt; writes nothing",
        "id", opt("--branch", required=True),
        opt("--json", action="store_true", help="emit receipt skeleton JSON (also the default)"))
    cmd('release', _Command("release", "cmd_release"), 'inspect or execute an opted-in reviewed full-green release', 'id',
        opt('--run', action='store_true'))
    cmd("train", _Command("train", "cmd_train"), "batch ready jobs onto local main, verify once, and print the release command", "id",
        opt("--jobs", nargs="+", help="explicit job order (space or comma separated); all must be ready"))
    cmd("land", _Command("train", "cmd_land"), "batch land ready jobs; host settlement requires fresh separate evidence", "id",
        opt("--jobs", nargs="+", help="explicit job order (space or comma separated); all must be ready"))
    cmd("full-run", _Command("full_gate", "cmd_full_run"), "run the native full gate and retain exact-candidate evidence without publishing completion", "id",
        opt("--branch", required=True), opt("--reviews", required=True, type=int))
    cmd("full-isolate", _Command("full_disposition", "cmd_full_isolate"),
        "rerun exactly one failed native test on its unchanged red-gate head", "id",
        opt("--branch", required=True), opt("--full-receipt", required=True), opt("--test", required=True))
    cmd("full-quarantine", _Command("full_disposition", "cmd_full_quarantine"),
        "record skip-only quarantine landing evidence while preserving the original red full result", "id",
        opt("--branch", required=True), opt("--file", required=True, help="JSON with full_receipt and quarantines"))
    gate = cmd("gate", _Command("full_gate", "cmd_gate"), "run the per-change readiness gate; --train or --nightly forces complete verification", "id",
        opt("--branch", required=True), opt("--reviews", required=True, type=int))
    gate_mode = gate.add_mutually_exclusive_group()
    gate_mode.add_argument('--train', action='store_true')
    gate_mode.add_argument('--nightly', action='store_true')
    cmd("spec-run", _Command("spec_run", "cmd_spec_run"), "run one focused command with native admission; never records full readiness", "id",
        opt("--branch"), opt("--head", help="exact clean worktree commit to test"),
        opt("--command", nargs=argparse.REMAINDER), opt("specs", nargs="*"))
    for name, record, help_text in (
        ("ready", False, "check a worker's ready packet against the branch and cached origin default (local main without origin)"),
        ("verify", True, "run node verification and record a hand-back with its duration"),
    ):
        ready = cmd(name, _Command("verify", "cmd_ready"), help_text, "id",
            opt("--branch", required=True), opt("--record", action="store_true", help="worker: run node verify and record"),
            opt('--enqueue', action='store_true', help='with --record: queue verification and return a request ID'),
            opt('--request-id', help='retry --record --enqueue with the same payload-bound ID'),
            opt("--summary", help="one-line hand-back result (required for --record with schema v3)"),
            opt("--question", help="one-line coordinator choice for a question hand-back"),
            opt("--reviews", type=int, help="completed review rounds, required with --record"),
            opt("--report"), opt("--lessons", help="proposed lessons, or none: <reason>"),
            opt("--full-receipt", help="with --record: adopt a native full-run receipt"),
            opt("--reverify", action="store_true", help="with --record: run verify even if a green run still proves it"))
        ready.set_defaults(record=record)
    cmd("host", _Command("host_actions", "cmd_host"), "explicit coordinator provisioning and host wake/watch actions",
        opt("action", choices=['provision', 'wake-setup', 'watch', 'schedule']), "id", opt("--model"), opt("--url"),
        opt("--schedule-id"), opt("--provider", help="explicit host runtime for schedule binding"),
        opt("--require-no-auto-settle", action="store_true", help="refuse provisioning if the adapter cannot enforce this policy"))
    cmd("digest", _Command("digest", "cmd_digest"), "commitments with landing evidence; --retro summarizes recorded job measurements", "id",
        opt("--json", action="store_true"), opt("--retro", action="store_true"),
        opt("--day", help="retro calendar date (YYYY-MM-DD); defaults to the previous local day"))
    cmd("schedule", _Command("schedules", "cmd_schedule"), "recurring work: brain schedule list | add <id> --when W --node N --brief B | "
        "bind <id> <host id> | unbind <id>", opt("action", choices=["list", "add", "bind", "unbind"], nargs="?",
                                                  default="list"),
        opt("id", nargs="?"), opt("host_id", nargs="?"), opt("--when"), opt("--node", default="root"), opt("--brief"),
        opt("--provider", help="runtime explicitly selected in the host schedule (add/bind only)"),
        opt("-v", "--verbose", action="store_true"))
    cmd("sync", _Command("sync", "cmd_sync"), "what changed in a node (and its home) since the last sync; --all: every external home",
        opt("id", nargs="?"), opt("--all", action="store_true"),
        opt("--offline", action="store_true"), opt("--mark", action="store_true"),
        opt("--reset-main", action="store_true", help="rescan from the common ancestor, or last-sync window for unrelated history; one node only"),
        opt("--limit", type=int, default=20, help="uncommitted files to list"))
    s = sub.add_parser("hook", help="runtime hooks: stop (called by the runtime), approve (trust them in Codex)")
    hsub = s.add_subparsers(dest="hcmd", metavar="<action>")
    t = hsub.add_parser("stop", help="called by a runtime when a turn ends; reads the hook's JSON on stdin")
    t.set_defaults(f=_Command("hosts", "cmd_hook"), event="stop")
    t = hsub.add_parser("approve", help="trust this instance and its project hooks in Codex (writes its config)")
    t.set_defaults(f=_Command("hosts", "cmd_hook_approve"))
    t = hsub.add_parser("pretool", help="check declared forbidden tool actions; reads runtime JSON on stdin")
    t.set_defaults(f=_Command("action_guard", "cmd_hook_pretool"))
    cmd("allow", _Command("action_guard", "cmd_allow"), "allow one exact forbidden shell command once", "id", "action",
        opt("--once", action="store_true", required=True), opt("--cwd"), opt("--tool", default="Bash"))
    cmd("wake", _Command("hosts", "cmd_wake"), "wake a node coordinator (or root); inbox remains the record", "id",
        opt("reason", nargs="?"), opt("--drain", action="store_true", help=argparse.SUPPRESS),
        opt("--retry", action="store_true", help=argparse.SUPPRESS))
    cmd("watch", _Command("watch", "cmd_watch"), "queue wakes only for new actionable file items",
        opt("--once", action="store_true", required=True))
    cmd("now", _Command("status", "cmd_now"), "the current time in the instance's timezone, as every record writes it (axiom 17)",
        opt("--iso", action="store_true"))
    cmd("tree", _Command("status", "cmd_tree"), "show the node tree")
    cmd("status", _Command("status", "cmd_status"), "status timestamps; --refresh regenerates only one node's facts block",
        opt("id", nargs="?"), opt("--refresh", action="store_true"),
        opt("--project", action="store_true", help="explicit phase for configured generated-facts ownership"),
        opt("--from-events", action="store_true"), opt("--json", action="store_true"))
    cmd("timings", _m_timings.cmd_timings, "recent command durations and automatic slow profiles",
        opt("--slow", action="store_true"), opt("--command"), opt("--node"),
        opt("--since", help="inclusive ISO timestamp or Unix seconds"),
        opt("--until", help="inclusive ISO timestamp or Unix seconds"),
        opt("--slowest", type=int, metavar="N"), opt("--summary", action="store_true", help="CPU plus lock hold p50/p95; legacy CPU is unknown"),
        opt("--json", action="store_true"))
    cmd("time", _Command("operation_timings", "cmd_time"), "time a command inside a Brain; never log arguments or output",
        opt("--node"), opt("--threshold", type=float, default=5.0),
        opt("command", nargs=argparse.REMAINDER))
    cmd("events", _Command("events", "cmd_events"), "private event metadata; --compare summarizes the last day", "id",
        opt("--compare", action="store_true"), opt("--json", action="store_true"),
        opt("--metrics", action="store_true"), opt("--explain", metavar="DIFFERENCE_ID"), opt("--reason"),
        opt("--rebuild-records", action="store_true"),
        opt("--record", metavar="FACT_TYPE"), opt("--job-key"), opt("--data", default="{}"))
    cmd("registry", _Command("status", "cmd_registry"), "regenerate registry.md")
    cmd("link", _Command("hosts", "cmd_link"), "create runtime links (axiom 22); --command puts 'brain' on your PATH; --global adds a "
        "pointer to this Brain in each runtime's user-wide instructions",
        opt("--command", action="store_true"), opt("--bin-dir", dest="bin_dir", default="~/.local/bin"),
        opt("--global", action="store_true", dest="glob"), opt("--remove-global", action="store_true", dest="unglobal"),
        opt("targets", nargs="*", metavar="RUNTIME=FOLDER",
            help="with --global: the config folders that get this brain's pointer, e.g. claude=~/.claude-work"))
    cmd("check", _Command("check", "cmd_check"), "verify what can be checked mechanically; --fix repairs the safe things",
        opt("--fix", action="store_true"),
        opt("--live", action="store_true", help="send real webhook probes; default check only reads cached results"))
    cmd("gate-lock", _Command("gate_lock", "cmd_gate_lock"), "serialize a command for one node",
        opt("--node", help="node id (default: unambiguous current folder)"),
        opt("--label"), opt("command", nargs=argparse.REMAINDER))
    heavy = cmd("heavy", _Command("machine", "cmd_heavy"), "admit heavy work or run a command in a slot",
        opt("--node", help="coordinator whose work is waiting (default root)"))
    run = heavy.add_subparsers(dest="heavy_action").add_parser("run", help="run a command in a machine slot")
    run.add_argument("--node", help="coordinator whose work is waiting (default root)")
    run.add_argument("--label", help="optional description, private to this instance")
    run.add_argument("command", nargs=argparse.REMAINDER, help="-- command and arguments")
    run.set_defaults(f=_Command("machine", "cmd_heavy_run"))
    cmd("machine", _Command("machine", "cmd_machine"), "machine state, sampler and bindings",
        opt("action", nargs="?", choices=["sample", "install", "uninstall", 'wake-setup', 'queue-service']), opt('id', nargs='?'),
        opt("--yes", action="store_true", help="install/remove the sampler with operator consent"),
        opt("--missing-homes", action="store_true", dest="missing_homes"))
    cmd("brains", _Command("hosts", "cmd_brains"), "the brains on this machine: brain brains [forget <name>]",
        opt("action", nargs="?", choices=["list", "forget"], default="list"), opt("name", nargs="?"))
    cmd("use", _Command("hosts", "cmd_use"), "set this machine's default brain: brain use <name>", "name")
    cmd("version", _Command("upgrade", "cmd_version"), "installed core SHA/date and cached origin ancestry; never fetches",
        opt("--ref", help="cached origin ref (default origin/HEAD or refs/remotes/origin/main)"),
        opt("--json", action="store_true"))
    cmd("upgrade", _Command("upgrade", "cmd_upgrade"), "update core/ to the latest version, then link and check",
        opt("--node", help="node whose in-flight gate refuses (default: the node whose home ships this core)"),
        opt("--rollback", action="store_true", help="swap back to the previous installed core release"))
    cmd("changes", _Command("upgrade", "cmd_changes"), "what changed in core for agents: unread entries (default), --all, --seen",
        opt("id", nargs="?"), opt("--seen", action="store_true", help="mark every entry read on this machine"),
        opt("--all", action="store_true"), opt("--json", action="store_true"))
    cmd("selftest", cmd_selftest, "run the core's scenario tests in throwaway folders",
        opt("-v", "--verbose", action="store_true"), opt("-k"),
        opt("--jobs", type=int, default=1, help="parallel pytest workers (requires requirements-test.txt); 1 uses stdlib unittest"))
    def cmd_help(a):
        ap.print_help()
    sub.add_parser("help", help="show this help").set_defaults(f=cmd_help)

    a, extra = ap.parse_known_args()
    if isinstance(getattr(a, "f", None), _Command):
        a.f = a.f.resolve()
    # argparse's trailing nargs='*' otherwise rejects `scrub node --check file`.
    if extra:
        if getattr(a, "f", None) == _m_nodes.cmd_scrub and all(not p.startswith("-") for p in extra):
            a.paths.extend(extra)
        elif getattr(getattr(a, 'f', None), '__name__', '') == 'cmd_spec_run' and extra[0] == '--':
            a.specs.extend(extra[1:])
        else:
            ap.error("unrecognized arguments: " + " ".join(extra))
    handler = getattr(a, 'f', None)
    local_source_registration = handler and handler.__name__ == 'cmd_spend' and a.add_source
    _m_timings.select_command(a.cmd or 'dashboard', persist=bool(handler) and
                             not local_source_registration and
                             (writes(a) or handler.__name__ not in READ_ONLY or command_read(a)))
    _m_timings.select_node(getattr(a, 'node', None) or getattr(a, 'id', None))
    if not getattr(a, "f", None):
        if a.cmd in ("item", "specialist", "wt", "hook"):
            sub.choices[a.cmd].print_help()
            return
        if a.all:
            dashboard_all()
        elif not _m_status.cmd_dashboard(a):
            ap.print_help()
        return
    if a.f.__name__ == 'cmd_task_apply_plan':
        # This endpoint preflights the entire plan before opening its journal.
        a.f(a)
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
    if a.f.__name__ == "cmd_machine" and a.action == "sample":
        a.f(a)  # sampler and watcher each take a short nonblocking instance lock
        return
    if a.f.__name__ == 'cmd_gate_queue' and a.action in ('drain', 'wait'):
        phase = 'apply' if a.action == 'drain' else 'wait'
        with _m_timings.queue_phase(_m_nodes.find_root(), phase):
            a.f(a)
        return
    if a.f.__name__ == 'cmd_jobs' and not writes(a) and not getattr(a, 'live', False):
        from . import jobs
        # Candidate discovery never takes the writer lock or caches authority.
        # A possible closure uses the normal command transaction and fresh reads.
        a._reconcile_landings = jobs.jobs_need_reconciliation(a)
    if command_read(a):
        handler = a.f
        def read_handler(args):
            with _m_command_reads.snapshot(), _m_timings.phase("compute"):
                return handler(args)
        read_handler.__name__ = handler.__name__
        a.f = read_handler
    root = _m_nodes.try_find_root() if writes(a) else None
    if root:
        from . import handback_queue
        try:
            accepted = handback_queue.fast_intent(a, root)
            if accepted:
                if callable(accepted):
                    accepted()
                return
        except (OSError, ValueError) as error:
            _m_common.die(str(error))
        # Reject malformed arguments before lock admission or journal preparation.
        # Handlers still validate current authority and mutable state under lock.
        if a.f.__name__ in ('cmd_item_accept', 'cmd_specialist_accept'):
            from . import knowledge
            knowledge.review_principal(_m_instance.Instance(root), a.reviewed_by)
        if a.f.__name__ == 'cmd_task' and a.action in ('add', 'set'):
            try:
                _m_tasks.command_changes(a)
            except ValueError as error:
                _m_common.die(str(error))
        out = _m_common.outward_links(root)
        if out:  # checked before anything is written: no command half-done through a link
            _m_common.die("these links (symbolic or hard) lead out of the brain, and brain writes only inside it: "
                + ", ".join(str(p.relative_to(Path(root).resolve())) if Path(root).resolve() in p.parents else str(p)
                            for p in out[:5]) + ("…" if len(out) > 5 else "") + "; remove them (brain check lists them)")
        def attempt():
            operation = None
            try:
                with _m_instance.brain_lock(root):
                    operation = _m_events.Command(root, a)
                    # Durable source publication belongs to the caller; generated
                    # views belong to the coalesced queue drain, after it returns.
                    operation.defer_views = a.f.__name__ in ('cmd_task', 'cmd_handback', 'cmd_gate_queue')
                    with operation:
                        a.f(a)
            finally:
                if operation is not None and getattr(operation, 'views_deferred', False):
                    try:
                        handback_queue.start_intent_drainer(operation.inst)
                    except OSError:
                        print('brain: state saved; generated view stale; run brain queue drain', file=sys.stderr)
        if a.f.__name__ == 'cmd_status' and a.refresh:
            refresh_with_retry(attempt, root)
        else:
            attempt()
    else:
        a.f(a)


REFRESH_TRIES = 5
REFRESH_WRITER_WAIT = 120  # one deadline across all live-owner waits and admission races


def refresh_with_retry(attempt, root):
    with _m_status_refresh.invocation():
        return _refresh_with_retry(attempt, root)


def _refresh_with_retry(attempt, root):
    """Retry fresh state, then use bounded final admission and unlocked builds."""
    import contextlib
    import random
    import time
    for tries in range(1, REFRESH_TRIES + 1):
        before = _m_instance.settings(_m_instance.Instance(root))
        try:
            return attempt()
        except SystemExit as error:
            # Only ordinary concurrent writes are retried. A settings or
            # binding edit (instance, local, node policy) refuses as before.
            if (not _m_status_refresh.contended(error) or
                    _m_instance.settings(_m_instance.Instance(root)) != before):
                raise
        time.sleep(random.uniform(0.05, 0.25) * tries)
    print(f'brain: status refresh contended {REFRESH_TRIES} times; retrying with bounded admission', file=sys.stderr)
    deadline = None
    while True:
        before = _m_instance.settings(_m_instance.Instance(root))
        try:
            with _m_instance.held(root):
                # Admission can race another released writer. Reconciliation
                # re-reads the leases under the lock on every fresh attempt.
                with contextlib.ExitStack() as stack:
                    lock_wait = _m_instance.LOCK_WAIT
                    try:
                        if deadline is not None:
                            _m_instance.LOCK_WAIT = min(lock_wait, max(0, deadline - time.monotonic()))
                        stack.enter_context(_m_instance.brain_lock(root))
                    except SystemExit:
                        if deadline is not None and time.monotonic() >= deadline:
                            _m_common.die('status refresh refused: refresh deadline expired during lock admission; '
                                          'retry from fresh state')
                        raise
                    finally:
                        # The CLI owns this process-local admission policy only
                        # while acquiring; nested work keeps its normal bound.
                        _m_instance.LOCK_WAIT = lock_wait
                    if deadline is not None and time.monotonic() >= deadline:
                        _m_common.die('status refresh refused: refresh deadline expired; retry from fresh state')
                    return attempt()
        except _m_events.RefreshBlocked as error:
            # Command.__exit__ has closed our operation and attempt has released
            # the physical writer lock. Its live owners can now reacquire it.
            if _m_instance.settings(_m_instance.Instance(root)) != before:
                raise
            if deadline is None:
                deadline = time.monotonic() + REFRESH_WRITER_WAIT
            current = _m_instance.Instance(root)
            while any(_m_events.lease_live(current, ref) for ref in error.refs):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _m_common.die('status refresh refused: live writers did not finish before the refresh deadline; '
                                  'generated view still needs reconciliation')
                time.sleep(min(0.05, remaining))
            if time.monotonic() >= deadline:
                _m_common.die('status refresh refused: refresh deadline expired; retry from fresh state')
            if _m_instance.settings(_m_instance.Instance(root)) != before:
                _m_common.die('state changed during the unlocked step; retry from fresh state')
