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


class Review(Scenario):
    def test_review_brief_goes_to_the_other_runtime(self):
        r = self.init("alice")
        self.b(r, "principal", "add", "team")
        home = self.repo(self.tmp / "app")
        self.b(r, "new", "app", "--title", "App", "--owners", "team", "--home", str(home))
        self.b(r, "wt", "add", "app", "fix-login", "--base", "main")
        wt = self.tmp / "app-work" / "worktrees" / "fix-login"
        (wt / "login.py").write_text("def login(u, p):\n    return True\n")
        self.git("add", "-A", cwd=wt); self.git("commit", "-qm", "Fix login", cwd=wt)
        out = self.b(r, "review", "app", "--branch", "fix-login", "--built-by", "claude", "--focus", "auth")
        self.assertIn("Reviewer runtime: codex", out)
        self.assertIn(f"Run in: {wt}", out)
        self.assertIn('"Review reason": ""', self.b(r, "jobs", "app", "--branch", "fix-login"))
        self.assertIn("login.py", out)
        self.assertIn("Don't modify any file", out)
        self.assertIn("has its own owners", out)
        self.assertNotIn(str(r), out)                        # nothing from above the boundary
        self.assertIn("brain recall app", out)
        (r / "nodes/app/knowledge.md").write_text("# K\n\n- notes\n")
        self.assertNotIn("knowledge.md", self.b(r, "review", "app", "--branch", "fix-login", "--built-by", "claude", "--reason", "security"))
        self.assertIn("Reviewer runtime: claude",
                      self.b(r, "review", "app", "--branch", "fix-login", "--built-by", "codex", "--reason", "security"))
        self.b(r, "review", "app", "--branch", "fix-login", "--built-by", "codex", "--by", "codex", ok=False)



class AutonomySync(Scenario):
    def test_sync_uses_judgement_timestamp_after_refresh_and_handles_empty_status(self):
        r = self.init('alice')
        st = r / 'status.md'
        st.write_text('Updated: 2020-01-02 03:04:05 +00:00 by coordinator\n\n## Now\n- Ready.\n')
        self.b(r, 'status', 'root', '--refresh')
        self.assertTrue(st.read_text().startswith('Updated: 2020-01-02'))
        self.assertIn('changes since 2020-01-02 03:04 (status.md Updated)', self.b(r, 'sync', 'root', '--offline'))
        self.b(r, 'sync', 'root', '--mark', '--offline')
        self.assertIn('(last sync)', self.b(r, 'sync', 'root', '--offline'))
        # Without a machine-local sync mark an empty status falls back, rather than raising IndexError.
        local = r / '.brain/local.json'
        data = json.loads(local.read_text())
        data['nodes']['root'].pop('synced_at')
        local.write_text(json.dumps(data))
        st.write_text('')
        self.assertIn('(last 7 days)', self.b(r, 'sync', 'root', '--offline'))



if __name__ == "__main__":
    unittest.main()
