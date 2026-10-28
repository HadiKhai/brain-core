"""Supervised per-instance write applier; transport and recovery remain queue-owned."""
import hashlib
import os
import plistlib
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import common as _m_common
from . import handback_queue as _m_queue
from . import instance as _m_instance
from . import machine as _m_machine
from . import lifecycle_identity as _m_identity
from . import timings as _m_timings


def service_spec(inst):
    label = 'org.brain.write-queue.' + hashlib.sha256(str(inst.root).encode()).hexdigest()[:16]
    args = [sys.executable, '-B', str(_m_common.STABLE_ENTRY_POINT), 'machine', 'queue-service', 'run']
    env = {'BRAIN_ROOT': str(inst.root), 'BRAIN_NO_REEXEC': '1',
           'BRAIN_CONFIG': str(_m_instance.brains_file())}
    if any('\n' in value or '\r' in value or '\x00' in value for value in args + list(env.values())):
        raise ValueError('write queue service paths contain unsupported characters')
    if sys.platform == 'darwin':
        path = Path.home() / 'Library/LaunchAgents' / (label + '.plist')
        payload = plistlib.dumps(dict(Label=label, ProgramArguments=args, WorkingDirectory=str(inst.root),
                                     EnvironmentVariables=env, RunAtLoad=True, KeepAlive=True,
                                     ThrottleInterval=5)).decode()
    elif sys.platform.startswith('linux'):
        path = Path.home() / '.config/systemd/user' / (label + '.service')
        def quote(value):
            # systemd performs specifier and ExecStart environment expansion.
            return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'
        command = ' '.join(quote(value).replace('$', '$$') for value in args)
        payload = ('[Unit]\nDescription=Brain write queue applier\n[Service]\nType=simple\n' +
                   'WorkingDirectory=' + quote(str(inst.root)) + '\n' +
                   ''.join('Environment=' + quote(k + '=' + v) + '\n' for k, v in env.items()) +
                   'ExecStart=' + command + '\nRestart=always\nRestartSec=5\n' +
                   '[Install]\nWantedBy=default.target\n')
    else:
        raise ValueError('write queue service requires launchd or systemd --user')
    return label, path, payload


def service_control(inst, action, yes=False):
    """Only an explicit --yes changes the user service manager."""
    label, path, payload = service_spec(inst)
    print(payload if action == 'install' else 'remove ' + str(path))
    if not yes:
        print("dry run; --yes requires the operator's consent")
        return
    if os.environ.get('BRAIN_MACHINE_TESTING') == '1':
        _m_common.die('write queue service: fixture mode cannot change OS services')
    def run(args, allowed=(0,)):
        result = subprocess.run(args, capture_output=True, text=True, timeout=15)
        if result.returncode not in allowed:
            raise ValueError('write queue service manager refused ' + args[1])
        return result
    # A stable reservation serializes service changes without the instance lock.
    with _m_instance.reservation(inst, 'write-queue-service'):
        if path.exists() or path.is_symlink():
            if path.is_symlink() or path.read_text() != payload:
                raise ValueError('existing write queue service differs; preserve it for inspection')
        if sys.platform == 'darwin':
            domain = 'gui/' + str(os.getuid())
            loaded = run(['launchctl', 'print', domain + '/' + label], allowed=(0, 113))
            if loaded.returncode == 0:
                run(['launchctl', 'bootout', domain + '/' + label])
            if action == 'uninstall':
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                _m_common.atomic_write(path, payload, private=True)
                run(['launchctl', 'bootstrap', domain, str(path)])
        else:
            if action == 'uninstall':
                if path.exists():
                    run(['systemctl', '--user', 'disable', '--now', path.name])
                    path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                _m_common.atomic_write(path, payload, private=True)
            run(['systemctl', '--user', 'daemon-reload'])
            if action == 'install':
                run(['systemctl', '--user', 'enable', '--now', path.name])
                run(['systemctl', '--user', 'restart', path.name])
    if action == 'uninstall':
        # The invoking CLI is outside the stopped unit and its child teardown.
        wake_fallback(inst)


def wake_fallback(inst):
    """After releasing the service lease, cover producers that saw it held."""
    try:
        _m_queue.intent_read(inst, 'blocked')
    except FileNotFoundError:
        if _m_queue.pending_intents(inst):
            _m_queue.start_intent_drainer(inst)
    # Retained application failures require explicit recovery, not an exit retry.


def lease_identity(inst):
    """Observe only: taking even a short flock could suppress a producer's wake."""
    try:
        with _m_common.directory_target(_m_queue.intent_path(inst, 'applier.lease'), private=True) as target:
            info = target.stat()
            return [info.st_dev, info.st_ino] if info else None
    except FileNotFoundError:
        return None


def valid_service_owner(owner):
    return (isinstance(owner, dict) and type(owner.get('version')) is int and owner['version'] == 1
            and owner.get('state') in ('running', 'stopped', 'failed')
            and isinstance(owner.get('owner'), list) and len(owner['owner']) == 2
            and type(owner['owner'][0]) is int and owner['owner'][0] > 0
            and isinstance(owner['owner'][1], str) and bool(owner['owner'][1])
            and owner['owner'][1] != 'unknown'
            and isinstance(owner.get('host'), str) and bool(owner['host'])
            and isinstance(owner.get('lease'), list) and len(owner['lease']) == 2
            and all(type(value) is int and value >= 0 for value in owner['lease'])
            and owner['lease'][1] > 0)


def queue_health(inst):
    """Recorded backlog and service process identity; never takes an applier lock."""
    result = dict(installed=None, running=None, failed=None, state='unknown',
                  pending=None, oldest_age_s=None, last_applied=None)
    try:
        _, path, payload = service_spec(inst)
        result['installed'] = path.exists()
        if result['installed'] and (path.is_symlink() or path.read_text() != payload):
            raise ValueError('service definition changed')
        lease = lease_identity(inst)
        try:
            owner = _m_queue.intent_read(inst, 'service.json')
        except FileNotFoundError:
            owner = {}
        else:
            if not valid_service_owner(owner):
                raise ValueError('invalid service owner')
        running = False
        if owner.get('state') == 'running' and lease is not None and owner.get('lease') == lease:
            host = _m_identity.lifecycle_host_identity()
            if host is None or owner.get('host') != host:
                raise ValueError('unknown or foreign service host')
            pid, stamp = owner['owner']
            if type(pid) is not int or pid <= 0 or not isinstance(stamp, str) or stamp == 'unknown':
                raise ValueError('invalid service process')
            actual = _m_machine.process_stamp(pid)
            if actual == 'unknown':
                raise ValueError('unknown service process')
            running = actual is not None and actual == stamp
        result.update(running=running,
                      failed=owner.get('state') == 'failed' or
                      (owner.get('state') == 'running' and not running))
        rows = _m_queue.intent_index(inst)
        pending = _m_queue.pending_intents(inst)
        result['pending'] = len(pending)
        now = time.time()
        result['oldest_age_s'] = (max(0, now - _m_queue.intent_path(inst, pending[0][1]['id'] + '.request').stat().st_mtime)
                                  if pending else 0.0)
        for _, data in reversed(rows):
            receipt = _m_queue.intent_receipt(inst, data['id'])
            if receipt and receipt['state'] == 'applied':
                result['last_applied'] = dict(id=data['id'], at=_m_queue.intent_path(inst, data['id'] + '.receipt').stat().st_mtime)
                break
        try:
            _m_queue.intent_read(inst, 'blocked')
        except FileNotFoundError:
            pass
        else:
            result['failed'] = True
        result['state'] = ('failed' if result['failed'] else 'running' if running else
                           'installed' if result['installed'] else 'not installed')
    except (OSError, ValueError, KeyError, TypeError, SystemExit):
        result.update(state='unknown', failed=None)
    return result


def queue_summary(inst):
    health = queue_health(inst)
    age = health['oldest_age_s']
    last = health['last_applied']
    return ('write queue: ' + health['state'] + '; pending ' + str(health['pending']) +
            '; oldest ' + (f'{age:.1f}s' if age is not None else 'unknown') +
            '; last applied ' + (last['id'] + ' at ' + str(last['at']) if last else 'none recorded'))


def run_service(inst, stop=None, interval=0.25):
    """Keep the existing applier lease across idle periods and fresh transactions."""
    _m_common.import_whole_core(__package__)  # keep lazy application imports on this source version
    stop = stop if stop is not None else threading.Event()
    fd = _m_queue.intent_lease(inst)
    if fd is None:
        print('write queue service: another applier owns the lease; standing down')
        return False
    handlers = {}
    clean_stop = False
    try:
        info = os.fstat(fd)
        stamp = _m_machine.process_stamp(os.getpid())
        host = _m_identity.lifecycle_host_identity()
        if stamp in (None, 'unknown') or host is None:
            raise ValueError('service process identity unavailable')
        owner = dict(version=1, state='running', owner=[os.getpid(), stamp], host=host, lease=[info.st_dev, info.st_ino])
        _m_queue.intent_store(inst, 'service.json', owner)
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                handlers[sig] = signal.signal(sig, lambda *_: stop.set())
        try:
            first = True
            while not stop.is_set():
                with _m_common.directory_target(_m_queue.intent_path(inst, 'applier.lease'), private=True) as target:
                    target.check_file(fd)
                current = _m_instance.Instance(inst.root)
                # drain_intents owns/closes its descriptor; the original open
                # description retains the exclusive lease through its finalizer.
                pending = _m_queue.pending_intents(current)
                if pending or first:
                    age = (max(0, time.time() - _m_queue.intent_path(current, pending[0][1]['id'] + '.request').stat().st_mtime)
                           if pending else 0.0)
                    with _m_timings.queue_phase(current.root, 'apply', backlog_s=age):
                        _m_queue.drain_intents(current, os.dup(fd))
                first = False
                stop.wait(interval)
        except BaseException:
            _m_queue.intent_store(inst, 'service.json', {**owner, 'state': 'failed'})
            raise
        else:
            _m_queue.intent_store(inst, 'service.json', {**owner, 'state': 'stopped'})
            clean_stop = True
        return True
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
        os.close(fd)
        if clean_stop:
            wake_fallback(inst)
