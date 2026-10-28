"""Canonical focused execution with native admission and private, scrubbed logs."""
import json
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile

from . import common as _m_common
from . import instance as _m_instance

_m_jobs = _m_common.LazyModule(__package__, 'jobs')
_m_verify = _m_common.LazyModule(__package__, 'verify')
_m_worktrees = _m_common.LazyModule(__package__, 'worktrees')
_m_nodes = _m_common.LazyModule(__package__, 'nodes')
_m_gate_lock = _m_common.LazyModule(__package__, 'gate_lock')


def check_head(w, branch, expected=None):
    head = _m_common.git('rev-parse', '--verify', 'HEAD', cwd=w['path'])
    actual = _m_common.git('symbolic-ref', '--quiet', '--short', 'HEAD', cwd=w['path'])
    if head.returncode or actual.returncode or actual.stdout.strip() != branch:
        _m_common.die('spec-run worktree is missing or on another branch')
    if expected:
        status = _m_common.git('status', '--porcelain', '--untracked-files=all', cwd=w['path'])
        if head.stdout.strip() != expected or status.returncode or status.stdout:
            _m_common.die('spec-run requires the requested head and a clean worktree')
    return head.stdout.strip()


def cmd_spec_run(a):
    canonical = a.command is None
    if canonical and (not a.head or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', a.head)):
        _m_common.die('spec-run requires --head with a full commit SHA')
    if canonical and (not a.specs or any(not s or s.startswith('-') for s in a.specs)):
        _m_common.die('spec-run requires focused spec ids, not runner options')
    if not canonical and (not a.command or a.specs or a.head):
        _m_common.die('spec-run --command requires an executable; use --head only with spec ids')
    inst = _m_nodes.load()
    n, home = _m_worktrees.git_home(inst, a.id)
    if not a.branch:
        cwd = Path.cwd().resolve()
        matches = [branch for branch, row in n['local'].get('worktrees', {}).items()
                   if Path(row['path']).resolve() == cwd or Path(row['path']).resolve() in cwd.parents]
        if len(matches) != 1:
            _m_common.die('spec-run requires a registered current worktree or --branch')
        a.branch = matches[0]
    w = n['local'].get('worktrees', {}).get(a.branch)
    if not w or not _m_jobs.open_job(_m_jobs.read_jobs(n) or [], a.branch):
        _m_common.die('spec-run requires an open job with a registered worktree')
    head = check_head(w, a.branch, a.head)
    argv = a.command
    if canonical:
        try:
            argv = shlex.split(n['fm'].get('verify_spec', ''))
        except ValueError:
            _m_common.die('spec-run verify_spec must be an executable argument prefix')
        if not argv:
            _m_common.die('spec-run requires node verify_spec (focused runner argument prefix)')
        argv += a.specs
    command = shlex.join(argv)
    env = _m_verify.verify_env(n, a.branch, w)
    _m_instance.unlock()
    with _m_gate_lock.node_gate(inst, n, env, a.branch) as gate_env, _m_verify.verification_resources(inst, n) as resources:
        with _m_verify.command_reservation(inst, n, a.branch, kind='heavy') as token:
            check_head(w, a.branch, a.head)
            with tempfile.TemporaryDirectory(prefix='brain-spec-run-') as temporary:
                run_env = dict(gate_env, TMPDIR=temporary, TMP=temporary, TEMP=temporary)
                run_env.pop('BRAIN_TEST_RUN_ROOT', None)
                result, log = _m_verify.run_verify_stream(inst, n, a.branch, command, w, run_env, token)
                resources['failed'] = bool(result)
            check_head(w, a.branch, a.head)
    receipt = dict(version=1, scope='focused', node=n['id'], branch=a.branch,
                   head=head, command=command, result=result,
                   log=log, checked_at=inst.now_dt().isoformat())
    if canonical:
        receipt['specs'] = a.specs
    folder = n['dir'] / 'spec-runs'
    _m_common.safe_target(folder)
    folder.mkdir(exist_ok=True)
    path = folder / (os.urandom(16).hex() + '.json')
    _m_common.atomic_write(path, json.dumps(receipt, indent=2) + '\n', private=True)
    print('spec-run: focused result ' + str(result) + '; receipt ' + str(path))
    if result:
        sys.exit(result)
