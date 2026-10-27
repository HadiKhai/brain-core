"""Reusable, exact-candidate checks for core's supported publication instructions.

This module never pushes. Receipts are local job evidence, not readiness or permission.
"""
import hashlib
import json
import os
import shlex
import subprocess
from pathlib import Path

from . import common as _m_common
from . import instance as _m_instance

_m_guard = _m_common.LazyModule(__package__, 'guard')
_m_jobs = _m_common.LazyModule(__package__, 'jobs')
_m_policy = _m_common.LazyModule(__package__, 'policy')
_m_verify = _m_common.LazyModule(__package__, 'verify')


def land_check_instructions(n):
    """Generate requirements only; displaying a brief never executes a check."""
    if not n['fm'].get('land_check', '').strip():
        return []
    return ['The configured land_check is mandatory on the exact candidate. Run '
            '`brain land-check ' + n['id'] + ' --branch <job> --run`; only '
            '`brain land-check ' + n['id'] + ' --branch <job> --push-step` emits a supported '
            'push command after validating the current passing receipt. Failed, missing or stale '
            'receipts refuse that step. This brief is not a passing receipt.', '']


def _fresh_job(inst, n, row, running=False):
    fresh = _m_instance.Instance(inst.root)
    node = fresh.node(n['id'])
    matches = [r for r in _m_jobs.read_jobs(node) or [] if _m_jobs.job_key(r) == _m_jobs.job_key(row)]
    if len(matches) != 1 or (running and matches[0].get('Ended')):
        raise ValueError('land-check job changed or closed')
    current = matches[0]
    try:
        from . import job_attempts as _m_attempts
    except ImportError:
        path = node['dir'] / 'job-attempts' / (_m_jobs.job_key(current) + '.json')
        if path.exists() or path.is_symlink() or (_m_common.CORE / 'lib/brain/job_attempts.py').exists():
            raise ValueError('land-check cannot read replacement attempt evidence')
        attempt = None
    else:
        attempt = _m_attempts.current_attempt(node, current, strict=True)
    if attempt and (attempt['state'] != 'terminal' or attempt['binding'] != _m_attempts.attempt_binding(node, current)):
        raise ValueError('land-check refuses an active, interrupted or stale replacement attempt')
    if attempt:
        signal = _m_jobs.read_handback(node, current, strict=True)
        if not signal or signal.get('attempt_id') != attempt['id'] or signal.get('state') != 'done':
            raise ValueError('land-check requires the replacement attempt\'s matching done publication')
    return fresh, node, current, attempt.get('id') if attempt else None


def _identity(n, w):
    from . import nodes as _m_nodes
    command = n['fm'].get('land_check', '')
    if not command.strip():
        return None
    path = Path(w['path'])
    head = _m_common.git('rev-parse', '--verify', 'HEAD^{commit}', cwd=path)
    dirty = _m_common.git('status', '--porcelain=v1', '-z', '--untracked-files=all',
                          '--ignore-submodules=none', cwd=path)
    if head.returncode or dirty.returncode or dirty.stdout:
        raise ValueError('land-check requires a clean exact candidate tip')
    policy_file = n['dir'] / 'publication-policy.json'
    policy = policy_file.read_bytes().hex() if policy_file.exists() else None
    identity = {'node': n['id'], 'settings': n['fm'], 'policy': policy,
                'limits': _m_policy.limit_snapshot(n), 'worktree': str(path.resolve()),
                'ports': [w.get('port'), w.get('ports', 1)], 'path': os.environ.get('PATH', '')}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return {'head': head.stdout.strip(), 'command': _m_nodes.import_text(command, _m_guard.never_terms(n)),
            'config_digest': digest, 'inputs': _m_verify.verify_inputs(n, w)}


def run_check(inst, n, row, w, env=None, related=()):
    """Run once on the candidate. Retain failures, including commands that mutate the tip/config."""
    with _m_instance.brain_lock(inst):
        n = _m_instance.Instance(inst.root).node(n['id'])
        if not n['fm'].get('land_check', '').strip():
            return None
        fresh, node, current, attempt = _fresh_job(inst, n, row, running=True)
        before = _identity(node, w)
        if before is None:
            return None
        related_before = [_fresh_job(inst, node, other, running=True) for other in related]
    command = node['fm']['land_check']
    execution_env = env or _m_verify.verify_env(node, current['Job'], w)
    result = subprocess.run(command, shell=True, cwd=w['path'], env=execution_env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    code = result.returncode
    with _m_instance.brain_lock(inst):
        fresh, node, current, after_attempt = _fresh_job(inst, node, current, running=True)
        try:
            unchanged = before == _identity(node, w) and attempt == after_attempt
        except (OSError, ValueError):
            unchanged = False
        if not unchanged and code == 0:
            code = 1
        receipt = {'version': 1, 'node': node['id'], 'job_key': _m_jobs.job_key(current),
                   'branch': current['Job'], **before, 'attempt': attempt,
                   'checked_at': fresh.now_dt().isoformat(), 'returncode': code,
                   'result': 'passed' if code == 0 else 'failed'}
        _m_jobs.record_land_check(fresh, node, _m_jobs.job_key(current), receipt)
        for _, _, other, identity in related_before:
            _, _, other, current_identity = _fresh_job(fresh, node, other, running=True)
            if identity != current_identity:
                raise ValueError('related landing job attempt changed')
            other_receipt = {**receipt, 'job_key': _m_jobs.job_key(other), 'branch': other['Job'], 'attempt': identity}
            _m_jobs.record_land_check(fresh, node, _m_jobs.job_key(other), other_receipt)
    return receipt


def require_current(inst, n, row, w):
    """Read-only evidence consumption. Never run configured commands from reads."""
    n = _m_instance.Instance(inst.root).node(n['id'])
    if not n['fm'].get('land_check', '').strip():
        return None
    fresh, node, current, attempt = _fresh_job(inst, n, row, running=True)
    expected = _identity(node, w)
    if expected is None:
        return None
    receipt = _m_jobs.read_land_check(node, current)
    when = fresh.parse_time(receipt['checked_at']) if receipt and isinstance(receipt.get('checked_at'), str) else None
    if (expected is None or not receipt or type(receipt.get('version')) is not int or receipt.get('version') != 1 or receipt.get('result') != 'passed'
            or type(receipt.get('returncode')) is not int or receipt.get('returncode') != 0 or receipt.get('node') != node['id']
            or receipt.get('job_key') != _m_jobs.job_key(current) or receipt.get('branch') != current['Job']
            or receipt.get('attempt') != attempt or any(receipt.get(k) != v for k, v in expected.items())
            or not when or when > fresh.now_dt()):
        raise ValueError('current passing land-check evidence is absent, failed or stale; run brain land-check --run')
    return receipt


def push_step(inst, n, row, w, ref=None, home=None, expected_base=None):
    """Generate a supported push step only after current policy and receipt checks."""
    with _m_instance.brain_lock(inst):
        fresh = _m_instance.Instance(inst.root)
        node = fresh.node(n['id'])
        decision = _m_policy.publication_decision(fresh, node, 'push')
        if decision['decision'] != 'allow':
            raise ValueError(decision['reason'])
        require_current(fresh, node, row, w)
        target = ref or row['Job']
        if not target or target.startswith('-') or _m_common.git('check-ref-format', '--branch', target).returncode:
            raise ValueError('invalid push ref')
        candidate = _m_common.git('rev-parse', '--verify', 'HEAD^{commit}', cwd=w['path']).stdout.strip()
        destination = home or node['home_path']
        actual = _m_common.git('rev-parse', '--verify', 'refs/heads/' + target + '^{commit}', cwd=destination)
        if actual.returncode or actual.stdout.strip() != (expected_base or candidate):
            raise ValueError('push destination ref differs from the checked candidate tip')
        source = candidate + ':refs/heads/' + target
        step = 'git -C ' + shlex.quote(str(destination)) + ' push origin ' + shlex.quote(source)
        if _m_guard.find_terms(step, _m_guard.never_terms(node)):
            raise ValueError('supported push step contains protected metadata; ask the coordinator')
        return step


def cmd_land_check(a):
    from . import nodes as _m_nodes
    inst = _m_nodes.load()
    n = inst.node(a.id)
    row = _m_jobs.open_job(_m_jobs.read_jobs(n) or [], a.branch)
    w = n['local'].get('worktrees', {}).get(a.branch)
    if not row or not w:
        _m_common.die('land-check requires a recorded open job and worktree')
    try:
        if a.run:
            receipt = run_check(inst, n, row, w)
            print('land-check: ' + (receipt['result'] if receipt else 'not configured; no outward permission granted'))
            if receipt and receipt['result'] != 'passed':
                _m_common.die('land-check failed; no push step')
        if a.push_step:
            print(push_step(inst, n, row, w))
        elif not a.run:
            receipt = require_current(inst, n, row, w)
            print('land-check: ' + ('current passing evidence' if receipt else 'not configured'))
    except (OSError, ValueError) as error:
        _m_common.die(_m_guard.redact(str(error), _m_guard.never_terms(n)))
