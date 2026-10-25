"""Task visibility, dispatch and scoped evidence. No host calls or implicit unpause."""
import datetime
import hashlib
from pathlib import Path

from . import common as _m_common
from . import instance as _m_instance
from . import tasks as _m_tasks


def instance_for(n):
    root = next((p for p in (n['dir'], *n['dir'].parents) if (p / 'instance.md').is_file()), None)
    if root is None:
        raise ValueError('task node is outside an instance')
    return _m_instance.Instance(root)


def boundary(n):
    current = n
    while current:
        if current.get('sealed'):
            return current['id']
        current = current.get('parent')
    return 'root'


def resolve(n, ref, local=None, cache=None):
    if not isinstance(ref, str):
        raise ValueError('task dependency must be an id or node:id')
    if ':' not in ref:
        _m_tasks.record_path(n, ref)
        return n, (local if local is not None else _m_tasks.read_records(n)).get(ref), ref
    node_id, ident = ref.rsplit(':', 1)
    inst = instance_for(n)
    target = next((x for x in inst.nodes() if x['id'] == node_id), None)
    if target is None or boundary(n) != boundary(target) or set(n['owners']) != set(target['owners']):
        raise ValueError('cross-node dependency is unavailable within this ownership boundary')
    _m_tasks.record_path(target, ident)
    if node_id == n['id'] and local is not None:
        rows = local
    elif cache is None:
        rows = _m_tasks.read_records(target)
    else:
        if node_id not in cache:
            cache[node_id] = _m_tasks.read_records(target)
        rows = cache[node_id]
    return target, rows.get(ident), ident


def links(row):
    return row.get('depends_on', []) + row.get('superseded_by', []) + ([row['parent']] if row.get('parent') else [])


def validate_graph(n, rows):
    visited, active, cache = set(), set(), {n['id']: rows}
    def visit(node, row):
        key = (node['id'], row['id'])
        if key in active:
            raise ValueError('task dependency, parent or successor cycle')
        if key in visited:
            return
        active.add(key)
        for ref in links(row):
            target, dep, _ = resolve(node, ref, cache.get(node['id']), cache)
            if dep is None:
                if row.get('source') != 'linear':
                    raise ValueError('task dependency or successor is missing')
            else:
                visit(target, dep)
        active.remove(key)
        visited.add(key)
    for row in rows.values():
        visit(n, row)


def blockers(n, row, rows=None, seen=None):
    result = []
    for ref in row.get('depends_on', []) + row.get('superseded_by', []):
        target, dep, _ = resolve(n, ref, rows)
        if not dep or dep.get('status') not in ('merged', 'done'):
            result.append(ref)
        elif dep.get('source') != 'linear':
            try:
                completion(target, dep, seen, rows if target['id'] == n['id'] else None)
            except (ValueError, OSError):
                result.append(ref)
    return result


def completion(n, row, seen=None, records=None):
    """A code SHA, operational receipt and report artifact prove different work."""
    seen = set(seen or ())
    identity = (n['id'], row['id'])
    if identity in seen:
        raise ValueError('completion dependency cycle')
    seen.add(identity)
    if row.get('source') == 'linear' and row.get('provider', {}).get('state_type') == 'completed':
        return
    kind = row.get('completion_kind', 'merge')
    if not row.get('evidence'):
        raise ValueError('completion requires recorded evidence')
    if kind == 'merge':
        head, base = row.get('merge_sha', ''), row.get('baseline_sha', '')
        paths = [p.strip() for p in row.get('code_path', '').split(',') if p.strip()]
        scopes = row.get('scope', [])
        if not _m_tasks.SHA.fullmatch(head) or not _m_tasks.SHA.fullmatch(base) or head == base or not paths or not scopes:
            raise ValueError('code completion requires a changed baseline, delivering SHA, code paths and scope')
        home = n.get('home_path')
        if not home or _m_common.git('merge-base', '--is-ancestor', head, 'refs/heads/main', cwd=home).returncode:
            raise ValueError('delivering SHA must be on intended local main')
        if _m_common.git('merge-base', '--is-ancestor', base, head, cwd=home).returncode:
            raise ValueError('completion baseline is not an ancestor of delivery')
        changed = set(_m_common.git('diff', '--name-only', base, head, cwd=home).stdout.splitlines())
        if any(p not in changed for p in paths):
            raise ValueError('delivery code paths must actually change from the task baseline')
        for scope in scopes:
            if not any(p == scope or p.startswith(scope.rstrip('/') + '/') for p in paths):
                raise ValueError('delivery does not cover every declared code scope')
    elif kind in ('ops', 'report'):
        if row.get('status') != 'done':
            raise ValueError('operations and reports complete as done, not merged')
        receipt = row.get('completion_receipt')
        if not isinstance(receipt, dict) or not receipt.get('ref') or not receipt.get('scope') or receipt.get('result') != 'passed':
            raise ValueError('completion requires a scoped passed receipt with source reference')
        if not instance_for(n).parse_time(receipt.get('at')):
            raise ValueError('completion receipt requires an absolute timestamp')
        if kind == 'report':
            relative = receipt.get('artifact', '')
            path = n['dir'] / relative
            if not relative or Path(relative).is_absolute() or '..' in Path(relative).parts or path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(n['dir'].resolve()):
                raise ValueError('report artifact must be an existing node-local regular file')
            if hashlib.sha256(path.read_bytes()).hexdigest() != receipt.get('sha256') or not receipt.get('version'):
                raise ValueError('report completion requires the exact artifact digest and version')
    else:
        raise ValueError('unknown completion kind')
    if row.get('unresolved_dependencies') or blockers(n, row, records, seen):
        raise ValueError('unfinished dependencies or successors prevent completion')


def holds(inst, n, row=None):
    reasons = []
    current = n
    while current:
        if 'state:paused' in _m_common.lst(current['fm'].get('labels')):
            reasons.append('node or ancestor is explicitly paused')
        if current['fm'].get('tasks.hold'):
            reasons.append('node or ancestor task hold remains active')
        current = current.get('parent')
    if _m_tasks.source(n) == 'linear':
        path = n['dir'] / 'task-sync.json'
        if path.exists():
            import json
            try:
                if json.loads(path.read_text()).get('state') != 'synced':
                    reasons.append('external task sync is unknown')
            except (OSError, ValueError, AttributeError):
                reasons.append('external task sync is unknown')
    if row and row.get('hold'):
        reasons.append('task hold remains active')
    from . import machine as _m_machine
    sample = _m_machine.machine_samples(inst)
    if sample and sample.get('episode'):
        reasons.append('native machine hold remains active')
    capacity = n['fm'].get('tasks.capacity')
    if capacity:
        try:
            limit = int(capacity)
            if limit < 1:
                raise ValueError()
        except (TypeError, ValueError):
            raise ValueError('tasks.capacity must be a positive integer')
        from . import jobs as _m_jobs
        records = _m_tasks.read_records(n)
        active_states = ('in-progress', 'in-review', 'testing')
        live = [r for r in _m_jobs.read_jobs(n) or [] if not r.get('Ended') and
                (not r.get('Task') or records.get(r['Task'], {}).get('status') in active_states)]
        identities = {_m_jobs.job_key(r) for r in live}
        identities.update(a['id'] for task in records.values() if task['status'] in active_states
                          for a in task.get('attempts', []) if a.get('state') == 'open' and a.get('id'))
        if len(identities) >= limit:
            reasons.append('configured active-attempt capacity is occupied')
    return reasons


def park_fields(inst):
    return {'review_at': (inst.now_dt() + datetime.timedelta(days=1)).isoformat(),
            'next_action': 'Repair the blocker, then explicitly resume the task'}


def brief_index(inst, n):
    rows = [r for r in _m_tasks.projection(n) if r['status'] not in ('merged', 'done', 'dropped')]
    if not rows:
        return []
    parked = sum(r['status'] == 'parked' for r in rows)
    lines = ['## Open task obligations', '', f'{len(rows)} open; {parked} parked. Attempts do not replace these obligations.', '']
    lines += [r['id'] + ' [' + r['status'] + ']: ' + r['title'] + '; next: ' +
              (r.get('next_action') or r.get('resume') or 'Resolve dependencies and follow build order') for r in rows]
    return lines + ['']


def reconcile_dependencies(inst, changed_node=None, event_id=None):
    """Landing-event projection; no dispatch, unpause, provider call or wake."""
    changed = []
    with _m_instance.brain_lock(inst):
        for n in inst.nodes():
            rows = _m_tasks.read_records(n)
            for row in rows.values():
                if row.get('event_pending'):
                    _m_tasks.emit_task_landed(inst, n, row)
            for ident, row in rows.items():
                if row.get('unresolved_dependencies') or row.get('status') != 'blocked' or not row.get('depends_on') or row.get('park_reason') and row.get('strikes'):
                    continue
                if changed_node and not any(ref.startswith(changed_node + ':') or ':' not in ref and n['id'] == changed_node for ref in row['depends_on']):
                    continue
                if holds(inst, n, row) or blockers(n, row, rows):
                    continue
                updated = {**row, 'status': 'ready', 'updated': inst.now(),
                           'dependency_event': event_id or 'reconcile', 'next_action': 'Start the task in build order'}
                candidate = {**rows, ident: updated}
                _m_tasks.validate(n, candidate)
                _m_tasks.save_record(n, updated)
                rows[ident] = updated
                changed.append({'node': n['id'], 'task': ident})
        if changed:
            inst.commit([], 'Reconcile task dependency landings')
    return changed


def attempt(inst, n, ident, attempt_id, *, branch='', head='', source_ref='', close=False):
    """Register plain-Git or operational attempts without executing host actions."""
    if not attempt_id.strip() or not source_ref.strip():
        raise ValueError('attempt needs a stable id and source receipt reference')
    with _m_instance.brain_lock(inst):
        rows = _m_tasks.read_records(n)
        row = dict(rows[ident])
        attempts = [dict(a) for a in row.get('attempts', [])]
        prior = next((a for a in attempts if a.get('id') == attempt_id), None)
        if prior and not close:
            return row
        if close:
            if not prior:
                raise ValueError('attempt not found')
            if prior.get('state') == 'closed':
                return row
            prior.update(state='closed', ended=inst.now())
            row.update(status='blocked', next_action='Reconcile attempt evidence before resuming')
        else:
            _m_tasks.prepare_start(n, ident, branch)
            if row.get('completion_kind') == 'merge':
                if not branch or not _m_tasks.SHA.fullmatch(head):
                    raise ValueError('plain-Git attempt needs a branch and exact starting head')
                actual = _m_common.git('rev-parse', '--verify', 'refs/heads/' + branch, cwd=n['home_path']).stdout.strip()
                if actual != head:
                    raise ValueError('plain-Git attempt head does not match the branch')
                row.setdefault('baseline_sha', head)
            attempts.append({'id': attempt_id, 'branch': branch, 'head': head, 'ref': source_ref,
                             'state': 'open', 'started': inst.now()})
            row.update(status='in-progress')
            row['aliases'] = list(dict.fromkeys(row.get('aliases', []) + ([branch] if branch else [])))
        row.update(attempts=attempts, updated=inst.now(), last_progress_at=inst.now())
        rows[ident] = row
        _m_tasks.validate(n, rows)
        _m_tasks.save_record(n, row)
        inst.commit([], n['id'] + ': task attempt ' + ident)
        return row


def alias(inst, n, ident, old, new):
    with _m_instance.brain_lock(inst):
        rows = _m_tasks.read_records(n)
        row = dict(rows[ident])
        aliases = row.get('aliases', [])
        if old not in aliases or not new.strip():
            raise ValueError('rename must reference a known task branch alias')
        if any(new in r.get('aliases', []) for r in rows.values() if r['id'] != ident):
            raise ValueError('alias already belongs to another task')
        row['aliases'] = list(dict.fromkeys(aliases + [new]))
        row['updated'] = inst.now()
        _m_tasks.save_record(n, row)
        inst.commit([], n['id'] + ': task branch alias ' + ident)
        return row


def cmd_task_flow(a):
    import json
    from . import nodes as _m_nodes
    inst = _m_nodes.load()
    n = inst.node(a.node)
    try:
        if a.action == 'attempt':
            value = attempt(inst, n, a.task_id, a.attempt_id, branch=a.branch, head=a.head, source_ref=a.ref, close=a.close)
        elif a.action == 'alias':
            value = alias(inst, n, a.task_id, a.old, a.new)
        elif a.action == 'launch-plan':
            value = launch_plan(inst, n, a.task_id, a.branch)
        elif a.action == 'reconcile':
            value = reconcile_dependencies(inst, a.node, a.event_id)
        else:
            value = brief_index(inst, n)
        print(json.dumps(value, ensure_ascii=False))
    except (OSError, ValueError, KeyError, TypeError) as error:
        _m_common.die(str(error))


def register(actions):
    p = actions.add_parser('launch-plan', help='contract-gated native launch commands; no host action')
    p.add_argument('node'); p.add_argument('task_id', nargs='?'); p.add_argument('--branch')
    p.set_defaults(f=cmd_task_flow)
    p = actions.add_parser('attempt', help='record an attempt for plain-Git or operational work')
    p.add_argument('node'); p.add_argument('task_id'); p.add_argument('attempt_id')
    p.add_argument('--branch', default=''); p.add_argument('--head', default=''); p.add_argument('--ref', required=True)
    p.add_argument('--close', action='store_true'); p.set_defaults(f=cmd_task_flow)
    p = actions.add_parser('alias', help='retain an existing task through a branch rename')
    for key in ('node', 'task_id', 'old', 'new'): p.add_argument(key)
    p.set_defaults(f=cmd_task_flow)
    p = actions.add_parser('reconcile', help='reevaluate dependencies from retained landing evidence')
    p.add_argument('node'); p.add_argument('--event-id'); p.set_defaults(f=cmd_task_flow)
    p = actions.add_parser('brief', help='all open and parked obligations for a cold handover')
    p.add_argument('node'); p.set_defaults(f=cmd_task_flow)


def launch_plan(inst, n, task_id=None, branch=None):
    """Native coordinator hook: explicit task authority, never Markdown authority.

    This returns existing native commands. The lifecycle/host adapter owns actual
    launch and rebinding; generating a plan never creates a thread or worktree.
    """
    from . import jobs as _m_jobs
    rows = _m_tasks.read_records(n)
    if not task_id:
        task_id = next((r['id'] for r in _m_tasks.projection(n) if r['status'] == 'ready' and
                        not r['blocked_by'] and not holds(inst, n, r)), None)
    if not task_id:
        return None
    _m_tasks.prepare_start(n, task_id, branch or '')
    row = rows[task_id]
    import re
    if not re.fullmatch(r'(?:repo:D[0-9]+|private:D-[A-Za-z0-9-]+)', row.get('authority', '')):
        raise ValueError('task launch requires its explicit decision authority')
    relative = row.get('contract', '')
    path = n['dir'] / relative
    if not relative or Path(relative).is_absolute() or '..' in Path(relative).parts or path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(n['dir'].resolve()):
        raise ValueError('task launch requires its node-local contract file')
    text = path.read_text()
    if _m_jobs.contract_problems(text):
        raise ValueError('task launch contract is incomplete')
    if row.get('completion_kind') == 'merge' and not row.get('scope'):
        raise ValueError('task launch requires explicit code scope')
    selected_branch = branch or 'job/task-' + task_id.lower()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./-]*', selected_branch):
        raise ValueError('invalid launch branch')
    commands = [
        ['brain', 'wt', 'add', n['id'], selected_branch, '--task-id', task_id],
        ['brain', 'jobs', n['id'], '--branch', selected_branch, '--contract-file', str(path)],
        ['brain', 'brief', n['id'], '--branch', selected_branch, '--task', row['title'],
         '--why-separate', 'Implement the scoped task under its explicit contract'],
    ]
    return {'task': task_id, 'node': n['id'], 'branch': selected_branch, 'authority': row['authority'],
            'contract': relative, 'contract_sha256': hashlib.sha256(text.encode()).hexdigest(),
            'commands': commands, 'host_action': 'launch the generated native brief through the configured lifecycle adapter'}
