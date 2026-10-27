"""Read-only age and waiting/failure evidence for cached dashboard snapshots."""
import json
import re

from . import common as _m_common

_m_guard = _m_common.LazyModule(__package__, 'guard')
_m_jobs = _m_common.LazyModule(__package__, 'jobs')
_m_nodes = _m_common.LazyModule(__package__, 'nodes')
_m_status = _m_common.LazyModule(__package__, 'status')


def ui_work_metadata(inst, n):
    """Open ledger work, with faults bound to the current worker's latest valid run.

    The host snapshot's record order is retained: the last matching record wins.
    Error excerpts and host identities never leave this reader.
    """
    rows = _m_jobs.read_jobs(n) or []
    opened = {row['Job']: row for row in rows if row.get('Job') and not row.get('Ended')}
    workers = n['local'].get('workers', {})
    workers = workers if isinstance(workers, dict) else {}
    try:
        data = json.loads((inst.root / '.brain' / 'worker-runs.json').read_text())
        records = data.get('runs', []) if isinstance(data, dict) else []
        records = records if isinstance(records, list) else []
    except (OSError, ValueError, UnicodeError):
        records = []
    latest = {}
    keys = {_m_jobs.job_key(row) for row in opened.values()}
    for record in records:
        if not isinstance(record, dict) or record.get('node') != n['id']:
            continue
        key, thread, run, state = (record.get(name) for name in ('job_key', 'thread', 'run', 'status'))
        if (not isinstance(key, str) or key not in keys or not isinstance(thread, str) or not thread
                or workers.get(key) != thread or not isinstance(run, str) or not run.strip()
                or not isinstance(state, str) or not state.strip()):
            continue
        latest[key] = record
    now = inst.now_dt()
    out = {}
    for branch, row in opened.items():
        started = inst.parse_time(row.get('Started'))
        record = latest.get(_m_jobs.job_key(row), {})
        fault = record.get('status') == 'failed'
        category = ''
        if fault:
            error = str(record.get('error', '')).casefold()
            category = 'at capacity' if 'at capacity' in error else (
                'usage limit' if 'usage limit' in error else 'provider error')
        out[branch] = {
            'since': started.isoformat(timespec='seconds') if started else None,
            'age_hours': round(max(0, _m_status.elapsed(started, now) / 3600), 2) if started else None,
            'fault': fault, 'failure_category': category,
        }
    return out


def ui_blocked_metadata(inst, n, text, file_only=False):
    """Use explicit waiting fields; a blocked section alone does not identify its owner."""
    data = _m_status.blocked_metadata(inst, n, 'blocked', text, file_only=file_only)
    match = re.search(r'\bwaiting\s+on\s+([^;]+);\s*clears\s+when\s+([^;]+)', text, re.I)
    waiting, clears = (part.strip() for part in match.groups()) if match else ('', '')
    names = ['operator', 'you']
    operator = inst.cfg.get('operator')
    if isinstance(operator, str) and operator.strip():
        names.append(operator.strip())
    waiting_on_operator = bool(re.search(
        r'(?<!\w)(?:' + '|'.join(re.escape(name.casefold()) for name in names) + r')(?!\w)',
        waiting.casefold()))
    terms = _m_guard.never_terms(n)
    return {**data,
            'waiting_on': _m_nodes.import_text(waiting, terms),
            'clears_when': _m_nodes.import_text(clears, terms),
            'waiting_on_operator': waiting_on_operator}
