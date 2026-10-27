"""Node-local task records, dependency checks and evidence-backed transitions.

Records are Markdown with JSON-valued frontmatter, one file per task. All
mutations use the instance lock; reads never migrate or contact a provider.
"""
import json
import re
from pathlib import Path

from . import common as _m_common
from . import instance as _m_instance
from . import nodes as _m_nodes

VERSION = 1
STATUSES = ('backlog', 'ready', 'in-progress', 'in-review', 'testing', 'merged', 'done', 'blocked', 'parked', 'dropped')
KINDS = ('feature', 'bug', 'fix', 'test', 'chore', 'research')
ID = re.compile(r'[A-Za-z][A-Za-z0-9_-]*\Z')
SHA = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')
FIELDS = {'title', 'kind', 'status', 'priority', 'order', 'parent', 'depends_on', 'requirement', 'decisions',
          'owner', 'job', 'branch', 'pr', 'merge_sha', 'code_path', 'evidence', 'estimate', 'spent', 'strikes',
          'park_reason', 'resume', 'drop_reason', 'description', 'source', 'provenance', 'external_url'}
FIELDS |= {'superseded_by', 'completion_kind', 'completion_receipt', 'baseline_sha', 'scope', 'aliases',
           'last_progress_at', 'review_at', 'next_action', 'drop_decision', 'hold', 'execution_owner', 'unresolved_dependencies', 'contract', 'authority'}
LISTS = {'depends_on', 'decisions', 'evidence', 'provenance', 'superseded_by', 'scope', 'aliases', 'unresolved_dependencies'}


def record_path(n, ident, folder='tasks'):
    if not isinstance(ident, str) or not ID.fullmatch(ident):
        raise ValueError('record id must contain only letters, numbers, underscores and hyphens')
    base = n['dir'] / folder
    path = base / (ident + '.md')
    if base.is_symlink() or path.is_symlink() or (path.exists() and path.stat().st_nlink != 1):
        raise ValueError('record folders and files must not be links')
    return path


def read_records(n, folder='tasks', ident=None):
    base = n['dir'] / folder
    if base.is_symlink():
        raise ValueError('record folder must not be a link')
    records = {}
    paths = [record_path(n, ident, folder)] if ident is not None else sorted(base.glob('*.md'))
    for path in paths:
        if not path.exists():
            continue
        record_path(n, path.stem, folder)
        fm, body = _m_common.read_fm(path)
        row = {}
        for key, value in fm.items():
            try:
                row[key] = json.loads(value)
            except ValueError:
                row[key] = value
        if row.get('version') != VERSION or row.get('id') != path.stem:
            raise ValueError('unsupported or mismatched record: ' + path.name)
        row['description'] = body.strip()
        if folder == 'tasks':
            for key in ('superseded_by', 'scope', 'aliases', 'unresolved_dependencies'):
                row.setdefault(key, [])
            row.setdefault('completion_kind', 'merge')
            row.setdefault('last_progress_at', row.get('created', row.get('updated', '')))
        records[row['id']] = row
    return records


def save_record(n, row, folder='tasks'):
    path = record_path(n, row['id'], folder)
    _m_common.safe_target(path)
    path.parent.mkdir(exist_ok=True)
    values = {k: v for k, v in row.items() if k != 'description'}
    text = '---\n' + '\n'.join(k + ': ' + json.dumps(v, ensure_ascii=False) for k, v in values.items())
    _m_common.atomic_write(path, text + '\n---\n' + row.get('description', '').strip() + '\n')
    _m_common.touch(path)


def source(n):
    value = n['fm'].get('tasks.source', 'local')
    if value not in ('local', 'linear'):
        raise ValueError('tasks.source must be local or linear')
    return value


def normalize(changes):
    unknown = set(changes) - FIELDS
    if unknown:
        raise ValueError('unknown task fields: ' + ', '.join(sorted(unknown)))
    out = dict(changes)
    if 'completion_receipt' in out and isinstance(out['completion_receipt'], str):
        try:
            out['completion_receipt'] = json.loads(out['completion_receipt'])
        except ValueError:
            raise ValueError('completion_receipt must be JSON')
    for key in LISTS & out.keys():
        if isinstance(out[key], str):
            out[key] = [s.strip() for s in out[key].split(',') if s.strip()]
        if not isinstance(out[key], list) or any(not isinstance(x, str) or not x.strip() for x in out[key]):
            raise ValueError(key + ' must be a list of nonempty strings')
    if 'status' in out:
        out['status'] = out['status'].strip().lower().replace(' ', '-')
    for key in ('order', 'strikes'):
        if key in out:
            try:
                if isinstance(out[key], bool) or int(out[key]) < 0 or str(int(out[key])) != str(out[key]):
                    raise ValueError()
                out[key] = int(out[key])
            except (TypeError, ValueError):
                raise ValueError(key + ' must be a nonnegative integer')
    return out


def validate(n, records, selected=None):
    jobs = requirements = None
    for ident, row in records.items():
        if selected is not None and ident != selected:
            continue
        record_path(n, ident)
        if not isinstance(row.get('title'), str) or not row['title'].strip():
            raise ValueError(ident + ': title required')
        if row.get('status') not in STATUSES or row.get('kind') not in KINDS:
            raise ValueError(ident + ': unknown status or kind')
        for key in ('title', 'status', 'kind', 'owner', 'source', 'job', 'branch', 'code_path', 'merge_sha',
                    'baseline_sha', 'park_reason', 'resume', 'review_at', 'next_action', 'requirement', 'parent', 'drop_decision', 'hold'):
            if key in row and not isinstance(row[key], str):
                raise ValueError(ident + ': invalid scalar field ' + key)
        if row.get('source') == 'linear':
            from . import task_linear as _m_linear
            if not isinstance(row.get('provider'), dict) or row['provider'].get('team') != _m_linear.config(n):
                raise ValueError('external task lacks provider identity')
            _m_linear.uid(row['provider'].get('id'))
        if row.get('source') not in ('local', 'linear'):
            raise ValueError(ident + ': invalid task source')
        if not isinstance(row.get('owner'), str) or not row['owner'].strip():
            if row.get('source') != 'linear':
                raise ValueError(ident + ': owner required')
        if row.get('priority') not in ('P0', 'P1', 'P2', 'P3'):
            raise ValueError(ident + ': priority must be P0-P3')
        for key in ('order', 'strikes'):
            if type(row.get(key)) is not int or row[key] < 0:
                raise ValueError(ident + ': invalid ' + key)
        for key in LISTS:
            if not isinstance(row.get(key), list) or any(not isinstance(x, str) or not x.strip() for x in row[key]):
                raise ValueError(ident + ': invalid ' + key)
        deps = [d for d in row['depends_on'] if ':' not in d] + ([row['parent']] if row.get('parent') else [])
        external = row.get('source') == 'linear' and isinstance(row.get('provider'), dict)
        if any(d not in records for d in deps) and not external:
            raise ValueError(ident + ': missing dependency or parent')
        for link in row['decisions']:
            if not re.fullmatch(r'(?:repo:D[0-9]+|private:D-[A-Za-z0-9-]+)', link):
                raise ValueError('decision links use repo:D123 or private:D-123')
        if row.get('requirement'):
            if requirements is None:
                requirements = {}
            requirement = row['requirement']
            if requirement not in requirements:
                requirements.update(read_records(n, 'requirements', requirement))
            if requirement not in requirements:
                raise ValueError(ident + ': requirement not found')
        if row['status'] in ('in-progress', 'in-review', 'testing') and not external:
            if jobs is None:
                from . import jobs as _m_jobs
                jobs = _m_jobs.read_jobs(n) or []
            has_attempt = any(a.get('state') == 'open' and a.get('id') for a in row.get('attempts', []) if isinstance(a, dict))
            if not has_attempt and not any(r.get('Job') == row.get('branch') and not r.get('Ended') and
                       _m_jobs.job_key(r) == row.get('job') for r in jobs):
                raise ValueError(ident + ': active status requires a live ledger job and branch')
        if row['status'] in ('merged', 'done'):
            from . import task_flow as _m_flow
            _m_flow.completion(n, row, records=records)
        if row['status'] == 'parked' and (not row.get('park_reason') or not row.get('resume') or
                not row.get('next_action') or not (row.get('owner') or row.get('execution_owner')) or not _m_instance.Instance(instance_root(n)).parse_time(row.get('review_at'))):
            raise ValueError(ident + ': parked tasks require a reason and resume condition')
        if row['status'] == 'dropped' and not (external and row['provider'].get('state_type') == 'canceled') and (not row.get('drop_reason') or not re.fullmatch(r'(?:repo:D[0-9]+|private:D-[A-Za-z0-9-]+)', row.get('drop_decision', ''))):
            raise ValueError(ident + ': dropped tasks require an explicit reason')
    from . import task_flow as _m_flow
    _m_flow.validate_graph(n, records)


def independent_add(changes):
    from . import task_flow
    return changes.get('status', 'backlog') in ('backlog', 'ready') and not task_flow.links(changes)


def independent_set(n, ident, changes):
    from . import task_flow
    row = read_records(n, ident=ident).get(ident)
    return row is not None and not task_flow.links({**row, **normalize(changes)})


def mutation_paths(inst, target=None, changes=None, ident=None, create=False):
    if target is not None and ident is not None and (
            create and independent_add(normalize(changes or {})) or
            not create and independent_set(inst.node(target), ident, changes or {})):
        node = inst.node(target)
        paths = [record_path(node, ident)]
        effective = {**read_records(node, ident=ident).get(ident, {}), **normalize(changes or {})}
        requirement = effective.get('requirement')
        if requirement:
            paths.append(record_path(node, requirement, 'requirements'))
        if effective.get('status') in ('in-progress', 'in-review', 'testing'):
            paths.append(node['dir'] / 'job-ledger.md')
        if effective.get('status') in ('done', 'merged'):
            if effective.get('completion_kind', 'merge') == 'merge' and node.get('home_path'):
                paths.extend(_m_instance.git_state_paths(node['home_path'], task_proof=True))
            receipt = effective.get('completion_receipt')
            relative = receipt.get('artifact') if isinstance(receipt, dict) else None
            if isinstance(relative, str) and relative and not Path(relative).is_absolute() and '..' not in Path(relative).parts:
                path = node['dir'] / relative
                if not path.is_dir():
                    paths.extend((path, path.resolve()))
        return paths
    paths = []
    def artifact(node, receipt):
        if isinstance(receipt, str):
            try:
                receipt = json.loads(receipt)
            except ValueError:
                return  # the existing validator refuses malformed receipts
        relative = receipt.get('artifact') if isinstance(receipt, dict) else None
        if isinstance(relative, str) and relative and not Path(relative).is_absolute() and '..' not in Path(relative).parts:
            path = node['dir'] / relative
            if not path.is_dir():  # directory artifacts are invalid; never traverse their contents
                paths.extend((path, path.resolve()))
    from . import task_flow as _m_flow
    nodes = {node['id']: node for node in inst.nodes()}
    selected = list(nodes) if target is None else [target]
    cache, visited, proof_nodes = {}, set(), set()

    def visit(node, row, proof=True):
        key = (node['id'], row['id'], proof)
        if key in visited:
            return
        visited.add(key)
        if proof:
            artifact(node, row.get('completion_receipt'))
        if proof and row.get('status') in ('merged', 'done') and row.get('completion_kind', 'merge') == 'merge':
            proof_nodes.add(node['id'])
        for ref in _m_flow.links(row):
            peer, dep, _ = _m_flow.resolve(node, ref, cache.get(node['id']), cache)
            if dep is not None:
                visit(peer, dep, proof)

    for node_id in selected:
        node = nodes[node_id]
        cache[node_id] = read_records(node)
        if target == node_id and ident is not None:
            # Include implicit completion-kind conversions and normalized status.
            effective = {'status': 'backlog', 'completion_kind': 'merge',
                         **cache[node_id].get(ident, {}), **normalize(changes or {}), 'id': ident}
            cache[node_id] = {**cache[node_id], ident: effective}
        for row in cache[node_id].values():
            visit(node, row, target is None or ident is None or row['id'] == ident)
    for node_id in cache:
        node = nodes[node_id]
        paths.extend(node['dir'] / name for name in
                     ('tasks', 'requirements', 'job-ledger.md', 'job-handbacks', 'job-attempts'))
    for node_id in sorted(proof_nodes):
        node = nodes[node_id]
        if node.get('home_path'):
            paths.extend(_m_instance.git_state_paths(node['home_path'], task_proof=True))
    return paths


def prepare_mutation(inst, n, ident, changes, create=False, provider=False):
    rows = (read_records(n, ident=ident) if create or independent_set(n, ident, changes)
            else read_records(n))
    if create == (ident in rows):
        raise ValueError('task already exists' if create else 'task not found')
    changes = normalize(changes)
    if not create and 'source' in changes and changes['source'] != rows[ident].get('source'):
        raise ValueError('task source cannot be changed; preserve provider ownership')
    if source(n) == 'linear' and not provider and (create and changes.get('source') != 'local' or
            not create and rows[ident].get('source') == 'linear' and
            set(changes) & {'title', 'description', 'status', 'priority', 'owner', 'depends_on'}):
        raise ValueError('Linear owns external task fields; use task sync or the provider adapter')
    if create:
        row = {'version': VERSION, 'id': ident, 'title': '', 'kind': 'feature', 'status': 'backlog',
               'priority': 'P2', 'order': 0, 'owner': ','.join(n.get('owners', [])), 'depends_on': [], 'decisions': [],
               'evidence': [], 'provenance': [], 'superseded_by': [], 'scope': [], 'aliases': [], 'unresolved_dependencies': [],
               'strikes': 0, 'source': 'local', 'completion_kind': 'merge', 'created': inst.now(),
               'last_progress_at': inst.now()}
    else:
        row = dict(rows[ident])
    row.update(changes, updated=inst.now())
    if set(changes) & {'status', 'job', 'evidence', 'merge_sha', 'completion_receipt'}:
        row['last_progress_at'] = changes.get('last_progress_at') or inst.now()
    if create:
        from . import task_flow
        # Every linked creation retains the complete graph, including qualified returns.
        if task_flow.links(row):
            rows = read_records(n)
    rows[ident] = row
    validate(n, rows, selected=ident)
    closed_transition = row['status'] in ('merged', 'done') and (create or read_records(n, ident=ident).get(ident, {}).get('status') != row['status'])
    if closed_transition:
        row['event_pending'] = 'task_landed'
    return row, closed_transition


def mutate(inst, n, ident, changes, create=False, provider=False):
    with _m_instance.brain_lock(inst):
        current = _m_instance.Instance(inst.root)
        node = current.node(n['id'])
        independent = (create and independent_add(normalize(changes)) or
                       not create and independent_set(node, ident, changes))
        with _m_instance.released(inst, node if independent else list(current.nodes()),
                extra=lambda: mutation_paths(current, node['id'], changes, ident, create)):
            row, closed_transition = prepare_mutation(current, node, ident, changes, create, provider)
        n = node
        save_record(n, row)
        inst.commit([], n['id'] + ': task ' + ident)
        if closed_transition:
            emit_task_landed(inst, n, row)
        return row


def projection(n):
    rows = read_records(n)
    blocks = {}
    for row in rows.values():
        for dependency in set(row.get('depends_on', [])):
            blocks.setdefault(dependency, []).append(row['id'])
    result = []
    for row in sorted(rows.values(), key=lambda r: (r.get('order', 0), r.get('priority', 'P2'), r['id'])):
        from . import task_flow as _m_flow
        blockers = _m_flow.blockers(n, row, rows) + row.get('unresolved_dependencies', [])
        result.append({**row, 'blocked_by': blockers,
                       'blocks': blocks.get(row['id'], [])})
    return result


def requirement_add(inst, n, ident, quote, at, ref):
    record_path(n, ident, 'requirements')
    if not ident.startswith('REQ-') or not quote.strip() or not inst.parse_time(at) or not ref.strip():
        raise ValueError('requirement needs REQ- id, exact quote, absolute date and source reference')
    if ident in read_records(n, 'requirements'):
        raise ValueError('requirement already exists')
    row = {'version': VERSION, 'id': ident, 'quote': quote, 'at': at, 'ref': ref, 'created': inst.now()}
    save_record(n, row, 'requirements')
    inst.commit([], n['id'] + ': requirement ' + ident)
    return row


def cmd_requirement(a):
    inst = _m_nodes.load()
    n = inst.node(a.node)
    try:
        row = requirement_add(inst, n, a.requirement_id, a.quote, a.at, a.ref)
        print(json.dumps(row, ensure_ascii=False))
    except (OSError, ValueError) as error:
        _m_common.die(str(error))


def command_changes(a):
    """Validate argument shape without reading mutable task or ledger state."""
    changes = {}
    for item in a.fields:
        key, sep, value = item.partition('=')
        if not sep or key in changes:
            raise ValueError('fields must be unique key=value pairs')
        changes[key] = value
    for key in ('title', 'status', 'owner'):
        if getattr(a, key) is not None:
            changes[key] = getattr(a, key)
    changes = normalize(changes)
    if not ID.fullmatch(a.task_id):
        raise ValueError('record id must contain only letters, numbers, underscores and hyphens')
    if 'status' in changes and changes['status'] not in STATUSES:
        raise ValueError(a.task_id + ': unknown status or kind')
    if 'kind' in changes and changes['kind'] not in KINDS:
        raise ValueError(a.task_id + ': unknown status or kind')
    if (a.action == 'add' or 'title' in changes) and not changes.get('title', '').strip():
        raise ValueError(a.task_id + ': title required')
    if 'priority' in changes and changes['priority'] not in ('P0', 'P1', 'P2', 'P3'):
        raise ValueError(a.task_id + ': priority must be P0-P3')
    if 'source' in changes and changes['source'] not in ('local', 'linear'):
        raise ValueError(a.task_id + ': invalid task source')
    return changes


def cmd_task(a):
    if a.action in ('add', 'set'):
        _m_instance.unlock()  # native mutate owns its write; parsing/validation do not
    inst = _m_nodes.load()
    n = inst.node(a.node)
    try:
        if a.action in ('add', 'set'):
            changes = command_changes(a)
            rows = [mutate(inst, n, a.task_id, changes, create=a.action == 'add')]
            _m_instance.unlock()
            # A new open task does not change the ledger/status fact projection.
            a._open_task_added = a.action == 'add' and rows[0]['status'] in ('backlog', 'ready')
        else:
            rows = projection(n)
            if a.action == 'show':
                rows = [r for r in rows if r['id'] == a.task_id]
                if not rows:
                    raise ValueError('task not found')
            if a.status:
                rows = [r for r in rows if r['status'] == a.status]
            if a.owner:
                rows = [r for r in rows if r.get('owner') == a.owner]
            if a.action == 'next':
                from . import task_flow as _m_flow
                rows = [r for r in rows if r['status'] == 'ready' and not r['blocked_by'] and not _m_flow.holds(inst, n, r)][:1]
            if a.action == 'check':
                validate(n, read_records(n))
                print('tasks: valid')
                return
        if a.json:
            if a.action == 'next':
                from . import task_flow as _m_flow
                all_rows = projection(n)
                value = {'next': rows[0] if rows else None,
                         'counts': {'tasks': len(all_rows), 'parked': sum(r['status'] == 'parked' for r in all_rows),
                                    'held': sum(bool(_m_flow.holds(inst, n, r)) for r in all_rows),
                                    'active_attempts': sum(sum(not a.get('ended') for a in r.get('attempts', [])) for r in all_rows)},
                         'holds': _m_flow.holds(inst, n)}
            else:
                value = rows
            print(json.dumps(value, ensure_ascii=False))
        else:
            for row in rows:
                if a.action == 'graph':
                    print(row['id'] + ' -> ' + ', '.join(row.get('depends_on', [])))
                else:
                    print(f"{row['id']} [{row['status']}] {row['title']}" +
                          (' · blocked by ' + ', '.join(row['blocked_by']) if row.get('blocked_by') else ''))
    except (OSError, ValueError) as error:
        _m_common.die(str(error))


def patch_parser(parser):
    """Metadata admission and full task dispatch share the exact set grammar."""
    parser.add_argument('node')
    parser.add_argument('task_id')
    parser.add_argument('fields', nargs='*')
    parser.add_argument('--request-id', help='metadata set retries: same payload-bound queue ID; queue wait ID before dependent reads')
    parser.add_argument('--title')
    parser.add_argument('--status', choices=STATUSES)
    parser.add_argument('--owner')
    parser.add_argument('--json', action='store_true')


def register(sub):
    parser = sub.add_parser('task', help='node-local tasks, dependencies and completion evidence')
    actions = parser.add_subparsers(dest='action', required=True)
    for action in ('sync', 'create'):
        external = actions.add_parser(action, help='explicit Linear task adapter operation')
        external.add_argument('node')
        if action == 'create':
            external.add_argument('--title', required=True)
            external.add_argument('--description', default='')
        external.set_defaults(f=cmd_task_provider)
    from . import task_flow as _m_flow
    _m_flow.register(actions)
    from . import task_requirements as _m_requirements
    from . import tasks_backfill as _m_backfill
    _m_requirements.register(actions)
    from . import task_reconcile
    task_reconcile.register(actions)
    migration = actions.add_parser('migrate', help='additively migrate local task sources after install/upgrade')
    migration.add_argument('node', nargs='?')
    migration.add_argument('--all', action='store_true')
    migration.set_defaults(f=_m_backfill.cmd_task_migrate)
    fill = actions.add_parser('backfill', help='import node-local promises without changing source files')
    fill.add_argument('node')
    fill.add_argument('--apply', action='store_true')
    fill.add_argument('--register', help='versioned stopped-work register; imported as open reconciliation')
    fill.set_defaults(f=cmd_task_backfill)
    failure = actions.add_parser('event', help='record one identified failed round')
    failure.add_argument('node')
    failure.add_argument('task_id')
    failure.add_argument('--event-id', required=True)
    failure.add_argument('--reason', required=True)
    failure.set_defaults(f=cmd_task_event)
    req = actions.add_parser('requirement-add')
    req.add_argument('node')
    req.add_argument('requirement_id')
    req.add_argument('--quote', required=True)
    req.add_argument('--at', required=True)
    req.add_argument('--ref', required=True)
    req.set_defaults(f=cmd_requirement)
    for action in ('add', 'set', 'show', 'list', 'next', 'graph', 'check'):
        p = actions.add_parser(action)
        if action == 'set':
            patch_parser(p)
            p.set_defaults(f=cmd_task)
            continue
        p.add_argument('node')
        if action in ('add', 'set', 'show'):
            p.add_argument('task_id')
        if action in ('add', 'set'):
            p.add_argument('fields', nargs='*')
            p.add_argument('--request-id', help='metadata set retries: same payload-bound queue ID; queue wait ID before dependent reads')
        p.add_argument('--title')
        p.add_argument('--status', choices=STATUSES)
        p.add_argument('--owner')
        p.add_argument('--json', action='store_true')
        p.set_defaults(f=cmd_task)


def prepare_start(n, task_id=None, branch=''):
    """Refuse before worktree creation. Existing instances remain opt-in."""
    rows = read_records(n)
    if branch.startswith('import:'):
        return
    if not task_id:
        if rows:
            raise ValueError('this node uses tasks; starting a job requires --task-id')
        return
    row = rows.get(task_id)
    if not row or row['status'] != 'ready':
        raise ValueError('starting a job requires a ready task')
    from . import task_flow as _m_flow
    if _m_flow.holds(_m_flow.instance_for(n), n, row):
        raise ValueError('task dispatch is held; explicit resume required')
    if row.get('unresolved_dependencies') or _m_flow.blockers(n, row, rows):
        raise ValueError('task dependencies are unfinished')
    validate(n, rows)


def job_started(inst, n, row):
    ident = row.get('Task')
    if ident:
        from . import jobs as _m_jobs
        from . import task_flow as _m_flow
        current = read_records(n)[ident]
        baseline = inst.local_of(n['id']).get('worktrees', {}).get(row['Job'], {}).get('start')
        if not baseline and n.get('home_path'):
            baseline = _m_common.git('rev-parse', '--verify', 'HEAD', cwd=n['home_path']).stdout.strip()
        changes = {'status': 'in-progress', 'job': _m_jobs.job_key(row), 'branch': row['Job'],
                   'aliases': list(dict.fromkeys(current.get('aliases', []) + [row['Job']])),
                   'execution_owner': ','.join(n.get('owners', []))}
        if baseline and not current.get('baseline_sha'):
            changes['baseline_sha'] = baseline
        updated = mutate(inst, n, ident, changes, provider=True)
        attempts = updated.get('attempts', [])
        key = _m_jobs.job_key(row)
        if not any(a.get('job') == key for a in attempts):
            updated['attempts'] = attempts + [{'id': key, 'job': key, 'branch': row['Job'], 'head': baseline or '', 'started': row['Started'], 'state': 'open'}]
            save_record(n, updated)
        provider_job_state(inst, n, ident, 'started')


def event(inst, n, ident, event_id, reason):
    """One strike per recorded failure identity, even after delivery retries."""
    if not event_id.strip() or not reason.strip():
        raise ValueError('failure event requires an id and reason')
    with _m_instance.brain_lock(inst):
        rows = read_records(n)
        if ident not in rows:
            raise ValueError('task not found')
        row = dict(rows[ident])
        events = list(row.get('failure_events', []))
        if event_id in events:
            return row
        if row['status'] in ('merged', 'done', 'dropped'):
            raise ValueError('closed task cannot receive a failure')
        row['failure_events'] = events + [event_id]
        row['strikes'] += 1
        if row['strikes'] >= 2:
            row.update(status='parked', park_reason=reason, resume='Repair the failing step and explicitly set ready')
            from . import task_flow as _m_flow
            row.update(_m_flow.park_fields(inst))
        else:
            row.update(status='blocked', park_reason=reason)
        row['updated'] = inst.now()
        rows[ident] = row
        validate(n, rows)
        save_record(n, row)
        inst.commit([], n['id'] + ': task failure ' + ident)
        return row


def handback_event(inst, n, row, data):
    ident = row.get('Task')
    if not ident:
        return
    packet = data.get('ready') or {}
    failed = data.get('state') == 'failed' or type(packet.get('verify_result')) is int and packet['verify_result'] != 0
    if failed:
        event_id = 'handback:' + str(data.get('attempt_id') or data.get('at'))
        event(inst, n, ident, event_id, data.get('summary') or 'Verification failed')
    elif data.get('state') == 'done' and packet.get('verify_result') == 0:
        existing = read_records(n)[ident]
        if existing['status'] != 'parked':
            mutate(inst, n, ident, {'status': 'testing', 'evidence': existing['evidence'] + [
                'Verification exit 0 at ' + data['at']]}, provider=True)


def job_closed(inst, n, row):
    ident = row.get('Task')
    if not ident:
        return
    from . import sync as _m_sync
    from . import jobs as _m_jobs
    current = read_records(n)[ident]
    for attempt in current.get('attempts', []):
        if attempt.get('job') == _m_jobs.job_key(row):
            attempt.update(state='closed', ended=row.get('Ended') or inst.now())
    save_record(n, current)
    packet = _m_sync.integration_packet(inst, n, row) or {}
    changes = {'status': 'parked', 'park_reason': row.get('Cause') or 'Job closed without proven task completion',
               'resume': 'Start a new task job or supply verified main landing evidence'}
    head = row.get('Head commit') or packet.get('head', '')
    if row.get('Outcome') == 'merged' and packet.get('verify_result') == 0 and head == packet.get('head'):
        base = packet.get('integration_base') or packet.get('main')
        main = _m_common.git('rev-parse', '--verify', 'refs/heads/main^{commit}', cwd=n['home_path']).stdout.strip()
        diff = _m_common.git('diff', '--name-only', base or head + '^', head, cwd=n['home_path'])
        if (diff.returncode == 0 and diff.stdout.strip() and SHA.fullmatch(main) and
                _m_common.git('merge-base', '--is-ancestor', head, main, cwd=n['home_path']).returncode == 0):
            changes = {'status': 'done', 'merge_sha': main, 'code_path': ', '.join(diff.stdout.splitlines()),
                       'baseline_sha': current.get('baseline_sha') or base or '',
                       'evidence': list(dict.fromkeys(current['evidence'] + [
                           'Verified job ' + row['Job'] + ' at ' + head + '; landed on main ' + main])),
                       'park_reason': '', 'resume': ''}
    from . import task_flow as _m_flow
    if changes['status'] == 'parked':
        changes.update(_m_flow.park_fields(inst))
    # Existing task proof remains unchanged if the job endpoint is not on main.
    try:
        mutate(inst, n, ident, changes, provider=True)
        if changes['status'] == 'done':
            provider_job_state(inst, n, ident, 'completed')
    except ValueError:
        mutate(inst, n, ident, {'status': 'parked', 'park_reason': 'Landing evidence is incomplete',
                              'resume': 'Provide the exact verified commit already on main', **_m_flow.park_fields(inst)}, provider=True)


def train_finished(inst, n, rows, data):
    """Publish one validated task graph; a verified train journal retries torn writes.

    The caller holds the instance lock and has published the entire ledger. No
    provider requests occur here: external completion intent uses the sync outbox.
    """
    import copy
    from . import jobs as _m_jobs
    from . import task_flow as _m_flow
    linked = []
    for entry in data['accepted']:
        job = next((r for r in rows if _m_jobs.job_key(r) == entry['key']), None)
        if not job or job.get('Job') != entry['branch'] or job.get('Head commit') != entry['head'] or not job.get('Ended'):
            raise ValueError('train task reconciliation requires the accepted ended ledger jobs')
        if job.get('Task'):
            linked.append((entry, job))
    if not linked:
        return
    records = copy.deepcopy(read_records(n))
    changed = set()
    tip = data.get('tip', '')
    verified = (SHA.fullmatch(tip) and n.get('home_path') and
                _m_common.git('merge-base', '--is-ancestor', tip, 'refs/heads/main', cwd=n['home_path']).returncode == 0 and
                any(type(run.get('result')) is int and run['result'] == 0 and run.get('head') == tip and
                    run.get('jobs') == [e['branch'] for e in data['accepted']] for run in data.get('runs', [])))
    for entry, job in linked:
        ident = job['Task']
        if ident in changed or ident not in records:
            raise ValueError('train task binding is missing or ambiguous')
        row = records[ident]
        receipt = {k: entry[k] for k in ('key', 'head', 'base')}
        receipt.update(batch=data['batch'], tip=tip)
        if row.get('train_completion') == receipt:
            continue
        if row.get('job') != entry['key'] or row.get('branch') != entry['branch']:
            raise ValueError('train task binding changed; retain journal for reconciliation')
        for attempt in row.get('attempts', []):
            if attempt.get('job') == entry['key']:
                attempt.update(state='closed', ended=data['ended'])
        row.update(status='parked', park_reason='Train landing evidence is incomplete',
                   resume='Provide exact verified main landing evidence and resume the task',
                   **_m_flow.park_fields(inst))
        if (verified and SHA.fullmatch(entry['head']) and SHA.fullmatch(entry['base']) and
                _m_common.git('merge-base', '--is-ancestor', entry['head'], tip, cwd=n['home_path']).returncode == 0 and
                _m_common.git('merge-base', '--is-ancestor', entry['base'], entry['head'], cwd=n['home_path']).returncode == 0):
            diff = _m_common.git('diff', '--name-only', entry['base'], entry['head'], cwd=n['home_path'])
            if diff.returncode == 0 and diff.stdout.strip():
                evidence = 'Verified train ' + data['batch'] + ' at ' + tip
                row.update(status='done', merge_sha=tip, code_path=', '.join(diff.stdout.splitlines()),
                           baseline_sha=row.get('baseline_sha') or entry['base'],
                           evidence=list(dict.fromkeys(row['evidence'] + [evidence])), park_reason='', resume='')
        row.update(train_completion=receipt, updated=data['ended'], last_progress_at=data['ended'])
        changed.add(ident)
    # Completion can depend on another accepted task. Park failed proofs to a
    # fixed point before validating anything, so iteration order cannot hide debt.
    while True:
        failed = []
        for ident in changed:
            if records[ident]['status'] == 'done':
                try:
                    _m_flow.completion(n, records[ident], records=records)
                except (ValueError, OSError):
                    failed.append(ident)
        if not failed:
            break
        for ident in failed:
            records[ident].update(status='parked', park_reason='Train landing evidence is incomplete',
                                 resume='Provide exact verified main landing evidence and resume the task')
    validate(n, records)  # no task writes until the full prospective graph passes
    for ident in sorted(changed):
        row = records[ident]
        if row['status'] == 'done':
            row['event_pending'] = 'task_landed'
            if row.get('source') == 'linear':
                row['provider_pending'] = 'completed'
        save_record(n, row)
    # Also flush pending facts from a prior interrupted invocation, after every
    # task outcome is durable. The producer deduplicates by task/evidence identity.
    for _, job in linked:
        row = read_records(n)[job['Task']]
        if row.get('event_pending'):
            emit_task_landed(inst, n, row)


def due_tasks(inst, n):
    """File-only task actions; a parked task is always visible until resumed."""
    items = []
    for row in projection(n):
        kind = None
        if row.get('provider_pending'):
            kind = 'task-provider-pending'
        elif row['status'] == 'ready' and not row['blocked_by']:
            kind = 'task-ready'
        elif row['status'] == 'blocked' and row['depends_on'] and not row['blocked_by']:
            kind = 'task-unblocked'
        elif row['status'] == 'parked':
            kind = 'task-parked'
        elif row['status'] in ('in-progress', 'in-review', 'testing'):
            at = inst.parse_time(row.get('last_progress_at') or row.get('updated'))
            if at and (inst.now_dt() - at).total_seconds() >= 900:
                kind = 'task-stalled'
        progress = inst.parse_time(row.get('last_progress_at') or row.get('created'))
        if row['status'] not in ('merged', 'done', 'dropped') and progress and (inst.now_dt() - progress).total_seconds() >= 86400:
            items.append({'node': n['id'], 'kind': 'task-aging', 'task': row['id'],
                          'episode': row['id'] + ':aging:' + (row.get('last_progress_at') or row['created']),
                          'text': row['id'] + ': reconsider the task after 24h without substantive progress'})
        if kind:
            items.append({'node': n['id'], 'kind': kind, 'task': row['id'],
                          'episode': row['id'] + ':' + row['updated'],
                          'text': row['id'] + ': ' + row['title'] + ('; resume: ' + row['resume'] if kind == 'task-parked' else '')})
    return items


def cmd_task_event(a):
    inst = _m_nodes.load()
    n = inst.node(a.node)
    try:
        print(json.dumps(event(inst, n, a.task_id, a.event_id, a.reason)))
    except (OSError, ValueError) as error:
        _m_common.die(str(error))


def cmd_task_backfill(a):
    from . import tasks_backfill as _m_backfill
    inst = _m_nodes.load()
    try:
        value = _m_backfill.import_register(inst, inst.node(a.node), a.register, a.apply) if a.register else _m_backfill.backfill(inst, inst.node(a.node), a.apply)
        print(json.dumps(value, ensure_ascii=False))
    except (OSError, ValueError) as error:
        _m_common.die(str(error))


def cmd_task_provider(a):
    from . import task_linear as _m_linear
    inst = _m_nodes.load()
    n = inst.node(a.node)
    try:
        if a.action == 'create':
            result = _m_linear.create(inst, n, a.title, a.description)
        else:
            if not provider_pull_on_wake(inst, n):
                raise ValueError('provider sync unknown')
            result = projection(n)
        print(json.dumps(result, ensure_ascii=False))
    except (OSError, ValueError, KeyError, TypeError):
        _m_common.die('External task operation failed; inspect provider configuration and retained mirror')


def provider_pull_on_wake(inst, n):
    if source(n) != 'linear':
        return True
    from . import task_linear as _m_linear
    state = 'synced'
    try:
        _m_linear.sync(inst, n)
    except (OSError, ValueError, KeyError, TypeError):
        state = 'unknown'
        print('! External task sync failed; retained mirror and pending state need repair')
    path = n['dir'] / 'task-sync.json'
    _m_common.atomic_write(path, json.dumps({'version': 1, 'state': state, 'at': inst.now()}) + '\n')
    _m_common.touch(path)
    inst.commit([], n['id'] + ': task provider reading')
    return state == 'synced'


def provider_job_state(inst, n, ident, state_type):
    if read_records(n)[ident].get('source') != 'linear':
        return
    from . import task_linear as _m_linear
    try:
        _m_linear.write_state(inst, n, ident, state_type)
        _m_linear.pull(inst, n)
    except (OSError, ValueError, KeyError, TypeError):
        print('! External task state is pending; retained for retry on the next sync')


def upsert_source_task(inst, n, source_id, title, *, kind='chore', description='', source_ref='', decisions=None, requirement=None):
    """Idempotent producer API: observations never reset existing task progress."""
    import hashlib
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError('source task needs a stable source id')
    suffix = hashlib.sha256(source_id.encode()).hexdigest()[:16].upper()
    ident = 'TASK-' + suffix
    with _m_instance.brain_lock(inst):
        existing = read_records(n)
        if ident in existing:
            if source_id not in existing[ident].get('provenance', []):
                raise ValueError('source task identity collision')
            return existing[ident]
        req_id = requirement or 'REQ-' + suffix
        if not requirement and req_id not in read_records(n, 'requirements'):
            save_record(n, {'version': VERSION, 'id': req_id, 'quote': title, 'at': inst.now(),
                            'date_basis': 'event-observed', 'ref': source_ref or source_id,
                            'created': inst.now()}, 'requirements')
        return mutate(inst, n, ident, {'title': title, 'kind': kind, 'description': description,
                                      'source': 'local', 'provenance': [source_id, source_ref or source_id],
                                      'requirement': req_id, 'decisions': decisions or []}, create=True)


def ui_tasks(inst, n):
    """Read-only snapshot, recursively screened by the node's visibility guard."""
    from . import guard as _m_guard
    try:
        requirements = read_records(n, 'requirements')
        rows = []
        for row in projection(n):
            req = requirements.get(row.get('requirement'), {})
            delivery = 'open'
            if row['status'] in ('merged', 'done'):
                from . import task_flow as _m_flow
                try:
                    _m_flow.completion(n, row)
                    delivery = 'provider' if row.get('source') == 'linear' else 'verified'
                except (OSError, ValueError):
                    delivery = 'unproven'
            from . import task_flow as _m_flow
            holds = _m_flow.holds(inst, n, row) if row['status'] == 'ready' else []
            row = {**row, 'delivery_state': delivery, 'attempt_count': len(row.get('attempts', [])),
                   'dispatch_holds': holds,
                   'ready_now': row['status'] == 'ready' and not row['blocked_by'] and not holds}
            rows.append({**row, 'request': {'quote': req.get('quote', ''), 'at': req.get('at', ''),
                                          'ref': req.get('ref', ''), 'date_basis': req.get('date_basis', 'source-timestamp')}})
        terms = _m_guard.never_terms(n)
        def screen(value):
            if isinstance(value, str):
                return _m_guard.redact(value, terms)
            if isinstance(value, list):
                return [screen(x) for x in value]
            if isinstance(value, dict):
                return {k: screen(v) for k, v in value.items()}
            return value
        return {'source': source(n), 'rows': screen(rows), 'error': ''}
    except (OSError, ValueError, TypeError, KeyError):
        return {'source': 'unknown', 'rows': [], 'error': 'Task records need repair before they can be displayed'}


def instance_root(n):
    from . import task_flow as _m_flow
    return _m_flow.instance_for(n).root


def brief_index(inst, n):
    from . import task_flow as _m_flow
    return _m_flow.brief_index(inst, n)


def reconcile_dependencies(inst, changed_node=None, event_id=None):
    from . import task_flow as _m_flow
    return _m_flow.reconcile_dependencies(inst, changed_node, event_id)


def emit_task_landed(inst, n, row):
    """Metadata-only event integration; an unavailable journal keeps retry intent."""
    import hashlib
    try:
        from . import events as _m_events
    except ImportError:
        return False
    identity = hashlib.sha256(json.dumps([row['id'], row.get('merge_sha'), row.get('completion_receipt'), row.get('provider', {}).get('updated_at')], sort_keys=True).encode()).hexdigest()
    try:
        prior = next((e for e in _m_events.facts(inst, n, row.get('job')) if
                      e.get('data', {}).get('fact_type') == 'task_landed' and e['data'].get('inputs') == identity), None)
        evidence = {'ref': row['id']}
        if row.get('merge_sha'):
            evidence['head'] = row['merge_sha']
        receipt = prior or _m_events.record_fact(inst, n, 'task_landed', job_key=row.get('job'),
                        phase='finished', state='done', inputs=identity, evidence=evidence)
        current = read_records(n)[row['id']]
        current.pop('event_pending', None)
        current['landing_event'] = receipt['ref']
        save_record(n, current)
        inst.commit([], n['id'] + ': task landing event recorded')
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def launch_plan(inst, n, task_id=None, branch=None):
    from . import task_flow as _m_flow
    return _m_flow.launch_plan(inst, n, task_id, branch)
