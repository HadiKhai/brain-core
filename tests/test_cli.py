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
