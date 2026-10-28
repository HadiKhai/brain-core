"""Prune and message preparation use fabricated workspaces and host references only."""
import argparse
import contextlib
import importlib.util
import io
import json
import os
import shutil
from pathlib import Path
from unittest import mock

import core_loader
from scenario import CORE, AutonomyProject


class PruneThreadLifecycle(AutonomyProject):
    def setUp(self):
        super().setUp()
        loader = core_loader.SourceFileLoader('orphan_fixture', str(CORE / 'bin/brain'))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        self.core = importlib.util.module_from_spec(spec)
        loader.exec_module(self.core)

    def merged(self):
        root, home, wt = self.project()
        (wt / 'result').write_text('done\n')
        self.git('add', 'result', cwd=wt)
        self.git('commit', '-qm', 'result', cwd=wt)
        self.git('merge', '-q', 'job-a', cwd=home)
        return root, home, wt

    def bind(self, root, source='both'):
        if source in ('both', 'worker'):
            n = self.core.Instance(root).node('app')
            row = self.core.open_job(self.core.read_jobs(n), 'job-a')
            key = self.core.job_key(row)
            self.b(root, 'set', 'app', 'worker:' + key + '=fixture-bound-worker')
        if source in ('both', 'active'):
            status = root / 'nodes/app/status.md'
            status.write_text(status.read_text() + '\n## Active work\n\n'
                              '| Branch | Thread | PR | State |\n| --- | --- | --- | --- |\n'
                              '| `job-a` | fixture-active-worker | - | merged |\n')

    def test_prune_lists_both_bindings_before_removal_and_close_checklist(self):
        root, home, wt = self.merged()
        self.bind(root)
        inst = self.core.Instance(root)
        original_remove = self.core.remove_worktree
        output = io.StringIO()

        def remove(*args, **kwargs):
            self.assertTrue(wt.exists())
            self.assertIn('fixture-bound-worker (worker:', output.getvalue())
            self.assertIn('fixture-active-worker (Active work)', output.getvalue())
            self.assertIn('Required order before this command: worker told the job is done', output.getvalue())
            return original_remove(*args, **kwargs)

        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(self.core, 'load', return_value=inst), \
                mock.patch.object(self.core, 'remove_worktree', side_effect=remove), \
                contextlib.redirect_stdout(output):
            self.core.cmd_wt_prune(argparse.Namespace(id='app', dry_run=False, offline=True))
        out = output.getvalue()
        self.assertIn('fixture-bound-worker (worker:', out)
        self.assertIn('fixture-active-worker (Active work)', out)
        self.assertLess(out.index('bound worker threads'), out.index('Threads to settle'))
        self.assertLess(out.index('worker told the job is done'), out.index('then safely settled'))
        self.assertIn('While its workspace still exists, message the thread before pruning', out)
        self.assertIn('This command removes the worktree now. Do not message these threads after removal.', out)
        self.assertIn('If its worktree is already gone, do not message it', out)
        self.assertLess(out.index('Required order before this command'), out.index('removed: job-a'))
        self.assertFalse(wt.exists())

    def test_dry_run_preserves_ledger_local_and_stale_git_registration(self):
        root, home, wt = self.merged()
        self.bind(root)
        # A missing Git worktree registration must survive a listing-only command.
        extra = wt.parent / 'unused'
        self.git('worktree', 'add', '-qb', 'unused', str(extra), cwd=home)
        shutil.rmtree(extra)
        before_git = self.git('worktree', 'list', '--porcelain', cwd=home)
        tracked = [root / '.brain/local.json', root / 'nodes/app/job-ledger.md']
        before = [p.read_bytes() for p in tracked]
        out = self.b(root, 'wt', 'prune', 'app', '--dry-run')
        self.assertIn('fixture-bound-worker', out)
        self.assertIn('fixture-active-worker', out)
        self.assertIn('dry-run: listing only', out)
        self.assertIn('Required order before removal: message the thread before pruning', out)
        self.assertIn('Then confirm it has stopped', out)
        self.assertNotIn('This command removes the worktree now', out)
        self.assertNotIn('Threads to settle', out)
        self.assertIn('would remove: job-a', out)
        self.assertTrue(wt.exists())
        self.assertEqual(before, [p.read_bytes() for p in tracked])
        self.assertEqual(before_git, self.git('worktree', 'list', '--porcelain', cwd=home))

    def test_dry_run_does_not_fetch_or_prune_git(self):
        root, home, wt = self.merged()
        self.git('remote', 'add', 'origin', str(self.tmp / 'unused-origin'), cwd=home)
        inst = self.core.Instance(root)
        original_git = self.core.git
        calls = []

        def git(*args, **kwargs):
            calls.append(args)
            return original_git(*args, **kwargs)

        with mock.patch.object(self.core, 'load', return_value=inst), \
                mock.patch.object(self.core, 'git', side_effect=git), contextlib.redirect_stdout(io.StringIO()):
            self.core.cmd_wt_prune(argparse.Namespace(id='app', dry_run=True, offline=False))
        self.assertFalse(any(args[0] == 'fetch' for args in calls))
        self.assertNotIn(('worktree', 'prune'), calls)
        self.assertTrue(wt.exists())

    def test_prune_already_settled_job_is_not_blocked(self):
        root, home, wt = self.merged()
        self.bind(root)
        inst = self.core.Instance(root)
        n = inst.node('app')
        rows = self.core.read_jobs(n)
        self.core.open_job(rows, 'job-a').update(Ended='2026-01-01T12:00+00:00', Outcome='merged')
        self.core.write_jobs(inst, n, rows)
        self.b(root, 'jobs', 'app', '--branch', 'job-a', '--settled',
               '--thread', 'fixture-bound-worker', '--thread', 'fixture-active-worker',
               '--attestation-file', __import__('test_jobs').settlement_export(self, root, 'app', branch='job-a'))
        out = self.b(root, 'wt', 'prune', 'app', '--offline')
        self.assertIn('fixture-bound-worker', out)
        self.assertIn('Already settled threads need no message', out)
        self.assertIn('removed: job-a', out)
        self.assertFalse(wt.exists())

    def test_message_helpers_warn_for_each_binding_source_after_workspace_loss(self):
        for source in ('worker', 'active'):
            with self.subTest(source=source):
                root, home, wt = self.merged()
                self.bind(root, source)
                target = 'fixture-bound-worker' if source == 'worker' else 'fixture-active-worker'
                # While the worktree exists, this is a valid send target.
                good = self.b(root, 'brief', 'app', '--task', 'fixture', '--to', target)
                self.assertNotIn('bound worker thread workspace', good)
                shutil.rmtree(wt)
                for command in (('brief', 'app', '--task', 'fixture', '--to', target),
                                ('review', 'app', '--branch', 'job-a', '--built-by', 'codex')):
                    # Review hand-backs default to the coordinator; exercise that path too.
                    self.b(root, 'set', 'app', 'coordinator=' + target)
                    if command[0] == 'review':
                        # A different live review branch avoids its independent workspace refusal.
                        self.b(root, 'wt', 'add', 'app', 'job-review')
                        command = ('review', 'app', '--branch', 'job-review', '--built-by', 'codex')
                    out = self.b(root, *command)
                    self.assertIn('warning: job job-a:', out)
                    self.assertIn('fresh thread in a fresh worktree', out)
                # Cleanup drops the path binding; retained thread references must still warn.
                local_path = root / '.brain/local.json'
                local = json.loads(local_path.read_text())
                local['nodes']['app']['worktrees'].pop('job-a')
                local_path.write_text(json.dumps(local))
                out = self.b(root, 'brief', 'app', '--task', 'fixture', '--to', target)
                self.assertIn('warning: job job-a:', out)
                other = self.b(root, 'brief', 'app', '--task', 'fixture', '--to', 'unrelated-thread')
                self.assertNotIn('bound worker thread workspace', other)
                shutil.rmtree(root)
                shutil.rmtree(home.parent)

    def test_rotation_warns_in_output_and_handover(self):
        root, home, wt = self.merged()
        self.bind(root)
        shutil.rmtree(wt)
        out = self.b(root, 'rotate', 'app')
        self.assertIn('warning: job job-a:', out)
        packet = (root / 'nodes/app/handover.md').read_text()
        self.assertIn('warning: job job-a:', packet)
        self.assertIn('fresh thread in a fresh worktree', packet)

    def test_import_study_threads_do_not_need_worktrees(self):
        root, home, wt = self.merged()
        branch = 'import:' + self.git('rev-parse', 'HEAD', cwd=home).strip()
        self.b(root, 'jobs', 'app', '--branch', branch, '--start')
        n = self.core.Instance(root).node('app')
        row = self.core.open_job(self.core.read_jobs(n), branch)
        self.b(root, 'set', 'app', 'worker:' + self.core.job_key(row) + '=fixture-study-worker')
        status = root / 'nodes/app/status.md'
        status.write_text(status.read_text() + '\n## Active work\n\n'
                          '| Branch | Thread | PR | State |\n| --- | --- | --- | --- |\n'
                          '| ' + branch + ' | fixture-study-worker | - | running |\n')
        for command in (('brief', 'app', '--task', 'fixture', '--to', 'fixture-study-worker'),
                        ('rotate', 'app')):
            self.assertNotIn('bound worker thread workspace', self.b(root, *command))
        self.assertNotIn('bound worker thread workspace', (root / 'nodes/app/handover.md').read_text())

    def test_unknown_machine_workspace_is_not_reported_as_gone(self):
        root, home, wt = self.merged()
        self.bind(root, 'active')
        local_path = root / '.brain/local.json'
        local = json.loads(local_path.read_text())
        local['nodes']['app']['worktrees'].pop('job-a')
        local_path.write_text(json.dumps(local))
        out = self.b(root, 'brief', 'app', '--task', 'fixture', '--to', 'fixture-active-worker')
        self.assertIn('not registered on this machine; confirm its host workspace before sending', out)
        self.assertNotIn('workspace is gone', out)

    def test_rotation_omits_settled_history_but_targeted_send_still_warns(self):
        root, home, wt = self.merged()
        self.bind(root)
        self.b(root, 'wt', 'prune', 'app', '--offline')
        self.b(root, 'jobs', 'app', '--branch', 'job-a', '--settled',
               '--thread', 'fixture-bound-worker', '--thread', 'fixture-active-worker',
               '--attestation-file', __import__('test_jobs').settlement_export(self, root, 'app', branch='job-a'))
        self.assertNotIn('bound worker thread workspace', self.b(root, 'rotate', 'app'))
        out = self.b(root, 'brief', 'app', '--task', 'fixture', '--to', 'fixture-bound-worker')
        self.assertIn('warning: job job-a:', out)

    def test_rm_lists_threads_and_post_removal_recovery(self):
        root, home, wt = self.merged()
        self.bind(root)
        out = self.b(root, 'wt', 'rm', 'app', 'job-a')
        self.assertIn('fixture-bound-worker (worker:', out)
        self.assertLess(out.index('bound worker threads'), out.index('Threads to settle'))
        self.assertIn('If its worktree is already gone, do not message it', out)
        self.assertFalse(wt.exists())

    def test_unbound_cleanup_lists_none_without_host_steps(self):
        root, home, wt = self.merged()
        out = self.b(root, 'wt', 'prune', 'app', '--offline')
        listing = out.split('Threads to settle')[0]
        self.assertIn('none recorded', listing)
        self.assertNotIn('Required order before', listing)
        self.assertNotIn('message the thread before pruning', listing)

    def test_missing_own_worktree_refusals_suggest_fresh_thread(self):
        root, home, wt = self.merged()
        shutil.rmtree(wt)
        for command in (('brief', 'app', '--branch', 'job-a', '--task', 'fixture'),
                        ('review', 'app', '--branch', 'job-a', '--built-by', 'codex')):
            out = self.b(root, *command, ok=False)
            self.assertIn('the worktree for job-a is gone', out)
            self.assertIn('fresh thread in a fresh worktree', out)

    def test_stale_warnings_are_redacted_and_internal_threads_are_valid(self):
        root, home, wt = self.merged()
        self.bind(root)
        shutil.rmtree(wt)
        (root / 'nodes/app/never-name.txt').write_text('job-a\n')
        # The shared formatter must redact its own diagnostic, not just the final brief.
        inst = self.core.Instance(root)
        n = inst.node('app')
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.core.host_send(inst, n, 'fixture-bound-worker')
        self.assertIn('warning:', err.getvalue())
        self.assertNotIn('job-a', err.getvalue())
        n['external'] = False
        self.assertEqual(self.core.stale_thread_warnings(n), [])
