"""Local dashboard identity and conservative reuse of its requested port."""
import hashlib
import json
import os
import stat
import tempfile
import urllib.error
import urllib.request

from . import common


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
    """Legacy argv/cwd cannot prove the selected instance; never authorize a signal.

    Selectors can occur anywhere, and re-exec can consume them into BRAIN_ROOT.
    An inherited environment can also select a different instance from the cwd.
    Only the authenticated identity path below can establish safe replacement.
    """
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
    # Missing/unusable identity is ambiguous, including legacy dashboards. Leave
    # the listener alone; cmd_ui reports the requested port busy when bind fails.
    return None
