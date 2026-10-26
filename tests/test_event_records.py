"""Ledger and hand-back projections are materialized from durable source events."""
import json
import sys
from unittest import mock
from scenario import Scenario, CORE
sys.path.insert(0, str(CORE / 'lib'))
from brain import events, instance, jobs


class EventRecords(Scenario):
    def test_worker_paperwork_rebuilds_from_events_without_coordinator_edits(self):
        root = self.init('demo')
        home = self.repo(self.tmp / 'code')
        self.b(root, 'new', 'app', '--title', 'App', '--home', str(home))
        self.b(root, 'wt', 'add', 'app', 'job-one', '--no-setup')
        self.b(root, 'handback', 'app', '--branch', 'job-one', '--state', 'blocked',
               '--summary', 'PRIVATE-SUMMARY', '--lessons', 'none: PRIVATE-LESSON')
        inst = instance.Instance(root)
        n = inst.node('app')
        row = jobs.read_jobs(n)[0]
        ledger, handback = n['dir'] / jobs.JOBS_FILE, jobs.handback_file(n, row)
        expected = ledger.read_bytes(), handback.read_bytes()
        self.assertEqual(events.record_read(inst, n, 'jobs').encode(), expected[0])
        self.assertEqual(events.record_read(inst, n, 'handback', jobs.job_key(row)).encode(), expected[1])
        ledger.unlink(); handback.unlink()
        self.b(root, 'events', 'app', '--rebuild-records')
        self.assertEqual((ledger.read_bytes(), handback.read_bytes()), expected)
        journal = (root / '.brain/events.jsonl').read_text()
        self.assertNotIn('PRIVATE-SUMMARY', journal)
        self.assertNotIn('PRIVATE-LESSON', journal)

    def test_crash_after_event_before_file_is_recovered(self):
        root = self.init('demo')
        inst = instance.Instance(root)
        n = inst.node('root')
        path = n['dir'] / jobs.JOBS_FILE
        original = events.common.DirectoryTarget.atomic_write
        def fail_projection(target, text, **kwargs):
            if target.path == path:
                raise OSError('simulated interruption')
            return original(target, text, **kwargs)
        with mock.patch.object(events.common.DirectoryTarget, 'atomic_write', autospec=True, side_effect=fail_projection):
            with self.assertRaises(OSError):
                events.write_record(inst, n, 'jobs', 'ledger', 'durable record\n')
        self.assertFalse(path.exists())
        with instance.brain_lock(inst):
            events.recover_records(inst)
        self.assertEqual(path.read_text(), 'durable record\n')
        self.assertTrue(all(e['applied'] for e in events.journal(inst).state['records'].values()))

    def test_recovery_preserves_independent_changes_and_objects_are_verified(self):
        root = self.init('demo')
        inst = instance.Instance(root)
        n = inst.node('root')
        path = n['dir'] / jobs.JOBS_FILE
        events.write_record(inst, n, 'jobs', 'ledger', 'recorded\n')
        path.write_text('independent edit\n')
        with instance.brain_lock(inst), self.assertRaises(ValueError):
            events.recover_records(inst, n, rebuild=True)
        self.assertEqual(path.read_text(), 'independent edit\n')
        entry = next(iter(events.journal(inst).state['records'].values()))
        obj = root / '.brain/event-objects' / (entry['object'] + '.json')
        obj.write_text('{}')
        with self.assertRaises(ValueError):
            events.record_read(inst, n, 'jobs')

    def test_existing_records_backfill_with_reconstructed_origin(self):
        root = self.init('demo')
        inst = instance.Instance(root)
        n = inst.node('root')
        path = n['dir'] / jobs.JOBS_FILE
        path.write_text('# Jobs\n' + jobs.JOBS_MARK + '\n')
        with instance.brain_lock(inst):
            events.backfill_records(inst)
        rows = [e for e in events.read(inst) if e['kind'] == 'record']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['data']['origin'], 'reconstructed')
        self.assertEqual(events.record_read(inst, n, 'jobs'), path.read_text())
        with instance.brain_lock(inst):
            events.backfill_records(inst)
        self.assertEqual(len([e for e in events.read(inst) if e['kind'] == 'record']), 1)

    def test_malformed_record_identity_is_quarantined_before_native_write(self):
        root = self.init('demo')
        path = root / '.brain/events.jsonl'
        bad = {'ts': '2025-01-01 00:00 +00:00', 'node': events.node_ref('root'), 'kind': 'record',
               'actor': 'brain', 'ref': 'a' * 32, 'data': {'record_type': 'handback', 'key': '../../foreign',
               'object': 'b' * 64, 'after': 'c' * 64, 'before': None, 'origin': 'observed'}}
        with path.open('a') as stream:
            stream.write(json.dumps(bad) + '\n')
        self.b(root, 'backlog', 'root', 'add', 'safe new item')
        self.assertTrue(list((root / '.brain').glob('events.rejected.*')))
        self.assertNotIn('../../foreign', path.read_text())
        self.assertIn('safe new item', (root / 'backlog.md').read_text())
