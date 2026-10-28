"""Optimistic preparation for explicit status refreshes, never action authority."""
import contextlib
import os
import hashlib
import json
import re
import stat
from contextvars import ContextVar
from pathlib import Path

from . import common, instance, timings

_ACTIVE = ContextVar('brain_status_refresh_prepare', default=False)


def active():
    return _ACTIVE.get()


def requested(inst):
    from . import events
    return any(op.command == 'status' and op.inst.root == inst.root for op in events._CURRENT)


def generation(paths, shallow=False):
    """Cheap ref/config generations, including replacement-ref target aliases."""
    result = []
    def visit(path):
        try:
            st = path.lstat()
            if path.name == '.git' and stat.S_ISDIR(st.st_mode):
                result.append((str(path), st.st_dev, st.st_ino, st.st_mode))
                return  # metadata marker identity, never the object store
            result.append((str(path), st.st_dev, st.st_ino, st.st_mode, st.st_size,
                           st.st_mtime_ns, st.st_ctime_ns))
            if stat.S_ISLNK(st.st_mode):
                visit(path.resolve())
            elif stat.S_ISDIR(st.st_mode) and not shallow:
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

    def bases(self, cwd, env):
        cached = self.layouts.get(cwd)
        if cached is not None and generation(cached[1]) == cached[2]:
            return cached[0]
        markers = []
        home = Path(cwd or Path.cwd())
        for parent in (home, *home.parents):
            markers.append(parent / '.git')
            if (parent / '.git').exists():
                break
        marker_stamp = generation(markers)
        def discover():
            # Bypass both the observer and display caches for layout discovery.
            token = common._GIT_OBSERVER.set(None)
            try:
                return common.git('rev-parse', '--absolute-git-dir', '--git-common-dir', cwd=cwd,
                                  env=dict(os.environ) if env is None else env)
            finally:
                common._GIT_OBSERVER.reset(token)
        first = discover()
        bases = [(home / line).resolve() for line in first.stdout.splitlines()] if not first.returncode else []
        layout_paths = markers + [base / 'commondir' for base in bases]
        stamp = generation(layout_paths)
        second = discover()
        if (first.returncode, first.stdout) != (second.returncode, second.stdout) or generation(markers) != marker_stamp or generation(layout_paths) != stamp:
            common.die('status refresh Git dependency bindings changed; retry from fresh state')
        self.layouts[cwd] = (bases, layout_paths, stamp)
        return bases

    def paths(self, args, cwd, env):
        bases = self.bases(cwd, env)
        paths = []
        home = Path(cwd or Path.cwd())
        for parent in (home, *home.parents):
            paths.append(parent / '.git')
            if (parent / '.git').exists():
                break
        # Object bytes are immutable, but replacement/ancestry/config generations
        # can change their meaning. Ref tips are included only when queried.
        refs = set()
        query = list(args)
        while query and (query[0] == '--no-optional-locks' or query[0] == '-c'):
            query = query[1:] if query[0] != '-c' else query[2:]
        command = query[0] if query else ''
        if any(re.match(r'^HEAD(?:$|[~^:])', arg) for arg in args) or command == 'log':
            refs.add('HEAD')
        if 'main' in args:
            refs.add('refs/heads/main')
        broad = command == 'for-each-ref' or any(a in ('--all', '--branches') for a in args)
        # Optimize only the native explicit-ref/SHA grammar. Unqualified names
        # and other revision expressions fall back to replay, never partial
        # dependency guesses. Valid ref names include Unicode, '+' and '@'.
        replay = command not in ('remote', 'symbolic-ref', 'rev-parse', 'merge-base', 'rev-list', 'log', 'for-each-ref', 'diff')
        revisions = query[1:query.index('--')] if '--' in query else query[1:]
        option_value = False
        revision_count = 0
        for arg in revisions:
            if option_value:
                option_value = False
                continue
            if command == 'log' and arg in ('-S', '-G'):
                option_value = True
                continue
            if arg.startswith('-'):
                continue
            for revision in re.split(r'\.\.\.?', arg):
                revision_count += 1
                name = re.split(r'[~^:]', revision, maxsplit=1)[0]
                if name.startswith('refs/') and not re.search(r'[\s{}?*\[\]\\]', name):
                    refs.add(name)
                elif name == 'HEAD':
                    refs.add('HEAD')
                elif name == 'main':
                    refs.add('refs/heads/main')
                elif not re.fullmatch(r'[0-9a-f]{40,64}', name):
                    replay = True
        mutable_diff = command == 'diff' and revision_count != 2
        replay = replay or mutable_diff
        if replay:
            broad = True
            refs.add('HEAD')
        for base in bases:
            paths.extend(base / name for name in ('config', 'config.worktree', 'commondir', 'packed-refs', 'reftable',
                         'shallow', 'info/grafts', os.environ.get('GIT_REPLACE_REF_BASE', 'refs/replace/')))
            if command in ('status', 'ls-files') or mutable_diff:
                paths.append(base / 'index')
            if broad:
                paths.append(base / 'refs')
            replacements = base / os.environ.get('GIT_REPLACE_REF_BASE', 'refs/replace/')
            if replacements.is_dir():
                refs.update(p.relative_to(base).as_posix() for p in replacements.rglob('*') if p.is_file())
        # A linked worktree's HEAD may point into common refs; replacement
        # symbolic chains can likewise cross either metadata directory.
        pending, seen = list(refs), set()
        while pending:
            ref = pending.pop()
            if ref in seen:
                continue
            seen.add(ref)
            for base in bases:
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
        replay = replay or command == 'status'
        return paths, replay

    def observe(self, args, cwd, env, result):
        key = (args, str(cwd) if cwd is not None else None)
        if result is None:
            paths, replay = self.paths(args, key[1], env)
            self.pending[key] = (paths, replay, generation(paths))
            return
        paths, replay, before = self.pending.pop(key)
        value = (result.returncode, result.stdout)
        discovered, _ = self.paths(args, key[1], env)
        if set(discovered) != set(paths):
            common.die('status refresh Git dependency bindings changed; retry from fresh state')
        after = generation(paths)
        if before != after:
            # One unrelated commit may land during the query itself. Compare
            # its semantic answer once instead of refusing on ref clocks alone.
            checked, after = self.reread(args, cwd, env, paths)
            if checked != value:
                common.die('status refresh inputs changed during computation; retry from fresh state')
        if key in self.queries and self.queries[key][-1] != value:
            common.die('status refresh inputs changed during computation; retry from fresh state')
        self.queries[key] = (env, paths, replay, after, value)

    def reread(self, args, cwd, env, paths):
        # A semantic reread may adopt new ref values, never a new dependency
        # topology with an obsolete closure (including moved .git bindings).
        discovered, _ = self.paths(args, str(cwd) if cwd is not None else None, env)
        if set(discovered) != set(paths):
            common.die('status refresh Git dependency bindings changed; retry from fresh state')
        before = generation(paths)
        token = common._GIT_OBSERVER.set(None)
        try:
            result = common.git(*args, cwd=cwd, env=dict(os.environ) if env is None else env)
        finally:
            common._GIT_OBSERVER.reset(token)
        after = generation(paths)
        discovered, _ = self.paths(args, str(cwd) if cwd is not None else None, env)
        if set(discovered) != set(paths) or before != after:
            common.die('status refresh Git inputs kept changing during validation; retry from fresh state')
        return (result.returncode, result.stdout), after

    def validate(self):
        validated = []
        for (args, cwd), (env, paths, replay, stamp, before) in self.queries.items():
            if replay or generation(paths) != stamp:
                after, stamp = self.reread(args, cwd, env, paths)
                if after != before:
                    common.die('status refresh inputs changed during computation; retry from fresh state')
            validated.append([args, cwd, env, paths, stamp, before])
        # A later query must not invalidate an earlier answer. One final
        # semantic check tolerates a single unrelated commit at this boundary;
        # further generation drift refuses, without a validation retry loop.
        for item in validated:
            args, cwd, env, paths, stamp, before = item
            if generation(paths) != stamp:
                after, item[4] = self.reread(args, cwd, env, paths)
                if after != before:
                    common.die('status refresh inputs changed during computation; retry from fresh state')
        if any(generation(item[3]) != item[4] for item in validated):
            common.die('status refresh Git inputs kept changing during validation; retry from fresh state')
        self.validated = validated

    def final_check(self):
        if any(generation(item[3]) != item[4] for item in self.validated):
            common.die('status refresh Git inputs kept changing during validation; retry from fresh state')


def input_paths(inst, n):
    """File inputs of snapshot_view/facts_block; Git is fenced by query results.

    Discovery, proposed learning and source policy read the whole node tree, but
    job/task/judgement bodies belong only to the node being projected. Never
    fence repository HEAD, refs directories, transcript telemetry or caches.
    """
    paths = [inst.local_path, inst.root / '.brain/communications.json',
             inst.root / '.brain/wake/lifecycle.json']
    paths.extend((inst.root / '.brain/teaching').glob('*.json'))
    paths.extend(item['path'] for item in inst.items())
    for node in inst.nodes():
        paths.extend(node['dir'] / name for name in ('node.md', 'never-name.txt'))
        paths.extend((node['dir'] / 'specialists').glob('*/role.md'))
        for child in (node['dir'] / 'nodes').glob('*'):
            if child.is_dir():
                paths.append(child / 'node.md')
    paths.extend(n['dir'] / name for name in (
        'status.md', 'brief.md', 'backlog.md', 'inbox.md', 'AGENTS.md', 'job-ledger.md', 'job-handbacks',
        'job-attempts', 'job-contracts', 'job-trains', 'handback-queue', 'sources/import-proposals.md',
        'direction.md', 'role.md', 'standards.md', 'lessons.md', 'synthesis.json', 'feedback.md'))
    from . import rotation, status, reports, tasks, task_flow, verify, watch, jobs, job_attempts, machine
    from . import recurrence, hosts
    for target in recurrence.synthesis_targets(inst, n):
        if 'error' not in target:
            paths.extend(target['dir'] / name for name in ('direction.md', 'role.md', 'standards.md', 'lessons.md', 'synthesis.json'))
    paths.extend((rotation.rotation_file(inst, n), status.hook_question_queue(n),
                  reports.preview_receipt_file(inst, n), inst.root / '.brain/worker-runs.json'))
    for family in ('review-receipts', 'review-findings', 'preview-receipts'):
        paths.append(inst.root / '.brain' / family / hashlib.sha256(n['id'].encode()).hexdigest())
    try:
        receipt = json.loads(reports.preview_receipt_file(inst, n).read_text())
        for artifact in [receipt.get('tour'), *receipt.get('screenshots', [])]:
            if isinstance(artifact, dict) and isinstance(artifact.get('path'), str):
                path = Path(artifact['path'])
                if not path.is_dir():
                    paths.append(path)
    except (OSError, ValueError, TypeError, AttributeError):
        pass  # the display retains an invalid/missing preview verdict
    paths.append(inst.root / '.brain/wake' / ('job-stalls-' + watch.watch_digest(n['id']) + '.json'))
    if n['id'] == 'root':
        paths.extend((inst.root / '.brain/coordinator-runs.json', inst.root / '.brain/core-remote.json',
                      inst.root / '.brain/host-observations.json'))
        paths.append(common.CORE / 'adapters/hosts' / (inst.cfg.get('host', '') + '.env'))
        for peer in inst.nodes():
            if not peer['archived'] and peer['local'].get('coordinator'):
                target, _ = hosts.wake_target(inst, peer)
                directory = hosts.wake_directory(inst, target)
                paths.append(directory / 'turn.json')
                paths.extend(directory.glob('*.pending'))
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
                        if not relative.is_absolute() and '..' not in relative.parts and not (peer['dir'] / relative).is_dir():
                            paths.append(peer['dir'] / relative)
                except ValueError:
                    pass  # the projection preserves the existing invalid-input verdict
    # Ready packet and attempt validation can read worktree input files and
    # stored receipts. Do not include the worktree directory recursively.
    for branch, binding in n['local'].get('worktrees', {}).items():
        paths.append(verify.ready_path(inst, n, branch))
        root = Path(binding['path'])
        for pattern in common.lst(n['fm'].get('verify_inputs')) or common.lst(verify.VERIFY_INPUTS):
            paths.extend(path for path in root.glob(pattern) if path.is_file())
    # Readers follow internal aliases; fence their physical source as well.
    aliases = [p.resolve() for path in paths if path.is_dir() for p in path.rglob('*') if p.is_symlink()]
    return paths + [p.resolve() for p in paths] + aliases


def membership_paths(inst, selected):
    """Directory generations cover additions after the unlocked path discovery."""
    paths = [inst.root / '.brain/teaching']
    for node in inst.nodes():
        paths.extend(node['dir'] / name for name in ('nodes', 'library', 'specialists'))
    for directory, subdirs, _files in os.walk(inst.root):
        subdirs[:] = [name for name in subdirs if name not in ('.git', '.brain') and
                      not (name == 'core' and Path(directory) == inst.root)]
        if Path(directory).name == 'library':
            paths.append(Path(directory))
    if any(node['id'] == 'root' for node in selected):
        from . import hosts
        paths.extend(hosts.wake_directory(inst, hosts.wake_target(inst, node)[0])
                     for node in inst.nodes() if not node['archived'] and node['local'].get('coordinator'))
    return tuple(sorted(set(paths)))


GIT_DRIFT = ('status refresh inputs changed during computation',
             'status refresh Git inputs kept changing during validation')
# Plain contention: another writer moved inputs or held a lease during this
# refresh. Any other refusal stays final: a repair's own cause, and a settings
# or binding edit (see cli.refresh_with_retry), which refuses as before.
CONTENTION = ('another write is active; cannot reconcile its snapshot',
              'state changed during the unlocked step', *GIT_DRIFT)
HELD_BUILDS = 3


def contended(error):
    message = error.code if isinstance(error.code, str) else ''
    if 'status refresh maintenance refused' in message:
        message = message.partition('; cause: ')[2]
    return any(text in message for text in CONTENTION)


def settings_changed():
    # The caller publishes with settings loaded during this held attempt.
    return instance.settings(instance.Instance(instance._LOCK['held_root'])) != instance._LOCK['held_settings']


def compute(inst, n, build, *, all_nodes=False, targets=None, extra=()):
    """Release physical ownership, prepare once, then refuse relevant drift.

    Query-result fences preserve path-specific history and ancestry semantics:
    an unrelated commit may move HEAD/main without changing any answer used by
    this projection. The CLI retries a refused refresh whole; no stale text
    reaches the writer.
    """
    if active():
        return build(inst, n)
    if instance.holding():
        # The contended last attempt keeps the lock, so no Brain writer can
        # publish beside it. Worktree files and Git still can: every build stays
        # fenced, drift rebuilds under the lock, and only a bounded number.
        for _ in range(HELD_BUILDS):
            try:
                result = fenced(inst, n, build, all_nodes, targets, extra)
            except SystemExit as error:
                # A settings or binding edit is not retried: it refuses as before.
                if not contended(error) or settings_changed():
                    raise
                continue
            if settings_changed():
                common.die('state changed during the unlocked step; retry from fresh state')
            return result
        common.die(f'status refresh inputs kept changing through {HELD_BUILDS} locked builds; '
                   'worktree files and Git are outside the Brain lock')
    return fenced(inst, n, build, all_nodes, targets, extra)


def fenced(inst, n, build, all_nodes, targets, extra):
    git_inputs = GitInputs()
    with instance.brain_lock(inst):
        current = instance.Instance(inst.root)
        node = current.node(n['id'])
        selected = (list(current.nodes()) if all_nodes else
                    [current.node(nid) for nid in targets] if targets is not None else [node])
        bindings = [(Path(w['path']), Path(w['path']).is_dir())
                    for target in selected for w in target['local'].get('worktrees', {}).values()]
        def paths():
            # Instance policy (inherited status_source, timezone) is read too;
            # released() fences it, and a held build must as well.
            policy = [current.root / 'instance.md', (current.root / 'instance.md').resolve()]
            return ([path for target in selected for path in input_paths(current, target)] + policy +
                    list(extra() if callable(extra) else extra))
        from . import events
        owner = next((op for op in reversed(events._CURRENT) if isinstance(op, events.Command)
                      and op.command == 'status' and op.inst.root == inst.root), None)
        window = events.resources(inst) if owner is not None and not instance.holding() else None
        def attribute_window():
            # Runs after released() retook the lock: whatever moved is foreign.
            after = events.resources(inst)
            owner.foreign.update(k for k in window.keys() | after.keys() if window.get(k) != after.get(k))
        with contextlib.ExitStack() as stack:
            if window is not None:
                stack.callback(attribute_window)
            stack.enter_context(contextlib.nullcontext() if instance.holding() else
                                instance.released(inst, list(current.nodes())))
            token = _ACTIVE.set(True)
            observer = common._GIT_OBSERVER.set(git_inputs.observe)
            reads = common._GIT_READS.set(None)
            try:
                from . import ui_readmodel, spend, status, jobs, command_reads
                with ui_readmodel.observe(current), spend.spend_read_only(), status.machine_alert_snapshot() as alerts, timings.phase('refresh_compute'):
                    before_paths = tuple(sorted(set(paths())))
                    before = generation(before_paths)
                    before_directories = membership_paths(current, selected)
                    before_membership = generation(before_directories, shallow=True)
                    # Repeated projections in this one fenced read share source
                    # observations. Discovery and every publication guard stay
                    # outside the scope; no result survives a compute boundary.
                    with command_reads.scope(jobs.read_jobs, status.file_state, status.status_facts,
                                             status.facts_meta, status.status_section, status.active_work,
                                             status.inbox_lines, status.blocked_entries, status.pending_imports):
                        result = build(current, node)
                    final_paths = tuple(sorted(set(paths())))
                    expected = generation(final_paths)
                    final_directories = membership_paths(current, selected)
                    expected_membership = generation(final_directories, shallow=True)
                    if (before_paths != final_paths or before != expected or before_directories != final_directories or
                            before_membership != expected_membership or not status.machine_alerts_current(current, alerts)):
                        common.die('state changed during the unlocked step; retry from fresh state')
            finally:
                common._GIT_OBSERVER.reset(observer)
                common._GIT_READS.reset(reads)
                _ACTIVE.reset(token)
        with timings.phase('refresh_revalidate'):
            if generation(final_paths) != expected or generation(final_directories, shallow=True) != expected_membership:
                common.die('state changed during the unlocked step; retry from fresh state')
            if any(path.is_dir() != existed for path, existed in bindings):
                common.die('status refresh inputs changed during computation; retry from fresh state')
            git_inputs.validate()
            if (not status.machine_alerts_current(current, alerts) or generation(final_paths) != expected or
                    generation(final_directories, shallow=True) != expected_membership):
                common.die('state changed during the unlocked step; retry from fresh state')
            git_inputs.final_check()
        return result
