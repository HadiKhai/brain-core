"""Carry an accepted review across a pure rebase: same patch and same rules, both computed by git."""
import hashlib
import json
import os
import re
import subprocess

from . import common as _m_common

_m_guard = _m_common.LazyModule(__package__, 'guard')
_m_jobs = _m_common.LazyModule(__package__, 'jobs')
_m_review = _m_common.LazyModule(__package__, 'review')
_m_verify = _m_common.LazyModule(__package__, 'verify')

REFUSED = 'review carry-over refused: '
HEAD = re.compile(r'[0-9a-f]{40,64}')
CARRY_KEYS = ('source_head', 'source_receipt', 'source_sha256', 'source_base', 'source_patch_id',
              'base', 'patch_id', 'rules_digest')
# Pin every option that shapes the patch text, so user configuration cannot change an id.
DIFF = ('diff', '--binary', '--full-index', '--no-color', '--no-ext-diff', '--no-textconv', '--no-renames',
        '--no-relative', '--diff-algorithm=myers', '--indent-heuristic', '--src-prefix=a/', '--dst-prefix=b/', '-U3')


class NoSource(ValueError):
    """The job has no earlier original review receipt to carry."""


def _git(repo, *args, data=None):
    env = {**os.environ, 'GIT_OPTIONAL_LOCKS': '0'}
    return subprocess.run(['git', '-C', str(repo), *args], input=data, capture_output=True, env=env, check=False)


def _commit(repo, rev):
    result = _git(repo, 'rev-parse', '--verify', '--quiet', rev + '^{commit}')
    value = result.stdout.decode().strip()
    if result.returncode or not HEAD.fullmatch(value):
        raise ValueError(REFUSED + 'commit ' + rev + ' is unavailable')
    return value


def _ancestor(repo, older, newer):
    return _git(repo, 'merge-base', '--is-ancestor', older, newer).returncode == 0


def merge_base(repo, head, main):
    result = _git(repo, 'merge-base', head, main)
    value = result.stdout.decode().strip()
    if result.returncode or not HEAD.fullmatch(value):
        raise ValueError(REFUSED + 'no merge base between ' + head[:12] + ' and main')
    return value


def patch_id(repo, base, head):
    """git patch-id of base..head: --stable as specified, and --verbatim so whitespace bytes count too."""
    diff = _git(repo, *DIFF, base, head)
    if diff.returncode:
        raise ValueError(REFUSED + 'cannot diff ' + base[:12] + '..' + head[:12])
    if not diff.stdout:
        raise ValueError(REFUSED + 'empty patch ' + base[:12] + '..' + head[:12])
    ids = {}
    for mode in ('stable', 'verbatim'):
        result = _git(repo, 'patch-id', '--' + mode, data=diff.stdout)
        fields = result.stdout.decode().split()
        if result.returncode or len(fields) != 2 or not re.fullmatch(r'[0-9a-f]{40,64}', fields[0]):
            raise ValueError(REFUSED + 'git patch-id --' + mode + ' failed for ' + base[:12] + '..' + head[:12])
        ids[mode] = fields[0]
    return ids


def main_commit(n):
    """The main a rebased job must contain (local main for local merges, as ready_preflight checks)."""
    repo = n['home_path']
    ref = _m_verify.integration_ref(repo)
    if not ref:
        raise ValueError(REFUSED + 'integration main is unavailable')
    main = _commit(repo, ref)
    local = _m_verify.integration_parent(n, main)
    if not local:
        raise ValueError(REFUSED + 'local integration main is unavailable')
    return local


def _current_digest(n, row, rules):
    from . import rule_contracts
    recorded = row.get('Review rules digest')
    if not recorded:
        raise ValueError(REFUSED + 'the job has no recorded Review rules digest')
    current = rule_contracts.rules_digest(rule_contracts.applicable_rules(n) if rules is None else rules)
    if recorded != current:
        raise ValueError(REFUSED + 'review rules changed since the review brief (recorded ' + recorded[:12] +
                         ', current ' + current[:12] + ')')
    return current


def _relative(inst, path):
    return str(path.relative_to(inst.root))


def carry_candidate(inst, n, row, head, rules=None):
    """Return the receipt carried to head, or raise ValueError naming each refusal; writes nothing."""
    if not n.get('external') or not n.get('home_path'):
        raise ValueError(REFUSED + 'carry-over needs an external git home')
    folder = _m_review.review_receipt_path(n, row, head).parent
    sources = []
    for path in sorted(folder.glob('*.json')) if folder.is_dir() else []:
        if not HEAD.fullmatch(path.stem) or path.stem == head:
            continue  # reviewer history files and the head itself are never sources
        sources.append(path)
    refusals, matches = [], []
    repo = n['home_path']
    for path in sources:
        label = 'receipt at ' + path.stem[:12]
        try:
            if path.is_symlink():
                raise ValueError('linked receipt')
            raw = path.read_bytes()
            source = json.loads(raw)
        except (OSError, ValueError) as error:
            refusals.append(label + ': unreadable (' + str(error) + ')')
            continue
        if isinstance(source, dict) and 'carry' in source:
            continue  # a carried receipt is never a source; its original stays in this folder
        matches.append((path, raw, source))
    if not matches and not refusals:
        raise NoSource(REFUSED + 'no earlier review receipt for this job')
    current = _current_digest(n, row, rules)
    main = main_commit(n)
    base = merge_base(repo, head, main)
    patch = patch_id(repo, base, head)
    accepted = []
    for path, raw, source in matches:
        label = 'receipt at ' + path.stem[:12]
        try:
            if not isinstance(source, dict) or source.get('rules_digest') != current:
                raise ValueError('rules digest ' + str((source or {}).get('rules_digest'))[:12] +
                                 ' differs from current ' + current[:12])
            _m_review.validate_review_receipt(inst, n, row, path.stem, source, rules=rules)
            source_base = merge_base(repo, path.stem, main)
            source_patch = patch_id(repo, source_base, path.stem)
        except ValueError as error:
            refusals.append(label + ': ' + str(error).replace(REFUSED, ''))
            continue
        if source_patch != patch:
            refusals.append(label + ': patch differs (reviewed stable ' + source_patch['stable'][:12] +
                            ' verbatim ' + source_patch['verbatim'][:12] + ', head stable ' +
                            patch['stable'][:12] + ' verbatim ' + patch['verbatim'][:12] + ')')
            continue
        accepted.append((inst.parse_time(source['observed_at']), path, raw, source, source_base, source_patch))
    if not accepted:
        raise ValueError(REFUSED + '; '.join(refusals))
    _, path, raw, source, source_base, source_patch = max(accepted, key=lambda item: item[0])
    if source['verdict'] != 'pass':
        raise ValueError(REFUSED + 'the latest review of this patch (receipt at ' + path.stem[:12] + ') is blocked')
    carry = {'source_head': path.stem, 'source_receipt': _relative(inst, path),
             'source_sha256': hashlib.sha256(raw).hexdigest(), 'source_base': source_base,
             'source_patch_id': source_patch, 'base': base, 'patch_id': patch, 'rules_digest': current}
    return dict(source, head=head, carry=carry)


def validate_carried(inst, n, row, head, data, rules=None):
    """Recheck a carried receipt from its untouched source and immutable commits."""
    carry = data.get('carry')
    if (not isinstance(carry, dict) or set(carry) != set(CARRY_KEYS) or
            any(not isinstance(carry[k], str) or not HEAD.fullmatch(carry[k])
                for k in ('source_head', 'source_base', 'base', 'source_sha256', 'rules_digest')) or
            carry['source_head'] == head):
        raise ValueError(REFUSED + 'malformed carry provenance')
    if not n.get('external') or not n.get('home_path'):
        raise ValueError(REFUSED + 'carry-over needs an external git home')
    path = _m_review.review_receipt_path(n, row, carry['source_head'])
    if carry['source_receipt'] != _relative(inst, path) or path.is_symlink():
        raise ValueError(REFUSED + 'carry provenance names a different source receipt')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != carry['source_sha256']:
        raise ValueError(REFUSED + 'the source review receipt changed since carry-over')
    source = json.loads(raw)
    if not isinstance(source, dict) or 'carry' in source or data != dict(source, head=head, carry=carry):
        raise ValueError(REFUSED + 'carried receipt differs from its source review')
    _m_review.validate_review_receipt(inst, n, row, carry['source_head'], source, rules=rules)
    if source['verdict'] != 'pass':
        raise ValueError(REFUSED + 'the source review is blocked')
    current = _current_digest(n, row, rules)
    if not (source.get('rules_digest') == carry['rules_digest'] == current):
        raise ValueError(REFUSED + 'rules digest differs from the carried review')
    repo, main = n['home_path'], main_commit(n)
    if not (_ancestor(repo, carry['source_base'], carry['source_head']) and _ancestor(repo, carry['base'], head) and
            _ancestor(repo, carry['source_base'], carry['base']) and _ancestor(repo, carry['base'], main)):
        raise ValueError(REFUSED + 'carry bases are not on main below their heads')
    source_patch = patch_id(repo, carry['source_base'], carry['source_head'])
    patch = patch_id(repo, carry['base'], head)
    if not (source_patch == patch == carry['patch_id'] == carry['source_patch_id']):
        raise ValueError(REFUSED + 'patch ids no longer match')
    return data


def record_carried_receipt(inst, n, branch):
    """Write the carried receipt at the current branch head; the source receipt stays untouched."""
    from . import instance as _m_instance
    with _m_instance.brain_lock(inst):
        inst = _m_instance.Instance(inst.root)
        n = inst.node(n['id'])
        row = _m_jobs.open_job(_m_jobs.read_jobs(n) or [], branch)
        if not row:
            raise ValueError(REFUSED + 'the review receipt needs an open job')
        if not n.get('external') or not n.get('home_path'):
            raise ValueError(REFUSED + 'carry-over needs an external git home')
        head = _commit(n['home_path'], 'refs/heads/' + branch)
        path = _m_review.review_receipt_path(n, row, head)
        if path.exists() or path.is_symlink():
            raise ValueError(REFUSED + 'an exact-head review receipt already exists')
        data = validate_carried(inst, n, row, head, carry_candidate(inst, n, row, head))
        serialized = json.dumps(data, sort_keys=True) + '\n'
        _m_guard.no_terms_in(n, serialized, 'carried review receipt')
        _m_common.safe_target(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _m_common.atomic_write(path, serialized, private=True)
    return path, data


def ensure_carried(inst, n, branch):
    """Readiness hook: persist a carry-over when one applies; refusals are reported by the review validator."""
    try:
        row = _m_jobs.open_job(_m_jobs.read_jobs(n) or [], branch)
        if not row or not n.get('external') or not n.get('home_path'):
            return None
        path = _m_review.review_receipt_path(n, row, _commit(n['home_path'], 'refs/heads/' + branch))
        if path.exists() or not any(HEAD.fullmatch(p.stem) for p in path.parent.glob('*.json')):
            return None  # exact-head evidence or nothing to carry: never take the lock
        return record_carried_receipt(inst, n, branch)[0]
    except (OSError, ValueError, TypeError, KeyError):
        return None
