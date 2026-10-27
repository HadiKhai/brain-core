"""Actionable watcher regressions; fabricated instances and fake transports only."""
import contextlib
import argparse
import datetime
import io
import json
import os
import sys
import threading
import time
from unittest import mock

from scenario import Scenario, CORE
import core_loader


# Delivery mechanics opt into kinds excluded by the decision-only sampler.
EXPLICIT_DELIVERY_KINDS = {'inbox', 'handback', 'unsettled-threads', 'overlapping-inputs',
                           'idle-with-runnable-work', 'machine-hold', 'machine-alert',
                           'machine-repair', 'restart', 'rotate', 'overdue-question'}

class Watcher(Scenario):
    fixture_templates = True
    fixture_init_in_process = True

    def test_direct_wake_snapshot_batches_discovery_and_refreshes_after_append(self):
        m, inst = self.m, self.inst
        cfg = self.tmp / 'watch-transcripts'
        folder = cfg / 'sessions'
        folder.mkdir(parents=True)
        transcript = folder / 'rollout-fixture.jsonl'
        transcript.write_text('{}\n')
        observed = []
        appended = threading.Event()
        resume = threading.Event()
        def writer():
            if resume.wait(10):
                with transcript.open('a') as stream:
                    stream.write('{}\n')
                appended.set()
        def scan(*args):
            first = m.spend_input_signature(inst)
            if not observed:
                resume.set()
                if not appended.wait(10):
                    raise RuntimeError('fixture writer timeout')
            second = m.spend_input_signature(inst)
            self.assertIs(first, second)
            observed.append(first)
            return {}
        thread = threading.Thread(target=writer)
        thread.start()
        try:
            with mock.patch.object(m, 'spend_sources', return_value=([('codex', cfg)], {cfg}, set())), \
                    mock.patch.object(m, 'watch_scan', side_effect=scan):
                m.wake_snapshot(inst, inst.node('root'), [])
                m.wake_snapshot(inst, inst.node('root'), [])
            self.assertNotEqual(observed[0], observed[1])
        finally:
            resume.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())

    def test_direct_wake_snapshot_cannot_publish_sources_changed_during_cached_scan(self):
        m, inst = self.m, self.inst
        root = inst.node('root')
        inbox = self.root / 'inbox.md'
        inbox.write_text('- Choose blue\n')
        attempts = []
        def scan(*args):
            attempts.append(m.inbox_lines(root))
            if len(attempts) == 1:
                inbox.write_text('- Choose green\n')
                self.assertEqual(m.inbox_lines(root), ['- Choose blue'])  # this scan's reads stay cached
            text = attempts[-1][0][2:]
            return {'root': {'node': root, 'items': [{'id': m.watch_digest([text]),
                            'kind': 'inbox', 'node': 'root', 'text': text}],
                            'command': 'fixture-send'}}
        with mock.patch.object(m, 'watch_scan', side_effect=scan):
            groups = m.watch_groups(inst)
        # The changed first scan is never publishable; only the fresh rescan is.
        self.assertEqual(attempts, [['- Choose blue'], ['- Choose green']])
        self.assertFalse(groups.stale)
        self.assertEqual([i['text'] for i in groups['root']['items']], ['Choose green'])
        self.assertEqual(m.inbox_lines(root), ['- Choose green'])

    def test_direct_watch_fallback_discards_failed_scan_reads_before_new_source_fence(self):
        m, inst = self.m, self.inst
        root = inst.node('root')
        inbox = self.root / 'inbox.md'
        inbox.write_text('- Choose blue\n')
        observations = []
        def scan(*args):
            lines = m.inbox_lines(root)
            observations.append(lines)
            if len(observations) == 1:
                inbox.write_text('- Choose green\n')
                raise OSError('transient fixture scan failure')
            return {'root': {'node': root, 'items': [{'id': m.watch_digest(['green']),
                            'kind': 'inbox', 'node': 'root', 'text': lines[0]}],
                            'command': 'fixture-send'}}
        with mock.patch.object(m, 'watch_scan', side_effect=scan):
            groups = m.watch_groups(inst)
        self.assertEqual(observations, [['- Choose blue'], ['- Choose green']])
        self.assertFalse(groups['root']['episodes_known'])
        self.assertEqual(groups['root']['items'][0]['text'], '- Choose green')

    def test_corrupt_episode_receipts_still_enqueue_current_questions(self):
        directory = self.root / '.brain/wake'
        directory.mkdir(exist_ok=True)
        receipt = directory / 'due-episodes.json'
        receipt.write_text('{broken')
        (self.root / 'inbox.md').write_text('- Host question: Choose a color <!-- host-question:fixture -->\n')
        with mock.patch.object(self.m, 'wake_start'):
            self.watch(kinds={'inbox'})
        self.assertEqual(receipt.read_text(), '{broken')
        self.assertTrue(list(self.m.wake_directory(self.inst, self.inst.node('root')).glob('*.pending')))

    def typed_question(self, expired=False):
        now = self.inst.now_dt()
        spec = {'options': [{'id': 'a', 'label': 'Continue', 'action': 'routine'},
                            {'id': 'b', 'label': 'Hold', 'action': 'routine'}],
                'recommendation': 'a', 'default': 'a',
                'deadline': (now + datetime.timedelta(minutes=-1 if expired else 30)).isoformat()}
        with mock.patch.object(self.m, 'request_wake', return_value=False):
            return self.m.communication_record(self.inst, self.inst.node('root'), 'question', 'Choose', 'fixture', spec)

    def test_answered_question_between_scan_and_publication_cannot_wake(self):
        question = self.typed_question()
        groups = self.m.watch_groups(self.inst)
        self.b(self.root, 'question-answer', question['id'], 'a')
        with mock.patch.object(self.m, 'wake_start'), mock.patch.object(self.m, 'request_wake') as wake:
            self.m.watch_once(self.inst, kinds={'question'}, groups=groups)
        wake.assert_not_called()

    def test_expired_routine_question_reconciles_before_watcher_observation(self):
        question = self.typed_question(expired=True)
        with mock.patch.object(self.m, 'request_wake') as wake:
            self.watch(kinds={'question'})
        wake.assert_not_called()
        self.assertEqual(self.m.communication_read(self.inst)['questions'][question['id']]['answer'], 'a')

    def setUp(self):
        super().setUp()
        self.patch = mock.patch.dict(os.environ, self.env)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.m = core_loader.SourceFileLoader('watch_fixture', str(CORE / 'bin/brain')).load_module()
        self.root = self.cached_setup('watch', self.watch_fixture)
        self.inst = self.m.Instance(self.root)

    def watch_fixture(self):
        self.root = self.init('oak')
        self.b(self.root, 'set', 'root', 'wake=fixture-send', 'coordinator=fixture-root')
        return self.root

    def watch(self, kinds=None):
        with contextlib.redirect_stdout(io.StringIO()):
            self.m.watch_once(self.inst, kinds=kinds)

    def test_stale_precomputed_scan_cannot_retire_new_pending_or_restore_closed_episode(self):
        m, inst = self.m, self.inst
        root = inst.node('root')
        def scan(present):
            items = [{'id': m.watch_digest(['fixture-need']), 'node': 'root',
                      'kind': 'blocked-overdue', 'text': 'Choose a fixture color'}] if present else []
            return m.watch_due_episodes(inst, lambda: {'root': {
                'node': root, 'command': 'fixture-send', 'items': items}})
        stale_absent = scan(False)
        newer = scan(True)
        current_id = newer['root']['items'][0]['id']
        with mock.patch.object(m, 'wake_start'):
            m.watch_once(inst, kinds={'blocked-overdue'}, quiet=True, groups=newer)
            directory = m.wake_directory(inst, root)
            pending = list(directory.glob('*.pending'))
            self.assertEqual(len(pending), 1)
            before = pending[0].read_bytes()
            seen = inst.root / '.brain/wake/due-seen.json'
            acknowledged = seen.read_bytes()
            m.watch_once(inst, kinds={'blocked-overdue'}, quiet=True, groups=stale_absent)
            self.assertEqual(pending[0].read_bytes(), before)
            self.assertEqual(seen.read_bytes(), acknowledged)
            receipt = inst.root / '.brain/wake/due-episodes.json'
            self.assertIn(current_id, json.loads(receipt.read_text())['open'].values())
            closed = scan(False)
            m.watch_once(inst, kinds={'blocked-overdue'}, quiet=True, groups=closed)
            self.assertFalse(list(directory.glob('*.pending')))
            acknowledged = seen.read_bytes()
            m.watch_once(inst, kinds={'blocked-overdue'}, quiet=True, groups=newer)
            self.assertFalse(list(directory.glob('*.pending')), 'retired episode must not be restored')
            self.assertEqual(seen.read_bytes(), acknowledged)

    def test_scan_runs_outside_receipt_lock_and_overtaken_scan_is_stale(self):
        import fcntl
        m, inst = self.m, self.inst
        root = inst.node('root')
        lock = inst.root / '.brain/wake/due-episodes.lock'
        def items(present):
            return [{'id': m.watch_digest(['fixture-need']), 'node': 'root', 'kind': 'blocked-overdue',
                     'text': 'Choose a fixture color'}] if present else []
        newer = {}
        def older_scan():
            with open(lock, 'a+') as stream:  # no receipt lock is held while sources are scanned
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(stream, fcntl.LOCK_UN)
            newer['groups'] = m.watch_due_episodes(inst, lambda: {'root': {
                'node': root, 'command': 'fixture-send', 'items': items(True)}})
            return {'root': {'node': root, 'command': 'fixture-send', 'items': items(False)}}
        older = m.watch_due_episodes(inst, older_scan)
        self.assertTrue(older.stale)
        self.assertIsNone(older.receipt_revision)
        current = newer['groups']['root']['items'][0]['id']
        receipt = json.loads((inst.root / '.brain/wake/due-episodes.json').read_text())
        self.assertEqual(list(receipt['open'].values()), [current], 'a scan overtaken by another reconciliation must not close its receipt')

    def test_source_signature_sees_change_behind_internal_link(self):
        m, inst = self.m, self.inst
        target = inst.root / '.brain/fixture-inbox.txt'
        target.write_text('- Choose blue\n')
        inbox = inst.root / 'inbox.md'
        inbox.unlink(missing_ok=True)
        inbox.symlink_to(target)
        self.assertFalse(m.outward_links(inst.root))
        layout = {}
        before = m.watch_source_signature(inst, layout)
        target.write_text('- Choose cyan\n')  # same size; only the referent changes
        self.assertNotEqual(m.watch_source_signature(inst, layout), before)

    def test_source_signature_sees_retargeted_home_with_cached_layout(self):
        import subprocess
        m, inst = self.m, self.inst
        homes = self.tmp / 'fixture-homes'
        for name in ('a', 'b'):
            (homes / name).mkdir(parents=True)
            subprocess.run(['git', 'init', '-q', str(homes / name)], check=True)
        link = homes / 'home'
        link.symlink_to(homes / 'a')
        external = {**inst.node('root'), 'id': 'fixture-external', 'parent': None,
                    'external': True, 'home_path': link}
        nodes = list(inst.nodes()) + [external]
        with mock.patch.object(type(inst), 'nodes', return_value=nodes):
            layout = {}
            before = m.watch_source_signature(inst, layout)
            link.unlink()
            link.symlink_to(homes / 'b')
            self.assertNotEqual(m.watch_source_signature(inst, layout), before)
            plain = homes / 'plain'
            plain.mkdir()
            (plain / '.git').symlink_to(homes / 'a' / '.git')
            external['home_path'] = plain
            before = m.watch_source_signature(inst, layout)
            (plain / '.git').unlink()
            (plain / '.git').symlink_to(homes / 'b' / '.git')
            self.assertNotEqual(m.watch_source_signature(inst, layout), before)

    def test_dispatcher_defers_on_stale_scan_instead_of_fallback_identity(self):
        m, inst = self.m, self.inst
        dispatcher = m._owners['dispatch_run']
        (self.root / 'inbox.md').write_text('- Host question: choose a color <!-- host-question:color -->\n')
        stale = m.WatchGroups({}, None, None, stale=True)
        with mock.patch.object(m._owners['watch_groups'], 'watch_groups', return_value=stale), \
                mock.patch.object(m._owners['request_wake'], 'request_wake') as request:
            result = dispatcher.dispatch_run(inst)
        request.assert_not_called()
        self.assertTrue(result.get('busy'))

    def test_direct_overdue_wake_fails_open_on_stale_identities(self):
        m, inst = self.m, self.inst
        root = inst.node('root')
        stale = m.WatchGroups({}, None, None, stale=True)
        with mock.patch.object(m._owners['watch_groups'], 'watch_groups', return_value=stale), \
                mock.patch.object(m, 'wake_start') as start:
            self.assertFalse(m.request_wake(inst, root, 'overdue:' + 'a' * 64))
        start.assert_not_called()
        self.assertFalse(list(m.wake_directory(inst, root).glob('*.pending')))

    def test_command_snapshot_keeps_overdue_questions_repeatable_and_retains_pending_wake(self):
        m, inst = self.m, self.inst
        line = '- 2020-01-01 00:00 +00:00 from runtime: Host question: choose a color <!-- host-question:color -->'
        (self.root / 'inbox.md').write_text(line + '\n')
        root = inst.node('root')
        snapshot = m._owners['overdue_questions']._m_command_reads.snapshot
        with mock.patch.dict(os.environ, {**self.env, 'BRAIN_ROOT': str(self.root)}), \
                mock.patch.object(m, 'changes_due', return_value=[]), \
                mock.patch.object(m, 'core_update'), mock.patch.object(m, 'auto_upgrade', return_value=None), \
                mock.patch.object(m, 'wake_start'):
            with snapshot():
                first = list(m.overdue_questions(inst, root, m.inbox_lines(root)))
                self.assertEqual(first, [line])
                self.assertEqual(list(m.overdue_questions(inst, root, m.inbox_lines(root))), first)
            m.watch_once(inst, kinds={'overdue-question'}, quiet=True)
            pending = list(m.wake_directory(inst, root).glob('*.pending'))
            self.assertEqual(len(pending), 1)
            before, inode = pending[0].read_bytes(), pending[0].stat().st_ino
            episode = json.loads(before)['items'][0]['id']
            output = io.StringIO()
            with mock.patch.object(sys, 'argv', ['brain', 'due', 'root', '--json']), \
                    contextlib.redirect_stdout(output):
                m.main()
            self.assertTrue(any(i['kind'] == 'overdue-question' for i in json.loads(output.getvalue())))
            self.assertEqual((pending[0].read_bytes(), pending[0].stat().st_ino), (before, inode))
            receipt = json.loads((self.root / '.brain/wake/due-episodes.json').read_text())
            self.assertIn(episode, receipt['open'].values())

    def since(self, node='root'):
        with mock.patch.object(self.m, 'load', return_value=self.inst), \
                mock.patch.object(self.m, 'request_wake') as request, \
                mock.patch.object(self.m, 'core_update') as upgrade, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.m.cmd_due(argparse.Namespace(id=node, since_wake=True, quiet=False, json=True))
            request.assert_not_called()
            upgrade.assert_not_called()
        return json.loads(output.getvalue())

    def machine_tick(self, at, reading):
        with mock.patch.object(self.m.time, 'time', return_value=at), \
                mock.patch.object(self.m, 'machine_load', return_value=reading), \
                contextlib.redirect_stdout(io.StringIO()):
            self.m.machine_sample(self.inst)
            self.watch(kinds={'machine-alert', 'machine-hold', 'machine-repair'})
            self.m.wake_drain(self.inst, self.inst.node('root'), once=True)

    def queued_machine_work(self):
        with self.m.machine_queue() as queue:
            queue.rows = [{'token': 'a' * 32, 'owner': [os.getpid(), self.m.process_stamp(os.getpid())],
                           'kind': 'verify', 'state': 'queued',
                           'scope': self.m.machine_scope(self.inst, 'root')}]

    def idle_work(self):
        return [i for i in self.m.due_items(self.inst, self.inst.node('root'), include_stale=False)
                if i['kind'] == 'idle-with-runnable-work']

    def test_idle_work_names_unstarted_tasks_in_build_order_only(self):
        backlog = self.root / 'backlog.md'
        backlog.write_text('- NEXT after another job: old prose\n- [ ] Outside before\n'
                           '## Build order\n- [x] Finished\n- [X] Also finished\n'
                           '- [ ] Deferred (parked: later)\n- [ ] Decision (waits: operator)\n'
                           '- [ ] First runnable\n- [ ] Second runnable\n'
                           '## Other work\n- [ ] Outside after\n')
        items = self.idle_work()
        self.assertEqual(len(items), 1)
        self.assertIn('First runnable', items[0]['text'])
        for ignored in ('Outside', 'Finished', 'finished', 'Deferred', 'Decision', 'Second runnable', 'old prose'):
            self.assertNotIn(ignored, items[0]['text'])
        self.assertIn('First runnable', self.b(self.root, 'due', 'root', '--json'))

    def test_idle_work_each_nonrunnable_state_is_quiet(self):
        backlog = self.root / 'backlog.md'
        for task in ('- [x] Finished', '- [X] Finished', '- [ ] Deferred (parked: reason)',
                     '- [ ] Decision (waits: operator)', '- NEXT legacy prose'):
            with self.subTest(task=task):
                backlog.write_text('## Build order\n' + task + '\n')
                self.assertEqual(self.idle_work(), [])
        backlog.write_text('## Build order\n```md\n- [ ] Example\n```\n'
                           '<!-- - [ ] Hidden -->\n## Other\n- [ ] Outside\n')
        self.assertEqual(self.idle_work(), [])
        backlog.write_text('## Build order\n- [ ] Actually runnable\n')
        self.assertEqual(len(self.idle_work()), 1)  # the exclusions must not disable the detector
        with mock.patch.object(self.m, 'read_jobs', return_value=None):
            self.assertEqual(self.idle_work(), [])  # unknown capacity never grants a start
        self.b(self.root, 'set', 'root', 'labels=state:paused')
        self.inst = self.m.Instance(self.root)
        self.assertEqual(self.idle_work(), [])

    def test_idle_work_invalid_parallel_cap_is_reported_and_never_launch_advice(self):
        (self.root / 'backlog.md').write_text('## Build order\n- [ ] Runnable\n')
        for cap in ('0', '-1', 'two', '1.5'):
            with self.subTest(cap=cap):
                self.assertIn('parallel_jobs', self.b(self.root, 'set', 'root', 'parallel_jobs=' + cap, ok=False))
        path = self.root / 'node.md'
        fm, body = self.m.read_fm(path)
        fm['parallel_jobs'] = 'invalid'
        self.m.write_fm(path, fm, body)
        self.inst = self.m.Instance(self.root)
        self.assertEqual(self.idle_work(), [])
        self.assertIn('parallel_jobs', self.b(self.root, 'check', ok=False))

    def test_idle_work_cap_and_exact_open_job_branch_matching(self):
        backlog = self.root / 'backlog.md'
        backlog.write_text('## Build order\n- [ ] Build `job/plan`\n- [ ] Build `job/plan-extra`\n')
        row = dict.fromkeys(self.m.JOB_COLS, '')
        row.update(Job='job/plan', Started=self.inst.now())
        self.m.write_jobs(self.inst, self.inst.node('root'), [row])
        self.assertEqual(self.idle_work(), [])  # absent parallel_jobs defaults to one
        self.b(self.root, 'set', 'root', 'parallel_jobs=2')
        self.inst = self.m.Instance(self.root)
        items = self.idle_work()
        self.assertEqual(len(items), 1)
        self.assertIn('job/plan-extra', items[0]['text'])
        self.assertNotIn('`job/plan`', items[0]['text'])
        second = {**row, 'Job': 'job/other'}
        self.m.write_jobs(self.inst, self.inst.node('root'), [row, second])
        self.assertEqual(self.idle_work(), [])  # at the explicit cap
        second['Ended'] = self.inst.now()
        self.m.write_jobs(self.inst, self.inst.node('root'), [row, second])
        self.assertEqual(len(self.idle_work()), 1)  # closed jobs free a slot
        row['Ended'] = self.inst.now()
        self.m.write_jobs(self.inst, self.inst.node('root'), [row, second])
        self.assertIn('`job/plan`', self.idle_work()[0]['text'])  # only open jobs mark progress
        row.update(Job='job/plan+extra', Ended='')
        self.m.write_jobs(self.inst, self.inst.node('root'), [row])
        backlog.write_text('## Build order\n- [ ] Active `job/plan+extra`\n- [ ] Next `job/plan`\n')
        self.assertIn('Next `job/plan`', self.idle_work()[0]['text'])
        self.assertNotIn('Active', self.idle_work()[0]['text'])

    def test_idle_work_wakes_once_changes_and_rearms_without_sleeps(self):
        backlog = self.root / 'backlog.md'
        backlog.write_text('## Build order\n- [ ] First runnable\n')
        with mock.patch.object(self.m, 'changes_due', return_value=[]), \
                mock.patch.object(self.m, 'wake_start'), mock.patch.object(self.m, 'WAKE_INTERVAL', 0), \
                mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            def tick(expected):
                self.watch(kinds=EXPLICIT_DELIVERY_KINDS)
                self.m.wake_drain(self.inst, self.inst.node('root'), once=True)
                self.assertEqual(send.call_count, expected)
            tick(1)
            tick(1)
            backlog.write_text('Unrelated prose changed\n## Build order\n- [ ] First runnable\n')
            tick(1)
            backlog.write_text('## Build order\n- [x] First runnable\n- [ ] Next runnable\n')
            tick(2)
            self.assertEqual([i['kind'] for i in self.since()], ['idle-with-runnable-work'])
            backlog.write_text('## Build order\n- [x] First runnable\n- [x] Next runnable\n')
            tick(2)
            backlog.write_text('## Build order\n- [ ] Next runnable\n')
            tick(3)

    def test_queued_hold_wakes_once_for_four_ticks_and_changing_numbers(self):
        self.queued_machine_work()
        reading = json.loads(self.env['BRAIN_MACHINE_READING'])
        with mock.patch.object(self.m, 'machine_reading', return_value=reading), \
                mock.patch.object(self.m, 'changes_due', return_value=[]), mock.patch.object(self.m, 'wake_start'), \
                mock.patch.object(self.m, 'WAKE_INTERVAL', 0), mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            for load in (1.5, 1.5, 1.5, 1.5, 1.6, 1.9):
                reading['load_per_cpu'] = [load] * 3
                self.watch(kinds=EXPLICIT_DELIVERY_KINDS)
                self.m.wake_drain(self.inst, self.inst.node('root'), once=True)
            self.assertEqual(send.call_count, 1)
            history = self.m.wake_history(self.m.wake_directory(self.inst, self.inst.node('root')))
            self.assertEqual([i['kind'] for i in history[-1]['items']], ['machine-hold'])

    def test_queued_hold_releases_rearms_changes_reason_and_keeps_questions(self):
        self.queued_machine_work()
        good = json.loads(self.env['BRAIN_MACHINE_READING'])
        reading = dict(good)
        with mock.patch.object(self.m, 'machine_reading', return_value=reading), \
                mock.patch.object(self.m, 'changes_due', return_value=[]), mock.patch.object(self.m, 'wake_start'), \
                mock.patch.object(self.m, 'WAKE_INTERVAL', 0), mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            def tick(expected):
                self.watch(kinds=EXPLICIT_DELIVERY_KINDS)
                self.m.wake_drain(self.inst, self.inst.node('root'), once=True)
                self.assertEqual(send.call_count, expected)
            reading['load_per_cpu'] = [1.5] * 3
            tick(1)
            reading.update(good)
            tick(1)  # release is quiet
            reading['load_per_cpu'] = [1.5] * 3
            tick(2)
            reading.update(good, thermal='pressure')
            tick(3)  # a different reason opens another episode
            (self.root / 'inbox.md').write_text('- fixture: choose a plan <!-- host-question:fixture -->\n')
            tick(4)  # real work still wakes under an unchanged hold

    def test_recovered_pending_queue_hold_keeps_mixed_question(self):
        self.queued_machine_work()
        good = json.loads(self.env['BRAIN_MACHINE_READING'])
        reading = {**good, 'load_per_cpu': [1.5] * 3}
        with mock.patch.object(self.m, 'machine_reading', return_value=reading), \
                mock.patch.object(self.m, 'changes_due', return_value=[]), mock.patch.object(self.m, 'wake_start'), \
                mock.patch.object(self.m, 'WAKE_INTERVAL', 0), mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            self.watch(kinds=EXPLICIT_DELIVERY_KINDS)  # queued, not delivered yet
            directory = self.m.wake_directory(self.inst, self.inst.node('root'))
            pending = list(directory.glob('*.pending'))
            self.assertEqual(len(pending), 1)
            value = json.loads(pending[0].read_text())
            question = {'id': 'b' * 64, 'kind': 'inbox'}
            value['items'].append(question)
            pending[0].write_text(json.dumps(value))
            reading.update(good)
            self.m.wake_drain(self.inst, self.inst.node('root'), once=True)
            self.assertEqual(send.call_count, 1)
            self.assertEqual(self.m.wake_history(directory)[-1]['items'], [question])

    def test_due_quiet_claims_queue_hold_once_and_shares_watcher_receipt(self):
        self.queued_machine_work()
        reading = {**json.loads(self.env['BRAIN_MACHINE_READING']), 'load_per_cpu': [1.5] * 3}
        results = []
        self.m.refresh_status(self.inst, self.inst.node('root'))
        with mock.patch.object(self.m, 'machine_reading', return_value=reading), \
                mock.patch.object(self.m, 'changes_due', return_value=[]), mock.patch.object(self.m, 'load', return_value=self.inst), \
                mock.patch.object(self.m, 'wake_start'), mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            for _ in range(4):
                with self.assertRaises(SystemExit) as result:
                    self.m.cmd_due(argparse.Namespace(id='root', quiet=True, json=False))
                results.append(result.exception.code)
            self.assertEqual(results, [1, 0, 0, 0])
            self.watch()
            self.m.wake_drain(self.inst, self.inst.node('root'), once=True)
            send.assert_not_called()
            (self.root / 'inbox.md').write_text('- fixture: choose a plan <!-- host-question:fixture -->\n')
            with self.assertRaises(SystemExit) as result:
                self.m.cmd_due(argparse.Namespace(id='root', quiet=True, json=False))
            self.assertEqual(result.exception.code, 1)

    def test_queue_hold_identity_survives_restarts_and_sustained_readings(self):
        self.queued_machine_work()
        good = json.loads(self.env['BRAIN_MACHINE_READING'])
        reading = {**good, 'load_per_cpu': [1.5] * 3}
        with mock.patch.object(self.m, 'machine_reading', return_value=reading):
            first = self.m.machine_hold_items(self.inst)
            # A reader in another process gets the persisted episode, independent of text values.
            restarted = self.m.Instance(self.root)
            reading['sample_reasons'] = ['sustained 5-minute load']
            self.assertEqual(self.m.machine_hold_items(restarted), first)
            reading.pop('sample_reasons')
            reading['load_per_cpu'] = [1.9] * 3
            self.assertEqual(self.m.machine_hold_items(restarted), first)
            reading.update(good)
            self.assertEqual(self.m.machine_hold_items(restarted), {})
            reading['load_per_cpu'] = [1.5] * 3
            second = self.m.machine_hold_items(restarted)
            self.assertNotEqual(first, second)
            self.assertFalse(next(iter(second.values()))['reported'])

    def test_queue_hold_enqueue_failure_retries_and_recovery_retires_pending(self):
        self.queued_machine_work()
        good = json.loads(self.env['BRAIN_MACHINE_READING'])
        reading = {**good, 'load_per_cpu': [1.5] * 3}
        with mock.patch.object(self.m, 'machine_reading', return_value=reading), \
                mock.patch.object(self.m, 'changes_due', return_value=[]), mock.patch.object(self.m, 'wake_start'), \
                mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            with mock.patch.object(self.m, 'request_wake', return_value=False):
                self.watch(kinds=EXPLICIT_DELIVERY_KINDS)
            self.assertFalse(next(iter(self.m.machine_hold_items(self.inst).values()))['reported'])
            self.watch(kinds=EXPLICIT_DELIVERY_KINDS)
            directory = self.m.wake_directory(self.inst, self.inst.node('root'))
            self.assertEqual(len(list(directory.glob('*.pending'))), 1)
            reading.update(good)
            self.m.wake_drain(self.inst, self.inst.node('root'), once=True)
            send.assert_not_called()
            self.assertFalse(list(directory.glob('*.pending')))

    def test_broken_hold_state_preserves_real_work_and_pending_questions(self):
        self.queued_machine_work()
        reading = {**json.loads(self.env['BRAIN_MACHINE_READING']), 'load_per_cpu': [1.5] * 3}
        with mock.patch.object(self.m, 'machine_reading', return_value=reading), \
                mock.patch.object(self.m, 'changes_due', return_value=[]), mock.patch.object(self.m, 'wake_start'), \
                mock.patch.object(self.m, 'WAKE_INTERVAL', 0), mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            self.watch()
            state = self.root / '.brain/wake/machine-holds.json'
            state.write_text('broken')
            (self.root / 'inbox.md').write_text('- fixture: choose a plan <!-- host-question:fixture -->\n')
            self.watch()
            self.m.wake_drain(self.inst, self.inst.node('root'), once=True)
            self.assertEqual(send.call_count, 1)
            self.assertEqual(state.read_text(), 'broken')
            history = self.m.wake_history(self.m.wake_directory(self.inst, self.inst.node('root')))
            self.assertIn('inbox', [i['kind'] for i in history[-1]['items']])

    def test_queued_thermal_hold_and_alert_enqueue_one_wake(self):
        self.queued_machine_work()
        hot = {**json.loads(self.env['BRAIN_MACHINE_READING']), 'thermal': 'pressure'}
        with mock.patch.object(self.m, 'machine_reading', return_value=hot), \
                mock.patch.object(self.m, 'changes_due', return_value=[]), mock.patch.object(self.m, 'wake_start'), \
                mock.patch.object(self.m, 'WAKE_INTERVAL', 0), mock.patch.object(self.m, 'wake_send', return_value=True), \
                mock.patch.object(self.m, 'request_wake', wraps=self.m.request_wake) as request:
            self.machine_tick(time.time(), hot)
            self.assertEqual(request.call_count, 1)  # no detach window between hold and immediate thermal alert
            self.assertEqual({i['kind'] for i in request.call_args.kwargs['items']},
                             {'machine-hold', 'machine-alert'})

    def test_machine_open_close_are_private_and_never_wake(self):
        base = time.time()
        good = json.loads(self.env['BRAIN_MACHINE_READING'])
        high = {**good, 'load_per_cpu': [1, 1, 1]}
        state = self.root / '.brain/machine.json'
        self.m.refresh_status(self.inst, self.inst.node('root'))
        state.write_text(json.dumps({'samples': [{'at': base - i * 60, 'reading': high} for i in (4, 3, 2, 1)]}))
        before = (self.root / 'inbox.md').read_bytes()
        head = self.git('rev-parse', 'HEAD', cwd=self.root)
        with mock.patch.object(self.m, 'changes_due', return_value=[]), mock.patch.object(self.m, 'wake_start'), \
                mock.patch.object(self.m, 'WAKE_INTERVAL', 0), mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            self.machine_tick(base, high)
            episode = json.loads(state.read_text())['episode_id']
            due = self.m.due_items(self.inst, self.inst.node('root'), include_stale=False)
            self.assertEqual([(i['kind'], i.get('informational')) for i in due], [('machine', True)])
            self.assertEqual(self.m.watch_items(self.inst, self.inst.node('root')), [])
            with mock.patch.object(self.m, 'load', return_value=self.inst), self.assertRaises(SystemExit) as quiet:
                self.m.cmd_due(argparse.Namespace(id='root', quiet=True, json=False))
            self.assertEqual(quiet.exception.code, 0)  # telemetry cannot stale facts or wake a quiet tick
            for offset in (60, 120, 180):
                self.machine_tick(base + offset, good)
            self.assertEqual(send.call_count, 0)
        data = json.loads(state.read_text())
        self.assertEqual([(r['episode'], r['state']) for r in data['transitions']], [(episode, 'open'), (episode, 'close')])
        self.assertEqual((self.root / 'inbox.md').read_bytes(), before)
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=self.root), head)
        self.assertEqual(self.m.due_items(self.inst, self.inst.node('root'), include_stale=False), [])
        self.assertNotIn('.brain/', self.git('ls-files', cwd=self.root))

    def test_machine_hold_over_60_minutes_wakes_once_per_episode(self):
        base = time.time()
        high = {**json.loads(self.env['BRAIN_MACHINE_READING']), 'load_per_cpu': [1, 1, 1]}
        state = self.root / '.brain/machine.json'
        state.write_text(json.dumps({'samples': [{'at': base - i * 60, 'reading': high} for i in (4, 3, 2, 1)]}))
        with mock.patch.object(self.m, 'changes_due', return_value=[]), mock.patch.object(self.m, 'wake_start'), \
                mock.patch.object(self.m, 'WAKE_INTERVAL', 0), mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            self.machine_tick(base, high)
            self.machine_tick(base + 3600, high)
            self.assertEqual(send.call_count, 0)
            self.machine_tick(base + 3601, high)
            self.assertEqual(send.call_count, 1)
            # Thermal pressure in the same prolonged hold does not create a second escalation.
            self.machine_tick(base + 3661, {**high, 'thermal': 'pressure'})
            self.machine_tick(base + 3721, high)
            self.assertEqual(send.call_count, 1)
            history = self.m.wake_history(self.m.wake_directory(self.inst, self.inst.node('root')))
            self.assertEqual([i['kind'] for i in history[-1]['items']], ['machine-alert'])

    def test_machine_thermal_pressure_wakes_once_and_rearms_after_recovery(self):
        base = time.time()
        good = json.loads(self.env['BRAIN_MACHINE_READING'])
        hot = {**good, 'thermal': 'pressure'}
        with mock.patch.object(self.m, 'changes_due', return_value=[]), mock.patch.object(self.m, 'wake_start'), \
                mock.patch.object(self.m, 'WAKE_INTERVAL', 0), mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            self.machine_tick(base, hot)  # raw pressure alerts immediately, before sustained admission history
            self.assertEqual(send.call_count, 1)
            self.machine_tick(base + 60, {**good, 'thermal': 'unknown'})
            self.machine_tick(base + 120, hot)
            self.assertEqual(send.call_count, 1)
            for offset in (180, 240, 300):
                self.machine_tick(base + offset, good)
            self.assertEqual(send.call_count, 1)  # clear is quiet
            self.machine_tick(base + 360, hot)
            self.assertEqual(send.call_count, 2)

    def test_legacy_machine_lines_are_quiet_but_questions_with_markers_survive(self):
        legacy = '- fixture from brain: heavy work hold cleared [machine episode ' + 'a' * 24 + ' close]'
        inbox = self.root / 'inbox.md'
        inbox.write_text(legacy + '\n')
        with mock.patch.object(self.m, 'changes_due', return_value=[]), mock.patch.object(self.m, 'wake_start'), \
                mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            self.watch()
            self.m.wake_drain(self.inst, self.inst.node('root'), once=True)
            self.assertEqual(send.call_count, 0)
            inbox.write_text('- question <!-- host-question:fixture --> [machine episode ' + 'a' * 24 + ' close]\n')
            self.watch()
            self.m.wake_drain(self.inst, self.inst.node('root'), once=True)
            self.assertEqual(send.call_count, 1)

    def test_pending_machine_transitions_are_retired_and_mixed_questions_survive(self):
        n = self.inst.node('root')
        transition = {'id': 'a' * 64, 'kind': 'machine'}
        question = {'id': 'b' * 64, 'kind': 'inbox'}
        with mock.patch.object(self.m, 'wake_start'), mock.patch.object(self.m, 'WAKE_INTERVAL', 0), \
                mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            self.m.request_wake(self.inst, n, 'old transition', items=[transition])
            self.m.wake_drain(self.inst, n, once=True)
            self.assertEqual(send.call_count, 0)
            self.m.request_wake(self.inst, n, 'mixed transition and question', items=[transition, question])
            self.m.wake_drain(self.inst, n, once=True)
            self.assertEqual(send.call_count, 1)
            history = self.m.wake_history(self.m.wake_directory(self.inst, n))
            self.assertEqual(history[-1]['items'], [question])

    def test_failed_machine_alert_is_retired_after_recovery(self):
        base = time.time()
        good = json.loads(self.env['BRAIN_MACHINE_READING'])
        with mock.patch.object(self.m, 'changes_due', return_value=[]), mock.patch.object(self.m, 'wake_start'), \
                mock.patch.object(self.m, 'WAKE_INTERVAL', 0):
            with mock.patch.object(self.m, 'wake_send', return_value=False):
                self.machine_tick(base, {**good, 'thermal': 'pressure'})
            directory = self.m.wake_directory(self.inst, self.inst.node('root'))
            self.assertTrue(list(directory.glob('*.pending')))
            with mock.patch.object(self.m, 'wake_send', return_value=True) as send:
                self.machine_tick(base + 60, good)
            self.assertEqual(send.call_count, 0)
            self.assertFalse(list(directory.glob('*.pending')))

    def test_machine_unknown_thermal_rearms_recovered_load_holds(self):
        base = time.time()
        good = {**json.loads(self.env['BRAIN_MACHINE_READING']), 'thermal': 'unknown'}
        high = {**good, 'load_per_cpu': [1, 1, 1]}
        state = self.root / '.brain/machine.json'
        state.write_text(json.dumps({'samples': [{'at': base - i * 60, 'reading': high} for i in (4, 3, 2, 1)]}))
        with mock.patch.object(self.m, 'changes_due', return_value=[]), mock.patch.object(self.m, 'wake_start'), \
                mock.patch.object(self.m, 'WAKE_INTERVAL', 0), mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            self.machine_tick(base, high)
            first = json.loads(state.read_text())['episode_id']
            self.machine_tick(base + 3601, high)
            self.assertEqual(send.call_count, 1)
            for offset in (3661, 3721, 3781):
                self.machine_tick(base + offset, good)
            self.assertFalse(json.loads(state.read_text())['alert_open'])
            for offset in (3841, 3901, 3961, 4021, 4081):
                self.machine_tick(base + offset, high)
            self.assertNotEqual(json.loads(state.read_text())['episode_id'], first)
            self.assertEqual(send.call_count, 1)
            self.machine_tick(base + 7682, high)
            self.machine_tick(base + 7742, high)
            self.assertEqual(send.call_count, 2)

    def test_failed_machine_load_alert_retires_with_unknown_thermal_recovery(self):
        base = time.time()
        good = {**json.loads(self.env['BRAIN_MACHINE_READING']), 'thermal': 'unknown'}
        high = {**good, 'load_per_cpu': [1, 1, 1]}
        state = self.root / '.brain/machine.json'
        state.write_text(json.dumps({'samples': [{'at': base - i * 60, 'reading': high} for i in (4, 3, 2, 1)]}))
        with mock.patch.object(self.m, 'changes_due', return_value=[]), mock.patch.object(self.m, 'wake_start'), \
                mock.patch.object(self.m, 'WAKE_INTERVAL', 0):
            with mock.patch.object(self.m, 'wake_send', return_value=False):
                self.machine_tick(base, high)
                self.machine_tick(base + 3601, high)
            directory = self.m.wake_directory(self.inst, self.inst.node('root'))
            self.assertTrue(list(directory.glob('*.pending')))
            for offset in (3661, 3721, 3781):
                with mock.patch.object(self.m.time, 'time', return_value=base + offset), \
                        mock.patch.object(self.m, 'machine_load', return_value=good), contextlib.redirect_stdout(io.StringIO()):
                    self.m.machine_sample(self.inst)
            with mock.patch.object(self.m, 'wake_send', return_value=True) as send:
                self.watch(kinds=EXPLICIT_DELIVERY_KINDS)
                self.m.wake_drain(self.inst, self.inst.node('root'), once=True)
            self.assertEqual(send.call_count, 0)
            self.assertFalse(list(directory.glob('*.pending')))

    def test_delivery_history_and_since_wake_track_ids_only_after_success(self):
        inbox = self.root / 'inbox.md'
        inbox.write_text('- fabricated first item\n')
        n = self.inst.node('root')
        directory = self.m.wake_directory(self.inst, n)
        with mock.patch.object(self.m, 'changes_due', return_value=[]), \
                mock.patch.object(self.m, 'wake_start'), mock.patch.object(self.m, 'WAKE_INTERVAL', 0):
            first = self.since()
            self.assertEqual(len(first), 1)
            self.watch(kinds=EXPLICIT_DELIVERY_KINDS)
            self.assertEqual(self.since(), first)  # durable enqueue is not delivery
            with mock.patch.object(self.m, 'wake_send', return_value=False):
                self.m.wake_drain(self.inst, n, once=True)
            self.assertEqual(self.since(), first)
            self.assertFalse((directory / 'history.json').exists())
            with mock.patch.object(self.m, 'wake_send', return_value=True) as send:
                self.m.wake_drain(self.inst, n, once=True)
                self.assertEqual(self.since(), first)  # the item that woke us remains visible
                inbox.write_text(inbox.read_text() + '- fabricated second item\n')
                new = [i for i in self.since() if i['id'] != first[0]['id']]
                self.assertEqual(len(new), 1)
                self.watch(kinds=EXPLICIT_DELIVERY_KINDS)
                self.m.wake_drain(self.inst, n, once=True)
                self.assertEqual(send.call_count, 2)
            history = self.m.wake_history(directory)
            self.assertEqual([r['items'] for r in history], [
                [{'id': first[0]['id'], 'kind': 'inbox'}], [{'id': new[0]['id'], 'kind': 'inbox'}]])
            self.assertNotIn('fabricated', (directory / 'history.json').read_text())
            self.assertEqual([i['id'] for i in self.since()], [new[0]['id']])
            # Third wake still hides A and B: snapshots contain all work due at each wake.
            inbox.write_text(inbox.read_text() + '- fabricated third item\n')
            self.watch(kinds=EXPLICIT_DELIVERY_KINDS)
            with mock.patch.object(self.m, 'wake_send', return_value=True):
                self.m.wake_drain(self.inst, n, once=True)
            self.assertEqual([i['text'] for i in self.since()], ['fabricated third item'])
            self.assertNotIn('.brain/', self.git('ls-files', cwd=self.root))

    def test_child_local_work_never_wakes_root_but_questions_and_failures_do(self):
        self.b(self.root, 'new', 'sample', '--title', 'Sample')
        self.inst = self.m.Instance(self.root)
        child, root = self.inst.node('sample'), self.inst.node('root')
        row = {**dict.fromkeys(self.m.JOB_COLS, ''), 'Job': 'job/closed',
               'Started': '2020-01-01T10:00Z', 'Ended': '2020-01-01T11:00Z', 'Outcome': 'closed'}
        self.m.write_jobs(self.inst, child, [row])
        directory = self.m.wake_directory(self.inst, root)
        with mock.patch.object(self.m, 'changes_due', return_value=[]), \
                mock.patch.object(self.m, 'wake_start'), mock.patch.object(self.m, 'WAKE_INTERVAL', 0), \
                mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            self.assertIn('unsettled-threads', [i['kind'] for i in self.m.watch_items(self.inst, child)])
            self.watch(kinds=EXPLICIT_DELIVERY_KINDS)
            self.m.wake_drain(self.inst, root, once=True)
            self.assertEqual(send.call_count, 0)
            budget = {**row, 'Job': 'import:budget', 'Ended': '', 'Outcome': ''}
            self.m.write_jobs(self.inst, child, [row, budget])
            self.inst.local_of('sample')['worktrees'] = {budget['Job']: {'path': str(self.root)}}
            self.inst.save_local()
            contract = self.m.contract_file(child, budget)
            contract.parent.mkdir(exist_ok=True)
            contract.write_text('## Budget\n\n1 h\n')
            self.assertTrue(any('over its 1-hour budget' in text
                                for text in self.m.runaway_jobs(self.inst, child)))
            self.watch(kinds=EXPLICIT_DELIVERY_KINDS)
            self.m.wake_drain(self.inst, root, once=True)
            self.assertEqual(send.call_count, 0)
            self.assertEqual(self.since(), [])
            # A child with a coordinator receives its own settlement work.
            self.inst.local_of('sample').update(coordinator='fixture-child', wake='fixture-send')
            self.inst.save_local()
            self.watch(kinds=EXPLICIT_DELIVERY_KINDS)
            self.assertTrue(list(self.m.wake_directory(self.inst, child).glob('*.pending')))
            self.inst.local_of('sample').pop('coordinator')
            self.inst.save_local()
            line = '- 2020-01-01 00:00 +00:00 from runtime: Host question: Choose blue <!-- host-question:abc -->'
            (child['dir'] / 'inbox.md').write_text(line + '\n')
            self.watch(kinds=EXPLICIT_DELIVERY_KINDS)
            self.assertIn('overdue-question', [i['kind'] for i in self.since()])
            routed = self.since('sample')
            self.assertTrue(routed)
            self.assertTrue(all(i['node'] == 'sample' for i in routed))
            self.m.wake_drain(self.inst, root, once=True)
            self.assertEqual(send.call_count, 1)
            self.watch(kinds=EXPLICIT_DELIVERY_KINDS)
            self.m.wake_drain(self.inst, root, once=True)
            self.assertEqual(send.call_count, 1)
            self.assertIn('overdue-question', [i['kind'] for i in self.m.wake_history(directory)[-1]['items']])
            (child['dir'] / 'inbox.md').write_text(line + '\n- from watcher: worker run failed (provider error)\n')
            self.watch(kinds=EXPLICIT_DELIVERY_KINDS)
            self.m.wake_drain(self.inst, root, once=True)
            self.assertEqual(send.call_count, 2)

    def test_overdue_block_is_one_wake_even_as_age_advances(self):
        status = self.root / 'status.md'
        with status.open('a') as stream:
            stream.write('\n## Blocked\n- Choose blue; since: 2020-01-01T00:00Z; recommendation: blue; default: gray\n')
        n = self.inst.node('root')
        with mock.patch.object(self.m, 'changes_due', return_value=[]), \
                mock.patch.object(self.m, 'wake_start'), mock.patch.object(self.m, 'WAKE_INTERVAL', 0), \
                mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            self.watch()
            self.m.wake_drain(self.inst, n, once=True)
            self.assertEqual(send.call_count, 1)
            import datetime
            later = self.inst.now_dt() + datetime.timedelta(days=2)
            with mock.patch.object(self.inst, 'now_dt', return_value=later):
                self.watch()
                self.m.wake_drain(self.inst, n, once=True)
                self.assertEqual([i['kind'] for i in self.since()], ['blocked-overdue'])
            self.assertEqual(send.call_count, 1)

    def test_due_episode_ignores_child_commits_and_block_timestamp_churn(self):
        self.b(self.root, 'new', 'sample', '--title', 'Sample')
        self.inst = self.m.Instance(self.root)
        root, child = self.inst.node('root'), self.inst.node('sample')
        status = self.root / 'status.md'
        original = status.read_text()
        with mock.patch.object(self.m, 'changes_due', return_value=[]), \
                mock.patch.object(self.m, 'wake_start'), mock.patch.object(self.m, 'WAKE_INTERVAL', 0), \
                mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            for index in range(6):
                status.write_text(original + '\n## Blocked\n- Choose blue; since: 2020-01-0'
                                  + str(index + 1) + 'T00:00Z; recommendation: blue; default: gray\n')
                if index:
                    (child['dir'] / 'status.md').write_text('Updated: fixture ' + str(index) + '\n')
                    self.git('add', str(child['dir'] / 'status.md'), cwd=self.root)
                    self.git('commit', '-qm', 'child status fixture ' + str(index), cwd=self.root)
                self.watch()
                self.m.wake_drain(self.inst, root, once=True)
            self.assertEqual(send.call_count, 1)

    def test_due_item_resolution_rearms_same_item_without_expiring_sent_receipt(self):
        root = self.inst.node('root')
        inbox = self.root / 'inbox.md'
        with mock.patch.object(self.m, 'changes_due', return_value=[]), \
                mock.patch.object(self.m, 'wake_start'), mock.patch.object(self.m, 'WAKE_INTERVAL', 0), \
                mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            inbox.write_text('- Choose blue\n')
            self.watch(kinds=EXPLICIT_DELIVERY_KINDS)
            self.m.wake_drain(self.inst, root, once=True)
            inbox.write_text('')
            self.watch(kinds=EXPLICIT_DELIVERY_KINDS)
            inbox.write_text('- Choose blue\n')
            self.watch(kinds=EXPLICIT_DELIVERY_KINDS)
            self.m.wake_drain(self.inst, root, once=True)
            self.assertEqual(send.call_count, 2)
            self.assertEqual([i['text'] for i in self.since()], ['Choose blue'])

    def test_stale_concurrent_scan_cannot_rearm_delivered_due_episode(self):
        root = self.inst.node('root')
        inbox = self.root / 'inbox.md'
        inbox.write_text('')
        scanned, resume = threading.Event(), threading.Event()
        original = self.m.watch_due_episodes
        errors = []
        def paused(inst, scan):
            if threading.current_thread().name == 'old-reader':
                scanned.set()
                if not resume.wait(10):
                    raise RuntimeError('fixture scan timeout')
            return original(inst, scan)
        def old_reader():
            try:
                self.m.watch_groups(self.m.Instance(self.root))
            except BaseException as error:
                errors.append(error)
        with mock.patch.object(self.m, 'changes_due', return_value=[]), \
                mock.patch.object(self.m, 'wake_start'), mock.patch.object(self.m, 'WAKE_INTERVAL', 0), \
                mock.patch.object(self.m, 'wake_send', return_value=True) as send, \
                mock.patch.object(self.m, 'watch_due_episodes', side_effect=paused):
            reader = threading.Thread(target=old_reader, name='old-reader')
            reader.start()
            try:
                self.assertTrue(scanned.wait(10))
                inbox.write_text('- Decision needed: choose blue\n')
                self.watch(kinds=EXPLICIT_DELIVERY_KINDS)
                self.m.wake_drain(self.inst, root, once=True)
                self.assertEqual(send.call_count, 1)
            finally:
                resume.set()
                reader.join(10)
            self.assertFalse(reader.is_alive())
            self.assertEqual(errors, [])
            self.watch(kinds=EXPLICIT_DELIVERY_KINDS)
            self.m.wake_drain(self.inst, root, once=True)
            self.assertEqual(send.call_count, 1)

    def test_due_job_text_and_state_changes_wake_but_display_metadata_does_not(self):
        root = self.inst.node('root')
        item = {'node': 'root', 'kind': 'overlapping-inputs', 'job': 'fixture-job',
                'text': 'job/fixture: review waiting inputs'}
        with mock.patch.object(self.m, 'due_items', side_effect=lambda *a, **k: [dict(item)]), \
                mock.patch.object(self.m, 'changes_due', return_value=[]), \
                mock.patch.object(self.m, 'wake_start'), mock.patch.object(self.m, 'WAKE_INTERVAL', 0), \
                mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            self.watch(kinds=EXPLICIT_DELIVERY_KINDS)
            self.m.wake_drain(self.inst, root, once=True)
            item.update(count=9, at='2099-01-01T00:00Z', head='a' * 40)
            self.watch(kinds=EXPLICIT_DELIVERY_KINDS)
            self.m.wake_drain(self.inst, root, once=True)
            self.assertEqual(send.call_count, 1)
            item['text'] = 'job/fixture: conflicting inputs need repair'
            self.watch(kinds=EXPLICIT_DELIVERY_KINDS)
            self.m.wake_drain(self.inst, root, once=True)
            self.assertEqual(send.call_count, 2)
            item['state'] = 'blocked'
            self.watch(kinds=EXPLICIT_DELIVERY_KINDS)
            self.m.wake_drain(self.inst, root, once=True)
            self.assertEqual(send.call_count, 3)

    def test_resolved_due_episode_pending_retires_but_inbox_decision_still_delivers(self):
        root = self.inst.node('root')
        status = self.root / 'status.md'
        original = status.read_text()
        status.write_text(original + '\n## Blocked\n- Choose blue; since: 2020-01-01T00:00Z\n')
        with mock.patch.object(self.m, 'changes_due', return_value=[]), \
                mock.patch.object(self.m, 'wake_start'), mock.patch.object(self.m, 'WAKE_INTERVAL', 0), \
                mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            self.watch(kinds=EXPLICIT_DELIVERY_KINDS)
            (self.root / 'inbox.md').write_text('- Decision needed: choose green\n')
            self.watch(kinds=EXPLICIT_DELIVERY_KINDS)
            status.write_text(original)
            self.m.wake_drain(self.inst, root, once=True)
            self.assertEqual(send.call_count, 1)
            refs = self.m.wake_history(self.m.wake_directory(self.inst, root))[-1]['items']
            self.assertEqual([i['kind'] for i in refs], ['inbox'])
            self.assertFalse(list(self.m.wake_directory(self.inst, root).glob('*.pending')))

    def test_direct_question_delivery_history_and_corrupt_history(self):
        n = self.inst.node('root')
        episode = 'a' * 32
        (self.root / 'inbox.md').write_text('- question <!-- wake:' + episode + ' --> <!-- host-question:abc -->\n')
        with mock.patch.object(self.m, 'changes_due', return_value=[]), \
                mock.patch.object(self.m, 'wake_start'), mock.patch.object(self.m, 'wake_send', return_value=True):
            first = self.since()
            self.assertTrue(self.m.request_wake(self.inst, n, 'wake:' + episode))
            self.m.wake_drain(self.inst, n, once=True)
            self.assertEqual(self.since(), first)
            path = self.m.wake_directory(self.inst, n) / 'history.json'
            self.assertEqual(self.m.wake_history(path.parent)[-1]['items'],
                             [{'id': first[0]['id'], 'kind': 'inbox'}])
            # The next direct question is handled while delivery waits. Its trigger survives.
            (self.root / 'inbox.md').write_text('- second <!-- wake:' + 'b' * 32 + ' --> <!-- host-question:def -->\n')
            second = self.since()
            self.assertTrue(self.m.request_wake(self.inst, n, 'wake:' + 'b' * 32))
            (self.root / 'inbox.md').write_text('')
            with mock.patch.object(self.m, 'WAKE_INTERVAL', 0):
                self.m.wake_drain(self.inst, n, once=True)
            self.assertEqual(self.m.wake_history(path.parent)[-1]['items'],
                             [{'id': second[0]['id'], 'kind': 'inbox'}])
            path.write_text('{torn')
            with self.assertRaises(SystemExit):
                self.since()
            self.assertEqual(path.read_text(), '{torn')

    def test_history_capture_failure_cannot_block_question_delivery(self):
        n = self.inst.node('root')
        with mock.patch.object(self.m, 'watch_groups', side_effect=ValueError('unrelated broken node')), \
                mock.patch.object(self.m, 'wake_start'), mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            self.assertTrue(self.m.request_wake(self.inst, n, 'wake:' + 'a' * 32))
            directory = self.m.wake_directory(self.inst, n)
            (directory / 'history.json').write_text('{torn')
            self.m.wake_drain(self.inst, n, once=True)
            self.assertEqual(send.call_count, 1)
            self.assertFalse(list(directory.glob('*.pending')))
            self.assertEqual(self.m.wake_history(directory)[-1]['items'], [])
            damaged = list(directory.glob('history-invalid-*.json'))
            self.assertEqual(len(damaged), 1)
            self.assertEqual(damaged[0].read_text(), '{torn')

    def test_corrupt_queue_metadata_does_not_hold_other_questions(self):
        n = self.inst.node('root')
        with mock.patch.object(self.m, 'wake_start'), mock.patch.object(self.m, 'WAKE_INTERVAL', 0), \
                mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            self.assertTrue(self.m.request_wake(self.inst, n, 'wake:' + 'c' * 32))
            directory = self.m.wake_directory(self.inst, n)
            for index, value in enumerate(('{torn', '{"items":null}', '{"items":[{}]}')):
                (directory / (f'{index:064x}' + '.pending')).write_text(value)
                self.m.wake_drain(self.inst, n, once=True)
            self.assertEqual(send.call_count, 3)
            self.assertFalse(list(directory.glob('*.pending')))

    def test_history_write_failure_does_not_repeat_delivered_wake(self):
        n = self.inst.node('root')
        original = self.m.atomic_write
        def fail_history(path, *args, **kwargs):
            if path.name == 'history.json':
                raise OSError('fixture disk failure')
            return original(path, *args, **kwargs)
        with mock.patch.object(self.m, 'wake_start'), mock.patch.object(self.m, 'WAKE_INTERVAL', 0), \
                mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            self.assertTrue(self.m.request_wake(self.inst, n, 'fixture request'))
            with mock.patch.object(self.m, 'atomic_write', side_effect=fail_history):
                self.m.wake_drain(self.inst, n, once=True)
            self.m.wake_drain(self.inst, n, once=True)
            self.assertEqual(send.call_count, 1)
            self.assertFalse(list(self.m.wake_directory(self.inst, n).glob('*.pending')))
            self.assertIn('history write failed', self.m.wake_diagnostic(self.inst, n))

    def test_queue_publication_needs_no_hardlinks_and_pending_retry_does_not_scan(self):
        n = self.inst.node('root')
        with mock.patch.object(self.m, 'wake_start'), mock.patch.object(self.m.os, 'link', side_effect=OSError('unsupported')):
            self.assertTrue(self.m.request_wake(self.inst, n, 'fixture request'))
            with mock.patch.object(self.m, 'watch_groups') as scan:
                self.assertTrue(self.m.request_wake(self.inst, n, 'fixture request'))
                scan.assert_not_called()
        self.assertEqual(len(list(self.m.wake_directory(self.inst, n).glob('*.pending'))), 1)

    def test_retarget_filters_child_housekeeping_but_preserves_questions(self):
        self.b(self.root, 'new', 'sample', '--title', 'Sample')
        self.b(self.root, 'set', 'sample', 'coordinator=fixture-child', 'wake=fixture-send')
        self.inst = self.m.Instance(self.root)
        child, root = self.inst.node('sample'), self.inst.node('root')
        housekeeping = {'id': 'a' * 64, 'kind': 'unsettled-threads'}
        question = {'id': 'b' * 64, 'kind': 'inbox'}
        with mock.patch.object(self.m, 'wake_start'), mock.patch.object(self.m, 'WAKE_INTERVAL', 0), \
                mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            self.m.request_wake(self.inst, child, 'housekeeping', items=[housekeeping])
            self.m.request_wake(self.inst, child, 'mixed', items=[housekeeping, question])
            self.b(self.root, 'set', 'sample', 'coordinator=')
            self.m.wake_drain(self.inst, child, once=True)
            self.m.wake_drain(self.inst, root, once=True)
            self.assertEqual(send.call_count, 1)
            history = self.m.wake_history(self.m.wake_directory(self.inst, root))
            self.assertEqual(history[-1]['items'], [question])

    def test_since_wake_is_stable_while_host_starts_before_send_returns(self):
        n = self.inst.node('root')
        inbox = self.root / 'inbox.md'
        inbox.write_text('- fabricated A\n')
        with mock.patch.object(self.m, 'changes_due', return_value=[]), \
                mock.patch.object(self.m, 'wake_start'), mock.patch.object(self.m, 'WAKE_INTERVAL', 0):
            self.watch(kinds=EXPLICIT_DELIVERY_KINDS)
            with mock.patch.object(self.m, 'wake_send', return_value=True):
                self.m.wake_drain(self.inst, n, once=True)
            inbox.write_text(inbox.read_text() + '- fabricated B\n')
            self.watch(kinds=EXPLICIT_DELIVERY_KINDS)
            observed = []
            def host_starts(command, reason=None):
                observed.extend(self.since())
                return True
            with mock.patch.object(self.m, 'wake_send', side_effect=host_starts):
                self.m.wake_drain(self.inst, n, once=True)
            self.assertEqual([i['text'] for i in observed], ['fabricated B'])
            self.assertEqual(observed, self.since())
            directory = self.m.wake_directory(self.inst, n)
            (directory / 'inflight.json').write_text('{"before":[]}')
            self.assertEqual(observed, self.since())  # a stale unlocked flight cannot shift comparison

    def test_first_logged_wake_preserves_preupgrade_acknowledgements(self):
        n = self.inst.node('root')
        inbox = self.root / 'inbox.md'
        inbox.write_text('- fabricated old A\n')
        with mock.patch.object(self.m, 'changes_due', return_value=[]), mock.patch.object(self.m, 'wake_start'):
            self.watch(kinds=EXPLICIT_DELIVERY_KINDS)
            directory = self.m.wake_directory(self.inst, n)
            # Model an old-core delivery: watcher acknowledgement and sent receipt, no log.
            old_ids = [i['id'] for path in directory.glob('*.pending')
                       for i in json.loads(path.read_text())['items']]
            (self.root / '.brain/watch.json').write_text(json.dumps({'version': 1, 'seen': old_ids}))
            for path in directory.glob('*.pending'):
                path.rename(path.with_suffix('.sent'))
            inbox.write_text(inbox.read_text() + '- fabricated new B\n')
            self.watch(kinds=EXPLICIT_DELIVERY_KINDS)
            with mock.patch.object(self.m, 'wake_send', return_value=True):
                self.m.wake_drain(self.inst, n, once=True)
            self.assertEqual([i['text'] for i in self.since()], ['fabricated new B'])

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
            items.append({'kind': 'inbox', 'text': 'new fabricated question <!-- host-question:new -->'})
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
            self.watch(kinds=EXPLICIT_DELIVERY_KINDS)
            directory = self.m.wake_directory(self.inst, self.inst.node('root'))
            self.m.wake_drain(self.inst, self.inst.node('root'), once=True)
            self.assertTrue(list(directory.glob('*.pending')))
            with mock.patch.object(self.m, 'request_wake') as request:
                self.watch(kinds=EXPLICIT_DELIVERY_KINDS)
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

    def test_blocked_episodes_share_direct_and_watcher_identity(self):
        n = self.inst.node('root')
        row = {**dict.fromkeys(self.m.JOB_COLS, ''), 'Job': 'import:unborn', 'Started': '2026-01-01T10:00Z'}
        self.m.write_jobs(self.inst, n, [row])
        args = argparse.Namespace(summary="Synthetic result", id='root', branch=row['Job'], state='blocked', lessons='none: fixture',
                                  report='report.md', reviews=None, need_human=None)
        directory = self.m.wake_directory(self.inst, n)
        episodes, identities = [], []
        with mock.patch.object(self.m, 'load', return_value=self.inst), \
                mock.patch.object(self.m, 'wake_start'), \
                mock.patch.object(self.m, 'wake_send', return_value=True) as send, \
                contextlib.redirect_stdout(io.StringIO()):
            for count in (1, 2):
                self.m.cmd_handback(args)
                record = self.m.read_handback(n, row)
                episode = record['wake_id']
                self.assertRegex(episode, r'^[0-9a-f]{32}$')
                item = next(i for i in self.m.watch_items(self.inst, n) if i['kind'] == 'handback')
                self.assertEqual(item['reason'], 'wake:' + episode)
                self.assertEqual(item['id'], self.m.watch_digest(
                    ['root', 'handback', [self.m.job_key(row), 'blocked', episode]]))
                episodes.append(episode)
                identities.append(item['id'])
                self.m.cmd_handback(args)  # Re-running an undelivered blocked episode keeps its identity.
                self.assertEqual(self.m.read_handback(n, row)['wake_id'], episode)
                self.assertEqual(len(list(directory.glob('*.pending'))), 1)
                if count == 2:
                    (directory / 'last').write_text(str(time.time() - 121))
                self.m.wake_drain(self.inst, n, once=True)
                self.assertEqual(send.call_count, count)
                self.watch()
                self.watch()
                self.m.wake_drain(self.inst, n, once=True)
                self.assertEqual(send.call_count, count)
                self.assertFalse(list(directory.glob('*.pending')))
        self.assertEqual(len(set(episodes)), 2)
        self.assertEqual(len(set(identities)), 2)

    def test_legacy_blocked_handback_keeps_stable_reason_and_identity(self):
        n = self.inst.node('root')
        row = {**dict.fromkeys(self.m.JOB_COLS, ''), 'Job': 'import:unborn', 'Started': '2026-01-01T10:00Z'}
        self.m.write_jobs(self.inst, n, [row])
        signal = self.m.handback_file(n, row)
        signal.parent.mkdir(exist_ok=True)
        record = {'job': row['Job'], 'started': row['Started'], 'state': 'blocked', 'report': 'report.md'}
        signal.write_text(json.dumps(record))
        item = next(i for i in self.m.watch_items(self.inst, n) if i['kind'] == 'handback')
        self.assertEqual(item['reason'], 'handback:' + self.m.digest([self.m.job_key(row), 'blocked']))
        self.assertEqual(item['id'], self.m.watch_digest(['root', 'handback', [self.m.job_key(row), 'blocked']]))
        with mock.patch.object(self.m, 'wake_start'), mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            self.watch()
            self.m.wake_drain(self.inst, n, once=True)
            self.assertEqual(send.call_count, 1)
            signal.write_text(json.dumps({**record, 'report': 'another-report.md'}))
            self.watch()
            self.m.wake_drain(self.inst, n, once=True)
            self.assertEqual(send.call_count, 1)

    def test_question_episode_shares_inbox_wake_and_keeps_unanswered_dedup(self):
        n = self.inst.node('root')
        row = {**dict.fromkeys(self.m.JOB_COLS, ''), 'Job': 'import:unborn', 'Started': '2026-01-01T10:00Z'}
        self.m.write_jobs(self.inst, n, [row])
        args = argparse.Namespace(summary="Synthetic result", question="Synthetic choice?", id='root', branch=row['Job'], state='question', lessons='none: fixture',
                                  report='report.md', reviews=None, need_human=None)
        directory = self.m.wake_directory(self.inst, n)
        with mock.patch.object(self.m, 'load', return_value=self.inst), \
                mock.patch.object(self.m, 'wake_start'), \
                mock.patch.object(self.m, 'wake_send', return_value=True) as send, \
                contextlib.redirect_stdout(io.StringIO()):
            self.m.cmd_handback(args)
            episode = self.m.read_handback(n, row)['wake_id']
            self.assertIn('<!-- wake:' + episode + ' -->', (self.root / 'inbox.md').read_text())
            item = next(i for i in self.m.watch_items(self.inst, n) if i['kind'] == 'inbox')
            self.assertEqual(item['reason'], 'wake:' + episode)
            self.m.wake_drain(self.inst, n, once=True)
            self.watch()
            self.m.cmd_handback(args)  # The same unanswered question still has one inbox owner.
            self.assertEqual(self.m.read_handback(n, row)['wake_id'], episode)
            self.watch()
            self.m.wake_drain(self.inst, n, once=True)
            self.assertEqual(send.call_count, 1)
            (self.root / 'inbox.md').write_text('')
            self.watch()
            (directory / 'last').write_text(str(time.time() - 121))
            self.m.wake_drain(self.inst, n, once=True)
            self.assertEqual(send.call_count, 1)
            self.m.cmd_handback(args)
            (directory / 'last').write_text(str(time.time() - 121))
            self.m.wake_drain(self.inst, n, once=True)
            self.watch()
            self.m.wake_drain(self.inst, n, once=True)
            self.assertEqual(send.call_count, 2)

    def test_answered_blocked_human_episode_does_not_wake_again_after_rerun(self):
        n = self.inst.node('root')
        row = {**dict.fromkeys(self.m.JOB_COLS, ''), 'Job': 'import:unborn', 'Started': '2026-01-01T10:00Z'}
        self.m.write_jobs(self.inst, n, [row])
        args = argparse.Namespace(summary="Synthetic result", id='root', branch=row['Job'], state='blocked', lessons='none: fixture',
                                  report='report.md', reviews=None, need_human='Choose blue')
        directory = self.m.wake_directory(self.inst, n)
        with mock.patch.object(self.m, 'load', return_value=self.inst), \
                mock.patch.object(self.m, 'wake_start'), \
                mock.patch.object(self.m, 'wake_send', return_value=True) as send, \
                contextlib.redirect_stdout(io.StringIO()):
            self.m.cmd_handback(args)
            episode = self.m.read_handback(n, row).get('wake_id')
            self.m.wake_drain(self.inst, n, once=True)
            self.watch()
            self.m.cmd_handback(args)
            repeated = self.m.read_handback(n, row).get('wake_id')
            self.watch()
            (self.root / 'inbox.md').write_text('')
            self.watch()
            (directory / 'last').write_text(str(time.time() - 121))
            self.m.wake_drain(self.inst, n, once=True)
            # Behaviour first: this also fails on the pre-episode runtime without a missing-field error.
            self.assertEqual(send.call_count, 1)
            self.assertRegex(episode, r'^[0-9a-f]{32}$')
            self.assertEqual(repeated, episode)
            self.m.cmd_handback(args)
            self.assertNotEqual(self.m.read_handback(n, row)['wake_id'], episode)
            (directory / 'last').write_text(str(time.time() - 121))
            self.m.wake_drain(self.inst, n, once=True)
            self.watch()
            self.m.wake_drain(self.inst, n, once=True)
            self.assertEqual(send.call_count, 2)

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
        # Exercise the retry mechanism independently of the no-auto-fallback policy.
        with mock.patch.object(self.m._owners['watch_run']._m_usage, 'class_allows_fallback', return_value=True), \
                mock.patch.object(self.m, 'wake_start'):
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
            self.m.request_wake(self.inst, child, '<!-- host-question:abc -->')
            with mock.patch.object(self.m, 'wake_send', return_value=True):
                self.m.wake_drain(self.inst, child, once=True)
            # Match cmd_due's escalation reason: a previously delivered escalation is not resent.
            import hashlib
            reason = 'overdue:' + hashlib.sha256(line.encode()).hexdigest()
            self.m.request_wake(self.inst, self.inst.node('root'), reason)
            with mock.patch.object(self.m, 'wake_send', return_value=True) as send:
                self.m.wake_drain(self.inst, self.inst.node('root'), once=True)
                self.assertEqual(send.call_count, 1)
                self.watch(kinds=EXPLICIT_DELIVERY_KINDS)
                self.watch(kinds=EXPLICIT_DELIVERY_KINDS)
                self.m.wake_drain(self.inst, self.inst.node('root'), once=True)
                self.assertEqual(send.call_count, 1)
            child_queue = self.m.wake_directory(self.inst, child)
            self.assertFalse(list(child_queue.glob('*.pending')))
            (child['dir'] / 'inbox.md').write_text('- new fabricated worker result\n')
            self.watch(kinds=EXPLICIT_DELIVERY_KINDS)
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

    def test_corrupt_state_wakes_current_work_and_external_links_refused(self):
        state = self.root / '.brain/watch.json'
        for damaged in ('{broken', '{}'):
            state.write_text(damaged)
            (self.root / 'inbox.md').write_text('- new work ' + damaged + '\n')
            with mock.patch.object(self.m, 'changes_due', return_value=[]), \
                    mock.patch.object(self.m, 'request_wake', return_value=True) as wake:
                self.watch(kinds=EXPLICIT_DELIVERY_KINDS)
                wake.assert_called()
            self.assertEqual(state.read_text(), damaged)
        state.unlink()
        sentinel = self.tmp / 'outside'
        sentinel.write_text('sentinel')
        state.symlink_to(sentinel)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.watch(kinds=EXPLICIT_DELIVERY_KINDS)
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

    def test_ten_nodes_observed_no_host_network_or_model_calls(self):
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
                mock.patch.object(self.m, 'usage_reading', side_effect=AssertionError('no provider polling')), \
                mock.patch.object(self.m, 'watch_items', wraps=self.m.watch_items) as observed:
            self.watch()
        expected = {'root'} | {'node' + str(i) for i in range(9)}
        self.assertCountEqual([call.args[1]['id'] for call in observed.call_args_list], expected)
        state = json.loads((self.root / '.brain/watch.json').read_text())
        self.assertEqual({item['node'] for item in state['items'] if item['kind'] == 'handback'}, expected)


    def failure_fixture(self):
        n = self.inst.node('root')
        row = {**dict.fromkeys(self.m.JOB_COLS, ''), 'Job': 'import:fixture',
               'Started': '2026-01-01T10:00Z', 'Class': 'build'}
        self.m.write_jobs(self.inst, n, [row])
        key = self.m.job_key(row)
        self.inst.local_of('root')['workers'] = {key: 'fixture-thread'}
        run = {'node': 'root', 'job_key': key, 'thread': 'fixture-thread', 'run': 'fixture-run',
               'status': 'failed', 'runtime': 'codex', 'error': 'model at capacity'}
        (self.root / '.brain/worker-runs.json').write_text(json.dumps({'runs': [run]}))
        return n, row, run

    def test_bad_class_record_still_notifies_and_fingerprints_unrelated_inbox(self):
        n, row, run = self.failure_fixture()
        (self.root / 'inbox.md').write_text('- unrelated fabricated question <!-- host-question:unrelated -->\n')
        owner = self.m._owners['watch_run']
        with mock.patch.object(owner._m_jobs, 'job_class', create=True, side_effect=SystemExit('unknown class')), \
                mock.patch.object(self.m, 'wake_start'):
            self.watch()
        self.assertIn('worker run failed (at capacity)', (self.root / 'inbox.md').read_text())
        self.assertIn('unrelated fabricated question', (self.root / 'inbox.md').read_text())
        self.assertEqual(self.m.read_jobs(n)[0]['Switches'], '')
        self.assertTrue((self.root / '.brain/watch.json').exists())
        self.assertEqual(len(list(self.m.wake_directory(self.inst, n).glob('*.pending'))), 1)

    def test_bad_route_configuration_disables_only_one_automatic_retry(self):
        n, row, run = self.failure_fixture()
        owner = self.m._owners['watch_run']
        for index, exc in enumerate((SystemExit('bad cap'), OSError('fixture'), ValueError('bad cap'))):
            run['run'] = 'bad-route-' + str(index)
            (self.root / '.brain/worker-runs.json').write_text(json.dumps({'runs': [run]}))
            with mock.patch.object(owner._m_usage, 'class_allows_fallback', return_value=True), \
                    mock.patch.object(owner._m_usage, 'class_route', side_effect=exc) as route, \
                    mock.patch.object(self.m, 'wake_start'):
                self.watch()
                route.assert_called_once()
            self.assertEqual(self.m.read_jobs(n)[0]['Switches'], '')
        self.assertEqual((self.root / 'inbox.md').read_text().count('worker run failed'), 3)

    def test_corrupt_host_file_logs_once_but_new_inbox_still_wakes(self):
        path = self.root / '.brain/worker-runs.json'
        path.write_text('{torn')
        (self.root / 'inbox.md').write_text('- new fabricated work <!-- host-question:work -->\n')
        with mock.patch.object(self.m, 'wake_start'), contextlib.redirect_stdout(io.StringIO()) as output:
            self.m.watch_once(self.inst)
            self.m.watch_once(self.inst)
        self.assertEqual(output.getvalue().count('worker-runs invalid'), 1)
        state = json.loads((self.root / '.brain/watch.json').read_text())
        self.assertTrue(state['seen'])
        self.assertEqual(len(list(self.m.wake_directory(self.inst, self.inst.node('root')).glob('*.pending'))), 1)
        # Valid JSON with a malformed runs collection is contained too.
        path.write_text(json.dumps({'runs': 'invalid'}))
        with mock.patch.object(self.m, 'wake_start'):
            self.watch()

    def test_capacity_switch_commits_cleanly_without_waiting_on_own_flock(self):
        n, row, run = self.failure_fixture()
        self.inst.commit([n['dir'] / self.m.JOBS_FILE], 'fixture initial job')
        with mock.patch.object(self.m._owners['watch_run']._m_usage, 'class_allows_fallback', return_value=True), \
                mock.patch.object(self.m, 'LOCK_WAIT', 0), mock.patch.object(self.m, 'wake_start'):
            self.watch()
        self.assertEqual(self.git('status', '--porcelain', cwd=self.root), '')
        self.assertIn('runtime switch at capacity', self.git('log', '-1', '--format=%s', cwd=self.root))
        self.assertIsNone(self.m._owners['watch_run']._m_instance._LOCK['fh'])
        self.assertNotIn('BRAIN_LOCK_HELD', os.environ)

    def test_real_policy_capacity_failure_only_notifies_without_retry_or_switch(self):
        n, row, run = self.failure_fixture()
        self.assertFalse(self.m._owners['watch_run']._m_usage.class_allows_fallback('build'))
        self.inst.commit([n['dir'] / self.m.JOBS_FILE], 'fixture initial job')
        before = self.git('rev-parse', 'HEAD', cwd=self.root).strip()
        with mock.patch.object(self.m, 'wake_start'):
            self.watch()
            self.watch()
        inbox = (self.root / 'inbox.md').read_text()
        self.assertEqual(inbox.count('worker run failed (at capacity)'), 1)
        self.assertNotIn('one retry recorded', inbox)
        self.assertNotIn('relaunch', inbox)
        recorded = self.m.read_jobs(n)[0]
        self.assertEqual(recorded['Switches'], '')
        self.assertEqual(recorded.get('Capacity retry', ''), '')
        commits = self.git('log', before + '..HEAD', '--format=%s', cwd=self.root).splitlines()
        self.assertEqual(commits, ['root: worker failure notice'])
        self.assertEqual(self.git('status', '--porcelain', cwd=self.root), '')

    def test_worker_binding_cli_survives_unrelated_set(self):
        key = 'a' * 24
        self.b(self.root, 'set', 'root', 'worker:' + key + '=fixture-thread')
        self.b(self.root, 'set', 'root', 'preview=http://127.0.0.1:4500')
        local = self.m.Instance(self.root).local_of('root')
        self.assertEqual(local['workers'][key], 'fixture-thread')
        self.assertEqual(local['preview'], 'http://127.0.0.1:4500')
        self.assertNotIn('worker:', (self.root / 'node.md').read_text())
        self.b(self.root, 'set', 'root', 'worker:' + key + '=')
        self.assertNotIn(key, self.m.Instance(self.root).local_of('root')['workers'])

    def test_failed_direct_blocked_handback_recovers_one_wake(self):
        import argparse
        n, row, run = self.failure_fixture()
        (self.root / '.brain/worker-runs.json').unlink()
        self.inst.local_of('root')['worktrees'] = {row['Job']: {'path': str(self.root)}}
        self.inst.save_local()
        # Both a plain blocked signal and a question-bearing blocked signal need recovery.
        for need in (None, 'fabricated choice'):
            with mock.patch.object(self.m, 'load', return_value=self.inst), \
                    mock.patch.object(self.m, 'request_wake', return_value=False), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.m.cmd_handback(argparse.Namespace(summary="Synthetic result", id='root', branch=row['Job'], state='blocked',
                    reviews=None, lessons='none: fixture', report='fixture.md', need_human=need))
            with mock.patch.object(self.m, 'wake_start'):
                self.watch()
                self.watch()
        directory = self.m.wake_directory(self.inst, n)
        self.assertEqual(len(list(directory.glob('*.pending'))), 2)  # one per distinct plain/question item
        self.assertIn('fabricated choice', (self.root / 'inbox.md').read_text())

    def test_failed_stop_question_retries_without_duplicate_when_delivered(self):
        marker = '<!-- host-question:fixture -->'
        (self.root / 'inbox.md').write_text('- fabricated question ' + marker + '\n')
        n = self.inst.node('root')
        with mock.patch.object(self.m, 'wake_start'):
            self.watch()
            with mock.patch.object(self.m, 'wake_send', return_value=True) as send:
                self.m.wake_drain(self.inst, n, once=True)
                self.watch()
                self.m.wake_drain(self.inst, n, once=True)
                self.assertEqual(send.call_count, 1)

    def test_bad_watcher_state_sampler_one_line_and_due_root_diagnostic(self):
        import argparse
        path = self.root / '.brain/watch.json'
        path.write_text('{torn')
        with mock.patch.object(self.m, 'load', return_value=self.inst), \
                mock.patch.object(self.m, 'machine_sample_once'), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.m.cmd_machine(argparse.Namespace(action='sample'))
        self.assertEqual(output.getvalue().splitlines(),
                         ['watch: watcher state invalid; retained for repair (brain due root)'])
        self.assertEqual(path.read_text(), '{torn')
        with mock.patch.object(self.m, 'load', return_value=self.inst), \
                mock.patch.object(self.m, 'core_update', return_value=None), mock.patch.object(self.m, 'auto_upgrade', return_value=None), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.m.cmd_due(argparse.Namespace(id='root', json=True, quiet=False))
        self.assertTrue(any(i['kind'] == 'watcher' and 'watcher state invalid' in i['text']
                            for i in json.loads(output.getvalue())))

    def test_capacity_retry_respects_node_runtime_pin(self):
        n, row, run = self.failure_fixture()
        fm, body = self.m.read_fm(n['dir'] / 'node.md')
        fm['runtime'] = 'codex'
        self.m.write_fm(n['dir'] / 'node.md', fm, body)
        with mock.patch.object(self.m._owners['watch_run']._m_usage, 'class_allows_fallback', return_value=True), \
                mock.patch.object(self.m, 'wake_start'):
            self.watch()
        self.assertEqual(self.m.read_jobs(self.inst.node('root'))[0]['Switches'], '')
        self.assertNotIn('relaunch', (self.root / 'inbox.md').read_text())


class StallWatcher(Scenario):
    """Small file/Git fixtures; no CLI installation, sleeps, host or provider calls."""
    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, self.env)
        env.start()
        self.addCleanup(env.stop)
        self.m = core_loader.SourceFileLoader('stall_fixture', str(CORE / 'bin/brain')).load_module()
        self.root = self.tmp / 'oak'
        self.root.mkdir()
        for name, value in {
            'instance.md': '---\nprincipals: oak\nruntimes: codex\ntimezone: UTC\n---\n',
            'node.md': '---\nid: root\nowners: oak\nhome: here\n---\n',
            '.gitignore': '.brain/\n',
        }.items():
            (self.root / name).write_text(value)
        (self.root / '.brain').mkdir()
        self.inst = self.m.Instance(self.root)
        self.inst.local_of('root').update(coordinator='fixture-root', wake='fixture-send')
        self.inst.save_local()

    def watch(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.m.watch_once(self.inst)

    def since(self, node='root'):
        with mock.patch.object(self.m, 'load', return_value=self.inst), \
                mock.patch.object(self.m, 'request_wake') as request, \
                mock.patch.object(self.m, 'core_update') as upgrade, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.m.cmd_due(argparse.Namespace(id=node, since_wake=True, quiet=False, json=True))
            request.assert_not_called()
            upgrade.assert_not_called()
        return json.loads(output.getvalue())

    def stall_fixture(self, state='frozen'):
        folder = self.root / 'nodes/mill'
        folder.mkdir(parents=True)
        (folder / 'node.md').write_text('---\nid: mill\nhome: none\nverify: true\n---\n')
        self.inst.local_of('mill').update(coordinator='fixture-mill', wake='fixture-send')
        self.inst.save_local()
        n = self.inst.node('mill')
        row = {**dict.fromkeys(self.m.JOB_COLS, ''), 'Job': 'job/river',
               'Started': '2026-01-01T10:00Z', 'Reviews': '0'}
        self.m.write_jobs(self.inst, n, [row])
        self.stall_status(n, state)
        self.inst.local_of('mill')['worktrees'] = {row['Job']: {'path': str(n['dir']),
            'start': '', 'port': 4500}}
        self.inst.save_local()
        return n, row

    def stall_git_home(self, n):
        home = self.repo(self.tmp / 'source')
        node_file = n['dir'] / 'node.md'
        node_file.write_text(node_file.read_text().replace('home: none', 'home: external'))
        self.inst.local_of(n['id'])['home'] = str(home)
        self.inst.save_local()
        return self.inst.node(n['id'])

    def stall_status(self, n, state):
        (n['dir'] / 'status.md').write_text('## Active work\n\n'
            '| Branch | Thread | PR | State |\n| --- | --- | --- | --- |\n'
            '| job/river | fixture-worker | - | ' + state + ' |\n')

    @contextlib.contextmanager
    def stall_transport(self):
        with mock.patch.object(self.m, 'changes_due', return_value=[]), \
                mock.patch.object(self.m, 'watch_completion_items', return_value=[]), \
                mock.patch.object(self.m, 'local_job_state', return_value={'exists': True, 'tip': '', 'merged': False}), \
                mock.patch.object(self.m, 'wake_start'), mock.patch.object(self.m, 'WAKE_INTERVAL', 0), \
                mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            yield send

    def stall_tick(self, minute, n):
        now = datetime.datetime(2026, 1, 1, 10, tzinfo=datetime.timezone.utc) + datetime.timedelta(minutes=minute)
        with mock.patch.object(type(self.inst), 'now_dt', return_value=now):
            self.watch()
            self.m.wake_drain(self.inst, n, once=True)
            return [i for i in self.m.watch_state(self.inst)['items']
                    if i['kind'] == 'job-stall' and i['node'] == n['id']]

    def test_stall_fifteen_minutes_wakes_named_coordinator_once(self):
        n, _ = self.stall_fixture()
        with self.stall_transport() as send:
            self.assertEqual(self.stall_tick(0, n), [])
            send.reset_mock()  # initial unrelated due work is outside the stall episode
            self.assertEqual(self.stall_tick(14, n), [])
            send.assert_not_called()
            items = self.stall_tick(15, n)
            self.assertEqual(len(items), 1)
            self.assertIn('job/river frozen 15 min', items[0]['text'])
            self.assertEqual(send.call_count, 1)
            first = items[0]['id']
            self.assertEqual(self.since(n['id'])[0]['id'], first)
            for minute in (16, 25, 60):
                self.assertEqual(self.stall_tick(minute, n)[0]['id'], first)
            self.assertEqual(send.call_count, 1)
            root_items = self.m.watch_groups(self.inst)['root']['items']
            self.assertFalse(any(i['kind'] == 'job-stall' for i in root_items))

    def test_stall_event_resets_and_second_stall_wakes_again(self):
        n, row = self.stall_fixture('queued')
        with self.stall_transport() as send:
            self.stall_tick(0, n)
            send.reset_mock()
            first = self.stall_tick(15, n)[0]['id']
            row['Reviews'] = '1'
            self.m.write_jobs(self.inst, n, [row])
            self.assertEqual(self.stall_tick(16, n), [])
            self.assertEqual(self.stall_tick(30, n), [])
            second = self.stall_tick(31, n)[0]
            self.assertNotEqual(second['id'], first)
            self.assertIn('job/river queued 15 min', second['text'])
            self.stall_tick(32, n)
            self.assertEqual(send.call_count, 2)

    def test_stall_event_at_fourteen_minutes_prevents_wake(self):
        n, row = self.stall_fixture()
        with self.stall_transport() as send:
            self.stall_tick(0, n)
            send.reset_mock()
            row['Review head'] = 'a' * 40
            self.m.write_jobs(self.inst, n, [row])
            self.assertEqual(self.stall_tick(14, n), [])
            self.assertEqual(self.stall_tick(15, n), [])
            send.assert_not_called()
            self.assertEqual(len(self.stall_tick(29, n)), 1)

    def test_stall_parked_blocked_and_human_handbacks_do_not_stall(self):
        n, row = self.stall_fixture()
        with self.stall_transport():
            for state in ('parked: waiting for human', 'blocked: human choice', 'running'):
                self.stall_status(n, state)
                self.stall_tick(0, n)
                self.assertEqual(self.stall_tick(30, n), [])
            self.stall_status(n, 'frozen: waiting for human')
            self.stall_tick(31, n)
            self.assertEqual(self.stall_tick(60, n), [])
            self.stall_status(n, 'queued')
            path = self.m.handback_file(n, row)
            path.parent.mkdir(exist_ok=True)
            for state in ('blocked', 'question', 'done'):
                path.write_text(json.dumps({'job': row['Job'], 'started': row['Started'], 'state': state}))
                self.stall_tick(61, n)
                self.assertEqual(self.stall_tick(90, n), [])

    def test_stall_queue_move_and_commit_reset_silence(self):
        n, _ = self.stall_fixture()
        n = self.stall_git_home(n)
        with self.stall_transport() as send:
            self.stall_tick(0, n)
            send.reset_mock()
            self.stall_status(n, 'queued: verify lane')
            self.assertEqual(self.stall_tick(14, n), [])
            self.assertEqual(self.stall_tick(15, n), [])
            parent = self.git('rev-parse', 'HEAD', cwd=n['home_path']).strip()
            tip = self.git('commit-tree', 'HEAD^{tree}', '-p', parent, '-m', 'Fixture progress', cwd=n['home_path']).strip()
            self.git('update-ref', 'refs/heads/job/river', tip, cwd=n['home_path'])
            self.assertEqual(self.stall_tick(28, n), [])
            self.assertEqual(self.stall_tick(29, n), [])
            send.assert_not_called()
            self.assertEqual(len(self.stall_tick(43, n)), 1)

    def test_stall_verify_log_activity_resets_and_threshold_is_configurable(self):
        n, row = self.stall_fixture('queued')
        node_file = n['dir'] / 'node.md'
        node_file.write_text(node_file.read_text().replace('verify: true', 'verify: true\njob_stall_minutes: 5'))
        self.inst = self.m.Instance(self.root)
        n = self.inst.node('mill')
        with self.stall_transport() as send:
            self.stall_tick(0, n)
            send.reset_mock()
            folder = n['dir'] / 'job-handbacks'
            folder.mkdir(exist_ok=True)
            log = folder / (self.m.job_key(row) + '-verify-1-abcdef123456.log')
            log.write_text('verify started\n')
            self.assertEqual(self.stall_tick(4, n), [])
            self.assertEqual(self.stall_tick(5, n), [])
            log.write_text('verify finished\n')
            self.assertEqual(self.stall_tick(8, n), [])
            self.assertEqual(self.stall_tick(12, n), [])
            send.assert_not_called()
            self.assertEqual(len(self.stall_tick(13, n)), 1)

    def test_stall_pending_retires_on_event_without_dropping_question(self):
        n, row = self.stall_fixture()
        with self.stall_transport() as send:
            self.stall_tick(0, n)
            send.reset_mock()
            # Queue without delivering, then resolve before the drain can send it.
            with mock.patch.object(self.m, 'wake_drain'):
                self.stall_tick(15, n)
            directory = self.m.wake_directory(self.inst, n)
            pending = list(directory.glob('*.pending'))
            self.assertEqual(len(pending), 1)
            value = json.loads(pending[0].read_text())
            question = {'kind': 'inbox', 'id': 'c' * 64}
            value['items'].append(question)
            pending[0].write_text(json.dumps(value))
            row['Reviews'] = '1'
            self.m.write_jobs(self.inst, n, [row])
            self.assertEqual(self.stall_tick(16, n), [])
            self.assertEqual(send.call_count, 1)
            self.assertEqual(self.m.wake_history(directory)[-1]['items'], [question])

    def test_stall_due_event_observation_rearms_even_between_watcher_ticks(self):
        n, row = self.stall_fixture()
        with self.stall_transport() as send:
            self.stall_tick(0, n)
            send.reset_mock()
            first = self.stall_tick(15, n)[0]['id']
            row['Reviews'] = '1'
            self.m.write_jobs(self.inst, n, [row])
            now = datetime.datetime(2026, 1, 1, 10, 16, tzinfo=datetime.timezone.utc)
            with mock.patch.object(type(self.inst), 'now_dt', return_value=now):
                self.assertEqual(self.m.watch_job_stalls(self.inst, n), [])
            self.inst = self.m.Instance(self.root)  # use persisted observations after a process restart
            second = self.stall_tick(31, self.inst.node('mill'))[0]['id']
            self.assertNotEqual(first, second)
            self.assertEqual(send.call_count, 2)

    def test_stall_acknowledged_compaction_restart_rearms_each_resumed_run(self):
        n, row = self.stall_fixture('running')
        path = self.m.handback_file(n, row)
        path.parent.mkdir(exist_ok=True)
        signal = {'version': 2, 'job': row['Job'], 'started': row['Started'],
                  'state': 'blocked', 'restart': 'compactions', 'restart_id': 'a' * 32,
                  'lessons': 'none: fixture', 'ready': None, 'ready_reasons': []}
        path.write_text(json.dumps(signal))
        with self.stall_transport() as send:
            self.stall_tick(0, n)
            send.reset_mock()
            n['local']['worktrees'][row['Job']]['restart_ack'] = self.m.digest([self.m.job_key(row), signal])
            self.inst.save_local()
            self.assertTrue(self.m.restart_acknowledged(n, row, signal))
            self.stall_status(n, 'frozen')
            self.assertEqual(self.stall_tick(1, n), [])
            self.assertEqual(len(self.stall_tick(16, n)), 1)
            self.assertEqual(send.call_count, 1)
            signal['restart_id'] = 'b' * 32  # an acknowledgement never consumes a later restart
            path.write_text(json.dumps(signal))
            self.assertFalse(self.m.restart_acknowledged(n, row, signal))
            n['local']['worktrees'][row['Job']]['restart_ack'] = self.m.digest([self.m.job_key(row), signal])
            self.inst.save_local()
            # Even when restart/ack happen between ticks and the label stays frozen,
            # the new acknowledged signal starts a fresh silence clock.
            self.assertEqual(self.stall_tick(18, n), [])
            self.assertEqual(len(self.stall_tick(33, n)), 1)
            self.assertEqual(len(self.stall_tick(34, n)), 1)
            signal['restart_id'] = 'c' * 32
            path.write_text(json.dumps(signal))
            self.assertEqual(self.stall_tick(35, n), [])
            self.assertEqual(self.stall_tick(60, n), [])
            history = self.m.wake_history(self.m.wake_directory(self.inst, n))
            stalls = [i for delivery in history for i in delivery['items'] if i['kind'] == 'job-stall']
            self.assertEqual(len(stalls), 2)
            self.assertEqual(len({i['id'] for i in stalls}), 2)

    def test_stall_legacy_completed_job_does_not_wake_and_new_signal_wins(self):
        n, row = self.stall_fixture()
        n = self.stall_git_home(n)
        head = self.git('rev-parse', 'HEAD', cwd=n['home_path']).strip()
        self.git('branch', row['Job'], head, cwd=n['home_path'])
        packet = {'version': 1, 'node': n['id'], 'branch': row['Job'],
                  'head': head, 'main': head, 'integration_base': head,
                  'verify_command': 'true', 'verify_result': 0, 'review_rounds': 1,
                  'inputs': 'c' * 64, 'checked_at': row['Started']}
        legacy = self.m.ready_path(self.inst, n, row['Job'])
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps(packet))
        with self.stall_transport() as send:
            now = datetime.datetime(2026, 1, 1, 10, tzinfo=datetime.timezone.utc)
            with mock.patch.object(type(self.inst), 'now_dt', return_value=now):
                self.assertEqual(self.m.read_ready_packet(self.inst, n, row['Job'], row), packet)
                self.assertEqual(self.m.watch_recorded_ready(self.inst, n, row, None), packet)
                self.assertEqual(self.m.due_evidence(self.inst, n)[0]['handbacks'][self.m.job_key(row)],
                                 {'state': 'done', 'report': '', 'legacy_ready': True})
            self.stall_tick(0, n)
            send.reset_mock()
            self.assertEqual(self.stall_tick(15, n), [])
            send.assert_not_called()
            # A retained legacy success must not override a newer acknowledged restart.
            signal = {'job': row['Job'], 'started': row['Started'], 'state': 'blocked',
                      'restart': 'compactions', 'restart_id': 'a' * 32}
            handback = self.m.handback_file(n, row)
            handback.parent.mkdir(exist_ok=True)
            handback.write_text(json.dumps(signal))
            n['local']['worktrees'][row['Job']]['restart_ack'] = self.m.digest([self.m.job_key(row), signal])
            self.inst.save_local()
            self.assertEqual(self.stall_tick(16, n), [])
            self.assertEqual(len(self.stall_tick(31, n)), 1)
            handback.write_text('{}')  # unknown current signals still fence the legacy packet
            self.assertIsNone(self.m.job_signal(self.inst, n, row))
            self.assertEqual(self.stall_tick(32, n), [])
            handback.write_text('{torn')
            with self.assertRaises(ValueError):
                self.m.watch_job_stalls(self.inst, n)


class WakeNoise(Scenario):
    """Fabricated file/Git instances without an unused installed core or host setup."""
    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(os.environ, self.env)
        patch.start()
        self.addCleanup(patch.stop)
        self.m = core_loader.SourceFileLoader('wake_noise_fixture', str(CORE / 'bin/brain')).load_module()
        self.root = self.repo(self.tmp / 'oak', {
            'instance.md': '---\nname: oak\ntimezone: UTC\nruntimes: codex\nhost: t3\n---\n',
            '.gitignore': '.brain/\n'})
        self.inst = self.m.Instance(self.root)
        owner = self.m._owners['cmd_init']
        owner._make_node(self.inst, self.root, 'root', 'Oak', 'oak', '', 'here', 'codex', None)
        self.inst.local_of('root').update(wake='fixture-send', coordinator='fixture-root')
        self.inst.save_local()

    watch = Watcher.watch
    queued_machine_work = Watcher.queued_machine_work

    def file_evidence(self, inst, node):
        # These receipt fixtures have no jobs or generated status facts. Avoid
        # unrelated Git/status display work while retaining the real inbox source.
        return ({'jobs': [], 'worktrees': {}, 'handbacks': {}, 'inbox': self.m.inbox_lines(node)}, '', '')

    def test_failed_episode_publication_keeps_queued_wake_for_next_tick(self):
        status = self.root / 'status.md'
        status.write_text(status.read_text() + '\n## Blocked\n- Waiting on operator; '
                          'since: 2020-01-01T00:00Z; recommendation: approve; default: wait\n')
        root = self.inst.node('root')
        directory = self.m.wake_directory(self.inst, root)
        publish = self.m.machine_publish
        def failed_episode(inst, path, text):
            if path.name == 'due-episodes.json':
                raise OSError('fabricated episode publication failure')
            return publish(inst, path, text)
        with mock.patch.object(self.m, 'machine_publish', side_effect=failed_episode), \
                mock.patch.object(self.m, 'changes_due', return_value=[]), \
                mock.patch.object(self.m, 'wake_start'), mock.patch.object(self.m, 'WAKE_INTERVAL', 0), \
                mock.patch.object(self.m, 'wake_send', side_effect=[False, True]) as send:
            self.watch()
            pending = list(directory.glob('*.pending'))
            self.assertEqual(len(pending), 1)
            self.m.wake_drain(self.inst, root, once=True)
            self.assertTrue(pending[0].exists(), 'failed delivery must retain the accepted wake')
            self.assertEqual(send.call_count, 1)
            self.watch()
            self.assertTrue(pending[0].exists(), 'the next scan must not retire unknown episode work')
            self.m.wake_drain(self.inst, root, once=True)
            self.assertEqual(send.call_count, 2)
            self.assertFalse(list(directory.glob('*.pending')))
            self.assertTrue(self.m.wake_history(directory)[-1]['items'])

    def test_unknown_episode_state_preserves_pending_open_blocker(self):
        status = self.root / 'status.md'
        status.write_text(status.read_text() + '\n## Blocked\n- Waiting on operator; '
                          'since: 2020-01-01T00:00Z; recommendation: approve; default: wait\n')
        root = self.inst.node('root')
        directory = self.m.wake_directory(self.inst, root)
        episodes = self.root / '.brain/wake/due-episodes.json'
        with mock.patch.object(self.m, 'load', return_value=self.inst), \
                mock.patch.object(self.m, 'due_evidence', side_effect=self.file_evidence), \
                mock.patch.object(self.m, 'changes_due', return_value=[]), \
                mock.patch.object(self.m, 'core_update'), mock.patch.object(self.m, 'auto_upgrade', return_value=None), \
                mock.patch.object(self.m, 'wake_start'), mock.patch.object(self.m, 'WAKE_INTERVAL', 0), \
                mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            for fault in ('corrupt', 'unreadable'):
                with self.subTest(fault=fault):
                    episodes.unlink(missing_ok=True)
                    self.watch()
                    pending = list(directory.glob('*.pending'))
                    self.assertEqual(len(pending), 1)
                    healthy = episodes.read_text()
                    original = self.m.watch_read
                    def read(path, default):
                        if path == episodes and fault == 'unreadable':
                            raise OSError('fabricated episode read failure')
                        return original(path, default)
                    if fault == 'corrupt':
                        episodes.write_text('{broken')
                    with mock.patch.object(self.m, 'watch_read', side_effect=read):
                        with contextlib.redirect_stdout(io.StringIO()) as output:
                            self.m.cmd_due(argparse.Namespace(id='root', quiet=False, json=True))
                        self.assertIn('blocked-overdue', output.getvalue())
                        self.assertTrue(pending[0].exists(), 'filtered due must retain a still-open blocker')
                        self.m.wake_drain(self.inst, root, once=True)
                    self.assertEqual(send.call_count, 1 if fault == 'corrupt' else 2)
                    self.assertEqual(episodes.read_text(), '{broken' if fault == 'corrupt' else healthy)
                    self.assertFalse(list(directory.glob('*.pending')))

    def test_corrupt_wake_receipts_preserve_evidence_and_wake_new_work(self):
        root = self.inst.node('root')
        inbox = self.root / 'inbox.md'
        with mock.patch.object(self.m, 'due_evidence', side_effect=self.file_evidence), \
                mock.patch.object(self.m, 'changes_due', return_value=[]), \
                mock.patch.object(self.m, 'core_update'), mock.patch.object(self.m, 'auto_upgrade', return_value=None), \
                mock.patch.object(self.m, 'load', return_value=self.inst), \
                mock.patch.object(self.m, 'wake_start'), mock.patch.object(self.m, 'WAKE_INTERVAL', 0), \
                mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            for name in ('wake/due-seen.json', 'wake/due-episodes.json', 'watch.json'):
                path = self.root / '.brain' / name
                path.parent.mkdir(exist_ok=True)
                for damaged in ('{broken', '{"version":1,"seen":"invalid","open":[]}'):
                    with self.subTest(name=name, damaged=damaged):
                        path.write_text(damaged)
                        before = send.call_count
                        inbox.write_text('- 2020-01-01 00:00 +00:00 from runtime: Host question: '
                                         'new decision ' + str(before) + ' <!-- host-question:new -->\n')
                        with contextlib.redirect_stdout(io.StringIO()) as output:
                            self.m.cmd_due(argparse.Namespace(id='root', quiet=False, json=True))
                        self.assertIn('overdue-question', output.getvalue())
                        self.m.wake_drain(self.inst, root, once=True)
                        self.assertEqual(send.call_count, before + 1)
                        inbox.write_text('- another new inbox item ' + str(before) + '\n')
                        self.watch(kinds=EXPLICIT_DELIVERY_KINDS)
                        self.m.wake_drain(self.inst, root, once=True)
                        self.assertEqual(send.call_count, before + 2)
                        self.assertEqual(path.read_text(), damaged)
            original_read = self.m.watch_read
            def unreadable(path, default):
                if path.name in ('watch.json', 'due-seen.json', 'due-episodes.json'):
                    raise OSError('fabricated receipt I/O failure')
                return original_read(path, default)
            with mock.patch.object(self.m, 'watch_read', side_effect=unreadable):
                before = send.call_count
                inbox.write_text('- 2020-01-01 00:00 +00:00 from runtime: Host question: '
                                 'new unreadable decision <!-- host-question:unreadable -->\n')
                with contextlib.redirect_stdout(io.StringIO()):
                    self.m.cmd_due(argparse.Namespace(id='root', quiet=False, json=True))
                self.m.wake_drain(self.inst, root, once=True)
                self.assertEqual(send.call_count, before + 1)
                inbox.write_text('- new inbox work with unreadable receipts\n')
                self.watch(kinds=EXPLICIT_DELIVERY_KINDS)
                self.m.wake_drain(self.inst, root, once=True)
                self.assertEqual(send.call_count, before + 2)

            before = send.call_count
            repeated = '- 2020-01-01 00:00 +00:00 from runtime: Host question: '
            repeated += 'reopened while receipts are corrupt <!-- host-question:reopen -->\n'
            for text, expected in ((repeated, before + 1), ('', before + 1), (repeated, before + 2)):
                inbox.write_text(text)
                with contextlib.redirect_stdout(io.StringIO()):
                    self.m.cmd_due(argparse.Namespace(id='root', quiet=False, json=True))
                self.m.wake_drain(self.inst, root, once=True)
                self.assertEqual(send.call_count, expected)

    def test_closed_episode_acknowledgements_are_pruned_and_pending_work_retained(self):
        root = self.inst.node('root')
        inbox = self.root / 'inbox.md'
        line = '- 2020-01-01 00:00 +00:00 from runtime: Host question: choose blue <!-- host-question:blue -->\n'
        with mock.patch.object(self.m, 'due_evidence', side_effect=self.file_evidence), \
                mock.patch.object(self.m, 'changes_due', return_value=[]), \
                mock.patch.object(self.m, 'core_update'), mock.patch.object(self.m, 'auto_upgrade', return_value=None), \
                mock.patch.object(self.m, 'load', return_value=self.inst), \
                mock.patch.object(self.m, 'wake_start'), mock.patch.object(self.m, 'WAKE_INTERVAL', 0), \
                mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            for index in range(8):
                for text in (line, ''):
                    inbox.write_text(text)
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.m.cmd_due(argparse.Namespace(id='root', quiet=False, json=True))
                    self.m.wake_drain(self.inst, root, once=True)
                self.assertEqual(send.call_count, index + 1)
                self.assertEqual(json.loads((self.root / '.brain/wake/due-seen.json').read_text())['seen'], [])
            self.watch()
            self.assertEqual(json.loads((self.root / '.brain/watch.json').read_text())['seen'], [])
            inbox.write_text('- a queued decision <!-- host-question:queued -->\n')
            self.watch()  # durable pending inbox work remains protected after its source clears
            pending_ids = set(json.loads((self.root / '.brain/watch.json').read_text())['seen'])
            self.assertTrue(pending_ids)
            inbox.write_text('')
            self.watch()
            self.assertEqual(set(json.loads((self.root / '.brain/watch.json').read_text())['seen']), pending_ids)
            self.m.wake_drain(self.inst, root, once=True)
            self.watch()
            self.assertEqual(json.loads((self.root / '.brain/watch.json').read_text())['seen'], [])

    def test_due_escalation_survives_transport_expiry_with_operator_block_and_hold(self):
        self.queued_machine_work()
        root = self.inst.node('root')
        child = self.root / 'nodes/sample'
        self.m._owners['cmd_init']._make_node(self.inst, child, 'sample', 'Sample', 'oak', '', 'here', 'codex', [])
        status = self.root / 'status.md'
        status.write_text(status.read_text() + '\n## Blocked\n- Waiting on the operator to approve deployment; '
                          'since: 2020-01-01T00:00Z; recommendation: approve; default: wait\n')
        (self.root / 'inbox.md').write_text('- 2020-01-01 00:00 +00:00 from runtime: '
                                          'Host question: approve deployment <!-- host-question:deploy -->\n')
        reading = {**json.loads(self.env['BRAIN_MACHINE_READING']), 'load_per_cpu': [1.5] * 3}
        outputs = []
        directory = self.m.wake_directory(self.inst, root)
        with mock.patch.object(self.m, 'machine_reading', return_value=reading), \
                mock.patch.object(self.m, 'changes_due', return_value=[]), \
                mock.patch.object(self.m, 'core_update'), mock.patch.object(self.m, 'auto_upgrade', return_value=None), \
                mock.patch.object(self.m, 'load', return_value=self.inst), \
                mock.patch.object(self.m, 'wake_start'), mock.patch.object(self.m, 'WAKE_INTERVAL', 0), \
                mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            for index in range(6):
                for sent in directory.glob('*.sent'):
                    old = time.time() - self.m.WAKE_DEDUP_SECONDS - 1
                    os.utime(sent, (old, old))
                if index:
                    (child / 'backlog.md').write_text('Child activity fixture ' + str(index) + '\n')
                    self.git('add', 'nodes/sample/backlog.md', cwd=self.root)
                    self.git('commit', '-qm', 'activity fixture', cwd=self.root)
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.m.cmd_due(argparse.Namespace(id='root', quiet=False, json=True, machine=True))
                outputs.append(json.loads(output.getvalue()))
                self.watch()
                self.m.wake_drain(self.inst, root, once=True)
            self.assertTrue(all(any(i['kind'] == 'blocked-overdue' for i in out) for out in outputs))
            self.assertTrue(all(any(i['kind'] == 'machine' for i in out) for out in outputs))
            self.assertEqual(send.call_count, 1, 'six unchanged ticks must deliver only one wake')

    def test_due_escalation_changed_and_reopened_question_wakes_again(self):
        inbox = self.root / 'inbox.md'
        original = '- 2020-01-01 00:00 +00:00 from runtime: Host question: choose blue <!-- host-question:blue -->\n'
        inbox.write_text(original)
        root = self.inst.node('root')
        with mock.patch.object(self.m, 'changes_due', return_value=[]), \
                mock.patch.object(self.m, 'core_update'), mock.patch.object(self.m, 'auto_upgrade', return_value=None), \
                mock.patch.object(self.m, 'load', return_value=self.inst), \
                mock.patch.object(self.m, 'wake_start'), mock.patch.object(self.m, 'WAKE_INTERVAL', 0), \
                mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            for text, expected in [(original, 1), (original.replace('2020-01-01', '2020-01-02'), 1),
                                   (original, 1), ('', 1), (original, 2),
                                   (original.replace('choose blue', 'choose green'), 3)]:
                inbox.write_text(text)
                with contextlib.redirect_stdout(io.StringIO()):
                    self.m.cmd_due(argparse.Namespace(id='root', quiet=False, json=True))
                self.m.wake_drain(self.inst, root, once=True)
                self.assertEqual(send.call_count, expected)

    def test_delivered_wake_reason_names_opaque_item_and_cause(self):
        (self.root / 'inbox.md').write_text('- confidential fixture decision\n')
        root = self.inst.node('root')
        with mock.patch.object(self.m, 'changes_due', return_value=[]), \
                mock.patch.object(self.m, 'wake_start'), mock.patch.object(self.m, 'WAKE_INTERVAL', 0), \
                mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            self.watch(kinds=EXPLICIT_DELIVERY_KINDS)
            self.m.wake_drain(self.inst, root, once=True)
            reason = send.call_args.kwargs['reason']
            item = self.m.wake_history(self.m.wake_directory(self.inst, root))[-1]['items'][0]
            self.assertIn(item['id'], reason)
            self.assertIn('new', reason)
            self.assertNotIn('confidential', reason)

    def test_due_escalation_receipt_is_an_exact_rehearsal_write(self):
        (self.root / 'inbox.md').write_text('- 2020-01-01 00:00 +00:00 from runtime: Host question: choose blue '
                                          '<!-- host-question:blue -->\n')
        with mock.patch.object(self.m, 'load', return_value=self.inst), \
                mock.patch.object(self.m, 'changes_due', return_value=[]), \
                mock.patch.object(self.m, 'core_update'), mock.patch.object(self.m, 'auto_upgrade', return_value=None), \
                mock.patch.object(self.m, 'wake_start'), contextlib.redirect_stdout(io.StringIO()):
            self.m.cmd_due(argparse.Namespace(id='root', quiet=False, json=True))
        self.assertTrue((self.root / '.brain/wake/due-seen.json').is_file())
        self.assertFalse((self.root / '.brain/watch.json').exists())
        self.assertTrue(self.m.allowed_write('brain/.brain/wake/due-seen.json', 'read'))
        self.assertFalse(self.m.allowed_write('brain/.brain/other-watch.json', 'read'))

    def test_old_escalation_acknowledgement_migrates_without_replaying_backlog(self):
        inbox = self.root / 'inbox.md'
        inbox.write_text('- 2020-01-01 00:00 +00:00 from runtime: Host question: choose blue '
                         '<!-- host-question:blue -->\n')
        with mock.patch.object(self.m, 'changes_due', return_value=[]), \
                mock.patch.object(self.m, 'wake_start'), mock.patch.object(self.m, 'WAKE_INTERVAL', 0), \
                mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            key = self.m.watch_digest(['overdue', 'root', inbox.read_text().strip()])
            (self.root / '.brain/watch.json').write_text(json.dumps({'version': 1, 'seen': [key]}))
            (self.root / '.brain/wake').mkdir(exist_ok=True)
            (self.root / '.brain/wake/due-episodes.json').write_text(json.dumps({'version': 1, 'open': {}}))
            self.watch()
            root = self.inst.node('root')
            self.m.wake_drain(self.inst, root, once=True)
            self.assertFalse(any(i['kind'] == 'overdue-question'
                                 for i in self.m.wake_history(self.m.wake_directory(self.inst, root))[-1]['items']))
            first = send.call_count  # the independent inbox item may still be new
            original = inbox.read_text()
            inbox.write_text('')
            self.watch()
            inbox.write_text(original)
            self.watch()
            self.m.wake_drain(self.inst, root, once=True)
            self.assertEqual(send.call_count, first + 1)

    def test_question_crossing_escalation_tier_keeps_new_item_wakes(self):
        inbox = self.root / 'inbox.md'
        at = self.inst.now_dt()
        inbox.write_text('- ' + at.isoformat() + ' from runtime: Host question: choose blue '
                         '<!-- host-question:blue -->\n')
        root = self.inst.node('root')
        with mock.patch.object(self.m, 'changes_due', return_value=[]), \
                mock.patch.object(self.m, 'core_update'), mock.patch.object(self.m, 'auto_upgrade', return_value=None), \
                mock.patch.object(self.m, 'load', return_value=self.inst), \
                mock.patch.object(self.m, 'wake_start'), mock.patch.object(self.m, 'WAKE_INTERVAL', 0), \
                mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            self.watch(kinds=EXPLICIT_DELIVERY_KINDS)
            self.m.wake_drain(self.inst, root, once=True)
            self.assertEqual(send.call_count, 1)
            with mock.patch.object(self.m.Instance, 'now_dt', return_value=at + datetime.timedelta(minutes=11)):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.m.cmd_due(argparse.Namespace(id='root', quiet=False, json=True))
                self.m.wake_drain(self.inst, root, once=True)
                self.assertEqual(send.call_count, 2)
                self.assertIn('escalation tier item ', send.call_args.kwargs['reason'])
                inbox.write_text(inbox.read_text() + '- a genuinely new decision\n')
                self.watch(kinds=EXPLICIT_DELIVERY_KINDS)
                self.m.wake_drain(self.inst, root, once=True)
                self.assertEqual(send.call_count, 3)


class WakeVisibility(Scenario):
    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, self.env)
        env.start()
        self.addCleanup(env.stop)
        self.m = core_loader.SourceFileLoader('visible_fixture', str(CORE / 'bin/brain')).load_module()
        self.root = self.init('alice')
        for nid in ('app', 'app/leaf'):
            self.b(self.root, 'new', nid, '--title', 'Example')
        self.b(self.root, 'set', 'app', 'coordinator=fixture-app', 'wake=fixture-send', 'wake_policy=visible')
        self.b(self.root, 'set', 'root', 'coordinator=fixture-root', 'wake=fixture-send', 'wake_policy=questions')
        self.inst = self.m.Instance(self.root)

    def test_visible_coordinator_union_includes_unbound_descendant_inbox(self):
        m = self.m
        leaf = self.inst.node('app/leaf')
        (leaf['dir'] / 'inbox.md').write_text('- Select a fixture size\n')
        app = self.inst.node('app')
        items = m.coordinator_due_items(self.inst, [app])
        self.assertTrue(any(i['node'] == leaf['id'] and 'fixture size' in i['text'] for i in items))
        groups = m.watch_groups(self.inst)
        self.assertTrue(any(i['node'] == leaf['id'] and 'fixture size' in i['text'] for i in groups['app']['items']))
        self.assertFalse(any('fixture size' in i['text'] for i in groups['root']['items']))

    def test_root_question_policy_drops_known_results_but_preserves_question(self):
        m, root = self.m, self.inst.node('root')
        with mock.patch.object(m, 'wake_start'), mock.patch.object(m, 'WAKE_INTERVAL', 0), \
                mock.patch.object(m, 'wake_send', return_value=True) as send:
            for i, state in enumerate(('done', 'blocked'), 1):
                m.request_wake(self.inst, root, 'fixture-result-' + state,
                               items=[{'id': format(i, '064x'), 'kind': 'handback', 'state': state}])
                m.wake_drain(self.inst, root, once=True)
            self.assertEqual(send.call_count, 0)
            m.request_wake(self.inst, root, 'fixture-question',
                           items=[{'id': '3' * 64, 'kind': 'handback', 'state': 'question'}])
            m.wake_drain(self.inst, root, once=True)
            self.assertEqual(send.call_count, 1)

    def test_visible_queue_matches_due_and_fresh_inbox_union(self):
        m = self.m
        self.inst.local_of('root')['wake_policy'] = 'visible'
        self.inst.save_local()
        for nid in ('root', 'app', 'app/leaf'):
            (self.inst.node(nid)['dir'] / 'inbox.md').write_text('- Choose fixture ' + nid + '\n')
        def key(i):
            text = i['text'].removeprefix('inbox: ')
            return i['node'], i['kind'], ' '.join(text.split())
        with mock.patch.object(m, 'changes_due', return_value=[]):
            for child_bound in (False, True):
                self.inst.local_of('app/leaf')['coordinator'] = 'fixture-leaf' if child_bound else ''
                if child_bound:
                    self.inst.local_of('app/leaf')['wake_policy'] = 'visible'
                self.inst.save_local()
                groups = m.watch_groups(self.inst)
                for owner in ('root', 'app') + (('app/leaf',) if child_bound else ()):
                    n = self.inst.node(owner)
                    visible = {key(i) for i in m.coordinator_due_items(self.inst, [n])}
                    subtree = [x for x in self.inst.nodes() if owner == 'root' or x['id'] == owner
                               or x['id'].startswith(owner + '/')]
                    inbox, _ = m.inbox_items(self.inst, subtree, owner, readonly=True)
                    visible |= {key(i) for i in inbox if not i['reported']}
                    self.assertEqual({key(i) for i in groups[owner]['items']}, visible)
