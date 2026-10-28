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
        first = self.git("rev-parse", "HEAD", cwd=bob / "core").strip()
        self.assertFalse((bob / ".brain/core-current").exists())  # a plain core before its first upgrade
        (src / "README.md").write_text((src / "README.md").read_text() + "\nUPGRADED\n")
        self.git("commit", "-qam", "Improve the README", cwd=src)
        out = self.b(bob, "upgrade")
        self.assertIn("Improve the README", out)
        self.assertIn("UPGRADED", (bob / "core" / "README.md").read_text())
        self.assertIn("Upgrade core", self.git("log", "-1", "--format=%s", cwd=bob))
        # first-install migration: both the plain core's release and the new one are pinned releases
        second = self.git("rev-parse", "HEAD", cwd=bob / "core").strip()
        self.assertEqual(os.readlink(bob / ".brain/core-current"), os.path.join("cores", second))
        self.assertEqual(os.readlink(bob / ".brain/core-previous"), os.path.join("cores", first))
        self.assertIn("UPGRADED", (bob / ".brain/cores" / second / "README.md").read_text())
        self.assertNotIn("UPGRADED", (bob / ".brain/cores" / first / "README.md").read_text())
        self.assertNotIn("core", self.git("status", "--porcelain", cwd=bob))
        probe = "import sys; print(sys.modules['brain'].__file__)"
        t = subprocess.run([sys.executable, "-c", f"import runpy; runpy.run_path({str(cmd)!r}, run_name='p'); {probe}"],
                           cwd=self.tmp, env=env, capture_output=True, text=True)
        self.assertTrue(t.stdout.strip().startswith(str((bob / ".brain/cores" / second).resolve())), t.stderr)
        t = subprocess.run([str(cmd), "tree"], cwd=self.tmp, env=env, capture_output=True, text=True)
        self.assertIn("root: Bob", t.stdout, t.stderr)
        self.assertIn("up to date", self.b(bob, "upgrade"))
        # rollback swaps back and points the staged core and gitlink at the previous release
        self.assertIn("rolled back", self.b(bob, "upgrade", "--rollback"))
        self.assertEqual(os.readlink(bob / ".brain/core-current"), os.path.join("cores", first))
        self.assertEqual(os.readlink(bob / ".brain/core-previous"), os.path.join("cores", second))
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=bob / "core").strip(), first)
        self.assertIn("Roll back core", self.git("log", "-1", "--format=%s", cwd=bob))
        self.assertIn("Improve the README", self.b(bob, "upgrade"))
        self.assertEqual(os.readlink(bob / ".brain/core-current"), os.path.join("cores", second))
        # a release whose launcher fails never activates, and the staged launcher is restored
        (src / "bin/brain").write_text("raise SystemExit('broken launcher')\n")
        self.git("commit", "-qam", "Break the launcher", cwd=src)
        self.assertIn("staged core restored to", self.b(bob, "upgrade", ok=False))
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=bob / "core").strip(), second)
        self.assertEqual(os.readlink(bob / ".brain/core-current"), os.path.join("cores", second))
        t = subprocess.run([str(cmd), "tree"], cwd=self.tmp, env=env, capture_output=True, text=True)
        self.assertIn("root: Bob", t.stdout, t.stderr)
        self.git("revert", "--no-edit", "HEAD", cwd=src)

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

    def test_m7_native_upgrade_advances_core_with_blocked_job_and_retained_worktree(self):
        src = self.core_repo()
        self.install("--operator", "sample", "--title", "Sample", "--tz", "UTC", "--no-usage",
                     "--core-url", str(src), "--bin-dir", str(self.tmp / "bin"))
        root = self.tmp / "Brain"
        home = self.repo(self.tmp / "project")
        self.b(root, "new", "project", "--title", "Project", "--home", str(home))
        self.b(root, "wt", "add", "project", "parked")
        self.b(root, "handback", "project", "--branch", "parked", "--state", "blocked",
               "--summary", "Fixture job is blocked", "--lessons", "none: fixture")
        ledger = root / "nodes/project/job-ledger.md"
        before = ledger.read_bytes()
        worktree = json.loads((root / ".brain/local.json").read_text())["nodes"]["project"]["worktrees"]["parked"]
        (src / "README.md").write_text((src / "README.md").read_text() + "\nUPGRADED\n")
        self.git("commit", "-qam", "Fixture core update", cwd=src)
        head = self.git("rev-parse", "HEAD", cwd=src).strip()
        output = self.b(root, "upgrade")
        self.assertIn("2 open jobs/worktrees idle; not affected", output)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=root / "core").strip(), head)
        self.assertIn("UPGRADED", (root / "core/README.md").read_text())
        self.assertEqual(ledger.read_bytes(), before)
        self.assertTrue(Path(worktree["path"]).is_dir())

    def test_automatic_task_migration_preserves_jobs_and_explicit_backfill_links(self):
        m = core_loader.SourceFileLoader('brain_upgrade_migration', str(CORE / 'bin/brain')).load_module()
        root = self.init('demo')
        inst = m.Instance(root)
        node = inst.node('root')
        self.repo(root)
        self.git('checkout', '-qb', 'job/example', cwd=root)
        node['local']['worktrees'] = {'job/example': {'path': str(root)}}
        with mock.patch.dict(os.environ, self.env, clear=True):
            m.job_started(inst, node, 'job/example', 'codex')
            ledger = root / 'job-ledger.md'
            before = ledger.read_bytes()
            row = m.read_jobs(node)[0]
            key = m.job_key(row)
            backfill = m._owners['cmd_task_migrate']
            self.assertEqual(backfill.migrate(inst, [node])[0]['created'], 1)
            self.assertEqual(ledger.read_bytes(), before)
            tasks = backfill._m_tasks
            records = tasks.read_records(node)
            task = next(iter(records.values()))
            self.assertEqual(task['job'], key)
            self.assertIn('job:' + key, task['provenance'])
            self.assertEqual(task['raw_receipts'][0]['raw'], row)
            self.assertEqual(backfill.migrate(inst, [node])[0]['created'], 0)
            self.assertEqual(ledger.read_bytes(), before)
            self.assertEqual(backfill.backfill(inst, node, apply=True), [])
            self.assertEqual(m.read_jobs(node)[0]['Task'], task['id'])
            linked = ledger.read_bytes()
            self.assertEqual(backfill.backfill(inst, node, apply=True), [])
            self.assertEqual(ledger.read_bytes(), linked)
            self.assertEqual(tasks.read_records(node), records)



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
    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(os.environ, self.env)
        patch.start()
        self.addCleanup(patch.stop)

    def module(self):
        return core_loader.SourceFileLoader("r2_upgrade", str(CORE / "bin/brain")).load_module()

    def upgrade_fixture(self, m):
        root = self.repo(self.tmp / "instance", {
            ".gitmodules": '[submodule "core"]\npath = core\n',
            "instance.md": '---\nname: fixture\noperator: fixture\nprincipals: fixture\nruntimes: codex\ntimezone: UTC\n---\n',
            "node.md": '---\nid: root\ntitle: Fixture\n---\n',
        })
        (root / '.brain').mkdir()
        self.repo(root / "core", {"CHANGES.md": "## 2026-01-01: Fixture\n"})
        home = self.repo(self.tmp / "project")
        inst = m.Instance(root)
        inst.local['machine'] = 'fixture'
        n = inst.node('root')
        n['home_path'] = home
        inst.nodes = lambda: [n]
        inst.commit = mock.Mock()
        return inst, n

    def upgrade_attempt(self, m, inst, refused=False, **args):
        mutations = []
        real_run = subprocess.run

        def run(args, **kwargs):
            if "submodule" in args:
                mutations.append(args)
                return subprocess.CompletedProcess(args, 0, "", "")
            return real_run(args, **kwargs)

        output = io.StringIO()
        with mock.patch.object(m, "find_root", return_value=inst.root), \
                mock.patch.object(m, "Instance", return_value=inst), \
                mock.patch.object(m.subprocess, "run", side_effect=run), contextlib.redirect_stdout(output):
            if refused:
                with self.assertRaises(SystemExit) as error:
                    m.cmd_upgrade(argparse.Namespace(**args))
                self.assertIn("requires an idle brain", str(error.exception))
                self.assertIn("Retry brain upgrade when idle", str(error.exception))
                self.assertEqual(mutations, [])
                inst.commit.assert_not_called()
                return str(error.exception)
            m.cmd_upgrade(argparse.Namespace(**args))
        self.assertEqual(len(mutations), 1)
        self.assertIn("core is up to date", output.getvalue())
        return output.getvalue()

    def lease(self, m, **updates):
        row = {"token": "a" * 32, "kind": "heavy", "state": "running",
               "owner": [os.getpid(), m.process_stamp(os.getpid())]}
        row.update(updates)
        path = m.machine_dir() / "machine-care-queue.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([row]))
        return path

    def test_m7_inflight_admission_and_verify_lease_refuse_without_writes(self):
        m = self.module()
        inst, n = self.upgrade_fixture(m)
        for kind in ("heavy", "verify"):
            with self.subTest(kind=kind):
                token = m.machine_wait(kind, inst, n["id"])
                try:
                    path = m.machine_dir() / "machine-care-queue.json"
                    before = path.read_bytes()
                    message = self.upgrade_attempt(m, inst, refused=True)
                    self.assertIn(kind, message)
                    self.assertEqual(path.read_bytes(), before)
                finally:
                    m.machine_release(token)

    def test_m7_live_train_lock_refuses_but_released_lock_is_idle(self):
        import fcntl
        m = self.module()
        inst, n = self.upgrade_fixture(m)
        lock = n["home_path"] / ".git/brain-train.lock"
        with lock.open("w+") as held:
            held.write("stale text does not decide lock ownership")
            held.flush()
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertIn("train", self.upgrade_attempt(m, inst, refused=True))
        before = lock.read_bytes()
        self.upgrade_attempt(m, inst)
        self.assertEqual(lock.read_bytes(), before)

    def test_live_node_gate_refuses_without_admission_or_train_lock(self):
        import fcntl
        m = self.module()
        inst, n = self.upgrade_fixture(m)
        path = m.gate_path(inst.root, n["id"])
        path.parent.mkdir(parents=True)
        with path.open("w+") as held:
            held.write("metadata is not ownership")
            held.flush()
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            before = path.read_bytes()
            self.assertIn("node gate", self.upgrade_attempt(m, inst, refused=True))
            self.assertEqual(path.read_bytes(), before)
        self.upgrade_attempt(m, inst)
        self.assertEqual(path.read_bytes(), before)

    def pin_fixture_core(self, m, inst):
        """Mark the fixture's staged core as an installed, current release (dirs only, no install)."""
        sha = self.git("rev-parse", "HEAD", cwd=inst.root / "core").strip()
        (inst.root / ".brain/cores" / sha).mkdir(parents=True)
        (inst.root / ".brain/cores" / (sha + ".pin")).touch()
        os.symlink(os.path.join("cores", sha), inst.root / ".brain/core-current")

    def test_pinned_guard_ignores_foreign_node_gate_but_refuses_its_own(self):
        import fcntl
        m = self.module()
        inst, n = self.upgrade_fixture(m)
        other = {**n, "id": "other", "home_path": self.repo(self.tmp / "other-home"), "local": {}}
        other["dir"] = self.tmp / "other-node"
        other["dir"].mkdir()
        inst.nodes = lambda: [n, other]
        url = "https://example.invalid/core.git"
        self.git("remote", "add", "origin", url, cwd=inst.root / "core")
        self.git("remote", "add", "origin", "git@example.invalid:core", cwd=n["home_path"])

        def held(node):
            path = m.gate_path(inst.root, node)
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("w+")
            self.addCleanup(handle.close)
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle

        foreign = held("other")
        # A plain core keeps the whole-brain guard: every process runs from the directory it rewrites.
        self.assertIn("other: in-flight node gate", self.upgrade_attempt(m, inst, refused=True))
        self.pin_fixture_core(m, inst)
        self.assertEqual(m.own_node(inst), "root")  # the node whose home ships this core
        output = self.upgrade_attempt(m, inst)
        self.assertIn("open jobs/worktrees idle; not affected", output)
        own = held("root")
        message = self.upgrade_attempt(m, inst, refused=True)
        self.assertIn("root: in-flight node gate", message)
        self.assertNotIn("other:", message)
        own.close()
        self.upgrade_attempt(m, inst)
        # An explicit --node names the own node; the other gate then refuses alone.
        self.assertIn("other: in-flight node gate", self.upgrade_attempt(m, inst, refused=True, node="other"))
        foreign.close()
        # No node can be named as the core's owner: keep the whole-brain guard.
        self.git("remote", "set-url", "origin", "https://example.invalid/elsewhere.git", cwd=n["home_path"])
        self.assertIsNone(m.own_node(inst))
        held("other")
        self.assertIn("other: in-flight node gate", self.upgrade_attempt(m, inst, refused=True))

    def test_unknown_node_gate_inventory_refuses_before_upgrade(self):
        m = self.module()
        inst, n = self.upgrade_fixture(m)
        for kind in ("symlink", "hardlink", "directory", "fifo"):
            with self.subTest(kind=kind):
                path = m.gate_path(inst.root, n["id"])
                path.parent.mkdir(parents=True, exist_ok=True)
                other = path.parent / "other"
                if kind == "symlink":
                    other.write_text("keep")
                    path.symlink_to(other)
                elif kind == "hardlink":
                    other.write_text("keep")
                    os.link(other, path)
                elif kind == "directory":
                    path.mkdir()
                else:
                    os.mkfifo(path)
                try:
                    self.assertIn("node gate", self.upgrade_attempt(m, inst, refused=True))
                finally:
                    if path.is_dir():
                        path.rmdir()
                    else:
                        path.unlink()
                    if other.exists():
                        other.unlink()

    def test_initial_admission_wait_allows_upgrade_but_running_holder_refuses(self):
        m = self.module()
        inst, n = self.upgrade_fixture(m)
        with m.node_gate(inst, n, self.env, 'admission'):
            with m.gate_admission(inst, n['id']):
                before = m.gate_path(inst.root, n['id']).read_bytes()
                self.upgrade_attempt(m, inst)
                self.assertEqual(m.gate_path(inst.root, n['id']).read_bytes(), before)
            self.assertIn('node gate', self.upgrade_attempt(m, inst, refused=True))

    def test_held_dead_pid_is_visibly_stale_and_cannot_hide_surviving_child(self):
        import fcntl
        m = self.module()
        inst, n = self.upgrade_fixture(m)
        dead = subprocess.Popen([sys.executable, '-c', 'pass'], env=self.env)
        dead.wait(timeout=5)
        path = m.gate_path(inst.root, n['id'])
        path.parent.mkdir(parents=True)
        with path.open('w+') as held:
            json.dump(dict(node=n['id'], pid=dead.pid, phase='waiting-for-admission',
                           phase_owner=[os.getpid(), m.process_stamp(os.getpid())]), held)
            held.flush()
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertIn('stale holder pid ' + str(dead.pid), self.upgrade_attempt(m, inst, refused=True))

    def test_uncertain_waiting_phase_remains_busy(self):
        m = self.module()
        inst, n = self.upgrade_fixture(m)
        with m.node_gate(inst, n, self.env, 'uncertain') as env:
            fd = int(env['BRAIN_GATE_FD'])
            with m.gate_admission(inst, n['id']):
                row = m.gate_metadata(fd)
            for changes in ({'phase_owner': [True, 'unknown']}, {'phase_owner': [os.getpid(), 'recycled']},
                            {'pid': None}, {'owner_stamp': 'unknown'}, {'node': 'another'}, {'phase': 'other'}):
                with self.subTest(changes=changes):
                    m.gate_write_metadata(fd, {**row, **changes})
                    self.assertIn('node gate', self.upgrade_attempt(m, inst, refused=True))

    def test_m7_parked_blocked_done_jobs_and_worktrees_upgrade_with_notice(self):
        m = self.module()
        inst, n = self.upgrade_fixture(m)
        rows = [{**dict.fromkeys(m.JOB_COLS, ""), "Job": state, "Started": "2026-01-01 00:00 +00:00"}
                for state in ("parked", "blocked", "done")]
        m.write_jobs(inst, n, rows)
        for row in rows[1:]:
            path = m.handback_file(n, row)
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps({"job": row["Job"], "started": row["Started"], "state": row["Job"]}))
        worktrees = n["local"]["worktrees"] = {}
        for row in rows:
            path = self.tmp / row["Job"]
            path.mkdir()
            worktrees[row["Job"]] = {"path": str(path)}
        before = {p: p.read_bytes() for p in n["dir"].rglob("*") if p.is_file()}
        output = self.upgrade_attempt(m, inst)
        self.assertIn("6 open jobs/worktrees idle; not affected", output)
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        inst.local["auto_upgrade"] = "on"
        calls, real_run = [], subprocess.run

        def run_upgrade(args, **kwargs):
            if "upgrade" in args:
                calls.append(args)
                return subprocess.CompletedProcess(args, 0, output, "")
            return real_run(args, **kwargs)

        with mock.patch.object(m, "core_update", return_value=("a" * 40, "b" * 40)), \
                mock.patch.object(m, "Instance", return_value=inst), mock.patch.object(m, "brain_lock"), \
                mock.patch.object(m.subprocess, "run", side_effect=run_upgrade):
            summary = m.auto_upgrade(inst)
            self.assertIn("core-upgraded: core is up to date", summary)
            self.assertIn("6 open jobs/worktrees idle; not affected", summary)
        self.assertEqual(len(calls), 1)
        self.lease(m, state="queued")
        self.upgrade_attempt(m, inst)  # Waiting for admission is not a running holder.

    def test_m7_dead_pid_and_recycled_pid_leases_are_idle_but_live_child_is_busy(self):
        m = self.module()
        inst, n = self.upgrade_fixture(m)
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait(timeout=10)
        path = self.lease(m, owner=[dead.pid, "departed"])
        before = path.read_bytes()
        self.upgrade_attempt(m, inst)
        self.assertEqual(path.read_bytes(), before)
        self.lease(m, owner=[os.getpid(), "different start time"])
        self.upgrade_attempt(m, inst)
        self.lease(m, owner=[dead.pid, "departed"], child=[os.getpid(), m.process_stamp(os.getpid())])
        self.assertIn("heavy", self.upgrade_attempt(m, inst, refused=True))

    def test_m7_unknown_admission_inventory_and_liveness_refuse(self):
        m = self.module()
        inst, n = self.upgrade_fixture(m)
        path = self.lease(m)
        for bad in ("{", "{}", '[{"state":"running"}]'):
            path.write_text(bad)
            self.assertIn("unreadable", self.upgrade_attempt(m, inst, refused=True))
            self.assertEqual(path.read_text(), bad)
        self.lease(m)
        with mock.patch.object(m, "process_stamp", return_value="unknown"):
            self.upgrade_attempt(m, inst, refused=True)
        with mock.patch.object(Path, "read_text", side_effect=PermissionError):
            self.assertTrue(m.open_jobs_anywhere(inst))

    def test_m7_dangling_ledger_link_and_invalid_node_inventory_refuse(self):
        m = self.module()
        inst, n = self.upgrade_fixture(m)
        (n["dir"] / m.JOBS_FILE).symlink_to(self.tmp / "missing-ledger")
        self.assertIn("unreadable job ledger", self.upgrade_attempt(m, inst, refused=True))
        (n["dir"] / m.JOBS_FILE).unlink()
        n["local"] = None
        self.assertIn("unreadable", self.upgrade_attempt(m, inst, refused=True))

    def test_m7_auto_upgrade_rechecks_live_lease_after_instance_lock(self):
        m = self.module()
        inst, n = self.upgrade_fixture(m)
        inst.local["auto_upgrade"] = "on"
        case = self

        class Lock:
            def __init__(self, unused): pass
            def __enter__(self): case.lease(m, kind="verify")
            def __exit__(self, *exc): pass

        with mock.patch.object(m, "core_update", return_value=("a" * 40, "b" * 40)), \
                mock.patch.object(m, "Instance", return_value=inst), mock.patch.object(m, "brain_lock", Lock), \
                mock.patch.object(m.subprocess, "run", wraps=subprocess.run) as run:
            self.assertIsNone(m.auto_upgrade(inst))
        self.assertFalse(any("upgrade" in c.args[0] for c in run.call_args_list))

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


class PinnedCoreReleases(Scenario):
    """Versioned releases on owned copies: install, swap, pinned imports, prune and rollback."""
    core_repo = InstallAndUpgrade.core_repo

    def staged(self, value):
        src = self.core_repo()
        (src / "lib/brain/_pin_probe.py").write_text(f"VALUE = {value!r}\n")
        self.git("add", "-A", cwd=src)
        self.git("commit", "-qm", value, cwd=src)
        root = self.tmp / "Brain"
        (root / ".brain").mkdir(parents=True)
        self.git("clone", "-q", str(src), str(root / "core"), cwd=self.tmp)
        return src, root

    def advance(self, src, root, value):
        (src / "lib/brain/_pin_probe.py").write_text(f"VALUE = {value!r}\n")
        self.git("commit", "-qam", value, cwd=src)
        self.git("pull", "-q", "--ff-only", cwd=root / "core")
        return self.git("rev-parse", "HEAD", cwd=root / "core").strip()

    def test_install_versioned_release_with_checked_hash_bytecode_and_atomic_swap(self):
        m = core_loader.SourceFileLoader("pinned_install", str(CORE / "bin/brain")).load_module()
        releases = m._owners["cmd_upgrade"]._m_core_releases
        src, root = self.staged("A")
        sha = self.git("rev-parse", "HEAD", cwd=root / "core").strip()
        self.assertIsNone(releases.current(root))
        release = releases.install(root, sha)
        self.assertEqual(release, root / ".brain/cores" / sha)
        self.assertTrue(releases.pin_path(release).is_file())
        self.assertFalse((release / ".git").exists())
        # Ask the launcher's interpreter where it reads bytecode (Apple's build has its own prefix).
        where = subprocess.run([sys.executable, "-c", "import importlib.util, sys; "
                                "print(importlib.util.cache_from_source(sys.argv[1]))", str(release / "lib/brain/cli.py")],
                               capture_output=True, text=True, check=True,
                               env={k: v for k, v in os.environ.items() if k not in releases.BYTECODE_ENV})
        pyc = Path(where.stdout.strip())
        self.assertEqual(int.from_bytes(pyc.read_bytes()[4:8], "little"), 0b11)  # checked hash, not mtime
        self.assertEqual(releases.install(root, sha), release)  # an installed release is reused, not rewritten
        self.assertIsNone(releases.swap(root, sha))
        self.assertEqual(os.readlink(root / ".brain/core-current"), os.path.join("cores", sha))
        second = self.advance(src, root, "B")
        (root / "core/README.md").write_text("uncommitted local edit\n")
        releases.install(root, second)
        self.assertNotIn("uncommitted", (root / ".brain/cores" / second / "README.md").read_text())
        self.git("checkout", "-q", "--", "README.md", cwd=root / "core")
        with mock.patch.object(releases.os, "replace", wraps=os.replace) as rename:
            self.assertEqual(releases.swap(root, second), sha)
        self.assertEqual([Path(c.args[1]).name for c in rename.call_args_list], ["core-current", "core-previous"])
        self.assertEqual((releases.current(root), releases.previous(root)), (second, sha))
        self.assertEqual(list((root / ".brain").glob(".core-*")), [])
        self.assertEqual(list((root / ".brain/cores").glob(".partial-*")), [])
        third = self.advance(src, root, "C")
        releases.install(root, third)
        real_point = releases._point

        def interrupted(where, name, target):
            if name == releases.PREVIOUS:
                raise OSError("interrupted")
            real_point(where, name, target)

        with mock.patch.object(releases, "_point", side_effect=interrupted):
            with self.assertRaises(OSError):
                releases.swap(root, third)
        # Interrupted between the renames: the new release runs, the older rollback target survives.
        self.assertEqual((releases.current(root), releases.previous(root)), (third, sha))
        (src / "bin/brain").write_text("raise SystemExit('broken launcher')\n")
        self.git("commit", "-qam", "broken launcher", cwd=src)
        self.git("pull", "-q", "--ff-only", cwd=root / "core")
        broken = self.git("rev-parse", "HEAD", cwd=root / "core").strip()
        with self.assertRaises(ValueError):
            releases.install(root, broken)
        self.assertFalse((root / ".brain/cores" / broken).exists())
        self.assertFalse(releases.pin_path(root / ".brain/cores" / broken).exists())
        self.assertEqual(releases.current(root), third)  # a failed install never swaps

    def test_process_started_before_swap_keeps_its_release_and_blocks_prune(self):
        m = core_loader.SourceFileLoader("pinned_process", str(CORE / "bin/brain")).load_module()
        releases = m._owners["cmd_upgrade"]._m_core_releases
        src, root = self.staged("A")
        first = self.git("rev-parse", "HEAD", cwd=root / "core").strip()
        releases.install(root, first)
        releases.swap(root, first)
        probe = ("import runpy, sys\n"
                 "runpy.run_path(sys.argv[1], run_name='probe')\n"
                 "print('ready', flush=True)\n"
                 "sys.stdin.readline()\n"
                 "lazy = sys.modules['brain.common'].LazyModule('brain', '_pin_probe')\n"
                 "print(lazy.VALUE, sys.modules['brain._pin_probe'].__file__, flush=True)\n")
        env = {k: v for k, v in self.env.items() if k != "BRAIN_NO_REEXEC"}
        child = subprocess.Popen([sys.executable, "-c", probe, str(root / "core/bin/brain")], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=self.tmp)
        try:
            self.assertEqual(child.stdout.readline().strip(), "ready", child.stderr.read() if child.poll() else "")
            for value in ("B", "C"):
                sha = self.advance(src, root, value)
                releases.install(root, sha)
                releases.swap(root, sha)
            # first is neither current nor previous now, but the running process still pins it
            self.assertEqual(releases.prune(root), ([], [first]))
            self.assertTrue((root / ".brain/cores" / first / "lib/brain").is_dir())
            out, err = child.communicate("go\n", timeout=60)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
        value, path = out.split()
        self.assertEqual(value, "A", err)
        self.assertTrue(path.startswith(str((root / ".brain/cores" / first).resolve())), path)
        self.assertEqual(releases.prune(root), ([first], []))
        self.assertFalse((root / ".brain/cores" / first).exists())
        self.assertFalse(releases.pin_path(root / ".brain/cores" / first).exists())
        self.assertEqual(sorted(p.name for p in (root / ".brain/cores").iterdir() if p.is_dir()),
                         sorted([releases.current(root), releases.previous(root)]))

    def test_rollback_swaps_back_and_refuses_without_previous(self):
        m = core_loader.SourceFileLoader("pinned_rollback", str(CORE / "bin/brain")).load_module()
        releases = m._owners["cmd_upgrade"]._m_core_releases
        src, root = self.staged("A")
        first = self.git("rev-parse", "HEAD", cwd=root / "core").strip()
        with self.assertRaises(ValueError):
            releases.rollback(root)  # a plain core has nothing to roll back to
        releases.install(root, first)
        releases.swap(root, first)
        with self.assertRaises(ValueError):
            releases.rollback(root)
        second = self.advance(src, root, "B")
        releases.install(root, second)
        releases.swap(root, second)
        self.assertEqual(releases.rollback(root), (second, first))
        self.assertEqual((releases.current(root), releases.previous(root)), (first, second))
        self.assertEqual(releases.rollback(root), (first, second))  # rolling forward again is the same swap
        self.assertEqual((releases.current(root), releases.previous(root)), (second, first))

    def test_gate_command_keeps_its_release_across_exec_and_nested_commands(self):
        m = core_loader.SourceFileLoader("pinned_gate", str(CORE / "bin/brain")).load_module()
        releases = m._owners["cmd_upgrade"]._m_core_releases
        root = self.init("demo")
        core = root / "core"
        shutil.rmtree(core / ".git", ignore_errors=True)
        (core / "lib/brain/_pin_probe.py").write_text("VALUE = 'A'\n")
        self.git("init", "-q", "-b", "main", cwd=core)
        self.git("add", "-A", cwd=core)
        self.git("commit", "-qm", "A", cwd=core)
        first = self.git("rev-parse", "HEAD", cwd=core).strip()
        releases.install(root, first)
        releases.swap(root, first)
        launcher = core / "bin/brain"
        probe_nested = ("import runpy, sys; runpy.run_path(sys.argv[1], run_name='nested'); "
                        "import brain._pin_probe as p, brain.common as c; print(p.VALUE, c.STABLE_ENTRY_POINT)")
        probe = ("import os, subprocess, sys\n"
                 "print(os.environ.get('BRAIN_CORE_RELEASE'), flush=True)\n"
                 "sys.stdin.readline()\n"
                 f"r = subprocess.run([sys.executable, '-c', {probe_nested!r}, sys.argv[1]], capture_output=True, text=True)\n"
                 "print(r.stdout.strip() or r.stderr.strip()[-300:], flush=True)\n")
        gate = subprocess.Popen([sys.executable, str(launcher), "gate-lock", "--label", "pin", "--",
                                 sys.executable, "-u", "-c", probe, str(launcher)], cwd=root, env=self.env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            line = gate.stdout.readline().strip()
            self.assertEqual(Path(line), (root / ".brain/cores" / first).resolve(), gate.stderr.read() if gate.poll() else "")
            for value in ("B", "C"):
                (core / "lib/brain/_pin_probe.py").write_text(f"VALUE = {value!r}\n")
                self.git("commit", "-qam", value, cwd=core)
                sha = self.git("rev-parse", "HEAD", cwd=core).strip()
                releases.install(root, sha)
                releases.swap(root, sha)
            # The gate's launcher exec'd away; its inherited pin still protects the release.
            self.assertEqual(releases.prune(root), ([], [first]))
            out, err = gate.communicate("go\n", timeout=120)
        finally:
            if gate.poll() is None:
                gate.kill()
                gate.wait()
        value, stable = out.split()[-2:]
        self.assertEqual(value, "A", out + err)
        self.assertEqual(Path(stable), launcher)  # services register the launcher, never a release
        self.assertEqual(releases.prune(root), ([first], []))
