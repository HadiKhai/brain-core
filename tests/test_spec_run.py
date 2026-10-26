"""Focused command execution never manufactures full readiness."""
import json
from pathlib import Path

import test_train
from scenario import Scenario


class SpecRun(Scenario):
    project = test_train.MergeTrain.project
    job = test_train.MergeTrain.job

    def test_command_argv_and_receipt_are_focused_only(self):
        root, home = self.project()
        self.job('one')
        folder = root / 'nodes/a/job-handbacks'
        before = {p.name: p.read_bytes() for p in folder.glob('*.json')}
        out = self.b(root, 'spec-run', 'a', '--branch', 'one', '--command',
                     'python3', '-c', 'import sys; print(sys.argv[1])', 'literal; echo no')
        receipt = json.loads(next((root / 'nodes/a/spec-runs').glob('*.json')).read_text())
        self.assertEqual(Path(receipt['log']).read_text(), 'literal; echo no\n')
        self.assertEqual(receipt['scope'], 'focused')
        self.assertEqual(receipt['result'], 0)
        self.assertEqual(receipt['head'], self.git('rev-parse', 'one', cwd=home).strip())
        self.assertNotIn('ready', receipt)
        self.assertEqual({p.name: p.read_bytes() for p in folder.glob('*.json')}, before)

    def test_failing_command_preserves_nonzero_and_log(self):
        root, _ = self.project()
        self.job('one')
        self.b(root, 'spec-run', 'a', '--branch', 'one', '--command',
               'python3', '-c', 'raise SystemExit(7)', ok=False)
        receipt = json.loads(next((root / 'nodes/a/spec-runs').glob('*.json')).read_text())
        self.assertEqual(receipt['result'], 7)
        self.assertTrue(Path(receipt['log']).is_file())

    def test_missing_command_refuses_before_a_receipt(self):
        root, _ = self.project()
        self.job('one')
        self.b(root, 'spec-run', 'a', '--branch', 'one', '--command', ok=False)
        self.assertFalse((root / 'nodes/a/spec-runs').exists())
