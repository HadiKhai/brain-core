"""Register an existing linked checkout without executing or changing its work."""
import argparse
from pathlib import Path

from . import common as _m_common
from . import instance as _m_instance

_m_guard = _m_common.LazyModule(__package__, 'guard')
_m_jobs = _m_common.LazyModule(__package__, 'jobs')
_m_knowledge = _m_common.LazyModule(__package__, 'knowledge')
_m_nodes = _m_common.LazyModule(__package__, 'nodes')
_m_worktrees = _m_common.LazyModule(__package__, 'worktrees')


def adoption_checkout(home, branch):
    listing = _m_common.git('worktree', 'list', '--porcelain', '-z', cwd=home)
    if listing.returncode:
        raise ValueError('adoption refused: cannot enumerate linked worktrees')
    records, current = [], {}
    for field in listing.stdout.split('\0'):
        if not field:
            if current:
                records.append(current)
                current = {}
            continue
        name, _, value = field.partition(' ')
        current[name] = value
    if current:
        records.append(current)
    found = [record for record in records if record.get('branch') == 'refs/heads/' + branch]
    if len(found) != 1:
        raise ValueError('adoption refused: branch must have exactly one existing linked worktree')
    path = Path(found[0]['worktree'])
    if (not path.is_dir() or path.is_symlink() or path.resolve() == home.resolve()
            or 'bare' in found[0] or 'prunable' in found[0]):
        raise ValueError('adoption refused: use an existing linked worktree, never the home checkout')
    selected = _m_common.git('symbolic-ref', '--quiet', '--short', 'HEAD', cwd=path)
    if selected.returncode or selected.stdout.strip() != branch:
        raise ValueError('adoption refused: worktree branch changed')
    return path.resolve(), found[0].get('HEAD', '')


def adopt_job(inst, n, branch, thread):
    if not n['external'] or not n.get('home_path'):
        raise ValueError('adoption refused: node needs an external Git home')
    if not branch or _m_common.git('check-ref-format', '--branch', branch, cwd=n['home_path']).returncode:
        raise ValueError('adoption refused: invalid branch')
    if not isinstance(thread, str) or not thread or any(c.isspace() for c in thread):
        raise ValueError('adoption refused: supply one explicit worker thread ID')
    identities = _m_jobs.thread_reference_ids(thread)
    if len(identities) != 1:
        raise ValueError('adoption refused: supply one explicit worker thread ID')
    thread = next(iter(identities))
    _m_guard.no_terms_in(n, branch, 'the adopted branch')
    if _m_common.outward_links(inst.root):
        raise ValueError('adoption refused: unsafe instance links')
    path, head = adoption_checkout(n['home_path'], branch)
    with _m_instance.brain_lock(inst):
        rows = _m_jobs.ledger_rows(n)
        if rows is None:
            raise ValueError('adoption refused: unreadable job ledger')
        row = _m_jobs.open_job(rows, branch)
        workers = n['local'].get('workers', {})
        worktrees = n['local'].get('worktrees', {})
        if not isinstance(workers, dict) or not isinstance(worktrees, dict):
            raise ValueError('adoption refused: invalid local ownership bindings')
        for node in inst.nodes():
            if thread in _m_jobs.thread_reference_ids(node['local'].get('coordinator', '')):
                raise ValueError('adoption refused: worker thread is a bound coordinator')
            other_workers = node['local'].get('workers', {})
            other_worktrees = node['local'].get('worktrees', {})
            if not isinstance(other_workers, dict) or not isinstance(other_worktrees, dict):
                raise ValueError('adoption refused: invalid instance ownership bindings')
            for key, bound in other_workers.items():
                if thread in _m_jobs.thread_reference_ids(bound) and not (
                        node['id'] == n['id'] and row and key == _m_jobs.job_key(row)):
                    raise ValueError('adoption refused: thread belongs to another ledger job')
            for name, binding in other_worktrees.items():
                if Path(binding['path']).resolve() == path and not (node['id'] == n['id'] and name == branch):
                    raise ValueError('adoption refused: checkout already belongs to another job')
        if row:
            binding = worktrees.get(branch, {})
            if workers.get(_m_jobs.job_key(row)) == thread and Path(binding.get('path', '.')).resolve() == path:
                return row, False
            raise ValueError('adoption refused: open job already exists; do not rebind it by adoption')
        if any(old['Job'] == branch for old in rows) or branch in worktrees:
            raise ValueError('adoption refused: historical job binding exists; use a new branch and checkout')
        route = argparse.Namespace(branch=branch, model=None)
        runtime, _ = _m_knowledge.worker_route(inst, n, route)
        # Resolve every refusal before recording ownership. No setup, checkout,
        # reset, host call or invented terminal handback occurs here.
        count = _m_worktrees.port_count(n)
        port = _m_worktrees.free_port(inst, count)
        _m_jobs.job_started(inst, n, branch, runtime)
        row = _m_jobs.open_job(_m_jobs.read_jobs(n) or [], branch)
        if not row:
            raise ValueError('adoption refused: job ledger publication failed')
        local = inst.local_of(n['id'])
        local.setdefault('worktrees', {})[branch] = {
            'path': str(path), 'port': port, **({'ports': count} if count > 1 else {}),
            'base': _m_worktrees.default_base(n['home_path']), 'start': head, 'created': inst.now(),
            'adopted': True}
        local.setdefault('workers', {})[_m_jobs.job_key(row)] = thread
        inst.save_local()
        return row, True


def cmd_adopt_job(a):
    inst = _m_nodes.load()
    try:
        row, created = adopt_job(inst, inst.node(a.id), a.branch, a.thread)
    except (OSError, ValueError) as error:
        _m_common.die(str(error))
    print(('adopted' if created else 'already adopted') + f": {row['Job']} ({_m_jobs.job_key(row)}); host activity unknown")
