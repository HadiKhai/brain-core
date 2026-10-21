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



if __name__ == "__main__":
    unittest.main()
