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
from . import instance as _m_instance
from . import timings as _m_timings

_m_gate_lock = _m_common.LazyModule(__package__, 'gate_lock')
_m_history = _m_common.LazyModule(__package__, 'history')
_m_nodes = _m_common.LazyModule(__package__, 'nodes')
_m_schedules = _m_common.LazyModule(__package__, 'schedules')
_m_ui = _m_common.LazyModule(__package__, 'ui')


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
            # Machine samples, hold inventory and wake state are always private.
            os.fchmod(stream.fileno(), 0o600)
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
    return machine_sample(inst)


@contextlib.contextmanager
def machine_sample_lock(inst):
    # A busy publication is skipped, with live fallback. Collection and planning
    # have already completed; never wait for another instance writer here.
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
                with _m_timings.phase('lock_wait'):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("machine sample: skipped; instance busy")
                yield False
                return
            held = os.fstat(fd)
            current = os.stat('lock', dir_fd=dfd, follow_symlinks=False)
            if (not stat.S_ISREG(held.st_mode) or held.st_nlink != 1
                    or (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino)):
                _m_common.die('machine sampler: instance lock changed')
            os.fchmod(fd, 0o600)
            stream = os.fdopen(fd, "r+")
            fd = None
            stream.seek(0)
            stream.truncate()
            stream.write(str(os.getpid()))
            stream.flush()
            _m_instance._LOCK["fh"], _m_instance._LOCK["depth"] = stream, 1
            _m_instance._LOCK['hold_started'] = time.perf_counter()
            os.environ["BRAIN_LOCK_HELD"] = f"{inst.root.resolve()}|{os.getpid()}"
            try:
                yield True
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


def machine_notice_data(data):
    pending = data.get("pending_notice")
    if not pending:
        return data
    rows = machine_transitions(data)
    state = 'open' if data['episode'] else 'close'
    if not rows or (rows[-1]['episode'], rows[-1]['state']) != (data['episode_id'], state):
        at = data.get('pending_at')
        rows.append({'at': at if machine_timestamp(at) else time.time(),
                     'episode': data['episode_id'], 'state': state})
    # Machine care acts autonomously. Transitions never append to an inbox or wake a model.
    return {**data, 'transitions': rows[-100:], 'pending_notice': None, 'pending_at': None}


def machine_notice(inst, data, checked=False):
    if not data.get('pending_notice'):
        return data
    if not checked:
        machine_commit_check(inst)
    data = machine_notice_data(data)
    machine_publish(inst, inst.root / ".brain/machine.json", json.dumps(data) + "\n")
    return data


def machine_sample_plan(inst):
    path = inst.root / ".brain/machine.json"
    try:
        before = path.read_bytes()
    except FileNotFoundError:
        before = None
    try:
        data = json.loads(before) if before is not None else {}
    except (ValueError, UnicodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    notice_required = False
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
        notice_required = True
        data = machine_notice_data(data)  # retain the old transition before superseding it
    reading = _m_ui.machine_load()
    now = time.time()
    old = data.get("samples", [])
    old = old if isinstance(old, list) else []
    samples = sorted((s for s in old if machine_valid_sample(s) and now - 86400 <= s["at"] < now),
                     key=lambda s: s["at"])
    if samples and int(samples[-1]["at"] // 60) == int(now // 60):
        samples.pop()  # at most one reading per minute, even with repeated invocations
    samples.append({"at": now, "reading": reading})
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
    return before, result, policy, notice_required


def machine_sample(inst):
    if _m_instance._LOCK['fh'] is not None:
        print('machine sample: skipped; instance busy')
        return
    with _m_timings.phase('sample_compute'):
        before, result, policy, recovering_notice = machine_sample_plan(inst)
        refusal = None
        if result['pending_notice'] or recovering_notice:
            try:
                machine_commit_check(inst)
            except (OSError, SystemExit) as error:
                refusal = error
    path = inst.root / '.brain/machine.json'
    with machine_sample_lock(inst) as acquired:
        if not acquired:
            return
        try:
            current = path.read_bytes()
        except FileNotFoundError:
            current = None
        if current != before or machine_policy() != policy:
            print('machine sample: skipped; state changed during collection')
            return
        if refusal is not None:
            # Keep a newly observed transition pending even when its safety
            # check refuses acknowledgement. An older pending notice retains
            # its original bytes until that check succeeds on a later tick.
            if not recovering_notice:
                machine_publish(inst, path, json.dumps(result) + '\n')
            raise refusal
        machine_publish(inst, path, json.dumps(result) + '\n')
        if result['pending_notice']:
            machine_notice(inst, result, checked=True)
    print(f"machine sample: {len(result['samples'])} readings; " +
          ('; '.join(result['heat_reasons']) if result['episode'] else 'no sustained heat'))


def machine_cron_quote(value):
    return "'%'".join(shlex.quote(part) for part in value.split("%"))


def machine_service(inst, action, yes=False):
    """Dry by default. No network, privilege escalation, shell setup or installs."""
    import plistlib
    if yes and os.environ.get("BRAIN_MACHINE_TESTING") == "1":
        _m_common.die("machine sampler: fixture mode cannot change OS services")
    label = "org.brain.machine." + hashlib.sha256(str(inst.root).encode()).hexdigest()[:16]
    args = [sys.executable, str(_m_common.STABLE_ENTRY_POINT), "machine", "sample"]
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
            _m_common.atomic_write(path, payload, private=True)
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


def machine_job_owner(inst, node, job=None, cwd=None):
    """Capture a ledger attempt, never treat a command label as job ownership."""
    from . import jobs
    try:
        n = inst.node(node)
        if cwd is not None or job is None:
            path = Path(cwd or os.getcwd()).resolve()
            matches = [branch for branch, w in n['local'].get('worktrees', {}).items()
                       if w.get('path') and (Path(w['path']).resolve() == path or
                                             Path(w['path']).resolve() in path.parents)]
            if len(matches) != 1:
                return {}
            job = matches[0]
        row = jobs.open_job(jobs.read_jobs(n) or [], job)
        key = jobs.job_key(row) if row else None
        return {'owner_job': key, 'owner_worker': n['local'].get('workers', {}).get(key)}
    except (Exception, SystemExit):
        return {}  # unknown ownership must retain the ordinary wake


def machine_job_reader_live(inst, row):
    """Only fresh bound worker evidence can replace a terminal coordinator wake."""
    from . import jobs, spend
    if not row.get('owner_job'):
        return False
    try:
        fresh = _m_instance.Instance(inst.root)
        n = fresh.node(row['node'])
        if (not row.get('owner_worker') or
                n['local'].get('workers', {}).get(row['owner_job']) != row['owner_worker']):
            return False
        job = next((r for r in jobs.read_jobs(n) or [] if jobs.job_key(r) == row['owner_job']), None)
        return bool(job and not job['Ended'] and not jobs.job_signal(fresh, n, job, strict=True)
                    and (n['id'], row['owner_job']) in spend.live_threads(fresh))
    except (Exception, SystemExit):
        return False  # unavailable liveness is a reason to wake, never to suppress


@contextlib.contextmanager
def machine_run(kind, inst, node, job=None, clock=None, close_success=True, release=True, cwd=None):
    """Own admission through cleanup; all unsuccessful exits share one finalizer."""
    clock = clock or time
    token = MachineRun(clock)
    row = dict(token=str(token), owner=[os.getpid(), 'unknown'], kind=kind, state='admitting',
               since=clock.time(), reason='checking machine admission', reading={},
               clears_when='admission checks complete', managed=True)
    if inst is not None and node is not None:
        row['scope'] = machine_scope(inst, node)
        row.update(machine_job_owner(inst, node, job, cwd))
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
            machine_cleanup(token, [] if getattr(token, 'retain', False) else [lambda: machine_release(token)], primary=error)
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
                         ((v.get('outcome') == 'success' and v.get('notified_completed')) or
                          (v.get('notified_exited')
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
                # Completion is returned on the owning command's stdout. Retain
                # the private outcome, including old rows, without a node wake.
                row['notified_completed'] = True
                continue
            elif row.get('closed_at') is not None:
                continue
            elif row['state'] == 'queued':
                if elapsed >= 900:
                    events.append(('coordinator', row['node']))
                if elapsed >= 2700:
                    events.append(('root', 'root'))
            elif row.get('resumed_at') is not None:
                # machine_admit reports resumption to its waiting caller.
                row['notified_resumed'] = True
                continue
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
                if event in ('exited', 'completed') and machine_job_reader_live(inst, row):
                    # Delivery to the live builder's inbox is the final receipt.
                    # Its later completion must not turn this into a delayed wake.
                    row['notified_' + event] = True
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
                and ("session" not in row or (type(row['session']) is int and row['session'] > 0))
                and ("uncertain" not in row or type(row['uncertain']) is bool)
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
        if row.get('session'):
            try:
                return MachineProcesses().members(row['session'], {}) != {}
            except (OSError, ValueError):
                return True
        return bool(row.get('uncertain'))  # no session authority to re-prove

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
                 reduction_ready=False, token=None, gate_phase='running', reporter=print, cancelled=None):
    clock = clock or (token.clock if token is not None else time)
    with (_m_gate_lock.gate_admission(inst, node, gate_phase) if inst is not None and node is not None
          else contextlib.nullcontext()):
        if token is not None:
            return machine_admit(token, kind, inst, node, job, clock, read, pause, reduction_ready, reporter, cancelled)
        with machine_run(kind, inst, node, job, clock, close_success=False) as token:
            return machine_admit(token, kind, inst, node, job, clock, read, pause, reduction_ready, reporter, cancelled)


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


def machine_hold_read(path):
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
    return data


@contextlib.contextmanager
def machine_hold_store(inst):
    """Short private transaction shared by due ticks and watcher delivery receipts."""
    import fcntl
    path = inst.root / '.brain/wake/machine-holds.json'
    from . import ui_readmodel
    view = ui_readmodel.projection(inst.root)
    if view is not None:
        _m_common.safe_target(path)
        yield view.store(path, lambda: machine_hold_read(path))
        return
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
        data = machine_hold_read(path)
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
    from . import ui_readmodel
    with machine_hold_store(inst) as data:
        previous = data['holds']
        data['holds'] = {scope: previous[scope] if scope in previous and previous[scope]['reason'] == reason
                         else {'reason': reason, 'episode': ui_readmodel.episode(inst, [scope, reason, previous.get(scope)], 32), 'reported': False}
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


class MachineProcesses:
    """Kernel identities for private foreground sessions; never signal a bare PID.

    Darwin's audit-token signal validates pidversion in the kernel. Linux pidfds
    bind signal delivery to an opened process. Neither uses ps or a check/kill gap.
    An incomplete scan cannot certify an empty session.
    """
    def __init__(self):
        import errno
        self.errno = errno
        self.pidfds, self.references, self.dead = {}, {}, {}
        self.next_reference = 0
        self.owned_births = set()
        if sys.platform == 'darwin':
            import ctypes
            self.ctypes = ctypes
            self.lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
            self.lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                              ctypes.c_void_p, ctypes.c_int]
            self.lib.proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32,
                                               ctypes.c_void_p, ctypes.c_int]
            self.lib.proc_signal_with_audittoken.argtypes = [ctypes.c_void_p, ctypes.c_int]
        elif not (sys.platform.startswith('linux') and hasattr(os, 'pidfd_open')
                  and hasattr(signal, 'pidfd_send_signal')):
            raise OSError('heavy run requires Darwin audit-token signals or Linux pidfds')

    def identity(self, pid, zombies=False, parent=False):
        if sys.platform == 'darwin':
            # XNU proc_info_private.h: PROC_PIDUNIQIDENTIFIERINFO=17,
            # struct proc_uniqidentifierinfo (56 bytes), uniqueid at 16,
            # pidversion at 32. Fail closed if the kernel ABI is unavailable.
            import struct
            data = self.ctypes.create_string_buffer(56)
            count = self.lib.proc_pidinfo(pid, 17, int(zombies), data, len(data))
            if count != len(data):
                error = self.ctypes.get_errno()
                if error == self.errno.ESRCH:
                    return None
                raise OSError(error, 'heavy run: process identity unavailable')
            birth = (struct.unpack_from('Q', data.raw, 16)[0], struct.unpack_from('I', data.raw, 32)[0])
            return birth + (struct.unpack_from('Q', data.raw, 24)[0],) if parent else birth
        if zombies:
            try:
                fields = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
                return (int(fields[19]),) if fields[0] == 'Z' else None
            except (FileNotFoundError, ProcessLookupError):
                return None
        import select
        previous = self.pidfds.get(pid)
        if previous is not None:
            if select.select([previous[1]], [], [], 0)[0]:
                del self.pidfds[pid]
                self.references.pop(previous[0])
                os.close(previous[1])
                return None
            return previous
        try:
            fd = os.pidfd_open(pid)
        except ProcessLookupError:
            return None
        # A serial is never reused, even when a dead descriptor is closed and
        # the OS recycles its fd number. Stale serials cannot signal a new owner.
        self.next_reference += 1
        identity = (self.next_reference, fd)
        if select.select([fd], [], [], 0)[0]:
            os.close(fd)
            return None
        self.references[identity[0]] = fd
        self.pidfds[pid] = identity
        return identity

    def close(self):
        for fd in self.references.values():
            os.close(fd)
        self.references.clear()
        self.pidfds.clear()

    def __del__(self):
        self.close()

    def pids(self):
        if sys.platform == 'darwin':
            count = self.lib.proc_listpids(1, 0, None, 0)
            if count <= 0:
                raise OSError('heavy run: process inventory unavailable')
            # Extra room handles ordinary forks; a full buffer is never complete.
            data = (self.ctypes.c_int * (count // 4 + 256))()
            size = self.lib.proc_listpids(1, 0, data, self.ctypes.sizeof(data))
            if size <= 0 or size >= self.ctypes.sizeof(data) or size % 4:
                raise OSError('heavy run: incomplete process inventory')
            return {pid for pid in data[:size // 4] if pid > 0}
        return {int(path.name) for path in Path('/proc').iterdir() if path.name.isdigit()}

    def linux_observation(self, pid):
        try:
            fields = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
            return int(fields[19]), fields[0] == 'Z', int(fields[3])
        except (FileNotFoundError, ProcessLookupError):
            return None

    def capture_linux(self, session, exclude):
        owned, dead, changed = {}, {}, False
        for pid in self.pids() - set(exclude):
            row = self.linux_observation(pid)
            if row is None or row[2] != session:
                continue  # gone or foreign, including zombies with a known SID
            if row[1]:
                dead[pid] = row[0]
                changed |= self.dead.get(pid) != row[0]
            else:
                reference = self.identity(pid)
                after = self.linux_observation(pid)
                if reference is None or after is None or after[1] or after[2] != session or self.identity(pid) != reference:
                    changed = True
                else:
                    owned[pid] = reference
        self.dead = dead
        return owned, changed

    def capture(self, session, exclude):
        if sys.platform.startswith('linux'):
            return self.capture_linux(session, exclude)
        owned, dead, ancestry, racing = {}, {}, {}, set()
        leader = self.identity(session, zombies=True)
        if leader is not None:
            self.owned_births.add(leader[0])
        for pid in self.pids() - set(exclude):
            info = self.identity(pid, zombies=True, parent=True)
            if info is None:
                continue  # already reaped; the second inventory finds survivors
            before = info[:2]
            ancestry[before[0]] = info[2]
            current = self.identity(pid)
            if current is None:
                dead[pid] = before
                continue
            try:
                sid = os.getsid(pid)
            except ProcessLookupError:
                racing.add(before[0])
                continue
            after = self.identity(pid)
            if before != current or current != after:
                racing.add(before[0])
            elif sid == session:
                owned[pid] = after
                self.owned_births.add(after[0])
        # Darwin getsid cannot classify zombies. Kernel parent unique IDs bind
        # their ancestry without stale numeric parent links. Only an owned fork
        # can invalidate emptiness; foreign exits never require host quiescence.
        pending = dict(ancestry)
        while pending:
            found = {birth for birth, parent in pending.items() if parent in self.owned_births}
            if not found:
                break
            self.owned_births.update(found)
            for birth in found:
                del pending[birth]
        dead = {pid: birth for pid, birth in dead.items() if birth[0] in self.owned_births}
        changed = bool(racing & self.owned_births) or any(self.dead.get(pid) != birth for pid, birth in dead.items())
        self.dead = dead
        return owned, changed

    def members(self, session, known, exclude=()):
        if sys.platform == 'darwin':
            self.owned_births.update(birth[0] for birth in known.values())
        owned, changed = self.capture(session, exclude)
        # Already observed identities remain ours if they later leave the session.
        for pid, birth in known.items():
            current = self.identity(pid)
            if current is not None and current[0] == birth[0]:
                owned[pid] = current
        if not owned:
            # Retry an owned fork/exit seen during enumeration. Ordinary foreign
            # churn is not an observer failure and never delays session cleanup.
            again, moved = self.capture(session, exclude)
            if moved:
                return None
            owned.update(again)
        known.clear()
        known.update(owned)
        return owned

    def send(self, pid, identity, number):
        if sys.platform == 'darwin':
            token = (self.ctypes.c_uint32 * 8)()
            token[5], token[7] = pid, identity[1]
            error = self.lib.proc_signal_with_audittoken(token, number)
            if error and error != self.errno.ESRCH:
                raise OSError(error, 'heavy run: owned process signal rejected')
        else:
            fd = self.references.get(identity[0])
            if fd is not None and identity == (identity[0], fd):
                try:
                    signal.pidfd_send_signal(fd, number)
                except ProcessLookupError:
                    pass


def machine_heavy_publish(token, pid):
    machine_child(token, pid)
    with machine_queue() as queue:
        row = next(r for r in queue.rows if r['token'] == token)
        row['session'] = pid


def machine_heavy_uncertain(token, session=None):
    # A bounded observer failure is an explicit retained reservation, never an
    # endless supervisor or evidence that remaining work has stopped. Once both
    # helpers exit, admission re-proves session cleanup (or a known reboot).
    with machine_queue() as queue:
        for row in queue.rows:
            if row['token'] == token:
                row['uncertain'] = True
                if session is not None:
                    row.setdefault('session', session)


class MachineEscalation:
    def __init__(self, clock=time.monotonic, grace=1):
        self.clock, self.grace, self.deadline = clock, grace, None
        self.notified = set()

    def apply(self, observer, members, number):
        if self.deadline is None:
            self.deadline = self.clock() + self.grace
        forced = self.clock() >= self.deadline
        for pid, identity in list(members.items()):
            if forced or (pid, identity) not in self.notified:
                observer.send(pid, identity, signal.SIGKILL if forced else number)
                self.notified.add((pid, identity))


def machine_heavy_monitor(observer, session, done, cancellation, pause, known=None, exclude=()):
    """Both owners use the same cleanup proof, including after peer death."""
    known = {} if known is None else known
    escalation = MachineEscalation()
    failed_at = None
    while True:
        failed, members = False, None
        try:
            members = observer.members(session, known, exclude)
            if members is None:
                failed = True
            elif done() and not members:
                return True
        except (OSError, ValueError):
            failed = True
        number = cancellation()
        if number:
            try:
                escalation.apply(observer, known if members is None else members, number)
            except OSError:
                failed = True
        if failed:
            failed_at = time.monotonic() if failed_at is None else failed_at
            if time.monotonic() - failed_at >= 2:
                # A single observer error never cancels work or arms a future
                # cancellation's grace period. Only persistent failure stops
                # known identities; unknown work retains its reservation.
                if not number:
                    try:
                        escalation.apply(observer, known, signal.SIGTERM)
                    except OSError:
                        pass
                return False
        else:
            failed_at = None
        pause()


def machine_supervise(control, token, command, inherited=()):
    """A published session leader cleans up on wrapper EOF; the wrapper is backup."""
    import select
    stopped = [0]
    def stop(number, frame):
        stopped[0] = stopped[0] or number
    for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(number, stop)
    proc = None
    try:
        observer = MachineProcesses()
        if os.read(control, 1) != b'1':
            machine_release(token)
            return 125
        try:
            proc = subprocess.Popen(command, pass_fds=inherited)
        except OSError:
            machine_release(token)
            return 127
        def pause():
            nonlocal control
            if control is not None:
                if select.select([control], [], [], 0.02)[0]:
                    data = os.read(control, 1)
                    stopped[0] = stopped[0] or (data[0] if data else signal.SIGTERM)
                    if not data:
                        os.close(control)
                        control = None
            else:
                threading.Event().wait(0.02)
        clean = machine_heavy_monitor(observer, os.getpid(), lambda: proc.poll() is not None,
                                       lambda: stopped[0], pause, exclude=(os.getpid(),))
        if not clean:
            machine_heavy_uncertain(token, os.getpid())
            return 125
        machine_release(token)
        code = proc.wait()
        return 128 + stopped[0] if stopped[0] else (code if code >= 0 else 128 - code)
    finally:
        if control is not None:
            os.close(control)


def machine_execute(command, inst=None, node=None, label=None, cwd=None, env=None, stdout=None, stderr=None):
    """Run argv once in a verify slot without adding bytes to child streams."""
    if not command:
        _m_common.die('heavy run: provide a command after --')
    observer = MachineProcesses()  # refuse unsupported ownership before admission/spawn
    child_env = os.environ if env is None else env
    gate_fd = (_m_gate_lock.gate_inherited_fd(inst.root, child_env, node)
               if inst is not None and node is not None else None)
    from . import handback_queue as _m_queue
    inherited = tuple(dict.fromkeys(fd for fd in (gate_fd, _m_queue.queue_descendant_fd()) if fd is not None))
    stopped = [0]
    def stop(number, frame):
        stopped[0] = stopped[0] or number
    try:
        with machine_run('verify', inst, node, label, release=False, cwd=cwd or os.getcwd()) as token:
            previous = {number: signal.signal(number, stop)
                        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
            proc, control_read, control_write = None, None, None
            try:
                machine_wait('verify', inst, node, job=label, token=token, reporter=None,
                             cancelled=lambda: stopped[0])
                if stopped[0]:
                    raise MachineSignal(stopped[0])
                control_read, control_write = os.pipe()
                bootstrap = ("import sys; sys.dont_write_bytecode=True; sys.path.insert(0,sys.argv.pop(1)); "
                             "from brain.machine import machine_supervise; "
                             "sys.exit(machine_supervise(int(sys.argv[1]),sys.argv[2],sys.argv[4:],"
                             "tuple(map(int,sys.argv[3].split(','))) if sys.argv[3] else ()))")
                proc = subprocess.Popen([sys.executable, '-I', '-S', '-c', bootstrap,
                                         str(Path(__file__).resolve().parent.parent), str(control_read), str(token),
                                         ','.join(map(str, inherited)), *command],
                                        cwd=cwd, env=env, stdout=stdout, stderr=stderr,
                                        pass_fds=(control_read,) + inherited, start_new_session=True)
                token.retain = True
                os.close(control_read)
                control_read = None
                machine_heavy_publish(token, proc.pid)
                if not stopped[0]:
                    os.write(control_write, b'1')
                else:
                    os.close(control_write)
                    control_write = None
                while observer.identity(proc.pid) is not None:
                    if stopped[0] and control_write is not None:
                        try:
                            os.write(control_write, bytes([stopped[0]]))
                        except BrokenPipeError:
                            pass
                        os.close(control_write)
                        control_write = None
                    threading.Event().wait(0.02)
                # Do not reap the leader before cleanup: its unreaped PID anchors
                # session identity against reuse while the backup inspects it.
                clean = machine_heavy_monitor(observer, proc.pid, lambda: True,
                        lambda: signal.SIGTERM, lambda: threading.Event().wait(0.02), exclude=(proc.pid,))
                code = proc.wait()
                if not clean:
                    machine_heavy_uncertain(token, proc.pid)
                    token.result = 125
                else:
                    token.retain = False
                    machine_release(token)
                    token.result = code if code >= 0 else 128 - code
                if stopped[0]:
                    raise MachineSignal(stopped[0])
                return token.result
            except OSError:
                if proc is None:
                    raise
                token.result = 125
                return token.result
            finally:
                for fd in (control_read, control_write):
                    if fd is not None:
                        os.close(fd)
                if proc is None:
                    machine_release(token)
                elif proc.returncode is None:
                    # Publication or wrapper error: EOF asks the supervisor to
                    # stop. Also cover its crash through our own session proof.
                    clean = machine_heavy_monitor(observer, proc.pid,
                            lambda: observer.identity(proc.pid) is None,
                            lambda: signal.SIGTERM, lambda: threading.Event().wait(0.02), exclude=(proc.pid,))
                    if clean:
                        proc.wait()
                        token.retain = False
                        machine_release(token)
                    else:
                        machine_heavy_uncertain(token, proc.pid)
                for number, handler in previous.items():
                    signal.signal(number, handler)
    except MachineSignal as error:
        return 128 + error.number


def heavy_node_for_cwd(inst, cwd):
    """The node whose external home (or its `<home>-work` worktree folder) holds `cwd`."""
    cwd = Path(cwd).resolve()
    best, depth = None, -1
    for n in inst.nodes():
        home = n.get('home_path')
        if n['id'] == 'root' or not home:
            continue
        home = Path(home)
        for place in (home, home.parent / (home.name + '-work')):
            place = place.resolve()
            if (place == cwd or place in cwd.parents) and len(place.parts) > depth:
                best, depth = n['id'], len(place.parts)
    return best


def cmd_heavy_run(a):
    command = a.command[1:] if a.command[:1] == ['--'] else a.command
    inst = _m_nodes.load()
    # A builder's verify run belongs to its own node, including its inbox report.
    node = inst.node(a.node or os.environ.get('BRAIN_GATE_NODE')
                     or heavy_node_for_cwd(inst, os.getcwd()) or 'root')
    try:
        code = machine_execute(command, inst, node['id'], a.label)
    except OSError as error:
        _m_common.die('heavy run: ' + str(error))
    sys.exit(code)


def cmd_heavy(a):
    inst = _m_nodes.load()
    node = inst.node(getattr(a, "node", None) or os.environ.get('BRAIN_GATE_NODE') or "root")
    with machine_run('heavy', inst, node['id']) as token:
        machine_wait(inst=inst, node=node['id'], token=token)
    print("machine care: heavy work allowed (admission only)")


# ---------- machine subscriptions ----------
def cmd_machine(a):
    inst = _m_nodes.load()
    if a.action == 'queue-service':
        from . import write_queue_service
        action = getattr(a, 'id', None) or 'status'
        if action in ('install', 'uninstall'):
            write_queue_service.service_control(inst, action, a.yes)
        elif action == 'status':
            print(json.dumps(write_queue_service.queue_health(inst), sort_keys=True))
        elif action == 'run':
            write_queue_service.run_service(inst)
        else:
            _m_common.die('machine queue-service requires install, uninstall, status or run')
        return
    if a.action == 'wake-setup':
        if not getattr(a, 'id', None):
            _m_common.die('machine wake-setup requires a target node')
        from . import hosts as _m_hosts
        print(json.dumps(_m_hosts.wake_setup_plan(inst, inst.node(a.id)), indent=2))
        return
    if a.action == "sample":
        machine_sample_once(inst)
        from . import host_observations as _m_host_observations
        _m_host_observations.host_export_if_enabled(inst)
        from . import host_tasks as _m_host_tasks
        _m_host_tasks.host_task_refresh_if_enabled(inst)
        from . import dispatcher as _m_dispatcher
        _m_dispatcher.dispatch_if_enabled(inst)

        from . import digest as _m_digest
        try:
            _m_digest.daily_retro(inst)
        except (OSError, ValueError, TypeError):
            print('daily retro unavailable; retained job records are unchanged')
        from . import watch as _m_watch
        try:
            _m_watch.watch_once(inst, episode_blocking=False)  # busy episodes retry on the next minute tick
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
        if a.yes:
            # Keep service mutations serialized without holding ordinary state
            # while either the reservation or the service manager is waiting.
            with _m_instance.released(inst):
                with _m_instance.reservation(inst, 'machine-service'):
                    machine_service(inst, a.action, a.yes)
        else:
            machine_service(inst, a.action, a.yes)
        return
    nodes = inst.nodes()
    if a.missing_homes:
        for n in nodes:
            if n["external"] and not n["archived"] and not (n["home_path"] and n["home_path"].exists()):
                print(f"{n['id']}\t{n['fm'].get('home_url', 'none')}")
        return
    print(machine_summary(machine_state(inst)))
    from . import write_queue_service
    print(write_queue_service.queue_summary(inst))
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



def machine_admit(token, kind, inst, node, job, clock, read, pause, reduction_ready, reporter=print, cancelled=None):
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
        if cancelled and cancelled():
            raise MachineSignal(cancelled())
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
            if 'since' in snapshot and reporter is not None:
                reporter(f"machine care: {kind} resumed after {elapsed:.0f}s" +
                      ('; reduced parallelism (1)' if reduced else ''), flush=True)
            return token
        status = (re.sub(r"\d+(?:\.\d+)?", "#", detail), position, running, limit)
        now = clock.monotonic()
        if status != previous or now - last_report >= 60:
            wait = estimate['wait_seconds']
            wait_text = f'approx. capacity wait {wait:.0f}s' if wait is not None else 'wait estimate unknown'
            if reporter is not None:
                reporter(f"machine care: {kind} held {elapsed:.0f}s; {detail}; {wait_text}; retry in {MACHINE_RETRY}s", flush=True)
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
