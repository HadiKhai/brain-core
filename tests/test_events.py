"""Metadata-only event stream scenarios; all data and instances are fabricated."""
import argparse
import contextlib
import fcntl
import importlib
import io
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from scenario import Scenario, CORE
from test_jobs import settlement_export

sys.path.insert(0, str(CORE / 'lib'))
from brain import events, instance, transitions


class EventAppend(Scenario):
    def test_provisional_stale_block_preserves_unowned_prose_and_marker_scope(self):
        from brain import status
        root = self.init('stale-prose')
        inst = instance.Instance(root)
        node = inst.node('root')
        path = root / 'status.md'
        original = 'Updated: 2026-01-01 00:00 +00:00\n\n# Status\n\n## Now\n\nHuman prose\n'
        path.write_text(original)
        with mock.patch.object(status, 'facts_block', return_value=status.FACTS_START + '\nFacts\n' + status.FACTS_END):
            expected = status.refreshed_text(inst, node)
            self.assertTrue(status.mark_generated_stale(node))
            self.assertEqual(events.judgement(path.read_text()), original)
            self.assertEqual(status.refreshed_text(inst, node), expected)
        path.write_text(original + '\nA prose quotation: ' + status.FACTS_STALE + '\n')
        self.assertNotIn('generated view stale', status.status_line(node))

    def test_failed_batch_marks_preexisting_invalid_target_without_a_prior_file_marker(self):
        from brain import status
        root = self.init('prior-invalid')
        self.b(root, 'new', 'peer', '--title', 'Peer')
        inst = instance.Instance(root)
        with instance.brain_lock(inst):
            events.append(inst, 'peer', 'reconcile', 'a' * 32, {'invalidate': [events.node_ref('peer')]})
            operation = events.Command(root, command='intent_queue')
            operation.write_batch = True
            operation.project_nodes = ['root', 'peer']
            with mock.patch.object(events, 'released_view', side_effect=RuntimeError('failed view')), \
                    contextlib.redirect_stderr(io.StringIO()), operation:
                pass
        self.assertIn(status.FACTS_STALE, (root / 'nodes/peer/status.md').read_text())
        self.assertIn(events.node_ref('peer'), events.journal(instance.Instance(root)).state['invalid'])

    def test_wt_retries_the_whole_projection_batch_when_an_earlier_node_changes(self):
        root = self.init('batch-projection-race')
        for nid in ('app', 'peer'):
            self.b(root, 'new', nid, '--title', nid.title())
        inst = instance.Instance(root)
        original = events.snapshot_view
        rendered, concurrent = [], []

        def observed(current, node, known):
            self.assertIsNone(instance._LOCK['fh'])
            view = original(current, node, known)
            rendered.append(node['id'])
            if node['id'] == 'peer' and not concurrent:
                request = self.b(root, 'inbox', 'app', '--add', 'concurrent question').strip()
                self.b(root, 'queue', 'wait', request)  # its batch view is published
                concurrent.append(next(r['data']['views'][events.node_ref('app')] for r in reversed(self.records(root))
                                       if events.node_ref('app') in r['data'].get('views', {})))
            return view

        with mock.patch.object(events, 'snapshot_view', side_effect=observed):
            with instance.brain_lock(inst), events.Command(root, command='wt_add', node='app'):
                for nid in ('app', 'peer'):
                    (root / 'nodes' / nid / 'backlog.md').write_text('- durable mutation\n')
        self.assertEqual(rendered, ['app', 'peer', 'app', 'peer'])
        last = self.records(root)[-1]['data']
        self.assertTrue(last['maintenance_ok'])
        app = last['views'][events.node_ref('app')]
        self.assertEqual(app['comparison'], concurrent[0]['comparison'])
        self.assertTrue(any(line.startswith('Due: 1') for line in app['facts']), app['facts'])
        self.assertIn('concurrent question', (root / 'nodes/app/inbox.md').read_text())
        self.assertEqual(events.events_view(inst, inst.node('app'))['comparison'], app['comparison'])
        self.assertEqual(events.pending(self.records(root)), {})

    def test_projection_ref_fence_handles_reftable_storage_and_updates(self):
        root = self.init('reftable-fence')
        home = self.tmp / 'reftable-home'
        result = subprocess.run(['git', 'init', '-q', '--ref-format=reftable', str(home)],
                                env=self.env, capture_output=True, text=True)
        if result.returncode:
            self.skipTest('installed Git does not support reftable fixtures: ' + result.stderr)
        self.git('commit', '--allow-empty', '-qm', 'First', cwd=home)
        inst = instance.Instance(root)
        def inputs():
            return instance.git_state_paths(home, refs={'refs/heads/main', 'refs/remotes/origin/HEAD'},
                                            include_head=False)
        self.assertIn(home / '.git/reftable', inputs())
        with instance.brain_lock(inst), self.assertRaises(instance.StateChanged):
            with instance.released(inst, extra=inputs, retry=True):
                self.git('commit', '--allow-empty', '-qm', 'Concurrent ref update', cwd=home)

    def test_short_writers_prepare_facts_and_machine_text_without_the_writer_lock(self):
        from brain import status
        for command in ('set', 'task', 'handback', 'wt_add', 'wt_setup', 'upgrade', 'intent_queue', 'inbox_seen',
                        'question_answer', 'notify', 'jobs', 'backlog', 'changes', 'check', 'fixture_future_writer'):
            with self.subTest(command=command):
                root = self.init('unlocked-' + command)
                home = self.repo(self.tmp / ('code-' + command))
                self.b(root, 'new', 'app', '--title', 'App', '--home', str(home))
                self.b(root, 'new', 'peer', '--title', 'Peer')
                inst = instance.Instance(root)
                entered = []
                app_reads = []

                def unlocked(original, name):
                    def call(*args, **kwargs):
                        self.assertIsNone(instance._LOCK['fh'], name)
                        entered.append(name)
                        if name == 'file_state' and entered.count(name) == 1:
                            # A real sibling CLI writer must finish while this
                            # command is still in its projection window.
                            self.b(root, 'backlog', 'peer', 'add', 'concurrent writer')
                        return original(*args, **kwargs)
                    return call

                def snapshot(*args, **kwargs):
                    if args[1]['id'] == 'app':
                        app_reads.append(1)
                    return original_snapshot(*args, **kwargs)

                original_snapshot = events.snapshot_view
                with mock.patch.object(events, 'snapshot_view', side_effect=snapshot), \
                        mock.patch.object(status, 'file_state', side_effect=unlocked(status.file_state, 'file_state')), \
                        mock.patch.object(status, 'status_facts', side_effect=unlocked(status.status_facts, 'status_facts')), \
                        mock.patch.object(status._m_machine, 'machine_state',
                                          side_effect=unlocked(status._m_machine.machine_state, 'machine_state')):
                    with instance.brain_lock(inst), events.Command(root, command=command, node='app'):
                        (root / 'nodes/app/backlog.md').write_text('- durable mutation\n')
                self.assertEqual(len(app_reads), 1)
                self.assertIn('status_facts', entered)
                self.assertIn('machine_state', entered)
                self.assertIn('concurrent writer', (root / 'nodes/peer/backlog.md').read_text())
                records = self.records(root)
                self.assertEqual(events.pending(records), {})
                self.assertEqual(records[-1]['data']['maintenance_ok'], command in events.SCOPED_PROJECTIONS)

    def test_writer_timing_phases_never_overlap_facts_and_physical_lock_hold(self):
        from brain import status, timings
        for command in ('question_answer', 'jobs', 'wt_add', 'task_apply_plan'):
            with self.subTest(command=command):
                root = self.init('timing-' + command)
                inst, intervals = instance.Instance(root), []
                record = timings.record
                def observed(name, seconds):
                    end = time.perf_counter()
                    if name in ('status_facts', 'lock_hold'):
                        intervals.append((name, end - seconds, end))
                    record(name, seconds)
                with mock.patch.object(timings, 'record', side_effect=observed):
                    with instance.brain_lock(inst), events.Command(root, command=command):
                        (root / 'backlog.md').write_text('- durable mutation\n')
                facts = [r for r in intervals if r[0] == 'status_facts']
                holds = [r for r in intervals if r[0] == 'lock_hold']
                self.assertTrue(facts and holds, intervals)
                for _, start, end in facts:
                    for _, locked, unlocked in holds:
                        self.assertTrue(end <= locked or start >= unlocked, intervals)
                self.assertTrue(self.records(root)[-1]['data']['maintenance_ok'])

    def test_direct_refresh_and_reconciliation_prepare_facts_unlocked(self):
        from brain import status
        root = self.init('direct-projection')
        inst = instance.Instance(root)
        facts, observed = status.status_facts, []
        def unlocked(*args, **kwargs):
            self.assertIsNone(instance._LOCK['fh'])
            observed.append(True)
            return facts(*args, **kwargs)
        with mock.patch.object(status, 'status_facts', side_effect=unlocked):
            with instance.brain_lock(inst), events.Command(root, command='check'):
                status.refresh_status(inst, inst.node('root'))
                status.due_evidence(inst, inst.node('root'))
            (root / 'backlog.md').write_text('- external edit\n')
            with instance.brain_lock(inst):
                events.reconcile(inst)
        self.assertTrue(observed)
        self.assertEqual(events.pending(self.records(root)), {})

    def test_short_writer_retries_from_fresh_status_and_preserves_concurrent_prose(self):
        from brain import status
        root = self.init('retry-status')
        inst = instance.Instance(root)
        path = root / 'status.md'
        original = events.snapshot_view
        calls = []

        def edited(current, node, known):
            self.assertIsNone(instance._LOCK['fh'])
            calls.append(list(known))
            view = original(current, node, known)
            if len(calls) == 1:
                text = path.read_text().replace('## Now', 'Concurrent human prose\n\n## Now')
                path.write_text(text)
            return view

        with mock.patch.object(events, 'snapshot_view', side_effect=edited):
            with instance.brain_lock(inst), events.Command(root, command='task'):
                (root / 'backlog.md').write_text('- durable mutation\n')
        self.assertEqual(len(calls), 2)
        self.assertIn('Concurrent human prose', path.read_text())
        self.assertTrue(status.facts_meta(inst.node('root')))
        self.assertTrue(self.records(root)[-1]['data']['maintenance_ok'])
        self.assertEqual(events.pending(self.records(root)), {})

    def test_short_writer_retry_exhaustion_keeps_mutation_and_invalidates_only_the_view(self):
        import contextlib
        import io
        root = self.init('exhaust-status')
        inst = instance.Instance(root)
        path = root / 'status.md'
        original = events.snapshot_view
        calls = []

        def edited(current, node, known):
            self.assertIsNone(instance._LOCK['fh'])
            calls.append(1)
            view = original(current, node, known)
            with path.open('a') as stream:
                stream.write('\nConcurrent human prose\n')
            return view

        with mock.patch.object(events, 'snapshot_view', side_effect=edited), \
                contextlib.redirect_stderr(io.StringIO()):
            with instance.brain_lock(inst), events.Command(root, command='handback'):
                (root / 'backlog.md').write_text('- durable mutation\n')
        self.assertEqual(len(calls), events.RELEASED_PROJECTION_ATTEMPTS)
        self.assertEqual((root / 'backlog.md').read_text(), '- durable mutation\n')
        self.assertEqual(path.read_text().count('Concurrent human prose'), len(calls))
        records = self.records(root)
        self.assertEqual(records[-1]['kind'], 'commit')
        self.assertFalse(records[-1]['data']['maintenance_ok'])
        self.assertIn(events.node_ref('root'), records[-1]['data']['invalidate'])
        self.assertEqual(events.pending(records), {})

    def test_deferred_queue_view_is_invalid_until_one_batch_maintenance(self):
        root = self.init('deferred-view')
        inst = instance.Instance(root)
        with mock.patch.object(events, 'released_view', side_effect=AssertionError('deferred view rendered')):
            with instance.brain_lock(inst):
                op = events.Command(root, command='intent_queue', node='root')
                op.defer_views = True
                with op:
                    (root / 'backlog.md').write_text('- deferred mutation\n')
        commit = self.records(root)[-1]
        self.assertEqual((commit['kind'], commit['ref']), ('commit', op.ref))
        self.assertFalse(commit['data']['maintenance_ok'])
        self.assertTrue(commit['data']['deferred'])
        self.assertIn(events.node_ref('root'), commit['data']['invalidate'])
        self.assertTrue(commit['data']['resources'])
        self.assertIn(events.node_ref('root'), events.journal(instance.Instance(root)).state['invalid'])
        from brain import status
        self.assertIn(status.FACTS_STALE, (root / 'status.md').read_text())
        self.assertIn('generated view stale', status.status_line(inst.node('root')))
        self.assertEqual(status.status_lag(inst, inst.node('root')), 'generated view maintenance pending')
        with self.assertRaisesRegex(SystemExit, 'no recorded view'):
            events.events_view(inst, inst.node('root'))
        with instance.brain_lock(inst):
            batch = events.Command(root, command='intent_queue', node='root')
            batch.write_batch = True
            with batch:
                pass
        begin = next(r for r in self.records(root) if r['ref'] == batch.ref and r['kind'] == 'begin')
        self.assertTrue(begin['data']['write_batch'])
        terminal = self.records(root)[-1]
        self.assertEqual(terminal['ref'], batch.ref)
        self.assertTrue(terminal['data']['maintenance_ok'])
        self.assertIn(events.node_ref('root'), terminal['data']['views'])
        self.assertFalse(events.journal(instance.Instance(root)).state['invalid'])
        self.assertNotIn(status.FACTS_STALE, (root / 'status.md').read_text())
        self.assertNotIn('generated view stale', status.status_line(inst.node('root')))

    def test_resource_scopes_and_gap_scan_discover_nodes_once_per_batch(self):
        root = self.init('batch-scopes')
        self.b(root, 'new', 'app', '--title', 'App')
        inst = instance.Instance(root)
        values = {f'nodes/app/task-{i}.md': events.digest(str(i).encode()) for i in range(200)}
        values.update({'node.md': 'root-hash', '.brain/local.json': 'local-hash',
                       '@ready/app/proof.json': 'ready-hash'})
        expected = [{'id': events.digest(k.encode()), 'scope': events.resource_scope(inst, k),
                     'hash': v} for k, v in sorted(values.items())]
        with mock.patch.object(inst, 'nodes', wraps=inst.nodes) as discover:
            self.assertEqual(events.resource_list(inst, values, display=False), expected)
            self.assertEqual(discover.call_count, 1)
        with mock.patch.object(events, 'resources', return_value=values), \
                mock.patch.object(inst, 'nodes', wraps=inst.nodes) as discover:
            self.assertTrue(events.detect_gaps(inst, 'root', record=False))
            # replay, resource_list, screening, scope lookup; independent of resources.
            self.assertEqual(discover.call_count, 4)

    def test_hook_batch_publishes_one_index_before_cursor_and_replays_after_crash(self):
        root = self.init('hook-batch')
        inst = instance.Instance(root)
        path = root / '.brain/hook-events.jsonl'
        for i in range(12):
            events.append_line(path, {'node': events.node_ref('root'), 'ref': f'{i:032x}',
                                     'ts': inst.now(), 'data': {'resources': []}})
        store = events.journal(inst)
        cursor = root / '.brain/hook-events-offset'
        write = events.common.atomic_write
        def crash_before_cursor(target, *args, **kwargs):
            if target == cursor:
                raise OSError('cursor publication failed')
            return write(target, *args, **kwargs)
        with instance.brain_lock(inst), mock.patch.object(store, 'save', wraps=store.save) as save, \
                mock.patch.object(events.common, 'atomic_write', side_effect=crash_before_cursor):
            with self.assertRaises(OSError):
                events.fold_hooks(inst)
            self.assertEqual(save.call_count, 1)
        events._JOURNALS.pop(str(root.resolve()), None)
        with instance.brain_lock(inst):
            events.fold_hooks(instance.Instance(root))
        hooks = [e for e in self.records(root) if e['kind'] == 'hook']
        self.assertEqual(len(hooks), 12)
        self.assertEqual(int(events.private_read(cursor)), path.stat().st_size)

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

    def test_preparation_failure_keeps_primary_error_and_closes_its_lease(self):
        root = self.init('preparation-failure')
        inst = instance.Instance(root)
        op = events.Command(root, command='status')
        primary, secondary = ValueError('preparation body'), OSError('terminal publication')
        with instance.brain_lock(inst), \
                mock.patch.object(events, 'fold_hooks', side_effect=primary), \
                mock.patch.object(op, 'finish', side_effect=secondary):
            with self.assertRaises(ValueError) as caught:
                op.__enter__()
        self.assertIs(primary, caught.exception)
        self.assertIs(secondary, caught.exception.__cause__)
        self.assertNotIn(op, events._CURRENT)
        self.assertIsNone(op.lease)
        self.assertFalse(events.lease_path(inst, op.ref).exists())

    def test_event_source_refusal_precedes_repair_and_reconstruction_writes(self):
        root = self.init('preflight-refusal')
        (root / 'backlog.md').write_text('- independently changed\n')
        before = {p.relative_to(root).as_posix(): p.read_bytes()
                  for p in (root / '.brain').rglob('*') if p.is_file()}
        inst = instance.Instance(root)
        with instance.brain_lock(inst), mock.patch.object(events, 'source', return_value='events'):
            with self.assertRaises(SystemExit):
                events.Command(root, command='check').__enter__()
        after = {p.relative_to(root).as_posix(): p.read_bytes()
                 for p in (root / '.brain').rglob('*') if p.is_file()}
        self.assertEqual({k: v for k, v in before.items() if k != '.brain/lock'},
                         {k: v for k, v in after.items() if k != '.brain/lock'})

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
        def writes():
            batches = {e['ref'] for e in self.records(r) if e['kind'] == 'begin' and e['data'].get('write_batch')}
            return [c for c in self.commits(r) if c['ref'] not in batches]
        count = len(writes())
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
                result = self.b(r, *args)
                count += 1
                if args[0] == 'inbox' and '--add' in args:
                    self.b(r, 'queue', 'wait', result.strip())
                    # Identify this request's apply and publication independently
                    # of coalesced maintenance for an earlier foreground writer.
                    begin = next(e for e in self.records(r) if e['kind'] == 'begin'
                                 and e['data'].get('write_request') == result.strip())
                    apply = next(c for c in writes() if c['ref'] == begin['ref'])
                    self.assertTrue(apply['data']['deferred'])
                    marker = json.loads((r / '.brain/write-queue' / (result.strip() + '.view')).read_text())
                    published = next(c for c in self.commits(r) if c['ref'] == marker['ref'])
                    self.assertTrue(published['data']['maintenance_ok'])
                self.assertEqual(len(writes()), count)
        local = json.loads((r / '.brain/local.json').read_text())
        wt = Path(local['nodes']['app']['worktrees']['job-one']['path'])
        self.git('commit', '--allow-empty', '-qm', 'worker change', cwd=wt)
        self.git('merge', '-q', 'job-one', cwd=home)
        self.assertEqual(len(writes()), count)
        self.b(r, 'jobs', 'app', '--branch', 'job-one', '--lessons', 'none: PRIVATE-LESSON')
        self.b(r, 'wt', 'prune', 'app', '--offline')
        self.assertEqual(len(writes()), count + 2)
        journal = (r / '.brain/events.jsonl').read_text()
        for message in ('PRIVATE-SUMMARY', 'PRIVATE-LESSON', 'PRIVATE-INBOX', 'PRIVATE-SCHEDULE'):
            self.assertNotIn(message, journal)
        self.assertEqual(events.pending(self.records(r)), {})
        inst = instance.Instance(r)
        self.assertEqual(events.normalize(events.events_view(inst, inst.node('app'))),
                         events.normalize(events.snapshot_view(inst, inst.node('app'))))


class EventViews(Scenario):
    fixture_init_in_process = True

    def test_snapshot_setup_failure_restores_both_read_scopes_before_writer_recheck(self):
        from brain import command_reads, spend
        r = self.init('snapshot-entry-failure')
        inst = instance.Instance(r)
        before = spend._UI_DISCOVERY.get()
        original = instance.Instance.nodes
        def fail_during_setup(value):
            if command_reads._VALUES.get() is not None:
                raise OSError('node discovery failed')
            return original(value)
        with mock.patch.dict(os.environ, self.env, clear=True), instance.brain_lock(inst), \
                mock.patch.object(instance.Instance, 'nodes', fail_during_setup):
            with self.assertRaisesRegex(OSError, 'node discovery failed'):
                with instance.released(inst), command_reads.snapshot():
                    self.fail('failed entry must not reach the body')
            self.assertIsNotNone(instance._LOCK['fh'])
            self.assertIsNone(command_reads._VALUES.get())
            self.assertIs(spend._UI_DISCOVERY.get(), before)
        with mock.patch.dict(os.environ, self.env, clear=True), command_reads.snapshot():
            self.assertIsNotNone(command_reads._VALUES.get())
        self.assertIsNone(command_reads._VALUES.get())

    def test_terminal_projection_samples_machine_alongside_view_without_writer_lock(self):
        import threading
        from brain import status
        r = self.init('parallel-machine')
        inst = instance.Instance(r)
        op = argparse.Namespace(inst=inst, node='root', command='handback', record_paths=set(),
                                close_job=False, study_handback=False)
        sampled, rendering = threading.Event(), threading.Event()
        original = events.snapshot_view
        def machine(_inst):
            self.assertIsNone(instance._LOCK['fh'])
            sampled.set()
            self.assertTrue(rendering.wait(3), 'view must run before sampling finishes')
            return {'fixture': True}
        def view(*args, **kwargs):
            self.assertTrue(sampled.wait(3), 'machine sampling must already be running')
            rendering.set()
            return original(*args, **kwargs)
        with mock.patch.dict(os.environ, self.env, clear=True), instance.brain_lock(inst), \
                mock.patch.object(status._m_machine, 'machine_state', side_effect=machine) as read, \
                mock.patch.object(status._m_machine, 'machine_summary', return_value='fixture machine'), \
                mock.patch.object(events, 'snapshot_view', side_effect=view):
            events.maintain(op, {})
        self.assertEqual(read.call_count, 1)
        self.assertIn('fixture machine', (r / 'status.md').read_text())

    def test_handback_projection_snapshot_preserves_generated_bytes_and_expires(self):
        import contextlib
        from brain import command_reads, jobs, status
        r = self.init('handback-snapshot')
        home = self.repo(self.tmp / 'code')
        self.b(r, 'new', 'app', '--title', 'App', '--home', str(home))
        self.b(r, 'wt', 'add', 'app', 'job-one', '--no-setup')
        inst = instance.Instance(r)
        n = inst.node('app')
        path = n['dir'] / 'status.md'
        original = path.read_bytes()
        op = argparse.Namespace(inst=inst, node='app', command='handback', record_paths=set(),
                                close_job=False, study_handback=False, args=argparse.Namespace())
        clock = inst.now_dt()
        snapshot = command_reads.snapshot
        results = []
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(instance.Instance, 'now_dt', return_value=clock), \
                mock.patch.object(status._m_machine, 'machine_state', return_value={}), \
                mock.patch.object(status._m_machine, 'machine_summary', return_value='fixture machine'), \
                mock.patch.object(events, 'status_commit'):
            for command in ('handback', 'task'):
                op.command = command
                results = []
                for reads in (contextlib.nullcontext, snapshot):
                    path.write_bytes(original)
                    with mock.patch.object(command_reads, 'snapshot', reads), instance.brain_lock(inst):
                        view = events.maintain(op, {})
                    results.append((json.dumps(view, ensure_ascii=False).encode(), path.read_bytes()))
                    self.assertIsNone(command_reads._VALUES.get())
                self.assertEqual(results[0], results[1])
        self.assertEqual(results[0], results[1])
        # Parsing and publication consumers outside the display scope stay fresh.
        ledger = n['dir'] / 'job-ledger.md'
        ledger.write_text(ledger.read_text().replace('| job-one |', '| job-two |'))
        self.assertEqual(jobs.read_jobs(n)[0]['Job'], 'job-two')

    def test_handback_projection_ref_drift_refuses_and_clears_read_scope(self):
        from brain import command_reads
        r = self.init('handback-ref-drift')
        home = self.repo(self.tmp / 'code')
        self.b(r, 'new', 'app', '--title', 'App', '--home', str(home))
        inst = instance.Instance(r)
        op = argparse.Namespace(inst=inst, node='app', command='handback', record_paths=set(),
                                close_job=False, study_handback=False)
        original = events.snapshot_view
        def drift(*args, **kwargs):
            result = original(*args, **kwargs)
            self.git('commit', '--allow-empty', '-qm', 'Concurrent home change', cwd=home)
            return result
        with mock.patch.dict(os.environ, self.env, clear=True), instance.brain_lock(inst), \
                mock.patch.object(events, 'snapshot_view', side_effect=drift):
            with self.assertRaisesRegex(SystemExit, 'state changed during the unlocked step'):
                events.maintain(op, {})
        self.assertIsNone(command_reads._VALUES.get())

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

    def test_valid_peer_refresh_beside_deferred_batch_never_reconciles_its_sources(self):
        r = self.init('deferred-peer')
        self.b(r, 'new', 'other', '--title', 'Other')
        inst = instance.Instance(r)
        with instance.brain_lock(r):
            deferred = events.Command(r, command='task')
            deferred.defer_views = True
            with deferred:
                (r / 'backlog.md').write_text('- deferred source\n')
            batch = events.Command(r, command='intent_queue')
            batch.write_batch = True
            with batch:
                # An existing source gap in the invalid view stays unadopted.
                (r / 'source.md').write_text('Pending source\n')
                before = (r / '.brain/events.jsonl').read_bytes()
                state = events.replay(inst)[0].copy()
                invalid = events.journal(inst).state['invalid'].copy()
                with mock.patch.object(events, '_CURRENT', []):
                    for held in (False, True):
                        with instance.held(r) if held else contextlib.nullcontext():
                            events.reconcile(inst, node=inst.node('other'))
                            for target in (None, inst.node('root')):
                                with self.assertRaises(SystemExit):
                                    events.reconcile(inst, node=target)
                            for path in (r / 'nodes/other/backlog.md', r / '.brain/local.json'):
                                original = path.read_bytes()
                                try:
                                    path.write_bytes(original + b' ')
                                    with self.assertRaises(SystemExit):
                                        events.reconcile(inst, node=inst.node('other'))
                                finally:
                                    path.write_bytes(original)
                    self.assertEqual((r / '.brain/events.jsonl').read_bytes(), before)
                    self.assertEqual(events.replay(inst)[0], state)
                    self.assertEqual(events.journal(inst).state['invalid'], invalid)
                    self.assertTrue(events.lease_live(inst, batch.ref))

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

    def test_lease_probes_share_a_dead_lease_and_reconciliation_waits_them_out(self):
        r = self.init('demo')
        inst = instance.Instance(r)
        with instance.brain_lock(r), events.Command(r, command='fixture', node='root'):
            pass  # a journal baseline and the lease directory
        ref = 'b' * 32
        events.append(inst, 'root', 'begin', ref, {'command': 'interrupted'})
        path = events.lease_path(inst, ref, create=True)
        path.write_text('')
        probe = os.open(path, os.O_RDONLY)
        try:
            events.fcntl.flock(probe, events.fcntl.LOCK_SH)  # another reader's probe in flight
            self.assertFalse(events.lease_live(inst, ref))
            releaser = threading.Timer(0.2, lambda: events.fcntl.flock(probe, events.fcntl.LOCK_UN))
            releaser.start()
            with events.dead_lease(inst, ref) as held:
                self.assertIsNotNone(held)
            releaser.join()
        finally:
            os.close(probe)

    def test_held_refresh_defers_reconciliation_beside_a_live_writer(self):
        r = self.init('demo')
        self.b(r, 'new', 'app', '--title', 'App')
        inst = instance.Instance(r)
        with instance.brain_lock(r):
            with events.Command(r, command='fixture', node='app') as op:
                (r / 'nodes/app/backlog.md').write_text('- app writer pending\n')
                before = (r / '.brain/events.jsonl').read_bytes()
                with mock.patch.object(events, '_CURRENT', []):
                    with self.assertRaisesRegex(SystemExit, 'another write is active'):
                        events.reconcile(inst, node=inst.node('app'))
                    err = io.StringIO()
                    with instance.held(r), contextlib.redirect_stderr(err):
                        events.reconcile(inst, node=inst.node('app'))
                        with self.assertRaisesRegex(SystemExit, 'another write is active'):
                            events.reconcile(inst)  # whole-instance reconciliation stays strict
                    self.assertIn('left to a later refresh', err.getvalue())
                    self.assertFalse(instance.holding())
                self.assertEqual((r / '.brain/events.jsonl').read_bytes(), before)
                self.assertTrue(events.lease_live(inst, op.ref))

    def test_held_refresh_renders_committed_events_view_beside_a_live_writer(self):
        r = self.init('demo')
        for nid in ('app', 'other'):
            self.b(r, 'new', nid, '--title', nid)
        self.b(r, 'status', 'app', '--refresh')
        inst = instance.Instance(r)
        view = events.replay(inst)[1]['app']
        with instance.brain_lock(r):
            with events.Command(r, command='fixture', node='other'):
                with mock.patch.object(events, '_CURRENT', []):
                    with self.assertRaisesRegex(SystemExit, 'another write is active; cannot reconcile'):
                        events.events_view_without_current(inst, inst.node('app'))
                    with instance.held(r):
                        self.assertEqual(events.events_view_without_current(inst, inst.node('app')), view)
                        events.append(inst, 'other', 'begin', 'c' * 32, {'command': 'interrupted'})
                        with self.assertRaisesRegex(SystemExit, 'interrupted or active write needs reconciliation'):
                            events.events_view_without_current(inst, inst.node('app'))

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
                            with instance.held(r), self.assertRaisesRegex(SystemExit, 'needs reconciliation') as refused:
                                events.reconcile(inst, node=inst.node('other'), snapshot=True)
                            self.assertEqual(isinstance(refused.exception, events.RefreshBlocked), fault == 'invalid')
                            if fault == 'invalid':
                                self.assertEqual(refused.exception.refs, (op.ref,))
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
        request = self.b(r, 'inbox', 'app', '--add', 'MESSAGE-PRIVATE').strip()
        self.b(r, 'queue', 'wait', request)
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
        # The command publishes the handback before its generated table. Join
        # this fixture's independent lease before inspecting that projection.
        with (r / '.brain/write-queue/applier.lease').open('r') as lease:
            fcntl.flock(lease, fcntl.LOCK_EX)
        self.assertIn('| job-one | - | - | blocked |', st.read_text())
        self.assertNotIn('PRIVATE-BODY', (r / '.brain/events.jsonl').read_text())

    def test_refresh_overflow_is_unpublished_until_preparation_succeeds(self):
        from brain import status
        for failure in ('contention', 'preparation'):
            with self.subTest(failure=failure):
                r = self.init('overflow-' + failure)
                self.b(r, 'status', 'root', '--refresh')
                path, archive = r / 'status.md', r / 'status-archive.md'
                text = path.read_text()
                start, end = text.index('## Now\n'), text.index('## Next')
                path.write_text(text[:start] + '## Now\n\n' + ''.join('- note %d\n' % i for i in range(35)) + '\n' + text[end:])
                archive.write_text('Existing archive\n')
                before = path.read_bytes(), archive.read_bytes()
                error = SystemExit('state changed during the unlocked step; retry from fresh state') if failure == 'contention' else ValueError('preparation failed')
                with instance.brain_lock(r), mock.patch.object(status, 'refreshed_text', side_effect=error), \
                        mock.patch.object(events, 'cap_now', wraps=events.cap_now) as publish, \
                        contextlib.redirect_stderr(io.StringIO()), \
                        self.assertRaisesRegex(SystemExit, 'maintenance refused'):
                    with events.Command(r, command='status'):
                        pass
                publish.assert_not_called()
                self.assertEqual((path.read_bytes(), archive.read_bytes()), before)
                self.assertEqual(events.pending(events.read(instance.Instance(r))), {})
                if failure == 'contention':
                    self.assertEqual(events.read(instance.Instance(r))[-1]['kind'], 'abort')
                original = events.cap_now
                published = []
                def owned_publish(*args, **kwargs):
                    self.assertIsNotNone(instance._LOCK['fh'])
                    self.assertIsNotNone(kwargs.get('prepared'))
                    published.append(True)
                    return original(*args, **kwargs)
                with instance.brain_lock(r), mock.patch.object(events, 'cap_now', side_effect=owned_publish):
                    with events.Command(r, command='status'):
                        pass
                self.assertEqual(published, [True])
                self.b(r, 'status', 'root', '--refresh')
                now = path.read_text().split('## Now\n', 1)[1].split('## Next', 1)[0]
                self.assertEqual(sum(bool(line.strip()) for line in now.splitlines()), 30)
                for i in range(30, 35):
                    self.assertEqual(archive.read_text().count('- note %d\n' % i), 1)
                self.assertTrue(archive.read_text().startswith('Existing archive\n'))

    def test_overflow_plan_is_read_only_and_uses_the_publication_clock(self):
        r = self.init('overflow-clock')
        inst = instance.Instance(r)
        path, archive = r / 'status.md', r / 'status-archive.md'
        path.write_text('# Demo\n\n## Now\n' + ''.join('- item %d\n' % i for i in range(35)))
        archive.write_text('Existing archive\n')
        before = path.read_bytes(), archive.read_bytes()
        with mock.patch.object(inst, 'now', return_value='2026-01-01 10:00 +00:00'):
            plan = events.plan_cap_now(inst, inst.node('root'))
        self.assertEqual((path.read_bytes(), archive.read_bytes()), before)
        with instance.brain_lock(r), mock.patch.object(inst, 'now', return_value='2026-01-01 10:01 +00:00'):
            events.cap_now(inst, inst.node('root'), prepared=plan)
        self.assertIn('2026-01-01 10:01 +00:00: Now overflow', archive.read_text())
        self.assertNotIn('2026-01-01 10:00', archive.read_text())
        self.assertEqual(path.read_text(), plan[0])

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
        self.b(r, 'jobs', 'app', '--branch', 'job-one', '--settled', '--thread', 'fixture-thread',
               '--attestation-file', settlement_export(self, r, 'app', branch='job-one'))
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
