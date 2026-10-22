"""Ownership, selection and negative proofs for the repository's unit lane map."""
import copy
import importlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import check_lanes
import lanes


class RepositoryLanes(unittest.TestCase):
    def test_test_tool_pin_and_browser_assets_have_inputs(self):
        mapping = lanes.lane_map()
        sys.path.insert(0, str(lanes.ROOT / 'lib'))
        from brain.verify_lanes import matches
        for path in ('requirements-test.txt', '.gitignore'):
            with self.subTest(path=path):
                self.assertTrue(all(matches(path, lane['inputs']) for lane in mapping))
        ui = next(lane for lane in mapping if lane['name'] == 'ui')
        for path in ('tests/live.py', 'tests/ui-checks.js', 'tests/ui-fitness.js'):
            with self.subTest(path=path):
                self.assertTrue(matches(path, ui['inputs']))
        changed = copy.deepcopy(mapping)
        changed[0]['inputs'].remove('requirements-test.txt')
        with self.assertRaisesRegex(ValueError, 'missing shared inputs: .*requirements-test.txt'):
            check_lanes.check(mapping=changed)

    def test_runtime_assets_and_imported_fixture_select_consumers(self):
        mapping = {lane['name']: lane for lane in lanes.lane_map()}
        sys.path.insert(0, str(lanes.ROOT / 'lib'))
        from brain.verify_lanes import matches
        self.assertTrue(all(matches('roles/coordinator.md', lane['inputs']) for lane in mapping.values()))
        self.assertTrue(matches('tests/live.py', mapping['ui']['inputs']))

    def test_uncovered_repository_file_and_support_helper_fail(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            shutil.copytree(lanes.ROOT / 'tests', root / 'tests', ignore=shutil.ignore_patterns('__pycache__'))
            shutil.copytree(lanes.ROOT / 'lib', root / 'lib', ignore=shutil.ignore_patterns('__pycache__'))
            (root / 'fixture-runtime.txt').write_text('made-up runtime input\n')
            with self.assertRaisesRegex(ValueError, 'file has no lane inputs: fixture-runtime.txt'):
                check_lanes.check(root)
            (root / 'fixture-runtime.txt').unlink()
            (root / 'tests/helper.py').write_text('FIXTURE = 1\n')
            with mock.patch.object(lanes, 'SUPPORT', lanes.SUPPORT + ('helper.py',)):
                with self.assertRaisesRegex(ValueError, 'file has no lane inputs: tests/helper.py'):
                    check_lanes.check(root)

    def test_lost_nonselective_import_edge_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            shutil.copytree(lanes.ROOT / 'tests', root / 'tests', ignore=shutil.ignore_patterns('__pycache__'))
            shutil.copytree(lanes.ROOT / 'lib', root / 'lib', ignore=shutil.ignore_patterns('__pycache__'))
            path = root / 'lib/brain/jobs.py'
            source = path.read_text()
            self.assertIn('from . import sync as _m_sync\n', source)
            path.write_text(source.replace('from . import sync as _m_sync\n', ''))
            with self.assertRaisesRegex(ValueError, 'nonselective module input missing: .*lib/brain/sync.py'):
                check_lanes.check(root)

    def test_conditional_canonical_class_cannot_be_skipped(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            shutil.copytree(lanes.ROOT / 'tests', root / 'tests', ignore=shutil.ignore_patterns('__pycache__'))
            shutil.copytree(lanes.ROOT / 'lib', root / 'lib', ignore=shutil.ignore_patterns('__pycache__'))
            path = root / 'tests/test_messages.py'
            path.write_text(path.read_text() + '\nif True:\n class ConditionalFixture(Scenario):\n  def test_example(self): pass\n')
            with self.assertRaisesRegex(ValueError, 'canonical class exactly once'):
                check_lanes.check(root)

    def test_command_compatibility_shared_and_target_guards_fail(self):
        mapping = lanes.lane_map()
        for replacement, message in [('echo success', 'unexpected test command'),
                                     ('python3 -B -m pytest -q -p no:cacheprovider README.md', 'unexpected test target')]:
            with self.subTest(message=message):
                changed = copy.deepcopy(mapping)
                changed[0]['command'] = replacement
                with self.assertRaisesRegex(ValueError, message):
                    check_lanes.check(mapping=changed)
        with self.assertRaisesRegex(ValueError, 'compatibility entry has no catch-all'):
            check_lanes.check(mapping=[lane for lane in mapping if lane['name'] != 'catch-all'])
        changed = copy.deepcopy(mapping)
        changed[0]['inputs'].remove('tests/scenario.py')
        with self.assertRaisesRegex(ValueError, 'missing shared inputs'):
            check_lanes.check(mapping=changed)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'tests').mkdir()
            path = root / 'tests/test_example.py'
            path.write_text('def test_standalone(): pass\n')
            with self.assertRaisesRegex(ValueError, 'assign standalone pytest functions'):
                lanes.test_targets(root, [path.name])
            path.write_text('FIXTURE = 1\n')
            with self.assertRaisesRegex(ValueError, 'no canonical test classes'):
                lanes.test_targets(root, [path.name])

    def test_coverage_and_shared_selection(self):
        mapping = lanes.lane_map()
        result = check_lanes.check(mapping=mapping)
        self.assertEqual(result['unit_files'], len(list((lanes.ROOT / 'tests').glob('test_*.py'))))
        self.assertGreater(result['lanes'], 1)
        self.assertEqual(set(result['outside_job_lanes']), {'live.py', 'ui-checks.js', 'ui-fitness.js'})
        sys.path.insert(0, str(lanes.ROOT / 'lib'))
        from brain.verify_lanes import matches
        self.assertTrue(all(matches('lib/brain/common.py', lane['inputs']) for lane in mapping))
        selected = {lane['name'] for lane in mapping if matches('lib/brain/train.py', lane['inputs'])}
        self.assertIn('train', selected)
        self.assertIn('catch-all', selected)
        self.assertLess(len(selected), len(mapping))
        self.assertNotIn('usage', selected)
        self.assertNotIn('cli', selected)
        self.assertTrue(all(not matches('docs/example.md', lane['inputs']) for lane in mapping))
        self.assertTrue(all(not matches('README.md', lane['inputs']) for lane in mapping))
        self.assertTrue(all(not matches('ui/screenshots/example.png', lane['inputs']) for lane in mapping))

    def test_unassigned_duplicate_and_removed_tests_fail(self):
        mapping = lanes.lane_map()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            shutil.copytree(lanes.ROOT / 'tests', root / 'tests', ignore=shutil.ignore_patterns('__pycache__'))
            (root / 'tests/test_unassigned.py').write_text('import unittest\nclass Unassigned(unittest.TestCase):\n def test_example(self): pass\n')
            with self.assertRaisesRegex(ValueError, 'exactly one lane: test_unassigned.py'):
                check_lanes.check(root, mapping)
            shutil.copytree(lanes.ROOT / 'lib', root / 'lib', ignore=shutil.ignore_patterns('__pycache__'))
            script = root / 'tests/check_lanes.py'
            result = subprocess.run([sys.executable, '-B', str(script)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn('test_unassigned.py', result.stderr)
            (root / 'tests/test_unassigned.py').unlink()
            self.assertGreater(check_lanes.check(root, mapping)['lanes'], 0)
            result = subprocess.run([sys.executable, '-B', str(script)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            duplicate = copy.deepcopy(mapping)
            duplicate.append({**mapping[0], 'name': 'duplicate'})
            with self.assertRaisesRegex(ValueError, 'exactly one lane'):
                check_lanes.check(root, duplicate)
            omitted = copy.deepcopy(mapping)
            omitted[0]['command'] = omitted[0]['command'].replace('::WorkerBriefs', '::MissingClass')
            with self.assertRaisesRegex(ValueError, 'canonical class exactly once'):
                check_lanes.check(root, omitted)
            (root / 'tests/test_train.py').unlink()
            with self.assertRaisesRegex(ValueError, 'missing test: test_train.py'):
                check_lanes.check(root, mapping)

    def test_unknown_test_program_fails(self):
        mapping = lanes.lane_map()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            shutil.copytree(lanes.ROOT / 'tests', root / 'tests', ignore=shutil.ignore_patterns('__pycache__'))
            (root / 'tests/new-check.js').write_text('throw new Error("example");\n')
            with self.assertRaisesRegex(ValueError, 'unclassified tests/ file: new-check.js'):
                check_lanes.check(root, mapping)

    def test_relative_imports_propagate_at_call_time(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            shutil.copytree(lanes.ROOT / 'tests', root / 'tests', ignore=shutil.ignore_patterns('__pycache__'))
            shutil.copytree(lanes.ROOT / 'lib', root / 'lib', ignore=shutil.ignore_patterns('__pycache__'))
            path = root / 'lib/brain/usage.py'
            path.write_text(path.read_text() + '\nfrom . import train as _m_train\n\ndef fixture_callback():\n return _m_train.ref(None, None)\n')
            affected = {lane['name'] for lane in lanes.lane_map(root) if 'lib/brain/train.py' in lane['inputs']}
            self.assertIn('usage', affected)
            self.assertIn('hosts', affected)

    def test_startup_changes_force_map_but_function_body_keeps_selection(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            shutil.copytree(lanes.ROOT / 'tests', root / 'tests', ignore=shutil.ignore_patterns('__pycache__'))
            shutil.copytree(lanes.ROOT / 'lib', root / 'lib', ignore=shutil.ignore_patterns('__pycache__'))
            before = lanes.lane_map(root)
            path = root / 'lib/brain/train.py'
            source = path.read_text()
            path.write_text(source.replace('r = _m_common.git(', 'r = _m_common.git(  ', 1))
            self.assertEqual(before, lanes.lane_map(root))
            path.write_text(source.replace('def ref(home, name):', 'def ref(home, name):\n    fixture_value = 1'))
            self.assertEqual(before, lanes.lane_map(root))
            path.write_text(source + '\nFIXTURE_STARTUP = 1\n')
            after = lanes.lane_map(root)
            self.assertTrue(all(a['command'] != b['command'] for a, b in zip(before, after)))

    def test_compatibility_import_has_exact_canonical_classes(self):
        compatibility = importlib.import_module('test_brain')
        expected = {}
        for files in lanes.LANE_TESTS.values():
            for file in files:
                module = importlib.import_module(Path(file).stem)
                for name, value in vars(module).items():
                    if isinstance(value, type) and issubclass(value, unittest.TestCase) and value.__module__ == module.__name__:
                        self.assertNotIn(name, expected, 'compatibility would overwrite a class')
                        expected[name] = value
        actual = {name: value for name, value in vars(compatibility).items()
                  if isinstance(value, type) and issubclass(value, unittest.TestCase)}
        self.assertEqual(expected, actual)
        self.assertEqual(unittest.TestLoader().loadTestsFromModule(compatibility).countTestCases(), 0)
        count = sum(unittest.TestLoader().loadTestsFromTestCase(value).countTestCases() for value in expected.values())
        self.assertGreater(count, 0)

    def test_script_output_is_valid_engine_map(self):
        proc = subprocess.run([sys.executable, '-B', str(lanes.ROOT / 'tests/lanes.py')], capture_output=True, text=True, check=True)
        mapping = json.loads(proc.stdout)
        self.assertEqual(mapping, lanes.lane_map())
        sys.path.insert(0, str(lanes.ROOT / 'lib'))
        from brain.verify_lanes import lane_map
        config = {'fm': {'verify_lanes': json.dumps(mapping)}}
        parsed, digest = lane_map(config, {'path': str(lanes.ROOT)}, {})
        self.assertEqual(len(parsed), len(mapping))
        self.assertEqual(len(digest), 64)
