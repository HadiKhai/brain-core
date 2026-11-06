"""Synthetic local schedules and gaps; no host calls or operator data."""
import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import core_loader

CORE = Path(__file__).resolve().parent.parent


class InstructionCaptureRepair(unittest.TestCase):
    def setUp(self):
        m = core_loader.SourceFileLoader('capture_repair_fixture', str(CORE / 'bin/brain')).load_module()
        self.repair = importlib.import_module(m._owners['capture_stop'].__package__ + '.instruction_capture_repair')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / '.brain').mkdir()
        self.local = {'schedules': {'sample-tick': 'task-tick', 'disabled-tick': 'task-disabled'},
                      'nodes': {'root': {'wake': 'https://example.invalid/hooks/scheduled-task%3Aroot-wake/secret'},
                                'sample': {'wake': 'https://example.invalid/hooks/scheduled-task%3Asample-wake/secret'}}}
        self.save_local()
        self.tasks = [{'scheduledTaskId': sid, 'prompt': prompt, 'enabled': enabled} for sid, prompt, enabled in (
            ('task-tick', 'Run the recurring fixture.\nKeep its exact whitespace.  ', True),
            ('scheduled-task:root-wake', 'Role: root fixture.\nRead current work.', True),
            ('scheduled-task:sample-wake', self.repair.AUTOMATED_TURN_PREFIX + '\nAlready marked.', True),
            ('task-disabled', 'A disabled historical fixture.', False),
            ('unrecorded-task', 'An unrelated host task.', True))]
        self.gaps = self.root / '.brain/instruction-capture/gaps'
        self.gaps.mkdir(parents=True)
        self.inbox = self.root / 'inbox.md'
        self.inbox.write_text('Retain all original gap inbox lines.\n')

    def save_local(self):
        (self.root / '.brain/local.json').write_text(json.dumps(self.local))

    def gap(self, name, *quotes):
        path = self.gaps / (name + '.json')
        path.write_text(json.dumps({'kind': 'instruction-capture-gap', 'code': 'second_stop_unconverted',
                                   'items': [{'kind': 'standing_instruction', 'quote': q} for q in quotes]}))
        return path

    def test_plan_joins_all_recorded_nodes_and_preserves_exact_prompt_bodies(self):
        plan = self.repair.marker_plan(self.root, {'tasks': self.tasks})
        self.assertTrue(plan['complete'])
        self.assertEqual(len(plan['tasks']), 4)
        self.assertEqual({r['scheduledTaskId'] for r in plan['updates']}, {'task-tick', 'scheduled-task:root-wake'})
        originals = {r['scheduledTaskId']: r['prompt'] for r in self.tasks}
        for row in plan['updates']:
            self.assertEqual(row['prompt'], self.repair.AUTOMATED_TURN_PREFIX + '\n' + originals[row['scheduledTaskId']])
        self.assertNotIn('secret', json.dumps(plan))

    def test_plan_reports_missing_binding_and_handles_provisioned_wake_ids(self):
        self.local['host_wake_tasks'] = {'bound-thread': 'task-other-wake'}
        self.save_local()
        plan = self.repair.marker_plan(self.root, self.tasks)
        self.assertFalse(plan['complete'])
        self.assertEqual(plan['missing'], ['task-other-wake'])
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            self.repair.marker_plan(self.root, self.tasks + [self.tasks[0]])

    def test_reconciliation_is_exact_whole_gap_dry_run_and_idempotent(self):
        matched = self.gap('scheduled', 'Run the recurring fixture.', 'Keep its exact whitespace.  ')
        disabled = self.gap('historical', 'A disabled historical fixture.')
        retired = self.gap('retired', 'An unrelated host task.')
        self.gap('mixed', 'Run the recurring fixture.', 'Always retain operator instructions.')
        self.gap('split', 'Run the recurring fixture.', 'Read current work.')
        self.gap('paraphrase', 'run the recurring fixture.')
        self.gap('empty', '')
        before = {p: p.read_bytes() for p in self.gaps.iterdir()}
        inbox = self.inbox.read_bytes()
        result = self.repair.reconcile_gaps(self.root, self.tasks)
        self.assertEqual({r['gap'] for r in result['reconciliations']}, {matched.name, disabled.name, retired.name})
        self.assertFalse((self.gaps.parent / 'reconciliations').exists())
        result = self.repair.reconcile_gaps(self.root, self.tasks, dry_run=False)
        self.assertTrue(result['complete'])
        for row in result['reconciliations']:
            self.assertEqual(row['disposition'], 'non_operator')
            self.assertEqual(row['reason'], self.repair.REASON)
            self.assertIn('+00:00', row['timestamp'])
            saved = json.loads((self.gaps.parent / 'reconciliations' / row['gap']).read_text())
            self.assertEqual(saved, row)
        self.assertEqual(self.repair.reconcile_gaps(self.root, self.tasks, dry_run=False)['reconciliations'], [])
        self.assertEqual(before, {p: p.read_bytes() for p in self.gaps.iterdir()})
        self.assertEqual(self.inbox.read_bytes(), inbox)
        changed = json.loads(matched.read_text())
        changed['extra'] = 'new evidence'
        matched.write_text(json.dumps(changed))
        result = self.repair.reconcile_gaps(self.root, self.tasks, dry_run=False)
        self.assertFalse(result['complete'])
        self.assertIn('does not match', result['warnings'][0]['error'])

    def test_bad_gap_is_reported_while_other_gaps_continue(self):
        (self.gaps / 'bad.json').write_text('{')
        self.gap('good', 'Read current work.')
        result = self.repair.reconcile_gaps(self.root, self.tasks)
        self.assertEqual(len(result['warnings']), 1)
        self.assertEqual(len(result['reconciliations']), 1)
        self.assertFalse(result['complete'])

    def test_cli_fixture_plan_and_dry_run_are_local_and_private(self):
        tasks = self.root / 'tasks.json'
        tasks.write_text(json.dumps({'tasks': self.tasks}))
        plan = self.root / 'plan.json'
        args = [sys.executable, '-B', '-m', 'brain.instruction_capture_repair']
        options = ['--root', str(self.root), '--tasks-json', str(tasks)]
        env = {**os.environ, 'PYTHONPATH': str(CORE / 'lib')}
        run = subprocess.run(args + ['plan'] + options + ['--output', str(plan)],
                             env=env, text=True, capture_output=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(len(json.loads(plan.read_text())['updates']), 2)
        self.assertEqual(plan.stat().st_mode & 0o777, 0o600)
        self.gap('candidate', 'Read current work.')
        run = subprocess.run(args + ['reconcile'] + options + ['--dry-run'],
                             env=env, text=True, capture_output=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)['reconciliations'][0]['gap'], 'candidate.json')
        self.assertFalse((self.gaps.parent / 'reconciliations').exists())
        self.local['schedules']['missing'] = 'missing-task'
        self.save_local()
        run = subprocess.run(args + ['plan'] + options + ['--output', str(plan)],
                             env=env, text=True, capture_output=True, timeout=10)
        self.assertEqual(run.returncode, 1, run.stderr)
        self.assertEqual(json.loads(plan.read_text())['missing'], ['missing-task'])
