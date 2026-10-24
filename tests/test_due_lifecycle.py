"""Episode lifecycle proofs, using fabricated nodes and a controlled instance clock."""
import argparse
import contextlib
import datetime
import io
import json
import os
import sys
from unittest import mock

import core_loader
from scenario import CORE, Scenario


class DueLifecycle(Scenario):
    def setUp(self):
        super().setUp()
        self.env['BRAIN_NO_UPDATE_CHECK'] = '1'
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

    def identity_row(self, **fields):
        return {**dict.fromkeys(self.m.JOB_COLS, ''), 'Job': 'job/palette',
                'Started': '2026-01-15T08:00Z', **fields}

    def ledger_need(self, row, kind):
        self.m.write_jobs(self.inst, self.n, [row])
        self.n['local']['worktrees'] = {row['Job']: {'path': str(self.root)}} if not row['Ended'] else {}
        with mock.patch.object(self.m, 'local_job_state', return_value={'exists': True, 'merged': True}), \
                mock.patch.object(self.m, 'handback_detail', return_value=self.detail), \
                mock.patch.object(self.m, 'changes_due', return_value=[]):
            due = next(i for i in self.m.due_items(self.inst, self.n, include_advice=False) if i['kind'] == kind)
            watch = next(i for i in self.m.watch_items(self.inst, self.n) if i['kind'] == kind)
        return due['id'], watch['id']

    def test_merged_ledger_need_ignores_handback_display(self):
        row = self.identity_row()
        self.detail = 'summary: drew 12 circles; lessons: compare colors; report: reports/first.md'
        first = self.ledger_need(row, 'merged-job')
        self.detail = 'summary: drew 24 squares; lessons: compare shapes; report: reports/second.md'
        self.assertEqual(self.ledger_need(row, 'merged-job'), first)
        self.assertNotEqual(self.ledger_need(self.identity_row(Started='2026-01-15T09:00Z'), 'merged-job'), first)

    def test_lesson_outcome_ignores_handback_display(self):
        row = self.identity_row(Ended='2026-01-15T10:00Z', Outcome='merged', **{'Lesson check': 'required'})
        self.detail = 'summary one; lessons one; report: reports/first.md'
        first = self.ledger_need(row, 'lesson-outcome')
        self.detail = 'summary two; lessons two; report: reports/second.md'
        self.assertEqual(self.ledger_need(row, 'lesson-outcome'), first)
        self.assertNotEqual(self.ledger_need({**row, 'Job': 'job/brush'}, 'lesson-outcome'), first)
        self.m.write_jobs(self.inst, self.n, [{**row, 'Lessons': 'none: routine change'}])
        self.assertNotIn('lesson-outcome', [i['kind'] for i in self.m.due_items(self.inst, self.n)])

    def advice_setup(self):
        self.row = self.identity_row()
        self.m.write_jobs(self.inst, self.n, [self.row])
        self.stats = {'open': [{**self.row, 'age_h': 4}], 'median_hours': 1, 'median_tokens': 10}
        self.usage = {'processed': 30, 'compactions': 3, 'subagents': 1, 'sub_share': .6}
        for patch in (mock.patch.object(self.m, 'jobs_summary', side_effect=lambda *a: self.stats),
                      mock.patch.object(self.m, 'live_usage', side_effect=lambda *a: self.usage),
                      mock.patch.object(self.m, 'job_budget', return_value={'hours': 1, 'tokens': 10})):
            patch.start()
            self.addCleanup(patch.stop)

    def advice(self):
        return [i for i in self.m.due_items(self.inst, self.n, include_stale=False) if i['kind'] == 'job']

    def test_hourly_aging_never_rekeys_or_wakes(self):
        self.advice_setup()
        self.inst.local_of('root').update(coordinator='fixture-root', wake='fixture-send')
        self.inst.save_local()
        (self.root / 'inbox.md').write_text('- Choose a fabricated paint color\n')
        first_ids, displays = None, set()
        with mock.patch.object(self.m, 'changes_due', return_value=[]), \
                mock.patch.object(self.m, 'wake_start'), mock.patch.object(self.m, 'WAKE_INTERVAL', 0), \
                mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            for hour in range(6):
                self.now += datetime.timedelta(hours=1)
                self.stats['open'][0]['age_h'] += 1
                self.stats['median_hours'] += .1
                self.stats['median_tokens'] += 1
                self.usage['processed'] += 5
                self.usage['compactions'] += 1
                advice = self.advice()
                identities = {i['id'] for i in advice}
                if first_ids is None:
                    first_ids = identities
                self.assertEqual(identities, first_ids)
                displays.add(tuple(i['text'] for i in advice))
                with contextlib.redirect_stdout(io.StringIO()):
                    self.m.watch_once(self.inst)
                self.m.wake_drain(self.inst, self.n, once=True)
                self.assertEqual(send.call_count, 1)
            self.assertEqual(len(first_ids), 6)
            self.assertGreater(len(displays), 1)
            history = self.m.wake_history(self.m.wake_directory(self.inst, self.n))
            self.assertTrue(history)
            self.assertFalse(any(i['kind'] in ('job', 'stale-status') for h in history for i in h['items']))

    def test_advice_latches_threshold_dips_and_distinguishes_conditions_jobs_and_rebrief(self):
        self.advice_setup()
        first = {i['condition']: i['id'] for i in self.advice()}
        self.assertEqual(set(first), {'median-age', 'median-tokens', 'budget-hours', 'budget-tokens',
                                      'compaction-limit', 'subagent-share'})
        self.stats.update(median_hours=100, median_tokens=1000)
        self.usage['sub_share'] = .1
        dipped = {i['condition']: i['id'] for i in self.advice()}
        self.assertEqual(dipped, first)
        self.m.acknowledge_usage_restart(self.inst, self.n, self.row)
        after = {i['condition']: i['id'] for i in self.advice()}
        self.assertEqual(set(after), {'budget-hours', 'budget-tokens', 'compaction-limit'})
        self.assertTrue(set(after.values()).isdisjoint(first.values()))
        self.stats['open'][0]['Job'] = 'job/brush'
        self.m.write_jobs(self.inst, self.n, [{**self.row, 'Job': 'job/brush'}])
        new = {i['id'] for i in self.advice()}
        self.assertTrue(new.isdisjoint(after.values()))

    def test_advice_first_crossing_and_state_change(self):
        self.advice_setup()
        self.stats.update(median_hours=100, median_tokens=1000)
        self.stats['open'][0]['age_h'] = .1
        self.usage.update(processed=0, compactions=0, subagents=0, sub_share=0)
        self.assertEqual(self.advice(), [])
        self.usage['processed'] = 11
        first = self.advice()
        self.assertEqual([i['condition'] for i in first], ['budget-tokens'])
        self.usage['compactions'] = 3
        second = self.advice()
        self.assertEqual(len(second), 2)
        self.assertIn(first[0]['id'], {i['id'] for i in second})
        lifecycle = self.root / '.brain/wake/lifecycle.json'
        before = lifecycle.read_bytes()
        projection = self.m.runaway_jobs(self.inst, self.n, structured=True, readonly=True)
        self.assertEqual({self.m.reminder_key(i) for i in projection}, {i['id'] for i in second})
        self.assertEqual(lifecycle.read_bytes(), before)
        with mock.patch.object(self.m, 'job_signal', return_value={'state': 'blocked'}):
            changed = self.advice()
        self.assertTrue({i['id'] for i in changed}.isdisjoint(i['id'] for i in second))

    def test_docker_report_only_inbox_never_wakes(self):
        self.inst.local_of('root').update(coordinator='fixture-root', wake='fixture-send')
        self.inst.save_local()
        line = '- Docker leftovers (report only): compose oak-verify-demo; owner: unknown; age: 1900s; state: exited <!-- docker-leftover:' + 'a' * 24 + ' -->\n'
        (self.root / 'inbox.md').write_text(line)
        with mock.patch.object(self.m, 'changes_due', return_value=[]), \
                mock.patch.object(self.m, 'wake_start'), mock.patch.object(self.m, 'WAKE_INTERVAL', 0), \
                mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            self.assertFalse(self.m.watch_items(self.inst, self.n))
            with contextlib.redirect_stdout(io.StringIO()):
                self.m.watch_once(self.inst)
            self.m.wake_drain(self.inst, self.n, once=True)
            self.assertEqual(send.call_count, 0)
        self.assertEqual((self.root / 'inbox.md').read_text(), line)

    def request(self, text='Choose a color?', stamp='2026-01-07 12:00 +00:00'):
        line = '- ' + stamp + ' from worker: ' + text + '\n'
        with (self.root / 'inbox.md').open('a') as f:
            f.write(line)
        return line

    def requests(self):
        return [i for i in self.m.inbox_items(self.inst)[0] if i['kind'] == 'inbox']

    def test_fix1_stale_answer_refuses_unscanned_recurrence(self):
        line = self.request()
        old = self.requests()[0]['id']
        self.m.answer_inbox(self.inst, old, 'First answer')
        (self.root / 'inbox.md').write_text('')
        self.assertEqual(self.requests(), [])  # observe resolution
        (self.root / 'inbox.md').write_text(line)
        receipt = self.root / '.brain/wake/lifecycle.json'
        before = receipt.read_bytes()
        # No normal inbox scan between restoring the source and answering its old ID.
        with self.assertRaises(SystemExit):
            self.m.answer_inbox(self.inst, old, 'Obsolete answer')
        self.assertEqual(receipt.read_bytes(), before)
        with self.assertRaises(SystemExit):
            self.m.record_answer(self.inst, self.n, line.rstrip('\n'), old, 'Obsolete direct answer')
        self.assertEqual(receipt.read_bytes(), before)
        self.assertEqual((self.root / 'inbox.md').read_text(), line)
        current = self.m.inbox_items(self.inst, readonly=True, requests_only=True)[0]
        current = next(i['id'] for i in current if i['kind'] == 'inbox')
        self.assertNotEqual(current, old)
        again = self.m.inbox_items(self.inst, readonly=True, requests_only=True)[0]
        self.assertEqual(next(i['id'] for i in again if i['kind'] == 'inbox'), current)
        (self.root / 'inbox.md').write_text('')
        self.assertEqual(self.requests(), [])  # also retire an unanswered recurrence
        (self.root / 'inbox.md').write_text(line)
        with self.assertRaises(SystemExit):
            self.m.answer_inbox(self.inst, current, 'Obsolete unanswered occurrence')
        repeated = self.m.inbox_items(self.inst, readonly=True, requests_only=True)[0]
        repeated = next(i['id'] for i in repeated if i['kind'] == 'inbox')
        self.assertNotIn(repeated, (old, current))
        current = repeated
        self.m.answer_inbox(self.inst, current, 'Current answer from a read-only projection')
        records = json.loads(receipt.read_text())['answers']
        self.assertEqual([r['answer'] for r in records], ['First answer', 'Current answer from a read-only projection'])
        self.assertEqual(self.requests(), [])

    def test_fix1_partial_due_projection_preserves_unchanged_ids(self):
        with mock.patch.object(self.m, 'status_lag', return_value='unchanged stale facts'), \
                mock.patch.object(self.m, 'runaway_jobs', return_value=['unchanged job advice']):
            first = self.m.due_items(self.inst, self.n)
            second = self.m.due_items(self.inst, self.n)
        first = {i['kind']: i['id'] for i in first}
        second = {i['kind']: i['id'] for i in second}
        self.assertIn('stale-status', first)
        self.assertIn('job', first)
        self.assertEqual(first, second)
        receipt = self.root / '.brain/wake/lifecycle.json'
        legacy = json.loads(receipt.read_text())
        for record in legacy['reminders']['root'].values():
            record.pop('kind', None)
        receipt.write_text(json.dumps(legacy))
        with mock.patch.object(self.m, 'status_lag', return_value='unchanged stale facts'), \
                mock.patch.object(self.m, 'runaway_jobs', return_value=['unchanged job advice']):
            upgraded = self.m.due_items(self.inst, self.n)
        self.assertEqual({i['kind']: i['id'] for i in upgraded}, first)
        raw = [{'node': 'root', 'kind': 'stale-status', 'text': 'A transient need'}]
        old = self.m.reminder_items(self.inst, self.n, raw)[0]['id']
        self.m.reminder_items(self.inst, self.n, [])  # a complete empty snapshot retires it
        self.assertNotEqual(self.m.reminder_items(self.inst, self.n, raw)[0]['id'], old)

    def test_fix2_identical_snapshots_share_reminder_ids_and_recurrences_rearm(self):
        raw = [{'node': 'root', 'kind': 'stale-status', 'text': 'The same stale facts'}]
        left, right = self.m.lifecycle_empty(), self.m.lifecycle_empty()
        first = self.m.reminder_reconcile(self.inst, self.n, raw, left)[0]['id']
        self.assertEqual(self.m.reminder_reconcile(self.inst, self.n, raw, right)[0]['id'], first)
        for data in (left, right):
            self.m.reminder_reconcile(self.inst, self.n, [], data)
        second = self.m.reminder_reconcile(self.inst, self.n, raw, left)[0]['id']
        self.assertNotEqual(second, first)
        self.assertEqual(self.m.reminder_reconcile(self.inst, self.n, raw, right)[0]['id'], second)
        self.assertEqual(self.m.reminder_reconcile(self.inst, self.n, raw, left)[0]['id'], second)
        # Existing random IDs remain valid until their episode is actually resolved.
        legacy = self.m.lifecycle_empty()
        key = self.m.reminder_key(raw[0])
        legacy['reminders']['root'] = {key: {'id': 'a' * 24, 'at': self.inst.now()}}
        self.assertEqual(self.m.reminder_reconcile(self.inst, self.n, raw, legacy)[0]['id'], 'a' * 24)
        self.m.reminder_reconcile(self.inst, self.n, [], legacy)
        self.assertNotEqual(self.m.reminder_reconcile(self.inst, self.n, raw, legacy)[0]['id'], 'a' * 24)

    def test_fix2_retired_reminder_bound_preserves_receipts_and_visible_need(self):
        first = [{'node': 'root', 'kind': 'missing-preview', 'text': 'Preview needed'}]
        second = [{'node': 'root', 'kind': 'stale-status', 'text': 'New stale facts'}]
        with mock.patch.object(self.m, 'LIFECYCLE_LIMIT', 1):
            self.m.reminder_items(self.inst, self.n, first)
            self.m.reminder_items(self.inst, self.n, [])
            self.m.reminder_items(self.inst, self.n, second)
            receipt = self.root / '.brain/wake/lifecycle.json'
            before = receipt.read_bytes()
            visible = self.m.reminder_items(self.inst, self.n, first)
            self.assertEqual(len(visible), 1)
            self.assertEqual(visible[0]['kind'], 'missing-preview')
            self.assertEqual(visible[0]['lifecycle_error'], 'receipt state unknown; no suppression')
            self.assertEqual(receipt.read_bytes(), before)
        for invalid in ([], {'bad': 'not-an-id'}):
            data = self.m.lifecycle_empty()
            data['reminders_retired']['root'] = invalid
            with self.assertRaises(ValueError):
                self.m.reminder_reconcile(self.inst, self.n, first, data)

    def test_fix1_import_rebrief_acknowledges_exact_row_after_delivery(self):
        home = self.repo(self.tmp / 'source')
        fm, body = self.m.read_fm(self.root / 'node.md')
        self.m.write_fm(self.root / 'node.md', {**fm, 'home': 'external'}, body)
        self.inst.local_of('root')['home'] = str(home)
        self.inst.save_local()
        self.inst = self.m.Instance(self.root)
        self.n = self.inst.node('root')
        branch = self.m.import_job_id(home)
        row = {**dict.fromkeys(self.m.JOB_COLS, ''), 'Job': branch, 'Started': '2026-01-15T09:00Z'}
        other = {**row, 'Job': 'import:' + 'b' * 40, 'Started': '2026-01-15T08:00Z'}
        self.m.write_jobs(self.inst, self.n, [other, row])
        args = argparse.Namespace(id='root', small=False, branch=None, import_study=True,
            task=None, task_file=None, context_file=None, why_separate='fixture import',
            allow_unexplained=False, peer=None, runtime='codex', model='fixture-model', job_class='build',
            effort=None, to=None, out=None)
        usage = {'compactions': 3, 'processed': 300, 'sessions': 1}
        with mock.patch.object(self.m, 'find_root', return_value=self.root), \
                mock.patch.object(self.m, 'live_usage', return_value=usage), \
                mock.patch.object(self.m, 'job_budget', return_value={'tokens': 100}):
            first = self.m.usage_restart_item(self.inst, self.n, row)
            previous = self.m.usage_restart_item(self.inst, self.n, other)
            stream = mock.Mock(wraps=io.StringIO())
            stream.flush.side_effect = BrokenPipeError('fixture delivery failure')
            with mock.patch.object(sys, 'stdout', stream), self.assertRaises(BrokenPipeError):
                self.m.cmd_brief(args)
            self.assertEqual(self.m.usage_restart_item(self.inst, self.n, row)['episode'], first['episode'])
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.m.cmd_brief(args)
            self.assertIn('Role: import worker', output.getvalue())
            self.assertIsNone(self.m.usage_restart_item(self.inst, self.n, row))
            self.assertEqual(self.m.usage_restart_item(self.inst, self.n, other)['episode'], previous['episode'])

    def test_m8_answer_retains_source_and_recurrent_request(self):
        line = self.request()
        item = self.requests()[0]
        self.assertEqual(item['age_days'], 8)
        saved = {str(p.relative_to(self.root)): p.read_bytes() for p in (self.root / '.brain').rglob('*') if p.is_file()}
        with mock.patch.object(self.m, 'machine_episode_due', side_effect=AssertionError('machine scan')), \
                mock.patch.object(self.m, 'pending_imports', side_effect=AssertionError('import scan')), \
                mock.patch.object(self.m, 'runaway_jobs', side_effect=AssertionError('job scan')):
            request_projection = self.m.inbox_items(self.inst, [self.n], readonly=True, requests_only=True)[0]
        self.assertEqual([i['id'] for i in request_projection], [item['id']])
        with mock.patch.object(self.m, 'load', return_value=self.inst), contextlib.redirect_stdout(io.StringIO()) as output:
            self.m.cmd_inbox(argparse.Namespace(id='root', new=False, json=True, weekly=True))
        self.assertEqual([i['id'] for i in json.loads(output.getvalue())], [item['id']])
        self.assertEqual({str(p.relative_to(self.root)): p.read_bytes() for p in (self.root / '.brain').rglob('*') if p.is_file()}, saved)
        self.now -= datetime.timedelta(days=2)
        with mock.patch.object(self.m, 'load', return_value=self.inst), contextlib.redirect_stdout(io.StringIO()) as output:
            self.m.cmd_inbox(argparse.Namespace(id='root', new=False, json=True, weekly=True))
        self.assertEqual(json.loads(output.getvalue()), [])
        self.now += datetime.timedelta(days=2)
        with mock.patch.object(self.m, 'load', return_value=self.inst), contextlib.redirect_stdout(io.StringIO()):
            self.m.cmd_inbox(argparse.Namespace(id='root', new=True, json=True, weekly=False))
        self.assertEqual(self.requests()[0]['id'], item['id'])  # reporting is not answering
        state = (self.root / '.brain/wake/lifecycle.json').read_bytes()
        with mock.patch.object(self.m, 'inbox_items', return_value=([
                {**item, 'id': 'abcdef111111111111111111'}, {**item, 'id': 'abcdef222222222222222222'}], {})):
            with self.assertRaises(SystemExit):
                self.m.answer_inbox(self.inst, 'abcdef', 'Ambiguous answer')
        self.assertEqual((self.root / '.brain/wake/lifecycle.json').read_bytes(), state)
        before = (self.root / 'inbox.md').read_text()
        self.m.answer_inbox(self.inst, item['id'][:8], 'Blue; this is recorded text.')
        self.assertEqual((self.root / 'inbox.md').read_text(), before)
        self.assertEqual(self.requests(), [])
        records = json.loads((self.root / '.brain/wake/lifecycle.json').read_text())['answers']
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
        changed_need = [{**raw[0], 'text': 'Waiting for a different dependency'}]
        self.assertEqual(len(self.m.reminder_items(self.inst, self.n, changed_need)), 1)
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
            before = (self.root / '.brain/wake/lifecycle.json').read_bytes()
            with self.assertRaises(SystemExit):
                self.m.record_reminder(self.inst, self.n, [protected_item], current['id'], 'ack',
                                       'Handled', resume_condition='Later')
            self.assertEqual((self.root / '.brain/wake/lifecycle.json').read_bytes(), before)
        item = self.m.reminder_items(self.inst, self.n, raw)[0]
        self.m.record_reminder(self.inst, self.n, raw, item['id'], 'ack', 'Waiting',
                               until='2026-01-15T13:00:00Z', resume_condition='Dependency ready')
        self.now += datetime.timedelta(hours=1)
        self.assertEqual(len(self.m.reminder_items(self.inst, self.n, raw)), 1)
        row = {**dict.fromkeys(self.m.JOB_COLS, ''), 'Job': 'import:fixture',
               'Started': '2026-01-15T09:00Z'}
        signal = {'version': 3, 'job': row['Job'], 'started': row['Started'], 'lessons': 'none: fixture',
                  'state': 'blocked', 'summary': 'First dependency', 'report': 'fixture.md', 'wake_id': 'a' * 32}
        handback = self.m.handback_file(self.n, row)
        handback.parent.mkdir(exist_ok=True)
        handback.write_text(json.dumps(signal))
        state = {'jobs': [row], 'worktrees': {}, 'handbacks': {self.m.job_key(row): signal}, 'inbox': []}
        due = self.m.due_items(self.inst, self.n, (state, '', ''), include_stale=False, include_advice=False)
        item = next(i for i in due if i['kind'] == 'handback')
        self.m.record_reminder(self.inst, self.n, due, item['id'], 'ack', 'Handled first dependency',
                               resume_condition='Dependency ready')
        signal['summary'] = 'A different dependency with the same pending wake ID'
        handback.write_text(json.dumps(signal))
        changed = self.m.due_items(self.inst, self.n, (state, '', ''), include_stale=False, include_advice=False)
        self.assertTrue(any(i['kind'] == 'handback' for i in changed))

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
        receipt = self.root / '.brain/wake/lifecycle.json'
        receipt.write_text('{damaged receipt')
        self.assertEqual(self.m.reminder_items(self.inst, self.n, raw)[0]['kind'], 'missing-preview')
        self.assertEqual(receipt.read_text(), '{damaged receipt')
        self.request('A live protected question')
        self.assertTrue(self.m.inbox_lines(self.n))
        receipt.unlink()
        outside = self.tmp / 'outside-receipts.json'
        outside.write_text('retain outside')
        receipt.symlink_to(outside)
        self.assertTrue(self.m.reminder_items(self.inst, self.n, raw))
        self.assertEqual(outside.read_text(), 'retain outside')

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
        before = (self.root / '.brain/wake/lifecycle.json').read_bytes()
        with self.assertRaises(SystemExit):
            self.m.record_freshness(self.inst, self.n)
        self.assertEqual((self.root / '.brain/wake/lifecycle.json').read_bytes(), before)
        (self.root / 'source.txt').unlink()
        (self.root / 'source.txt').write_text('input three')
        fm, body = self.m.read_fm(self.root / 'node.md')
        self.m.write_fm(self.root / 'node.md', {**fm, **self.n['fm']}, body)
        problems = self.m.collect_problems(self.inst)[0]
        self.assertTrue(any('upstream index stale' in p for p in problems))
        self.m.record_freshness(self.inst, self.n)
        self.assertFalse(any('upstream index' in p for p in self.m.collect_problems(self.inst)[0]))

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
        args = argparse.Namespace(id='root', small=False, branch=None, import_study=False,
            task='Inspect a fixture', task_file=None, context_file=None, why_separate='fixture work',
            allow_unexplained=False, peer=None, runtime='codex', model='fixture-model', job_class='build',
            effort=None, to=None, out=None)
        with mock.patch.object(self.m, 'load', return_value=self.inst), \
                mock.patch.object(self.m, 'acknowledge_usage_restart') as ack:
            with contextlib.redirect_stdout(io.StringIO()):
                self.m.cmd_brief(args)
            ack.assert_called_once()
            ack.reset_mock()
            stream = mock.Mock(wraps=io.StringIO())
            stream.flush.side_effect = BrokenPipeError('fixture delivery failure')
            with mock.patch.object(sys, 'stdout', stream), self.assertRaises(BrokenPipeError):
                self.m.cmd_brief(args)
            ack.assert_not_called()

    def test_cli_lifecycle_commands_use_fixture_state_only(self):
        self.request()
        asks = [i for i in json.loads(self.b(self.root, 'inbox', 'root', '--weekly', '--json'))
                if i['kind'] == 'inbox']
        self.assertEqual(len(asks), 1)
        self.assertIn('answer recorded', self.b(self.root, 'answer', asks[0]['id'], 'Fixture answer'))
        self.assertEqual(json.loads(self.b(self.root, 'inbox', 'root', '--weekly', '--json')), [])
        fm, body = self.m.read_fm(self.root / 'node.md')
        self.m.write_fm(self.root / 'node.md', {**fm, 'labels': 'kind:code'}, body)
        closed = {**dict.fromkeys(self.m.JOB_COLS, ''), 'Job': 'job-merged',
                  'Started': '2026-01-01T10:00Z', 'Ended': '2026-01-01T11:00Z',
                  'Outcome': 'merged', 'Settled': 'yes'}
        parked = {**dict.fromkeys(self.m.JOB_COLS, ''), 'Job': 'import:fixture',
                  'Started': '2026-01-15T09:00Z'}
        self.m.write_jobs(self.inst, self.n, [closed, parked])
        handback = self.m.handback_file(self.n, parked)
        handback.parent.mkdir(exist_ok=True)
        handback.write_text(json.dumps({'version': 2, 'job': parked['Job'], 'started': parked['Started'],
            'state': 'blocked', 'lessons': 'none: fixture', 'report': 'fixture-report.md',
            'ready': None, 'ready_reasons': [], 'wake_id': 'a' * 32}))
        due = json.loads(self.b(self.root, 'due', 'root', '--json'))
        preview = next(i for i in due if i['kind'] == 'missing-preview')
        blocked = next(i for i in due if i['kind'] == 'handback')
        self.assertIn('deferred', self.b(self.root, 'due', 'root', '--defer', preview['id'],
                                        '2999-01-01T00:00:00Z', '--reason', 'Fixture preview later'))
        self.assertIn('acknowledged', self.b(self.root, 'due', 'root', '--ack', blocked['id'],
            '--reason', 'Dependency handled', '--resume', 'Fixture dependency ready'))
        due = json.loads(self.b(self.root, 'due', 'root', '--json'))
        self.assertNotIn('missing-preview', [i['kind'] for i in due])
        self.assertNotIn('handback', [i['kind'] for i in due])
        handled = json.loads(self.b(self.root, 'due', 'root', '--handled', '--json'))
        self.assertEqual(len([i for i in handled if i.get('handling')]), 2)
        self.assertIn('unconfigured', self.b(self.root, 'index', 'root'))
        self.assertIn('gap', json.loads(self.b(self.root, 'machine', 'wake-setup', 'root')))
