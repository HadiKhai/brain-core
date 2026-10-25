"""Spend mechanics for Brain."""
import datetime
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import functools
import json
import os
import re
import time
from pathlib import Path

from . import common as _m_common
from . import connect as _m_connect
from . import guard as _m_guard
from . import history as _m_history
from . import jobs as _m_jobs
from . import nodes as _m_nodes
from . import ui as _m_ui


# Read from the runtimes' own transcripts on this machine, in the subscription folders this brain may use. Only
# aggregates are kept (counts per minute, never message text), in .brain/spend/, and each file is read once and
# then incrementally: transcripts only grow, so the next read starts where the last one stopped.

SPEND_IDLE_MIN = 5  # a gap between two events of more than this many minutes is idle time
SPEND_VERSION = 6
SPEND_FIELDS = ("events", "fresh", "cache_read", "cache_write", "output", "reasoning", "tools")
SPEND_TOKENS = ("fresh", "cache_read", "cache_write", "output", "reasoning")  # reasoning is part of output
JOB_MARK = "brain-job: {}"  # in every job brief: the sessions a brief starts are found again by it
JOB_MARK_RE = re.compile(rb"brain-job: ([0-9a-f]{24})\b")
SPEND_STAMP = re.compile(rb'"timestamp":"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(\.\d+)?')
SPEND_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,79}")
SPEND_RECENT = 64  # Claude splits one response into a few adjacent records: ids remembered to count it once


def spend_stamp(raw):
    """A record's UTC time in seconds, from the raw line (both runtimes write UTC `…Z` stamps). Codex puts it
    first, Claude Code near the end: the last one in the line is the record's own."""
    at = raw.rfind(b'"timestamp":"')
    m = SPEND_STAMP.match(raw, at) if at >= 0 else None
    if not m:
        return None
    import calendar
    try:
        y, mo, d, h, mi, sec = (int(x) for x in m.groups()[:6])
        return calendar.timegm((y, mo, d, h, mi, sec, 0, 0, 0)) + (float(m[7]) if m[7] else 0.0)
    except (ValueError, OverflowError):
        return None


def spend_count(value):
    return value if type(value) is int and 0 <= value < 10 ** 12 else 0


def spend_new(runtime, path):
    return {"v": SPEND_VERSION, "runtime": runtime, "id": "", "parent": "", "subagent": False,
            "context": None, "turns": 0, "turn_contexts": 0, "user_turns": 0,
            "job": "", "first_at": None, "last_at": None, "models": [], "m": {}, "mc": {}, "records": False, "compactions": [],
            "first": None, "last": None, "state": {"offset": 0, "prompt_done": False, "recent": [], "tools": [],
                                                   "model": -1, "last_input": 0, "prev": None, "own_start": None}}


def spend_counts(agg):
    """Does the record being read count: inside the read's window, when it has one."""
    st = agg["state"]
    return st.get("lo") is None or (st.get("at") is not None and st["lo"] <= st["at"] <= st["hi"])


def opaque(value):
    """A transcript's own id (a message, tool call or response), kept only as a digest: equal ids still match."""
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def spend_add(agg, key, minute, model, values):
    """Add per-field values to one minute of one model. Buckets are keyed "<minute>:<model index>". A read limited
    to a window ([lo, hi] seconds, a job's) counts nothing outside it."""
    st = agg["state"]
    if minute is None or (st.get("lo") is not None and (st.get("at") is None or not st["lo"] <= st["at"] <= st["hi"])):
        return
    b = agg[key].setdefault(f"{minute}:{model}", [0] * len(SPEND_FIELDS))
    for i, v in enumerate(values):
        b[i] += v
    agg["first"] = minute if agg["first"] is None else min(agg["first"], minute)
    agg["last"] = minute if agg["last"] is None else max(agg["last"], minute)


def spend_model(agg, name):
    name = name if isinstance(name, str) and SPEND_MODEL.fullmatch(name) else "unknown"  # an id, nothing more
    if name not in agg["models"]:
        agg["models"].append(name)
    return agg["models"].index(name)


def spend_claude_line(agg, raw, minute, at=None):
    st = agg["state"]
    spend_add(agg, "m", minute, -1, (1, 0, 0, 0, 0, 0, 0))
    wanted = b'"usage"' in raw or b'compact_boundary' in raw or b'"user"' in raw
    if not wanted:
        return
    try:
        r = json.loads(raw)
    except ValueError:
        return
    if not isinstance(r, dict):
        return
    kind = r.get("type")
    msg = r.get("message") if isinstance(r.get("message"), dict) else {}
    if kind == "user" and not any(r.get(k) for k in ("isMeta", "isCompactSummary", "isSidechain")):
        content = msg.get("content")
        if isinstance(content, list) and any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
            return
        agg["turns"] += 1
        if not st["prompt_done"]:  # only the opening prompt names the job
            st["prompt_done"] = True
            m = JOB_MARK_RE.search(raw)
            agg["job"] = m.group(1).decode() if m else ""
        return
    if kind == "system" and r.get("subtype") == "compact_boundary":
        meta = r.get("compactMetadata") if isinstance(r.get("compactMetadata"), dict) else {}
        if spend_counts(agg):
            agg["compactions"].append([minute, spend_count(meta.get("preTokens")), meta.get("trigger") == "auto"])
        return
    usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else None
    if kind != "assistant" or not usage or msg.get("model") == "<synthetic>":
        return
    model = spend_model(agg, msg.get("model"))
    now = [spend_count(usage.get(k)) for k in ("input_tokens", "cache_read_input_tokens",
                                                "cache_creation_input_tokens", "output_tokens")]
    agg["context"] = sum(now[:3])
    mid = opaque(msg["id"]) if isinstance(msg.get("id"), str) else None
    tools = 0
    for c in msg.get("content") if isinstance(msg.get("content"), list) else []:
        tid = opaque(c["id"]) if isinstance(c, dict) and c.get("type") in ("tool_use", "server_tool_use") and \
            isinstance(c.get("id"), str) else None
        if tid and tid not in st["tools"]:
            st["tools"] = (st["tools"] + [tid])[-SPEND_RECENT * 2:]
            tools += 1
    prior = next((p for p in st["recent"] if mid and p[0] == mid), None)
    if prior:  # the same response again: count only what grew, in the minute it was first counted (a windowed
        # read, a job's, counts it in the minute it grew: the first may lie before the job began)
        delta = [max(0, a - b) for a, b in zip(now, prior[1])]
        prior[1] = [max(a, b) for a, b in zip(now, prior[1])]
        agg["context"] = sum(prior[1][:3])
        windowed = st.get("lo") is not None
        spend_add(agg, "m", minute if windowed else prior[2], model if windowed else prior[3], (0, *delta, 0, tools))
        return
    if mid:
        st["recent"] = (st["recent"] + [[mid, now, minute, model]])[-SPEND_RECENT:]
    spend_add(agg, "m", minute, model, (0, *now, 0, tools))


CODEX_TOOL_CALLS = {"function_call", "custom_tool_call", "local_shell_call", "web_search_call", "tool_search_call",
                    "image_generation_call", "mcp_tool_call"}


def spend_codex_line(agg, raw, minute, at=None):
    st = agg["state"]
    if at is not None and st["own_start"] is not None and at < st["own_start"]:
        return  # history a fork copied from its parent: the parent's own file counts it, its time too
    spend_add(agg, "m", minute, -1, (1, 0, 0, 0, 0, 0, 0))
    hot = (b'"session_meta"', b'"token_usage_record"', b'"token_count"', b'"compacted"', b'"turn_context"', b'"user_message"',
           b'_call"')
    if not any(h in raw for h in hot) and not (not st["prompt_done"] and b'"user' in raw):
        return
    try:
        r = json.loads(raw)
    except ValueError:
        return
    p = r.get("payload") if isinstance(r, dict) and isinstance(r.get("payload"), dict) else None
    if p is None:
        return
    kind, ptype = r.get("type"), p.get("type")
    if kind == "session_meta":
        if st["own_start"] is None:
            src = p.get("source")
            spawn = src.get("subagent", {}).get("thread_spawn", {}) if isinstance(src, dict) and \
                isinstance(src.get("subagent"), dict) else {}
            agg.update(id=agg["id"] or _m_history.history_str(p.get("id")),
                       parent=agg.get("parent") or (_m_history.history_str(spawn.get("parent_thread_id")) if isinstance(spawn, dict) else ""),
                       subagent=isinstance(src, dict) and "subagent" in src or p.get("thread_source") == "subagent")
            st["own_start"] = at  # a fork's copied history is older than its own start, to the millisecond
        return
    if kind == "turn_context":
        agg["turn_contexts"] += 1
        agg["turns"] = agg["turn_contexts"]
        st["model"] = spend_model(agg, p.get("model"))
        return
    if kind == "compacted":
        if spend_counts(agg):
            agg["compactions"].append([minute, st["last_input"], None])
        return
    if kind == "response_item" and ptype in CODEX_TOOL_CALLS:
        spend_add(agg, "m", minute, st["model"], (0, 0, 0, 0, 0, 0, 1))
        return
    if kind == "event_msg" and ptype == "user_message":
        agg["user_turns"] += 1
        if not agg["turn_contexts"]:
            agg["turns"] = agg["user_turns"]
    if not st["prompt_done"] and ((kind == "response_item" and ptype == "message" and p.get("role") == "user") or
                                  (kind == "event_msg" and ptype == "user_message")):
        m = JOB_MARK_RE.search(raw)
        if m:
            agg["job"] = m.group(1).decode()
        return
    if kind == "token_usage_record":
        if p.get("thread_id") not in (None, agg["id"]):
            return  # another thread's response, carried along
        st["prompt_done"] = True
        rid = opaque(p["response_id"]) if isinstance(p.get("response_id"), str) else None
        if rid and rid in st["recent"]:
            return
        if rid:
            st["recent"] = (st["recent"] + [rid])[-SPEND_RECENT:]
        u = p.get("usage") if isinstance(p.get("usage"), dict) else {}
        inp, cached = spend_count(u.get("input_tokens")), spend_count(u.get("cached_input_tokens"))
        st["last_input"] = inp or st["last_input"]
        agg["context"] = inp  # Codex input includes cached tokens; do not count them twice.
        agg["records"] = True
        spend_add(agg, "m", minute, st["model"], (0, max(0, inp - cached), cached, spend_count(u.get("cache_write_input_tokens")),
                                                   spend_count(u.get("output_tokens")),
                                                   spend_count(u.get("reasoning_output_tokens")), 0))
        return
    if kind == "event_msg" and ptype == "token_count" and isinstance(p.get("info"), dict):
        st["prompt_done"] = True
        info = p["info"]
        total = info.get("total_token_usage") if isinstance(info.get("total_token_usage"), dict) else None
        last = info.get("last_token_usage") if isinstance(info.get("last_token_usage"), dict) else {}
        if "input_tokens" in last:
            agg["context"] = spend_count(last.get("input_tokens"))
        st["last_input"] = spend_count(last.get("input_tokens")) or st["last_input"]
        if not total:
            return
        keys = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
        now = [spend_count(total.get(k)) for k in keys]
        # Cumulative, and a fork's first counter already holds its parent's total: what this session used
        # starts at its first counter, less that counter's own last response.
        prev = st["prev"] or [max(0, a - spend_count(last.get(k))) for a, k in zip(now, keys)]
        d = [max(0, a - b) for a, b in zip(now, prev)]
        st["prev"] = [max(a, b) for a, b in zip(now, prev)]
        spend_add(agg, "mc", minute, st["model"], (0, max(0, d[0] - d[1]), d[1], 0, d[2], d[3], 0))


def spend_read(agg, fh, budget=None):
    """Read what was appended since the last read. Only whole lines: one being written is read next time.
    A compressed transcript is finished: it is read whole, in one go."""
    packed = isinstance(fh, _m_history.Decompressed)
    deadline = time.monotonic() + budget if budget and not packed else None
    line_fn = spend_codex_line if agg["runtime"] == "codex" else spend_claude_line
    st = agg["state"]
    if not packed:
        fh.seek(st["offset"])
    for raw in fh:
        if not raw.endswith(b"\n"):
            break
        st["offset"] += len(raw)
        line = raw.strip()
        if line.startswith(b"{"):
            at = spend_stamp(line)
            st["at"] = at
            if at is not None:
                agg["first_at"] = at if agg.get("first_at") is None else min(agg["first_at"], at)
                agg["last_at"] = at if agg.get("last_at") is None else max(agg["last_at"], at)
            line_fn(agg, line, int(at) // 60 if at is not None else None, at)
        if deadline and time.monotonic() > deadline:
            return False
    return True


SPEND_UNKEPT = ("path", "cwd", "id", "parent", "fork_of")  # known again at each read, never written
_SPEND_MEM = {}  # this process's aggregates, by real path: a file that hasn't grown isn't read again
_SPEND_TERMS = {}


def spend_terms(inst):
    """This brain's never-name words, looked up again at least once a minute (the live view runs for hours)."""
    hit = _SPEND_TERMS.get(str(inst.root))
    if not hit or time.monotonic() - hit[0] > 60:
        hit = _SPEND_TERMS[str(inst.root)] = (time.monotonic(), brain_terms(inst))
    return hit[1]


def brain_terms(inst):
    """Every never-name word of this brain: what machine-wide figures may never show or keep."""
    return list({t for n in inst.nodes() for t in _m_guard.never_terms(n)})


# A dashboard may aggregate in memory, but viewing it must never publish spend caches.
_SPEND_READ_ONLY = ContextVar('brain_spend_read_only', default=False)


@contextmanager
def spend_read_only():
    token = _SPEND_READ_ONLY.set(True)
    try:
        yield
    finally:
        _SPEND_READ_ONLY.reset(token)


def spend_cache_dir(inst):
    return inst.root / ".brain" / "spend"


def claude_session_id(path):
    """Claude's live session id is its file name, even with copied leading records."""
    return Path(path).name.removesuffix(".zst").removesuffix(".jsonl")


def spend_file(inst, path, runtime, allowed, forbidden, budget=None, window=None):
    """One transcript's aggregate, brought up to date; None when unreadable or outside the folders this brain may
    read (checked on the open file, as `brain history` does). A file that shrank or was replaced is read again.
    With a window ([lo, hi] seconds), only what happened inside it, to the second; cached by exact window."""
    try:
        real = Path(path).resolve()
    except (OSError, RuntimeError):
        return None
    fh, ident = _m_history.transcript_open(real, allowed, forbidden)
    if not fh:
        return None
    with fh:
        try:
            st = os.fstat(fh.fileno())
            size = st.st_size
            stamp = [size, st.st_mtime_ns, st.st_ctime_ns, st.st_mode, st.st_nlink]
        except OSError:
            return None
        packed = isinstance(fh, _m_history.Decompressed)
        mem = (str(inst.root.resolve()), str(real), runtime, tuple(window) if window else None)
        known = _SPEND_MEM.get(mem)
        terms = spend_terms(inst)
        if known and known[0] == (stamp, list(ident)) and known[1].get("complete", True) and \
                not any(_m_guard.find_terms(m, terms) for m in known[1]["models"]) and \
                (_SPEND_READ_ONLY.get() or known[1].get("published")):
            return known[1]  # unchanged, and its names still pass today's never-name words
        identity = str(real) if window is None else str(real) + ':' + repr(tuple(window))
        key = hashlib.sha256(identity.encode()).hexdigest()[:32]
        cf = spend_cache_dir(inst) / "files" / f"{key}.json"
        # Read-only viewers retain partial progress in memory; a disk cache is optional.
        agg = known[1] if known and known[0][1] == list(ident) else None
        if agg is None:
            try:
                agg = json.loads(cf.read_text())
            except (OSError, ValueError):
                pass
        if not (isinstance(agg, dict) and agg.get("v") == SPEND_VERSION and agg.get("key") == key
                and agg.get("ino") == list(ident) and agg.get('runtime') == runtime
                and agg["state"]["offset"] <= size
                and (agg.get('stamp') == stamp or (isinstance(agg.get('stamp'), list)
                     and agg['stamp'][0] < size))
                and (not packed or agg["state"]["offset"] in (0, size))):
            agg = {**spend_new(runtime, real), "key": key, "ino": list(ident)}
            if window is not None:
                agg["state"].update(lo=window[0], hi=window[1])
        # Never-name words may have been added since the cache was written: names are screened at every read,
        # and a cache holding one is written again.
        screened = [m if not _m_guard.find_terms(m, terms) else "other" for m in agg["models"]]
        stale, agg["models"] = screened != agg["models"], screened
        # Who the session is, never kept on disk: Claude names a session's file by its id, its sub-agents' files
        # sit below it; a Codex transcript says so in its first line.
        if runtime == "claude":
            sub = real.parent.name == "subagents"
            agg.update(id=f"{real.parent.parent.name}/{claude_session_id(real)}" if sub else claude_session_id(real), subagent=sub,
                       parent=real.parent.parent.name if sub else "")
        elif not agg.get("id"):
            meta = codex_meta(real, allowed, forbidden)
            if meta:
                agg.update(id=meta[0], parent=meta[2])
        dirty = agg.get("stamp") != stamp or not agg.get("published")
        if agg["state"]["offset"] < size or stale or dirty:
            if dirty or stale:
                agg["published"] = False
            try:
                if agg["state"]["offset"] < size:
                    agg["complete"] = spend_read(agg, fh, budget)
            except OSError:
                return None
            if packed:
                agg["state"]["offset"] = size  # read whole: what it holds is counted
            agg["models"] = [m if not _m_guard.find_terms(m, terms) else "other" for m in agg["models"]]
            agg["stamp"] = stamp
            try:
                if not _SPEND_READ_ONLY.get():  # explicit accounting may persist windowed aggregates too
                    agg["published"] = True
                    cf.parent.mkdir(parents=True, exist_ok=True)
                    _m_common.atomic_write(cf, json.dumps({k: v for k, v in agg.items() if k not in SPEND_UNKEPT},
                                                separators=(",", ":")))
            except OSError:
                agg["published"] = False  # still right for this run; publish on the next explicit read
        # A time slice can end exactly after the final whole line. There is no next
        # read to mark EOF in that case; the saved cursor already proves completion.
        if agg['state']['offset'] == size:
            agg['complete'] = True
        agg["path"] = str(real)  # this run's own, never written
        _SPEND_MEM[mem] = ((stamp, list(ident)), agg)
    return agg


_UI_DISCOVERY = ContextVar('brain_ui_session_discovery', default=None)


def ui_discovery(fn):
    """Share one read-only session discovery across a dashboard's job-usage batch."""
    @functools.wraps(fn)
    def read(*args):
        memo = _UI_DISCOVERY.get()
        if memo is None:
            return fn(*args)
        key = (fn.__name__, tuple(frozenset(arg) if isinstance(arg, set) else arg for arg in args))
        if key not in memo:
            value = fn(*args)
            memo[key] = tuple(value) if fn.__name__ == 'spend_walk' else value
        return memo[key]
    return read


@ui_discovery
def spend_sources(inst):
    """[(runtime, config folder)] of every subscription this brain may read, and the folders it may not."""
    try:
        sources, _, forbidden = _m_history.history_sources(inst)
    except SystemExit:
        return [], set(), set()
    allowed = {Path(s["config_dir"]).resolve() for s in sources}
    return [(s["runtime"], Path(s["config_dir"])) for s in sources], allowed, forbidden


def claude_folder(path):
    """The folder Claude Code keeps a working folder's sessions in: every character but letters and digits
    becomes a dash; very long names are cut, with a suffix of their own."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


@ui_discovery
def spend_walk(folder):
    """Transcripts below a folder, without following links into loops; each real file once."""
    seen = set()
    for top, dirs, files in os.walk(folder, followlinks=False):
        for f in files:
            if f.endswith(".jsonl") or f.endswith(".jsonl.zst"):
                p = Path(top) / f
                try:
                    real = p.resolve()
                except (OSError, RuntimeError):
                    continue
                if real not in seen:
                    seen.add(real)
                    yield real


def spend_once(aggs):
    """Each session once: Codex may keep a session both as a transcript and as its compressed copy."""
    best = {}
    for a in aggs:
        key = (a["runtime"], a["id"] or a["path"])
        if key not in best or (a["last"] or 0, a["state"]["offset"]) > (best[key]["last"] or 0, best[key]["state"]["offset"]):
            best[key] = a
    return list(best.values())


def spend_sum(aggs, since=None, until=None):
    """Totals over sessions, counting only minutes in [since, until] (epoch seconds)."""
    aggs = spend_once(aggs)
    lo = since // 60 if since else None
    hi = until // 60 if until else None
    out = {k: 0 for k in SPEND_TOKENS}
    out.update(tools=0, compactions=0, auto_compactions=0, sessions=0, subagents=0, sub_tokens=0, agent_minutes=0,
               models={}, runtimes={}, minutes=set(), largest_compaction=0)
    for agg in aggs:
        mins, used = set(), 0
        rows = list(agg["m"].items())
        token_rows = agg["mc"].items() if agg["runtime"] == "codex" and not agg["records"] else rows
        for key, b in rows:
            minute = int(key.split(":")[0])
            if (lo is None or minute >= lo) and (hi is None or minute <= hi):
                if b[0]:
                    mins.add(minute)
                out["tools"] += b[6]
        for key, b in token_rows:
            minute, model = (int(x) for x in key.split(":"))
            if (lo is None or minute >= lo) and (hi is None or minute <= hi):
                tokens = dict(zip(SPEND_FIELDS, b))
                for k in SPEND_TOKENS:
                    out[k] += tokens[k]
                n = sum(tokens[k] for k in SPEND_TOKENS if k != "reasoning")
                used += n
                if n:
                    name = f"{agg['runtime']}:{agg['models'][model] if 0 <= model < len(agg['models']) else 'unknown'}"
                    out["models"][name] = out["models"].get(name, 0) + n
        if not mins and not used:
            continue
        out["sessions"] += 1
        out["runtimes"][agg["runtime"]] = out["runtimes"].get(agg["runtime"], 0) + used
        if agg["subagent"]:
            out["subagents"] += 1
            out["sub_tokens"] += used
        for c in agg["compactions"]:
            if c[0] is not None and (lo is None or c[0] >= lo) and (hi is None or c[0] <= hi):
                out["compactions"] += 1
                out["auto_compactions"] += c[2] is True
                out["largest_compaction"] = max(out["largest_compaction"], c[1] or 0)
        out["agent_minutes"] += spend_active(mins)
        out["minutes"] |= mins
    out["processed"] = out["fresh"] + out["cache_read"] + out["cache_write"] + out["output"]
    out["sub_share"] = round(out["sub_tokens"] / out["processed"], 3) if out["processed"] else 0
    out["active_minutes"] = spend_active(out.pop("minutes"))
    return out


def spend_active(minutes):
    """Minutes of activity: each minute with an event, plus the gaps of at most SPEND_IDLE_MIN between them."""
    total, prev = 0, None
    for m in sorted(minutes):
        total += 1 if prev is None or m - prev > SPEND_IDLE_MIN else m - prev
        prev = m
    return total


@ui_discovery
def spend_inside(cwd, base):
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        return False  # a transcript without its own folder is in no place (never the command's own)
    try:
        c, b = Path(cwd).resolve(), Path(base).resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return False
    return c == b or b in c.parents


SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{7,79}")


def spend_session_ids():
    """The calling session's own id, as its runtime tells its shells (Claude Code and Codex)."""
    out = []
    for var in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"):
        v = os.environ.get(var, "")
        if SESSION_ID.fullmatch(v):
            out.append(v)
    return out


@ui_discovery
def codex_meta(path, allowed, forbidden):
    """(id, cwd, parent thread) from a Codex transcript's first line, without reading the rest."""
    fh, _ = _m_history.transcript_open(path, allowed, forbidden)
    if not fh:
        return None
    with fh:
        try:
            r = json.loads(fh.readline(1 << 20))
        except (OSError, ValueError):
            return None
    p = r.get("payload") if isinstance(r, dict) and r.get("type") == "session_meta" and \
        isinstance(r.get("payload"), dict) else None
    if p is None:
        return None
    src = p.get("source")
    spawn = src.get("subagent", {}).get("thread_spawn", {}) if isinstance(src, dict) and \
        isinstance(src.get("subagent"), dict) else {}
    return _m_history.history_str(p.get("id")), _m_history.history_str(p.get("cwd")), \
        _m_history.history_str(spawn.get("parent_thread_id")) if isinstance(spawn, dict) else ""


@ui_discovery
def claude_mark(path, allowed, forbidden):
    """The job a Claude session's first message (its brief) names, without reading the rest: "" for none."""
    fh, _ = _m_history.transcript_open(path, allowed, forbidden)
    if not fh:
        return ""
    with fh:
        for k, raw in enumerate(fh):
            if k > 500:
                break
            if b'"user"' not in raw:
                continue
            try:
                r = json.loads(raw)
            except ValueError:
                continue
            msg = r.get("message") if isinstance(r, dict) and isinstance(r.get("message"), dict) else {}
            if r.get("type") != "user" or r.get("isMeta") or r.get("isCompactSummary") or r.get("isSidechain"):
                continue
            content = msg.get("content")
            if isinstance(content, list) and any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
                continue
            m = JOB_MARK_RE.search(raw)
            return m.group(1).decode() if m else ""
    return ""


@ui_discovery
def claude_cwd(path, allowed, forbidden):
    """The folder a Claude session ran in, from its first records (read fresh: never kept)."""
    fh, _ = _m_history.transcript_open(path, allowed, forbidden)
    s = _m_history.history_head(Path(path), "claude", fh) if fh else None
    return s["cwd"] if s else ""


@ui_discovery
def codex_mark(path, allowed, forbidden):
    """The job a Codex session's opening user messages (its brief) name: "" for none. Stops at its first reply."""
    fh, _ = _m_history.transcript_open(path, allowed, forbidden)
    if not fh:
        return ""
    with fh:
        for k, raw in enumerate(fh):
            if k > 400 or b'"token_usage_record"' in raw or b'"token_count"' in raw:
                break
            if b'"user' not in raw:
                continue
            try:
                r = json.loads(raw)
            except ValueError:
                continue
            p = r.get("payload") if isinstance(r, dict) and isinstance(r.get("payload"), dict) else {}
            if (r.get("type") == "response_item" and p.get("type") == "message" and p.get("role") == "user") or \
                    (r.get("type") == "event_msg" and p.get("type") == "user_message"):
                m = JOB_MARK_RE.search(raw)
                if m:
                    return m.group(1).decode()
    return ""


def job_window(inst, row):
    a = inst.parse_time(row.get("Started", ""))
    b = inst.parse_time(row.get("Ended", "")) if row.get("Ended") else None
    return (a.timestamp() if a else None), (b.timestamp() if b else time.time())


def job_sessions(inst, n, row, worktree=None):
    """The sessions that did this job, and the sub-agents they started: begun in its worktree, given a brief that
    names it (a reviewer runs in its coordinator's folder), or recorded by its hand-back (a worker elsewhere).
    Only transcripts touched while the job was open are looked at, and only these are read in full."""
    start, end = job_window(inst, row)
    if start is None:
        return []
    key = _m_jobs.job_key(row)
    w = worktree or (n["local"].get("worktrees", {}).get(row["Job"]) or {}).get("path")
    wt = Path(w) if w else None
    hb = _m_jobs.read_handback(n, row) or {}
    ids = {i for i in hb.get("sessions", []) if isinstance(i, str) and SESSION_ID.fullmatch(i)} \
        if isinstance(hb.get("sessions"), list) else set()
    places = [p for p in (wt, n["dir"], inst.root) if p]
    sources, allowed, forbidden = spend_sources(inst)
    touched = lambda p: p.stat().st_mtime >= start - 60  # noqa: E731
    aggs, files, metas, in_wt = [], set(), [], set()

    def take(runtime, p):
        try:
            if p.resolve() in files or not touched(p):
                return None
        except (OSError, RuntimeError):
            return None
        files.add(p.resolve())
        agg = spend_file(inst, p, runtime, allowed, forbidden)
        if agg:
            aggs.append(agg)
        return agg

    for runtime, cfg in sources:
        if runtime == "claude":
            proj = cfg / "projects"
            names = {}  # Claude's folder name for each place -> (that place, is it the worktree)
            for p in places:
                for q in {p, p.resolve()}:
                    names.setdefault(claude_folder(q), (p, p == wt))
            for name, (place, here) in names.items():
                # A long name is cut, with a suffix: every folder sharing the cut is looked at.
                folders = [proj / name] if len(name) <= 200 else list(proj.glob(name[:200] + "*"))
                for folder in folders:
                    for f in sorted(folder.glob("*.jsonl")) if folder.is_dir() else []:
                        # In the worktree, every session is the job's. In a coordinator's folder only those its
                        # brief names: a long coordinator session is never read for this.
                        try:
                            # Claude's folder name stands for more than one path (app-work, app_work, a cut long
                            # one): only a session that really ran there counts.
                            if not touched(f) or not spend_inside(claude_cwd(f, allowed, forbidden), place):
                                continue
                            if here:
                                in_wt.add(str(f.resolve()))
                            if here or (touched(f) and claude_mark(f, allowed, forbidden) == key):
                                take("claude", f)
                        except OSError:
                            pass
            for i in ids:
                for f in proj.glob(f"*/{i}.jsonl"):
                    take("claude", f)
        else:
            # Every Codex transcript written to since the job started, wherever its day folder: a resumed session
            # keeps writing to the file of the day it began.
            mine = []
            for root in (cfg / "sessions", cfg / "archived_sessions"):
                for f in spend_walk(root) if root.is_dir() else []:
                    try:
                        if f.name.startswith("rollout-") and touched(f):
                            m = codex_meta(f, allowed, forbidden)
                            if m:
                                mine.append((f, *m))
                    except OSError:
                        pass
            for f, sid, cwd, parent in mine:
                if wt and spend_inside(cwd, wt):
                    in_wt.add(str(f.resolve()))
                if sid in ids or (wt and spend_inside(cwd, wt)) or (
                        any(spend_inside(cwd, p) for p in places) and codex_mark(f, allowed, forbidden) == key):
                    take("codex", f)
            metas += mine
    keep = [a for a in aggs if not a["subagent"] and (a["path"] in in_wt or a["job"] == key or a["id"] in ids)]
    chosen = {a["id"] for a in keep}
    for a in keep:  # Claude keeps a session's sub-agents beside it
        if a["runtime"] == "claude":
            for f in sorted((Path(a["path"]).parent / a["id"] / "subagents").glob("agent-*.jsonl")):
                agg = take("claude", f) if f.resolve() not in files else next(
                    (x for x in aggs if x["path"] == str(f.resolve())), None)
                if agg and agg["id"] not in chosen:
                    keep.append(agg)
                    chosen.add(agg["id"])
    grew = True
    while grew:  # Codex sub-agents are sessions of their own that name their parent, at any depth
        grew = False
        for f, sid, cwd, parent in metas:
            if parent in chosen and sid not in chosen:
                agg = take("codex", f) or next((x for x in aggs if x["path"] == str(f.resolve())), None)
                if agg:
                    keep.append(agg)
                    chosen.add(sid)
                    grew = True
    return keep


def job_usage(inst, n, row, worktree=None):
    """What a job cost, counted only while it was open: tokens by kind, active time, compactions, sub-agents,
    tool calls and the models that ran it."""
    signature = hashlib.sha256(json.dumps(job_usage_signature(inst, n, row),
                                          sort_keys=True).encode()).hexdigest()
    key = _m_jobs.digest([n['id'], _m_jobs.job_key(row), str(worktree or '')])
    cache = spend_cache_dir(inst) / 'jobs' / (key + '.json')
    try:
        saved = json.loads(cache.read_text())
        used = saved.get('usage')
        if (saved.get('version') == SPEND_VERSION and saved.get('signature') == signature
                and isinstance(used, dict) and isinstance(used.get('models'), dict)
                and all(type(used.get(k)) is int and used[k] >= 0
                        for k in (*SPEND_TOKENS, 'processed', 'compactions', 'sessions',
                                  'tools', 'subagents', 'active_minutes', 'agent_minutes'))):
            used['models'] = spend_models(used['models'], spend_terms(inst))
            return used
    except (OSError, ValueError, AttributeError):
        pass
    start, end = job_window(inst, row)
    if start is None:
        return None
    _, allowed, forbidden = spend_sources(inst)
    hi = end if row.get("Ended") else float("inf")
    exact = []
    for a in job_sessions(inst, n, row, worktree):
        if (a.get("first_at") is not None and a["first_at"] < start) or (a.get("last_at") or 0) > hi:
            # The session ran before the job began (a reused worktree), or after it ended: read again, counting
            # only what happened while the job was open, to the second.
            w = spend_file(inst, Path(a["path"]), a["runtime"], allowed, forbidden, window=(start, hi))
            a = {**w, "subagent": a["subagent"]} if w else a
        exact.append(a)
    u = spend_sum(exact, start, end)
    u["source"] = f"transcripts: {count_of(u['sessions'], 'session')}" if u["sessions"] else "none found"
    if not _SPEND_READ_ONLY.get():
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            _m_common.atomic_write(cache, json.dumps({'version': SPEND_VERSION,
                                   'signature': signature, 'usage': u}, separators=(',', ':')))
        except OSError:
            pass  # Accounting is useful even when the optional private cache is unwritable.
    return u


def count_of(k, word):
    return f"{k} {word}{'' if k == 1 else 's'}"


def tokens_text(v):
    v = int(v or 0)
    for unit, size in (("B", 10 ** 9), ("M", 10 ** 6), ("k", 10 ** 3)):
        if v >= size:
            return f"{v / size:.3g}{unit}"
    return str(v)


def tokens_value(text):
    """'2M', '1.5k', '40000': a number of tokens, or None."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kKmMbB]?)\s*", str(text or ""))
    return int(float(m[1]) * {"": 1, "k": 10 ** 3, "m": 10 ** 6, "b": 10 ** 9}[m[2].lower()]) if m else None


def usage_cells(u, terms=()):
    """The ledger's usage columns for a job."""
    if not u:
        return {}
    models = sorted(spend_models(u["models"], terms).items(), key=lambda kv: -kv[1])
    total = sum(v for _, v in models) or 1
    return {"Active h": f"{u['active_minutes'] / 60:.1f}", "Agent h": f"{u['agent_minutes'] / 60:.1f}",
            "Fresh in": str(u["fresh"]), "Cache read": str(u["cache_read"]), "Cache write": str(u["cache_write"]),
            "Output": str(u["output"]), "Reasoning": str(u["reasoning"]), "Tool calls": str(u["tools"]),
            "Compactions": str(u["compactions"]), "Sub-agents": str(u["subagents"]),
            "Sub share": f"{round(100 * u['sub_share'])}%",
            "Models": ", ".join(f"{k} {round(100 * v / total)}%" for k, v in models if v) or "",
            "Usage": u["source"]}


def row_usage(row):
    """A ledger row's usage, read back from its cells; None for jobs measured before the ledger had them."""
    if not row.get("Usage"):
        return None
    num = lambda k: int(row[k]) if str(row.get(k, "")).isdigit() else 0  # noqa: E731
    try:
        active, agent = float(row.get("Active h") or 0), float(row.get("Agent h") or 0)
    except ValueError:
        active = agent = 0.0
    out = {k: num(c) for k, c in (("fresh", "Fresh in"), ("cache_read", "Cache read"), ("cache_write", "Cache write"),
                                  ("output", "Output"), ("reasoning", "Reasoning"), ("tools", "Tool calls"),
                                  ("compactions", "Compactions"), ("subagents", "Sub-agents"))}
    out["processed"] = out["fresh"] + out["cache_read"] + out["cache_write"] + out["output"]
    share = re.fullmatch(r"(\d+)%", row.get("Sub share", "") or "")
    out.update(active_h=active, agent_h=agent, sub_share=int(share[1]) / 100 if share else 0, source=row["Usage"])
    return out


def contract_budget(text):
    """A contract's optional ## Budget: tokens processed and hours, e.g. "20M tokens, 3 h"."""
    body = {k.lower(): v for k, v in _m_ui.md_sections(text or "").items()}.get("budget", "")
    t = re.search(r"(\d+(?:\.\d+)?\s*[kKmMbB]?)\s*tokens", body)
    h = re.search(r"(\d+(?:\.\d+)?)\s*(?:h|hours?)\b", body)
    out = {"tokens": tokens_value(t[1]) if t else None, "hours": float(h[1]) if h else None}
    return out if out["tokens"] or out["hours"] else None


def job_budget(n, row):
    f = _m_jobs.contract_file(n, row)
    try:
        return contract_budget(f.read_text()) if f.is_file() else None
    except OSError:
        return None


def budget_text(b):
    return " · ".join(filter(None, [f"{tokens_text(b['tokens'])} tokens" if b.get("tokens") else "",
                                    f"{b['hours']:g} h" if b.get("hours") else ""]))


def job_round_usage(inst, n, row, usage):
    """Budget counters since the last delivered restart; lifetime totals remain untouched.

    Missing counters stay unknown. Legacy receipts without active minutes use elapsed
    time since their acknowledgement instead of charging the previous worker's hours.
    """
    from . import reminders as _m_reminders
    key = _m_jobs.digest([n['id'], _m_jobs.job_key(row)])
    with _m_reminders.lifecycle_store(inst, readonly=True) as data:
        record = data['usage'].get(key)
    if record is None:
        return None
    if not isinstance(record, dict) or record.get('job') != _m_jobs.job_key(row):
        raise ValueError('invalid round usage receipt')
    baseline = record.get('baseline')
    if not isinstance(baseline, dict) or any(type(baseline.get(k)) is not int or baseline[k] < 0
                                           for k in ('processed', 'compactions')):
        raise ValueError('invalid round usage baseline')
    # A pending initial restart has not yet started a replacement round. When a
    # later round crosses its threshold its acknowledgement time must survive.
    since = record.get('round_at') or (record.get('at') if not record.get('episode') else None)
    if since is None:
        return None
    at = inst.parse_time(since)
    if not at or at > inst.now_dt():
        raise ValueError('invalid round usage baseline time')
    out = {'since': since}
    for k in ('processed', 'compactions', 'active_minutes'):
        base, value = baseline.get(k), (usage or {}).get(k)
        if base is not None and (type(base) is not int or base < 0):
            raise ValueError('invalid round usage baseline')
        if type(value) is not int or value < 0 or base is None:
            out[k] = None
        elif value < base:
            raise ValueError('round usage counters regressed; retain baseline for review')
        else:
            out[k] = value - base
    out['hours'] = (out['active_minutes'] / 60 if out['active_minutes'] is not None
                    else (inst.now_dt() - at).total_seconds() / 3600)
    return out


def usage_line(u, budget=None, hours=None, round_usage=None):
    """One line: what a job has cost so far, and its budget."""
    if not u or not u.get("sessions") and not u.get("source", "").startswith("host"):
        return "usage: no transcripts found for this job on this machine"
    out = (f"usage: {tokens_text(u['processed'])} tokens processed (fresh in {tokens_text(u['fresh'])}, cache read "
           f"{tokens_text(u['cache_read'])}, cache write {tokens_text(u['cache_write'])}, out {tokens_text(u['output'])}"
           + (f", reasoning {tokens_text(u['reasoning'])}" if u.get("reasoning") else "") + ")"
           + f"; active {u['active_minutes'] / 60:.1f} h (agents {u['agent_minutes'] / 60:.1f} h)"
           + f"; {count_of(u['tools'], 'tool call')}; {count_of(u['compactions'], 'compaction')}"
           + (f"; {count_of(u['subagents'], 'sub-agent')}, {round(100 * u['sub_share'])}% of tokens" if u["subagents"] else ""))
    if round_usage is not None:
        shown = lambda k: tokens_text(round_usage[k]) if round_usage.get(k) is not None else 'unknown'
        out += f"; since restart: {shown('processed')} tokens, {shown('compactions')} compactions"
        hours = round_usage['hours']
    meter = round_usage if round_usage is not None else u
    if budget:
        over = [f"{tokens_text(meter['processed'])} of {tokens_text(budget['tokens'])} tokens"
                for _ in [0] if budget.get("tokens") and meter.get("processed") is not None
                and meter["processed"] > budget["tokens"]]
        over += [f"{hours:.1f} of {budget['hours']:g} h" for _ in [0]
                 if budget.get("hours") and hours is not None and hours > budget["hours"]]
        out += f"; budget {budget_text(budget)}" + (": over (" + ", ".join(over) + ")" if over else "")
    return out


_LIVE = {}


def host_row_usage(row):
    """A ledger row's usage as its host reported it, in the shape transcript totals take."""
    u = row_usage(row) or {}
    models = {}
    for part in (row.get("Models") or "").split(", "):
        m = re.fullmatch(r"(\S+) (\d+)%", part.strip())
        if m:
            models[m[1]] = u.get("processed", 0) * int(m[2]) // 100
    sub = round(u.get("processed", 0) * u.get("sub_share", 0))
    return {**{k: u.get(k, 0) for k in (*SPEND_TOKENS, "processed", "tools", "compactions", "subagents")},
            "auto_compactions": 0, "sessions": 1, "sub_share": u.get("sub_share", 0), "sub_tokens": sub,
            "models": models, "runtimes": {}, "largest_compaction": 0, "source": row.get("Usage", "host"),
            "active_minutes": round(u.get("active_h", 0) * 60), "agent_minutes": round(u.get("agent_h", 0) * 60)}


_UI_USAGE = ContextVar('brain_ui_live_usage', default=None)


@contextmanager
def live_usage_snapshot(values, revision):
    """Keep unchanged UI job totals even after the ordinary CLI's one-minute cache expires."""
    token = _UI_USAGE.set((values, revision))
    discovery = _UI_DISCOVERY.set({})
    try:
        yield
    finally:
        _UI_USAGE.reset(token)
        _UI_DISCOVERY.reset(discovery)


def live_usage(inst, n, row):
    scope = _UI_USAGE.get()
    if scope is None:
        return _live_usage(inst, n, row)
    values, revision = scope
    job = (str(inst.root), n['id'], _m_jobs.job_key(row))
    key = (revision, job_usage_signature(inst, n, row))
    old = values.get(job)
    if old is None or old[0] != key:
        values[job] = (key, _read_live_usage(inst, n, row))
    return values[job][1]


@ui_discovery
def spend_input_signature(inst):
    """Cheap metadata revision shared by all jobs in one dashboard snapshot.

    This reads no transcript body. Permission roots and file identity participate so
    a caller's unchanged HEAD cannot mask appended, replaced or newly forbidden data.
    """
    sources, allowed, forbidden = spend_sources(inst)
    paths = []
    for runtime, cfg in sources:
        for folder in ((cfg / 'projects',) if runtime == 'claude' else
                       (cfg / 'sessions', cfg / 'archived_sessions')):
            for path in spend_walk(folder):
                if not _m_history.history_within(path, allowed) or _m_history.history_within(path, forbidden):
                    continue
                try:
                    st = path.stat()
                    paths.append((str(path), st.st_dev, st.st_ino, st.st_size,
                                  st.st_mtime_ns, st.st_ctime_ns, st.st_mode, st.st_nlink))
                except OSError:
                    paths.append((str(path), None))
    return (tuple((runtime, str(cfg)) for runtime, cfg in sources),
            tuple(sorted(str(p) for p in allowed)), tuple(sorted(str(p) for p in forbidden)),
            tuple(sorted(paths)))


def job_usage_signature(inst, n, row):
    try:
        st = _m_jobs.handback_file(n, row).stat()
        handback = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    except OSError:
        handback = None
    return (spend_input_signature(inst), handback, json.dumps(n['local'], sort_keys=True),
            tuple(_m_guard.never_terms(n)), tuple(row.get(name, '') for name in _m_jobs.JOB_COLS))


def _live_usage(inst, n, row):
    """An open job's usage so far, refreshed when transcript or permission metadata changes.
    What its host reported wins over what the transcripts show."""
    if str(row.get("Usage", "")).startswith("host"):
        return host_row_usage(row)
    key = (str(inst.root), n["id"], _m_jobs.job_key(row))
    signature = job_usage_signature(inst, n, row)
    hit = _LIVE.get(key)
    if hit and hit[0] == signature:
        return hit[1]
    u = _read_live_usage(inst, n, row)
    _LIVE[key] = (signature, u)
    return u


def _read_live_usage(inst, n, row):
    if str(row.get('Usage', '')).startswith('host'):
        return host_row_usage(row)
    try:
        return job_usage(inst, n, row)
    except (OSError, ValueError, RuntimeError):
        return None


def host_usage_cells(path, n):
    """Usage a host reports for a job, as JSON with the counts `brain jobs` keeps (unknown keys are refused)."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        _m_common.die(f"can't read {path}: {e}")
    known = {"fresh", "cache_read", "cache_write", "output", "reasoning", "tools", "compactions", "subagents",
             "sub_share", "active_minutes", "agent_minutes", "models", "host", "meter_percent"}
    if not isinstance(data, dict) or set(data) - known or not isinstance(data.get("host", ""), str):
        _m_common.die("host usage is a JSON object with: " + ", ".join(sorted(known)))
    meter = _m_jobs.forecast_number(data.get("meter_percent"))
    if "meter_percent" in data and (meter is None or meter > 100):
        _m_common.die("meter_percent is an attributed job share from 0 to 100, never an account-wide delta")
    nums = {k: data.get(k, 0) for k in known - {"models", "host", "sub_share", "meter_percent"}}
    if any(type(v) is not int or v < 0 for v in nums.values()) or \
            not (isinstance(data.get("sub_share", 0), (int, float)) and 0 <= data.get("sub_share", 0) <= 1) or \
            not isinstance(data.get("models", {}), dict) or \
            any(not isinstance(k, str) or not SPEND_MODEL.fullmatch(k) or type(v) is not int or v < 0
                for k, v in data.get("models", {}).items()):
        _m_common.die("host usage counts are whole numbers, sub_share a fraction from 0 to 1, models {model id: tokens}")
    u = {**nums, "sub_share": data.get("sub_share", 0), "models": data.get("models", {}), "sessions": 1}
    u["processed"] = u["fresh"] + u["cache_read"] + u["cache_write"] + u["output"]
    label = " ".join(data.get("host", "").split())[:40]
    if _m_nodes.SECRET_LINE.search(label):
        _m_common.die("unsafe host label")
    if _m_guard.find_terms(" ".join([label, *u["models"]]), _m_guard.never_terms(n)):
        _m_common.die("the host's label or a model name holds a never-name word of this node; leave it out")
    return {**usage_cells({**u, "source": ""}), "Usage": "host" + (f": {label}" if label else ""),
            "Meter %": str(meter) if meter is not None else ""}


def usage_advice(u, median_tokens=None, round_usage=None):
    """Advice from a job's usage: never a gate."""
    out = []
    if not u:
        return out
    if median_tokens and u["processed"] > 2 * median_tokens:
        out.append(f"advice: {tokens_text(u['processed'])} tokens, over twice the median of {tokens_text(median_tokens)} "
                   "for merged jobs here")
    meter = round_usage if round_usage is not None else u
    if meter.get("compactions") is not None and meter["compactions"] >= 3:
        out.append(f"advice: {meter['compactions']} compactions: past the context window again and again; hand back and "
                   "restart from a brief with what is done so far")
    if u["subagents"] and u["sub_share"] > 0.5:
        out.append(f"advice: sub-agents spent {round(100 * u['sub_share'])}% of its tokens: give them narrower tasks, "
                   "or do the work in the main thread")
    return out



def spend_all(inst, budget=None, match=None):
    """Every transcript in the folders this brain may read (or those `match(path, head)` keeps), up to date.
    With a budget in seconds, stops early and says so: the next call goes on where this one stopped."""
    sources, allowed, forbidden = spend_sources(inst)
    deadline = time.monotonic() + budget if budget else None
    aggs, total, done, seen = [], 0, True, set()
    for runtime, cfg in sources:
        roots = [cfg / "projects"] if runtime == "claude" else [cfg / "sessions", cfg / "archived_sessions"]
        for root in roots:
            if not root.is_dir():
                continue
            for f in spend_walk(root):
                if runtime == "claude" and f.parent.name != "subagents" and f.parent.parent != root.resolve():
                    continue  # a session file sits right in its project folder; sub-agents below it
                if f in seen:
                    continue  # two subscription folders linked to one: each file counts once
                seen.add(f)
                total += 1
                if deadline and time.monotonic() > deadline:
                    done = False
                    continue
                if match and not match(runtime, f, allowed, forbidden):
                    continue
                left = (deadline - time.monotonic()) if deadline else None
                agg = spend_file(inst, f, runtime, allowed, forbidden, budget=left)
                if agg:
                    aggs.append(agg)
                    done = done and agg.get("complete", True)
    return aggs, total, done


def spend_days(inst, aggs, days=14, weeks=8):
    """Per runtime: tokens and active hours per local day and per week, newest last."""
    zone = inst.zone()
    now = datetime.datetime.now(zone) if zone else datetime.datetime.now().astimezone()
    today = now.date()
    day_keys = [(today - datetime.timedelta(days=k)).isoformat() for k in range(days - 1, -1, -1)]
    monday = today - datetime.timedelta(days=today.weekday())
    week_keys = [(monday - datetime.timedelta(weeks=k)).isoformat() for k in range(weeks - 1, -1, -1)]
    lo = int((datetime.datetime.combine(monday - datetime.timedelta(weeks=weeks - 1), datetime.time(),
                                        now.tzinfo)).timestamp()) // 60
    out = {}
    for agg in spend_once(aggs):
        rt = out.setdefault(agg["runtime"], {"days": {k: {"tokens": 0, "fresh_out": 0, "minutes": set()} for k in day_keys},
                                             "weeks": {k: {"tokens": 0, "fresh_out": 0, "minutes": set()} for k in week_keys}})
        token_rows = agg["mc"] if agg["runtime"] == "codex" and not agg["records"] else agg["m"]
        for rows, tokens in ((agg["m"], False), (token_rows, True)):
            for key, b in rows.items():
                minute = int(key.split(":")[0])
                if minute < lo:
                    continue
                d = datetime.datetime.fromtimestamp(minute * 60, now.tzinfo).date()
                for bucket in (rt["days"].get(d.isoformat()),
                               rt["weeks"].get((d - datetime.timedelta(days=d.weekday())).isoformat())):
                    if bucket is None:
                        continue
                    if tokens:
                        bucket["tokens"] += b[1] + b[2] + b[3] + b[4]
                        bucket["fresh_out"] += b[1] + b[4]
                    elif b[0]:
                        bucket["minutes"].add(minute)
    for rt in out.values():
        for part in ("days", "weeks"):
            for k, v in rt[part].items():
                v["active_h"] = round(spend_active(v.pop("minutes")) / 60, 1)
    return out


def spend_models(models, terms=()):
    """Model names as they may be shown: an id, without a never-name word (one that holds one is "other")."""
    out = {}
    for name, v in models.items():
        name = name if SPEND_MODEL.fullmatch(name) and not _m_guard.find_terms(name, terms) else "other"
        out[name] = out.get(name, 0) + v
    return out


def spend_summary(u, terms=()):
    """Totals as plain numbers, for --json and the live view."""
    keep = (*SPEND_TOKENS, "processed", "tools", "compactions", "auto_compactions", "sessions", "subagents",
            "sub_share", "runtimes", "largest_compaction")
    return {**{k: u[k] for k in keep}, "models": spend_models(u["models"], terms),
            "active_h": round(u["active_minutes"] / 60, 1), "agent_h": round(u["agent_minutes"] / 60, 1)}


_MACHINE = {}


def spend_machine(inst, budget=2.0):
    """This machine's totals per runtime, for the live view: counted a little at each refresh until complete,
    then again at most once a minute."""
    hit = _MACHINE.get(str(inst.root))
    if hit and hit["complete"] and time.monotonic() - hit["at"] < 60:
        return hit["value"]
    aggs, total, done = spend_all(inst, budget=budget)
    value = {"complete": done, "files": total, "read": len(aggs), "runtimes": spend_days(inst, aggs),
             "since_week": spend_summary(spend_sum(aggs, time.time() - 7 * 86400), brain_terms(inst)) if done else None}
    _MACHINE[str(inst.root)] = {"at": time.monotonic(), "complete": done, "value": value}
    return value


def spend_node_file(inst, n):
    return spend_cache_dir(inst) / "nodes" / f"{_m_connect.connect_name(n['id'])}.json"


def any_cwd(path, allowed, forbidden):
    """The folder a transcript's first records ran in, sub-agents' sidechain records included: "" if none."""
    fh, _ = _m_history.transcript_open(path, allowed, forbidden)
    if not fh:
        return ""
    with fh:
        for k, raw in enumerate(fh):
            if k > 200:
                break
            if b'"cwd"' not in raw:
                continue
            try:
                r = json.loads(raw)
            except ValueError:
                continue
            if isinstance(r, dict) and isinstance(r.get("cwd"), str) and r["cwd"]:
                return r["cwd"]
    return ""


def spend_lookback(inst, n, since=None, also=()):
    """Every past session about a node's home (as `brain history` finds them), as aggregates only."""
    if n["external"]:
        match_home, _ = _m_history.history_matcher(inst, n, also)
    else:
        if also:
            _m_common.die("--also requires an external git home")
        # Exact node folders keep a parent's spend from absorbing child nodes.
        match_home = lambda cwd, remote: spend_inside(cwd, n["dir"]) and Path(cwd).resolve() == n["dir"].resolve()
    recorded = n["local"].get("coordinator_sessions", [])
    coordinators = {i for i in recorded if isinstance(i, str) and SESSION_ID.fullmatch(i)} \
        if isinstance(recorded, list) else set()
    heads = {}
    coordinator_ids = {"claude": set(), "codex": set()}

    def match(runtime, f, allowed, forbidden):
        if runtime == "claude" and f.parent.name == "subagents":
            parent = f.parent.parent.name
            # Recorded identity vouches for descendants independently of cwd. A
            # session admitted only by its home still cannot vouch for a foreign cwd.
            return parent in coordinator_ids["claude"] or (
                parent in heads and bool(match_home(any_cwd(f, allowed, forbidden), "")))
        fh, why = _m_history.transcript_open(f, allowed, forbidden)
        s = _m_history.history_head(f, runtime, fh) if fh else None
        sid = claude_session_id(f) if runtime == "claude" else s["id"] if s else ""
        if not s or not (sid in coordinators or match_home(s["cwd"], s["repo_url"])):
            return False
        heads[sid] = True
        if sid in coordinators:
            coordinator_ids[runtime].add(sid)
        return True
    aggs, _, _ = spend_all(inst, match=match)
    ids = {a["id"] for a in aggs}
    sources, allowed, forbidden = spend_sources(inst)
    grew = True
    while grew:  # Codex sub-agents run where their parent did; any started elsewhere still name it
        grew = False
        for runtime, cfg in sources:
            if runtime != "codex":
                continue
            for root in (cfg / "sessions", cfg / "archived_sessions"):
                for f in spend_walk(root) if root.is_dir() else []:
                    m = codex_meta(f, allowed, forbidden)
                    if not m or m[2] not in ids:
                        continue
                    by_coordinator = m[2] in coordinator_ids["codex"]
                    if by_coordinator and m[0] not in coordinator_ids["codex"]:
                        # Propagate identity even if the child was already admitted
                        # by home, so its scratch-folder grandchildren count too.
                        coordinator_ids["codex"].add(m[0])
                        grew = True
                    if m[0] not in ids and (by_coordinator or match_home(m[1], "")):
                        agg = spend_file(inst, f, "codex", allowed, forbidden)
                        if agg:
                            aggs.append(agg)
                            ids.add(agg["id"])
                            grew = True
    lo = since or None
    sessions = []
    for a in spend_once(aggs):
        u = spend_sum([a], lo)
        if not u["sessions"]:
            continue
        sessions.append({"runtime": a["runtime"], "id": opaque(a["id"]), "subagent": a["subagent"],
                         "parent": opaque(a["parent"]) if a["parent"] else "",
                         "start": (a["first"] or 0) * 60, "end": (a["last"] or 0) * 60,
                         **spend_summary(u, _m_guard.never_terms(n))})
    total = spend_sum(aggs, lo)
    record = {"node": n["id"], "at": inst.now(), "since": lo, "total": spend_summary(total, _m_guard.never_terms(n)),
              "days": spend_days(inst, aggs, days=0, weeks=26), "sessions": sorted(sessions, key=lambda x: x["start"])}
    f = spend_node_file(inst, n)
    f.parent.mkdir(parents=True, exist_ok=True)
    _m_common.atomic_write(f, json.dumps(record, separators=(",", ":")))
    return record, aggs


def spend_lines(title, u, terms=()):
    """What a set of sessions cost, in a few lines."""
    real = u["fresh"] + u["output"]
    lines = [f"{title}: {count_of(u['sessions'], 'session')} ({count_of(u['subagents'], 'sub-agent')})",
             f"  tokens: {tokens_text(u['processed'])} processed: fresh in {tokens_text(u['fresh'])}, cache read "
             f"{tokens_text(u['cache_read'])}, cache write {tokens_text(u['cache_write'])}, out {tokens_text(u['output'])}"
             + (f" (reasoning {tokens_text(u['reasoning'])})" if u["reasoning"] else "")
             + (f"; fresh input and output are {100 * real / u['processed']:.1f}% of it" if u["processed"] else ""),
             f"  active {u['active_minutes'] / 60:.1f} h (agents {u['agent_minutes'] / 60:.1f} h)"
             f"; {count_of(u['tools'], 'tool call')}",
             f"  compactions: {u['compactions']}" + (f" ({u['compactions'] / max(1, u['sessions'] - u['subagents']):.1f} "
                                                      "per session)" if u["compactions"] else "")
             + (f"; sub-agents spent {round(100 * u['sub_share'])}% of the tokens" if u["subagents"] else "")]
    models = spend_models(u["models"], terms)
    total = sum(models.values()) or 1
    if models:
        lines.append("  models: " + ", ".join(f"{k} {round(100 * v / total)}%" for k, v in
                                              sorted(models.items(), key=lambda kv: -kv[1]) if v))
    return lines


def cmd_spend(a):
    """Time and tokens from this machine's transcripts: a node's past sessions (opt-in, like history), or every
    session here (--machine). Aggregates only, kept in .brain/spend/, never committed."""
    inst = _m_nodes.load()
    since = None
    if a.since:
        try:
            d = datetime.date.fromisoformat(a.since)
        except ValueError:
            _m_common.die("--since is a date: YYYY-MM-DD")
        zone = inst.zone()
        since = int(datetime.datetime.combine(d, datetime.time(), zone or datetime.datetime.now().astimezone().tzinfo)
                    .timestamp())
    if a.machine:
        if a.id:
            _m_common.die("--machine counts every session here: no node")
        aggs, total, _ = spend_all(inst)
        u = spend_sum(aggs, since)
        days = spend_days(inst, aggs)
        if a.json:
            print(json.dumps({"total": spend_summary(u, brain_terms(inst)), "files": total, "runtimes": days}, indent=1))
            return
        for line in spend_lines("this machine" + (f" since {a.since}" if a.since else ""), u, brain_terms(inst)):
            print(line)
        for rt, v in days.items():
            week = ", ".join(f"{k[5:]} {tokens_text(x['tokens'])}" for k, x in v["weeks"].items() if x["tokens"])
            print(f"  {rt} by week (from Monday): {week or 'nothing'}")
        return
    if not a.id:
        _m_common.die("brain spend <node> [--since DATE], or brain spend --machine")
    n = inst.node(a.id)
    if n["external"] and (not n["home_path"] or not _m_nodes.repo_root(n["home_path"])):
        _m_common.die(f"{n['id']}: spend looks back over sessions about an external git home (brain set {n['id']} home=<path>)")
    record, aggs = spend_lookback(inst, n, since, a.also)
    if a.json:
        print(_m_guard.redact(json.dumps(record, indent=1), _m_guard.never_terms(n)))
        return
    u = spend_sum(aggs, since)
    lines = spend_lines(n["id"] + (f" since {a.since}" if a.since else ""), u, _m_guard.never_terms(n))
    many = sum(1 for x in record["sessions"] if not x["subagent"] and x["compactions"] >= 3)
    if many:
        lines.append(f"  {count_of(many, 'session')} compacted 3 times or more: long runs past the window, "
                     "where a restart from a brief costs less")
    weeks = record["days"]
    for rt, v in weeks.items():
        week = ", ".join(f"{k[5:]} {tokens_text(x['tokens'])}" for k, x in v["weeks"].items() if x["tokens"])
        if week:
            lines.append(f"  {rt} by week (from Monday): {week}")
    lines.append(f"  kept in {_m_common.tilde(spend_node_file(inst, n))} (this machine only, never committed)")
    print(_m_guard.redact("\n".join(lines), _m_guard.never_terms(n)))


# ---------- coordinator accounting ----------


_COORDINATOR_FILES = {}  # selected paths per process/sid; accounting itself memoizes unchanged files


def coordinator_measure(inst, n):
    """Current coordinator only. Reject unrelated file names before any transcript open."""
    ids = n["local"].get("coordinator_sessions", [])
    sid = ids[-1] if isinstance(ids, list) and ids else None
    if not isinstance(sid, str) or not SESSION_ID.fullmatch(sid):
        return None
    sources, allowed, forbidden = spend_sources(inst)  # recheck subscription/privacy gates every time
    key = (str(inst.root), sid, tuple(sources), tuple(sorted(allowed)), tuple(sorted(forbidden)))
    cached = _COORDINATOR_FILES.get(key)
    if cached and time.monotonic() - cached[0] < 30:
        selected = cached[1]
    else:
        selected = []
        for runtime, cfg in sources:
            roots = [cfg / "projects"] if runtime == "claude" else [cfg / "sessions", cfg / "archived_sessions"]
            for root in roots:
                for path in spend_walk(root) if root.is_dir() else []:
                    if runtime == "claude":
                        if path.parent.parent != root.resolve() or claude_session_id(path) != sid:
                            continue
                    elif not path.name.removesuffix(".zst").endswith("-" + sid + ".jsonl"):
                        continue
                    selected.append((runtime, path))
        if selected:  # an absent file may appear at any moment after a fresh thread starts
            _COORDINATOR_FILES[key] = (time.monotonic(), selected)
    aggs = []
    for runtime, path in selected:
        agg = spend_file(inst, path, runtime, allowed, forbidden)
        # spend_file confirms Codex identity from metadata; never trust a file name alone.
        if agg and agg["id"] == sid and agg.get("complete", True):
            aggs.append(agg)
    if not aggs:
        return None
    a = max(spend_once(aggs), key=lambda a: a.get("last_at") or 0)
    return {"context": a.get("context"), "turns": a.get("turns", 0)}
