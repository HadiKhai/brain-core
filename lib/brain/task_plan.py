"""Apply reviewed, source-bound housekeeping operations as one native commit."""
import copy
import difflib
import hashlib
import json
from pathlib import Path

from . import common

events = common.LazyModule(__package__, 'events')
guard = common.LazyModule(__package__, 'guard')
instance = common.LazyModule(__package__, 'instance')
jobs = common.LazyModule(__package__, 'jobs')
nodes = common.LazyModule(__package__, 'nodes')
status = common.LazyModule(__package__, 'status')
tasks = common.LazyModule(__package__, 'tasks')


def digest(value):
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def resolve_identity(values, expected, what):
    matches = [value for value in values if digest(value) == expected]
    if len(matches) != 1:
        raise ValueError(what + ' identity is missing or ambiguous')
    return matches[0]


def operations(plan, name):
    value = plan.get(name, [])
    if not isinstance(value, list) or any(not isinstance(op, dict) for op in value):
        raise ValueError(name + ' must be a list of operations')
    return value


def target_id(n, target, records, create=False):
    if not isinstance(target, dict) or target.get('node_identity_sha256') != digest(n['id']):
        raise ValueError('operation is outside the selected node')
    if create:
        ident = target.get('create_id')
        tasks.record_path(n, ident)
        if digest(ident) != target.get('task_identity_sha256'):
            raise ValueError('created task identity does not match create_id')
        return ident
    return resolve_identity(records, target.get('task_identity_sha256'), 'task')


def check_before(n, ident, before, original, created):
    if not isinstance(before, dict):
        raise ValueError('operation requires a before precondition')
    if before.get('must_be_absent') is True or before.get('must_be_created_in_this_batch') is True:
        if ident in original or (before.get('must_be_created_in_this_batch') and ident not in created):
            raise ValueError('task absence/creation precondition is stale')
        return
    path = tasks.record_path(n, ident)
    if ident not in original or digest(path.read_bytes()) != before.get('record_sha256'):
        raise ValueError('task record precondition is stale')
    for key in ('status', 'depends_on'):
        if key in before and original[ident].get(key) != before[key]:
            raise ValueError('task ' + key + ' precondition is stale')


def delivery_fields(n, row, proof):
    if not isinstance(proof, dict):
        raise ValueError('delivery must be an object')
    base, head = proof.get('baseline_sha', ''), proof.get('merge_sha', '')
    if not tasks.SHA.fullmatch(base) or not tasks.SHA.fullmatch(head) or not n.get('home_path'):
        raise ValueError('delivery requires full commits and an intended home')
    changed = common.git('diff', '--name-only', '-z', base, head, cwd=n['home_path'])
    if changed.returncode:
        raise ValueError('delivery diff is unavailable')
    paths = set(changed.stdout.rstrip('\0').split('\0')) - {''}
    paths.update(row.get('scope', []))
    result = {'baseline_sha': base, 'merge_sha': head}
    for key, field in (('code_paths', 'code_path'), ('scope', 'scope')):
        refs = proof.get(key)
        if not isinstance(refs, list) or not refs or any(not isinstance(ref, dict) for ref in refs):
            raise ValueError('delivery requires hashed paths and scope')
        resolved = [resolve_identity(paths, ref.get('sha256'), 'delivery path') for ref in refs]
        result[field] = ','.join(resolved) if field == 'code_path' else resolved
    return result


def terminal_retirement(inst, n, row):
    """Use the native lifecycle reader; absence or stale prose is never proof."""
    from . import host_instances, host_lifecycle, host_rpc
    rpc = host_instances.routed_host_rpc(inst, n['id'], host_rpc.call)
    with instance.released(inst, n, extra=lambda: tasks.mutation_paths(inst, n['id'])):
        _, inventory, callers = host_lifecycle.lifecycle_inventory(n, rpc)
        refs = jobs.bound_worker_threads(n, row['Job'])
        group = set()
        for reference, _ in refs:
            identities = jobs.thread_reference_ids(reference)
            if len(identities) != 1:
                raise ValueError('retirement requires exact worker ownership')
            group.update(identities)
        if not group or not group.issubset(inventory):
            raise ValueError('retirement requires exhaustive fresh host evidence')
        for _ in range(len(inventory)):
            children = {ident for ident, thread in inventory.items() if thread.get('parentThreadId') in group}
            if children.issubset(group):
                break
            group.update(children)
        protected = set(callers)
        for node in inst.nodes():
            protected.update(jobs.thread_reference_ids(node['local'].get('coordinator', '')))
            ledger = jobs.read_jobs(node)
            if ledger is None:
                raise ValueError('retirement ownership is unreadable')
            for other in ledger:
                if not other.get('Ended') and (node['id'], jobs.job_key(other)) != (n['id'], jobs.job_key(row)):
                    for reference, _ in jobs.bound_worker_threads(node, other['Job']):
                        protected.update(jobs.thread_reference_ids(reference))
        if group & protected or any(host_lifecycle.terminal_host_thread(rpc, ident) is None for ident in sorted(group)):
            raise ValueError('retirement has live, pending, protected or unknown workers')


def prepare_jobs(inst, n, plan, records):
    ops = operations(plan, 'stale_job_operations')
    if not ops:
        return [], []
    ledger = jobs.ledger_rows(n)
    if ledger is None:
        raise ValueError('job ledger is unreadable')
    ledger = copy.deepcopy(ledger)
    closed, seen = [], set()
    for op in ops:
        if op.get('action') != 'close' or op.get('host_settlement') is not False:
            raise ValueError('only native ledger close is supported; host settlement is separate')
        key = op.get('job_key')
        matches = [row for row in ledger if jobs.job_key(row) == key]
        if key in seen or len(matches) != 1:
            raise ValueError('duplicate or ambiguous job identity')
        seen.add(key)
        row = matches[0]
        if (digest(row['Job']) != op.get('branch_identity_sha256') or
                row.get('Started') != op.get('expected_started') or
                row.get('Ended') or op.get('expected_open') is not True):
            raise ValueError('job incarnation precondition is stale')
        home = n.get('home_path')
        head, main = op.get('expected_head', ''), op.get('main_delivery', '')
        if not home or not tasks.SHA.fullmatch(head) or not tasks.SHA.fullmatch(main):
            raise ValueError('job close requires source delivery proof')
        tip = common.git('rev-parse', '--verify', 'refs/heads/' + row['Job'] + '^{commit}', cwd=home)
        if tip.returncode or tip.stdout.strip() != head:
            raise ValueError('job head precondition is stale')
        for old, new in ((head, main), (main, 'refs/heads/main')):
            if common.git('merge-base', '--is-ancestor', old, new, cwd=home).returncode:
                raise ValueError('job delivery is not on intended main')
        path = op.get('code_path')
        if not isinstance(path, str) or not path or path.startswith('-') or ':' in path:
            raise ValueError('job close requires a source path')
        if common.git('cat-file', '-e', main + ':' + path, cwd=home).returncode:
            raise ValueError('job delivery source path is unavailable')
        base = row.get('Base commit') or n['local'].get('worktrees', {}).get(row['Job'], {}).get('start')
        if not isinstance(base, str) or not tasks.SHA.fullmatch(base):
            raise ValueError('job close requires a retained source baseline')
        if base == head:
            terminal_retirement(inst, n, row)
        else:
            changed = common.git('diff', '--name-only', '-z', base, head, cwd=home)
            if (common.git('merge-base', '--is-ancestor', base, head, cwd=home).returncode or
                    changed.returncode or path not in changed.stdout.split('\0')):
                raise ValueError('job delivery path does not change from its retained baseline')
        row.update(jobs.prepare_ledger_close(inst, n, row, op.get('reason')))
        prepared = tasks.prepare_job_closed(inst, n, row, records)
        if prepared:
            current, changes = prepared
            if current.get('source') != 'local':
                raise ValueError('provider-owned job tasks require their provider workflow')
            records[current['id']] = current
            records[current['id']], _ = tasks.prepare_record(inst, n, current['id'], changes, records, provider=True)
        closed.append(row)
    return ledger, closed


class Prepared(dict):
    """Prepared bytes plus the mutable source generations consumed by validation."""
    def fresh(self, inst):
        return all(instance.state_signature(inst, nodes, extra=paths) == generation
                   for nodes, paths, generation in self.guards)


def source_paths(inst, n, plan):
    selected = list(inst.nodes())
    paths = []
    for node in selected:
        paths.extend(node['dir'] / name for name in ('tasks', 'requirements', 'job-ledger.md',
            'status.md', 'job-trains', 'job-handbacks', 'job-attempts'))
        if node.get('home_path'):
            paths.extend(instance.git_state_paths(node['home_path'], task_proof=True))
    keys = {op.get('job_key') for op in operations(plan, 'stale_job_operations')}
    if keys and n.get('home_path'):
        refs = {'refs/heads/' + row['Job'] for row in jobs.read_jobs(n) or [] if jobs.job_key(row) in keys}
        paths.extend(instance.git_state_paths(n['home_path'], refs=refs, include_head=False))
    return selected, paths


def artifact_paths(inst, n, records):
    paths = []
    for node in inst.nodes():
        rows = records if node['id'] == n['id'] else tasks.read_records(node)
        for row in rows.values():
            receipt = row.get('completion_receipt')
            relative = receipt.get('artifact') if isinstance(receipt, dict) else None
            if isinstance(relative, str) and relative and not Path(relative).is_absolute() and '..' not in Path(relative).parts:
                path = node['dir'] / relative
                if not path.is_dir():
                    paths.extend((path, path.resolve()))
    return paths


def prepare(inst, n, plan):
    if not isinstance(plan, dict) or type(plan.get('version')) is not int or plan['version'] != 1:
        raise ValueError('unsupported housekeeping plan version')
    if not isinstance(plan.get('batch_id'), str) or not plan['batch_id'].strip():
        raise ValueError('plan requires a batch_id')
    # Peer proposals are reference material, never authority to write another node.
    if any(op.get('apply_here') is not False for op in operations(plan, 'peer_task_plans')):
        raise ValueError('peer proposals must remain apply_here=false')
    selected, paths = source_paths(inst, n, plan)
    generation = instance.state_signature(inst, selected, extra=paths)
    original = tasks.read_records(n)
    records = copy.deepcopy(original)
    requirements = tasks.read_records(n, 'requirements')
    pending_requirements = {}
    for op in operations(plan, 'requirement_operations'):
        if op.get('action') != 'requirement-add' or op.get('node_identity_sha256') != digest(n['id']):
            raise ValueError('unsupported requirement operation or node identity')
        ident = op.get('id')
        tasks.record_path(n, ident, 'requirements')
        quote, at, ref = op.get('quote'), op.get('at'), op.get('ref')
        if (not ident.startswith('REQ-') or not isinstance(quote, str) or not quote.strip() or
                not inst.parse_time(at) or not isinstance(ref, str) or not ref.strip()):
            raise ValueError('requirement needs an exact quote, absolute date and source')
        if ident in pending_requirements:
            raise ValueError('duplicate requirement operation')
        existing = requirements.get(ident)
        if existing and any(existing.get(key) != op[key] for key in ('quote', 'at', 'ref')):
            raise ValueError('requirement identity precondition is stale')
        pending_requirements[ident] = existing or {
            'version': tasks.VERSION, 'id': ident, 'quote': quote, 'at': at, 'ref': ref, 'created': inst.now()}
    requirements.update(pending_requirements)
    ledger, closed = prepare_jobs(inst, n, plan, records)
    changed = {ident for ident in records if records[ident] != original[ident]}
    created, seen = set(), set()
    for op in operations(plan, 'core_task_operations'):
        if op.get('apply_here') is not True or op.get('action') not in ('add', 'set'):
            raise ValueError('task operations require add/set and explicit apply_here=true')
        if op.get('late_bound_delivery') or op.get('application_hold') or op.get('extra_preconditions'):
            raise ValueError('unresolved task authority/preconditions; bind a fresh reviewed plan first')
        create = op['action'] == 'add'
        ident = target_id(n, op.get('target'), records, create)
        if ident in seen:
            raise ValueError('duplicate task operation')
        seen.add(ident)
        check_before(n, ident, op.get('before'), original, created)
        if create and op['before'].get('must_be_absent') is not True:
            raise ValueError('task creation requires must_be_absent')
        changes = tasks.normalize(op.get('fields', {}))
        if 'delivery' in op:
            changes.update(delivery_fields(n, records.get(ident, {}), op['delivery']))
        records[ident], _ = tasks.prepare_record(inst, n, ident, changes, records, create=create)
        changed.add(ident)
        if create:
            created.add(ident)
    seen = set()
    for op in operations(plan, 'dependency_operations'):
        ident = target_id(n, op.get('target'), records)
        if ident in seen:
            raise ValueError('duplicate dependency operation')
        seen.add(ident)
        check_before(n, ident, op.get('before'), original, created)
        refs = op.get('depends_on_refs')
        unresolved = op.get('unresolved_prose', [])
        if not isinstance(refs, list) or not isinstance(unresolved, list):
            raise ValueError('dependency operations require reference lists')
        dependencies = []
        for ref in refs:
            # Only native, same-node identities; displays never become executable IDs.
            dependencies.append(target_id(n, ref, records))
        records[ident], _ = tasks.prepare_record(inst, n, ident, {
            'depends_on': dependencies, 'unresolved_dependencies': unresolved}, records)
        changed.add(ident)
    proof_paths = artifact_paths(inst, n, records)
    proof_generation = instance.state_signature(inst, selected, extra=proof_paths)
    for ident in sorted(changed):
        tasks.validate(n, records, selected=ident, requirements=requirements)
    writes = {}
    for ident in sorted(changed):
        writes[tasks.record_path(n, ident)] = tasks.record_text(records[ident])
    for ident, row in pending_requirements.items():
        if ident not in tasks.read_records(n, 'requirements'):
            writes[tasks.record_path(n, ident, 'requirements')] = tasks.record_text(row)
    if closed:
        writes[n['dir'] / jobs.JOBS_FILE] = jobs.jobs_text(inst, n, ledger)
        active = status.active_work(n, include_closed=True)
        for row in closed:
            matches = [cells for cells in active if cells and cells[0].replace('`', '').split()[:1] == [row['Job']]]
            if len(matches) == 1 and len(matches[0]) >= 4 and sum(r['Job'] == row['Job'] for r in ledger) == 1:
                matches[0][3] = row['Outcome']
        path = n['dir'] / 'status.md'
        if path.exists():
            with path.open(newline='') as stream:
                writes[path] = events.active_table_text(stream.read(), {'active': active})
    result = Prepared()
    for path, text in writes.items():
        common.safe_target(path)
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError('batch targets must not be links')
        guard.no_terms_in(n, text, 'housekeeping output')
        before = path.read_text() if path.exists() else ''
        if before != text:
            result[path] = (before, text)
    result.guards = [(selected, paths, generation), (selected, proof_paths, proof_generation)]
    return result


def stable_preparation(inst, n, plan):
    # A forward main movement can remain valid. Recompute all semantic proof
    # once, rather than adopting stale results or using a global HEAD fence.
    for _ in range(2):
        result = prepare(inst, n, plan)
        if result.fresh(inst):
            return result
    raise ValueError('consumed housekeeping state changed during validation; refresh the plan')


def apply_plan(inst, node_id, plan, dry_run=False):
    with instance.brain_lock(inst):
        current = instance.Instance(inst.root)
        n = current.node(node_id)
        if common.outward_links(current.root):
            raise ValueError('unsafe instance links; no housekeeping operations applied')
        writes = stable_preparation(current, n, plan)
        execution = plan.get('execution', {})
        if not dry_run and execution.get('explicit_7_3d_live_signal_received') is False:
            raise ValueError('plan still holds live apply authority')
        if dry_run:
            return ''.join(''.join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                fromfile='a/' + str(path.relative_to(inst.root)), tofile='b/' + str(path.relative_to(inst.root))))
                for path, (before, after) in writes.items())
        # All semantic and source checks finish before the first mutation. Native
        # event journaling retains interrupted I/O for recovery, as in single close.
        with current.commit_batch(n['id'] + ': apply reviewed housekeeping plan'):
            with events.Command(current.root, command='task_apply_plan', node=node_id):
                # Native journal recovery may restore an interrupted source; a
                # second preflight must still match before publishing this plan.
                writes = stable_preparation(current, n, plan)
                if not writes.fresh(current):
                    raise ValueError('consumed housekeeping state changed before publication')
                for path, (_, text) in writes.items():
                    if path == n['dir'] / jobs.JOBS_FILE:
                        events.write_record(current, n, 'jobs', 'ledger', text)
                    else:
                        path.parent.mkdir(exist_ok=True)
                        common.atomic_write(path, text)
                    common.touch(path)
        return 'applied: ' + str(len(writes)) + ' files in one commit'


def cmd_task_apply_plan(a):
    inst = nodes.load()
    try:
        plan = json.loads(Path(a.plan).read_text())
        print(apply_plan(inst, a.node, plan, a.dry_run), end='\n')
    except (OSError, ValueError, TypeError, KeyError) as error:
        common.die('apply-plan: ' + str(error))


def register(actions):
    parser = actions.add_parser('apply-plan', help='validate a reviewed housekeeping plan, then commit once')
    parser.add_argument('node')
    parser.add_argument('plan')
    parser.add_argument('--dry-run', action='store_true', help='validate and print the proposed unified diff without writing')
    parser.set_defaults(f=cmd_task_apply_plan)
