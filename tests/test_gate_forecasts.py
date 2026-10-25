"""Queue estimates use fabricated clocks, scopes and native admission records."""
import contextlib
import io
import json
import os
from unittest import mock

import core_loader
from scenario import Scenario, CORE


class GateForecasts(Scenario):
    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(os.environ, self.env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.m = core_loader.SourceFileLoader('gate_forecast_core', str(CORE / 'bin/brain')).load_module()
        self.root = self.init('sample')
        self.inst = self.m.Instance(self.root)

    def row(self, token, state='queued', **extra):
        return dict(token=token * 32, owner=[123, 'fixture'], kind='verify', state=state,
                    queued_at=950, scope=self.m.machine_scope(self.inst, 'root'), **extra)

    def test_priority_ages_normal_work_and_preserves_manual_order_ties(self):
        normal = self.row('a')
        landing = self.row('b', priority='landing')
        old = {**self.row('c'), 'queued_at': 100}
        rows = [normal, landing, old]
        self.assertEqual(self.m.queue_order(rows, now=1000), [old, landing, normal])
        self.assertEqual(rows, [normal, landing, old])
        peer = self.row('d', priority='landing')
        self.assertEqual(self.m.queue_order([peer, landing], now=1000), [peer, landing])
        self.assertTrue(self.m.machine_queue.valid(landing))
        for extra in ({'priority': 'urgent'}, {'queued_at': True}, {'expected_seconds': float('nan')}):
            self.assertFalse(self.m.machine_queue.valid({**normal, **extra}))

    def test_slot_estimate_uses_remaining_runtime_and_every_preceding_gate(self):
        running = self.row('a', 'running', started_at=900, expected_seconds=300)
        first = self.row('b', expected_seconds=100)
        target = self.row('c', expected_seconds=200)
        rows = [running, first, target]
        estimate = self.m.queue_wait_estimate(rows, target, slots=1, now=1000)
        self.assertEqual(estimate['wait_seconds'], 300)
        self.assertEqual(estimate['completion_seconds'], 500)
        self.assertEqual(estimate['position'], 2)
        self.assertEqual(self.m.queue_wait_estimate(rows, running, slots=1, now=1000)['completion_seconds'], 200)
        self.assertEqual(self.m.queue_wait_estimate(rows, target, slots=2, now=1000)['wait_seconds'], 100)
        unknown = [{**running, 'expected_seconds': None}, first, target]
        self.assertIsNone(self.m.queue_wait_estimate(unknown, target, slots=1, now=1000)['wait_seconds'])
        self.assertIsNone(self.m.queue_wait_estimate(rows, target, slots=1, now=1301)['wait_seconds'])
        self.assertIsNone(self.m.queue_wait_estimate(rows, target, slots=1, now=1000, held=True)['wait_seconds'])

    def test_projection_uses_priority_order_and_is_readonly_without_process_probes(self):
        directory = self.tmp / 'machine'
        directory.mkdir()
        normal = self.row('a', expected_seconds=100)
        landing = self.row('b', priority='landing', expected_seconds=200)
        landing['owner'] = [124, 'fixture']
        path = directory / 'machine-care-queue.json'
        path.write_text(json.dumps([normal, landing]))
        before = path.read_bytes()
        with mock.patch.object(self.m, 'machine_dir', return_value=directory), \
                mock.patch.object(self.m.time, 'time', return_value=1000), \
                mock.patch.object(self.m, 'machine_limit', return_value=1), \
                mock.patch.object(self.m, 'process_stamp', side_effect=AssertionError('no live probe')):
            view = self.m.job_queue_projection(self.inst, 'root', normal['owner'])
        self.assertEqual(view['position'], 2)
        self.assertEqual(view['estimate']['wait_seconds'], 200)
        self.assertEqual(path.read_bytes(), before)

    def test_native_admission_uses_configured_landing_priority_and_duration(self):
        n = self.inst.node('root')
        n['fm'].update(verify_priority='landing', verify_estimate_seconds='120')
        directory = self.tmp / 'machine'
        directory.mkdir()
        with mock.patch.object(self.m, 'machine_dir', return_value=directory), \
                mock.patch.object(self.inst, 'node', return_value=n), \
                mock.patch.object(self.m, 'process_stamp', return_value='fixture'), \
                mock.patch.object(self.m, 'machine_policy', return_value=self.m.MACHINE_DEFAULTS), \
                mock.patch.object(self.m, 'machine_reading', return_value={}), \
                mock.patch.object(self.m, 'machine_reasons', return_value=[]), \
                mock.patch.object(self.m, 'machine_limit', return_value=1), \
                contextlib.redirect_stdout(io.StringIO()):
            timings = []
            timing_token = self.m.ADMISSION_TIMING.set(timings)
            try:
                token = self.m.machine_wait('verify', self.inst, 'root')
            finally:
                self.m.ADMISSION_TIMING.reset(timing_token)
            self.assertEqual(len(timings), 1)
            self.assertGreaterEqual(timings[0], 0)
            try:
                with self.m.machine_queue() as q:
                    self.assertEqual(q.rows[0]['priority'], 'landing')
                    self.assertEqual(q.rows[0]['expected_seconds'], 120)
                    self.assertGreaterEqual(q.rows[0]['started_at'], q.rows[0]['queued_at'])
            finally:
                self.m.machine_release(token)
            with self.m.machine_queue() as q:
                self.assertEqual(q.rows, [])
