"""Per-change selection and native full-proof boundaries, without running a full suite."""
import contextlib
import ast
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
import test_train
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
                mock.patch.object(per_change_gate, 'body_only', return_value=True), \
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

    def test_definition_time_changes_and_unknown_startup_select_full(self):
        with mock.patch.object(per_change_gate.lanes, 'lane_map', return_value=self.mapping), \
                mock.patch.object(per_change_gate.check_lanes, 'check'), \
                mock.patch.object(per_change_gate, 'changed_paths', return_value=['lib/brain/train.py']):
            for startup in (False, ValueError('missing base source')):
                with self.subTest(startup=startup), mock.patch.object(per_change_gate, 'body_only',
                        return_value=startup, side_effect=startup if isinstance(startup, Exception) else None):
                    plan = per_change_gate.select(base='fixture')
                    self.assertEqual(plan['scope'], 'full')
                    self.assertIn('tests/test_usage.py', plan['files'])

    def test_real_base_startup_comparison_keeps_only_function_body_edits_selective(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'lib/brain/train.py'
            source.parent.mkdir(parents=True)
            original = 'VALUE = 1\ndef entry(value=1):\n return value\n'
            source.write_text(original)
            for args in [('init', '-q'), ('add', '.'), ('commit', '-qm', 'base')]:
                subprocess.run(['git', '-c', 'user.name=test', '-c', 'user.email=t@t', *args],
                               cwd=root, check=True, capture_output=True)
            base = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root).decode().strip()
            for changed, expected in [(original.replace('return value', 'return value + 1'), True),
                                      (original.replace('VALUE = 1', 'VALUE = 2'), False),
                                      (original.replace('value=1', 'value=2'), False),
                                      ('from . import common\n' + original, False)]:
                source.write_text(changed)
                self.assertEqual(per_change_gate.body_only(root, base, 'lib/brain/train.py'), expected)
            with self.assertRaises(ValueError):
                per_change_gate.body_only(root, 'missing-ref', 'lib/brain/train.py')

    def test_explicit_core_imports_include_train_without_attribute_calls(self):
        for source in ('from brain.train import ref\nref()', 'import brain.train as batch\n',
                       'from brain import train\n'):
            self.assertTrue(lanes.source_inputs(ast.parse(source), set())[0])

    def test_real_source_map_selects_body_consumers_and_expands_startup_to_full(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for directory in ('tests', 'lib'):
                shutil.copytree(CORE / directory, root / directory,
                                ignore=shutil.ignore_patterns('__pycache__'))
            for args in [('init', '-q'), ('add', '.'), ('commit', '-qm', 'base')]:
                subprocess.run(['git', '-c', 'user.name=test', '-c', 'user.email=t@t', *args],
                               cwd=root, check=True, capture_output=True)
            base = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root).decode().strip()
            path = root / 'lib/brain/train.py'
            original = path.read_text()
            path.write_text(original.replace('def ref(home, name):',
                                              'def ref(home, name):\n    fixture_value = 1'))
            body = per_change_gate.select(root, base)
            self.assertEqual(body['scope'], 'change', body)
            self.assertIn('tests/test_train.py', body['files'])
            self.assertNotIn('tests/test_usage.py', body['files'])
            path.write_text(original + '\nFIXTURE_STARTUP = 1\n')
            startup = per_change_gate.select(root, base)
            self.assertEqual(startup['scope'], 'full')
            self.assertIn('tests/test_usage.py', startup['files'])

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
                    self.assertEqual(kwargs['env']['BRAIN_CORE'], str(CORE))
                    self.assertEqual(kwargs['env']['BRAIN_NO_REEXEC'], '1')
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

    def test_change_packet_cannot_be_relabelled_as_an_adoptable_full_receipt(self):
        root, _, _ = self.configure()
        self.record(root)
        packet = self.read_packet()
        self.b(root, 'full-run', 'a', '--branch', 'job', '--reviews', '1')
        receipt = next((root / 'nodes/a/full-gates').glob('*.json'))
        data = json.loads(receipt.read_text())
        data['packet'] = packet
        receipt.write_text(json.dumps(data))
        self.b(root, 'handback', 'a', '--branch', 'job', '--state', 'done', '--reviews', '1',
               '--summary', 'Refuse partial receipt', '--full-receipt', receipt.stem, ok=False)


class NativeFullTrain(Scenario):
    project = test_train.MergeTrain.project
    job = test_train.MergeTrain.job
    detach = test_train.MergeTrain.detach
    batches = test_train.MergeTrain.batches

    def test_batch_forces_full_scope_instead_of_inheriting_change_mode(self):
        root, _ = self.project(command='test "$BRAIN_VERIFY_SCOPE" = full',
                               settings=('verify_change=echo selected',))
        self.job('one')
        self.detach()
        self.env['BRAIN_VERIFY_SCOPE'] = 'change'
        output = self.b(root, 'train', 'a', '--jobs', 'one')
        self.assertIn('merged 1 job(s); 1 full verify run(s)', output)
        self.assertEqual(len(self.batches()[0]['runs']), 1)
        self.assertEqual(self.batches()[0]['runs'][0]['result'], 0)


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
