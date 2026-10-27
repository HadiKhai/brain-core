"""Verify mechanics for Brain."""
import contextlib
import contextvars
import fnmatch
import hashlib
import importlib
import json
import math
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

from . import common as _m_common
from . import docker_leftovers as _m_docker_leftovers
from . import guard as _m_guard
from . import gate_lock as _m_gate_lock
from . import history as _m_history
from . import instance as _m_instance
from . import jobs as _m_jobs
from . import machine as _m_machine
from . import nodes as _m_nodes
from . import worktrees as _m_worktrees
from . import verify_lanes as _m_lanes


def ready_path(inst, n, branch):
    import hashlib
    key = hashlib.sha256(branch.encode()).hexdigest()
    return _m_worktrees.workbench(inst, n) / "scratch" / "ready" / f"{key}.json"


def integration_ref(home):
    """Cached remote default, never a network request or a local-branch fallback for origin."""
    if "origin" in _m_common.git("remote", cwd=home).stdout.split():
        target = _m_common.git("symbolic-ref", "refs/remotes/origin/HEAD", cwd=home).stdout.strip()
        return target if target.startswith("refs/remotes/origin/") else ""
    return "refs/heads/main"


def integration_parent(n, remote_main):
    ref = integration_ref(n["home_path"])
    if n["fm"].get("merge", "local") != "pr" and ref.startswith("refs/remotes/origin/"):
        local_ref = ref.replace("refs/remotes/origin/", "refs/heads/", 1)
        return _m_common.git("rev-parse", "--verify", "--quiet", local_ref + "^{commit}", cwd=n["home_path"]).stdout.strip()
    return remote_main


def main_notice(n):
    home = n["home_path"]
    if not home or not home.exists():
        return
    ref = integration_ref(home)
    if ref == "":
        print("main: origin default branch unavailable; explicitly fetch origin and run git remote set-head origin -a")
        return
    if not ref.startswith("refs/remotes/origin/"):
        return
    short = ref.removeprefix("refs/remotes/")
    remote = _m_common.git("rev-parse", "--verify", "--quiet", ref + "^{commit}", cwd=home)
    if remote.returncode:
        print("main: origin default branch unavailable; explicitly fetch origin and run git remote set-head origin -a")
        return
    name = ref.removeprefix("refs/remotes/origin/")
    counts = _m_common.git("rev-list", "--left-right", "--count", "refs/heads/" + name + "..." + ref, cwd=home)
    if counts.returncode == 0:
        ahead, behind = counts.stdout.split()
        print(f"main: {short} (cached); local {name} is {behind} behind / {ahead} ahead")
        if int(behind):
            print(f"Leave the operator checkout untouched. In the job worktree: git rebase {short}; "
                  "then hand back again. Fetch explicitly first to refresh cached refs.")
            if n["fm"].get("merge", "local") != "pr":
                print("Local merge mode also requires a current default in a coordinator-owned worktree; "
                      "use merge=pr for host integration when the operator owns the default checkout.")
    else:
        print(f"main: {short} (cached); local {name} is missing")


def ready_refs(n, branch):
    home = n["home_path"]
    return tuple(_m_common.git("rev-parse", "--verify", "--quiet", ref + "^{commit}", cwd=home).stdout.strip()
                 for ref in ("refs/heads/" + branch, integration_ref(home)))


def ready_preflight(n, branch, base=None):
    """Test the proposed integration against the cached remote default (local main without origin)."""
    reasons = []
    if not n["external"] or not n["home_path"] or not n["home_path"].exists():
        return ["an external git home on this machine is required"], "", ""
    head, main = ready_refs(n, branch)
    if not head or not main:
        return ["branch or integration main is missing (origin needs a valid cached origin/HEAD)"], head, main
    if base is not None:
        local_main = _m_common.git("rev-parse", "--verify", "--quiet", "refs/heads/main^{commit}", cwd=n["home_path"]).stdout.strip()
        if (n["fm"].get("merge", "local") == "pr" or not isinstance(base, str)
                or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", base)
                or not local_main or _m_common.git("merge-base", "--is-ancestor", base, local_main, cwd=n["home_path"]).returncode):
            reasons.append("train requires a recorded base ancestor of local main in merge=local mode")
        else:
            main = base
    if branch == "main" or ("refs/heads/" + branch ==
                            integration_ref(n["home_path"]).replace("refs/remotes/origin/", "refs/heads/")):
        reasons.append("the job branch must differ from main")
    if _m_common.git("merge-base", "--is-ancestor", main, head, cwd=n["home_path"]).returncode:
        reasons.append("worker must rebase on current main")
        result = _m_common.git("merge-tree", "--write-tree", main, head, cwd=n["home_path"])
        if result.returncode == 1:
            reasons.append("merge-tree is not clean")
        elif result.returncode == 129:
            reasons.append("git is too old for merge-tree --write-tree; Git 2.38 or newer is required")
        elif result.returncode:
            reasons.append(f"merge-tree failed (exit {result.returncode}); merge cleanliness is unknown")
    # A local merge integrates with unpublished local work too; PR mode integrates on the host.
    ref = integration_ref(n["home_path"])
    if n["fm"].get("merge", "local") != "pr" and ref.startswith("refs/remotes/origin/"):
        local_ref = ref.replace("refs/remotes/origin/", "refs/heads/", 1)
        local = _m_common.git("rev-parse", "--verify", "--quiet", local_ref + "^{commit}", cwd=n["home_path"]).stdout.strip()
        if not local:
            reasons.append("local integration branch is missing; configure merge=pr for host merges")
        elif _m_common.git("merge-base", "--is-ancestor", ready_refs(n, branch)[1], local, cwd=n["home_path"]).returncode:
            reasons.append("local default must include origin default for merge=local; coordinator must update it "
                           "in a coordinator-owned worktree, or configure merge=pr; never change the operator checkout")
        elif _m_common.git("merge-base", "--is-ancestor", base or local, head, cwd=n["home_path"]).returncode:
            reasons.append("worker must rebase on current local main as well as origin default for merge=local")
    titles = _m_common.git("log", "--format=%s", main + ".." + head, cwd=n["home_path"])
    if titles.returncode:
        reasons.append("branch commit titles are unreadable")
    else:
        reasons.extend("unfinished commit: " + title for title in titles.stdout.splitlines()
                       if re.match(r"(?:wip|fixup!|squash!)", title, re.IGNORECASE))
    w = n["local"].get("worktrees", {}).get(branch)
    if not w or not Path(w["path"]).exists():
        reasons.append("job worktree is missing")
    else:
        path = w["path"]
        if _m_common.git("symbolic-ref", "--short", "HEAD", cwd=path).stdout.strip() != branch:
            reasons.append("job worktree is on a different branch")
        status = _m_common.git("-c", "diff.ignoreSubmodules=none", "-c", "status.showUntrackedFiles=all", "status", "--porcelain",
                     "--ignore-submodules=none", "--untracked-files=all", cwd=path)
        if status.returncode or status.stdout.strip():
            reasons.append("job worktree is dirty or unreadable")
    return reasons, head, main


def read_ready_packet(inst, n, branch, row=None):
    """New records take precedence; never resurrect stale evidence after a worker replaces it."""
    if row and _m_jobs.handback_file(n, row).exists():
        signal = _m_jobs.read_handback(n, row)
        if not signal:
            return None
        if signal.get("version") in (2, 3):
            packet = signal.get("ready")
            return packet if isinstance(packet, dict) else None
    if not n["external"] or not n["home_path"]:
        return None
    try:
        packet = json.loads(ready_path(inst, n, branch).read_text())
        if not isinstance(packet, dict):
            return None
        if row:
            checked, started = inst.parse_time(packet.get("checked_at")), inst.parse_time(row.get("Started"))
            if not checked or not started or checked < started:
                return None
        return packet
    except (OSError, ValueError):
        return None


def packet_main_matches(n, packet, main):
    """Old v1 packets captured local main; new v1 packets also capture the integration base.

    Interpret the old evidence in place, without rewriting a worker's file. Preflight still
    requires the current base to contain origin's default and the job to contain that base.
    """
    base = integration_parent(n, main)
    expected_main = main if "integration_base" in packet else base
    return (packet.get("main") == expected_main and
            packet.get("integration_base", packet.get("main")) == base)


class ReadyStateResult(dict):
    """Dictionary-compatible result with cache-private policy provenance."""
    def __init__(self, data, policy_reasons=(), head=''):
        super().__init__(data)
        self.policy_reasons = tuple(policy_reasons)
        self.policy_head = head



_DISPLAY_CACHE = contextvars.ContextVar('brain_ui_ready_cache', default=None)


@contextlib.contextmanager
def display_cache(values, keys):
    """A dashboard can reuse display validation for unchanged job inputs; full gates never do."""
    token = _DISPLAY_CACHE.set((values, keys))
    try:
        yield
    finally:
        _DISPLAY_CACHE.reset(token)


def ready_state(inst, n, branch, train=False, display=False):
    cached = _DISPLAY_CACHE.get() if display and not train else None
    if cached is None:
        return _ready_state(inst, n, branch, train, display)
    values, keys = cached
    job = (n['id'], branch)
    key = keys.get(job)
    if key is None:
        return _ready_state(inst, n, branch, train, display)
    previous = values.get(job)
    if previous is None or previous[0] != key:
        result = _ready_state(inst, n, branch, train, display)
        values[job] = (key, result)
    else:
        result = previous[1]
        if isinstance(result, ReadyStateResult):
            policy = ready_policy_reasons(inst, n, branch, result.policy_head, result.get('packet'))
            if tuple(policy) != result.policy_reasons:
                # Preserve cached Git/input facts. Only replace reasons contributed
                # by policy validators, including recovery from a former refusal.
                reasons = [reason for reason in result.get('reasons', []) if reason not in result.policy_reasons]
                reasons.extend(policy)
                result = ReadyStateResult({**result, 'ready': not reasons, 'reasons': reasons}, policy, result.policy_head)
                values[job] = (key, result)
    return values[job][1]


def ready_policy_reasons(inst, n, branch, head, packet=None, preview=True):
    """Use independently owned receipt validators; absent required APIs fail closed."""
    reasons = []
    cfg = getattr(inst, 'cfg', {})
    cfg = cfg if isinstance(cfg, dict) else {}
    for module_name, function in (('.review', 'validate_ready_reviews'),
                                  ('.preview_evidence', 'validate_ready_preview')):
        if module_name == '.preview_evidence' and not preview:
            continue
        try:
            validator = getattr(importlib.import_module(module_name, __package__), function, None)
        except ImportError:
            validator = None
        if validator is not None:
            reasons.extend(validator(inst, n, branch, head, packet=packet))
            continue
        if module_name == '.review':
            row = _m_jobs.open_job(_m_jobs.read_jobs(n) or [], branch)
            required = (n['fm'].get('review_independence', cfg.get('review_independence')) == 'fresh-thread'
                        or (row or {}).get('Review identity required') == 'true')
        else:
            required = (bool(n['local'].get('preview') or n['local'].get('review_urls'))
                        or str(n['fm'].get('requires_preview_evidence', cfg.get('requires_preview_evidence', ''))).lower()
                        in ('true', 'yes', '1', 'on'))
        if required:
            reasons.append(function + ' unavailable; required receipt policy cannot be validated')
    scrubber = VerifyScrubber(_m_guard.never_terms(n))
    return [scrubber.feed(reason, final=True) for reason in reasons]


def current_ready_reasons(reasons):
    """Retire only the former rehearsal gate in already-published hand-backs."""
    legacy = "passing rehearsal missing for this head and main; run brain rehearse"
    return [reason for reason in reasons if isinstance(reason, str) and reason != legacy]


def _ready_state(inst, n, branch, train=False, display=False, advisory=False):
    display = display and (not train or advisory)
    reasons, head, main = ready_preflight(n, branch)
    row = _m_jobs.open_job(_m_jobs.read_jobs(n) or [], branch)
    signal = _m_jobs.read_handback(n, row) if row else None
    packet = read_ready_packet(inst, n, branch, row)
    if train and packet:
        reasons, head, main = ready_preflight(n, branch, base=packet.get("integration_base", packet.get("main")))
        remote = ready_refs(n, branch)[1]
        old_remote = packet.get("main")
        if (not isinstance(old_remote, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", old_remote)
                or _m_common.git("merge-base", "--is-ancestor", old_remote, remote, cwd=n["home_path"]).returncode):
            reasons.append("recorded remote base is not an ancestor of current integration main")
    policy_reasons = ready_policy_reasons(inst, n, branch, head, packet)
    reasons.extend(policy_reasons)
    if row:
        from . import job_attempts as _m_attempts
        attempt = _m_attempts.current_attempt(n, row, strict=False)
        if attempt and (attempt['state'] != 'terminal' or not signal or signal.get('attempt_id') != attempt['id']):
            reasons.append('replacement handback attempt is ' + attempt['state'] + '; prior completion is historical')
    if row and packet:
        try:
            if _m_jobs.train_ejection(n, row, packet):
                reasons.append('train ejected this ready evidence; worker must hand back again')
        except (OSError, ValueError):
            reasons.append('train ejection evidence unreadable; refusing readiness')
    if signal and signal["state"] != "done":
        reasons.append("worker hand-back is " + signal["state"])
    if row and _m_jobs.handback_file(n, row).exists() and not signal:
        reasons.append("worker hand-back unreadable; return to worker")
    if signal and signal.get("version") in (2, 3):
        reasons += current_ready_reasons(signal.get("ready_reasons", []))
    if packet is None:
        reasons.append("ready packet missing or unreadable; worker must record it")
    else:
        if packet.get("version") != 1 or packet.get("node") != n["id"] or packet.get("branch") != branch:
            reasons.append("ready packet identifies a different job or format")
        if packet.get("head") != head:
            reasons.append("branch head changed; return to worker")
        if packet.get('verify_cheap', '') != n['fm'].get('verify_cheap', '').strip():
            reasons.append('cheap verification configuration changed; hand back again')
        if not train and not packet_main_matches(n, packet, main):
            reasons.append("main moved; return to worker to rebase and verify again")
        w = n["local"].get("worktrees", {}).get(branch)
        lanes_configured = (packet.get('verify_scope') != 'change'
                            and bool(n["fm"].get("verify_lanes") or n["fm"].get("verify_lanes_from")))
        cheap = display and lanes_configured and packet.get('verify_scope') != 'full'
        if not cheap and isinstance(w, dict) and w.get("path") and Path(w["path"]).exists():
            files = verify_input_files(n, w)
            if packet.get("inputs") != verify_inputs(n, w, files):  # every readiness reader comes through here
                reasons.append(verify_input_reason(n, read_verify_input_files(n, packet), files))
        verify = n["fm"].get("verify", "").strip()
        if not verify or packet.get("verify_command") != verify or type(packet.get("verify_result")) is not int \
                or packet["verify_result"] != 0:
            reasons.append("node verify missing, changed or failed")
        if not verification_config_valid(n, packet):
            reasons.append('per-change/full verification configuration changed; hand back again')
        port_context = packet.get('verify_port_context')
        rounds = packet.get('review_rounds')
        reverify = shlex.join(['brain', 'handback', n['id'], '--branch', branch, '--state', 'done',
                              '--reviews', str(rounds if type(rounds) is int and rounds >= 1 else 0),
                              '--reverify', '--summary', 'Reverified port environment'])
        if (not isinstance(port_context, dict) or set(port_context) != {'export_port', 'digest'}
                or type(port_context.get('export_port')) is not bool
                or not isinstance(port_context.get('digest'), str)
                or not re.fullmatch(r'[0-9a-f]{24}', port_context['digest'])):
            reasons.append('recorded verify port environment missing or invalid; run ' + reverify)
        elif port_context['export_port'] != (n['fm'].get('verify_export_port') == 'true'):
            reasons.append('verify port export policy changed; run ' + reverify)
        if packet.get('verify_scope') == 'full':
            pass  # The complete command covers every lane, without running its map.
        elif cheap:
            try:
                _m_lanes.recorded_lane_environment({}, packet.get('verify_lane_env'))
            except ValueError as error:
                reasons.append(str(error))
            rows = packet.get('verify_lanes')
            if (packet.get('lane_config_hash') != _m_lanes.lane_config_hash(n) or not isinstance(rows, list)
                    or not rows or any(not isinstance(r, dict) or r.get('status') != 'passed'
                                       or type(r.get('result')) is not int or r['result'] != 0 for r in rows)):
                reasons.append('recorded verify lane evidence missing, changed or failed; hand back again')
        elif packet.get('verify_scope') == 'change':
            pass  # The project-owned selector is bound by verify_config_hash above.
        elif isinstance(w, dict) and w.get("path"):
            try:
                env = verify_env(n, branch, w)
                if lanes_configured or packet.get('verify_lanes') is not None:
                    env = _m_lanes.recorded_lane_environment(env, packet.get('verify_lane_env'))
                snap = _m_lanes.lane_snapshot(n, w, env, verify_inputs(n, w))
                if snap:
                    snap['check'] = n['fm'].get('verify_lanes_check', '').strip()
                    checked, current_head, current_main = ready_preflight(n, branch, base=main if train else None)
                    reasons.extend(checked)
                    if current_head != head or current_main != main:
                        reasons.append('lane map command changed integration refs; hand back again')
                if snap and not lane_packet_valid(packet, snap):
                    reasons.append("verify lane configuration, inputs or results changed; hand back again")
                if not snap and packet.get("verify_lanes") is not None:
                    reasons.append("verify lane configuration removed; hand back again")
            except (ValueError, OSError, UnicodeError, RecursionError) as error:
                reasons.append("verify lanes invalid: " + _m_guard.redact(str(error), _m_guard.never_terms(n)))
        rounds = packet.get("review_rounds")
        if type(rounds) is not int or rounds < 1:
            reasons.append("no completed review rounds recorded")
        if not isinstance(packet.get("checked_at"), str) or not inst.parse_time(packet["checked_at"]):
            reasons.append("packet has no valid check time")
    scrubber = VerifyScrubber(_m_guard.never_terms(n))
    reasons = [scrubber.feed(reason, final=True) for reason in reasons]
    return ReadyStateResult({"ready": not reasons, "reasons": reasons, "packet": packet,
                             "validation": "recorded" if display else "full"}, policy_reasons, head)


def due_ready_state(inst, n, branch):
    """Offer batch advice without promoting old-base proof to current readiness."""
    ordinary = ready_state(inst, n, branch, display=True)
    moved = {"worker must rebase on current main",
             "worker must rebase on current local main as well as origin default for merge=local",
             "main moved; return to worker to rebase and verify again"}
    if (ordinary['ready'] or n['fm'].get('merge', 'local') == 'pr'
            or not ordinary['reasons'] or any(r not in moved for r in ordinary['reasons'])):
        return ordinary
    recorded = _ready_state(inst, n, branch, train=True, display=True, advisory=True)
    if not recorded['ready']:
        return ordinary
    result = _m_common.git('merge-tree', '--write-tree', 'refs/heads/main',
                           'refs/heads/' + branch, cwd=n['home_path'])
    if result.returncode:
        return ordinary  # conflict or unreadable classification keeps every refusal
    return {**ordinary, 'ready': False, 'reasons': [],
            'batch_advice': 'eligible for batch; exact-result verification required'}


VERIFY_INPUTS = ".env,.env.*"  # files git doesn't track that verify reads, unless the node names its own

def verify_inputs(n, w, files=None):
    """Keep the existing digest contract: exact files plus ports decide validity and reuse.
    The optional map lets a caller compare and diagnose the same single snapshot."""
    files = verify_input_files(n, w) if files is None else files
    return _m_jobs.digest([sorted(files.items()), w.get("port"), w.get("ports", 1)]
                          + ([w['env']] if w.get('env') else []))


def verify_reuse(n, branch, previous, head, main, verify, cwd, inputs=None, port_context=None):
    """The earlier green packet, if it still proves this commit: same node, branch, command and local main, and
    nothing changed since the commit it verified except paths the node declares can't affect verify
    (verify_skip globs, e.g. docs/*,*.md). (verified head, changed paths) or None."""
    p = previous if isinstance(previous, dict) else {}
    sha = lambda v: isinstance(v, str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", v)
    if not (verification_config_valid(n, p)
            and p.get("version") == 1 and p.get("node") == n["id"] and p.get("branch") == branch
            and p.get("verify_command") == verify and type(p.get("verify_result")) is int and p["verify_result"] == 0
            and packet_main_matches(n, p, main) and type(p.get("review_rounds")) is int and p["review_rounds"] >= 1
            and isinstance(p.get("checked_at"), str) and sha(p.get("head")) and p.get("inputs") == inputs
            and port_context is not None and p.get('verify_port_context') == port_context
            and p.get('verify_cheap', '') == n['fm'].get('verify_cheap', '').strip()):
        return None  # only a complete green packet, for the same inputs, can stand for a new run
    base = p.get("verified_head") or p["head"]
    if not sha(base) or (base != p["head"] and _m_common.git("merge-base", "--is-ancestor", base, p["head"], cwd=cwd).returncode):
        return None  # the verified commit must be the packet's own commit or one before it
    if base == head:
        return base, []
    diff = _m_common.git("-c", "diff.ignoreSubmodules=none", "diff", "--name-only", "--no-renames", "--ignore-submodules=none",
               base, head, cwd=cwd)
    if diff.returncode:
        return None
    changed = [x for x in diff.stdout.splitlines() if x]
    skip = _m_common.lst(n["fm"].get("verify_skip"))
    if not skip or not all(any(fnmatch.fnmatchcase(x, pat) for pat in skip) for x in changed):
        return None
    return base, changed


VERIFY_PROGRESS_SECONDS = 5
VERIFY_LOG_KEEP = 5


class VerifyScrubber:
    """Keep enough original characters to match a term split across arbitrary pipe/UTF-8 reads.
    Casefold spans map back to original characters (including expanding folds such as sharp s).
    A mask absent from every term cannot introduce a new forbidden word at a replacement edge.
    """
    def __init__(self, terms):
        words = [t.casefold() for t, _ in terms if t]
        self.pattern = re.compile("|".join(re.escape(t) for t in sorted(words, key=len, reverse=True))) if words else None
        self.keep = max(map(len, words), default=0)
        self.mask = next(chr(c) for c in range(0x2588, 0x110000) if all(chr(c).casefold() not in t for t in words))
        self.pending = ""

    def feed(self, text, final=False):
        from bisect import bisect_right
        raw = self.pending + text
        cut = len(raw) if final else max(0, len(raw) - self.keep)
        spans = []
        if self.pattern:
            folded = raw.casefold()
            offsets = None
            if len(folded) != len(raw):
                offsets = [0]
                for c in raw:
                    offsets.append(offsets[-1] + len(c.casefold()))
            for match in self.pattern.finditer(folded):
                start = bisect_right(offsets, match.start()) - 1 if offsets else match.start()
                end = bisect_right(offsets, match.end() - 1) if offsets else match.end()
                spans.append((start, end))
            # Expanding folds can produce overlapping ORIGINAL spans. Move the boundary
            # backwards through all of them, so no earlier span is accidentally split.
            for start, end in reversed(spans):
                if start < cut < end:
                    cut = start
        self.pending = raw[cut:]
        out, pos = [], 0
        for start, end in spans:
            if end <= cut and end > pos:
                start = max(start, pos)
                out.extend((raw[pos:start], self.mask * (end - start)))
                pos = end
        out.append(raw[pos:cut])
        return "".join(out)


def verify_log_dir(inst, n, branch):
    """Pin the node and its hand-back folder; never follow a planted directory link into a checkout."""
    row = _m_jobs.open_job(_m_jobs.read_jobs(n) or [], branch)
    key = _m_jobs.job_key(row) if row else _m_jobs.digest([n["id"], branch])
    parent = n["dir"].resolve()
    roots = []
    for place in _m_history.home_places(n):
        try:
            root_fd = os.open(place.resolve(), _m_history.HISTORY_DIR_FLAGS)
        except FileNotFoundError:
            continue  # an absent folder cannot contain the already-existing node folder
        except OSError:
            _m_common.die("verify can't locate a home/checkout folder; refusing to write logs")
        try:
            location = _m_history.history_fd_path(root_fd)
            if not location:
                _m_common.die("verify can't locate a home/checkout folder; refusing to write logs")
            roots.append(Path(location))  # actual on-disk case, for every protected root as well
        finally:
            os.close(root_fd)

    def check(fd):
        location = _m_history.history_fd_path(fd)
        where = Path(location) if location else None
        if not where or any(where == p or p in where.parents for p in roots):
            _m_common.die("verify log folder is unknown or inside a home/checkout; refusing to run verify")
        if where != parent / "job-handbacks" and where != parent:
            _m_common.die("verify log folder moved; refusing to write")

    try:
        nfd = os.open(parent, _m_history.HISTORY_DIR_FLAGS)
    except OSError:
        _m_common.die("verify log node folder is a link or not a folder")
    try:
        # Canonical on-disk spelling, including case on a case-insensitive filesystem.
        location = _m_history.history_fd_path(nfd)
        if location:
            parent = Path(location)
        check(nfd)
        try:
            os.mkdir("job-handbacks", 0o700, dir_fd=nfd)
        except FileExistsError:
            pass
        try:
            dfd = os.open("job-handbacks", _m_history.HISTORY_DIR_FLAGS, dir_fd=nfd)
        except OSError:
            _m_common.die("verify log hand-back folder is a link or not a folder")
    finally:
        os.close(nfd)
    try:
        check(dfd)
    except BaseException:
        os.close(dfd)
        raise
    return parent / "job-handbacks", dfd, key, check


class _ResourceCommandFailure(Exception):
    pass


@contextlib.contextmanager
def verification_resources(inst, n):
    state = {'failed': False}
    if not n.get('fm', {}).get('verify_resources'):
        yield state
        return
    try:
        module = importlib.import_module('.resource_leases', __package__)
    except ImportError:
        _m_common.die('configured verification resources require the native lease module')
    try:
        with module.configured_resource_lease(inst, n):
            yield state
            if state['failed']:
                raise _ResourceCommandFailure('command failed; resource cleanup remains unconfirmed')
    except _ResourceCommandFailure:
        pass  # preserve the command result while the native lease retains its claim


def verify_cleanup(inst, n):
    try:
        module = importlib.import_module('.docker_cleanup', __package__)
    except ModuleNotFoundError as error:
        if error.name != __package__ + '.docker_cleanup':
            raise
        return
    report = module.docker_cleanup_nodes(inst, [n])
    if report.get('errors'):
        raise ValueError('verification resource cleanup incomplete')


def queued_attempt_phase():
    from . import job_attempts as _m_attempts
    try:
        _m_attempts.attempt_phase('queued')
    except (OSError, ValueError):
        pass  # unreadable phase evidence remains unknown; publication rechecks it


@contextlib.contextmanager
def command_reservation(inst, n, branch=None, kind='verify'):
    """One producer context spans native admission, execution and every cleanup."""
    managed = getattr(_m_machine, 'machine_run', None)
    with managed(kind, inst, n['id'], branch, release=False) if managed else contextlib.nullcontext(None) as run:
        primary = None
        token = run
        try:
            token = (_m_machine.machine_wait(kind, inst, n['id'], job=branch, token=run)
                     if managed else _m_machine.machine_wait(kind, inst, n['id']))
            if kind == 'verify':
                from . import job_attempts as _m_attempts
                _m_attempts.attempt_phase('verifying')
            yield token
        except BaseException as error:
            primary = error
            raise
        finally:
            actions = []
            if token is not None:
                actions.append(lambda: _m_machine.machine_release(token))
            if kind == 'verify':
                actions.append(queued_attempt_phase)
            actions.extend([lambda: verify_cleanup(inst, n),
                            lambda: _m_docker_leftovers.docker_leftover_nodes(inst, [n])])
            if managed:
                finish = getattr(_m_machine, 'machine_finish_intent', None)
                if finish is not None:
                    actions.insert(0, lambda: finish(inst, run, primary))
                failure = primary or (_ResourceCommandFailure('nonzero command result') if run.result else None)
                _m_machine.machine_cleanup(run, actions, primary=failure)
            else:
                errors = []
                for action in actions:
                    try:
                        action()
                    except BaseException as error:
                        errors.append(error)
                if errors and primary is None:
                    raise errors[0]


@contextlib.contextmanager
def verify_reservation(inst, n, branch=None):
    with command_reservation(inst, n, branch) as token:
        yield token


def run_cheap_checks(inst, n, branch, w, env):
    command = n['fm'].get('verify_cheap', '').strip()
    if not command:
        return None
    with command_reservation(inst, n, branch, kind='heavy') as token:
        return run_verify_stream(inst, n, branch, command, w, env, token, lane='cheap')


def run_verify(inst, n, branch, verify, w, env):
    with _m_gate_lock.node_gate(inst, n, env, branch) as gate_env, verification_resources(inst, n) as resources:
        if verify in {n.get('fm', {}).get(key, '').strip() for key in ('verify', 'verify_change')}:
            cheap = run_cheap_checks(inst, n, branch, w, gate_env)
            if cheap and cheap[0]:
                resources['failed'] = True
                return cheap
        with verify_reservation(inst, n, branch) as token:
            result = run_verify_stream(inst, n, branch, verify, w, gate_env, token)
            resources['failed'] = bool(result[0])
            return result


def run_verify_stream(inst, n, branch, verify, w, env, token, lane=None):
    """Drain the same combined stdout/stderr pipe as before; stdin, environment and shell stay unchanged.
    Only redacted text reaches disk or failure tails. Memory holds at most 15 bounded
    lines per named lane, plus an overall tail and the scrubber's protected suffix.
    """
    import codecs
    import io
    import selectors
    from collections import deque
    folder, dfd, key, check = verify_log_dir(inst, n, branch)
    job_key = key
    if lane is not None:
        key += "-" + hashlib.sha256(lane.encode()).hexdigest()[:16]
    terms = _m_guard.never_terms(n)
    scrubber = VerifyScrubber(terms)
    say = lambda text: print(VerifyScrubber(terms).feed(text, final=True), flush=True)
    name = key + "-verify-" + str(time.time_ns()) + "-" + os.urandom(6).hex() + ".log"
    proc = None
    try:
        check(dfd)
        fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=dfd)
        with os.fdopen(fd, "w+", encoding="utf-8") as log:
            os.fchmod(log.fileno(), 0o600)
            say("verify log: " + str(folder / name))
            # Never touch arbitrary entries or another job's logs. Unlinking a link never follows it.
            old = sorted(x for x in os.listdir(dfd) if x != name and
                         re.fullmatch(re.escape(key) + r"-verify-\d+-[0-9a-f]{12}\.log", x))
            # Keep this run even if the wall clock moved backwards since older runs.
            for entry in old[:max(0, len(old) - VERIFY_LOG_KEEP + 1)]:
                check(dfd)
                os.unlink(entry, dir_fd=dfd)
            # One input map belongs to one retained log. Also retire legacy unpaired maps.
            retained_maps = {verify_input_map_name(job_key, entry) for entry in os.listdir(dfd)
                             if re.fullmatch(re.escape(job_key) + r"(?:-[0-9a-f]{16})?-verify-\d+-[0-9a-f]{12}\.log", entry)}
            for entry in os.listdir(dfd):
                if re.fullmatch(re.escape(job_key) + r"-inputs-[0-9a-f]{24}\.map", entry) and entry not in retained_maps:
                    check(dfd)
                    os.unlink(entry, dir_fd=dfd)
            overall, lanes, failed = deque(maxlen=15), {}, set()
            pending, lines, chars = "", 0, 0
            line_decoder = io.IncrementalNewlineDecoder(None, translate=True)

            def consume(text, final=False):
                nonlocal pending, lines, chars
                check(dfd)
                log.write(text)
                log.flush()
                chars += len(text)
                display = line_decoder.decode(text, final=final)
                lines += display.count("\n")
                pending += display
                while "\n" in pending or len(pending) > 16384:
                    if "\n" in pending[:16385]:
                        line, pending = pending.split("\n", 1)
                    else:
                        line, pending = pending[:16384], pending[16384:]
                    tail(line)

            def tail(line):
                overall.append(line)
                match = re.match(r"^\[lane ([^]\r\n]{1,120})\]\s?(.*)", line)
                if match:
                    lane, body = match.groups()
                    lanes.setdefault(lane, deque(maxlen=15)).append(line)
                    if re.search(r"\b(?:FAIL(?:ED|URE)?|ERROR)\b|\bexit[ =:]+[1-9]\d*\b", body, re.I):
                        failed.add(lane)

            started = time.monotonic()
            deadline = started + VERIFY_PROGRESS_SECONDS
            # The bootstrap cannot exec the verify until its PID/start stamp is durable.
            # EOF before publication means no verify starts. The bootstrap FD closes
            # before exec; only the validated node gate FD survives into the real gate.
            node_gate_fd = _m_gate_lock.gate_inherited_fd(inst.root, env, n['id'])
            from . import handback_queue as _m_queue
            queue_fd = _m_queue.queue_descendant_fd()
            gate_read, gate_write = os.pipe()
            bootstrap = ("import json,os,sys,signal; fd=int(sys.argv[1]); data=bytearray()\n"
                         "while True:\n"
                         " chunk=os.read(fd,8192)\n"
                         " if not chunk: break\n"
                         " data.extend(chunk)\n"
                         "os.close(fd)\n"
                         "if not data: os._exit(125)\n"
                         "command,env=json.loads(data)\n"
                         "for name in ('SIGPIPE','SIGXFZ','SIGXFSZ'):\n"
                         " if hasattr(signal,name): signal.signal(getattr(signal,name),signal.SIG_DFL)\n"
                         "os.execve('/bin/sh',['/bin/sh','-c',command],env)")
            try:
                proc = subprocess.Popen([sys.executable, "-I", "-S", "-c", bootstrap, str(gate_read)],
                                        cwd=w["path"], env=env, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, pass_fds=(gate_read,) +
                                        ((node_gate_fd,) if node_gate_fd is not None else ()) +
                                        ((queue_fd,) if queue_fd is not None else ()))
                os.close(gate_read)
                gate_read = None
                _m_machine.machine_child(token, proc.pid)
                from . import owned_runs
                owned_runs.queue_child(proc.pid)
                payload = memoryview(json.dumps([verify, env]).encode())
                while payload:
                    payload = payload[os.write(gate_write, payload):]
            finally:
                if gate_read is not None:
                    os.close(gate_read)
                os.close(gate_write)
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    for selected, _ in selector.select(max(0, deadline - time.monotonic())):
                        data = os.read(selected.fileobj.fileno(), 8192)
                        if data:
                            consume(scrubber.feed(decoder.decode(data)))
                        else:
                            selector.unregister(selected.fileobj)
                            consume(scrubber.feed(decoder.decode(b"", final=True), final=True), final=True)
                    now = time.monotonic()
                    if now >= deadline:
                        say(f"verify: running {now - started:.0f}s; {lines} lines, {chars} characters logged")
                        deadline = now + VERIFY_PROGRESS_SECONDS
            # A command may close stdout and continue running: progress still matters then.
            while proc.poll() is None:
                try:
                    proc.wait(timeout=max(0.01, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    now = time.monotonic()
                    say(f"verify: running {now - started:.0f}s; {lines} lines, {chars} characters logged")
                    deadline = now + VERIFY_PROGRESS_SECONDS
            if pending:
                tail(pending)
            if proc.returncode:
                say(f"verify failed (exit {proc.returncode}); output tail:")
                for lane in sorted(failed):
                    say("verify failing lane: " + lane)
                    say("\n".join(lanes[lane]))
                # Always retain context for failures outside the named convention as well.
                say("verify overall tail:")
                say("\n".join(overall))
            if hasattr(token, 'result'):
                token.result = proc.returncode
            return proc.returncode, str(folder / name)
    finally:
        if proc is not None:
            if proc.poll() is None:
                proc.kill()  # subprocess.run also kills on exceptions; SIGTERM can be ignored
                proc.wait()
            proc.stdout.close()
        os.close(dfd)


def verify_env(n, branch, w):
    """Expose reserved ports without claiming generic application environment names."""
    ports = {"PORT": str(w["port"]), "PORTS": str(w.get("ports", 1)),
             "PORT_LAST": str(w["port"] + w.get("ports", 1) - 1)}
    from . import job_env as _m_job_env
    overrides = _m_job_env.apply_job_env(os.environ, w)
    env = {**overrides, "MAIN": str(n["home_path"]), "WORKTREE": w["path"], "BRANCH": branch,
           "NODE": n["id"], **{"BRAIN_" + key: value for key, value in ports.items()}}
    if n["fm"].get("verify_export_port") == "true":
        for key, value in ports.items():
            env.setdefault(key, value)  # An operator's explicit value always wins, even if empty.
    return env


def verify_port_context(n, env):
    """Bind effective aliases, including unset/empty, without publishing their values."""
    return dict(export_port=n['fm'].get('verify_export_port') == 'true',
                digest=_m_jobs.digest([(key, env.get(key)) for key in ('PORT', 'PORTS', 'PORT_LAST')]))


def lane_packet_valid(packet, snap):
    rows = packet.get("verify_lanes")
    expected = [l for l in snap['lanes'] if l['schedule'] != 'nightly']
    if (packet.get('lane_context_hash') != snap['evidence_context'] or packet.get('lane_map_hash') != snap['map_hash']
            or not isinstance(rows, list) or len(rows) != len(expected)):
        return False
    for row, lane in zip(rows, expected):
        if (not isinstance(row, dict) or row.get('name') != lane['name'] or row.get('command') != lane['command']
                or row.get('input_hash') != snap['hashes'][lane['name']] or type(row.get('result')) is not int
                or row['result'] != 0 or row.get('status') != 'passed' or not lane_evidence_valid(row)):
            return False
    check = packet.get('verify_lanes_check')
    return not snap.get('check') or isinstance(check, dict) and type(check.get('result')) is int and check['result'] == 0


def lane_evidence_valid(row):
    duration = row.get('duration')
    return (isinstance(duration, (int, float)) and not isinstance(duration, bool)
            and math.isfinite(duration) and duration >= 0 and isinstance(row.get('log'), str)
            and bool(row['log']) and isinstance(row.get('verified_head'), str)
            and bool(re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', row['verified_head'])))


def run_lanes(inst, n, branch, w, env, snap, previous, head, main, inputs, force=False):
    with _m_gate_lock.node_gate(inst, n, env, branch) as env, verification_resources(inst, n) as resources:
        prior = previous if isinstance(previous, dict) else {}
        eligible = (prior.get('version') == 1 and prior.get('node') == n['id']
                    and prior.get('branch') == branch and prior.get('verify_command') == n['fm'].get('verify', '').strip()
                    and isinstance(prior.get('checked_at'), str) and inst.parse_time(prior['checked_at'])
                    and packet_main_matches(n, prior, main)
                    and prior.get('lane_context_hash') == snap['evidence_context'] and prior.get('inputs') == inputs
                    and prior.get('verify_port_context') == verify_port_context(n, env)
                    and type(prior.get('review_rounds')) is int and prior['review_rounds'] >= 1)
        history = prior.get('lane_green', prior.get('verify_lanes', []))
        old = {r['name']: r for r in history if isinstance(r, dict) and isinstance(r.get('name'), str)} if eligible and isinstance(history, list) else {}
        rows, results, check = [], {}, None
        cheap = run_cheap_checks(inst, n, branch, w, env)
        if cheap and cheap[0]:
            resources['failed'] = True
            return dict(verify_lanes=[], verify_result=cheap[0], verify_reused=False,
                        verify_lanes_check=dict(command=n['fm']['verify_cheap'], result=cheap[0],
                                                duration=0, log=cheap[1]))
        command = n['fm'].get('verify_lanes_check', '').strip()
        if command:
            with command_reservation(inst, n, branch, kind='heavy') as token:
                start = time.monotonic()
                result, log = run_verify_stream(inst, n, branch, command, w, env, token, lane='fitness')
                check = dict(command=command, result=result, duration=time.monotonic()-start, log=log)
        for lane in snap['lanes']:
            if lane['schedule'] == 'nightly':
                continue
            name = lane['name']
            previous_lane = old.get(name, {})
            changed = _m_lanes.changed_since(w['path'], previous_lane.get('verified_head'), head) if previous_lane else None
            reuse = (not force and eligible and type(previous_lane.get('result')) is int and previous_lane['result'] == 0
                     and previous_lane.get('status') == 'passed' and lane_evidence_valid(previous_lane)
                     and Path(previous_lane['log']).is_file() and previous_lane.get('command') == lane['command']
                     and previous_lane.get('input_hash') == snap['hashes'][name] and changed is not None
                     and not lane['always'] and not any(_m_lanes.matches(p, lane['strict_inputs']) for p in changed))
            row = dict(name=name, command=lane['command'], input_hash=snap['hashes'][name],
                       result=None, duration=0, log=None, reused=False, verified_head=head, status='skipped')
            if check and check['result'] or any(results.get(req) != 0 for req in lane['requires']):
                pass
            elif reuse:
                row.update(previous_lane, reused=True)
            else:
                with verify_reservation(inst, n, branch) as token:
                    print('verify lane: ' + name, flush=True)
                    start = time.monotonic()
                    result, log = run_verify_stream(inst, n, branch, lane['command'], w, env, token, lane=name)
                    row.update(result=result, log=log, duration=time.monotonic()-start,
                               status='passed' if result == 0 else 'failed')
            if row['result'] == 0:
                old[name] = dict(row)
            rows.append(row)
            results[name] = row['result']
        result = next((r['result'] or 1 for r in rows if r['result'] != 0), 0)
        if check and check['result']:
            result = check['result']
        resources['failed'] = bool(result)
        return dict(verify_lanes=rows, lane_green=list(old.values()), lane_map_hash=snap['map_hash'], lane_config_hash=snap['config_hash'], lane_context_hash=snap['evidence_context'],
                    verify_lane_env=_m_lanes.lane_environment(env),
                    verify_lanes_check=check, verify_result=result,
                    verify_reused=bool(rows) and all(r['reused'] for r in rows) and not check)


def show_lanes(n, packet):
    rows = (packet or {}).get('verify_lanes', [])
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict):
            text = 'verify lane %s: %s (exit %s)%s; %.3fs; log: %s' % (row.get('name'), row.get('status'), row.get('result'),
                    ' (reused)' if row.get('reused') else '', row.get('duration') if isinstance(row.get('duration'), (int, float)) else 0, row.get('log') or 'none')
            print(_m_guard.redact(text, _m_guard.never_terms(n)))
    check = (packet or {}).get('verify_lanes_check')
    if isinstance(check, dict):
        print(_m_guard.redact('verify lanes check: exit %s; log: %s' % (check.get('result'), check.get('log')), _m_guard.never_terms(n)))


def record_full_fact(inst, n, branch, head, base, inputs, forced=False):
    """Only actual successful full execution emits this optional typed observation."""
    try:
        events = importlib.import_module('.events', __package__)
    except ModuleNotFoundError as error:
        if error.name == __package__ + '.events':
            return False
        raise
    row = _m_jobs.open_job(_m_jobs.read_jobs(n) or [], branch)
    if not row:
        return False
    events.record_fact(inst, n, 'verify_completed', job_key=_m_jobs.job_key(row),
                       state='done', phase='execution', head=head, base=base, inputs=inputs,
                       reason_code='forced_reverify' if forced else 'full_pass')
    return True


def moved_ref_details(n, branch, head, head_after, main, main_after, integration_base, integration_after):
    ref = integration_ref(n['home_path'])
    local_ref = (ref.replace('refs/remotes/origin/', 'refs/heads/', 1)
                 if n['fm'].get('merge', 'local') != 'pr' else ref)
    return list(dict.fromkeys(
        '%s %s -> %s' % (name, old or 'missing', new or 'missing')
        for name, old, new in (('refs/heads/' + branch, head, head_after),
                               (ref, main, main_after), (local_ref, integration_base, integration_after))
        if old != new))


def verification_config_valid(n, packet):
    explicit = packet.get('verify_scope') in ('change', 'full')
    if n['fm'].get('verify_change', '').strip() or explicit:
        return explicit and packet.get('verify_config_hash') == _m_lanes.lane_config_hash(n)
    return packet.get('verify_scope') is None


def record_ready(inst, n, branch, reviews, previous=None, reverify=False, full_receipt=None, full=False):
    reasons, head, main = ready_preflight(n, branch)
    integration_base = integration_parent(n, main) if head and main else main
    change = n['fm'].get('verify_change', '').strip()
    # Explicit complete gates bypass selection. Otherwise the project-owned
    # per-change command owns readiness; train supplies the landing unit's full run.
    full = full or full_receipt is not None
    change = change if not full else ''
    preview_context = {'integration_base': integration_base, 'verify_scope': 'change' if change else 'full'}
    # A supplied full receipt owns the map environment. Check its preview
    # policy after adoption loads that packet; review and preflight still run.
    reasons.extend(ready_policy_reasons(inst, n, branch, head, preview_context, preview=full_receipt is None))
    from . import rehearse as _m_rehearse
    rehearsal = _m_rehearse.read_evidence(n, branch, head)
    verify = n["fm"].get("verify", "").strip()
    if not verify:
        reasons.append("node has no verify command; coordinator must set verify=<command>")
    if reviews is None or reviews < 1:
        reasons.append("--record requires --reviews N completed review rounds (at least one)")
    if reasons:
        return None, reasons
    from . import preview_evidence as _m_preview
    preview_scope = (None if full_receipt is not None else
                     _m_preview.preview_requirement(inst, n, branch, head, preview_context))
    w = n["local"]["worktrees"][branch]
    input_files = verify_input_files(n, w)
    inputs = verify_inputs(n, w, input_files)
    if previous and previous.get("integration_base", previous.get("main")) != integration_base:
        reverify = True
    env = verify_env(n, branch, w)
    env = {**env, 'BRAIN_ROOT': str(inst.root), 'BRAIN_VERIFY_BASE': integration_base, 'BRAIN_VERIFY_HEAD': head,
           'BRAIN_VERIFY_SCOPE': 'change' if change else 'full'}
    lane_env = _m_lanes.lane_environment(env)
    preview_context['verify_lane_env'] = lane_env
    port_context = verify_port_context(n, env)
    try:
        snap = None if full or change else _m_lanes.lane_snapshot(n, w, env, inputs)
        if snap:
            _m_guard.no_terms_in(n, json.dumps(snap['lanes'], ensure_ascii=False), 'verify lane map')
            snap['check'] = n['fm'].get('verify_lanes_check', '').strip()
            for warning in snap['warnings']:
                print(_m_guard.redact('verify warning: ' + warning, _m_guard.never_terms(n)))
    except (ValueError, OSError, UnicodeError, RecursionError) as error:
        return None, ['verify lanes invalid: ' + _m_guard.redact(str(error), _m_guard.never_terms(n))]
    if full_receipt is not None:
        if reverify:
            return None, ['full receipt cannot be combined with --reverify']
        from . import full_gate as _m_full_gate
        try:
            packet = _m_full_gate.adopt_full_receipt(inst, n, branch, full_receipt, head, main,
                                                   inputs, port_context, snap, reviews)
        except (OSError, ValueError, KeyError, TypeError):
            return None, ['full receipt missing, changed, failed or stale; run full-run on this exact candidate']
        preview_reasons = _m_preview.validate_ready_preview(inst, n, branch, head, packet)
        if preview_reasons:
            return None, preview_reasons
        return {**packet, 'preview_scope': _m_preview.preview_requirement(inst, n, branch, head, packet)}, []
    reuse = None if full or reverify or snap or (previous or {}).get('verify_lanes') is not None else verify_reuse(n, branch, previous, head, main, verify, w["path"], inputs, port_context)
    # Do not sweep an operator's dirty .gitignore into
    # the worker hand-back commit just because we added the local-output exclusion.
    gi = inst.root / ".gitignore"
    with _m_instance.brain_lock(inst.root):
        dirty_ignore = _m_common.git("status", "--porcelain", "--", ".gitignore", cwd=inst.root).stdout.strip()
        _m_nodes.ensure_gitignore(inst.root)
        if dirty_ignore:
            _m_common.TOUCHED.discard(gi)
    _m_instance.unlock()  # a suspended outer CLI scope must not retain this write lock
    if reuse:
        input_ref = (previous.get("input_files_ref") if read_verify_input_files(n, previous) == input_files
                     else write_verify_input_files(inst, n, branch, inputs, input_files, previous.get("verify_log")))
        base, changed = reuse
        print(f"verify: reused the green run of {base[:12]}"
              + (f": since then only {len(changed)} file(s) under verify_skip changed" if changed else
                 ": nothing changed since") + " (--reverify runs it again)")
        return {**{key: previous[key] for key in ('verify_scope', 'verify_config_hash') if key in previous},
                "version": 1, "node": n["id"], "branch": branch, "head": head, "main": main,
                "integration_base": integration_base, "verify_command": verify, "verify_cheap": n["fm"].get("verify_cheap", "").strip(), "verify_result": 0, "verified_head": base, "verify_reused": True,
                "inputs": inputs, "input_files_ref": input_ref, "verify_log": previous.get("verify_log"), "rehearsal": rehearsal,
                "verify_port_context": port_context,
                "preview_scope": preview_scope,
                "verify_lane_env": lane_env,
                "review_rounds": reviews, "checked_at": _m_jobs.stamp(inst)}, []
    _m_instance.unlock()  # verification belongs to this worker, not the instance-wide lock
    lane_evidence = run_lanes(inst, n, branch, w, env, snap, previous, head, main, inputs, reverify) if snap else None
    if lane_evidence:
        result = lane_evidence['verify_result']
        log_path = next((r['log'] for r in reversed(lane_evidence['verify_lanes']) if r.get('log')), None)
    else:
        result, log_path = run_verify(inst, n, branch, change or verify, w, env)
    after, head_after, main_after = ready_preflight(n, branch)
    after_files = verify_input_files(n, w)
    input_reasons = ([verify_input_reason(n, input_files, after_files, during=True)]
                     if verify_inputs(n, w, after_files) != inputs else [])
    if snap:
        try:
            after_snap = _m_lanes.lane_snapshot(n, w, env, verify_inputs(n, w))
            if after_snap != {k: v for k, v in snap.items() if k != 'check'}:
                after.append('verify lane map, toolchain or input files changed during verify')
        except (ValueError, OSError, UnicodeError, RecursionError):
            after.append('verify lane inputs became unreadable during verify')
    integration_after = integration_parent(n, main_after)
    if after or head_after != head or main_after != main or integration_after != integration_base:
        # What passed isn't the commit on main that was asked about: never record it as evidence.
        moved = moved_ref_details(n, branch, head, head_after, main, main_after,
                                 integration_base, integration_after)
        return None, input_reasons + ["verify changed the job's branch, local main or worktree ("
                      + "; ".join(after + moved)
                      + "); commit or discard what it changed, then hand back again"]
    if input_reasons:
        return None, input_reasons
    if _m_preview.preview_requirement(inst, n, branch, head, preview_context) != preview_scope:
        return None, ['preview classification changed during verify; hand back again']
    policy_after = ready_policy_reasons(inst, n, branch, head, preview_context)
    if policy_after:
        return None, policy_after
    input_ref = write_verify_input_files(inst, n, branch, inputs, input_files, log_path)
    packet = {"version": 1, "node": n["id"], "branch": branch, "head": head, "main": main,
              "integration_base": integration_base, "verify_command": verify, "verify_cheap": n["fm"].get("verify_cheap", "").strip(), "verify_result": result, "verify_log": log_path, "verified_head": head,
              "inputs": inputs, "input_files_ref": input_ref, "review_rounds": reviews, "checked_at": _m_jobs.stamp(inst), "rehearsal": rehearsal}
    packet['verify_port_context'] = port_context
    packet['preview_scope'] = preview_scope
    packet['verify_lane_env'] = lane_env
    if full or change:
        packet.update(verify_scope='change' if change else 'full',
                      verify_config_hash=_m_lanes.lane_config_hash(n))
    if lane_evidence:
        packet.update(lane_evidence)
        show_lanes(n, packet)
    elif result == 0 and not change:
        record_full_fact(inst, n, branch, head, main,
                         _m_jobs.digest([verify, inputs, port_context]), forced=reverify)
    return packet, []


def cmd_ready(a):
    if a.reverify and not a.record:
        _m_common.die("--reverify forces a verify run: it goes with --record --reviews N")
    if a.record:  # compatibility alias: one writer, one record, same verification
        a.state = "done"
        return _m_jobs.cmd_handback(a)
    inst = _m_nodes.load()
    main_notice(inst.node(a.id))
    n = inst.node(a.id)
    state = ready_state(inst, n, a.branch)
    show_lanes(n, state["packet"])
    print(VerifyScrubber(_m_guard.never_terms(n)).feed(
        "ready" if state["ready"] else "not ready: " + "; ".join(state["reasons"]), final=True))
    if not state["ready"]:
        sys.exit(1)

def verify_input_files(n, w):
    """Private per-path evidence. Contents never leave this reader; hashes are never diagnostics."""
    root = Path(w["path"])
    found = {}
    for pat in _m_common.lst(n["fm"].get("verify_inputs")) or _m_common.lst(VERIFY_INPUTS):
        for f in root.glob(pat):
            rel = f.relative_to(root)
            if ".git" in rel.parts or not f.is_file():
                continue
            try:
                found[str(rel)] = hashlib.sha256(f.read_bytes()).hexdigest()
            except OSError:
                found[str(rel)] = "unreadable"
    return found


def verify_input_reason(n, before, after, during=False):
    """Explain input differences by relative path/action only. Old packets cannot name past paths."""
    terms = _m_guard.never_terms(n)
    scrubber = VerifyScrubber(terms)
    def valid_map(value):
        return isinstance(value, dict) and all(
            isinstance(k, str) and k not in ("", ".") and not Path(k).is_absolute()
            and ".." not in Path(k).parts and str(Path(k)) == k and isinstance(v, str)
            for k, v in value.items())
    if not valid_map(before) or not valid_map(after):
        text = ("verify_inputs changed since the last verify (or the packet predates this check); "
                "per-path evidence unavailable; hand back again")
    else:
        changes = []
        for path in sorted(before.keys() | after.keys()):
            if path in before and path in after and before[path] == after[path]:
                continue
            action = "added" if path not in before else "removed" if path not in after else "modified"
            screened = scrubber.feed(path, final=True)
            shown = screened if all(c.isprintable() for c in screened) else json.dumps(screened, ensure_ascii=True)
            changes.append(shown + " (" + action + ")")
        if changes:
            text = ("verify modified its own inputs: " if during else
                    "verify_inputs changed since the last verify: ") + ", ".join(changes)
            if during:
                text += ("; make worktree-setup.sh produce these files before the first verify, or stop the "
                         "test runner writing them; remove them from verify_inputs only if they cannot "
                         "affect the result; hand back again")
            else:
                text += "; hand back again"
        else:
            text = ("verify_inputs or the job's ports changed since the last verify "
                    "(or the packet predates this check); no file differences identified; hand back again")
    return scrubber.feed(text, final=True)


def read_verify_input_files(n, packet):
    """Private evidence is optional for diagnostics, never a substitute for the packet's digest."""
    name = packet.get("input_files_ref") if isinstance(packet, dict) else None
    if not isinstance(name, str) or not re.fullmatch(r"[0-9a-f]{24}-inputs-[0-9a-f]{24}\.map", name):
        return None
    folder = n["dir"] / "job-handbacks"
    fh, _ = _m_history.history_open(folder / name, [folder.resolve()], _m_history.home_places(n))
    if fh is None:
        return None
    try:
        with fh:
            st = os.fstat(fh.fileno())
            if st.st_nlink != 1 or st.st_mode & 0o077:
                return None
            data = json.load(fh)
        if isinstance(data, dict) and data.get("version") == 1 and data.get("inputs") == packet.get("inputs"):
            return data.get("files")
    except (OSError, ValueError):
        pass
    return None


def verify_input_map_name(key, log_name):
    return key + "-inputs-" + hashlib.sha256(log_name.encode()).hexdigest()[:24] + ".map"


def write_verify_input_files(inst, n, branch, inputs, files, log_path):
    """One atomic 0600 map per verify log, with the same retention as that log."""
    folder, dfd, key, check = verify_log_dir(inst, n, branch)
    temp = key + "-inputs-" + os.urandom(12).hex() + ".tmp"
    try:
        log_name = Path(log_path).name if isinstance(log_path, str) else ""
        if not re.fullmatch(re.escape(key) + r"(?:-[0-9a-f]{16})?-verify-\d+-[0-9a-f]{12}\.log", log_name):
            return None  # legacy evidence without a log cannot carry a retained map
        name = verify_input_map_name(key, log_name)
        check(dfd)
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=dfd)
        with os.fdopen(fd, "w") as fh:
            os.fchmod(fh.fileno(), 0o600)
            check(dfd)
            fh.write(json.dumps({"version": 1, "inputs": inputs, "files": files}) + "\n")
        check(dfd)
        os.replace(temp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        return name
    finally:
        try:
            os.unlink(temp, dir_fd=dfd)
        except FileNotFoundError:
            pass
        os.close(dfd)
