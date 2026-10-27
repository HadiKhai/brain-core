"""Guard mechanics for Brain."""
import contextlib
import contextvars
import hashlib
import io
import itertools
import os
import re
import shutil
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import common as _m_common
from . import jobs as _m_jobs
from . import machine as _m_machine
from . import nodes as _m_nodes
from . import status as _m_status
from . import term_cache as _m_term_cache


def trace_patterns(inst):
    pats = set(_m_common.lst(inst.cfg.get("trace_patterns")))
    for r in {str(inst.root), str(inst.root.resolve())}:
        pats.add(r)
        if r.startswith("/private/"):
            pats.add(r[len("/private"):])
        pats.add(_m_common.tilde(r))
    return {p for p in pats if p and p != "~"}


def scan_traces(inst, home, errors=None):
    """Files in `home` (tracked or untracked, not ignored) that mention anything above the boundary. What couldn't
    be checked goes to `errors` (a list), when given."""
    hits, errs = [], []
    for pat in sorted(trace_patterns(inst)):
        needle = str(Path.home()) + pat[1:] if pat.startswith("~/") else pat
        if (home / ".git").exists():
            files = []
            for e in ([needle, pat] if pat.startswith("~/") else [needle]):
                r = _m_common.git("grep", "--untracked", "-a", "-l", "-F", "-i", "-e", e, cwd=home)
                if r.returncode > 1 or r.stderr.strip():
                    errs.append((home, f"git grep: {r.stderr.strip() or 'exit ' + str(r.returncode)}"))
                files += r.stdout.split()
        else:
            files = []
            for dp, dn, fn in os.walk(home, onerror=lambda e: errs.append((Path(e.filename), e.strerror))):
                dn[:] = [x for x in dn if x not in (".git", "node_modules")]
                for f in fn:
                    p = Path(dp) / f
                    if file_mentions(p, [needle.casefold(), pat.casefold()], errors=errs):
                        files.append(str(p.relative_to(home)))
        for f in sorted(set(files)):
            hits.append((pat, f))
    if errors is not None:
        errors += sorted({f"{_m_common.tilde(w)}: {why}" for w, why in errs})
    return hits



# ---------- words a home must never contain ----------

NEVER_FILE = "never-name.txt"  # in a node folder (outside the home): one term per line; also covers descendants

_TERM_SCAN_CACHE = contextvars.ContextVar('brain_term_scan_cache', default=None)
_TERM_VERDICTS = contextvars.ContextVar('brain_term_verdicts', default=None)


@contextlib.contextmanager
def term_scan_cache(persist=False):
    """Share observations within a command; only check --fix publishes verdicts."""
    if _TERM_SCAN_CACHE.get() is not None:
        yield
        return
    token = _TERM_SCAN_CACHE.set({})
    verdicts = {}
    saved = _TERM_VERDICTS.set((verdicts, persist))
    try:
        yield
    finally:
        for cache in verdicts.values():
            cache.close()
        _TERM_VERDICTS.reset(saved)
        _TERM_SCAN_CACHE.reset(token)


def term_file_identity(value):
    # ctime catches same-size rewrites with restored mtime, chmod and hard links.
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns,
            value.st_ctime_ns, value.st_mode, value.st_uid, value.st_gid, value.st_nlink)


def term_verdict_cache(n):
    current = _TERM_VERDICTS.get()
    if current is None:
        return None
    top = n
    while top['parent']:
        top = top['parent']
    root = top['dir']
    caches, writable = current
    if root not in caches:
        caches[root] = _m_term_cache.VerdictCache(root, writable=writable)
    return caches[root]


def byte_mentions(data, needles, chunk=1 << 20, principals=(), lessons=False):
    """Reuse matching only after the complete small content has been read."""
    cache = _TERM_SCAN_CACHE.get()
    key = ('content', hashlib.sha256(data).digest(), tuple(needles), tuple(principals), lessons)
    old = cache.get(key) if cache is not None else None
    if old is not None:
        return set(old)
    found = stream_mentions(io.BytesIO(data), needles, chunk=chunk, principals=principals, lessons=lessons)
    if cache is not None:
        cache[key] = frozenset(found)
    return found


def never_terms(n):
    """[(term, label)] for a node: its own never-name.txt and its ancestors'. The label never shows the term."""
    out, m = [], n
    while m:
        f = m["dir"] / NEVER_FILE
        if f.exists():
            k = 0
            for line in f.read_text().splitlines():
                t = line.strip()
                if t and not t.startswith("#"):
                    k += 1
                    out.append((t, f"term {k} of {m['id']}/{NEVER_FILE}"))
        m = m["parent"]
    return [(t, redact(label, out)) for t, label in out]  # even a node's own id may hold one of its words


def governance_text(text, principals=(), drop=False):
    """Screen known learning frontmatter; never exempt body text or non-principal values.

    `drop` is for worker output, after screening: governance stays on disk, not in the brief.
    Callers must establish that this is a library item or specialist role, not arbitrary task text.
    """
    m = _m_common.FRONTMATTER_RE.match(text)
    if not m:
        return text
    keys = [line.split(":", 1)[0].strip() for line in m.group(1).splitlines() if ":" in line]
    if any(keys.count(key) > 1 for key in ("scope", "source_owners")):
        return text  # read_fm keeps the last value; ineffective duplicates are ordinary data.
    ids = set(principals)

    def line(match):
        if drop:
            return ""
        values = match.group(2).split(",")
        values = ["" if value.strip() in ids else value for value in values]
        return match.group(1) + ",".join(values) + match.group(3)

    header = re.sub(r"^([ \t]*(?:scope|source_owners)[ \t]*:)([^\r\n]*)(\r?\n|\r?$)", line,
                    m.group(1), flags=re.M)
    return text[:m.start(1)] + header + text[m.end(1):]


def governance_lessons(text, principals):
    """Mask only exact IDs in the scope segment that parse_lessons recognizes."""
    ids = set(principals)

    def line(match):
        values = ["" if value.strip() in ids else value for value in match.group(3).split(",")]
        return f"- {match.group(1)} · from {match.group(2)} · scope {','.join(values)}: {match.group(4)}"

    return re.sub(r"^- (.+?) · from (.+?) · scope (.+?): (.+)$", line, text, flags=re.M)


def governance_principals(n):
    while n["parent"]:
        n = n["parent"]
    return _m_common.lst(_m_common.read_fm(n["dir"] / "instance.md")[0].get("principals"))


def governance_file(n, path):
    """Only real instance library items and specialist role files get metadata treatment."""
    raw = os.fspath(path)
    folder, name = os.path.split(raw)
    if (not raw.endswith('.md') or (os.path.basename(folder) != 'library'
            and not (name in ('role.md', 'lessons.md')
                     and os.path.basename(os.path.dirname(folder)) == 'specialists'))):
        return False
    path = Path(path)
    if path.suffix != '.md':
        return False
    if path.parent.name == "library" and path.suffix == ".md":
        owner = path.parent.parent
    elif path.name in ("role.md", "lessons.md") and path.parent.parent.name == "specialists":
        owner = path.parent.parent.parent
    else:
        return False
    path = Path(os.path.abspath(path))
    owner = Path(os.path.abspath(owner))
    if path.is_symlink():
        return False
    while n["parent"]:
        n = n["parent"]
    try:
        parts = owner.relative_to(n["dir"].absolute()).parts
        relative = path.relative_to(n["dir"].absolute())
    except ValueError:
        return False
    # A real instance path cannot acquire a governance exemption through a
    # linked library/specialist directory, even when the final file is regular.
    if any(n["dir"].joinpath(*relative.parts[:index]).is_symlink()
           for index in range(1, len(relative.parts) + 1)):
        return False
    if (len(parts) % 2 or any(parts[i] != "nodes" for i in range(0, len(parts), 2))
            or not (owner / "node.md").is_file()):
        return False
    return "lessons" if path.name == "lessons.md" and path.parent.parent.name == "specialists" else "frontmatter"


def find_terms(text, terms, principals=(), lessons=False):
    # Casefold, not lower: context-free (Greek Σ) and full (ß -> ss).
    if principals:
        text = governance_lessons(text, principals) if lessons else governance_text(text, principals)
    low = text.casefold()
    return [label for t, label in terms if t.casefold() in low]


def no_terms_in(n, text, what, principals=()):
    """Briefs and reports go to agents who must not see the words they must never write."""
    hits = find_terms(text, never_terms(n), principals)
    if hits:
        _m_common.die(f"{what} contains {', '.join(hits)}: reword it (the term itself stays out of every brief)")


NOTE_FILES = {"AGENTS.md", "CLAUDE.md", "brief.md", "status.md", "decisions.md", "backlog.md", "inbox.md", "node.md",
              NEVER_FILE, "job-ledger.md", "backlog.md", "status-archive.md", "worktree-setup.sh"}  # a node's own notes: they may discuss the rule


def redact(text, terms):
    """`text` with every term blanked out: what brain prints never shows the word it is guarding against."""
    for t, _ in terms:
        text = re.sub(re.escape(t), "[…]", text, flags=re.I)
        if t.casefold() in text.casefold():  # what re.I doesn't fold (ß, final sigma): blank the whole line
            text = "[…]"
    return text


def guard_paths(n, paths=()):
    """The home and every recorded worktree of it, or the paths given (which must exist)."""
    if paths:
        out = [Path(x).expanduser().resolve() for x in paths]
        missing = [redact(str(x), never_terms(n)) for x in out if not x.exists()]
        if missing:
            _m_common.die(f"not found: {', '.join(missing)}")
        return out
    if not n["external"]:
        return [n["dir"]]  # the home is the node folder itself (its notes aside)
    out = [n["home_path"]] if n["home_path"] and n["home_path"].exists() else []
    out += [Path(w["path"]) for w in n["local"].get("worktrees", {}).values() if Path(w["path"]).exists()]
    return out


def stream_mentions(fh, needles, size=None, chunk=1 << 20, principals=(), raw=None, lessons=False):
    """Read a whole byte stream (or exactly one Git blob), preserving UTF-8 and folded chunk edges."""
    import codecs
    found, keep = set(), max((len(x) for x in needles), default=1) - 1
    dec, tail = codecs.getincrementaldecoder("utf-8")(errors="ignore"), ""
    # Buffer only the opening frontmatter until its closing delimiter is established.
    # Malformed/unclosed frontmatter falls back to screening every original byte.
    prefix, opening, edge, raw_tail = ([] if principals and not lessons else None), "", "", ""
    lesson_tail = ""
    while True:
        block = fh.read(chunk if size is None else min(chunk, size))
        if size is not None:
            size -= len(block)
            if not block and size:
                raise OSError("unexpected end of Git blob")
        final = not block or size == 0
        decoded = dec.decode(block, final=final)
        if raw is not None:
            raw_text = raw_tail + decoded.casefold()
            raw.update(x for x in needles if x in raw_text)
            raw_tail = raw_text[-keep:] if keep else ""
        if prefix is not None:
            prefix.append(decoded)
            opening = (opening + decoded)[:5]
            started = opening.startswith("---\n")
            possible = "---\n".startswith(opening)
            probe = edge + decoded
            candidate = "".join(prefix) if final or re.search(r"\n---", probe) else None
            complete = candidate is not None and _m_common.FRONTMATTER_RE.match(candidate)
            if not final and ((possible and not started) or (started and not complete)):
                edge = probe[-3:]
                continue
            decoded = governance_text(candidate if candidate is not None else "".join(prefix), principals)
            prefix = None
        if principals and lessons:
            lines = (lesson_tail + decoded).split("\n")
            lesson_tail = "" if final else lines.pop()
            decoded = governance_lessons("\n".join(lines) + ("\n" if not final and lines else ""), principals)
        text = tail + decoded.casefold()
        found |= {x for x in needles if x in text}
        if final:
            return found
        tail = text[-keep:] if keep else ""


def file_mentions(fp, needles, errors=None, chunk=1 << 20, principals=(), lessons=False, policy=None,
                  missing_ok=False):
    """Which of `needles` (case-folded) appear in a regular file, read whole in chunks that overlap by the longest
    needle, so nothing is missed at a boundary or past any size. Not a regular file: nothing (a FIFO would block
    a read forever). A regular file that can't be read: nothing, and the reason goes to `errors`."""
    found = set()
    try:
        if not stat.S_ISREG(os.stat(fp).st_mode):
            return found
        with open(fp, "rb", buffering=0) as fh:
            before = os.fstat(fh.fileno())
            identity = term_file_identity(before)
            if policy is not None and policy.file_clean(fp, before):
                return found  # opening/fstat still verifies current read access
            cache = _TERM_SCAN_CACHE.get()
            key = ('file', os.path.abspath(fp), tuple(needles), tuple(principals), lessons)
            old = cache.get(key) if cache is not None else None
            if old is not None and old[0] == identity:
                return set(old[1])
            buffered = False
            if cache is not None and before.st_size <= chunk:
                data = fh.read(before.st_size + 1)
                if len(data) == before.st_size:
                    # Worktrees often contain identical bytes with distinct inodes.
                    # Read them once, but decode/search each content only once.
                    buffered = True
                    found = byte_mentions(data, needles, chunk=chunk, principals=principals, lessons=lessons)
                else:
                    fh.seek(0)  # file grew: retain the complete streaming scan
            if not buffered:
                found = stream_mentions(fh, needles, chunk=chunk, principals=principals, lessons=lessons)
            if term_file_identity(os.fstat(fh.fileno())) != identity:
                if errors is not None:
                    errors.append((fp, 'file changed during term scan'))
            elif cache is not None:
                cache[key] = (identity, frozenset(found))
                if policy is not None and buffered:
                    policy.remember_file(fp, before, data, not found)
    except OSError as e:
        if isinstance(e, FileNotFoundError) and missing_ok and not os.path.lexists(fp):
            return found  # valid tracked deletion; its index body remains guarded
        if errors is not None:
            errors.append((fp, e.strerror or str(e)))
    return found


def home_files(root, in_node, with_stage=False):
    """(relative paths, error): files of `root` that git would keep (tracked, or untracked and not ignored).
    For a node whose home is its own folder: without its notes, its child nodes' folders, and the core."""
    r = _m_common.git('ls-files', *(('--stage', '-t') if with_stage else ('--cached',)),
                       '--others', '--exclude-standard', '-z', '--', '.', cwd=root)
    if r.returncode or r.stderr.strip():  # "could not open directory …" is only a warning, with exit 0
        error = r.stderr.strip() or f"git ls-files exited {r.returncode}"
        return (None, [], error) if with_stage else (None, error)
    out, entries = [], []
    for record in (x for x in r.stdout.split("\0") if x):
        f = record
        if with_stage:
            if record.startswith('? '):
                f = record[2:]
            else:
                try:
                    meta, f = record[2:].split('\t', 1)
                    mode, oid, stage = meta.split()
                except ValueError:
                    return None, [], 'invalid Git file inventory'
                entries.append((mode, oid, stage, f))
        parts = Path(f).parts
        if in_node:
            if len(parts) == 1 and parts[0] in NOTE_FILES:
                continue
            if parts[0] == "job-handbacks":
                continue  # worker-owned node records, never home content
            if parts[0] == "core" and (root / "instance.md").exists():
                continue
            if len(parts) > 2 and parts[0] == "nodes" and (root / "nodes" / parts[1] / "node.md").exists():
                continue  # a child node (always in nodes/<name>/) is its own home
        out.append(f)
    return (out, entries, None) if with_stage else (out, None)


def term_git_directory(root):
    """Read Git's explicit worktree binding; unusual discovery uses Git itself."""
    if not any(os.environ.get(key) for key in ('GIT_DIR', 'GIT_COMMON_DIR', 'GIT_WORK_TREE')):
        try:
            directory = root / '.git'
            if directory.is_file():
                text = directory.read_text()
                if not text.startswith('gitdir: '):
                    raise ValueError('invalid Git directory file')
                directory = (root / text[8:].strip()).resolve()
            if directory.is_dir():
                common = directory / 'commondir'
                if common.exists():
                    directory = directory / common.read_text().strip()
                return str(directory.resolve()), None
        except (OSError, ValueError):
            pass
    result = _m_common.git('rev-parse', '--git-common-dir', cwd=root)
    return (str((root / result.stdout.strip()).resolve()), None) if not result.returncode else (None, result)


def term_file_observations(n, root, files, entries, needles, principals, policies):
    """Independent read observations; matching labels and final ordering stay in the caller."""
    tracked = {f for _, _, _, f in entries}
    submodules = {f for mode, _, _, f in entries if mode == '160000'}
    observed, errors = {}, []
    for f in files:
        if f in submodules or f in observed:
            continue
        fp = os.path.join(root, f)
        kind = governance_file(n, fp) if f.endswith('.md') else False
        seen = file_mentions(fp, needles, errors=errors, principals=principals if kind else (),
                             lessons=kind == 'lessons', policy=policies.get(kind), missing_ok=f in tracked)
        observed[f] = (kind, seen)
    return observed, errors


@term_scan_cache()
def scan_terms(n, paths=(), terms=None):
    """([(label, where)], [errors]): never-name terms in the names and contents of a home's files (tracked, or
    untracked and not ignored), its index blobs, and populated submodules recursively. For a home with its own
    git repository, also its commit messages and branch names. Neither the label nor `where` shows the term.
    Any error makes the scan incomplete, never clean."""
    terms = never_terms(n) if terms is None else terms
    verdict_cache = term_verdict_cache(n)
    if not terms:
        if verdict_cache is not None:
            verdict_cache.forget(n['dir'])
        return [], []
    hits, errors, repos, unread = [], [], {}, []
    folded_terms = [(t.casefold(), label) for t, label in terms]
    needles = tuple(t for t, _ in folded_terms)
    path_matches = {}
    visited = set()
    principals = governance_principals(n)
    policies = {}
    if verdict_cache is not None:
        for kind in (False, 'frontmatter', 'lessons'):
            policies[kind] = verdict_cache.policy(n['dir'], [t for t, _ in terms], principals, kind)
    stores = {}

    def failed(what, root, r):
        errors.append(f"{what} in {redact(_m_common.tilde(root), terms)}: {redact((r.stderr or '').strip(), terms)}")

    pending = list(guard_paths(n, paths))
    # Every worktree still gets a fresh tagged inventory. Bound read concurrency
    # instead of serially paying process/I/O latency for hundreds of worktrees.
    initial = {root: root.resolve() == n['dir'].resolve() for root in pending
               if (root / '.git').exists() or root.resolve() == n['dir'].resolve()}
    if len(initial) > 1:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {root: pool.submit(home_files, root, in_node, True) for root, in_node in initial.items()}
            inventories = {root: future.result() for root, future in futures.items()}
            futures = {root: pool.submit(contextvars.copy_context().run, term_file_observations,
                                         n, root, files, entries, needles, principals, policies)
                       for root, (files, entries, error) in inventories.items() if error is None}
            observations = {root: future.result() for root, future in futures.items()}
    else:
        inventories = {}
        observations = {}
    while pending:
        root = pending.pop(0)
        if root.resolve() in visited:
            continue
        visited.add(root.resolve())
        in_node = root.resolve() == n["dir"].resolve()
        own_repo = (root / ".git").exists() and not in_node
        if own_repo or in_node:
            storage = None
            directory, error = term_git_directory(root)
            if error is not None:
                failed('git rev-parse', root, error)
            else:
                if own_repo:
                    repos.setdefault(directory, root)
                if policies:
                    if directory not in stores:
                        stores[directory] = _m_term_cache.ObjectStorage(directory)
                    storage = stores[directory]
            files, entries, err = inventories.pop(root) if root in inventories else home_files(root, in_node, with_stage=True)
            if err:
                errors.append(f"listing {redact(_m_common.tilde(root), terms)}: {redact(err, terms)}")
                continue
            tracked = {f for _, _, _, f in entries}
            submodules = {f for mode, _, _, f in entries if mode == "160000"}
            observed, read_errors = (observations.pop(root) if root in observations else
                                     term_file_observations(n, root, files, entries, needles, principals, policies))
            unread.extend(read_errors)
            kinds = {}
            for f in files:  # names and folders, then contents: every file, binary or not, whole
                if f not in path_matches:
                    path_matches[f] = find_terms(f, terms)
                hits += [(label, f"{_m_common.tilde(root)}/{f}") for label in path_matches[f]]
                if f in submodules:
                    sub = root / f
                    if not (sub / ".git").exists():
                        errors.append(f"submodule unavailable: {redact(_m_common.tilde(sub), terms)}")
                    else:
                        pending.append(sub)
                    continue
                kind, seen = observed[f]
                kinds[f] = kind
                hits += [(label, f"{_m_common.tilde(root)}/{f}") for t, label in folded_terms if t in seen]
            blobs = {}
            included = set(files)
            for mode, oid, stage, f in entries:
                if f in included and mode != "160000" and set(oid) != {"0"}:
                    kind = kinds.get(f, False) if mode in ("100644", "100755") else False
                    blobs.setdefault((oid, kind), []).append((f, stage))
            blobs = {key: names for key, names in blobs.items()
                     if not (policies.get(key[1]) and policies[key[1]].blob_clean(key[0], storage))}
            if blobs:
                # One streaming process per repository: no size cap, and no process per file.
                with subprocess.Popen(["git", "cat-file", "--batch"], cwd=root, stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
                    try:
                        remaining = iter(blobs.items())
                        # Requests fit in one pipe write. Drain every response
                        # before sending more, so large blobs cannot deadlock.
                        batch_size = max(1, min(32, os.fpathconf(proc.stdin.fileno(), 'PC_PIPE_BUF') // 66))
                        while True:
                            batch = list(itertools.islice(remaining, batch_size))
                            if not batch:
                                break
                            proc.stdin.write(''.join(oid + '\n' for (oid, _), _ in batch).encode())
                            proc.stdin.flush()
                            for (oid, kind), names in batch:
                                header = proc.stdout.readline().split()
                                if len(header) != 3 or header[1] != b"blob":
                                    raise OSError("Git index blob unavailable")
                                size = int(header[2])
                                if size <= 1 << 20:
                                    data = proc.stdout.read(size)
                                    if len(data) != size:
                                        raise OSError('unexpected end of Git blob')
                                    seen = byte_mentions(data, needles, principals=principals if kind else (),
                                                         lessons=kind == 'lessons')
                                else:
                                    seen = stream_mentions(proc.stdout, needles, size=size,
                                                           principals=principals if kind else (), lessons=kind == "lessons")
                                if proc.stdout.read(1) != b"\n":
                                    raise OSError("incomplete Git blob response")
                                # Only canonical bodies authorize future OID reuse.
                                # Replacement refs and malformed object contents
                                # can return bytes whose identity differs.
                                algorithm = hashlib.sha256 if len(oid) == 64 else hashlib.sha1
                                canonical = (algorithm(b'blob ' + str(size).encode() + b'\0' + data).hexdigest()
                                             if size <= 1 << 20 else None)
                                if policies.get(kind) and canonical == oid:
                                    policies[kind].remember_blob(oid, not seen, storage)
                                hits += [(label, f"index stage {stage}: {_m_common.tilde(root)}/{f}")
                                         for f, stage in names for t, label in folded_terms if t in seen]
                    except (OSError, ValueError) as e:
                        errors.append(redact(f"reading index in {_m_common.tilde(root)}: {e}", terms))
                    finally:
                        proc.stdin.close()
                        proc.stdout.close()
                    if proc.wait():
                        errors.append(f"git cat-file failed in {redact(_m_common.tilde(root), terms)}")
            if storage is not None and storage.reused and term_git_directory(root)[0] != directory:
                errors.append('Git directory binding changed during term scan')
                for policy in policies.values():
                    policy.data['stores'].pop(storage.key, None)
        else:  # a folder given by path, outside any repository
            for dp, dn, fn in os.walk(root, onerror=lambda e: unread.append((Path(e.filename), e.strerror))):
                dn[:] = [x for x in dn if x not in (".git", "node_modules")]
                for f in fn:
                    fp = Path(dp) / f
                    kind = governance_file(n, fp)
                    seen = file_mentions(fp, needles, errors=unread,
                                         principals=principals if kind else (), lessons=kind == "lessons",
                                         policy=policies.get(kind))
                    hits += [(label, _m_common.tilde(fp)) for t, label in folded_terms
                             if t in seen or t in str(fp.relative_to(root)).casefold()]
    for storage in stores.values():
        if storage.stamp is not None and not storage.unchanged():
            if storage.reused:
                errors.append('Git object storage changed during term scan')
            for policy in policies.values():
                policy.data['stores'].pop(storage.key, None)
    for root in repos.values():
        log = _m_common.git("log", "--all", "--format=%h%x00%B%x01", cwd=root)
        if log.returncode:
            failed("git log", root, log)
        for entry in log.stdout.split("\x01"):
            if "\x00" in entry:
                sha, body = entry.strip().split("\x00", 1)
                hits += [(label, f"commit {sha} in {_m_common.tilde(root)}") for label in find_terms(body, terms)]
        refs = _m_common.git("for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes", cwd=root)
        if refs.returncode:
            failed("git for-each-ref", root, refs)
        for ref in refs.stdout.split():
            hits += [(label, f"branch {ref} in {_m_common.tilde(root)}") for label in find_terms(ref, terms)]
    errors += [f"couldn't read {redact(_m_common.tilde(fp), terms)}: {why}" for fp, why in unread]
    hits = sorted({(label, redact(where, terms)) for label, where in hits}, key=lambda h: (h[1], h[0]))
    return hits, errors


def cmd_guard(a):
    """Before a merge or a pull request: nothing in the home names what it must never name."""
    inst = _m_nodes.load()
    n = inst.node(a.id)
    print(_m_machine.machine_summary(_m_machine.machine_state(inst)))
    if not never_terms(n) and not n["sealed"]:
        print(f"guard: {n['id']} has no {NEVER_FILE} (here or above) and isn't sealed: nothing to check")
        return
    paths = guard_paths(n, a.paths)
    if not paths:
        _m_common.die(f"{n['id']}: its home isn't on this machine (brain set {n['id']} home=<path>), or give a path")
    terms = never_terms(n)
    hits, errors = scan_terms(n, a.paths)
    for hp in (paths if n["sealed"] else []):
        terrs = []
        hits += [(redact(f"trace of '{pat}' (axiom 6)", terms), redact(f"{_m_common.tilde(hp)}/{f}", terms))
                 for pat, f in scan_traces(inst, hp, terrs)]
        errors += [redact(e, terms) for e in terrs]
    for label, where in hits:
        print(f"  ✗ {where}: {label}")
    for e in errors:
        print(f"  ! couldn't check: {e}")
    where = ", ".join(redact(_m_common.tilde(x), terms) for x in paths)
    if errors:
        print(f"guard: incomplete ({len(errors)} error(s){', ' + str(len(hits)) + ' found' if hits else ''}) ({where})")
        sys.exit(2)
    print(f"guard: {'clean' if not hits else str(len(hits)) + ' found'} ({where})")
    if hits:
        sys.exit(1)

# ---------- sealing ----------

def rules_section(agents_file, headings=("Rules for this node", "Instance rules")):
    """The text under a node's own rules heading, without template comments."""
    if not agents_file.exists():
        return ""
    text, out, inside = agents_file.read_text(), [], False
    for line in text.splitlines():
        if line.startswith("## "):
            inside = line[3:].strip() in headings
        elif inside:
            out.append(line)
    body = re.sub(r"<!--.*?-->", "", "\n".join(out), flags=re.S).strip()
    return body


def home_rules(inst, n):
    """(rules written into the home, rules from above the new boundary to consider). Neither names anything
    above the boundary by construction; the trace scan verifies (axiom 6)."""
    own = rules_section(n["dir"] / "AGENTS.md")
    above = []
    m = n["parent"]
    while m:
        r = rules_section(m["dir"] / "AGENTS.md")
        if r:
            above.append((m["id"], r))
        m = m["parent"]
    return own, above


def cmd_seal(a):
    inst = _m_nodes.load()
    before = _m_nodes.snapshot(inst)
    n = inst.node(a.id)
    if n["id"] == "root":
        _m_common.die("the root can't be sealed")
    inst.check_principals(a.owners)
    if set(_m_common.lst(a.owners)) == set(n["parent"]["owners"]):
        _m_common.die("those are the parent's owners: there is no boundary to seal (use 'brain owners')")
    home = Path(a.home).expanduser().resolve()
    if inst.root.resolve() in [home, *home.parents]:
        _m_common.die("the home must be outside the instance (axiom 8)")
    terms = never_terms(n)  # taken now: nothing below may move or change them
    keep = {"node.md", "AGENTS.md", "nodes", NEVER_FILE, _m_jobs.JOBS_FILE, "job-handbacks"}
    for rel in a.move or []:  # every move is checked before anything moves
        src = n["dir"] / rel
        try:
            inside = src.resolve().relative_to(n["dir"].resolve())
        except ValueError:
            _m_common.die(f"'{rel}' isn't inside the node's folder")
        if not inside.parts or inside.parts[0] in keep or src.is_symlink():
            _m_common.die(redact(f"'{rel}' is part of the node's record and stays on the operator's side", terms))
        if not src.exists():
            _m_common.die(redact(f"no '{rel}' in {n['dir']}", terms))
        if (home / rel).exists():
            _m_common.die(redact(f"{home / rel} already exists", terms))
    created_repo = False
    home.mkdir(parents=True, exist_ok=True)
    if not (home / ".git").exists() and not a.no_git:
        _m_common.git("init", "-q", "-b", "main", cwd=home, check=True)
        created_repo = True
    moved = []
    for rel in a.move or []:
        src = n["dir"] / rel
        dest = home / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        _m_common.touch(src)
        moved.append(rel)
    own, above = home_rules(inst, n)
    wrote_rules = False
    if not (home / "AGENTS.md").exists():
        (home / "AGENTS.md").write_text(_m_common.fill((_m_common.CORE / "templates" / "home-AGENTS.md").read_text(),
                                             {"TITLE": n["fm"].get("title", n["id"]),
                                              "RULES": own or "<!-- This project's own rules. -->"}))
        wrote_rules = True
        for rt in inst.runtimes:
            link = inst.adapter(rt).get("INSTRUCTIONS_LINK")
            if link and not (home / link).exists():
                (home / link).symlink_to("AGENTS.md")
    fm, body = _m_common.read_fm(n["dir"] / "node.md")
    fm["owners"] = a.owners
    _m_nodes.bind_home(inst, n["id"], fm, str(home))
    if a.url:
        fm["home_url"] = a.url
    _m_common.write_fm(n["dir"] / "node.md", fm, body)
    inst.save_local()
    after = _m_nodes.snapshot(inst)
    _m_nodes.resync_views(inst)
    _m_status.write_registry(inst)
    inst.commit([], f"{a.id}: seal -> owners {a.owners}, home external")
    print(redact(f"{a.id}: owners -> {a.owners}; home -> {_m_common.tilde(home)}", terms))
    for nid in sorted(after):
        if before.get(nid) and before[nid][1] != after[nid][1]:
            print(redact(f"  {nid}: effective owners {','.join(before[nid][1])} -> {','.join(after[nid][1])}", terms))
    if moved:
        print(redact(f"  moved to the home: {', '.join(moved)}", terms))
    terrs = []
    hits = scan_traces(inst, home, terrs)
    words, werrs = scan_terms(inst.node(a.id), [home], terms=terms)  # the words taken before anything moved
    terrs += werrs
    hits += [(label, where) for label, where in words]
    if hits or terrs:
        print("  ✗ upward traces or never-name words in the home; remove them before anything is committed there:"
              if hits else "  ! couldn't check the home for upward traces, so nothing was committed there:")
        nt = never_terms(inst.node(a.id))
        for pat, f in hits:
            print(redact(f"    {f}: mentions '{pat}'", nt))
        for e in terrs:
            print(redact(f"    {e}", nt))
    elif created_repo:
        _m_common.git("add", "-A", cwd=home, check=True)
        _m_common.git("commit", "-qm", "Initial commit", cwd=home)
        print("  home: new repository, initial commit made")
    else:
        print("  home: existing; nothing committed there (its own change process decides)")
    if wrote_rules:
        print(f"  home AGENTS.md: the shared baseline{' + this node' + chr(39) + 's own rules' if own else ''}")
    elif own:
        print("  home has its own AGENTS.md: propose these rules through its change process (axiom 12):")
        print("\n".join(redact("    " + l, terms) for l in own.splitlines()))
    if above:
        print("  rules from above the boundary; write in the ones the new owners agree to, in their own words:")
        for nid, r in above:
            print(redact(f"    from {nid}:", terms))
            print("\n".join(redact("      " + l, terms) for l in r.splitlines()))
    print("  still yours to do (skills/node-transition, Sealing):\n"
          "    - ask the principals in scope whether earlier items' scopes change (axiom 11)\n"
          "    - the coordinator stays on this side and bridges the boundary")
    if terrs:
        sys.exit(2)
    if hits:
        sys.exit(1)


# ---------- move, split, merge ----------
