"""Opt-in fingerprints for explicitly declared upstream files and index output."""
import hashlib
import json
import os
import stat
from pathlib import Path

from . import common as _m_common

_m_guard = _m_common.LazyModule(__package__, 'guard')
_m_history = _m_common.LazyModule(__package__, 'history')
_m_nodes = _m_common.LazyModule(__package__, 'nodes')
_m_reminders = _m_common.LazyModule(__package__, 'reminders')


INDEX_FILE_LIMIT = 64
INDEX_BYTE_LIMIT = 16 * 1024 * 1024


def freshness_contract(n):
    inputs, output = n['fm'].get('upstream_inputs'), n['fm'].get('upstream_output')
    if not inputs and not output:
        return None
    if not isinstance(inputs, str) or not isinstance(output, str) or not output.strip():
        raise ValueError('upstream_inputs and upstream_output are both required')
    values = json.loads(inputs) if inputs.lstrip().startswith('[') else [x.strip() for x in inputs.split(',')]
    if (not isinstance(values, list) or not 1 <= len(values) <= INDEX_FILE_LIMIT or
            any(not isinstance(x, str) or not x.strip() for x in values) or len(values) != len(set(values))):
        raise ValueError('upstream_inputs must declare 1 to 64 distinct explicit files')
    for name in values + [output]:
        path = Path(name)
        if (path.is_absolute() or any(p in ('..', '.brain', '.git', 'nodes') for p in path.parts)
                or any(x in name for x in ('*', '?', '\x00'))):
            raise ValueError('upstream paths must be explicit files within this node home')
    if output in values:
        raise ValueError('index output cannot also be an upstream input')
    return {'inputs': sorted(values), 'output': output}


def freshness_hash(inst, n, name):
    home = n['home_path']
    if not home or not home.is_dir():
        raise ValueError('node home is unavailable')
    home = home.resolve()
    path = home / name
    forbidden = []
    for other in inst.nodes():
        if other['id'] != n['id']:
            # Ancestor instance directories are not a forbidden subtree: the own
            # home may be nested there. Child and unrelated boundaries are.
            forbidden += [p.resolve() for p in (other['dir'], other['home_path'])
                          if p and home != p.resolve() and p.resolve() not in home.parents]
    stream, identity = _m_history.history_open(path, [home], forbidden)
    if stream is None:
        if not path.exists() and not path.is_symlink() and name == n['fm'].get('upstream_output'):
            raise FileNotFoundError('index output missing')
        raise ValueError('declared file is unavailable or outside its allowed scope')
    with stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > INDEX_BYTE_LIMIT:
            raise ValueError('declared file must be an unshared regular file of at most 16 MiB')
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > INDEX_BYTE_LIMIT:
                raise ValueError('declared file grew beyond its bound')
            digest.update(chunk)
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError('declared file changed while fingerprinting')
        if _m_history.history_fd_path(stream.fileno()) != str(path.resolve()):
            raise ValueError('declared file moved while fingerprinting')
        attached = os.stat(path, follow_symlinks=False)
        if (attached.st_dev, attached.st_ino) != identity:
            raise ValueError('declared file was replaced while fingerprinting')
        return digest.hexdigest()


def freshness_measure(inst, n):
    contract = freshness_contract(n)
    if contract is None:
        return None
    hashes = {name: freshness_hash(inst, n, name) for name in contract['inputs']}
    output = freshness_hash(inst, n, contract['output'])
    payload = {'contract': contract, 'inputs': hashes, 'output': output}
    return {**payload, 'fingerprint': hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()}


def freshness_state(inst, n):
    try:
        current = freshness_measure(inst, n)
        if current is None:
            return None
        records = _m_reminders.lifecycle_read(inst).get('indexes', {})
        if not isinstance(records, dict):
            raise ValueError('invalid index fingerprint receipts')
        recorded = records.get(n['id'])
        return {'state': 'fresh' if isinstance(recorded, dict) and recorded.get('fingerprint') == current['fingerprint']
                else 'stale', 'recorded_at': recorded.get('at') if isinstance(recorded, dict) else None}
    except FileNotFoundError:
        return {'state': 'missing'}
    except (OSError, ValueError, TypeError):
        return {'state': 'unknown'}


def record_freshness(inst, n):
    try:
        current = freshness_measure(inst, n)
        if current is None:
            _m_common.die('configure upstream_inputs and upstream_output before recording an index')
        with _m_reminders.lifecycle_store(inst) as data:
            records = data.setdefault('indexes', {})
            if not isinstance(records, dict) or len(records) >= _m_reminders.LIFECYCLE_LIMIT and n['id'] not in records:
                raise ValueError('invalid or full index fingerprint receipts')
            # A receipt records observation, not proof that a generator was executed.
            records[n['id']] = {**current, 'at': inst.now()}
    except (OSError, ValueError, TypeError):
        _m_common.die('index fingerprint could not be recorded; inputs/output must all be readable and scope-allowed')


def cmd_index(a):
    inst = _m_nodes.load()
    n = inst.node(a.id)
    if getattr(a, 'refresh', False):
        try:
            regenerate_index(inst, n)
        except (OSError, ValueError):
            _m_common.die('managed index regeneration refused; check declared scope and output ownership')
    elif a.record:
        record_freshness(inst, n)
    state = freshness_state(inst, n)
    print(_m_guard.redact(n['id'], _m_guard.never_terms(n)) + ': index ' +
          (state['state'] if state else 'unconfigured'))


MANAGED_INDEX_MARKER = '<!-- brain:upstream-index v1 -->'


def regenerate_index(inst, n):
    """Opt-in native link index; never execute a command or replace an unowned file."""
    import urllib.parse
    contract = freshness_contract(n)
    if contract is None or n['fm'].get('upstream_generator') != 'links':
        raise ValueError('configure upstream_generator=links and explicit inputs/output')
    home = n.get('home_path')
    if not home or not home.is_dir():
        raise ValueError('node home is unavailable')
    home = home.resolve()
    target = home / contract['output']
    # A missing output still needs containment, boundary and link checks.
    for path in [target, *target.parents]:
        if path == home:
            break
        if path.is_symlink():
            raise ValueError('managed index paths must not be linked')
    if home not in target.resolve().parents:
        raise ValueError('managed index is outside its node home')
    for other in inst.nodes():
        if other['id'] == n['id']:
            continue
        for boundary in (other['dir'], other.get('home_path')):
            if boundary:
                boundary = boundary.resolve()
                if home != boundary and home not in boundary.parents:
                    continue
                if boundary != home and (target.resolve() == boundary or boundary in target.resolve().parents):
                    raise ValueError('managed index crosses another node boundary')
    inputs = {name: freshness_hash(inst, n, name) for name in contract['inputs']}
    existing = None
    if target.exists():
        freshness_hash(inst, n, contract['output'])
        existing = target.read_text()
        if not existing.startswith(MANAGED_INDEX_MARKER + '\n'):
            raise ValueError('existing index is not owned by the native link generator')
    lines = [MANAGED_INDEX_MARKER, '# Source index', '', '| Source | SHA-256 |', '| --- | --- |']
    for name, fingerprint in inputs.items():
        label = name.replace('\\', '\\\\').replace('|', '\\|').replace('[', '\\[').replace(']', '\\]')
        lines.append('| [' + label + '](' + urllib.parse.quote(name, safe='/') + ') | `' + fingerprint + '` |')
    text = '\n'.join(lines) + '\n'
    if existing != text:
        if not target.parent.is_dir():
            raise ValueError('managed index parent must already exist')
        _m_common.safe_target(target)
        _m_common.atomic_write(target, text)
    current = freshness_measure(inst, n)
    if current['inputs'] != inputs:
        raise ValueError('upstream inputs changed during generation; receipt not recorded')
    record_freshness(inst, n)
    return existing != text


def maintain_indexes(inst, nodes):
    """Run only explicitly opted-in native generators at writer maintenance."""
    changed = []
    for n in nodes:
        if n['fm'].get('upstream_generator') != 'links':
            continue
        if (freshness_state(inst, n) or {}).get('state') == 'fresh':
            continue
        try:
            if regenerate_index(inst, n):
                changed.append(n['id'])
        except (OSError, ValueError, SystemExit):
            print('brain: managed source index needs repair; existing content retained', file=__import__('sys').stderr)
    return changed
