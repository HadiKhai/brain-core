"""Evidence-backed repair of legacy imports, retaining every task and source."""
import copy
import contextlib
import json
import re
from collections import Counter
from pathlib import Path

from . import common as _m_common
from . import inbox_notices as _m_notices
from . import instance as _m_instance
from . import jobs as _m_jobs
from . import nodes as _m_nodes
from . import reminders as _m_reminders
from . import tasks as _m_tasks

DECISION = 'D-task-counts-v1'
RULE = ('Import only explicit backlog/build-order work and demonstrably live jobs. '
        'Drop machine notices, FYIs, answered inbox questions and status history; '
        'retain source records. Close imported job tasks only against retained job/Git evidence. '
        'Unknown or unfinished work remains open for reconciliation.')


def job_evidence(n, row):
    """Local observations only; missing proof never means abandoned or superseded."""
    branch = row['Job']
    w = n.get('local', {}).get('worktrees', {}).get(branch, {})
    home = n.get('home_path')
    result = {'state': 'unknown', 'ref': 'job-ledger.md#' + _m_jobs.job_key(row),
              'branch': branch, 'ended': row.get('Ended', ''), 'outcome': row.get('Outcome', ''),
              'cause': row.get('Cause', '')}
    if home and Path(home).is_dir():
        def ref(value):
            return _m_common.git('rev-parse', '--verify', '--quiet', value + '^{commit}', cwd=home).stdout.strip()
        tip = ref('refs/heads/' + branch)
        base = row.get('Base commit') or w.get('start', '')
        # A closed attempt's retained head takes precedence over a reused branch.
        head = row.get('Head commit') if row.get('Ended') else tip or row.get('Head commit')
        main = ref('refs/heads/main')
        result.update(base=base, head=head or '', main=main)
        dirty = False
        if not row.get('Ended') and w.get('path') and Path(w['path']).is_dir():
            actual = _m_common.git('symbolic-ref', '--quiet', '--short', 'HEAD', cwd=w['path'])
            if actual.returncode == 0 and actual.stdout.strip() == branch:
                status = _m_common.git('status', '--porcelain', '--untracked-files=normal', cwd=w['path'])
                dirty = status.returncode != 0 or bool(status.stdout.strip())
                result['dirty'] = dirty
        if (_m_tasks.SHA.fullmatch(base or '') and _m_tasks.SHA.fullmatch(head or '') and
                base != head and main and not dirty and
                _m_common.git('merge-base', '--is-ancestor', base, head, cwd=home).returncode == 0 and
                _m_common.git('merge-base', '--is-ancestor', head, main, cwd=home).returncode == 0):
            paths = _m_common.git('diff', '--name-only', base, head, cwd=home)
            if paths.returncode == 0 and paths.stdout.splitlines():
                # Head itself is a delivering main commit, including fast-forward landings.
                return {**result, 'state': 'merged', 'merge_sha': head, 'code_paths': paths.stdout.splitlines()}
        if not row.get('Ended') and w.get('path') and tip:
            signal = _m_jobs.job_signal(_m_instance.Instance(_m_tasks.instance_root(n)), n, row)
            if signal:
                return {**result, 'state': 'stopped', 'worker_state': signal['state'],
                        'worker_at': signal.get('at'), 'worker_ref': str(_m_jobs.handback_file(n, row).relative_to(n['dir']))}
            path = Path(w['path'])
            if path.is_dir():
                actual = _m_common.git('symbolic-ref', '--quiet', '--short', 'HEAD', cwd=path)
                root = _m_common.git('rev-parse', '--show-toplevel', cwd=path)
                common = _m_common.git('rev-parse', '--path-format=absolute', '--git-common-dir', cwd=home)
                other = _m_common.git('rev-parse', '--path-format=absolute', '--git-common-dir', cwd=path)
                if (actual.returncode == root.returncode == common.returncode == other.returncode == 0 and
                        actual.stdout.strip() == branch and Path(root.stdout.strip()).resolve() == path.resolve() and
                        common.stdout.strip() == other.stdout.strip()):
                    return {**result, 'state': 'live', 'worktree': str(path)}
    if row.get('Ended') and row.get('Outcome') == 'abandoned' and row.get('Cause'):
        # A bare claim of supersession is not proof of delivered replacement work.
        if not re.search(r'supersed', row['Cause'], re.I):
            result['state'] = 'abandoned'
    return result


def source_kind(row):
    for value in row.get('provenance', []):
        if re.fullmatch(r'(?:inbox\.md|status\.md|backlog\.md|build-order\.md):[A-F0-9]{16}', value):
            return value.split(':')[0]
        if re.fullmatch(r'job:[a-f0-9]{24}', value):
            return 'job'
    return row.get('source', 'unknown')


def nonwork_reason(inst, n, row, requirements, lifecycle):
    origin = source_kind(row)
    if origin == 'status.md':
        return 'Imported status history is not a work item.'
    if origin in ('backlog.md', 'build-order.md'):
        from . import tasks_backfill
        path = n['dir'] / origin
        excluded = tasks_backfill.nonwork_source_lines(n, origin)
        active = {line for number, line in enumerate(path.read_text().splitlines(), 1) if number not in excluded} if path.exists() else set()
        if requirements.get(row.get('requirement'), {}).get('quote') in set(excluded.values()) - active:
            return 'Imported decision/history, authority rule, resume condition or code example is not a work item.'
        return None
    if origin != 'inbox.md':
        return None
    req = requirements.get(row.get('requirement'), {})
    quote = req.get('quote') or '- ' + row['title']
    if _m_notices.terminal_notice(quote):
        return 'Imported terminal machine notice is telemetry, not work.'
    # Only explicitly labelled FYIs, not a substring inside an actual instruction.
    text = re.sub(r'^- .*? from [^:]+: ', '', quote)
    protected = re.search(r'<!-- (?:host-question|handback|worker-failure|communication):', quote)
    if not protected and re.match(r'(?:FYI(?:\s+only)?\b|no action (?:needed|required)\b)', text, re.I):
        return 'Imported explicitly labelled FYI is not work.'
    episodes = lifecycle['inbox'].get(n['id'], {})
    if not isinstance(episodes, dict):
        raise ValueError('invalid inbox lifecycle receipts')
    key = _m_reminders.inbox_source_id(n, quote)
    current = episodes.get(key)
    if key in episodes:
        _m_reminders.validate_inbox_episode(n, key, current, lifecycle['answers'])
    if current is not None and current.get('answered') is True:
        return 'Imported inbox question already has an exact retained answer.'
    return 'Legacy inbox import is a routing message, not a standalone work item; source and pending inbox are retained.'


def count_node(inst, n):
    from . import status
    rows = list(_m_tasks.read_records(n).values())
    requirements = list(_m_tasks.read_records(n, 'requirements').values())
    return {'node': n['id'], 'tasks': len(rows),
            'by_status': dict(sorted(Counter(r['status'] for r in rows).items())),
            'by_source': dict(sorted(Counter(source_kind(r) for r in rows).items())),
            'open_by_source': dict(sorted(Counter(source_kind(r) for r in rows
                                                  if r['status'] not in ('done', 'merged', 'dropped')).items())),
            'in_progress': sum(r['status'] == 'in-progress' for r in rows),
            'requirements': len(requirements),
            'requirements_dropped': sum(r.get('status') == 'dropped' for r in requirements),
            'inbox_pending': len(status.inbox_lines(n, readonly=True))}


def plan_node(inst, n, lifecycle):
    original = _m_tasks.read_records(n)
    rows = copy.deepcopy(original)
    requirements = _m_tasks.read_records(n, 'requirements')
    ledger = _m_jobs.read_jobs(n)
    if ledger is None:
        raise ValueError('unrecognized ledger; no reconciliation written')
    jobs = {_m_jobs.job_key(r): r for r in ledger}
    actions, unresolved = [], []
    for ident, row in rows.items():
        # Reconciliation owns only legacy imports; human/provider-managed tasks stay intact.
        if row.get('source') != 'local' or source_kind(row) not in (
                'inbox.md', 'status.md', 'backlog.md', 'build-order.md', 'job') or row['status'] in ('dropped', 'done', 'merged'):
            continue
        reason = nonwork_reason(inst, n, row, requirements, lifecycle)
        proof = None
        if source_kind(row) == 'job':
            job = jobs.get(row.get('job'))
            proof = job_evidence(n, job) if job else {'state': 'unknown', 'ref': 'missing ledger job'}
            if proof['state'] == 'merged':
                row.update(status='done', merge_sha=proof['merge_sha'], baseline_sha=proof['base'],
                           code_path=','.join(proof['code_paths']), scope=row.get('scope') or proof['code_paths'],
                           event_pending='task_landed')
            elif proof['state'] == 'abandoned':
                reason = 'Imported job attempt abandoned: ' + proof['cause']
            elif proof['state'] == 'live':
                continue
            else:
                # No closure from absence, age, stale thread IDs or a bare supersession claim.
                unresolved.append({'task': ident, 'evidence': proof})
                if row['status'] == 'parked' and row.get('reconcile_rule') == DECISION:
                    continue
                row.update(status='parked', park_reason='Job delivery/liveness is not proved',
                           resume='Confirm the live attempt or map its retained scope to a delivering main commit and code paths',
                           review_at=inst.now(), next_action='Reconcile job evidence with the coordinator',
                           reconciliation=True)
        elif not reason:
            continue
        if reason:
            row.update(status='dropped', drop_reason=reason, drop_decision='private:' + DECISION)
        evidence = json.dumps(proof, sort_keys=True) if proof else reason
        row['evidence'] = list(dict.fromkeys([*row.get('evidence', []), evidence]))
        row.update(reconcile_rule=DECISION, updated=inst.now())
        actions.append({'task': ident, 'from': original[ident]['status'], 'to': row['status'],
                        'reason': reason, 'evidence': proof})
    # Completion guards still apply: inferred delivery cannot bypass an explicit acceptance scope.
    for action in actions:
        row = rows[action['task']]
        if row['status'] == 'done':
            from . import task_flow
            task_flow.completion(n, row, records=rows)
    req_changes = []
    for ident, req in requirements.items():
        refs = [r for r in rows.values() if r.get('requirement') == ident]
        if refs and all(r['status'] == 'dropped' for r in refs) and req.get('status') != 'dropped':
            # Only requirements linked to this migration's dropped records are owned here.
            if all(r.get('reconcile_rule') == DECISION for r in refs):
                req.update(status='dropped', drop_decision='private:' + DECISION,
                           drop_reason='All imported work records for this source were dropped; exact source retained.')
                req_changes.append(req)
    decision = n['dir'] / 'decisions.md'
    decision_text = decision.read_text() if decision.exists() else '# Decisions\n'
    marker = '<!-- task-reconcile:' + DECISION + ' -->'
    if (actions or req_changes) and marker not in decision_text:
        decision_text += '\n## ' + DECISION + ' — ' + inst.now() + '\n\n' + marker + '\n\n' + RULE + '\n'
    for action in actions:
        _m_tasks.record_path(n, action['task'])
    for req in req_changes:
        _m_tasks.record_path(n, req['id'], 'requirements')
    if actions or req_changes:
        _m_common.safe_target(decision)
        if decision.is_symlink():
            raise ValueError('decision log must be a regular node-local file')
    return {'node': n['id'], 'changes': actions, 'unresolved': unresolved,
            'requirements_dropped': len(req_changes), 'records_deleted': 0}, rows, req_changes, decision_text


def reconcile(inst, nodes, apply=False, drain=False, archive=False):
    """Default is a read-only plan. Apply never removes records or changes the ledger."""
    with _m_instance.brain_lock(inst) if apply else contextlib.nullcontext():
        lifecycle = _m_reminders.lifecycle_read(inst)
        plans = [(n, plan_node(inst, n, lifecycle)) for n in nodes]
        if drain:
            _m_notices.expire(inst, nodes, data=lifecycle)
            if len(json.dumps(lifecycle, sort_keys=True).encode()) > _m_reminders.LIFECYCLE_BYTES:
                raise ValueError('notice expiry exceeds lifecycle bound; retain records for repair')
        archives = [_m_notices.archive_plan(inst, n, lifecycle) for n in nodes] if archive else []
        for (_, (result, *_)), archived in zip(plans, archives):
            result['inbox_lines_archived'] = len(archived['episodes'])
        # Preflight every node before the first publication.
        if apply:
            for n, (result, rows, requirements, decision_text) in plans:
                if result['changes'] or requirements:
                    path = n['dir'] / 'decisions.md'
                    _m_common.atomic_write(path, decision_text)
                    _m_common.touch(path)
                for action in result['changes']:
                    _m_tasks.save_record(n, rows[action['task']])
                for req in requirements:
                    _m_tasks.save_record(n, req, 'requirements')
            if drain:
                _m_notices.expire(inst, nodes)
            for archived in archives:
                _m_notices.publish_archive(archived)
            inst.commit([], 'Reconcile imported task records without deleting sources')
            # Publication follows the entire durable batch, including interrupted replays.
            for n in nodes:
                for row in _m_tasks.read_records(n).values():
                    if row.get('reconcile_rule') == DECISION and row.get('event_pending') == 'task_landed':
                        _m_tasks.emit_task_landed(inst, n, row)
        return [plan[0] for _, plan in plans]


def cmd_records(a):
    inst = _m_nodes.load()
    if a.all and a.node:
        _m_common.die('choose a node or --all')
    nodes = [n for n in inst.nodes() if not n['archived']] if a.all else [inst.node(a.node or 'root')]
    try:
        value = ([count_node(inst, n) for n in nodes] if a.action == 'counts' else
                 reconcile(inst, nodes, a.apply, a.drain_notices, a.archive_handled))
        print(json.dumps(value, ensure_ascii=False))
    except (OSError, ValueError) as error:
        _m_common.die(str(error))


def register(actions):
    for action in ('counts', 'reconcile-records'):
        p = actions.add_parser(action, help='count or safely reconcile retained imported records')
        p.add_argument('node', nargs='?'); p.add_argument('--all', action='store_true')
        if action == 'reconcile-records':
            p.add_argument('--apply', action='store_true')
            p.add_argument('--drain-notices', action='store_true', help='expire terminal machine notices, retaining source lines')
            p.add_argument('--archive-handled', action='store_true', help='retain answered/expired lines in inbox-archive.md before removing them from the active inbox')
        p.set_defaults(f=cmd_records)
