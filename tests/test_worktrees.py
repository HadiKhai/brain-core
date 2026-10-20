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


class Workbench(Scenario):
    def test_failed_setup_keeps_the_worktree_and_reports_failure(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "code" / "app")
        self.b(r, "new", "app", "--title", "App", "--home", str(home))
        (r / "nodes" / "app" / "worktree-setup.sh").write_text('echo partial > partial.txt\nexit 7\n')
        p = subprocess.run([sys.executable, str(r / "core" / "bin" / "brain"), "wt", "add", "app", "job-fails"],
                           cwd=r, env=self.env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 7, p.stdout + p.stderr)
        self.assertIn("setup: FAILED", p.stdout)
        self.assertNotIn("launch a worker there", p.stdout)
        local = json.loads((r / ".brain" / "local.json").read_text())
        path = Path(local["nodes"]["app"]["worktrees"]["job-fails"]["path"])
        self.assertEqual((path / "partial.txt").read_text(), "partial\n")
        self.assertIn("job-fails", self.b(r, "wt", "list", "app"))

    def test_worktrees_with_setup_ports_and_prune(self):
        r = self.init("alice")
        self.b(r, "principal", "add", "team")
        origin = self.tmp / "origin.git"
        self.git("init", "-q", "--bare", "-b", "main", str(origin), cwd=self.tmp)
        home = self.repo(self.tmp / "code" / "app", {"README.md": "app\n", ".gitignore": ".env\n"})
        self.git("remote", "add", "origin", str(origin), cwd=home)
        self.git("push", "-q", "-u", "origin", "main", cwd=home)
        self.git("remote", "set-head", "origin", "main", cwd=home)
        (home / ".env").write_text("SECRET=1\n")
        self.b(r, "new", "app", "--title", "App", "--owners", "team", "--home", str(home))
        (r / "nodes" / "app" / "worktree-setup.sh").write_text('cp "$MAIN/.env" .env\necho "PORT=$PORT" >> .env\n')

        out = self.b(r, "wt", "add", "app", "feat-a")
        wa = self.tmp / "code" / "app-work" / "worktrees" / "feat-a"
        self.assertIn("setup: ok", out)
        self.assertIn("SECRET=1", (wa / ".env").read_text())
        self.b(r, "wt", "add", "app", "feat-b")
        wb_ = wa.parent / "feat-b"
        ports = {(w / ".env").read_text().split("PORT=")[1].strip() for w in (wa, wb_)}
        self.assertEqual(len(ports), 2, "each worktree gets its own port")
        self.assertIn("✓", self.b(r, "check"))                  # nothing in the home, nothing in the instance

        # feat-a is done and merged; feat-b has unfinished work; feat-c is new and untouched
        (wa / "a.txt").write_text("a\n")
        self.git("add", "a.txt", cwd=wa); self.git("commit", "-qm", "A", cwd=wa)
        self.git("merge", "-q", "feat-a", cwd=home); self.git("push", "-q", "origin", "main", cwd=home)
        (wb_ / "wip.txt").write_text("wip\n")
        self.b(r, "wt", "add", "app", "feat-c")
        self.assertIn("would remove: feat-a", self.b(r, "wt", "prune", "app", "--dry-run"))
        out = self.b(r, "wt", "prune", "app")
        self.assertIn("removed: feat-a", out)
        self.assertIn("feat-b (uncommitted work)", out)
        self.assertIn("feat-c (not merged)", out)
        self.assertFalse(wa.exists())
        self.assertTrue((wb_ / "wip.txt").exists())
        self.b(r, "wt", "rm", "app", "feat-b", ok=False)        # uncommitted work is never discarded silently
        self.assertIn("feat-c", self.b(r, "wt", "list"))

    def test_reports_have_a_predictable_place(self):
        r = self.init("alice")
        self.b(r, "new", "site", "--title", "Site")
        d = Path(self.b(r, "report", "site", "UI audit: mobile").strip())
        self.assertEqual(d.parent, r / "nodes" / "site" / "reports")
        self.assertTrue(d.name.endswith("-ui-audit-mobile"))
        for h in ("## Answer", "## Findings", "## Controls", "## Not run", "## How to reproduce"):
            self.assertIn(h, (d / "index.md").read_text())
        self.assertIn("UI audit: mobile", self.b(r, "reports"))
        self.assertIn("scripts/", self.b(r, "workbench", "site"))

    def test_a_workbench_inside_the_instance_is_refused(self):
        r = self.init("alice")
        self.b(r, "principal", "add", "team")
        home = self.repo(self.tmp / "h")
        self.b(r, "new", "x", "--title", "X", "--owners", "team", "--home", str(home))
        self.b(r, "set", "x", "workbench=" + str(r / "wb"))
        self.assertIn("workbench is inside the instance", self.b(r, "check", ok=False))



class WorktreeSetupDiagnostics(Scenario):
    def finish(self, r):
        return self.b(r, "handback", "a", "--branch", "job", "--state", "done", "--reviews", "1")

    def test_setup_completes_before_return_and_first_verify(self):
        r = self.init('alice')
        home = self.repo(self.tmp / 'app', {'.gitignore': '.env\nsetup-started\n',
                         'verify.py': "from pathlib import Path\nassert Path('.env').read_text() == 'GENERATED=prepared-fixture\\n'\n"})
        self.b(r, 'new', 'a', '--title', 'A', '--home', str(home))
        self.b(r, 'set', 'a', 'verify=python3 -B verify.py')
        release = home / 'allow-setup'
        (r / 'nodes/a/worktree-setup.sh').write_text(
            'touch setup-started\nwhile ! test -f "$MAIN/allow-setup"; do sleep 0.02; done\n'
            "printf 'GENERATED=prepared-fixture\\n' > .env\n")
        process = subprocess.Popen([sys.executable, str(r / 'core/bin/brain'), 'wt', 'add', 'a', 'job'],
                                   cwd=r, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        wt = self.tmp / 'app-work/worktrees/job'
        try:
            deadline = time.monotonic() + 10
            while not (wt / 'setup-started').exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue((wt / 'setup-started').exists())
            self.assertIsNone(process.poll())
            self.assertFalse((wt / '.env').exists())
        finally:
            release.touch()
            output, errors = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertIn('setup: running; waiting for completion', output)
        self.assertRegex(output, r'setup: ok in \d+\.\d+s')
        self.assertLess(output.index('setup: ok'), output.index('launch a worker'))
        self.assertEqual((wt / '.env').read_text(), 'GENERATED=prepared-fixture\n')
        self.assertTrue(self.finish(r).strip().endswith('ready'))

    def test_setup_failure_reports_duration_masks_words_and_returns_failure(self):
        r = self.init('alice')
        home = self.repo(self.tmp / 'app')
        self.b(r, 'new', 'a', '--title', 'A', '--home', str(home))
        (r / 'nodes/a/never-name.txt').write_text('HiddenFixture\n')
        (r / 'nodes/a/worktree-setup.sh').write_text('echo HiddenFixture\nexit 7\n')
        process = subprocess.run([sys.executable, str(r / 'core/bin/brain'), 'wt', 'add', 'a', 'job'],
                                 cwd=r, env=self.env, capture_output=True, text=True)
        self.assertEqual(process.returncode, 7)
        self.assertRegex(process.stdout, r'setup: FAILED \(exit 7\) in \d+\.\d+s')
        self.assertNotIn('hiddenfixture', (process.stdout + process.stderr).casefold())
        self.assertNotIn('launch a worker', process.stdout)
        self.assertTrue((self.tmp / 'app-work/worktrees/job').exists())


if __name__ == "__main__":
    unittest.main()
