"""Scenario tests for core/bin/brain. Run with `brain selftest` (or python3 -m unittest in tests/).

Every test builds instances in a throwaway folder that is also $HOME, so nothing real is touched.
"""
import json
import re
import datetime
import importlib.util
import core_loader
import threading
from unittest import mock
import argparse
import contextlib
import io
import os
import shutil
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

CORE = Path(os.environ.get("BRAIN_CORE") or Path(__file__).resolve().parent.parent)


from scenario import Scenario, AutonomyProject, shlex_quote


class FixtureSetup(Scenario):
    def test_run_cleanup_retries_errors_and_handles_missing_and_read_only_entries(self):
        script = '''import os, pathlib, shutil, sys
from unittest import mock
import tempdirs
root = tempdirs.configure()
assert not (root / 'persistent.json').exists()
persistent = tempdirs.fixture_parent(pathlib.Path(sys.argv[1]))
locked = root / 'locked'
locked.mkdir()
(locked / 'sample').touch()
locked.chmod(0)
original = shutil.rmtree
with mock.patch.object(shutil, 'rmtree', side_effect=PermissionError('sample busy cleanup')):
    tempdirs.cleanup()
assert root.exists() and persistent.exists()
def racing(path, onerror):
    path = pathlib.Path(path)
    error = FileNotFoundError('sample concurrent removal')
    onerror(os.unlink, str(path / 'already-gone'), (FileNotFoundError, error, None))
    return original(path, onerror=onerror)
with mock.patch.object(shutil, 'rmtree', side_effect=racing):
    tempdirs.cleanup()
assert not root.exists() and not persistent.exists()
'''
        env = dict(self.env)
        env.pop('BRAIN_TEST_RUN_ROOT', None)
        result = subprocess.run([sys.executable, '-B', '-c', script, str(self.tmp)],
                                env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_fixture_directory_is_removed_after_success_failure_and_interrupt(self):
        for stage in ('success', 'setup', 'test', 'interrupt'):
            with self.subTest(stage=stage):
                paths = []
                class Probe(Scenario):
                    def setUp(self):
                        super().setUp()
                        paths.append(self.tmp)
                        if stage == 'setup':
                            raise RuntimeError('sample setup failure')

                    def runTest(self):
                        if stage == 'test':
                            raise AssertionError('sample test failure')
                        if stage == 'interrupt':
                            raise KeyboardInterrupt

                case = Probe()
                result = unittest.TestResult()
                try:
                    if stage == 'interrupt':
                        with self.assertRaises(KeyboardInterrupt):
                            case.run(result)
                    else:
                        case.run(result)
                        self.assertEqual(result.wasSuccessful(), stage == 'success')
                    self.assertEqual(len(paths), 1)
                    self.assertFalse(paths[0].exists())
                finally:
                    # Explicit disposal also lets this regression run against
                    # the old helper without leaving its injected setup leak.
                    for path in paths:
                        shutil.rmtree(path, ignore_errors=True)

    def test_cached_setup_relocates_worktrees_and_never_shares_mutable_state(self):
        import scenario
        built = []
        children = []
        def child():
            case = Scenario()
            case.fixture_init_in_process = case.fixture_templates = True
            case.setUp()
            children.append(case)
            self.addCleanup(case.doCleanups)
            self.addCleanup(case.tearDown)
            return case
        def build(case):
            built.append(case.tmp)
            root = case.init('alice')
            home = case.repo(case.tmp / 'app')
            case.b(root, 'new', 'app', '--title', 'App', '--home', str(home))
            worktree = case.tmp / 'worker'
            case.git('worktree', 'add', '-qb', 'job', str(worktree), cwd=home)
            script = case.tmp / 'binding.txt'
            script.write_text(str(home))
            (case.tmp / 'absolute-link').symlink_to(home)
            return {'root': root, 'home': home, 'worktree': worktree, 'command': str(script)}
        with mock.patch.object(scenario, '_setup_templates', {}), \
                mock.patch.object(scenario, '_instance_templates', {}), \
                mock.patch.object(scenario, '_repo_templates', {}):
            try:
                first, second = child(), child()
                left = first.cached_setup('isolation-control', lambda: build(first))
                right = second.cached_setup('isolation-control', lambda: build(second))
                self.assertEqual(built, [first.tmp])
                self.assertEqual(Path(right['command']).read_text(), str(right['home']))
                self.assertEqual((second.tmp / 'absolute-link').resolve(), right['home'])
                for case, tree in ((first, left), (second, right)):
                    self.assertEqual(case.git('status', '--porcelain', cwd=tree['root']), '')
                    self.assertEqual(case.git('status', '--porcelain', cwd=tree['worktree']), '')
                    self.assertEqual(Path(case.git('rev-parse', '--show-toplevel', cwd=tree['worktree']).strip()),
                                     tree['worktree'])
                    binding = json.loads((tree['root'] / '.brain/local.json').read_text())
                    self.assertEqual(binding['nodes']['app']['home'], str(tree['home']))
                    prefix = Path(case.env['PYTHONPYCACHEPREFIX'])
                    self.assertEqual(case.env['PYTHONDONTWRITEBYTECODE'], '1')
                    self.assertTrue((prefix / (tree['root'] / 'core/lib/brain').relative_to(tree['root'].anchor)).is_symlink())
                for relative in ('README.md', '.git/config'):
                    a, b = left['home'] / relative, right['home'] / relative
                    self.assertNotEqual((a.stat().st_dev, a.stat().st_ino), (b.stat().st_dev, b.stat().st_ino))
                    self.assertEqual(a.stat().st_nlink, 1)
                    self.assertEqual(b.stat().st_nlink, 1)
                (left['worktree'] / 'README.md').write_text('only the first worktree\n')
                (left['root'] / 'core/bin/brain').write_text('changed first installed core\n')
                self.assertEqual((right['worktree'] / 'README.md').read_text(), 'hello\n')
                self.assertEqual((right['root'] / 'core/bin/brain').read_bytes(), (CORE / 'bin/brain').read_bytes())
                first.tearDown()
                self.assertIn('app', second.b(right['root'], 'tree'))
                raw = subprocess.run([sys.executable, str(right['root'] / 'core/bin/brain'), 'tree'],
                                     cwd=right['root'], env=second.env, capture_output=True, text=True)
                self.assertEqual(raw.returncode, 0, raw.stdout + raw.stderr)
                self.assertIn('app', raw.stdout)
                self.assertEqual(second.git('status', '--porcelain', cwd=right['worktree']), '')
            finally:
                for cache in (scenario._setup_templates, scenario._instance_templates, scenario._repo_templates):
                    for template in cache.values():
                        template.directory.cleanup()

    def test_instance_templates_preserve_registration_operator_and_independent_git(self):
        import scenario
        cases = []
        with mock.patch.object(scenario, '_instance_templates', {}):
            try:
                for _ in range(2):
                    case = Scenario()
                    case.fixture_init_in_process = case.fixture_templates = True
                    case.setUp()
                    cases.append(case)
                    self.addCleanup(case.doCleanups)
                    self.addCleanup(case.tearDown)
                    case.env['BRAIN_CONFIG'] = str(case.tmp / 'custom/brains.json')
                    root = case.init('alice')
                    second = case.init('bob', operator='other')
                    registration = json.loads(Path(case.env['BRAIN_CONFIG']).read_text())
                    self.assertEqual(registration['default'], 'alice')
                    self.assertEqual(registration['brains'], {'alice': {'root': str(root)}, 'bob': {'root': str(second)}})
                    self.assertIn('principals: other', (second / 'instance.md').read_text())
                    self.assertEqual(case.git('status', '--porcelain', cwd=root), '')
                    self.assertEqual(case.git('status', '--porcelain', cwd=second), '')
                    nested = case.repo(case.tmp / 'nested' / 'repositories' / 'app')
                    self.assertEqual(case.git('status', '--porcelain', cwd=nested), '')
                self.assertEqual(len(scenario._instance_templates), 2)
                first, second = (case.tmp / 'alice' for case in cases)
                before = (second / '.git/config').read_bytes()
                (first / '.git/config').write_text('changed first config\n')
                self.assertEqual((second / '.git/config').read_bytes(), before)
            finally:
                for template in scenario._instance_templates.values():
                    template.directory.cleanup()

    def test_failed_setup_snapshot_is_never_published(self):
        import scenario
        case = Scenario()
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.addCleanup(case.tearDown)
        candidates = []
        def broken_copy(source, destination):
            candidates.append(destination.parent)
            destination.mkdir()
            (destination / 'partial').write_text('incomplete\n')
            raise OSError('injected snapshot failure')
        with mock.patch.object(scenario, '_setup_templates', {}), \
                mock.patch.object(scenario, '_copy_fixture_tree', side_effect=broken_copy):
            with self.assertRaisesRegex(OSError, 'injected snapshot failure'):
                case.cached_setup('broken', lambda: case.tmp)
            self.assertEqual(scenario._setup_templates, {})
            self.assertEqual(len(candidates), 1)
            self.assertFalse(candidates[0].exists())

    def test_concurrent_runtime_cache_initialization_compiles_each_core_source_once(self):
        import scenario
        source, package = self.fixture_source()
        results, errors = [], []
        original_compile = scenario.py_compile.compile
        with mock.patch.object(scenario, 'CORE', source), \
                mock.patch.object(scenario, '_runtime_bytecode', None), \
                mock.patch.object(scenario.py_compile, 'compile', wraps=original_compile) as compile_source:
            try:
                def initialize():
                    try:
                        results.append(scenario.fixture_runtime_cache(source))
                    except BaseException as error:
                        errors.append(error)
                threads = [threading.Thread(target=initialize) for _ in range(8)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)
                self.assertFalse(any(thread.is_alive() for thread in threads))
                self.assertEqual(errors, [])
                self.assertEqual(len(results), 8)
                self.assertEqual(len(set(results)), 1)
                compiled = [call.args[0] for call in compile_source.call_args_list
                            if Path(call.args[0]).parent == package]
                self.assertEqual(sorted(compiled), sorted(str(path) for path in package.glob('*.py')))
            finally:
                if scenario._runtime_bytecode is not None:
                    scenario._runtime_bytecode.cleanup()

    def test_private_runtime_cache_reuses_code_without_poisoning_other_cores(self):
        import scenario
        source, package = self.fixture_source()
        state = package / 'state.py'
        state.write_text('def current(): return "first"\n')
        (source / 'bin/brain').write_text(
            'import sys\nfrom pathlib import Path\n'
            'sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))\n'
            'from brain.state import current\nprint(current())\nprint(current.__code__.co_filename)\n')
        first, second = self.tmp / 'one', self.tmp / 'two'
        shutil.copytree(source, first)
        shutil.copytree(source, second)
        with mock.patch.object(scenario, 'CORE', source), \
                mock.patch.object(scenario, '_runtime_bytecode', None):
            try:
                def read(core):
                    return self.run_([], self.tmp, brain=core / 'bin/brain').splitlines()
                self.assertEqual(read(first), ['first', str(first / 'lib/brain/state.py')])
                self.assertEqual(read(second), ['first', str(second / 'lib/brain/state.py')])
                prefix = Path(scenario._runtime_bytecode.name)
                links = [prefix / (core / 'lib/brain').relative_to(core.anchor)
                         for core in (first, second)]
                self.assertTrue(all(link.is_symlink() for link in links))
                self.assertEqual(links[0].resolve(), links[1].resolve())
                cache = next(links[0].glob('state.*.pyc'))
                before = cache.read_bytes()
                self.assertEqual(int.from_bytes(before[4:8], 'little'), 3)
                changed = first / 'lib/brain/state.py'
                stamp = changed.stat()
                changed.write_text('def current(): return "other"\n')
                os.utime(changed, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
                self.assertEqual(changed.stat().st_size, stamp.st_size)
                self.assertEqual(read(first), ['other', str(changed)])
                self.assertEqual(read(second)[0], 'first')
                self.assertEqual(cache.read_bytes(), before)
                self.assertEqual(list(first.rglob('__pycache__')), [])
                self.assertEqual(list(second.rglob('__pycache__')), [])
                changed.unlink()
                missing = self.run_([], self.tmp, ok=False, brain=first / 'bin/brain')
                self.assertIn('ModuleNotFoundError', missing)
            finally:
                if scenario._runtime_bytecode is not None:
                    scenario._runtime_bytecode.cleanup()

    def test_commit_avoids_existing_path_queries_and_preserves_owned_deletions(self):
        root = self.init('alice')
        deleted = root / 'deleted.txt'
        deleted.write_text('tracked\n')
        self.git('add', 'deleted.txt', cwd=root)
        self.git('commit', '-qm', 'tracked deletion fixture', cwd=root)
        deleted.unlink()
        created = root / 'created.txt'
        created.write_text('new owned file\n')
        empty = root / 'empty'
        empty.mkdir()
        (root / 'unrelated.txt').write_text('leave this work alone\n')
        module = self.load_fixture_source(CORE)
        common = module._owners['TOUCHED']
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(common, 'git', wraps=common.git) as query:
            module.Instance(root).commit([created, deleted, empty, root / 'missing'], 'owned changes')
        lookups = [call.args for call in query.call_args_list if call.args[:1] == ('ls-files',)]
        self.assertEqual(set(lookups), {('ls-files', '--', name) for name in ('deleted.txt', 'empty', 'missing')})
        self.assertEqual(self.git('show', '--pretty=format:', '--name-only', 'HEAD', cwd=root).split(),
                         ['created.txt', 'deleted.txt'])
        self.assertEqual(self.git('status', '--porcelain', cwd=root).strip(), '?? unrelated.txt')

    def test_template_bytecode_is_built_once_and_validates_changed_source(self):
        import scenario
        source, package = self.fixture_source()
        state = package / 'state.py'
        state.write_text('def current(): return "first"\n')
        original_compile = scenario.py_compile.compile
        with mock.patch.object(scenario, 'CORE', source), \
                mock.patch.object(scenario, '_core_template', None), \
                mock.patch.object(scenario.py_compile, 'compile', wraps=original_compile) as compile_source:
            try:
                first, second = self.tmp / 'first', self.tmp / 'second'
                scenario.clone_fixture_core(source, first)
                compiled = compile_source.call_count
                scenario.clone_fixture_core(source, second)
                if sys.platform != 'darwin':
                    self.assertEqual(compiled, 0)
                    return
                self.assertEqual(compiled, len(list(package.glob('*.py'))))
                self.assertEqual(compile_source.call_count, compiled)
                cached = next((second / 'lib/brain/__pycache__').glob('state.*.pyc'))
                self.assertEqual(int.from_bytes(cached.read_bytes()[4:8], 'little'), 3)
                code = ('import sys; sys.path.insert(0, sys.argv[1]); '
                        'from brain.state import current; '
                        'print(current()); print(current.__code__.co_filename)')
                def read(core):
                    result = subprocess.run([sys.executable, '-B', '-X', 'pycache_prefix=', '-c', code, str(core / 'lib')],
                                            env=self.env, capture_output=True, text=True, check=True)
                    return result.stdout.splitlines()
                self.assertEqual(read(first), ['first', str(first / 'lib/brain/state.py')])
                changed = second / 'lib/brain/state.py'
                stamp = changed.stat()
                changed.write_text('def current(): return "other"\n')
                os.utime(changed, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
                self.assertEqual(changed.stat().st_size, stamp.st_size)
                self.assertEqual(read(second), ['other', str(changed)])
                self.assertEqual(read(first)[0], 'first')
                changed.unlink()
                missing = subprocess.run([sys.executable, '-B', '-X', 'pycache_prefix=', '-c', code, str(second / 'lib')],
                                         env=self.env, capture_output=True, text=True)
                self.assertNotEqual(missing.returncode, 0)
                self.assertIn('ModuleNotFoundError', missing.stderr)
            finally:
                if scenario._core_template is not None:
                    scenario._core_template.cleanup()

    def test_failed_template_compilation_is_not_published(self):
        import scenario
        source, package = self.fixture_source()
        candidates = []
        def broken_compile(path, **options):
            candidates.append(Path(path).parents[3])
            raise RuntimeError('injected compilation error')
        with mock.patch.object(scenario, 'CORE', source), \
                mock.patch.object(scenario, '_core_template', None), \
                mock.patch.object(sys, 'platform', 'darwin'), \
                mock.patch.object(scenario.py_compile, 'compile', broken_compile):
            with self.assertRaisesRegex(RuntimeError, 'injected compilation error'):
                scenario.clone_fixture_core(source, self.tmp / 'failed')
            self.assertIsNone(scenario._core_template)
            self.assertTrue(candidates)
            self.assertFalse(any(path.exists() for path in candidates))

    def fixture_source(self):
        core = self.tmp / 'source-core'
        (core / 'bin').mkdir(parents=True)
        package = core / 'lib/brain'
        package.mkdir(parents=True)
        (core / 'bin/brain').write_text('# modular fixture\n')
        (package / '__init__.py').write_text('package_state = []\n')
        (package / 'jobs.py').write_text('def job_started(): return None\n')
        return core, package

    def load_fixture_source(self, core):
        loader = core_loader.SourceFileLoader('compiled_fixture', str(core / 'bin/brain'))
        module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        try:
            loader.exec_module(module)
        finally:
            package = getattr(loader, 'package_name', None)
            if package is not None:
                self.addCleanup(lambda: [sys.modules.pop(key, None) for key in list(sys.modules)
                                         if key == package or key.startswith(package + '.')])
        return module


    def test_deferred_command_uses_real_module_and_current_patch(self):
        loader = core_loader.SourceFileLoader('deferred_cli_fixture', str(CORE / 'bin/brain'))
        module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        try:
            loader.exec_module(module)
            package = loader.package_name
            cli = sys.modules[package + '.cli']
            owner = sys.modules[package + '.diagnostics']
            command = cli._Command('diagnostics', 'cmd_diagnose')
            self.assertIs(command.resolve(), owner.cmd_diagnose)
            args = argparse.Namespace()
            with mock.patch.object(owner, 'cmd_diagnose', return_value='patched') as execute:
                self.assertIs(command.resolve(), execute)
                self.assertEqual(command(args), 'patched')
                execute.assert_called_once_with(args)
            self.assertIs(command.resolve(), owner.cmd_diagnose)
            self.assertIs(sys.modules[package + '.diagnostics'], owner)
        finally:
            package = getattr(loader, 'package_name', None)
            if package is not None:
                for name in list(sys.modules):
                    if name == package or name.startswith(package + '.'):
                        del sys.modules[name]

    def test_compiled_source_reuses_code_but_executes_fresh_module_state(self):
        core, package = self.fixture_source()
        (package / 'state.py').write_text(
            'import os\nloaded_env = os.environ.get("BRAIN_FIXTURE_SENTINEL")\n'
            'items = []\ndef remember(value):\n items.append(value)\n return items\n')
        before = list(sys.meta_path)
        with mock.patch.dict(os.environ, {'BRAIN_FIXTURE_SENTINEL': 'first'}):
            first = self.load_fixture_source(core)
        self.assertEqual(first.remember('only first'), ['only first'])
        with mock.patch.dict(os.environ, {'BRAIN_FIXTURE_SENTINEL': 'second'}):
            second = self.load_fixture_source(core)
        self.assertIs(first.remember.__code__, second.remember.__code__)
        self.assertIsNot(first._owners['items'], second._owners['items'])
        self.assertEqual(first.loaded_env, 'first')
        self.assertEqual(second.loaded_env, 'second')
        self.assertEqual(second.items, [])
        self.assertEqual(second.remember('only second'), ['only second'])
        self.assertEqual(first.items, ['only first'])
        self.assertEqual(second.remember.__code__.co_filename, str(package / 'state.py'))
        self.assertEqual(sys.meta_path, before)
        self.assertEqual(list(core.rglob('__pycache__')), [])

    def test_compiled_source_observes_same_metadata_edits_and_restores_failed_imports(self):
        core, package = self.fixture_source()
        source = package / 'state.py'
        source.write_text('value = "first"\ndef current(): return value\n')
        first = self.load_fixture_source(core)
        stamp = source.stat()
        source.write_text('value = "other"\ndef current(): return value\n')
        os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        self.assertEqual(source.stat().st_size, stamp.st_size)
        self.assertEqual(source.stat().st_mtime_ns, stamp.st_mtime_ns)
        second = self.load_fixture_source(core)
        self.assertEqual(first.current(), 'first')
        self.assertEqual(second.current(), 'other')
        before = list(sys.meta_path)
        source.write_text('value = !!!\n')
        with self.assertRaises(SyntaxError):
            self.load_fixture_source(core)
        self.assertEqual(sys.meta_path, before)
        source.write_text('value = "fixed"\ndef current(): return value\n')
        self.assertEqual(self.load_fixture_source(core).current(), 'fixed')
        source.unlink()
        loader = core_loader._FixtureSourceLoader('missing', str(source))
        with self.assertRaises(FileNotFoundError):
            loader.get_code('missing')
        self.assertEqual(list(core.rglob('__pycache__')), [])

    def test_single_file_fixture_keeps_ordinary_loader(self):
        source = self.tmp / 'legacy.py'
        source.write_text('legacy_state = []\n')
        loader = core_loader.SourceFileLoader('legacy_fixture', str(source))
        module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(module)
        self.assertEqual(module.legacy_state, [])
        self.assertFalse(hasattr(loader, 'package_name'))

    def test_atomic_template_and_owned_module_restoration(self):
        import scenario
        import types

        failures = []
        source = self.tmp / 'source'
        source.mkdir()
        (source / 'complete').write_text('complete source\n')
        partials = []
        real_copytree = shutil.copytree

        def interrupted_copy(src, dst, **kwargs):
            partials.append(Path(dst).parent)
            Path(dst).mkdir()
            (Path(dst) / 'partial').write_text('interrupted\n')
            raise OSError('injected partial copy')

        with mock.patch.object(scenario, 'CORE', source), \
                mock.patch.object(scenario, '_core_template', None), \
                mock.patch.object(sys, 'platform', 'darwin'):
            try:
                with mock.patch.object(scenario.shutil, 'copytree', interrupted_copy):
                    with self.assertRaisesRegex(OSError, 'injected partial copy'):
                        scenario.clone_fixture_core(source, self.tmp / 'failed')
                if scenario._core_template is not None:
                    failures.append('failed template was published')
                if any(path.exists() for path in partials):
                    failures.append('partial template was not discarded')
                destination = self.tmp / 'recovered'

                def clone_template(argv, **options):
                    template = Path(scenario._core_template.name) / 'core'
                    self.assertEqual(argv, ['/bin/cp', '-cR', str(template), str(destination)])
                    self.assertEqual(options, {'check': True, 'capture_output': True, 'text': True})
                    real_copytree(template, destination)
                    return subprocess.CompletedProcess(argv, 0, stdout='', stderr='')

                with mock.patch.object(scenario.subprocess, 'run', side_effect=clone_template) as clone:
                    scenario.clone_fixture_core(source, destination)
                clone.assert_called_once_with(
                    ['/bin/cp', '-cR', str(Path(scenario._core_template.name) / 'core'), str(destination)],
                    check=True, capture_output=True, text=True)
                if sorted(p.name for p in destination.iterdir()) != ['complete']:
                    failures.append('next copy reused the partial template')
                elif (destination / 'complete').read_text() != 'complete source\n':
                    failures.append('next copy changed source contents')
            finally:
                if scenario._core_template is not None:
                    scenario._core_template.cleanup()

        for outcome in ('success', 'command-error', 'loader-error'):
            environment = dict(os.environ)
            argv, argv_values = sys.argv, list(sys.argv)
            cwd, bytecode = Path.cwd(), sys.dont_write_bytecode
            wrapper = types.ModuleType('fixture_setup')
            wrapper.state = object()
            collision = types.ModuleType('_brain_fixture_720001')
            collision.state = object()
            foreign = types.ModuleType('_brain_fixture_foreign_during_call')
            preserved = {'fixture_setup': wrapper, '_brain_fixture_720001': collision}
            original_exec = core_loader.SourceFileLoader.exec_module

            def execute(loader, module):
                sys.modules[foreign.__name__] = foreign
                if outcome == 'loader-error':
                    real_import = core_loader.importlib.import_module

                    def broken_import(name, package=None):
                        real_import(name, package)
                        raise RuntimeError('injected loader error')

                    with mock.patch.object(core_loader.importlib, 'import_module', broken_import):
                        original_exec(loader, module)
                else:
                    original_exec(loader, module)

            with mock.patch.dict(sys.modules, preserved), \
                    mock.patch.object(core_loader, '_serial', iter([720001, 720002])), \
                    mock.patch.object(core_loader.SourceFileLoader, 'exec_module', execute):
                before = dict(sys.modules)
                if outcome == 'loader-error':
                    with self.assertRaisesRegex(RuntimeError, 'injected loader error'):
                        self.fixture_cli(['--help'], self.tmp, CORE / 'bin/brain')
                elif outcome == 'command-error':
                    with self.assertRaisesRegex(AssertionError, 'fixture command failed'):
                        self.fixture_cli(['not-a-command'], self.tmp, CORE / 'bin/brain')
                else:
                    self.assertIn('usage:', self.fixture_cli(['--help'], self.tmp, CORE / 'bin/brain'))
                for name, module in preserved.items():
                    if sys.modules.get(name) is not module:
                        failures.append(outcome + ': replaced preexisting ' + name)
                if sys.modules.get(foreign.__name__) is not foreign:
                    failures.append(outcome + ': deleted unrelated module')
                leaked = [name for name in sys.modules if name.startswith('_brain_fixture_')
                          and name not in before and name != foreign.__name__]
                if leaked:
                    failures.append(outcome + ': leaked owned modules ' + repr(leaked))
                self.assertEqual(dict(os.environ), environment)
                self.assertIs(sys.argv, argv)
                self.assertEqual(sys.argv, argv_values)
                self.assertEqual(Path.cwd(), cwd)
                self.assertIs(sys.dont_write_bytecode, bytecode)
        self.assertEqual(failures, [], '\n'.join(failures))

    def test_in_process_initialization_restores_context_and_clones_independent_core(self):
        environment, argv, cwd = dict(os.environ), list(sys.argv), Path.cwd()
        self.fixture_init_in_process = True
        first = self.init('first')
        second = self.init('second', operator='other')
        self.assertEqual(dict(os.environ), environment)
        self.assertEqual(sys.argv, argv)
        self.assertEqual(Path.cwd(), cwd)
        for root, operator in ((first, 'first'), (second, 'other')):
            self.assertIn('principals: ' + operator, (root / 'instance.md').read_text())
            self.assertTrue((root / 'CLAUDE.md').is_symlink())
            self.assertEqual(self.git('status', '--porcelain', cwd=root), '')
            self.assertIn('root:', self.b(root, 'tree'))
            self.assertTrue((root / '.brain/events.jsonl').is_file())
        source = CORE / 'bin/brain'
        left, right = first / 'core/bin/brain', second / 'core/bin/brain'
        self.assertEqual(left.read_bytes(), source.read_bytes())
        self.assertEqual(right.read_bytes(), source.read_bytes())
        self.assertNotEqual((left.stat().st_dev, left.stat().st_ino),
                            (right.stat().st_dev, right.stat().st_ino))
        left.write_text('changed only in the first fixture\n')
        self.assertEqual(right.read_bytes(), source.read_bytes())
        self.assertEqual(left.stat().st_nlink, 1)
        self.assertEqual(right.stat().st_nlink, 1)
        self.assertFalse((first / 'core/.git').exists())

    def test_setup_failures_restore_context_and_evidence_keeps_real_subprocess(self):
        environment, argv, cwd = dict(os.environ), list(sys.argv), Path.cwd()
        with self.assertRaisesRegex(AssertionError, 'fixture command failed'):
            self.fixture_cli(['not-a-command'], cwd=self.tmp, brain=CORE / 'bin/brain')
        self.assertEqual(dict(os.environ), environment)
        self.assertEqual(sys.argv, argv)
        self.assertEqual(Path.cwd(), cwd)
        with mock.patch.object(self, '_evidence', object(), create=True), \
                mock.patch.object(self, 'run_', return_value='real child') as child:
            self.assertEqual(self.fixture_cli(['now'], cwd=self.tmp, brain=CORE / 'bin/brain'), 'real child')
            child.assert_called_once_with(['now'], cwd=self.tmp, brain=CORE / 'bin/brain')


class SettlementDocsHelp(unittest.TestCase):
    def test_settlement_guides_require_export_for_each_recording_command(self):
        for name in ('roles/coordinator.md', 'skills/home-job/SKILL.md',
                     'adapters/hosts/t3.md', 'README.md', 'CHANGES.md'):
            text = (CORE / name).read_text()
            commands = re.findall(r'`(brain jobs [^`]*--settled[^`]*)`', text)
            self.assertTrue(commands, name)
            for command in commands:
                with self.subTest(document=name, command=command):
                    self.assertIn('--attestation-file', command)
                    if name != 'CHANGES.md':  # Historical release entries keep their original contract.
                        self.assertIn('--thread', command)
        for name in ('roles/coordinator.md', 'skills/home-job/SKILL.md'):
            text = (CORE / name).read_text()
            self.assertIn('version 1', text)
            self.assertIn('five minutes', ' '.join(text.split()))
            self.assertIn('--job-key <key>', text)

    def test_jobs_help_describes_settlement_export_and_selection(self):
        result = subprocess.run([sys.executable, str(CORE / 'bin/brain'), 'jobs', '--help'],
                                cwd=CORE, env={**os.environ, 'BRAIN_NO_REEXEC': '1'}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        help_text = ' '.join(result.stdout.split())
        for clause in ('--settled', '--all-closed', '--attestation-file',
                       'fresh version 1 coordinator export',
                       'exhaustive per-job/thread/project coverage', '--job-key',
                       '--thread', 'repeat for every confirmed settled thread'):
            self.assertIn(clause, help_text)

    def test_t3_settlement_guide_has_explicit_target_and_self_recovery(self):
        text = (CORE / 'adapters/hosts/t3.md').read_text()
        for clause in ('Never omit', '`threadIds` field', '`settlesWhenTurnEnds`',
                       'action="unsettle" threadId="<your exact ID>"', 'settled=true',
                       'brain settle <node> --job <key>', 'one `--thread <id>`'):
            self.assertIn(clause, text)


class EntryPoints(Scenario):
    def test_worktree_rehearse_refuses_switch_and_preserves_explicit_core_selection(self):
        loader = core_loader.SourceFileLoader('reexec_fixture', str(CORE / 'bin/brain'))
        module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(module)
        package = loader.package_name
        self.addCleanup(lambda: [sys.modules.pop(k) for k in list(sys.modules)
                                 if k == package or k.startswith(package + '.')])
        cli = sys.modules[package + '.cli']
        common = sys.modules[package + '.common']
        source = self.tmp / 'worktree'
        source.mkdir()
        (source / '.git').write_text('gitdir: fixture\n')
        root = self.tmp / 'instance'
        other = root / 'core/bin/brain'
        other.parent.mkdir(parents=True)
        other.touch()
        entry = source / 'bin/brain'
        env = {k: v for k, v in self.env.items() if k != 'BRAIN_NO_REEXEC'}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(common, 'CORE', source), \
                mock.patch.object(common, 'ENTRY_POINT', entry), \
                mock.patch.object(cli._m_nodes, 'try_find_root', return_value=root), \
                mock.patch.object(cli._m_instance, 'brains_here', return_value=[('fixture', root)]):
            for prefix in ([], ['-a'], ['--all'], ['-aa'], ['-b', 'fixture'], ['--brain=fixture']):
                with self.subTest(prefix=prefix), \
                        mock.patch.object(sys, 'argv', [str(entry), *prefix, 'rehearse', '--help']), \
                        mock.patch.object(cli.os, 'execv') as execute:
                    cli.take_brain_flag()
                    with self.assertRaises(SystemExit) as refused:
                        cli.reexec_into_instance_core()
                    message = str(refused.exception)
                    self.assertIn('refusing instance-core re-execution', message)
                    self.assertIn(str(source), message)
                    self.assertIn(str(other.parent.parent), message)
                    self.assertIn('BRAIN_NO_REEXEC=1', message)
                    execute.assert_not_called()
                    self.assertNotIn('BRAIN_NO_REEXEC', os.environ)
            with mock.patch.object(sys, 'argv', [str(entry), 'rehearse', '--help']), \
                    mock.patch.object(cli.os, 'execv') as execute:
                with mock.patch.dict(os.environ, {'BRAIN_NO_REEXEC': '1'}):
                    cli.reexec_into_instance_core()
                execute.assert_not_called()
                with mock.patch.object(common, 'ENTRY_POINT', other):
                    cli.reexec_into_instance_core()
                execute.assert_not_called()
            with mock.patch.object(sys, 'argv', [str(entry), 'ui', '--help']), \
                    mock.patch.object(cli.os, 'execv') as execute:
                cli.reexec_into_instance_core()
                execute.assert_not_called()
            with mock.patch.object(sys, 'argv', [str(entry), 'now']), \
                    mock.patch.object(cli.os, 'execv') as execute:
                cli.reexec_into_instance_core()
                execute.assert_called_once_with(sys.executable, [sys.executable, str(other), 'now'])

    def test_global_pointer_is_added_once_and_removable(self):
        r = self.init("alice")
        claude_md = self.tmp / ".claude" / "CLAUDE.md"
        claude_md.parent.mkdir()
        claude_md.write_text("# My own rules\n\nKeep me.\n")
        out = self.b(r, "link", "--global")
        self.assertIn("claude:", out)
        self.assertIn("codex:", out)
        for f in (claude_md, self.tmp / ".codex" / "AGENTS.md"):
            text = f.read_text()
            self.assertIn(f"alice's Brain is at `~/alice`", text)
            self.assertIn("only a pointer", text)
        self.assertIn("already up to date", self.b(r, "link", "--global"))
        self.assertEqual(claude_md.read_text().count("brain:begin"), 1)
        self.assertTrue(claude_md.read_text().startswith("# My own rules\n\nKeep me.\n"))
        self.b(r, "link", "--remove-global")
        self.assertEqual(claude_md.read_text(), "# My own rules\n\nKeep me.\n")
        self.env["CODEX_HOME"] = str(self.tmp / "ch")
        self.b(r, "link", "--global")
        self.assertTrue((self.tmp / "ch" / "AGENTS.md").exists())


    def test_launcher_leaves_core_clean_without_bytecode_environment(self):
        core = self.tmp / "core-copy"
        # Model an installed core: UI development dependencies and build caches do not ship.
        shutil.copytree(CORE, core, ignore=shutil.ignore_patterns(
            ".git", "__pycache__", ".DS_Store", "node_modules", "*.tsbuildinfo"))
        self.git("init", "-q", "-b", "main", cwd=core)
        self.git("add", "-A", cwd=core)
        self.git("commit", "-qm", "fixture core", cwd=core)
        env = dict(self.env)
        env.pop("PYTHONPYCACHEPREFIX", None)
        env.pop("PYTHONDONTWRITEBYTECODE", None)
        # Also force beside-code caches: some system Pythons redirect them even
        # without either environment variable, hiding writes in an installed core.
        beside_code = ("import runpy, sys; sys.pycache_prefix = None; "
                       "sys.argv = sys.argv[1:]; runpy.run_path(sys.argv[0], run_name='__main__')")
        for prefix in ([], ["-c", beside_code]):
            with self.subTest(prefix=prefix):
                result = subprocess.run([sys.executable, *prefix, str(core / "bin/brain"), "--help"],
                                        cwd=core, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.git("status", "--porcelain", "--untracked-files=all", "--ignored", cwd=core), "")
                self.assertEqual(list((core / "lib").rglob("__pycache__")), [])

    def test_argument_parsing_compiles_only_the_light_core(self):
        # Every hook, wake and hand-back pays startup; sources compile without bytecode.
        probe = ("import json, runpy, sys\n"
                 "sys.argv = sys.argv[1:]\n"
                 "try:\n"
                 "    runpy.run_path(sys.argv[0], run_name='__main__')\n"
                 "except SystemExit:\n"
                 "    pass\n"
                 "sys.stderr.write('\\n' + json.dumps(sorted(m for m in sys.modules if m.startswith('brain.'))))\n")
        heavy = {'brain.' + m for m in ('status', 'jobs', 'guard', 'hosts', 'machine', 'ui', 'rehearse',
                                         'verify', 'history', 'knowledge', 'events', 'check', 'briefs')}
        r = self.init('alice')
        for args, handler in ((['--help'], None), (['handback', '--help'], None), (['task', '--help'], None),
                              (['now'], 'brain.status')):
            with self.subTest(args=args):
                result = subprocess.run([sys.executable, '-B', '-c', probe, str(CORE / 'bin/brain'), *args],
                                        cwd=r, env=self.env, capture_output=True, text=True)
                loaded = set(json.loads(result.stderr.splitlines()[-1]))
                self.assertIn('brain.cli', loaded)
                self.assertEqual(loaded & heavy, {handler} - {None}, result.stderr)
                if handler:
                    self.assertRegex(result.stdout, r'^\d{4}-\d\d-\d\d \d\d:\d\d ')

    def test_lazy_module_loads_on_first_use_and_forwards_patches(self):
        package = 'lazy_fixture_' + self.tmp.name.replace('-', '_')
        folder = self.tmp / 'lazy-lib' / package
        folder.mkdir(parents=True)
        for name in ('__init__.py', 'common.py', 'command_reads.py'):
            shutil.copyfile(CORE / 'lib/brain' / name, folder / name)
        (folder / 'heavy.py').write_text('value = 1\n')
        (folder / 'user.py').write_text("from . import common as _m_common\n"
                                        "_m_heavy = _m_common.LazyModule(__package__, 'heavy')\n"
                                        "def read():\n    return _m_heavy.value\n")
        sys.path.insert(0, str(folder.parent))
        self.addCleanup(sys.path.remove, str(folder.parent))
        self.addCleanup(lambda: [sys.modules.pop(k) for k in list(sys.modules)
                                 if k == package or k.startswith(package + '.')])
        user = importlib.import_module(package + '.user')
        self.assertIn('lazy module', repr(user._m_heavy))
        self.assertNotIn(package + '.heavy', sys.modules)
        self.assertEqual(user.read(), 1)
        heavy = sys.modules[package + '.heavy']
        self.assertIs(user._m_heavy.__dict__, heavy.__dict__)
        with mock.patch.object(user._m_heavy, 'value', 2):
            self.assertEqual((heavy.value, user.read()), (2, 2))
        self.assertEqual(heavy.value, 1)
        user._m_heavy.extra = 3
        self.assertEqual(heavy.extra, 3)
        del user._m_heavy.extra
        self.assertFalse(hasattr(heavy, 'extra'))

    def test_policy_and_complete_rule_snapshot_cli_boundaries(self):
        r = self.init('alice')
        self.b(r, 'new', 'demo', '--title', 'Demo')
        node = r / 'nodes/demo'
        (node / 'brief.md').write_text('## Limits\n- Never open a pull request.\n')
        policy = json.loads(self.b(r, 'policy', 'demo', '--json'))
        reviewed_at = '2026-01-01 00:00 +00:00'  # fixed synthetic review evidence
        policy.update(reviewer='alice', reviewed_at=reviewed_at)
        for rule in policy['rules']:
            rule['actions'] = {'push': 'deny', 'pr': 'deny'}
        (node / 'publication-policy.json').write_text(json.dumps(policy))
        paths = [node / 'node.md', r / 'registry.md', r / '.brain/local.json']
        before = [p.read_bytes() for p in paths]
        refused = self.b(r, 'set', 'demo', 'coordinator=demo-thread', 'merge=pr', ok=False)
        self.assertIn('Never open a pull request.', refused)
        self.assertIn('brief.md', refused)
        self.assertEqual(before, [p.read_bytes() for p in paths])
        self.b(r, 'set', 'demo', 'merge=local')
        (node / 'brief.md').write_text('## Limits\n- Ask about each outward action.\n')
        unknown_before = [p.read_bytes() for p in paths]
        self.assertIn('coordinator', self.b(r, 'set', 'demo', 'merge=pr', ok=False))
        self.assertEqual(unknown_before, [p.read_bytes() for p in paths])
        snapshot = {'version': 1, 'node': 'demo', 'scope': ['alice'], 'importer': 'worker-1',
                    'importer_runtime': 'codex', 'provenance': 'example@abc',
                    'rules': [{'id': 'examples', 'text': 'Use fabricated examples.', 'source': 'example@abc:1'}]}
        source = node / 'sources/import-proposals.md'
        source.write_text(json.dumps(snapshot))
        sheet = json.loads(self.b(r, 'proposals', 'demo', '--json'))
        digest = sheet[0]['rules_snapshot']['digest']
        due = json.loads(self.b(r, 'due', 'demo', '--json'))
        activation = [i for i in due if i['kind'] == 'rule-activation-review']
        self.assertEqual(len(activation), 1)
        manifest = {**snapshot, 'snapshot_hash': digest, 'reviewer': 'alice',
                    'reviewer_runtime': 'claude', 'reviewed_at': reviewed_at}
        review = r / 'review.json'
        review.write_text(json.dumps(manifest))
        original = source.read_bytes()
        out = self.b(r, 'proposals', 'demo', '--approve-all', '--review-manifest', str(review))
        self.assertIn('supplied evidence', out)
        self.assertIn('Use fabricated examples.', (node / 'AGENTS.md').read_text())
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(json.loads(self.b(r, 'proposals', 'demo', '--json')), [])
        self.assertNotIn('rule-activation-review', [i['kind'] for i in json.loads(self.b(r, 'due', 'demo', '--json'))])
        self.assertIn('recorded open job', self.b(r, 'land-check', 'demo', '--branch', 'absent', '--run', ok=False))


if __name__ == "__main__":
    unittest.main()


class GitReadLocks(Scenario):
    def setUp(self):
        super().setUp()
        self.m = core_loader.SourceFileLoader('git_locks_fixture', str(CORE / 'bin/brain')).load_module()
        self.common = self.m._owners['git']

    def test_status_and_diff_override_only_child_environment(self):
        result = subprocess.CompletedProcess([], 0, '', '')
        supplied = {'PATH': self.env['PATH'], 'GIT_OPTIONAL_LOCKS': '1', 'SAMPLE': 'retained'}
        queries = [('status', '--porcelain'), ('diff', '--name-only'),
                   ('-c', 'status.showUntrackedFiles=all', 'status', '--porcelain'),
                   ('--no-optional-locks', '-c', 'diff.ignoreSubmodules=none', 'diff', '--quiet'),
                   ('-c', 'core.quotepath=false', '--no-optional-locks', 'status', '-uno')]
        with mock.patch.dict(os.environ, {'GIT_OPTIONAL_LOCKS': '1'}), \
                mock.patch.object(subprocess, 'run', return_value=result) as run:
            for query in queries:
                for env in (None, supplied):
                    with self.subTest(query=query, custom_env=env is not None):
                        self.common.git(*query, cwd=self.tmp, env=env)
                        child = run.call_args.kwargs['env']
                        self.assertEqual(child['GIT_OPTIONAL_LOCKS'], '0')
                        self.assertEqual(child['PATH'], (os.environ if env is None else supplied)['PATH'])
                        self.assertIsNot(child, env)
                        self.assertEqual(os.environ['GIT_OPTIONAL_LOCKS'], '1')
                        self.assertEqual(supplied['GIT_OPTIONAL_LOCKS'], '1')
            for query in [('add', '-A'), ('commit', '-qm', 'sample'), ('update-index', '--refresh'),
                          ('config', 'sample.value', 'status'), ('show', 'HEAD:status')]:
                for env in (None, supplied):
                    self.common.git(*query, env=env)
                    self.assertIs(run.call_args.kwargs['env'], env)

    def test_reads_keep_stale_index_and_return_nasty_paths_with_a_held_lock(self):
        names = ['space name.txt', 'café.txt', 'line\nbreak.txt']
        root = self.repo(self.tmp / 'repo with café\nand spaces', files={name: 'same\n' for name in names})
        # Changed inode/mtime with identical bytes forces an ordinary status to
        # refresh stat data. A changed tracked file and new files remain visible.
        for name in names:
            path = root / name
            original = path.stat()
            os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns + 2_000_000_000))
        (root / names[0]).write_text('different\n')
        untracked = ['new space.txt', '雪.txt', 'new\nline.txt']
        for name in untracked:
            (root / name).write_text('untracked\n')
        index = root / '.git/index'
        def identity():
            value = index.stat()
            return value.st_ino, value.st_mtime_ns, index.read_bytes()
        before = identity()
        expected = subprocess.run(['git', '--no-optional-locks', '-C', str(root), 'status',
                                   '--porcelain=v1', '-z', '-uall'], env=self.env,
                                  capture_output=True, text=True, check=True).stdout
        lock = root / '.git/index.lock'
        lock.write_bytes(b'foreign lock must survive')
        with mock.patch.dict(os.environ, self.env, clear=True):
            for prefix in ((), ('-c', 'core.quotepath=false'), ('--no-optional-locks',)):
                status = self.common.git(*prefix, 'status', '--porcelain=v1', '-z', '-uall', cwd=root, check=True)
                self.assertEqual(status.stdout, expected)
                self.assertEqual(identity(), before)
                diff = self.common.git(*prefix, 'diff', '--name-only', '-z', cwd=root, check=True)
                self.assertEqual(diff.stdout, names[0] + '\0')
                self.assertEqual(identity(), before)
            tracked = self.common.git('status', '--porcelain=v1', '-z', '-uno', cwd=root, check=True).stdout
        for name in untracked:
            self.assertIn('?? ' + name + '\0', expected)
            self.assertNotIn(name + '\0', tracked)
        self.assertIn(' M ' + names[0] + '\0', tracked)
        self.assertEqual(lock.read_bytes(), b'foreign lock must survive')
        lock.unlink()
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.common.git('status', '--porcelain=v1', '-z', '-uall', cwd=root, check=True)
            self.assertEqual(identity(), before)
            self.common.git('add', '--', names[0], cwd=root, check=True)
        self.assertNotEqual(index.read_bytes(), before[2])
        self.assertFalse(lock.exists())

    def test_core_revision_ignores_untracked_files_but_worktree_activity_keeps_them(self):
        root = self.repo(self.tmp / 'code')
        (root / 'new worker file.txt').write_text('work in progress\n')
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(self.common, 'CORE', root):
            self.assertNotIn('uncommitted changes', self.m.core_revision())
            state = self.m.ui_worktree(None, root, 'main', {'path': str(root)})
            self.assertEqual(state['dirty'], 1)
            self.assertIsNotNone(state['wrote'])
            (root / 'README.md').write_text('tracked work\n')
            self.assertIn('uncommitted changes', self.m.core_revision())

    def test_retention_diagnostic_diff_keeps_the_source_index_unchanged(self):
        root = self.repo(self.tmp / 'diagnostic source')
        names = ('bin/brain', 'lib/brain/rehearse.py', 'lib/brain/rehearse_transactions.py',
                 'lib/brain/events.py', 'lib/brain/instance.py', 'tests/scenario.py',
                 'tests/test_rehearse.py', 'tests/nightly_rehearse.py')
        for name in names:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('synthetic diagnostic source\n')
        self.git('add', '-A', cwd=root)
        self.git('commit', '-qm', 'diagnostic fixture', cwd=root)
        self.git('update-index', '--split-index', cwd=root)
        backing = list((root / '.git').glob('sharedindex.*'))
        self.assertTrue(backing)
        for shared in backing:
            os.utime(shared, ns=(1_000_000_000, 1_000_000_000))
        original = (root / 'README.md').stat()
        os.utime(root / 'README.md', ns=(original.st_atime_ns, original.st_mtime_ns + 2_000_000_000))
        index = root / '.git/index'
        before = {p: (p.stat().st_ino, p.stat().st_mtime_ns, p.read_bytes()) for p in [index, *backing]}
        owned, evidence = self.tmp / 'owned', self.tmp / 'evidence'
        owned.mkdir()
        evidence.mkdir(mode=0o700)
        module = self.m._owners['NativeRetention']
        with mock.patch.dict(os.environ, {**self.env, 'BRAIN_REHEARSAL_EVIDENCE_DIR': str(evidence)}, clear=True), \
                mock.patch.object(module, '__file__', str(root / 'lib/brain/rehearse.py')):
            retention = module.NativeRetention(owned, 'fixture')
        self.assertEqual(retention.packet['retention_errors'], [])
        self.assertEqual(retention.packet['source_binding']['head'], self.git('rev-parse', 'HEAD', cwd=root).strip())
        self.assertTrue(list(evidence.glob('native-retention-*.json')))
        self.assertEqual({p: (p.stat().st_ino, p.stat().st_mtime_ns, p.read_bytes()) for p in [index, *backing]}, before)

    def test_diff_against_head_preserves_the_source_index(self):
        root = self.repo(self.tmp / 'diff café\nwith spaces')
        index = root / '.git/index'
        path = root / 'README.md'
        stamp = path.stat()
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 2_000_000_000))
        before = index.stat().st_ino, index.stat().st_mtime_ns, index.read_bytes()
        with mock.patch.dict(os.environ, self.env, clear=True):
            for prefix in ((), ('--no-optional-locks',), ('-c', 'diff.autoRefreshIndex=true')):
                with self.subTest(prefix=prefix):
                    result = self.common.git(*prefix, 'diff', '--binary', 'HEAD', cwd=root, check=True)
                    self.assertEqual(result.stdout, '')
                    self.assertEqual((index.stat().st_ino, index.stat().st_mtime_ns, index.read_bytes()), before)

    def test_diff_keeps_raw_output_exit_codes_and_custom_split_indexes(self):
        root = self.repo(self.tmp / 'split source', files={'same.txt': 'same\n', 'changed.txt': 'before\n'})
        self.git('update-index', '--split-index', cwd=root)
        custom = root / '.git/custom index\n雪'
        shutil.copyfile(root / '.git/index', custom)
        env = {**self.env, 'GIT_INDEX_FILE': str(custom), 'GIT_OPTIONAL_LOCKS': '1'}
        (root / 'changed.txt').write_text('after\n')
        stamp = (root / 'same.txt').stat()
        os.utime(root / 'same.txt', ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 2_000_000_000))
        before = custom.stat().st_ino, custom.stat().st_mtime_ns, custom.read_bytes()
        foreign = Path(str(custom) + '.lock')
        foreign.write_bytes(b'foreign index lock')
        modes = [('--name-only', '-z'), ('--name-status', '-z'), ('--raw', '-z'),
                 ('--binary', 'HEAD'), ('--numstat', '-z'), ('--quiet',),
                 ('--exit-code', '--name-only'), ('--name-only', 'missing-ref')]
        with mock.patch.dict(os.environ, self.env, clear=True):
            for mode in modes:
                with self.subTest(mode=mode):
                    expected = subprocess.run(['git', '--no-optional-locks', '-C', str(root), 'diff', *mode],
                                              env=env, capture_output=True, text=True, errors='replace')
                    actual = self.common.git('diff', *mode, cwd=root, env=env)
                    self.assertEqual((actual.returncode, actual.stdout, actual.stderr),
                                     (expected.returncode, expected.stdout, expected.stderr))
                    self.assertEqual((custom.stat().st_ino, custom.stat().st_mtime_ns, custom.read_bytes()), before)
        self.assertEqual(env['GIT_INDEX_FILE'], str(custom))
        self.assertEqual(env['GIT_OPTIONAL_LOCKS'], '1')
        self.assertEqual(foreign.read_bytes(), b'foreign index lock')

    def test_private_diff_index_keeps_racy_clean_detection(self):
        root = self.repo(self.tmp / 'racy source')
        self.git('config', 'core.trustctime', 'false', cwd=root)
        path = root / 'README.md'
        instant = 1_600_000_000_000_000_000
        os.utime(path, ns=(instant, instant))
        self.git('add', 'README.md', cwd=root)
        index = root / '.git/index'
        os.utime(index, ns=(instant, instant))
        path.write_text('other\n')  # same length as the fixture's hello newline
        os.utime(path, ns=(instant, instant))
        (root / '.git/index.lock').write_bytes(b'foreign')
        expected = subprocess.run(['git', '--no-optional-locks', '-C', str(root), 'diff', '--name-only'],
                                  env=self.env, capture_output=True, text=True, check=True)
        with mock.patch.dict(os.environ, self.env, clear=True):
            actual = self.common.git('diff', '--name-only', cwd=root, check=True)
        self.assertEqual(expected.stdout, 'README.md\n')
        self.assertEqual(actual.stdout, expected.stdout)

    def test_split_index_reads_keep_backing_metadata_and_outside_indexes(self):
        root = self.repo(self.tmp / 'split backing café\nsource')
        self.git('update-index', '--split-index', cwd=root)
        shared = list((root / '.git').glob('sharedindex.*'))
        self.assertTrue(shared)
        outside = self.tmp / 'outside index\n雪'
        shutil.copy2(root / '.git/index', outside)
        (root / 'README.md').write_text('tracked change\n')
        (root / 'new.txt').write_text('untracked change\n')
        for source in (root / '.git/index', outside):
            env = {**self.env, 'GIT_INDEX_FILE': str(source)}
            foreign = Path(str(source) + '.lock')
            foreign.write_bytes(b'foreign')
            for query in [('status', '--porcelain=v1', '-z', '-uall'), ('status', '--porcelain=v1', '-z', '-uno'),
                          ('diff', '--name-only', '-z'), ('diff', '--binary', 'HEAD')]:
                with self.subTest(index=source.name, query=query):
                    expected = subprocess.run(['git', '--no-optional-locks', '-C', str(root), *query],
                                              env=env, capture_output=True, text=True, check=True)
                    for backing in shared:
                        os.utime(backing, ns=(1_000_000_000, 1_000_000_000))
                    def metadata():
                        return {p: (p.stat().st_ino, p.stat().st_mtime_ns, p.read_bytes()) for p in [source, *shared]}
                    before = metadata()
                    with mock.patch.dict(os.environ, self.env, clear=True):
                        actual = self.common.git(*query, cwd=root, env=env, check=True)
                    self.assertEqual((actual.stdout, actual.stderr), (expected.stdout, expected.stderr))
                    self.assertEqual(metadata(), before)
            self.assertEqual(foreign.read_bytes(), b'foreign')
            foreign.unlink()

    def test_split_linked_worktree_keeps_common_refs_and_worktree_configuration(self):
        home = self.repo(self.tmp / 'common store')
        root = self.tmp / 'linked café\ncheckout'
        self.git('worktree', 'add', '-qb', 'worker', str(root), cwd=home)
        self.git('config', 'extensions.worktreeConfig', 'true', cwd=home)
        self.git('config', '--worktree', 'core.quotePath', 'false', cwd=root)
        self.git('update-index', '--split-index', cwd=root)
        own = Path(self.git('rev-parse', '--absolute-git-dir', cwd=root).removesuffix('\n'))
        backing = list(own.glob('sharedindex.*'))
        self.assertTrue(backing)
        index = own / 'index'
        lock = own / 'index.lock'
        lock.write_bytes(b'foreign')
        (root / 'README.md').write_text('tracked change\n')
        (root / '雪.txt').write_text('new change\n')
        for query in [('status', '--short', '--branch'), ('diff', '--name-only', 'HEAD')]:
            expected = subprocess.run(['git', '--no-optional-locks', '-C', str(root), *query],
                                      env=self.env, capture_output=True, text=True, check=True)
            for shared in backing:
                os.utime(shared, ns=(1_000_000_000, 1_000_000_000))
            before = {p: (p.stat().st_ino, p.stat().st_mtime_ns, p.read_bytes()) for p in [index, *backing]}
            with mock.patch.dict(os.environ, self.env, clear=True):
                actual = self.common.git(*query, cwd=root, check=True)
            self.assertEqual((actual.stdout, actual.stderr), (expected.stdout, expected.stderr))
            self.assertEqual({p: (p.stat().st_ino, p.stat().st_mtime_ns, p.read_bytes()) for p in [index, *backing]}, before)
        self.assertEqual(lock.read_bytes(), b'foreign')

    def test_split_index_reads_preserve_conditional_configuration(self):
        root = self.repo(self.tmp / 'conditional café source')
        self.git('update-index', '--split-index', cwd=root)
        own = root / '.git'
        include = own / 'conditional.inc'
        nested = own / 'nested.inc'
        patterns = self.tmp / 'ignore.patterns'
        patterns.write_text('ignored.tmp\n')
        include.write_text('[include]\n\tpath = ./nested.inc\n')
        nested.write_text('[core]\n\texcludesFile = ' + str(patterns) + '\n\tquotePath = false\n')
        (root / 'ignored.tmp').write_text('ignored work\n')
        (root / '雪.txt').write_text('visible work\n')
        (root / 'README.md').write_text('tracked work\n')
        cases = [('gitdir:' + str(own), False),
                 ('gitdir/i:' + ''.join(c.swapcase() if c.isascii() else c for c in str(own)), False),
                 ('gitdir:**/brain-diff-index-*/', True)]
        backing = list(own.glob('sharedindex.*'))
        for predicate, visible_ignored in cases:
            key = 'includeIf.' + predicate + '.path'
            self.git('config', key, './conditional.inc', cwd=root)
            try:
                for query in [('status', '--porcelain'), ('diff', '--name-only')]:
                    with self.subTest(predicate=predicate, query=query):
                        expected = subprocess.run(['git', '--no-optional-locks', '-C', str(root), *query],
                                                  env=self.env, capture_output=True, text=True, check=True)
                        for shared in backing:
                            os.utime(shared, ns=(1_000_000_000, 1_000_000_000))
                        paths = [own / 'index', *backing]
                        before = {p: (p.stat().st_ino, p.stat().st_mtime_ns, p.read_bytes()) for p in paths}
                        with mock.patch.dict(os.environ, self.env, clear=True):
                            actual = self.common.git(*query, cwd=root, check=True)
                        self.assertEqual((actual.stdout, actual.stderr), (expected.stdout, expected.stderr))
                        if query[0] == 'status':
                            self.assertEqual('ignored.tmp' in actual.stdout, visible_ignored)
                        self.assertEqual({p: (p.stat().st_ino, p.stat().st_mtime_ns, p.read_bytes()) for p in paths}, before)
            finally:
                self.git('config', '--unset', key, cwd=root)

    def test_split_private_index_preserves_sha256_and_caller_git_environment(self):
        root = self.tmp / 'sha256 source'
        root.mkdir()
        self.git('init', '-q', '-b', 'main', '--object-format=sha256', cwd=root)
        (root / 'tracked.txt').write_text('before\n')
        self.git('add', '-A', cwd=root)
        self.git('commit', '-qm', 'sha256 fixture', cwd=root)
        self.git('update-index', '--split-index', cwd=root)
        (root / 'tracked.txt').write_text('after!\n')
        (root / 'ignored.tmp').write_text('ignored work\n')
        patterns = self.tmp / 'caller.patterns'
        patterns.write_text('ignored.tmp\n')
        env = {**self.env, 'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'core.excludesFile',
               'GIT_CONFIG_VALUE_0': str(patterns)}
        original_env = env.copy()
        paths = [root / '.git/index', *(root / '.git').glob('sharedindex.*')]
        for query in [('status', '--porcelain'), ('diff', '--binary', 'HEAD')]:
            expected = subprocess.run(['git', '--no-optional-locks', '-C', str(root), *query],
                                      env=env, capture_output=True, text=True, check=True)
            for backing in paths[1:]:
                os.utime(backing, ns=(1_000_000_000, 1_000_000_000))
            before = {p: (p.stat().st_ino, p.stat().st_mtime_ns, p.read_bytes()) for p in paths}
            actual = self.common.git(*query, cwd=root, env=env, check=True)
            self.assertEqual((actual.stdout, actual.stderr), (expected.stdout, expected.stderr))
            self.assertEqual({p: (p.stat().st_ino, p.stat().st_mtime_ns, p.read_bytes()) for p in paths}, before)
        self.assertEqual(env, original_env)

    def test_flattened_split_index_keeps_racy_clean_detection(self):
        root = self.repo(self.tmp / 'split racy source')
        self.git('config', 'core.trustctime', 'false', cwd=root)
        path = root / 'README.md'
        instant = 1_600_000_000_000_000_000
        os.utime(path, ns=(instant, instant))
        self.git('add', 'README.md', cwd=root)
        self.git('update-index', '--split-index', cwd=root)
        indexes = [root / '.git/index', *(root / '.git').glob('sharedindex.*')]
        for index in indexes:
            os.utime(index, ns=(instant, instant))
        path.write_text('other\n')
        os.utime(path, ns=(instant, instant))
        (root / '.git/index.lock').write_bytes(b'foreign')
        expected = subprocess.run(['git', '--no-optional-locks', '-C', str(root), 'diff', '--name-only'],
                                  env=self.env, capture_output=True, text=True, check=True)
        for index in indexes:
            os.utime(index, ns=(instant, instant))
        before = {p: (p.stat().st_ino, p.stat().st_mtime_ns, p.read_bytes()) for p in indexes}
        with mock.patch.dict(os.environ, self.env, clear=True):
            actual = self.common.git('diff', '--name-only', cwd=root, check=True)
        self.assertEqual(expected.stdout, 'README.md\n')
        self.assertEqual(actual.stdout, expected.stdout)
        self.assertEqual({p: (p.stat().st_ino, p.stat().st_mtime_ns, p.read_bytes()) for p in indexes}, before)
