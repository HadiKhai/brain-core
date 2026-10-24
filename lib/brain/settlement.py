"""Validate coordinator-supplied host observations; never claim independent host proof."""
from . import jobs as _m_jobs
from . import status as _m_status


SETTLEMENT_MAX_AGE = 300
SETTLEMENT_TERMINAL = ('completed', 'failed', 'interrupted', 'cancelled', 'rolled_back')


def validate_settlement_attestation(inst, n, targets, data):
    def refuse():
        raise ValueError('incomplete, stale or unsafe coordinator settlement attestation; no ledger rows changed')
    if not isinstance(data, dict) or data.get('version') != 1 or data.get('node') != n['id']:
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
    protected = set()
    for node in inst.nodes():
        if node['local'].get('coordinator'):
            protected.update(_m_jobs.thread_reference_ids(node['local']['coordinator']))
        ledger = _m_jobs.read_jobs(node)
        if ledger is None:
            refuse()
        for row in ledger:
            if not row.get('Ended'):
                for ref, _ in _m_jobs.bound_worker_threads(node, row['Job']):
                    protected.update(_m_jobs.thread_reference_ids(ref))
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
        known = set()
        workers = n['local'].get('workers', {})
        if isinstance(workers, dict) and workers.get(entry['job_key']):
            known.update(_m_jobs.thread_reference_ids(workers[entry['job_key']]))
        branch_rows = [r for r in _m_jobs.read_jobs(n) or [] if r['Job'] == row['Job']]
        for work in _m_status.active_work(n, include_closed=True):
            name = work[0].replace('`', '').split()
            if name and name[0] == row['Job'] and len(work) > 1 and work[1] not in ('', '-', '—'):
                if len(branch_rows) > 1:
                    refuse()  # a branch-only display cannot attribute a historical target
                known.update(_m_jobs.thread_reference_ids(work[1]))
        if not known.issubset(ids):
            refuse()
        for thread in threads:
            parent = thread.get('parentThreadId')
            if parent is not None and parent not in ids:
                refuse()
    return sorted(all_ids)
