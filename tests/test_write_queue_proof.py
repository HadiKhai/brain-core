"""Read-only proof analysis, c50 receipt identity and bounded workload contracts."""
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

TOOLS = Path(__file__).resolve().parent.parent / 'tools'


def load_tool(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    # Keep standalone tool imports local; no core loader or live instance discovery.
    with mock.patch.object(sys, 'path', [str(TOOLS), *sys.path]):
        spec.loader.exec_module(module)
    return module


class WriteQueueProof(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Inventory fixtures import tests without copying the standalone tools.
        # Execution still requires the real tool; collection requires only tests.
        global proof
        proof = load_tool('write_queue_proof')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.events = self.root / 'events.jsonl'
        self.queue = self.root / 'queue'
        self.queue.mkdir()
        (self.queue / 'index').write_text('')
        self.start = proof.timestamp('2026-10-28T01:00:00+02:00')
        self.end = proof.timestamp('2026-10-28T02:00:00+02:00')
        self.ident = 'a' * 32

    def analyze(self, rows):
        self.events.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        return proof.analyze([self.events], self.queue, self.start, self.end,
                             now=self.end)

    def event(self, command='task set', kind='timing', **kwargs):
        return dict(ts='2026-10-28T01:20:00+02:00', kind=kind,
                    data=dict(command=command, elapsed_s=.1, success=True, **kwargs))

    def request(self, ident=None, receipt=True, state='applied'):
        ident = ident or self.ident
        payload = dict(kind='task', node='fixture', task='proof', changes={'title': 'sample'})
        digest = proof.payload_hash(payload)
        entry = dict(id=ident, hash=digest)
        index = self.queue / 'index'
        sequence = index.stat().st_size
        with index.open('a') as stream:
            stream.write(json.dumps(entry) + '\n')
        (self.queue / (ident + '.request')).write_text(json.dumps(dict(version=2, **entry, payload=payload)))
        if receipt:
            (self.queue / (ident + '.receipt')).write_text(json.dumps(dict(**entry, sequence=sequence, state=state)))
        return entry

    def test_percentiles_do_not_hide_a_single_slow_caller(self):
        rows = [self.event(kind='proof_call', exit_code=0, duration_scope='launch_to_exit') for _ in range(100)]
        rows[-1]['data']['elapsed_s'] = 1
        stats = self.analyze(rows)['families']['task set']
        self.assertEqual((stats['p95_s'], stats['max_s'], stats['at_least_1s']), (.1, 1, 1))
        self.assertEqual(stats['caller_latency'], 'FAIL')

    def test_internal_timings_and_legacy_labels_never_prove_whole_process_or_bypass(self):
        report = self.analyze([self.event('task'), self.event('handback'), self.event('inbox'), self.event('status')])
        self.assertEqual(report['observed_commands']['task']['count'], 1)
        for stats in report['families'].values():
            self.assertEqual(stats['caller_latency'], 'UNKNOWN')
            self.assertEqual(stats['lock_evidence'], 'UNKNOWN')
        self.assertEqual(report['refresh']['gate'], 'UNKNOWN')

    def test_every_call_needs_matching_lock_attempt_evidence(self):
        rows = [self.event(request_id=self.ident, lock_attempts=0, lock_acquisitions=0),
                self.event(kind='proof_call', request_id='b' * 32, exit_code=0, duration_scope='launch_to_exit')]
        self.assertEqual(self.analyze(rows)['families']['task set']['lock_evidence'], 'UNKNOWN')
        rows[1]['data']['request_id'] = self.ident
        self.assertEqual(self.analyze(rows)['families']['task set']['lock_evidence'], 'PASS')
        rows[0]['data']['lock_attempts'] = 1
        self.assertEqual(self.analyze(rows)['families']['task set']['lock_evidence'], 'FAIL')

    def test_zero_wait_is_not_zero_acquisitions(self):
        report = self.analyze([self.event(lock_attempts=1, lock_acquisitions=1, lock_wait_s=0)])
        self.assertEqual(report['families']['task set']['lock_evidence'], 'FAIL')

    def test_global_lock_failures_include_unmigrated_commands(self):
        row = self.event('wt', lock_wait_failure=True)
        row['data']['success'] = False
        self.assertEqual(self.analyze([row])['global_lock_failures']['confirmed'], 1)
        self.assertEqual(self.analyze([row])['global_lock_failures']['gate'], 'FAIL')

    def test_global_failures_include_unmatched_callers_and_deduplicate_exact_matches(self):
        row = self.event(kind='proof_call', request_id=self.ident, exit_code=1, lock_wait_failure=True)
        row['data']['success'] = False
        report = self.analyze([self.event('show'), row])['global_lock_failures']
        self.assertEqual((report['confirmed'], report['gate']), (1, 'FAIL'))
        telemetry = self.event(request_id=self.ident, lock_wait_failure=True)
        telemetry['data']['success'] = False
        self.assertEqual(self.analyze([telemetry, row])['global_lock_failures']['confirmed'], 1)
        del row['data']['lock_wait_failure']
        report = self.analyze([self.event('show'), row])['global_lock_failures']
        self.assertEqual((report['unknown_causes'], report['gate']), (1, 'UNKNOWN'))

    def test_retry_calls_need_one_timing_per_invocation(self):
        call = self.event(kind='proof_call', request_id=self.ident, exit_code=0)
        timing = self.event(request_id=self.ident, lock_attempts=0, lock_acquisitions=0)
        report = self.analyze([call, call, timing])
        self.assertEqual(report['families']['task set']['lock_evidence'], 'UNKNOWN')
        report = self.analyze([call, call, timing, timing])
        self.assertEqual(report['families']['task set']['lock_evidence'], 'PASS')

    def test_ambiguous_durable_acceptance_is_reconciled_despite_timeout(self):
        self.request(receipt=False)
        row = self.event(kind='proof_call', request_id=self.ident, exit_code=-1,
                         acceptance_echo=False, cause='TimeoutExpired')
        row['data']['success'] = False
        q = self.analyze([row])['queue']
        self.assertEqual(q['accepted'], 1)
        self.assertEqual(q['ambiguous_accepted'], [self.ident])
        self.assertEqual(q['missing_receipts'], [self.ident])
        self.assertEqual(q['reconciliation'], 'FAIL')
        receipt = dict(id=self.ident, hash=json.loads((self.queue / (self.ident + '.request')).read_text())['hash'],
                       sequence=0, state='rejected', reason='conflict')
        (self.queue / (self.ident + '.receipt')).write_text(json.dumps(receipt))
        q = self.analyze([row])['queue']
        self.assertEqual(q['rejected'], {self.ident: 'conflict'})
        self.assertEqual(q['reconciliation'], 'FAIL')
        receipt['state'] = 'applied'
        (self.queue / (self.ident + '.receipt')).write_text(json.dumps(receipt))
        self.assertEqual(self.analyze([row])['queue']['reconciliation'], 'PASS')

    def test_start_without_completion_can_still_have_durable_acceptance(self):
        self.request(receipt=False)
        row = self.event(kind='proof_start', request_id=self.ident, call_id='pending')
        report = self.analyze([row])
        self.assertEqual(report['queue']['accepted'], 1)
        self.assertEqual(report['queue']['reconciliation'], 'FAIL')
        self.assertEqual(report['unfinished_calls'], ['pending'])

    def test_failure_with_wait_is_suspected_not_invented_as_confirmed(self):
        row = self.event('handback', lock_wait_s=300)
        row['data']['success'] = False
        report = self.analyze([row])['global_lock_failures']
        self.assertEqual((report['confirmed'], report['unknown_causes'], report['failures_with_wait']), (0, 1, 1))
        self.assertEqual(report['gate'], 'UNKNOWN')

    def test_half_open_window_and_explicit_timezone(self):
        rows = [self.event() for _ in range(3)]
        for row, at in zip(rows, [self.start, self.end, self.start.replace(hour=0)]):
            row['ts'] = at.isoformat()
        self.assertEqual(self.analyze(rows)['observed_commands']['task set']['count'], 1)
        with self.assertRaises(ValueError):
            proof.timestamp('2026-10-28T01:00:00')

    def test_future_end_and_bad_lines_are_incomplete(self):
        self.events.write_text('{broken\n')
        report = proof.analyze([self.events], self.queue, self.start, self.end, now=self.start)
        self.assertFalse(report['window']['complete'])
        self.assertEqual(report['evidence'], 'INCOMPLETE')
        self.assertEqual(len(report['errors']), 1)

    def test_nonfinite_or_missing_latency_cannot_pass(self):
        for value in [float('nan'), float('inf'), -1, True, None]:
            row = self.event(kind='proof_call', duration_scope='launch_to_exit', exit_code=0)
            row['data']['elapsed_s'] = value
            self.assertEqual(self.analyze([row])['families']['task set']['caller_latency'], 'UNKNOWN')

    def test_missing_and_duplicate_finished_calls_remain_visible(self):
        start = self.event(kind='proof_start', call_id='x')
        self.assertEqual(self.analyze([start])['unfinished_calls'], ['x'])
        done = self.event(kind='proof_call', call_id='x')
        self.assertEqual(self.analyze([start, done, done])['unfinished_calls'], ['x'])

    def test_success_without_queue_acceptance_is_not_receipt_proof(self):
        row = self.event(kind='proof_call', acceptance_echo=False, request_id=self.ident)
        report = self.analyze([row])
        self.assertEqual(report['queue']['accepted'], 0)
        self.assertEqual(report['unbound_acceptances'], 1)

    def test_retry_reconciles_once_at_first_byte_offset(self):
        entry = self.request()
        with (self.queue / 'index').open('a') as stream:
            stream.write(json.dumps(entry) + '\n')
        q = proof.queue_snapshot(self.queue, {self.ident})
        self.assertEqual((q['indexed'], q['reconciled'], q['pending']), (1, 1, 0))
        self.assertEqual(q['reconciliation'], 'PASS')

    def test_pending_backlog_and_unknown_age_are_explicit(self):
        self.request(receipt=False)
        q = proof.queue_snapshot(self.queue, {self.ident})
        self.assertEqual((q['pending'], q['missing_receipts']), (1, [self.ident]))
        self.assertIsNone(q['oldest_age_s'])
        self.assertEqual(q['reconciliation'], 'FAIL')

    def test_later_receipt_with_pending_predecessor_is_not_ordered_application(self):
        self.request(receipt=False)
        self.request(ident='b' * 32)
        q = proof.queue_snapshot(self.queue, {'b' * 32})
        self.assertIn('pending predecessor', q['errors'][0])
        self.assertEqual(q['reconciliation'], 'FAIL')

    def test_hash_sequence_duplicate_and_orphan_receipts_fail(self):
        self.request()
        path = self.queue / (self.ident + '.receipt')
        original = json.loads(path.read_text())
        for field, value in [('hash', 'changed'), ('sequence', 1), ('sequence', False), ('state', 'queued')]:
            path.write_text(json.dumps({**original, field: value}))
            self.assertTrue(proof.queue_snapshot(self.queue, {self.ident})['errors'])
        path.write_text(json.dumps(original))
        (self.queue / ('b' * 32 + '.receipt')).write_text(json.dumps(original))
        self.assertTrue(proof.queue_snapshot(self.queue, {self.ident})['errors'])

    def test_payload_tampering_and_truncated_index_fail(self):
        self.request()
        path = self.queue / (self.ident + '.request')
        request = json.loads(path.read_text())
        request['payload']['task'] = 'other'
        path.write_text(json.dumps(request))
        self.assertTrue(proof.queue_snapshot(self.queue, {self.ident})['errors'])
        (self.queue / 'index').write_text('{')
        self.assertTrue(proof.queue_snapshot(self.queue, {self.ident})['errors'])

    def test_rejections_remain_separate_and_empty_samples_unknown(self):
        self.request(state='rejected')
        q = proof.queue_snapshot(self.queue, {self.ident})
        self.assertEqual(q['rejected'], {self.ident: 'unknown'})
        self.assertEqual(q['reconciliation'], 'FAIL')
        self.assertEqual(proof.queue_snapshot(self.queue, set())['reconciliation'], 'UNKNOWN')

    def test_analyzer_does_not_mutate_inputs(self):
        self.request()
        self.events.write_text(json.dumps(self.event()) + '\n')
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob('*') if p.is_file()}
        proof.analyze([self.events], self.queue, self.start, self.end, now=self.end)
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before})

    def test_refresh_outcomes_and_failure_causes_are_reported(self):
        row = self.event('status --refresh', kind='proof_call', exit_code=4, cause='refresh failed')
        row['data']['success'] = False
        report = self.analyze([row])
        self.assertEqual(report['refresh']['gate'], 'FAIL')
        self.assertEqual(report['refresh']['causes'], {'refresh failed': 1})

    def test_successful_caller_refresh_never_hides_unmatched_internal_failure(self):
        failed = self.event('status --refresh', call_id='failed', cause='refresh failed')
        failed['data']['success'] = False
        passed = self.event('status --refresh', kind='proof_call', call_id='passed', exit_code=0)
        report = self.analyze([failed, passed])['refresh']
        self.assertEqual((report['count'], report['nonzero_exits'], report['gate']), (2, 1, 'FAIL'))

    def test_matched_refresh_is_counted_once_and_preserves_worst_outcome(self):
        internal = self.event('status --refresh', call_id='same')
        caller = self.event('status --refresh', kind='proof_call', call_id='same', exit_code=0)
        report = self.analyze([internal, caller])['refresh']
        self.assertEqual((report['count'], report['gate']), (1, 'PASS'))
        internal['data']['success'] = False
        report = self.analyze([internal, caller])['refresh']
        self.assertEqual((report['count'], report['nonzero_exits'], report['gate']), (1, 1, 'FAIL'))
        internal['data']['success'] = None
        self.assertEqual(self.analyze([internal, caller])['refresh']['gate'], 'UNKNOWN')


class WriteQueueDriver(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global driver
        driver = load_tool('write_queue_driver')

    def test_execute_journals_only_its_n_submissions_and_refuses_existing_output(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            node = root / 'nodes/brain-core/node.md'
            node.parent.mkdir(parents=True)
            node.write_text('fixture')
            launcher = root / 'brain'
            launcher.write_text('#!' + sys.executable + '\nimport sys\n'
                                'a=sys.argv\nprint(a[a.index("--request-id")+1] if "--request-id" in a else "ok")\n')
            launcher.chmod(0o700)
            output = root / 'calls.jsonl'
            args = ['--root', str(root), '--brain', str(launcher), '-n', '4',
                    '--output', str(output), '--execute']
            self.assertEqual(driver.main(args), 0)
            rows = [json.loads(line) for line in output.read_text().splitlines()]
            calls = [r['data'] for r in rows if r['kind'] == 'proof_call']
            self.assertEqual(len(calls), 6)
            self.assertEqual(sum(c['acceptance_echo'] for c in calls), 4)
            self.assertEqual(calls[0]['command'], 'task add')
            self.assertEqual(calls[-1]['command'], 'status --refresh')
            with self.assertRaises(FileExistsError):
                driver.main(args)
            self.assertEqual(node.read_text(), 'fixture')

    def test_plan_only_touches_marked_inbox_and_unique_dedicated_task(self):
        task, operations = driver.plan(6, 'abc')
        self.assertTrue(task.startswith('TASK-WRITE-QUEUE-PROOF-'))
        self.assertEqual(len(operations), 6)
        for args, data in operations:
            self.assertIn('brain-core', args)
            self.assertNotIn('handback', args)
            self.assertIn('--request-id', args)
            self.assertEqual(len(data['request_id']), 32)
            if args[0] == 'task':
                self.assertIn(task, args)
                self.assertTrue(args[4].startswith('next_action=write-queue proof'))

    def test_default_is_a_plan_without_subprocess_or_journal(self):
        with mock.patch.object(driver.subprocess, 'run') as run, mock.patch('builtins.print') as output:
            self.assertEqual(driver.main(['--root', '/unused', '--brain', '/unused/brain']), 0)
        run.assert_not_called()
        self.assertIn('operations', json.loads(output.call_args.args[0]))

    def test_launch_to_exit_failure_and_timeout_are_retained(self):
        rows = []
        result = subprocess.CompletedProcess([], 7, '', 'lock unavailable')
        with mock.patch.object(driver.subprocess, 'run', return_value=result):
            record = driver.invoke(Path('/fixture/brain'), Path('/fixture'), [],
                                   dict(command='inbox add', request_id='a' * 32), rows.append, 1)
        self.assertEqual(record['exit_code'], 7)
        self.assertEqual(record['cause'], 'lock unavailable')
        self.assertFalse(record['acceptance_echo'])
        self.assertEqual([r['kind'] for r in rows], ['proof_start', 'proof_call'])
        with mock.patch.object(driver.subprocess, 'run', side_effect=subprocess.TimeoutExpired([], 1)):
            record = driver.invoke(Path('/fixture/brain'), Path('/fixture'), [], {}, rows.append, 1)
        self.assertEqual(record['exit_code'], -1)
        self.assertEqual(record['cause'], 'TimeoutExpired')

    def test_launch_environment_does_not_inherit_instance_lock(self):
        with mock.patch.dict(driver.os.environ, {'BRAIN_LOCK_HELD': '1'}), \
                mock.patch.object(driver.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'a' * 32, '')) as run:
            result = driver.invoke(Path('/fixture/brain'), Path('/fixture'), [],
                                   dict(request_id='a' * 32), lambda row: None, 1)
        self.assertNotIn('BRAIN_LOCK_HELD', run.call_args.kwargs['env'])
        self.assertTrue(result['acceptance_echo'])
