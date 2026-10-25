"""Score an observation window from supplied, hash-verified source receipts."""
import datetime
import hashlib
import json
import math
import re
from pathlib import Path

from . import common as _m_common
from . import guard as _m_guard
from . import nodes as _m_nodes


# Counts need full-window coverage before a zero can pass. Durations are seconds.
SCORECARD_TARGETS = {
    'context_reexplanations': ('eq', 0), 'stale_answers': ('eq', 0),
    'project_setup_seconds': ('lt', 120), 'must_use_cases': ('matrix', None),
    'unauthorized_publications': ('eq', 0), 'checkout_edits': ('eq', 0),
    'machine_setup_seconds': ('lt', 900), 'machine_setup_commands': ('le', 3),
    'missing_history': ('eq', 0), 'boundary_leaks': ('eq', 0),
    'repeated_corrections': ('eq', 0), 'repeated_library_explanations': ('eq', 0),
    'repository_boundary_mentions': ('eq', 0),
}


def scorecard_window(value):
    if not isinstance(value, dict):
        raise ValueError('window requires start and end timestamps')
    times = []
    for key in ('start', 'end'):
        stamp = value.get(key)
        if not isinstance(stamp, str):
            raise ValueError('timestamp required')
        at = datetime.datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        if at.tzinfo is None:
            raise ValueError('window timestamps require timezones')
        times.append(at)
    if times[0] >= times[1]:
        raise ValueError('window must have positive duration')
    return times


def scorecard_refs(root, refs):
    if not isinstance(refs, list) or not 1 <= len(refs) <= 50:
        raise ValueError('source evidence required')
    verified = []
    for ref in refs:
        if not isinstance(ref, dict) or not isinstance(ref.get('path'), str):
            raise ValueError('invalid source reference')
        relative = Path(ref['path'])
        if (relative.is_absolute() or '..' in relative.parts or not relative.parts
                or any(ord(c) < 32 for c in str(relative))
                or not re.fullmatch('[0-9a-f]{64}', str(ref.get('sha256', '')))):
            raise ValueError('invalid source reference')
        path = root / relative
        if (any(p.is_symlink() for p in [path, *path.parents]) or not path.is_file()
                or path.stat().st_nlink != 1):
            raise ValueError('unsafe source reference')
        if not path.resolve().is_relative_to(root):
            raise ValueError('source reference outside node')
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(65536), b''):
                digest.update(block)
        if digest.hexdigest() != ref['sha256']:
            raise ValueError('source evidence changed')
        verified.append({'path': str(relative), 'sha256': ref['sha256']})
    return verified


def scorecard_matrix(value, cases, runtimes):
    if (not isinstance(cases, list) or not cases or len(cases) > 10000
            or any(not isinstance(case, str) or not case.strip() for case in cases)
            or not runtimes or len(set(cases)) != len(cases) or not isinstance(value, list)):
        return 'unknown'
    results = {}
    expected = {(case, runtime) for case in cases for runtime in runtimes}
    for row in value:
        if (not isinstance(row, dict) or not isinstance(row.get('case'), str)
                or not isinstance(row.get('runtime'), str) or type(row.get('passed')) is not bool):
            return 'unknown'
        key = (row['case'], row['runtime'])
        if key not in expected or key in results:
            return 'unknown'
        results[key] = row['passed']
    if any(not passed for passed in results.values()):
        return 'fail'
    return 'pass' if set(results) == expected else 'unknown'


def night_scorecard(root, data, runtimes):
    root = Path(root).resolve()
    if (not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] != 1
            or not isinstance(data.get('measurements'), dict)):
        raise ValueError('scorecard requires version 1 measurements')
    start, end = scorecard_window(data.get('window'))
    metrics = {}
    for name, (operator, target) in SCORECARD_TARGETS.items():
        result = {'status': 'unknown', 'target': {'operator': operator, 'value': target}, 'evidence': []}
        rec = data['measurements'].get(name)
        if isinstance(rec, dict):
            try:
                refs = scorecard_refs(root, rec.get('evidence'))
                low, high = scorecard_window(rec.get('coverage'))
                if low > start or high < end:
                    raise ValueError('incomplete observation coverage')
                value = rec.get('value')
                if operator == 'matrix':
                    result['status'] = scorecard_matrix(value, data.get('must_cases'), runtimes)
                elif (type(value) in (int, float) and math.isfinite(value) and value >= 0
                      and (not name.endswith('_seconds') and type(value) is int or name.endswith('_seconds'))):
                    passed = value == target if operator == 'eq' else value < target if operator == 'lt' else value <= target
                    result.update(status='pass' if passed else 'fail', value=value)
                result['evidence'] = refs
            except (OSError, ValueError, TypeError, OverflowError):
                pass
        metrics[name] = result
    statuses = [m['status'] for m in metrics.values()]
    return {'version': 1, 'window': data['window'], 'metrics': metrics,
            'status': 'fail' if 'fail' in statuses else 'unknown' if 'unknown' in statuses else 'pass',
            'scope': 'supplied measurements and hash-verified receipts; observation completeness is asserted by the collector'}


def cmd_scorecard(a):
    inst = _m_nodes.load()
    n = inst.node(a.id)
    try:
        path = Path(a.file).expanduser().resolve()
        if path.stat().st_size > 1024 * 1024:
            raise ValueError('scorecard manifest exceeds 1 MiB')
        data = json.loads(path.read_text())
        result = night_scorecard(n['dir'], data, inst.runtimes)
    except (OSError, ValueError, TypeError) as error:
        _m_common.die(str(error))
    print(_m_guard.redact(json.dumps(result, sort_keys=True), _m_guard.never_terms(n)))
