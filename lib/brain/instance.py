"""Instance mechanics for Brain."""
import datetime
import json
import os
import re
import socket
import subprocess
import time
from pathlib import Path

from . import common as _m_common


class Instance:
    def __init__(self, root):
        self.root = root
        self.cfg, _ = _m_common.read_fm(root / "instance.md")
        self.principals = _m_common.lst(self.cfg.get("principals"))
        self.revoked = _m_common.lst(self.cfg.get("revoked"))
        self.runtimes = _m_common.lst(self.cfg.get("runtimes"))
        self.tz = self.cfg.get("timezone", "UTC")
        self.local_path = root / ".brain" / "local.json"
        self.local = self._load_local()

    # time (axiom 17)
    def zone(self):
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(self.tz)
        except Exception:
            return None

    def now_dt(self):
        z = self.zone()
        return datetime.datetime.now(z) if z else datetime.datetime.now().astimezone()

    def now(self):
        """A timestamp for the notes, with its UTC offset, so no reader has to guess the zone."""
        d = self.now_dt()
        z = d.strftime("%z")
        return f"{d:%Y-%m-%d %H:%M} {z[:3]}:{z[3:]}" if z else f"{d:%Y-%m-%d %H:%M}"

    def parse_time(self, s):
        """'2026-10-19 06:31', '2026-10-19 06:31 +03:00', '2026-10-19T03:31Z'... A time without a zone is in the
        instance's timezone."""
        m = re.match(r"\s*(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?)\s*(Z|[+-]\d{2}:?\d{2})?\s*$",
                     s or "")
        if not m:
            return None
        z = m.group(3) or ""
        z = "+00:00" if z == "Z" else (z[:3] + ":" + z[-2:] if z else "")
        try:
            d = datetime.datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}{z}")
        except ValueError:
            return None
        if d.tzinfo is None:
            d = d.replace(tzinfo=self.zone()) if self.zone() else d.astimezone()
        return d

    # this machine's bindings (axiom 23)
    def _load_local(self):
        if self.local_path.exists():
            data = json.loads(self.local_path.read_text())
        else:
            data = {}
        data.setdefault("machine", socket.gethostname().split(".")[0])
        data.setdefault("nodes", {})
        return data

    def save_local(self):
        _m_common.safe_target(self.local_path)
        self.local_path.parent.mkdir(exist_ok=True)
        _m_common.atomic_write(self.local_path, json.dumps(self.local, indent=2, sort_keys=True) + "\n")

    def local_of(self, nid):
        return self.local["nodes"].setdefault(nid, {})

    def adapter(self, runtime):
        return _m_common.read_env(_m_common.CORE / "adapters" / "runtimes" / f"{runtime}.env")

    def host_adapter(self):
        return _m_common.read_env(_m_common.CORE / "adapters" / "hosts" / f"{self.cfg.get('host', '')}.env")

    def thread_link(self, tid):
        fmt = self.host_adapter().get("THREAD_LINK")
        return fmt.replace("{id}", tid) if fmt and tid else tid

    def node_dir(self, nid):
        d = self.root
        if nid in ("root", ""):
            return d
        for seg in nid.split("/"):
            d = d / "nodes" / seg
        return d

    def nodes(self):
        out = []

        def rec(d, nid, parent):
            fm, _ = _m_common.read_fm(d / "node.md")
            n = {"id": nid, "dir": d, "parent": parent, "fm": fm,
                 "local": self.local["nodes"].get(nid, {})}
            out.append(n)
            kids = d / "nodes"
            if kids.is_dir():
                for c in sorted(kids.iterdir()):
                    if (c / "node.md").exists():
                        rec(c, c.name if nid == "root" else f"{nid}/{c.name}", n)

        rec(self.root, "root", None)
        for n in out:  # effective owners: inherit from parent when unset
            own = _m_common.lst(n["fm"].get("owners"))
            n["owners"] = own or (n["parent"]["owners"] if n["parent"] else [])
            n["sealed"] = bool(n["parent"]) and set(n["owners"]) != set(n["parent"]["owners"])
            home = n["fm"].get("home", "here")
            n["external"] = home not in ("here", "none", "")
            if home == "here":
                n["home_path"] = n["dir"]
            elif home == "external":
                h = n["local"].get("home")
                n["home_path"] = Path(h).expanduser() if h else None
            elif n["external"]:  # legacy: a path in node.md
                n["home_path"] = Path(home).expanduser()
            else:
                n["home_path"] = None
            n["info"] = {**n["fm"], **{k: v for k, v in n["local"].items()}}
            n["archived"] = "state:archived" in _m_common.lst(n["fm"].get("labels"))
        return out

    def node(self, nid):
        for n in self.nodes():
            if n["id"] == nid:
                return n
        _m_common.die(f"no node '{nid}'")

    def items(self):
        out = []
        for d, subdirs, _ in os.walk(self.root):  # never into .git (git may be writing there), core or .brain
            subdirs[:] = sorted(s for s in subdirs if s not in (".git", ".brain") and
                                not (s == "core" and Path(d) == self.root))
            if Path(d).name != "library":
                continue
            for f in sorted(Path(d).glob("*.md")):
                if f.name.lower() == "readme.md":
                    continue
                fm, _ = _m_common.read_fm(f)
                out.append({"path": f, "fm": fm})
        return out

    def item(self, iid):
        for i in self.items():
            if i["fm"].get("id") == iid or i["path"].stem == iid:
                return i
        _m_common.die(f"no item '{iid}'")

    def check_principals(self, owners):
        for p in _m_common.lst(owners):
            if p not in self.principals:
                _m_common.die(f"unknown principal '{p}'; add it to principals in instance.md first")
            if p in self.revoked:
                _m_common.die(f"principal '{p}' is revoked")

    def commit(self, paths, msg):
        """Commit `paths` plus what this command wrote. Other uncommitted work stays as it is."""
        if not (self.root / ".git").exists():
            return
        root = self.root.resolve()
        want = []
        for x in {*(Path(q) if Path(q).is_absolute() else self.root / q for q in paths), *_m_common.TOUCHED}:
            x = Path(os.path.abspath(x))
            rx = x.parent.resolve() / x.name
            if root not in [rx, *rx.parents] or ".brain" in rx.relative_to(root).parts:
                continue
            rel = str(rx.relative_to(root)) or "."
            if _m_common.git("ls-files", "--", rel, cwd=root).stdout.strip() or x.is_symlink() or (
                    x.exists() and not (x.is_dir() and not _m_common.holds_files(x))):
                want.append(rel)  # an empty folder is nothing git can add: naming it fails the whole commit
        if not want:
            return
        with brain_lock(self):
            self._commit(root, sorted(set(want)), msg)

    def commit_settings_batch(self, paths, msg):
        """Explicit named-path boundary: no ambient TOUCHED paths enter a settings batch.

        The caller validates every target is clean before publication; Git's path-limited
        commit keeps unrelated staged entries intact. Default commit behavior is unchanged.
        """
        root = self.root.resolve()
        want = []
        for path in paths:
            target = Path(path).absolute()
            relative = target.relative_to(root)
            if ".brain" in relative.parts or target.is_dir():
                _m_common.die("settings batch commit accepts shared file paths only")
            want.append(str(relative))
        if want and (root / ".git").exists():
            with brain_lock(self):
                self._commit(root, sorted(set(want)), msg)

    def _commit(self, root, want, msg):
        import time
        for attempt in range(8):  # a git command outside brain may hold git's index.lock for a moment  # another brain command may be committing at this moment (git's index.lock)
            add = subprocess.run(["git", "-C", str(root), "add", "-A", "--", *want], capture_output=True, text=True)
            if add.returncode == 0:
                r = subprocess.run(["git", "-C", str(self.root), "diff", "--cached", "--quiet", "--", *want])
                if not r.returncode:
                    return  # nothing of ours left to commit
                c = subprocess.run(["git", "-C", str(self.root), "commit", "-qm", msg, "--", *want],
                                   capture_output=True, text=True)
                if c.returncode == 0:
                    return
                add = c
            if "index.lock" not in (add.stderr or "") or attempt == 7:
                _m_common.die(f"git: {(add.stderr or add.stdout).strip()}")
            time.sleep(0.15 * (attempt + 1))


_LOCK = {"depth": 0, "fh": None}
LOCK_WAIT = int(os.environ.get("BRAIN_LOCK_WAIT") or 300)  # seconds a command waits for another before giving up


class brain_lock:
    """Several coordinators may run `brain` at once: every command that changes something runs under one lock
    per instance (machine-local, in .brain/, never deleted: a lock on a deleted file isn't a lock for the next
    process). The holder writes its pid into the file and says so in BRAIN_LOCK_HELD, so a brain command it
    starts itself (git runs hooks inside our commit) goes on without waiting for it, and only while it holds it."""

    def __init__(self, inst_or_root):
        root = inst_or_root.root if isinstance(inst_or_root, Instance) else inst_or_root  # (a Path's .root is "/")
        self.path = Path(root) / ".brain" / "lock"
        self.key = str(Path(root).resolve())

    def owner_is_running_us(self):
        held = os.environ.get("BRAIN_LOCK_HELD", "")
        if not held.startswith(self.key + "|"):
            return False
        pid = held.rsplit("|", 1)[1]
        try:
            os.kill(int(pid), 0)  # still alive...
            return self.path.read_text().strip() == pid  # ...and still the holder
        except (OSError, ValueError):
            return False

    def __enter__(self):
        if _LOCK["fh"] is None and not self.owner_is_running_us():
            import fcntl
            import time
            self.path.parent.mkdir(exist_ok=True)
            fh = open(self.path, "a+")
            if os.fstat(fh.fileno()).st_nlink > 1 or self.path.is_symlink():
                fh.close()
                _m_common.die(f"{_m_common.tilde(self.path)} is linked to another file; brain won't write it (remove it)")
            deadline = time.time() + LOCK_WAIT
            while True:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.time() > deadline:
                        fh.close()
                        _m_common.die(f"another brain command has held this instance's lock for {LOCK_WAIT // 60} minutes; "
                            f"try again (lock: {_m_common.tilde(self.path)})")
                    time.sleep(0.05)
            fh.seek(0)
            fh.truncate()
            fh.write(str(os.getpid()))
            fh.flush()
            _LOCK["fh"] = fh
            os.environ["BRAIN_LOCK_HELD"] = f"{self.key}|{os.getpid()}"
        _LOCK["depth"] += 1
        return self

    def __exit__(self, *exc):
        _LOCK["depth"] -= 1
        if _LOCK["depth"] == 0:
            unlock()


def unlock():
    """Let go of the instance lock now (before a long, unrelated step); a later `with brain_lock` takes it again."""
    fh = _LOCK["fh"]
    if fh:
        fh.seek(0)
        fh.truncate()  # no holder: whoever still has our BRAIN_LOCK_HELD can't skip the lock any more
        fh.flush()
        fh.close()
        _LOCK["fh"] = None
        os.environ.pop("BRAIN_LOCK_HELD", None)


def resolve_ref(nodes, ref):
    """The node a cited id means today: itself, or the node it was moved to or merged into (axiom 19)."""
    for key in ("id", "former_ids", "merged_from"):
        for n in nodes:
            if (n["id"] == ref) if key == "id" else ref in _m_common.lst(n["fm"].get(key)):
                return n
    return None


def is_instance(d):
    return (d / "instance.md").exists() and (d / "node.md").exists()


def instance_above(p):
    p = Path(p).expanduser().resolve()
    return next((d for d in [p, *p.parents] if is_instance(d)), None)


# ---------- the brains on this machine ----------
# One machine may hold several instances (say, one personal and one for an employer). They never share
# files; this list only lets one `brain` command reach each of them and keeps their ports apart.

def brains_file():
    if os.environ.get("BRAIN_CONFIG"):
        return Path(os.environ["BRAIN_CONFIG"]).expanduser()
    return Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser() / "brain" / "brains.json"


def read_brains():
    try:
        data = json.loads(brains_file().read_text())
    except (OSError, ValueError):
        data = {}
    data.setdefault("brains", {})
    return data


def write_brains(data):
    f = brains_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def brains_here():
    """[(name, root)] for every registered brain that is still on this machine."""
    return [(k, Path(v["root"])) for k, v in sorted(read_brains()["brains"].items()) if is_instance(Path(v["root"]))]


# ---------- machine care: one policy and FIFO across every instance ----------
