"""Delivery measurements from explicit observed facts, with missing evidence left unknown."""
import datetime as _datetime
from . import events as _m_events


_TERMINAL = ('delivery_completed', 'landed', 'job_landed', 'task_landed')


def delivery_measurements(inst, n, now=None):
    now = now or inst.now_dt()
    groups = {}
    reconstructed = 0
    for index, event in enumerate(_m_events.facts(inst, n)):
        data = event['data']
        if data.get('origin') != 'observed':
            reconstructed += 1
            continue
        key = data.get('job_key')
        if not key:
            continue
        at = _datetime.datetime.fromisoformat(data['at'])
        if at > now:
            continue
        groups.setdefault(key, []).append((at, index, data))
    jobs = []
    for key, rows in sorted(groups.items()):
        rows.sort(key=lambda row: (row[0], row[1]))
        jobs.append(_delivery_job_metrics(key, rows, now))
    return {'node': n['id'], 'jobs': jobs, 'reconstructed_facts_excluded': reconstructed,
            'basis': 'explicit observed events; missing observations remain unknown'}


def _delivery_job_metrics(key, rows, now):
    requests = [at for at, _, d in rows if d['fact_type'] in ('request_received', 'job_requested')]
    request = requests[0] if requests else None
    previews = [at for at, _, d in rows if d['fact_type'] == 'preview_usable' and d.get('evidence')
                and (request is None or at >= request)]
    preview = previews[0] if previews else None
    return {'job_key': key, 'requested_at': request.isoformat() if request else None,
            'first_usable_preview_at': preview.isoformat() if preview else None,
            'request_to_preview_seconds': (preview - request).total_seconds() if request and preview else None,
            'preview_measurement': 'observed' if request and preview else
                                   'missing_request' if not request else 'missing_usable_preview_evidence'}
