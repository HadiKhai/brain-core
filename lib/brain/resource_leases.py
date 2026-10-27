"""Cooperative machine-wide ownership of named database, service and preview lifetimes.

Only opaque resource/scope hashes and exact process identities are stored. These
leases coordinate callers; they never stop services or delete application data.
"""
import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import time

from . import common as _m_common

_m_machine = _m_common.LazyModule(__package__, 'machine')

_RESOURCE_HASH = re.compile(r'[0-9a-f]{64}')
_RESOURCE_TOKEN = re.compile(r'[0-9a-f]{32}')


def resource_digest(resource):
    if not isinstance(resource, str) or not resource.strip() or len(resource) > 4096 or '\x00' in resource:
        raise ValueError('resource requires a canonical nonempty key')
    return hashlib.sha256(resource.encode()).hexdigest()


def resource_valid(row, digest):
    def identity(who):
        return (isinstance(who, list) and len(who) == 2 and type(who[0]) is int and 0 < who[0] <= 2147483647
                and isinstance(who[1], str) and who[1] not in ('', 'unknown'))
    return (isinstance(row, dict) and row.get('version') == 1 and row.get('resource') == digest
            and isinstance(row.get('token'), str) and _RESOURCE_TOKEN.fullmatch(row['token'])
            and row.get('state') in ('active', 'released') and row.get('lifetime') in ('persistent', 'process')
            and identity(row.get('owner'))
            and isinstance(row.get('children'), list) and all(identity(who) for who in row['children'])
            and isinstance(row.get('scope'), list) and len(row['scope']) == 2
            and all(isinstance(s, str) and _RESOURCE_HASH.fullmatch(s) for s in row['scope'])
            and (row.get('boot') is None or isinstance(row['boot'], str))
            and type(row.get('uncertain_child', False)) is bool)


@contextlib.contextmanager
def resource_store(digest):
    """Short stable per-resource lock; never unlink a lock another caller can hold."""
    if not isinstance(digest, str) or not _RESOURCE_HASH.fullmatch(digest):
        raise ValueError('invalid resource identity')
    folder = _m_machine.machine_dir() / 'resource-leases'
    folder.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(folder), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    lock = None
    temporary = None
    try:
        try:
            lock = os.open(digest + '.lock', os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                           0o600, dir_fd=fd)
        except FileExistsError:
            # Concurrent creators reopen the stable inode. Never unlink or
            # replace it, and never follow a link supplied by another caller.
            lock = os.open(digest + '.lock', os.O_RDWR | os.O_NOFOLLOW, dir_fd=fd)
        info = os.fstat(lock)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('unsafe resource lease lock')
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            record_fd = os.open(digest + '.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(record_fd) as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 65536:
                    raise ValueError('unsafe resource lease record')
                raw = stream.read(65537)
                if len(raw) > 65536:
                    raise ValueError('resource lease record too large')
                row = json.loads(raw)
                if not resource_valid(row, digest):
                    raise ValueError('invalid resource lease record')
        except FileNotFoundError:
            row = None
        state = {'row': row}
        before = json.dumps(row, sort_keys=True)
        yield state
        if json.dumps(state['row'], sort_keys=True) != before:
            if not resource_valid(state['row'], digest):
                raise ValueError('invalid resource lease publication')
            temporary = digest + '-' + os.urandom(12).hex() + '.tmp'
            out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            with os.fdopen(out, 'w') as stream:
                json.dump(state['row'], stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, digest + '.json', src_dir_fd=fd, dst_dir_fd=fd)
            os.fsync(fd)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary, dir_fd=fd)
            except FileNotFoundError:
                pass
        if lock is not None:
            os.close(lock)
        os.close(fd)


def resource_alive(row):
    if row['state'] == 'released':
        return False
    if row['lifetime'] == 'persistent':
        return True  # owner death/reboot cannot prove a database or preview was cleaned
    boot = _m_machine.machine_boot_id()
    if boot is not None and row.get('boot') is not None and boot != row['boot']:
        return False
    if row.get('uncertain_child'):
        return True
    for pid, stamp in [row['owner']] + row['children']:
        observed = _m_machine.process_stamp(pid)
        if observed == 'unknown' or observed == stamp:
            return True
    return False


def resource_claim(inst, node, resource, lifetime='persistent'):
    if lifetime not in ('persistent', 'process'):
        raise ValueError('invalid resource lifetime')
    digest = resource_digest(resource)
    stamp = _m_machine.process_stamp(os.getpid())
    if stamp in (None, 'unknown'):
        raise ValueError('resource owner identity unknown; no lease acquired')
    with resource_store(digest) as state:
        if state['row'] is not None and resource_alive(state['row']):
            raise ValueError('resource busy: ' + digest[:12])
        row = {'version': 1, 'resource': digest, 'token': os.urandom(16).hex(), 'state': 'active',
               'owner': [os.getpid(), stamp], 'children': [], 'boot': _m_machine.machine_boot_id(),
               'scope': _m_machine.machine_scope(inst, node), 'since': time.time(), 'lifetime': lifetime}
        state['row'] = row
        return dict(row)


def resource_child(receipt, pid):
    """Register an owned service before handing its lifetime to another caller.

    Unknown child publication retains the lease for inspection, even after the
    parent exits. It never becomes evidence that a service was stopped.
    """
    if type(pid) is not int or not 0 < pid <= 2147483647:
        raise ValueError('invalid resource child')
    stamp = _m_machine.process_stamp(pid)
    unknown = stamp in (None, 'unknown')
    with resource_store(receipt['resource']) as state:
        row = state['row']
        if row is None or row['token'] != receipt['token'] or row['state'] != 'active':
            raise ValueError('resource lease replaced or released')
        if row['owner'][0] != os.getpid() or _m_machine.process_stamp(os.getpid()) != row['owner'][1]:
            raise ValueError('resource lease belongs to another owner')
        if unknown:
            row['uncertain_child'] = True
        elif [pid, stamp] not in row['children']:
            row['children'].append([pid, stamp])
    if unknown:
        raise ValueError('resource child identity unknown; lease retained')


def resource_release(receipt):
    with resource_store(receipt['resource']) as state:
        row = state['row']
        if row is None or row['token'] != receipt['token']:
            return False
        if row['state'] == 'released':
            return True
        if (row['owner'][0] != os.getpid() or _m_machine.process_stamp(os.getpid()) != row['owner'][1]
                or row.get('uncertain_child')):
            return False
        for pid, stamp in row['children']:
            observed = _m_machine.process_stamp(pid)
            if observed == 'unknown' or observed == stamp:
                return False
        row.update(state='released', released_at=time.time())
        return True


@contextlib.contextmanager
def resource_lease(inst, node, resources):
    """Acquire several canonical resource keys or roll back every partial claim."""
    if not isinstance(resources, (list, tuple)):
        raise ValueError('resource keys must be a list')
    ordered = sorted(set(resources), key=resource_digest)
    receipts, primary, entered = [], None, False
    try:
        for resource in ordered:
            receipts.append(resource_claim(inst, node, resource))
        entered = True
        yield receipts
    except BaseException as error:
        primary = error
        raise
    finally:
        errors = []
        for receipt in reversed(receipts):
            if entered and primary is not None:
                continue  # a failed body has not established service cleanup
            try:
                if not resource_release(receipt):
                    raise ValueError('resource cleanup incomplete; lease retained')
            except BaseException as error:
                errors.append(error)
        if errors and primary is None:
            raise errors[0]
        if errors and primary is not None:
            primary.resource_cleanup_errors = [type(error).__name__ + ': ' + str(error) for error in errors]


@contextlib.contextmanager
def configured_resource_lease(inst, n, setting='verify_resources'):
    """Optional node setting for an existing runner; no setting means no lease IO."""
    value = n.get('fm', {}).get(setting, '')
    if not isinstance(value, str):
        raise ValueError('configured resources must be comma-separated canonical keys')
    resources = [key.strip() for key in value.split(',') if key.strip()]
    with resource_lease(inst, n['id'], resources) as receipts:
        yield receipts
