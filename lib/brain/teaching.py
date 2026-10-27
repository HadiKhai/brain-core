"""Scope-checked native teaching briefs and reversible checked adaptation copies."""
import hashlib
import json
from pathlib import Path
import re

from . import common as _m_common
from . import feedback as _m_feedback
from . import guard as _m_guard
from . import knowledge as _m_knowledge
from . import nodes as _m_nodes


def teaching_root(node, branch):
    if branch:
        entry = node['local'].get('worktrees', {}).get(branch)
        if not entry or not entry.get('path'):
            raise ValueError('teaching needs an existing registered worktree for --branch')
        root = Path(entry['path']).expanduser()
        current = _m_common.git('branch', '--show-current', cwd=root)
        if current.returncode or current.stdout.strip() != branch:
            raise ValueError('registered teaching branch changed or is unavailable')
        return root
    if node['external']:
        raise ValueError('external native teaching requires --branch for an isolated registered worktree')
    return node.get('home_path') or node['dir']


def native_target(root, relative):
    part = Path(relative)
    if part.is_absolute() or not part.parts or any(p in ('..', '.git', '.brain') for p in part.parts):
        raise ValueError('native target must stay inside the target home and outside repository metadata')
    path = root / part
    if root.resolve() not in path.resolve().parents:
        raise ValueError('native target escapes the target home')
    current = path
    while current != root:
        if current.is_symlink():
            raise ValueError('native target cannot follow links')
        current = current.parent
    if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
        raise ValueError('native target is not an independent regular file')
    return path


def native_copy(inst, node, item, root, a, terms):
    if not a.to or not a.check:
        raise ValueError('native teaching requires --to and the target\'s --check')
    source_bytes = item['path'].read_bytes()
    content = Path(a.file).expanduser().read_text()
    if not content.strip() or _m_guard.find_terms(content, terms):
        raise ValueError('native adaptation is empty or contains forbidden source/target content')
    target = native_target(root, a.to)
    previous = target.read_bytes() if target.exists() else None
    if previous is not None:
        try:
            previous_text = previous.decode('utf-8')
        except UnicodeError:
            raise ValueError('existing native target must be UTF-8 text')
        if not a.expected_sha256 or hashlib.sha256(previous).hexdigest() != a.expected_sha256:
            raise ValueError('existing native file requires its current --expected-sha256')
        if re.search(r'@generated|do not edit|generated file', previous.decode(errors='replace'), re.I):
            raise ValueError('native file is generated; teach its generator source instead')
    elif a.expected_sha256:
        raise ValueError('expected native file no longer exists')
    # Check containment and availability before touching the native tree.
    from . import learning as _m_learning
    if _m_learning.resolve_enforcing_check({'home_path': root, 'dir': root}, a.check) is None:
        raise ValueError('native check is missing or outside the target home')
    receipt_dir = inst.root / '.brain' / 'teaching'
    receipt_key = hashlib.sha256((node['id'] + '\n' + (a.branch or '') + '\n' + a.to).encode()).hexdigest()
    receipt = receipt_dir / (receipt_key + '.json')
    _m_common.safe_target(receipt)
    created = []
    parent = target.parent
    while not parent.exists():
        created.append(parent)
        parent = parent.parent
    for parent in reversed(created):
        parent.mkdir()
    applied = False
    try:
        _m_common.atomic_write(target, content)
        applied = True
        proof = _m_feedback.run_check_proof(inst, root, a.check, a.branch or '')
        if proof is None or target.read_text() != content or item['path'].read_bytes() != source_bytes:
            raise ValueError('native teaching check failed or changed the native adaptation')
        record = {'node': node['id'], 'item': item['fm'].get('id'), 'target': a.to,
                  'branch': a.branch or '', 'at': inst.now(),
                  'source_sha256': hashlib.sha256(source_bytes).hexdigest(),
                  'native_sha256': hashlib.sha256(content.encode()).hexdigest(), 'proof': proof}
        receipt_dir.mkdir(parents=True, exist_ok=True)
        _m_common.atomic_write(receipt, json.dumps(record, indent=2) + '\n', private=True)
    except (Exception, SystemExit):
        if applied and target.is_file() and target.read_text() == content:
            if previous is None:
                target.unlink()
            else:
                _m_common.atomic_write(target, previous_text)
        for parent in created:
            try:
                parent.rmdir()
            except OSError:
                pass
        raise
    print('native teaching applied and checked: ' + str(target))
    print('Native copy is uncommitted; continue the target\'s own change process.')


def teaching_summary(inst, node):
    """Aggregate private receipts without copying source references into node facts."""
    rows = []
    for path in (inst.root / '.brain' / 'teaching').glob('*.json'):
        try:
            row = json.loads(path.read_text())
            if isinstance(row, dict) and row.get('node') == node['id']:
                rows.append(row)
        except (OSError, ValueError):
            continue
    return {'copies': len(rows), 'last_at': max((row.get('at', '') for row in rows), default=None)}


def cmd_teach(a):
    inst = _m_nodes.load()
    node = inst.node(a.node)
    item = inst.item(a.item)
    try:
        if node['archived']:
            raise ValueError('archived nodes cannot receive teaching')
        ok, why = _m_knowledge.usable(item['fm'], node['owners'])
        if not ok:
            raise ValueError('item cannot be taught here: ' + why)
        terms = _m_knowledge.learning_terms(inst, item['fm'], [node])
        body = _m_common.read_fm(item['path'])[1]
        if _m_guard.find_terms(body, terms):
            raise ValueError('item content is forbidden at its source or target')
        if a.file:
            root = teaching_root(node, a.branch)
            current = node
            while current:
                if current['sealed'] and set(node['owners']) - set(_m_common.lst(a.consented_by)):
                    raise ValueError('native teaching across a boundary needs --consented-by for every target owner')
                current = current['parent']
            native_copy(inst, node, item, root, a, terms)
        else:
            if a.branch:
                teaching_root(node, a.branch)
            if a.to or a.check or a.expected_sha256:
                raise ValueError('native application options require --file with an adapted native version')
            text = ('Teach this capability using the target\'s native conventions.\n'
                    'Inspect its harness and generator sources; adapt names, stack and checks.\n'
                    'Produce a native file, then apply with brain teach --file --to --check.\n'
                    'Use the target\'s own review/change process; teaching does not publish.\n\n' + body)
            _m_guard.no_terms_in(node, text, 'teaching brief')
            print(text)
    except (OSError, ValueError) as error:
        _m_common.die(str(error))
