"""Pinned preview diff reuse must retain live classification authority."""
import os
import subprocess
from pathlib import Path
from unittest import mock

import test_guard
from scenario import Scenario


class PreviewImmutableQueries(Scenario):
    fixture_templates = True
    fixture_init_in_process = True
    context = test_guard.PreviewDiffScoping.context
    declaration = test_guard.PreviewDiffScoping.declaration
    change = test_guard.PreviewDiffScoping.change

    def test_identical_diff_runs_git_once_but_policy_and_ref_changes_are_seen(self):
        m, inst, n, data = self.context()
        self.change(m, inst, n, data, {'server/api.py': 'backend\n'})
        original, calls = m.git, []
        def counted(*args, **kwargs):
            calls.append(args)
            return original(*args, **kwargs)
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(m, 'git', side_effect=counted), \
                m.git_read_snapshot():
            first = m.preview_requirement(inst, n, 'job', data['head'])
            self.assertEqual(first['status'], 'not_applicable')
            self.assertEqual(m.preview_requirement(inst, n, 'job', data['head']), first)
            self.assertEqual(sum(args[0] == 'diff' for args in calls), 1)
            self.declaration(m, inst, n, 'ui_globs=server/**')
            self.assertEqual(m.preview_requirement(inst, n, 'job', data['head'])['status'], 'required')
            self.assertEqual(sum(args[0] == 'diff' for args in calls), 1)
            self.git('update-ref', 'refs/heads/job', 'main', cwd=n['home_path'])
            self.assertEqual(m.preview_requirement(inst, n, 'job', data['head'])['status'], 'required')
        with mock.patch.dict(os.environ, self.env, clear=True), m.git_read_snapshot():
            self.assertEqual(m.preview_requirement(inst, n, 'job', data['head'])['status'], 'required')

    def test_new_head_and_base_get_new_diffs_and_existing_receipts_still_refuse(self):
        m, inst, n, data = self.context()
        self.change(m, inst, n, data, {'server/api.py': 'backend\n'})
        with mock.patch.dict(os.environ, self.env, clear=True), m.git_read_snapshot():
            first = m.preview_requirement(inst, n, 'job', data['head'])
            self.assertEqual(first['status'], 'not_applicable')
            scope = self.change(m, inst, n, data, {'ui/page.html': 'rendered\n'})
            self.assertEqual(scope['status'], 'required')
            self.assertEqual(scope['paths'], ['server/api.py', 'ui/page.html'])
            self.assertTrue(m.validate_ready_preview(inst, n, 'job', data['head']))
            self.git('update-ref', 'refs/heads/main', data['head'], cwd=n['home_path'])
            scope = m.preview_requirement(inst, n, 'job', data['head'])
            self.assertEqual(scope['status'], 'not_applicable')
            self.assertEqual(scope['paths'], [])
            path = m.preview_receipt_path(inst, n, m.read_jobs(n)[-1], data['head'])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{}')
            self.assertTrue(m.validate_ready_preview(inst, n, 'job', data['head']))

    def test_diff_config_changes_and_failed_diff_reads_do_not_reuse_exemptions(self):
        m, inst, n, data = self.context()
        self.change(m, inst, n, data, {'server/api.py': 'backend\n'})
        original, pending = m.git, [False]
        def unreadable(*args, **kwargs):
            if args[0] == 'diff' and pending[0]:
                return subprocess.CompletedProcess(args, 128, '', 'unreadable')
            return original(*args, **kwargs)
        with mock.patch.dict(os.environ, self.env, clear=True), m.git_read_snapshot(), \
                mock.patch.object(m, 'git', side_effect=unreadable):
            self.assertEqual(m.preview_requirement(inst, n, 'job', data['head'])['status'], 'not_applicable')
            self.git('config', 'diff.renames', 'false', cwd=n['home_path'])
            pending[0] = True
            self.assertEqual(m.preview_requirement(inst, n, 'job', data['head'])['status'], 'required')
            pending[0] = False
            self.assertEqual(m.preview_requirement(inst, n, 'job', data['head'])['status'], 'not_applicable')

    def test_rename_across_the_rendered_boundary_retains_both_paths(self):
        m, inst, n, data = self.context()
        self.change(m, inst, n, data, {'source.html': 'same\n'})
        self.git('merge', '--ff-only', 'job', cwd=n['home_path'])
        wt = Path(n['local']['worktrees']['job']['path'])
        (wt / 'ui').mkdir()
        self.git('mv', 'source.html', 'ui/page.html', cwd=wt)
        self.git('commit', '-qm', 'Move into rendered inputs', cwd=wt)
        data['head'] = self.git('rev-parse', 'HEAD', cwd=wt).strip()
        with mock.patch.dict(os.environ, self.env, clear=True), m.git_read_snapshot():
            scope = m.preview_requirement(inst, n, 'job', data['head'])
            self.assertEqual(scope['status'], 'required')
            self.assertEqual(scope['paths'], ['source.html', 'ui/page.html'])
            self.assertEqual(m.preview_requirement(inst, n, 'job', data['head']), scope)
