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


class Schedules(Scenario):
    def test_schedules_are_shared_and_bound_per_machine(self):
        r = self.init("alice")
        self.b(r, "schedule", "add", "daily-brief", "--when", "daily 09:00", "--brief", "Run the node-brief skill.")
        self.assertIn("schedule: daily-brief", self.git("log", "-1", "--format=%s", cwd=r))
        self.assertIn("not on this machine", self.b(r, "schedule"))
        self.b(r, "schedule", "bind", "daily-brief", "host-42")
        self.assertIn("on this machine: host-42", self.b(r, "schedule", "list"))
        self.assertNotIn("host-42", self.git("show", "HEAD", cwd=r))       # the binding isn't shared
        self.assertIn("daily-brief                  on this machine", self.b(r, "machine"))
        self.assertIn("✓", self.b(r, "check"))
        (r / "schedules.md").write_text((r / "schedules.md").read_text().replace("node: root", "node: ghost"))
        self.assertIn("unknown node 'ghost'", self.b(r, "check", ok=False))

    def test_sync_all_covers_every_external_home(self):
        r = self.init("alice")
        self.b(r, "principal", "add", "team")
        for nid in ("one", "two"):
            home = self.repo(self.tmp / nid, {"README.md": f"{nid}\n"})
            self.b(r, "new", nid, "--title", nid, "--owners", "team", "--home", str(home))
        self.b(r, "new", "local", "--title", "Local")
        out = self.b(r, "sync", "--all", "--offline")
        self.assertIn("# one:", out)
        self.assertIn("# two:", out)
        self.assertNotIn("# local:", out)



if __name__ == "__main__":
    unittest.main()
