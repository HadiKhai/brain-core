"""Round budgets retain lifetime accounting and restart only on delivered rebriefs."""
import datetime
import json
from unittest import mock

import core_loader
from scenario import CORE, Scenario


class UsageRounds(Scenario):
    def setUp(self):
        super().setUp()
        self.root = self.init('oak')
        self.m = core_loader.SourceFileLoader('round_fixture', str(CORE / 'bin/brain')).load_module()
        self.inst = self.m.Instance(self.root)
        self.n = self.inst.node('root')
        self.row = {**dict.fromkeys(self.m.JOB_COLS, ''), 'Job': 'job/palette',
                    'Started': '2026-01-15T08:00Z'}
        self.now = datetime.datetime(2026, 1, 15, 12, tzinfo=datetime.timezone.utc)
        clock = mock.patch.object(self.m.Instance, 'now_dt', side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.usage = self.m.host_row_usage({**self.row, 'Usage': 'host', 'Fresh in': '210',
                                           'Compactions': '4', 'Active h': '2'})
        for patch in (mock.patch.object(self.m, 'live_usage', side_effect=lambda *a: dict(self.usage)),
                      mock.patch.object(self.m, 'job_budget', return_value={'tokens': 100, 'hours': 1})):
            patch.start()
            self.addCleanup(patch.stop)

    def restart(self):
        self.assertIsNotNone(self.m.usage_restart_item(self.inst, self.n, self.row))
        self.m.acknowledge_usage_restart(self.inst, self.n, self.row)

    def test_initial_brief_keeps_lifetime_and_restart_resets_only_round(self):
        self.m.acknowledge_usage_restart(self.inst, self.n, self.row)
        self.assertIsNone(self.m.job_round_usage(self.inst, self.n, self.row, self.usage))
        self.restart()
        before = dict(self.usage)
        self.now += datetime.timedelta(hours=2)
        self.usage.update(processed=240, fresh=240, compactions=5, active_minutes=150)
        round_usage = self.m.job_round_usage(self.inst, self.n, self.row, self.usage)
        self.assertEqual((round_usage['processed'], round_usage['compactions'], round_usage['active_minutes']),
                         (30, 1, 30))
        self.assertEqual(round_usage['hours'], .5)
        text = self.m.usage_line(self.usage, {'tokens': 100, 'hours': 1}, 6, round_usage=round_usage)
        self.assertIn('usage: 240 tokens', text)
        self.assertIn('since restart: 30 tokens', text)
        self.assertNotIn(': over', text)
        self.assertEqual(before['processed'], 210)
        self.assertEqual(self.usage['processed'], 240)
        # Reprinting the same brief is not another round.
        self.m.acknowledge_usage_restart(self.inst, self.n, self.row)
        self.assertEqual(self.m.job_round_usage(self.inst, self.n, self.row, self.usage), round_usage)

    def test_pending_second_restart_preserves_current_round_meter(self):
        self.restart()
        self.usage.update(processed=420, compactions=7)
        self.assertIsNotNone(self.m.usage_restart_item(self.inst, self.n, self.row))
        self.assertEqual(self.m.job_round_usage(self.inst, self.n, self.row, self.usage)['processed'], 210)

    def test_budget_and_compaction_advice_uses_round_counters(self):
        self.m.write_jobs(self.inst, self.n, [self.row])
        stats = {'open': [{**self.row, 'age_h': 4}], 'median_hours': None, 'median_tokens': None}
        self.restart()
        with mock.patch.object(self.m, 'jobs_summary', return_value=stats):
            self.assertEqual(self.m.runaway_jobs(self.inst, self.n), [])
            self.usage.update(processed=311, compactions=7, active_minutes=181)
            warnings = ' '.join(self.m.runaway_jobs(self.inst, self.n))
        for part in ('100-token budget', '1-hour budget', '3 compactions'):
            self.assertIn(part, warnings)

    def test_unknown_counter_stays_unknown_and_regression_refuses(self):
        self.restart()
        self.usage['processed'] = None
        round_usage = self.m.job_round_usage(self.inst, self.n, self.row, self.usage)
        self.assertIsNone(round_usage['processed'])
        self.assertEqual(round_usage['compactions'], 0)
        self.usage['processed'] = 200
        with self.assertRaisesRegex(ValueError, 'regressed'):
            self.m.job_round_usage(self.inst, self.n, self.row, self.usage)

    def test_regressed_round_read_is_flagged_without_rewriting_strict_baseline(self):
        self.restart()
        self.usage.update(processed=420, compactions=7)
        self.assertIsNotNone(self.m.usage_restart_item(self.inst, self.n, self.row))
        before = self.m.lifecycle_read(self.inst)
        for counter in ('processed', 'compactions', 'active_minutes'):
            with self.subTest(counter=counter):
                usage = {**self.usage, counter: 0}
                with self.assertRaisesRegex(ValueError, 'regressed'):
                    self.m.job_round_usage(self.inst, self.n, self.row, usage)
                read = self.m.job_round_usage_read(self.inst, self.n, self.row, usage)
                self.assertEqual(read['state'], 'regressed')
                for name in ('processed', 'compactions', 'active_minutes', 'hours'):
                    self.assertIsNone(read[name])
                self.assertEqual(self.m.lifecycle_read(self.inst), before)
                with self.assertRaisesRegex(ValueError, 'regressed'):
                    self.m.acknowledge_usage_restart(self.inst, self.n, self.row,
                                                    observed_usage=usage)
                self.assertEqual(self.m.lifecycle_read(self.inst), before)

    def test_relaunched_lower_usage_keeps_status_due_and_ui_available_and_flagged(self):
        self.restart()
        self.row.update(Usage='host', **{'Fresh in': '10', 'Compactions': '0', 'Active h': '0.1'})
        self.m.write_jobs(self.inst, self.n, [self.row])
        self.usage.update(processed=10, fresh=10, compactions=0, active_minutes=6)
        baseline = self.m.lifecycle_read(self.inst)['usage']
        warnings = self.m.runaway_jobs(self.inst, self.n, readonly=True)
        self.assertTrue(any('counters regressed' in line for line in warnings))
        self.b(self.root, 'status', 'root')
        due = json.loads(self.b(self.root, 'due', 'root', '--json'))
        self.assertTrue(any(item.get('job') == self.m.job_key(self.row)
                            and item.get('condition') == 'usage-repair' for item in due))
        output = json.loads(self.b(self.root, 'ui', '--json'))
        node = next(node for node in output['nodes'] if node['id'] == 'root')
        job = next(job for job in node['jobs']['open'] if job['Job'] == self.row['Job'])
        self.assertEqual(job['usage']['processed'], 10)
        self.assertEqual(job['round_usage']['state'], 'regressed')
        self.assertIsNone(job['round_usage']['processed'])
        self.assertTrue(any(item.get('condition') == 'usage-repair' for item in output['due']))
        self.assertEqual(self.m.lifecycle_read(self.inst)['usage'], baseline)

    def test_legacy_baseline_uses_elapsed_time_without_fabricating_active_counts(self):
        key = self.m.digest([self.n['id'], self.m.job_key(self.row)])
        with self.m.lifecycle_store(self.inst) as data:
            data['usage'][key] = {'job': self.m.job_key(self.row),
                'baseline': {'compactions': 3, 'processed': 200}, 'episode': None,
                'at': '2026-01-15T11:00Z'}
        self.now += datetime.timedelta(minutes=30)
        round_usage = self.m.job_round_usage(self.inst, self.n, self.row, self.usage)
        self.assertEqual(round_usage['processed'], 10)
        self.assertIsNone(round_usage['active_minutes'])
        self.assertEqual(round_usage['hours'], 1.5)
        # The identity includes the job start, not just its branch.
        self.assertIsNone(self.m.job_round_usage(self.inst, self.n,
                          {**self.row, 'Started': '2026-01-15T11:00Z'}, self.usage))

    def test_retiring_initial_budget_alert_never_invents_a_delivered_restart(self):
        key = self.m.digest([self.n['id'], self.m.job_key(self.row)])
        self.usage.update(compactions=2, active_minutes=240)
        for round_field in ({}, {'round_at': None}):
            with self.subTest(round_field=round_field):
                record = {'job': self.m.job_key(self.row), 'episode': 'a' * 24,
                          'baseline': {'compactions': 0, 'processed': 0},
                          'at': '2026-01-15T11:00Z', **round_field}
                with self.m.lifecycle_store(self.inst) as data:
                    data['usage'][key] = record
                self.usage['compactions'] = 2
                self.assertIsNone(self.m.job_round_usage(self.inst, self.n, self.row, self.usage))
                self.assertIsNone(self.m.usage_restart_item(self.inst, self.n, self.row))
                self.assertEqual(self.m.lifecycle_read(self.inst)['usage'][key]['baseline'], record['baseline'])
                for compactions in (2, 3):
                    self.usage['compactions'] = compactions
                    self.m.usage_restart_item(self.inst, self.n, self.row)
                    round_usage = self.m.job_round_usage(self.inst, self.n, self.row, self.usage)
                    self.assertIsNone(round_usage)
                    line = self.m.usage_line(self.usage, {'hours': 2}, 4, round_usage=round_usage)
                    self.assertNotIn('since restart', line)
                    self.assertIn('4.0 of 2 h', line)
                self.m.acknowledge_usage_restart(self.inst, self.n, self.row)
                round_usage = self.m.job_round_usage(self.inst, self.n, self.row, self.usage)
                self.assertEqual(round_usage['hours'], 0)
                self.assertEqual(round_usage['since'], self.inst.now())

    def test_bad_baseline_is_retained_for_repair(self):
        self.restart()
        key = self.m.digest([self.n['id'], self.m.job_key(self.row)])
        with self.m.lifecycle_store(self.inst) as data:
            data['usage'][key]['baseline']['active_minutes'] = True
        with self.assertRaisesRegex(ValueError, 'baseline'):
            self.m.job_round_usage(self.inst, self.n, self.row, self.usage)

    def test_successful_manual_compaction_rebrief_records_round_once(self):
        signal = {'restart': 'compactions', 'restart_id': 'a' * 32}
        with mock.patch.object(self.m, 'job_signal', return_value=signal):
            self.m.acknowledge_usage_restart(self.inst, self.n, self.row)
            self.assertEqual(self.m.job_round_usage(self.inst, self.n, self.row, self.usage)['processed'], 0)
            self.n['local']['worktrees'] = {self.row['Job']: {
                'restart_ack': self.m.digest([self.m.job_key(self.row), signal])}}
            self.usage['processed'] += 20
            self.m.acknowledge_usage_restart(self.inst, self.n, self.row)
            self.assertEqual(self.m.job_round_usage(self.inst, self.n, self.row, self.usage)['processed'], 20)

    def test_captured_prelaunch_usage_does_not_erase_new_worker_usage(self):
        self.assertIsNotNone(self.m.usage_restart_item(self.inst, self.n, self.row))
        captured = dict(self.usage)
        self.usage.update(processed=260, fresh=260, compactions=5, active_minutes=135)
        with mock.patch.object(self.m, 'live_usage', side_effect=AssertionError('read after launch')):
            self.m.acknowledge_usage_restart(self.inst, self.n, self.row, observed_usage=captured)
        round_usage = self.m.job_round_usage(self.inst, self.n, self.row, self.usage)
        self.assertEqual((round_usage['processed'], round_usage['compactions'], round_usage['active_minutes']),
                         (50, 1, 15))
        self.assertEqual(captured['processed'], 210)
        self.assertEqual(captured['active_minutes'], 120)

    def test_unknown_or_incomplete_captured_usage_refuses_without_changing_receipt(self):
        self.assertIsNotNone(self.m.usage_restart_item(self.inst, self.n, self.row))
        before = self.m.lifecycle_read(self.inst)
        good = dict(self.usage)
        invalid = [None, {}, [], {k: v for k, v in good.items() if k != 'active_minutes'},
                   {**good, 'sessions': 0, 'source': 'none found'}, {**good, 'sessions': None},
                   {**good, 'source': 'not counted'}, {k: v for k, v in good.items() if k != 'source'},
                   {**good, 'active_minutes': None}, {**good, 'active_minutes': True},
                   {**good, 'processed': -1}, {**good, 'processed': 1.5},
                   {**good, 'compactions': None}]
        with mock.patch.object(self.m, 'live_usage', side_effect=AssertionError('fallback from unknown snapshot')):
            for usage in invalid:
                with self.subTest(usage=usage), self.assertRaisesRegex(ValueError, 'usage unavailable'):
                    self.m.acknowledge_usage_restart(self.inst, self.n, self.row, observed_usage=usage)
                self.assertEqual(self.m.lifecycle_read(self.inst), before)

    def test_captured_usage_cannot_regress_an_existing_round_baseline(self):
        self.restart()
        self.usage.update(processed=420, compactions=7)
        self.assertIsNotNone(self.m.usage_restart_item(self.inst, self.n, self.row))
        before = self.m.lifecycle_read(self.inst)
        with self.assertRaisesRegex(ValueError, 'regressed'):
            self.m.acknowledge_usage_restart(self.inst, self.n, self.row,
                                            observed_usage={**self.usage, 'processed': 200})
        self.assertEqual(self.m.lifecycle_read(self.inst), before)
