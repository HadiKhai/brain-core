"""Additive, replayable imports of node-local commitments; never edits source prose."""
import datetime
import hashlib
import re

from . import common as _m_common
from . import instance as _m_instance
from . import jobs as _m_jobs
from . import tasks as _m_tasks


SOURCES = ('backlog.md', 'build-order.md', 'status.md', 'inbox.md')


def fingerprint(*parts):
    return hashlib.sha256('\0'.join(parts).encode()).hexdigest()[:16].upper()


def candidates(inst, n):
    """Import explicit list entries, not narrative paragraphs or generated facts."""
    entries = []
    for filename in SOURCES:
        path = n['dir'] / filename
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError('backfill source must be a regular node-local file')
        section, generated = '', False
        observed = datetime.datetime.fromtimestamp(path.stat().st_mtime, datetime.timezone.utc).isoformat()
        for line_no, raw in enumerate(path.read_text().splitlines(), 1):
            if '<!-- brain:facts' in raw:
                generated = True
            if generated:
                if 'brain:facts:end' in raw or 'brain:end facts' in raw or '<!-- /brain:facts' in raw:
                    generated = False
                continue
            if raw.startswith('#'):
                section = raw.lstrip('#').strip().lower()
                continue
            if filename == 'status.md' and not any(word in section for word in ('build order', 'promised', 'next', 'parked')):
                continue
            match = re.match(r'^(?:[-*] |(\d+)[.)] )(?:\[([ xX])\] )?(.+)$', raw)
            if not match or match[2] and match[2].lower() == 'x':
                continue
            title = match[3].strip()
            key = fingerprint(filename, title)
            ident_match = re.search(r'\b[A-Z][A-Z0-9]+-[0-9]+\b', title)
            ident = ident_match[0] if ident_match else 'TASK-' + key
            # Date is observation time, not an invented date for the original ask.
            dated = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?: [+-]\d{2}:\d{2})?)', title)
            at = dated[1] if dated and inst.parse_time(dated[1]) else observed
            entries.append({'id': ident, 'title': title, 'quote': raw, 'at': at,
                            'date_basis': 'source-timestamp' if dated else 'file-observed',
                            'ref': filename + ':' + str(line_no), 'origin': filename + ':' + key,
                            'order': int(match[1]) if match[1] else len(entries) + 1,
                            'status': 'backlog', 'source': 'local'})
    for row in _m_jobs.read_jobs(n) or []:
        if row.get('Ended'):
            continue
        key = _m_jobs.job_key(row)
        entries.append({'id': row.get('Task') or 'TASK-' + key.upper(), 'title': 'Work on ' + row['Job'],
                        'quote': row['Job'], 'at': row['Started'], 'date_basis': 'job-start',
                        'ref': 'job-ledger.md#' + key, 'origin': 'job:' + key, 'order': len(entries) + 1,
                        'status': 'in-progress', 'job': key, 'branch': row['Job'], 'source': 'local'})
    # Exact external IDs collapse duplicate prose references. Open job identity wins.
    unique = {}
    for entry in entries:
        if entry['id'] in unique:
            old = unique[entry['id']]
            entry = {**old, **entry, 'refs': old.get('refs', [old['ref']]) + [entry['ref']]}
        unique[entry['id']] = entry
    return list(unique.values())


def backfill(inst, n, apply=False):
    with _m_instance.brain_lock(inst):
        if _m_jobs.read_jobs(n) is None:
            raise ValueError('unrecognized ledger; no backfill written')
        records = _m_tasks.read_records(n)
        requirements = _m_tasks.read_records(n, 'requirements')
        planned = []
        for entry in candidates(inst, n):
            ident = entry['id']
            origins = {p for r in records.values() for p in r.get('provenance', [])}
            if ident in records or entry['origin'] in origins:
                continue
            # Provider-owned issue references await a pull. Never manufacture a
            # parallel local product issue for an external source.
            if _m_tasks.source(n) == 'linear' and not ident.startswith('TASK-'):
                continue
            req_id = 'REQ-' + fingerprint(entry['origin'])
            ref = entry['ref']
            task = {'version': _m_tasks.VERSION, 'id': ident, 'title': entry['title'], 'kind': 'chore',
                    'status': entry['status'], 'priority': 'P2', 'order': entry['order'],
                    'owner': ','.join(n.get('owners', [])), 'depends_on': [], 'decisions': [], 'evidence': [],
                    'provenance': [entry['origin'], *entry.get('refs', [ref])], 'requirement': req_id,
                    'strikes': 0, 'source': 'local', 'created': inst.now(), 'updated': inst.now()}
            for key in ('job', 'branch'):
                if key in entry:
                    task[key] = entry[key]
            # Import only explicit links. Ambiguous prose stays backlog.
            task['decisions'] = re.findall(r'\b(?:repo:D[0-9]+|private:D-[A-Za-z0-9-]+)', entry['title'])
            requirement = {'version': _m_tasks.VERSION, 'id': req_id, 'quote': entry['quote'], 'at': entry['at'],
                           'date_basis': entry['date_basis'], 'ref': ref, 'created': inst.now()}
            planned.append((task, requirement))
            records[ident] = task
        # Preflight the whole batch before writing the first record. Requirement
        # existence is checked after staging them in memory, without temp files.
        validation_rows = {ident: {**row, 'requirement': ''} for ident, row in records.items()}
        _m_tasks.validate(n, validation_rows)
        if apply:
            for task, requirement in planned:
                if requirement['id'] not in requirements:
                    _m_tasks.save_record(n, requirement, 'requirements')
                _m_tasks.save_record(n, task)
            jobs = _m_jobs.read_jobs(n) or []
            for row in jobs:
                task = next((r for r in records.values() if r.get('job') == _m_jobs.job_key(row) and not row.get('Ended')), None)
                if task:
                    row['Task'] = task['id']
            if planned and jobs:
                _m_jobs.write_jobs(inst, n, jobs)
            inst.commit([], n['id'] + ': backfill task records')
        return [task for task, _ in planned]
