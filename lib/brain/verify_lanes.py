"""Project-declared verify lanes. No project source parsing or third-party glob engine."""
import hashlib
import json
import os
import platform
import signal
import re
import shutil
import shlex
import subprocess
import sys
from pathlib import Path

from . import common as _m_common
from . import jobs as _m_jobs

VERIFY_LANE_MAP_TIMEOUT = 5
LANE_ENV_KEYS = {'VIRTUAL_ENV', 'JAVA_HOME', 'RUSTUP_TOOLCHAIN', 'NODE_OPTIONS',
                 'PYTHONPATH', 'CC', 'CXX', 'CFLAGS', 'CXXFLAGS', 'LDFLAGS',
                 'PORT', 'PORTS', 'PORT_LAST'}


def lane_environment(env):
    # Capture only the fingerprinted environment; None preserves an unset variable.
    return {key: env.get(key) for key in sorted(LANE_ENV_KEYS | {'PATH'})}


def recorded_lane_environment(env, recorded):
    if (not isinstance(recorded, dict) or set(recorded) != LANE_ENV_KEYS | {'PATH'}
            or any(value is not None and (not isinstance(value, str) or '\0' in value)
                   for value in recorded.values())):
        raise ValueError('recorded verify lane environment missing or invalid; hand back again')
    restored = {key: value for key, value in env.items() if key not in recorded}
    restored.update((key, value) for key, value in recorded.items() if value is not None)
    return restored


def glob_regex(pattern):
    original = pattern
    if pattern.startswith('!'):
        pattern = pattern[1:]
    if (not pattern or pattern.startswith(('/', '#')) or any(x in pattern for x in '[]{}()\\!')
            or any(p in ('', '.', '..') for p in pattern.split('/'))):
        raise ValueError('unsupported lane glob: ' + original)
    out, i = '', 0
    while i < len(pattern):
        c = pattern[i]
        if pattern[i:i+2] == '**':
            if (i and pattern[i-1] != '/') or (i+2 < len(pattern) and pattern[i+2] != '/'):
                raise ValueError('unsupported lane glob: ' + original)
            if pattern[i+2:i+3] == '/':
                out += '(?:.*/)?'
                i += 3
            else:
                out += '.*'
                i += 2
        else:
            out += '[^/]*' if c == '*' else '[^/]' if c == '?' else re.escape(c)
            i += 1
    return re.compile('^' + out + '$', re.DOTALL)


def matches(path, patterns):
    positive = [p for p in patterns if not p.startswith('!')]
    return (any(glob_regex(p).fullmatch(path) for p in positive)
            and not any(excludes(path, p[1:]) for p in patterns if p.startswith('!')))


def excludes(path, pattern):
    # Negative patterns use explicit-dot semantics, while positive patterns
    # deliberately overinclude dotfiles. Align ** segments with a small DP.
    parts, pats = path.split('/'), pattern.split('/')
    memo = {}
    def match(i, j):
        if (i, j) in memo:
            return memo[i, j]
        if j == len(pats):
            result = i == len(parts)
        elif pats[j] == '**':
            result = match(i, j + 1) or (i < len(parts) and not parts[i].startswith('.') and match(i + 1, j))
        else:
            result = (i < len(parts) and (not parts[i].startswith('.') or pats[j].startswith('.'))
                      and bool(glob_regex(pats[j]).fullmatch(parts[i])) and match(i + 1, j + 1))
        memo[i, j] = result
        return result
    return bool(glob_regex(pattern).fullmatch(path)) and match(0, 0)


def lane_config_hash(n):
    return _m_jobs.digest(sorted((k, v) for k, v in n['fm'].items() if k.startswith('verify')))


def map_output(command, w, env):
    proc = subprocess.Popen(command, shell=True, cwd=w['path'], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        stdout, _ = proc.communicate(timeout=VERIFY_LANE_MAP_TIMEOUT)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.stdout.close()
        proc.stderr.close()
        proc.wait()
        raise ValueError('verify_lanes_from timed out')
    if proc.returncode:
        raise ValueError('verify_lanes_from failed (exit %d)' % proc.returncode)
    return stdout.decode('utf-8')


def lane_map(n, w, env):
    fm = n['fm']
    inline, source = fm.get('verify_lanes', '').strip(), fm.get('verify_lanes_from', '').strip()
    if inline and source:
        raise ValueError('use only one of verify_lanes and verify_lanes_from')
    if not inline and not source:
        return [], ''
    if source:
        raw = map_output(source, w, env)
        data = json.loads(raw)
    elif inline.startswith('['):
        raw, data = inline, json.loads(inline)
    else:
        raw, data = inline, []
        for item in inline.split(';'):
            name, sep, rest = item.partition(':')
            command, pipe, inputs = rest.rpartition('|')
            if not sep or not pipe:
                raise ValueError('verify_lanes needs name: command | input globs')
            data.append(dict(name=name.strip(), command=command.strip(), inputs=_m_common.lst(inputs)))
    if not isinstance(data, list) or not data:
        raise ValueError('lane map must be a nonempty JSON array')
    lanes, names = [], set()
    for item in data:
        if not isinstance(item, dict) or set(item) - {'name', 'command', 'inputs', 'requires', 'strict_inputs', 'always', 'schedule'}:
            raise ValueError('invalid lane map entry')
        name, command = item.get('name'), item.get('command')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,119}', name) or name in names:
            raise ValueError('lane names must be unique simple identifiers')
        if not isinstance(command, str) or not command.strip():
            raise ValueError('lane command must be nonempty')
        lane = dict(name=name, command=command, inputs=item.get('inputs', []),
                    strict_inputs=item.get('strict_inputs', []), requires=item.get('requires', []),
                    always=item.get('always', False), schedule=item.get('schedule', 'job'))
        for field in ('inputs', 'strict_inputs', 'requires'):
            if not isinstance(lane[field], list) or not all(isinstance(x, str) for x in lane[field]):
                raise ValueError('lane ' + field + ' must be a list of strings')
        if type(lane['always']) is not bool or lane['schedule'] not in ('job', 'nightly'):
            raise ValueError('invalid lane always or schedule')
        if not lane['always'] and not any(not p.startswith('!') for p in lane['inputs']):
            raise ValueError('lane needs a positive input glob unless always: ' + name)
        for pat in lane['inputs'] + lane['strict_inputs']:
            glob_regex(pat)
        lanes.append(lane)
        names.add(name)
    if not any(l['schedule'] != 'nightly' for l in lanes):
        raise ValueError('lane map must contain a job-scheduled lane')
    ordered = []
    while len(ordered) < len(lanes):
        available = [l for l in lanes if l not in ordered and set(l['requires']) <= {x['name'] for x in ordered}]
        if not available:
            raise ValueError('lane requirements are unknown or cyclic')
        ordered.extend(available)
    for lane in ordered:
        if lane['schedule'] != 'nightly' and any(l['name'] in lane['requires'] and l['schedule'] == 'nightly' for l in ordered):
            raise ValueError('job lane cannot require a nightly lane')
    return ordered, hashlib.sha256(raw.encode()).hexdigest()


def tracked_files(w):
    proc = _m_common.git('ls-files', '--stage', '-z', cwd=w['path'])
    if proc.returncode or '\ufffd' in proc.stdout:
        raise ValueError('cannot enumerate lane inputs losslessly')
    found = {}
    for row in proc.stdout.split('\0'):
        if not row:
            continue
        meta, path = row.split('\t', 1)
        mode, oid, stage = meta.split()
        if stage != '0':
            raise ValueError('unmerged lane input')
        file = Path(w['path']) / path
        if mode == '160000':
            value = oid
        else:
            try:
                data = os.fsencode(os.readlink(file)) if mode == '120000' else file.read_bytes()
                value = hashlib.sha256(data).hexdigest()
            except OSError:
                raise ValueError('unreadable lane input: ' + path)
        found[path] = [mode, value]
    return found


LOCK_NAMES = {'package-lock.json', 'npm-shrinkwrap.json', 'yarn.lock', 'pnpm-lock.yaml', 'bun.lock', 'bun.lockb',
              'poetry.lock', 'uv.lock', 'Pipfile.lock', 'Cargo.lock', 'Gemfile.lock', 'composer.lock', 'go.sum',
              '.tool-versions', '.python-version', '.node-version', '.nvmrc', 'rust-toolchain.toml',
              'package.json', 'pyproject.toml', 'Pipfile', 'Cargo.toml', 'go.mod', 'Gemfile',
              '.npmrc', '.yarnrc', '.yarnrc.yml', 'requirements.txt', 'requirements-dev.txt'}


def lane_snapshot(n, w, env, inputs):
    lanes, map_hash = lane_map(n, w, env)
    if not lanes:
        return None
    files = tracked_files(w)
    warnings = []
    for lane in lanes:
        for pat in lane['inputs'] + lane['strict_inputs']:
            literal = pat[1:] if pat.startswith('!') else pat
            if any(c.isspace() or c == '|' for c in literal) and literal not in files:
                raise ValueError('whitespace or pipe in lane glob must name a tracked path: ' + pat)
            if not pat.startswith('!') and not any(matches(path, [pat]) for path in files):
                warnings.append('lane ' + lane['name'] + ' positive glob matches no tracked files: ' + pat)
    tools = []
    # Resolved executable identities and project pins define the toolchain.
    commands = [lane['command'] for lane in lanes] + [n['fm'].get('verify_lanes_check', '')]
    executables = {'sh', 'python3', 'node', 'npm', 'yarn', 'pnpm', 'cargo', 'go', 'ruby', 'java'}
    for command in commands:
        try:
            words = shlex.split(command)
            # Conservatively fingerprint every PATH-resolved token, including
            # commands after cd, pipelines, env wrappers and shell conditionals.
            executables.update(word for word in words if '=' not in word and '/' not in word)
        except ValueError:
            pass  # The shell reports malformed commands as an ordinary lane failure.
    for tool in sorted(executables):
        path = shutil.which(tool, path=env.get('PATH', os.defpath))
        try:
            path = str(Path(path).resolve()) if path else None
            stat = Path(path).stat() if path else None
            tools.append([tool, path, [stat.st_size, stat.st_mtime_ns] if stat else None])
        except OSError:
            raise ValueError('cannot inspect verify toolchain')
    context = _m_jobs.digest([map_hash, sorted((k, v) for k, v in n['fm'].items() if k.startswith('verify')),
                           sorted((k, v) for k, v in files.items() if Path(k).name in LOCK_NAMES or Path(k).name.endswith('.lock')),
                           platform.platform(), sys.version, str(n['home_path']), str(w['path']), tools,
                           lane_environment(env)])
    hashes = {l['name']: _m_jobs.digest([inputs, sorted((p, v) for p, v in files.items() if matches(p, l['inputs']))]) for l in lanes}
    return dict(lanes=lanes, map_hash=map_hash, config_hash=lane_config_hash(n), context=context, hashes=hashes, files=files, warnings=warnings)


def changed_since(cwd, head, current):
    if not isinstance(head, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', head):
        return None
    if _m_common.git('merge-base', '--is-ancestor', head, current, cwd=cwd).returncode:
        return None
    proc = _m_common.git('diff', '--name-only', '-z', '--no-renames', '--ignore-submodules=none', head, current, cwd=cwd)
    return proc.stdout.rstrip('\0').split('\0') if proc.returncode == 0 else None
