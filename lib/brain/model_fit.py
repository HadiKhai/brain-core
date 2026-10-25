"""Learn model recommendations from comparable, attributed ledger observations."""
import json
import math
import statistics

from . import common as _m_common
from . import guard as _m_guard
from . import jobs as _m_jobs
from . import nodes as _m_nodes
from . import spend as _m_spend


def model_fit_candidates(rows, kind, runtime, effort, allowed, min_samples=3):
    """Rank observed success/review effort before measured token and active-time cost.

    These are job-outcome proxies, not a claim of causal model quality. Only
    single-model attributed, reviewed merges and explicit failed outcomes count.
    Unknown, open, parked, switched or incomparable jobs do not become zero-cost
    successes. Conflicting duplicate job identities are excluded entirely.
    """
    unique, conflicts = {}, set()
    for row in rows:
        key = _m_jobs.job_key(row)
        if key in unique and unique[key] != row:
            conflicts.add(key)
        unique[key] = row
    groups = {}
    for key, row in unique.items():
        if (key in conflicts or not row.get('Ended') or row.get('Switches') or
                (row.get('Class'), row.get('Runtime'), row.get('Effort')) != (kind, runtime, effort) or
                row.get('Outcome') not in ('merged', 'failed')):
            continue
        model = row.get('Model')
        if model not in allowed or not isinstance(model, str) or not _m_spend.SPEND_MODEL.fullmatch(model):
            continue
        if row.get('Models') not in (model + ' 100%', runtime + ':' + model + ' 100%'):
            continue
        if not str(row.get('Usage', '')).startswith(('host', 'transcripts:')):
            continue
        if any(not str(row.get(k, '')).isdigit() for k in ('Fresh in', 'Cache read', 'Cache write', 'Output', 'Reviews')):
            continue
        usage = _m_spend.row_usage(row)
        reviews = int(row['Reviews'])
        try:
            active = float(row['Active h'])
        except (KeyError, ValueError, TypeError):
            continue
        if (not usage or usage['processed'] <= 0 or not math.isfinite(active) or active < 0 or
                (row['Outcome'] == 'merged' and reviews < 1)):
            continue
        groups.setdefault(model, []).append((row['Outcome'], reviews, usage['processed'], active))
    out = []
    for model, observations in groups.items():
        merged = [o for o in observations if o[0] == 'merged']
        if len(merged) < min_samples:
            continue
        out.append({'model': model, 'samples': len(observations), 'merged': len(merged),
                    'failed': len(observations) - len(merged), 'success_rate': len(merged) / len(observations),
                    'median_reviews': statistics.median(o[1] for o in merged),
                    'median_tokens': statistics.median(o[2] for o in merged),
                    'median_active_hours': statistics.median(o[3] for o in merged)})
    return sorted(out, key=lambda f: (-f['success_rate'], f['median_reviews'], f['median_tokens'],
                                     f['median_active_hours'], f['model']))


def model_fit_report(inst, n, kind, runtime, effort):
    if runtime not in inst.runtimes or kind not in _m_jobs.JOB_CLASSES or effort not in _m_jobs.JOB_EFFORTS:
        _m_common.die('model fit needs a declared runtime, job class and effort')
    key = 'model_fit_models_' + runtime
    models = _m_common.lst(n['fm'].get(key, inst.cfg.get(key, '')))
    allowed = {model for model in models if _m_spend.SPEND_MODEL.fullmatch(model)
               and not _m_guard.find_terms(model, _m_guard.never_terms(n))}
    try:
        minimum = int(n['fm'].get('model_fit_min_samples', inst.cfg.get('model_fit_min_samples', 3)))
        if minimum < 1 or minimum > 1000:
            raise ValueError
    except (ValueError, TypeError):
        _m_common.die('model_fit_min_samples must be an integer from 1 to 1000')
    candidates = model_fit_candidates(_m_jobs.read_jobs(n) or [], kind, runtime, effort, allowed, minimum)
    return {'class': kind, 'runtime': runtime, 'effort': effort, 'min_samples': minimum,
            'candidates': candidates, 'recommendation': candidates[0]['model'] if candidates else None,
            'evidence': 'Comparable attributed ledger outcomes; success and review counts are quality proxies.'}


def learned_model_selection(inst, n, kind, runtime, effort):
    enabled = n['fm'].get('model_fit', inst.cfg.get('model_fit', 'off'))
    if enabled not in ('on', 'off'):
        _m_common.die('model_fit must be on or off')
    if enabled != 'on':
        return None
    report = model_fit_report(inst, n, kind, runtime, effort)
    if not report['recommendation']:
        return None
    winner = report['candidates'][0]
    return winner['model'], (f"model fit ({kind}/{runtime}/{effort}; "
                             f"{winner['merged']} merged of {winner['samples']} attributed observations)")


def cmd_model_fit(a):
    inst = _m_nodes.load()
    n = inst.node(a.id)
    print(json.dumps(model_fit_report(inst, n, a.job_class, a.runtime, a.effort), sort_keys=True))
