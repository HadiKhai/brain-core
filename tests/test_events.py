"""Metadata-only event stream scenarios; all data and instances are fabricated."""
import argparse
import importlib
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from scenario import Scenario, CORE
from test_jobs import settlement_export

sys.path.insert(0, str(CORE / 'lib'))
from brain import events, instance, transitions


class EventAppend(Scenario):
    fixture_init_in_process = True
    fixture_templates = True
    def test_private_journal_append_tightens_existing_mode_before_new_data(self):
        r = self.init('alice')
        path = r / '.brain/fixture-journal.jsonl'
        path.write_text('{"earlier": true}\n')
        path.chmod(0o644)
        inode = path.stat().st_ino
        events.append_line(path, {'new': True})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.stat().st_ino, inode)
        self.assertEqual([json.loads(line) for line in path.read_text().splitlines()],
                         [{'earlier': True}, {'new': True}])

    def test_canonical_parent_retarget_refuses_append_and_private_read(self):
        r = self.init('demo')
        for operation in ('append', 'read', 'index'):
            with self.subTest(operation=operation):
                storage = r / ('storage-' + operation)
                storage.mkdir()
                outside = self.tmp / ('outside-' + operation)
                outside.mkdir()
                target = storage / 'record'
                target.write_bytes(b'owned\n')
                (outside / 'record').write_bytes(b'foreign\n')
                before = {p.name: p.read_bytes() for p in outside.iterdir()}
                checked = events.common.safe_target

                def retarget(path):
                    result = checked(path)
                    if path == target:
                        storage.rename(r / ('retained-' + operation))
                        storage.symlink_to(outside)
                    return result

                with mock.patch.object(events.common, 'safe_target', side_effect=retarget):
                    with self.assertRaises((OSError, SystemExit)):
                        if operation == 'append':
                            events.append_line(target, {'fixture': True})
                        elif operation == 'read':
                            events.private_read(target)
                        else:
                            store = events.Journal(instance.Instance(r))
                            store.path = target
                            store.load_index()
                self.assertEqual({p.name: p.read_bytes() for p in outside.iterdir()}, before)
                self.assertEqual((r / ('retained-' + operation) / 'record').read_bytes(), b'owned\n')

    def test_lease_close_uses_original_directory_and_preserves_replacement_inode(self):
        r = self.init('demo')
        inst = instance.Instance(r)
        op = events.Command(r, command='fixture')
        op.start()
        lease = events.lease_path(inst, op.ref)
        retained = lease.with_name('retained-lease')
        try:
            lease.rename(retained)
            lease.write_bytes(b'foreign replacement\n')
            op.close_lease()
            self.assertEqual(lease.read_bytes(), b'foreign replacement\n')
            self.assertTrue(retained.exists())
        finally:
            op.close_lease()

    def test_lease_close_after_alias_retarget_does_not_remove_foreign_lease(self):
        r = self.init('demo')
        inst = instance.Instance(r)
        op = events.Command(r, command='fixture')
        op.start()
        old = r / '.brain/events-leases'
        moved = r / '.brain/retained-leases'
        outside = self.tmp / 'outside'
        outside.mkdir()
        foreign = outside / op.ref
        foreign.write_bytes(b'foreign lease\n')
        try:
            old.rename(moved)
            old.symlink_to(outside)
            op.close_lease()
            self.assertEqual(foreign.read_bytes(), b'foreign lease\n')
            self.assertFalse((moved / op.ref).exists())
        finally:
            op.close_lease()

    def test_alias_retarget_after_validation_keeps_checked_publication_target(self):
        r = self.init('demo')
        safe_target = events.common.safe_target
        for operation in ('append', 'append-new-parent', 'index', 'lease'):
            with self.subTest(operation=operation):
                storage = r / ('state-' + operation)
                storage.mkdir()
                outside = self.tmp / ('outside-' + operation)
                outside.mkdir()
                (outside / 'sentinel').write_bytes(b'keep external fixture bytes\n')
                alias = r / ('alias-' + operation)
                alias.symlink_to(storage)
                relative = Path('missing/record') if operation in ('append-new-parent', 'lease') else Path('record')
                target = alias / relative
                if operation in ('append', 'index'):
                    (outside / relative).write_bytes(b'external original\n')
                before = {p.relative_to(outside): p.read_bytes() if p.is_file() else None
                          for p in outside.rglob('*')}
                checked = []

                def retarget(path):
                    result = safe_target(path)
                    if path == target:
                        self.assertEqual(result, storage / relative)
                        checked.append(result)
                        alias.unlink()
                        alias.symlink_to(outside)
                    return result

                try:
                    with mock.patch.object(events.common, 'safe_target', side_effect=retarget):
                        if operation.startswith('append'):
                            events.append_line(target, {'fixture': True})
                        elif operation == 'index':
                            journal = events.Journal(instance.Instance(r))
                            journal.path = target
                            journal.save()
                        else:
                            self.assertEqual(events.private_path(target), storage / relative)
                finally:
                    self.assertEqual({p.relative_to(outside): p.read_bytes() if p.is_file() else None
                                      for p in outside.rglob('*')}, before)
                self.assertEqual(checked, [storage / relative])
                if operation == 'lease':
                    self.assertTrue((storage / relative).parent.is_dir())
                elif operation == 'index':
                    self.assertEqual(json.loads((storage / relative).read_bytes())['version'], 2)
                else:
                    self.assertEqual((storage / relative).read_bytes(), b'{"fixture":true}\n')

    def test_physical_parent_keeps_file_link_and_descriptor_guards(self):
        from brain import history
        r = self.init('demo')
        storage = r / 'private-state'
        storage.mkdir()
        alias = r / 'alias'
        alias.symlink_to(storage)
        target = alias / 'sample.jsonl'
        events.append_line(target, {'fixture': True})
        before = target.read_bytes()
        with mock.patch.object(history, 'history_fd_path', return_value=str(r / 'moved')):
            with self.assertRaisesRegex(OSError, 'directory moved'):
                events.append_line(target, {'fixture': False})
        self.assertEqual(target.read_bytes(), before)
        linked = storage / 'linked.jsonl'
        os.link(target, linked)
        with self.assertRaises(SystemExit):
            events.append_line(target, {'fixture': False})
        linked.unlink()
        linked.symlink_to(target)
        with self.assertRaises(SystemExit):
            events.append_line(linked, {'fixture': False})
        self.assertEqual(target.read_bytes(), before)

    def records(self, root):
        return events.read(instance.Instance(root))

    def commits(self, root):
        return [e for e in self.records(root) if e['kind'] == 'commit']

    def test_one_commit_per_write_and_no_message_bodies(self):
        r = self.init('demo')
        self.b(r, 'new', 'app', '--title', 'App')
        count = len(self.commits(r))
        self.b(r, 'backlog', 'app', 'add', 'BODY-MUST-STAY-IN-FILE')
        self.assertEqual(len(self.commits(r)), count + 1)
        self.assertIn('BODY-MUST-STAY-IN-FILE', (r / 'nodes/app/backlog.md').read_text())
        self.assertNotIn('BODY-MUST-STAY-IN-FILE', (r / '.brain/events.jsonl').read_text())
        self.assertIn('nodes/app/backlog.md', [x['path'] for x in self.commits(r)[-1]['data']['resources']])
        self.b(r, 'set', 'app', 'note=SECOND-BODY')
        self.assertEqual(len(self.commits(r)), count + 2)
        self.assertNotIn('SECOND-BODY', (r / '.brain/events.jsonl').read_text())
        self.assertTrue((r / 'backlog.md').exists())
        self.assertIn('all checks pass', self.b(r, 'check'))
        self.assertNotIn('.brain/', self.git('ls-files', cwd=r))

    def test_rejected_write_aborts_without_commit(self):
        r = self.init('demo')
        self.b(r, 'new', 'app', '--title', 'App')
        count = len(self.commits(r))
        self.b(r, 'set', 'app', 'id=other', ok=False)
        self.assertEqual(len(self.commits(r)), count)
        self.assertEqual(self.records(r)[-1]['kind'], 'abort')
        self.assertEqual(events.pending(self.records(r)), {})

    def test_hook_outbox_has_no_lock_no_text_and_fold_is_idempotent(self):
        r = self.init('demo')
        n = r / 'nodes/app'
        self.b(r, 'new', 'app', '--title', 'App')
        with mock.patch.object(instance.brain_lock, '__enter__', side_effect=AssertionError('hook took lock')):
            self.assertTrue(transitions.inbox_append(n, '- runtime: INBOX-PRIVATE\n', nonblocking=True))
        outbox = r / '.brain/hook-events.jsonl'
        self.assertNotIn('INBOX-PRIVATE', outbox.read_text())
        inst = instance.Instance(r)
        with instance.brain_lock(inst):
            events.fold_hooks(inst)
            (r / '.brain/hook-events-offset').write_text('0')  # crash after journal append, before cursor publication
            events.fold_hooks(inst)
        hooks = [e for e in self.records(r) if e['kind'] == 'hook']
        self.assertEqual(len(hooks), 1)
        self.assertEqual(hooks[0]['data']['at'], json.loads(outbox.read_text())['ts'])

    def test_protected_metadata_is_screened_and_fsync_is_required(self):
        r = self.init('demo')
        self.b(r, 'new', 'app', '--title', 'App')
        (r / 'never-name.txt').write_text('hiddenword\n')
        self.b(r, 'backlog', 'app', 'add', 'a hiddenword detail')
        self.assertNotIn('hiddenword', (r / '.brain/events.jsonl').read_text().casefold())
        path = r / '.brain/probe.jsonl'
        with mock.patch.object(events.os, 'fsync', side_effect=OSError('injected fsync failure')):
            with self.assertRaises(OSError):
                events.append_line(path, {'test': True})

    def test_rotation_preserves_records_and_unresolved_operations(self):
        r = self.init('demo')
        inst = instance.Instance(r)
        (r / '.brain/events.jsonl').write_text('')
        with instance.brain_lock(inst):
            with mock.patch.object(inst, 'now', return_value='2026-01-31 23:59 +00:00'):
                events.append(inst, 'root', 'begin', 'a' * 32, {'command': 'fixture'})
            with mock.patch.object(inst, 'now', return_value='2026-02-01 00:00 +00:00'):
                events.append(inst, 'root', 'commit', 'a' * 32, {'resources': []})
        self.assertTrue((r / '.brain/events-2026-01.jsonl').exists())
        self.assertEqual(events.pending(self.records(r)), {})

    def test_partial_failure_leaves_begin_and_live_lease_does_not_look_dead(self):
        r = self.init('demo')
        with instance.brain_lock(r):
            with self.assertRaises(RuntimeError):
                with events.Command(r, command='fixture') as op:
                    self.assertTrue(events.lease_live(op.inst, op.ref))
                    (r / 'backlog.md').write_text('- retained\n')
                    raise RuntimeError('injected after persistence')
        opened = events.pending(self.records(r))
        self.assertEqual(len(opened), 1)
        self.assertFalse(events.lease_live(instance.Instance(r), next(iter(opened))))

    def test_linked_journal_refuses_before_mutation(self):
        r = self.init('demo')
        self.b(r, 'new', 'app', '--title', 'App')
        journal = r / '.brain/events.jsonl'
        journal.unlink()
        journal.symlink_to(r / 'backlog.md')
        before = (r / 'nodes/app/node.md').read_bytes()
        target_before = (r / 'backlog.md').read_bytes()
        self.b(r, 'set', 'app', 'note=not-written', ok=False)
        self.assertEqual((r / 'nodes/app/node.md').read_bytes(), before)
        self.assertEqual((r / 'backlog.md').read_bytes(), target_before)

    def test_failed_setup_aborts_after_hook_without_masking_primary_exception(self):
        r = self.init('demo')
        self.b(r, 'new', 'app', '--title', 'App')
        for error in (SystemExit(7), KeyboardInterrupt('fixture cancellation'), RuntimeError('fixture primary')):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(type(error)) as caught:
                    with instance.brain_lock(r):
                        with events.Command(r, command='wt_setup', node='app') as operation:
                            transitions.inbox_append(r / 'nodes/app', '- runtime: PRIVATE-EXIT\n', nonblocking=True)
                            raise error
                self.assertIs(caught.exception, error)
                records = self.records(r)
                terminal = [e for e in records if e['ref'] == operation.ref][-1]
                self.assertEqual(terminal['kind'], 'abort')
                self.assertEqual(terminal['data']['command'], 'wt_setup')
                self.assertEqual(events.pending(records), {})
                hook = [e for e in records if e['kind'] == 'hook'][-1]
                self.assertEqual(hook['data']['resources'][0]['hash'],
                                 events.digest((r / 'nodes/app/inbox.md').read_bytes()))
                self.assertNotIn('PRIVATE-EXIT', (r / '.brain/events.jsonl').read_text())

    def test_failed_setup_with_hook_and_partial_write_stays_pending(self):
        r = self.init('demo')
        self.b(r, 'new', 'app', '--title', 'App')
        error = RuntimeError('fixture primary')
        with self.assertRaises(RuntimeError) as caught:
            with instance.brain_lock(r):
                with events.Command(r, command='wt_setup', node='app') as operation:
                    transitions.inbox_append(r / 'nodes/app', '- runtime: fixture exit\n', nonblocking=True)
                    (r / 'nodes/app/backlog.md').write_text('- retained partial write\n')
                    raise error
        self.assertIs(caught.exception, error)
        records = self.records(r)
        self.assertEqual(set(events.pending(records)), {operation.ref})
        self.assertFalse(any(e['ref'] == operation.ref and e['kind'] in ('abort', 'commit') for e in records))
        self.assertTrue(any(e['kind'] == 'hook' for e in records))
        self.assertEqual((r / 'nodes/app/backlog.md').read_text(), '- retained partial write\n')

    def test_scripted_day_records_each_brain_write_and_omits_external_git(self):
        r = self.init('demo')
        home = self.repo(self.tmp / 'code')
        self.b(r, 'new', 'app', '--title', 'App', '--home', str(home))
        count = len(self.commits(r))
        commands = [
            ('set', 'app', 'preview=none'),
            ('wt', 'add', 'app', 'job-one', '--no-setup'),
            ('handback', 'app', '--branch', 'job-one', '--state', 'done', '--summary', 'PRIVATE-SUMMARY', '--lessons', 'none: PRIVATE-LESSON'),
            ('review', 'app', '--branch', 'job-one', '--built-by', 'codex', '--by', 'claude'),
            ('inbox', 'app', '--add', 'PRIVATE-INBOX'),
            ('schedule', 'add', 'tick', '--node', 'app', '--when', 'daily', '--brief', 'PRIVATE-SCHEDULE'),
        ]
        for args in commands:
            with self.subTest(command=args[0]):
                self.b(r, *args)
                count += 1
                self.assertEqual(len(self.commits(r)), count)
        local = json.loads((r / '.brain/local.json').read_text())
        wt = Path(local['nodes']['app']['worktrees']['job-one']['path'])
        self.git('commit', '--allow-empty', '-qm', 'worker change', cwd=wt)
        self.git('merge', '-q', 'job-one', cwd=home)
        self.assertEqual(len(self.commits(r)), count)
        self.b(r, 'jobs', 'app', '--branch', 'job-one', '--lessons', 'none: PRIVATE-LESSON')
        self.b(r, 'wt', 'prune', 'app', '--offline')
        self.assertEqual(len(self.commits(r)), count + 2)
        journal = (r / '.brain/events.jsonl').read_text()
        for message in ('PRIVATE-SUMMARY', 'PRIVATE-LESSON', 'PRIVATE-INBOX', 'PRIVATE-SCHEDULE'):
            self.assertNotIn(message, journal)
        self.assertEqual(events.pending(self.records(r)), {})
        inst = instance.Instance(r)
        self.assertEqual(events.normalize(events.events_view(inst, inst.node('app'))),
                         events.normalize(events.snapshot_view(inst, inst.node('app'))))


class EventViews(Scenario):
    cli_in_process = True
    fixture_runtime_only_core = True
    fixture_init_in_process = True
    fixture_templates = True
    def tracked_state(self, root, paths):
        return {name: ((root / name).read_bytes() if (root / name).exists() else None,
                       self.git('ls-files', '--stage', '--', name, cwd=root),
                       self.git('diff', '--cached', '--binary', '--', name, cwd=root))
                for name in paths}

    def projection_dirt(self, root, prefix=''):
        status = root / prefix / 'status.md'
        archive = root / prefix / 'status-archive.md'
        text = __import__('re').sub(r'(?ms)^## Now\n.*?(?=^## |\Z)',
                                   '## Now\n\n' + ''.join('- operator note %d\n' % i for i in range(35)) + '\n',
                                   status.read_text())
        status.write_text(text + '\nOperator fact: retain this prose.\n')
        archive.write_text('operator archive\n')
        names = [str(status.relative_to(root)), str(archive.relative_to(root))]
        self.git('add', '--', *names, cwd=root)
        status.write_text(status.read_text() + '\nUnstaged operator prose.\n')
        archive.write_text(archive.read_text() + 'unstaged archive\n')
        return names

    def test_new_root_and_child_preserve_preexisting_tracked_and_staged_state(self):
        r = self.init('demo')
        self.b(r, 'new', 'projects', '--title', 'Projects')
        paths = self.projection_dirt(r) + self.projection_dirt(r, 'nodes/projects')
        (r / 'foreign.txt').write_text('staged foreign fixture\n')
        self.git('add', '--', 'foreign.txt', cwd=r)
        paths.append('foreign.txt')
        private = r / '.brain/private-fixture'
        private.write_bytes(b'private fixture\n')
        before = self.tracked_state(r, paths)
        self.b(r, 'new', 'other', '--title', 'Other')
        self.b(r, 'new', 'projects/app', '--title', 'App')
        self.assertEqual(self.tracked_state(r, paths), before)
        self.assertEqual(private.read_bytes(), b'private fixture\n')
        for node in ('nodes/other', 'nodes/projects/nodes/app'):
            self.assertIn('Facts refreshed:', (r / node / 'status.md').read_text())
        self.assertEqual(events.pending(events.read(instance.Instance(r))), {})

    def adoption_projection_case(self, parent, count):
        r = self.init('demo')
        if parent != 'root':
            self.b(r, 'new', parent, '--title', 'Projects')
        homes = [self.repo(self.tmp / ('code' + str(i))) for i in range(count)]
        external = [{p.relative_to(h): p.read_bytes() for p in h.rglob('*')
                     if p.is_file() and '.git' not in p.parts} for h in homes]
        paths = self.projection_dirt(r)
        if parent != 'root':
            paths += self.projection_dirt(r, 'nodes/' + parent)
        before = self.tracked_state(r, paths)
        head = self.git('rev-parse', 'HEAD', cwd=r).strip()
        self.b(r, 'adopt', *map(str, homes), '--parent', parent)
        self.assertEqual(self.tracked_state(r, paths), before)
        messages = self.git('log', '--format=%s', head + '..HEAD', cwd=r)
        self.assertNotIn('refresh status facts', messages)
        inst = instance.Instance(r)
        for i, home in enumerate(homes):
            nid = ('code' + str(i)) if parent == 'root' else parent + '/code' + str(i)
            n = inst.node(nid)
            self.assertIn('Facts refreshed:', (n['dir'] / 'status.md').read_text())
            self.assertIn('import:', (n['dir'] / 'job-ledger.md').read_text())
            self.assertEqual({p.relative_to(home): p.read_bytes() for p in home.rglob('*')
                              if p.is_file() and '.git' not in p.parts}, external[i])
        self.assertEqual(events.pending(events.read(inst)), {})
        self.assertTrue(events.read(inst)[-1]['data']['maintenance_ok'])

    def test_adopt_root_multi_defers_projection(self):
        self.adoption_projection_case('root', 2)

    def test_adopt_root_single_defers_projection(self):
        self.adoption_projection_case('root', 1)

    def test_adopt_child_single_defers_projection(self):
        self.adoption_projection_case('projects', 1)

    def test_adopt_child_multi_defers_projection(self):
        self.adoption_projection_case('projects', 2)

    def test_explicit_import_first_duplicate_and_explicit_refresh_freshness(self):
        from brain import briefs, jobs, status
        r = self.init('demo')
        home = self.repo(self.tmp / 'code')
        self.b(r, 'new', 'app', '--title', 'App', '--home', str(home))
        inst = instance.Instance(r)
        n = inst.node('app')
        branch = briefs.import_job_id(home)
        paths = self.projection_dirt(r, 'nodes/app')
        before = self.tracked_state(r, paths)
        for _ in range(2):
            self.b(r, 'jobs', 'app', '--branch', branch, '--start')
            self.assertEqual(self.tracked_state(r, paths), before)
            self.assertEqual(len(jobs.read_jobs(instance.Instance(r).node('app'))), 1)
        self.assertIsNotNone(status.status_lag(instance.Instance(r), n))
        self.b(r, 'jobs', 'app', '--branch', 'import:wrong', '--start', ok=False)
        self.assertEqual(self.tracked_state(r, paths), before)
        self.assertEqual(events.read(instance.Instance(r))[-1]['kind'], 'abort')
        self.b(r, 'status', 'app', '--refresh')
        self.assertIsNone(status.status_lag(instance.Instance(r), instance.Instance(r).node('app')))
        self.assertIn('operator note 34', (n['dir'] / 'status-archive.md').read_text())
        self.assertEqual(events.pending(events.read(instance.Instance(r))), {})

    def test_creation_abort_partial_and_private_connect_keep_native_lifecycle(self):
        r = self.init('demo')
        inst = instance.Instance(r)
        before = (r / 'status.md').read_bytes()
        clock = inst.now_dt() + __import__('datetime').timedelta(minutes=2)
        with mock.patch.object(instance.Instance, 'now_dt', return_value=clock), instance.brain_lock(r):
            with events.Command(r, command='connect'):
                pass
            self.assertEqual((r / 'status.md').read_bytes(), before)
            with self.assertRaises(RuntimeError):
                with events.Command(r, command='new'):
                    raise RuntimeError('before body write')
            self.assertEqual(events.read(inst)[-1]['kind'], 'abort')
            with self.assertRaises(RuntimeError):
                with events.Command(r, command='adopt') as op:
                    self.assertTrue(events.lease_live(inst, op.ref))
                    (r / 'backlog.md').write_text('partial fixture\n')
                    raise RuntimeError('after body write')
            self.assertFalse(events.lease_live(inst, op.ref))
        self.assertEqual(len(events.pending(events.read(inst))), 1)
        self.assertEqual((r / 'status.md').read_bytes(), before)

    def test_shared_creation_defers_tracked_projection_until_explicit_refresh(self):
        from brain import common, nodes, status
        r = self.init('demo')
        path = r / 'status.md'
        before = path.read_bytes()
        inst = instance.Instance(r)
        clock = inst.now_dt() + __import__('datetime').timedelta(minutes=2)
        with mock.patch.object(instance.Instance, 'now_dt', return_value=clock), instance.brain_lock(r):
            with events.Command(r, command='new') as op:
                self.assertTrue(events.lease_live(op.inst, op.ref))
                nodes.create_node(inst, 'app', 'App')
            self.assertFalse(events.lease_live(op.inst, op.ref))
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue((r / 'nodes/app/status.md').exists())
        self.assertEqual(events.pending(events.read(inst)), {})
        self.assertTrue(events.read(inst)[-1]['data']['maintenance_ok'])
        self.assertNotEqual(events.replay(inst)[1], {})
        node_path = r / 'node.md'
        fm, body = common.read_fm(node_path)
        fm['preview_url'] = 'https://example.invalid/shared-explicit-refresh-fixture'
        common.write_fm(node_path, fm, body)
        inst = instance.Instance(r)
        self.assertIsNotNone(status.status_lag(inst, inst.node('root')))
        self.assertEqual(path.read_bytes(), before)
        self.b(r, 'status', 'root', '--refresh')
        self.assertNotEqual(path.read_bytes(), before)
        self.assertIsNone(status.status_lag(instance.Instance(r), instance.Instance(r).node('root')))
        self.assertIn('\nPublished preview: https://example.invalid/shared-explicit-refresh-fixture\n',
                      path.read_text())

    def test_connect_shared_mutation_still_materializes_projection(self):
        for during_maintenance in (False, True):
            with self.subTest(during_maintenance=during_maintenance):
                r = self.init('demo-' + str(during_maintenance).lower())
                inst = instance.Instance(r)
                before = (r / 'status.md').read_bytes()
                clock = inst.now_dt() + __import__('datetime').timedelta(minutes=1)
                dispatch = events.dispatch_landings
                def shared_write():
                    (r / 'backlog.md').write_text('# Backlog\n\n- public pending work\n')
                def maintain_write(inst):
                    shared_write()
                    return dispatch(inst)
                with mock.patch.object(instance.Instance, 'now_dt', return_value=clock), \
                        mock.patch.object(events, 'dispatch_landings',
                                          side_effect=maintain_write if during_maintenance else dispatch), \
                        instance.brain_lock(r):
                    with events.Command(r, command='connect') as op:
                        self.assertTrue(events.lease_live(op.inst, op.ref))
                        if not during_maintenance:
                            shared_write()
                self.assertFalse(events.lease_live(op.inst, op.ref))
                self.assertNotEqual((r / 'status.md').read_bytes(), before)
                self.assertIn('Facts refreshed: ' + clock.strftime('%Y-%m-%d %H:%M %z')[:-2] + ':00',
                              (r / 'status.md').read_text())
                self.assertEqual(events.read(inst)[-1]['data']['resources'][0]['id'],
                                 events.digest(b'backlog.md'))
                self.assertEqual(events.pending(events.read(inst)), {})
                self.assertTrue(events.read(inst)[-1]['data']['maintenance_ok'])

    def test_node_refresh_does_not_adopt_a_foreign_live_subtree(self):
        r = self.init('demo')
        for nid in ('app', 'app/child', 'other'):
            self.b(r, 'new', nid, '--title', nid)
        inst = instance.Instance(r)
        with instance.brain_lock(r):
            with events.Command(r, command='fixture', node='app') as op:
                (r / 'nodes/app/nodes/child/backlog.md').write_text('- pending\n')
                count = len(events.read(inst))
                with mock.patch.object(events, '_CURRENT', []):
                    events.reconcile(inst, node=inst.node('other'))
                    self.assertEqual(len(events.read(inst)), count)
                    self.assertTrue(events.lease_live(inst, op.ref))
                    for node in (None, inst.node('app/child')):
                        with self.assertRaises(SystemExit):
                            events.reconcile(inst, node=node)
                    local = r / '.brain/local.json'
                    local.write_text(local.read_text() + ' ')
                    with self.assertRaises(SystemExit):
                        events.reconcile(inst, node=inst.node('other'))

    def test_released_root_writer_keeps_event_reconciliation_out_of_descendants(self):
        r = self.init('demo')
        self.b(r, 'new', 'app', '--title', 'App')
        inst = instance.Instance(r)
        with instance.brain_lock(r):
            with events.Command(r, command='fixture', node='root') as op:
                (r / 'backlog.md').write_text('- root writer pending\n')
                before = (r / '.brain/events.jsonl').read_bytes()
                forged = argparse.Namespace(ref=op.ref, inst=argparse.Namespace(root=r / 'foreign'))
                for current in ([], [forged]):
                    with mock.patch.object(events, '_CURRENT', current):
                        # File-backed refresh still defers a healthy live writer.
                        events.reconcile(inst, node=inst.node('app'), snapshot=True)
                        with self.assertRaisesRegex(SystemExit, 'another write is active'):
                            events.reconcile(inst, node=inst.node('app'))
                        self.assertEqual((r / '.brain/events.jsonl').read_bytes(), before)
                        self.assertTrue(events.lease_live(inst, op.ref))

    def test_snapshot_refresh_does_not_hide_partial_or_damaged_operations_beside_live_writer(self):
        for fault in ('partial', 'damage', 'invalid'):
            with self.subTest(fault=fault):
                r = self.init('demo-' + fault)
                for nid in ('app', 'other'):
                    self.b(r, 'new', nid, '--title', nid)
                inst = instance.Instance(r)
                with instance.brain_lock(r):
                    with events.Command(r, command='fixture', node='app') as op:
                        ref = 'a' * 32
                        if fault == 'partial':
                            events.append(inst, 'other', 'begin', ref, {'command': 'interrupted'})
                            (r / 'nodes/other/backlog.md').write_text('- partial write\n')
                        elif fault == 'damage':
                            events.append(inst, 'other', 'damage', ref, {'reason': 'fixture damage'})
                        else:
                            events.append(inst, 'other', 'commit', ref,
                                          {'invalidate': [events.node_ref('other')]})
                        before = (r / '.brain/events.jsonl').read_bytes()
                        pending = events.pending(events.read(inst))
                        with mock.patch.object(events, '_CURRENT', []):
                            with self.assertRaisesRegex(SystemExit, 'another write is active'):
                                events.reconcile(inst, node=inst.node('other'), snapshot=True)
                            self.assertEqual((r / '.brain/events.jsonl').read_bytes(), before)
                            self.assertEqual(events.pending(events.read(inst)), pending)
                            self.assertTrue(events.lease_live(inst, op.ref))

    records = EventAppend.records
    commits = EventAppend.commits
    def view(self, root, node='app'):
        return json.loads(self.b(root, 'status', node, '--from-events', '--json'))

    def test_events_view_matches_refreshed_snapshot_and_never_reads_node_sources(self):
        r = self.init('demo')
        self.b(r, 'new', 'app', '--title', 'App')
        self.b(r, 'inbox', 'app', '--add', 'MESSAGE-PRIVATE')
        self.b(r, 'status', 'app', '--refresh')
        inst = instance.Instance(r)
        expected = events.snapshot_view(inst, inst.node('app'))
        actual = self.view(r)
        self.assertEqual(events.normalize(actual), events.normalize(expected))
        with mock.patch.object(events, 'snapshot_view', side_effect=AssertionError('read snapshot')):
            self.assertEqual(events.events_view(inst, inst.node('app')), actual)
        self.assertNotIn('MESSAGE-PRIVATE', json.dumps(actual))
        self.assertTrue(json.loads(self.b(r, 'events', 'app', '--compare', '--json'))['current']['equal'])

    def test_missing_event_is_detected_before_next_write_and_reconciled_by_refresh(self):
        r = self.init('demo')
        self.b(r, 'new', 'app', '--title', 'App')
        self.b(r, 'backlog', 'app', 'add', 'retained item')
        path = r / '.brain/events.jsonl'
        lines = path.read_text().splitlines()
        removed = json.loads(lines.pop())
        self.assertEqual(removed['kind'], 'commit')
        path.write_text('\n'.join(lines) + '\n')  # mutation control: skip the terminal hook
        self.b(r, 'status', 'app', '--from-events', ok=False)
        result = json.loads(self.b(r, 'events', 'app', '--compare', '--json'))
        self.assertFalse(result['current']['equal'])
        self.assertGreater(result['differences'], 0)
        self.b(r, 'set', 'app', 'runtime=codex')  # must not hide the original gap
        self.b(r, 'status', 'app', '--refresh')
        result = json.loads(self.b(r, 'events', 'app', '--compare', '--json'))
        self.assertGreater(result['differences'], 0)
        self.assertTrue(result['current']['equal'])
        self.assertTrue(any(e['kind'] == 'reconcile' and e['data']['flagged'] for e in self.records(r)))
        self.assertIn('retained item', (r / 'nodes/app/backlog.md').read_text())
        self.view(r)

    def test_live_operation_cannot_be_reconciled_and_event_reader_fails_closed(self):
        r = self.init('demo')
        self.b(r, 'new', 'app', '--title', 'App')
        inst = instance.Instance(r)
        with instance.brain_lock(r):
            with events.Command(r, command='fixture', node='app'):
                before = len(self.records(r))
                events.reconcile(inst)
                self.assertEqual(len(self.records(r)), before)
                with self.assertRaises(SystemExit):
                    events.events_view(inst, inst.node('app'))

    def test_switch_is_explicit_and_requires_a_clean_day(self):
        r = self.init('demo')
        self.b(r, 'new', 'app', '--title', 'App')
        self.b(r, 'set', 'app', 'status_source=events', ok=False)
        self.assertNotIn('status_source: events', (r / 'nodes/app/node.md').read_text())
        inst = instance.Instance(r)
        first = inst.now_dt() - __import__('datetime').timedelta(days=1, minutes=1)
        with mock.patch.object(inst, 'now', return_value=first.strftime('%Y-%m-%d %H:%M +00:00')):
            events.append_line(r / '.brain/events-compare.jsonl', {'ts': inst.now(), 'node': 'app', 'kind': 'comparison', 'equal': True})
        self.b(r, 'status', 'app', '--refresh')
        self.b(r, 'set', 'app', 'status_source=events')
        self.assertIn('status_source: events', (r / 'nodes/app/node.md').read_text())
        self.b(r, 'backlog', 'app', 'add', 'new item after switch')
        self.b(r, 'status', 'app', '--refresh')
        self.view(r)
        self.assertTrue((r / 'nodes/app/status.md').exists())

    def test_facts_refresh_on_write_active_table_and_now_archive_without_extra_commits(self):
        r = self.init('demo')
        home = self.repo(self.tmp / 'code')
        self.b(r, 'new', 'app', '--title', 'App', '--home', str(home))
        self.b(r, 'wt', 'add', 'app', 'job-one', '--no-setup')
        st = r / 'nodes/app/status.md'
        self.assertIn('| job-one | - | - | open |', st.read_text())
        text = st.read_text()
        start = text.index('## Now\n')
        end = text.index('## Next', start)
        text = text[:start] + '## Now\n\n' + ''.join('- note %d\n' % i for i in range(35)) + '\n' + text[end:]
        st.write_text(text)
        count = int(self.git('rev-list', '--count', 'HEAD', cwd=r))
        self.b(r, 'backlog', 'app', 'add', 'second item')
        self.assertEqual(int(self.git('rev-list', '--count', 'HEAD', cwd=r)), count + 1)
        now = st.read_text().split('## Now\n', 1)[1].split('## Next', 1)[0]
        self.assertEqual(sum(bool(line.strip()) for line in now.splitlines()), 30)
        archive = (r / 'nodes/app/status-archive.md').read_text()
        for i in range(30, 35):
            self.assertIn('- note %d\n' % i, archive)
        self.b(r, 'handback', 'app', '--branch', 'job-one', '--state', 'blocked', '--summary', 'PRIVATE-SUMMARY', '--lessons', 'none: PRIVATE-BODY')
        self.assertIn('| job-one | - | - | blocked |', st.read_text())
        self.assertNotIn('PRIVATE-BODY', (r / '.brain/events.jsonl').read_text())

    def test_hook_fold_refreshes_its_node_even_when_next_command_targets_another(self):
        r = self.init('demo')
        self.b(r, 'new', 'app', '--title', 'App')
        self.b(r, 'new', 'other', '--title', 'Other')
        transitions.inbox_append(r / 'nodes/app', '- runtime: private question\n', nonblocking=True)
        self.b(r, 'status', 'app', '--from-events', ok=False)
        self.b(r, 'backlog', 'other', 'add', 'different node')
        actual = self.view(r)
        inst = instance.Instance(r)
        self.assertEqual(events.normalize(actual), events.normalize(events.snapshot_view(inst, inst.node('app'))))

    def test_missing_entire_hook_is_detected_and_explanation_does_not_store_message(self):
        r = self.init('demo')
        self.b(r, 'new', 'app', '--title', 'App')
        self.b(r, 'backlog', 'app', 'add', 'persisted without hooks')
        path = r / '.brain/events.jsonl'
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        ref = rows[-1]['ref']
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows if row['ref'] != ref))
        self.b(r, 'status', 'app', '--refresh')
        result = json.loads(self.b(r, 'events', 'app', '--compare', '--json'))
        self.assertGreater(result['differences'], 0)
        self.assertTrue(result['current']['equal'])
        self.assertTrue(result['unexplained'])
        key = result['unexplained'][0]
        self.b(r, 'events', 'app', '--compare', '--explain', key, '--reason', 'PRIVATE-EXPLANATION')
        self.assertNotIn(key, json.loads(self.b(r, 'events', 'app', '--compare', '--json'))['unexplained'])
        self.assertNotIn('PRIVATE-EXPLANATION', path.read_text())

    def test_job_setup_has_its_own_operation_and_failure_keeps_state_event(self):
        r = self.init('demo')
        home = self.repo(self.tmp / 'code')
        self.b(r, 'new', 'app', '--title', 'App', '--home', str(home))
        (r / 'nodes/app/worktree-setup.sh').write_text('exit 7\n')
        self.b(r, 'wt', 'add', 'app', 'setup-job', ok=False)
        records = self.records(r)
        self.assertTrue(any(e['kind'] == 'commit' and e['data'].get('command') == 'wt_add' for e in records))
        self.assertTrue(any(e['kind'] == 'abort' and e['data'].get('command') == 'wt_setup' for e in records))
        self.assertEqual(events.pending(records), {})
        self.assertIn('setup-job', (r / 'nodes/app/job-ledger.md').read_text())

    def test_partial_hook_line_is_quarantined_without_truncating_outbox(self):
        r = self.init('demo')
        self.b(r, 'new', 'app', '--title', 'App')
        outbox = r / '.brain/hook-events.jsonl'
        outbox.write_bytes(b'{"unfinished":')
        inst = instance.Instance(r)
        with instance.brain_lock(r):
            events.fold_hooks(inst)
        self.assertEqual(outbox.read_bytes(), b'{"unfinished":')
        self.assertEqual((r / '.brain/hook-events-offset').read_text(), str(len(b'{"unfinished":')))
        self.assertTrue(list((r / '.brain').glob('hook-events.rejected.*')))
        with self.assertRaises(SystemExit):
            events.events_view(inst, inst.node('app'))

    def test_active_work_preserves_thread_refs_through_close_and_until_settlement(self):
        r = self.init('demo')
        home = self.repo(self.tmp / 'code')
        self.b(r, 'new', 'app', '--title', 'App', '--home', str(home))
        self.b(r, 'wt', 'add', 'app', 'job-one', '--no-setup')
        st = r / 'nodes/app/status.md'
        st.write_text(st.read_text().replace('| job-one | - | - | open |', '| job-one | fixture-thread | #12 | open |'))
        self.b(r, 'backlog', 'app', 'add', 'retain references')
        self.assertIn('| job-one | fixture-thread | #12 | open |', st.read_text())
        self.b(r, 'jobs', 'app', '--branch', 'job-one', '--close', '--reason', 'fixture complete')
        self.assertIn('| job-one | fixture-thread | #12 | closed |', st.read_text())
        self.assertIn('fixture-thread', self.b(r, 'jobs', 'app', '--branch', 'job-one'))
        self.b(r, 'jobs', 'app', '--branch', 'job-one', '--settled', '--attestation-file', settlement_export(self, r, 'app', branch='job-one'))
        self.assertNotIn('| job-one |', st.read_text())

    def test_projection_preserves_bound_source_states_over_completed_ledger(self):
        from brain import jobs, status
        r = self.init('demo')
        home = self.repo(self.tmp / 'code')
        self.b(r, 'new', 'app', '--title', 'App', '--home', str(home))
        self.b(r, 'wt', 'add', 'app', 'job-one', '--no-setup')
        inst = instance.Instance(r)
        n = inst.node('app')
        rows = jobs.read_jobs(n)
        rows[0].update(Ended='2026-01-01T00:00:00+00:00', Outcome='merged')
        jobs.write_jobs(inst, n, rows)  # ledger data alone is not an owner transition
        path = n['dir'] / 'status.md'
        for state in ('working', 'unknown', '', 'review — merged into preview',
                      'merged — fixture complete', 'done: fixture complete'):
            for branch in ('`job-one`', '`job-one` (fixture annotation)'):
                with self.subTest(state=state, branch=branch):
                    path.write_text('## Active work\n| Branch | Thread | PR | State |\n'
                                    + '| ' + branch + ' | fixture-thread | #12 | ' + state + ' |\n')
                    expected = [['job-one', 'fixture-thread', '#12', state]]
                    self.b(r, 'backlog', 'app', 'add', 'fixture refresh')
                    self.assertEqual(self.view(r)['active'], expected)
                    table = path.read_text().split('## Active work', 1)[1]
                    self.b(r, 'status', 'app', '--refresh')
                    self.assertEqual(self.view(r)['active'], expected)
                    self.assertEqual(path.read_text().split('## Active work', 1)[1], table)
                    self.assertEqual(status.active_work_closed(expected[0]),
                                     status.active_work_closed(status.active_work(n, include_closed=True)[0]))

    def test_cleanup_records_replay_view_without_materializing_status_until_refresh(self):
        for action in ('prune', 'rm'):
            with self.subTest(action=action):
                r = self.init('demo-' + action)
                home = self.repo(self.tmp / ('code-' + action))
                self.b(r, 'new', 'app', '--title', 'App', '--home', str(home))
                self.b(r, 'wt', 'add', 'app', 'job-one', '--no-setup')
                local = json.loads((r / '.brain/local.json').read_text())
                wt = Path(local['nodes']['app']['worktrees']['job-one']['path'])
                self.git('commit', '--allow-empty', '-qm', 'fixture complete', cwd=wt)
                self.git('merge', '-q', 'job-one', cwd=home)
                path = r / 'nodes/app/status.md'
                archive = path.with_name('status-archive.md')
                path.write_text('## Now\n\n' + ''.join('- fixture note %d\n' % i for i in range(35))
                                + '\n## Active work\n| Branch | Thread | PR | State |\n'
                                  '| `job-one` (fixture annotation) | fixture-thread | - | merged — fixture complete |\n')
                before = path.read_bytes()
                self.assertFalse(archive.exists())
                count = len(self.commits(r))
                args = ('--offline',) if action == 'prune' else ('job-one',)
                self.b(r, 'wt', action, 'app', *args)
                self.assertFalse(wt.exists())
                self.assertEqual(path.read_bytes(), before)
                self.assertFalse(archive.exists())
                commits = self.commits(r)
                self.assertEqual(len(commits), count + 1)
                self.assertEqual(commits[-1]['data']['command'], 'wt_' + action)
                self.assertTrue(commits[-1]['data']['maintenance_ok'])
                self.assertEqual(events.pending(self.records(r)), {})
                view = self.view(r)
                self.assertEqual(commits[-1]['data']['views'][events.node_ref('app')], view)
                self.assertEqual(view['active'], [['job-one', 'fixture-thread', '-', 'merged — fixture complete']])
                self.b(r, 'status', 'app', '--refresh')
                self.assertNotEqual(path.read_bytes(), before)
                self.assertIn('## Facts (generated', path.read_text())
                self.assertTrue(archive.exists())
                self.assertIn('- fixture note 34', archive.read_text())
                self.assertEqual(self.view(r)['active'], view['active'])

    def test_projection_retains_ambiguous_unmatched_and_incomplete_owners(self):
        from brain import jobs, status
        r = self.init('demo')
        home = self.repo(self.tmp / 'code')
        self.b(r, 'new', 'app', '--title', 'App', '--home', str(home))
        self.b(r, 'wt', 'add', 'app', 'job-one', '--no-setup')
        inst = instance.Instance(r)
        n = inst.node('app')
        rows = jobs.read_jobs(n)
        rows[0].update(Ended='2026-01-01T00:00:00+00:00', Outcome='merged')
        path = n['dir'] / 'status.md'
        source = ('## Active work\n| Branch | Thread | PR | State |\n'
                  '| `job-one` | fixture-first | - | working |\n'
                  '| job-one (fixture annotation) | fixture-second | - | unknown |\n'
                  '| orphan | fixture-orphan | - | working |\n'
                  '| incomplete | fixture-incomplete |\n')
        for mode in ('duplicate-source', 'duplicate-ledger', 'settled-ledger', 'missing-ledger'):
            with self.subTest(mode=mode):
                ledger = [dict(rows[0])]
                if mode == 'duplicate-ledger':
                    ledger.append({**rows[0], 'Started': '2025-01-01T00:00:00+00:00'})
                elif mode == 'settled-ledger':
                    ledger[0]['Settled'] = '2026-01-01T00:01:00+00:00'
                elif mode == 'missing-ledger':
                    ledger = []
                jobs.write_jobs(inst, n, ledger)
                path.write_text(source)
                first = None
                for _ in range(2):
                    view = events.snapshot_view(inst, n)
                    bound = {cells[1]: cells[3] for cells in view['active'] if cells[1] != '-'}
                    self.assertEqual(bound, {'fixture-first': 'working', 'fixture-second': 'unknown',
                                             'fixture-orphan': 'working', 'fixture-incomplete': 'unknown'})
                    if first is not None:
                        self.assertEqual(view['active'], first)
                    first = view['active']
                    events.active_table(inst, n, view)
                events.close_active_work(inst, n, rows[0])
                self.assertEqual(status.active_work(n, include_closed=True), first)

    def test_status_commit_is_hourly_and_skips_someone_elses_judgement(self):
        r = self.init('demo')
        self.b(r, 'new', 'app', '--title', 'App')
        self.b(r, 'status', 'app', '--refresh')
        inst = instance.Instance(r)
        n = inst.node('app')
        with instance.brain_lock(inst):
            with mock.patch.object(inst, '_commit') as commit:
                events.status_commit(inst, n)
                commit.assert_not_called()
            stamp = r / '.brain/events-status-commits.json'
            stamp.write_text('{}')
            with mock.patch.object(inst, '_commit') as commit:
                events.status_commit(inst, n)
                commit.assert_called_once()
            path = n['dir'] / 'status.md'
            original = path.read_bytes()
            path.write_bytes(original + b' ')
            with mock.patch.object(inst, '_commit') as commit:
                events.status_commit(inst, n, force=True)
                commit.assert_not_called()
            path.write_bytes(original.replace(b'\n', b'\r\n'))
            with mock.patch.object(inst, '_commit') as commit:
                events.status_commit(inst, n, force=True)
                commit.assert_not_called()
            path.write_bytes(original)
            path.write_text(path.read_text().replace('- Node created.', '- Other coordinator draft.'))
            with mock.patch.object(inst, '_commit') as commit:
                events.status_commit(inst, n, force=True)
                commit.assert_not_called()

    def test_unlocked_wait_does_not_claim_other_commands_writes_or_leave_false_pending(self):
        r = self.init('demo')
        self.b(r, 'new', 'app', '--title', 'App')
        with instance.brain_lock(r):
            with self.assertRaises(RuntimeError):
                with events.Command(r, command='waiting_writer'):
                    with events.Command(r, command='other_writer', node='app'):
                        (r / 'nodes/app/backlog.md').write_text('- other writer\n')
                    raise RuntimeError('the waiting operation failed without its own writes')
        self.assertEqual(events.pending(self.records(r)), {})
        self.assertEqual(self.records(r)[-1]['kind'], 'abort')
        owner = next(e for e in reversed(self.records(r)) if e['kind'] == 'commit')
        self.assertEqual(owner['data']['command'], 'other_writer')
        self.assertIn('nodes/app/backlog.md', [x['path'] for x in owner['data']['resources']])
