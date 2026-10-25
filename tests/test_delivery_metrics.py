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
