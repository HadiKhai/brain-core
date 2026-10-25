"""Causal progress and communication metrics from explicit file-backed evidence."""
import datetime
import json
from pathlib import Path

from . import common as _m_common
from . import coordinator_advisories as _m_coordinators
from . import events as _m_events
from . import guard as _m_guard
from . import jobs as _m_jobs
from . import nodes as _m_nodes
from . import ui as _m_ui


CAUSAL_REASONS = {
    'review_pending': ('reviewer', 'a review verdict is recorded', 'review'),
    'operator_decision': ('operator', 'the explicit decision is recorded', 'decision'),
    'capacity_wait': ('machine', 'native admission grants capacity', 'admission'),
    'dependency_pending': ('dependency owner', 'each named dependency is delivered', 'dependency'),
    'tests_pending': ('builder', 'the affected checks pass', 'focused_checks'),
    'integration_pending': ('coordinator', 'integration proof is recorded', 'integration'),
    'authorization_pending': ('authorized decision maker', 'explicit authorization is recorded', 'authorization'),
}


def introspection_time(value):
    if not isinstance(value, str):
        return None
    try:
        at = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return at if at.tzinfo is not None else None
    except ValueError:
        return None


def introspection_observed(events, now, job_key=None):
    """Unknown, reconstructed, wrong-job and future inputs cannot establish observations."""
    out, seen = [], set()
    for event in events:
        if not isinstance(event, dict) or event.get('kind') != 'fact' or not isinstance(event.get('data'), dict):
            continue
        data = event['data']
        try:
            _m_events.validate_fact(data)
        except (ValueError, TypeError, KeyError):
            continue
        at = introspection_time(data.get('at'))
        identity = event.get('id') or event.get('ref')
        if (not isinstance(identity, str) or not identity or identity in seen or not at or at > now
                or data.get('origin') != 'observed' or (job_key is not None and data.get('job_key') != job_key)):
            continue
        seen.add(identity)
        out.append((at, {**event, 'id': identity}))
    return [event for at, event in sorted(out, key=lambda pair: (pair[0], pair[1]['id']))]


def causal_job_view(row, job_key, events, rows_by_key, now, outcome):
    observed = introspection_observed(events, now, job_key)
    phases = [e for e in observed if e['data']['fact_type'] == 'phase_changed']
    latest = phases[-1] if phases else None
    data = latest['data'] if latest else {}
    reason = data.get('reason_code')
    owner, clears, gate = CAUSAL_REASONS.get(reason, (None, None, None))
    dependencies = []
    for key in data.get('depends_on', []):
        dep = rows_by_key.get(key)
        state = ('recorded_merged' if dep.get('Ended') and dep.get('Outcome') == 'merged' else
                 'closed_without_delivery' if dep.get('Ended') else 'open') if dep else 'unknown'
        dependencies.append({'job_key': key, 'state': state,
                             'branch': dep.get('Job') if dep else None,
                             'evidence': 'job-ledger.md' if dep else None})
    finished = bool(row.get('Ended'))
    return {'job_key': job_key, 'branch': row['Job'], 'outcome': outcome,
            'phase': 'finished' if finished else data.get('phase'),
            'state': 'done' if finished else data.get('state'),
            'blocker': reason if not finished and data.get('state') == 'waiting' else None,
            'owner': owner if not finished else None, 'clears_when': clears if not finished else None,
            'next_gate': gate if not finished else None, 'dependencies': dependencies,
            'evidence': ([{'event_id': latest['id'], 'at': data['at']}] if latest else []) +
                        ([{'source': 'job-ledger.md', 'ended': row['Ended']}] if finished else []),
            'scope': 'explicit observed phases and recorded ledger state; absent fields remain unknown'}


def communication_metrics(rows, events, start, end, queue):
    if not start < end:
        raise ValueError('metrics window must have positive duration')
    observed = introspection_observed(events, end)
    opened, answered, turns, conflicts = {}, {}, {}, set()
    for event in observed:
        data = event['data']
        kind, ref, at = data['fact_type'], data.get('evidence', {}).get('ref'), introspection_time(data['at'])
        if not ref:
            continue  # identity is required to join questions and deduplicate turns
        if kind == 'question_opened':
            opened.setdefault(ref, at)
        elif kind == 'question_answered' and ref in opened and at >= opened[ref]:
            answered.setdefault(ref, at)
        elif kind == 'turn_completed' and start <= at < end and data.get('reason_code') in ('decision', 'routine'):
            code = data['reason_code']
            if ref in turns and turns[ref] != code:
                conflicts.add(ref)
            turns[ref] = code
    waits = [(at - opened[ref]).total_seconds() for ref, at in answered.items() if start <= at < end]
    active = [(end - at).total_seconds() for ref, at in opened.items() if ref not in answered and at < end]
    valid_turns = [code for ref, code in turns.items() if ref not in conflicts]
    merges = sum(1 for row in rows if row.get('Outcome') == 'merged'
                 and (at := introspection_time(row.get('Ended'))) is not None and start <= at < end)
    hours = (end - start).total_seconds() / 3600
    return {'window': {'start': start.isoformat(), 'end': end.isoformat()},
            'queue_depth': queue['count'], 'queue_source': queue['source'],
            'oldest_queued_seconds': queue['oldest_wait_seconds'],
            'question_wait_seconds': {'answered_mean': sum(waits) / len(waits) if waits else None,
                                      'answered_count': len(waits), 'open_count': len(active),
                                      'oldest_open': max(active) if active else None},
            'recorded_merges': merges, 'merges_per_hour': merges / hours,
            'turns_observed': len(valid_turns), 'conflicting_turns': len(conflicts),
            'decision_turn_share': (valid_turns.count('decision') / len(valid_turns)) if valid_turns else None,
            'scope': 'recorded ledger merges and explicit observed question/turn events; no transcript inference'}


def introspection_queue(inst, n):
    now = inst.now_dt().timestamp()
    record = _m_coordinators.coordinator_host_runs(inst, now).get(n['id'])
    record = record if record and record['thread'] == n['local'].get('coordinator') else None
    queued = record['queued'] if record else _m_coordinators.coordinator_native_queue(inst, n, now)
    return {'count': len(queued), 'oldest_wait_seconds': max(now - at for at in queued.values()) if queued else None,
            'source': 'fresh_host_snapshot' if record else 'native_pending_only'}


def introspection_snapshot(inst, n, branch=None, hours=24, limit=50):
    if type(limit) is not int or not 1 <= limit <= 200 or not 0 < hours <= 24 * 31:
        raise ValueError('introspection needs limit 1..200 and hours greater than zero, at most 744')
    rows = _m_jobs.read_jobs(n)
    facts = _m_events.facts(inst, n)
    now = inst.now_dt()
    if rows is None:
        return {'version': 1, 'jobs': [], 'metrics': None, 'scope': 'legacy ledger unreadable; work state unknown'}
    by_key = {_m_jobs.job_key(row): row for row in rows}
    selected = [row for row in rows if row['Job'] == branch] if branch else [row for row in rows if not row['Ended']]
    views = []
    for row in selected[:limit]:
        contract = _m_jobs.contract_file(n, row)
        outcome = None
        if contract.is_file():
            outcome = _m_ui.md_sections(contract.read_text()).get('Outcome')
        views.append(causal_job_view(row, _m_jobs.job_key(row), facts, by_key, now, outcome))
    result = {'version': 1, 'node': n['id'], 'jobs': views, 'partial': len(selected) > limit,
              'metrics': communication_metrics(rows, facts, now - datetime.timedelta(hours=hours), now,
                                               introspection_queue(inst, n))}
    return result


def cmd_introspect(a):
    inst = _m_nodes.load()
    n = inst.node(a.id)
    try:
        result = introspection_snapshot(inst, n, a.branch, a.hours, a.limit)
    except (OSError, ValueError, TypeError) as error:
        _m_common.die(str(error))
    print(_m_guard.redact(json.dumps(result, sort_keys=True), _m_guard.never_terms(n)))
