"""Remove the T3-managed worktrees of settled host threads; keep anything uncertain.

T3 gives a thread its own git worktree under ``~/.t3/worktrees/<project>/`` and
never removes it, not even when the thread is settled or deleted. Only worktrees
of the instance's own repository are in scope; node homes and other checkouts
are reported, never changed. A worktree is removed only when every host thread
that records it is settled with no active run, no process has its working
directory inside it, it has no uncommitted or untracked changes, and its HEAD
has no commit missing from every other branch, remote branch and tag. Removal
always goes through ``git worktree remove`` without force, so Git's own records
stay right and Git's own refusal is honored. Anything else is kept and listed.

Thread state comes from T3's store, opened read-only. An unreadable or changed
store means no thread is known, so everything is kept.
"""
import concurrent.futures
import ctypes
import json
import os
import sqlite3
import struct
import subprocess
import sys
import time
from pathlib import Path

from . import common as _m_common

REAP_SECONDS = 10
SETTLE_SECONDS = 2
GIT_SECONDS = 10
WORKERS = 4
TERMINAL_RUNS = ('completed', 'cancelled', 'failed', 'interrupted', 'rolled_back')
_STORE_QUERY = (
    "select t.thread_id, json_extract(t.payload_json, '$.worktreePath'),"
    " json_extract(t.payload_json, '$.settledAt'), json_extract(t.payload_json, '$.settledOverride'),"
    " exists(select 1 from orchestration_v2_projection_runs r where r.thread_id = t.thread_id"
    " and (r.status not in (" + ','.join("'" + s + "'" for s in TERMINAL_RUNS) + ") or r.completed_at is null))"
    " from orchestration_v2_projection_threads t"
    " where json_extract(t.payload_json, '$.worktreePath') is not null")


def t3_home():
    return Path(os.environ.get('BRAIN_T3_HOME') or '~/.t3').expanduser()


def _real(path):
    return os.path.realpath(os.fspath(path))


def host_threads(home):
    """{worktree path: [thread facts]} from T3's store, read-only; None when unknown."""
    db = Path(home) / 'userdata' / 'statev2.sqlite'
    if not db.is_file():
        return None
    try:
        con = sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
        try:
            rows = con.execute(_STORE_QUERY).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    threads = {}
    for identity, path, settled_at, override, active in rows:
        if not isinstance(identity, str) or not isinstance(path, str) or not path.startswith('/'):
            continue
        threads.setdefault(_real(path), []).append({
            'thread': identity, 'settled': bool(settled_at) and override != 'unsettled', 'active': bool(active)})
    return threads


def live_directories():
    """Working directories of running processes; None when they cannot be listed."""
    try:
        result = subprocess.run(['lsof', '-nP', '-w', '-a', '-d', 'cwd', '-Fn'], capture_output=True,
                                text=True, timeout=_timeout(), cwd='/')
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode not in (0, 1) or not result.stdout:
        return None
    return {_real(line[1:]) for line in result.stdout.splitlines() if line.startswith('n/')}


_deadline = [None]  # the running pass's monotonic deadline; every subprocess stays inside it


def _timeout():
    if _deadline[0] is None:
        return GIT_SECONDS
    return max(0.05, min(GIT_SECONDS, _deadline[0] - time.monotonic()))


def _git(args, cwd=None, git_dir=None, stdin=None):
    prefix = ['git'] + (['--git-dir', git_dir] if git_dir else []) + (['-C', cwd] if cwd else [])
    env = {**os.environ, 'GIT_OPTIONAL_LOCKS': '0', 'LC_ALL': 'C'}
    return subprocess.run(prefix + args, capture_output=True, text=True, timeout=_timeout(),
                          env=env, cwd='/', input=stdin)


def instance_repository(root):
    """The instance repository's common Git directory, or None outside Git."""
    result = _git(['rev-parse', '--git-common-dir'], cwd=str(root))
    if result.returncode or not result.stdout.strip():
        return None
    return _real(Path(root) / result.stdout.strip())


def linked_admin(entry):
    """(admin dir, common dir) of a linked worktree whose admin entry points back here."""
    dotgit = Path(entry) / '.git'
    try:
        if dotgit.is_symlink() or not dotgit.is_file():
            return None
        text = dotgit.read_text().strip()
        if not text.startswith('gitdir: '):
            return None
        admin = Path(entry) / text[len('gitdir: '):]
        back = (admin / 'gitdir').read_text().strip()
        common = (admin / 'commondir').read_text().strip()
    except (OSError, UnicodeError):
        return None
    if _real(back) != _real(dotgit):
        return None
    return _real(admin), _real(admin / common)


def inventory(home):
    """Every worktree folder T3 keeps, as (project folder name, path)."""
    root = Path(home) / 'worktrees'
    found = []
    try:
        projects = sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink())
    except OSError:
        return found
    for project in projects:
        try:
            entries = sorted(p for p in project.iterdir() if p.is_dir() and not p.is_symlink())
        except OSError:
            continue
        found += [(project.name, entry) for entry in entries]
    return found


# APFS reports each file's private size, clone id and clone refcount; a folder of
# clones looks free per file but holds real blocks shared with its siblings.
class _AttrList(ctypes.Structure):
    _fields_ = [('bitmapcount', ctypes.c_ushort), ('reserved', ctypes.c_ushort),
                ('commonattr', ctypes.c_uint), ('volattr', ctypes.c_uint), ('dirattr', ctypes.c_uint),
                ('fileattr', ctypes.c_uint), ('forkattr', ctypes.c_uint)]


_PRIVATE, _CLONEID, _REFCNT = 0x8, 0x100, 0x1000
_LIBC = None


def _clone_info(path):
    """(private bytes, clone id, refcount); Nones where the filesystem says nothing."""
    global _LIBC
    if sys.platform != 'darwin':
        return None, None, None
    try:
        if _LIBC is None:
            _LIBC = ctypes.CDLL(None, use_errno=True)
        attrs = _AttrList(5, 0, 0x80000000, 0, 0, 0, _PRIVATE | _CLONEID | _REFCNT)
        buffer = ctypes.create_string_buffer(64)
        if _LIBC.getattrlist(os.fsencode(path), ctypes.byref(attrs), buffer, 64, 0x1 | 0x20) != 0:
            return None, None, None
        raw = buffer.raw
        returned = struct.unpack_from('5I', raw, 4)[4]
        offset, private, clone, refs = 24, None, None, None
        if returned & _PRIVATE:
            private = struct.unpack_from('q', raw, offset)[0]
            offset += 8
        if returned & _CLONEID:
            clone = struct.unpack_from('Q', raw, offset)[0]
            offset += 8
        if returned & _REFCNT:
            refs = struct.unpack_from('I', raw, offset)[0]
        return private, clone, refs
    except (OSError, AttributeError, ValueError, struct.error):
        return None, None, None


def measure(entry):
    """{'listed', 'private', 'shared': {clone id: [refcount, files here, bytes]}, 'inodes'}."""
    listed = private = 0
    shared, inodes = {}, {}
    for folder, _, files in os.walk(entry):
        for name in files:
            path = os.path.join(folder, name)
            try:
                info = os.lstat(path)
            except OSError:
                continue
            if (info.st_mode & 0o170000) != 0o100000 or not info.st_blocks:
                continue
            allocated = info.st_blocks * 512
            if info.st_nlink > 1:
                inodes[info.st_ino] = inodes.get(info.st_ino, 0) + 1
                if inodes[info.st_ino] > 1:
                    continue
            own, clone, refs = _clone_info(path)
            if own is None or own > allocated:
                own = allocated
            listed += allocated
            private += own
            if clone and refs and refs > 1 and allocated > own:
                family = shared.setdefault(clone, [refs, 0, 0])
                family[1] += 1
                family[2] = max(family[2], allocated - own)
    return {'listed': listed, 'private': private, 'shared': shared}


def freed(sizes):
    """Bytes removing all these folders together frees: private data plus whole clone families."""
    families, private = {}, 0
    for size in sizes:
        private += size['private']
        for clone, (refs, seen, extra) in size['shared'].items():
            family = families.setdefault(clone, [refs, 0, 0])
            family[1] += seen
            family[2] = max(family[2], extra)
    return private + sum(extra for refs, seen, extra in families.values() if seen >= refs)


def unique_commits(entry):
    """True when HEAD has commits on no other branch, remote branch or tag; None if unknown."""
    head = _git(['symbolic-ref', '-q', '--short', 'HEAD'], cwd=str(entry))
    branch = head.stdout.strip() if head.returncode == 0 else ''
    args = ['rev-list', '-n', '1', 'HEAD', '--not']
    if branch:
        args.append('--exclude=' + branch)
    result = _git(args + ['--branches', '--remotes', '--tags'], cwd=str(entry))
    if result.returncode:
        return None
    return bool(result.stdout.strip())


def reachable_commits(common, checked_out):
    """(commits reachable from every branch, remote branch and tag except ``checked_out``, ref tips); None if unknown.

    One walk classifies the whole sweep. A HEAD inside it is on some other ref; a
    HEAD outside it gets the exact per-worktree check. Removal never relies on it:
    the check just before each removal is always the exact one.
    """
    refs = _git(['for-each-ref', '--format=%(objectname) %(refname)', 'refs/heads', 'refs/remotes', 'refs/tags'],
                git_dir=common)
    if refs.returncode:
        return None
    tips = {}
    for line in refs.stdout.splitlines():
        sha, _, ref = line.partition(' ')
        tips[ref] = sha
    others = sorted({sha for ref, sha in tips.items() if ref not in checked_out})
    walk = _git(['rev-list', '--stdin'], git_dir=common, stdin='\n'.join(others) + '\n')
    if walk.returncode:
        return None
    return set(walk.stdout.split()), tips


def admin_head(admin, tips):
    """The commit a worktree's HEAD names, from its admin entry; None when not resolvable here."""
    try:
        head = (Path(admin) / 'HEAD').read_text().strip()
    except (OSError, UnicodeError):
        return None
    if head.startswith('ref: '):
        return tips.get(head[len('ref: '):])
    return head if len(head) in (40, 64) else None


def judge(entry, repository, threads, live, keep, wanted=None, shared=None):
    """(verdict, reason, thread ids); verdict is remove, keep or other (a foreign repository)."""
    entry = Path(entry)
    path = _real(entry)
    records = (threads or {}).get(path, [])
    ids = sorted(r['thread'] for r in records)
    if wanted is not None and not set(ids) & wanted:
        return 'skip', 'not a selected thread', ids
    if keep is None:
        return 'keep', 'keep setting unreadable', ids
    if entry.name in keep or path in keep:
        return 'keep', 'kept by setting', ids
    linked = linked_admin(entry)
    if linked is None:
        return 'keep', 'no matching git admin entry', []
    if linked[1] != repository:
        return 'other', 'another repository', []
    if threads is None:
        return 'keep', 'host thread state unknown', []
    if not records:
        return 'keep', 'no host thread records this worktree', ids
    if not all(r['settled'] for r in records):
        return 'keep', 'thread unsettled', ids
    if any(r['active'] for r in records):
        return 'keep', 'thread running', ids
    if live is None:
        return 'keep', 'live processes unknown', ids
    if any(d == path or d.startswith(path + os.sep) for d in live):
        return 'keep', 'a process works inside it', ids
    try:
        status = _git(['status', '--porcelain=v1', '--untracked-files=all', '--ignore-submodules=none'], cwd=path)
        if status.returncode:
            return 'keep', 'git status failed', ids
        if status.stdout.strip():
            return 'keep', 'uncommitted or untracked changes', ids
        head = admin_head(linked[0], shared[1]) if shared else None
        unique = False if head is not None and head in shared[0] else unique_commits(path)
    except (OSError, subprocess.SubprocessError):
        return 'keep', 'git check failed', ids
    if unique is None:
        return 'keep', 'commit check failed', ids
    if unique:
        return 'keep', 'commits only here', ids
    populated = populated_submodules(path, linked[0])
    if populated is None:
        return 'keep', 'submodule check failed', ids
    if populated:
        return 'keep', 'a submodule is checked out', ids
    return 'remove', 'settled and clean', ids


def populated_submodules(entry, admin):
    """True when a submodule (or any nested repository) has Git data or a checkout here; None if unknown."""
    try:
        modules = Path(admin) / 'modules'
        if modules.is_dir() and any(modules.iterdir()):
            return True
        failed = []
        for folder, dirs, files in os.walk(entry, onerror=failed.append):
            if folder != entry and ('.git' in dirs or '.git' in files):
                return True
        if failed:
            return None
    except OSError:
        return None
    return False


def remove(entry, common, admin):
    """git worktree remove, never forced; True only once Git and the folder agree it is gone.

    Git refuses any worktree whose admin entry has a modules folder, even an empty
    one (Git itself calls that a possible false positive). Only an empty one is
    removed (rmdir fails on anything else) and the same unforced removal repeats,
    so Git's own refusal of changed, untracked, locked or submodule worktrees holds.
    """
    path = _real(entry)
    result = _git(['worktree', 'remove', path], git_dir=common)
    if result.returncode and 'containing submodules' in result.stderr:
        modules = Path(admin) / 'modules'
        if modules.is_symlink():
            return False
        try:
            os.rmdir(modules)
        except OSError:
            return False
        result = _git(['worktree', 'remove', path], git_dir=common)
    if result.returncode or os.path.lexists(path):
        return False
    listed = _git(['worktree', 'list', '--porcelain'], git_dir=common)
    return listed.returncode == 0 and ('worktree ' + path) not in listed.stdout.splitlines()


def keep_setting(inst):
    """Folder names and paths never removed; None when the setting cannot be read (nothing is removed)."""
    try:
        value = inst.node('root')['fm'].get('t3_worktree_keep', '')
    except (KeyError, OSError, ValueError, TypeError, SystemExit):
        return None
    if not isinstance(value, str):
        return None
    return {_real(v.strip()) if v.strip().startswith('/') else v.strip() for v in value.split(',') if v.strip()}


def reap(root, home=None, apply=False, threads_wanted=None, keep=(), deadline=None, sizes=True, others=False):
    """Judge T3 worktrees; when apply, remove the settled clean ones. Never raises for one entry.

    With ``threads_wanted`` only those threads' worktrees are judged (the settle
    path). ``deadline`` bounds every subprocess and defers what is left.
    """
    home = Path(home) if home else t3_home()
    started = time.monotonic()
    previous, _deadline[0] = _deadline[0], deadline
    try:
        return _reap(root, home, apply, threads_wanted, keep, deadline, sizes, others, started)
    finally:
        _deadline[0] = previous


def _reap(root, home, apply, threads_wanted, keep, deadline, sizes, others, started):
    entries = inventory(home)
    threads = host_threads(home) if entries else {}
    wanted = set(threads_wanted) if threads_wanted is not None else None
    if wanted is not None:
        paths = {path for path, records in (threads or {}).items() if any(r['thread'] in wanted for r in records)}
        entries = [item for item in entries if _real(item[1]) in paths]
    result = {'rows': [], 'removed': [], 'errors': [], 'store': threads is not None, 'live': True,
              'repository': True, 'freed': 0}
    if not entries:
        result['seconds'] = round(time.monotonic() - started, 3)
        return result
    repository = instance_repository(root)
    live = live_directories()
    keep = set(keep) if keep is not None else None
    shared = None
    if repository is not None and wanted is None:
        checked_out = set()
        for _, entry in entries:
            linked = linked_admin(entry)
            if linked is not None and linked[1] == repository:
                try:
                    head = (Path(linked[0]) / 'HEAD').read_text().strip()
                except (OSError, UnicodeError):
                    continue
                if head.startswith('ref: '):
                    checked_out.add(head[len('ref: '):])
        try:
            shared = reachable_commits(repository, checked_out)
        except (OSError, subprocess.SubprocessError):
            shared = None

    def one(item):
        project, entry = item
        try:
            verdict, reason, ids = judge(entry, repository, threads, live, keep, wanted, shared)
        except (OSError, ValueError, subprocess.SubprocessError):
            verdict, reason, ids = 'keep', 'check failed', []
        row = {'project': project, 'path': str(entry), 'verdict': verdict, 'reason': reason, 'threads': ids}
        if sizes and verdict != 'skip' and (others or verdict != 'other'):
            try:
                row['size'] = measure(entry)
            except OSError:
                row['size'] = {'listed': 0, 'private': 0, 'shared': {}}
        return row

    rows = []
    if repository is not None:
        with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
            rows = [row for row in pool.map(one, entries) if row['verdict'] != 'skip']
    result.update(rows=rows, store=threads is not None, live=live is not None, repository=repository is not None)
    for row in rows if apply else ():
        if row['verdict'] != 'remove':
            continue
        if deadline is not None and time.monotonic() >= deadline:
            row['verdict'], row['reason'] = 'keep', 'time budget spent; next pass'
            continue
        try:
            # Fresh thread and process state and the exact commit check, right before removal.
            again = judge(row['path'], repository, host_threads(home), live_directories(), keep, wanted)
            linked = linked_admin(row['path'])
            if again[0] != 'remove' or linked is None:
                row['verdict'], row['reason'] = 'keep', again[1] if again[0] != 'remove' else 'admin entry changed'
                continue
            done = remove(row['path'], linked[1], linked[0])
        except (OSError, ValueError, subprocess.SubprocessError):
            done = False
        if done:
            row['verdict'] = 'removed'
            result['removed'].append(row['path'])
        else:
            row['verdict'], row['reason'] = 'keep', 'git worktree remove refused'
            result['errors'].append(row['path'])
    chosen = [r['size'] for r in rows if r['verdict'] in ('remove', 'removed') and 'size' in r]
    result['freed'] = freed(chosen)
    result['seconds'] = round(time.monotonic() - started, 3)
    return result


def human(size):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1000 or unit == 'GB':
            return (f'{size:.0f} {unit}' if unit == 'B' else f'{size:.1f} {unit}')
        size /= 1000.0


def report(result, apply):
    rows = result['rows']
    lines = []
    if not result['repository']:
        return ['t3 worktrees: the instance is not a Git repository; nothing judged']
    if not result['store']:
        lines.append('t3 worktrees: host thread state unreadable; every worktree kept')
    if not result['live']:
        lines.append('t3 worktrees: live processes unknown; every worktree kept')
    own = [r for r in rows if r['verdict'] != 'other']
    done = 'removed' if apply else 'removable'
    count = sum(r['verdict'] in ('remove', 'removed') for r in own)
    lines.append(f"t3 worktrees: {len(own)} of this instance, {count} {done}, "
                 f"{len(own) - count} kept; {'freed' if apply else 'would free'} {human(result['freed'])} "
                 f"({result['seconds']:.1f}s{'' if apply else ', dry run'})")
    for r in own:
        size = human(freed([r['size']])) if 'size' in r else '?'
        threads = ' ' + ','.join(r['threads']) if r['threads'] and r['verdict'] != 'remove' else ''
        lines.append(f"  {r['verdict']:<7} {size:>9}  {r['project']}/{Path(r['path']).name}  {r['reason']}{threads}")
    others = {}
    for r in rows:
        if r['verdict'] == 'other':
            count_size = others.setdefault(r['project'], [0, []])
            count_size[0] += 1
            if 'size' in r:
                count_size[1].append(r['size'])
    for project, (count, sizes) in sorted(others.items()):
        size = ', ' + human(freed(sizes)) if sizes else ''
        lines.append(f"  other repository (report only): {project}: {count} worktree(s){size}")
    return lines


def reap_settled(inst, thread_ids=None):
    """Settle-path hook: remove worktrees of settled threads; bounded, never raises.

    Selected threads get SETTLE_SECONDS each; a whole due-tick pass gets REAP_SECONDS.
    """
    seconds = REAP_SECONDS if thread_ids is None else SETTLE_SECONDS * max(1, len(thread_ids))
    try:
        result = reap(inst.root, apply=True, threads_wanted=thread_ids, keep=keep_setting(inst),
                      deadline=time.monotonic() + seconds, sizes=False)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError,
            subprocess.SubprocessError, sqlite3.Error) as error:
        return {'rows': [], 'removed': [], 'errors': [type(error).__name__], 'freed': 0}
    return result


def reap_due(inst):
    """Due-tick hook, off unless the root node sets t3_worktree_reap: on."""
    try:
        enabled = inst.node('root')['fm'].get('t3_worktree_reap') == 'on'
    except (KeyError, OSError, ValueError, SystemExit):
        return None
    return reap_settled(inst) if enabled else None


def cmd_t3_worktrees(a):
    from . import nodes as _m_nodes
    inst = _m_nodes.load()
    result = reap(inst.root, apply=a.apply, threads_wanted=a.thread, keep=keep_setting(inst), others=a.others)
    if a.json:
        for row in result['rows']:
            if 'size' in row:
                row['size'] = {'listed': row['size']['listed'], 'frees': freed([row['size']])}
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print('\n'.join(report(result, a.apply)))
    if result['errors']:
        _m_common.die('t3 worktrees: git refused to remove ' + str(len(result['errors'])) + ' worktree(s); kept')
