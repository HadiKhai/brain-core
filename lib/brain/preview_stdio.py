"""Trusted, explicitly registered local drivers for the preview three-method API.

This is a transport, not a lifecycle engine or a sandbox. The operator trusts
the executable and checkout; the product remains responsible for real fencing,
descendant quiescence and recoverable backup/rollback semantics.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import time

from . import common as _m_common


JSON_LIMIT = 65536
STDERR_LIMIT = 16384
CALL_SECONDS = 10
REGISTRATION_KEYS = {'version', 'name', 'protocol', 'executable', 'argv', 'checkout', 'head', 'files'}
OPERATION_TYPES = {'acquire', 'backup', 'migration', 'restart', 'acceptance', 'rollback', 'release'}
POLL_SECONDS = 0.1


def operation_remaining(deadline):
    if deadline is None:
        return None
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise ValueError('preview operation deadline exceeded; outcome unknown')
    return seconds


def strict_json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError('duplicate JSON key')
            value[key] = item
        return value
    def invalid(value):
        raise ValueError('non-finite JSON number')
    if len(raw) > JSON_LIMIT:
        raise ValueError('preview JSON exceeds limit')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


def trusted_path(value, directory=False):
    if not isinstance(value, str) or not value or '\0' in value:
        raise ValueError('absolute physical preview path required')
    path = Path(value)
    if not path.is_absolute() or str(path) != value or path.resolve(strict=True) != path:
        raise ValueError('absolute physical preview path required')
    for parent in (path, *path.parents):
        info = parent.lstat()
        # A sticky system temporary directory is a boundary, not authority to
        # replace its privately owned child. Other writable ancestors refuse.
        sticky = (parent != path and info.st_uid == 0 and stat.S_ISDIR(info.st_mode)
                  and info.st_mode & stat.S_ISVTX)
        if info.st_uid not in (0, os.getuid()) or (info.st_mode & 0o022 and not sticky):
            raise ValueError('untrusted preview path ownership or permissions')
    info = path.stat()
    if directory:
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError('preview checkout must be a directory')
    elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('preview file must be an unlinked regular file')
    return path


def file_identity(path, deadline=None):
    operation_remaining(deadline)
    path = trusted_path(str(path))
    with _m_common.directory_target(path) as target:
        with os.fdopen(target.open(), 'rb') as stream:
            before = os.fstat(stream.fileno())
            if before.st_size > 128 * 1024 * 1024:
                raise ValueError('preview registered file exceeds identity limit')
            digest = hashlib.sha256()
            hash_deadline = time.monotonic() + CALL_SECONDS
            if deadline is not None:
                hash_deadline = min(hash_deadline, deadline)
            for block in iter(lambda: stream.read(65536), b''):
                if time.monotonic() >= hash_deadline:
                    raise ValueError('preview registered file identity timed out')
                digest.update(block)
            target.check_file(stream.fileno())
            if target.generation(before) != target.generation(os.fstat(stream.fileno())):
                raise ValueError('preview file changed while reading')
            operation_remaining(deadline)
            return [before.st_dev, before.st_ino, digest.hexdigest()]


def private_json(path):
    path = trusted_path(str(path))
    with _m_common.directory_target(path, private=True) as target:
        with os.fdopen(target.open(), 'rb') as stream:
            info = os.fstat(stream.fileno())
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('preview registration must be private to this user')
            raw = stream.read(JSON_LIMIT + 1)
            target.check_file(stream.fileno())
            if target.generation(info) != target.generation(os.fstat(stream.fileno())):
                raise ValueError('preview registration changed while reading')
    return strict_json(raw)


def checkout_identity(path, head, deadline=None):
    operation_remaining(deadline)
    path = trusted_path(path, directory=True)
    # Explicit configuration cannot turn this read into an fsmonitor command.
    env = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'GIT_CONFIG_NOSYSTEM': '1',
           'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_OPTIONAL_LOCKS': '0'}
    def git(*args):
        try:
            output = bounded_process(['/usr/bin/git', '-c', 'core.fsmonitor=false',
                '-c', 'core.untrackedCache=false', '-C', str(path), *args],
                str(path), b'', env, operation_remaining(deadline))
        except subprocess.SubprocessError as error:
            raise ValueError('preview checkout identity unavailable') from error
        operation_remaining(deadline)
        return output.decode('utf-8').strip()
    if git('rev-parse', '--show-toplevel') != str(path) or git('rev-parse', 'HEAD') != head:
        raise ValueError('preview checkout head mismatched')
    if git('status', '--porcelain', '--untracked-files=all', '--ignore-submodules=none'):
        raise ValueError('preview checkout must be clean')
    info = path.stat()
    operation_remaining(deadline)
    return [info.st_dev, info.st_ino]


def registration_identity(value, deadline=None):
    operation_remaining(deadline)
    if (not isinstance(value, dict) or set(value) not in (REGISTRATION_KEYS, REGISTRATION_KEYS | {'operation_deadlines'})
            or type(value['version']) is not int or value['version'] != 1
            or not isinstance(value['name'], str) or not re.fullmatch('[a-z][a-z0-9_-]{0,63}', value['name'])
            or not isinstance(value['protocol'], str) or not value['protocol'].strip() or len(value['protocol']) > 256
            or not isinstance(value['head'], str) or not re.fullmatch('[0-9a-f]{40,64}', value['head'])
            or not isinstance(value['argv'], list) or len(value['argv']) > 32
            or any(not isinstance(arg, str) or not arg or len(arg) > 4096 or '\0' in arg for arg in value['argv'])
            or not isinstance(value['files'], dict) or not 1 <= len(value['files']) <= 32):
        raise ValueError('invalid version 1 preview registration')
    if 'operation_deadlines' in value:
        deadlines = value['operation_deadlines']
        if (not isinstance(deadlines, dict) or not deadlines or not set(deadlines) <= OPERATION_TYPES
                or any(type(seconds) is not int or not 1 <= seconds <= 86400
                       for seconds in deadlines.values())):
            raise ValueError('invalid preview operation deadlines')
    checkout = checkout_identity(value['checkout'], value['head'], deadline)
    executable = trusted_path(value['executable'])
    if not os.access(executable, os.X_OK) or str(executable) not in value['files']:
        raise ValueError('preview executable requires a pinned digest and execute permission')
    files = {}
    for path, digest in value['files'].items():
        if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
            raise ValueError('invalid preview file digest')
        identity = file_identity(path, deadline)
        if identity[2] != digest:
            raise ValueError('preview registered file digest changed')
        files[path] = identity
    # Absolute file arguments (for example a JavaScript entrypoint) are pinned
    # too. Other arguments are literal values, never shell text or templates.
    for arg in value['argv']:
        if arg.startswith('/') and arg not in files:
            raise ValueError('absolute preview argument requires a pinned file')
    operation_remaining(deadline)
    return dict(checkout=checkout, files=files)


def registration_path(n):
    return n['dir'] / '.brain' / 'preview-adapter.json'


def install_registration(n, source):
    value = private_json(source)
    identity = registration_identity(value)
    path = registration_path(n)
    trusted_path(str(n['dir']), directory=True)
    saved = json.dumps(dict(version=1, registration=value, identity=identity),
                       sort_keys=True, allow_nan=False) + '\n'
    if len(saved.encode('utf-8')) > JSON_LIMIT:
        raise ValueError('preview registration exceeds limit')
    with _m_common.directory_target(path, create=True, private=True) as target:
        # Do not inherit permissive permissions from an existing target.
        info = target.stat()
        if info and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise ValueError('existing preview registration is not private')
        if info:
            raise ValueError('preview adapter already registered; explicit removal required')
        target.atomic_write(saved)


def bootstrap_preview_adapters(n, existing):
    path = registration_path(n)
    # lexists distinguishes absent (disabled) from dangling or linked refusal.
    if not os.path.lexists(path):
        return existing
    saved = private_json(path)
    if (not isinstance(saved, dict) or set(saved) != {'version', 'registration', 'identity'}
            or type(saved['version']) is not int or saved['version'] != 1
            or registration_identity(saved['registration']) != saved['identity']):
        raise ValueError('preview registration identity changed')
    value = saved['registration']
    if value['name'] in existing:
        raise ValueError('duplicate preview adapter registration')
    return {**existing, value['name']: StdioPreviewAdapter(value, saved['identity'], path, saved)}


def bounded_process(argv, cwd, raw, env, seconds=None):
    """Join only our direct child; pipe EOF/exit never certify descendants."""
    output, errors = bytearray(), bytearray()
    deadline = time.monotonic() + (CALL_SECONDS if seconds is None else min(CALL_SECONDS, seconds))
    if time.monotonic() >= deadline:
        raise ValueError('preview operation deadline exceeded; outcome unknown')
    child = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, close_fds=True, env=env)
    try:
        with selectors.DefaultSelector() as selector:
            for stream, event, label in ((child.stdin, selectors.EVENT_WRITE, 'input'),
                    (child.stdout, selectors.EVENT_READ, 'output'), (child.stderr, selectors.EVENT_READ, 'error')):
                if label == 'input' and not raw:
                    stream.close()
                    continue
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, event, label)
            offset = 0
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError('preview transport timed out; effects unknown')
                for key, _ in selector.select(remaining):
                    stream = key.fileobj
                    if key.data == 'input':
                        offset += os.write(stream.fileno(), raw[offset:])
                        if offset == len(raw):
                            selector.unregister(stream)
                            stream.close()
                    else:
                        block = os.read(stream.fileno(), 4096)
                        if not block:
                            selector.unregister(stream)
                            stream.close()
                            continue
                        buffer = output if key.data == 'output' else errors
                        buffer.extend(block)
                        if len(buffer) > (JSON_LIMIT if key.data == 'output' else STDERR_LIMIT):
                            raise ValueError('preview transport output exceeds limit; effects unknown')
        remaining = deadline - time.monotonic()
        if remaining <= 0 or child.wait(timeout=remaining) != 0:
            raise ValueError('preview transport failed; effects unknown')
        return bytes(output)
    finally:
        if child.poll() is None:
            child.kill()  # Only the owned direct invocation; never a process tree.
        child.wait()
        for stream in (child.stdin, child.stdout, child.stderr):
            stream.close()


def bounded_call(argv, cwd, request, seconds=None):
    raw = (json.dumps(request, sort_keys=True, allow_nan=False) + '\n').encode('utf-8')
    if len(raw) > JSON_LIMIT:
        raise ValueError('preview request exceeds limit')
    return strict_json(bounded_process(argv, cwd, raw, {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, seconds))


class StdioPreviewAdapter:
    def __init__(self, registration, identity, path, saved):
        self.registration, self.identity = registration, identity
        self.path, self.saved = path, saved
        self.protocol = registration['protocol']

    def check(self, deadline=None):
        operation_remaining(deadline)
        if private_json(self.path) != self.saved or registration_identity(self.registration, deadline) != self.identity:
            raise ValueError('preview registration changed')
        operation_remaining(deadline)

    def exchange(self, method, step, context, operation=None, deadline=None):
        operation_remaining(deadline)
        self.check(deadline)
        operation_remaining(deadline)
        binding = dict(version=1, adapter=self.registration['name'], protocol=self.protocol,
            request_id=os.urandom(16).hex(), method=method, step=step,
            context_sha256=hashlib.sha256(json.dumps(context, sort_keys=True,
                                                   allow_nan=False).encode('utf-8')).hexdigest())
        if operation is not None:
            binding['operation'] = dict(operation)
        response = bounded_call([self.registration['executable'], *self.registration['argv']],
                                self.registration['checkout'], {**binding, 'context': context}, operation_remaining(deadline))
        self.check(deadline)
        operation_remaining(deadline)
        if (not isinstance(response, dict) or set(response) != set(binding) | {'result'}
                or type(response.get('version')) is not int
                or any(response.get(key) != value for key, value in binding.items())):
            raise ValueError('preview response binding mismatched; effects unknown')
        if operation is not None and type(response['operation']['timeout_seconds']) is not int:
            raise ValueError('preview operation binding mismatched; outcome unknown')
        return response['result']

    def invoke(self, method, step, context):
        seconds = self.registration.get('operation_deadlines', {}).get(step if method == 'step' else method)
        if seconds is None:
            result = self.exchange(method, step, context)
        else:
            # One start only. Losing the process or handle remains an unknown
            # lifecycle intent, never permission to restart the operation.
            deadline = time.monotonic() + seconds
            operation = dict(id=os.urandom(16).hex(), method=method, timeout_seconds=seconds)
            call = 'start'
            while True:
                observation = self.exchange(call, step, context, operation, deadline)
                if (not isinstance(observation, dict) or set(observation) != {'state', 'receipt', 'value'}
                        or not isinstance(observation['state'], str)
                        or observation['state'] not in ('pending', 'done', 'failed', 'unknown')
                        or not isinstance(observation['receipt'], str)
                        or not observation['receipt'].strip() or len(observation['receipt']) > 4096):
                    raise ValueError('invalid preview operation observation; outcome unknown')
                state, result = observation['state'], observation['value']
                if state in ('pending', 'unknown'):
                    if result is not None or state == 'unknown':
                        raise ValueError('preview operation outcome unknown')
                    time.sleep(min(POLL_SECONDS, max(0, deadline - time.monotonic())))
                    call = 'status'
                    continue
                if (state == 'failed' and (method != 'step' or not isinstance(result, dict)
                                          or result.get('ok') is not False)
                        or state == 'done' and method == 'step'
                        and (not isinstance(result, dict) or result.get('ok') is not True)):
                    raise ValueError('invalid preview operation terminal state; outcome unknown')
                break
        expected = ({'resource', 'host', 'run_id', 'lease', 'receipt'} if method == 'acquire' else
                    {'ok', 'receipt', 'ownership', 'quiescent'} if method == 'step' else
                    {'ownership', 'released', 'quiescent'})
        if not isinstance(result, dict) or set(result) != expected:
            raise ValueError('preview response result schema mismatched')
        if seconds is not None and time.monotonic() >= deadline:
            raise ValueError('preview operation deadline exceeded; outcome unknown')
        # The lifecycle engine validates ownership, receipts and product-asserted
        # quiescence. Transport success does not supply those assertions.
        return result

    def acquire(self, context):
        return self.invoke('acquire', None, context)

    def step(self, name, context):
        return self.invoke('step', name, context)

    def release(self, context):
        return self.invoke('release', None, context)


def cmd_preview_adapter(a):
    from . import nodes as _m_nodes
    inst = _m_nodes.load()
    try:
        install_registration(inst.node(a.id), a.register)
    except (OSError, ValueError, TypeError, KeyError, RecursionError, subprocess.SubprocessError):
        _m_common.die('preview adapter registration refused; no driver invoked')
    print('preview adapter: registered on this machine')


def cmd_preview_upgrade(a):
    from . import nodes as _m_nodes
    from . import preview_upgrade as _m_preview_upgrade
    inst = _m_nodes.load()
    result = _m_preview_upgrade.landed_preview(inst, inst.node(a.id), a.head)
    print(json.dumps(result, sort_keys=True, allow_nan=False))
