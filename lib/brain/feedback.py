"""Node-local correction ledger with executed check proof, never supplied green flags."""
import hashlib
import json
import os
import signal
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from . import common as _m_common
from . import guard as _m_guard
from . import history as _m_history
from . import jobs as _m_jobs
from . import learning as _m_learning
from . import nodes as _m_nodes

MARKER = '<!-- brain:feedback v1 -->'
FEEDBACK_CHECK_TIMEOUT = 30


def feedback_records(node):
    path = node['dir'] / 'feedback.md'
    if not path.exists():
        return []
    if path.is_symlink() or not path.is_file():
        raise ValueError('repair feedback.md: linked or non-file feedback store')
    text = path.read_text()
    match = re.fullmatch(r'# Feedback\n\n' + re.escape(MARKER) + r'\n```json\n(.*)\n```\n?', text, re.S)
    if not match:
        raise ValueError('repair feedback.md: unrecognized feedback store; existing data retained')
    rows = json.loads(match.group(1))
    if not isinstance(rows, list):
        raise ValueError('repair feedback.md: invalid records')
    seen = set()
    for row in rows:
        if (not isinstance(row, dict) or not re.fullmatch(r'F-\d{4,}', str(row.get('id', '')))
                or row['id'] in seen or row.get('status') not in ('open', 'closed')
                or any(not isinstance(row.get(k), str) for k in ('comment', 'created', 'principle', 'job', 'check', 'before', 'after'))):
            raise ValueError('repair feedback.md: invalid record')
        seen.add(row['id'])
        if row['status'] == 'closed':
            proof = row.get('proof')
            if (not isinstance(proof, dict) or type(proof.get('exit_code')) is not int or proof['exit_code'] != 0
                    or not re.fullmatch(r'[0-9a-f]{64}', str(proof.get('check_sha256', '')))
                    or any(not row[k] for k in ('check', 'job', 'before', 'after'))
                    or proof.get('job') != row['job'] or proof.get('check') != row['check']):
                raise ValueError('repair feedback.md: closed record lacks mapped green proof')
    return rows


def save_feedback(inst, node, rows):
    path = node['dir'] / 'feedback.md'
    text = '# Feedback\n\n' + MARKER + '\n```json\n' + json.dumps(rows, indent=2, ensure_ascii=False) + '\n```\n'
    _m_guard.no_terms_in(node, text, 'feedback ledger')
    _m_common.atomic_write(path, text)
    inst.commit([path], 'record correction feedback')


def feedback_projection(inst, node):
    try:
        rows = feedback_records(node)
        return _m_history.history_screen(rows, _m_guard.never_terms(node))
    except (OSError, ValueError):
        return [{'id': 'feedback-state', 'status': 'open', 'comment': 'repair feedback.md: invalid or unreadable feedback ledger'}]


def feedback_due(inst, node):
    return [{'node': node['id'], 'kind': 'feedback', 'episode': 'feedback-' + row['id'],
             'text': row['id'] + ': ' + row['comment']} for row in feedback_projection(inst, node) if row['status'] == 'open']


def feedback_check_root(node, row):
    worktree = node['local'].get('worktrees', {}).get(row['job'])
    if worktree:
        root = Path(worktree['path']).expanduser()
        if not root.is_dir():
            raise ValueError('mapped job worktree is unavailable')
        branch = _m_common.git('branch', '--show-current', cwd=root)
        if branch.returncode or branch.stdout.strip() != row['job']:
            raise ValueError('mapped job worktree branch changed')
        return root
    if not any(r['Job'] == row['job'] for r in (_m_jobs.read_jobs(node) or [])):
        raise ValueError('mapped job is not in the node ledger')
    return node.get('home_path') or node['dir']


def run_feedback_check(inst, node, row):
    if not all(row.get(k) for k in ('job', 'check', 'before', 'after')):
        raise ValueError('map job, check and before/after evidence before closing feedback')
    root = feedback_check_root(node, row)
    execution_node = {**node, 'home_path': root}
    check = _m_learning.resolve_enforcing_check(execution_node, row['check'])
    if check is None:
        raise ValueError('mapped check is missing or outside its job home')
    before = hashlib.sha256(check.read_bytes()).hexdigest()
    head = _m_common.git('rev-parse', 'HEAD', cwd=root)
    if head.returncode:
        raise ValueError('mapped check needs a git snapshot')
    command = ([sys.executable, str(check)] if check.suffix == '.py' else
               ['sh', str(check)] if check.suffix == '.sh' else [str(check)])
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(command, cwd=root, stdout=output, stderr=output, start_new_session=True)
        try:
            exit_code = process.wait(timeout=FEEDBACK_CHECK_TIMEOUT)
        except subprocess.TimeoutExpired:
            exit_code = None
        finally:
            # The check owns its process group, including children after the leader exits.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        if exit_code is None:
            return None
        output.seek(0)
        excerpt = output.read(16384)
    after = hashlib.sha256(check.read_bytes()).hexdigest()
    current = _m_common.git('rev-parse', 'HEAD', cwd=root)
    if exit_code or before != after or current.returncode or current.stdout != head.stdout:
        return None
    return {'exit_code': 0, 'at': inst.now(), 'job': row['job'], 'check': row['check'],
            'check_sha256': before, 'head': head.stdout.strip(),
            'output_sha256': hashlib.sha256(excerpt).hexdigest()}


def cmd_feedback(a):
    inst = _m_nodes.load()
    node = inst.node(a.id)
    try:
        rows = feedback_records(node)
        store_path = node['dir'] / 'feedback.md'
        store_before = store_path.read_bytes() if store_path.exists() else None
        if a.action == 'list':
            visible = feedback_projection(inst, node)
            if a.json:
                print(json.dumps(visible, indent=1, ensure_ascii=False))
            else:
                for row in visible:
                    print(f"{row['id']} {row['status']}: {row['comment']}")
            return
        if node['archived']:
            raise ValueError('archived nodes cannot change feedback')
        if not a.value:
            raise ValueError('feedback needs a comment or record id')
        if a.action == 'add':
            number = max([int(r['id'][2:]) for r in rows] or [0]) + 1
            row = {'id': f'F-{number:04d}', 'status': 'open', 'comment': a.value.strip(), 'created': inst.now(),
                   'principle': 'unclassified', 'job': '', 'check': '', 'before': '', 'after': ''}
            if not row['comment']:
                raise ValueError('feedback comment must be nonempty')
            rows.append(row)
        else:
            row = next((r for r in rows if r['id'] == a.value), None)
            if row is None:
                raise ValueError('unknown feedback id')
        for field in ('principle', 'job', 'check', 'before', 'after'):
            value = getattr(a, field, None)
            if value is not None:
                if field == 'check' and (Path(value).is_absolute() or '..' in Path(value).parts):
                    raise ValueError('check must be a relative path inside its job home')
                if field == 'principle' and not re.fullmatch(r'[a-z][a-z0-9-]*', value):
                    raise ValueError('principle needs a stable id')
                row[field] = value.strip()
        if a.action == 'close':
            proof = run_feedback_check(inst, node, row)
            if proof is None:
                _m_common.die('mapped check did not pass unchanged; feedback remains open')
            if store_path.read_bytes() != store_before:
                raise ValueError('feedback mapping changed while the check ran; existing state retained')
            row.update(status='closed', proof=proof, closed=inst.now())
        else:
            row['status'] = 'open'
            row.pop('proof', None)
            row.pop('closed', None)
        save_feedback(inst, node, rows)
        print(f"{row['id']}: {row['status']}")
    except (OSError, ValueError) as error:
        _m_common.die(str(error))
