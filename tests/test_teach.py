"""Native teaching uses registered isolated worktrees and checked, scoped adaptations."""
from scenario import AutonomyProject


class NativeTeaching(AutonomyProject):
    def fixture(self):
        r, home, worktree = self.project()
        self.b(r, 'item', 'add', 'short-labels', '--from', 'app', '--kind', 'standard',
               '--title', 'Short labels', '--ref', 'notes.md:1', '--body', 'Use concise navigation labels.')
        adapted = self.tmp / 'native.md'
        adapted.write_text('# Native standard\nUse concise navigation labels in the application.\n')
        check = worktree / 'checks/native.py'
        check.parent.mkdir()
        check.write_text('from pathlib import Path\nassert "concise navigation" in Path("docs/standard.md").read_text()\n')
        return r, home, worktree, adapted, check

    def teach(self, r, adapted, *extra, ok=True):
        return self.b(r, 'teach', 'short-labels', 'app', '--branch', 'job-a', '--file', str(adapted),
                      '--to', 'docs/standard.md', '--check', 'checks/native.py', *extra, ok=ok)

    def test_applies_native_copy_after_check_and_keeps_source_and_main_checkout(self):
        r, home, worktree, adapted, check = self.fixture()
        source = (r / 'library/short-labels.md').read_bytes()
        self.assertIn('native teaching applied', self.teach(r, adapted))
        self.assertEqual((worktree / 'docs/standard.md').read_bytes(), adapted.read_bytes())
        self.assertFalse((home / 'docs/standard.md').exists())
        self.assertEqual((r / 'library/short-labels.md').read_bytes(), source)
        self.assertEqual(len(list((r / '.brain/teaching').glob('*.json'))), 1)
        self.assertNotIn('short-labels', (worktree / 'docs/standard.md').read_text())

    def test_failed_check_rolls_back_only_new_teaching_and_creates_no_receipt(self):
        r, home, worktree, adapted, check = self.fixture()
        check.write_text('raise SystemExit(1)\n')
        self.assertIn('native teaching check failed', self.teach(r, adapted, ok=False))
        self.assertFalse((worktree / 'docs/standard.md').exists())
        self.assertFalse(list((r / '.brain/teaching').glob('*.json')))
        self.assertFalse((worktree / 'docs').exists())

    def test_scope_paths_existing_generated_files_and_stale_overwrite_are_refused(self):
        import hashlib
        r, home, worktree, adapted, check = self.fixture()
        target = worktree / 'docs/standard.md'
        target.parent.mkdir()
        target.write_text('old native rule\n')
        self.teach(r, adapted, ok=False)
        self.assertEqual(target.read_text(), 'old native rule\n')
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        self.assertIn('native teaching applied', self.teach(r, adapted, '--expected-sha256', digest))
        self.teach(r, adapted, '--expected-sha256', digest, ok=False)
        self.b(r, 'teach', 'short-labels', 'app', '--branch', 'job-a', '--file', str(adapted),
               '--to', '../escape.md', '--check', 'checks/native.py', ok=False)
        self.b(r, 'item', 'quarantine', 'short-labels', '--reason', 'withdrawn')
        self.teach(r, adapted, ok=False)

    def test_default_brief_is_read_only_and_names_the_native_adaptation_steps(self):
        r, home, worktree, adapted, check = self.fixture()
        out = self.b(r, 'teach', 'short-labels', 'app')
        self.assertIn('native conventions', out)
        self.assertIn('concise navigation', out)
        self.assertFalse((worktree / 'docs').exists())

    def test_generated_output_and_owner_scope_refuse_without_writes(self):
        import hashlib
        r, home, worktree, adapted, check = self.fixture()
        target = worktree / 'docs/standard.md'
        target.parent.mkdir()
        target.write_text('DO NOT EDIT: generated file\n')
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        self.assertIn('generator source', self.teach(r, adapted, '--expected-sha256', digest, ok=False))
        self.assertEqual(target.read_text(), 'DO NOT EDIT: generated file\n')
        foreign = self.repo(self.tmp / 'foreign')
        self.b(r, 'principal', 'add', 'team')
        self.b(r, 'new', 'foreign', '--title', 'Foreign', '--owners', 'team', '--home', str(foreign))
        self.assertIn('cannot be taught', self.b(r, 'teach', 'short-labels', 'foreign', ok=False))
