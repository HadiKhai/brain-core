"""Optimistic preparation for explicit status refreshes, never action authority."""
import os
import hashlib
import json
import re
from contextvars import ContextVar
from pathlib import Path

from . import common, instance, timings

_ACTIVE = ContextVar('brain_status_refresh_prepare', default=False)


def active():
    return _ACTIVE.get()


def requested(inst):
    from . import events
    return any(op.command == 'status' and op.inst.root == inst.root for op in events._CURRENT)


def generation(paths):
    """Cheap ref/config generations, including replacement-ref target aliases."""
    result = []
    def visit(path):
        try:
            st = path.lstat()
            if path.name == '.git' and path.is_dir() and not path.is_symlink():
                result.append((str(path), st.st_dev, st.st_ino, st.st_mode))
                return  # metadata marker identity, never the object store
            result.append((str(path), st.st_dev, st.st_ino, st.st_mode, st.st_size,
                           st.st_mtime_ns, st.st_ctime_ns))
            if path.is_symlink():
                visit(path.resolve())
            elif path.is_dir():
                for child in sorted(path.iterdir()):
                    visit(child)
        except FileNotFoundError:
            result.append((str(path), None))
    for path in sorted(set(paths)):
        visit(path)
    return result


class GitInputs:
    """Fence query dependencies without rerunning hundreds of immutable proofs."""
    def __init__(self):
        self.layouts = {}
        self.queries = {}
        self.pending = {}

    def paths(self, args, cwd, env):
        if cwd not in self.layouts:
            # Bypass both the observer and display caches for layout discovery.
            token = common._GIT_OBSERVER.set(None)
            try:
                result = common.git('rev-parse', '--absolute-git-dir', '--git-common-dir', cwd=cwd,
                                    env=dict(os.environ) if env is None else env)
            finally:
                common._GIT_OBSERVER.reset(token)
            self.layouts[cwd] = [(Path(cwd or Path.cwd()) / line).resolve()
                                 for line in result.stdout.splitlines()] if not result.returncode else []
        bases = self.layouts[cwd]
        paths = []
        home = Path(cwd or Path.cwd())
        for parent in (home, *home.parents):
            paths.append(parent / '.git')
            if (parent / '.git').exists():
                break
        # Object bytes are immutable, but replacement/ancestry/config generations
        # can change their meaning. Ref tips are included only when queried.
        refs = set(re.findall(r'refs/[A-Za-z0-9_./-]+', ' '.join(args)))
        query = list(args)
        while query and (query[0] == '--no-optional-locks' or query[0] == '-c'):
            query = query[1:] if query[0] != '-c' else query[2:]
        command = query[0] if query else ''
        if 'HEAD' in args or command == 'log':
            refs.add('HEAD')
        if 'main' in args:
            refs.add('refs/heads/main')
        broad = command == 'for-each-ref' or any(a in ('--all', '--branches') for a in args)
        for base in bases:
            paths.extend(base / name for name in ('config', 'commondir', 'packed-refs', 'reftable',
                         'shallow', 'info/grafts', os.environ.get('GIT_REPLACE_REF_BASE', 'refs/replace/')))
            if broad:
                paths.append(base / 'refs')
            pending, seen = list(refs), set()
            while pending:
                ref = pending.pop()
                if ref in seen:
                    continue
                seen.add(ref)
                path = base / ref
                paths.append(path)
                try:
                    value = path.read_text().strip()
                    if value.startswith('ref: refs/') and '..' not in Path(value[5:]).parts:
                        pending.append(value[5:])
                except (OSError, UnicodeError):
                    pass
        # Worktree status/diffs with an implicit working tree need a real query
        # replay. They are uncommon preview checks, never completion DAG proofs.
        replay = command == 'status' or command == 'diff' and not any(re.fullmatch(r'[0-9a-f]{40,64}', a) for a in args)
        return paths, replay

    def observe(self, args, cwd, env, result):
        key = (args, str(cwd) if cwd is not None else None)
        if result is None:
            paths, replay = self.paths(args, key[1], env)
            self.pending[key] = (paths, replay, generation(paths))
            return
        paths, replay, before = self.pending.pop(key)
        value = (result.returncode, result.stdout)
        if before != generation(paths) or key in self.queries and self.queries[key][-1] != value:
            common.die('status refresh inputs changed during computation; retry from fresh state')
        self.queries[key] = (env, paths, replay, before, value)

    def validate(self):
        for (args, cwd), (env, paths, replay, stamp, before) in self.queries.items():
            if replay or generation(paths) != stamp:
                after = common.git(*args, cwd=cwd, env=dict(os.environ) if env is None else env)
                if (after.returncode, after.stdout) != before:
                    common.die('status refresh inputs changed during computation; retry from fresh state')


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
        'status.md', 'brief.md', 'inbox.md', 'AGENTS.md', 'job-ledger.md', 'job-handbacks',
        'job-attempts', 'job-contracts', 'job-trains', 'handback-queue', 'sources/import-proposals.md',
        'direction.md', 'role.md', 'standards.md', 'lessons.md', 'synthesis.json', 'feedback.md'))
    from . import rotation, status, reports, tasks, task_flow, verify, watch, jobs, job_attempts, machine
    paths.extend((rotation.rotation_file(inst, n), status.hook_question_queue(n),
                  reports.preview_receipt_file(inst, n), inst.root / '.brain/worker-runs.json'))
    for family in ('review-receipts', 'review-findings', 'preview-receipts'):
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
    if any(job_attempts.current_attempt(n, row, strict=False) for row in jobs.read_jobs(n) or []):
        paths.extend(machine.machine_dir() / name for name in ('machine-care-queue.json', 'machine-care.json'))
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


def compute(inst, n, build, *, all_nodes=False, targets=None, extra=()):
    """Release physical ownership, prepare once, then refuse relevant drift.

    Query-result fences preserve path-specific history and ancestry semantics:
    an unrelated commit may move HEAD/main without changing any answer used by
    this projection. No retries, and no stale text reaches the writer.
    """
    if active():
        return build(inst, n)
    git_inputs = GitInputs()
    with instance.brain_lock(inst):
        current = instance.Instance(inst.root)
        node = current.node(n['id'])
        selected = (list(current.nodes()) if all_nodes else
                    [current.node(nid) for nid in targets] if targets is not None else [node])
        bindings = [(Path(w['path']), Path(w['path']).is_dir())
                    for target in selected for w in target['local'].get('worktrees', {}).values()]
        def paths():
            return [path for target in selected for path in input_paths(current, target)] + list(extra() if callable(extra) else extra)
        with instance.released(inst, list(current.nodes()), extra=paths):
            token = _ACTIVE.set(True)
            observer = common._GIT_OBSERVER.set(git_inputs.observe)
            reads = common._GIT_READS.set(None)
            try:
                from . import ui_readmodel, spend
                with ui_readmodel.observe(current), spend.spend_read_only(), timings.phase('refresh_compute'):
                    result = build(current, node)
            finally:
                common._GIT_OBSERVER.reset(observer)
                common._GIT_READS.reset(reads)
                _ACTIVE.reset(token)
        with timings.phase('refresh_revalidate'):
            if any(path.is_dir() != existed for path, existed in bindings):
                common.die('status refresh inputs changed during computation; retry from fresh state')
            git_inputs.validate()
        return result
