"""Source-first diagnosis permits; this module never executes a probe."""
import copy
import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath

from . import common as _m_common
from . import guard as _m_guard
from . import instance as _m_instance
from . import jobs as _m_jobs
from . import nodes as _m_nodes
from . import worktrees as _m_worktrees


def diagnostic_validate(state):
    if (not isinstance(state, dict) or type(state.get('version')) is not int or state['version'] != 1
            or not re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', str(state.get('head', '')))
            or not isinstance(state.get('question'), str) or not state['question'].strip()
            or not isinstance(state.get('attempts'), list) or len(state['attempts']) > 1000
            or not isinstance(state.get('sources'), list) or not state['sources']
            or state.get('mode') not in ('source', 'ambiguous', 'runtime')
            or not isinstance(state.get('reason'), str) or not state['reason'].strip()):
        raise ValueError('invalid diagnostic state; preserve it for inspection')
    for source in state['sources']:
        if (not isinstance(source, dict) or not isinstance(source.get('path'), str)
                or PurePosixPath(source['path']).is_absolute() or '..' in PurePosixPath(source['path']).parts
                or type(source.get('line')) is not int or source['line'] < 1
                or not re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', str(source.get('blob', '')))):
            raise ValueError('invalid canonical diagnostic source')
    pending = []
    identities = set()
    for attempt in state['attempts']:
        if (not isinstance(attempt, dict) or not re.fullmatch('[0-9a-f]{24}', str(attempt.get('id', '')))
                or attempt['id'] in identities or attempt.get('state') not in ('pending', 'setup-failed', 'observed')):
            raise ValueError('invalid diagnostic attempts')
        identities.add(attempt['id'])
        if attempt['state'] == 'pending':
            pending.append(attempt['id'])
    if len(pending) > 1 or state.get('pending') != (pending[0] if pending else None):
        raise ValueError('invalid pending diagnostic attempt')
    return state


def diagnostic_plan(previous, head, question, sources, finding, reason):
    if (not isinstance(question, str) or not question.strip() or len(question) > 4000
            or not isinstance(reason, str) or not reason.strip() or len(reason) > 4000
            or finding not in ('source', 'ambiguous', 'runtime') or not sources):
        raise ValueError('canonical sources and a source finding/reason are required before a probe')
    state = copy.deepcopy(diagnostic_validate(previous)) if previous is not None else {
        'version': 1, 'head': head, 'question': question.strip(), 'sources': sources,
        'mode': finding, 'reason': reason.strip(), 'attempts': [], 'pending': None}
    if state['head'] != head or state['question'] != question.strip():
        raise ValueError('diagnostic identity changed')
    if finding == 'source' and state['pending']:
        raise ValueError('finish the current pending probe before changing to a source answer')
    if finding != 'source':
        if sum(a['state'] == 'setup-failed' for a in state['attempts']) >= 2:
            raise ValueError('two probe setups failed; rule from recorded source evidence or narrow the question')
        if state['pending'] is None:
            if len(state['attempts']) >= 1000:
                raise ValueError('diagnostic attempt bound reached')
            state['pending'] = os.urandom(12).hex()
            state['attempts'].append({'id': state['pending'], 'state': 'pending'})
    state.update(sources=sources, mode=finding, reason=reason.strip(), probe_allowed=finding != 'source')
    return diagnostic_validate(state)


def diagnostic_outcome(previous, identity, outcome):
    state = copy.deepcopy(diagnostic_validate(previous))
    if not identity or state['pending'] != identity:
        raise ValueError('outcome must identify the current pending attempt')
    if outcome not in ('setup-failed', 'observed'):
        raise ValueError('invalid probe outcome')
    next(a for a in state['attempts'] if a['id'] == identity)['state'] = outcome
    state.update(pending=None, probe_allowed=False)
    return state


def diagnostic_sources(where, head, refs):
    if not refs or len(refs) > 50:
        raise ValueError('read canonical source before planning a probe (--source path:line)')
    sources = []
    for ref in refs:
        name, sep, number = ref.rpartition(':')
        path = PurePosixPath(name)
        if (not sep or not number.isdigit() or int(number) < 1 or not name or path.is_absolute()
                or '..' in path.parts or '\\' in name or any(ord(c) < 32 for c in name)):
            raise ValueError('source needs a repo-relative path:line')
        tree = _m_common.git('ls-tree', '-z', head, '--', name, cwd=where)
        entries = tree.stdout.rstrip('\0').split('\0')
        fields = entries[0].split('\t', 1) if len(entries) == 1 else []
        if (tree.returncode or len(fields) != 2 or fields[1] != name
                or not re.fullmatch(r'100(?:644|755) blob [0-9a-f]{40,64}', fields[0])):
            raise ValueError('source must name a tracked regular file at the job HEAD')
        blob = fields[0].split()[2]
        content = _m_common.git('cat-file', 'blob', blob, cwd=where)
        if content.returncode or len(content.stdout.encode()) > 1024 * 1024:
            raise ValueError('canonical source is unavailable or exceeds 1 MiB')
        if int(number) > len(content.stdout.splitlines()):
            raise ValueError('source line is outside the canonical file')
        sources.append({'path': name, 'line': int(number), 'blob': blob})
    return sources


def cmd_diagnose(a):
    inst = _m_nodes.load()
    n = inst.node(a.id)
    where, _ = _m_worktrees.job_where(n, a.branch)
    row = _m_jobs.open_job(_m_jobs.read_jobs(n) or [], a.branch)
    if not row:
        _m_common.die('diagnosis requires an open job')
    actual = _m_common.git('symbolic-ref', '--short', 'HEAD', cwd=where).stdout.strip()
    if actual != a.branch:
        _m_common.die('diagnostic worktree branch changed')
    head = _m_common.git('rev-parse', '--verify', 'HEAD', cwd=where).stdout.strip()
    question = a.question.strip()
    _m_guard.no_terms_in(n, question + (a.reason or ''), 'diagnostic evidence')
    identity = hashlib.sha256(json.dumps([n['id'], _m_jobs.job_key(row), head, question]).encode()).hexdigest()
    path = inst.root / '.brain/diagnostics' / (identity + '.json')
    try:
        with _m_instance.brain_lock(inst):
            if any(p.is_symlink() for p in [path, *path.parents]) or (path.exists() and path.stat().st_nlink != 1):
                raise ValueError('linked diagnostic state')
            _m_common.safe_target(path)
            if path.exists() and path.stat().st_size > 1024 * 1024:
                raise ValueError('diagnostic state exceeds 1 MiB')
            previous = json.loads(path.read_text()) if path.exists() else None
            if a.action == 'plan':
                sources = diagnostic_sources(where, head, a.source)
                _m_guard.no_terms_in(n, json.dumps(sources), 'diagnostic sources')
                state = diagnostic_plan(previous, head, question, sources, a.finding, a.reason)
            else:
                state = diagnostic_outcome(previous, a.attempt, a.state)
            state['updated_at'] = inst.now_dt().isoformat()
            # A permit binds the actual checkout head; never publish one after a concurrent move.
            if _m_common.git('rev-parse', '--verify', 'HEAD', cwd=where).stdout.strip() != head:
                raise ValueError('diagnostic HEAD changed during source inspection')
            path.parent.mkdir(parents=True, exist_ok=True)
            _m_common.atomic_write(path, json.dumps(state, sort_keys=True) + '\n', private=True)
    except (OSError, ValueError, TypeError, UnicodeError) as error:
        _m_common.die(str(error))
    print(json.dumps(state, sort_keys=True))
