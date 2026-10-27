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
    cli_in_process = True
    def test_check_warns_on_recorded_provider_mismatch(self):
        r = self.init("alice")
        self.b(r, "new", "app", "--title", "App", "--runtime", "codex")
        self.b(r, "schedule", "add", "app-tick", "--node", "app", "--when", "hourly",
               "--brief", "Check for work.")
        f = r / "schedules.md"
        f.write_text(f.read_text().replace("brief:", "provider: claude\nbrief:"))
        expected = "schedule 'app-tick' provider 'claude' differs from node 'app' runtime pin 'codex'"
        before = f.read_text()
        self.assertIn(expected, self.b(r, "check"))
        self.assertIn(expected, self.b(r, "check", "--fix"))
        self.assertEqual(before, f.read_text())
        self.b(r, "set", "app", "runtime=claude")
        self.assertNotIn("runtime pin", self.b(r, "check"))
        self.b(r, "set", "app", "runtime=")
        self.assertNotIn("runtime pin", self.b(r, "check"))

    def test_add_and_bind_record_provider_without_changing_legacy_bindings(self):
        r = self.init("alice")
        self.b(r, "set", "root", "runtime=codex")
        self.b(r, "schedule", "add", "tick", "--when", "hourly", "--brief", "Check work.",
               "--provider", "claude")
        f = r / "schedules.md"
        self.assertIn("provider: claude\n", f.read_text())
        shared = f.read_text()
        self.assertIn("provider 'claude' differs", self.b(r, "check"))
        self.b(r, "schedule", "bind", "tick", "host-42", "--provider", "codex")
        local_path = r / ".brain" / "local.json"
        local = json.loads(local_path.read_text())
        self.assertEqual(local["schedules"]["tick"], "host-42")
        self.assertEqual(local["schedule_providers"]["tick"], "codex")
        self.assertEqual(shared, f.read_text())
        self.assertNotIn("runtime pin", self.b(r, "check"))
        self.assertIn("on this machine: host-42", self.b(r, "schedule", "list"))
        self.assertRegex(self.b(r, "machine"), r"tick\s+on this machine")
        self.assertNotIn("host-42", self.git("show", "HEAD", cwd=r))
        # A replacement host task must not inherit the old task's provider.
        self.b(r, "schedule", "bind", "tick", "host-43")
        self.assertNotIn("tick", json.loads(local_path.read_text())["schedule_providers"])
        self.assertIn("provider 'claude' differs", self.b(r, "check"))
        self.b(r, "schedule", "bind", "tick", "host-43", "--provider", "codex")
        self.b(r, "schedule", "unbind", "tick")
        local = json.loads(local_path.read_text())
        self.assertNotIn("tick", local["schedules"])
        self.assertNotIn("tick", local["schedule_providers"])
        self.assertIn("provider 'claude' differs", self.b(r, "check"))

    def test_legacy_schedule_without_provider_does_not_guess(self):
        r = self.init("alice")
        self.b(r, "set", "root", "runtime=codex")
        self.b(r, "schedule", "add", "legacy", "--when", "hourly", "--brief", "Check work.")
        self.b(r, "schedule", "bind", "legacy", "claude-looking-task-id")
        self.assertNotIn("runtime pin", self.b(r, "check"))
        self.assertIn("on this machine: claude-looking-task-id", self.b(r, "schedule"))

    def test_check_redacts_schedule_names_in_shared_and_bound_warnings(self):
        r = self.init("alice")
        self.b(r, "new", "app", "--title", "App", "--runtime", "codex")
        (r / "nodes" / "app" / "never-name.txt").write_text("velnora\n")
        self.b(r, "schedule", "add", "velnora-shared", "--node", "app", "--when", "hourly",
               "--brief", "Check work.", "--provider", "claude")
        self.b(r, "schedule", "add", "velnora-local", "--node", "app", "--when", "hourly",
               "--brief", "Check work.", "--provider", "codex")
        self.b(r, "schedule", "bind", "velnora-local", "host-42", "--provider", "claude")
        out = self.b(r, "check")
        self.assertNotIn("velnora", out.casefold())
        for suffix in ("shared", "local"):
            self.assertIn("schedule '[…]-" + suffix + "' provider 'claude' differs from node 'app' runtime pin 'codex'", out)

    def test_provider_is_not_part_of_multiline_brief(self):
        r = self.init("alice")
        f = r / "schedules.md"
        f.write_text("# Schedules\n\n## tick\nwhen: hourly\nnode: root\n"
                     "brief: Check work.\n  Report blockers.\nprovider: codex\n")
        before = f.read_bytes()
        out = self.b(r, "schedule", "list", "--verbose")
        prefix = "Automated turn: no operator present; cancellations are host events."
        brief = prefix + "  Check work. Report blockers."
        self.assertIn("brief: " + brief + "\n", out)
        self.assertNotIn(brief + " provider:", out)
        self.assertEqual(out.count(prefix), 1)
        self.assertEqual(f.read_bytes(), before)
        loader = core_loader.SourceFileLoader('schedule_fixture', str(CORE / 'bin/brain'))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        schedule = module.read_schedules(module.Instance(r))[0]
        self.assertEqual(schedule['brief'], brief)
        self.assertEqual(schedule['provider'], 'codex')

    def test_invalid_provider_refuses_before_writing(self):
        r = self.init("alice")
        self.b(r, "schedule", "add", "tick", "--when", "hourly", "--brief", "Check work.")
        self.b(r, "schedule", "bind", "tick", "original-host")
        shared = (r / "schedules.md").read_text()
        local = (r / ".brain" / "local.json").read_bytes()
        for args in (("add", "other", "--when", "hourly", "--brief", "Check work."),
                     ("bind", "tick", "host-42")):
            for provider in ("unknown-engine", "codex\nnode: ghost", ""):
                with self.subTest(action=args[0], provider=provider):
                    self.assertIn("provider must be an instance runtime", self.b(
                        r, "schedule", *args, "--provider", provider, ok=False))
                    self.assertEqual(shared, (r / "schedules.md").read_text())
                    self.assertEqual(local, (r / ".brain" / "local.json").read_bytes())

    def test_check_uses_current_node_for_former_schedule_reference(self):
        r = self.init("alice")
        self.b(r, "new", "app", "--title", "App", "--runtime", "codex")
        self.b(r, "new", "group", "--title", "Group")
        self.b(r, "schedule", "add", "tick", "--node", "app", "--when", "hourly",
               "--brief", "Check work.", "--provider", "claude")
        self.b(r, "move", "app", "group")
        out = self.b(r, "check")
        self.assertIn("schedule 'tick' provider 'claude' differs from node 'group/app' runtime pin 'codex'", out)
        self.assertNotIn("unknown node", out)
        f = r / "schedules.md"
        f.write_text(f.read_text().replace("node: app", "node: ghost"))
        out = self.b(r, "check", ok=False)
        self.assertIn("unknown node 'ghost'", out)
        self.assertNotIn("runtime pin", out)

    def test_local_provider_without_binding_is_not_schedule_evidence(self):
        r = self.init("alice")
        self.b(r, "set", "root", "runtime=codex")
        self.b(r, "schedule", "add", "tick", "--when", "hourly", "--brief", "Check work.")
        local_path = r / ".brain" / "local.json"
        local = json.loads(local_path.read_text())
        local["schedule_providers"] = {"tick": "claude"}
        local_path.write_text(json.dumps(local))
        self.assertNotIn("runtime pin", self.b(r, "check"))
        # Also leave unrelated providers alone when changing a single binding.
        self.b(r, "schedule", "add", "other", "--when", "hourly", "--brief", "Check work.")
        self.b(r, "schedule", "bind", "other", "host-7", "--provider", "claude")
        self.b(r, "schedule", "bind", "tick", "host-8", "--provider", "codex")
        self.b(r, "schedule", "unbind", "tick")
        self.assertEqual(json.loads(local_path.read_text())["schedule_providers"], {"other": "claude"})

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
