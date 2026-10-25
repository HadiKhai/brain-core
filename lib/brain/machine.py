"""Machine mechanics for Brain."""
import contextlib
import contextvars
import datetime
import hashlib
import json
import math
import os
import re
import shlex
import signal
import threading
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
                    "verify_day": 2, "verify_night": 1, "night_start": 22, "night_end": 8, "cpu_idle_min_pct": 15,
                    "memory_pressure_max_pct": 80, "max_hold_minutes": 20}
MACHINE_RETRY = 5
ADMISSION_TIMING = contextvars.ContextVar("brain_admission_timing", default=None)


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
    for key in ("cpus", "mem_free_gb", "mem_total_gb", "disk_free_gb", "disk_total_gb",
                "cpu_idle_pct", "memory_pressure_pct"):
        if key in reading and not number(reading[key]):
            return False
    if any(key in reading and reading[key] > 100 for key in ("cpu_idle_pct", "memory_pressure_pct")):
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
        if (type(data.get("episode", False)) is not bool or not isinstance(prior, list) or any(not machine_heat_reason(r) for r in prior)
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
        # CPU admission is instantaneous: a good observation clears a held step.
        if "cpu_idle_pct" in reading:
            policy = machine_policy(strict=False)
            if ('sustained thermal pressure' in data.get('heat_reasons', [])
                    and not machine_thermal_recovered(data['samples'])):
                reading['sample_reasons'] = machine_reasons(reading, policy) + ['thermal warning unresolved']
            return reading
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
    if "cpu_idle_pct" in latest["reading"]:
        reasons += [r for r in machine_reasons(latest["reading"], policy)
                    if r.startswith(("CPU idle", "memory pressure"))]
    if sustained(lambda r: "cpu_idle_pct" not in r and bool(r.get("load_per_cpu")) and
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
    latest = samples[-1]["reading"]
    if "sustained thermal pressure" in prior_heat:
        return machine_thermal_recovered(samples)
    if "cpu_idle_pct" in latest:
        return not machine_reasons(latest, policy)
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
                   and all(machine_heat_reason(r) for r in prior)
                   and (not data.get("episode") or bool(prior)))
    pending = data.get("pending_notice")
    if pending is not None:
        metadata_ok = (metadata_ok and isinstance(pending, str) and
                       (pending == "heavy work hold cleared" or pending.startswith("hold heavy work: "))
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
    prior = [r for r in prior if machine_heat_reason(r)] if isinstance(prior, list) else []
    if "cpu_idle_pct" in samples[-1]["reading"]:
        heat += [r for r in reasons if r.startswith("CPU idle")]
        # Legacy load episodes close as soon as the CPU-based policy admits.
        active = bool(heat) or ('sustained thermal pressure' in prior and not machine_thermal_recovered(samples))
    else:
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
            if k.endswith("_pct") and v > 100:
                raise ValueError("percentage exceeds 100")
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
    idle = reading.get("cpu_idle_pct")
    if idle is not None and idle < policy["cpu_idle_min_pct"]:
        reasons.append(f"CPU idle {idle:g}% < {policy['cpu_idle_min_pct']:g}% (5-minute load/CPU "
                       + (f"{loads[1]:.2f}" if loads else "unknown") + ")")
    if idle is None and loads and loads[1] > policy["load5_per_cpu"]:
        reasons.append(f"5-minute load/CPU {loads[1]:.2f} > {policy['load5_per_cpu']:g}")
    pressure = reading.get("memory_pressure_pct")
    if pressure is not None and pressure > policy["memory_pressure_max_pct"]:
        reasons.append(f"memory pressure {pressure:g}% > {policy['memory_pressure_max_pct']:g}%")
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



class MachineRun(str):
    """Opaque queue token with process-local result and injected clock."""
    def __new__(cls, clock):
        token = super().__new__(cls, os.urandom(16).hex())
        token.clock, token.result = clock, 0
        return token


class MachineSignal(BaseException):
    def __init__(self, number):
        self.number = number
        super().__init__('received ' + signal.Signals(number).name)


@contextlib.contextmanager
def machine_signals():
    """Unwind catchable termination; restore embedding applications' handlers."""
    previous = {}
    def stop(number, frame):
        raise MachineSignal(number)
    try:
        if threading.current_thread() is threading.main_thread():
            for number in (signal.SIGTERM, signal.SIGHUP):
                previous[number] = signal.signal(number, stop)
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def machine_exit_fields(error=None, result=0):
    if isinstance(error, MachineSignal):
        return 'cancelled', str(error)
    if isinstance(error, KeyboardInterrupt):
        return 'cancelled', 'interrupted (KeyboardInterrupt)'
    if error is not None:
        return 'error', type(error).__name__ + ': ' + (str(error) or 'command stopped before completion')
    if result:
        return 'failed', 'command exited with status ' + str(result)
    return 'success', 'command completed successfully'


def machine_terminal_row(row, now, outcome, reason):
    """The only terminal transition, shared by unwind and dead-owner recovery."""
    if row.get('ended_at') is not None:
        return False
    row.update(state='completed' if outcome == 'success' else 'exited', outcome=outcome,
               reason=reason, ended_at=now, closed_at=now, updated_at=now)
    row.pop('finalizing', None)
    return True


def machine_finish_intent(inst, token, error=None):
    """Publish owner intent before cleanup exposes queue removal to recovery."""
    if inst is None:
        return
    outcome, reason = machine_exit_fields(error, token.result)
    for attempt in range(2):
        try:
            with machine_hold_store(inst) as data:
                row = data.get('waits', {}).get(token)
                if row is not None and row.get('ended_at') is None:
                    row['finalizing'] = True
                    # Success is provisional until every cleanup action finishes.
                    row['terminal_intent'] = ({'outcome': outcome, 'reason': reason}
                                              if outcome != 'success' else None)
            return
        except (OSError, ValueError):
            if attempt:
                print('machine care: terminal intent unavailable; private state needs repair', file=sys.stderr)


def machine_cleanup(token, actions, primary=None):
    """Attempt every cleanup; preserve the primary exception and secondary evidence."""
    errors = []
    for action in actions:
        try:
            action()
        except BaseException as error:
            errors.append(error)
    if errors:
        token.cleanup_errors = getattr(token, 'cleanup_errors', []) + [
            type(error).__name__ + ': ' + str(error) for error in errors]
        if primary is None:
            raise errors[0]


def machine_terminal(inst, token, error=None, result=0):
    if inst is None:
        return
    outcome, reason = machine_exit_fields(error, result)
    with machine_hold_store(inst) as data:
        row = data.get('waits', {}).get(token)
        if row is not None:
            if machine_terminal_row(row, token.clock.time(), outcome, reason):
                if getattr(token, 'cleanup_errors', None):
                    row['cleanup_errors'] = list(token.cleanup_errors)
    try:
        machine_wait_notices(inst, token.clock.time())
    except (Exception, SystemExit):
        print('machine care: terminal wake pending; notice scan will retry', file=sys.stderr)


@contextlib.contextmanager
def machine_run(kind, inst, node, job=None, clock=None, close_success=True, release=True):
    """Own admission through cleanup; all unsuccessful exits share one finalizer."""
    clock = clock or time
    token = MachineRun(clock)
    row = dict(token=str(token), owner=[os.getpid(), 'unknown'], kind=kind, state='admitting',
               since=clock.time(), reason='checking machine admission', reading={},
               clears_when='admission checks complete', managed=True)
    if inst is not None and node is not None:
        row['scope'] = machine_scope(inst, node)
    with machine_signals():
        try:
            row['owner'][1] = process_stamp(os.getpid()) or 'unknown'
            if inst is not None and node is not None:
                machine_record_wait(inst, node, job, row, clock.time())
            yield token
            if close_success:
                machine_finish_intent(inst, token)
                if release:
                    machine_release(token)
                machine_terminal(inst, token, result=token.result)
        except BaseException as error:
            # Retry a transient private publication failure once. Never mask the
            # original error with a queue-release failure or invent a successful run.
            machine_finish_intent(inst, token, error)
            machine_cleanup(token, [lambda: machine_release(token)], primary=error)
            if inst is not None and node is not None:
                for attempt in range(2):
                    try:
                        with machine_hold_store(inst) as data:
                            data.setdefault('waits', {}).setdefault(str(token),
                                dict(row, node=node, job=job, updated_at=clock.time()))
                        machine_terminal(inst, token, error)
                        break
                    except (OSError, ValueError):
                        if attempt:
                            print('machine care: terminal record unavailable; private state needs repair', file=sys.stderr)
            raise


def machine_record_wait(inst, node, job, row, now):
    # Per-instance details remain private; the shared machine queue uses opaque owners.
    from . import hosts as _m_hosts
    with machine_hold_store(inst) as data:
        waits = data.setdefault('waits', {})
        previous = waits.get(row['token'], {})
        if previous.get('ended_at') is not None:
            return
        waits[row['token']] = {**previous, **row, 'node': node, 'job': job,
                               'updated_at': now}
        # Pending delivery is authority, not disposable history. Retired tokens must
        # outlive the historical inbox markers they suppress (which have no expiry).
        closed = sorted((k for k, v in waits.items() if
                         ((v.get('notified_completed' if v.get('outcome') == 'success' else 'notified_exited')
                           and getattr(_m_hosts, 'wake_terminal_delivered', lambda *_: False)(inst, hashlib.sha256(
                               (k + (':completed' if v.get('outcome') == 'success' else ':exited')).encode()).hexdigest())) or
                          (not v.get('managed') and v['state'] == 'running' and
                           (v.get('resumed_at') is None or v.get('notified_resumed'))))),
                        key=lambda k: waits[k]['updated_at'])
        retired = set(data.get('retired_waits', []))
        for key in closed[:-100]:
            retired.add(key)
            del waits[key]
        data['retired_waits'] = sorted(retired)
    machine_wait_notices(inst, now)


def machine_wait_text(row):
    since = datetime.datetime.fromtimestamp(row['since'], datetime.timezone.utc).isoformat()
    return (f"{row['kind']} {row.get('job') or row['token'][:8]} held since {since}: {row['reason']}; "
            f"clears when {row['clears_when']}")


def machine_wait_notices(inst, now=None):
    from . import hosts as _m_hosts
    from . import transitions as _m_transitions
    from . import guard as _m_guard
    now = time.time() if now is None else now
    with machine_hold_store(inst) as data:
        # Prune dead continuations from reporting, without deleting their evidence.
        try:
            live = json.loads((machine_dir() / 'machine-care-queue.json').read_text())
            if not isinstance(live, list) or any(not machine_queue.valid(r) for r in live):
                raise ValueError('invalid machine queue')
            live = {r['token'] for r in live if machine_queue.alive(r)}
        except FileNotFoundError:
            live = set()
        except (OSError, ValueError):
            live = None  # unknown cannot establish cancellation
        for token, row in data.get('waits', {}).items():
            if row.get('ended_at') is None:
                stamp = process_stamp(row['owner'][0])
                dead = stamp is None or (stamp != 'unknown' and row['owner'][1] != 'unknown'
                                         and stamp != row['owner'][1])
                unknown = stamp == 'unknown' or row['owner'][1] == 'unknown'
                if dead:
                    intent = row.get('terminal_intent') if row.get('finalizing') else None
                    if (isinstance(intent, dict) and intent.get('outcome') in ('error', 'failed', 'cancelled')
                            and isinstance(intent.get('reason'), str) and intent['reason']):
                        machine_terminal_row(row, now, intent['outcome'], intent['reason'])
                    else:
                        machine_terminal_row(row, now, 'abandoned', 'owner stopped before the command completed')
                elif (not row.get('finalizing') and unknown and row['state'] in ('queued', 'running')
                      and (live is None or token not in live)):
                    machine_terminal_row(row, now, 'unknown',
                        'owner identity is unknown; command completion cannot be established')
                elif (row['state'] == 'queued' and live is not None and token not in live
                      and not row.get('managed')):
                    machine_terminal_row(row, now, 'error',
                        'command reservation is missing; owner has not been shown to have stopped')
                # A known-live managed owner owns its outcome through cleanup.
                # Queue removal precedes that finalizer; recovery must not turn
                # this ordinary release window into an immutable invented cause.
            elapsed = now - row['since']
            events = []
            if row['state'] == 'exited':
                events.append(('exited', row['node']))
            elif row['state'] == 'completed':
                events.append(('completed', row['node']))
            elif row.get('closed_at') is not None:
                continue
            elif row['state'] == 'queued':
                if elapsed >= 900:
                    events.append(('coordinator', row['node']))
                if elapsed >= 2700:
                    events.append(('root', 'root'))
            elif row.get('resumed_at') is not None:
                events.append(('resumed', row['node']))
            for event, target in events:
                if row.get('notified_' + event):
                    continue
                identity = hashlib.sha256((token + ':' + event).encode()).hexdigest()
                # Durable report and stable identity; terminal wakes also explain the exit.
                try:
                    n = inst.node(target)
                    source_node = inst.node(row['node'])
                except SystemExit:
                    # Renamed/removed routing retains its undelivered receipt;
                    # it cannot prevent another owned command from starting.
                    continue
                marker = '<!-- machine-wait:' + token + ':' + event + ' -->'
                text = ('command completed: ' + row['reason'] if event == 'completed' else
                        'held step exited: ' + row['reason'] if event == 'exited' else machine_wait_text(row))
                if event == 'resumed':
                    text = 'held step resumed automatically: ' + text
                    if row.get('reduced'):
                        text += '; reduced parallelism 1'
                at = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat()
                text = _m_guard.redact(text, _m_guard.never_terms(n) + _m_guard.never_terms(source_node))
                line = '- ' + at + ' from machine: ' + text + ' ' + marker + '\n'
                try:
                    created = _m_transitions.inbox_append(n['dir'], line, marker, nonblocking=True)
                    if created and event in ('exited', 'completed'):
                        _m_common.touch(n['dir'] / 'inbox.md')
                except BlockingIOError:
                    continue
                # Enqueue is idempotent across an interrupted receipt publication.
                reason = 'machine-wait:' + identity + (': ' + text if event in ('exited', 'completed') else '')
                if _m_hosts.request_wake(inst, inst.node(target), reason,
                                        items=[{'id': identity, 'kind': 'machine-' + event}]):
                    row['notified_' + event] = True


def machine_exit_notice(inst, identity):
    """Resolve a terminal wake's plain reason privately at delivery time."""
    from . import guard as _m_guard
    with machine_hold_store(inst) as data:
        for token, row in data.get('waits', {}).items():
            if (row['state'] in ('exited', 'completed') and
                    hashlib.sha256((token + (':completed' if row['state'] == 'completed' else ':exited')).encode()).hexdigest() == identity):
                return _m_guard.redact(row['reason'], _m_guard.never_terms(inst.node(row['node'])))
    return 'command exited; read its recorded reason'


def machine_wait_pending_ids(inst):
    """Only live queued episodes may deliver a hold escalation; unknown retains it."""
    with machine_hold_store(inst) as data:
        try:
            rows = json.loads((machine_dir() / 'machine-care-queue.json').read_text())
        except FileNotFoundError:
            rows = []
        if not isinstance(rows, list) or any(not machine_queue.valid(row) for row in rows):
            raise ValueError('invalid machine queue')
        live = {row['token'] for row in rows if row['state'] == 'queued' and machine_queue.alive(row)}
        return {hashlib.sha256((token + ':' + event).encode()).hexdigest()
                for token, row in data.get('waits', {}).items()
                if token in live and row['state'] == 'queued' and row.get('closed_at') is None
                for event in ('coordinator', 'root')}


def machine_wait_notice_line(line):
    """Only autonomous producer lines own delivery, never copied signal markers."""
    if re.search(r'<!-- (?:host-question|handback|worker-failure):', line):
        return False
    return re.fullmatch(
        r'(?:- |inbox: )\d{4}-\d{2}-\d{2}T\S+ from machine: '
        r'(?:held step (?:exited: |resumed automatically: ).+|command completed: .+|'
        r'(?:heavy|verify) .+ held since .+: .+; clears when .+) '
        r'<!-- machine-wait:[0-9a-f]{32}:(?:coordinator|root|resumed|exited|completed) -->',
        line.rstrip('\r\n')) is not None


def machine_wait_line_retired(inst, line):
    if not machine_wait_notice_line(line):
        return False
    match = re.search(r'<!-- machine-wait:([0-9a-f]{32}):(coordinator|root) -->', line)
    if not match:
        return False
    try:
        data = json.loads((inst.root / '.brain/wake/machine-holds.json').read_text())
        row = data.get('waits', {}).get(match[1])
        return (match[1] in data.get('retired_waits', []) or
                bool(row and (row.get('state') == 'running' or row.get('closed_at') is not None)))
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def machine_wait_items(inst, nodes, state, now=None):
    now = time.time() if now is None else now
    rows = state.get('waits', [])
    own = machine_scope(inst, 'root')[0]
    targets = {n['id'] for n in nodes if not n['archived']}
    items = []
    for row in rows:
        if row.get('scope', [None])[0] != own or row.get('state') != 'queued' or 'since' not in row:
            continue
        for node in targets:
            if row['scope'][1] != machine_scope(inst, node)[1] and not (node == 'root' and now-row['since'] >= 2700):
                continue
            if now - row['since'] >= 900:
                items.append({'node': node, 'kind': 'machine-stall', 'episode': row['token'],
                              'text': machine_wait_text(row)})
    return items



def machine_boot_id():
    """Stable OS boot identity; never estimate it from uptime or wall time.

    Unknown readings preserve the existing PID/start-time checks. A known boot
    change invalidates the whole reservation before any old numeric PID probe.
    """
    try:
        if sys.platform.startswith('linux'):
            identity = Path('/proc/sys/kernel/random/boot_id').read_text().strip().lower()
            if re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', identity):
                return 'linux:' + identity
        elif sys.platform == 'darwin':
            result = subprocess.run(['sysctl', '-n', 'kern.boottime'], capture_output=True,
                                    text=True, timeout=2, env={**os.environ, 'LC_ALL': 'C'})
            match = re.search(r'\{\s*sec\s*=\s*([0-9]+),\s*usec\s*=\s*([0-9]+)\s*\}', result.stdout)
            if result.returncode == 0 and match and int(match[2]) < 1000000:
                return 'darwin:' + str(int(match[1])) + ':' + str(int(match[2]))
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


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
                and row.get("priority", "normal") in ("normal", "landing")
                and all(key not in row or queue_number(row[key]) for key in ("queued_at", "started_at"))
                and (row.get("expected_seconds") is None or queue_number(row["expected_seconds"]))
                and ("child" not in row or identity(row["child"]))
                and ("boot" not in row or (isinstance(row["boot"], str)
                     and re.fullmatch(r"(?:linux|darwin):[a-zA-Z0-9:-]+", row["boot"]) is not None)))

    @staticmethod
    def alive(row):
        if row.get("boot"):
            current_boot = machine_boot_id()
            if current_boot is not None and current_boot != row["boot"]:
                return False
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




def queue_number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def queue_order(rows, now=None):
    """Stable priority view of queued records; caller list order resolves every tie.

    Old records without timestamps retain precedence. Normal work ages ahead of
    landing work after ten minutes, so a stream of landing requests cannot starve it.
    This returns a copy and never reorders running reservations in the native store.
    """
    now = time.time() if now is None else now
    def rank(row):
        queued = row.get('queued_at')
        if queued is None or queue_number(queued) and now - queued >= 600:
            return 0
        return 1 if row.get('priority') == 'landing' else 2
    return sorted(rows, key=rank)


def queue_wait_estimate(rows, target, slots, now=None, held=False):
    """Capacity-only simulation; environmental holds and unknown/overrun durations stay unknown."""
    import heapq
    now = time.time() if now is None else now
    waiting = queue_order([r for r in rows if r['kind'] == target['kind'] and r['state'] == 'queued'], now)
    out = dict(position=waiting.index(target) + 1 if target in waiting else None,
               wait_seconds=None, completion_seconds=None,
               basis='recorded capacity and per-gate duration; environmental holds not predicted')
    if target.get('state') == 'running':
        expected, start = target.get('expected_seconds'), target.get('started_at')
        if queue_number(expected) and queue_number(start) and 0 <= now - start < expected:
            out.update(wait_seconds=0, completion_seconds=expected - (now - start))
        return out
    if held or target not in waiting or type(slots) is not int or slots < 1:
        return out
    running = [r for r in rows if r['kind'] == target['kind'] and r['state'] == 'running']
    if not running and waiting[0] == target:
        out['wait_seconds'] = 0
        if queue_number(target.get('expected_seconds')):
            out['completion_seconds'] = target['expected_seconds']
        return out
    if len(running) > slots:
        return out
    available = [0.0] * (slots - len(running))
    for row in running:
        expected, start = row.get('expected_seconds'), row.get('started_at')
        if not queue_number(expected) or not queue_number(start) or not 0 <= now - start < expected:
            return out
        available.append(expected - (now - start))
    heapq.heapify(available)
    for row in waiting:
        wait = heapq.heappop(available)
        duration = row.get('expected_seconds')
        if row == target:
            out['wait_seconds'] = wait
            if queue_number(duration):
                out['completion_seconds'] = wait + duration
            return out
        if not queue_number(duration):
            return out
        heapq.heappush(available, wait + duration)
    return out


def gate_expected_seconds(inst, node):
    """Explicit per-gate estimate, or three measured completed readiness runs on this node."""
    from . import jobs as _m_jobs
    import statistics
    if inst is None or node is None:
        return None
    n = inst.node(node)
    configured = _m_jobs.forecast_number(n['fm'].get('verify_estimate_seconds'))
    if configured is not None and configured > 0:
        return configured
    values = [_m_jobs.forecast_number(r.get('Verify s')) for r in _m_jobs.read_jobs(n) or []
              if r.get('Ended') and r.get('Outcome') == 'merged' and r.get('Verify reused') == 'false']
    values = [v for v in values if v is not None and v > 0]
    return statistics.median(values) if len(values) >= 3 else None


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
        waiting = queue_order([r for r in rows if r['kind'] == row['kind'] and r['state'] == 'queued'])
        estimate = queue_wait_estimate(rows, row, machine_limit(machine_policy(strict=False)))
        return {'state': row['state'], 'owner': row['owner'], 'owner_activity': 'unknown',
                'position': waiting.index(row) + 1 if row in waiting else None,
                'priority': row.get('priority', 'normal'), 'estimate': estimate,
                'recorded_at': path.stat().st_mtime, 'source': 'recorded native admission queue'}
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError):
        return {'state': 'unknown', 'position': None, 'owner': None, 'owner_activity': 'unknown'}


def machine_wait(kind="heavy", inst=None, node=None, job=None, clock=None, read=None, pause=None,
                 reduction_ready=False, token=None):
    clock = clock or (token.clock if token is not None else time)
    if token is not None:
        return machine_admit(token, kind, inst, node, job, clock, read, pause, reduction_ready)
    with machine_run(kind, inst, node, job, clock, close_success=False) as token:
        return machine_admit(token, kind, inst, node, job, clock, read, pause, reduction_ready)


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


def machine_top_cpu():
    """A bounded diagnostic sample; executable names only, never argv or identity proof."""
    try:
        result = subprocess.run(['ps', '-A', '-o', 'pid=', '-o', 'pcpu=', '-o', 'comm='],
                                capture_output=True, text=True, timeout=1,
                                env={**os.environ, 'LC_ALL': 'C'})
        if result.returncode:
            return []
        rows = []
        for line in result.stdout.splitlines()[:4096]:
            fields = line.split(None, 2)
            if len(fields) != 3:
                continue
            try:
                pid, percent = int(fields[0]), float(fields[1])
            except ValueError:
                continue
            if pid <= 0 or not math.isfinite(percent) or percent < 0:
                continue
            command = ''.join(c for c in Path(fields[2]).name if c.isprintable())[:120]
            if command:
                rows.append({'pid': pid, 'cpu_percent': percent, 'command': command})
        return sorted(rows, key=lambda r: (-r['cpu_percent'], r['pid']))[:3]
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def machine_state(inst=None):
    reading = machine_reading(inst)
    policy = machine_policy(strict=False)
    reading["held"] = machine_reasons(reading, policy)
    if policy.get("_error"):
        reading["held"].append(policy["_error"])
    reading["verify_limit"] = machine_limit(policy)
    if reading["held"]:
        reading["top_cpu"] = machine_top_cpu()
    # Reading the live view never creates state or a lock file.
    try:
        rows = json.loads((machine_dir() / "machine-care-queue.json").read_text())
        if not isinstance(rows, list) or any(not machine_queue.valid(r) for r in rows):
            raise ValueError("invalid queue")
        rows = [r for r in rows if machine_queue.alive(r)]
        reading["waits"] = [r for r in rows if 'since' in r]
        reading["legacy_waiting_scopes"] = [r['scope'] for r in rows
            if r['state'] == 'queued' and 'scope' in r and 'since' not in r]
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
    # Guard this store's destination. An unrelated unsafe log link must not
    # prevent recording why launch refused; safe_target still rejects escapes
    # and hard links on the record and lock themselves.
    _m_common.safe_target(path)
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
        waits = data.get('waits', {})
        if (not isinstance(waits, dict) or any(not machine_queue.valid({**row, 'state': 'running'})
                or row.get('state') not in ('admitting', 'queued', 'running', 'exited', 'completed')
                or token != row['token'] or not isinstance(row.get('node'), str)
                or not machine_timestamp(row.get('updated_at')) for token, row in waits.items())):
            raise ValueError('invalid recorded machine waits')
        retired = data.get('retired_waits', [])
        if (not isinstance(retired, list) or any(not isinstance(token, str) or
                not re.fullmatch(r'[0-9a-f]{32}', token) for token in retired)):
            raise ValueError('invalid retired machine waits')
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
    top = reading.get("top_cpu", []) if reading["held"] else []
    detail = ("; top CPU: " + ", ".join(f"{r['command']} pid={r['pid']} {r['cpu_percent']:g}%" for r in top)) if top else ""
    return ("machine: load/CPU 1/5/15m " + ("/".join(f"{v:.2f}" for v in loads) if loads else "unknown") +
            f"; free memory {reading.get('mem_free_gb', 'unknown')} GB; free disk " +
            f"{reading.get('disk_free_gb', 'unknown')} GB; thermal {reading.get('thermal', 'unknown')}; " +
            f"verifies {reading['verifying']}/{reading['verify_limit']}; queued {reading['queued']}; " +
            ("held: " + "; ".join(reading["held"]) if reading["held"] else "heavy work allowed") + detail)


def cmd_heavy(a):
    inst = _m_nodes.load()
    node = inst.node(getattr(a, "node", None) or "root")
    with machine_run('heavy', inst, node['id']) as token:
        machine_wait(inst=inst, node=node['id'], token=token)
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
        from . import watch as _m_watch
        try:
            _m_watch.watch_once(inst)  # same minute timer; sampler has released its instance lock
        except (OSError, ValueError):
            print('watch: watcher state invalid; retained for repair (brain due root)')
        # The release extension owns opt-in policy and attempt fencing. Older
        # installations without it keep the same native sampler behavior.
        from importlib import import_module
        try:
            _m_release = import_module(__package__ + '.release')
        except ModuleNotFoundError as error:
            if error.name != __package__ + '.release':
                raise
        else:
            _m_release.tick_releases(inst)
        return
    if a.action in ("install", "uninstall"):
        machine_service(inst, a.action, a.yes)
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


def machine_heat_reason(reason):
    return isinstance(reason, str) and (reason in ("sustained 5-minute load", "sustained thermal pressure")
                                       or reason.startswith("CPU idle"))



def machine_thermal_recovered(samples):
    end, previous = samples[-1]['at'], samples[-1]['at']
    for sample in reversed(samples):
        at, reading = sample['at'], sample['reading']
        if not 0 <= previous - at <= 90 or reading.get('thermal') != 'normal':
            return False
        if end - at >= 120:
            return True
        previous = at
    return False



def machine_admit(token, kind, inst, node, job, clock, read, pause, reduction_ready):
    """A live queued process owns its continuation; no replay of arbitrary commands."""
    clock = clock or time
    token.clock = clock
    read = read or (lambda: machine_reading(inst))
    pause = pause or clock.sleep
    owner = [os.getpid(), process_stamp(os.getpid())]
    if owner[1] in (None, "unknown"):
        _m_common.die("machine care: cannot establish this process's start time; no heavy work started")
    boot = machine_boot_id()
    previous, last_report, started = None, 0, clock.monotonic()
    snapshot = None
    expected = gate_expected_seconds(inst, node) if kind == 'verify' else None
    priority = (inst.node(node)['fm'].get('verify_priority', 'normal')
                if kind == 'verify' and inst is not None and node is not None else 'normal')
    if priority not in ('normal', 'landing'):
        _m_common.die('verify_priority must be normal or landing; no work started')
    while True:
        policy = machine_policy(strict=False)
        reading = read()
        reasons = list(machine_reasons(reading, policy))
        if policy.get("_error"):
            reasons.append(policy["_error"])
        elapsed = max(0, clock.monotonic() - started)
        reduced = bool(reasons) and elapsed >= policy['max_hold_minutes'] * 60
        hard = [r for r in reasons if not (r.startswith('CPU idle') or '5-minute load' in r)]
        if reduced and hard:
            _m_common.die('machine care: maximum hold reached; unsafe resource pressure; no command started')
        if reduced and not policy.get('_error') and not reduction_ready:
            _m_common.die('machine care: maximum hold reached; cannot enforce reduced parallelism '
                          'for this verify command; no command started. Use a literal make or pytest command.')
        # Degrade resource admission, never bypass a broken policy or FIFO ownership.
        resource_hold = bool(reasons) and (not reduced or bool(policy.get('_error')))
        with machine_queue() as queue:
            row = next((r for r in queue.rows if r["token"] == token), None)
            if row is None:
                row = {"token": token, "owner": owner, "kind": kind, "state": "queued",
                       "priority": priority, "expected_seconds": expected, "queued_at": clock.time()}
                if boot is not None:
                    row["boot"] = boot
                if inst is not None and node is not None:
                    row["scope"] = machine_scope(inst, node)
                queue.rows.append(row)
            waiting = queue_order([r for r in queue.rows if r["kind"] == kind and r["state"] == "queued"], clock.time())
            running = sum(r["kind"] == "verify" and r["state"] == "running" for r in queue.rows)
            position = waiting.index(row) + 1
            limit = 1 if reduced else machine_limit(policy)
            admitted = not resource_hold and (kind != "verify" or (position == 1 and running < limit))
            detail = ("; ".join(reasons) if resource_hold else
                      f"queued #{position}, {running}/{limit} verify slots busy")
            if admitted:
                row['state'] = 'running'
                row['started_at'] = clock.time()
                row['reduced'] = reduced
                if 'since' in row:
                    row['resumed_at'] = clock.time()
            else:
                row.setdefault('since', clock.time())
                row.update(reason=detail, reading=reading, position=position,
                           clears_when='resource thresholds clear once, or maximum hold expires; FIFO verify slot available')
            if job is not None:
                row["job"] = hashlib.sha256(job.encode()).hexdigest()
            estimate = queue_wait_estimate(queue.rows, row, limit, now=clock.time(), held=resource_hold)
            snapshot = dict(row)
        # No machine lock is held across instance state publication or transport enqueue.
        if inst is not None and node is not None:
            machine_record_wait(inst, node, job, snapshot, clock.time())
        if admitted:
            timing = ADMISSION_TIMING.get()
            if timing is not None:
                timing.append(max(0, clock.monotonic() - started))
            if 'since' in snapshot:
                print(f"machine care: {kind} resumed after {elapsed:.0f}s" +
                      ('; reduced parallelism (1)' if reduced else ''), flush=True)
            return token
        status = (re.sub(r"\d+(?:\.\d+)?", "#", detail), position, running, limit)
        now = clock.monotonic()
        if status != previous or now - last_report >= 60:
            wait = estimate['wait_seconds']
            wait_text = f'approx. capacity wait {wait:.0f}s' if wait is not None else 'wait estimate unknown'
            print(f"machine care: {kind} held {elapsed:.0f}s; {detail}; {wait_text}; retry in {MACHINE_RETRY}s", flush=True)
            previous, last_report = status, now
        pause(MACHINE_RETRY)



def machine_environment(token, env):
    """Propagate degraded admission to the command, without rewriting its shell text."""
    with machine_queue() as queue:
        reduced = any(r['token'] == token and r.get('reduced') for r in queue.rows)
    if not reduced:
        return env
    return {**env, 'BRAIN_REDUCED_PARALLELISM': '1', 'BRAIN_PARALLELISM': '1',
            'CMAKE_BUILD_PARALLEL_LEVEL': '1', 'MAKEFLAGS': '-j1', 'OMP_NUM_THREADS': '1',
            'OPENBLAS_NUM_THREADS': '1', 'MKL_NUM_THREADS': '1', 'CARGO_BUILD_JOBS': '1'}
