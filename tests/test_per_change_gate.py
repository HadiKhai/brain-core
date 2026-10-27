"""Per-change selection and native full-proof boundaries, without running a full suite."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import core_loader
import lanes
import per_change_gate
from scenario import CORE, Scenario
from test_verify import WorkerReadyPackets
from test_selftest_parallel import ParallelSelftest


class ChangeSelection(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mapping = lanes.lane_map()

    def selection(self, paths=(), error=None):
        with mock.patch.object(per_change_gate.lanes, 'lane_map', return_value=self.mapping), \
                mock.patch.object(per_change_gate.check_lanes, 'check'), \
                mock.patch.object(per_change_gate, 'changed_paths', return_value=paths, side_effect=error):
            return per_change_gate.select(base='fixture')

    def test_shared_unknown_missing_and_invalid_map_choose_full(self):
        for path in ('lib/brain/common.py', 'tests/scenario.py', 'tests/conftest.py',
                     'bin/brain', 'requirements-test.txt', 'new-runtime-file',
                     'tests/test_brain.py', 'tests/missing.py'):
            with self.subTest(path=path):
                self.assertEqual(self.selection([path])['scope'], 'full')
        for error in (ValueError('unknown base'), OSError('read failed'), SyntaxError('parse failed')):
            self.assertEqual(self.selection(error=error)['scope'], 'full')
        with mock.patch.object(per_change_gate.lanes, 'lane_map', side_effect=SyntaxError('bad source')):
            self.assertEqual(per_change_gate.select(base='fixture')['scope'], 'full')

    def test_source_import_closure_selects_train_consumers(self):
        plan = self.selection(['lib/brain/train.py'])
        self.assertEqual(plan['scope'], 'change')
        self.assertIn('tests/test_train.py', plan['files'])
        self.assertIn('tests/test_train_landing.py', plan['files'])
        self.assertNotIn('tests/test_usage.py', plan['files'])
        self.assertLess(len(plan['files']), len(list((CORE / 'tests').glob('test_*.py'))) - 1)
        self.assertTrue(all('::' in target for target in plan['targets']))

    def test_test_edits_include_importing_test_helpers(self):
        plan = self.selection(['tests/test_verify.py'])
        self.assertIn('tests/test_verify.py', plan['files'])
        self.assertIn('tests/test_verify_lanes.py', plan['files'])
        self.assertIn('tests/test_per_change_gate.py', plan['files'])
        self.assertNotIn('tests/test_usage.py', plan['files'])

    def test_documentation_only_keeps_cheap_and_guard_without_unit_tests(self):
        plan = self.selection(['docs/DECISIONS.md', 'README.md'])
        self.assertEqual(plan['scope'], 'change')
        self.assertEqual(plan['targets'], [])

    def test_nonselective_imports_and_full_flags_keep_complete_suite(self):
        self.assertEqual(self.selection(['lib/brain/verify.py'])['scope'], 'full')
        with mock.patch.object(per_change_gate.check_lanes, 'check'), \
                mock.patch.object(per_change_gate, 'changed_paths', side_effect=AssertionError('full needs no base')):
            self.assertEqual(per_change_gate.select(full=True)['targets'], ['tests/test_brain.py'])

    def test_nasty_git_paths_are_lossless_and_deletions_and_new_files_count(self):
        with tempfile.TemporaryDirectory(prefix='gate space é-') as folder:
            root = Path(folder)
            def git(*args):
                return subprocess.run(['git', '-c', 'user.name=test', '-c', 'user.email=t@t', *args],
                                      cwd=root, check=True, capture_output=True).stdout.decode().strip()
            git('init', '-q')
            names = ['spaces here.py', 'é漢字.py', 'line\nbreak.py', '-option.py']
            for name in names:
                (root / name).write_text('before')
            git('add', '.')
            git('commit', '-qm', 'base')
            base = git('rev-parse', 'HEAD')
            for name in names:
                (root / name).write_text('after')
            (root / 'é漢字.py').unlink()
            (root / 'untracked\nnew').touch()
            self.assertEqual(per_change_gate.changed_paths(root, base), sorted(names + ['untracked\nnew']))
            for invalid in (None, '--output=evil', 'missing-ref'):
                with self.assertRaises(ValueError):
                    per_change_gate.changed_paths(root, invalid)
            git('checkout', '--orphan', 'unrelated')
            git('commit', '--allow-empty', '-qm', 'unrelated')
            with self.assertRaises(ValueError):
                per_change_gate.changed_paths(root, base)

    def test_execution_orders_cheap_guard_and_selected_tests_and_stops_on_failure(self):
        plan = dict(targets=['tests/test_train.py::MergeTrain'])
        env = {'NODE': 'fixture', 'BRAIN_ROOT': '/fixture'}
        with mock.patch.dict(os.environ, env, clear=True):
            for failed in (None, 0, 1, 2):
                calls = []
                def execute(argv, **kwargs):
                    calls.append(argv)
                    return mock.Mock(returncode=7 if len(calls) - 1 == failed else 0)
                self.assertEqual(per_change_gate.run(plan, CORE, 2, execute), 0 if failed is None else 7)
                self.assertEqual(len(calls), 3 if failed is None else failed + 1)
                self.assertTrue(calls[0][-1].endswith('check_lanes.py'))
                if len(calls) > 1:
                    self.assertIn('guard', calls[1])
                if len(calls) > 2:
                    self.assertIn('--serial-after-parallel', calls[2])
                    self.assertEqual(calls[2][-1], plan['targets'][0])
            execute = mock.Mock(return_value=mock.Mock(returncode=0))
            self.assertEqual(per_change_gate.run({'targets': []}, CORE, 1, execute), 0)
            self.assertEqual(execute.call_count, 2)
        with mock.patch.dict(os.environ, {}, clear=True), contextlib.redirect_stderr(io.StringIO()):
            execute = mock.Mock()
            self.assertEqual(per_change_gate.run(plan, CORE, 1, execute), 2)
            execute.assert_not_called()


class NativeChangeGate(Scenario):
    job = WorkerReadyPackets.job
    record = WorkerReadyPackets.record
    packet = WorkerReadyPackets.packet

    def configure(self):
        root, home, wt = self.job()
        self.b(root, 'set', 'a', 'verify_change=printf "change:%s:%s:%s" "$BRAIN_VERIFY_SCOPE" "$BRAIN_VERIFY_BASE" "$BRAIN_VERIFY_HEAD"',
               'verify=echo complete-suite')
        return root, home, wt

    def read_packet(self):
        return json.loads(self.packet().read_text())['ready']

    def test_native_ready_runs_change_binds_configuration_and_reuses_only_matching_proof(self):
        root, home, wt = self.configure()
        self.record(root)
        packet = self.read_packet()
        self.assertEqual(packet['verify_scope'], 'change')
        output = Path(packet['verify_log']).read_text()
        self.assertIn('change:change:' + self.git('rev-parse', 'main', cwd=home).strip(), output)
        self.assertTrue(output.endswith(self.git('rev-parse', 'HEAD', cwd=wt).strip()))
        self.assertNotIn('complete-suite', output)
        self.assertIn('ready', self.b(root, 'ready', 'a', '--branch', 'job'))
        self.record(root)
        self.assertTrue(self.read_packet()['verify_reused'])
        self.b(root, 'set', 'a', 'verify_change=echo modified-selection')
        self.assertIn('verification configuration changed', self.b(root, 'ready', 'a', '--branch', 'job', ok=False))
        self.record(root)
        self.assertIn('modified-selection', Path(self.read_packet()['verify_log']).read_text())

    def test_gate_does_not_publish_completion_and_full_modes_bypass_selection(self):
        root, _, _ = self.configure()
        self.b(root, 'set', 'a', 'verify_change=false')
        self.b(root, 'gate', 'a', '--branch', 'job', '--reviews', '1', ok=False)
        self.assertEqual(list((root / 'nodes/a/job-handbacks').glob('*.json')), [])
        for flag in ('--train', '--nightly'):
            self.b(root, 'gate', 'a', '--branch', 'job', '--reviews', '1', flag)
        receipts = list((root / 'nodes/a/full-gates').glob('*.json'))
        self.assertEqual(len(receipts), 2)
        for receipt in receipts:
            packet = json.loads(receipt.read_text())['packet']
            self.assertEqual(packet['verify_scope'], 'full')
            self.assertIn('complete-suite', Path(packet['verify_log']).read_text())
        self.assertEqual(list((root / 'nodes/a/job-handbacks').glob('*.json')), [])
        self.b(root, 'set', 'a', 'verify_change=echo selected')
        self.b(root, 'gate', 'a', '--branch', 'job', '--reviews', '1')
        self.assertEqual(list((root / 'nodes/a/job-handbacks').glob('*.json')), [])
        self.assertEqual(len(list((root / 'nodes/a/full-gates').glob('*.json'))), 2)

    def test_full_run_bypasses_both_selectors_and_can_be_adopted(self):
        root, _, _ = self.configure()
        self.b(root, 'set', 'a', 'verify_lanes_from=echo broken-map')
        self.b(root, 'full-run', 'a', '--branch', 'job', '--reviews', '1')
        receipt = next((root / 'nodes/a/full-gates').glob('*.json'))
        self.assertEqual(json.loads(receipt.read_text())['packet']['verify_scope'], 'full')
        # Even an invalid map cannot narrow or block adoption of the complete run.
        self.b(root, 'handback', 'a', '--branch', 'job', '--state', 'done', '--reviews', '1',
               '--summary', 'Complete gate', '--full-receipt', receipt.stem)
        self.assertEqual(self.read_packet()['verify_scope'], 'full')
        self.b(root, 'ready', 'a', '--branch', 'job')

    def test_partial_success_emits_no_full_completion_fact(self):
        root, _, _ = self.configure()
        m = core_loader.SourceFileLoader('change_fact', str(CORE / 'bin/brain')).load_module()
        inst = m.Instance(root)
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(m, 'record_full_fact') as fact:
            packet, reasons = m.record_ready(inst, inst.node('a'), 'job', 1)
        self.assertEqual(reasons, [])
        self.assertEqual(packet['verify_result'], 0)
        fact.assert_not_called()


class SelectedSerialTail(Scenario):
    result_fixture = ParallelSelftest.result_fixture

    def test_selected_files_finish_parallel_before_serial(self):
        folder, env, command = self.result_fixture(
            'import unittest\nfrom pathlib import Path\n'
            'class Regular(unittest.TestCase):\n'
            ' def test_regular(self): Path("regular.done").touch()\n'
            'class DecisionGate(unittest.TestCase):\n'
            ' def test_serial(self): assert Path("regular.done").exists()\n')
        shutil.copyfile(CORE / 'tests/lanes.py', folder / 'lanes.py')
        result = subprocess.run(command + ['-n', str(min(2, os.cpu_count() or 1)),
                                '--serial-after-parallel', '--max-worker-restart=0'],
                                cwd=folder, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = [json.loads(line) for line in (folder / 'results.jsonl').read_text().splitlines()]
        complete = [row for row in rows if row['event'] == 'completed']
        self.assertEqual(len(complete), 2)
        self.assertTrue(complete[0]['nodeid'].endswith('Regular::test_regular'))
        self.assertTrue(complete[1]['nodeid'].endswith('DecisionGate::test_serial'))
