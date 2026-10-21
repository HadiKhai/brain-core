"""Fabricated rehearsal comparisons and real sandbox mutation checks."""
import contextlib
import hashlib
import importlib
import io
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from brain import rehearse as rehearsal
from scenario import Scenario, CORE


def result(text='', code=0, seconds=0.01, unexpected=0):
    return dict(text=text, code=code, seconds=seconds, unexpected=unexpected, timeout=False)


class RehearsalComparison(unittest.TestCase):
    def test_clean_candidate_and_normalization(self):
        self.assertEqual([], rehearsal.compare({'check': result('✓ all good')}, {'check': result('✓ all good')}))
        self.assertEqual(rehearsal.normalize('{"b":2,"a":1}'), rehearsal.normalize('{"a":1,"b":2}'))
        self.assertEqual(rehearsal.normalize('/tmp/one 2026-01-02 03:04', ['/tmp/one']),
                         rehearsal.normalize('/tmp/two 2026-02-03 04:05', ['/tmp/two']))

    def test_all_four_incident_classes_and_hook(self):
        before = {'check': result('✓ good'), 'fix': result('✓ good'),
                  'ready:0:0': result('ready'), 'ready:1:0': result('ready'),
                  'due:0': result('[]'), 'due:1': result('[]'),
                  'brief:0': result('Stop when: ask for consent!'), 'hook': result()}
        after = {'check': result('✗ cannot heal', 1), 'fix': result('✗ cannot heal', 1),
                 'ready:0:0': result('not ready: main resolution', 1),
                 'ready:1:0': result('not ready: main resolution', 1),
                 'due:0': result('[{"kind":"load","text":"noise"}]'),
                 'due:1': result('[{"kind":"load","text":"noise"}]'),
                 'brief:0': result('quiet'), 'hook': result(seconds=1.1)}
        flags = rehearsal.compare(before, after)
        for flag in ('fix: new check errors', 'ready:0:0: ready became not ready',
                     'ready:1:0: ready became not ready', 'due:0: new due items',
                     'due:1: new due items', 'brief:0: brief warning disappeared', 'hook: latency exceeds 1 s'):
            self.assertIn(flag, flags)

    def test_additional_duplicate_errors_and_due_items_are_not_lost(self):
        for key, text in [('check', '✗ same'), ('due:0', '[{"kind":"a"}]')]:
            new = text + '\n' + text if key == 'check' else '[{"kind":"a"},{"kind":"a"}]'
            self.assertTrue(rehearsal.compare({key: result(text)}, {key: result(new)}))

    def test_unexpected_write_and_invalid_due_fail(self):
        self.assertIn('ui: unexpected writes', rehearsal.compare({'ui': result()}, {'ui': result(unexpected=1)}))
        self.assertIn('due:0: invalid due output', rehearsal.compare({'due:0': result('[]')}, {'due:0': result('oops')}))

    def test_sandbox_unavailable_refuses(self):
        with mock.patch.object(rehearsal.sys, 'platform', 'linux'):
            with self.assertRaises(rehearsal.RehearsalError):
                rehearsal.sandbox_prefix(Path('/tmp/fabricated'))


class RehearsalRedaction(Scenario):
    def probe(self, out, err='', code=0):
        lane = self.tmp / 'lane'
        (lane / 'brain').mkdir(parents=True, exist_ok=True)
        process = mock.Mock(returncode=code, pid=123)
        process.communicate.return_value = (out, err)
        with mock.patch.object(rehearsal, 'sandbox_prefix', return_value=[]), \
                mock.patch.object(rehearsal.subprocess, 'Popen', return_value=process), \
                mock.patch.object(rehearsal.os, 'killpg'):
            return rehearsal.run_command(lane / 'brain/core', lane, [], 'read', [],
                                         [('invented-canary', 'term'), ('Straße', 'term')])

    def test_stdout_stderr_redacted_before_comparison_and_digest(self):
        printed = io.StringIO()
        handler = logging.StreamHandler(printed)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        with contextlib.redirect_stdout(printed), contextlib.redirect_stderr(printed):
            old = self.probe('[{"text":"invented-canary","kind":"hold"}]')
            same = self.probe('[{"kind":"hold","text":"INVENTED-CANARY"}]')
            changed = self.probe('[{"text":"invented-canary","kind":"new"}]')
            error = self.probe('', '✗ invented-canary failed', code=1)
        self.assertEqual([], rehearsal.compare({'due:0': old}, {'due:0': same}))
        digest = lambda r: hashlib.sha256(r['text'].encode()).hexdigest()
        self.assertEqual(digest(old), digest(same))
        self.assertNotEqual(digest(old), digest(changed))
        self.assertIn('due:0: new due items', rehearsal.compare({'due:0': old}, {'due:0': changed}))
        self.assertIn('check: new check errors', rehearsal.compare({'check': result()}, {'check': error}))
        self.assertEqual('', printed.getvalue())
        for r in (old, same, changed, error):
            self.assertNotIn('invented-canary', json.dumps(r).casefold())
            self.assertIn('[…]', r['text'])

    def test_unicode_folds_and_escaped_json_preserve_real_differences(self):
        old = self.probe(json.dumps([{'text': 'STRASSE', 'kind': 'old'}]))
        same = self.probe(json.dumps([{'text': 'Straße', 'kind': 'old'}]))
        changed = self.probe(json.dumps([{'text': 'Straße', 'kind': 'new'}]))
        self.assertEqual(old['text'], same['text'])
        self.assertIn('due:0: new due items', rehearsal.compare({'due:0': old}, {'due:0': changed}))
        self.assertNotIn('strasse', changed['text'].casefold())
        split = self.probe('invented-', 'canary')
        self.assertEqual('[…]', split['text'])

    def test_json_quoted_terms_are_masked_without_losing_structure(self):
        terms = [('fabricated-"cedar', 'one'), ('invented-\\willow', 'two')]
        text = json.dumps({'items': [term for term, _ in terms]})
        masked = rehearsal.redact_output(rehearsal.normalize(text), terms)
        self.assertEqual({'items': ['[…]', '[…]']}, json.loads(masked))

    def test_overlapping_terms_are_order_independent(self):
        terms = [('fabricated-cedar', 'one'), ('cedar-project', 'two')]
        for order in (terms, terms[::-1]):
            self.assertEqual('keep […] changed', rehearsal.redact_output('keep fabricated-cedar-project changed', order))

    def test_evidence_stdout_and_refusal_hide_probe_content(self):
        from types import SimpleNamespace
        node = dict(id='engine', external=True, home_path=self.tmp / 'candidate')
        inst = SimpleNamespace(root=self.tmp / 'source', node=lambda _: node, nodes=lambda: [node])
        path = self.tmp / 'evidence.json'
        probe = self.probe('[{"text":"invented-canary","kind":"hold"}]')
        evidence = dict(passed=True, commands=1, regressions=[],
                        output_digests={'due:0': hashlib.sha256(probe['text'].encode()).hexdigest()})
        args = SimpleNamespace(id='engine', branch='job')
        printed = io.StringIO()
        handler = logging.StreamHandler(printed)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        with mock.patch.object(rehearsal._m_nodes, 'find_root', return_value=inst.root), \
                mock.patch.object(rehearsal._m_instance, 'Instance', return_value=inst), \
                mock.patch.object(rehearsal, 'evidence_path', return_value=path), \
                mock.patch.object(rehearsal._m_guard, 'never_terms', return_value=[('invented-canary', 'term')]), \
                mock.patch.object(rehearsal, 'rehearse', return_value=evidence) as run, \
                contextlib.redirect_stdout(printed), contextlib.redirect_stderr(printed):
            rehearsal.cmd_rehearse(args)
            saved = path.read_text()
            self.assertEqual(evidence, json.loads(saved))
            run.side_effect = rehearsal.RehearsalError('invented-canary raw probe output')
            with self.assertRaises(SystemExit) as caught:
                rehearsal.cmd_rehearse(args)
            self.assertNotIn('invented-canary', str(caught.exception))
            self.assertIn('rehearsal refused or incomplete', str(caught.exception))
        self.assertFalse(path.exists())
        for text in (saved, printed.getvalue()):
            self.assertNotIn('invented-canary', text)
            self.assertNotIn('raw probe output', text)
        self.assertIn('rehearse: passed', printed.getvalue())


class RehearsalStability(Scenario):
    def setUp(self):
        super().setUp()
        self.root = self.repo(self.tmp / 'live', {'note.md': 'original\n',
                                                '.gitignore': '.brain/\nignored/\n'})
        (self.root / 'core/bin').mkdir(parents=True)
        (self.root / 'core/bin/brain').write_text('fixture')
        self.git('add', 'core', cwd=self.root)
        self.git('commit', '-qm', 'core fixture', cwd=self.root)
        self.oid = self.git('rev-parse', 'HEAD', cwd=self.root).strip()
        self.home = self.repo(self.tmp / 'candidate', {'README.md': 'candidate'})
        (self.home / 'bin').mkdir()
        (self.home / 'bin/brain').write_text('fixture')
        self.git('add', 'bin', cwd=self.home)
        self.git('commit', '-qm', 'candidate fixture', cwd=self.home)
        self.head = self.git('rev-parse', 'HEAD', cwd=self.home).strip()

    def run_rehearsal(self, mutation=lambda: None, during_run=False, damage_copy=None):
        from types import SimpleNamespace
        inst = SimpleNamespace(root=self.root, nodes=lambda: [])
        node = dict(external=True, home_path=self.home, id='engine')
        copy = rehearsal.copy_tree
        def copying(source, dest):
            copy(source, dest)
            if source == self.root:
                if damage_copy is not None:
                    damage_copy(dest)
                if not during_run:
                    mutation()
        def lane(*args):
            if during_run:
                mutation()
            return {'hook': result()}
        with mock.patch.dict(os.environ, self.env), \
                mock.patch.object(rehearsal, 'sandbox_self_check'), \
                mock.patch.object(rehearsal, 'sandbox_prefix', return_value=[]), \
                mock.patch.object(rehearsal._m_verify, 'ready_refs', return_value=(self.head, self.head)), \
                mock.patch.object(rehearsal._m_verify, 'integration_parent', return_value=self.head), \
                mock.patch.object(rehearsal._m_instance, 'Instance', return_value=inst), \
                mock.patch.object(rehearsal, 'probe_delivery', return_value=[]), \
                mock.patch.object(rehearsal, 'copy_tree', side_effect=copying), \
                mock.patch.object(rehearsal, 'run_lane', side_effect=lane), \
                mock.patch.object(rehearsal._m_jobs, 'stamp', return_value='fixture'):
            return rehearsal.rehearse(inst, node, 'job')

    def write(self, rel, text='changed'):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def test_volatile_churn_during_copy_and_run_does_not_refuse(self):
        paths = ['.git/refs/t3/checkpoint', '.git/logs/HEAD', '.git/COMMIT_EDITMSG',
                 '.git/hook-once', '.git/index.lock', '.git/config.lock',
                 '.brain/spend.json', '.brain/spend/usage.json', '.brain/watch.json',
                 '.brain/a.lock', '.brain/lock', '.brain/machine.json', '.brain/hooks.log',
                 '.brain/hooks.json', '.brain/local.json', 'ignored/cache', 'untracked.tmp']
        for during_run in (False, True):
            for rel in paths + ['.git/index']:
                with self.subTest(path=rel, during_run=during_run):
                    def mutate():
                        if rel == '.git/index':
                            os.utime(self.root / rel, None)
                        else:
                            self.write(rel, self.oid + '\n' if rel.startswith('.git/refs/') else 'churn')
                    try:
                        self.assertTrue(self.run_rehearsal(mutate, during_run)['passed'])
                    finally:
                        if rel != '.git/index':
                            (self.root / rel).unlink(missing_ok=True)

    def test_tracked_edits_and_real_ref_moves_during_copy_refuse(self):
        parent = self.git('rev-parse', 'HEAD^', cwd=self.root).strip()
        for rel, text in [('note.md', 'edited'), ('core/bin/brain', 'edited core'),
                          ('.git/refs/heads/main', parent + '\n'),
                          ('.git/refs/tags/release', self.oid + '\n'),
                          ('.git/HEAD', self.oid + '\n')]:
            with self.subTest(path=rel):
                with self.assertRaisesRegex(rehearsal.RehearsalError, 'live brain moved while copying'):
                    self.run_rehearsal(lambda: self.write(rel, text))

    def test_tracked_edit_during_run_refuses_evidence(self):
        outcome = self.run_rehearsal(lambda: self.write('note.md'), during_run=True)
        self.assertFalse(outcome['passed'])
        self.assertIn('live brain changed during rehearsal; evidence refused', outcome['regressions'])

    def test_index_refresh_and_packing_refs_do_not_change_stability(self):
        with mock.patch.dict(os.environ, self.env):
            before = rehearsal.stability_snapshot(self.root)
            self.git('update-index', '--refresh', cwd=self.root)
            self.git('update-ref', 'refs/t3/checkpoint', self.oid, cwd=self.root)
            self.git('pack-refs', '--all', cwd=self.root)
            self.assertEqual(before, rehearsal.stability_snapshot(self.root))
            self.git('commit', '--allow-empty', '-qm', 'move head', cwd=self.root)
            self.assertNotEqual(before, rehearsal.stability_snapshot(self.root))

    def test_tracked_links_and_replaced_parents_never_read_targets(self):
        outside = self.tmp / 'outside'
        outside.mkdir()
        (outside / 'secret').write_text('original')
        self.write('nested/secret', 'tracked')
        (self.root / 'alias').symlink_to(outside / 'secret')
        self.git('add', 'alias', 'nested', cwd=self.root)
        (self.root / 'nested/secret').unlink()
        (self.root / 'nested').rmdir()
        (self.root / 'nested').symlink_to(outside, target_is_directory=True)
        with mock.patch.dict(os.environ, self.env):
            before = rehearsal.stability_snapshot(self.root)
            (outside / 'secret').write_text('target changed')
            self.assertEqual(before, rehearsal.stability_snapshot(self.root))
            self.assertTrue(before['alias'].startswith('link:'))
            self.assertTrue(before['nested'].startswith('link:'))
            self.assertNotIn('nested/secret', before)

    def test_gitlink_tracks_nested_dirty_content_and_refs_not_private_churn(self):
        nested = self.repo(self.tmp / 'nested-core')
        import shutil
        shutil.rmtree(self.root / 'core')
        self.git('rm', '-qr', '--cached', 'core', cwd=self.root)
        self.git('submodule', 'add', '-q', str(nested), 'core', cwd=self.root)
        self.git('commit', '-qam', 'gitlink core', cwd=self.root)
        with mock.patch.dict(os.environ, self.env):
            before = rehearsal.stability_snapshot(self.root)
            gitdir = Path(self.git('rev-parse', '--absolute-git-dir', cwd=self.root / 'core').strip())
            (gitdir / 'COMMIT_EDITMSG').write_text('churn')
            self.assertEqual(before, rehearsal.stability_snapshot(self.root))
            (self.root / 'core/README.md').write_text('dirty core')
            self.assertNotEqual(before, rehearsal.stability_snapshot(self.root))

    def test_incomplete_git_copies_refuse_and_repacking_live_source_is_allowed(self):
        blob = self.git('rev-parse', 'HEAD:note.md', cwd=self.root).strip()
        def damage(dest):
            (dest / '.git/objects' / blob[:2] / blob[2:]).unlink()
        with self.assertRaises(rehearsal.RehearsalError):
            self.run_rehearsal(damage_copy=damage)
        self.assertTrue(self.run_rehearsal(
            lambda: self.git('repack', '-adq', cwd=self.root))['passed'])

    def test_connectivity_requires_branch_and_tag_objects_but_not_private_refs(self):
        self.git('checkout', '-qb', 'other', cwd=self.root)
        self.write('branch-only', 'branch content')
        self.git('add', 'branch-only', cwd=self.root)
        self.git('commit', '-qm', 'other branch', cwd=self.root)
        blob = self.git('rev-parse', 'HEAD:branch-only', cwd=self.root).strip()
        self.git('tag', 'release', cwd=self.root)
        self.git('checkout', '-q', 'main', cwd=self.root)
        self.write('.git/refs/t3/checkpoint', 'f' * 40 + '\n')
        dest = self.tmp / 'copy'
        rehearsal.copy_tree(self.root, dest)
        with mock.patch.dict(os.environ, self.env):
            rehearsal.materialize_git(self.root, dest)
            (dest / '.git/objects' / blob[:2] / blob[2:]).unlink()
            with self.assertRaises(rehearsal.RehearsalError):
                rehearsal.validate_git_objects(dest)
            (dest / '.git/refs/heads/other').unlink()
            with self.assertRaises(rehearsal.RehearsalError):
                rehearsal.validate_git_objects(dest)
            (dest / '.git/refs/tags/release').unlink()
            rehearsal.validate_git_objects(dest)

    def test_copied_git_locks_do_not_block_materialization(self):
        for rel in ['.git/config.lock', '.git/index.lock', '.git/HEAD.lock',
                    '.git/refs/heads/main.lock']:
            self.write(rel, 'in-progress writer')
        self.assertTrue(self.run_rehearsal()['passed'])
        self.assertTrue((self.root / '.git/config.lock').exists())

    def test_gitlink_metadata_links_and_incomplete_copies_refuse(self):
        import shutil
        nested = self.repo(self.tmp / 'nested-core')
        shutil.rmtree(self.root / 'core')
        self.git('rm', '-qr', '--cached', 'core', cwd=self.root)
        self.git('submodule', 'add', '-q', str(nested), 'core', cwd=self.root)
        self.git('commit', '-qam', 'gitlink core', cwd=self.root)
        pointer = self.root / 'core/.git'
        original = pointer.read_text()
        pointer.unlink()
        pointer.symlink_to(self.root / '.git/modules/core', target_is_directory=True)
        with mock.patch.dict(os.environ, self.env):
            with self.assertRaises(rehearsal.RehearsalError):
                rehearsal.stability_snapshot(self.root)
        pointer.unlink()
        pointer.write_text(original)
        self.assertTrue(self.run_rehearsal()['passed'])
        blob = self.git('rev-parse', 'HEAD:README.md', cwd=self.root / 'core').strip()
        copy = rehearsal.copy_tree
        def copying(source, dest):
            copy(source, dest)
            if dest.name == '.git' and dest.parent.name == 'core':
                (dest / 'objects' / blob[:2] / blob[2:]).unlink()
        with mock.patch.object(rehearsal, 'copy_tree', side_effect=copying):
            with self.assertRaisesRegex(rehearsal.RehearsalError, 'live brain moved while copying'):
                self.run_rehearsal()

    def test_metadata_links_and_unavailable_metadata_refuse(self):
        import shutil
        for rel in ['root', '.git', 'gitdir-parent', '.git/config', '.git/config.worktree',
                    '.git/index', '.git/HEAD', '.git/packed-refs', '.git/refs',
                    '.git/refs/heads', '.git/refs/tags', '.git/refs/heads/alias',
                    '.git/refs/tags/alias', 'unavailable']:
            with self.subTest(path=rel):
                root = self.tmp / 'linked-case'
                shutil.copytree(self.root, root)
                outside = self.tmp / 'metadata-target'
                try:
                    if rel == 'root':
                        outside.symlink_to(root, target_is_directory=True)
                        root_to_check = outside
                    elif rel == 'unavailable':
                        shutil.rmtree(root / '.git')
                        root_to_check = root
                    elif rel == 'gitdir-parent':
                        outside.mkdir()
                        shutil.move(str(root / '.git'), str(outside / 'admin'))
                        alias = root / 'metadata-alias'
                        alias.symlink_to(outside, target_is_directory=True)
                        (root / '.git').write_text('gitdir: ' + str(alias / 'admin') + '\n')
                        root_to_check = root
                    else:
                        path = root / rel
                        path.parent.mkdir(parents=True, exist_ok=True)
                        if path.exists():
                            shutil.move(str(path), str(outside))
                        else:
                            outside.write_text(self.oid + '\n')
                        path.symlink_to(outside, target_is_directory=outside.is_dir())
                        root_to_check = root
                    with mock.patch.dict(os.environ, self.env):
                        with self.assertRaises(rehearsal.RehearsalError):
                            rehearsal.stability_snapshot(root_to_check)
                finally:
                    shutil.rmtree(root)
                    if outside.is_symlink() or outside.is_file():
                        outside.unlink()
                    elif outside.exists():
                        shutil.rmtree(outside)

    def test_untracked_manual_core_is_bound_and_root_must_own_its_git(self):
        import shutil
        self.git('rm', '-qr', 'core', cwd=self.root)
        self.repo(self.root / 'core')
        with mock.patch.dict(os.environ, self.env):
            before = rehearsal.stability_snapshot(self.root)
            (self.root / 'core/README.md').write_text('dirty manual core')
            self.assertNotEqual(before, rehearsal.stability_snapshot(self.root))
            child = self.root / 'non-repository'
            child.mkdir()
            with self.assertRaises(rehearsal.RehearsalError):
                rehearsal.stability_snapshot(child)

    def test_tracked_unusual_paths_remain_bound(self):
        for name in ['line\nbreak', 'unicode-\u2603']:
            self.write(name, 'tracked')
            self.git('add', '--', name, cwd=self.root)
            with mock.patch.dict(os.environ, self.env):
                before = rehearsal.stability_snapshot(self.root)
                self.write(name, 'edited')
                self.assertNotEqual(before, rehearsal.stability_snapshot(self.root))

    @unittest.skipIf(sys.platform == 'darwin', 'filesystem requires valid UTF-8 filenames')
    def test_tracked_non_utf8_path_remains_bound(self):
        name = os.fsdecode(b'byte-\xff')
        self.write(name, 'tracked')
        self.git('add', '--', name, cwd=self.root)
        with mock.patch.dict(os.environ, self.env):
            before = rehearsal.stability_snapshot(self.root)
            self.write(name, 'edited')
            self.assertNotEqual(before, rehearsal.stability_snapshot(self.root))

    def test_exhaustive_snapshot_still_detects_runtime_mutations(self):
        before = rehearsal.snapshot(self.root)
        self.write('.brain/watch.json')
        self.assertNotEqual(before, rehearsal.snapshot(self.root))


@unittest.skipUnless(sys.platform == 'darwin', 'requires macOS OS sandbox')
class RehearsalSandbox(Scenario):
    def runner(self, script, args=(), terms=(), kind='read'):
        lane = self.tmp / 'lane'
        lane.mkdir(exist_ok=True)
        (lane / 'brain').mkdir(exist_ok=True)
        core = lane / 'brain/core'
        (core / 'bin').mkdir(parents=True, exist_ok=True)
        (core / 'bin/brain').write_text(script)
        return rehearsal.run_command(core, lane, list(args), kind, [], terms)

    def test_registered_hook_latency_is_measured(self):
        r = self.runner('import json, os, time\n'
                        'if json.load(open(os.environ["BRAIN_CONFIG"]))["brains"]:\n'
                        '    time.sleep(1.5)\n', kind='hook')
        self.assertEqual(0, r['code'])
        self.assertIn('hook: latency exceeds 1 s', rehearsal.compare({'hook': result()}, {'hook': r}))

    def test_read_command_cannot_rewrite_machine_bindings(self):
        r = self.runner('from pathlib import Path\nPath(".brain").mkdir(exist_ok=True)\n'
                        'Path(".brain/local.json").write_text("{}")\n')
        self.assertEqual(0, r['code'])
        self.assertEqual(1, r['unexpected'])
        self.assertTrue(rehearsal.allowed_write('brain/.brain/local.json', 'fix'))
        self.assertFalse(rehearsal.allowed_write('brain/.brain/unknown-cache.json', 'read'))

    def test_outside_process_survives_candidate_signal(self):
        victim = subprocess.Popen(['/bin/sleep', '10'])
        try:
            r = self.runner('import os, signal\nos.kill(' + str(victim.pid) + ', signal.SIGKILL)\n')
            self.assertNotEqual(0, r['code'])
            self.assertIsNone(victim.poll())
        finally:
            if victim.poll() is None:
                victim.terminate()
            victim.wait()

    def test_detached_drain_is_reaped_before_snapshot(self):
        script = ('import sys, time\nfrom pathlib import Path\n'
                  'if "--drain" in sys.argv:\n'
                  '    time.sleep(0.5)\n'
                  '    Path("late-write").write_text("oops")\n'
                  'else:\n'
                  '    sys.path.insert(0, ' + repr(str(CORE / 'lib')) + ')\n'
                  '    from brain import hosts, common\n'
                  '    from types import SimpleNamespace\n'
                  '    common.CORE = Path(__file__).parents[1]\n'
                  '    hosts.wake_start(SimpleNamespace(root=Path.cwd()), {"id": "root"})\n')
        r = self.runner(script)
        self.assertEqual(0, r['code'], r['text'])
        import time
        time.sleep(0.7)
        self.assertFalse((self.tmp / 'lane/brain/late-write').exists())
        self.assertEqual(0, r['unexpected'])

    def test_invalid_utf8_is_replaced(self):
        r = self.runner('import os\nos.write(1, b"\\xff")\n')
        self.assertEqual(0, r['code'])
        self.assertEqual('\ufffd', r['text'])

    def test_sandbox_self_check_and_shadow_executable_refusal(self):
        lane = self.tmp / 'boundary'
        lane.mkdir()
        rehearsal.sandbox_self_check(lane)
        tools = self.tmp / 'tools'
        tools.mkdir()
        fake = tools / 'sandbox-exec'
        fake.write_text('#!/bin/sh\nexit 0\n')
        fake.chmod(0o755)
        with mock.patch.dict(os.environ, {'PATH': str(tools) + os.pathsep + os.environ['PATH']}):
            with self.assertRaises(rehearsal.RehearsalError):
                rehearsal.sandbox_self_check(lane)
        with mock.patch.object(rehearsal, 'sandbox_prefix', return_value=[]):
            with self.assertRaises(rehearsal.RehearsalError):
                rehearsal.sandbox_self_check(lane)

    def test_alias_escapes_are_denied_and_source_hardlinks_are_detached(self):
        outside = self.tmp / 'outside'
        outside.write_text('unchanged')
        lane = self.tmp / 'lane'
        (lane / 'brain').mkdir(parents=True)
        (lane / 'brain/alias').symlink_to(outside)
        r = self.runner('from pathlib import Path\nPath("alias").write_text("changed")\n')
        self.assertNotEqual(0, r['code'])
        self.assertEqual('unchanged', outside.read_text())
        r = self.runner('import os\nos.link(' + repr(str(outside)) + ', "new-alias")\n')
        self.assertNotEqual(0, r['code'])
        self.assertEqual('unchanged', outside.read_text())
        source = self.tmp / 'source'
        source.mkdir()
        os.link(outside, source / 'hardlink')
        rehearsal.copy_tree(source, self.tmp / 'copy')
        (self.tmp / 'copy/hardlink').write_text('copy only')
        self.assertEqual('unchanged', outside.read_text())

    def test_os_boundary_preserves_live_brain_home_and_git(self):
        live = self.repo(self.tmp / 'live', {'note.md': 'original\n'})
        outside_home = self.tmp / 'operator'
        outside_home.mkdir()
        note = outside_home / 'note'
        note.write_text('original home')
        original = rehearsal.snapshot(live)
        script = 'from pathlib import Path\nPath(' + repr(str(live / 'note.md')) + ').write_text("mutated")\n'
        r = self.runner(script)
        self.assertNotEqual(0, r['code'])
        self.assertEqual(original, rehearsal.snapshot(live))
        r = self.runner('from pathlib import Path\nPath(' + repr(str(note)) + ').write_text("mutated")\n')
        self.assertNotEqual(0, r['code'])
        self.assertEqual('original home', note.read_text())
        r = self.runner('import socket\nsocket.create_connection(("127.0.0.1", 9))\n')
        self.assertNotEqual(0, r['code'])

    def test_caught_external_write_still_fails_and_empty_directory_is_seen(self):
        outside = self.tmp / 'outside'
        outside.write_text('unchanged')
        script = ('from pathlib import Path\ntry:\n    Path(' + repr(str(outside)) +
                  ').write_text("changed")\nexcept OSError:\n    pass\n')
        r = self.runner(script)
        self.assertEqual(0, r['code'])
        self.assertEqual(1, r['unexpected'])
        self.assertEqual('unchanged', outside.read_text())
        r = self.runner('from pathlib import Path\nPath("unapproved").mkdir()\n')
        self.assertEqual(1, r['unexpected'])

    def test_unexpected_copy_mutation_is_detected(self):
        r = self.runner('from pathlib import Path\nPath("node.md").write_text("changed")\n')
        self.assertEqual(0, r['code'])
        self.assertEqual(1, r['unexpected'])

    def test_never_name_output_is_redacted_without_publishing_it(self):
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed), contextlib.redirect_stderr(printed):
            r = self.runner('print("invented-canary")', terms=[('invented-canary', 'term')])
        self.assertEqual(0, r['code'])
        self.assertEqual('[…]', r['text'])
        self.assertEqual('', printed.getvalue())


class RehearsalEvidence(Scenario):
    def test_train_rehearsal_uses_recorded_base_after_origin_main_advances(self):
        from brain.instance import Instance
        from brain.verify import ready_state
        r = self.init('sample')
        home = self.repo(self.tmp / 'app')
        main = self.git('rev-parse', 'main', cwd=home).strip()
        self.git('remote', 'add', 'origin', str(home), cwd=home)
        self.git('update-ref', 'refs/remotes/origin/main', main, cwd=home)
        self.git('symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/main', cwd=home)
        self.b(r, 'new', 'a', '--title', 'A', '--home', str(home))
        self.b(r, 'set', 'a', 'verify=true', 'rehearse=required')
        self.b(r, 'wt', 'add', 'a', 'job')
        wt = self.tmp / 'app-work/worktrees/job'
        self.git('commit', '--allow-empty', '-qm', 'job change', cwd=wt)
        inst = Instance(r)
        n = inst.node('a')
        head = self.git('rev-parse', 'job', cwd=home).strip()
        evidence = dict(version=1, node='a', branch='job', head=head, main=main,
                        integration_base=main, passed=True,
                        installed_core_commit=rehearsal.installed_core_commit(r))
        path = rehearsal.evidence_path(n, 'job')
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(evidence))
        self.b(r, 'ready', 'a', '--branch', 'job', '--record', '--reviews', '1')
        self.git('commit', '--allow-empty', '-qm', 'advance origin main', cwd=home)
        newer = self.git('rev-parse', 'HEAD', cwd=home).strip()
        self.git('update-ref', 'refs/remotes/origin/main', newer, cwd=home)
        with mock.patch.dict(os.environ, self.env):
            train = ready_state(inst, n, 'job', train=True)
            self.assertTrue(train['ready'], train['reasons'])
            self.assertFalse(ready_state(inst, n, 'job')['ready'])
            path.write_text(json.dumps({**evidence, 'integration_base': newer}))
            mismatched = ready_state(inst, n, 'job', train=True)
            self.assertFalse(mismatched['ready'])
            self.assertIn('passing rehearsal missing', '; '.join(mismatched['reasons']))

    def test_installed_core_commit_change_invalidates_evidence(self):
        from brain.instance import Instance
        r = self.init('sample')
        home = self.repo(self.tmp / 'app')
        self.b(r, 'new', 'a', '--title', 'A', '--home', str(home))
        self.git('branch', 'job', cwd=home)
        self.git('init', '-q', '-b', 'main', cwd=r / 'core')
        self.git('commit', '--allow-empty', '-qm', 'installed baseline', cwd=r / 'core')
        n = Instance(r).node('a')
        head = self.git('rev-parse', 'job', cwd=home).strip()
        evidence = dict(version=1, node='a', branch='job', head=head, main=head,
                        integration_base=head, passed=True,
                        installed_core_commit=rehearsal.installed_core_commit(r))
        path = rehearsal.evidence_path(n, 'job')
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(evidence))
        self.assertEqual(evidence, rehearsal.read_evidence(n, 'job', head))
        self.git('commit', '--allow-empty', '-qm', 'upgraded baseline', cwd=r / 'core')
        self.assertIsNone(rehearsal.read_evidence(n, 'job', head))

    def test_required_ready_gate_and_head_main_invalidation(self):
        r = self.init('sample')
        home = self.repo(self.tmp / 'app')
        self.b(r, 'new', 'a', '--title', 'A', '--home', str(home))
        self.b(r, 'set', 'a', 'verify=true', 'rehearse=required')
        self.b(r, 'wt', 'add', 'a', 'job')
        wt = self.tmp / 'app-work/worktrees/job'
        self.git('commit', '--allow-empty', '-qm', 'change', cwd=wt)
        self.assertIn('passing rehearsal missing', self.b(r, 'ready', 'a', '--branch', 'job', '--record', '--reviews', '1', ok=False))
        head = self.git('rev-parse', 'job', cwd=home).strip()
        main = self.git('rev-parse', 'main', cwd=home).strip()
        import hashlib
        path = self.tmp / 'app-work/scratch/rehearse' / (hashlib.sha256(b'job').hexdigest() + '.json')
        path.parent.mkdir(parents=True)
        evidence = dict(version=1, node='a', branch='job', head=head, main=main,
                        integration_base=main, passed=True, commands=7,
                        installed_core_commit=rehearsal.installed_core_commit(r))
        path.write_text(json.dumps(evidence))
        self.assertIn('ready', self.b(r, 'ready', 'a', '--branch', 'job', '--record', '--reviews', '1'))
        packet = json.loads(next((r / 'nodes/a/job-handbacks').glob('*.json')).read_text())['ready']
        self.assertEqual(evidence, packet['rehearsal'])
        reused = self.b(r, 'ready', 'a', '--branch', 'job', '--record', '--reviews', '1')
        self.assertIn('verify: reused', reused)
        packet = json.loads(next((r / 'nodes/a/job-handbacks').glob('*.json')).read_text())['ready']
        self.assertEqual(evidence, packet['rehearsal'])
        self.assertIn('input_files_ref', packet)
        self.assertEqual('ready', self.b(r, 'ready', 'a', '--branch', 'job').strip())
        baseline = r / 'core/CHANGES.md'
        old = baseline.read_text()
        baseline.write_text(old + '\nBaseline changed\n')
        self.assertIn('passing rehearsal missing', self.b(r, 'ready', 'a', '--branch', 'job', ok=False))
        baseline.write_text(old)
        self.git('commit', '--allow-empty', '-qm', 'new head', cwd=wt)
        self.assertIn('passing rehearsal missing', self.b(r, 'ready', 'a', '--branch', 'job', ok=False))
        self.git('commit', '--allow-empty', '-qm', 'new main', cwd=home)
        self.assertIn('passing rehearsal missing', self.b(r, 'ready', 'a', '--branch', 'job', ok=False))

@unittest.skipUnless(sys.platform == 'darwin', 'requires macOS OS sandbox')
class RehearsalPipeline(Scenario):
    def test_committed_candidate_clean_pass_and_all_incident_mutations(self):
        """Actual subprocess orchestration with fabricated installed/candidate CLIs."""
        from brain.instance import Instance
        r = self.init('sample')
        home = self.tmp / 'candidate'
        (home / 'bin').mkdir(parents=True)
        fixture = '''import sys
command = sys.argv[1]
mode = MODE
if command == 'due':
    print('[{"kind":"hold","text":"invented-canary"},{"kind":"load","text":"new noise"}]'
          if mode else '[{"kind":"hold","text":"invented-canary"}]')
elif command in ('check',):
    print('✗ upgrade cannot heal' if mode else '✓ checks pass')
    sys.exit(1 if mode else 0)
elif command == 'ready':
    print('not ready: changed main resolution' if mode else 'ready')
    sys.exit(1 if mode else 0)
elif command == 'brief':
    print('quiet' if mode else 'Stop when: ask for consent!')
elif command == 'ui':
    print('{}')
elif command == 'status':
    print('updated')
elif command == 'hook':
    import json, os, time
    if mode and json.load(open(os.environ['BRAIN_CONFIG']))['brains']:
        time.sleep(1.5)
'''
        baseline = fixture.replace('MODE', 'False')
        (home / 'bin/brain').write_text(baseline)
        self.git('init', '-q', '-b', 'main', cwd=home)
        self.git('add', '.', cwd=home)
        self.git('commit', '-qm', 'fixture', cwd=home)
        self.b(r, 'new', 'a', '--title', 'A', '--home', str(home))
        self.b(r, 'wt', 'add', 'a', 'job')
        # Register a second open record to prove every ready record is exercised.
        self.b(r, 'wt', 'add', 'a', 'other')
        self.b(r, 'new', 'b', '--title', 'B')
        # Root and sibling probes can legitimately name another node's term.
        (r / 'nodes/b/never-name.txt').write_text('invented-canary\n')
        (r / 'core/bin/brain').write_text(baseline)
        original = rehearsal.snapshot(r)
        inst = Instance(r)
        n = inst.node('a')
        clean = rehearsal.rehearse(inst, n, 'job')
        self.assertTrue(clean['passed'], clean['regressions'])
        self.assertEqual(0, clean['changed_outputs'])
        self.assertNotIn('invented-canary', json.dumps(clean))
        self.assertEqual(original, rehearsal.snapshot(r))
        wt = self.tmp / 'candidate-work/worktrees/job'
        (wt / 'bin/brain').write_text(fixture.replace('MODE', 'True'))
        self.git('commit', '-qam', 'incident mutations', cwd=wt)
        bad = rehearsal.rehearse(inst, n, 'job')
        self.assertFalse(bad['passed'])
        for flag in ('fix: new check errors', 'ready:1:0: ready became not ready',
                     'ready:1:1: ready became not ready', 'due:0: new due items',
                     'due:1: new due items', 'brief:0: brief warning disappeared', 'hook: latency exceeds 1 s'):
            self.assertIn(flag, bad['regressions'])
        self.assertEqual(original, rehearsal.snapshot(r))
        # No private fixture command text is retained in public evidence.
        self.assertNotIn('upgrade cannot heal', json.dumps(bad))
        self.assertNotIn('invented-canary', json.dumps(bad))
        self.assertNotEqual(clean['output_digests']['due:0'], bad['output_digests']['due:0'])

class RehearsalDelivery(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)

    def tearDown(self):
        self.folder.cleanup()

    def test_real_post_user_agent_status_codes_and_no_redirect_or_url_output(self):
        import http.server
        import threading
        from brain.instance import Instance
        seen = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append((self.path, self.headers.get('User-Agent'),
                             self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(int(self.path.split('/')[1]))
                self.send_header('Location', '/204/redirected-secret')
                self.end_headers()
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = 'http://127.0.0.1:' + str(server.server_port)
        nodes = [dict(id='root' if i == 0 else 'n' + str(i),
                      local=dict(coordinator='thread', wake=base + '/' + str(status) + '/capability'))
                 for i, status in enumerate((204, 403, 302))]
        (self.root / 'instance.md').write_text('---\nname: sample\n---\n')
        inst = Instance(self.root)
        for node in nodes:
            directory = self.root if node['id'] == 'root' else self.root / 'nodes' / node['id']
            directory.mkdir(parents=True, exist_ok=True)
            (directory / 'node.md').write_text('---\nid: ' + node['id'] + '\nhome: here\n---\n')
            inst.local['nodes'][node['id']] = node['local']
        try:
            statuses = rehearsal.probe_delivery(inst, inst.nodes())
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        self.assertEqual([204, 403, 302], [s['status'] for s in statuses])
        self.assertEqual(['wake:1: HTTP 403', 'wake:2: HTTP 302'], rehearsal.delivery_regressions(statuses))
        self.assertEqual(3, len(seen))
        self.assertTrue(all(agent and 'Python-urllib' not in agent for _, agent, _ in seen))
        self.assertTrue(all(body == b'{"event":"brain-wake"}' for _, _, body in seen))
        self.assertNotIn('capability', json.dumps(statuses))
        self.assertNotIn(base, json.dumps(statuses))

    def test_outward_links_refuse_delivery_and_shared_addresses_are_deduplicated(self):
        from types import SimpleNamespace
        node = dict(id='root', local=dict(coordinator='t', wake='https://example.invalid/private'))
        other = dict(id='other', local=dict(coordinator='t2', wake=node['local']['wake']))
        inst = SimpleNamespace(root=self.root, node=lambda _: node, local_of=lambda _: {})
        with mock.patch.object(rehearsal._m_common, 'outward_links', return_value=['unsafe']), \
                mock.patch('urllib.request.OpenerDirector.open') as post:
            with self.assertRaises(rehearsal.RehearsalError):
                rehearsal.probe_delivery(inst, [node, other])
            post.assert_not_called()
        with mock.patch.object(rehearsal._m_common, 'outward_links', return_value=[]), \
                mock.patch.object(rehearsal, 'isolated_probe_instance', return_value=inst), \
                mock.patch.object(rehearsal._m_hosts, 'wake_probe', return_value=204, create=True) as post:
            self.assertEqual([dict(coordinator=0, status=204)], rehearsal.probe_delivery(inst, [node, other]))
            post.assert_called_once()

    def test_real_delivery_seeds_only_the_command_copy_cache(self):
        import http.server
        import threading
        from brain.instance import Instance
        seen = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                seen.append(True)
                self.send_response(204)
                self.end_headers()
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        (self.root / 'instance.md').write_text('---\nname: sample\n---\n')
        (self.root / 'node.md').write_text('---\nid: root\nhome: here\n---\n')
        inst = Instance(self.root)
        inst.local['nodes']['root'] = dict(coordinator='t', wake='http://127.0.0.1:' + str(server.server_port))
        original = rehearsal.snapshot(self.root)
        try:
            with tempfile.TemporaryDirectory() as td:
                private = rehearsal.isolated_probe_instance(inst, Path(td))
                statuses = rehearsal.probe_delivery(inst, [inst.node('root')], private)
                self.assertEqual([dict(coordinator=0, status=204)], statuses)
                self.assertEqual(204, rehearsal._m_hosts.wake_probe(private, private.node('root')))
                self.assertEqual(1, len(seen))
                with self.assertRaises(rehearsal.RehearsalError):
                    rehearsal.probe_delivery(inst, [inst.node('root')], inst)
            self.assertEqual(original, rehearsal.snapshot(self.root))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_shared_probe_is_preferred_and_cache_is_isolated(self):
        from brain.instance import Instance
        (self.root / 'instance.md').write_text('---\nname: sample\n---\n')
        (self.root / 'node.md').write_text('---\nid: root\nhome: here\n---\n')
        inst = Instance(self.root)
        inst.local['nodes']['root'] = dict(coordinator='t', wake='https://example.invalid/private')
        node = inst.node('root')
        original = rehearsal.snapshot(self.root)
        def probe(private, target, force=False):
            self.assertTrue(force)
            self.assertNotEqual(inst.root, private.root)
            (private.root / '.brain/probe-cache').write_text('cached')
            return 403
        with mock.patch.object(rehearsal._m_hosts, 'wake_probe', side_effect=probe, create=True) as shared:
            self.assertEqual([dict(coordinator=0, status=403)], rehearsal.probe_delivery(inst, [node]))
            shared.assert_called_once()
        self.assertEqual(original, rehearsal.snapshot(self.root))

@unittest.skipUnless(sys.platform == 'darwin', 'requires macOS OS sandbox')
class RehearsalNativeCore(Scenario):
    def test_real_core_cli_clean_candidate_and_byte_identical_source(self):
        import http.server
        import shutil
        import threading
        r = self.init('sample')
        home = self.tmp / 'core-home'
        shutil.copytree(CORE, home, symlinks=True, ignore=shutil.ignore_patterns('.git', '__pycache__'))
        self.git('init', '-q', '-b', 'main', cwd=home)
        self.git('add', '.', cwd=home)
        self.git('commit', '-qm', 'candidate core fixture', cwd=home)
        self.b(r, 'new', 'engine', '--title', 'Engine', '--home', str(home))
        self.b(r, 'wt', 'add', 'engine', 'job')
        self.b(r, 'check', '--fix')
        # Model an upgrade from a core whose check did not probe delivery. The
        # candidate check must see the real status cached into its throwaway copy.
        entry = r / 'core/bin/brain'
        shutil.copy2(entry, entry.with_name('installed-entry'))
        entry.write_text('import sys, runpy\nfrom pathlib import Path\n'
                         'if sys.argv[1:2] == ["check"]:\n    print("✓ checks pass")\n'
                         'else:\n    runpy.run_path(str(Path(__file__).with_name("installed-entry")), run_name="__main__")\n')
        posts = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                posts.append(True)
                self.send_response(204)
                self.end_headers()
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        local_file = r / '.brain/local.json'
        local = json.loads(local_file.read_text())
        local['nodes'].setdefault('root', {}).update(coordinator='thread', wake='http://127.0.0.1:' + str(server.server_port))
        local_file.write_text(json.dumps(local))
        original_brain = rehearsal.snapshot(r)
        original_home = rehearsal.snapshot(home)
        try:
            output = self.b(r, 'rehearse', 'engine', '--branch', 'job')
            self.assertIn('rehearse: passed', output)
            self.assertEqual(1, len(posts))
            self.assertEqual(original_brain, rehearsal.snapshot(r))
            self.assertEqual(original_home, rehearsal.snapshot(home))
            files = list((self.tmp / 'core-home-work/scratch/rehearse').glob('*.json'))
            self.assertEqual(1, len(files))
            evidence = json.loads(files[0].read_text())
            self.assertTrue(evidence['passed'])
            self.assertEqual(11, evidence['commands'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

class RehearsalGitFormats(Scenario):
    def test_reftable_and_sha256_formats_survive_without_hooks_or_helpers(self):
        for name, option, key, value in [('reftable', '--ref-format=reftable', 'extensions.refstorage', 'reftable'),
                                         ('sha256', '--object-format=sha256', 'extensions.objectformat', 'sha256')]:
            with self.subTest(format=name):
                source = self.tmp / name
                source.mkdir()
                init = subprocess.run(['git', 'init', '-q', '-b', 'main', option], cwd=source,
                                      env=self.env, capture_output=True, text=True)
                if init.returncode and ('unknown option' in init.stderr or 'unknown ref storage format' in init.stderr):
                    self.skipTest('installed Git does not support this repository format')
                self.assertEqual(0, init.returncode, init.stderr)
                (source / 'note').write_text('fixture')
                self.git('add', '.', cwd=source)
                self.git('commit', '-qm', 'fixture', cwd=source)
                canary = self.tmp / (name + '-hook-ran')
                hook = source / '.git/hooks/post-checkout'
                hook.parent.mkdir(exist_ok=True)
                hook.write_text('#!/bin/sh\ntouch ' + str(canary) + '\n')
                hook.chmod(0o755)
                self.git('config', 'core.hooksPath', str(hook.parent), cwd=source)
                self.git('config', 'credential.helper', 'fixture-helper', cwd=source)
                dest = self.tmp / (name + '-copy')
                rehearsal.copy_tree(source, dest)
                rehearsal.materialize_git(source, dest)
                self.assertEqual(value, self.git('config', '--get', key, cwd=dest).strip())
                self.assertEqual(self.git('rev-parse', 'HEAD', cwd=source), self.git('rev-parse', 'HEAD', cwd=dest))
                self.git('checkout', '--detach', 'HEAD', cwd=dest)
                self.assertFalse(canary.exists())
                config = self.git('config', '--local', '--list', cwd=dest)
                self.assertNotIn('fixture-helper', config)
                self.assertIn('core.hookspath=/dev/null', config)
