"""Terminal Active work notes must not strand finished fabricated jobs."""
import importlib.util
import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import core_loader
from scenario import CORE, AutonomyProject


def load_core():
    loader = core_loader.SourceFileLoader('prune_states_fixture', str(CORE / 'bin/brain'))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    core = importlib.util.module_from_spec(spec)
    loader.exec_module(core)
    return core


class ActiveWorkStateParsing(unittest.TestCase):
    def setUp(self):
        self.core = load_core()
        self.temp = tempfile.TemporaryDirectory(prefix='brain-state-test-')
        self.addCleanup(self.temp.cleanup)
        self.node = {'id': 'app', 'dir': Path(self.temp.name), 'home_path': CORE,
                     'local': {'worktrees': {}}, 'parent': None}

    def test_terminal_notes_are_filtered_but_references_stay_inspectable(self):
        terminal = ['merged', ' Merged — fixture shipped ', 'MERGED – fixture shipped',
                    'closed - fixture retired', 'done: fixture complete', 'abandoned — fixture dropped']
        live = ['working', 'review — merged into preview', 'not merged', 'merged?',
                'merged-ish', 'done pending review', 'merged/working', 'merged —',
                'unmerged — still editing', '', 'unknown']
        rows = [(f'job-{i}', state) for i, state in enumerate(terminal + live)]
        (self.node['dir'] / 'status.md').write_text(
            '## Active work\n| Branch | Thread | PR | State |\n| --- | --- | --- | --- |\n' +
            ''.join('| ' + branch + ' | fixture-thread | - | ' + state + ' |\n' for branch, state in rows))
        visible = self.core.active_work(self.node)
        self.assertEqual([r[0] for r in visible], [branch for branch, _ in rows[len(terminal):]])
        self.assertEqual(len(self.core.active_work(self.node, include_closed=True)), len(rows))

    def guard(self, state, rows=None):
        record = {'path': str(CORE)}
        row = ['job-a', 'fixture-worker', '-', state]

        def git(*args, **kwargs):
            return subprocess.CompletedProcess(args, 0, 'refs/heads/job-a' if args[0] == 'symbolic-ref' else 'tip', '')

        with mock.patch.object(self.core, 'git', side_effect=git), \
                mock.patch.object(self.core, 'read_jobs', return_value=rows or [{'Job': 'job-a', 'Ended': 'fixture-end'}]), \
                mock.patch.object(self.core, 'active_work', return_value=[row]):
            return self.core.removal_refusal(self.node, 'job-a', record)

    def test_removal_guard_checks_state_even_for_unfiltered_rows(self):
        for state in ['merged', 'merged — fixture shipped', 'CLOSED: fixture retired', 'done', 'abandoned']:
            with self.subTest(state=state):
                self.assertEqual(self.guard(state), '')

    def test_nonterminal_refusal_names_the_actual_state(self):
        state = 'review — merged into preview'
        reason = self.guard(state)
        self.assertIn('live bound thread', reason)
        self.assertIn('state: ' + state, reason)
        self.assertIn('reconcile', reason)

    def test_refusal_redacts_protected_state_notes(self):
        (self.node['dir'] / 'never-name.txt').write_text('HiddenFixture\n')
        reason = self.guard('working — HiddenFixture')
        self.assertIn('live bound thread', reason)
        self.assertNotIn('HiddenFixture', reason)

    def test_closed_job_checklist_accepts_the_same_terminal_notes(self):
        (self.node['dir'] / 'status.md').write_text(
            '## Active work\n| Branch | Thread | PR | State |\n'
            '| job-a | fixture-worker | - | merged — fixture shipped |\n')
        row = {'Job': 'job-a', 'Started': 'fixture-start', 'Ended': 'fixture-end', 'Outcome': 'merged'}
        with mock.patch.object(self.core, 'read_jobs', return_value=[row]), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.core.print_thread_checklist(self.node, row)
        self.assertIn('Worker from Active work: fixture-worker', output.getvalue())
        self.assertNotIn('ownership ambiguous', output.getvalue())


class PruneAnnotatedStates(AutonomyProject):
    def merged(self):
        root, home, wt = self.project()
        (wt / 'result').write_text('fixture complete\n')
        self.git('add', 'result', cwd=wt)
        self.git('commit', '-qm', 'fixture result', cwd=wt)
        self.git('merge', '-q', 'job-a', cwd=home)
        return root, home, wt

    def active(self, root, state, branch='`job-a` (fixture annotation)'):
        (root / 'nodes/app/status.md').write_text(
            '## Active work\n| Branch | Thread | PR | State |\n| --- | --- | --- | --- |\n' +
            '| ' + branch + ' | fixture-worker | - | ' + state + ' |\n')

    def test_merged_note_prunes_and_lists_thread_without_rewriting_status(self):
        root, home, wt = self.merged()
        self.active(root, 'merged — fixture shipped')
        before = (root / 'nodes/app/status.md').read_bytes()
        local_path = root / '.brain/local.json'
        ledger_path = root / 'nodes/app/job-ledger.md'
        local_before, ledger_before = local_path.read_bytes(), ledger_path.read_bytes()
        dry = self.b(root, 'wt', 'prune', 'app', '--offline', '--dry-run')
        self.assertIn('would remove: job-a', dry)
        self.assertIn('fixture-worker', dry)
        self.assertTrue(wt.exists())
        self.assertEqual(local_path.read_bytes(), local_before)
        self.assertEqual(ledger_path.read_bytes(), ledger_before)
        out = self.b(root, 'wt', 'prune', 'app', '--offline')
        self.assertIn('removed: job-a', out)
        self.assertIn('fixture-worker', out)
        self.assertFalse(wt.exists())
        self.assertEqual((root / 'nodes/app/status.md').read_bytes(), before)

    def test_terminal_note_cannot_override_dirty_or_unmerged_work(self):
        root, home, wt = self.merged()
        self.active(root, 'merged — fixture shipped')
        (wt / 'unfinished').write_text('fixture work\n')
        out = self.b(root, 'wt', 'prune', 'app', '--offline')
        self.assertIn('job-a (uncommitted work)', out)
        self.assertNotIn('live bound thread', out)
        self.assertTrue(wt.exists())
        (wt / 'unfinished').unlink()
        (wt / 'unmerged').write_text('fixture follow-on\n')
        self.git('add', 'unmerged', cwd=wt)
        self.git('commit', '-qm', 'fixture follow-on', cwd=wt)
        out = self.b(root, 'wt', 'prune', 'app', '--offline')
        self.assertIn('job-a (not merged)', out)
        self.assertTrue(wt.exists())

    def test_live_state_and_other_open_claimant_still_block(self):
        root, home, wt = self.merged()
        self.active(root, 'working — fixture in progress')
        out = self.b(root, 'wt', 'prune', 'app', '--offline')
        self.assertIn('state: working — fixture in progress', out)
        self.assertTrue(wt.exists())
        self.active(root, 'merged — fixture shipped')
        self.b(root, 'wt', 'add', 'app', 'other-job')
        local_path = root / '.brain/local.json'
        local = json.loads(local_path.read_text())
        local['nodes']['app']['worktrees']['other-job']['path'] = str(wt)
        local_path.write_text(json.dumps(local))
        self.assertIn('open ledger job other-job', self.b(root, 'wt', 'prune', 'app', '--offline'))
        self.assertIn('open ledger job other-job', self.b(root, 'wt', 'rm', 'app', 'job-a', '--force', ok=False))
        self.assertTrue(wt.exists())

    def test_explicit_remove_accepts_terminal_note(self):
        root, home, wt = self.merged()
        self.active(root, 'closed — fixture retired')
        out = self.b(root, 'wt', 'rm', 'app', 'job-a')
        self.assertIn('removed the worktree for job-a', out)
        self.assertFalse(wt.exists())
