"""Native direct-full receipts can satisfy readiness once, without rerunning."""
import json
from pathlib import Path

import test_train
from scenario import Scenario


class FullGateReceipt(Scenario):
    project = test_train.MergeTrain.project
    job = test_train.MergeTrain.job

    def test_direct_full_pass_is_adopted_without_another_execution(self):
        root, _ = self.project()
        self.job('one')
        self.b(root, 'full-run', 'a', '--branch', 'one', '--reviews', '1')
        receipt = next((root / 'nodes/a/full-gates').glob('*.json'))
        before = len(list((root / 'nodes/a/job-handbacks').glob('*-verify-*.log')))
        self.b(root, 'handback', 'a', '--branch', 'one', '--state', 'done', '--reviews', '1',
               '--summary', 'Adopted full gate', '--full-receipt', receipt.stem)
        self.assertEqual(len(list((root / 'nodes/a/job-handbacks').glob('*-verify-*.log'))), before)
        signals = [json.loads(p.read_text()) for p in (root / 'nodes/a/job-handbacks').glob('*.json')]
        self.assertTrue(any(s.get('ready', {}).get('external_full_receipt') == receipt.stem for s in signals))

    def test_stale_full_receipt_refuses_without_a_second_gate(self):
        root, _ = self.project()
        self.job('one')
        self.b(root, 'full-run', 'a', '--branch', 'one', '--reviews', '1')
        receipt = next((root / 'nodes/a/full-gates').glob('*.json'))
        self.b(root, 'set', 'a', 'verify=false')
        out = self.b(root, 'handback', 'a', '--branch', 'one', '--state', 'done', '--reviews', '1',
                     '--summary', 'Stale gate', '--full-receipt', receipt.stem, ok=False)
        self.assertIn('full receipt', out)

    def test_invalid_handback_options_do_not_start_attempt_or_verify(self):
        root, _ = self.project()
        self.job('one')
        before = {p.name: p.read_bytes() for p in (root / 'nodes/a/job-attempts').glob('*.json')}
        self.b(root, 'handback', 'a', '--branch', 'one', '--state', 'done', '--summary', 'Invalid request',
               '--reviews', '0', '--reverify', ok=False)
        self.assertEqual({p.name: p.read_bytes() for p in (root / 'nodes/a/job-attempts').glob('*.json')}, before)
