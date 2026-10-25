"""Landing publication and conflict classification use fabricated repositories only."""
import contextlib
import io
import json
import os
import subprocess
from unittest import mock
from pathlib import Path

import core_loader
from scenario import Scenario, CORE


class TrainLanding(Scenario):
    def setUp(self):
        super().setUp()
        self.core = core_loader.SourceFileLoader('landing_fixture', str(CORE / 'bin/brain')).load_module()
        self.env_patch = mock.patch.dict(os.environ, self.env)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.home = self.repo(self.tmp / 'app', {'shared': 'base\n'})
        self.base = self.git('rev-parse', 'HEAD', cwd=self.home).strip()
        self.tip = self.base

    def publish(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.core.publish_main(self.home, self.base, self.tip, 'train/fixture')
        return output.getvalue()

    def changed_tip(self):
        self.git('checkout', '-qb', 'job', cwd=self.home)
        (self.home / 'shared').write_text('landed\n')
        self.git('commit', '-qam', 'job', cwd=self.home)
        self.tip = self.git('rev-parse', 'HEAD', cwd=self.home).strip()
        self.git('checkout', '-q', 'main', cwd=self.home)

    def test_clean_checked_out_main_uses_git_fast_forward(self):
        self.changed_tip()
        git = self.core.git
        with mock.patch.object(self.core, 'git', wraps=git) as calls:
            self.publish()
        merges = [c for c in calls.call_args_list if c.args[:2] == ('merge', '--ff-only')]
        self.assertEqual(len(merges), 1)
        self.assertIn('--no-overwrite-ignore', merges[0].args)
        self.assertIn('--no-autostash', merges[0].args)
        self.assertFalse(any(c.args[0] == 'update-ref' for c in calls.call_args_list))
        self.assertEqual(self.git('rev-parse', 'main', cwd=self.home).strip(), self.tip)
        self.assertEqual((self.home / 'shared').read_text(), 'landed\n')
        self.assertEqual(self.git('status', '--porcelain', cwd=self.home), '')

    def test_occupancy_change_at_final_preflight_refuses_before_cas(self):
        self.changed_tip()
        self.git('checkout', '--detach', '-q', self.base, cwd=self.home)
        linked = self.tmp / 'late-main'
        original = self.core.main_checkouts
        calls = []
        def attach(home):
            calls.append(True)
            if len(calls) == 2:
                self.git('worktree', 'add', str(linked), 'main', cwd=self.home)
            return original(home)
        with mock.patch.object(self.core, 'main_checkouts', side_effect=attach):
            with self.assertRaisesRegex(SystemExit, 'occupancy changed before publication'):
                self.publish()
        self.assertEqual(self.git('rev-parse', 'main', cwd=self.home).strip(), self.base)
        self.assertEqual((linked / 'shared').read_text(), 'base\n')
        self.assertEqual(self.git('status', '--porcelain', cwd=linked), '')

    def test_unoccupied_success_still_uses_one_cas(self):
        self.changed_tip()
        self.git('checkout', '--detach', '-q', self.base, cwd=self.home)
        with mock.patch.object(self.core, 'git', wraps=self.core.git) as calls:
            self.publish()
        self.assertEqual(sum(c.args[0] == 'update-ref' for c in calls.call_args_list), 1)
        self.assertEqual(self.git('rev-parse', 'main', cwd=self.home).strip(), self.tip)
        self.assertEqual((self.home / 'shared').read_text(), 'base\n')

    def test_dirty_checked_out_main_refuses_without_writes(self):
        self.changed_tip()
        (self.home / 'shared').write_text('operator edit\n')
        index = (self.home / '.git/index').read_bytes()
        with self.assertRaisesRegex(SystemExit, 'dirty or changed') as error:
            self.publish()
        self.assertIn(str(self.home), str(error.exception))
        self.assertIn('merge --ff-only', str(error.exception))
        self.assertEqual(self.git('rev-parse', 'main', cwd=self.home).strip(), self.base)
        self.assertEqual((self.home / 'shared').read_text(), 'operator edit\n')
        self.assertEqual((self.home / '.git/index').read_bytes(), index)

    def test_hidden_index_edit_refuses_without_writes(self):
        self.changed_tip()
        self.git('update-index', '--assume-unchanged', 'shared', cwd=self.home)
        (self.home / 'shared').write_text('hidden operator edit\n')
        index = (self.home / '.git/index').read_bytes()
        with self.assertRaisesRegex(SystemExit, 'dirty or changed'):
            self.publish()
        self.assertEqual((self.home / 'shared').read_text(), 'hidden operator edit\n')
        self.assertEqual((self.home / '.git/index').read_bytes(), index)
        self.assertEqual(self.git('rev-parse', 'main', cwd=self.home).strip(), self.base)

    def test_linked_main_with_newline_path_uses_git_fast_forward(self):
        self.changed_tip()
        self.git('checkout', '--detach', '-q', self.base, cwd=self.home)
        linked = self.tmp / 'linked\ncheckout'
        self.git('worktree', 'add', str(linked), 'main', cwd=self.home)
        self.publish()
        self.assertEqual((linked / 'shared').read_text(), 'landed\n')
        self.assertEqual((self.home / 'shared').read_text(), 'base\n')
        self.assertEqual(self.git('status', '--porcelain', cwd=linked), '')

    def test_concurrent_operator_edit_is_not_rolled_back(self):
        self.changed_tip()
        git, link = self.core.git, os.link
        injected = []
        def edit():
            if not injected:
                (self.home / 'shared').write_text('concurrent operator edit\n')
                injected.append(True)
        def racing_git(*args, **kwargs):
            if args[:2] == ('merge', '--ff-only'):
                edit()
            return git(*args, **kwargs)
        def racing_link(src, dst, *args, **kwargs):
            result = link(src, dst, *args, **kwargs)
            if 'files' in Path(src).parts:
                edit()
                raise OSError('injected failure after concurrent operator edit')
            return result
        with mock.patch.object(self.core, 'git', side_effect=racing_git), mock.patch.object(os, 'link', side_effect=racing_link):
            with self.assertRaises(SystemExit):
                self.publish()
        self.assertTrue(injected, 'exercise the real Git boundary or old checkout mutation')
        self.assertEqual((self.home / 'shared').read_text(), 'concurrent operator edit\n')
        self.assertEqual(self.git('rev-parse', 'main', cwd=self.home).strip(), self.base)

    def test_partial_git_checkout_failure_reports_state_without_repair(self):
        self.changed_tip()
        git, link = self.core.git, os.link
        mutations = []
        def failing_git(*args, **kwargs):
            if args[:2] == ('merge', '--ff-only'):
                self.git('checkout', self.tip, '--', 'shared', cwd=self.home)
                mutations.append(True)
                return subprocess.CompletedProcess(args, 128, '', 'injected partial Git checkout failure')
            return git(*args, **kwargs)
        def failing_link(src, dst, *args, **kwargs):
            result = link(src, dst, *args, **kwargs)
            if 'files' in Path(src).parts:
                mutations.append(True)
                raise OSError('injected partial checkout failure')
            return result
        with mock.patch.object(self.core, 'git', side_effect=failing_git), mock.patch.object(os, 'link', side_effect=failing_link):
            with self.assertRaises(SystemExit) as error:
                self.publish()
        self.assertTrue(mutations)
        self.assertIn('main=' + self.base, str(error.exception))
        self.assertIn('git status', str(error.exception))
        self.assertIn('merge --ff-only', str(error.exception))
        self.assertEqual((self.home / 'shared').read_text(), 'landed\n')
        self.assertEqual(self.git('rev-parse', 'main', cwd=self.home).strip(), self.base)

    def test_failed_cas_does_not_rewind_same_tip_publication(self):
        self.changed_tip()
        self.git('checkout', '--detach', '-q', self.base, cwd=self.home)
        git = self.core.git
        writes = []
        def racing_git(*args, **kwargs):
            if args[0] == 'update-ref':
                writes.append(args)
                if len(writes) == 1:
                    self.git('update-ref', 'refs/heads/main', self.tip, self.base, cwd=self.home)
            return git(*args, **kwargs)
        with mock.patch.object(self.core, 'git', side_effect=racing_git):
            with self.assertRaises(SystemExit):
                self.publish()
        self.assertEqual(len(writes), 1, 'a known failed CAS never authorizes rollback')
        self.assertEqual(self.git('rev-parse', 'main', cwd=self.home).strip(), self.tip)
        self.assertEqual((self.home / 'shared').read_text(), 'base\n')

    def test_late_ignored_file_refuses_git_checkout(self):
        self.git('checkout', '-qb', 'job', cwd=self.home)
        (self.home / 'keep').write_text('landed file\n')
        self.git('add', 'keep', cwd=self.home)
        self.git('commit', '-qm', 'job', cwd=self.home)
        self.tip = self.git('rev-parse', 'HEAD', cwd=self.home).strip()
        self.git('checkout', '-q', 'main', cwd=self.home)
        (self.home / '.git/info/exclude').write_text('keep\n')
        git, link = self.core.git, os.link
        injected = []
        def create():
            if not injected:
                (self.home / 'keep').write_text('late operator data\n')
                injected.append(True)
        def racing_git(*args, **kwargs):
            if args[:2] == ('merge', '--ff-only'):
                create()
            return git(*args, **kwargs)
        def racing_link(src, dst, *args, **kwargs):
            if str(dst) == 'keep':
                create()
            return link(src, dst, *args, **kwargs)
        with mock.patch.object(self.core, 'git', side_effect=racing_git), mock.patch.object(os, 'link', side_effect=racing_link):
            with self.assertRaises(SystemExit) as error:
                self.publish()
        self.assertTrue(injected)
        self.assertEqual((self.home / 'keep').read_text(), 'late operator data\n')
        self.assertEqual(self.git('rev-parse', 'main', cwd=self.home).strip(), self.base)
        self.assertIn('main=' + self.base, str(error.exception))
        self.assertIn('git status', str(error.exception))
        self.assertIn('merge --ff-only', str(error.exception))

    def test_only_conflicting_open_jobs_are_classified_without_mutating_evidence(self):
        self.changed_tip()
        self.git('branch', 'clean', self.base, cwd=self.home)
        self.git('checkout', '-qb', 'conflict', self.base, cwd=self.home)
        (self.home / 'shared').write_text('conflicting\n')
        self.git('commit', '-qam', 'conflicting', cwd=self.home)
        self.git('checkout', '-q', 'main', cwd=self.home)
        rows = [{'Job': branch, 'Started': '2026-01-01T00:00:00Z', 'Ended': ''}
                for branch in ('job', 'clean', 'conflict')]
        rows.append({'Job': 'ended', 'Ended': '2026-01-01T01:00:00Z'})
        n = {'id': 'a', 'home_path': self.home}
        before = [dict(row) for row in rows]
        result = self.core.conflicting_jobs(n, rows, [{'branch': 'job'}], self.tip)
        self.assertEqual([item['branch'] for item in result], ['conflict'])
        self.assertEqual(result[0]['head'], self.git('rev-parse', 'conflict', cwd=self.home).strip())
        self.assertEqual(rows, before)
        # Recovery/ledger publication must preserve these unlanded rows too.
        jobs = self.core.finish.__globals__['_m_jobs']
        guard = self.core.finish.__globals__['_m_guard']
        with mock.patch.object(jobs, 'read_jobs', return_value=rows), mock.patch.object(jobs, 'write_jobs') as write, \
                mock.patch.object(guard, 'never_terms', return_value=[]), contextlib.redirect_stdout(io.StringIO()):
            path = self.tmp / 'journal.json'
            self.core.finish(mock.Mock(), n, path,
                             {'accepted': [], 'rebase': result, 'state': 'verified'})
            write.assert_called_once_with(mock.ANY, n, before)
            self.assertEqual([e['branch'] for e in json.loads(path.read_text())['rebase']], ['conflict'])

    def test_unknown_merge_result_refuses_classification(self):
        git = self.core.git
        def failed_git(*args, **kwargs):
            if args[0] == 'merge-tree':
                return subprocess.CompletedProcess(args, 128, '', 'injected failure')
            return git(*args, **kwargs)
        self.tip = self.git('commit-tree', 'HEAD^{tree}', '-p', 'HEAD', '-m', 'landing', cwd=self.home).strip()
        self.git('branch', 'job', self.tip, cwd=self.home)
        with mock.patch.object(self.core, 'git', side_effect=failed_git):
            with self.assertRaisesRegex(SystemExit, 'merge cleanliness is unknown'):
                self.core.conflicting_jobs({'home_path': self.home},
                    [{'Job': 'job', 'Started': '', 'Ended': ''}], [], self.base)
        self.assertEqual(self.git('rev-parse', 'main', cwd=self.home).strip(), self.base)
