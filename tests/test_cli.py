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
        shutil.copytree(CORE, core, ignore=shutil.ignore_patterns(".git", "__pycache__", ".DS_Store"))
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



if __name__ == "__main__":
    unittest.main()
