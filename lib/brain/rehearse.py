"""Compare installed and candidate cores without granting either live write access.

The macOS sandbox is a prerequisite, not an optional best-effort safeguard. Other
platforms refuse until they have an equivalent OS-enforced write/network boundary.
Only counts, fixed labels and digests leave the private temporary directory.
"""
import collections
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import common as _m_common
from . import guard as _m_guard
from . import hosts as _m_hosts
from . import instance as _m_instance
from . import jobs as _m_jobs
from . import nodes as _m_nodes
from . import upgrade as _m_upgrade
from . import verify as _m_verify


class RehearsalError(Exception):
    pass


def node_instance(n):
    root = n
    while root.get('parent'):
        root = root['parent']
    return _m_instance.Instance(root['dir'])


def installed_core_commit(root):
    core = root / 'core'
    if (core / '.git').exists():
        result = _m_common.git('rev-parse', '--verify', 'HEAD', cwd=core)
        if result.returncode or not re.fullmatch(r'[0-9a-f]{40,64}', result.stdout.strip()):
            raise RehearsalError('installed core commit unavailable')
        return result.stdout.strip()
    # Fixture and manually installed cores have no commit. Still bind their bytes.
    return 'unversioned:' + hashlib.sha256(json.dumps(core_identity(root), sort_keys=True).encode()).hexdigest()


def core_identity(root):
    """Canonical internal aliases for provenance; mutation audits stay literal."""
    core = root / 'core'
    entries = snapshot(core)
    for rel, value in entries.items():
        if value.startswith('link:'):
            try:
                target = (core / rel).resolve()
            except (OSError, RuntimeError):
                raise RehearsalError('installed core link identity unavailable') from None
            if target == root or root in target.parents:
                entries[rel] = 'link:brain:' + target.relative_to(root).as_posix()
    return entries


def evidence_path(n, branch):
    # Evidence stays in the workbench, leaving even instance Git byte-identical.
    from . import worktrees as _m_worktrees
    key = hashlib.sha256(branch.encode()).hexdigest()
    inst = node_instance(n)
    return _m_worktrees.workbench(inst, n) / 'scratch' / 'rehearse' / (key + '.json')


def read_evidence(n, branch, head, main=None, integration_base=None):
    if not n['external'] or not n.get('home_path') or not head:
        return None
    try:
        p = json.loads(evidence_path(n, branch).read_text())
        if (isinstance(p, dict) and p.get('version') == 1 and p.get('node') == n['id'] and p.get('branch') == branch
                and p.get('head') == head and p.get('passed') is True
                and p.get('installed_core_commit') == installed_core_commit(node_instance(n).root)
                and p.get('main') == (main if main is not None else _m_verify.ready_refs(n, branch)[1])
                and p.get('integration_base') == (integration_base if integration_base is not None
                                                  else _m_verify.integration_parent(n, p.get('main')))):
            return p
    except (OSError, ValueError, TypeError, RehearsalError):
        pass
    return None


def required(n):
    return n['fm'].get('rehearse') == 'required'


def isolated_probe_instance(inst, directory):
    """The shared transport helper's cache belongs in a private instance."""
    shutil.copy2(inst.root / 'instance.md', directory / 'instance.md')
    for node in inst.nodes():
        dest = directory / node['dir'].relative_to(inst.root)
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(node['dir'] / 'node.md', dest / 'node.md')
    (directory / '.brain').mkdir()
    (directory / '.brain/local.json').write_text(json.dumps(inst.local))
    return _m_instance.Instance(directory)


def probe_delivery(inst, nodes, probe_inst=None):
    """One authorized real POST per distinct safe binding; never publish capabilities."""
    statuses = []
    if _m_common.outward_links(inst.root):
        raise RehearsalError('outward-linked brain cannot probe delivery')
    if probe_inst is not None and probe_inst.root.resolve() == inst.root.resolve():
        raise RehearsalError('delivery cache requires a private instance')
    seen = set()
    for node in nodes:
        if not node['local'].get('coordinator') and node['id'] != 'root':
            continue
        _, address = _m_hosts.wake_target(inst, node)
        if not address.startswith(('https://', 'http://')) or address in seen:
            continue  # shell transports are not webhooks; never execute them here
        seen.add(address)
        try:
            if probe_inst is None:
                with tempfile.TemporaryDirectory(prefix='brain-rehearse-probe-') as td:
                    private = isolated_probe_instance(inst, Path(td))
                    status = _m_hosts.wake_probe(private, private.node(node['id']), force=True)
            else:
                status = _m_hosts.wake_probe(probe_inst, probe_inst.node(node['id']), force=True)
        except Exception:
            status = 0
        statuses.append({'coordinator': len(statuses), 'status': status if type(status) is int else 0})
    return statuses


def delivery_regressions(statuses):
    return ['wake:' + str(p['coordinator']) + ': HTTP ' + str(p['status'])
            for p in statuses if not 200 <= p['status'] < 300]


def snapshot(root, cache=None):
    """Hash regular files and link text, never traverse links or read their targets."""
    out = {}
    for dp, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            p = Path(dp) / name
            rel = p.relative_to(root).as_posix()
            if p.is_symlink() or p.is_dir() or p.is_file():
                out[rel] = snapshot_entry(p, cache)
    return out


def snapshot_entry(p, cache=None):
    """Read one entry; callers must keep its parent directories link-free."""
    if p.is_symlink():
        return 'link:' + os.readlink(p)
    if p.is_dir():
        return 'dir:' + str(p.stat().st_mode)
    if not p.is_file():
        return 'missing'
    st = p.stat()
    identity = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_mode)
    if cache is not None and cache.get(str(p), (None,))[0] == identity:
        return cache[str(p)][1]
    h = hashlib.sha256()
    with p.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    value = str(st.st_mode) + ':' + h.hexdigest()
    if cache is not None:
        cache[str(p)] = (identity, value)
    return value


def validate_git_metadata(root):
    """Refuse linked Git administration before asking Git to read it."""
    metadata = root / '.git'
    if root.is_symlink() or metadata.is_symlink():
        raise RehearsalError('linked Git metadata refused')
    if not metadata.exists():
        raise RehearsalError('source stability metadata unavailable')
    # Inspect gitfile/commondir paths before Git can canonicalize linked parents
    # or read linked config/index/refs. Both paths may be relative to their owner.
    own = metadata
    if metadata.is_file():
        pointer = metadata.read_text()
        if not pointer.startswith('gitdir: '):
            raise RehearsalError('source stability metadata unavailable')
        own = root / pointer[8:].strip()
    if any(p.is_symlink() for p in (own, *own.parents)):
        raise RehearsalError('linked Git metadata refused')
    if (own / 'commondir').is_symlink():
        raise RehearsalError('linked Git metadata refused')
    common = own / (own / 'commondir').read_text().strip() if (own / 'commondir').is_file() else own
    # Git itself reads these paths. Refuse links before asking it for index/refs;
    # private ref namespaces are neither read nor traversed by this check.
    for base in {own, common}:
        for parent in (base, *base.parents):
            if parent.is_symlink():
                raise RehearsalError('linked Git metadata refused')
        for rel in ('config', 'config.worktree', 'index', 'HEAD', 'packed-refs', 'refs',
                    'refs/heads', 'refs/tags'):
            path = base / rel
            if path.is_symlink():
                raise RehearsalError('linked Git metadata refused')
            if rel in ('refs/heads', 'refs/tags') and path.is_dir():
                # Only inspect link types: transient ref locks may disappear and
                # must not be opened or hashed while an unrelated writer runs.
                for dp, dirs, files in os.walk(path, followlinks=False):
                    if any((Path(dp) / name).is_symlink() for name in dirs + files):
                        raise RehearsalError('linked Git metadata refused')
    return own


def stability_snapshot(root):
    """Bind tracked bytes and refs; retained for callers inspecting fixed trees."""
    own = validate_git_metadata(root)
    def git(*args):
        r = _m_common.git(*args, cwd=root)
        if r.returncode:
            raise RehearsalError('source stability metadata unavailable')
        return r.stdout
    out = {}
    listing = subprocess.run(['git', '-C', str(root), 'ls-files', '--cached', '-z'], capture_output=True)
    if listing.returncode:
        raise RehearsalError('source stability metadata unavailable')
    tracked = {os.fsdecode(rel) for rel in listing.stdout.split(b'\0') if rel}
    for rel in tracked:
        path = root
        for part in Path(rel).parts:
            path = path / part
            if path.is_symlink() or not path.is_dir():
                break  # do not read a tracked descendant through a replaced parent
        key = path.relative_to(root).as_posix()
        out[key] = snapshot_entry(path)
        if not path.is_symlink() and path.is_dir() and key == rel:
            out[key] = stability_snapshot(path) if (path / '.git').exists() else snapshot(path)
    # A manually installed core may be outside the parent's index, but its bytes
    # are still executable rehearsal inputs. Preserve coverage for that layout.
    core = root / 'core'
    if core.exists() and not any(rel == 'core' or rel.startswith('core/') for rel in tracked):
        out['core'] = (snapshot_entry(core) if core.is_symlink() else
                       stability_snapshot(core) if (core / '.git').exists() else snapshot(core))
    out['.git:refs'] = git('for-each-ref', '--format=%(refname) %(objectname) %(symref)',
                           'refs/heads', 'refs/tags')
    out['.git:HEAD'] = (own / 'HEAD').read_text()
    return out


def copy_tree(source, dest):
    # copy2 creates independent inodes, including for source hard links.
    shutil.copytree(source, dest, symlinks=True)
    relocate_links(source, dest)
    capture_gitfiles(source, dest)


def capture_gitfiles(source, dest):
    """Capture external administration before this copy operation returns.

    Administration already copied with the tree (or another common directory)
    must be reused, not fetched again from a moving live submodule/worktree.
    """
    mappings = [(Path(os.path.abspath(source)), dest)]
    def captured(path):
        path = Path(os.path.abspath(path))
        for original, private in sorted(mappings, key=lambda pair: len(str(pair[0])), reverse=True):
            try:
                return private / path.relative_to(original)
            except ValueError:
                pass
        return None
    for dp, dirs, files in os.walk(dest, followlinks=False):
        dirs[:] = [name for name in dirs if name != '.git']
        metadata = Path(dp) / '.git'
        if '.git' not in files or metadata.is_symlink():
            continue
        original_repo = source / Path(dp).relative_to(dest)
        own = Path(os.path.abspath(validate_git_metadata(original_repo)))
        common = (Path(os.path.abspath(own / (own / 'commondir').read_text().strip()))
                  if (own / 'commondir').is_file() else own)
        private_common = captured(common)
        metadata.unlink()
        if private_common is not None:
            if not private_common.is_dir():
                raise RehearsalError('captured Git administration unavailable')
            copy_tree(private_common, metadata)
        else:
            copy_tree(common, metadata)
            mappings.append((common, metadata))
        private_own = captured(own)
        for name in ('HEAD', 'index'):
            origin = (private_own if private_own is not None else own) / name
            if origin.is_file() and origin != metadata / name:
                shutil.copy2(origin, metadata / name)
        # The copied administration's old relative worktree binding is invalid
        # at its new location. Remove it before any Git discovery in this copy.
        copied_locks = []
        for mp, md, mf in os.walk(metadata, followlinks=False):
            if any((Path(mp) / name).is_symlink() for name in md + mf):
                raise RehearsalError('linked Git metadata refused')
            copied_locks.extend(Path(mp) / name for name in mf if name.endswith('.lock'))
        for lock in copied_locks:
            lock.unlink()
        (metadata / 'config.worktree').unlink(missing_ok=True)
        unset = _m_common.git('--git-dir=' + str(metadata), '--work-tree=' + str(Path(dp)),
                              'config', '--no-includes', '--file', str(metadata / 'config'),
                              '--unset-all', 'core.worktree', cwd=Path(dp))
        if unset.returncode not in (0, 5):
            raise RehearsalError('captured Git worktree binding unavailable')


def relocate_links(source, dest):
    """Keep absolute internal aliases inside every relocated copy's own tree."""
    for dp, dirs, files in os.walk(dest, followlinks=False):
        for name in dirs + files:
            link = Path(dp) / name
            if not link.is_symlink():
                continue
            target = Path(os.readlink(link))
            if not target.is_absolute():
                continue
            try:
                suffix = target.relative_to(source)
            except ValueError:
                continue
            # Preserve alias/.. semantics: do not resolve or normalize the suffix
            # through either the live tree or copied links while rewriting it.
            relative = Path(os.path.relpath(dest, link.parent)) / suffix
            link.unlink()
            link.symlink_to(relative)


def validate_frozen_links(root):
    """No surviving alias may observe an external/live target, even read-only."""
    for dp, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            link = Path(dp) / name
            if link.is_symlink():
                try:
                    target = link.resolve()
                except (OSError, RuntimeError):
                    raise RehearsalError('cyclic or unavailable frozen link refused') from None
                if target != root and root not in target.parents:
                    raise RehearsalError('outward-linked frozen brain refused')


def materialize_git(source, dest):
    """Detach copied submodules/worktrees from their original Git administration."""
    if not (source / '.git').exists():
        return
    validate_git_metadata(source)
    def resolve(arg):
        r = _m_common.git('rev-parse', '--path-format=absolute', arg, cwd=source)
        if r.returncode:
            raise RehearsalError('Git metadata could not be copied safely')
        return Path(r.stdout.strip())
    own, common = resolve('--git-dir'), resolve('--git-common-dir')
    target = dest / '.git'
    if not (own == common and target.is_dir() and not target.is_symlink()):
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)
        copy_tree(common, target)
    # Preparation itself is outside the child sandbox. Never follow copied Git
    # metadata links while detaching it from the original repository.
    for dp, dirs, files in os.walk(target, followlinks=False):
        for name in dirs + files:
            if (Path(dp) / name).is_symlink():
                raise RehearsalError('linked Git metadata refused')
        # Source writers do not exist in the detached copy. Their copied lock
        # files would otherwise prevent safe config rewriting and later probes.
        for name in files:
            if name.endswith('.lock'):
                (Path(dp) / name).unlink()
    for name in ('HEAD', 'index'):
        if (own / name).is_file() and own / name != target / name:
            shutil.copy2(own / name, target / name)
    # A copied submodule config can still name its old relative worktree. Bind
    # both directories explicitly while reading it, before replacing that config.
    config = _m_common.git('--git-dir=' + str(target), '--work-tree=' + str(dest),
                           'config', '--no-includes', '--file', str(target / 'config'),
                           '--null', '--list', cwd=dest)
    if config.returncode:
        raise RehearsalError('Git format configuration unreadable')
    formats = []
    for entry in config.stdout.split('\0'):
        key, _, value = entry.partition('\n')
        if key == 'core.repositoryformatversion' or key.startswith('extensions.'):
            formats.append((key, value))
    for name in ('commondir', 'gitdir', 'worktrees', 'hooks', 'config.worktree'):
        p = target / name
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        elif p.exists() or p.is_symlink():
            p.unlink()
    # Never execute repository hooks or load include/configured external helpers.
    (target / 'config').write_text('[core]\n\trepositoryformatversion = 0\n\tbare = false\n\thooksPath = /dev/null\n')
    for key, value in formats:
        result = _m_common.git('config', '--file', str(target / 'config'), '--replace-all', key, value, cwd=dest)
        if result.returncode:
            raise RehearsalError('Git format configuration could not be preserved')

    validate_git_objects(dest)
    listing = subprocess.run(['git', '-C', str(source), 'ls-files', '--stage', '-z'], capture_output=True)
    if listing.returncode:
        raise RehearsalError('Git metadata could not be copied safely')
    gitlinks = {os.fsdecode(entry.partition(b'\t')[2]) for entry in listing.stdout.split(b'\0')
                if entry.startswith(b'160000 ') and entry.partition(b'\t')[2]}
    for rel in gitlinks:
        child = source / rel
        if not child.is_symlink() and (child / '.git').exists():
            materialize_git(child, dest / rel)
    # Also detach a manual, untracked core before its copy is used as a baseline.
    if ((source / 'core/.git').exists() and not (source / 'core').is_symlink()
            and 'core' not in gitlinks):
        materialize_git(source / 'core', dest / 'core')


def validate_git_objects(root):
    """Reject incomplete copied object stores without depending on private refs."""
    env = {k: v for k, v in os.environ.items() if k in ('PATH', 'LANG', 'LC_ALL', 'TZ')}
    env.update(HOME=str(root), GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1',
               GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0', GIT_NO_REPLACE_OBJECTS='1',
               GIT_NO_LAZY_FETCH='1')
    r = subprocess.run(['git', '-C', str(root), 'rev-list', '--quiet', '--objects',
                        '--missing=error', 'HEAD', '--branches', '--tags'],
                       env=env, capture_output=True)
    if r.returncode:
        raise RehearsalError('live brain moved while copying; retry with a stable source')


def normalize(text, roots=()):
    for root in sorted(map(str, roots), key=len, reverse=True):
        text = text.replace(root, '<copy>')
    text = re.sub(r'\b\d{4}-\d\d-\d\d[ T]\d\d:\d\d(?::\d\d(?:\.\d+)?)?(?:Z| ?[+-]\d\d:\d\d)?', '<time>', text)
    # Path substitutions happen after brief measurement. Different copy paths
    # therefore change byte totals even when the warning itself is unchanged.
    text = '\n'.join(normalize_warning(line) if is_warning(line) else line
                     for line in text.splitlines())
    # JSON object ordering must not create a diff; list ordering remains significant.
    try:
        return json.dumps(json.loads(text), sort_keys=True, ensure_ascii=False)
    except ValueError:
        return text.strip()


def is_warning(line):
    return re.search(r'warn|limit|stop|consent|✗|!', line, re.I)


def normalize_warning(line):
    return re.sub(r'\b\d+(?:\.\d+)?\s*(bytes?|[KMGT]i?B)\b', r'<size> \1', line, flags=re.I)


def expected_change_notices(inst, nodes, baseline, candidate):
    """Attribute only exact notices made solely from candidate CHANGES entries.

    Stable identities handle rewritten existing notices. New notices need this
    additional provenance check; a blanket exemption for core-changes would hide
    scheduler regressions. Keep rendering aligned with status.changes_due.
    """
    def entries(core):
        path = core / 'CHANGES.md'
        return _m_upgrade.changes_entries(path.read_text()) if path.is_file() else []
    old = {entry['id']: entry for entry in entries(baseline)}
    fresh = entries(candidate)
    changed = {entry['id'] for entry in fresh if old.get(entry['id']) != entry}
    expected = {}
    terms = [term for node in nodes for term in _m_guard.never_terms(node)]
    for i, node in enumerate(nodes):
        if node['id'] != 'root' and not node['local'].get('coordinator'):
            continue
        seen = inst.local.get('changes_seen', {}).get(node['id'])
        if seen is None:
            seen = _m_upgrade.changes_baseline(inst)
        if seen is None:
            continue  # A never-upgraded brain starts with no unread entries.
        unread = [entry for entry in fresh if entry['id'] not in set(seen)]
        if not unread or any(entry['id'] not in changed for entry in unread):
            continue
        text = (f"core changed: {len(unread)} unread entr{'y' if len(unread) == 1 else 'ies'} ("
                + '; '.join(entry['id'] for entry in unread[:3]) + (' …' if len(unread) > 3 else '')
                + f"); read them: brain changes {node['id']}, then brain changes {node['id']} --seen")
        item = dict(node=node['id'], kind='core-changes', text=_m_nodes.import_text(text, _m_guard.never_terms(node)))
        expected['due:' + str(i)] = json.loads(redact_output(normalize(json.dumps(item)), terms))
    return expected


def redact_text(text, terms):
    """Mask folded matches, keeping surrounding probe content comparable.

    guard.redact can blank an entire output for full Unicode folds. Map folded
    offsets back to source characters instead, and merge all overlapping matches
    before replacing them so term order cannot expose or partially mask a match.
    """
    folded, offsets = [], []
    for i, char in enumerate(text):
        part = char.casefold()
        folded.append(part)
        offsets.extend([i] * len(part))
    folded = ''.join(folded)
    spans = []
    for term, _ in terms:
        needle = term.casefold()
        if not needle:
            continue
        start = folded.find(needle)
        while start >= 0:
            spans.append((offsets[start], offsets[start + len(needle) - 1] + 1))
            start = folded.find(needle, start + 1)
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    parts, cursor = [], 0
    for start, end in merged:
        parts.extend((text[cursor:start], '[…]'))
        cursor = end
    return ''.join(parts) + text[cursor:]


def redact_output(text, terms):
    # Match decoded JSON strings too, including names with quotes/backslashes.
    # Replace individual tokens rather than rebuilding dicts: protected keys
    # may redact to the same spelling, but must not collapse distinct fields.
    try:
        json.loads(text)
    except ValueError:
        pass
    else:
        text = re.sub(r'"(?:[^"\\]|\\.)*"',
                      lambda m: json.dumps(redact_text(json.loads(m[0]), terms), ensure_ascii=False), text)
    return redact_text(text, terms)


def compare(before, after, expected_changes=None):
    """Return content-free regression labels; exact output stays in memory only."""
    flags = []
    for key, old in before.items():
        new = after[key]
        if new['timeout']:
            flags.append(key + ': timeout')
        if new['code'] != 0 and (old['code'] == 0 or new['code'] != old['code']):
            flags.append(key + ': new command failure')
        errors = lambda x: collections.Counter(l for l in x['text'].splitlines() if '✗' in l)
        if errors(new) - errors(old):
            flags.append(key + ': new check errors')
        ready = lambda x: x['code'] == 0 and x['text'].splitlines()[-1:] == ['ready']
        if key.startswith('ready:') and ready(old) and not ready(new):
            flags.append(key + ': ready became not ready')
        if key.startswith('due:') and not identical_baseline_failure(old, new):
            try:
                def items(value):
                    parsed = json.loads(value['text'])
                    if not isinstance(parsed, list) or any(not isinstance(item, dict) for item in parsed):
                        raise ValueError('invalid due items')
                    return parsed
                identity = lambda item: json.dumps([item.get(k) for k in ('node', 'kind', 'id')], sort_keys=True)
                previous, current = items(old), items(new)
                added = (collections.Counter(map(identity, current))
                         - collections.Counter(map(identity, previous)))
                expected = (expected_changes or {}).get(key)
                if (expected is not None and expected in current and added[identity(expected)]
                        and all(identity(item) != identity(expected) for item in previous)):
                    added[identity(expected)] -= 1
                if +added:
                    flags.append(key + ': new due items')
            except (ValueError, TypeError):
                flags.append(key + ': invalid due output')
        if key.startswith('brief:'):
            warnings = lambda x: collections.Counter(normalize_warning(l) for l in x['text'].splitlines()
                                                      if is_warning(l))
            if warnings(old) - warnings(new):
                flags.append(key + ': brief warning disappeared')
        if new['unexpected']:
            flags.append(key + ': unexpected writes')
        if key == 'hook' and new['seconds'] > 1:
            flags.append('hook: latency exceeds 1 s')
    return flags


def identical_baseline_failure(old, new):
    # Text is already normalized and redacted, exactly as for evidence digests.
    return (old['code'] != 0 and old['code'] == new['code'] and old['text'] == new['text']
            and not any(r['timeout'] or r['unexpected'] for r in (old, new)))


def sandbox_prefix(folder):
    exe = Path('/usr/bin/sandbox-exec')
    if sys.platform != 'darwin' or not exe.is_file():
        raise RehearsalError('rehearsal requires an OS write/network sandbox (currently macOS sandbox-exec)')
    # Refuse a shadow executable as well as avoiding its execution. The caller's
    # PATH is also inherited by Git/Python helpers, so don't silently trust it.
    found = shutil.which('sandbox-exec')
    if found is not None and Path(found).resolve() != exe:
        raise RehearsalError('shadowed sandbox executable refused')
    # Sandbox string literals use JSON-compatible escaping. File writes are allowed
    # only to this command's copy; even another lane's copy is read-only.
    profile = ('(version 1)(allow default)(deny network*)(deny mach-lookup)(deny appleevent-send)'
               '(deny signal (target others))(deny file-write*)'
               '(allow file-write* (subpath ' + json.dumps(str(folder.resolve())) + '))'
               '(allow file-write* (literal "/dev/null"))')
    return [str(exe), '-p', profile]


def sandbox_self_check(folder):
    """Prove denied writes and network, not merely that a launcher exits zero."""
    import socket
    with tempfile.TemporaryDirectory(prefix='brain-rehearse-denied-') as other:
        target = Path(other) / 'canary'
        target.write_bytes(b'unchanged')
        with socket.socket() as server:
            server.bind(('127.0.0.1', 0))
            server.listen(1)
            script = '''import pathlib, socket, sys
denied = 0
try:
    pathlib.Path(sys.argv[1]).write_bytes(b'changed')
except OSError:
    denied += 1
try:
    socket.create_connection(('127.0.0.1', int(sys.argv[2])), timeout=1).close()
except OSError:
    denied += 1
sys.exit(0 if denied == 2 else 1)
'''
            result = subprocess.run([*sandbox_prefix(folder), sys.executable, '-I', '-B', '-c', script,
                                     str(target), str(server.getsockname()[1])], capture_output=True, timeout=5)
            if result.returncode or target.read_bytes() != b'unchanged':
                raise RehearsalError('sandbox enforcement self-check failed')


def allowed_write(rel, kind):
    if (rel.startswith('home/.local/state/brain/') or rel.startswith('tmp/')
            or rel.startswith('home/Library/Caches/com.apple.DeveloperTools/')
            or rel in ('home/.local', 'home/.local/state', 'home/.local/state/brain',
                       'home/brains.json', 'home/write-attempt', 'home/Library', 'home/Library/Caches',
                       'home/Library/Caches/com.apple.DeveloperTools')):
        return True
    if not rel.startswith('brain/'):
        return False
    rel = rel[6:]
    if (rel in ('.brain/lock', '.brain/machine.json', '.brain/core-remote.json', '.brain/hooks.json',
                '.brain/hooks.log', '.brain/inbox-seen.json', '.brain/wake', '.brain/host-questions', '.brain/spend')
            or rel.startswith(('.brain/wake/', '.brain/host-questions/', '.brain/spend/'))):
        return True
    if rel == '.brain/local.json':
        return kind == 'fix'  # the supported migration moves machine bindings here
    if rel in ('.brain',):
        return True
    note = re.fullmatch(r'(?:nodes/[^/]+/)*([^/]+)', rel)
    if kind == 'fix':
        return (rel.startswith('.git/') or rel in ('.gitignore', 'registry.md')
                or (note and note[1] in ('node.md', 'status.md', 'brief.md', 'AGENTS.md', 'CLAUDE.md',
                                         'inbox.md', 'decisions.md'))
                or bool(re.fullmatch(r'(?:nodes/[^/]+/)*(?:\.claude|\.codex|\.agents)(?:/(?:settings.json|hooks.json|skills))?', rel)))
    if kind == 'refresh':
        return rel.startswith('.git/') or (note and note[1] == 'status.md')
    return False


def run_command(core, lane, args, kind, roots, terms=(), cache=None):
    brain = lane / 'brain'
    home = lane / 'home'
    home.mkdir(exist_ok=True)
    tmp = lane / 'tmp'
    tmp.mkdir(exist_ok=True)
    unrelated = lane / 'unrelated'
    unrelated.mkdir(exist_ok=True)
    if kind == 'hook':
        (home / 'brains.json').write_text(json.dumps({
            'brains': {'rehearsal': {'root': str(brain)}}, 'default': 'rehearsal'}))
    bootstrap = lane / 'bootstrap'
    bootstrap.mkdir(exist_ok=True)
    # Every Python helper inherits this supervisor. Detached wake drains remain
    # in the command's group, and each Python parent reaps its children on exit.
    (bootstrap / 'sitecustomize.py').write_text('''import atexit, subprocess
children = []
original = subprocess.Popen
class SupervisedPopen(original):
    def __init__(self, *args, **kwargs):
        kwargs['start_new_session'] = False
        super().__init__(*args, **kwargs)
        children.append(self)
subprocess.Popen = SupervisedPopen
def reap():
    for child in reversed(children):
        if child.poll() is None:
            try:
                child.kill()
            except ProcessLookupError:
                pass
            child.wait(timeout=5)
atexit.register(reap)
''')
    audit = lane / 'audit.py'
    violation = home / 'write-attempt'
    violation.unlink(missing_ok=True)
    # OS sandboxing is the enforcement boundary. The audit hook additionally
    # observes Python attempts even when candidate code catches a denied write.
    audit.write_text('''import os, pathlib, runpy, sys
boundary = pathlib.Path(sys.argv[1]).resolve()
marker = pathlib.Path(sys.argv[2])
entry = sys.argv[3]
sys.argv = sys.argv[3:]
busy = False
def audit(event, args):
    global busy
    if busy:
        return
    paths = []
    if event == 'open':
        mode, flags = args[1:3]
        if (isinstance(mode, str) and any(c in mode for c in 'wax+')) or (isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)):
            paths = args[:1]
    elif event in ('os.remove', 'os.rmdir', 'os.mkdir', 'os.chmod', 'os.chown', 'os.truncate', 'os.utime'):
        paths = args[:1]
    elif event in ('os.rename', 'os.link'):
        paths = args[:2]
    elif event == 'os.symlink':
        paths = args[1:2]
    busy = True
    try:
        for name in paths:
            if not isinstance(name, (str, bytes)):
                continue
            p = pathlib.Path(os.fsdecode(name)).resolve()
            if p != pathlib.Path('/dev/null') and boundary != p and boundary not in p.parents:
                marker.write_text('attempted')
    finally:
        busy = False
sys.addaudithook(audit)
runpy.run_path(entry, run_name='__main__')
''')
    # Deliberately build an allowlisted environment. No inherited Brain lock,
    # provider credentials, registries, Python startup hooks or Git overrides.
    env = {k: v for k, v in os.environ.items() if k in ('PATH', 'LANG', 'LC_ALL', 'TZ')}
    env.update(HOME=str(home), TMPDIR=str(tmp), BRAIN_CONFIG=str(home / 'brains.json'),
               PYTHONPATH=str(bootstrap),
               XDG_CONFIG_HOME=str(home / '.config'), BRAIN_ROOT=str(brain),
               BRAIN_NO_REEXEC='1', BRAIN_NO_UPDATE_CHECK='1', PYTHONDONTWRITEBYTECODE='1',
               BRAIN_USAGE_TEST_NO_KEYCHAIN='1', GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1',
               GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0',
               GIT_AUTHOR_NAME='Rehearsal', GIT_AUTHOR_EMAIL='rehearsal@example.invalid',
               GIT_COMMITTER_NAME='Rehearsal', GIT_COMMITTER_EMAIL='rehearsal@example.invalid')
    before = snapshot(lane, cache)
    start = time.monotonic()
    p = subprocess.Popen([*sandbox_prefix(lane), sys.executable, '-B', str(audit), str(lane), str(violation),
                          str(core / 'bin/brain'), *args],
                         cwd=unrelated if kind == 'hook' else brain, env=env, stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors='replace', start_new_session=True)
    timed_out = False
    try:
        out, err = p.communicate(json.dumps({'cwd': str(unrelated)}) if kind == 'hook' else '', timeout=60)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(p.pid, signal.SIGKILL)
        out, err = p.communicate()
    finally:
        # Stop remaining processes in this command's group. Any detached helper
        # still inherits the OS sandbox and cannot write to the live machine.
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elapsed = time.monotonic() - start
    # Normalize JSON escapes before redaction as well: an escaped spelling must
    # not become a protected literal after the privacy filter has run.
    text = redact_output(normalize(out + err, roots), terms)
    after = snapshot(lane, cache)
    changed = {k for k in before.keys() | after.keys() if before.get(k) != after.get(k)}
    return {'text': text, 'code': p.returncode, 'timeout': timed_out,
            'seconds': elapsed, 'unexpected': sum(not allowed_write(k, kind) for k in changed) + int(violation.exists())}


def run_lane(source, core, folder, nodes, commands):
    folder.mkdir()
    copy_tree(source, folder / 'brain')
    materialize_git(source, folder / 'brain')
    # A core submodule's .git file would otherwise point back to live metadata.
    old_core = folder / 'brain/core'
    if old_core.is_symlink():
        old_core.unlink()
    elif old_core.exists():
        shutil.rmtree(old_core)
    copy_tree(core, old_core)
    validate_frozen_links(folder / 'brain')
    results = {}
    cache = {}
    mutations = {}
    terms = [t for node in nodes for t in _m_guard.never_terms(node)]
    for key, args, kind in commands:
        if kind in ('fix', 'refresh'):
            # Mutating probes get independent copies, never mask errors in later probes.
            lane = folder / ('mutation-' + kind)
            if kind not in mutations:
                lane.mkdir()
                copy_tree(folder / 'brain', lane / 'brain')
                mutations[kind] = lane
        else:
            lane = folder
        results[key] = run_command(lane / 'brain/core', lane, args, kind,
                                   [lane / 'brain', folder / 'brain', source, core, folder, lane], terms, cache)
    return results


def rehearse(inst, n, branch):
    if not n['external']:
        raise RehearsalError('rehearsal needs an external core home and workbench')
    head, main = _m_verify.ready_refs(n, branch)
    integration_base = _m_verify.integration_parent(n, main)
    if not head or not main:
        raise RehearsalError('candidate branch or cached integration main missing')
    # Probe admission before any candidate is executed. The sandbox protects live
    # homes even when local.json points to them; they are read-only observations.
    with tempfile.TemporaryDirectory(prefix='brain-rehearse-') as td:
        folder = Path(td).resolve()
        sandbox_self_check(folder)
        frozen = folder / 'source'
        copy_tree(inst.root, frozen)
        validate_frozen_links(frozen)
        materialize_git(frozen, frozen)
        # The copy is common fixed input, not an atomic historical snapshot.
        # Live writers are irrelevant after preparation; audit only private data.
        baseline_commit = installed_core_commit(frozen)
        # Keep core-relative links into the captured brain valid until the core
        # is installed into a lane. No extra standalone baseline copy is needed.
        baseline = frozen / 'core'
        candidate = folder / 'candidate'
        # Clone transports only Git objects; do not run setup, hooks or a checkout
        # in the original home. No hard links and no origin survives in the copy.
        prep_home = folder / 'prepare-home'
        prep_home.mkdir()
        prep_env = {k: v for k, v in os.environ.items() if k in ('PATH', 'LANG', 'LC_ALL', 'TZ')}
        prep_env.update(HOME=str(prep_home), TMPDIR=str(folder), GIT_CONFIG_GLOBAL='/dev/null',
                        GIT_CONFIG_NOSYSTEM='1', GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0')
        prefix = sandbox_prefix(folder)
        r = subprocess.run([*prefix, 'git', '-c', 'core.hooksPath=/dev/null', 'clone', '--no-local', '--no-hardlinks',
                            '--no-checkout', '--quiet', str(n['home_path']), str(candidate)],
                           env=prep_env, capture_output=True)
        if r.returncode:
            raise RehearsalError('candidate snapshot failed')
        r = subprocess.run([*prefix, 'git', '-C', str(candidate), '-c', 'core.hooksPath=/dev/null',
                            'checkout', '--detach', '--quiet', head], env=prep_env, capture_output=True)
        if r.returncode or not (candidate / 'bin/brain').is_file():
            raise RehearsalError('candidate is not a core checkout')
        validate_frozen_links(candidate)
        subprocess.run([*prefix, 'git', '-C', str(candidate), 'remote', 'remove', 'origin'],
                       env=prep_env, capture_output=True, check=True)
        commands = []
        frozen_inst = _m_instance.Instance(frozen)
        nodes = frozen_inst.nodes()
        for i, node in enumerate(nodes):
            commands.append(('due:' + str(i), ['due', node['id'], '--json'], 'read'))
            commands.append(('brief:' + str(i), ['brief', node['id']], 'read'))
            for j, row in enumerate(_m_jobs.read_jobs(node) or []):
                if not row.get('Ended'):
                    commands.append(('ready:' + str(i) + ':' + str(j),
                                     ['ready', node['id'], '--branch', row['Job']], 'read'))
            commands.append(('refresh:' + str(i), ['status', node['id'], '--refresh'], 'refresh'))
        commands += [('check', ['check'], 'read'), ('fix', ['check', '--fix'], 'fix'),
                     ('ui', ['ui', '--json'], 'read'), ('hook', ['hook', 'stop'], 'hook')]
        # Seed the same real delivery result into both command copies. Otherwise
        # a new check's cache miss would POST inside the network-denying sandbox.
        delivery_source = folder / 'delivery-source'
        delivery_source.mkdir()
        delivery_inst = isolated_probe_instance(frozen_inst, delivery_source)
        delivery = probe_delivery(delivery_inst, delivery_inst.nodes(), frozen_inst)
        expected = expected_change_notices(frozen_inst, nodes, baseline, candidate)
        original = snapshot(frozen)
        # Equal-length paths prevent measured sizes from changing warning
        # thresholds, top-section selection, and section order before normalization.
        old = run_lane(frozen, baseline, folder / 'before', nodes, commands)
        new = run_lane(frozen, candidate, folder / 'after_', nodes, commands)
        flags = compare(old, new, expected)
        if snapshot(frozen) != original:
            flags.append('frozen brain changed during rehearsal; evidence refused')
        warnings = []
        for key, result in old.items():
            if identical_baseline_failure(result, new[key]):
                warnings.append(key + ': identical baseline failure')
            elif result['timeout'] or result['unexpected'] or result['code']:
                flags.append(key + ': baseline incomplete')
        if (_m_verify.ready_refs(n, branch) != (head, main)
                or _m_verify.integration_parent(n, main) != integration_base):
            flags.append('candidate or integration main moved during rehearsal')
        flags.extend(delivery_regressions(delivery))
        return {'version': 1, 'node': n['id'], 'branch': branch, 'head': head, 'main': main,
                'installed_core_commit': baseline_commit,
                'integration_base': integration_base,
                'checked_at': _m_jobs.stamp(frozen_inst), 'passed': not flags, 'regressions': flags,
                'warnings': warnings,
                'commands': len(commands), 'changed_outputs': sum(old[k]['text'] != new[k]['text'] for k in old),
                'hook_seconds': new['hook']['seconds'],
                'delivery': delivery,
                'output_digests': {k: hashlib.sha256(new[k]['text'].encode()).hexdigest() for k in new}}


def cmd_rehearse(a):
    # Do not register or rewrite machine bindings while observing the live brain.
    inst = _m_instance.Instance(_m_nodes.find_root())
    n = inst.node(a.id)
    try:
        if not n['external'] or not n.get('home_path'):
            raise RehearsalError('external candidate home required')
        path = evidence_path(n, a.branch)
        # Refuse linked evidence directories, including links into a live home.
        for parent in [path, *path.parents]:
            if parent.is_symlink():
                raise RehearsalError('linked evidence path refused')
        for protected in (inst.root.resolve(), n['home_path'].resolve()):
            if path.resolve() == protected or protected in path.resolve().parents:
                raise RehearsalError('evidence must stay outside live brain and home')
        path.unlink(missing_ok=True)  # a failed repeat must not resurrect an earlier pass
        result = rehearse(inst, n, a.branch)
        if _m_guard.find_terms(json.dumps(result), [t for node in inst.nodes() for t in _m_guard.never_terms(node)]):
            raise RehearsalError('protected evidence refused')
    except (RehearsalError, OSError, subprocess.SubprocessError):
        # Exception messages and command output can contain protected source content.
        _m_common.die('rehearsal refused or incomplete; no passing evidence recorded')
    path.parent.mkdir(parents=True, exist_ok=True)
    _m_common.atomic_write(path, json.dumps(result, indent=2) + '\n')
    print('rehearse: ' + ('passed' if result['passed'] else 'failed') +
          '; commands=' + str(result['commands']) + '; regressions=' + str(len(result['regressions'])))
    for flag in result['regressions']:
        print('- ' + flag)
    for warning in result.get('warnings', []):
        print('warning: ' + warning)
    if not result['passed']:
        sys.exit(1)
