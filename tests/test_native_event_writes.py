"""Short native writers journal and refresh state without a coordinator turn."""
import argparse
import json
import sys
from scenario import Scenario, CORE
sys.path.insert(0, str(CORE / 'lib'))
from brain import events, instance, transitions, jobs, job_attempts, handback_queue


class NativeEventWrites(Scenario):
    def prepare(self):
        root = self.init('demo')
        home = self.repo(self.tmp / 'code')
        self.b(root, 'new', 'app', '--title', 'App', '--home', str(home))
        self.b(root, 'wt', 'add', 'app', 'job-one', '--no-setup')
        inst = instance.Instance(root)
        n = inst.node('app')
        return root, inst, n, jobs.read_jobs(n)[0]

    def test_direct_inbox_and_attempt_and_queue_writes_have_terminal_events(self):
        root, inst, n, row = self.prepare()
        transitions.inbox_append(n['dir'], '- request: PRIVATE-INBOX\n')
        attempt = job_attempts.begin_attempt(inst, n, row)
        request = {'id': 'b' * 32, 'state': 'queued', 'summary': 'PRIVATE-SUMMARY'}
        handback_queue.write_queue_request(n, request)
        records = events.read(inst)
        commits = [e for e in records if e['kind'] == 'commit']
        commands = {e['data']['command'] for e in commits}
        self.assertTrue({'inbox', 'job_attempt', 'handback_queue'} <= commands)
        self.assertEqual(events.pending(records), {})
        self.assertNotIn('PRIVATE-INBOX', (root / '.brain/events.jsonl').read_text())
        self.assertNotIn('PRIVATE-SUMMARY', (root / '.brain/events.jsonl').read_text())
        self.assertIn('Facts refreshed:', (n['dir'] / 'status.md').read_text())

    def test_nested_native_write_remains_in_one_command_operation(self):
        _, inst, n, _ = self.prepare()
        before = len([e for e in events.read(inst) if e['kind'] == 'commit'])
        with instance.brain_lock(inst):
            with events.Command(inst.root, command='parent', node=n['id']):
                (n['dir'] / 'decisions.md').write_text('parent decision\n')
                transitions.inbox_append(n['dir'], '- request: child\n')
        commits = [e for e in events.read(inst) if e['kind'] == 'commit']
        self.assertEqual(len(commits), before + 1)
        paths = {r.get('path') for r in commits[-1]['data']['resources']}
        self.assertIn('nodes/app/decisions.md', paths)
        self.assertIn('nodes/app/inbox.md', paths)
        self.assertEqual(events.pending(events.read(inst)), {})

    def test_native_attempt_transitions_supply_observed_phase_facts_once(self):
        _, inst, n, row = self.prepare()
        attempt = job_attempts.begin_attempt(inst, n, row)
        job_attempts.update_attempt(inst, n, row, attempt['id'], 'verifying')
        job_attempts.update_attempt(inst, n, row, attempt['id'], 'verifying')
        job_attempts.finish_attempt(inst, n, row, attempt['id'])
        rows = [e['data'] for e in events.facts(inst, n, jobs.job_key(row))
                if e['data']['fact_type'] == 'phase_changed']
        self.assertEqual([(d['phase'], d['state']) for d in rows],
                         [('admission', 'waiting'), ('execution', 'working'), ('coordination', 'waiting')])
        self.assertEqual([d['reason_code'] for d in rows],
                         ['attempt_queued', 'attempt_verifying', 'attempt_terminal'])
        self.assertTrue(all(d['origin'] == 'observed' and 'at' in d for d in rows))
        self.assertTrue(all(set(d) <= {'version', 'fact_type', 'job_key', 'origin', 'at',
                                      'phase', 'state', 'reason_code', 'head', 'base', 'inputs'} for d in rows))
