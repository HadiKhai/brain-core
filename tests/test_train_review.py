"""Review regressions for rebase occupancy, publication recovery and red-batch proof."""
import argparse
import contextlib
import io
import json
import os
import subprocess
from pathlib import Path
from unittest import mock

import core_loader
from scenario import Scenario, CORE
import test_train


class TrainReviewFixes(Scenario):
    fixture_runtime_only_core = True
    fixture_init_in_process = True
    fixture_templates = True
    project = test_train.MergeTrain.project
    job = test_train.MergeTrain.job
    detach = test_train.MergeTrain.detach
    batches = test_train.MergeTrain.batches
    ledger = test_train.MergeTrain.ledger

    def test_main_mid_conflicting_rebase_is_occupied(self):
        root, home = self.project()
        base = self.git('rev-parse', 'main', cwd=home).strip()
        (home / 'shared').write_text('main change\n')
        self.git('commit', '-qam', 'main change', cwd=home)
        self.job('one')
        self.git('checkout', '-qb', 'onto', base, cwd=home)
        (home / 'shared').write_text('onto change\n')
        self.git('commit', '-qam', 'onto change', cwd=home)
        self.git('checkout', '-q', 'main', cwd=home)
        result = subprocess.run(['git', 'rebase', 'onto'], cwd=home, env=self.env, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('detached', self.git('worktree', 'list', '--porcelain', cwd=home))
        before = self.git('rev-parse', 'main', cwd=home)
        self.assertIn('main is checked out', self.b(root, 'train', 'a', ok=False))
        self.assertEqual(self.git('rev-parse', 'main', cwd=home), before)

    def crash_after_publication(self):
        root, home = self.project()
        self.job('one')
        self.detach()
        ledger = self.ledger()
        self.b(root, 'train', 'a')
        path = next((root / 'nodes/a/job-trains').glob('*.json'))
        data = json.loads(path.read_text())
        data['state'] = 'verified'
        path.write_text(json.dumps(data))
        (root / 'nodes/a/job-ledger.md').write_text(ledger)
        return root, home, path, data

    def test_crash_then_wt_rm_records_merged_batch_and_train_recovers(self):
        root, home, path, data = self.crash_after_publication()
        self.b(root, 'wt', 'rm', 'a', 'one')
        self.assertIn('| merged |', self.ledger())
        self.assertIn(data['batch'], self.ledger())
        self.b(root, 'train', 'a')
        self.assertEqual(json.loads(path.read_text())['state'], 'reconciled')
        self.git('checkout', '--detach', '-q', 'main', cwd=home)
        self.job('two')
        self.b(root, 'train', 'a', '--jobs', 'two')
        self.assertEqual(self.ledger().count('| merged |'), 2)

    def test_recovery_accepts_ended_row_with_same_head(self):
        root, home, path, data = self.crash_after_publication()
        self.b(root, 'wt', 'rm', 'a', 'one')
        # Simulate a pre-fix close: same retained head, but abandoned and no batch.
        ledger = self.ledger().replace('| merged |', '| abandoned |').replace(data['batch'], '')
        (root / 'nodes/a/job-ledger.md').write_text(ledger)
        self.b(root, 'train', 'a')
        self.assertIn('| merged |', self.ledger())
        self.assertIn(data['batch'], self.ledger())
        self.assertEqual(json.loads(path.read_text())['state'], 'reconciled')

    def test_unpublished_verified_tip_is_interrupted_and_job_requeued(self):
        root, home, path, data = self.crash_after_publication()
        self.git('update-ref', 'refs/heads/main', data['base'], cwd=home)
        # Requeue proof stops at staging setup so no new publication masks the recovery result.
        (root / 'nodes/a/worktree-setup.sh').write_text('exit 1\n')
        self.b(root, 'train', 'a', ok=False)
        self.assertEqual(json.loads(path.read_text())['state'], 'interrupted')
        self.assertNotIn('| merged |', self.ledger())
        new = next(b for b in self.batches() if b['batch'] != data['batch'])
        self.assertEqual([e['branch'] for e in new['queued']], ['one'])
        self.assertEqual(self.git('rev-parse', 'main', cwd=home).strip(), data['base'])
        self.assertFalse(self.git('status', '--porcelain', '--', 'nodes/a/job-trains', cwd=root).strip())

    def interaction_project(self, count, bad):
        root, home = self.project()
        condition = ' or '.join("Path('j%d').exists()" % i for i in bad)
        (home / 'verify.py').write_text("from pathlib import Path\nassert not (Path('j1').exists() and (" + condition + "))\n")
        self.git('commit', '-qam', 'interaction verify', cwd=home)
        for i in range(1, count + 1):
            self.job('j%d' % i)
        self.detach()
        return root, home

    def test_eight_jobs_keep_ejected_log_after_rotation(self):
        root, home = self.interaction_project(8, [8])
        self.b(root, 'train', 'a', '--jobs', *['j%d' % i for i in range(1, 9)])
        batch = self.batches()[0]
        self.assertEqual(batch['ejected'][0]['branch'], 'j8')
        log = Path(batch['ejected'][0]['log'])
        self.assertTrue(log.is_file())
        self.assertEqual(log.parent, root / 'nodes/a/job-trains')
        self.assertIn('AssertionError', log.read_text())
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.git('ls-files', '--', str(log.relative_to(root)), cwd=root).strip())

    def test_lone_red_full_prefix_is_confirmed_before_ejection(self):
        root, home = self.project('python3 -B verify.py')
        (home / 'verify.py').write_text("import os\nfrom pathlib import Path\np = Path(os.environ['MAIN']).parent / 'once'\nif os.environ['BRANCH'].startswith('train/') and not p.exists():\n p.touch()\n raise AssertionError('transient')\n")
        self.git('commit', '-qam', 'transient verify', cwd=home)
        self.job('one')
        self.detach()
        self.b(root, 'train', 'a')
        self.assertFalse(self.batches()[0]['ejected'])
        self.assertIn('| merged |', self.ledger())

    def test_baseline_proven_once_for_multiple_culprits(self):
        root, home = self.interaction_project(4, [2, 4])
        self.b(root, 'train', 'a', '--jobs', 'j1', 'j2', 'j3', 'j4')
        data = self.batches()[0]
        self.assertEqual([e['branch'] for e in data['ejected']], ['j2', 'j4'])
        self.assertEqual(sum(not run['jobs'] for run in data['runs']), 1)

    def test_prepublication_clean_main_checkout_fast_forwards(self):
        root, home = self.project('if test "${BRANCH#train/}" != "$BRANCH"; then git -C "$MAIN" checkout -q main; fi')
        self.job('one')
        self.detach()
        before = self.git('rev-parse', 'main', cwd=home)
        self.assertIn('merged 1 job(s)', self.b(root, 'train', 'a'))
        self.assertNotEqual(self.git('rev-parse', 'main', cwd=home), before)
        self.assertIn('| merged |', self.ledger())
        self.assertEqual((home / 'one').read_text(), 'one\n')
        self.assertFalse(self.git('status', '--porcelain', '--', 'nodes/a/job-trains', cwd=root).strip())

    def test_update_ref_cas_rejects_race_after_explicit_main_check(self):
        root, home = self.project()
        self.job('one')
        self.detach()
        base = self.git('rev-parse', 'main', cwd=home).strip()
        other = self.git('commit-tree', 'main^{tree}', '-p', 'main', '-m', 'concurrent main', cwd=home).strip()
        m = core_loader.SourceFileLoader('train_cas_review', str(CORE / 'bin/brain')).load_module()
        git = m.git
        def racing_git(*args, **kwargs):
            if args[:2] == ('update-ref', '-m'):
                self.git('update-ref', 'refs/heads/main', other, base, cwd=home)
            return git(*args, **kwargs)
        with mock.patch.dict(os.environ, {**self.env, 'BRAIN_ROOT': str(root)}), mock.patch.object(m, 'git', side_effect=racing_git), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(SystemExit, 'main changed during publication'):
                m.cmd_train(argparse.Namespace(id='a', jobs=None))
        self.assertEqual(self.git('rev-parse', 'main', cwd=home).strip(), other)
        self.assertNotIn('| merged |', self.ledger())

    def test_empty_batch_journal_committed(self):
        root, home = self.project('if test "${BRANCH#train/}" != "$BRANCH" && test -f one; then exit 1; fi')
        self.job('one')
        self.detach()
        self.b(root, 'train', 'a')
        self.assertEqual(self.batches()[0]['state'], 'empty')
        self.assertFalse(self.git('status', '--porcelain', '--', 'nodes/a/job-trains', cwd=root).strip())
        self.assertTrue(self.git('ls-files', '--', 'nodes/a/job-trains/*.json', cwd=root).strip())

    def test_failed_same_tip_cas_never_closes_ledger_on_retry(self):
        root, home = self.project()
        self.job('one')
        self.detach()
        m = core_loader.SourceFileLoader('failed_cas_retry', str(CORE / 'bin/brain')).load_module()
        git = m.git
        writes = []
        def racing_git(*args, **kwargs):
            if args[:2] == ('update-ref', '-m'):
                writes.append(args)
                self.git('update-ref', 'refs/heads/main', args[-2], args[-1], cwd=home)
            return git(*args, **kwargs)
        with mock.patch.dict(os.environ, {**self.env, 'BRAIN_ROOT': str(root)}), mock.patch.object(m, 'git', side_effect=racing_git), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(SystemExit, 'main changed during publication'):
                m.cmd_train(argparse.Namespace(id='a', jobs=None))
        self.assertEqual(len(writes), 1)
        data = self.batches()[0]
        self.assertEqual(data['state'], 'publication_failed')
        self.assertEqual(self.git('rev-parse', 'main', cwd=home).strip(), data['tip'])
        self.assertNotIn('| merged |', self.ledger())
        self.assertIn('unresolved publication', self.b(root, 'train', 'a', ok=False))
        self.assertNotIn('| merged |', self.ledger())

    def test_partial_git_failure_and_interrupted_retry_never_record_success(self):
        root, home = self.project()
        self.job('one')
        m = core_loader.SourceFileLoader('partial_merge_retry', str(CORE / 'bin/brain')).load_module()
        git = m.git
        def failing_git(*args, **kwargs):
            if args[:2] == ('merge', '--ff-only'):
                self.git('checkout', args[-1], '--', 'one', cwd=home)
                return subprocess.CompletedProcess(args, 128, '', 'injected Git I/O failure')
            return git(*args, **kwargs)
        with mock.patch.dict(os.environ, {**self.env, 'BRAIN_ROOT': str(root)}), mock.patch.object(m, 'git', side_effect=failing_git), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(SystemExit, 'Git fast-forward failed') as error:
                m.cmd_train(argparse.Namespace(id='a', jobs=None))
        self.assertIn('git status (porcelain):\nA  one', str(error.exception))
        self.assertEqual((home / 'one').read_text(), 'one\n')
        self.assertEqual(self.batches()[0]['state'], 'publication_failed')
        self.assertNotIn('| merged |', self.ledger())
        # Simulate process death before failure observation with main at the tip.
        path = next((root / 'nodes/a/job-trains').glob('*.json'))
        data = json.loads(path.read_text())
        data['state'] = 'publishing'
        path.write_text(json.dumps(data))
        self.git('update-ref', 'refs/heads/main', data['tip'], data['base'], cwd=home)
        self.assertIn('unresolved publication', self.b(root, 'train', 'a', ok=False))
        self.assertNotIn('| merged |', self.ledger())

    def test_linked_worktree_apply_rebase_also_occupies_main(self):
        root, home = self.project()
        self.detach()
        linked = self.tmp / 'linked'
        self.git('worktree', 'add', '--detach', str(linked), 'main', cwd=home)
        gitdir = Path(self.git('rev-parse', '--absolute-git-dir', cwd=linked).strip())
        state = gitdir / 'rebase-apply'
        state.mkdir()
        (state / 'head-name').write_text('refs/heads/main\n')
        self.assertIn('main is checked out', self.b(root, 'train', 'a', ok=False))
        (state / 'head-name').write_text('refs/heads/other\n')
        self.assertIn('no ready jobs', self.b(root, 'train', 'a'))
