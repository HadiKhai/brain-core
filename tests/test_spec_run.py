"""Focused command execution never manufactures full readiness."""
import argparse
import contextlib
import json
from pathlib import Path
import shlex
import sys
from unittest import mock

import test_train
from scenario import CORE, Scenario


class SpecRun(Scenario):
    project = test_train.MergeTrain.project
    job = test_train.MergeTrain.job

    def focused_project(self, body=None):
        root, home = self.project()
        script = ("import json, os, sys, tempfile\n"
                  "print(json.dumps(dict(args=sys.argv[1:], temporary=tempfile.gettempdir(), "
                  "inherited=os.environ.get('BRAIN_TEST_RUN_ROOT'))))\n")
        wt = self.job('one', {'focused.py': body or script})
        self.b(root, 'set', 'a', 'verify_spec=' + shlex.join([sys.executable, '-B', 'focused.py']))
        head = self.git('rev-parse', 'HEAD', cwd=wt).strip()
        return root, wt, head

    def focused(self, root, wt, head, *specs, ok=True):
        with mock.patch.dict(self.env, BRAIN_ROOT=str(root)):
            return self.run_(['spec-run', 'a', '--head', head, '--', *specs],
                             cwd=wt, brain=root / 'core/bin/brain', ok=ok)

    def test_canonical_ids_infer_job_and_remove_owned_temporary_directory(self):
        root, wt, head = self.focused_project()
        folder = root / 'nodes/a/job-handbacks'
        before = {p.name: p.read_bytes() for p in folder.glob('*.json')}
        self.focused(root, wt, head, 'sample.py::Case::test_one', 'literal; echo no')
        path = next((root / 'nodes/a/spec-runs').glob('*.json'))
        receipt = json.loads(path.read_text())
        output = json.loads(Path(receipt['log']).read_text())
        self.assertEqual(output['args'], receipt['specs'])
        self.assertEqual(receipt['specs'], ['sample.py::Case::test_one', 'literal; echo no'])
        self.assertEqual(receipt['head'], head)
        self.assertEqual(receipt['scope'], 'focused')
        self.assertIsNone(output['inherited'])
        self.assertFalse(Path(output['temporary']).exists())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual({p.name: p.read_bytes() for p in folder.glob('*.json')}, before)

    def test_canonical_failure_keeps_status_and_cleans_temporary_directory(self):
        root, wt, head = self.focused_project(
            'import tempfile\nprint(tempfile.gettempdir())\nraise SystemExit(7)\n')
        self.focused(root, wt, head, 'sample', ok=False)
        receipt = json.loads(next((root / 'nodes/a/spec-runs').glob('*.json')).read_text())
        self.assertEqual(receipt['result'], 7)
        self.assertFalse(Path(Path(receipt['log']).read_text().strip()).exists())

    def test_canonical_refuses_wrong_head_dirty_tree_missing_ids_and_options(self):
        root, wt, head = self.focused_project()
        for expected, ids in [('0' * 40, ['sample']), (head[:8], ['sample']),
                              (head, []), (head, ['--help'])]:
            with self.subTest(expected=expected, ids=ids):
                self.focused(root, wt, expected, *ids, ok=False)
        (wt / 'untracked').touch()
        self.assertIn('clean worktree', self.focused(root, wt, head, 'sample', ok=False))
        self.assertFalse((root / 'nodes/a/spec-runs').exists())

    def test_canonical_requires_configured_runner_and_registered_current_job(self):
        root, wt, head = self.focused_project()
        self.b(root, 'set', 'a', 'verify_spec=')
        self.assertIn('verify_spec', self.focused(root, wt, head, 'sample', ok=False))
        self.assertIn('registered current worktree', self.b(
            root, 'spec-run', 'a', '--head', head, '--', 'sample', ok=False))
        self.assertFalse((root / 'nodes/a/spec-runs').exists())

    def test_canonical_refuses_receipt_if_runner_changes_tested_tree(self):
        root, wt, head = self.focused_project(
            "from pathlib import Path\nPath('focused.py').write_text('changed')\n")
        self.assertIn('clean worktree', self.focused(root, wt, head, 'sample', ok=False))
        self.assertFalse((root / 'nodes/a/spec-runs').exists())

    def test_admission_drift_refuses_execution_and_interrupt_cleans_temp(self):
        root, wt, head = self.focused_project()
        n = dict(id='a', home_path=self.home, dir=root / 'nodes/a',
                 fm={'verify_spec': 'python3 focused.py'},
                 local={'worktrees': {'one': {'path': str(wt)}}})
        args = argparse.Namespace(command=None, specs=['sample'], head=head, branch='one', id='a')
        with self._fixture_module(root, CORE / 'bin/brain') as module:
            runner = module._owners['cmd_spec_run']
            for stage in ('admission', 'interrupt'):
                temporary = []
                @contextlib.contextmanager
                def admitted(*args, **kwargs):
                    if stage == 'admission':
                        (wt / 'drift').touch()
                    yield None
                def execute(*args, **kwargs):
                    temporary.append(Path(args[5]['TMPDIR']))
                    self.assertTrue(temporary[-1].is_dir())
                    raise KeyboardInterrupt
                with self.subTest(stage=stage), \
                        mock.patch.object(runner._m_nodes, 'load'), \
                        mock.patch.object(runner._m_worktrees, 'git_home', return_value=(n, self.home)), \
                        mock.patch.object(runner._m_jobs, 'read_jobs', return_value=[]), \
                        mock.patch.object(runner._m_jobs, 'open_job', return_value=True), \
                        mock.patch.object(runner._m_verify, 'verify_env', return_value=self.env), \
                        mock.patch.object(runner._m_instance, 'unlock'), \
                        mock.patch.object(runner._m_gate_lock, 'node_gate', side_effect=lambda *a: contextlib.nullcontext(self.env)), \
                        mock.patch.object(runner._m_verify, 'verification_resources', side_effect=lambda *a: contextlib.nullcontext({'failed': False})), \
                        mock.patch.object(runner._m_verify, 'command_reservation', side_effect=admitted), \
                        mock.patch.object(runner._m_verify, 'run_verify_stream', side_effect=execute) as run:
                    with self.assertRaises(SystemExit if stage == 'admission' else KeyboardInterrupt):
                        runner.cmd_spec_run(args)
                    if stage == 'admission':
                        run.assert_not_called()
                        (wt / 'drift').unlink()
                    else:
                        self.assertEqual(len(temporary), 1)
                        self.assertFalse(temporary[0].exists())
        self.assertFalse((root / 'nodes/a/spec-runs').exists())

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
