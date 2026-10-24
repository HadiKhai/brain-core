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


class InstallAndUpgrade(Scenario):
    def core_repo(self):
        src = self.tmp / "core-src"
        shutil.copytree(CORE, src, ignore=shutil.ignore_patterns(".git", "__pycache__", ".DS_Store", "node_modules", "*.tsbuildinfo"))
        self.git("init", "-q", "-b", "main", cwd=src)
        # The source must stay immutable while local submodule clones copy objects.
        # Larger UI assets can trigger background packing after the initial commit.
        self.git("config", "maintenance.auto", "false", cwd=src)
        self.git("config", "gc.auto", "0", cwd=src)
        self.git("add", "-A", cwd=src)
        self.git("commit", "-qm", "core", cwd=src)
        return src

    def install(self, *args):
        r = subprocess.run(["sh", str(CORE / "install.sh"), "--yes", *args], cwd=self.tmp,
                           env={**self.env, "SHELL": "/bin/zsh"},
                           capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if r.returncode:
            self.fail(f"install.sh failed:\n{r.stdout}\n{r.stderr}")
        return r.stdout + r.stderr

    def test_install_upgrade_and_second_machine(self):
        src = self.core_repo()
        bob = self.tmp / "Brain"
        out = self.install("--operator", "bob", "--title", "Bob", "--runtimes", "claude,codex", "--tz", "UTC",
                           "--core-url", str(src), "--bin-dir", str(self.tmp / "bin"))
        self.assertIn("✓ checks pass", out)
        self.assertIn("path = core", (bob / ".gitmodules").read_text())
        cmd = self.tmp / "bin" / "brain"
        self.assertTrue(cmd.is_symlink())
        env = {**self.env}
        env.pop("BRAIN_NO_REEXEC")
        t = subprocess.run([str(cmd), "tree"], cwd=self.tmp, env=env, capture_output=True, text=True)
        self.assertIn("root: Bob", t.stdout, t.stderr)
        rc = self.tmp / ".zshrc"
        self.assertIn('export PATH="$HOME/bin:$PATH"', rc.read_text())
        self.assertIn("Brain is at `~/Brain`", (self.tmp / ".claude" / "CLAUDE.md").read_text())
        self.assertNotIn("to PATH)", out)
        self.assertIn("Open a new terminal", out)
        if sys.platform == "darwin":  # the usage poller is installed by default (tests never load it)
            agent = self.tmp / "Library/LaunchAgents/org.brain.usage.plist"
            self.assertIn(str(bob / "core/bin/brain"), agent.read_text())
            self.assertIn("not loaded", out)

        # a second brain on this machine: its own folder and config folder, the same command
        self.install("--dir", "~/Work", "--name", "work", "--operator", "bob", "--title", "Work", "--runtimes",
                     "claude", "--tz", "UTC", "--core-url", str(src), "--bin-dir", str(self.tmp / "bin"),
                     "--global", "claude=~/.claude-work")
        self.assertEqual(os.path.realpath(cmd), str((bob / "core" / "bin" / "brain").resolve()))
        self.assertNotIn("Work", (self.tmp / ".claude" / "CLAUDE.md").read_text())
        self.assertIn("`brain -b work`", (self.tmp / ".claude-work" / "CLAUDE.md").read_text())
        t = subprocess.run([str(cmd), "-b", "work", "tree"], cwd=self.tmp, env=env, capture_output=True, text=True)
        self.assertIn("root: Work", t.stdout, t.stderr)
        t = subprocess.run([str(cmd), "tree"], cwd=self.tmp, env=env, capture_output=True, text=True)
        self.assertIn("root: Bob", t.stdout, t.stderr)
        self.assertIn("another Brain: --dir",
                      subprocess.run(["sh", str(CORE / "install.sh"), "--yes", "--title", "X", "--core-url", str(src)],
                                     cwd=self.tmp, env=self.env, capture_output=True, text=True).stderr)

        # a sealed node, so the second machine has a home to bind
        self.b(bob, "principal", "add", "team")
        code = self.repo(self.tmp / "code")
        self.b(bob, "new", "code", "--title", "Code", "--owners", "team", "--home", str(code))

        # upgrade: a new core commit reaches the instance
        (src / "README.md").write_text((src / "README.md").read_text() + "\nUPGRADED\n")
        self.git("commit", "-qam", "Improve the README", cwd=src)
        out = self.b(bob, "upgrade")
        self.assertIn("Improve the README", out)
        self.assertIn("UPGRADED", (bob / "core" / "README.md").read_text())
        self.assertIn("Upgrade core", self.git("log", "-1", "--format=%s", cwd=bob))
        self.assertIn("up to date", self.b(bob, "upgrade"))

        # second machine: same Brain, its own bindings
        bob2 = self.tmp / "Brain2"
        out = self.install("--from", str(bob), "--dir", str(bob2), "--home", f"code={code}",
                           "--bin-dir", str(self.tmp / "bin2"), "--no-modify-path")
        self.assertIn("✓ checks pass", out)
        self.assertIn("UPGRADED", (bob2 / "core" / "README.md").read_text())
        self.assertEqual(json.loads((bob2 / ".brain" / "local.json").read_text())["nodes"]["code"]["home"], str(code))
        self.assertIn("coordinator MISSING", self.b(bob2, "machine"))

        bob3 = self.tmp / "Brain3"
        out = self.install("--from", str(bob), "--dir", str(bob3), "--bin-dir", str(self.tmp / "bin"))
        self.assertIn("code: home not set", out)
        self.assertNotIn("bin2", rc.read_text())
        self.assertEqual(rc.read_text().count("# brain"), 1)
        self.assertIn("home not set on this machine", self.b(bob3, "check", ok=False))



class ReleaseNotes(Scenario):
    def test_shipped_changes_entry_ids_are_unique(self):
        m = core_loader.SourceFileLoader('brain_unique_changes', str(CORE / 'bin/brain')).load_module()
        ids = [entry['id'] for entry in m.changes_entries((CORE / 'CHANGES.md').read_text())]
        self.assertTrue(ids)
        self.assertEqual(len(ids), len(set(ids)))

    def test_upgrade_notices_have_a_dated_heading(self):
        notes = (CORE / "CHANGES.md").read_text()
        module = core_loader.SourceFileLoader("brain_release_notes", str(CORE / "bin/brain")).load_module()
        entries = module.changes_entries(notes)
        self.assertTrue(entries)
        preamble = notes[:notes.index("\n## " + entries[0]["id"])]
        self.assertNotIn("\nFor agents:", preamble)
        self.assertNotIn("\nAfter upgrading:", preamble)


class R2VersionAndUpgrade(Scenario):
    def module(self):
        return core_loader.SourceFileLoader("r2_upgrade", str(CORE / "bin/brain")).load_module()

    def test_m6_cached_version_never_fetches_and_labels_unknown_diverged(self):
        m = self.module()
        core = self.repo(self.tmp / "core")
        self.git("remote", "add", "origin", "https://invalid.example/core.git", cwd=core)
        old = self.git("rev-parse", "HEAD", cwd=core).strip()
        self.git("commit", "--allow-empty", "-qm", "next", cwd=core)
        newer = self.git("rev-parse", "HEAD", cwd=core).strip()
        self.env["GIT_COMMITTER_DATE"] = "2020-01-01T00:00:00+00:00"
        self.git("update-ref", "refs/remotes/origin/main", newer, cwd=core)
        self.git("checkout", "--detach", "-q", old, cwd=core)
        with mock.patch.object(m.subprocess, "run", wraps=subprocess.run) as run:
            info = m.core_version(core, now=2000000000)
        self.assertEqual(info["sha"], old)
        self.assertTrue(info["date"])
        self.assertEqual(info["ref"], "refs/remotes/origin/main")
        self.assertEqual(info["behind"], 1)
        self.assertEqual(info["cache_age_seconds"], 2000000000 - 1577836800)
        self.assertEqual(info["status"], "behind")
        self.assertFalse(any(any(x in str(c) for x in ("fetch", "ls-remote", "pull")) for c in (call.args[0] for call in run.call_args_list)))
        self.git("commit", "--allow-empty", "-qm", "fork", cwd=core)
        self.assertEqual(m.core_version(core)["status"], "diverged")
        self.assertEqual(m.core_version(core, ref="refs/remotes/origin/missing")["status"], "unknown")
        self.git("checkout", "--orphan", "unrelated", cwd=core)
        self.git("commit", "--allow-empty", "-qm", "unrelated history", cwd=core)
        self.assertEqual(m.core_version(core)["status"], "unknown")
        with self.assertRaises(SystemExit):
            m.core_version(core, ref="refs/remotes/origin/main~1")
        self.git("remote", "remove", "origin", cwd=core)
        self.assertEqual(m.core_version(core)["status"], "unknown")

    def test_version_command_reports_installed_identity_as_json(self):
        r = self.init("sample")
        copied = json.loads(self.b(r, "version", "--json"))
        self.assertIsNone(copied["sha"])  # A source copy must never report its enclosing instance's SHA.
        self.git("init", "-q", "-b", "main", cwd=r / "core")
        self.git("commit", "--allow-empty", "-qm", "Synthetic installed core", cwd=r / "core")
        info = json.loads(self.b(r, "version", "--json"))
        self.assertEqual(info["sha"], self.git("rev-parse", "HEAD", cwd=r / "core").strip())
        self.assertRegex(info["date"], r"^\d{4}-\d{2}-\d{2}T")
        self.assertTrue(info["ref"].startswith("refs/remotes/origin/"))
        self.assertIn("cache_age_seconds", info)
        self.assertIn("cache age:", self.b(r, "version"))

    def test_m7_malformed_worktree_records_are_unknown_busy(self):
        m = self.module()
        inst = mock.Mock()
        for worktrees in ({"job": None}, {"job": {}}, ["damaged"]):
            inst.nodes.return_value = [{"id": "sample", "dir": self.tmp, "local": {"worktrees": worktrees}}]
            with mock.patch.object(m, "read_jobs", return_value=[]):
                busy = m.open_jobs_anywhere(inst)
            self.assertTrue(busy)
            self.assertIn("unreadable", str(busy))
        inst.nodes.return_value = [{"id": "sample", "dir": self.tmp, "local": {"worktrees": {"job": {"path": str(self.tmp)}}}}]
        with mock.patch.object(m, "read_jobs", return_value=[]), mock.patch.object(Path, "stat", side_effect=PermissionError):
            self.assertIn("unreadable", str(m.open_jobs_anywhere(inst)))

    def test_m7_busy_and_unreadable_upgrade_refuse_before_mutation(self):
        m = self.module()
        root = self.repo(self.tmp / "instance", {".gitmodules": "[submodule \"core\"]\npath = core\n"})
        core = self.repo(root / "core", {"CHANGES.md": "## 2026-01-01: Base\n\nFor agents: original\n"})
        self.git("remote", "add", "origin", "https://invalid.example/core.git", cwd=core)
        old = self.git("rev-parse", "HEAD", cwd=core).strip()
        (core / "CHANGES.md").write_text("## 2026-01-02: New behavior\n\nFor agents: new rules\n" + (core / "CHANGES.md").read_text())
        self.git("commit", "-qam", "update", cwd=core)
        self.git("update-ref", "refs/remotes/origin/main", "HEAD", cwd=core)
        self.git("checkout", "--detach", "-q", old, cwd=core)
        fake = mock.Mock(root=root)
        for inventory in ([("sample", "job-one")], OSError("unreadable")):
            with mock.patch.object(m, "find_root", return_value=root), mock.patch.object(m, "Instance", return_value=fake), \
                 mock.patch.object(m, "open_jobs_anywhere", side_effect=inventory if isinstance(inventory, Exception) else None,
                                   return_value=inventory), mock.patch.object(m.subprocess, "run", wraps=subprocess.run) as run:
                with self.assertRaises(SystemExit) as err:
                    m.cmd_upgrade(argparse.Namespace())
            message = str(err.exception)
            self.assertIn("idle", message)
            self.assertIn("New behavior", message)
            self.assertIn("job-one" if isinstance(inventory, list) else "unreadable", message)
            self.assertFalse(any("submodule" in str(c) for c in run.call_args_list))
            self.assertEqual(self.git("rev-parse", "HEAD", cwd=core).strip(), old)
        fake.commit.assert_not_called()

    def test_m7_marked_but_unparseable_ledger_refuses_upgrade(self):
        m = self.module()
        root = self.repo(self.tmp / "instance", {".gitmodules": "[submodule \"core\"]\npath = core\n"})
        self.repo(root / "core", {"CHANGES.md": "## 2026-01-01: Fixture\n"})
        ledger = root / "job-ledger.md"
        ledger.write_text(m.JOBS_MARK + "\nDamaged table for an active import job\n")
        n = {"id": "sample", "dir": root, "local": {}}
        fake = mock.Mock(root=root)
        fake.nodes.return_value = [n]
        self.assertEqual(m.read_jobs(n), [])  # The permissive reader alone cannot establish idle.
        mutations = []
        real_run = subprocess.run
        def run(args, **kwargs):
            if "submodule" in args:
                mutations.append(args)
                return subprocess.CompletedProcess(args, 0, "", "")
            return real_run(args, **kwargs)
        with mock.patch.object(m, "find_root", return_value=root), mock.patch.object(m, "Instance", return_value=fake), \
             mock.patch.object(m.subprocess, "run", side_effect=run):
            with self.assertRaises(SystemExit) as refused:
                m.cmd_upgrade(argparse.Namespace())
        self.assertIn("unreadable job ledger", str(refused.exception))
        self.assertIn("idle", str(refused.exception))
        self.assertEqual(mutations, [])
        fake.commit.assert_not_called()
        header = "| Job | Started | Ended |\n| --- | --- | --- |\n"
        ledger.write_text(m.JOBS_MARK + "\n" + header)
        self.assertEqual(m.open_jobs_anywhere(fake), [])  # A structured empty ledger is idle.
        for damaged in ("| Job | Started | Ended |\n", header + "import:active | unreadable row\n",
                        header + "| import:active | missing-end-cell |\n"):
            ledger.write_text(m.JOBS_MARK + "\n" + damaged)
            self.assertIn("unreadable job ledger", str(m.open_jobs_anywhere(fake)))

    def test_m6_promisor_version_suppresses_descendant_fetches(self):
        root = self.tmp / "instance"
        core = self.repo(root / "core")
        (root / "instance.md").write_text("fixture\n")
        (root / "node.md").write_text("fixture\n")
        self.git("remote", "add", "origin", str(self.tmp / "absent.git"), cwd=core)
        self.git("config", "remote.origin.promisor", "true", cwd=core)
        ref = core / ".git/refs/remotes/origin/main"
        ref.parent.mkdir(parents=True)
        ref.write_text("f" * 40 + "\n")
        trace = self.tmp / "git.trace"
        env = {**self.env, "BRAIN_ROOT": str(root), "GIT_TRACE": str(trace), "GIT_NO_LAZY_FETCH": "0"}
        result = subprocess.run([sys.executable, str(CORE / "bin/brain"), "version", "--json"], cwd=root,
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "unknown")
        self.assertTrue(trace.exists())
        self.assertNotRegex(trace.read_text(), r"(?:run_command:|built-in:|exec:).*\b(?:fetch|upload-pack)\b")


if __name__ == "__main__":
    unittest.main()
