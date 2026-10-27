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
    def result_core(self):
        core, package = self.fixture_source()
        (core / 'bin/brain').write_bytes((CORE / 'bin/brain').read_bytes())
        for name in ('common', 'instance', 'events'):
            (package / (name + '.py')).write_text('')
        (package / 'cli.py').write_text(
            'import os,sys\nfrom pathlib import Path\n'
            'loaded = os.environ.get("BRAIN_RESULT_TEST")\ncalls = []\n'
            'def main():\n'
            ' mode = sys.argv[1]\n calls.append(mode)\n'
            ' if mode == "pid":\n  print(os.getpid()); return\n'
            ' print(len(calls), loaded, Path.cwd())\n'
            ' sys.stdout.write("first\\r\\nsecond\\r")\n'
            ' sys.stderr.write("error\\r\\n")\n'
            ' if mode == "mutate":\n'
            '  os.environ.clear(); os.chdir("/"); sys.argv.append("changed")\n'
            '  sys.path.clear(); sys.meta_path.clear(); sys.pycache_prefix = "changed"\n'
            '  sys.dont_write_bytecode = False\n'
            '  raise RuntimeError("injected command failure")\n'
            ' if mode == "string": raise SystemExit("refused")\n'
            ' if mode == "object": raise SystemExit({"reason": "refused"})\n'
            ' if mode == "none": raise SystemExit(None)\n'
            ' if mode != "normal": raise SystemExit(int(mode))\n')
        return core, package

    def test_asserted_main_results_match_native_streams_exits_and_fresh_state(self):
        core, _ = self.result_core()
        launcher = core / 'bin/brain'
        self.env['BRAIN_RESULT_TEST'] = 'fixture import environment'
        for mode in ('normal', 'none', '0', '7', '-1', '257', 'string', 'object'):
            with self.subTest(mode=mode):
                native = self.command_result([mode], self.tmp, launcher, native=True)
                result = self._in_process_result([mode], self.tmp, launcher, native.args)
                self.assertTrue(result.in_process)
                self.assertFalse(hasattr(result, 'pid'))
                self.assertEqual((result.args, result.returncode, result.stdout, result.stderr),
                                 (native.args, native.returncode, native.stdout, native.stderr))
                self.assertIn('1 fixture import environment', result.stdout)
        with mock.patch.object(type(self), 'cli_in_process', True):
            self.assertIn('refused', self.run_(['string'], self.tmp, ok=False, brain=launcher))
            with self.assertRaisesRegex(AssertionError, 'should have failed'):
                self.run_(['normal'], self.tmp, ok=False, brain=launcher)
            with self.assertRaisesRegex(AssertionError, 'refused'):
                self.run_(['string'], self.tmp, brain=launcher)

    def test_asserted_main_restores_globals_and_owned_imports_on_command_and_load_failure(self):
        core, package = self.result_core()
        environment, cwd = dict(os.environ), Path.cwd()
        saved = {name: getattr(sys, name) for name in
                 ('argv', 'path', 'meta_path', 'stdin', 'stdout', 'stderr', 'dont_write_bytecode', 'pycache_prefix')}
        contents = {name: list(saved[name]) for name in ('argv', 'path', 'meta_path')}
        foreign = type(sys)('_brain_fixture_foreign_result')
        with mock.patch.dict(sys.modules, {foreign.__name__: foreign}):
            before = {k: v for k, v in sys.modules.items() if k.startswith('_brain_fixture_')}
            for failure in ('command', 'import'):
                if failure == 'import':
                    (package / 'cli.py').write_text('invalid source !!!\n')
                with self.assertRaisesRegex(AssertionError, 'in-process brain command raised') as raised:
                    self._in_process_result(['mutate'], self.tmp, core / 'bin/brain', ['fixture'])
                self.assertIsInstance(raised.exception.__cause__, RuntimeError if failure == 'command' else SyntaxError)
                if failure == 'command':
                    self.assertIn('first\nsecond\nerror\n', str(raised.exception))
                self.assertEqual(os.environ, environment)
                self.assertEqual(Path.cwd(), cwd)
                for name, value in saved.items():
                    self.assertIs(getattr(sys, name), value)
                for name, values in contents.items():
                    self.assertEqual(getattr(sys, name), values)
                self.assertEqual({k: v for k, v in sys.modules.items() if k.startswith('_brain_fixture_')}, before)
        self.assertEqual(list(core.rglob('__pycache__')), [])

    def test_asserted_main_keeps_evidence_inherited_opt_in_and_launcher_overrides_native(self):
        # A focused test_cli.py run need not import the ordinary brain package.
        with self._fixture_module(self.tmp, CORE / 'bin/brain') as module:
            return self.assert_native_retention_fallbacks(module)

    def assert_native_retention_fallbacks(self, module):
        rehearse = sys.modules[module._owners['main'].__package__ + '.rehearse']
        rehearse_transactions = sys.modules[module._owners['main'].__package__ + '.rehearse_transactions']
        core, _ = self.result_core()
        launcher = core / 'bin/brain'
        def native(case=self, **options):
            result = case.command_result(['pid'], self.tmp, launcher, **options)
            self.assertFalse(getattr(result, 'in_process', False))
            self.assertNotEqual(int(result.stdout), os.getpid())
        with mock.patch.object(type(self), 'cli_in_process', True):
            result = self.command_result(['pid'], self.tmp, launcher)
            self.assertTrue(result.in_process)
            self.assertEqual(int(result.stdout), os.getpid())
            native(native=True)
            class Inherited(type(self)):
                pass
            inherited = Inherited()
            inherited.tmp, inherited.env = self.tmp, self.env
            native(inherited)
            for attribute, value in (('_evidence', {'retained': True}),
                                     ('retain_rehearsal_evidence', True), ('_native_retention', object())):
                with mock.patch.object(self, attribute, value, create=True):
                    native()
            for variable, value in (('BRAIN_NO_REEXEC', ''), ('BRAIN_REHEARSAL_EVIDENCE_DIR', str(self.tmp))):
                with mock.patch.dict(self.env, {variable: value}):
                    native()
            for context, value in ((rehearse._retention_children, []),
                                   (rehearse_transactions._validation_retainer, object())):
                token = context.set(value)
                try:
                    native()
                finally:
                    context.reset(token)
            launcher.write_text(launcher.read_text() + '\n# custom launcher contract\n')
            native()

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
                for repository in (right['root'], right['home']):
                    index = repository / '.git/index'
                    before = index.read_bytes()
                    self.assertEqual(second.git('status', '--porcelain', cwd=repository), '')
                    self.assertEqual(index.read_bytes(), before, 'cloned index must be prepared before use')
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

    def test_remote_setup_clones_keep_origin_local_and_independent_after_source_removal(self):
        import scenario
        cases = []
        built = []
        def build(case):
            built.append(case.tmp)
            seed = case.repo(case.tmp / 'seed')
            origin = case.tmp / 'origin.git'
            case.git('clone', '--bare', str(seed), str(origin), cwd=case.tmp)
            home = case.tmp / 'app'
            case.git('clone', str(origin), str(home), cwd=case.tmp)
            return seed, origin, home
        with mock.patch.object(scenario, '_setup_templates', {}):
            try:
                for _ in range(3):
                    case = Scenario()
                    case.setUp()
                    cases.append(case)
                    self.addCleanup(case.doCleanups)
                    self.addCleanup(case.tearDown)
                    seed, origin, home = case.cached_setup('remote-control', lambda: build(case))
                    self.assertEqual(case.git('remote', 'get-url', 'origin', cwd=home).strip(), str(origin))
                self.assertEqual(built, [cases[0].tmp])
                cases[0].tearDown()
                second, third = cases[1:]
                second.git('commit', '--allow-empty', '-qm', 'independent remote', cwd=second.tmp / 'app')
                second.git('push', 'origin', 'main', cwd=second.tmp / 'app')
                for case in (second, third):
                    case.git('fetch', 'origin', cwd=case.tmp / 'app')
                self.assertNotEqual(second.git('rev-parse', 'main', cwd=second.tmp / 'origin.git'),
                                    third.git('rev-parse', 'main', cwd=third.tmp / 'origin.git'))
            finally:
                for template in scenario._setup_templates.values():
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

    def test_identical_source_compiles_once_across_paths_with_nested_filenames_preserved(self):
        source = b'def outer():\n def inner(): return 42\n return inner\n'
        with mock.patch('builtins.compile', wraps=compile) as compile_source:
            first = core_loader._compiled_source('/owned/first/state.py', source)
            second = core_loader._compiled_source('/owned/second/state.py', source)
        self.assertEqual(compile_source.call_count, 1)
        left, right = {}, {}
        exec(first, left)
        exec(second, right)
        for namespace, path in ((left, '/owned/first/state.py'), (right, '/owned/second/state.py')):
            self.assertEqual(namespace['outer'].__code__.co_filename, path)
            self.assertEqual(namespace['outer']().__code__.co_filename, path)
            self.assertEqual(namespace['outer']()(), 42)
        self.assertIsNot(left['outer'], right['outer'])

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
        for name in ('roles/coordinator.md', 'skills/home-job/SKILL.md'):
            text = (CORE / name).read_text()
            self.assertIn('version 1', text)
            self.assertIn('five minutes', ' '.join(text.split()))
            self.assertIn('--job-key <key>', text)

    def test_jobs_help_describes_settlement_export_and_selection(self):
        result = subprocess.run([sys.executable, str(CORE / 'bin/brain'), 'jobs', '--help'],
                                cwd=CORE, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        help_text = ' '.join(result.stdout.split())
        for clause in ('--settled', '--all-closed', '--attestation-file',
                       'fresh version 1 coordinator export',
                       'exhaustive per-job/thread/project coverage', '--job-key'):
            self.assertIn(clause, help_text)


class EntryPoints(Scenario):
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
