"""Canonical focused execution with native admission and private, scrubbed logs."""
import json
import os
import shlex
import sys

from . import common as _m_common
from . import instance as _m_instance

_m_jobs = _m_common.LazyModule(__package__, 'jobs')
_m_verify = _m_common.LazyModule(__package__, 'verify')
_m_worktrees = _m_common.LazyModule(__package__, 'worktrees')
_m_nodes = _m_common.LazyModule(__package__, 'nodes')


def cmd_spec_run(a):
    argv = a.command
    if not argv:
        _m_common.die('spec-run requires --command followed by an executable and focused selector')
    inst = _m_nodes.load()
    n, home = _m_worktrees.git_home(inst, a.id)
    w = n['local'].get('worktrees', {}).get(a.branch)
    if not w or not _m_jobs.open_job(_m_jobs.read_jobs(n) or [], a.branch):
        _m_common.die('spec-run requires an open job with a registered worktree')
    head = _m_common.git('rev-parse', '--verify', 'HEAD', cwd=w['path'])
    branch = _m_common.git('symbolic-ref', '--quiet', '--short', 'HEAD', cwd=w['path'])
    if head.returncode or branch.returncode or branch.stdout.strip() != a.branch:
        _m_common.die('spec-run worktree is missing or on another branch')
    command = shlex.join(argv)
    env = _m_verify.verify_env(n, a.branch, w)
    _m_instance.unlock()
    result, log = _m_verify.run_verify(inst, n, a.branch, command, w, env)
    receipt = dict(version=1, scope='focused', node=n['id'], branch=a.branch,
                   head=head.stdout.strip(), command=command, result=result,
                   log=log, checked_at=inst.now_dt().isoformat())
    folder = n['dir'] / 'spec-runs'
    _m_common.safe_target(folder)
    folder.mkdir(exist_ok=True)
    path = folder / (os.urandom(16).hex() + '.json')
    _m_common.atomic_write(path, json.dumps(receipt, indent=2) + '\n', private=True)
    print('spec-run: focused result ' + str(result) + '; receipt ' + str(path))
    if result:
        sys.exit(result)
