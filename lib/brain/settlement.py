"""Validate coordinator-supplied host observations; never claim independent host proof."""
import json
import shlex

from . import common as _m_common

_m_jobs = _m_common.LazyModule(__package__, 'jobs')
_m_status = _m_common.LazyModule(__package__, 'status')
_m_nodes = _m_common.LazyModule(__package__, 'nodes')
_m_guard = _m_common.LazyModule(__package__, 'guard')


SETTLEMENT_MAX_AGE = 300
SETTLEMENT_TERMINAL = ('completed', 'failed', 'interrupted', 'cancelled', 'rolled_back')


def bound_settlement_threads(n, row):
    """Use exact job bindings; branch-only references require an unambiguous job."""
    key = _m_jobs.job_key(row)
    known = set()
    workers = n['local'].get('workers', {})
    if isinstance(workers, dict) and workers.get(key):
        known.update(_m_jobs.thread_reference_ids(workers[key]))
    retired = n['local'].get('retired_workers', {}).get(key, [])
    if not isinstance(retired, list) or any(not isinstance(ref, str) or not ref.strip() for ref in retired):
        raise ValueError('invalid retired thread bindings; leave unsettled')
    for reference in retired:
        known.update(_m_jobs.thread_reference_ids(reference))
    branch_rows = [r for r in _m_jobs.read_jobs(n) or [] if r['Job'] == row['Job']]
    for work in _m_status.active_work(n, include_closed=True):
        name = work[0].replace('`', '').split()
        if name and name[0] == row['Job'] and len(work) > 1 and work[1] not in ('', '-', '—'):
            if len(branch_rows) > 1:
                raise ValueError('ambiguous branch-only thread ownership; leave unsettled')
            known.update(_m_jobs.thread_reference_ids(work[1]))
    if any(not identity or any(c.isspace() for c in identity) for identity in known):
        raise ValueError('invalid bound thread identity; leave unsettled')
    return known


def protected_settlement_threads(inst):
    protected = set()
    for node in inst.nodes():
        if node['local'].get('coordinator'):
            protected.update(_m_jobs.thread_reference_ids(node['local']['coordinator']))
        ledger = _m_jobs.read_jobs(node)
        if ledger is None:
            raise ValueError('unrecognized job ledger; leave unsettled')
        for row in ledger:
            if not row.get('Ended'):
                for ref, _ in _m_jobs.bound_worker_threads(node, row['Job']):
                    protected.update(_m_jobs.thread_reference_ids(ref))
    return protected


def cmd_settle(a):
    inst = _m_nodes.load()
    n = inst.node(a.id)
    rows = _m_jobs.read_jobs(n)
    matches = [row for row in rows or [] if _m_jobs.job_key(row) == a.job]
    if len(matches) != 1 or not _m_jobs.closed_job(matches[0]):
        _m_common.die('settle requires the exact key of a merged or closed ledger job')
    row = matches[0]
    if _m_jobs.open_job(rows, row['Job']):
        _m_common.die('settle refused: another job on that branch is open')
    try:
        identities = bound_settlement_threads(n, row)
        if identities & protected_settlement_threads(inst):
            raise ValueError('settle refused: bound thread is a coordinator or belongs to an open job')
    except ValueError as error:
        _m_common.die(str(error))
    lines = ['Printed instructions only; no host calls performed.',
             'Check fresh terminal state and no pending requests for every thread, including reviewers and descendants.']
    for identity in sorted(identities):
        lines.append('t3_thread_organize ' + json.dumps({'action': 'settle', 'threadId': identity}, sort_keys=True))
    if not identities:
        lines.append('No bound thread IDs; discover ownership through exhaustive host project lists before settlement.')
    lines += ['Never omit threadId or use threadIds. A settlesWhenTurnEnds reply means you settled yourself: unsettle at once.',
              'Confirm settled=true for each thread in t3_thread_list, then save a fresh exhaustive export.',
              'Add --thread <id> for every exported reviewer or descendant too.',
              'brain jobs ' + shlex.join([n['id'], '--branch', row['Job'], '--job-key', a.job, '--settled',
                  '--attestation-file', '<fresh-export.json>'] +
                  [part for identity in sorted(identities) for part in ('--thread', identity)])]
    print(_m_guard.redact('\n'.join(lines), _m_guard.never_terms(n)))


def validate_settlement_attestation(inst, n, targets, data):
    def refuse():
        raise ValueError('incomplete, stale or unsafe coordinator settlement attestation; no ledger rows changed')
    if not isinstance(data, dict) or type(data.get('version')) is not int or data.get('version') != 1 or data.get('node') != n['id']:
        refuse()
    observed = inst.parse_time(data.get('observed_at')) if isinstance(data.get('observed_at'), str) else None
    if not observed or not 0 <= (inst.now_dt() - observed).total_seconds() <= SETTLEMENT_MAX_AGE:
        refuse()
    projects = data.get('project_ids', [data.get('project_id')])
    expected_projects = {n['local'][key] for key in ('home_project', 'host_project') if n['local'].get(key)}
    if (not isinstance(projects, list) or not projects
            or any(not isinstance(project, str) or not project.strip() for project in projects)
            or len(set(projects)) != len(projects) or not expected_projects.issubset(set(projects))):
        refuse()
    coverage = data.get('coverage')
    if not isinstance(coverage, dict) or any(coverage.get(k) is not True for k in ('project', 'nested', 'complete')):
        refuse()
    entries = data.get('jobs')
    keys = {_m_jobs.job_key(row) for row in targets}
    if (not isinstance(entries, list) or any(not isinstance(e, dict) or not isinstance(e.get('job_key'), str) for e in entries)
            or len(entries) != len(keys) or {e.get('job_key') for e in entries} != keys):
        refuse()
    protected = protected_settlement_threads(inst)
    all_ids = set()
    for entry in entries:
        row = next(r for r in targets if _m_jobs.job_key(r) == entry['job_key'])
        if not _m_jobs.closed_job(row):
            refuse()
        ended = inst.parse_time(row['Ended'])
        if ended and observed < ended:
            refuse()
        threads = entry.get('threads')
        if not isinstance(threads, list):
            refuse()
        ids = set()
        for thread in threads:
            if not isinstance(thread, dict):
                refuse()
            identity = thread.get('threadId')
            if (not isinstance(identity, str) or not identity or any(c.isspace() for c in identity)
                    or identity in all_ids or identity in protected or thread.get('job_key') != entry['job_key']
                    or thread.get('state') not in SETTLEMENT_TERMINAL or thread.get('settled') is not True
                    or thread.get('pending_requests') != []):
                refuse()
            ids.add(identity)
            all_ids.add(identity)
        known = bound_settlement_threads(n, row)
        if not known.issubset(ids):
            refuse()
        for thread in threads:
            parent = thread.get('parentThreadId')
            if parent is not None and parent not in ids:
                refuse()
    return sorted(all_ids)
