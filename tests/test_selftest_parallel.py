"""Parallel selftest selects one canonical suite without running it in this test."""
import argparse
import contextlib
import io
import os
import json
import shutil
import signal
import subprocess
import sys
import time
from unittest import mock

import core_loader
from scenario import CORE, Scenario


class ParallelSelftest(Scenario):
    def test_explicit_serial_method_does_not_serialize_unrelated_class_methods(self):
        folder, env, command = self.result_fixture(
            'import time, unittest\nfrom pathlib import Path\n'
            'class HandbackLifecycle(unittest.TestCase):\n'
            ' def test_regular(self): time.sleep(.1); Path("regular.done").touch()\n'
            ' def test_p6_cli_enqueue_foreground_terminal_and_live_join(self):\n'
            '  assert Path("regular.done").exists()\n')
        (folder / 'test_sample.py').rename(folder / 'test_brain.py')
        shutil.copyfile(CORE / 'tests/lanes.py', folder / 'lanes.py')
        command[-1] = 'test_brain.py'
        workers = min(2, os.cpu_count() or 1)
        result = subprocess.run(command + ['-n', str(workers), '--max-worker-restart=0'],
                                cwd=folder, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = [json.loads(line) for line in (folder / 'results.jsonl').read_text().splitlines()]
        completed = [row for row in rows if row['event'] == 'completed']
        self.assertEqual(len(completed), 2)
        self.assertTrue(all(row['outcome'] == 'passed' for row in completed))
        lanes = [row for row in rows if row['event'] == 'lane']
        self.assertEqual([row['lane'] for row in lanes],
                         ['parallel', 'serial'] if workers > 1 else ['parallel'])
        if workers > 1:
            self.assertEqual([row['tests'] for row in lanes], [1, 1])

    def test_canonical_serial_lane_waits_for_parallel_workers_without_dropping_tests(self):
        source = ('import time, unittest\nfrom pathlib import Path\n'
                  'class ParallelFixture(unittest.TestCase):\n'
                  ' def test_a(self): time.sleep(.1); Path("a.done").touch()\n'
                  ' def test_b(self): time.sleep(.1); Path("b.done").touch()\n'
                  'class DecisionGate(unittest.TestCase):\n'
                  ' def test_a(self): assert Path("a.done").exists() and Path("b.done").exists()\n'
                  ' def test_b(self): assert Path("a.done").exists() and Path("b.done").exists()\n')
        folder, env, command = self.result_fixture(source)
        (folder / 'test_sample.py').rename(folder / 'test_brain.py')
        shutil.copyfile(CORE / 'tests/lanes.py', folder / 'lanes.py')
        command[-1] = 'test_brain.py'
        workers = min(3, os.cpu_count() or 1)
        result = subprocess.run(command + ['-n', str(workers), '--max-worker-restart=0'],
                                cwd=folder, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = [json.loads(line) for line in (folder / 'results.jsonl').read_text().splitlines()]
        done = [row for row in rows if row['event'] == 'completed']
        self.assertEqual(len(done), 4)
        self.assertEqual(len({row['nodeid'] for row in done}), 4)
        self.assertTrue(all(row['outcome'] == 'passed' for row in done))
        lanes = [row for row in rows if row['event'] == 'lane']
        self.assertEqual([row['lane'] for row in lanes],
                         ['parallel', 'serial'] if workers > 1 else ['parallel'])
        if workers > 1:
            self.assertEqual([row['tests'] for row in lanes], [2, 2])
            self.assertEqual(lanes[-1]['workers'], 1)
        self.assertEqual(rows[-1]['collected'], 4)
        self.assertEqual(rows[-1]['exitstatus'], 0)

    def test_serial_lane_failure_remains_a_failed_session(self):
        folder, env, command = self.result_fixture(
            'import unittest\nclass ParallelFixture(unittest.TestCase):\n'
            ' def test_pass(self): pass\nclass DecisionGate(unittest.TestCase):\n'
            ' def test_failure(self): assert False, "serial failure fixture"\n')
        (folder / 'test_sample.py').rename(folder / 'test_brain.py')
        shutil.copyfile(CORE / 'tests/lanes.py', folder / 'lanes.py')
        command[-1] = 'test_brain.py'
        result = subprocess.run(command + ['-n', str(min(2, os.cpu_count() or 1)), '--max-worker-restart=0'],
                                cwd=folder, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        rows = [json.loads(line) for line in (folder / 'results.jsonl').read_text().splitlines()]
        done = [row for row in rows if row['event'] == 'completed']
        self.assertEqual(len(done), 2)
        self.assertEqual(sorted(row['outcome'] for row in done), ['failed', 'passed'])
        self.assertEqual(rows[-1]['failed'], 1)
        self.assertEqual(rows[-1]['exitstatus'], 1)

    def test_worker_oversubscription_refuses_before_running_tests(self):
        folder, env, command = self.result_fixture('def test_only(): pass\n')
        result = subprocess.run(command + ['-n', str((os.cpu_count() or 1) + 1)],
                                cwd=folder, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertIn('workers exceed available CPU cores', result.stdout + result.stderr)
        self.assertFalse((folder / 'results.jsonl').exists())

    def test_duration_order_preserves_inventory_and_prioritizes_longer_tests(self):
        folder, env, command = self.result_fixture(
            'def test_fast(): pass\ndef test_unknown(): pass\ndef test_slow(): pass\n')
        previous = folder / 'previous.jsonl'
        previous.write_text('\n'.join(json.dumps(dict(event='completed', nodeid=nodeid, duration=duration))
                                      for nodeid, duration in (
                                          ('tests/test_brain.py::test_fast', 1),
                                          ('tests/test_brain.py::test_slow', 20))) + '\n')
        result = subprocess.run(command + ['--duration-order=' + str(previous)],
                                cwd=folder, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = [json.loads(line) for line in (folder / 'results.jsonl').read_text().splitlines()]
        completed = [row for row in rows if row['event'] == 'completed']
        self.assertEqual([row['nodeid'] for row in completed],
                         ['test_sample.py::test_slow', 'test_sample.py::test_fast', 'test_sample.py::test_unknown'])
        self.assertTrue(all(row['outcome'] == 'passed' for row in completed))
        self.assertEqual(rows[-1]['collected'], 3)

    def test_duration_order_refuses_empty_and_unmatched_inputs(self):
        folder, env, command = self.result_fixture('def test_only(): pass\n')
        previous = folder / 'previous.jsonl'
        for content in ('', json.dumps(dict(event='completed', nodeid='other.py::test_missing', duration=1))):
            with self.subTest(content=content):
                previous.write_text(content)
                output = folder / 'results.jsonl'
                output.unlink(missing_ok=True)
                result = subprocess.run(command + ['--duration-order=' + str(previous)],
                                        cwd=folder, env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
                self.assertIn('cannot use duration-order:', result.stdout + result.stderr)
                rows = [json.loads(line) for line in output.read_text().splitlines()]
                self.assertFalse(any(row['event'] == 'completed' for row in rows))

    def test_parallel_workers_use_pinned_pytest_suite_and_no_restarts(self):
        m = core_loader.SourceFileLoader('parallel_selftest_fixture', str(CORE / 'bin/brain')).load_module()
        with mock.patch.object(m.subprocess, 'run', return_value=mock.Mock(returncode=0)) as execute:
            with self.assertRaises(SystemExit) as result:
                m.cmd_selftest(argparse.Namespace(verbose=False, k='SelectedClass', jobs=4))
        self.assertEqual(result.exception.code, 0)
        argv = execute.call_args.args[0]
        self.assertIn('pytest', argv)
        self.assertEqual(argv[argv.index('-n') + 1], '4')
        self.assertIn('--max-worker-restart=0', argv)
        self.assertEqual(argv.count(str(CORE / 'tests/test_brain.py')), 1)
        self.assertIn('SelectedClass', argv)

    def test_serial_default_remains_stdlib(self):
        m = core_loader.SourceFileLoader('serial_selftest_fixture', str(CORE / 'bin/brain')).load_module()
        with mock.patch.object(m.subprocess, 'run', return_value=mock.Mock(returncode=0)) as execute:
            with self.assertRaises(SystemExit):
                m.cmd_selftest(argparse.Namespace(verbose=False, k=None))
        self.assertIn('unittest', execute.call_args.args[0])

    def test_rehearse_from_worktree_keeps_selected_core_and_warns(self):
        m = core_loader.SourceFileLoader('rehearse_reexec_fixture', str(CORE / 'bin/brain')).load_module()
        other = self.tmp / 'instance/core/bin/brain'
        other.parent.mkdir(parents=True)
        other.write_text('# fixture')
        checkout = self.tmp / 'checkout'
        checkout.mkdir()
        (checkout / '.git').write_text('gitdir: fixture')
        owner = m.reexec_into_instance_core.__globals__
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(m, 'CORE', checkout), \
                mock.patch.object(owner['_m_nodes'], 'try_find_root', return_value=self.tmp / 'instance'), \
                mock.patch.object(m.sys, 'argv', ['brain', 'rehearse', 'app']), \
                mock.patch.object(m.os, 'execv') as execute, contextlib.redirect_stderr(io.StringIO()) as output:
            m.reexec_into_instance_core()
        execute.assert_not_called()
        self.assertIn('current worktree', output.getvalue())

    def test_parallel_dispatch_steals_remaining_work_without_retries(self):
        m = core_loader.SourceFileLoader('stealing_selftest_fixture', str(CORE / 'bin/brain')).load_module()
        with mock.patch.object(m.subprocess, 'run', return_value=mock.Mock(returncode=0)) as execute:
            with self.assertRaises(SystemExit):
                m.cmd_selftest(argparse.Namespace(verbose=False, k='FocusedFixture', jobs=2))
        argv = execute.call_args.args[0]
        self.assertIn('--dist=worksteal', argv)
        self.assertIn('--max-worker-restart=0', argv)
        self.assertEqual(argv[-2:], ['-k', 'FocusedFixture'])

    def result_fixture(self, source):
        folder = self.tmp / 'results'
        folder.mkdir()
        for name in ('pytest.ini', 'tests/conftest.py'):
            shutil.copyfile(CORE / name, folder / name.split('/')[-1])
        (folder / 'test_sample.py').write_text(source)
        env = dict(self.env)
        env.pop('PYTHONPATH', None)
        return folder, env, [sys.executable, '-B', '-m', 'pytest', '-p', 'no:cacheprovider',
                             '--completed-results=results.jsonl', 'test_sample.py']

    def test_completed_results_survive_interruption_before_session_end(self):
        folder, env, command = self.result_fixture(
            'import time\nfrom pathlib import Path\n'
            'def test_first(): pass\n'
            'def test_waiting():\n Path("waiting").touch()\n time.sleep(20)\n')
        with (folder / 'output.log').open('w') as output:
            process = subprocess.Popen(command, cwd=folder, env=env, stdout=output, stderr=output)
            try:
                deadline = time.monotonic() + 15
                while not (folder / 'waiting').exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue((folder / 'waiting').exists())
                rows = [json.loads(line) for line in (folder / 'results.jsonl').read_text().splitlines()]
                completed = [row for row in rows if row['event'] == 'completed']
                self.assertEqual([row['nodeid'] for row in completed], ['test_sample.py::test_first'])
                self.assertEqual(completed[0]['outcome'], 'passed')
                self.assertGreaterEqual(completed[0]['duration'], 0)
                self.assertFalse(any(row['event'] == 'session_finish' for row in rows))
                self.assertEqual(float(rows[0]['timeout']), 120)
                self.assertEqual(rows[0]['timeout_plugin'], '2.4.0')
                process.send_signal(signal.SIGINT)
                process.wait(timeout=10)
                self.assertNotEqual(process.returncode, 0)
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()

    def test_timeout_plugin_reports_named_failure_through_xdist(self):
        folder, env, command = self.result_fixture(
            'import time\ndef test_timed_out_fixture(): time.sleep(10)\n'
            'def test_fast_fixture(): pass\n')
        result = subprocess.run(command + ['-n', '2', '--max-worker-restart=0', '--timeout=0.2'],
                                cwd=folder, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        rows = [json.loads(line) for line in (folder / 'results.jsonl').read_text().splitlines()]
        failed = [row for row in rows if row['event'] == 'phase' and row['outcome'] == 'failed']
        self.assertEqual([row['nodeid'] for row in failed], ['test_sample.py::test_timed_out_fixture'])
        self.assertIn('Timeout (>0.2s) from pytest-timeout', failed[0]['longrepr'])
        self.assertEqual(len([row for row in rows if row['event'] == 'completed']), 2)
        self.assertEqual(rows[0]['timeout'], 0.2)

    def test_completed_results_preserve_setup_and_teardown_failures(self):
        folder, env, command = self.result_fixture(
            'import pytest\n@pytest.fixture\ndef setup_error(): raise RuntimeError("setup fixture")\n'
            '@pytest.fixture\ndef teardown_error():\n yield\n raise RuntimeError("teardown fixture")\n'
            'def test_setup(setup_error): pass\ndef test_teardown(teardown_error): pass\n'
            '@pytest.mark.skip(reason="fixture")\ndef test_skip(): pass\n')
        result = subprocess.run(command, cwd=folder, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        rows = [json.loads(line) for line in (folder / 'results.jsonl').read_text().splitlines()]
        outcomes = {row['nodeid']: row['outcome'] for row in rows if row['event'] == 'completed'}
        self.assertEqual(outcomes, {'test_sample.py::test_setup': 'failed',
                                    'test_sample.py::test_teardown': 'failed',
                                    'test_sample.py::test_skip': 'skipped'})
