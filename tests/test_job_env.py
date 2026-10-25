"""Per-job overrides are local, fingerprinted and never edit shared env files."""
import json

import test_train
from scenario import Scenario


class JobEnvironment(Scenario):
    project = test_train.MergeTrain.project
    job = test_train.MergeTrain.job

    def test_override_and_unset_reach_only_this_job(self):
        root, home = self.project()
        self.job('one')
        self.job('two')
        self.b(root, 'wt', 'env', 'a', 'one', 'FEATURE=fictional', '--unset', 'UNWANTED')
        self.b(root, 'spec-run', 'a', '--branch', 'one', '--command', 'python3', '-c',
               'import os; assert os.environ["FEATURE"] == "fictional"; assert "UNWANTED" not in os.environ')
        self.b(root, 'spec-run', 'a', '--branch', 'two', '--command', 'python3', '-c',
               'import os; assert \"FEATURE\" not in os.environ')
        data = json.loads((root / '.brain/local.json').read_text())
        self.assertNotIn('env', str(data.get('nodes', {}).get('a', {}).get('worktrees', {}).get('two', {})))
        self.assertEqual(self.git('status', '--porcelain', cwd=home), '')

    def test_reserved_overrides_refuse_before_local_write(self):
        root, _ = self.project()
        self.job('one')
        before = (root / '.brain/local.json').read_bytes()
        self.b(root, 'wt', 'env', 'a', 'one', 'BRAIN_ROOT=somewhere', ok=False)
        self.assertEqual((root / '.brain/local.json').read_bytes(), before)
