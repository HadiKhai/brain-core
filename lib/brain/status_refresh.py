"""Optimistic preparation for explicit status refreshes, never action authority."""
import os
import hashlib
import json
from contextvars import ContextVar
from pathlib import Path

from . import common, instance, timings

_ACTIVE = ContextVar('brain_status_refresh_prepare', default=False)


def active():
    return _ACTIVE.get()


def requested(inst):
    from . import events
    return any(op.command == 'status' and op.inst.root == inst.root for op in events._CURRENT)


def input_paths(inst, n):
    """File inputs of snapshot_view/facts_block; Git is fenced by query results.

    Discovery, proposed learning and source policy read the whole node tree, but
    job/task/judgement bodies belong only to the node being projected. Never
    fence repository HEAD, refs directories, transcript telemetry or caches.
    """
    paths = [inst.local_path, inst.root / '.brain/communications.json',
             inst.root / '.brain/wake/lifecycle.json', inst.root / '.brain/teaching']
    for node in inst.nodes():
        paths.extend(node['dir'] / name for name in ('node.md', 'never-name.txt', 'library', 'specialists'))
    paths.extend(n['dir'] / name for name in (
        'status.md', 'brief.md', 'inbox.md', 'job-ledger.md', 'job-handbacks',
        'job-attempts', 'job-contracts', 'handback-queue', 'sources/import-proposals.md',
        'direction.md', 'role.md', 'standards.md', 'lessons.md', 'synthesis.json', 'feedback.md'))
    from . import rotation, status, reports, tasks, task_flow, verify, watch
    paths.extend((rotation.rotation_file(inst, n), status.hook_question_queue(n),
                  reports.preview_receipt_file(inst, n), inst.root / '.brain/worker-runs.json'))
    for family in ('review-receipts', 'review-findings'):
        paths.append(inst.root / '.brain' / family / hashlib.sha256(n['id'].encode()).hexdigest())
    try:
        receipt = json.loads(reports.preview_receipt_file(inst, n).read_text())
        for artifact in [receipt.get('tour'), *receipt.get('screenshots', [])]:
            if isinstance(artifact, dict) and isinstance(artifact.get('path'), str):
                paths.append(Path(artifact['path']))
    except (OSError, ValueError, TypeError, AttributeError):
        pass  # the display retains an invalid/missing preview verdict
    paths.append(inst.root / '.brain/wake' / ('job-stalls-' + watch.watch_digest(n['id']) + '.json'))
    if n['id'] == 'root':
        paths.extend((inst.root / '.brain/coordinator-runs.json', inst.root / '.brain/core-remote.json'))
    # Only task nodes reached by the projection's dependency checks are read.
    pending, seen = [n], set()
    while pending:
        node = pending.pop()
        if node['id'] in seen:
            continue
        seen.add(node['id'])
        paths.append(node['dir'] / 'tasks')
        rows = tasks.read_records(node)
        for row in rows.values():
            for ref in row.get('depends_on', []) + row.get('superseded_by', []):
                try:
                    peer, dep, _ = task_flow.resolve(node, ref, rows)
                    pending.append(peer)
                    receipt = (dep or {}).get('completion_receipt')
                    if isinstance(receipt, dict) and isinstance(receipt.get('artifact'), str):
                        relative = Path(receipt['artifact'])
                        if not relative.is_absolute() and '..' not in relative.parts:
                            paths.append(peer['dir'] / relative)
                except ValueError:
                    pass  # the projection preserves the existing invalid-input verdict
    # Ready packet and attempt validation can read worktree input files and
    # stored receipts. Do not include the worktree directory recursively.
    for branch, binding in n['local'].get('worktrees', {}).items():
        paths.append(verify.ready_path(inst, n, branch))
        root = Path(binding['path'])
        for pattern in common.lst(n['fm'].get('verify_inputs')) or common.lst(verify.VERIFY_INPUTS):
            paths.extend(root.glob(pattern))
    # Readers follow internal aliases; fence their physical source as well.
    aliases = [p.resolve() for path in paths if path.is_dir() for p in path.rglob('*') if p.is_symlink()]
    return paths + [p.resolve() for p in paths] + aliases


def compute(inst, n, build):
    """Release physical ownership, prepare once, then refuse relevant drift.

    Query-result fences preserve path-specific history and ancestry semantics:
    an unrelated commit may move HEAD/main without changing any answer used by
    this projection. No retries, and no stale text reaches the writer.
    """
    if active():
        return build(inst, n)
    queries = {}
    def observed(args, cwd, env, result):
        key = (args, str(cwd) if cwd is not None else None)
        value = (result.returncode, result.stdout)
        if key in queries and queries[key][1] != value:
            common.die('status refresh inputs changed during computation; retry from fresh state')
        queries[key] = (env, value)
    with instance.brain_lock(inst):
        current = instance.Instance(inst.root)
        node = current.node(n['id'])
        bindings = [(Path(w['path']), Path(w['path']).is_dir())
                    for w in node['local'].get('worktrees', {}).values()]
        with instance.released(inst, list(current.nodes()), extra=lambda: input_paths(current, node)):
            token = _ACTIVE.set(True)
            observer = common._GIT_OBSERVER.set(observed)
            try:
                from . import ui_readmodel, spend
                with ui_readmodel.observe(current), spend.spend_read_only(), timings.phase('refresh_compute'):
                    result = build(current, node)
            finally:
                common._GIT_OBSERVER.reset(observer)
                _ACTIVE.reset(token)
        with timings.phase('refresh_revalidate'):
            if any(path.is_dir() != existed for path, existed in bindings):
                common.die('status refresh inputs changed during computation; retry from fresh state')
            for (args, cwd), (env, before) in queries.items():
                after = common.git(*args, cwd=cwd, env=dict(os.environ) if env is None else env)
                if (after.returncode, after.stdout) != before:
                    common.die('status refresh inputs changed during computation; retry from fresh state')
        return result
