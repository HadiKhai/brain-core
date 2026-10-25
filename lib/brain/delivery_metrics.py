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
    result = {'job_key': key, 'requested_at': request.isoformat() if request else None,
            'first_usable_preview_at': preview.isoformat() if preview else None,
            'request_to_preview_seconds': (preview - request).total_seconds() if request and preview else None,
            'preview_measurement': 'observed' if request and preview else
                                   'missing_request' if not request else 'missing_usable_preview_evidence'}
    intervals, end = _delivery_phase_intervals(rows, now, request)
    phases = {}
    for start, stop, data in intervals:
        phase = phases.setdefault(data['phase'], {'working': 0.0, 'waiting': 0.0})
        phase[data['state']] += (stop - start).total_seconds()
    observed = sum(sum(values.values()) for values in phases.values())
    elapsed = (end - request).total_seconds() if request else None
    result.update(phase_seconds=phases, phase_observed_seconds=observed,
                  unattributed_seconds=max(0.0, elapsed - observed) if elapsed is not None else None,
                  phase_coverage='observed' if intervals else 'no_phase_observations')
    return result


def _delivery_phase_intervals(rows, now, request=None):
    terminals = [at for at, _, data in rows if data['fact_type'] in _TERMINAL
                 and (request is None or at >= request)]
    end = min([now, *terminals])
    transitions = [(at, data) for at, _, data in rows if data['fact_type'] == 'phase_changed'
                   and 'phase' in data and 'state' in data and at <= end]
    intervals = []
    for i, (at, data) in enumerate(transitions):
        stop = transitions[i + 1][0] if i + 1 < len(transitions) else end
        start = max(at, request) if request else at
        if stop > start and data['state'] in ('working', 'waiting'):
            intervals.append((start, stop, data))
    return intervals, end
