"""Actionable watcher regressions; fabricated instances and fake transports only."""
import contextlib
import io
import json
import os
import time
from unittest import mock

from scenario import Scenario, CORE
import core_loader


class Watcher(Scenario):
    def setUp(self):
        super().setUp()
        self.patch = mock.patch.dict(os.environ, self.env)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.m = core_loader.SourceFileLoader('watch_fixture', str(CORE / 'bin/brain')).load_module()
        self.root = self.init('oak')
        self.b(self.root, 'set', 'root', 'wake=fixture-send', 'coordinator=fixture-root')
        self.inst = self.m.Instance(self.root)

    def watch(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.m.watch_once(self.inst)

    def test_unchanged_new_handback_removed_and_returned_and_stale(self):
        items = [{'kind': 'handback', 'job': 'made-up-job', 'state': 'done', 'text': 'report ready'}]
        with mock.patch.object(self.m, 'due_items', return_value=items), \
                mock.patch.object(self.m, 'changes_due', return_value=[]), \
                mock.patch.object(self.m, 'wake_start') as start:
            self.watch()
            directory = self.m.wake_directory(self.inst, self.inst.node('root'))
            self.assertEqual(len(list(directory.glob('*.pending'))), 1)
            first = (self.root / '.brain/watch.json').read_text()
            with mock.patch.object(self.m, 'request_wake') as request:
                self.watch()
                request.assert_not_called()
            self.assertEqual(len(list(directory.glob('*.pending'))), 1)
            # Display changes must not turn readiness re-evaluation into another wake.
            items[0]['text'] = 'report ready but base moved'
            self.watch()
            self.assertEqual(len(list(directory.glob('*.pending'))), 1)
            items.clear()
            self.watch()
            items.append({'kind': 'handback', 'job': 'made-up-job', 'state': 'done', 'text': 'report ready'})
            self.watch()
            self.assertEqual(len(list(directory.glob('*.pending'))), 1)
            items.append({'kind': 'inbox', 'text': 'new fabricated question'})
            self.watch()
            self.assertEqual(len(list(directory.glob('*.pending'))), 2)
            self.assertIn('handback', first)
            self.assertTrue(start.called)
        with mock.patch.object(self.m, 'due_items', return_value=[{'kind': 'stale-status', 'text': 'old'}]), \
                mock.patch.object(self.m, 'changes_due', return_value=[]), \
                mock.patch.object(self.m, 'request_wake') as wake, mock.patch.object(self.m, 'wake_start'):
            self.watch()
            wake.assert_not_called()

    def test_failed_transport_preserves_inbox_and_pending_without_new_request(self):
        (self.root / 'inbox.md').write_text('- fabricated new hand-back\n')
        before = (self.root / 'inbox.md').read_bytes()
        with mock.patch.object(self.m, 'wake_start'), mock.patch.object(self.m, 'wake_send', return_value=False):
            self.watch()
            directory = self.m.wake_directory(self.inst, self.inst.node('root'))
            self.m.wake_drain(self.inst, self.inst.node('root'), once=True)
            self.assertTrue(list(directory.glob('*.pending')))
            with mock.patch.object(self.m, 'request_wake') as request:
                self.watch()
                request.assert_not_called()
        self.assertEqual((self.root / 'inbox.md').read_bytes(), before)

    def test_real_handback_file_wakes_once_with_fake_transport(self):
        n = self.inst.node('root')
        row = {**dict.fromkeys(self.m.JOB_COLS, ''), 'Job': 'import:fixture', 'Started': '2026-01-01T10:00Z'}
        self.m.write_jobs(self.inst, n, [row])
        with mock.patch.object(self.m, 'wake_start'), mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            self.watch()
            signal = self.m.handback_file(n, row)
            signal.parent.mkdir(exist_ok=True)
            signal.write_text(json.dumps({'job': row['Job'], 'started': row['Started'], 'state': 'done',
                                          'report': 'fabricated/report.md'}))
            self.watch()
            self.m.wake_drain(self.inst, n, once=True)
            self.assertEqual(send.call_count, 1)
            self.watch()
            self.m.wake_drain(self.inst, n, once=True)
            self.assertEqual(send.call_count, 1)

    def test_provider_failure_closed_job_and_screened_excerpts(self):
        n = self.inst.node('root')
        row = {**dict.fromkeys(self.m.JOB_COLS, ''), 'Job': 'import:fixture', 'Started': '2026-01-01T10:00Z'}
        self.m.write_jobs(self.inst, n, [row])
        key = self.m.job_key(row)
        self.inst.local_of('root')['workers'] = {key: 'fixture-thread'}
        run = {'node': 'root', 'job_key': key, 'thread': 'fixture-thread', 'run': 'fixture-run',
               'status': 'failed', 'runtime': 'codex', 'error': 'provider error'}
        (self.root / '.brain/worker-runs.json').write_text(json.dumps({'runs': [run]}))
        (self.root / 'never-name.txt').write_text('FixturePrivatePhrase\n')
        (self.root / 'inbox.md').write_text('- FixturePrivatePhrase asks for help\n')
        with mock.patch.object(self.m, 'wake_start'):
            self.watch()
            state = (self.root / '.brain/watch.json').read_text()
            self.assertNotIn('FixturePrivatePhrase', state)
            self.assertIn('worker run failed (provider error)', (self.root / 'inbox.md').read_text())
            self.assertEqual(self.m.read_jobs(n)[0]['Switches'], '')
            row['Ended'] = '2026-01-01T11:00Z'
            row['Outcome'] = 'closed'
            self.m.write_jobs(self.inst, n, [row])
            run['run'] = 'later-run'
            (self.root / '.brain/worker-runs.json').write_text(json.dumps({'runs': [run]}))
            before = (self.root / 'inbox.md').read_bytes()
            self.watch()
            self.assertEqual((self.root / 'inbox.md').read_bytes(), before)

    def test_failed_enqueue_and_missing_transport_do_not_acknowledge(self):
        (self.root / 'inbox.md').write_text('- new fabricated request\n')
        with mock.patch.object(self.m, 'request_wake', return_value=False):
            self.watch()
        self.assertEqual(json.loads((self.root / '.brain/watch.json').read_text())['seen'], [])
        self.inst.local_of('root')['wake'] = ''
        self.watch()
        self.assertEqual(json.loads((self.root / '.brain/watch.json').read_text())['seen'], [])

    def test_failed_run_bound_open_job_capacity_retry_once_and_protected_classes(self):
        n = self.inst.node('root')
        row = {**dict.fromkeys(self.m.JOB_COLS, ''), 'Job': 'job/build', 'Started': '2026-01-01T10:00Z', 'Class': 'build'}
        self.m.write_jobs(self.inst, n, [row])
        key = self.m.job_key(row)
        self.inst.local_of('root')['workers'] = {key: 'worker-thread'}
        run = {'node': 'root', 'job_key': key, 'thread': 'worker-thread', 'run': 'run-one',
               'status': 'failed', 'runtime': 'codex', 'error': 'model at capacity secret=unpublished'}
        path = self.root / '.brain/worker-runs.json'
        path.write_text(json.dumps({'runs': [run]}))
        with mock.patch.object(self.m, 'wake_start'):
            self.watch()
            self.watch()
            inbox = (self.root / 'inbox.md').read_text()
            self.assertEqual(inbox.count('worker run failed'), 1)
            self.assertIn('brain brief root --branch job/build --runtime claude', inbox)
            self.assertNotIn('unpublished', inbox)
            self.assertEqual(self.m.read_jobs(n)[0]['Switches'].count('at capacity'), 1)
            run['run'] = 'run-two'
            run['runtime'] = 'claude'
            path.write_text(json.dumps({'runs': [run]}))
            self.watch()
            lines = (self.root / 'inbox.md').read_text().splitlines()
            self.assertNotIn('relaunch', lines[-1])
            self.assertEqual(self.m.read_jobs(n)[0]['Switches'].count('at capacity'), 1)
        for klass in ('review', 'diagnose'):
            row.update({'Switches': '', 'Capacity retry': '', 'Class': klass})
            self.m.write_jobs(self.inst, n, [row])
            run.update({'run': 'run-' + klass, 'runtime': 'codex'})
            path.write_text(json.dumps({'runs': [run]}))
            with mock.patch.object(self.m, 'wake_start'):
                self.watch()
            self.assertEqual(self.m.read_jobs(n)[0]['Switches'], '')
            self.assertNotIn('relaunch', (self.root / 'inbox.md').read_text().splitlines()[-1])
        # Handling an inbox notice must not recreate the old failure next minute.
        (self.root / 'inbox.md').write_text('')
        with mock.patch.object(self.m, 'wake_start'):
            self.watch()
        self.assertEqual((self.root / 'inbox.md').read_text(), '')
        run.update({'thread': 'unbound', 'run': 'run-three'})
        path.write_text(json.dumps({'runs': [run]}))
        before = (self.root / 'inbox.md').read_bytes()
        with mock.patch.object(self.m, 'wake_start'):
            self.watch()
        self.assertEqual((self.root / 'inbox.md').read_bytes(), before)

    def test_coordinator_root_overdue_and_direct_questions_do_not_repeat(self):
        self.b(self.root, 'new', 'sample', '--title', 'Sample')
        self.b(self.root, 'set', 'sample', 'coordinator=fixture-child', 'wake=fixture-child-wake')
        self.inst = self.m.Instance(self.root)
        child = self.inst.node('sample')
        line = '- 2020-01-01 00:00 +00:00 from runtime: Host question: fabricated <!-- host-question:abc -->'
        (child['dir'] / 'inbox.md').write_text(line + '\n')
        with mock.patch.object(self.m, 'wake_start'), mock.patch.object(self.m, 'changes_due', return_value=[]):
            # Match cmd_due's escalation reason: a previously delivered escalation is not resent.
            import hashlib
            reason = 'overdue:' + hashlib.sha256(line.encode()).hexdigest()
            self.m.request_wake(self.inst, self.inst.node('root'), reason)
            with mock.patch.object(self.m, 'wake_send', return_value=True) as send:
                self.m.wake_drain(self.inst, self.inst.node('root'), once=True)
                self.assertEqual(send.call_count, 1)
                self.watch()
                self.watch()
                self.m.wake_drain(self.inst, self.inst.node('root'), once=True)
                self.assertEqual(send.call_count, 1)
            child_queue = self.m.wake_directory(self.inst, child)
            self.assertFalse(list(child_queue.glob('*.pending')))
            (child['dir'] / 'inbox.md').write_text('- new fabricated worker result\n')
            self.watch()
            self.assertEqual(len(list(child_queue.glob('*.pending'))), 1)

    def test_busy_lock_skips_without_wake_and_sampler_runs_watcher(self):
        with self.m.brain_lock(self.inst), mock.patch.object(self.m, 'request_wake') as wake:
            self.watch()
            wake.assert_not_called()
        import argparse
        with mock.patch.object(self.m, 'load', return_value=self.inst), \
                mock.patch.object(self.m, 'machine_sample_once') as sample, \
                mock.patch.object(self.m, 'watch_once') as watch:
            self.m.cmd_machine(argparse.Namespace(action='sample'))
            sample.assert_called_once_with(self.inst)
            watch.assert_called_once_with(self.inst)

    def test_corrupt_state_fails_closed_and_external_links_refused(self):
        state = self.root / '.brain/watch.json'
        state.write_text('{broken')
        with mock.patch.object(self.m, 'request_wake') as wake:
            with self.assertRaises(ValueError):
                self.watch()
            wake.assert_not_called()
        self.assertEqual(state.read_text(), '{broken')
        state.write_text('{}')
        with self.assertRaises(ValueError):
            self.watch()
        state.unlink()
        sentinel = self.tmp / 'outside'
        sentinel.write_text('sentinel')
        state.symlink_to(sentinel)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.watch()
        self.assertEqual(sentinel.read_text(), 'sentinel')

    def test_router_interface_uses_cached_readings_and_respects_its_result(self):
        n = self.inst.node('root')
        row = {**dict.fromkeys(self.m.JOB_COLS, ''), 'Job': 'import:fixture', 'Started': '2026-01-01T10:00Z'}
        self.m.write_jobs(self.inst, n, [row])
        key = self.m.job_key(row)
        self.inst.local_of('root')['workers'] = {key: 'fixture-thread'}
        run = {'node': 'root', 'job_key': key, 'thread': 'fixture-thread', 'run': 'run-one',
               'status': 'failed', 'runtime': 'codex', 'error': 'at capacity'}
        path = self.root / '.brain/worker-runs.json'
        path.write_text(json.dumps({'runs': [run]}))
        owner = self.m._owners['watch_run']
        with mock.patch.object(owner._m_jobs, 'job_class', create=True, return_value='build') as klass, \
                mock.patch.object(owner._m_usage, 'class_allows_fallback', create=True, return_value=True) as allowed, \
                mock.patch.object(owner._m_usage, 'class_route', create=True, return_value=('codex', None, 'pin wins')) as route, \
                mock.patch.object(self.m, 'wake_start'):
            self.watch()
            klass.assert_called_once()
            allowed.assert_called_once_with('build')
            route.assert_called_once_with(self.inst, 'build', runtime='claude', cached_only=True)
            self.assertEqual(self.m.read_jobs(n)[0]['Switches'], '')
            run['run'] = 'run-two'
            path.write_text(json.dumps({'runs': [run]}))
            route.return_value = ('claude', None, 'cached route')
            self.watch()
            self.assertIn('codex -> claude: at capacity', self.m.read_jobs(n)[0]['Switches'])

    def test_ten_nodes_under_one_second_no_host_network_or_model_calls(self):
        for i in range(9):
            self.b(self.root, 'new', 'node' + str(i), '--title', 'Fixture')
        self.inst = self.m.Instance(self.root)
        for n in self.inst.nodes():
            self.inst.local_of(n['id']).update(coordinator='fixture-' + n['id'], wake='fixture-wake')
            row = {**dict.fromkeys(self.m.JOB_COLS, ''), 'Job': 'job/fixture', 'Started': '2026-01-01T10:00Z'}
            self.m.write_jobs(self.inst, n, [row])
            signal = self.m.handback_file(n, row)
            signal.parent.mkdir(exist_ok=True)
            signal.write_text(json.dumps({'job': row['Job'], 'started': row['Started'], 'state': 'done'}))
        with mock.patch.object(self.m, 'wake_start'), \
                mock.patch.object(self.m, 'core_update', side_effect=AssertionError('no remote check')), \
                mock.patch.object(self.m, 'ready_state', side_effect=AssertionError('watcher does not reverify readiness')), \
                mock.patch.object(self.m, 'usage_reading', side_effect=AssertionError('no provider polling')):
            start = time.monotonic()
            self.watch()
            duration = time.monotonic() - start
        self.assertLess(duration, 1.0)
        print('10-node watcher seconds:', round(duration, 3))
