"""Usage mechanics for Brain."""
import datetime
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import unicodedata
from pathlib import Path

from . import common as _m_common
from . import guard as _m_guard
from . import instance as _m_instance
from . import jobs as _m_jobs
from . import nodes as _m_nodes


# Account-wide server readings, shared by every brain here. No tokens or transcripts enter the cache.

USAGE_WINDOWS = {"five_hour": 300, "seven_day": 10080}
USAGE_MAX_AGE = 120
_KEYCHAIN_RETRY_AT = {}
_KEYCHAIN_CYCLE_BLOCKED = set()


def usage_file(name="usage.json"):
    return _m_instance.brains_file().parent / name


def usage_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def usage_number(value):
    import math
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def usage_cache():
    data = usage_json(usage_file())
    if not isinstance(data, dict) or not isinstance(data.get("subscriptions"), list) or \
            not isinstance(data.get("accounts"), dict):
        return {}
    if any(not isinstance(s, dict) or not isinstance(s.get("account_key"), str) for s in data["subscriptions"]):
        return {}
    for r in data["accounts"].values():
        if not isinstance(r, dict) or r.get("source") not in ("lookup", "log", "unknown") or \
                not isinstance(r.get("windows"), dict):
            return {}
        if r.get("read_at") is not None and (not usage_number(r["read_at"]) or usage_stamp(r["read_at"]) is None):
            return {}
        for name, w in r["windows"].items():
            if (name not in USAGE_WINDOWS and not name.startswith("seven_day_model:")) or not isinstance(w, dict):
                return {}
            absent = w.get("absent") is True
            if absent and (name != "five_hour" or w.get("source", r["source"]) != "lookup" or "used_percent" in w):
                return {}
            if (not absent and (not usage_number(w.get("used_percent")) or not 0 <= w["used_percent"] <= 100)) or \
                    (w.get("resets_at") is not None and (not usage_number(w["resets_at"]) or
                                                        usage_stamp(w["resets_at"]) is None)):
                return {}
            if w.get("read_at") is not None and usage_stamp(w["read_at"]) is None:
                return {}
            if "source" in w and w["source"] not in ("lookup", "log", "unknown"):
                return {}
            if name.startswith("seven_day_model:") and (not isinstance(w.get("models"), list) or
                    not w["models"] or any(not isinstance(m, str) or not m for m in w["models"])):
                return {}
    return data


def usage_config():
    path = usage_file("subscriptions.json")
    data = usage_json(path) if path.exists() else {"subscriptions": [
        {"id": rt, "runtime": rt, "config_dir": os.environ.get(env) or str(Path.home() / ("." + rt)),
         "brains": ["*"]} for rt, env in (("claude", "CLAUDE_CONFIG_DIR"), ("codex", "CODEX_HOME"))
        if shutil.which(rt)]}
    if isinstance(data, list):
        data = {"subscriptions": data}
    if not isinstance(data, dict) or not isinstance(data.get("subscriptions"), list):
        raise ValueError("invalid subscriptions.json (expected subscriptions list)")
    stop, margin = data.get("stop_percent", 85), data.get("pacing_margin", 10)
    if not usage_number(stop) or not 0 < stop <= 100 or not usage_number(margin) or not 0 <= margin <= 100:
        raise ValueError("invalid usage stop_percent or pacing_margin")
    subs, ids = [], set()
    for s in data["subscriptions"]:
        if not isinstance(s, dict) or not isinstance(s.get("runtime"), str) or s["runtime"] not in USAGE_RUNTIMES \
                or not isinstance(s.get("id"), str) \
                or not s["id"] or s["id"] in ids or not isinstance(s.get("brains", []), list) \
                or any(not isinstance(b, str) for b in s.get("brains", [])):
            raise ValueError("invalid subscription (unique id, runtime and brains list required)")
        sub_stop = s.get("stop_percent", stop)
        if not usage_number(sub_stop) or not 0 < sub_stop <= 100:
            raise ValueError("invalid subscription stop_percent (must be greater than 0 and at most 100)")
        ids.add(s["id"])
        rt = s["runtime"]
        cfg = s.get("config_dir") or os.environ.get(USAGE_RUNTIMES[rt]) or str(Path.home() / ("." + rt))
        host = s.get("host_provider_instance_id", "")
        label = s.get("label", s["id"])
        if not isinstance(cfg, str) or not isinstance(host, str) or not isinstance(label, str):
            raise ValueError("invalid subscription config_dir, label or host_provider_instance_id")
        subs.append({"id": s["id"], "runtime": rt, "label": label,
                     "config_dir": str(Path(cfg).expanduser().absolute()),
                     "host_provider_instance_id": host, "brains": s.get("brains", []), "stop_percent": sub_stop})
    return {"subscriptions": subs, "stop_percent": stop, "pacing_margin": margin,
            "autodetected": not path.exists()}


USAGE_RUNTIMES = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}


def usage_stamp(value):
    if usage_number(value):
        return value if 0 < value < 253402300799 else None
    if isinstance(value, str):
        try:
            # Python 3.9/3.10 accept only three or six fractional digits.
            value = re.sub(r"(T\d{2}:\d{2}:\d{2})\.(\d+)",
                           lambda m: m[1] + "." + (m[2] + "000000")[:6], value)
            dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.timestamp() if dt.tzinfo else None
        except (ValueError, OverflowError):
            pass
    return None


def usage_windows(data, runtime, absences=False):
    """Subscription windows only; scoped weekly limits carry their model aliases."""
    if not isinstance(data, dict):
        return {}
    out = {}
    if runtime == "codex":
        if (data.get("limitId") or data.get("limit_id") or "codex") != "codex":
            return {}
        for key in ("primary", "secondary"):
            w = data.get(key)
            if not isinstance(w, dict):
                continue
            mins = w.get("windowDurationMins", w.get("window_minutes"))
            name = next((n for n, m in USAGE_WINDOWS.items() if m == mins), None)
            used = w.get("usedPercent", w.get("used_percent"))
            reset = usage_stamp(w.get("resetsAt", w.get("resets_at")))
            if name and usage_number(used) and 0 <= used <= 100:
                out[name] = {"used_percent": used, "resets_at": reset}
        # A null slot alongside a valid weekly window declares no five-hour limit.
        # Omitted or malformed slots leave it unknown.
        if absences and "seven_day" in out and "five_hour" not in out and all(k in data for k in ("primary", "secondary")) \
                and any(data[k] is None for k in ("primary", "secondary")):
            out["five_hour"] = {"absent": True}
    else:
        limits = data.get("limits")
        if isinstance(limits, list):
            for w in limits:
                if not isinstance(w, dict):
                    continue
                name = {"session": "five_hour", "weekly_all": "seven_day"}.get(w.get("kind"))
                models = []
                if w.get("kind") == "weekly_scoped":
                    scope = w.get("scope")
                    model = scope.get("model") if isinstance(scope, dict) else None
                    if isinstance(model, dict):
                        models = [model[k] for k in ("id", "display_name")
                                  if isinstance(model.get(k), str) and model[k]]
                    if models:
                        name = "seven_day_model:" + models[0]
                used = w.get("percent")
                if name and usage_number(used) and 0 <= used <= 100:
                    out[name] = {"used_percent": used, "resets_at": usage_stamp(w.get("resets_at"))}
                    if models:
                        out[name]["models"] = models
        for name in USAGE_WINDOWS:
            if name in out:
                continue
            w = data.get(name)
            if not isinstance(w, dict):
                continue
            used = w.get("utilization", w.get("used_percentage"))
            if usage_number(used) and 0 <= used <= 100:
                out[name] = {"used_percent": used, "resets_at": usage_stamp(w.get("resets_at"))}
    return out


def usage_account(data):
    if isinstance(data, dict):
        for k in ("account_id", "accountId", "account_uuid", "uuid", "id"):
            if isinstance(data.get(k), str) and data[k]:
                return data[k]
    return None


def usage_codex(sub):
    """JSONL app-server protocol: initialize, initialized, account/read, account/rateLimits/read."""
    import selectors
    env = {**os.environ, "CODEX_HOME": sub["config_dir"]}
    p = subprocess.Popen(["codex", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, env=env)
    sel, pending = selectors.DefaultSelector(), b""
    sel.register(p.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + 15

    def send(msg):
        p.stdin.write((json.dumps(msg) + "\n").encode())
        p.stdin.flush()

    def call(i, method, params):
        nonlocal pending
        send({"id": i, "method": method, "params": params})
        while time.monotonic() < deadline:
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if isinstance(d, dict) and d.get("id") == i:
                    return d.get("result") or {}
            if not sel.select(max(0, deadline - time.monotonic())):
                break
            chunk = os.read(p.stdout.fileno(), 65536)
            if not chunk:
                break
            pending += chunk
            if len(pending) > 2 * 1024 * 1024:
                break
        raise ValueError("Codex usage lookup timed out")

    try:
        call(1, "initialize", {"clientInfo": {"name": "brain_usage", "version": "1"}})
        send({"method": "initialized", "params": {}})
        account = call(2, "account/read", {"refreshToken": False}).get("account") or {}
        result = call(3, "account/rateLimits/read", {})
        rates = (result.get("rateLimitsByLimitId") or {}).get("codex") or result.get("rateLimits")
        windows = usage_windows(rates, "codex", absences=True)
        if not windows:
            raise ValueError("Codex returned no subscription windows")
        # Older account/read omits the ID. Read only the account label, never decode or retain tokens.
        identity = usage_account(account)
        if not identity:
            auth = usage_json(Path(sub["config_dir"]) / "auth.json")
            identity = usage_account(auth.get("tokens")) if isinstance(auth, dict) else None
        return windows, identity
    finally:
        sel.close()
        p.terminate()
        try:
            p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
        p.stdin.close()
        p.stdout.close()


def usage_keychain_services(config_dir):
    import hashlib
    import unicodedata
    hashed = "Claude Code-credentials-" + hashlib.sha256(
        unicodedata.normalize("NFC", config_dir).encode()).hexdigest()[:8]
    # Claude Code suffixes the entry whenever CLAUDE_CONFIG_DIR is set, even to the default
    # folder (hosts set it), so the default folder may have a login under either name.
    return ["Claude Code-credentials", hashed] if config_dir == str(Path.home() / ".claude") else [hashed]


def usage_keychain(config_dir):
    """Use the macOS-trusted CLI; its secret stdout stays in a captured pipe only."""
    if sys.platform != "darwin" or (os.environ.get("BRAIN_USAGE_TESTING") == "1" and
                                    os.environ.get("BRAIN_USAGE_TEST_NO_KEYCHAIN") == "1"):
        return {}, "Keychain unavailable"
    found, notes = [], []
    for service in usage_keychain_services(config_dir):
        d, note = usage_keychain_read(service)
        oauth = d.get("claudeAiOauth")
        if isinstance(oauth, dict) and isinstance(oauth.get("accessToken"), str) and oauth["accessToken"]:
            found.append(d)
        else:
            notes.append(note or "No stored Claude OAuth token")
    if not found:
        return {}, notes[-1]
    def expiry(d):
        v = d["claudeAiOauth"].get("expiresAt")
        return v if usage_number(v) else 0
    return max(found, key=expiry), ""  # the login Claude renewed most recently


def usage_keychain_read(service):
    import getpass
    if service in _KEYCHAIN_CYCLE_BLOCKED or time.monotonic() < _KEYCHAIN_RETRY_AT.get(service, 0):
        return {}, "Keychain unavailable; retry after cooldown"
    user = os.environ.get("USER") or getpass.getuser()
    try:
        # security has no per-read no-UI flag. No stdin/tty, and bound any permission wait.
        r = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", service,
                            "-a", user, "-w"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           stdin=subprocess.DEVNULL, timeout=2, start_new_session=True)
        if r.returncode:
            raise ValueError("unavailable")
        d = json.loads(r.stdout)
        if not isinstance(d, dict):
            raise ValueError("invalid credential")
        _KEYCHAIN_RETRY_AT.pop(service, None)
        return d, ""
    except (OSError, ValueError, subprocess.TimeoutExpired):
        _KEYCHAIN_CYCLE_BLOCKED.add(service)
        _KEYCHAIN_RETRY_AT[service] = time.monotonic() + 600
        return {}, "Keychain denied/unavailable; retry in 10 minutes"


EXPIRED_CLAUDE_LOGIN = ("Claude login expired: it renews the next time Claude runs with this "
                        "subscription's folder; using logs if available")


def usage_claude(sub):
    import urllib.error
    import urllib.request
    import urllib.parse
    cfg = Path(sub["config_dir"])
    note, credentials = "", {}
    if sys.platform != "darwin":
        credentials = usage_json(cfg / ".credentials.json") or {}
    testing = os.environ.get("BRAIN_USAGE_TESTING") == "1"
    url = "https://api.anthropic.com/api/oauth/usage"
    if testing and os.environ.get("BRAIN_USAGE_TEST_URL"):
        url = os.environ["BRAIN_USAGE_TEST_URL"]
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "http" or parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("test usage URL must be loopback HTTP")
    if testing and os.environ.get("BRAIN_USAGE_TEST_TOKEN"):
        token = os.environ["BRAIN_USAGE_TEST_TOKEN"]
    else:
        if sys.platform == "darwin":
            credentials, note = usage_keychain(str(cfg))
        oauth = credentials.get("claudeAiOauth") or {}
        token = oauth.get("accessToken")
        if isinstance(token, str) and token and usage_number(oauth.get("expiresAt")) and \
                oauth["expiresAt"] / 1000 <= time.time():
            return {}, None, EXPIRED_CLAUDE_LOGIN
    if not isinstance(token, str) or not token:
        return {}, None, note or "No stored Claude OAuth token"
    # Redirects must never carry the Authorization header to another host.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    request = urllib.request.Request(url, headers={"Authorization": "Bearer " + token,
                                                   "anthropic-beta": "oauth-2025-04-20"})
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=8) as response:
            data = json.loads(response.read(1024 * 1024))
    except urllib.error.HTTPError as e:
        # Only the status code: the body and headers stay unread.
        return {}, None, EXPIRED_CLAUDE_LOGIN if e.code in (401, 403) else \
            f"Claude usage lookup refused (HTTP {e.code}); using logs if available"
    except (urllib.error.URLError, OSError):
        return {}, None, "Claude usage lookup unreachable (network); using logs if available"
    return usage_windows(data, "claude"), usage_account(data.get("account")) or usage_account(data), note


def usage_logs(sub):
    cfg, rt = Path(sub["config_dir"]), sub["runtime"]
    pattern = "sessions/*/*/*/rollout-*.jsonl" if rt == "codex" else "projects/*/*.jsonl"
    readings = []
    for path in cfg.glob(pattern):
        try:
            if path.stat().st_mtime < time.time() - 8 * 86400:
                continue
            # A bounded tail avoids loading long transcripts or retaining conversation content.
            with path.open("rb") as fh:
                fh.seek(0, 2)
                start = max(0, fh.tell() - 1024 * 1024)
                fh.seek(start)
                if start:
                    fh.readline()
                lines = fh.read().splitlines()
            for line in lines:
                try:
                    e = json.loads(line)
                    stamp = usage_stamp(e.get("timestamp"))
                    if not stamp or stamp > time.time() + 60:
                        continue
                    if rt == "codex":
                        p = e.get("payload") or {}
                        if e.get("type") != "event_msg" or p.get("type") != "token_count":
                            continue
                        windows = usage_windows(p.get("rate_limits"), rt)
                    else:
                        if e.get("apiError") != "usage_limit_reached":
                            continue
                        r = (e.get("apiErrorParams") or {}).get("rate_limit_info") or {}
                        name = r.get("rateLimitType")
                        windows = {name: {"used_percent": 100, "resets_at": usage_stamp(r.get("resetsAt"))}} \
                            if name in USAGE_WINDOWS and r.get("status") == "rejected" else {}
                    if windows:
                        readings.append((stamp, windows))
                except (ValueError, TypeError, AttributeError):
                    continue
        except OSError:
            continue
    if not readings:
        return {}, None
    # Preserve each window's actual observation time, especially sparse Claude limit-hit events.
    windows = {}
    for stamp, ws in sorted(readings, key=lambda r: r[0]):
        windows.update({k: {**w, "read_at": stamp} for k, w in ws.items()})
    return windows, min(w["read_at"] for w in windows.values())


def usage_key(runtime, identity):
    import hashlib
    return runtime + ":" + hashlib.sha256(identity.encode()).hexdigest()[:24]


def usage_reading(sub):
    account, note, windows = None, "", {}
    if not Path(sub["config_dir"]).is_dir():
        return usage_key(sub["runtime"], sub["config_dir"]), {
            "windows": {}, "read_at": None, "source": "unknown", "note": "Runtime config directory unavailable"}
    try:
        if sub["runtime"] == "codex":
            windows, account = usage_codex(sub)
        else:
            windows, account, note = usage_claude(sub)
    except Exception:
        # Never include exception messages: an upstream error can contain headers or credentials.
        note = "Runtime lookup failed; using logs if available"
    if windows:
        stamp, source = time.time(), "lookup"
    else:
        windows, stamp = usage_logs(sub)
        source = "log" if windows else "unknown"
    return usage_key(sub["runtime"], account or sub["config_dir"]), {
        "windows": windows, "read_at": stamp, "source": source, "note": note}


def usage_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2, allow_nan=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class usage_lock:
    def __init__(self, name):
        self.path, self.fh = usage_file(name), None

    def __enter__(self):
        import fcntl
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = self.path.open("a+")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.fh.close()
            self.fh = None
            return False
        self.fh.seek(0)
        self.fh.truncate()
        self.fh.write(str(os.getpid()))
        self.fh.flush()
        return True

    def __exit__(self, *exc):
        if self.fh:
            self.fh.seek(0)
            self.fh.truncate()
            self.fh.close()  # retain the inode: deleting a flock file races the next holder


def usage_merge(prior, reading):
    """Keep the newest observation of each window, including its source and time."""
    if not prior.get("windows"):
        return reading
    windows = {}
    changed = False
    for r in (prior, reading):
        for name, w in r.get("windows", {}).items():
            w = {**w, "read_at": w.get("read_at", r.get("read_at")), "source": w.get("source", r.get("source"))}
            old = windows.get(name)
            if old is None or (w["read_at"] or 0, w["source"] == "lookup") > \
                    (old["read_at"] or 0, old["source"] == "lookup"):
                windows[name] = w
                if r is reading:
                    changed = True
    if not changed and prior.get("windows"):
        return {**prior, "note": reading.get("note", "")}  # older numbers, today's reason
    if not windows:
        return reading
    newest = max(windows.values(), key=lambda w: w["read_at"] or 0)
    return {**reading, "windows": windows, "read_at": min(w["read_at"] or 0 for w in windows.values()),
            "source": newest["source"]}


def usage_refresh(config, stale_only=False, poll_interval=None):
    with usage_lock("usage-refresh.lock") as held:
        cached = usage_cache()
        if not held:
            return cached  # another reader/poller is refreshing; never duplicate its calls
        if stale_only and usage_cache_fresh(cached, config):
            return cached
        _KEYCHAIN_CYCLE_BLOCKED.clear()
        accounts, subs, paths = {}, [], {}
        for sub in config["subscriptions"]:
            path_key = (sub["runtime"], sub["config_dir"])
            if path_key not in paths:
                key, reading = usage_reading(sub)
                prior_sub = next((s for s in cached.get("subscriptions", []) if all(
                    s.get(k) == sub[k] for k in ("id", "runtime", "config_dir"))), {})
                prior = cached.get("accounts", {}).get(prior_sub.get("account_key"), {})
                if reading["source"] != "lookup" and prior.get("windows"):
                    key = prior_sub["account_key"]
                # A different account confirmed by a lookup must not inherit another account's limits.
                if reading["source"] != "lookup" or key == prior_sub.get("account_key"):
                    reading = usage_merge(prior, reading)
                paths[path_key] = key
                accounts[key] = usage_merge(accounts.get(key, {}), reading)
            subs.append({**sub, "account_key": paths[path_key]})
        if stale_only and accounts == cached.get("accounts") and subs == cached.get("subscriptions"):
            return cached  # a failed inline lookup cannot relabel or freshen the last good cache
        data = {**config, "subscriptions": subs, "accounts": accounts, "checked_at": time.time()}
        interval = poll_interval if poll_interval is not None else cached.get("poll_interval_seconds")
        if usage_number(interval) and interval > 0:
            data["poll_interval_seconds"] = interval
        usage_atomic(usage_file(), data)
        return data


def usage_max_age(cache):
    every = cache.get("poll_interval_seconds")
    return 2 * every if usage_number(every) and every > 0 else USAGE_MAX_AGE


def usage_poller_live():
    import fcntl
    try:
        with usage_file("usage-watch.lock").open() as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
    except FileNotFoundError:
        pass
    return False


def usage_cache_fresh(cache, config):
    checked = cache.get("checked_at")
    keys = ("id", "runtime", "label", "config_dir", "host_provider_instance_id", "brains")
    return usage_number(checked) and 0 <= time.time() - checked < usage_max_age(cache) and \
        [{k: s.get(k) for k in keys} for s in cache.get("subscriptions", [])] == \
        [{k: s.get(k) for k in keys} for s in config["subscriptions"]]


def usage_brain_name(inst):
    names = [name for name, root in _m_instance.brains_here() if root.resolve() == inst.root.resolve()]
    return names[0] if names else _m_nodes.brain_name(inst.root)


def usage_allowed(sub, name):
    return "*" in sub["brains"] or name in sub["brains"]


def usage_datetime(stamp, inst=None):
    zone = inst.zone() if inst else None
    return datetime.datetime.fromtimestamp(stamp, zone) if zone else datetime.datetime.fromtimestamp(stamp).astimezone()


def usage_time(stamp, inst=None):
    if not stamp:
        return "unknown"
    d = usage_datetime(stamp, inst)
    z = d.strftime("%z")
    return f"{d:%Y-%m-%d %H:%M} {z[:3]}:{z[3:]}"


def usage_age(seconds):
    if seconds is None:
        return "unknown"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    minutes = seconds // 60
    return f"{minutes // 60} h {minutes % 60} m" if minutes >= 60 else f"{minutes} m"


def usage_rows(inst=None, refresh=False, before_launch=False, node=None, extra=()):
    config = usage_config()
    cache = usage_cache()
    if refresh or (before_launch and not usage_cache_fresh(cache, config) and not usage_poller_live()):
        if inst:
            with _m_instance.released(inst, node if node is not None else [],
                                      extra=[usage_file("subscriptions.json"), *extra], local_keys=("fallback",)):
                cache = usage_refresh(config, stale_only=not refresh)
            config = usage_config()
        else:
            cache = usage_refresh(config, stale_only=not refresh)
    config = {**config, "max_age_seconds": usage_max_age(cache), "other_subscriptions": 0}
    brain = usage_brain_name(inst) if inst else None
    rows = []
    for sub in config["subscriptions"]:
        if inst and not usage_allowed(sub, brain):
            config["other_subscriptions"] += 1
            continue
        prior = next((s for s in cache.get("subscriptions", []) if all(s.get(k) == v for k, v in sub.items() if k != "stop_percent")), {})
        r = cache.get("accounts", {}).get(prior.get("account_key"), {})
        stamp = r.get("read_at")
        age = max(0, time.time() - stamp) if usage_number(stamp) else None
        windows = {}
        for name, w in r.get("windows", {}).items():
            reset = w.get("resets_at")
            windows[name] = {**w, "expired": bool(reset and reset <= time.time())}
            if inst and reset:
                windows[name]["reset_time"] = usage_datetime(reset, inst).isoformat(timespec="seconds")
        rows.append({**sub, "brains": ["*"] if "*" in sub["brains"] else ([brain] if inst else sub["brains"]),
                     "available_to_every_brain": "*" in sub["brains"], "autodetected": config["autodetected"], "config_dir": _m_common.tilde(sub["config_dir"]), "account_key": prior.get("account_key"),
                     "windows": windows, "read_at": stamp, "age_seconds": round(age, 1) if age is not None else None,
                     "source": r.get("source", "unknown"), "note": r.get("note", ""),
                     "reading_time": usage_datetime(stamp, inst).isoformat(timespec="seconds")
                     if inst and stamp else None})
    return rows, config


def class_allows_fallback(job_class):
    """Automatic cross-runtime retries are disabled; explicit runtime choices still win.

    Builders stay on Codex and review/diagnose stay on Claude. Missing legacy
    classes default to build, so they cannot authorize an automatic switch either.
    """
    return False


def class_runtime(inst, job_class, runtime=None, runtimes=None):
    """Resolve the engine before model-specific subscription limits are considered."""
    preferred = "claude" if job_class in ("diagnose", "review") else "codex"
    allowed_runtimes = list(inst.runtimes if runtimes is None else runtimes)
    reason = f"Routing: class {job_class or 'build'} defaults to {preferred}."
    if preferred not in allowed_runtimes:
        configured = inst.cfg.get("default_runtime")
        preferred = configured if configured in allowed_runtimes else next(iter(allowed_runtimes), None)
        if not preferred:
            _m_common.die("no allowed runtime for this job")
        reason += f" Class default unavailable; using configured/allowed runtime {preferred}."
    chosen = runtime or preferred
    if runtime:
        reason += f" Runtime pin: {runtime} wins."
    if job_class not in ("diagnose", "review"):
        reason += " Builder policy keeps Codex regardless of weekly usage; only an explicit runtime or node pin changes it."
    return chosen, reason


def class_route(inst, job_class, runtime=None, model=None, cached_only=False, weekly_cap=None, runtimes=None, node=None, extra=()):
    """Class policy, pins and weekly readings. Cached mode never contacts a provider.

    Usage informs this choice; even exhausted or unknown meters leave a brief available.
    The optional runtime is an explicit or node pin and always wins.
    """
    chosen, reason = class_runtime(inst, job_class, runtime, runtimes)
    allowed_runtimes = list(inst.runtimes if runtimes is None else runtimes)
    # weekly_cap is retained for callers of the earlier interface; it never switches builders.
    try:
        if usage_config()["autodetected"]:
            return chosen, None, reason + " Usage unknown: no subscriptions.json; no provider refresh."
        rows, policy = usage_rows(inst, before_launch=not cached_only, node=node, extra=extra)
    except (OSError, ValueError):
        rows, policy = [], {"max_age_seconds": USAGE_MAX_AGE}
    def weekly(row):
        w = row.get("windows", {}).get("seven_day", {})
        stamp = w.get("read_at", row.get("read_at"))
        if (w.get("absent") or w.get("expired") or
                w.get("source", row.get("source")) == "unknown" or not usage_number(stamp) or
                not 0 <= time.time() - stamp <= policy["max_age_seconds"] or
                not usage_number(w.get("used_percent"))):
            return None
        return w["used_percent"]
    def active_windows(row):
        return [w for name, w in row.get("windows", {}).items()
                if not w.get("expired") and not w.get("absent") and
                (name in USAGE_WINDOWS or (model and model.casefold() in
                 [m.casefold() for m in w.get("models", [])]))]
    def below_stop(row):
        active = active_windows(row)
        return bool(active) and all(w["used_percent"] < row.get("stop_percent", policy.get("stop_percent", 85)) for w in active)
    fallback = inst.local.get("fallback", [])
    fallback = fallback if isinstance(fallback, list) else []
    def rank(row):
        host = row.get("host_provider_instance_id")
        fullest = max((w["used_percent"] for w in active_windows(row)), default=0)
        return (fullest >= row.get("stop_percent", policy.get("stop_percent", 85)), fullest,
                fallback.index(host) if host in fallback else len(fallback), row["id"])
    available = sorted([r for r in rows if r["runtime"] == chosen], key=rank)
    measured = [(weekly(r), r) for r in available if weekly(r) is not None]
    selected = min(measured, key=lambda x: (rank(x[1])[0], x[0], rank(x[1])))[1] if measured else next(iter(available), None)
    used = weekly(selected) if selected else None
    if used is None:
        reason += " Usage unknown: weekly reading missing/stale; keeping this runtime."
    else:
        reason += f" Weekly meter {used:g}%; keeping {chosen}."
    if selected:
        active = active_windows(selected)
        if any(w["used_percent"] >= selected.get("stop_percent", policy.get("stop_percent", 85)) for w in active):
            reason += " Usage: subscription reached its stop line. Stop starting jobs; the brief remains available."
            if runtime:
                reason += f" Pinned runtime {runtime} reached its stop line."
            other = next((r for r in rows if r["runtime"] != chosen and
                          r["runtime"] in allowed_runtimes and below_stop(r)), None)
            if other:
                reason += f" Alternative: {other['label']} ({other['runtime']}); choose --runtime explicitly."
        host = selected.get("host_provider_instance_id") or "unbound"
        reason += f" Subscription: {selected['label']} ({selected['id']}) · host instance: {host}."
    return chosen, selected, reason


def usage_route(inst, runtimes, preferred=None, model=None, cached_only=False, node=None, extra=()):
    try:
        if usage_config()["autodetected"]:
            return None, "Usage unknown: no subscriptions.json; keeping the configured runtime (autodetected defaults allow every brain)."
        rows, policy = usage_rows(inst, before_launch=not cached_only, node=node, extra=extra)
    except (OSError, ValueError):
        return None, "Usage unknown: machine subscription configuration unavailable; no subscription selected."
    allowed = [r for r in rows if r["runtime"] in runtimes]
    fallback = inst.local.get("fallback", [])
    fallback = fallback if isinstance(fallback, list) else []
    eligible = []
    for r in allowed:
        active = {n: w for n, w in r["windows"].items() if not w.get("absent") and not w["expired"] and
                  (n in USAGE_WINDOWS or (model and model.casefold() in
                   [m.casefold() for m in w.get("models", [])]))}
        if any(w["used_percent"] >= r.get("stop_percent", policy["stop_percent"]) for w in active.values()):
            continue
        unknown = not active or any(w.get("source", r["source"]) == "unknown" or
            not w.get("read_at", r["read_at"]) or time.time() - w.get("read_at", r["read_at"]) > policy["max_age_seconds"]
            for w in active.values())
        weekly_windows = [w for n, w in active.items() if n.startswith("seven_day")]
        def ahead_of_pace(w):
            reset = w.get("resets_at")
            elapsed = max(0, min(100, 100 * (1 - (reset - time.time()) / (7 * 86400)))) if reset else 100
            return w["used_percent"] > elapsed + policy["pacing_margin"]
        ahead = any(ahead_of_pace(w) for w in weekly_windows)
        used = max((w["used_percent"] for w in active.values()), default=0)
        host = r["host_provider_instance_id"]
        rank = fallback.index(host) if host in fallback else len(fallback)
        eligible.append(((unknown, ahead, used, rank, r["runtime"] != preferred, r["id"]), r))
    if not eligible:
        if not allowed:
            return None, "Usage unknown: no subscription is allowed for this brain and runtime; keeping the configured runtime."
        return None, "Usage: all allowed subscriptions reached the stop line. Stop starting jobs; the brief remains available."
    score, selected = min(eligible, key=lambda x: x[0])
    reason = "usage unknown/stale; capacity unverified" if score[0] else \
        f"most headroom ({100 - score[2]:g}% remaining in the fullest reported window)"
    week = selected["windows"].get("seven_day", {})
    if not week.get("resets_at") or week.get("expired"):
        reason += "; weekly pace unknown"
    elif not score[1]:
        reason += "; within weekly pacing"
    if score[1]:
        reason += "; weekly usage ahead of pace, no better-paced subscription available"
    host = selected["host_provider_instance_id"] or "unbound (choose its provider instance before launch)"
    return selected, f"Subscription: {selected['label']} ({selected['id']}) · host instance: {host} · {reason}."


def usage_ui(inst):
    try:
        rows, _ = usage_rows(inst)  # UI reads the file; it never polls runtimes.
    except (OSError, ValueError):
        return []
    terms = [term for n in inst.nodes() for term in _m_guard.never_terms(n)]
    def hide(value):
        if isinstance(value, str):
            return _m_guard.redact(value, terms)
        if isinstance(value, list):
            return [hide(v) for v in value]
        if isinstance(value, dict):
            return {k: hide(v) for k, v in value.items()}
        return value
    rows = [{k: v for k, v in r.items() if k != "config_dir"} for r in rows]
    return hide(rows) if terms else rows


def usage_other_count(inst):
    try:
        return usage_rows(inst)[1]["other_subscriptions"]
    except (OSError, ValueError):
        return 0


USAGE_LABEL = "org.brain.usage"


def usage_agent_default():
    return Path.home() / "Library/LaunchAgents" / f"{USAGE_LABEL}.plist"


def usage_launchd(*args):
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def usage_service_managed():
    """True where this command may start, stop or restart the machine's real poller."""
    return sys.platform == "darwin" and not os.environ.get("BRAIN_USAGE_TESTING") and bool(shutil.which("launchctl"))


def usage_loaded():
    return usage_launchd("print", f"gui/{os.getuid()}/{USAGE_LABEL}").returncode == 0


def usage_stop():
    """Stop the loaded poller; True once launchd no longer has it (bootout finishes asynchronously)."""
    usage_launchd("bootout", f"gui/{os.getuid()}/{USAGE_LABEL}")
    for _ in range(20):
        if not usage_loaded():
            return True
        time.sleep(0.25)
    return False


def usage_install(a):
    import plistlib
    import shlex
    path = Path(a.launch_agent).expanduser() if a.launch_agent else usage_agent_default()
    if not a.launch_agent and sys.platform != "darwin":
        print("usage poller: automatic install is macOS-only here; keep one `brain usage --watch` running as a "
              "background service (a systemd user unit, say)")
        return
    config = str(_m_instance.brains_file().resolve())
    data = {"Label": USAGE_LABEL, "ProgramArguments": [sys.executable, str(_m_common.CORE / "bin/brain"),
            "usage", "--watch", "--every", f"{a.every:g}"], "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 30, "ProcessType": "Background",
            "StandardOutPath": str(Path.home() / "Library/Logs/brain-usage.log"),
            "StandardErrorPath": str(Path.home() / "Library/Logs/brain-usage.log"),
            "EnvironmentVariables": {"BRAIN_CONFIG": config, "BRAIN_NO_REEXEC": "1", "HOME": str(Path.home()),
                                     "PATH": os.environ.get("PATH", "/usr/bin:/bin")}}
    for env in USAGE_RUNTIMES.values():
        if os.environ.get(env):
            data["EnvironmentVariables"][env] = os.environ[env]
    same = False
    if path.exists():
        try:
            old = plistlib.loads(path.read_bytes())
        except Exception:
            old = None
        # Only ever replace our own agent; anything else at that path is the operator's to inspect.
        if not isinstance(old, dict) or old.get("Label") != USAGE_LABEL:
            _m_common.die(f"{_m_common.tilde(path)} exists and isn't the brain usage LaunchAgent; inspect it before replacing it")
        same = old == data
    if not same:
        path.parent.mkdir(parents=True, exist_ok=True)
        # launchd requires the log's parent to exist.
        (Path.home() / "Library/Logs").mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(plistlib.dumps(data))
        os.replace(tmp, path)
    state = "unchanged" if same else "wrote"
    if a.no_load or path != usage_agent_default() or not usage_service_managed():
        print(f"{state} {_m_common.tilde(path)}; not loaded. Start with: launchctl bootstrap gui/$(id -u) "
              f"{shlex.quote(str(path))}")
        return
    domain = f"gui/{os.getuid()}"
    if usage_loaded():
        if same:
            print(f"usage poller already running ({_m_common.tilde(path)}); every {a.every:g} s")
            return
        if not usage_stop():
            _m_common.die(f"wrote {_m_common.tilde(path)} but the running poller didn't stop; the old one is still running "
                f"(launchctl bootout {domain}/{USAGE_LABEL}, then brain usage install)")
    r = usage_launchd("bootstrap", domain, str(path))
    if r.returncode and not usage_loaded():
        _m_common.die(f"wrote {_m_common.tilde(path)} but launchd didn't load it: {r.stderr.strip() or r.returncode}")
    print(f"usage poller running: every {a.every:g} s, account-wide; log ~/Library/Logs/brain-usage.log "
          "(stop: brain usage uninstall)")


def usage_uninstall(a):
    path = Path(a.launch_agent).expanduser() if a.launch_agent else usage_agent_default()
    if path == usage_agent_default() and usage_service_managed() and usage_loaded() and not usage_stop():
        _m_common.die("the usage poller didn't stop; left its LaunchAgent in place")
    if path.exists():
        path.unlink()
        print(f"usage poller removed ({_m_common.tilde(path)}); readings stay until they expire")
    else:
        print("usage poller not installed")


def usage_restart(root):
    """After an upgrade, a running poller restarts so it runs the new core, if it runs this brain's core."""
    import plistlib
    if not (usage_service_managed() and usage_loaded()):
        return
    try:
        args = plistlib.loads(usage_agent_default().read_bytes())["ProgramArguments"]
        mine = Path(args[1]).resolve() == (root / "core/bin/brain").resolve()
    except Exception:
        mine = False
    if not mine:
        return  # another brain's core runs it; that brain's upgrade restarts it
    if usage_launchd("kickstart", "-k", f"gui/{os.getuid()}/{USAGE_LABEL}").returncode:
        print("brain: couldn't restart the usage poller; it runs the old core until it restarts "
              "(brain usage install)", file=sys.stderr)
    else:
        print("usage poller restarted on the new core")


def cmd_usage(a):
    if not usage_number(a.every) or a.every <= 0:
        _m_common.die("--every must be positive seconds")
    if a.action == "install":
        usage_install(a)
        return
    if a.action == "uninstall":
        usage_uninstall(a)
        return
    if a.watch:
        with usage_lock("usage-watch.lock") as held:
            if not held:
                print("brain: usage poller already running", file=sys.stderr)
                return
            try:
                while True:
                    try:
                        usage_refresh(usage_config(), poll_interval=a.every)
                    except Exception:
                        print("brain: usage refresh unavailable; retrying next interval", file=sys.stderr, flush=True)
                    time.sleep(a.every)
            except KeyboardInterrupt:
                return
    root = _m_nodes.try_find_root()
    inst = _m_instance.Instance(root) if root else None
    try:
        rows, policy = usage_rows(inst, refresh=a.refresh)
    except (OSError, ValueError) as e:
        _m_common.die(str(e))
    if not inst:
        visible = [r for r in rows if usage_allowed(r, None)]
        policy["other_subscriptions"] = len(rows) - len(visible)
        rows = visible
    if a.json:
        print(json.dumps({"scope": "account-wide", "stop_percent": policy["stop_percent"],
                          "other_subscriptions": policy["other_subscriptions"], "subscriptions": rows}, indent=2))
        return
    print(f"Account-wide subscription usage · default stop line {policy['stop_percent']:g}% · this machine's share: unavailable")
    table = [["runtime", "label", "stop line", "5h used / reset", "weekly used / reset", "reading / age", "source", "brains"]]
    for r in rows:
        if not r["read_at"]:
            print(f"{r['label']}: no reading yet: run `brain usage --refresh` or install the poller (`brain usage install`); stop line {r['stop_percent']:g}%")
            continue
        def window(name):
            w = r["windows"].get(name)
            if not w:
                return "unknown"
            if w.get("absent"):
                return "none"
            reset = usage_time(w.get("resets_at"), inst)
            return f"{w['used_percent']:g}% / {reset}" + (" (expired; unknown)" if w["expired"] else "")
        table.append([r["runtime"], r["label"], f"{r['stop_percent']:g}%", window("five_hour"), window("seven_day"),
                      f"{usage_time(r['read_at'], inst)} / {usage_age(r['age_seconds'])}", r["source"],
                      "every brain" + (" (autodetected)" if r["autodetected"] else "")
                      if r["available_to_every_brain"] else ','.join(r['brains']) or 'none'])
    widths = [max(len(row[i]) for row in table) for i in range(len(table[0]))]
    for row in table if len(table) > 1 else []:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())
    for r in rows:
        for name, w in r["windows"].items():
            if name.startswith("seven_day_model:"):
                print(f"  {r['label']} · weekly model {', '.join(w.get('models', []))}: "
                      f"{w['used_percent']:g}% / {usage_time(w.get('resets_at'), inst)}")
        if r["note"]:
            print(f"  {r['label']}: {r['note']}")
    if policy["other_subscriptions"]:
        print(f"{policy['other_subscriptions']} other subscription(s) unavailable to this brain.")
    if inst:
        for n in inst.nodes():
            for line in _m_jobs.job_budget_lines(inst, n):
                print(_m_guard.redact(f"{n['id']}: {line}", _m_guard.never_terms(n)))
