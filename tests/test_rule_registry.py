"""Rule metadata is syntax; tests and nodes below are entirely fabricated."""
import importlib
import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import core_loader
from scenario import Scenario, CORE


def registry_module():
    loader = core_loader.SourceFileLoader('registry_test_core', str(CORE / 'bin/brain'))
    spec = importlib.util.spec_from_loader('registry_test_core', loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return importlib.import_module(loader.package_name + '.rule_registry')


def rule(ident='R01', gate='none (manual observation)', applies='all'):
    return '<!-- rule:' + ident + ' -->\n- Preserve source evidence.\n' + (
        'Gate: ' + gate + '\n' if gate is not None else '') + 'Applies: ' + applies + '\n'


class RuleRegistryParsing(unittest.TestCase):
    def setUp(self):
        self.m = registry_module()

    def test_metadata_and_verbatim_sections(self):
        text = '# Rules\n\n' + rule() + '\n' + rule('R02', 'app:tests/check.py::Checks::test_receipt; app:tests/check.py::test_one')
        rows = self.m.parse_instance_rules(text)
        self.assertEqual([r['id'] for r in rows], ['R01', 'R02'])
        self.assertEqual(''.join(r['text'] for r in rows), text[text.index('<!--'):])
        self.assertEqual(rows[0]['line'], 3)
        self.assertEqual(self.m.gate_references(rows[1]['gate']), [
            ('app', 'tests/check.py', 'Checks::test_receipt'), ('app', 'tests/check.py', 'test_one')])

    def test_missing_empty_duplicate_and_malformed_gate_refuse(self):
        for value in (None, '', 'none ()', 'none ( )', 'tests/check.py', '../check.py::test_a',
                      '/tmp/check.py::test_a', 'check.py::', 'check.py::a; nonsense',
                      'app:../check.py::test_a', 'app:/tmp/check.py::test_a',
                      'app:checks.py::', 'unknown:bad:path.py::test_a'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.m.gate_references(value)
        parsed = self.m.parse_instance_rules(rule() + 'Gate: none (duplicate)\n')[0]
        self.assertIsNone(parsed['gate'])

    def test_fenced_and_commented_examples_do_not_supply_metadata_or_rules(self):
        text = rule(gate=None) + '```markdown\nGate: none (example)\n' + rule('R99') + '```\n'
        text += '<!--\nGate: none (comment)\n-->\n'
        rows = self.m.parse_instance_rules(text)
        self.assertEqual([row['id'] for row in rows], ['R01'])
        self.assertIsNone(rows[0]['gate'])

    def test_node_bullets_have_stable_content_ids_and_optional_explicit_ids(self):
        text = '# App\n- Outside section.\n## Rules for this node\n\n- Keep evidence.\n  Continued.\n\n'
        before = self.m.parse_node_rules(text, 'app')[0]
        annotated = text.rstrip() + ' Gate: none (manual) Applies: app\n'
        after = self.m.parse_node_rules(annotated, 'app')[0]
        self.assertEqual(before['id'], after['id'])
        self.assertEqual(after['gate'], 'none (manual)')
        self.assertEqual(after['applies'], 'app')
        rows = self.m.parse_node_rules(text + '<!-- rule:LOCAL-1 -->\n- Explicit. Gate: none (manual) Applies: all\n'
                                       '## Notes\n- Not a rule.\n', 'app')
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]['id'], 'LOCAL-1')
        self.assertEqual(rows[1]['text'].splitlines()[0], '<!-- rule:LOCAL-1 -->')

    def test_inline_comments_cannot_hide_a_node_rule_and_long_fences_stay_closed(self):
        rows = self.m.parse_node_rules('## Rules for this node\n- Keep <!-- note --> receipts.\n', 'app')
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]['gate'])
        text = rule(gate=None) + '````markdown\n```\nGate: none (example)\n````\n'
        self.assertIsNone(self.m.parse_instance_rules(text)[0]['gate'])

    def test_only_named_declarations_inside_home_count(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            (home / 'checks.py').write_text('class Checks:\n    def test_receipt(self): pass\n'
                                          'def test_one(): pass\n# def test_imaginary(): pass\n')
            for selector in ('Checks::test_receipt', 'test_receipt', 'test_one'):
                self.assertTrue(self.m._test_exists(home, 'checks.py', selector, {}))
            for selector in ('Checks', 'test_imaginary', 'Checks::test_missing', 'test_one::test_fake'):
                self.assertFalse(self.m._test_exists(home, 'checks.py', selector, {}))
            (home / 'escape.py').symlink_to(Path(__file__).resolve())
            self.assertFalse(self.m._test_exists(home, 'escape.py', 'test_one', {}))

    def test_javascript_literal_test_names_are_syntax_not_substring_matches(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            (home / 'checks.test.ts').write_text('test("keeps source evidence", () => {});\n'
                "it.only('keeps \\'quoted\\' evidence', () => {});\n"
                '// test("commented test", () => {});\n'
                'const example = `test("string example", () => {})`;\n'
                'const pattern = /test("regex example", () => {})/;\n'
                'matcher.test("member example", () => {});\n'
                '/* it("block example", () => {}); */\n')
            for selector in ('keeps source evidence', "keeps 'quoted' evidence"):
                self.assertEqual(self.m.gate_references('app:checks.test.ts::' + selector),
                                 [('app', 'checks.test.ts', selector)])
                self.assertTrue(self.m._test_exists(home, 'checks.test.ts', selector, {}))
            for selector in ('commented test', 'string example', 'regex example', 'member example', 'block example', 'missing'):
                self.assertFalse(self.m._test_exists(home, 'checks.test.ts', selector, {}))

    def test_real_size_registry_is_under_one_second(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            (home / 'checks.py').write_text('def test_receipt(): pass\n')
            nodes = {}
            for i in range(100):
                node_home = home / str(i)
                node_home.mkdir()
                (node_home / 'checks.py').write_text('def test_receipt(): pass\n')
                nodes['app-' + str(i)] = {'id': 'app-' + str(i), 'home_path': node_home}
            rows = self.m.parse_instance_rules(''.join(rule('R' + str(i), 'app-' + str(i) + ':checks.py::test_receipt')
                                                       for i in range(92)))
            start = time.monotonic()
            self.assertEqual(self.m.validate_registry(None, (rows, nodes)), [])
            self.assertLess(time.monotonic() - start, 1.0)


class RuleRegistryChecks(Scenario):
    header = ('---\nid: instance-rules\ntitle: Test rules\nkind: method\nsource_node: root\n'
              'source_ref: fabricated\nlearned_at: 2026-01-01\nsource_owners: alice\n'
              'scope: alice\nstatus: active\n---\n')

    def fixture(self, baseline=True):
        root = self.init('alice')
        self.b(root, 'new', 'app', '--title', 'App')
        self.b(root, 'new', 'excluded', '--title', 'Excluded')
        library = root / 'library/instance-rules.md'
        library.parent.mkdir(exist_ok=True)
        if baseline:
            self.b(root, 'check')
        return root, library

    def test_check_missing_gate_fails_and_none_reason_passes(self):
        root, library = self.fixture()
        library.write_text(self.header + rule(gate=None, applies='app'))
        output = self.b(root, 'check', ok=False)
        self.assertIn('R01', output)
        self.assertIn('Gate:', output)
        self.assertFalse((root / 'nodes/app/tasks/SWEEP-R01.md').exists())
        library.write_text(self.header + rule(applies='app'))
        self.assertIn('all checks pass', self.b(root, 'check'))

    def test_missing_test_and_unknown_applies_fail_with_rule_id(self):
        root, library = self.fixture()
        library.write_text(self.header + rule(gate='app:tests/checks.py::test_missing', applies='app'))
        self.assertIn('R01: app: missing Gate test', self.b(root, 'check', ok=False))
        library.write_text(self.header + rule(applies='unknown'))
        self.assertIn('rule R01: Applies', self.b(root, 'check', ok=False))

    def test_first_check_baselines_existing_rules_and_only_later_additions_sweep(self):
        root, library = self.fixture(baseline=False)
        library.write_text(self.header + rule(applies='app'))
        self.b(root, 'check')
        self.assertFalse((root / 'nodes/app/tasks/SWEEP-R01.md').exists())
        library.write_text(library.read_text() + rule('R02', applies='app, excluded'))
        self.b(root, 'check')
        self.assertTrue((root / 'nodes/app/tasks/SWEEP-R02.md').exists())
        self.assertTrue((root / 'nodes/excluded/tasks/SWEEP-R02.md').exists())
        self.assertFalse((root / 'nodes/app/tasks/SWEEP-R01.md').exists())

    def test_gate_owner_is_independent_of_rule_scope_and_unknown_owner_fails(self):
        root, library = self.fixture()
        (root / 'nodes/app/checks.py').write_text('def test_receipt(): pass\n')
        library.write_text(self.header + rule(gate='app:checks.py::test_receipt', applies='all'))
        self.b(root, 'check')
        self.assertTrue((root / 'nodes/excluded/tasks/SWEEP-R01.md').exists())
        library.write_text(self.header + rule(gate='unknown:checks.py::test_receipt', applies='all'))
        self.assertIn('rule R01: unknown or unbound Gate home unknown', self.b(root, 'check', ok=False))

    def test_overlapping_instance_and_node_ids_fail_before_any_sweep_is_written(self):
        root, library = self.fixture()
        library.write_text(self.header + rule(applies='app'))
        agents = root / 'nodes/app/AGENTS.md'
        agents.write_text(agents.read_text() + '\n<!-- rule:R01 -->\n'
                          '- Preserve node receipts. Gate: none (manual) Applies: app\n')
        for _ in range(2):
            output = self.b(root, 'check', ok=False)
            self.assertIn('rule app:R01: sweep ID collision with R01 in app', output)
            self.assertFalse((root / 'nodes/app/tasks/SWEEP-R01.md').exists())

    def test_rehearsal_read_validates_without_sweeps_or_checkpoint(self):
        root, library = self.fixture(baseline=False)
        library.write_text(self.header + rule(applies='app'))
        self.env['BRAIN_REHEARSAL_READ'] = '1'
        self.assertIn('all checks pass', self.b(root, 'check'))
        self.assertFalse((root / 'nodes/app/tasks/SWEEP-R01.md').exists())
        self.assertFalse((root / '.brain/rule-registry.json').exists())
        library.write_text(self.header + rule(gate=None, applies='app'))
        self.assertIn('rule R01: missing or duplicate Gate:', self.b(root, 'check', ok=False))

    def test_invalid_rule_keeps_its_gap_and_does_not_stop_other_rule_sweeps(self):
        root, library = self.fixture()
        library.write_text(self.header + rule(applies='app') + rule('R02', gate=None, applies='app'))
        self.assertIn('rule R02: missing or duplicate Gate:', self.b(root, 'check', ok=False))
        first = root / 'nodes/app/tasks/SWEEP-R01.md'
        second = root / 'nodes/app/tasks/SWEEP-R02.md'
        self.assertTrue(first.exists())
        self.assertFalse(second.exists())
        checkpoint = json.loads((root / '.brain/rule-registry.json').read_text())
        self.assertEqual(checkpoint['rules'], [json.dumps([None, 'R01'])])
        library.write_text(self.header + rule(applies='app') + rule('R02', applies='app'))
        self.b(root, 'check')
        self.assertTrue(second.exists())

    def test_diagnostics_and_live_probes_are_unlocked_but_repairs_stay_locked(self):
        root, _ = self.fixture()
        registry = registry_module()
        check = importlib.import_module(registry.__package__ + '.check')
        inst = registry.instance.Instance(root)
        for fix, live in ((False, False), (False, True), (True, False)):
            with self.subTest(fix=fix, live=live):
                calls = []
                def diagnostics(_):
                    calls.append(True)
                    if fix:
                        self.assertIsNotNone(registry.instance._LOCK['fh'])
                    else:
                        self.assertIsNone(registry.instance._LOCK['fh'])
                        result = subprocess.run([sys.executable, '-c',
                            'import fcntl,sys; f=open(sys.argv[1], "a+"); '
                            'fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)', str(root / '.brain/lock')],
                            capture_output=True, text=True)
                        self.assertEqual(result.returncode, 0, result.stderr)
                    return [], [], ['facts'] if fix and len(calls) == 1 else []
                def repair(*_):
                    self.assertIsNotNone(registry.instance._LOCK['fh'])
                    return []
                def probe(*_, **__):
                    self.assertIsNone(registry.instance._LOCK['fh'])
                    return 200
                with mock.patch.object(check._m_nodes, 'load', return_value=inst), \
                        mock.patch.object(check, 'collect_problems', side_effect=diagnostics), \
                        mock.patch.object(check, 'apply_fixes', side_effect=repair), \
                        mock.patch.object(check._m_hosts, 'wake_target', return_value=({'id': 'root'}, 'http://127.0.0.1:1/')), \
                        mock.patch.object(check._m_hosts, 'wake_disabled_notice', return_value=None), \
                        mock.patch.object(check._m_hosts, 'wake_probe', side_effect=probe), \
                        mock.patch.object(check._m_hosts, 'wake_read', return_value=None), \
                        mock.patch.object(check._m_hosts, 'wake_directory', return_value=root / '.brain'), \
                        contextlib.redirect_stdout(io.StringIO()), registry.instance.brain_lock(inst):
                    check.cmd_check(argparse.Namespace(fix=fix, live=live))

    def test_new_rule_sweeps_only_applicable_homes_and_repeated_check_is_idempotent(self):
        root, library = self.fixture()
        library.write_text(self.header + rule(applies='app'))
        self.b(root, 'check')
        task = root / 'nodes/app/tasks/SWEEP-R01.md'
        first = task.read_bytes()
        self.assertIn(rule(applies='app').strip(), task.read_text())
        self.assertIn('status: "ready"', task.read_text())
        self.assertFalse((root / 'nodes/excluded/tasks/SWEEP-R01.md').exists())
        self.b(root, 'check')
        self.assertEqual(first, task.read_bytes())
        library.write_text(library.read_text() + rule('R02', applies='app, excluded'))
        self.b(root, 'check')
        self.assertTrue((root / 'nodes/app/tasks/SWEEP-R02.md').exists())
        self.assertTrue((root / 'nodes/excluded/tasks/SWEEP-R02.md').exists())
        self.assertEqual(first, task.read_bytes())

    def test_new_node_bullet_gets_local_sweep_and_unrelated_collision_refuses(self):
        root, library = self.fixture()
        agents = root / 'nodes/app/AGENTS.md'
        agents.write_text(agents.read_text() + '\n- Preserve receipts. Gate: none (manual) Applies: app\n')
        self.b(root, 'check')
        self.assertEqual(len(list((root / 'nodes/app/tasks').glob('SWEEP-N-*.md'))), 1)
        self.assertEqual(list((root / 'nodes/excluded/tasks').glob('SWEEP-*.md')), [])
        self.b(root, 'task', 'add', 'app', 'SWEEP-R01', '--title', 'Unrelated', '--status', 'ready')
        library.write_text(self.header + rule(applies='app'))
        self.assertIn('sweep task collision', self.b(root, 'check', ok=False))

    def test_checkpoint_failure_replays_saved_tasks_and_corrupt_state_refuses(self):
        root, library = self.fixture()
        library.write_text(self.header + rule(applies='app'))
        m = registry_module()
        inst = m.instance.Instance(root)
        original = m.common.atomic_write
        def fail_checkpoint(path, text, *args, **kwargs):
            if Path(path).name == 'rule-registry.json':
                raise OSError('injected checkpoint failure')
            return original(path, text, *args, **kwargs)
        with mock.patch.object(m.common, 'atomic_write', side_effect=fail_checkpoint):
            with self.assertRaisesRegex(OSError, 'injected checkpoint'):
                m.sync_sweeps(inst)
        task = root / 'nodes/app/tasks/SWEEP-R01.md'
        self.assertTrue(task.exists())
        first = task.read_bytes()
        self.assertEqual(m.sync_sweeps(inst), [])
        self.assertEqual(first, task.read_bytes())
        (root / '.brain/rule-registry.json').write_text('{"version": 2}')
        with self.assertRaisesRegex(ValueError, 'invalid rule registry state'):
            m.sync_sweeps(inst)
