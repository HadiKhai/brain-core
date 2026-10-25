"""Round budgets retain lifetime accounting and restart only on delivered rebriefs."""
import datetime
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
        self.usage['processed'] = 420
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
