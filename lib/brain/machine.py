"""Machine mechanics for Brain."""
import contextlib
import datetime
import hashlib
import json
import math
import os
import re
import shlex
import stat
import subprocess
import sys
import time
from pathlib import Path

from . import common as _m_common
from . import history as _m_history
from . import instance as _m_instance
from . import nodes as _m_nodes
from . import schedules as _m_schedules
from . import ui as _m_ui


MACHINE_DEFAULTS = {"load5_per_cpu": 0.8, "memory_free_gb": 8, "disk_free_gb": 40,
                    "verify_day": 2, "verify_night": 1, "night_start": 22, "night_end": 8}
MACHINE_RETRY = 5


def machine_valid_sample(sample):
    import math
    def number(v):
        return type(v) in (int, float) and math.isfinite(v) and v >= 0
    if not isinstance(sample, dict) or not number(sample.get("at")):
        return False
    reading = sample.get("reading")
    if not isinstance(reading, dict) or "sample_reasons" in reading:
        return False
    for key in ("load", "load_per_cpu"):
        if key in reading and (not isinstance(reading[key], list) or len(reading[key]) != 3 or
                               not all(number(v) for v in reading[key])):
            return False
    for key in ("cpus", "mem_free_gb", "mem_total_gb", "disk_free_gb", "disk_total_gb"):
        if key in reading and not number(reading[key]):
            return False
    return reading.get("thermal", "unknown") in ("unknown", "normal", "pressure")


def machine_samples(inst, fresh=True):
    try:
        data = json.loads((inst.root / ".brain/machine.json").read_text())
        samples = data["samples"]
        if (not isinstance(samples, list) or not samples or len(samples) > 1440 or
                not all(machine_valid_sample(s) for s in samples) or
                any(a["at"] > b["at"] for a, b in zip(samples, samples[1:]))):
            return None
        age = time.time() - samples[-1]["at"]
        if fresh and not 0 <= age <= 180:
            return None
        prior = data.get("heat_reasons", [])
        if (type(data.get("episode", False)) is not bool or not isinstance(prior, list) or any(r not in ("sustained 5-minute load", "sustained thermal pressure") for r in prior)
                or (data.get("episode") and not prior)):
            return None
        return data
    except (OSError, ValueError, KeyError, TypeError):
        return None


def machine_reading(inst=None):
    # Explicit fixture injection; never used by the sampler service. No load-generating tests.
    if os.environ.get("BRAIN_MACHINE_TESTING") == "1":
        return json.loads(os.environ["BRAIN_MACHINE_READING"])
    if inst is None:
        root = _m_nodes.try_find_root()
        inst = _m_instance.Instance(root) if root else None
    data = machine_samples(inst) if inst else None
    if data:
        reading = dict(data["samples"][-1]["reading"])
        policy = machine_policy(strict=False)
        reasons = machine_sustained(data["samples"], policy)
        end, previous, covered = data["samples"][-1]["at"], data["samples"][-1]["at"], False
        for sample in reversed(data["samples"]):
            if not 0 <= previous - sample["at"] <= 90 or not sample["reading"].get("load_per_cpu"):
                break
            if end - sample["at"] >= 240:
                covered = True
                break
            previous = sample["at"]
        if not covered:
            # A fresh but incomplete window must not weaken the missing-file live guard.
            reasons += machine_reasons(reading, policy)
            previous = end
            for sample in reversed(data["samples"]):
                if not 0 <= previous - sample["at"] <= 90:
                    break
                previous = sample["at"]
                state = sample["reading"].get("thermal", "unknown")
                if state in ("normal", "pressure"):
                    if state == "pressure" and reading.get("thermal") == "unknown":
                        reasons.append("thermal warning unresolved")
                    break
        reading["sample_reasons"] = list(dict.fromkeys(reasons))
        prior = data.get("heat_reasons", [])
        if data.get("episode") and not machine_recovered(data["samples"], policy, prior):
            reading["sample_reasons"] = list(dict.fromkeys(reading["sample_reasons"] + prior))
        reading["sampled"] = True
        return reading
    return _m_ui.machine_load()


def machine_sustained(samples, policy):
    """Require near-minute continuity, not five rapid invocations or a gap in sampling."""
    latest = samples[-1]
    def sustained(predicate, minutes):
        if not predicate(latest["reading"]):
            return False
        end = latest["at"]
        previous = end
        for sample in reversed(samples):
            at = sample["at"]
            if previous - at > 90 or at > previous or not predicate(sample["reading"]):
                return False
            if end - at >= (minutes - 1) * 60:
                return True
            previous = at
        return False
    reasons = []
    if sustained(lambda r: bool(r.get("load_per_cpu")) and
                 r["load_per_cpu"][1] > policy["load5_per_cpu"], 5):
        reasons.append("sustained 5-minute load")
    warning_at, previous = None, None
    for sample in samples:
        at, state = sample["at"], sample["reading"].get("thermal", "unknown")
        if previous is not None and not 0 <= at - previous <= 90:
            warning_at = None
        if state == "normal":
            warning_at = None
        elif state == "pressure" and warning_at is None:
            warning_at = at
        previous = at
    # Unknown cannot clear an observed warning; initial unknown alone never invents one.
    if warning_at is not None and latest["at"] - warning_at >= 120:
        reasons.append("sustained thermal pressure")
    # Low memory/disk are immediate admission constraints, not heat episodes.
    reasons += [r for r in machine_reasons(latest["reading"], policy)
                if r.startswith("free ")]
    return reasons


def machine_recovered(samples, policy, prior_heat):
    end, previous = samples[-1]["at"], samples[-1]["at"]
    for sample in reversed(samples):
        at, r = sample["at"], sample["reading"]
        loads = r.get("load_per_cpu")
        if previous - at > 90 or at > previous:
            return False
        if (loads and loads[1] > policy["load5_per_cpu"]) or r.get("thermal") == "pressure":
            return False
        if "sustained 5-minute load" in prior_heat and not loads:
            return False
        if "sustained thermal pressure" in prior_heat and r.get("thermal") != "normal":
            return False
        if end - at >= 120:
            return True
        previous = at
    return False


def machine_timestamp(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def machine_transitions(data):
    rows = data.get('transitions', [])
    return [{'at': r['at'], 'episode': r['episode'], 'state': r['state']}
            for r in rows if isinstance(r, dict) and machine_timestamp(r.get('at'))
            and re.fullmatch(r'[0-9a-f]{24}', str(r.get('episode', '')))
            and r.get('state') in ('open', 'close')][-100:] if isinstance(rows, list) else []


def machine_episode_due(inst):
    """One rolling display; only exceptional episodes belong in the wake queue."""
    data = machine_samples(inst, fresh=False)
    if not data:
        return None
    now = time.time()
    sample = data['samples'][-1]
    fresh = 0 <= now - sample['at'] <= 180
    hot = fresh and sample['reading'].get('thermal') == 'pressure'
    started = data.get('episode_started')
    prolonged = (data.get('episode') is True and machine_timestamp(started)
                 and 0 <= started <= now and now - started > 3600)
    identity = data.get('alert_id')
    if (data.get('alert_open') is True and re.fullmatch(r'[0-9a-f]{24}', str(identity))
            and (hot or prolonged)):
        return {'kind': 'machine-alert', 'episode': identity,
                'text': 'Machine thermal pressure; inspect machine care' if hot else
                        'Machine heavy-work hold exceeds 60 minutes; inspect machine care'}
    if data.get('episode') is True:
        return {'kind': 'machine', 'text': 'Heavy work held: ' + '; '.join(data['heat_reasons']),
                'informational': True}
    return None


def machine_publish(inst, path, text):
    """Publish only in the pinned instance root or its private state directory."""
    _m_common.safe_target(path)
    expected = inst.root.resolve() / (".brain" if path.parent.name == ".brain" else "")
    if path.parent == inst.root / '.brain/wake':
        expected = inst.root.resolve() / '.brain/wake'
    fd = os.open(str(path.parent), _m_history.HISTORY_DIR_FLAGS)
    name = ".machine-" + os.urandom(12).hex() + ".tmp"
    try:
        if _m_history.history_fd_path(fd) != str(expected):
            _m_common.die("machine sampler: state folder moved")
        out = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        with os.fdopen(out, "w") as stream:
            try:
                prior = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISREG(prior.st_mode):
                    os.fchmod(stream.fileno(), stat.S_IMODE(prior.st_mode))
            except FileNotFoundError:
                pass
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if _m_history.history_fd_path(fd) != str(expected):
            _m_common.die("machine sampler: state folder moved")
        os.replace(name, path.name, src_dir_fd=fd, dst_dir_fd=fd)
    finally:
        try:
            os.unlink(name, dir_fd=fd)
        except FileNotFoundError:
            pass
        os.close(fd)


def machine_sample_once(inst):
    # Sampling must not queue behind long instance mutations or scan the whole brain.
    # It shares the ordinary instance lock; a busy minute is skipped, with live fallback.
    import fcntl
    folder = inst.root / ".brain"
    _m_common.safe_target(folder / "lock")
    dfd = os.open(str(folder), _m_history.HISTORY_DIR_FLAGS)
    try:
        fd = os.open("lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
        try:
            if os.fstat(fd).st_nlink != 1:
                _m_common.die("machine sampler: instance lock is hard-linked")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("machine sample: skipped; instance busy")
                return
            stream = os.fdopen(fd, "r+")
            fd = None
            stream.seek(0)
            stream.truncate()
            stream.write(str(os.getpid()))
            stream.flush()
            _m_instance._LOCK["fh"], _m_instance._LOCK["depth"] = stream, 1
            os.environ["BRAIN_LOCK_HELD"] = f"{inst.root.resolve()}|{os.getpid()}"
            try:
                machine_sample(inst)
            finally:
                _m_instance._LOCK["depth"] = 0
                _m_instance.unlock()
        finally:
            if fd is not None:
                os.close(fd)
    finally:
        os.close(dfd)


def machine_commit_check(inst):
    # Sample-only minutes stay fast. A transition invokes Git and must restore the
    # ordinary write checks, including metadata reached through a worktree's common dir.
    if _m_common.outward_links(inst.root):
        _m_common.die("machine sampler: unsafe instance links; notice remains pending (brain check lists them)")
    meta = inst.root / ".git"
    if not meta.exists():
        return
    if meta.is_file():
        match = re.fullmatch(r"gitdir: (.+)\s*", meta.read_text().strip())
        if not match:
            _m_common.die("machine sampler: invalid Git metadata; notice remains pending")
        meta = (inst.root / match[1]).resolve()
    common = meta / "commondir"
    bases = {meta.resolve()}
    if common.is_file():
        bases.add((meta / common.read_text().strip()).resolve())
    def unreadable(exc):
        raise exc
    for base in bases:
        for e in os.scandir(base):
            if e.is_symlink() or _m_common.hard_linked(e):
                _m_common.die("machine sampler: linked Git metadata; notice remains pending")
        for rel in ("refs", "logs", "objects/info", "objects/pack"):
            folder = base / rel
            if folder.is_symlink():
                _m_common.die("machine sampler: linked Git metadata; notice remains pending")
            if not folder.exists():
                continue
            for current, dirs, files in os.walk(folder, followlinks=False, onerror=unreadable):
                for name in dirs + files:
                    entry = Path(current) / name
                    if entry.is_symlink() or _m_common.shared_file(entry):
                        _m_common.die("machine sampler: linked Git metadata; notice remains pending")
        # Loose objects are immutable; check their fanout directories, not every object.
        objects = base / "objects"
        if objects.is_dir():
            for e in os.scandir(objects):
                if e.is_symlink():
                    _m_common.die("machine sampler: linked Git object directory; notice remains pending")


def machine_notice(inst, data):
    pending = data.get("pending_notice")
    if not pending:
        return data
    machine_commit_check(inst)
    rows = machine_transitions(data)
    state = 'open' if data['episode'] else 'close'
    if not rows or (rows[-1]['episode'], rows[-1]['state']) != (data['episode_id'], state):
        at = data.get('pending_at')
        rows.append({'at': at if machine_timestamp(at) else time.time(),
                     'episode': data['episode_id'], 'state': state})
    # Machine care acts autonomously. Transitions never append to an inbox or wake a model.
    data = {**data, 'transitions': rows[-100:], 'pending_notice': None, 'pending_at': None}
    machine_publish(inst, inst.root / ".brain/machine.json", json.dumps(data) + "\n")
    return data


def machine_sample(inst):
    path = inst.root / ".brain/machine.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    prior = data.get("heat_reasons", [])
    metadata_ok = (type(data.get("episode", False)) is bool and isinstance(prior, list)
                   and all(r in ("sustained 5-minute load", "sustained thermal pressure") for r in prior)
                   and (not data.get("episode") or bool(prior)))
    pending = data.get("pending_notice")
    if pending is not None:
        metadata_ok = (metadata_ok and isinstance(pending, str) and
                       (pending == "heavy work hold cleared" or pending.startswith("hold heavy work: sustained"))
                       and bool(re.fullmatch(r"[0-9a-f]{24}", str(data.get("episode_id", "")))))
    if not metadata_ok:
        data = {**data, "episode": False, "heat_reasons": [], "pending_notice": None, "episode_id": "",
                'alert_open': False, 'alert_id': None, 'episode_started': None}
    if (data.get("pending_notice") and isinstance(data.get("pending_notice"), str) and
            re.fullmatch(r"[0-9a-f]{24}", str(data.get("episode_id", "")))):
        data = machine_notice(inst, data)  # deliver old transition before it can be superseded
    now = time.time()
    old = data.get("samples", [])
    old = old if isinstance(old, list) else []
    samples = sorted((s for s in old if machine_valid_sample(s) and now - 86400 <= s["at"] < now),
                     key=lambda s: s["at"])
    if samples and int(samples[-1]["at"] // 60) == int(now // 60):
        samples.pop()  # at most one reading per minute, even with repeated invocations
    samples.append({"at": now, "reading": _m_ui.machine_load()})
    samples = samples[-1440:]
    policy = machine_policy()
    reasons = machine_sustained(samples, policy)
    heat = [r for r in reasons if r.startswith("sustained")]
    prior = data.get("heat_reasons", [])
    prior = [r for r in prior if r in ("sustained 5-minute load", "sustained thermal pressure")] if isinstance(prior, list) else []
    active = bool(heat) or (bool(data.get("episode")) and not machine_recovered(samples, policy, prior))
    heat = list(dict.fromkeys(heat + prior)) if active else []
    changed = active != bool(data.get("episode"))
    # Persist the episode and pending notice atomically; retry a pending delivery after a crash.
    pending = None
    episode_id = data.get("episode_id", "")
    if changed:
        if active or not re.fullmatch(r"[0-9a-f]{24}", str(episode_id)):
            previous_alert = data.get('alert_id') if data.get('alert_open') is True else None
            episode_id = previous_alert if re.fullmatch(r'[0-9a-f]{24}', str(previous_alert)) else os.urandom(12).hex()
        pending = ("hold heavy work: " + "; ".join(heat)) if active else "heavy work hold cleared"
    started = data.get('episode_started')
    started = (started if data.get('episode') is True and machine_timestamp(started)
               and started <= now else now) if active else None
    thermal = samples[-1]['reading'].get('thermal', 'unknown')
    alert_open = active or thermal == 'pressure' or (data.get('alert_open') is True and thermal == 'unknown'
                                                    and data.get('episode') is not True)
    alert_id = data.get('alert_id') if data.get('alert_open') is True else None
    if alert_open and not re.fullmatch(r'[0-9a-f]{24}', str(alert_id)):
        alert_id = episode_id if active else os.urandom(12).hex()
    result = {"samples": samples, "episode": active, "episode_id": episode_id,
              "episode_started": started, "alert_open": alert_open, "alert_id": alert_id if alert_open else None,
              "pending_notice": pending, "pending_at": now if pending else None,
              "transitions": machine_transitions(data), "heat_reasons": heat}
    machine_publish(inst, path, json.dumps(result) + "\n")
    if pending:
        machine_notice(inst, result)
    print(f"machine sample: {len(samples)} readings; " + ("; ".join(heat) if active else "no sustained heat"))


def machine_cron_quote(value):
    return "'%'".join(shlex.quote(part) for part in value.split("%"))


def machine_service(inst, action, yes=False):
    """Dry by default. No network, privilege escalation, shell setup or installs."""
    import plistlib
    label = "org.brain.machine." + hashlib.sha256(str(inst.root).encode()).hexdigest()[:16]
    args = [sys.executable, str(_m_common.CORE / "bin/brain"), "machine", "sample"]
    service_env = {"BRAIN_NO_REEXEC": "1", "BRAIN_CONFIG": str(_m_instance.brains_file())}
    if any("\n" in v or "\r" in v for v in args + [str(inst.root), str(_m_instance.brains_file())]):
        _m_common.die("machine sampler: service paths cannot contain line breaks")
    if sys.platform == "darwin":
        path = Path.home() / "Library/LaunchAgents" / (label + ".plist")
        payload = plistlib.dumps({"Label": label, "ProgramArguments": args,
                                 "WorkingDirectory": str(inst.root), "StartInterval": 60,
                                 "EnvironmentVariables": service_env}).decode()
        print(payload if action == "install" else "remove " + str(path))
        if not yes:
            print("dry run; --yes requires the operator's consent")
            return
        domain = f"gui/{os.getuid()}"
        if path.exists():
            try:
                if plistlib.loads(path.read_bytes()).get("Label") != label:
                    raise ValueError("different label")
            except (OSError, ValueError, plistlib.InvalidFileException):
                _m_common.die("machine sampler: existing plist is not this instance's service")
        loaded = subprocess.run(["launchctl", "print", domain + "/" + label], capture_output=True)
        if loaded.returncode == 0:
            stopped = subprocess.run(["launchctl", "bootout", domain + "/" + label], capture_output=True)
            if stopped.returncode:
                _m_common.die("machine sampler did not stop; left its plist in place")
        if action == "uninstall":
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            _m_common.atomic_write(path, payload)
            r = subprocess.run(["launchctl", "bootstrap", domain, str(path)], capture_output=True, text=True)
            if r.returncode:
                _m_common.die("machine sampler not loaded: " + r.stderr.strip())
    else:
        command = "cd " + machine_cron_quote(str(inst.root)) + " && env " + " ".join(
            machine_cron_quote(v) for v in [k + "=" + v for k, v in service_env.items()] + args)
        line = "* * * * * " + command.replace("%", r"\%") + " >/dev/null 2>&1 # " + label
        print(line if action == "install" else "remove cron entry # " + label)
        if not yes:
            print("dry run; --yes requires the operator's consent")
            return
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        if r.returncode not in (0, 1):
            _m_common.die("cannot read crontab")
        lines = [s for s in r.stdout.splitlines() if not s.endswith(" # " + label)]
        if action == "install":
            lines.append(line)
        r = subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True)
        if r.returncode:
            _m_common.die("cannot update crontab")


def machine_dir():
    # Independent of BRAIN_CONFIG/XDG_CONFIG_HOME: every brain for this OS user shares slots.
    return Path.home() / ".local/state/brain"


def machine_policy(strict=True):
    import math
    try:
        try:
            supplied = json.loads((machine_dir() / "machine-care.json").read_text())
        except FileNotFoundError:
            supplied = {}
        if not isinstance(supplied, dict):
            raise ValueError("configuration must be an object")
        policy = {**MACHINE_DEFAULTS, **supplied}
        for k in MACHINE_DEFAULTS:
            v = policy[k]
            if type(v) not in (int, float) or not math.isfinite(v) or v < 0:
                raise ValueError("invalid " + k)
            if k.startswith("verify_") and (type(v) is not int or v < 1):
                raise ValueError("verify limits must be positive integers")
            if k.startswith("night_") and (type(v) is not int or v > 23):
                raise ValueError("night hours must be integers from 0 to 23")
        policy["_configured"] = list(supplied)
        return policy
    except (OSError, ValueError):
        message = "machine policy invalid; repair ~/.local/state/brain/machine-care.json"
        if strict:
            _m_common.die(message + "; no heavy work started")
        return {**MACHINE_DEFAULTS, "_configured": [], "_error": message}


def machine_reasons(reading, policy):
    if "sample_reasons" in reading:
        return reading["sample_reasons"]
    reasons = []
    loads = reading.get("load_per_cpu")
    if loads and loads[1] > policy["load5_per_cpu"]:
        reasons.append(f"5-minute load/CPU {loads[1]:.2f} > {policy['load5_per_cpu']:g}")
    if reading.get("thermal") == "pressure":
        reasons.append("thermal pressure")
    for field, limit, title in (("mem_free_gb", "memory_free_gb", "free memory"),
                                ("disk_free_gb", "disk_free_gb", "free disk")):
        threshold = policy[limit]
        total = reading.get("mem_total_gb" if field == "mem_free_gb" else "disk_total_gb")
        if total and limit not in policy.get("_configured", []):
            threshold = min(threshold, total * (0.25 if field == "mem_free_gb" else 0.2))
        if field in reading and reading[field] < threshold:
            note = "; threshold exceeds machine; adjust machine-care.json" if total and threshold >= total else ""
            reasons.append(f"{title} {reading[field]:g} GB < {threshold:g} GB" + note)
    return reasons


def machine_limit(policy, hour=None):
    if hour is None:
        try:
            from zoneinfo import ZoneInfo
            with open("/etc/localtime", "rb") as source:
                hour = datetime.datetime.now(ZoneInfo.from_file(source)).hour
        except (OSError, ValueError, ImportError):
            return min(policy["verify_day"], policy["verify_night"])
    start, end = policy["night_start"], policy["night_end"]
    night = (hour >= start or hour < end) if start > end else start <= hour < end
    return policy["verify_night" if night else "verify_day"]


def process_stamp(pid, deadline=None):
    """PID plus OS start time prevents reclaiming a recycled PID as our worker."""
    def remaining():
        budget = 2 if deadline is None else min(2, deadline - time.monotonic())
        if budget <= 0:
            raise subprocess.TimeoutExpired('process identity', 0)
        return budget
    budget = remaining()
    try:
        if sys.platform.startswith("linux"):
            raw = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
            if raw[0] == "Z":
                return None
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            if not raw[19].isdigit() or not re.fullmatch(r"[0-9a-fA-F-]{36}", boot):
                return "unknown"
            remaining()
            return "linux:" + boot + ":" + raw[19]
        r = subprocess.run(["ps", "-p", str(pid), "-o", "lstart=", "-o", "stat="],
                           capture_output=True, text=True, timeout=budget,
                           env={**os.environ, "LC_ALL": "C", "TZ": "UTC0"})
        remaining()
        value = r.stdout.strip()
        if r.returncode == 0 and value:
            return None if value.split()[-1].startswith("Z") else " ".join(value.split()[:-1])
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except PermissionError:
            return "unknown"
        return "unknown"
    except (OSError, ValueError, IndexError, subprocess.SubprocessError) as error:
        if deadline is not None and isinstance(error, subprocess.TimeoutExpired):
            raise
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except OSError:
            pass
        return "unknown"


class machine_queue:
    """Short transaction lock; atomic JSON publication preserves queued work after a crash.
    Only opaque scope hashes, never node names, branches, commands or paths, identify work.
    """
    def __enter__(self):
        import fcntl
        self.folder = machine_dir()
        self.folder.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(str(self.folder), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            self.lock = os.open("machine-care.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        except BaseException:
            os.close(self.fd)
            raise
        if os.fstat(self.lock).st_nlink != 1 or not stat.S_ISREG(os.fstat(self.lock).st_mode):
            os.close(self.lock)
            os.close(self.fd)
            _m_common.die("machine care: lock is hard-linked")
        fcntl.flock(self.lock, fcntl.LOCK_EX)
        try:
            qfd = os.open("machine-care-queue.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.fd)
            with os.fdopen(qfd) as f:
                self.rows = json.load(f)
            if not isinstance(self.rows, list) or any(not self.valid(r) for r in self.rows):
                raise ValueError("machine care: invalid queue; preserve it for inspection")
        except FileNotFoundError:
            self.rows = []
        except BaseException:
            os.close(self.lock)
            os.close(self.fd)
            raise
        try:
            self.rows = [r for r in self.rows if self.alive(r)]
        except BaseException:
            os.close(self.lock)
            os.close(self.fd)
            raise
        return self

    @staticmethod
    def valid(row):
        def identity(who):
            return (isinstance(who, list) and len(who) == 2 and type(who[0]) is int and who[0] > 0
                    and isinstance(who[1], str) and bool(who[1]))
        return (isinstance(row, dict) and isinstance(row.get("token"), str)
                and bool(re.fullmatch(r"[0-9a-f]{32}", row["token"])) and identity(row.get("owner"))
                and row.get("kind") in ("heavy", "verify") and row.get("state") in ("queued", "running")
                and ("scope" not in row or (isinstance(row["scope"], list) and len(row["scope"]) == 2
                     and all(isinstance(s, str) and re.fullmatch(r"[0-9a-f]{64}", s) for s in row["scope"])))
                and ("child" not in row or identity(row["child"])))

    @staticmethod
    def alive(row):
        for key in ("owner", "child"):
            who = row.get(key)
            if who:
                stamp = process_stamp(who[0])
                if stamp == "unknown" or (stamp is not None and stamp == who[1]):
                    return True
        return False

    def __exit__(self, typ, value, tb):
        name = "machine-care-" + os.urandom(12).hex() + ".tmp"
        try:
            if typ is None:
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
                with os.fdopen(fd, "w") as f:
                    json.dump(self.rows, f)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(name, "machine-care-queue.json", src_dir_fd=self.fd, dst_dir_fd=self.fd)
        finally:
            try:
                os.unlink(name, dir_fd=self.fd)
            except FileNotFoundError:
                pass
            os.close(self.lock)
            os.close(self.fd)


def machine_scope(inst, node):
    return [hashlib.sha256(s.encode()).hexdigest() for s in (str(inst.root.resolve()), node)]


def job_queue_projection(inst, node, owner):
    """Read recorded admission only. Never acquire/prune the queue or infer live ownership."""
    path = machine_dir() / 'machine-care-queue.json'
    try:
        rows = json.loads(path.read_text())
        if not isinstance(rows, list) or any(not machine_queue.valid(r) for r in rows):
            raise ValueError('invalid recorded machine queue')
        matches = [r for r in rows if r.get('scope') == machine_scope(inst, node) and r['owner'] == owner]
        if len(matches) != 1:
            return None
        row = matches[0]
        waiting = [r for r in rows if r['kind'] == row['kind'] and r['state'] == 'queued']
        return {'state': row['state'], 'owner': row['owner'], 'owner_activity': 'unknown',
                'position': waiting.index(row) + 1 if row in waiting else None,
                'recorded_at': path.stat().st_mtime, 'source': 'recorded native admission queue'}
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError):
        return {'state': 'unknown', 'position': None, 'owner': None, 'owner_activity': 'unknown'}


def machine_wait(kind="heavy", inst=None, node=None):
    token = os.urandom(16).hex()
    owner = [os.getpid(), process_stamp(os.getpid())]
    if owner[1] in (None, "unknown"):
        _m_common.die("machine care: cannot establish this process's start time; no heavy work started")
    previous, last_report, started = None, 0, time.monotonic()
    try:
        while True:
            policy = machine_policy(strict=False)
            reading = machine_reading()
            reasons = machine_reasons(reading, policy)
            if policy.get("_error"):
                reasons.append(policy["_error"])
            with machine_queue() as queue:
                row = next((r for r in queue.rows if r["token"] == token), None)
                if row is None:
                    row = {"token": token, "owner": owner, "kind": kind, "state": "queued"}
                    if inst is not None and node is not None:
                        row["scope"] = machine_scope(inst, node)
                    queue.rows.append(row)
                waiting = [r for r in queue.rows if r["kind"] == kind and r["state"] == "queued"]
                running = sum(r["kind"] == "verify" and r["state"] == "running" for r in queue.rows)
                position = waiting.index(row) + 1
                if not reasons and (kind != "verify" or (position == 1 and running < machine_limit(policy))):
                    row["state"] = "running"
                    return token
            detail = ("; ".join(reasons) if reasons else
                      f"queued #{position}, {running}/{machine_limit(policy)} verify slots busy")
            status = (re.sub(r"\d+(?:\.\d+)?", "#", detail), position, running, machine_limit(policy))
            now = time.monotonic()
            if status != previous or now - last_report >= 60:
                print(f"machine care: {kind} held {now - started:.0f}s; {detail}; retry in {MACHINE_RETRY}s",
                      flush=True)
                previous, last_report = status, now
            time.sleep(MACHINE_RETRY)
    except BaseException:
        machine_release(token)
        raise


def machine_release(token):
    with machine_queue() as queue:
        queue.rows = [r for r in queue.rows if r["token"] != token]


def machine_child(token, pid):
    stamp = process_stamp(pid)
    if stamp in (None, "unknown"):
        _m_common.die("machine care: cannot establish verify child start time; verify not started")
    with machine_queue() as queue:
        for row in queue.rows:
            if row["token"] == token:
                row["child"] = [pid, stamp]
                return
        _m_common.die("machine care: reservation disappeared; verify not started")


def machine_state(inst=None):
    reading = machine_reading(inst)
    policy = machine_policy(strict=False)
    reading["held"] = machine_reasons(reading, policy)
    if policy.get("_error"):
        reading["held"].append(policy["_error"])
    reading["verify_limit"] = machine_limit(policy)
    # Reading the live view never creates state or a lock file.
    try:
        rows = json.loads((machine_dir() / "machine-care-queue.json").read_text())
        if not isinstance(rows, list) or any(not machine_queue.valid(r) for r in rows):
            raise ValueError("invalid queue")
        rows = [r for r in rows if machine_queue.alive(r)]
        reading["queued"] = sum(r["state"] == "queued" for r in rows)
        reading["waiting_scopes"] = [r["scope"] for r in rows if r["state"] == "queued" and "scope" in r]
        reading["verifying"] = sum(r["kind"] == "verify" and r["state"] == "running" for r in rows)
    except FileNotFoundError:
        reading.update(queued=0, verifying=0, waiting_scopes=[])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        reading.update(queued=None, verifying=None, waiting_scopes=[])
    return reading


@contextlib.contextmanager
def machine_hold_store(inst):
    """Short private transaction shared by due ticks and watcher delivery receipts."""
    import fcntl
    path = inst.root / '.brain/wake/machine-holds.json'
    lock = path.with_suffix('.lock')
    if _m_common.outward_links(inst.root):
        raise ValueError('unsafe machine hold state')
    path.parent.mkdir(exist_ok=True)
    _m_common.safe_target(lock)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a+') as stream:
        if os.fstat(stream.fileno()).st_nlink != 1:
            raise ValueError('linked machine hold lock')
        fcntl.flock(stream, fcntl.LOCK_EX)
        _m_common.safe_target(path)
        data = json.loads(path.read_text()) if path.exists() else {'version': 1, 'holds': {}}
        if (not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('holds'), dict)
                or any(not re.fullmatch(r'[0-9a-f]{64}', key) or not isinstance(row, dict)
                       or not re.fullmatch(r'[0-9a-f]{64}', str(row.get('reason', '')))
                       or not re.fullmatch(r'[0-9a-f]{32}', str(row.get('episode', '')))
                       or type(row.get('reported')) is not bool for key, row in data['holds'].items())):
            raise ValueError('invalid machine hold state')
        before = json.dumps(data, sort_keys=True)
        yield data
        if json.dumps(data, sort_keys=True) != before:
            machine_publish(inst, path, json.dumps(data, sort_keys=True) + '\n')


def machine_hold_items(inst, state=None):
    """One opaque episode per waiting scope and reason class, never per load reading."""
    state = machine_state(inst) if state is None else state
    if state.get('queued') is None:
        raise ValueError('machine queue unreadable')
    own = machine_scope(inst, 'root')
    scopes = {s[1] for s in state.get('waiting_scopes', []) if s[0] == own[0]}
    reasons = state.get('held', [])
    if not reasons and state.get('queued') and state.get('verifying', 0) >= state['verify_limit']:
        reasons = ['verify slots busy']
    def reason_class(text):
        if '5-minute load' in text:
            return 'load'
        if 'thermal' in text:
            return 'thermal'
        return re.sub(r'\d+(?:\.\d+)?', '#', text)
    # Raw and sustained readings describe the same reason class.
    reason = hashlib.sha256(json.dumps(sorted(set(map(reason_class, reasons)))).encode()).hexdigest()
    active = scopes | {own[1]} if scopes and reasons else set()
    if not active and not (inst.root / '.brain/wake/machine-holds.json').exists():
        return {}
    with machine_hold_store(inst) as data:
        previous = data['holds']
        data['holds'] = {scope: previous[scope] if scope in previous and previous[scope]['reason'] == reason
                         else {'reason': reason, 'episode': os.urandom(16).hex(), 'reported': False}
                         for scope in active}
        return {scope: dict(row) for scope, row in data['holds'].items()}


def machine_hold_claim(inst, episode, scope, deliver=None):
    """A due result or durable wake enqueue acknowledges the same episode exactly once."""
    with machine_hold_store(inst) as data:
        row = data['holds'].get(scope)
        if not row or row['episode'] != episode or row['reported']:
            return False
        if deliver is not None and not deliver():
            return False
        row['reported'] = True
        return True


def machine_summary(reading):
    loads = reading.get("load_per_cpu")
    return ("machine: load/CPU 1/5/15m " + ("/".join(f"{v:.2f}" for v in loads) if loads else "unknown") +
            f"; free memory {reading.get('mem_free_gb', 'unknown')} GB; free disk " +
            f"{reading.get('disk_free_gb', 'unknown')} GB; thermal {reading.get('thermal', 'unknown')}; " +
            f"verifies {reading['verifying']}/{reading['verify_limit']}; queued {reading['queued']}; " +
            ("held: " + "; ".join(reading["held"]) if reading["held"] else "heavy work allowed"))


def cmd_heavy(a):
    inst = _m_nodes.load()
    node = inst.node(getattr(a, "node", None) or "root")
    token = machine_wait(inst=inst, node=node["id"])
    machine_release(token)
    print("machine care: heavy work allowed (admission only)")


# ---------- machine subscriptions ----------
def cmd_machine(a):
    inst = _m_nodes.load()
    if a.action == 'wake-setup':
        if not getattr(a, 'id', None):
            _m_common.die('machine wake-setup requires a target node')
        from . import hosts as _m_hosts
        print(json.dumps(_m_hosts.wake_setup_plan(inst, inst.node(a.id)), indent=2))
        return
    if a.action == "sample":
        machine_sample_once(inst)
        from . import digest as _m_digest
        try:
            _m_digest.daily_retro(inst)
        except (OSError, ValueError, TypeError):
            print('daily retro unavailable; retained job records are unchanged')
        from . import watch as _m_watch
        try:
            _m_watch.watch_once(inst)  # same minute timer; sampler has released its instance lock
        except (OSError, ValueError):
            print('watch: watcher state invalid; retained for repair (brain due root)')
        return
    if a.action in ("install", "uninstall"):
        if not a.yes:
            machine_service(inst, a.action, False)
            return
        _m_instance.unlock()
        with _m_instance.reservation(inst, "machine-service"):
            machine_service(_m_instance.Instance(inst.root), a.action, a.yes)
        return
    nodes = inst.nodes()
    if a.missing_homes:
        for n in nodes:
            if n["external"] and not n["archived"] and not (n["home_path"] and n["home_path"].exists()):
                print(f"{n['id']}\t{n['fm'].get('home_url', 'none')}")
        return
    print(machine_summary(machine_state(inst)))
    print(f"machine: {inst.local['machine']}   bindings: {_m_common.tilde(inst.local_path)} (not in git)")
    print("fallback: " + json.dumps(inst.local.get("fallback", [])))
    name, others = _m_nodes.register(inst.root), [k for k, r in _m_instance.brains_here() if r.resolve() != inst.root.resolve()]
    dirs = "; ".join(f"{rt} {', '.join(ds)}" for rt, ds in sorted(inst.local.get("global", {}).items()))
    print(f"this brain: {name}   pointer in: {dirs or 'none (brain link --global)'}"
          + (f"   other brains here: {', '.join(others)}" if others else "") + "\n")
    for n in nodes:
        loc = n["local"]
        bits = []
        if n["external"]:
            hp = n["home_path"]
            bits.append(f"home {_m_common.tilde(hp) if hp else 'MISSING'}{'' if not hp or hp.exists() else ' (not found)'}")
        for k in _m_common.LOCAL_KEYS:
            if k == "home_project" and not n["external"]:
                continue
            bits.append(f"{k} {'✓' if loc.get(k) else 'MISSING'}")
        print(f"  {n['id']:<28} {'  '.join(bits)}")
    scheds = _m_schedules.read_schedules(inst)
    if scheds:
        bound = inst.local.get("schedules", {})
        print("\nschedules (each runs on one machine):")
        for x in scheds:
            print(f"  {x['id']:<28} {'on this machine' if bound.get(x['id']) else 'not here'}")
    print("\nTo bind what's missing: tell the root coordinator \"set up this machine\" (skill machine-setup).")
