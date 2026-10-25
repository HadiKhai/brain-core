"""Versioned worker attempts, separate from terminal handbacks and ledger judgement."""
import contextvars
import json
import os
import re

from . import common as _m_common
from . import instance as _m_instance
from . import jobs as _m_jobs
from . import machine as _m_machine
from . import verify as _m_verify


ATTEMPT_ACTIVE = ('queued', 'verifying')
ATTEMPT_STATES = ATTEMPT_ACTIVE + ('terminal', 'interrupted', 'invalidated')
ATTEMPT_CONTEXT = contextvars.ContextVar('brain_handback_attempt', default=None)


def attempt_phase(state):
    context = ATTEMPT_CONTEXT.get()
    if context:
        inst, n, row, identity = context
        try:
            return update_attempt(inst, n, row, identity, state)
        finally:
            # Native record_ready deliberately unlocked its outer CLI scope before
            # admission. A short phase write must not re-hold it during verify.
            _m_instance.unlock()


def attempt_path(n, row):
    return n['dir'] / 'job-attempts' / (_m_jobs.job_key(row) + '.json')


def attempt_binding(n, row):
    """Exact local refs and native input fingerprint; this never grants readiness."""
    home = n.get('home_path')
    branch = row['Job']
    w = n['local'].get('worktrees', {}).get(branch)
    head = (_m_common.git('rev-parse', '--verify', '--quiet', 'refs/heads/' + branch, cwd=home).stdout.strip()
            if home else '')
    base = _m_verify.ready_refs(n, branch)[1] if home else ''
    return {'head': head, 'base': base, 'inputs': _m_verify.verify_inputs(n, w) if w else '',
            'integration_base': _m_verify.integration_parent(n, base) if home else '',
            'verify_command': n['fm'].get('verify', ''),
            'input_selection': _m_jobs.digest(_m_common.lst(n['fm'].get('verify_inputs'))
                                              or _m_common.lst(_m_verify.VERIFY_INPUTS)),
            'port_reservation': _m_jobs.digest([w.get('port'), w.get('ports', 1)]) if w else ''}


def current_attempt(n, row, strict=True):
    try:
        path = attempt_path(n, row)
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError('linked job attempt evidence')
        data = json.loads(path.read_text())
        if sum(_m_jobs.job_key(r) == _m_jobs.job_key(row) for r in _m_jobs.read_jobs(n) or []) != 1:
            raise ValueError('ambiguous job-attempt ownership; coordinator must reconcile the job key')
        if (not isinstance(data, dict) or type(data.get('version')) is not int or data.get('version') != 1 or data.get('job_key') != _m_jobs.job_key(row)
                or data.get('state') not in ATTEMPT_STATES
                or not isinstance(data.get('id'), str) or not re.fullmatch('[0-9a-f]{32}', data['id'])
                or not isinstance(data.get('binding'), dict)
                or any(not isinstance(data['binding'].get(k), str) for k in ('head', 'base', 'inputs'))):
            raise ValueError('invalid job attempt')
        return data
    except FileNotFoundError:
        return None
    except (ValueError, OSError):
        if strict:
            raise
        return {'state': 'unknown', 'id': None}


def active_attempt(n, row, strict=True):
    data = current_attempt(n, row, strict)
    return data if data and data['state'] in ATTEMPT_ACTIVE + ('unknown',) else None


def write_attempt(n, row, data):
    from . import events
    with events.node_write(n["dir"], "job_attempt"):
        path = attempt_path(n, row)
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError('linked job attempt evidence')
        _m_common.safe_target(path)
        path.parent.mkdir(exist_ok=True)
        _m_common.atomic_write(path, json.dumps(data, sort_keys=True) + '\n')
        _m_common.touch(path)


def begin_attempt(inst, n, row, request_id=None):
    with _m_instance.brain_lock(inst):
        rows = _m_jobs.read_jobs(n) or []
        current = _m_jobs.open_job(rows, row['Job'])
        if not current or _m_jobs.job_key(current) != _m_jobs.job_key(row):
            raise ValueError('job changed before handback attempt')
        if sum(_m_jobs.job_key(r) == _m_jobs.job_key(row) for r in rows) != 1:
            raise ValueError('ambiguous job key; coordinator must reconcile it before handback')
        data = {'version': 1, 'id': os.urandom(16).hex(), 'job_key': _m_jobs.job_key(row),
                'state': 'queued', 'at': inst.now_dt().isoformat(), 'binding': attempt_binding(n, row),
                'owner': [os.getpid(), _m_machine.process_stamp(os.getpid())], 'request_id': request_id}
        write_attempt(n, row, data)
        return data


def update_attempt(inst, n, row, identity, state, **fields):
    if state not in ATTEMPT_STATES:
        raise ValueError('invalid attempt state')
    with _m_instance.brain_lock(inst):
        data = current_attempt(n, row)
        if not data or data['id'] != identity or data['state'] not in ATTEMPT_ACTIVE:
            return False
        data.update(fields, state=state, at=inst.now_dt().isoformat())
        write_attempt(n, row, data)
        return True


def finish_attempt(inst, n, row, identity, state='terminal'):
    if state not in ('terminal', 'interrupted', 'invalidated'):
        raise ValueError('attempt retirement needs a terminal state')
    return update_attempt(inst, n, row, identity, state)


def attempt_matches(n, row, attempt):
    rows = _m_jobs.read_jobs(n) or []
    current = _m_jobs.open_job(rows, row['Job'])
    return (current is not None and _m_jobs.job_key(current) == _m_jobs.job_key(row)
            and sum(_m_jobs.job_key(r) == _m_jobs.job_key(row) for r in rows) == 1
            and attempt.get('binding') == attempt_binding(n, row))


def job_projection(inst, n, row, strict=False):
    """One recorded-evidence view for jobs, due and dispatcher; no host calls."""
    hb = _m_jobs.read_handback(n, row, strict=strict)
    attempt = current_attempt(n, row, strict=strict)
    # Even an interrupted replacement prevents resurrecting the previous completion.
    effective = _m_jobs.job_signal(inst, n, row, strict=strict)
    if attempt and (attempt['state'] != 'terminal' or not hb or hb.get('attempt_id') != attempt['id']):
        effective = None
    state = ('closed' if row.get('Ended') else effective.get('state') if effective else
             attempt['state'] if attempt else 'open')
    queue = None
    if attempt and attempt.get('request_id'):
        from . import handback_queue as _m_queue
        queue = _m_queue.queue_projection(n, attempt['request_id'], strict=strict)
    admission = _m_machine.job_queue_projection(inst, n['id'], attempt.get('owner')) if attempt else None
    episode = None
    if effective:
        episode = effective.get('wake_id') or _m_jobs.digest([_m_jobs.job_key(row), {k: effective.get(k) for k in
                    ('state', 'summary', 'question', 'report', 'lessons', 'restart')}])
    action = {'queued': 'run the explicit foreground queue runner' if queue else 'wait for native admission',
              'verifying': 'wait for the owned verifier', 'interrupted': 'inspect retained evidence; hand back again',
              'invalidated': 'refresh inputs and hand back again', 'unknown': 'repair unreadable attempt evidence',
              'done': 'coordinator checks current readiness', 'blocked': 'coordinator resolves the block',
              'question': 'coordinator answers the question', 'closed': 'attest closed job threads safely settled'}
    return {'job_key': _m_jobs.job_key(row), 'branch': row['Job'], 'ledger': row,
            'state': state, 'summary': (effective.get('summary') or 'summary missing (legacy)') if effective else '',
            'question': (effective or {}).get('question'), 'handback': hb, 'signal': effective,
            'historical_summary': (hb.get('summary') or 'summary missing (legacy)') if hb and not effective else None,
            'missing_fields': ([key for key in ('summary', 'question')
                               if (key == 'summary' or hb.get('state') == 'question') and not hb.get(key)] if hb else []),
            'episode_id': episode, 'attempt': attempt, 'queue': queue, 'admission': admission, 'host_activity': 'unknown',
            'next_action': action.get(state, 'inspect recorded job evidence; host activity unknown')}
