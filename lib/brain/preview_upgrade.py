"""Explicit product-owned preview protocols; no shell or implicit provider calls.

A product extension registers a trusted adapter implementing ``protocol`` and
``acquire(context)``, ``step(name, context)`` and ``release(context)``. Steps synchronously join their resources and return their
exact ownership, quiescent=True, ok and a bounded opaque receipt. Backup produces
a recoverable snapshot; rollback restores that snapshot and confirms acceptance.
Core supplies no generic adapter and never guesses product backup semantics.
"""
import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat

from . import common as _m_common
from . import instance as _m_instance
from . import lifecycle_identity as _m_lifecycle_identity
from . import preview_stdio as _m_preview_stdio


PREVIEW_ADAPTERS = {}
PREVIEW_STEPS = ('backup', 'migration', 'restart', 'acceptance', 'rollback')


def register_preview_adapter(name, adapter):
    if (not isinstance(name, str) or not re.fullmatch('[a-z][a-z0-9_-]{0,63}', name)
            or name in PREVIEW_ADAPTERS or any(not callable(getattr(adapter, method, None))
                for method in ('acquire', 'step', 'release'))
            or not isinstance(getattr(adapter, 'protocol', None), str) or not adapter.protocol):
        raise ValueError('invalid or duplicate preview adapter')
    PREVIEW_ADAPTERS[name] = adapter


def preview_landed_head(n):
    if not n.get('home_path'):
        return None
    result = _m_common.git('rev-parse', '--verify', 'refs/heads/main^{commit}', cwd=n['home_path'])
    return result.stdout.strip() if not result.returncode else None


def preview_config(n):
    value = n['local'].get('preview_upgrade')
    if (not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1
            or value.get('enabled') is not True
            or set(value) != set(('version', 'enabled', 'adapter', 'protocol', 'resource') + PREVIEW_STEPS)
            or any(not isinstance(value.get(key), str) or not value[key].strip() or len(value[key]) > 256
                   for key in ('adapter', 'protocol', 'resource') + PREVIEW_STEPS)):
        raise ValueError('explicit complete per-node preview protocol required')
    return json.loads(json.dumps(value))


def preview_folder(n):
    # Local machine state, never public/shared node notes.
    return n['dir'] / '.brain' / 'preview-upgrades'


def preview_save(path, data):
    path = _m_common.safe_target(path) or path
    _m_common.atomic_write(path, json.dumps(data, sort_keys=True, allow_nan=False) + '\n')


@contextlib.contextmanager
def preview_resource_lock(inst, resource):
    """Serialize the exact configured resource across nodes in this instance.

    The adapter's returned lease still owns the real resource, including other
    instances/machines. A local file lock never substitutes for that authority.
    """
    folder = inst.root / '.brain' / 'preview-resources'
    folder = _m_common.safe_target(folder) or folder
    if folder.is_symlink():
        raise ValueError('linked preview resource lock directory')
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (hashlib.sha256(resource.encode()).hexdigest() + '.lock')
    path = _m_common.safe_target(path) or path
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('unsafe preview resource lock')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def preview_ownership_valid(value, context):
    return (isinstance(value, dict)
            and set(value) == {'resource', 'host', 'run_id', 'lease', 'receipt'}
            and all(value.get(key) == context.get(key) for key in ('resource', 'host', 'run_id'))
            and all(isinstance(value.get(key), str) and 0 < len(value[key]) <= 4096
                    for key in ('lease', 'receipt')))


def preview_receipt_valid(data, node, head, host):
    """Terminal text alone never certifies a complete, locally owned protocol."""
    if (not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] != 1
            or not isinstance(head, str) or not re.fullmatch('[0-9a-f]{40,64}', head)
            or data.get('node') != node or data.get('head') != head or data.get('host') != host
            or not isinstance(data.get('run_id'), str) or not re.fullmatch('[0-9a-f]{32}', data['run_id'])
            or data.get('state') not in ('running', 'pending', 'succeeded', 'failed', 'rollback_failed')
            or 'active_step' not in data
            or type(data.get('released')) is not bool
            or type(data.get('rolled_back')) is not bool or not isinstance(data.get('steps'), list)):
        return False
    try:
        preview_config({'local': {'preview_upgrade': data.get('config')}})
    except (ValueError, TypeError, KeyError):
        return False
    ownership = data.get('ownership')
    context = dict(resource=data['config']['resource'], host=host, run_id=data['run_id'])
    if not preview_ownership_valid(ownership, context):
        return data['state'] == 'pending' and not data['steps'] and data['active_step'] == 'acquire' and not data['released']
    steps, failed = data['steps'], None
    for index, item in enumerate(steps):
        if (not isinstance(item, dict) or set(item) != {'step', 'ok', 'receipt', 'ownership', 'quiescent'}
                or type(item.get('ok')) is not bool or not isinstance(item.get('receipt'), str)
                or not item['receipt'] or len(item['receipt']) > 4096
                or item.get('ownership') != ownership or item.get('quiescent') is not True):
            return False
        expected = 'rollback' if failed is not None else PREVIEW_STEPS[index] if index < 4 else None
        if item['step'] != expected or (failed == 'backup') or (index and steps[index-1]['step'] == 'rollback'):
            return False
        if not item['ok'] and item['step'] != 'rollback':
            failed = item['step']
    rollback = bool(steps and steps[-1]['step'] == 'rollback')
    restored = rollback and steps[-1]['ok']
    if data['rolled_back'] != restored:
        return False
    active = data.get('active_step')
    expected = None if rollback or failed == 'backup' else 'rollback' if failed else PREVIEW_STEPS[len(steps)] if len(steps) < 4 else None
    if active is not None and active != expected:
        return False
    state = data['state']
    if state in ('succeeded', 'failed', 'rollback_failed') and not data['released']:
        return False
    if state == 'succeeded':
        return len(steps) == 4 and failed is None and not rollback and active is None
    if state == 'failed':
        return failed is not None and (failed == 'backup' or restored) and active is None
    if state == 'rollback_failed':
        return rollback and not restored and active is None
    return True


def upgrade_preview(inst, n, head, adapters=None):
    """Run once after exact local landing; unknown effects need product reconciliation.

    A persistent running step is deliberately never retried automatically. A
    separate stable node lock prevents overlapping heads and configuration swaps.
    A failed protocol is terminal for this head; a future explicit reconciliation
    workflow belongs to its product adapter, not a blind retry here.
    """
    pending = lambda reason: dict(version=1, head=head, state='pending', reason=reason)
    if not isinstance(head, str) or not re.fullmatch('[0-9a-f]{40,64}', head):
        return pending('invalid landed head')
    host = _m_lifecycle_identity.lifecycle_host_identity()
    if not host:
        return pending('local host identity unavailable; product ownership unproven')
    try:
        fresh = _m_instance.Instance(inst.root)
        n = fresh.node(n['id'])
        config = preview_config(n)
        if any(other['id'] != n['id'] and isinstance(other['local'].get('preview_upgrade'), dict)
               and other['local']['preview_upgrade'].get('enabled') is True
               and other['local']['preview_upgrade'].get('resource') == config['resource'] for other in fresh.nodes()):
            return pending('preview resource is also configured for another node')
    except (OSError, ValueError, KeyError, TypeError):
        return pending('explicit complete per-node preview protocol required')
    if adapters is None:
        try:
            adapters = _m_preview_stdio.bootstrap_preview_adapters(n, PREVIEW_ADAPTERS)
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            return pending('trusted preview adapter registration unavailable or changed')
    adapter = adapters.get(config['adapter'])
    if (adapter is None or getattr(adapter, 'protocol', None) != config['protocol']
            or any(not callable(getattr(adapter, method, None)) for method in ('acquire', 'step', 'release'))):
        return pending('configured product adapter unavailable or protocol mismatched')
    if preview_landed_head(n) != head:
        return pending('head is not current landed main')
    folder = preview_folder(n)
    folder = _m_common.safe_target(folder) or folder
    if folder.is_symlink() or folder.parent.is_symlink():
        return pending('linked preview state directory')
    folder.mkdir(parents=True, exist_ok=True)
    lock = folder / 'owner.lock'
    lock = _m_common.safe_target(lock) or lock
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    resource_owner = contextlib.ExitStack()
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return pending('unsafe preview ownership lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return pending('preview resource already has an owner')
        try:
            resource_owner.enter_context(preview_resource_lock(inst, config['resource']))
        except (BlockingIOError, OSError, ValueError):
            return pending('exact preview resource already owned or lock unavailable')
        path = folder / (head + '.json')
        for other in folder.glob('*.json'):
            if other == path:
                continue
            _m_common.safe_target(other)
            prior = json.loads(other.read_text())
            if (not preview_receipt_valid(prior, n['id'], other.stem, host)
                    or prior.get('state') not in ('succeeded', 'failed')):
                return pending('prior preview operation requires product reconciliation')
        if path.exists():
            _m_common.safe_target(path)
            saved = json.loads(path.read_text())
            if (not preview_receipt_valid(saved, n['id'], head, host) or saved.get('config') != config):
                return pending('prior head receipt or configuration requires reconciliation')
            if saved.get('state') == 'running':
                saved.update(state='pending', reason='interrupted product step outcome unknown')
                preview_save(path, saved)
            return saved
        data = dict(version=1, node=n['id'], head=head, config=config, state='running',
                    host=host, run_id=os.urandom(16).hex(), steps=[], active_step='acquire',
                    ownership=None, released=False, rolled_back=False)
        preview_save(path, data)

        def current():
            try:
                current_instance = _m_instance.Instance(inst.root)
                now = current_instance.node(n['id'])
                claimed_elsewhere = any(other['id'] != n['id']
                    and isinstance(other['local'].get('preview_upgrade'), dict)
                    and other['local']['preview_upgrade'].get('enabled') is True
                    and other['local']['preview_upgrade'].get('resource') == config['resource']
                    for other in current_instance.nodes())
                return (_m_lifecycle_identity.lifecycle_host_identity() == host and not claimed_elsewhere
                        and preview_config(now) == config and preview_landed_head(now) == head)
            except (OSError, ValueError, KeyError, TypeError):
                return False

        ownership_context = dict(node=n['id'], head=head, host=host, run_id=data['run_id'],
                                 resource=config['resource'], protocol=config['protocol'])
        if not current():
            data.update(state='pending', reason='ownership inputs changed before acquire')
            preview_save(path, data)
            return data
        try:
            ownership = adapter.acquire(dict(ownership_context))
        except Exception:
            data.update(state='pending', reason='product ownership acquisition outcome unknown')
            preview_save(path, data)
            return data
        if not preview_ownership_valid(ownership, ownership_context):
            data.update(state='pending', reason='product did not return exact resource ownership')
            preview_save(path, data)
            return data
        ownership = json.loads(json.dumps(ownership))
        data.update(ownership=ownership, active_step=None)
        preview_save(path, data)

        def finish():
            # Never release an unknown/in-flight product operation. Its lease and
            # durable intent remain for explicit product reconciliation.
            if _m_lifecycle_identity.lifecycle_host_identity() != host:
                data.update(state='pending', reason='host changed before resource release')
                preview_save(path, data)
                return data
            if data['state'] == 'pending' or data['active_step'] is not None:
                return data
            try:
                result = adapter.release({**ownership_context, 'ownership': dict(ownership)})
            except Exception:
                result = None
            if (not isinstance(result, dict) or result.get('ownership') != ownership
                    or result.get('released') is not True or result.get('quiescent') is not True):
                data.update(state='pending', reason='product resource release or shutdown unconfirmed')
            else:
                data['released'] = True
            preview_save(path, data)
            return data

        def execute(step):
            if not current():
                data.update(state='pending', reason='main or preview configuration changed')
                preview_save(path, data)
                return None
            data['active_step'] = step
            preview_save(path, data)  # durable BEFORE crossing the product boundary
            context = dict(node=n['id'], head=head, host=host, run_id=data['run_id'], resource=config['resource'],
                           ownership=dict(ownership),
                           protocol=config['protocol'], operation=config[step],
                           idempotency_key=hashlib.sha256((n['id'] + ':' + head + ':' + step).encode()).hexdigest(),
                           backup_receipt=next((s['receipt'] for s in data['steps'] if s['step'] == 'backup'), None))
            try:
                result = adapter.step(step, context)
            except Exception:
                # A raised transport error has unknown effects. Never start rollback
                # concurrently with an operation whose shutdown was not confirmed.
                data.update(state='pending', reason='product step outcome unknown')
                preview_save(path, data)
                return None
            if (not isinstance(result, dict) or type(result.get('ok')) is not bool
                    or not isinstance(result.get('receipt'), str) or not result['receipt']
                    or len(result['receipt']) > 4096 or result.get('ownership') != ownership
                    or result.get('quiescent') is not True):
                data.update(state='pending', reason='product step did not confirm a bounded receipt')
                preview_save(path, data)
                return None
            data['steps'].append(dict(step=step, ok=result['ok'], receipt=result['receipt'],
                                      ownership=dict(ownership), quiescent=True))
            data['active_step'] = None
            preview_save(path, data)
            return result['ok']

        for step in PREVIEW_STEPS[:-1]:
            ok = execute(step)
            if ok is None:
                return data
            if not ok:
                if step != 'backup':
                    restored = execute('rollback')
                    if restored is None:
                        return data
                    data['rolled_back'] = restored
                    if not restored:
                        data.update(state='rollback_failed', reason='product rollback failed')
                        preview_save(path, data)
                        return finish()
                data.update(state='failed', reason=step + ' failed')
                preview_save(path, data)
                return finish()
        if not current():
            data.update(state='pending', reason='main or preview configuration changed after acceptance')
        else:
            data['state'] = 'succeeded'
        preview_save(path, data)
        return finish()
    finally:
        resource_owner.close()
        os.close(fd)


def landed_preview(inst, n, head):
    """Shared landing hook. Unconfigured nodes have no product side effects."""
    if 'preview_upgrade' not in n['local']:
        return dict(version=1, head=head, state='pending', reason='preview auto-upgrade not configured')
    _m_instance.unlock()
    try:
        return upgrade_preview(inst, n, head)
    except (OSError, ValueError, TypeError):
        return dict(version=1, head=head, state='pending', reason='preview state unreadable; retained for reconciliation')
