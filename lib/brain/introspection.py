"""Causal progress and communication metrics from explicit file-backed evidence."""
import datetime
import json
import re
from pathlib import Path

from . import common as _m_common
from . import coordinator_advisories as _m_coordinators
from . import events as _m_events
from . import guard as _m_guard
from . import jobs as _m_jobs
from . import nodes as _m_nodes
from . import review as _m_review
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
    for sequence, event in enumerate(events):
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
        out.append((at, sequence, {**event, 'id': identity}))
    return [event for at, sequence, event in sorted(out, key=lambda item: (item[0], item[1]))]


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



def review_class_advisories(receipts, job_key):
    """Compare explicit major/blocker classes across attributable review rounds."""
    classes = {}
    for rec in receipts:
        if (not isinstance(rec, dict) or type(rec.get('version')) is not int or rec['version'] != 1
                or rec.get('job_key') != job_key
                or rec.get('verdict') not in ('pass', 'blocked') or not isinstance(rec.get('findings'), list)
                or not re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', str(rec.get('head', '')))):
            continue
        reviewer = rec.get('reviewer', {})
        if (not isinstance(reviewer, dict) or not isinstance(reviewer.get('threadId'), str) or not reviewer['threadId']
                or reviewer.get('fresh_context') is not True or reviewer.get('context_reused') is not False
                or reviewer.get('built_change') is not False or not introspection_time(rec.get('observed_at'))):
            continue
        round_id = (reviewer['threadId'], rec['head'])
        for finding in rec['findings']:
            if (not isinstance(finding, dict) or finding.get('severity') not in ('blocker', 'major')
                    or not isinstance(finding.get('class'), str)):
                continue
            label = re.sub(r'[\s-]+', '_', finding['class'].strip().casefold())
            if not re.fullmatch('[a-z][a-z0-9_]{0,63}', label):
                continue
            classes.setdefault(label, {})[round_id] = {'head': rec['head'],
                'reviewer_thread': reviewer['threadId'], 'observed_at': rec['observed_at']}
    return [{'kind': 'repeated-review-class', 'failure_class': label, 'review_count': len(rounds),
             'text': 'Repeated major/blocker class; audit the whole class before another repair round.',
             'evidence': list(rounds.values()), 'informational': True}
            for label, rounds in sorted(classes.items()) if len(rounds) >= 2]


def permission_request_advisories(events, job_key, now):
    grants, out = {}, []
    for event in introspection_observed(events, now, job_key):
        data = event['data']
        kind = data['fact_type']
        if kind == 'phase_changed':
            grants = {key: value for key, value in grants.items() if key[1] == data.get('phase')}
        ref = data.get('evidence', {}).get('ref')
        key = (data.get('head'), data.get('phase'), data.get('reason_code'), ref)
        if any(value is None for value in key):
            continue
        if kind == 'authorization_granted':
            grants[key] = event['id']
        elif kind == 'authorization_revoked':
            grants.pop(key, None)
        elif kind == 'permission_requested' and key in grants:
            out.append({'kind': 'repeated-permission-request', 'authorization_ref': ref,
                        'phase': data['phase'], 'action': data['reason_code'], 'informational': True,
                        'text': 'Permission was already recorded for this exact head, phase and action; inspect the existing contract.',
                        'evidence': [grants[key], event['id']]})
    return out


def acknowledgement_wait_advisories(events, job_key, now):
    receipts, latest = {}, None
    for event in introspection_observed(events, now, job_key):
        data = event['data']
        ref = data.get('evidence', {}).get('ref')
        if data['fact_type'] == 'handoff_delivered' and data.get('state') == 'done' and ref:
            if data.get('reason_code') == 'ack_not_required':
                receipts[ref] = event['id']
            else:
                receipts.pop(ref, None)
        elif data['fact_type'] == 'acknowledgement_required':
            receipts.pop(ref, None)
        if data['fact_type'] == 'phase_changed':
            latest = event
    data = latest['data'] if latest else {}
    ref = data.get('evidence', {}).get('ref')
    if (data.get('phase') != 'acknowledgement' or data.get('state') != 'waiting'
            or data.get('reason_code') != 'delivery_receipt' or ref not in receipts):
        return []
    return [{'kind': 'needless-acknowledgement-wait', 'informational': True,
             'text': 'A delivered handoff explicitly requires no acknowledgement; continue within the existing contract.',
             'evidence': [receipts[ref], latest['id']]}]


def unchanged_verification_advisories(events, job_key, now):
    previous, seen_runs, out = None, set(), []
    for event in introspection_observed(events, now, job_key):
        data = event['data']
        if data['fact_type'] != 'verify_completed':
            continue
        ref = data.get('evidence', {}).get('ref')
        if not ref or ref in seen_runs:
            continue
        seen_runs.add(ref)
        if (data.get('state') != 'done' or data.get('reason_code') != 'full_pass'
                or any(not re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', str(data.get(k, '')))
                       for k in ('head', 'base')) or not data.get('inputs')):
            previous = None
            continue
        signature = (data['head'], data['base'], data['inputs'])
        if previous and previous[0] == signature:
            out.append({'kind': 'unchanged-input-rerun', 'informational': True,
                        'text': 'A successful full verify repeated with the same head, base and input fingerprint; inspect proof reuse.',
                        'evidence': [previous[1], event['id']], 'head': data['head'], 'inputs': data['inputs']})
        previous = (signature, event['id'])
    return out


def introspection_product_request(events, job_key, now):
    observed = introspection_observed(events, now, job_key)
    requests = [e for e in observed if e['data']['fact_type'] == 'product_work_requested'
                and e['data'].get('product') is True and e['data'].get('evidence', {}).get('ref')]
    if not requests:
        return None, []
    request = requests[-1]
    at = introspection_time(request['data']['at'])
    following = [e for e in observed if introspection_time(e['data']['at']) >= at]
    ref = request['data']['evidence']['ref']
    if any(e['data']['fact_type'] == 'product_outcome_delivered' and e['data'].get('state') == 'done'
           and e['data'].get('evidence', {}).get('ref') == ref for e in following):
        return None, []
    return request, following


def infrastructure_displacement_advisories(events, job_key, now):
    request, following = introspection_product_request(events, job_key, now)
    if not request:
        return []
    phases = [e for e in following if e['data']['fact_type'] == 'phase_changed']
    durations = {True: 0.0, False: 0.0}
    evidence = [request['id']]
    for index, event in enumerate(phases):
        data = event['data']
        if data.get('phase') != 'execution' or data.get('state') != 'working' or type(data.get('product')) is not bool:
            continue
        end = introspection_time(phases[index + 1]['data']['at']) if index + 1 < len(phases) else now
        durations[data['product']] += max(0, (end - introspection_time(data['at'])).total_seconds())
        evidence.append(event['id'])
    if durations[False] <= durations[True] or not durations[False]:
        return []
    return [{'kind': 'infrastructure-overtaking-product', 'informational': True,
             'text': 'Explicitly classified infrastructure phase time exceeds product phase time before recorded outcome delivery; inspect the requested outcome.',
             'infrastructure_phase_seconds': durations[False], 'product_phase_seconds': durations[True],
             'evidence': evidence, 'scope': 'observed classified phase elapsed time, not CPU or inferred activity'}]


def preview_testing_advisories(events, job_key, now, head):
    if not head:
        return []  # an old event head cannot establish the current checkout identity
    runs, preview = {}, None
    for event in introspection_observed(events, now, job_key):
        data = event['data']
        if data.get('head') != head:
            continue
        ref = data.get('evidence', {}).get('ref')
        if not ref:
            continue
        if data['fact_type'] in ('verify_completed', 'test_completed') and data.get('state') == 'done':
            runs[ref] = event['id']
        if data['fact_type'] in ('preview_usable', 'preview_unusable', 'preview_exempt'):
            preview = event
    usable = preview and ((preview['data']['fact_type'] == 'preview_usable' and
                           preview['data'].get('state') == 'done') or preview['data']['fact_type'] == 'preview_exempt')
    if len(runs) <= 2 or usable:
        return []
    return [{'kind': 'testing-without-usable-preview', 'informational': True, 'test_runs': len(runs),
             'text': 'More than two completed check runs have no current usable-preview receipt; provide something usable to try.',
             'head': head, 'evidence': list(runs.values()) + ([preview['id']] if preview else []),
             'scope': 'recorded completed runs and supplied usability receipts; no live browser verification'}]


def introspection_head(n, branch):
    saved = n['local'].get('worktrees', {}).get(branch, {})
    where = Path(saved['path']) if isinstance(saved, dict) and saved.get('path') else n.get('home_path')
    if not where or not where.is_dir():
        return None
    result = _m_common.git('rev-parse', '--verify', '--quiet', 'refs/heads/' + branch + '^{commit}', cwd=where)
    head = result.stdout.strip() if result.returncode == 0 else ''
    return head if re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', head) else None

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
        view = causal_job_view(row, _m_jobs.job_key(row), facts, by_key, now, outcome)
        reader = getattr(_m_review, 'review_evidence', None)
        receipts = list(reader(inst, n, branch=row['Job'])) if reader else []
        view['advisories'] = review_class_advisories(receipts, view['job_key'])
        view['advisories'] += permission_request_advisories(facts, view['job_key'], now)
        view['advisories'] += acknowledgement_wait_advisories(facts, view['job_key'], now)
        view['advisories'] += unchanged_verification_advisories(facts, view['job_key'], now)
        if not row['Ended']:
            view['advisories'] += infrastructure_displacement_advisories(facts, view['job_key'], now)
            view['head'] = introspection_head(n, row['Job'])
            if str(n['local'].get('preview', '')).strip().casefold() != 'none':
                view['advisories'] += preview_testing_advisories(facts, view['job_key'], now, view['head'])
            view['preview_evidence'] = 'head_bound_receipts' if view['head'] else 'head_unknown'
        view['review_evidence'] = 'receipt_hook' if reader else 'unavailable'
        views.append(view)
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
