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


class Scenario(unittest.TestCase):
    def setUp(self):
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
        self.run_(["init", str(root), "--operator", operator or name, "--title", f"{name} brain",
                   "--tz", "UTC"], cwd=self.tmp, brain=CORE / "bin" / "brain")
        return root

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
    def project(self):
        r = self.init('alice')
        home = self.repo(self.tmp / 'code' / 'app')
        self.b(r, 'new', 'app', '--title', 'App', '--home', str(home))
        self.b(r, 'wt', 'add', 'app', 'job-a')
        local = json.loads((r / '.brain' / 'local.json').read_text())
        wt = Path(local['nodes']['app']['worktrees']['job-a']['path'])
        return r, home, wt

    def due(self, r, node='app'):
        return json.loads(self.b(r, 'due', node, '--json'))



def shlex_quote(value):
    import shlex
    return shlex.quote(value)
