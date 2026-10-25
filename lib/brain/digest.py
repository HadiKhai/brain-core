"""Read-only commitment reports and daily summaries of recorded job measurements."""
import datetime
import hashlib
import json
import math
import re

from . import common as _m_common
from . import guard as _m_guard
from . import jobs as _m_jobs
from . import nodes as _m_nodes


SHA = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')


def verified_completions(n, tasks, checker):
    proven = set()
    for row in tasks:
        if row.get('status') not in ('done', 'merged'):
            continue
        try:
            checker(n, row)
        except (ValueError, OSError, TypeError, KeyError):
            continue
        proven.add(row['id'])
    return proven


def commitment_digest(tasks, requirements, completed, runnable=None):
    """Project completion already checked by the task domain's scoped verifier."""
    tasks = sorted(tasks, key=lambda r: (r.get('order', 0), r['id']))
    done = completed
    report = {key: [] for key in ('promised', 'done', 'not_done', 'needs_you', 'next')}
    for row in tasks:
        item = dict(row)
        req = requirements.get(row.get('requirement'), {})
        item['source'] = (' '.join(str(req.get(k, '')) for k in ('at', 'ref')).strip()
                          or 'promise source unavailable')
        if req.get('date_basis') in ('file-observed', 'job-start'):
            item['source'] = req['date_basis'] + ' context; ' + item['source']
        report['promised'].append(item)
        if row['id'] in done:
            item['completion_basis'] = ('external provider completion; local implementation unproven'
                if row.get('source') == 'linear' else row.get('completion_kind', 'merge'))
            report['done'].append(item)
            continue
        status = row.get('status', 'unknown')
        reason = row.get('park_reason') or row.get('drop_reason') or status
        if status in ('done', 'merged'):
            reason = 'completion lacks valid scoped evidence'
        item['reason'] = reason
        item['estimate'] = row.get('estimate') or 'estimate unavailable'
        report['not_done'].append(item)
        if row.get('needs_human'):
            report['needs_you'].append(item)
        ready = (row['id'] in runnable if runnable is not None else
                 all(dep in done for dep in row.get('depends_on', [])))
        if status == 'ready' and ready:
            report['next'].append(item)
    return report


def digest_text(title, report):
    total = len(report['promised'])
    state = 'blocked' if any(r.get('status') in ('blocked', 'parked') for r in report['not_done']) else 'on track'
    lines = [title + ' — ' + state, '', 'Promised:']
    lines += [f"- {r['id']} {r['title']} — {r['source']}" for r in report['promised']] or ['- none recorded']
    lines += ['', f"Done ✓: {len(report['done'])}/{total}"]
    for row in report['done']:
        basis = row['completion_basis']
        proof = (row.get('merge_sha', '') + ' ' + row.get('code_path', '') if basis == 'merge' else
                 str(row.get('completion_receipt', {}).get('ref', '')))
        lines.append(f"- {row['id']} {row['title']} — {basis}; {proof}; " + '; '.join(map(str, row.get('evidence', []))))
    lines += ['', 'Not done ✗:']
    lines += [f"- {r['id']} {r['title']} — {r['reason']}; {r['estimate']}" +
              ('; resumes when ' + r['resume'] if r.get('resume') else '') for r in report['not_done']] or ['- none']
    lines += ['', 'Needs you:']
    lines += [f"{i}. {r['id']} — {r['reason']}" + ('; resume: ' + r['resume'] if r.get('resume') else '')
              for i, r in enumerate(report['needs_you'], 1)] or ['- none recorded']
    lines += ['', 'Next:']
    lines += [f"- {r['id']} {r['title']} — {r['estimate']}" for r in report['next']] or ['- no runnable ready commitment']
    return '\n'.join(lines)


def _metric(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (ValueError, TypeError):
        return None


def efficiency_retro(rows):
    """Summarize supplied measurements; missing readings never become zero."""
    result = {'jobs': len(rows), 'failed': sum(r.get('Outcome') == 'failed' for r in rows),
              'findings': [], 'unknown_metrics': []}
    for key, field in (('elapsed_hours', 'Hours'), ('review_rounds', 'Reviews')):
        numbers = [_metric(r.get(field)) for r in rows]
        result[key] = round(sum(numbers), 3) if all(n is not None for n in numbers) else None
        if result[key] is None:
            result['unknown_metrics'].append(key)
    for row in sorted(rows, key=lambda r: (r.get('Job', ''), r.get('Started', ''))):
        reasons = []
        if (_metric(row.get('Reviews')) or 0) >= 3:
            reasons.append('three or more review rounds')
        if (_metric(row.get('Compactions')) or 0) >= 3:
            reasons.append('three or more compactions')
        if row.get('Outcome') in ('failed', 'blocked'):
            reasons.append('recorded outcome: ' + row['Outcome'])
        if reasons:
            result['findings'].append({'job': row['Job'], 'started': row.get('Started', ''),
                                       'reasons': reasons})
    return result


def retro_for_day(inst, n, day):
    rows = _m_jobs.read_jobs(n)
    if rows is None:
        raise ValueError('unreadable job ledger')
    selected = []
    for row in rows:
        ended = inst.parse_time(row.get('Ended', ''))
        if ended and ended.astimezone(inst.now_dt().tzinfo).date() == day:
            selected.append(row)
    return {'version': 1, 'day': day.isoformat(), 'observed_at': inst.now(),
            **efficiency_retro(selected)}


def daily_retro(inst):
    """Opt-in sampler output for the previous local calendar day, once per machine.

    Generated analysis remains local. It sends no host/operator messages and
    changes no decisions, lessons or job records.
    """
    if inst.node('root')['fm'].get('daily_retro') != 'on':
        return
    day = inst.now_dt().date() - datetime.timedelta(days=1)
    for n in inst.nodes():
        folder = inst.root / '.brain/daily-retro' / hashlib.sha256(n['id'].encode()).hexdigest()
        path = folder / (day.isoformat() + '.json')
        if path.exists():
            continue
        value = retro_for_day(inst, n, day)
        _m_common.safe_target(path)
        folder.mkdir(parents=True, exist_ok=True)
        _m_common.atomic_write(path, json.dumps(value, indent=2) + '\n')


def cmd_digest(a):
    inst = _m_nodes.load()
    n = inst.node(a.id)
    try:
        if a.retro:
            day = datetime.date.fromisoformat(a.day) if a.day else inst.now_dt().date() - datetime.timedelta(days=1)
            report = retro_for_day(inst, n, day)
            text = json.dumps(report, indent=2)
        else:
            from . import tasks as _m_tasks
            tasks = _m_tasks.read_records(n)
            requirements = _m_tasks.read_records(n, 'requirements')
            from . import task_flow as _m_task_flow
            completed = verified_completions(n, tasks.values(), _m_task_flow.completion)
            runnable = set()
            for row in tasks.values():
                if row.get('status') == 'ready':
                    try:
                        if not _m_task_flow.holds(inst, n, row) and not _m_task_flow.blockers(n, row, tasks):
                            runnable.add(row['id'])
                    except (ValueError, OSError, TypeError, KeyError):
                        pass  # unresolved scope/dependencies stay unfinished
            report = commitment_digest(list(tasks.values()), requirements, completed, runnable)
            text = json.dumps(report, indent=2) if a.json else digest_text(n['fm'].get('title', n['id']), report)
        print(_m_guard.redact(text, _m_guard.never_terms(n)))
    except ImportError:
        _m_common.die('commitment digest requires the task-store capability')
    except (OSError, ValueError, TypeError, KeyError) as error:
        _m_common.die('digest unavailable: ' + type(error).__name__)
