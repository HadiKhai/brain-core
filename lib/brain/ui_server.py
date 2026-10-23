"""Local dashboard identity and conservative reuse of its requested port."""
import hashlib
import json
import os
import shlex
import signal
import stat
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from . import common
from . import instance


def ui_code_version():
    digest = hashlib.sha256()
    paths = [common.CORE / 'bin/brain']
    for folder in (common.CORE / 'lib/brain', common.CORE / 'ui/dist'):
        paths.extend(sorted(folder.rglob('*')))
    for path in paths:
        if path.is_file() and not path.is_symlink() and '__pycache__' not in path.parts:
            digest.update(str(path.relative_to(common.CORE)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def ui_server_file(inst, port):
    return inst.root / '.brain/ui-servers' / (str(port) + '.json')


def ui_server_record(inst, port, value):
    path = ui_server_file(inst, port)
    common.safe_target(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.server-')
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(value, out)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def ui_server_request(port, key, stop=False):
    url = 'http://127.0.0.1:' + str(port)
    path = '/api/server/stop' if stop else '/api/server'
    request = urllib.request.Request(url + path + '?k=' + key, data=b'{}' if stop else None,
                                    headers={'Origin': url, 'Content-Type': 'application/json',
                                             'X-Brain-Review': key})
    # A local dashboard must not be sent through an HTTP proxy.
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=0.5) as response:
        return json.loads(response.read(4096))


def ui_legacy_owner(pid, root):
    """Old servers have no identity endpoint. Require our UID, Python CLI, and brain cwd."""
    if pid <= 1 or pid == os.getpid():
        return False
    try:
        result = subprocess.run(['ps', '-p', str(pid), '-o', 'uid=,command='], capture_output=True,
                                text=True, timeout=0.5)
        uid, command = result.stdout.strip().split(None, 1)
        args = shlex.split(command)
        if int(uid) != os.getuid() or not Path(args[0]).name.lower().startswith('python'):
            return False
        args = args[1:]
        while args and args[0] in ('-B', '-u'):
            args = args[1:]
        script = Path(args.pop(0))
        if (script.name != 'brain' or script.parent.name != 'bin' or not script.is_file()
                or not (script.parent.parent / 'lib/brain/ui.py').is_file()):
            return False
        if args[:1] in (['-b'], ['--brain']):
            args = args[2:]
        if args[:1] != ['ui']:
            return False
        cwd = subprocess.run(['lsof', '-a', '-p', str(pid), '-d', 'cwd', '-Fn'],
                             capture_output=True, text=True, timeout=0.5)
        return any(instance.instance_above(Path(line[1:])) == root.resolve()
                   for line in cwd.stdout.splitlines() if line.startswith('n'))
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return False


def ui_reuse_server(inst, port, version, write=False):
    """Return the matching server URL; otherwise stop only a positively identified brain UI."""
    record = None
    path = ui_server_file(inst, port)
    try:
        info = path.lstat()
        if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077:
            record = json.loads(path.read_text())
            identity = ui_server_request(port, record['key'])
            if identity == {k: record[k] for k in ('pid', 'version', 'root', 'write')}:
                if record['root'] != str(inst.root.resolve()):
                    common.die(f'port {port} belongs to another brain; leave it running')
                if identity['version'] == version and identity['write'] == write:
                    return f"http://127.0.0.1:{port}/?k={record['key']}"
                ui_server_request(port, record['key'], stop=True)
                return None
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError):
        pass
    try:
        result = subprocess.run(['lsof', '-nP', '-tiTCP:' + str(port), '-sTCP:LISTEN'],
                                capture_output=True, text=True, timeout=0.5)
        pids = set(int(x) for x in result.stdout.split())
    except (OSError, ValueError, subprocess.SubprocessError):
        pids = set()
    if len(pids) == 1:
        pid = next(iter(pids))
        if ui_legacy_owner(pid, inst.root) and ui_legacy_owner(pid, inst.root):
            os.kill(pid, signal.SIGTERM)
    return None
