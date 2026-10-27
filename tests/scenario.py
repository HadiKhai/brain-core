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
import py_compile
import shutil
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
import types
from functools import lru_cache
from pathlib import Path

CORE = Path(os.environ.get("BRAIN_CORE") or Path(__file__).resolve().parent.parent)

# Per-worker APFS template; instance state is always initialized anew. Checked
# bytecode avoids recompiling unchanged core sources in every real CLI child.
_core_template = None
_core_bytecode = {}
_runtime_bytecode = None
_bytecode_lock = threading.RLock()


@lru_cache(maxsize=8)
def fixture_executable_path(path):
    """Avoid Apple's Git dispatcher for fixtures using the selected developer Git.

    Resolve once per worker; keep explicit Git installations and non-Darwin PATHs
    unchanged. This selects the same executable as /usr/bin/git, with its real
    hooks, locking, environment and subprocess boundaries intact.
    """
    if sys.platform != 'darwin' or shutil.which('git', path=path) != '/usr/bin/git':
        return path
    try:
        result = subprocess.run(['/usr/bin/xcrun', '--find', 'git'], check=True,
                                capture_output=True, text=True)
        executable = Path(result.stdout.strip())
        if executable.is_absolute() and executable.is_file() and os.access(executable, os.X_OK):
            return str(executable.parent) + os.pathsep + path
    except (OSError, subprocess.CalledProcessError):
        pass
    return path


def compiled_fixture_core(source, identity):
    """Share immutable checked code, never initialized modules or instance state."""
    with _bytecode_lock:
        return _compiled_fixture_core(source, identity)


def _compiled_fixture_core(source, identity):
    key = str(identity)
    if key not in _core_bytecode:
        candidate = tempfile.TemporaryDirectory(prefix='brain-test-bytecode-')
        try:
            for module in (Path(source) / 'lib/brain').glob('*.py'):
                cache = Path(candidate.name) / (module.stem + '.' + sys.implementation.cache_tag + '.pyc')
                py_compile.compile(str(module), cfile=str(cache), doraise=True,
                                   invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH)
        except BaseException:
            candidate.cleanup()
            raise
        _core_bytecode[key] = candidate
    return Path(_core_bytecode[key].name)


def fixture_runtime_cache(core):
    """A worker-private prefix for both stdlib and copied-core import caches.

    Checked hashes reject changed sources even when size and mtime are restored.
    Children run with -B, so sharing immutable cache files cannot publish changed
    bytecode back into another fixture. CPython restores each installed filename.
    """
    with _bytecode_lock:
        return _fixture_runtime_cache(core)


def _fixture_runtime_cache(core):
    global _runtime_bytecode
    if _runtime_bytecode is None:
        candidate = tempfile.TemporaryDirectory(prefix='brain-test-runtime-cache-')
        try:
            # Compile the already loaded stdlib once, rather than losing Apple's
            # redirected caches when selecting a private cache prefix. Never
            # compile dependencies or write to a system/user cache directory.
            stdlib = Path(os.__file__).parent
            sources = {Path(module.__file__) for module in list(sys.modules.values())
                       if getattr(module, '__file__', None) and str(module.__file__).endswith('.py')}
            for source in sources:
                if stdlib not in source.parents or 'site-packages' in source.parts:
                    continue
                relative = source.absolute().relative_to(source.anchor)
                cache = Path(candidate.name) / relative.parent / (source.stem + '.' + sys.implementation.cache_tag + '.pyc')
                py_compile.compile(str(source), cfile=str(cache), doraise=True,
                                   invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH)
        except BaseException:
            candidate.cleanup()
            raise
        _runtime_bytecode = candidate
    prefix = Path(_runtime_bytecode.name)
    package = Path(core).resolve() / 'lib/brain'
    if package.is_dir():
        cached = prefix / package.relative_to(package.anchor)
        if not cached.exists():
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.symlink_to(compiled_fixture_core(CORE, CORE), target_is_directory=True)
    return prefix


def clone_fixture_core(source, destination, **kwargs):
    global _core_template
    if Path(source) != CORE or sys.platform != 'darwin':
        return shutil.copytree(source, destination, **kwargs)
    if _core_template is None:
        candidate = tempfile.TemporaryDirectory(prefix='brain-test-core-')
        try:
            shutil.copytree(source, Path(candidate.name) / 'core', **kwargs)
            package = Path(candidate.name) / 'core/lib/brain'
            if package.is_dir():
                shutil.copytree(compiled_fixture_core(Path(candidate.name) / 'core', CORE),
                                package / '__pycache__')
        except BaseException:
            candidate.cleanup()
            raise
        _core_template = candidate
    # APFS clones have independent inodes and copy-on-write file contents. Never
    # use hard links: scenarios deliberately edit their installed core copies.
    subprocess.run(['/bin/cp', '-cR', str(Path(_core_template.name) / 'core'),
                    str(destination)], check=True, capture_output=True, text=True)
    return str(destination)


_instance_templates = {}
_repo_templates = {}
_setup_templates = {}


def _copy_fixture_tree(source, destination):
    """Copy mutable fixtures with independent inodes, preserving relative links."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == 'darwin':
        subprocess.run(['/bin/cp', '-cR', str(source), str(destination)],
                       check=True, capture_output=True, text=True)
    else:
        shutil.copytree(source, destination, symlinks=True)


def _relocate_fixture_value(value, old, new):
    if isinstance(value, Path):
        return Path(str(value).replace(old, new))
    if isinstance(value, str):
        return value.replace(old, new)
    if isinstance(value, tuple):
        return tuple(_relocate_fixture_value(v, old, new) for v in value)
    if isinstance(value, list):
        return [_relocate_fixture_value(v, old, new) for v in value]
    if isinstance(value, dict):
        return {_relocate_fixture_value(k, old, new): _relocate_fixture_value(v, old, new)
                for k, v in value.items()}
    if value is None or type(value) in (bool, int, float):
        return value
    raise TypeError('fixture templates hold paths and plain values, never live objects')


class _FixtureTemplate:
    """A privately published filesystem snapshot, never shared mutable test state.

    Audited setups keep absolute machine bindings in ignored metadata, Git's
    worktree pointers and synthetic session files. Relocate those on each copy;
    Git objects and installed source stay byte-for-byte unchanged.
    """
    def __init__(self, source, home, value):
        self.directory = tempfile.TemporaryDirectory(prefix='brain-test-template-')
        self.home = str(home)
        self.relocations, self.links = [], []
        tree = Path(self.directory.name) / 'tree'
        try:
            self.value = _relocate_fixture_value(value, self.home, self.home)
            _copy_fixture_tree(source, tree)
            for directory, dirs, files in os.walk(tree, followlinks=False):
                base = Path(directory)
                # Installed source and compressed Git objects are immutable.
                if base.name == '.git':
                    dirs[:] = [d for d in dirs if d != 'objects']
                dirs[:] = [d for d in dirs if d != 'core']
                for name in dirs + files:
                    path = base / name
                    if path.is_symlink():
                        target = os.readlink(path)
                        if self.home in target:
                            self.links.append((path.relative_to(tree), target))
                    elif path.is_file() and not (base.name == '.git' and name == 'index'):
                        data = path.read_bytes()
                        if self.home.encode() in data:
                            # Reject an unrecognized binary binding rather than
                            # corrupting a database or a Git index by replacing it.
                            data.decode('utf-8')
                            self.relocations.append((path.relative_to(tree), data))
        except BaseException:
            self.directory.cleanup()
            raise

    def clone(self, destination, home):
        _copy_fixture_tree(Path(self.directory.name) / 'tree', destination)
        for relative, data in self.relocations:
            (destination / relative).write_bytes(data.replace(self.home.encode(), str(home).encode()))
        for relative, target in self.links:
            path = destination / relative
            path.unlink()
            path.symlink_to(target.replace(self.home, str(home)))
        return _relocate_fixture_value(self.value, self.home, str(home))


class Scenario(unittest.TestCase):
    fixture_init_in_process = False
    fixture_templates = False
    def setUp(self):
        # A real verify may enclose the test runner. Its descriptor stays open and
        # locked; its environment proof must not identify our temporary instances.
        gate_keys = ('BRAIN_GATE_FD', 'BRAIN_GATE_PATH', 'BRAIN_GATE_NODE')
        enclosing_gate = {key: os.environ[key] for key in gate_keys if key in os.environ}
        def restore_gate_environment():
            for key in gate_keys:
                os.environ.pop(key, None)
            os.environ.update(enclosing_gate)
        self.addCleanup(restore_gate_environment)
        for key in gate_keys:
            os.environ.pop(key, None)
        self.tmp = Path(tempfile.mkdtemp(prefix="brain-test-")).resolve()
        if getattr(self, 'retain_rehearsal_evidence', False):
            self.start_native_evidence()
        self.env = {**os.environ, "HOME": str(self.tmp), "BRAIN_NO_REEXEC": "1",
                    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                    "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
                    # Keep background Git writers out of fabricated read-only snapshots and teardown.
                    "GIT_CONFIG_COUNT": "3", "GIT_CONFIG_KEY_0": "gc.auto", "GIT_CONFIG_VALUE_0": "0",
                    "GIT_CONFIG_KEY_1": "maintenance.auto", "GIT_CONFIG_VALUE_1": "false",
                    "GIT_CONFIG_KEY_2": "gc.autoDetach", "GIT_CONFIG_VALUE_2": "false",
                    "PATH": fixture_executable_path(os.environ.get("PATH", "")),
                    "PYTHONPATH": str(CORE / "tests"), "BRAIN_USAGE_TESTING": "1",
                    "BRAIN_USAGE_TEST_NO_KEYCHAIN": "1", "BRAIN_MACHINE_TESTING": "1",
                    "BRAIN_MACHINE_READING": json.dumps({"cpus": 8, "load": [1, 1, 1],
                        "load_per_cpu": [0.125, 0.125, 0.125], "thermal": "normal",
                        "mem_free_gb": 32, "disk_free_gb": 200})}
        for k in ("BRAIN_ROOT", "BRAIN_CONFIG", "XDG_CONFIG_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME",
                  "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"):
            self.env.pop(k, None)

        self.env['PYTHONPYCACHEPREFIX'] = str(fixture_runtime_cache(CORE))
        self.env['PYTHONDONTWRITEBYTECODE'] = '1'

    def tearDown(self):
        # Diagnostic finalizers must run before removal, not in addCleanup.
        retainers = getattr(self, '_before_fixture_delete', ())
        preserve = False
        for retain in retainers:
            try:
                preserve = retain() is False or preserve
            except BaseException:
                preserve = True  # Retention must not replace a test exception.
        if preserve:
            return
        try:
            shutil.rmtree(self.tmp, ignore_errors=True)
        except BaseException as error:
            for retain in retainers:
                try:
                    retain(error)
                except BaseException:
                    pass  # Keep the original teardown exception.
            raise

    def start_native_evidence(self):
        if not os.environ.get('BRAIN_REHEARSAL_EVIDENCE_DIR') or hasattr(self, '_native_evidence'):
            return
        from brain import rehearse
        self._native_retention = rehearse.NativeRetention(self.tmp, 'before-helper-initialization',
            nodeid=self.id(), fixture=str(self.tmp), root='UNKNOWN', trace_path='UNKNOWN',
            before='UNKNOWN', after='UNKNOWN', observations='UNKNOWN',
            validation_instant='UNKNOWN', validator_result='UNKNOWN',
            ordered_trace_hex='UNKNOWN', exception={})
        self._native_evidence = self._native_retention.packet
        from brain import rehearse_transactions
        validation_token = rehearse_transactions._validation_retainer.set(self._native_retention)
        self.addCleanup(rehearse_transactions._validation_retainer.reset, validation_token)
        self._native_evidence_preserve = False
        self._native_children = []
        token = rehearse._retention_children.set(self._native_children)
        self.addCleanup(rehearse._retention_children.reset, token)
        self._before_fixture_delete = [*getattr(self, '_before_fixture_delete', ()),
                                       self.before_native_fixture_delete]
        # unittest omits tearDown if setUp raises; retain at the same pre-delete
        # boundary through cleanup too. A second call is harmless after removal.
        self.addCleanup(self.before_native_fixture_delete)

    def update_native_evidence(self, **fields):
        if hasattr(self, '_native_retention'):
            self._native_retention.checkpoint(**fields)

    def native_evidence_exception(self, error):
        if hasattr(self, '_native_retention'):
            self._native_retention.exception(error)

    def capture_native_trace(self, field, trace, required=False):
        if not hasattr(self, '_native_retention'):
            return
        try:
            if trace is not None and trace.exists():
                self._native_evidence[field] = trace.read_bytes().hex()
        except BaseException as error:
            self._native_retention.incomplete = True
            self._native_retention.exception(error)
        finally:
            self.retain_native_evidence()

    def before_native_fixture_delete(self, error=None):
        if not hasattr(self, '_native_retention') or not self.tmp.exists():
            return True
        if error is not None:
            self._native_retention.incomplete = True
            self._native_retention.exception(error)
        if any(child.incomplete and (child.owned == self.tmp or self.tmp in child.owned.parents)
               for child in self._native_children):
            self._native_retention.incomplete = True
        return self._native_retention.checkpoint(stage='before-fixture-delete', teardown='before-delete')

    def retain_native_evidence(self):
        if not hasattr(self, '_native_retention'):
            return True
        result = self._native_retention.checkpoint()
        self._native_evidence_preserve = not result
        return result

    # helpers
    def run_(self, args, cwd, ok=True, brain=None):
        # Existing scenarios assert complete UI data; the default JSON CLI may
        # now return an honest initial observation while its fill is running.
        if args and args[0] == 'ui' and '--json' in args and '--wait' not in args:
            args = [*args, '--wait']
        launcher = Path(brain or (cwd / 'core/bin/brain'))
        cache = fixture_runtime_cache(launcher.resolve().parent.parent)
        r = subprocess.run([sys.executable, '-B', '-X', 'pycache_prefix=' + str(cache),
                            str(launcher), *args], cwd=cwd,
                           env=self.env, capture_output=True, text=True)
        if ok is True and r.returncode:
            self.fail(f"brain {' '.join(args)} failed:\n{r.stdout}\n{r.stderr}")
        if ok is False and not r.returncode:
            self.fail(f"brain {' '.join(args)} should have failed:\n{r.stdout}")
        return r.stdout + r.stderr

    def git(self, *args, cwd):
        r = subprocess.run(["git", "-c", "protocol.file.allow=always", *args], cwd=cwd, env=self.env,
                           capture_output=True, text=True)
        if r.returncode:
            self.fail(f"git {' '.join(args)}: {r.stderr}")
        return r.stdout

    def init(self, name, operator=None):
        root = self.tmp / name
        use_template = self.fixture_templates and not getattr(self, '_evidence', None)
        key = self._fixture_key(name, operator or name)
        if use_template and key in _instance_templates and not root.exists():
            _instance_templates[key].clone(root, self.tmp)
            # Registration is machine-local and must describe this fresh HOME,
            # including any other instances already registered by this test.
            with self._fixture_module(self.tmp, CORE / 'bin/brain') as module:
                module._owners['cmd_init'].register(root)
        else:
            args = ['init', str(root), '--operator', operator or name,
                    '--title', f'{name} brain', '--tz', 'UTC']
            if self.fixture_init_in_process:
                self.fixture_cli(args, cwd=self.tmp, brain=CORE / 'bin/brain')
            else:
                self.run_(args, cwd=self.tmp, brain=CORE / 'bin/brain')
            if use_template and key not in _instance_templates:
                candidate = _FixtureTemplate(root, self.tmp, root)
                _instance_templates[key] = candidate
        fixture_runtime_cache(root / 'core')
        return root

    def _fixture_key(self, *parts):
        # The pytest identity changes per method; it is not fixture input.
        environment = tuple(sorted((k, v.replace(str(self.tmp), '<fixture-home>'))
                                   for k, v in self.env.items() if k != 'PYTEST_CURRENT_TEST'))
        return (str(CORE), environment, *parts)

    def cached_setup(self, key, build):
        """Reuse an audited setup tree; each test still gets independent files.

        Only an empty fresh HOME is eligible. Repeated calls in a mutated test
        take the original path, and evidence fixtures retain real subprocesses.
        """
        if getattr(self, '_evidence', None) or any(self.tmp.iterdir()):
            return build()
        key = self._fixture_key(type(self).__module__, type(self).__qualname__, key)
        if key not in _setup_templates:
            value = build()
            candidate = _FixtureTemplate(self.tmp, self.tmp, value)
            _setup_templates[key] = candidate
            return value
        self.tmp.rmdir()  # known empty; never remove a populated fixture
        value = _setup_templates[key].clone(self.tmp, self.tmp)
        self._prepare_fixture_cores()
        return value

    def _prepare_fixture_cores(self):
        # Raw subprocess.run/Popen calls inherit the same checked cache as run_.
        for launcher in self.tmp.glob('*/core/bin/brain'):
            fixture_runtime_cache(launcher.parent.parent)

    @contextlib.contextmanager
    def _fixture_module(self, cwd, brain=None):
        previous = Path.cwd()
        launcher = str(brain or cwd / 'core/bin/brain')
        loader = core_loader.SourceFileLoader('fixture_setup', launcher)
        try:
            with mock.patch.dict(os.environ, self.env, clear=True), \
                    mock.patch.object(sys, 'dont_write_bytecode', True):
                os.chdir(cwd)
                spec = importlib.util.spec_from_loader(loader.name, loader)
                module = importlib.util.module_from_spec(spec)
                loader.exec_module(module)
                yield module
        finally:
            os.chdir(previous)
            package = getattr(loader, 'package_name', None)
            if package is not None:
                for key in list(sys.modules):
                    if key == package or key.startswith(package + '.'):
                        del sys.modules[key]

    def fixture_cli(self, args, cwd, brain=None):
        """Fresh command state for setup only; never replace an asserted CLI call."""
        if getattr(self, '_evidence', None):
            return self.run_(args, cwd=cwd, brain=brain)
        stdout, stderr = io.StringIO(), io.StringIO()
        launcher = str(brain or cwd / 'core/bin/brain')
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                mock.patch.object(sys, 'argv', [launcher, *args]), \
                self._fixture_module(cwd, brain) as module:
            nodes = module._owners['cmd_init']
            copy = types.SimpleNamespace(**vars(shutil))
            copy.copytree = clone_fixture_core
            with mock.patch.object(nodes, 'shutil', copy):
                try:
                    module.main()
                except SystemExit as error:
                    if error.code not in (None, 0):
                        self.fail('fixture command failed: ' + ' '.join(args) + '\n' + stdout.getvalue() + stderr.getvalue())
        return stdout.getvalue() + stderr.getvalue()

    def fixture_b(self, root, *args):
        return self.fixture_cli(list(args), cwd=root)

    def b(self, root, *args, ok=True):
        return self.run_(list(args), cwd=root, ok=ok)

    def repo(self, path, files=None):
        files = files or {'README.md': 'hello\n'}
        key = self._fixture_key(tuple(sorted(files.items())))
        use_template = self.fixture_templates and not getattr(self, '_evidence', None)
        if use_template and key in _repo_templates and not path.exists():
            _repo_templates[key].clone(path, self.tmp)
            return path
        path.mkdir(parents=True, exist_ok=True)
        self.git('init', '-q', '-b', 'main', cwd=path)
        for k, v in files.items():
            (path / k).write_text(v)
        self.git('add', '-A', cwd=path)
        self.git('commit', '-qm', 'init', cwd=path)
        if use_template and key not in _repo_templates:
            candidate = _FixtureTemplate(path, self.tmp, path)
            _repo_templates[key] = candidate
        return path

    def all_text(self, folder):
        """Everything an instance holds, except the core (which is the same everywhere)."""
        out = []
        for p in Path(folder).rglob("*"):
            if p.is_file() and ".git" not in p.parts and "core" not in p.relative_to(folder).parts:
                try:
                    out.append(p.read_text(errors="ignore"))
                except OSError:
                    pass
        return "\n".join(out)



class AutonomyProject(Scenario):
    fixture_init_in_process = True
    fixture_templates = True

    def project(self):
        return self.cached_setup('autonomy-project', self.project_fixture)

    def project_fixture(self):
        r = self.init('alice')
        home = self.repo(self.tmp / 'code' / 'app')
        self.fixture_b(r, 'new', 'app', '--title', 'App', '--home', str(home))
        self.b(r, 'wt', 'add', 'app', 'job-a')
        local = json.loads((r / '.brain' / 'local.json').read_text())
        wt = Path(local['nodes']['app']['worktrees']['job-a']['path'])
        return r, home, wt

    def due(self, r, node='app'):
        return json.loads(self.b(r, 'due', node, '--json'))



def shlex_quote(value):
    import shlex
    return shlex.quote(value)
