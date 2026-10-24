"""Episode lifecycle proofs, using fabricated nodes and a controlled instance clock."""
import argparse
import contextlib
import datetime
import io
import json
import os
from unittest import mock

import core_loader
from scenario import CORE, Scenario


class DueLifecycle(Scenario):
    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, self.env)
        env.start()
        self.addCleanup(env.stop)
        self.m = core_loader.SourceFileLoader('lifecycle_fixture', str(CORE / 'bin/brain')).load_module()
        self.root = self.init('oak')
        self.inst = self.m.Instance(self.root)
        self.inst.local_of('root')
        self.n = self.inst.node('root')
        self.now = datetime.datetime(2026, 1, 15, 12, tzinfo=datetime.timezone.utc)
        clock = mock.patch.object(self.m.Instance, 'now_dt', side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def request(self, text='Choose a color?', stamp='2026-01-07 12:00 +00:00'):
        line = '- ' + stamp + ' from worker: ' + text + '\n'
        with (self.root / 'inbox.md').open('a') as f:
            f.write(line)
        return line

    def requests(self):
        return [i for i in self.m.inbox_items(self.inst)[0] if i['kind'] == 'inbox']

    def test_m8_answer_retains_source_and_recurrent_request(self):
        line = self.request()
        item = self.requests()[0]
        self.assertEqual(item['age_days'], 8)
        saved = {p.name: p.read_bytes() for p in (self.root / '.brain').iterdir() if p.is_file()}
        with mock.patch.object(self.m, 'machine_episode_due', side_effect=AssertionError('machine scan')), \
                mock.patch.object(self.m, 'pending_imports', side_effect=AssertionError('import scan')), \
                mock.patch.object(self.m, 'runaway_jobs', side_effect=AssertionError('job scan')):
            request_projection = self.m.inbox_items(self.inst, [self.n], readonly=True, requests_only=True)[0]
        self.assertEqual([i['id'] for i in request_projection], [item['id']])
        with mock.patch.object(self.m, 'load', return_value=self.inst), contextlib.redirect_stdout(io.StringIO()) as output:
            self.m.cmd_inbox(argparse.Namespace(id='root', new=False, json=True, weekly=True))
        self.assertEqual([i['id'] for i in json.loads(output.getvalue())], [item['id']])
        self.assertEqual({p.name: p.read_bytes() for p in (self.root / '.brain').iterdir() if p.is_file()}, saved)
        self.now -= datetime.timedelta(days=2)
        with mock.patch.object(self.m, 'load', return_value=self.inst), contextlib.redirect_stdout(io.StringIO()) as output:
            self.m.cmd_inbox(argparse.Namespace(id='root', new=False, json=True, weekly=True))
        self.assertEqual(json.loads(output.getvalue()), [])
        self.now += datetime.timedelta(days=2)
        with mock.patch.object(self.m, 'load', return_value=self.inst), contextlib.redirect_stdout(io.StringIO()):
            self.m.cmd_inbox(argparse.Namespace(id='root', new=True, json=True, weekly=False))
        self.assertEqual(self.requests()[0]['id'], item['id'])  # reporting is not answering
        state = (self.root / '.brain/lifecycle.json').read_bytes()
        with mock.patch.object(self.m, 'inbox_items', return_value=([
                {**item, 'id': 'abcdef111111111111111111'}, {**item, 'id': 'abcdef222222222222222222'}], {})):
            with self.assertRaises(SystemExit):
                self.m.answer_inbox(self.inst, 'abcdef', 'Ambiguous answer')
        self.assertEqual((self.root / '.brain/lifecycle.json').read_bytes(), state)
        before = (self.root / 'inbox.md').read_text()
        self.m.answer_inbox(self.inst, item['id'][:8], 'Blue; this is recorded text.')
        self.assertEqual((self.root / 'inbox.md').read_text(), before)
        self.assertEqual(self.requests(), [])
        records = json.loads((self.root / '.brain/lifecycle.json').read_text())['answers']
        self.assertEqual(records[-1]['answer'], 'Blue; this is recorded text.')
        self.assertIn(line.strip()[2:], records[-1]['source'])
        (self.root / 'inbox.md').write_text('')
        self.requests()  # observe resolution before an identical recurrence
        self.request()
        recurrent = self.requests()[0]
        self.assertNotEqual(item['id'], recurrent['id'])
        with self.assertRaises(SystemExit):
            self.m.answer_inbox(self.inst, item['id'], 'Stale answer')
        with self.assertRaises(SystemExit):
            self.m.answer_inbox(self.inst, 'ffffff', 'Unknown answer')

    def test_p4_acknowledges_only_current_parked_episode(self):
        raw = [{'node': 'root', 'kind': 'handback', 'state': 'blocked',
                'job': 'job-one', 'episode': 'first', 'text': 'Waiting for a fixture'}]
        item = self.m.reminder_items(self.inst, self.n, raw)[0]
        self.m.record_reminder(self.inst, self.n, raw, item['id'], 'ack',
                               'Handled dependency', resume_condition='Fixture becomes available')
        self.assertEqual(self.m.reminder_items(self.inst, self.n, raw), [])
        handled = self.m.reminder_items(self.inst, self.n, raw, include_handled=True)[0]
        self.assertEqual(handled['handling']['resume_condition'], 'Fixture becomes available')
        changed = [{**raw[0], 'episode': 'second'}]
        self.assertEqual(len(self.m.reminder_items(self.inst, self.n, changed)), 1)
        question = [{**raw[0], 'state': 'question', 'episode': 'question'}]
        protected = self.m.reminder_items(self.inst, self.n, question)[0]
        with self.assertRaises(SystemExit):
            self.m.record_reminder(self.inst, self.n, question, protected['id'], 'ack',
                                   'Handled', resume_condition='Later')
        for protected_item in ({**raw[0], 'reasons': ['verify failed']},
                               {**raw[0], 'protected': True},
                               {**raw[0], 'kind': 'restart'}, {**raw[0], 'state': 'failed'}):
            current = self.m.reminder_items(self.inst, self.n, [protected_item])[0]
            before = (self.root / '.brain/lifecycle.json').read_bytes()
            with self.assertRaises(SystemExit):
                self.m.record_reminder(self.inst, self.n, [protected_item], current['id'], 'ack',
                                       'Handled', resume_condition='Later')
            self.assertEqual((self.root / '.brain/lifecycle.json').read_bytes(), before)
        item = self.m.reminder_items(self.inst, self.n, raw)[0]
        self.m.record_reminder(self.inst, self.n, raw, item['id'], 'ack', 'Waiting',
                               until='2026-01-15T13:00:00Z', resume_condition='Dependency ready')
        self.now += datetime.timedelta(hours=1)
        self.assertEqual(len(self.m.reminder_items(self.inst, self.n, raw)), 1)

    def test_p14_deferral_expires_and_changed_need_reappears(self):
        raw = [{'node': 'root', 'kind': 'missing-preview', 'episode': 'merge-one', 'text': 'Preview required'}]
        item = self.m.reminder_items(self.inst, self.n, raw)[0]
        self.m.record_reminder(self.inst, self.n, raw, item['id'], 'defer', 'Preview later',
                               until='2026-01-16T12:00:00Z')
        self.assertEqual(self.m.reminder_items(self.inst, self.n, raw), [])
        self.assertNotIn('preview', self.n['local'])
        self.assertEqual(len(self.m.reminder_items(self.inst, self.n,
                                                 [{**raw[0], 'episode': 'merge-two'}])), 1)
        self.now += datetime.timedelta(days=1)
        self.assertEqual(len(self.m.reminder_items(self.inst, self.n, raw)), 1)
        failure = [{'node': 'root', 'kind': 'invalid-handback', 'text': 'Unreadable evidence'}]
        item = self.m.reminder_items(self.inst, self.n, failure)[0]
        with self.assertRaises(SystemExit):
            self.m.record_reminder(self.inst, self.n, failure, item['id'], 'defer', 'Later',
                                   until='2026-01-17T12:00:00Z')
        self.n['fm']['labels'] = 'kind:code'
        self.n['local']['preview'] = 'none'
        row = {**dict.fromkeys(self.m.JOB_COLS, ''), 'Job': 'job-merged',
               'Started': '2026-01-01T10:00Z', 'Ended': '2026-01-01T11:00Z', 'Outcome': 'merged', 'Settled': 'yes'}
        self.m.write_jobs(self.inst, self.n, [row])
        due = self.m.due_items(self.inst, self.n, include_stale=False, include_advice=False)
        preview = next(i for i in due if i['kind'] == 'missing-preview')
        self.assertTrue(any(i['kind'] == 'missing-preview' for i in self.m.watch_items(self.inst, self.n)))
        self.m.record_reminder(self.inst, self.n, due, preview['id'], 'defer', 'Later',
                               until='2026-01-17T12:00:00Z')
        self.assertEqual(self.n['local']['preview'], 'none')
        self.assertFalse(any(i['kind'] == 'missing-preview' for i in self.m.watch_items(self.inst, self.n)))
        self.now += datetime.timedelta(days=1)
        self.assertTrue(any(i['kind'] == 'missing-preview' for i in self.m.watch_items(self.inst, self.n)))

    def test_p15_freshness_requires_recorded_inputs_and_output(self):
        self.assertIsNone(self.m.freshness_state(self.inst, self.n))
        self.n['fm']['upstream_inputs'] = 'source.txt'
        self.n['fm']['upstream_output'] = 'index.md'
        (self.root / 'source.txt').write_text('input one')
        self.assertEqual(self.m.freshness_state(self.inst, self.n)['state'], 'missing')
        (self.root / 'index.md').write_text('index one')
        self.assertEqual(self.m.freshness_state(self.inst, self.n)['state'], 'stale')
        self.m.record_freshness(self.inst, self.n)
        self.assertEqual(self.m.freshness_state(self.inst, self.n)['state'], 'fresh')
        (self.root / 'index.md').write_text('changed index')
        self.assertEqual(self.m.freshness_state(self.inst, self.n)['state'], 'stale')
        self.m.record_freshness(self.inst, self.n)
        (self.root / 'source.txt').write_text('input two')
        self.assertEqual(self.m.freshness_state(self.inst, self.n)['state'], 'stale')
        (self.root / 'source.txt').unlink()
        self.assertEqual(self.m.freshness_state(self.inst, self.n)['state'], 'unknown')
        self.n['fm']['upstream_inputs'] = '../outside.txt'
        self.assertEqual(self.m.freshness_state(self.inst, self.n)['state'], 'unknown')
        outside = self.tmp / 'outside.txt'
        outside.write_text('outside')
        (self.root / 'source.txt').symlink_to(outside)
        self.n['fm']['upstream_inputs'] = 'source.txt'
        self.assertEqual(self.m.freshness_state(self.inst, self.n)['state'], 'unknown')
        before = (self.root / '.brain/lifecycle.json').read_bytes()
        with self.assertRaises(SystemExit):
            self.m.record_freshness(self.inst, self.n)
        self.assertEqual((self.root / '.brain/lifecycle.json').read_bytes(), before)

    def test_m12_wake_setup_targets_bound_coordinator_without_capability(self):
        self.inst.cfg['host'] = 't3'
        self.n['local']['coordinator'] = 'fixture-thread'
        self.n['local']['wake'] = 'https://example.invalid/private-capability'
        plan = self.m.wake_setup_plan(self.inst, self.n)
        self.assertEqual(plan['coordinator'], 'fixture-thread')
        self.assertEqual(plan['host_call']['schedule'], {'type': 'webhook'})
        self.assertTrue(plan['host_call']['bindToCurrentThread'])
        self.assertNotIn('threadId', plan['host_call'])
        self.assertNotIn('private-capability', json.dumps(plan))
        self.assertIn('fixture-thread', plan['handoff'])
        self.n['local']['coordinator'] = 'replacement-thread'
        self.assertEqual(self.m.wake_setup_plan(self.inst, self.n)['coordinator'], 'replacement-thread')
        with mock.patch.object(self.m, 'load', return_value=self.inst), \
                mock.patch.object(self.inst, 'save_local') as save, \
                mock.patch.object(self.m.subprocess, 'Popen') as launch, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.m.cmd_machine(argparse.Namespace(action='wake-setup', id='root'))
        self.assertEqual(json.loads(output.getvalue())['coordinator'], 'replacement-thread')
        save.assert_not_called()
        launch.assert_not_called()
        self.n['local'].pop('coordinator')
        self.assertIn('gap', self.m.wake_setup_plan(self.inst, self.n))
        self.inst.cfg['host'] = 'unsupported'
        self.assertIn('gap', self.m.wake_setup_plan(self.inst, self.n))

    def test_r1_usage_restart_is_stable_and_rebrief_baseline_resets(self):
        row = {**dict.fromkeys(self.m.JOB_COLS, ''), 'Job': 'job-fixture',
               'Started': '2026-01-15T09:00:00Z'}
        usage = {'compactions': 3, 'processed': 150, 'sessions': 1}
        with mock.patch.object(self.m, 'live_usage', side_effect=lambda *a: dict(usage)), \
                mock.patch.object(self.m, 'job_budget', return_value={'tokens': 100}):
            first = self.m.usage_restart_item(self.inst, self.n, row)
            self.assertIn('restart from brief', first['text'])
            self.assertIn('3 compactions', first['text'])
            self.assertIn('150', first['text'])
            usage.update(compactions=4, processed=210)
            self.assertEqual(first['episode'], self.m.usage_restart_item(self.inst, self.n, row)['episode'])
            self.m.acknowledge_usage_restart(self.inst, self.n, row)
            self.assertIsNone(self.m.usage_restart_item(self.inst, self.n, row))
            usage.update(compactions=6, processed=409)
            self.assertIsNone(self.m.usage_restart_item(self.inst, self.n, row))
            usage['processed'] = 410
            again = self.m.usage_restart_item(self.inst, self.n, row)
            self.assertNotEqual(first['episode'], again['episode'])
            evidence = ({'jobs': [row], 'worktrees': {}, 'handbacks': {self.m.job_key(row): None}, 'inbox': []}, '', '')
            due = self.m.due_items(self.inst, self.n, evidence=evidence, include_stale=False,
                                   include_ready=False, include_advice=False)
            self.assertEqual(sum(i['kind'] == 'restart' for i in due), 1)
            self.assertEqual(next(i for i in due if i['kind'] == 'restart')['episode'], again['episode'])
            self.assertEqual(row['Ended'], '')
            self.assertIsNone(self.m.usage_restart_item(self.inst, self.n, {**row, 'Ended': '2026-01-15T11:00Z'}))
