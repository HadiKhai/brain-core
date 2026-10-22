"""Thread display links and job incarnations cannot hide a stale workspace."""
import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import core_loader
from scenario import CORE, AutonomyProject


def load_target_core():
    loader = core_loader.SourceFileLoader('thread_targets_fixture', str(CORE / 'bin/brain'))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    core = importlib.util.module_from_spec(spec)
    loader.exec_module(core)
    return core


class ThreadTargetOwnership(unittest.TestCase):
    def setUp(self):
        self.core = load_target_core()
        self.temp = tempfile.TemporaryDirectory(prefix='brain-target-test-')
        self.addCleanup(self.temp.cleanup)
        self.node = {'id': 'app', 'dir': Path(self.temp.name), 'external': True,
                     'local': {'worktrees': {}, 'workers': {}}, 'parent': None}
        self.old = {'Job': 'job-a', 'Started': '2026-01-01T10:00+00:00',
                    'Ended': '2026-01-01T11:00+00:00', 'Outcome': 'merged'}
        self.current = {'Job': 'job-a', 'Started': '2026-01-02T10:00+00:00', 'Ended': ''}

    def warnings(self, rows, active, target):
        with mock.patch.object(self.core, 'read_jobs', return_value=rows), \
                mock.patch.object(self.core, 'active_work', return_value=active):
            return self.core.stale_thread_warnings(self.node, target)

    def test_linked_or_annotated_references_match_the_exact_target_id(self):
        for reference in ['fixture-worker', '`fixture-worker`', 'fixture-worker (Codex)',
                          't3-thread://v1/fixture-worker', '<t3-thread://v1/fixture-worker>',
                          '[fixture worker](t3-thread://v1/fixture-worker) (Codex)']:
            with self.subTest(reference=reference):
                active = [['job-a', reference, '-', 'merged']]
                warnings = self.warnings([self.old], active, 'fixture-worker')
                self.assertEqual(len(warnings), 1)
                self.assertIn('job job-a:', warnings[0])
                self.assertIn('fresh thread in a fresh worktree', warnings[0])
                self.assertEqual(self.warnings([self.old], active, 'fixture-work'), [])
                self.assertEqual(self.warnings([self.old], active, 'Fixture-worker'), [])
        with mock.patch.object(self.core, 'read_jobs', return_value=[self.old]), \
                mock.patch.object(self.core, 'active_work', return_value=active):
            self.assertIn((reference, 'Active work'), self.core.bound_worker_threads(self.node, 'job-a'))

    def test_current_workspace_does_not_establish_historical_target_ownership(self):
        old_key, current_key = self.core.job_key(self.old), self.core.job_key(self.current)
        self.node['local']['workers'] = {old_key: 'old-worker', current_key: 'new-worker'}
        self.node['local']['worktrees']['job-a'] = {'path': self.temp.name}
        active = [['job-a', '[old](t3-thread://v1/old-worker)', '-', 'merged'],
                  ['job-a', 'new-worker', '-', 'working']]
        self.assertEqual(len(self.warnings([self.old, self.current], active, 'old-worker')), 1)
        self.assertEqual(self.warnings([self.old, self.current], active, 'new-worker'), [])
        self.assertEqual(len(self.warnings([self.old, self.current], active, None)), 1)

    def test_ambiguous_active_reference_or_reused_worker_binding_still_warns(self):
        self.node['local']['worktrees']['job-a'] = {'path': self.temp.name}
        active = [['job-a', 'fixture-worker', '-', 'working']]
        self.assertEqual(len(self.warnings([self.old, self.current], active, 'fixture-worker')), 1)
        self.node['local']['workers'] = {
            self.core.job_key(self.old): 'fixture-worker',
            self.core.job_key(self.current): 'fixture-worker'}
        self.assertEqual(len(self.warnings([self.old, self.current], active, 'fixture-worker')), 1)
        duplicate = dict(self.current, Started=self.old['Started'])
        self.node['local']['workers'] = {self.core.job_key(duplicate): 'fixture-worker'}
        self.assertEqual(len(self.warnings([self.old, duplicate], active, 'fixture-worker')), 1)
        self.node['local']['workers'] = {self.core.job_key(self.current):
            '[first](t3-thread://v1/fixture-worker) [second](t3-thread://v1/other-worker)'}
        self.assertEqual(len(self.warnings([self.old, self.current], active, 'fixture-worker')), 1)

    def test_unbound_historical_active_reference_cannot_borrow_current_binding(self):
        self.node['local']['worktrees']['job-a'] = {'path': self.temp.name}
        self.node['local']['workers'] = {self.core.job_key(self.current): 'new-worker'}
        active = [['job-a', 'old-worker', '-', 'merged'], ['job-a', 'new-worker', '-', 'working']]
        self.assertEqual(len(self.warnings([self.old, self.current], active, None)), 1)
        self.assertEqual(self.warnings([self.old, self.current], active, 'new-worker'), [])


class ThreadTargetCommands(AutonomyProject):
    def test_brief_warns_for_a_linked_target_after_workspace_loss(self):
        root, home, wt = self.project()
        (root / 'nodes/app/status.md').write_text(
            '## Active work\n| Branch | Thread | PR | State |\n'
            '| job-a | [fixture worker](t3-thread://v1/fixture-worker) (Codex) | - | merged |\n')
        shutil.rmtree(wt)
        out = self.b(root, 'brief', 'app', '--task', 'fixture task', '--to', 'fixture-worker')
        self.assertIn('warning: job job-a:', out)

    def test_pruned_target_stays_stale_after_branch_recreation_elsewhere(self):
        root, home, old_wt = self.project()
        core = load_target_core()
        (old_wt / 'result').write_text('fixture done\n')
        self.git('add', 'result', cwd=old_wt)
        self.git('commit', '-qm', 'fixture result', cwd=old_wt)
        self.git('merge', '-q', 'job-a', cwd=home)
        n = core.Instance(root).node('app')
        old_key = core.job_key(core.open_job(core.read_jobs(n), 'job-a'))
        self.b(root, 'set', 'app', 'worker:' + old_key + '=old-worker')
        self.b(root, 'wt', 'prune', 'app', '--offline')
        self.b(root, 'set', 'app', 'workbench=' + str(self.tmp / 'fresh-workbench'))
        self.b(root, 'wt', 'add', 'app', 'job-a')
        n = core.Instance(root).node('app')
        current_key = core.job_key(core.open_job(core.read_jobs(n), 'job-a'))
        self.assertNotEqual(old_key, current_key)
        self.b(root, 'set', 'app', 'worker:' + current_key + '=new-worker')
        new_wt = Path(json.loads((root / '.brain/local.json').read_text())['nodes']['app']['worktrees']['job-a']['path'])
        self.assertTrue(new_wt.is_dir())
        self.assertFalse(old_wt.exists())
        old = self.b(root, 'brief', 'app', '--task', 'fixture task', '--to', 'old-worker')
        self.assertIn('warning: job job-a:', old)
        self.assertIn('fresh thread in a fresh worktree', old)
        current = self.b(root, 'brief', 'app', '--task', 'fixture task', '--to', 'new-worker')
        self.assertNotIn('bound worker thread workspace', current)
