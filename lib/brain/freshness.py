"""Opt-in fingerprints for explicitly declared upstream files and index output."""
import hashlib
import json
import os
import stat
from pathlib import Path

from . import common as _m_common
from . import guard as _m_guard
from . import history as _m_history
from . import nodes as _m_nodes
from . import reminders as _m_reminders


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
        if not path.exists() and name == n['fm'].get('upstream_output'):
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
    if a.record:
        record_freshness(inst, n)
    state = freshness_state(inst, n)
    print(_m_guard.redact(n['id'], _m_guard.never_terms(n)) + ': index ' +
          (state['state'] if state else 'unconfigured'))
