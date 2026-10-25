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
LISTS = {'depends_on', 'decisions', 'evidence', 'provenance'}


def record_path(n, ident, folder='tasks'):
    if not isinstance(ident, str) or not ID.fullmatch(ident):
        raise ValueError('record id must contain only letters, numbers, underscores and hyphens')
    base = n['dir'] / folder
    path = base / (ident + '.md')
    if base.is_symlink() or path.is_symlink() or (path.exists() and path.stat().st_nlink != 1):
        raise ValueError('record folders and files must not be links')
    return path


def read_records(n, folder='tasks'):
    base = n['dir'] / folder
    if base.is_symlink():
        raise ValueError('record folder must not be a link')
    records = {}
    for path in sorted(base.glob('*.md')):
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


def validate(n, records):
    jobs = None
    for ident, row in records.items():
        record_path(n, ident)
        if not isinstance(row.get('title'), str) or not row['title'].strip():
            raise ValueError(ident + ': title required')
        if row.get('status') not in STATUSES or row.get('kind') not in KINDS:
            raise ValueError(ident + ': unknown status or kind')
        if row.get('priority') not in ('P0', 'P1', 'P2', 'P3'):
            raise ValueError(ident + ': priority must be P0-P3')
        for key in ('order', 'strikes'):
            if type(row.get(key)) is not int or row[key] < 0:
                raise ValueError(ident + ': invalid ' + key)
        for key in LISTS:
            if not isinstance(row.get(key), list) or any(not isinstance(x, str) or not x.strip() for x in row[key]):
                raise ValueError(ident + ': invalid ' + key)
        deps = row['depends_on'] + ([row['parent']] if row.get('parent') else [])
        if any(d not in records for d in deps):
            raise ValueError(ident + ': missing dependency or parent')
        for link in row['decisions']:
            if not re.fullmatch(r'(?:repo:D[0-9]+|private:D-[A-Za-z0-9-]+)', link):
                raise ValueError('decision links use repo:D123 or private:D-123')
        if row.get('requirement') and row['requirement'] not in read_records(n, 'requirements'):
            raise ValueError(ident + ': requirement not found')
        if row['status'] in ('in-progress', 'in-review', 'testing'):
            if jobs is None:
                from . import jobs as _m_jobs
                jobs = _m_jobs.read_jobs(n) or []
            if not any(r.get('Job') == row.get('branch') and not r.get('Ended') and
                       _m_jobs.job_key(r) == row.get('job') for r in jobs):
                raise ValueError(ident + ': active status requires a live ledger job and branch')
        if row['status'] in ('merged', 'done'):
            if not SHA.fullmatch(row.get('merge_sha', '')) or not row.get('code_path') or not row['evidence']:
                raise ValueError(ident + ': completion requires merge_sha, code_path and evidence')
            home = n.get('home_path')
            if not home or _m_common.git('merge-base', '--is-ancestor', row['merge_sha'], 'refs/heads/main', cwd=home).returncode:
                raise ValueError(ident + ': merge_sha must be on local main')
        if row['status'] == 'parked' and (not row.get('park_reason') or not row.get('resume')):
            raise ValueError(ident + ': parked tasks require a reason and resume condition')
        if row['status'] == 'dropped' and not row.get('drop_reason'):
            raise ValueError(ident + ': dropped tasks require an explicit reason')
    visited, visiting = set(), set()
    def visit(ident):
        if ident in visiting:
            raise ValueError('task dependency or parent cycle')
        if ident in visited:
            return
        visiting.add(ident)
        row = records[ident]
        for dep in row['depends_on'] + ([row['parent']] if row.get('parent') else []):
            visit(dep)
        visiting.remove(ident)
        visited.add(ident)
    for ident in records:
        visit(ident)


def mutate(inst, n, ident, changes, create=False, provider=False):
    with _m_instance.brain_lock(inst):
        rows = read_records(n)
        if create == (ident in rows):
            raise ValueError('task already exists' if create else 'task not found')
        changes = normalize(changes)
        if source(n) == 'linear' and not provider and (create and changes.get('source') != 'local' or
                not create and rows[ident].get('source') == 'linear' and
                set(changes) & {'title', 'description', 'status', 'priority', 'owner', 'depends_on'}):
            raise ValueError('Linear owns external task fields; use task sync or the provider adapter')
        if create:
            row = {'version': VERSION, 'id': ident, 'title': '', 'kind': 'feature', 'status': 'backlog',
                   'priority': 'P2', 'order': 0, 'owner': ','.join(n.get('owners', [])), 'depends_on': [], 'decisions': [],
                   'evidence': [], 'provenance': [], 'strikes': 0, 'source': 'local', 'created': inst.now()}
        else:
            row = dict(rows[ident])
        row.update(changes, updated=inst.now())
        rows[ident] = row
        validate(n, rows)
        save_record(n, row)
        inst.commit([], n['id'] + ': task ' + ident)
        return row


def projection(n):
    rows = read_records(n)
    result = []
    for row in sorted(rows.values(), key=lambda r: (r.get('order', 0), r.get('priority', 'P2'), r['id'])):
        blockers = [d for d in row.get('depends_on', []) if d not in rows or rows[d].get('status') not in ('done', 'merged')]
        result.append({**row, 'blocked_by': blockers,
                       'blocks': [r['id'] for r in rows.values() if row['id'] in r.get('depends_on', [])]})
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


def cmd_task(a):
    inst = _m_nodes.load()
    n = inst.node(a.node)
    try:
        if a.action in ('add', 'set'):
            changes = {}
            for item in a.fields:
                key, sep, value = item.partition('=')
                if not sep or key in changes:
                    raise ValueError('fields must be unique key=value pairs')
                changes[key] = value
            if a.title is not None:
                changes['title'] = a.title
            if a.status is not None:
                changes['status'] = a.status
            if a.owner is not None:
                changes['owner'] = a.owner
            rows = [mutate(inst, n, a.task_id, changes, create=a.action == 'add')]
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
                rows = [r for r in rows if r['status'] == 'ready' and not r['blocked_by']][:1]
            if a.action == 'check':
                validate(n, read_records(n))
                print('tasks: valid')
                return
        if a.json:
            print(json.dumps(rows, ensure_ascii=False))
        else:
            for row in rows:
                if a.action == 'graph':
                    print(row['id'] + ' -> ' + ', '.join(row.get('depends_on', [])))
                else:
                    print(f"{row['id']} [{row['status']}] {row['title']}" +
                          (' · blocked by ' + ', '.join(row['blocked_by']) if row.get('blocked_by') else ''))
    except (OSError, ValueError) as error:
        _m_common.die(str(error))


def register(sub):
    parser = sub.add_parser('task', help='node-local tasks, dependencies and completion evidence')
    actions = parser.add_subparsers(dest='action', required=True)
    from . import task_requirements as _m_requirements
    from . import tasks_backfill as _m_backfill
    _m_requirements.register(actions)
    migration = actions.add_parser('migrate', help='additively migrate local task sources after install/upgrade')
    migration.add_argument('node', nargs='?')
    migration.add_argument('--all', action='store_true')
    migration.set_defaults(f=_m_backfill.cmd_task_migrate)
    fill = actions.add_parser('backfill', help='import node-local promises without changing source files')
    fill.add_argument('node')
    fill.add_argument('--apply', action='store_true')
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
        p.add_argument('node')
        if action in ('add', 'set', 'show'):
            p.add_argument('task_id')
        if action in ('add', 'set'):
            p.add_argument('fields', nargs='*')
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
    if any(rows.get(d, {}).get('status') not in ('merged', 'done') for d in row['depends_on']):
        raise ValueError('task dependencies are unfinished')
    validate(n, rows)


def job_started(inst, n, row):
    ident = row.get('Task')
    if ident:
        from . import jobs as _m_jobs
        mutate(inst, n, ident, {'status': 'in-progress', 'job': _m_jobs.job_key(row), 'branch': row['Job']}, provider=True)


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
    packet = _m_sync.integration_packet(inst, n, row) or {}
    changes = {'status': 'parked', 'park_reason': row.get('Cause') or 'Job closed without proven task completion',
               'resume': 'Start a new task job or supply verified main landing evidence'}
    head = row.get('Head commit') or packet.get('head', '')
    if row.get('Outcome') == 'merged' and packet.get('verify_result') == 0 and head == packet.get('head'):
        base = packet.get('integration_base') or packet.get('main')
        paths = _m_common.git('diff', '--name-only', base or head + '^', head, cwd=n['home_path']).stdout.splitlines()
        if paths:
            changes = {'status': 'merged', 'merge_sha': head, 'code_path': ', '.join(paths),
                       'evidence': ['Verified job ' + row['Job'] + ' at ' + inst.now()], 'park_reason': '', 'resume': ''}
    # Existing task proof remains unchanged if the job endpoint is not on main.
    try:
        mutate(inst, n, ident, changes, provider=True)
    except ValueError:
        mutate(inst, n, ident, {'status': 'parked', 'park_reason': 'Landing evidence is incomplete',
                              'resume': 'Provide the exact verified commit already on main'}, provider=True)


def due_tasks(inst, n):
    """File-only task actions; a parked task is always visible until resumed."""
    items = []
    for row in projection(n):
        kind = None
        if row['status'] == 'ready' and not row['blocked_by']:
            kind = 'task-ready'
        elif row['status'] == 'blocked' and row['depends_on'] and not row['blocked_by']:
            kind = 'task-unblocked'
        elif row['status'] == 'parked':
            kind = 'task-parked'
        elif row['status'] in ('in-progress', 'in-review', 'testing'):
            at = inst.parse_time(row.get('updated'))
            if at and (inst.now_dt() - at).total_seconds() >= 900:
                kind = 'task-stalled'
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
        print(json.dumps(_m_backfill.backfill(inst, inst.node(a.node), a.apply), ensure_ascii=False))
    except (OSError, ValueError) as error:
        _m_common.die(str(error))
