"""Delivery measurements from explicit observed facts, with missing evidence left unknown."""
import datetime as _datetime
from . import common as _m_common

_m_events = _m_common.LazyModule(__package__, 'events')


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
    completed = {key for key, rows in groups.items() if any(d['fact_type'] in _TERMINAL for _, _, d in rows)}
    for job in jobs:
        unresolved = [key for key in job['depends_on'] if key not in completed]
        job['unresolved_dependencies'] = unresolved
        job['blocking_dependencies'] = unresolved if job['job_key'] not in completed else []
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
    result.update(_delivery_rework_metrics(rows, end, request))
    result.update(_delivery_dependency_metrics(rows, end, request))
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


_REWORK_REASONS = ('review_finding', 'requirement_change')


def _delivery_rework_metrics(rows, end, request):
    result = {key: {'episodes': 0, 'seconds': 0.0} for key in (*_REWORK_REASONS, 'unclassified')}
    active = None
    for at, _, data in rows:
        if at > end:
            break
        kind = data['fact_type']
        if kind not in ('rework_started', 'rework_finished', *_TERMINAL):
            continue
        if active:
            start, reason = active
            start = max(start, request) if request else start
            result[reason]['seconds'] += max(0.0, (at - start).total_seconds())
            active = None
        if kind == 'rework_started':
            reason = data.get('reason_code')
            reason = reason if reason in _REWORK_REASONS else 'unclassified'
            result[reason]['episodes'] += 1
            active = (at, reason)
    if active:
        start, reason = active
        start = max(start, request) if request else start
        result[reason]['seconds'] += max(0.0, (end - start).total_seconds())
    return {'rework': result}


def _delivery_dependency_metrics(rows, end, request):
    declared, active, waits = [], None, {}
    for at, _, data in rows:
        if at > end:
            break
        kind = data['fact_type']
        if kind not in ('dependency_declared', 'dependencies_changed', 'dependency_blocked',
                        'dependency_unblocked', *_TERMINAL):
            continue
        if active:
            start, dependencies = active
            start = max(start, request) if request else start
            seconds = max(0.0, (at - start).total_seconds())
            for key in dependencies:
                waits[key] = waits.get(key, 0.0) + seconds
            active = None
        if 'depends_on' in data:
            declared = sorted(data['depends_on'])
        if kind == 'dependency_blocked':
            active = (at, declared)
    if active:
        start, dependencies = active
        start = max(start, request) if request else start
        for key in dependencies:
            waits[key] = waits.get(key, 0.0) + max(0.0, (end - start).total_seconds())
    ranked = sorted(waits, key=lambda key: (-waits[key], key))
    critical = ranked[0] if ranked and waits[ranked[0]] > 0 else None
    return {'depends_on': declared, 'dependency_wait_seconds': waits,
            'critical_dependency': critical,
            'critical_dependency_basis': 'largest_observed_wait' if critical else 'no_observed_dependency_wait',
            'forecast_critical_path': None}
