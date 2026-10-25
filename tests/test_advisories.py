"""Focused advisory policy tests with synthetic records only."""
import argparse
import contextlib
import io
import unittest
from pathlib import Path
from unittest import mock

import core_loader

CORE = Path(__file__).resolve().parent.parent


class SeparationPolicy(unittest.TestCase):
    def setUp(self):
        self.m = core_loader.SourceFileLoader('advisory_policy', str(CORE / 'bin/brain')).load_module()
        self.n = {'fm': {'separation_policy': 'required'}}
        self.a = argparse.Namespace(why_separate=None, allow_unexplained=False)
        self.guard = mock.patch.object(self.m, 'no_terms_in')
        self.guard.start()
        self.addCleanup(self.guard.stop)

    def test_required_policy_refuses_absent_reason(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.m.separation_lines(self.a, self.n, None)

    def test_required_policy_accepts_explicit_contract_default_and_urgent_reasons(self):
        self.a.why_separate = 'Independent interface ownership.'
        self.assertIn(self.a.why_separate, self.m.separation_lines(self.a, self.n, None)[0])
        self.a.why_separate = None
        self.assertIn('Independent review', self.m.separation_lines(self.a, self.n, None, 'Independent review')[0])
        with mock.patch.object(self.m, 'read_jobs', return_value=[{}]), \
             mock.patch.object(self.m, 'open_job', return_value={'Job': 'job/example'}), \
             mock.patch.object(self.m, 'contract_file') as contract:
            contract.return_value.is_file.return_value = True
            contract.return_value.read_text.return_value = '## Why separate\nIndependent storage ownership.\n'
            self.assertIn('Independent storage', self.m.separation_lines(self.a, self.n, 'job/example')[0])
        self.a.allow_unexplained = True
        self.assertIn('explicitly waived', self.m.separation_lines(self.a, self.n, None)[0])

    def test_legacy_warning_and_invalid_policy(self):
        self.n['fm'] = {}
        with contextlib.redirect_stderr(io.StringIO()) as stream:
            self.m.separation_lines(self.a, self.n, None)
        self.assertIn('warning:', stream.getvalue())
        self.n['fm']['separation_policy'] = 'silently-ignore'
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.m.separation_lines(self.a, self.n, None)
