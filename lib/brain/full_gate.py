"""Native direct-full evidence, separate from worker completion publication."""
import hashlib
import json
import os
import re
from pathlib import Path

from . import common as _m_common

_m_guard = _m_common.LazyModule(__package__, 'guard')
_m_jobs = _m_common.LazyModule(__package__, 'jobs')
_m_nodes = _m_common.LazyModule(__package__, 'nodes')
_m_verify = _m_common.LazyModule(__package__, 'verify')


def full_receipt_path(n, identity):
    if not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{32}', identity):
        raise ValueError('full receipt requires its native 32-character ID')
    path = n['dir'] / 'full-gates' / (identity + '.json')
    if (path.is_symlink() or path.parent.is_symlink()
            or path.exists() and (not path.is_file() or path.stat().st_nlink != 1)):
        raise ValueError('linked full receipt refused')
    return path


def log_digest(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('full receipt log unavailable')
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_full_receipt(inst, n, branch, data, head, main, inputs, port_context, snap, result=0):
    packet = data['packet']
    row = _m_jobs.open_job(_m_jobs.read_jobs(n) or [], branch)
    if not row or data.get('job_key') != _m_jobs.job_key(row):
        raise ValueError('full receipt belongs to another job attempt')
    if (type(data.get('version')) is not int or data['version'] != 1 or data.get('scope') != 'full'
            or not isinstance(packet, dict) or packet.get('version') != 1
            or packet.get('node') != n['id'] or packet.get('branch') != branch
            or packet.get('head') != head or packet.get('verified_head') != head
            or packet.get('main') != main or not _m_verify.packet_main_matches(n, packet, main)
            or packet.get('verify_command') != n['fm'].get('verify', '').strip()
            or not _m_verify.verification_config_valid(n, packet)
            or packet.get('verify_scope') == 'change'
            or type(packet.get('verify_result')) is not int or packet['verify_result'] != result
            or packet.get('verify_cheap', '') != n['fm'].get('verify_cheap', '').strip()
            or packet.get('inputs') != inputs or packet.get('verify_port_context') != port_context
            or not inst.parse_time(packet.get('checked_at'))):
        raise ValueError('full receipt is stale, failed or bound to another candidate')
    if packet.get('verify_scope') != 'full' and (bool(snap) != bool(packet.get('verify_lanes')) or (snap and not _m_verify.lane_packet_valid(packet, snap))):
        raise ValueError('full receipt lane evidence changed')
    logs = [r['log'] for r in packet.get('verify_lanes', [])] if snap else [packet['verify_log']]
    if data.get('logs') != {p: log_digest(p) for p in logs}:
        raise ValueError('full receipt logs changed')
    return packet


def adopt_full_receipt(inst, n, branch, identity, head, main, inputs, port_context, snap, reviews):
    data = json.loads(full_receipt_path(n, identity).read_text())
    if not isinstance(data, dict):
        raise ValueError('full receipt must be an object')
    if data.get('scope') == 'disposition':
        from . import full_disposition as _m_disposition
        packet = _m_disposition.validate_disposition(inst, n, branch, data, head, main, inputs, port_context)
    else:
        packet = validate_full_receipt(inst, n, branch, data, head, main, inputs, port_context, snap)
    return {**packet, 'review_rounds': reviews, 'verify_reused': True,
            'external_full_receipt': identity}


def publish_receipt(n, data, log=None):
    """Retain private log snapshots independently of the rotating worker log tail."""
    identity = os.urandom(16).hex()
    path = full_receipt_path(n, identity)
    _m_common.safe_target(path.parent)
    path.parent.mkdir(exist_ok=True)
    if log is not None:
        log_digest(log)
        target = path.with_suffix('.log')
        _m_common.atomic_write(target, Path(log).read_text(), private=True)
        data.update(log=str(target), log_sha256=log_digest(target))
    elif isinstance(data.get('packet'), dict):
        packet = data['packet']
        logs = [r['log'] for r in packet['verify_lanes']] if packet.get('verify_lanes') else [packet['verify_log']]
        retained = {}
        for index, original in enumerate(logs):
            log_digest(original)
            target = path.with_name(identity + '-' + str(index) + '.log')
            _m_common.atomic_write(target, Path(original).read_text(), private=True)
            retained[original] = str(target)
        if packet.get('verify_lanes'):
            packet['verify_lanes'] = [{**r, 'log': retained[r['log']]} for r in packet['verify_lanes']]
        packet['verify_log'] = retained[packet['verify_log']]
        data['logs'] = {p: log_digest(p) for p in retained.values()}
    serialized = json.dumps(data, indent=2) + '\n'
    _m_guard.no_terms_in(n, serialized, 'native full receipt')
    _m_common.atomic_write(path, serialized, private=True)
    return identity


def disposition_packet_valid(inst, n, branch, packet, head, main):
    """Revalidate the receipt graph and exact skip diff on every readiness read."""
    if packet.get('full_disposition') is not True or packet.get('verify_scope') != 'full':
        return False
    try:
        w = n['local']['worktrees'][branch]
        inputs = _m_verify.verify_inputs(n, w)
        port = _m_verify.verify_port_context(n, _m_verify.verify_env(n, branch, w))
        expected = adopt_full_receipt(inst, n, branch, packet['external_full_receipt'], head, main,
                                      inputs, port, None, packet['review_rounds'])
        return ({k: v for k, v in packet.items() if k != 'preview_scope'} ==
                {k: v for k, v in expected.items() if k != 'preview_scope'})
    except (OSError, ValueError, KeyError, TypeError, StopIteration, SyntaxError):
        return False


def cmd_full_run(a):
    inst = _m_nodes.load()
    n = inst.node(a.id)
    row = _m_jobs.open_job(_m_jobs.read_jobs(n) or [], a.branch)
    if not row:
        _m_common.die('full-run requires an open job')
    key = _m_jobs.job_key(row)
    packet, reasons = _m_verify.record_ready(inst, n, a.branch, a.reviews, reverify=False, full=True)
    if not packet or reasons or type(packet.get('verify_result')) is not int:
        _m_common.die('full-run failed: ' + '; '.join(reasons))
    current = _m_jobs.open_job(_m_jobs.read_jobs(n) or [], a.branch)
    if not current or _m_jobs.job_key(current) != key:
        _m_common.die('full-run job changed; no receipt published')
    try:
        identity = publish_receipt(n, dict(version=1, scope='full', job_key=key, packet=packet))
    except (OSError, ValueError, KeyError):
        _m_common.die('full receipt could not be recorded; no readiness published')
    result = packet['verify_result']
    print('full-run: ' + ('passed' if result == 0 else 'failed (exit ' + str(result) + ')')
          + '; full receipt ' + identity + '; no hand-back published')
    if result:
        raise _m_common.VerdictExit(result)


def cmd_gate(a):
    """Run the readiness policy without publishing a worker completion."""
    if a.train or a.nightly:
        return cmd_full_run(a)
    inst = _m_nodes.load()
    n = inst.node(a.id)
    if not _m_jobs.open_job(_m_jobs.read_jobs(n) or [], a.branch):
        _m_common.die('gate requires an open job')
    packet, reasons = _m_verify.record_ready(inst, n, a.branch, a.reviews)
    if not packet or reasons or type(packet.get('verify_result')) is not int or packet['verify_result'] != 0:
        _m_common.die('gate failed: ' + '; '.join(reasons))
    print('gate: passed; no hand-back or full receipt published')
