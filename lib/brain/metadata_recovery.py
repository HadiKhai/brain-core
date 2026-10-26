"""Explicit completion of interrupted settings packets, never generic rollback.

Only exact before/after file states may be adopted. Git history changes and
foreign edits require reconciliation by the owner, without automatic repair.
"""
import json
from pathlib import Path
import re

from . import common as _m_common
from . import instance as _m_instance


def unfinished_settings_batches(inst):
    folder = inst.root / '.brain/settings-batches'
    _m_common.safe_target(folder / 'pending.json')
    pending = []
    if folder.exists():
        for packet in folder.glob('*.json'):
            _m_common.safe_target(packet)
            if packet.is_symlink() or packet.stat().st_size > 8 * 1024 * 1024:
                raise ValueError('unsafe or oversized settings recovery packet')
            record = json.loads(packet.read_text())
            if not isinstance(record, dict) or record.get('state') not in (
                    'committed', 'applied-local', 'recovered', 'pending', 'failed', 'recovering'):
                raise ValueError('unknown settings recovery state')
            if record['state'] in ('pending', 'failed', 'recovering'):
                pending.append(packet)
    return pending


def metadata_target(root, relative):
    if (not isinstance(relative, str) or not relative or Path(relative).is_absolute()
            or any(p in ('', '.', '..') for p in relative.split('/'))
            or not (relative in ('node.md', 'registry.md', 'schedules.md', '.brain/local.json')
                    or re.fullmatch(r'(?:nodes/[A-Za-z0-9_.-]+/)+node\.md', relative))):
        raise ValueError('recovery packet contains a non-metadata target')
    path = root / relative
    for parent in [path, *path.parents]:
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError('metadata recovery refuses linked targets')
    _m_common.safe_target(path)
    return path


def recover_settings_batch(inst, packet):
    """Complete under the writer lock; retries never overwrite a third state."""
    packet = Path(packet).absolute()
    folder = inst.root / '.brain/settings-batches'
    if (packet.parent != folder or packet.is_symlink() or folder.is_symlink()
            or not re.fullmatch(r'[A-Za-z0-9_-]+\.json', packet.name)):
        raise ValueError('recovery requires an owned settings-batch packet')
    _m_common.safe_target(packet)
    with _m_instance.brain_lock(inst):
        if packet.stat().st_size > 8 * 1024 * 1024:
            raise ValueError('oversized settings recovery packet')
        if any(p != packet for p in unfinished_settings_batches(inst)):
            raise ValueError('another unfinished metadata owner requires reconciliation')
        record = json.loads(packet.read_text())
        if (not isinstance(record, dict) or record.get('state') not in ('pending', 'failed', 'recovering', 'recovered')
                or not isinstance(record.get('before'), dict) or not record['before']
                or not isinstance(record.get('after'), dict) or set(record['before']) != set(record['after'])
                or len(record['after']) > 128 or not isinstance(record.get('base'), str)
                or any(not isinstance(v, str) for v in record['after'].values())
                or any(v is not None and not isinstance(v, str) for v in record['before'].values())):
            raise ValueError('invalid settings recovery packet')
        targets = {r: metadata_target(inst.root, r) for r in record['after']}
        def contents(path):
            return path.read_text() if path.exists() else None
        for relative, path in targets.items():
            if contents(path) not in (record['before'][relative], record['after'][relative]):
                raise ValueError('metadata target changed by a foreign edit; no recovery writes made')
        if record['state'] == 'recovered':
            if any(contents(p) != record['after'][r] for r, p in targets.items()):
                raise ValueError('recovered metadata changed; reconcile explicitly')
            return packet
        head_result = _m_common.git('rev-parse', 'HEAD', cwd=inst.root)
        head = head_result.stdout.strip()
        if head_result.returncode or not head or head != record['base']:
            raise ValueError('Git HEAD changed since the packet; reconcile commit outcome explicitly')
        shared = [p for p in targets.values() if '.brain' not in p.relative_to(inst.root).parts]
        # Preserve unrelated staged work and refuse third-state staged metadata.
        for relative, path in targets.items():
            if path not in shared or not head:
                continue
            indexed = _m_common.git('show', ':' + relative, cwd=inst.root)
            value = indexed.stdout if indexed.returncode == 0 else None
            if value not in (record['before'][relative], record['after'][relative]):
                raise ValueError('staged metadata changed by a foreign edit')
        _m_common.durable_makedirs(folder)
        record['state'] = 'recovering'
        _m_common.atomic_write(packet, json.dumps(record, indent=2) + '\n')
        for relative, path in targets.items():
            # Do not overwrite another file writer arriving after validation.
            metadata_target(inst.root, relative)
            if contents(path) not in (record['before'][relative], record['after'][relative]):
                raise ValueError('metadata changed during recovery; pending packet retained')
            if contents(path) != record['after'][relative]:
                _m_common.durable_makedirs(path.parent)
                _m_common.atomic_write(path, record['after'][relative])
        if any(contents(p) != record['after'][r] for r, p in targets.items()):
            raise ValueError('metadata changed before recovery commit; packet retained')
        inst.commit_settings_batch(shared, 'settings: complete interrupted metadata batch')
        record.update(state='recovered', head=_m_common.git('rev-parse', 'HEAD', cwd=inst.root).stdout.strip())
        _m_common.atomic_write(packet, json.dumps(record, indent=2) + '\n')
        inst.local = inst._load_local()
    return packet


def cmd_settings_recover(a):
    from . import nodes as _m_nodes
    inst = _m_instance.Instance(_m_nodes.find_root())
    try:
        recover_settings_batch(inst, Path(a.file).expanduser())
    except (OSError, ValueError) as error:
        _m_common.die(str(error))
    print('settings recovery: exact metadata packet completed')
