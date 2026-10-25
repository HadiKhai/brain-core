"""Declared snapshots use fabricated repositories only; no rehearsal is run."""
import json
from pathlib import Path
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from brain import instance, rehearse, rehearse_external
from scenario import Scenario


class DeclaredExternalSnapshot(Scenario):
    def fixture(self):
        root = self.init('demo')
        home = self.repo(self.tmp / 'source')
        self.b(root, 'new', 'app', '--title', 'App', '--home', str(home))
        self.b(root, 'wt', 'add', 'app', 'job/sample', '--no-setup')
        inst = instance.Instance(root)
        tree = Path(inst.node('app')['local']['worktrees']['job/sample']['path'])
        data = inst.local
        data['rehearse_external'] = {'version': 1, 'roots': [str(home), str(tree)]}
        (root / '.brain/local.json').write_text(json.dumps(data))
        return instance.Instance(root), home, tree

    def test_declared_home_and_worktree_survive_peer_writes_with_detached_git(self):
        inst, home, tree = self.fixture()
        (tree / 'dirty.txt').write_text('captured dirty bytes')
        before = rehearse.snapshot(home)
        private = rehearse_external.freeze_declared(inst, self.tmp / 'captured')
        node = private.node('app')
        captured_tree = Path(node['local']['worktrees']['job/sample']['path'])
        self.assertNotEqual(node['home_path'], home)
        self.assertEqual((captured_tree / 'dirty.txt').read_text(), 'captured dirty bytes')
        self.assertTrue((captured_tree / '.git').is_dir())
        self.assertEqual(before, rehearse.snapshot(home))
        captured = rehearse.snapshot(self.tmp / 'captured')
        (tree / 'dirty.txt').write_text('peer edit')
        self.git('commit', '--allow-empty', '-qm', 'peer commit', cwd=tree)
        self.assertEqual(captured, rehearse.snapshot(self.tmp / 'captured'))
        self.assertEqual(private.local['nodes']['app']['home'], str(self.tmp / 'captured/0'))
        self.assertEqual(private.local['nodes']['app']['workbench'], str(home) + '-work')

    def test_unknown_overlapping_and_linked_roots_refuse_without_copy(self):
        inst, home, tree = self.fixture()
        link = self.tmp / 'linked'; link.symlink_to(home, target_is_directory=True)
        for roots in ([str(self.tmp)], [str(home), str(home)], [str(link)], [str(home / 'missing')]):
            inst.local['rehearse_external']['roots'] = roots
            with self.subTest(roots=roots), self.assertRaises(rehearse.RehearsalError):
                rehearse_external.freeze_declared(inst, self.tmp / 'refused')
            self.assertFalse((self.tmp / 'refused').exists())

    def test_absent_contract_preserves_observation_mode(self):
        root = self.init('demo')
        inst = instance.Instance(root)
        self.assertIs(rehearse_external.freeze_declared(inst, self.tmp / 'unused'), inst)
        self.assertFalse((self.tmp / 'unused').exists())

    def test_declared_workbench_sanitizes_nested_worktree_configuration(self):
        inst, home, tree = self.fixture()
        bench = Path(inst.node('app')['local']['workbench'])
        self.git('config', 'include.path', str(self.tmp / 'external-config'), cwd=tree)
        inst.local['rehearse_external']['roots'] = [str(bench)]
        private = rehearse_external.freeze_declared(inst, self.tmp / 'captured')
        copied = Path(private.node('app')['local']['worktrees']['job/sample']['path'])
        config = (copied / '.git/config').read_text()
        self.assertNotIn('include', config)
        self.assertIn('hooksPath = /dev/null', config)
        self.assertFalse((copied / '.git/commondir').exists())
