"""Parallel selftest selects one canonical suite without running it in this test."""
import argparse
import contextlib
import io
import os
from unittest import mock

import core_loader
from scenario import CORE, Scenario


class ParallelSelftest(Scenario):
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
