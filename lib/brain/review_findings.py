"""Durable finding closure tied to the tracked code inputs that were reviewed."""
import hashlib
import json
from pathlib import PurePosixPath

from . import common as _m_common
from . import guard as _m_guard
from . import instance as _m_instance
from . import jobs as _m_jobs


def finding_state_path(inst, n, row):
    return inst.root / '.brain/review-findings' / hashlib.sha256(n['id'].encode()).hexdigest() / (_m_jobs.job_key(row) + '.json')


def finding_inputs(repo, head, paths):
    if not isinstance(paths, list) or not paths or len(set(paths)) != len(paths):
        raise ValueError('finding paths must name distinct tracked inputs')
    out = {}
    for path in paths:
        if (not isinstance(path, str) or not path or PurePosixPath(path).is_absolute() or
                any(p in ('..', '.git') for p in PurePosixPath(path).parts) or
                str(PurePosixPath(path)) != path or any(c in path for c in '*?[]\\\x00\n')):
            raise ValueError('finding input must be a canonical repository-relative path')
        result = _m_common.git('rev-parse', '--verify', head + ':' + path, cwd=repo)
        out[path] = result.stdout.strip() if result.returncode == 0 else None
    return out


def read_finding_state(inst, n, row):
    path = finding_state_path(inst, n, row)
    if not path.exists():
        return {'version': 1, 'node': n['id'], 'job_key': _m_jobs.job_key(row), 'findings': {}}
    data = json.loads(path.read_text())
    if (not isinstance(data, dict) or data.get('version') != 1 or data.get('node') != n['id'] or
            data.get('job_key') != _m_jobs.job_key(row) or not isinstance(data.get('findings'), dict)):
        raise ValueError('invalid durable review finding state')
    return data


def review_finding_state(inst, n, branch, head):
    row = _m_jobs.open_job(_m_jobs.read_jobs(n) or [], branch)
    if not row:
        raise ValueError('finding state requires an open job')
    repo = n.get('home_path') if n.get('external') else inst.root
    out = []
    for record in read_finding_state(inst, n, row)['findings'].values():
        current = dict(record)
        if record['state'] == 'closed' and finding_inputs(repo, head, record['paths']) != record['inputs']:
            current['state'] = 'reopened'
        out.append(current)
    return out


def record_review_findings(inst, n, branch, head, findings):
    with _m_instance.brain_lock(inst):
        row = _m_jobs.open_job(_m_jobs.read_jobs(n) or [], branch)
        if not row:
            raise ValueError('finding state requires an open job')
        repo = n.get('home_path') if n.get('external') else inst.root
        actual = _m_common.git('rev-parse', '--verify', 'refs/heads/' + branch + '^{commit}', cwd=repo).stdout.strip()
        if head != actual:
            raise ValueError('findings must bind the current branch head')
        data = read_finding_state(inst, n, row)
        if not isinstance(findings, list):
            raise ValueError('findings must be a list')
        seen = set()
        for finding in findings:
            if (not isinstance(finding, dict) or finding.get('severity') not in ('blocker', 'major', 'minor') or
                    any(not isinstance(finding.get(k), str) or not finding[k].strip() for k in ('id', 'class', 'text')) or
                    finding.get('state') not in ('open', 'closed') or finding['id'] in seen):
                raise ValueError('invalid finding identity, class, severity or state')
            seen.add(finding['id'])
            inputs = finding_inputs(repo, head, finding.get('paths'))
            prior = data['findings'].get(finding['id'])
            if prior and sorted(prior['paths']) != sorted(finding['paths']):
                raise ValueError('existing finding input scope cannot be rebound; use a new finding id')
            if not prior and not any(inputs.values()):
                raise ValueError('new finding must bind at least one tracked input')
            if finding['state'] == 'closed' and (not isinstance(finding.get('evidence'), str) or not finding['evidence'].strip()):
                raise ValueError('closing a finding needs evidence')
            if prior and prior['state'] == 'closed' and prior['inputs'] == inputs:
                continue  # A new reviewer cannot reopen unchanged, already closed input.
            data['findings'][finding['id']] = {**finding, 'inputs': inputs, 'head': head, 'recorded_at': inst.now()}
        serialized = json.dumps(data, sort_keys=True) + '\n'
        _m_guard.no_terms_in(n, serialized, 'durable review findings')
        path = finding_state_path(inst, n, row)
        _m_common.safe_target(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _m_common.atomic_write(path, serialized)
    return review_finding_state(inst, n, branch, head)
