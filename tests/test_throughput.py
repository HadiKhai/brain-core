"""Measured daily rollups from fictional ledger and admission evidence."""
import datetime
import json
import os
from unittest import mock

import core_loader
from scenario import Scenario, CORE


class DailyThroughput(Scenario):
    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(os.environ, self.env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.m = core_loader.SourceFileLoader('throughput_core', str(CORE / 'bin/brain')).load_module()
        self.root = self.init('sample')
        self.inst = self.m.Instance(self.root)
        self.n = self.inst.node('root')
        self.now = datetime.datetime(2026, 1, 2, 12, tzinfo=datetime.timezone.utc)

    def row(self, name, **changes):
        return {**dict.fromkeys(self.m.JOB_COLS, ''), 'Job': name, 'Started': '2026-01-02T01:00Z',
                'Ended': '2026-01-02T02:00Z', 'Outcome': 'merged', 'Hours': '1',
                'Usage': 'host: fixture', 'Fresh in': '1000', 'Active h': '.5',
                'Verify runs': '3', 'Verify reuses': '1', 'Queue s': '12', **changes}

    def test_daily_rates_measurement_coverage_retries_and_delay_without_writes(self):
        rows = [self.row('one'), self.row('two', **{'Usage': 'none found', 'Fresh in': '', 'Queue s': ''}),
                self.row('parked', Outcome='closed'), self.row('future', Ended='2026-01-03T02:00Z'),
                self.row('old', Started='2025-01-01T00:00Z', Ended='2025-01-01T02:00Z')]
        self.m.write_jobs(self.inst, self.n, rows)
        before = (self.n['dir'] / 'job-ledger.md').read_bytes()
        with mock.patch.object(self.inst, 'now_dt', return_value=self.now):
            metrics = self.m.daily_throughput(self.inst, self.n, rows)
            payload = self.m.jobs_summary(self.inst, self.n)
        today = next(row for row in metrics if row['date'] == '2026-01-02')
        self.assertEqual(today['merged'], 2)
        self.assertAlmostEqual(today['merges_per_hour'], 2 / 12)
        self.assertEqual(today['minutes_per_merge'], 360)
        self.assertEqual(today['measured_merges'], 1)
        self.assertEqual(today['tokens_per_merge'], 1000)
        self.assertEqual(today['verify_runs'], 6)
        self.assertEqual(today['verify_reused'], 2)
        self.assertEqual(today['reruns'], 4)
        self.assertEqual(today['queue_seconds'], 12)
        self.assertEqual(today['queue_measured_jobs'], 1)
        self.assertEqual(payload['throughput'], metrics)
        self.assertEqual((self.n['dir'] / 'job-ledger.md').read_bytes(), before)
        self.assertFalse(any(row['date'] == '2026-01-03' for row in metrics))

    def test_duration_and_counter_ledger_consumption_keeps_numeric_evidence(self):
        row = self.row('one', Ended='', Outcome='')
        hb = dict(version=2, job=row['Job'], started=row['Started'], state='done', lessons='none: fixture',
                  verify_runs=2, verify_reused=1, admission_wait_seconds_total=7.5)
        path = self.m.handback_file(self.n, row)
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(hb))
        self.m.write_jobs(self.inst, self.n, [row])
        saved = self.m.read_jobs(self.n)[0]
        self.assertEqual((saved['Verify runs'], saved['Verify reuses'], saved['Queue s']), ('2', '1', '7.5'))
        path.write_text(json.dumps({**hb, 'verify_runs': True, 'admission_wait_seconds_total': 'private'}))
        self.m.write_jobs(self.inst, self.n, [row])
        self.assertNotIn('private', json.dumps(self.m.read_jobs(self.n)))
