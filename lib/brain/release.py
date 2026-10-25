"""Opt-in scheduled releases of reviewed, exact-full-green native train tips."""
import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import shlex
import sys
import tempfile
from pathlib import Path

from . import common as _m_common
from . import jobs as _m_jobs
from . import nodes as _m_nodes
from . import review as _m_review
from . import review_findings as _m_review_findings


def release_ref(home, name):
    result = _m_common.git('rev-parse', '--verify', '--quiet', name + '^{commit}', cwd=home)
    if result.returncode:
        raise ValueError('release ref unavailable')
    return result.stdout.strip()


def release_reviews(inst, n, entries):
    validator = getattr(_m_review, 'validate_review_receipt', None)
    location = getattr(_m_review, 'review_receipt_path', None)
    if validator is None or location is None:
        raise ValueError('release requires native independent-review receipt validation')
    rows = _m_jobs.read_jobs(n) or []
    for entry in entries:
        found = [row for row in rows if _m_jobs.job_key(row) == entry['key']]
        if len(found) != 1 or found[0].get('Head commit') != entry['head'] or found[0].get('Outcome') != 'merged':
            raise ValueError('release job/head closure evidence changed')
        row = found[0]
        path = location(n, row, entry['head'])
        if path.is_symlink():
            raise ValueError('linked release review receipt')
        receipt = json.loads(path.read_text())
        validator(inst, n, row, entry['head'], receipt)
        if receipt['verdict'] != 'pass':
            raise ValueError('release review is blocked')
        findings = _m_review_findings.read_finding_state(inst, n, row, required=True)['findings']
        if any(f['severity'] in ('blocker', 'major') and
               (f['state'] != 'closed' or _m_review_findings.finding_inputs(
                   n['home_path'], entry['head'], f['paths']) != f['inputs']) for f in findings.values()):
            raise ValueError('release has open or reopened durable review findings')


def release_candidate(inst, n, number):
    head = release_ref(n['home_path'], 'refs/heads/main')
    folder = n['dir'] / 'job-trains'
    if folder.is_symlink():
        raise ValueError('linked train evidence')
    candidates = []
    for path in sorted(folder.glob('*.json')):
        if path.is_symlink():
            raise ValueError('linked train journal')
        data = json.loads(path.read_text())
        if (data.get('state') in ('complete', 'reconciled') and data.get('tip') == head
                and data.get('publication_succeeded') is True):
            candidates.append(data)
    if len(candidates) != 1:
        raise ValueError('release needs one published native train for exact local main')
    data = candidates[0]
    if not data.get('accepted') or not re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', data.get('origin_base', '')):
        raise ValueError('release requires reviewed entries and recorded cached origin base')
    runs = [r for r in data.get('runs', []) if r.get('head') == head]
    if not runs:
        raise ValueError('release needs an exact-head full gate')
    run = runs[-1]
    log = Path(run.get('log', ''))
    seconds = run.get('seconds')
    if (type(run.get('result')) is not int or run['result'] != 0
            or run.get('command') != n['fm'].get('verify', '').strip()
            or not isinstance(seconds, (int, float)) or isinstance(seconds, bool)
            or not math.isfinite(seconds) or seconds < 0 or log.is_symlink() or not log.is_file()
            or hashlib.sha256(log.read_bytes()).hexdigest() != run.get('log_digest')):
        raise ValueError('release full gate is failed, changed or incomplete')
    release_reviews(inst, n, data['accepted'])
    changes = _m_common.git('show', head + ':CHANGES.md', cwd=n['home_path'])
    section = re.search(r'^## [^\n]*\brelease\s+%d\b[^\n]*\n(.*?)(?=^## |\Z)' % number,
                        changes.stdout, re.M | re.S | re.I)
    if (changes.returncode or not section or 'For agents' not in section[1]
            or 'After upgrading' not in section[1]):
        raise ValueError('release needs its numbered CHANGES section with upgrade guidance')
    return data


def remote_release_refs(n):
    result = _m_common.git('ls-remote', '--refs', 'origin', 'refs/heads/main', 'refs/tags/release-*', cwd=n['home_path'])
    if result.returncode:
        raise ValueError('origin release refs unavailable; no push')
    refs = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2 or not re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', fields[0]) or fields[1] in refs:
            raise ValueError('origin advertised ambiguous release refs')
        refs[fields[1]] = fields[0]
    if 'refs/heads/main' not in refs:
        raise ValueError('origin main missing; no initial publication is inferred')
    return refs


def release_guard_script(expected, tip, tag, existing_hook=None):
    """Validate Git's advertised old IDs; receive-pack then fences later ref races."""
    return ("import os,subprocess,sys\n"
            + "expected,tip,tag,hook=" + repr((expected, tip, tag, str(existing_hook) if existing_hook else None)) + "\n"
            + "data=sys.stdin.buffer.read(); seen=set()\n"
            + "for line in data.decode().splitlines():\n"
            + " fields=line.split()\n"
            + " if len(fields)!=4: sys.exit(1)\n"
            + " local_ref,local_sha,remote_ref,remote_sha=fields\n"
            + " if local_sha!=tip or remote_ref in seen: sys.exit(1)\n"
            + " seen.add(remote_ref)\n"
            + " if remote_ref=='refs/heads/main':\n"
            + "  if remote_sha!=expected: sys.exit(1)\n"
            + " elif remote_ref=='refs/tags/'+tag:\n"
            + "  if not remote_sha or set(remote_sha)!={'0'}: sys.exit(1)\n"
            + " else: sys.exit(1)\n"
            + "if seen!={'refs/heads/main','refs/tags/'+tag}: sys.exit(1)\n"
            + "if hook and os.path.isfile(hook) and os.access(hook,os.X_OK):\n"
            + " sys.exit(subprocess.run([hook,*sys.argv[1:]],input=data).returncode)\n")


def push_release(n, data, tag):
    refs = remote_release_refs(n)
    if refs['refs/heads/main'] != data['origin_base'] or 'refs/tags/' + tag in refs:
        raise ValueError('origin moved or tag exists; no push')
    if _m_common.git('merge-base', '--is-ancestor', data['origin_base'], data['tip'], cwd=n['home_path']).returncode:
        raise ValueError('release is not a fast-forward from recorded origin')
    hook_path = _m_common.git('rev-parse', '--git-path', 'hooks/pre-push', cwd=n['home_path'])
    if hook_path.returncode:
        raise ValueError('existing pre-push hook location unavailable')
    existing = Path(hook_path.stdout.strip()) if hook_path.stdout.strip() else None
    if existing is not None and not existing.is_absolute():
        existing = n['home_path'] / existing
    with tempfile.TemporaryDirectory(prefix='brain-release-guard-') as temporary:
        folder = Path(temporary)
        guard = folder / 'guard.py'
        guard.write_text(release_guard_script(data['origin_base'], data['tip'], tag, existing))
        hook = folder / 'pre-push'
        hook.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' ' + shlex.quote(str(guard)) + ' "$@"\n')
        hook.chmod(0o700)
        result = _m_common.git('-c', 'core.hooksPath=' + str(folder), 'push', '--atomic', '--porcelain', 'origin',
                               data['tip'] + ':refs/heads/main', data['tip'] + ':refs/tags/' + tag,
                               cwd=n['home_path'])
    if result.returncode:
        raise ValueError('atomic non-forced release push failed; inspect durable attempt before retry')


def release_due(inst, n, rows):
    if n['fm'].get('release_auto') != 'on':
        return False
    now = inst.now_dt()
    today = 0
    for row in rows:
        at = inst.parse_time(row.get('at'))
        if not at or row.get('state') not in ('prepared', 'complete', 'unknown'):
            raise ValueError('release history unreadable; retain it for reconciliation')
        today += at.astimezone(now.tzinfo).date() == now.date()
    return today < 2


@contextlib.contextmanager
def release_repository(n):
    result = _m_common.git('rev-parse', '--git-common-dir', cwd=n['home_path'])
    if result.returncode:
        raise ValueError('release repository metadata unavailable')
    common = Path(result.stdout.strip())
    common = (n['home_path'] / common).resolve() if not common.is_absolute() else common.resolve()
    path = common / 'brain-release.lock'
    _m_common.safe_target(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('unsafe release lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('another release owns this repository')
        ledger = common / 'brain-releases.json'
        _m_common.safe_target(ledger)
        rows = json.loads(ledger.read_text()) if ledger.exists() else []
        if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
            raise ValueError('release history unreadable')
        yield ledger, rows
    finally:
        os.close(fd)


def save_releases(path, rows):
    _m_common.atomic_write(path, json.dumps(rows, indent=2) + '\n')


def run_release(inst, n, slot=None):
    with release_repository(n) as (ledger, rows):
        if not release_due(inst, n, rows):
            raise ValueError('release is disabled or the daily cap is reached')
        if slot and any(row.get('slot') == slot for row in rows):
            return None
        refs = remote_release_refs(n)
        numbers = [int(match[1]) for ref in refs for match in [re.fullmatch(r'refs/tags/release-([1-9][0-9]*)', ref)] if match]
        local = _m_common.git('tag', '--list', 'release-*', cwd=n['home_path'])
        if local.returncode:
            raise ValueError('local release tags unavailable')
        numbers += [int(match[1]) for tag in local.stdout.splitlines() for match in [re.fullmatch(r'release-([1-9][0-9]*)', tag)] if match]
        number = max(numbers, default=0) + 1
        data = release_candidate(inst, n, number)
        if any(row.get('head') == data['tip'] for row in rows):
            raise ValueError('this head already has a release attempt; reconcile it before retry')
        attempt = dict(at=inst.now_dt().isoformat(), state='prepared', head=data['tip'],
                       tag='release-' + str(number), slot=slot, gate_seconds=data['runs'][-1]['seconds'])
        rows.append(attempt)
        save_releases(ledger, rows)  # reserve before the outward action; crashes stay fenced
        try:
            push_release(n, data, attempt['tag'])
            after = remote_release_refs(n)
            if after.get('refs/heads/main') != data['tip'] or after.get('refs/tags/' + attempt['tag']) != data['tip']:
                raise ValueError('origin confirmation changed; publication outcome unknown')
        except (Exception, SystemExit):
            attempt['state'] = 'unknown'
            save_releases(ledger, rows)
            raise ValueError('release outcome unconfirmed; durable attempt retained, no automatic retry') from None
        attempt['state'] = 'complete'
        save_releases(ledger, rows)
        return dict(attempt)


def tick_releases(inst):
    """Existing native sampler calls this; configuration is explicit standing consent."""
    results = []
    now = inst.now_dt()
    for n in inst.nodes():
        if n['fm'].get('release_auto') != 'on' or not n.get('home_path'):
            continue
        try:
            raw = n['fm'].get('release_hours', '9,21')
            hours = sorted(set(int(value) for value in raw.split(',')))
            if len(hours) != 2 or any(not 0 <= hour <= 23 for hour in hours):
                raise ValueError('release_hours requires two distinct hours')
            eligible = [hour for hour in hours if hour <= now.hour]
            if not eligible:
                continue
            slot = now.date().isoformat() + '-' + str(eligible[-1])
            result = run_release(inst, n, slot)
            if result:
                results.append({'node': n['id'], **result})
        except (OSError, ValueError, KeyError, TypeError):
            results.append(dict(node=n['id'], state='blocked'))
    return results


def cmd_release(a):
    inst = _m_nodes.load()
    n = inst.node(a.id)
    if not a.run:
        print('release: explicit release_auto=on; two windows ' + n['fm'].get('release_hours', '9,21') + '; cap two attempts per calendar day')
        return
    try:
        result = run_release(inst, n)
    except (OSError, ValueError, KeyError, TypeError) as error:
        _m_common.die(str(error))
    if result:
        print('release: ' + result['head'] + ' ' + result['tag'] + '; gate ' + str(result['gate_seconds']) + 's')
