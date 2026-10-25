"""Typed delivery facts contain evidence and precise times, never source prose."""
import datetime
import json
import sys
from unittest import mock

from scenario import Scenario, CORE
sys.path.insert(0, str(CORE / 'lib'))
from brain import events, instance


class EventFacts(Scenario):
    def test_exact_job_and_node_scope_and_observed_timestamp(self):
        root = self.init('demo')
        self.b(root, 'new', 'app', '--title', 'App')
        inst = instance.Instance(root)
        n = inst.node('app')
        at = datetime.datetime(2025, 1, 1, 12, 0, 0, 123456, tzinfo=datetime.timezone.utc)
        with mock.patch.object(inst, 'now_dt', return_value=at):
            event = events.record_fact(inst, n, 'product_test_started', 'a' * 24,
                                       phase='execution', state='working', product=True, head='b' * 40)
            events.record_fact(inst, n, 'review_completed', 'c' * 24, failure_class='assertion')
        rows = events.facts(inst, n, 'a' * 24)
        self.assertEqual(rows, [event])
        self.assertEqual(rows[0]['data']['at'], at.isoformat(timespec='microseconds'))
        self.assertEqual(events.facts(inst, inst.node('root')), [])
        self.assertEqual(events.facts(inst, n, 'd' * 24), [])

    def test_rejects_bodies_unknown_fields_bad_identities_and_future_times(self):
        root = self.init('demo')
        inst = instance.Instance(root)
        n = inst.node('root')
        count = len(events.read(inst))
        for data in ({'message': 'PRIVATE-BODY'}, {'phase': 'some free text'},
                     {'depends_on': ['invalid']}, {'head': 'not a hash'},
                     {'at': '2999-01-01T00:00:00+00:00'}, {'origin': 'guessed'}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                events.record_fact(inst, n, 'phase_changed', 'a' * 24, **data)
        self.assertEqual(len(events.read(inst)), count)
        self.assertNotIn('PRIVATE-BODY', (root / '.brain/events.jsonl').read_text())

    def test_cli_records_one_typed_fact_with_an_evidence_reference(self):
        root = self.init('demo')
        row = json.loads(self.b(root, 'events', 'root', '--record', 'preview_usable',
                               '--job-key', 'a' * 24, '--data', '{"evidence":{"head":"abc1234"}}', '--json'))
        self.assertEqual(row['kind'], 'fact')
        self.assertEqual(row['data']['fact_type'], 'preview_usable')
        self.assertEqual(len(events.facts(instance.Instance(root), instance.Instance(root).node('root'))), 1)

    def test_reconstructed_history_is_explicitly_labelled(self):
        root = self.init('demo')
        inst = instance.Instance(root)
        event = events.record_fact(inst, inst.node('root'), 'request_received', 'a' * 24,
                                   origin='reconstructed', at='2025-01-01T00:00:00+00:00')
        self.assertEqual(event['data']['origin'], 'reconstructed')
