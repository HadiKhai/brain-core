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
from . import verify as _m_verify


class RehearsalError(Exception):
    pass


def evidence_path(n, branch):
    # Evidence stays in the workbench, leaving even instance Git byte-identical.
    from . import worktrees as _m_worktrees
    key = hashlib.sha256(branch.encode()).hexdigest()
    root = n
    while root.get('parent'):
        root = root['parent']
    inst = _m_instance.Instance(root['dir'])
    return _m_worktrees.workbench(inst, n) / 'scratch' / 'rehearse' / (key + '.json')


def read_evidence(n, branch, head):
    if not n['external'] or not n.get('home_path') or not head:
        return None
    try:
        p = json.loads(evidence_path(n, branch).read_text())
        if (isinstance(p, dict) and p.get('version') == 1 and p.get('node') == n['id'] and p.get('branch') == branch
                and p.get('head') == head and p.get('passed') is True
                and p.get('main') == _m_verify.ready_refs(n, branch)[1]
                and p.get('integration_base') == _m_verify.integration_parent(n, p.get('main'))):
            return p
    except (OSError, ValueError, TypeError):
        pass
    return None


def required(n):
    return n['fm'].get('rehearse') == 'required'


def probe_delivery(inst, nodes):
    """One authorized real POST per bound coordinator; never publish capabilities."""
    import urllib.error
    import urllib.request
    statuses = []
    for node in nodes:
        if not node['local'].get('coordinator') and node['id'] != 'root':
            continue
        _, address = _m_hosts.wake_target(inst, node)
        if not address.startswith(('https://', 'http://')):
            continue  # shell transports are not webhooks; never execute them here
        try:
            if hasattr(_m_hosts, 'wake_probe'):
                status = _m_hosts.wake_probe(inst, node)
            else:
                # TODO(job/wake-agent): use hosts.wake_probe once it lands.
                class NoRedirect(urllib.request.HTTPRedirectHandler):
                    def redirect_request(self, req, fp, code, msg, headers, newurl):
                        return None
                opener = urllib.request.build_opener(NoRedirect)
                request = urllib.request.Request(address, data=b'{"event":"brain-wake"}',
                    headers={'Content-Type': 'application/json', 'User-Agent': 'brain-rehearse/1'})
                with opener.open(request, timeout=10) as response:
                    status = response.status
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
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
            if p.is_symlink():
                out[rel] = 'link:' + os.readlink(p)
            elif p.is_dir():
                out[rel] = 'dir:' + str(p.stat().st_mode)
            elif p.is_file():
                st = p.stat()
                identity = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_mode)
                if cache is not None and cache.get(str(p), (None,))[0] == identity:
                    out[rel] = cache[str(p)][1]
                    continue
                h = hashlib.sha256()
                with p.open('rb') as f:
                    for chunk in iter(lambda: f.read(1024 * 1024), b''):
                        h.update(chunk)
                out[rel] = str(st.st_mode) + ':' + h.hexdigest()
                if cache is not None:
                    cache[str(p)] = (identity, out[rel])
    return out


def copy_tree(source, dest):
    # copy2 creates independent inodes, including for source hard links.
    shutil.copytree(source, dest, symlinks=True)


def materialize_git(source, dest):
    """Detach copied submodules/worktrees from their original Git administration."""
    if not (source / '.git').exists():
        return
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
    for name in ('HEAD', 'index'):
        if (own / name).is_file():
            shutil.copy2(own / name, target / name)
    for name in ('commondir', 'gitdir', 'worktrees', 'hooks'):
        p = target / name
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        elif p.exists() or p.is_symlink():
            p.unlink()
    # Never execute repository hooks or load include/configured external helpers.
    (target / 'config').write_text('[core]\n\trepositoryformatversion = 0\n\tbare = false\n\thooksPath = /dev/null\n')


def normalize(text, roots=()):
    for root in sorted(map(str, roots), key=len, reverse=True):
        text = text.replace(root, '<copy>')
    text = re.sub(r'\b\d{4}-\d\d-\d\d[ T]\d\d:\d\d(?::\d\d(?:\.\d+)?)?(?:Z| ?[+-]\d\d:\d\d)?', '<time>', text)
    # JSON object ordering must not create a diff; list ordering remains significant.
    try:
        return json.dumps(json.loads(text), sort_keys=True, ensure_ascii=False)
    except ValueError:
        return text.strip()


def compare(before, after):
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
        if key.startswith('due:'):
            try:
                items = lambda x: collections.Counter(json.dumps(i, sort_keys=True) for i in json.loads(x['text']))
                if items(new) - items(old):
                    flags.append(key + ': new due items')
            except (ValueError, TypeError):
                flags.append(key + ': invalid due output')
        if key.startswith('brief:'):
            warnings = lambda x: collections.Counter(l for l in x['text'].splitlines()
                                                      if re.search(r'warn|limit|stop|consent|✗|!', l, re.I))
            if warnings(old) - warnings(new):
                flags.append(key + ': brief warning disappeared')
        if new['unexpected']:
            flags.append(key + ': unexpected writes')
        if key == 'hook' and new['seconds'] > 1:
            flags.append('hook: latency exceeds 1 s')
    return flags


def sandbox_prefix(folder):
    exe = shutil.which('sandbox-exec') if sys.platform == 'darwin' else None
    if not exe:
        raise RehearsalError('rehearsal requires an OS write/network sandbox (currently macOS sandbox-exec)')
    # Sandbox string literals use JSON-compatible escaping. File writes are allowed
    # only to this command's copy; even another lane's copy is read-only.
    profile = ('(version 1)(allow default)(deny network*)(deny mach-lookup)(deny appleevent-send)(deny file-write*)'
               '(allow file-write* (subpath ' + json.dumps(str(folder.resolve())) + '))'
               '(allow file-write* (literal "/dev/null"))')
    return [exe, '-p', profile]


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
    if rel.startswith('.brain/'):
        return True  # bookkeeping, registry/machine caches and instance locks
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
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
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
    if _m_guard.find_terms(out + err, terms):
        raise RehearsalError('protected output refused')
    after = snapshot(lane, cache)
    changed = {k for k in before.keys() | after.keys() if before.get(k) != after.get(k)}
    return {'text': normalize(out + err, roots), 'code': p.returncode, 'timeout': timed_out,
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
        subprocess.run([*sandbox_prefix(folder), '/usr/bin/true'], check=True, capture_output=True)
        original = snapshot(inst.root)
        frozen = folder / 'source'
        copy_tree(inst.root, frozen)
        materialize_git(inst.root, frozen)
        if snapshot(inst.root) != original:
            raise RehearsalError('live brain moved while copying; retry with a stable source')
        baseline = folder / 'installed'
        copy_tree(frozen / 'core', baseline)
        materialize_git(frozen / 'core', baseline)
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
        subprocess.run([*prefix, 'git', '-C', str(candidate), 'remote', 'remove', 'origin'],
                       env=prep_env, capture_output=True, check=True)
        commands = []
        nodes = _m_instance.Instance(frozen).nodes()
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
        old = run_lane(frozen, baseline, folder / 'before', nodes, commands)
        new = run_lane(frozen, candidate, folder / 'after', nodes, commands)
        flags = compare(old, new)
        if snapshot(inst.root) != original:
            flags.append('live brain changed during rehearsal; evidence refused')
        # Fail closed on incomplete baseline proof, not on preexisting check/ready failures.
        for key, result in old.items():
            if result['timeout'] or result['unexpected'] or (result['code'] and key.startswith(('due:', 'refresh:', 'brief:', 'ui', 'hook'))):
                flags.append(key + ': baseline incomplete')
        if (_m_verify.ready_refs(n, branch) != (head, main)
                or _m_verify.integration_parent(n, main) != integration_base):
            flags.append('candidate or integration main moved during rehearsal')
        delivery = probe_delivery(inst, inst.nodes())
        flags.extend(delivery_regressions(delivery))
        return {'version': 1, 'node': n['id'], 'branch': branch, 'head': head, 'main': main,
                'integration_base': integration_base,
                'checked_at': _m_jobs.stamp(inst), 'passed': not flags, 'regressions': flags,
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
    if not result['passed']:
        sys.exit(1)
