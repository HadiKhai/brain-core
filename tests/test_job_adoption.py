"""Adopting existing work preserves its checkout and records only known ownership."""
import json

from scenario import Scenario


class JobAdoption(Scenario):
    def setUp(self):
        super().setUp()
        self.root = self.init('oak')
        self.home = self.repo(self.tmp / 'code')
        self.b(self.root, 'new', 'app', '--title', 'App', '--home', str(self.home))
        self.b(self.root, 'set', 'app', 'runtime=codex')
        self.wt = self.tmp / 'external-work'
        self.git('worktree', 'add', '-b', 'job/palette', str(self.wt), cwd=self.home)
        (self.wt / 'draft.txt').write_text('unfinished drawing\n')

    def adopt(self, *extra, ok=True):
        return self.b(self.root, 'jobs', 'app', '--start', '--branch', 'job/palette',
                      '--thread', 'fixture-worker', *extra, ok=ok)

    def test_existing_worktree_is_adopted_without_touching_contents(self):
        before = self.git('status', '--porcelain=v1', cwd=self.wt)
        head = self.git('rev-parse', 'HEAD', cwd=self.wt)
        output = self.adopt()
        self.assertIn('adopted', output)
        local = json.loads((self.root / '.brain/local.json').read_text())['nodes']['app']
        self.assertEqual(local['worktrees']['job/palette']['path'], str(self.wt))
        self.assertEqual(self.git('status', '--porcelain=v1', cwd=self.wt), before)
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=self.wt), head)
        row = json.loads(self.b(self.root, 'jobs', 'app', '--live'))
        self.assertEqual(row['state'], 'open')
        self.assertEqual(row['host_activity'], 'unknown')
        self.assertEqual(local['workers'][row['job_key']], 'fixture-worker')
        self.assertEqual(row['ledger']['Runtime'], 'codex')
        self.assertIsNone(row['handback'])
        self.assertIn('job/palette', self.b(self.root, 'brief', 'app', '--branch', 'job/palette',
                                          '--task', 'Finish the drawing'))

    def test_repeated_exact_adoption_is_idempotent(self):
        self.adopt()
        first = json.loads(self.b(self.root, 'jobs', 'app', '--live'))
        self.adopt()
        self.assertEqual(json.loads(self.b(self.root, 'jobs', 'app', '--live')), first)
        self.assertIn('already', self.adopt())

    def test_coordinator_thread_and_rebinding_are_refused(self):
        self.b(self.root, 'set', 'app', 'coordinator=fixture-worker')
        self.adopt(ok=False)
        self.assertNotIn('job/palette', (self.root / '.brain/local.json').read_text())
        self.b(self.root, 'set', 'app', 'coordinator=fixture-coordinator')
        self.adopt()
        self.assertIn('already', self.adopt())
        self.assertIn('refused', self.b(self.root, 'jobs', 'app', '--start', '--branch', 'job/palette',
                                       '--thread', 'another-worker', ok=False))

    def test_root_checkout_and_absent_branch_are_refused(self):
        for branch in ('main', 'job/absent'):
            output = self.b(self.root, 'jobs', 'app', '--start', '--branch', branch,
                            '--thread', 'fixture-worker', ok=False)
            self.assertIn('refused', output)
        self.assertNotIn('job/palette', (self.root / '.brain/local.json').read_text())
