"""Delivery metrics use observed evidence instead of Git dates or saved URLs."""
import datetime
import json
import sys
from scenario import Scenario, CORE
sys.path.insert(0, str(CORE / 'lib'))
from brain import events, instance, delivery_metrics


class DeliveryMetrics(Scenario):
    def prepare(self):
        root = self.init('demo')
        inst = instance.Instance(root)
        n = inst.node('root')
        return root, inst, n

    def fact(self, inst, n, kind, seconds, key='a' * 24, **fields):
        at = datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc) + datetime.timedelta(seconds=seconds)
        return events.record_fact(inst, n, kind, key, at=at.isoformat(), **fields)

    def test_first_usable_preview_measures_precise_request_delay(self):
        root, inst, n = self.prepare()
        self.fact(inst, n, 'request_received', 0)
        self.fact(inst, n, 'preview_published', 10, evidence={'ref': 'published'})
        self.fact(inst, n, 'preview_usable', 50.125, evidence={'head': 'abc1234', 'ref': 'tour-1'})
        self.fact(inst, n, 'preview_usable', 70, evidence={'ref': 'tour-2'})
        row = delivery_metrics.delivery_measurements(inst, n)['jobs'][0]
        self.assertEqual(row['request_to_preview_seconds'], 50.125)
        self.assertEqual(row['preview_measurement'], 'observed')
        before = (root / '.brain/events.jsonl').read_bytes()
        cli = json.loads(self.b(root, 'events', 'root', '--metrics'))
        self.assertEqual(cli['jobs'][0]['request_to_preview_seconds'], 50.125)
        self.assertEqual((root / '.brain/events.jsonl').read_bytes(), before)

    def test_missing_request_or_usability_evidence_stays_unknown(self):
        _, inst, n = self.prepare()
        self.fact(inst, n, 'preview_usable', 20, evidence={'ref': 'tour'})
        self.fact(inst, n, 'request_received', 0, key='b' * 24)
        self.fact(inst, n, 'preview_usable', 20, key='b' * 24)
        rows = delivery_metrics.delivery_measurements(inst, n)['jobs']
        self.assertEqual([r['request_to_preview_seconds'] for r in rows], [None, None])
        self.assertEqual(rows[0]['preview_measurement'], 'missing_request')
        self.assertEqual(rows[1]['preview_measurement'], 'missing_usable_preview_evidence')

    def test_reconstructed_history_is_excluded_from_observed_delay(self):
        _, inst, n = self.prepare()
        self.fact(inst, n, 'request_received', 0, origin='reconstructed')
        self.fact(inst, n, 'preview_usable', 10, evidence={'ref': 'tour'})
        result = delivery_metrics.delivery_measurements(inst, n)
        self.assertEqual(result['reconstructed_facts_excluded'], 1)
        self.assertIsNone(result['jobs'][0]['request_to_preview_seconds'])

    def test_working_and_waiting_are_attributed_to_explicit_phases_only(self):
        _, inst, n = self.prepare()
        self.fact(inst, n, 'request_received', 0)
        self.fact(inst, n, 'phase_changed', 5, phase='admission', state='waiting')
        self.fact(inst, n, 'phase_changed', 15, phase='execution', state='working')
        self.fact(inst, n, 'phase_changed', 35, phase='review', state='waiting')
        self.fact(inst, n, 'phase_changed', 45, phase='coordination', state='working')
        self.fact(inst, n, 'delivery_completed', 50)
        result = delivery_metrics.delivery_measurements(inst, n)['jobs'][0]
        self.assertEqual(result['phase_seconds'], {
            'admission': {'working': 0, 'waiting': 10}, 'execution': {'working': 20, 'waiting': 0},
            'review': {'working': 0, 'waiting': 10}, 'coordination': {'working': 5, 'waiting': 0}})
        self.assertEqual(result['unattributed_seconds'], 5)
        self.assertEqual(result['phase_observed_seconds'], 45)

    def test_terminal_event_stops_open_interval_and_missing_phases_stay_unknown(self):
        _, inst, n = self.prepare()
        self.fact(inst, n, 'request_received', 0)
        self.fact(inst, n, 'phase_changed', 1, phase='execution', state='working')
        self.fact(inst, n, 'delivery_completed', 11)
        self.fact(inst, n, 'phase_changed', 20, phase='review', state='working')
        self.fact(inst, n, 'request_received', 0, key='b' * 24)
        result = delivery_metrics.delivery_measurements(inst, n)['jobs']
        self.assertEqual(result[0]['phase_observed_seconds'], 10)
        self.assertNotIn('review', result[0]['phase_seconds'])
        self.assertEqual(result[1]['phase_coverage'], 'no_phase_observations')
        self.assertEqual(result[1]['phase_seconds'], {})

    def test_open_phase_uses_explicit_now_and_done_phase_stops_attribution(self):
        _, inst, n = self.prepare()
        self.fact(inst, n, 'phase_changed', 2, phase='execution', state='working')
        self.fact(inst, n, 'phase_changed', 12, phase='execution', state='done')
        self.fact(inst, n, 'phase_changed', 15, phase='acknowledgement', state='waiting')
        now = datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc) + datetime.timedelta(seconds=25)
        result = delivery_metrics.delivery_measurements(inst, n, now)['jobs'][0]
        self.assertEqual(result['phase_seconds']['execution']['working'], 10)
        self.assertEqual(result['phase_seconds']['acknowledgement']['waiting'], 10)
        self.assertIsNone(result['unattributed_seconds'])

    def test_rework_separates_findings_requirements_and_unclassified_episodes(self):
        _, inst, n = self.prepare()
        self.fact(inst, n, 'rework_started', 0, reason_code='review_finding')
        self.fact(inst, n, 'rework_finished', 10)
        self.fact(inst, n, 'rework_started', 15, reason_code='requirement_change')
        self.fact(inst, n, 'rework_started', 20)
        self.fact(inst, n, 'delivery_completed', 24)
        rework = delivery_metrics.delivery_measurements(inst, n)['jobs'][0]['rework']
        self.assertEqual(rework, {'review_finding': {'episodes': 1, 'seconds': 10},
                                 'requirement_change': {'episodes': 1, 'seconds': 5},
                                 'unclassified': {'episodes': 1, 'seconds': 4}})

    def test_dependency_wait_ranking_and_completion_are_evidence_based(self):
        _, inst, n = self.prepare()
        b, c = 'b' * 24, 'c' * 24
        self.fact(inst, n, 'dependency_declared', 0, depends_on=[b, c])
        self.fact(inst, n, 'dependency_blocked', 5, depends_on=[b])
        self.fact(inst, n, 'dependency_unblocked', 15)
        self.fact(inst, n, 'dependency_blocked', 20, depends_on=[b, c])
        self.fact(inst, n, 'delivery_completed', 40)
        self.fact(inst, n, 'job_landed', 30, key=b, evidence={'head': 'abc1234'})
        result = delivery_metrics.delivery_measurements(inst, n)['jobs'][0]
        self.assertEqual(result['dependency_wait_seconds'], {b: 30, c: 20})
        self.assertEqual(result['critical_dependency'], b)
        self.assertEqual(result['critical_dependency_basis'], 'largest_observed_wait')
        self.assertEqual(result['unresolved_dependencies'], [c])
        self.assertEqual(result['blocking_dependencies'], [])
        self.assertIsNone(result['forecast_critical_path'])

    def test_declared_dependencies_do_not_invent_wait_or_forecast(self):
        _, inst, n = self.prepare()
        self.fact(inst, n, 'dependency_declared', 0, depends_on=['b' * 24])
        result = delivery_metrics.delivery_measurements(inst, n)['jobs'][0]
        self.assertEqual(result['blocking_dependencies'], ['b' * 24])
        self.assertEqual(result['dependency_wait_seconds'], {})
        self.assertIsNone(result['critical_dependency'])
        self.assertEqual(result['rework']['unclassified']['episodes'], 0)
