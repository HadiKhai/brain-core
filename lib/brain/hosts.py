"""Hosts mechanics for Brain."""
import datetime
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

from . import common as _m_common
from . import guard as _m_guard
from . import instance as _m_instance
from . import jobs as _m_jobs
from . import nodes as _m_nodes
from . import status as _m_status
from . import transitions as _m_transitions
from . import upgrade as _m_upgrade


# Wake state belongs to the instance, never to an external home. Only opaque reason
# digests persist here; transport bodies contain no transcript, node name or question.
WAKE_INTERVAL = 120
WAKE_TIMEOUT = 10
WAKE_DEDUP_SECONDS = 86400
WAKE_USER_AGENT = "brain-wake/1"


def wake_target(inst, n):
    target = n if n["local"].get("coordinator") else inst.node("root")
    return target, target["local"].get("wake") or inst.local_of("root").get("wake_default", "")


def wake_directory(inst, n):
    return inst.root / ".brain" / "wake" / urllib.parse.quote(n["id"], safe="")


def wake_start(inst, n):
    env = {**os.environ, "BRAIN_ROOT": str(inst.root), "BRAIN_NO_REEXEC": "1"}
    env.pop("BRAIN_LOCK_HELD", None)  # a detached worker never inherits its requester's lock bypass
    subprocess.Popen([sys.executable, str(_m_common.CORE / "bin" / "brain"), "wake", n["id"], "--drain"],
                     cwd=inst.root, env=env,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True)


def wake_recorded(inst, n, reason):
    target, _ = wake_target(inst, n)
    key = hashlib.sha256((n['id'] + '\0' + reason).encode()).hexdigest()
    directory = wake_directory(inst, target)
    return any((directory / (key + suffix)).exists() for suffix in ('.pending', '.sent'))


def wake_item_refs(items):
    """Persist identities and kinds only, never excerpts or raw source identifiers."""
    return sorted({i['id']: {'id': i['id'], 'kind': i['kind']} for i in items}.values(), key=lambda i: i['id'])


def wake_history(directory):
    path = directory / 'history.json'
    if not path.exists():
        return []
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or value.get('version') != 1 or not isinstance(value.get('deliveries'), list):
        raise ValueError('invalid wake history')
    for row in value['deliveries']:
        if (not isinstance(row, dict) or type(row.get('at')) not in (int, float)
                or not isinstance(row.get('items'), list)
                or any(not isinstance(row.get(key, []), list) for key in ('before', 'snapshot'))
                or any(not isinstance(i, dict) or not isinstance(i.get('kind'), str)
                       or not re.fullmatch(r'[0-9a-f]{64}', str(i.get('id', '')))
                       for key in ('items', 'before', 'snapshot') for i in row.get(key, []))):
            raise ValueError('invalid wake history')
    return value['deliveries']


def wake_delivery_items(inst, n, pending):
    """Legacy/direct requests have empty queue files; resolve their opaque reason keys."""
    from . import watch as _m_watch
    refs = []
    names = set()
    for path in pending:
        try:
            value = json.loads(path.read_text())
            items = value['items']
            if not isinstance(items, list):
                raise ValueError('invalid wake metadata')
            refs.extend(wake_item_refs(items))
        except (OSError, ValueError, KeyError, TypeError):
            names.add(path.stem)
    if not names:
        return wake_item_refs(refs)
    try:
        items = _m_watch.watch_groups(inst).get(n['id'], {}).get('items', [])
    except (Exception, SystemExit):
        items = []  # legacy metadata recovery must not block a question's transport
    for i in items:
        if 'reason' in i:
            key = hashlib.sha256((i.get('source', n['id']) + '\0' + i['reason']).encode()).hexdigest()
            if key in names:
                refs.append(i)
    return wake_item_refs(refs)


def wake_reason_items(inst, n, reason):
    """Capture a direct question/hand-back before it can be handled during rate wait."""
    if not reason.startswith(('wake:', 'handback:', 'overdue:', '<!--')):
        return []  # a manual reason need not correspond to a due item
    from . import watch as _m_watch
    target, _ = wake_target(inst, n)
    try:
        items = _m_watch.watch_groups(inst).get(target['id'], {}).get('items', [])
    except (Exception, SystemExit):
        items = []  # best-effort identity capture cannot suppress the direct wake
    return wake_item_refs(i for i in items
                          if i.get('source', target['id']) == n['id'] and i.get('reason') == reason)


def wake_snapshot(inst, n, refs):
    from . import watch as _m_watch
    try:
        items = _m_watch.watch_groups(inst).get(n['id'], {}).get('items', [])
    except (Exception, SystemExit):
        items = []  # a display snapshot must never gate delivery
    return wake_item_refs(items + refs)


def wake_previous_items(directory):
    """Use the same baseline while a host starts its turn and after send returns."""
    import fcntl
    history = wake_history(directory)
    baseline = history[-1].get('before', []) if history else []
    try:
        with (directory / 'lock').open('r') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # Only trust an in-flight comparison while a drain owns delivery.
                value = json.loads((directory / 'inflight.json').read_text())
                refs = value['before']
                if isinstance(refs, list) and wake_item_refs(refs) == refs:
                    return refs
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return baseline


def request_wake(inst, n, reason, items=None):
    """Fail open, enqueue then detach; never run a transport in the hook's process."""
    try:
        target, command = wake_target(inst, n)
        if not command or _m_common.outward_links(inst.root):
            return False
        directory = wake_directory(inst, target)
        directory.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256((n["id"] + "\0" + reason).encode()).hexdigest()
        pending = directory / (key + ".pending")
        delivered = directory / (key + ".sent")
        if delivered.exists() and time.time() - delivered.stat().st_mtime < WAKE_DEDUP_SECONDS:
            return True
        if pending.exists():
            return True
        # Publish complete metadata exclusively; an already-running drain may scan now.
        import fcntl
        _m_common.safe_target(pending)
        publish_lock = directory / 'publish.lock'
        _m_common.safe_target(publish_lock)
        lock_fd = os.open(publish_lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(lock_fd, 'a+') as lock:
            if os.fstat(lock.fileno()).st_nlink != 1:
                return False
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return pending.exists()  # the durable inbox lets the watcher retry otherwise
            if pending.exists():
                return True
            refs = wake_item_refs(items) if items is not None else wake_reason_items(inst, n, reason)
            _m_common.atomic_write(pending, json.dumps({'items': refs}) + '\n')
        wake_start(inst, target)
        return True
    except (Exception, SystemExit):
        return False  # the inbox remains durable; the watcher retries unacknowledged work


def wake_http_status(command):
    """Send only the constant event; return the HTTP code, never a response body."""
    import urllib.error
    import urllib.request

    # Never forward a webhook capability or its event to a redirect destination.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    request = urllib.request.Request(command, data=b'{"event":"brain-wake"}',
                                    headers={"Content-Type": "application/json", "User-Agent": WAKE_USER_AGENT})
    try:
        with opener.open(request, timeout=WAKE_TIMEOUT) as response:
            return response.status
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        return code


def wake_send(command):
    """The sole outward payload is a constant event. Discard all transport output."""
    payload = b'{"event":"brain-wake"}'
    if command.startswith(("https://", "http://")):
        status = wake_http_status(command)
        if not 200 <= status < 300:
            # Internal exception carries only a code; callers never expose transport errors.
            raise WakeRefused(status)
        return True
    import signal
    process = subprocess.Popen(command, shell=True, stdin=subprocess.PIPE,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        process.communicate(payload, timeout=WAKE_TIMEOUT)
        return process.returncode == 0
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        return False


class WakeRefused(Exception):
    pass


def wake_binding(command):
    return hashlib.sha256(command.encode()).hexdigest()


def wake_record(directory, command, status, name="delivery.json"):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    record = {"binding": wake_binding(command), "status": status, "at": time.time()}
    _m_common.atomic_write(directory / name, json.dumps(record))
    return record


def wake_read(directory, command, name="delivery.json"):
    try:
        record = json.loads((directory / name).read_text())
        status = record["status"]
        if (record["binding"] == wake_binding(command)
                and (type(status) is int and 100 <= status <= 599
                     or status in ("transport error", "delivery failed", "delivered; history write failed"))
                and type(record["at"]) in (int, float) and math.isfinite(record["at"])):
            return record
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def wake_probe(inst, n, force=False):
    """Probe a webhook at most daily per binding; return an HTTP code or safe error."""
    import fcntl
    target, command = wake_target(inst, n)
    if not command.startswith(("https://", "http://")):
        return "no webhook"
    if _m_common.outward_links(inst.root):
        return "unsafe instance"
    directory = wake_directory(inst, target)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = "probe-" + wake_binding(command) + ".json"
    lock = directory / "probe.lock"
    _m_common.safe_target(lock)
    with lock.open("a+") as stream:
        os.fchmod(stream.fileno(), 0o600)
        fcntl.flock(stream, fcntl.LOCK_EX)
        record = wake_read(directory, command, name)
        if not force and record and type(record["status"]) is int and 0 <= time.time() - record["at"] < 86400:
            return record["status"]
        try:
            status = wake_http_status(command)
        except Exception:
            (directory / name).unlink(missing_ok=True)
            return "transport error"
        wake_record(directory, command, status, name)
        return status


def wake_diagnostic(inst, n):
    target, command = wake_target(inst, n)
    directory = wake_directory(inst, target)
    records = [wake_read(directory, command),
               wake_read(directory, command, "probe-" + wake_binding(command) + ".json")]
    for record in records:
        if record and not (type(record["status"]) is int and 200 <= record["status"] < 300):
            status = record["status"]
            return "last wake delivery refused: HTTP " + str(status) if type(status) is int else "last wake delivery: " + status
    return None


def wake_drain(inst, n, once=False):
    """A short-lived stdlib worker waits out the rate window, without an LLM tick.
    Failed deliveries remain pending; stop after ten minutes, with due retrying later.
    """
    import fcntl
    directory = wake_directory(inst, n)
    if not directory.is_dir() or _m_common.outward_links(inst.root):
        return
    lock = directory / "lock"
    _m_common.safe_target(lock)
    attempted, owned = set(), False
    try:
        with lock.open("a+") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return  # the existing worker owns delivery, including its rate wait
            owned = True
            for sent in directory.glob("*.sent"):
                if time.time() - sent.stat().st_mtime >= WAKE_DEDUP_SECONDS:
                    sent.unlink(missing_ok=True)
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                if _m_common.outward_links(inst.root):
                    return
                pending = []
                for path in sorted(directory.glob("*.pending")):
                    # A requester may have checked .sent just before another worker published it.
                    # Recheck under the delivery lock so this race cannot send the same reason twice.
                    if path.with_suffix(".sent").exists():
                        path.unlink(missing_ok=True)
                    else:
                        pending.append(path)
                if not pending:
                    return
                last = directory / "last"
                try:
                    previous, now = float(last.read_text()), time.time()
                    if previous > now:
                        _m_common.atomic_write(last, str(now))  # bound deferral after a clock rollback
                        previous = now
                    delay = max(0, WAKE_INTERVAL - (now - previous))
                except (OSError, ValueError):
                    delay = 0
                if delay:
                    if once:
                        return
                    time.sleep(min(delay, 5))
                    continue
                attempted.update(path.name for path in pending)
                # Reload bindings so removed/changed transport capabilities take effect.
                current = _m_instance.Instance(inst.root)
                target, command = wake_target(current, current.node(n["id"]))
                if target["id"] != n["id"]:
                    from . import watch as _m_watch
                    destination = wake_directory(current, target)
                    destination.mkdir(parents=True, exist_ok=True)
                    for path in pending:
                        try:
                            refs = wake_item_refs(json.loads(path.read_text())['items'])
                        except (OSError, ValueError, KeyError, TypeError):
                            refs = []  # unknown/legacy requests must still carry questions through
                        allowed = [i for i in refs if i['kind'] not in _m_watch.WATCH_LOCAL_KINDS]
                        if refs and not allowed:
                            path.unlink(missing_ok=True)
                            continue
                        if allowed != refs:
                            _m_common.atomic_write(path, json.dumps({'items': allowed}) + '\n')
                        _m_common.safe_target(destination / path.name)
                        path.replace(destination / path.name)
                    if command:
                        wake_start(current, target)
                    return
                if not command:
                    return
                try:
                    history = wake_history(directory)
                except (OSError, ValueError):
                    # Preserve damaged evidence, but never hold a question behind log repair.
                    broken = directory / 'history.json'
                    if broken.exists():
                        broken.replace(directory / ('history-invalid-' + os.urandom(8).hex() + '.json'))
                    history = []
                refs = wake_delivery_items(current, target, pending)
                snapshot = wake_snapshot(current, target, refs)
                if history:
                    before = history[-1].get('snapshot', history[-1]['items'])
                else:
                    # An upgraded watcher already knows old queued/delivered identities.
                    # Do not bring those back merely because delivery logs are new.
                    from . import watch as _m_watch
                    try:
                        known = set(_m_watch.watch_state(current).get('seen', [])) - {i['id'] for i in refs}
                    except (OSError, ValueError):
                        known = set()
                    before = [i for i in snapshot if i['id'] in known]
                _m_common.atomic_write(last, str(time.time()))  # failures are rate-limited too
                inflight = directory / 'inflight.json'
                try:
                    _m_common.atomic_write(inflight, json.dumps({'before': before}) + '\n')
                except OSError:
                    pass  # comparison bookkeeping cannot suppress a question
                try:
                    sent = wake_send(command)
                    status = 200 if sent else "delivery failed"
                except WakeRefused as error:
                    sent, status = False, error.args[0]
                except Exception:
                    sent, status = False, "transport error"
                if sent:
                    for path in pending:  # one generic tick covers all currently queued reasons
                        _m_common.atomic_write(path.with_suffix(".sent"), "delivered\n")
                        path.unlink(missing_ok=True)
                    # Logging cannot undo delivery and cause another model wake on retry.
                    try:
                        _m_common.atomic_write(directory / 'history.json', json.dumps({'version': 1,
                            'deliveries': (history + [{'at': time.time(), 'items': refs,
                                'before': before, 'snapshot': snapshot}])[-100:]}) + '\n')
                    except OSError:
                        status = 'delivered; history write failed'
                try:
                    inflight.unlink(missing_ok=True)
                except OSError:
                    pass  # an unlocked in-flight file is ignored by readers
                try:
                    wake_record(directory, command, status)
                except OSError:
                    pass  # diagnostic bookkeeping cannot undo a successful delivery
                if once:
                    return
    except BaseException:
        owned = False  # unexpected errors leave durable pending state for due, not a crash loop
        raise
    finally:
        # A requester can publish after our final scan and lose its worker to LOCK_NB.
        # Recheck only after releasing the flock so the replacement can take over.
        # Reasons already attempted stay pending on failure, without a respawn loop.
        if owned and not once and not _m_common.outward_links(inst.root) and any(
                path.name not in attempted for path in directory.glob("*.pending")):
            wake_start(inst, n)


def cmd_wake(a):
    inst = _m_nodes.load()
    n = inst.node(a.id)
    if a.drain:
        wake_drain(inst, n)
    elif a.reason is not None:
        request_wake(inst, n, a.reason)
    else:
        _m_common.die('usage: brain wake <node> "<reason>"')


def link_runtimes(inst):
    made = []
    # Visit the node tree, never .git, homes, or arbitrary source folders. Git maintenance can remove
    # object directories during a recursive filesystem glob, and those folders cannot need runtime links.
    node_dirs = [n["dir"] for n in inst.nodes()]
    for rt in inst.runtimes:
        ad = inst.adapter(rt)
        link = ad.get("INSTRUCTIONS_LINK")
        if link:
            for agents in (d / "AGENTS.md" for d in node_dirs):
                rel = agents.relative_to(inst.root).parts
                if "core" in rel or "sources" in rel:
                    continue
                l = agents.parent / link
                if not l.is_symlink():
                    if l.exists():
                        _m_common.die(f"{l} is a real file; merge it into AGENTS.md and delete it")
                    l.symlink_to("AGENTS.md")
                    _m_common.touch(l)
                    made.append(l)
        hl, hs = ad.get("HOOKS_LINK"), ad.get("HOOKS_SOURCE")
        if hl and hs:
            dirs = [inst.root] if ad.get("HOOKS_SCOPE") == "root" else \
                [n["dir"] for n in inst.nodes() if not ({"core", "sources"} & set(n["dir"].relative_to(inst.root).parts))]
            for d in dirs:
                l = d / hl
                target = os.path.relpath(inst.root / "core" / "adapters" / "runtimes" / hs, l.parent)
                if l.is_symlink() and os.readlink(l) != target:
                    l.unlink()
                if not l.is_symlink():
                    if l.exists():
                        _m_common.die(f"{l} is a real file; merge it into core/adapters/runtimes/{hs} or delete it")
                    l.parent.mkdir(parents=True, exist_ok=True)
                    l.symlink_to(target)
                    _m_common.touch(l)
                    made.append(l)
        sk = ad.get("SKILLS_DIR")
        if sk:
            l = inst.root / sk
            target = os.path.relpath(inst.root / "core" / "skills", l.parent)
            if l.is_symlink() and os.readlink(l) != target:
                l.unlink()
            if not l.is_symlink():
                if l.exists():
                    _m_common.die(f"{l} exists and is not a link; move its contents to core/skills")
                l.parent.mkdir(parents=True, exist_ok=True)
                l.symlink_to(target)
                _m_common.touch(l)
                made.append(l)
    return made


GLOBAL_BEGIN = "<!-- brain:begin {} (written by `brain link --global`; edit outside these lines) -->"
GLOBAL_END = "<!-- brain:end {} -->"
LEGACY_BLOCK = re.compile(r"<!-- brain:begin \(written by .*?<!-- brain:end -->\n?", re.S)  # before names


def block_re(name):
    return re.compile(re.escape(f"<!-- brain:begin {name} ") + ".*?" + re.escape(GLOBAL_END.format(name)) + "\n?",
                      re.S)


def global_dirs(inst):
    """{runtime: [config folders]} that get this brain's pointer on this machine: the ones given with
    `brain link --global <runtime>=<folder>` (kept in .brain/local.json), else the runtime's own folder
    (its GLOBAL_HOME_ENV variable, else GLOBAL_HOME_DEFAULT)."""
    saved = inst.local.get("global", {})
    out = {}
    for rt in inst.runtimes:
        ad = inst.adapter(rt)
        g = ad.get("GLOBAL_INSTRUCTIONS")
        if not g:
            continue
        default = ad.get("GLOBAL_HOME_DEFAULT") or str(Path(g).parent)
        dirs = saved.get(rt) or [os.environ.get(ad.get("GLOBAL_HOME_ENV") or "") or default]
        out[rt] = [Path(d).expanduser() for d in dirs]
    return out


def global_file(inst, rt, folder):
    ad = inst.adapter(rt)
    g = ad["GLOBAL_INSTRUCTIONS"]
    default = ad.get("GLOBAL_HOME_DEFAULT") or str(Path(g).parent)
    return Path(folder).expanduser() / g[len(default):].lstrip("/")


def global_block(inst, name):
    title = _m_common.read_fm(inst.root / "node.md")[0].get("title", "Brain")
    op = inst.cfg.get("operator", "the operator")
    root = _m_common.tilde(inst.root)
    return "\n".join([
        GLOBAL_BEGIN.format(name),
        f"## {title}",
        "",
        f"{op}'s Brain is at `{root}` (its name: `{name}`): their projects, notes and decisions, kept as a "
        "tree of nodes.",
        "",
        f"- Asked about {op}'s projects, priorities or \"the brain\"? Run `brain -b {name}` (a dashboard) or "
        f"`brain -b {name} show <node>`. `{root}/registry.md` lists every node.",
        "- To work on one of those projects, work in its node folder or its home, where its own rules apply.",
        f"- This is only a pointer. Outside `{root}` the Brain's rules don't apply, and nothing from it may be "
        "written into another project.",
        GLOBAL_END.format(name), ""])


def own_legacy(inst, m):
    return f"`{_m_common.tilde(inst.root)}`" in m.group(0)


def has_pointer(f, inst, name):
    text = f.read_text() if f.exists() else ""
    return bool(block_re(name).search(text)) or any(own_legacy(inst, m) for m in LEGACY_BLOCK.finditer(text))


def other_pointers(f, inst, name):
    text = f.read_text() if f.exists() else ""
    names = set(re.findall(r"<!-- brain:begin (\S+) \(", text)) - {name}
    return names | {"an older one"} if any(not own_legacy(inst, m) for m in LEGACY_BLOCK.finditer(text)) else names


def set_pointer(f, inst, name, add):
    text = f.read_text() if f.exists() else ""
    new = block_re(name).sub("", text)
    new = LEGACY_BLOCK.sub(lambda m: "" if own_legacy(inst, m) else m.group(0), new).rstrip("\n")
    if add:
        new = (new + "\n\n" if new else "") + global_block(inst, name)
    elif new:
        new += "\n"
    if new == text:
        return False
    f.parent.mkdir(parents=True, exist_ok=True)
    _m_common.safe_target(f)
    f.write_text(new)
    return True


def install_global(inst, targets=(), remove=False, refresh=False):
    """Put this brain's pointer in its runtimes' user-wide instructions, or take it out. `targets`
    ("<runtime>=<folder>") choose the folders for a runtime on this machine; the pointer leaves the ones it
    was in before. A folder that already points to another brain is only shared when named explicitly.
    `refresh`: only rewrite pointers that are already there."""
    name = _m_nodes.register(inst.root)
    before = global_dirs(inst)
    chosen = {}
    for t in targets:
        rt, _, d = t.partition("=")
        if rt not in before or not d:
            _m_common.die(f"expected <runtime>=<folder>, runtime one of: {', '.join(before) or 'none'} (got '{t}')")
        chosen.setdefault(rt, []).append(_m_common.tilde(Path(d).expanduser().resolve()))
    saved = inst.local.setdefault("global", {})
    saved.update(chosen)
    after = global_dirs(inst)
    done = []
    for rt, dirs in after.items():
        files = [global_file(inst, rt, d) for d in dirs]
        stale = {global_file(inst, rt, d) for d in before[rt]} - set(files) if chosen.get(rt) else set()
        for f in [*stale, *(files if remove else [])]:
            if set_pointer(f, inst, name, add=False):
                done.append(f"{rt}: {_m_common.tilde(f)} cleared")
        if remove:
            continue
        kept = []
        for d, f in zip(dirs, files):
            if refresh and not has_pointer(f, inst, name):
                continue
            others = other_pointers(f, inst, name)
            if others and rt not in chosen and not has_pointer(f, inst, name):
                done.append(f"{rt}: {_m_common.tilde(f)} already points to {', '.join(sorted(others))}: skipped. Give "
                            f"'{name}' its own folder (brain -b {name} link --global {rt}=<folder>), or share "
                            f"this one ({rt}={_m_common.tilde(d)})")
                continue
            if set_pointer(f, inst, name, add=True):
                done.append(f"{rt}: {_m_common.tilde(f)} points to '{name}'")
            kept.append(_m_common.tilde(d))
        if kept:  # where the pointer is now, so a session started with one of these folders finds this brain
            saved[rt] = kept
    if remove:
        inst.local.pop("global", None)
    elif not saved:
        inst.local.pop("global", None)
    inst.save_local()
    return done


def install_command(inst, bin_dir):
    bin_dir = Path(bin_dir).expanduser()
    bin_dir.mkdir(parents=True, exist_ok=True)
    l = bin_dir / "brain"
    target = inst.root / "core" / "bin" / "brain"
    if l.is_symlink():
        cur = Path(os.path.realpath(l))
        if cur.exists() and cur != target.resolve() and _m_instance.is_instance(cur.parent.parent.parent):
            # another brain's command: keep it, it reaches this one too (each brain runs its own core)
            print(f"command: {_m_common.tilde(l)} (already there; reaches this brain with: brain -b {_m_nodes.register(inst.root)})")
            return
        l.unlink()
    elif l.exists():
        _m_common.die(f"{l} exists and isn't a link; remove it first")
    l.symlink_to(target)
    on_path = str(bin_dir.resolve()) in [str(Path(p).expanduser().resolve()) for p in
                                         os.environ.get("PATH", "").split(":") if p]
    print(f"command: {_m_common.tilde(l)} -> {_m_common.tilde(target)}" + ("" if on_path else f"  (add {_m_common.tilde(bin_dir)} to PATH)"))


def cmd_link(a):
    inst = _m_nodes.load()
    made = link_runtimes(inst)
    for l in made:
        print(f"linked {l.relative_to(inst.root)}")
    if made:
        inst.commit([], "Link runtime instruction files")
    if a.command:
        install_command(inst, a.bin_dir)
    if a.glob or a.unglobal or a.targets:
        for d in install_global(inst, a.targets, remove=a.unglobal) or ["global instructions: already up to date"]:
            print(d)
    else:
        for d in install_global(inst, refresh=True):  # keep existing pointers current (e.g. after an upgrade)
            print(d)


def cmd_brains(a):
    data = _m_instance.read_brains()
    if a.action == "forget":
        if data["brains"].pop(a.name or "", None) is None:
            _m_common.die(f"no brain '{a.name}' on this machine")
        if data.get("default") == a.name:
            data["default"] = next(iter(sorted(data["brains"])), None)
        _m_instance.write_brains(data)
        print(f"forgot '{a.name}' (its folder is untouched)")
        return
    here = _m_nodes.try_find_root()
    if not data["brains"]:
        print("no brains on this machine yet")
        return
    for name, v in sorted(data["brains"].items()):
        root = Path(v["root"])
        dirs = [f"{rt} {', '.join(ds)}" for rt, ds in sorted(_m_nodes.local_of_root(root).get("global", {}).items())]
        print(f"{'*' if here and root.resolve() == here.resolve() else ' '} {name:<16} {_m_common.tilde(root)}"
              f"{'' if _m_instance.is_instance(root) else '  (missing)'}{'  [default]' if data.get('default') == name else ''}"
              + (f"\n  {'':<16} pointer in: {'; '.join(dirs)}" if dirs else ""))
    print("\n* = the one a command here uses.  brain -b <name> <command> picks one; brain use <name> sets the default;"
          "\nbrain --all shows every dashboard.")


def cmd_use(a):
    data = _m_instance.read_brains()
    if a.name not in data["brains"]:
        _m_common.die(f"no brain '{a.name}' on this machine ({', '.join(sorted(data['brains'])) or 'none'})")
    data["default"] = a.name
    _m_instance.write_brains(data)
    print(f"default brain: {a.name} (inside another brain's folders, that brain is used)")


# ---------- hooks ----------

COORDINATOR_FILES = ["brief.md", "status.md", "decisions.md", "backlog.md", "sources", "AGENTS.md"]
REMIND_MINUTES = int(os.environ.get("BRAIN_HOOK_REMIND_MINUTES", "60"))


def node_at(inst, cwd):
    """The node whose own folder holds `cwd` (not one of its children's)."""
    cwd = Path(cwd).resolve()
    best = None
    for n in inst.nodes():
        d = n["dir"].resolve()
        if d == cwd or d in cwd.parents:
            if best is None or len(d.parts) > len(best["dir"].resolve().parts):
                best = n
    return best


def notes_lag(inst, n):
    """Why this node's notes may be behind: coordinator files changed after status.md, or uncommitted."""
    d = n["dir"]
    st = d / "status.md"
    out = []
    if st.exists():
        newer = []
        for name in COORDINATOR_FILES:
            f = d / name
            files = [x for x in f.rglob("*") if x.is_file() and x.name != ".gitkeep"] if f.is_dir() else [f]
            newer += [x for x in files if x.exists() and x != st and x.stat().st_mtime > st.stat().st_mtime + 1]
        if newer:
            out.append("changed after status.md: " + ", ".join(sorted(str(x.relative_to(d)) for x in newer)[:5]))
        if _m_status.FACTS_START in st.read_text():
            events = [d / _m_jobs.JOBS_FILE, d / "inbox.md", *(d / "job-handbacks").glob("*.json")]
            if any(f.exists() and f.stat().st_mtime > st.stat().st_mtime for f in events):
                out.append(f"job or inbox files changed after facts; run brain status {n['id']} --refresh")
    rel = d.relative_to(inst.root)
    paths = [str(rel / name) for name in COORDINATOR_FILES]
    dirty = _m_common.git("status", "--porcelain", "--", *paths, cwd=inst.root).stdout.split("\n")
    dirty = [l[3:] for l in dirty if l.strip()]
    if dirty:
        out.append("not committed: " + ", ".join(dirty[:5]))
    return out


HOOK_TRANSCRIPT_LIMIT = 4 * 1024 * 1024
HOOK_TRANSCRIPT_CHUNK = 64 * 1024


def hook_operator_message(rec):
    if not isinstance(rec, dict):
        return False
    msg = rec.get("message")
    if rec.get("type") == "user" and isinstance(msg, dict):
        blocks = msg.get("content", [])
        return isinstance(blocks, (str, list)) and not any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks)
    item = rec.get("payload", {}) if rec.get("type") in ("response_item", "event_msg") else rec
    return isinstance(item, dict) and (item.get("type") == "message" and item.get("role") == "user"
                                      or item.get("type") == "user_message")


def hook_turn_records(fh):
    """Walk backward to the latest operator message, reading at most four MiB of a snapshot.
    A capped, incomplete leading record is discarded; never parse a fragment as a record.
    """
    position = os.fstat(fh.fileno()).st_size
    budget, carry, records = HOOK_TRANSCRIPT_LIMIT, b"", []
    while position and budget:
        amount = min(HOOK_TRANSCRIPT_CHUNK, position, budget)
        position -= amount
        budget -= amount
        fh.seek(position)
        lines = (fh.read(amount) + carry).split(b"\n")
        carry = lines[0] if position else b""
        for line in reversed(lines[1:] if position else lines):
            try:
                rec = json.loads(line)
            except ValueError:
                continue  # partial final line, malformed record, or blank trailing line
            if hook_operator_message(rec):
                return list(reversed(records))
            records.append(rec)
    return list(reversed(records))


def hook_questions(path):
    """Unanswered question calls in the bounded latest turn of the hook-supplied transcript."""
    pending = {}
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        return []  # a FIFO/device must never hold a stop open
    with os.fdopen(fd, "rb") as fh:
        for rec in hook_turn_records(fh):
            if not isinstance(rec, dict):
                continue
            kind = rec.get("type")
            msg = rec.get("message", {})
            if not isinstance(msg, dict):
                msg = {}
            blocks = msg.get("content", [])
            if kind == "user" and isinstance(blocks, (str, list)):
                # Tool results are user records too; they don't start another operator turn.
                results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
                if not results:
                    pending = {}
                for b in results:
                    if not b.get("is_error"):
                        pending.pop(b.get("tool_use_id"), None)
            if kind == "assistant" and isinstance(blocks, list):
                for b in blocks:
                    if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "AskUserQuestion":
                        pending[b.get("id")] = b.get("input", {})
            # Codex rollout response items use function_call / function_call_output.
            item = rec.get("payload", {}) if kind == "response_item" else rec
            if not isinstance(item, dict):
                continue
            if item.get("type") == "message" and item.get("role") == "user":
                pending = {}
            if item.get("type") == "function_call" and item.get("name", "").split(".")[-1] == "request_user_input":
                try:
                    args = item.get("arguments", "{}")
                    pending[item.get("call_id")] = json.loads(args) if isinstance(args, str) else args
                except ValueError:
                    pass
            if item.get("type") == "function_call_output":
                try:
                    output = item.get("output", "{}")
                    output = json.loads(output) if isinstance(output, str) else output
                    answers = output.get("answers") if isinstance(output, dict) else None
                    args = pending.get(item.get("call_id"))
                    if isinstance(answers, dict) and isinstance(args, dict):
                        answered = {key for key, value in answers.items() if isinstance(value, dict)
                                    and isinstance(value.get("answers"), list) and any(
                                        isinstance(a, str) and a.strip() for a in value["answers"])}
                        args["questions"] = [q for q in args.get("questions", [])
                                             if not isinstance(q, dict) or q.get("id") not in answered]
                except ValueError:
                    pass
    return [q for args in pending.values() if isinstance(args, dict)
            for q in args.get("questions", []) if isinstance(q, dict) and isinstance(q.get("question"), str)]


def capture_hook_questions(inst, n, data):
    path = data.get("transcript_path")
    if not isinstance(path, str) or not path:
        return
    questions = hook_questions(path)
    if not questions:
        return
    # Same boundary checks as CLI writes, but no instance lock: a parent brain command may hold it.
    if _m_common.outward_links(inst.root):
        return
    terms = _m_guard.never_terms(n)
    for q in questions:
        parts = [q["question"]]
        for option in q.get("options", []):
            if isinstance(option, dict):
                parts += [v for k in ("label", "description") if isinstance((v := option.get(k)), str)]
        text = " ".join(_m_nodes.import_text("\n".join(parts), terms).split())
        text = _m_nodes.import_text(text, terms)  # whitespace folding can form a protected phrase or secret assignment
        # Hash only screened content; raw tool ids, paths, answers and session ids never persist.
        marker = "<!-- host-question:" + hashlib.sha256(text.encode()).hexdigest() + " -->"
        wake_id = os.urandom(16).hex()
        line = f"- {inst.now()} from runtime: Host question: {text} <!-- wake:{wake_id} --> {marker}\n"
        try:
            created = _m_transitions.inbox_append(n["dir"], line, marker, nonblocking=True)
        except BlockingIOError:
            import fcntl
            queue = _m_status.hook_question_queue(n)
            queue.mkdir(parents=True, exist_ok=True)
            pending = queue / (hashlib.sha256(text.encode()).hexdigest() + ".json")
            # Serialize only this marker's queue publication; hooks never wait on the lock.
            lock = pending.with_suffix(".lock")
            _m_common.safe_target(lock)
            try:
                with lock.open("a+") as stream:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    created = not pending.exists()
                    if created:
                        _m_common.atomic_write(pending, json.dumps({"line": line, "marker": marker}) + "\n")
            except BlockingIOError:
                created = False  # the other publisher is recording this same question
        if created:
            request_wake(inst, n, 'wake:' + wake_id)


def cmd_hook(a):
    """Runtime hook. `stop`: before a turn ends in a node folder, make sure its notes are current (axioms 15-17).
    Never fails the runtime: any problem means "let it stop"."""
    try:
        hook_stop(a)
    except (Exception, SystemExit):  # a refusal inside bookkeeping must never break an agent's turn
        return


def hook_stop(a):
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return
    if a.event != "stop" or not isinstance(data, dict):
        return
    cwd = data.get("cwd") or os.getcwd()
    start = Path(cwd)
    root = _m_nodes.root_for_path(start)
    if not root:
        return
    inst = _m_instance.Instance(root)
    n = node_at(inst, cwd)
    if not n:
        places = [(Path(p).expanduser().resolve(), node) for node in inst.nodes()
                  for p in [node["home_path"], node["local"].get("workbench"),
                            *[w["path"] for w in node["local"].get("worktrees", {}).values()]] if p]
        n = next((node for p, node in sorted(places, key=lambda x: len(x[0].parts), reverse=True)
                  if p == start.resolve() or p in start.resolve().parents), None)
    if not n:
        return
    try:
        capture_hook_questions(inst, n, data)
    except (Exception, SystemExit):
        pass  # question bookkeeping must not suppress existing reminders or fail a turn
    if data.get("stop_hook_active") or not node_at(inst, cwd):
        return  # workers capture questions without holding their turn for coordinator notes
    try:
        unread = _m_upgrade.changes_unread(inst, n) if (n["id"] == "root" or n["local"].get("coordinator")) else []
    except (OSError, ValueError):
        unread = []  # fail open: a reminder is never worth a broken stop
    brain = shlex.quote(str(inst.root / "core" / "bin" / "brain"))
    upgrade = (
        # A running coordinator loaded its rules when its session began: an upgrade reaches it here, at the end of
        # any turn (a tick's included), whatever its tick prompt checks, until it marks the entries read.
        f"core was upgraded and a coordinator's loaded rules are out of date ({len(unread)} unread change "
        f"entr{'y' if len(unread) == 1 else 'ies'}: " + "; ".join(e["id"] for e in unread[:3]) + "). If you are "
        f"this node's coordinator: run `{brain} changes {n['id']}`, re-read "
        f"{shlex.quote(str(inst.root / 'core' / 'roles' / 'coordinator.md'))} and every skill the entries name, do "
        f"what their 'After upgrading' asks of this node, then run `{brain} changes {n['id']} --seen`. If you are a "
        "worker or any other session, don't mark them read: tell your coordinator, and go on") if unread else ""
    why = notes_lag(inst, n)
    future = bool(_m_status.future_entries(inst, n)[0])
    state_f = inst.root / ".brain" / "hooks.json"
    try:
        contained = not _m_common.outward_links(inst.root)
    except OSError:
        contained = False
    try:
        state = json.loads(state_f.read_text()) if state_f.exists() else {}
        state = state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        state = {}  # unreadable or half-written: start over (it only spaces out reminders)
    sid = data.get("session_id") or "?"
    now = inst.now_dt()
    last = state.get(sid)
    last = inst.parse_time(last) if last else None
    remind = last is None or _m_status.elapsed(last, now) > REMIND_MINUTES * 60
    if not remind:
        upgrade = ""  # once per session per reminder interval: a worker in a node folder isn't held every turn
    if future and remind:
        why.append("status.md has a future Updated: time; fix it with `brain now` if you are its coordinator; "
                   "if you are a worker, tell your coordinator")
    if not why:
        u = _m_status.updated_at(inst, n)
        stale = u is None or _m_status.elapsed(u, now) > REMIND_MINUTES * 60
        if stale and remind:
            why.append(f"status.md was last updated {_m_status.ago(inst, u)}")
    if not why and not upgrade:
        return
    state[sid] = now.isoformat(timespec="seconds")
    try:
        log = inst.root / ".brain" / "hooks.log"
        if not contained or any(f.is_file() and not f.is_symlink() and f.stat().st_nlink > 1 for f in (state_f, log)):
            raise OSError("bookkeeping would leave the brain")  # the reminder still goes out; only this is skipped
        state_f.parent.mkdir(exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".hooks.", suffix=".json", dir=state_f.parent)  # new, never one planted
        tmp = Path(name)
        with os.fdopen(fd, "w") as out:
            out.write(json.dumps(dict(list(state.items())[-200:]), indent=1))
        os.replace(tmp, state_f)  # whole or not at all: another session's hook never reads half a file
        held = why + ([f"upgrade notice ({len(unread)} unread change entr{'y' if len(unread) == 1 else 'ies'})"]
                      if upgrade else [])
        with open(log, "a") as fh:
            fh.write(f"{now.isoformat(timespec='seconds')} stop {n['id']} {sid}: "
                     f"{_m_guard.redact('; '.join(held), _m_guard.never_terms(n))}\n")
    except OSError:
        pass  # the reminder still goes out; only its bookkeeping is lost
    if not why:  # only the upgrade: one notice, then the session goes on
        print(json.dumps({"decision": "block", "reason": _m_guard.redact(f"Before you stop, node `{n['id']}`: {upgrade}.",
                                                                 _m_guard.never_terms(n))}))
        return
    reason = (f"Before you stop, node `{n['id']}`: " + (upgrade + ". Also: " if upgrade else "") +
              f"{'; '.join(why)}. If you are this node's coordinator, record "
              f"what this conversation decided, started, finished or blocked (decisions.md; status.md and its "
              f"Updated line; timestamps from `core/bin/brain now`; preserve the generated facts block and run "
              f"`core/bin/brain status {n['id']} --refresh`), then commit `{n['id']}: <what changed>`. If you are a worker, don't edit those files: append anything "
              f"the coordinator should know to inbox.md. If this conversation changed nothing here (you only read "
              f"this node, or it isn't yours), record nothing and end by repeating your previous answer unchanged: "
              f"your last message is what the operator reads.")
    print(json.dumps({"decision": "block", "reason": _m_guard.redact(reason, _m_guard.never_terms(n))}))


def codex_hooks(root):
    """Ask Codex (app-server, hooks/list) for the project hooks it sees at `root`."""
    import select
    exe = shutil.which("codex")
    if not exe:
        _m_common.die("codex isn't installed")
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "brain", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "hooks/list", "params": {"cwds": [str(root)]}}]
    pr = subprocess.Popen([exe, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, text=True)
    pr.stdin.write("".join(json.dumps(m) + "\n" for m in msgs))
    pr.stdin.flush()
    found, deadline = None, datetime.datetime.now() + datetime.timedelta(seconds=30)
    while datetime.datetime.now() < deadline and found is None:
        if not select.select([pr.stdout], [], [], 1)[0]:
            continue
        line = pr.stdout.readline()
        if not line:
            break
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if m.get("id") == 2:
            found = [h for d in m.get("result", {}).get("data", []) for h in d.get("hooks", [])]
    pr.kill()
    if found is None:
        _m_common.die("codex app-server didn't answer hooks/list")
    return found


def cmd_hook_approve(a):
    """Trust this instance's project hooks in Codex (the operator's explicit consent)."""
    inst = _m_nodes.load()
    real = inst.root.resolve()
    home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    cfg = home / "config.toml"
    text = cfg.read_text() if cfg.exists() else ""
    add = []
    if f'[projects."{real}"]' not in text:  # first: Codex lists no hooks for an untrusted project
        home.mkdir(parents=True, exist_ok=True)
        text = text.rstrip("\n") + ("\n\n" if text.strip() else "") + f'[projects."{real}"]\ntrust_level = "trusted"\n'
        cfg.write_text(text)
        print(f"codex: trusting the project {_m_common.tilde(real)}")
    ours = str(real / ".codex" / "hooks.json")
    hooks = [h for h in codex_hooks(real) if h.get("sourcePath") == ours]
    if not hooks:
        print("codex: no project hooks found here (run 'brain link' first)")
        return
    for h in hooks:
        if h.get("trustStatus") == "trusted":
            continue
        key = h["key"]
        block = f'[hooks.state."{key}"]'
        if block in text:  # an older approval: replace its hash
            text = re.sub(re.escape(block) + r"\ntrusted_hash = \"[^\"]*\"\n?", "", text)
        add.append(f'{block}\ntrusted_hash = "{h["currentHash"]}"\n')
        print(f"codex: trusting {h['eventName']} hook: {h['command'][:80]}")
    if add:
        home.mkdir(parents=True, exist_ok=True)
        cfg.write_text(text.rstrip("\n") + ("\n\n" if text.strip() else "") + "\n".join(add))
        print(f"codex: wrote {_m_common.tilde(cfg)}")
    else:
        print("codex: already trusted")


# ---------- ui: a live, read-only view in a browser on this machine ----------
