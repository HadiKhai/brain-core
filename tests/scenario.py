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
import types
from pathlib import Path

CORE = Path(os.environ.get("BRAIN_CORE") or Path(__file__).resolve().parent.parent)

# Source-only per-worker APFS template; instance state is always initialized anew.
_core_template = None


def clone_fixture_core(source, destination, **kwargs):
    global _core_template
    if Path(source) != CORE or sys.platform != 'darwin':
        return shutil.copytree(source, destination, **kwargs)
    if _core_template is None:
        candidate = tempfile.TemporaryDirectory(prefix='brain-test-core-')
        try:
            shutil.copytree(source, Path(candidate.name) / 'core', **kwargs)
        except BaseException:
            candidate.cleanup()
            raise
        _core_template = candidate
    # APFS clones have independent inodes and copy-on-write file contents. Never
    # use hard links: scenarios deliberately edit their installed core copies.
    subprocess.run(['/bin/cp', '-cR', str(Path(_core_template.name) / 'core'),
                    str(destination)], check=True, capture_output=True, text=True)
    return str(destination)


class Scenario(unittest.TestCase):
    fixture_init_in_process = False
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
        self.env = {**os.environ, "HOME": str(self.tmp), "BRAIN_NO_REEXEC": "1",
                    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                    "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
                    # Keep background Git writers out of fabricated read-only snapshots and teardown.
                    "GIT_CONFIG_COUNT": "3", "GIT_CONFIG_KEY_0": "gc.auto", "GIT_CONFIG_VALUE_0": "0",
                    "GIT_CONFIG_KEY_1": "maintenance.auto", "GIT_CONFIG_VALUE_1": "false",
                    "GIT_CONFIG_KEY_2": "gc.autoDetach", "GIT_CONFIG_VALUE_2": "false",
                    "PATH": os.environ.get("PATH", ""),
                    "PYTHONPATH": str(CORE / "tests"), "BRAIN_USAGE_TESTING": "1",
                    "BRAIN_USAGE_TEST_NO_KEYCHAIN": "1", "BRAIN_MACHINE_TESTING": "1",
                    "BRAIN_MACHINE_READING": json.dumps({"cpus": 8, "load": [1, 1, 1],
                        "load_per_cpu": [0.125, 0.125, 0.125], "thermal": "normal",
                        "mem_free_gb": 32, "disk_free_gb": 200})}
        for k in ("BRAIN_ROOT", "BRAIN_CONFIG", "XDG_CONFIG_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME",
                  "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"):
            self.env.pop(k, None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # helpers
    def run_(self, args, cwd, ok=True, brain=None):
        r = subprocess.run([sys.executable, str(brain or (cwd / "core" / "bin" / "brain")), *args], cwd=cwd,
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
        args = ["init", str(root), "--operator", operator or name, "--title", f"{name} brain",
                "--tz", "UTC"]
        if self.fixture_init_in_process:
            self.fixture_cli(args, cwd=self.tmp, brain=CORE / 'bin/brain')
        else:
            self.run_(args, cwd=self.tmp, brain=CORE / 'bin/brain')
        return root


    def fixture_cli(self, args, cwd, brain=None):
        """Fresh command state for setup only; never replace an asserted CLI call."""
        if getattr(self, '_evidence', None):
            return self.run_(args, cwd=cwd, brain=brain)
        stdout, stderr = io.StringIO(), io.StringIO()
        previous = Path.cwd()
        loader = core_loader.SourceFileLoader('fixture_setup', str(brain or cwd / 'core/bin/brain'))
        try:
            with mock.patch.dict(os.environ, self.env, clear=True), \
                    mock.patch.object(sys, 'argv', [str(brain or cwd / 'core/bin/brain'), *args]), \
                    mock.patch.object(sys, 'dont_write_bytecode', True), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                os.chdir(cwd)
                spec = importlib.util.spec_from_loader(loader.name, loader)
                module = importlib.util.module_from_spec(spec)
                loader.exec_module(module)
                nodes = module._owners['cmd_init']
                copy = types.SimpleNamespace(**vars(shutil))
                copy.copytree = clone_fixture_core
                with mock.patch.object(nodes, 'shutil', copy):
                    try:
                        module.main()
                    except SystemExit as error:
                        if error.code not in (None, 0):
                            self.fail('fixture command failed: ' + ' '.join(args) + '\n' + stdout.getvalue() + stderr.getvalue())
        finally:
            os.chdir(previous)
            # The next setup call gets new TOUCHED, context variables and caches,
            # just as a subprocess would; no fixture package survives the call.
            package = getattr(loader, 'package_name', None)
            if package is not None:
                for key in list(sys.modules):
                    if key == package or key.startswith(package + '.'):
                        del sys.modules[key]
        return stdout.getvalue() + stderr.getvalue()

    def fixture_b(self, root, *args):
        return self.fixture_cli(list(args), cwd=root)

    def b(self, root, *args, ok=True):
        return self.run_(list(args), cwd=root, ok=ok)

    def repo(self, path, files=None):
        path.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", "main", cwd=path)
        for k, v in (files or {"README.md": "hello\n"}).items():
            (path / k).write_text(v)
        self.git("add", "-A", cwd=path)
        self.git("commit", "-qm", "init", cwd=path)
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
    def project(self):
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
