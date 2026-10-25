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


import test_hosts as _test_hosts
import test_verify as _test_verify
from scenario import Scenario, AutonomyProject, shlex_quote


class Hardening(Scenario):
    """What the second-runtime reviews broke, kept broken-proof."""

    def never(self, r, node):
        nd = next(p_.parent for p_ in r.rglob("node.md") if f"id: {node}\n" in p_.read_text())
        (nd / "never-name.txt").write_text("OldCo\n")
        return nd

    def test_backlog_has_the_same_note_exemption_as_decisions(self):
        r = self.init('fixture')
        self.b(r, 'new', 'notes', '--title', 'Notes')
        nd = self.never(r, 'notes')
        for name in ('decisions.md', 'backlog.md'):
            (nd / name).write_text('Discuss the OldCo never-name rule.\n')
        self.assertIn('guard: clean', self.b(r, 'guard', 'notes'))

    def test_guard_on_a_home_inside_the_brain(self):
        r = self.init("alice")
        self.b(r, "new", "notes", "--title", "Notes")
        nd = self.never(r, "notes")
        (r / ".gitignore").write_text((r / ".gitignore").read_text() + "dist/\n")
        (nd / "dist").mkdir()
        (nd / "dist" / "gen.js").write_text("OldCo\n")                    # ignored: not part of the home
        os.mkfifo(nd / "sources" / "events.pipe")                         # a special file never blocks the scan
        self.assertIn("guard: clean", self.b(r, "guard", "notes"))
        (nd / ".github").mkdir()
        (nd / ".github" / "ci.yml").write_text("run: oldco\n")             # dot-folders are part of it
        (nd / "sources" / "OldCo").mkdir()
        (nd / "sources" / "OldCo" / "clean.txt").write_text("nothing\n")   # a folder's name is too
        out = self.b(r, "guard", "notes", ok=False)
        self.assertIn(".github/ci.yml", out)
        self.assertIn("sources/[…]/clean.txt", out)
        self.assertNotIn("oldco", out.lower())

    def test_an_unreadable_repository_is_never_clean(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "code" / "app")
        self.b(r, "new", "app", "--title", "App", "--home", str(home))
        self.never(r, "app")
        (home / ".git" / "HEAD").rename(home / ".git" / "HEAD.bak")
        r_ = subprocess.run([sys.executable, str(r / "core" / "bin" / "brain"), "guard", "app"], cwd=r, env=self.env,
                            capture_output=True, text=True)
        self.assertEqual(r_.returncode, 2, r_.stdout + r_.stderr)
        self.assertIn("incomplete", r_.stdout)
        (home / ".git" / "HEAD.bak").rename(home / ".git" / "HEAD")
        out = self.b(r, "guard", "app", str(self.tmp / "OldCo-missing"), ok=False)
        self.assertNotIn("oldco", out.lower())

    def test_no_review_brief_into_a_missing_home(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "code" / "app")
        self.b(r, "new", "app", "--title", "App", "--home", str(home))
        self.b(r, "review", "app", "--branch", "main", "--base", "main")
        loc = json.loads((r / ".brain" / "local.json").read_text())
        loc["nodes"]["app"].pop("home")
        (r / ".brain" / "local.json").write_text(json.dumps(loc))
        self.assertIn("isn't on this machine", self.b(r, "review", "app", "--branch", "main", ok=False))

    def test_a_git_hook_that_runs_brain_does_not_deadlock(self):
        r = self.init("alice")
        gi = r / ".gitignore"                       # something for the hook's `check --fix` to repair and commit
        gi.write_text(gi.read_text().replace(".brain/\n", ""))
        self.git("commit", "-qam", "drop .brain/ from .gitignore", cwd=r)
        hook = r / ".git" / "hooks" / "post-commit"
        hook.write_text(f"#!/bin/sh\n'{sys.executable}' '{r / 'core' / 'bin' / 'brain'}' check --fix >/dev/null 2>&1\n")
        hook.chmod(0o755)
        p = subprocess.run([sys.executable, str(r / "core" / "bin" / "brain"), "new", "x", "--title", "X"], cwd=r,
                           env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(".brain/", gi.read_text())    # the hook did run, inside brain's own commit

    def test_a_branch_called_job_keeps_its_row(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "code" / "app")
        self.b(r, "new", "app", "--title", "App", "--home", str(home))
        self.b(r, "wt", "add", "app", "Job")
        self.b(r, "review", "app", "--branch", "Job", "--built-by", "claude")
        self.b(r, "wt", "add", "app", "other")
        self.assertIn("open  Job", self.b(r, "jobs", "app"))
        self.assertIn("| Job | 20", (r / "nodes" / "app" / "job-ledger.md").read_text())



class Hardening3(Scenario):
    """Round three of the second-runtime review."""

    def test_commands_that_change_things_take_turns(self):
        r = self.init("alice")
        for i in range(6):
            self.b(r, "new", f"n{i}", "--title", f"N{i}")
        ps = [subprocess.Popen([sys.executable, str(r / "core" / "bin" / "brain"), "set", f"n{i}", f"coordinator=t{i}"],
                               cwd=r, env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
              for i in range(6)]
        self.assertEqual([p.communicate()[1] and p.returncode or p.returncode for p in ps], [0] * 6)
        loc = json.loads((r / ".brain" / "local.json").read_text())
        self.assertEqual({k: v.get("coordinator") for k, v in loc["nodes"].items() if k.startswith("n")},
                         {f"n{i}": f"t{i}" for i in range(6)})            # no binding lost

    def test_a_dead_holders_permission_does_not_outlive_it(self):
        import fcntl
        r = self.init("alice")
        lock = r / ".brain" / "lock"
        lock.parent.mkdir(exist_ok=True)
        with open(lock, "a+") as fh:                                  # someone else holds the lock now
            fcntl.flock(fh, fcntl.LOCK_EX)
            fh.seek(0); fh.truncate(); fh.write("1"); fh.flush()     # pid 1: alive, but not our ancestor's lock...
            env = {**self.env, "BRAIN_LOCK_HELD": f"{r.resolve()}|999999", "BRAIN_LOCK_WAIT": "2"}  # ...a dead holder
            p = subprocess.run([sys.executable, str(r / "core" / "bin" / "brain"), "new", "x", "--title", "X"], cwd=r,
                               env=env, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(p.returncode, 0)                      # it waited, then gave up: it didn't skip
            self.assertIn("held this instance's lock", p.stderr)
        self.assertNotIn("id: x\n", "".join(q.read_text() for q in r.rglob("node.md")))

    def test_guard_edge_cases(self):
        r = self.init("alice")
        self.b(r, "new", "notes", "--title", "Notes")
        nd = r / "nodes" / "notes"
        (nd / "never-name.txt").write_text("OldCo\n")
        comp = nd / "sources" / "component"
        comp.mkdir(parents=True)
        (comp / "node.md").write_text("# docs for a component, not a brain node\n")
        (comp / "artifact.py").write_text("# from OldCo\n")
        self.assertIn("artifact.py", self.b(r, "guard", "notes", ok=False))   # only real child nodes are skipped
        (comp / "artifact.py").write_text("clean\n")
        locked = nd / "sources" / "locked.txt"
        locked.write_text("OldCo\n")
        locked.chmod(0)
        try:
            p = subprocess.run([sys.executable, str(r / "core" / "bin" / "brain"), "guard", "notes"], cwd=r,
                               env=self.env, capture_output=True, text=True)
            self.assertEqual(p.returncode, 2, p.stdout)                       # unreadable isn't clean
            self.assertIn("couldn't read", p.stdout)
        finally:
            locked.chmod(0o644)
        # a sealed home whose repository can't be read: incomplete, not clean
        self.b(r, "principal", "add", "team")
        sh = self.repo(self.tmp / "code" / "shared")
        self.b(r, "new", "shared", "--title", "Shared", "--owners", "team", "--home", str(sh))
        (sh / ".git" / "HEAD").rename(sh / ".git" / "HEAD.bak")
        p = subprocess.run([sys.executable, str(r / "core" / "bin" / "brain"), "guard", "shared"], cwd=r,
                           env=self.env, capture_output=True, text=True)
        (sh / ".git" / "HEAD.bak").rename(sh / ".git" / "HEAD")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)

    def test_decisions_with_offsets_reach_the_live_view(self):
        r = self.init("alice")
        self.b(r, "new", "a", "--title", "A")
        d = r / "nodes" / "a" / "decisions.md"
        d.write_text(d.read_text() + "\n## 2026-10-19 06:32 +03:00: Use Postgres\nWhy: writes.\n"
                     "\n## 2026-01-10 09:00: An old one, in winter\nWhy: x.\n")
        st = json.loads(self.b(r, "ui", "--json"))
        dec = {x["title"]: x["t"] for x in next(n for n in st["nodes"] if n["id"] == "a")["decisions"]}
        self.assertEqual(dec["Use Postgres"], 1792380720)                  # 03:32Z
        self.assertEqual(dec["An old one, in winter"], 1768035600)         # UTC instance: 09:00Z



class Hardening4(Scenario):
    """Round four of the second-runtime review."""

    def held(self, r):
        import fcntl
        lock = r / ".brain" / "lock"
        lock.parent.mkdir(exist_ok=True)
        fh = open(lock, "a+")
        fcntl.flock(fh, fcntl.LOCK_EX)
        return fh

    def run_b(self, r, *args, env=None):
        return subprocess.run([sys.executable, str(r / "core" / "bin" / "brain"), *args], cwd=r,
                              env={**self.env, "BRAIN_LOCK_WAIT": "2", **(env or {})}, capture_output=True, text=True,
                              input="", timeout=60)

    def test_unreadable_files_in_a_repository_are_not_clean(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "code" / "app", {"a.txt": "OldCo\n"})
        self.b(r, "new", "app", "--title", "App", "--home", str(home))
        (r / "nodes" / "app" / "never-name.txt").write_text("OldCo\n")
        (home / "a.txt").chmod(0)
        try:
            p = self.run_b(r, "guard", "app")
            self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        finally:
            (home / "a.txt").chmod(0o644)

    def test_every_writer_waits_and_readers_do_not(self):
        r = self.init("alice")
        self.b(r, "item", "add", "m1", "--from", "root", "--title", "M", "--kind", "method", "--ref", "x", "--body", "b")
        item = next(r.rglob("library/m1.md"))
        before = item.read_text()
        fh = self.held(r)
        try:
            p = self.run_b(r, "item", "quarantine", "m1")
            self.assertNotEqual(p.returncode, 0)                     # it waited, then gave up...
            self.assertEqual(item.read_text(), before)               # ...without touching the item
            out = self.tmp / "brief.txt"
            self.assertNotEqual(self.run_b(r, "brief", "root", "--task", "x", "--out", str(out)).returncode, 0)
            self.assertFalse(out.exists())
            for args in (["principal", "list"], ["schedule", "list"], ["brief", "root", "--task", "x"], ["show", "root"]):
                self.assertEqual(self.run_b(r, *args).returncode, 0, args)   # reading never waits
        finally:
            fh.close()

    def test_the_stop_hook_never_breaks_a_turn(self):
        r = self.init("alice")
        self.b(r, "new", "a", "--title", "A")
        a = r / "nodes" / "a"
        st = a / "status.md"
        st.write_text(st.read_text().replace(st.read_text().splitlines()[0], "Updated: 2020-01-01 00:00 by a coordinator"))
        (r / ".brain").mkdir(exist_ok=True)
        (r / ".brain" / "hooks.json").write_text("{half a fi")              # half-written by another session
        payload = json.dumps({"cwd": str(a), "session_id": "s1"})
        p = subprocess.run([sys.executable, str(r / "core" / "bin" / "brain"), "hook", "stop"], cwd=a, env=self.env,
                           input=payload, capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('"decision": "block"', p.stdout)                      # the reminder still goes out
        (r / ".brain").chmod(0o500)                                         # nothing can be written there
        try:
            p = subprocess.run([sys.executable, str(r / "core" / "bin" / "brain"), "hook", "stop"], cwd=a, env=self.env,
                               input=json.dumps({"cwd": str(a), "session_id": "s2"}), capture_output=True, text=True,
                               timeout=30)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(p.stderr, "")
        finally:
            (r / ".brain").chmod(0o755)



class Hardening5(Scenario):
    """Round five of the second-runtime review."""

    def test_big_files_and_unreadable_folders(self):
        r = self.init("alice")
        self.b(r, "new", "notes", "--title", "Notes")
        nd = r / "nodes" / "notes"
        (nd / "never-name.txt").write_text("OldCo\n")
        big = nd / "sources" / "big.txt"
        big.write_bytes(b"x" * (2_000_010 - 3) + b"Old" + b"Co and more\n")   # past 2 MB, across a 1 MiB boundary
        self.assertIn("big.txt", self.b(r, "guard", "notes", ok=False))
        big.write_bytes(b"x" * ((1 << 20) - 3) + b"OldCo\n")                     # split exactly at a chunk edge
        self.assertIn("big.txt", self.b(r, "guard", "notes", ok=False))
        big.unlink()
        hidden = nd / "sources" / "hidden"
        hidden.mkdir()
        (hidden / "f.txt").write_text("OldCo\n")
        hidden.chmod(0)
        try:
            p = subprocess.run([sys.executable, str(r / "core" / "bin" / "brain"), "guard", "notes"], cwd=r,
                               env=self.env, capture_output=True, text=True)
            self.assertEqual(p.returncode, 2, p.stdout)                           # couldn't look: not clean
        finally:
            hidden.chmod(0o755)



class Hardening6(Scenario):
    """Round six of the second-runtime review."""

    def test_a_character_split_between_reads_is_still_found(self):
        r = self.init("alice")
        self.b(r, "new", "notes", "--title", "Notes")
        nd = r / "nodes" / "notes"
        (nd / "never-name.txt").write_text("Café\n")
        f = nd / "sources" / "edge.txt"
        for pad in range(1, 6):                                       # é straddles the 1 MiB edge at some pad
            f.write_bytes(b"x" * ((1 << 20) - pad) + "Café\n".encode())
            self.assertIn("edge.txt", self.b(r, "guard", "notes", ok=False), pad)

    def test_seal_commits_nothing_that_names_a_never_word(self):
        r = self.init("alice")
        self.b(r, "principal", "add", "team")
        self.b(r, "new", "legacy", "--title", "OldCo port")
        (r / "nodes" / "legacy" / "never-name.txt").write_text("OldCo\n")
        home = self.tmp / "code" / "legacy"
        p = subprocess.run([sys.executable, str(r / "core" / "bin" / "brain"), "seal", "legacy", "--owners", "team",
                            "--home", str(home)], cwd=r, env=self.env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertNotIn("oldco", (p.stdout + p.stderr).lower())
        self.assertEqual(subprocess.run(["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=home,
                                        capture_output=True).returncode, 1 if (home / ".git").exists() else 128)



class Hardening7(Scenario):
    """Round seven of the second-runtime review."""

    def test_binary_files_greek_words_and_the_word_list_itself(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "code" / "app", {".gitattributes": "*.dat binary\n"})
        self.b(r, "new", "app", "--title", "App", "--home", str(home))
        (r / "nodes" / "app" / "never-name.txt").write_text("OldCo\nΟΣ\n")
        (home / "x.dat").write_bytes(b"\0\1OldCo\0")                       # git calls it binary
        self.assertIn("x.dat", self.b(r, "guard", "app", ok=False))
        (home / "x.dat").unlink()
        for pad in (2, 3):                                                   # Σ on a chunk edge
            (home / "g.txt").write_bytes(b"x" * ((1 << 20) - pad) + "ΟΣ\n".encode())
            self.assertIn("g.txt", self.b(r, "guard", "app", ok=False))
        (home / "g.txt").unlink()
        self.b(r, "principal", "add", "team")
        self.b(r, "new", "legacy", "--title", "Legacy")
        (r / "nodes" / "legacy" / "never-name.txt").write_text("OldCo\n")
        for rel in ("never-name.txt", "./never-name.txt", "sources/../never-name.txt"):
            out = self.b(r, "seal", "legacy", "--owners", "team", "--home", str(self.tmp / "h"), "--move", rel, ok=False)
            self.assertIn("stays on the operator's side", out)
            self.assertTrue((r / "nodes" / "legacy" / "never-name.txt").exists())
            self.assertFalse((self.tmp / "h").exists())                       # refused before anything happened



class Hardening8(Scenario):
    """Ordinary staged edits, deletions and populated submodules from review round eight."""

    def test_runtime_links_visit_only_registered_nodes(self):
        r = self.init("alice")
        impostors = [r / ".git" / "nodes" / "fake", r / "artifacts" / "nodes" / "fake"]
        for folder in impostors:
            folder.mkdir(parents=True)
            (folder / "AGENTS.md").write_text("Unrelated instructions\n")
        self.b(r, "new", "a", "--title", "A")
        self.b(r, "new", "a/b", "--title", "B")
        for folder in impostors:
            self.assertFalse((folder / "CLAUDE.md").exists())
        self.assertTrue((r / "nodes" / "a" / "nodes" / "b" / "CLAUDE.md").is_symlink())

    def test_index_contents_and_tracked_deletions(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "code" / "app", {"a.txt": "clean\n"})
        self.b(r, "new", "app", "--title", "App", "--home", str(home))
        (r / "nodes" / "app" / "never-name.txt").write_text("OldCo\nCafé\n")
        f = home / "a.txt"
        # Binary content and an accented term split across a chunk edge must also be checked in the index.
        f.write_bytes(b"\0OldCo\n" + b"x" * ((1 << 20) - 9) + "Café\n".encode())
        self.git("add", "a.txt", cwd=home)
        f.write_text("clean again\n")
        out = self.b(r, "guard", "app", ok=False)
        self.assertIn("index stage 0:", out)
        self.assertIn("term 1", out)
        self.assertIn("term 2", out)
        self.assertNotIn("OldCo", out)
        f.unlink()
        out = self.b(r, "guard", "app", ok=False)
        self.assertIn("index stage 0:", out)
        self.assertNotIn("incomplete", out)
        self.git("add", "-u", cwd=home)
        self.assertIn("clean", self.b(r, "guard", "app"))
        # A clean unstaged deletion is clean too, while its clean blob remains staged.
        self.git("restore", "--source=HEAD", "--staged", "--worktree", "a.txt", cwd=home)
        f.unlink()
        self.assertIn("clean", self.b(r, "guard", "app"))

    def test_submodules_include_working_index_and_history(self):
        r = self.init("alice")
        lib = self.repo(self.tmp / "lib", {"note.txt": "clean\n"})
        home = self.repo(self.tmp / "code" / "app")
        self.git("submodule", "add", "-q", str(lib), "vendor/lib", cwd=home)
        self.git("commit", "-qam", "add library", cwd=home)
        self.b(r, "new", "app", "--title", "App", "--home", str(home))
        (r / "nodes" / "app" / "never-name.txt").write_text("OldCo\n")
        sub = home / "vendor" / "lib"
        self.assertIn("clean", self.b(r, "guard", "app"))
        (sub / "note.txt").write_text("OldCo\n")
        self.assertIn("vendor/lib/note.txt", self.b(r, "guard", "app", ok=False))
        self.git("add", "note.txt", cwd=sub)
        (sub / "note.txt").write_text("clean\n")
        self.assertIn("index stage 0:", self.b(r, "guard", "app", ok=False))
        self.git("restore", "--staged", "note.txt", cwd=sub)
        self.git("branch", "OldCo-migration", cwd=sub)
        self.assertIn("branch", self.b(r, "guard", "app", ok=False))
        self.git("branch", "-D", "OldCo-migration", cwd=sub)
        self.git("commit", "--allow-empty", "-qm", "OldCo migration", cwd=sub)
        self.assertIn("commit", self.b(r, "guard", "app", ok=False))
        self.git("submodule", "deinit", "-f", "vendor/lib", cwd=home)
        out = self.b(r, "guard", "app", ok=False)
        self.assertIn("incomplete", out)
        self.assertIn("submodule unavailable", out)

    def test_seal_redacts_inherited_owner_summary(self):
        r = self.init("oldco")
        self.b(r, "new", "legacy", "--title", "Legacy")
        (r / "nodes" / "legacy" / "never-name.txt").write_text("OldCo\n")
        self.b(r, "principal", "add", "team")
        out = self.b(r, "seal", "legacy", "--owners", "team", "--home", str(self.tmp / "code" / "legacy"))
        self.assertIn("effective owners", out)
        self.assertNotIn("oldco", out.casefold())



class ImportSafety(Scenario):
    def test_secret_prefixes_do_not_match_project_metadata(self):
        r = self.init("alice")
        for name in ("task-scheduler", "risk-analysis", "disk-usage-tool", "sk-" + "a" * 24):
            h = self.repo(self.tmp / name)
            self.b(r, "adopt", str(h))
            row = json.loads(self.b(r, "scan", str(h), "--json"))[0]
            self.assertEqual(row["name"], name)
            self.assertNotIn("[redacted secret]", row["path"])
            self.b(r, "brief", name, "--import")
            self.b(r, "item", "add", name, "--from", name, "--title", name, "--kind", "lesson",
                   "--ref", name + "/AGENTS.md", "--repo", name, "--commit", "a" * 40,
                   "--proposed", "--body", "Bound memory.")
        for i, body in enumerate(("sk-" + "a" * 24, "ghp_" + "b" * 36, "AKIA" + "A" * 16,
                                  "PASSWORD=fake", "Api_Key: fake")):
            self.b(r, "item", "add", "unsafe-" + str(i), "--from", "root", "--title", "Unsafe",
                   "--kind", "lesson", "--ref", "source", "--repo", "fixture", "--commit", "a" * 40,
                   "--proposed", "--body", body, ok=False)
        self.b(r, "item", "add", "case-sensitive", "--from", "root", "--title", "Safe",
               "--kind", "fact", "--ref", "source", "--repo", "fixture", "--commit", "a" * 40,
               "--proposed", "--body", "SK-" + "a" * 24 + " xsk-" + "b" * 24)

    def test_local_origins_never_enter_committed_node_metadata(self):
        r = self.init("alice")
        for i, origin in enumerate((str(self.tmp / "origin"), "~/origin", "./origin", "../origin",
                                    "file:///tmp/origin", "relative/origin")):
            h = self.repo(self.tmp / ("local-" + str(i)))
            self.git("remote", "add", "origin", origin, cwd=h)
            command = "adopt" if i % 2 else "new"
            args = (str(h),) if command == "adopt" else (h.name, "--title", "Local", "--home", str(h))
            self.b(r, command, *args)
            text = self.git("show", "HEAD:nodes/" + h.name + "/node.md", cwd=r)
            self.assertNotIn("home_url:", text.replace("home_url: none", ""))
            self.assertNotIn(origin, text)
        h = self.repo(self.tmp / "rewritten")
        self.git("config", "url.file:///tmp/private-origin/.insteadOf", "https://example.test/", cwd=h)
        self.git("remote", "add", "origin", "https://example.test/project", cwd=h)
        self.b(r, "adopt", str(h))
        self.assertNotIn("file:", (r / "nodes/rewritten/node.md").read_text())

    def test_ssh_clone_users_survive_without_passwords(self):
        r = self.init("alice")
        for i, (origin, clean) in enumerate((
                ("git@github.com:org/repo", "git@github.com:org/repo"),
                ("ssh://git:fake@host.test/x?private#fragment", "ssh://git@host.test/x"),
                ("user:fake@host.test:org/repo", "user@host.test:org/repo"),
                ("ftp://user:fake@host.test/x", "ftp://host.test/x"))):
            h = self.repo(self.tmp / ("remote-" + str(i)))
            self.git("remote", "add", "origin", origin, cwd=h)
            self.b(r, "new", h.name, "--title", "Remote", "--home", str(h))
            self.assertIn("home_url: " + clean, (r / "nodes" / h.name / "node.md").read_text())
            self.assertEqual(json.loads(self.b(r, "scan", str(h), "--json"))[0]["remotes"], [clean])

    def test_direct_learning_keeps_never_name_compatibility(self):
        r = self.init("alice")
        (r / "never-name.txt").write_text("PrivateFixtureTerm\n")
        args = ("--from", "root", "--title", "Fact", "--kind", "fact", "--ref", "source",
                "--body", "PrivateFixtureTerm")
        self.b(r, "item", "add", "legacy", *args)
        self.assertIn("PrivateFixtureTerm", (r / "library/legacy.md").read_text())
        self.b(r, "item", "add", "proposal", *args, "--proposed", "--repo", "fixture",
               "--commit", "a" * 40, ok=False)



class SmallFixes(Scenario):
    """Briefs written to files, a merge with empty folders, history batches that fit the readers."""

    def test_every_brief_out_stays_out_of_the_home_its_checkouts_and_the_instance(self):
        r, home, wt = _test_verify.WorkerReadyPackets.job(self)
        self.b(r, "specialist", "new", "writer", "--at", "root", "--title", "Writer")
        outside = self.tmp / "briefs"
        outside.mkdir()
        (outside / "into-home").symlink_to(home)
        (home / "precious.txt").write_text("precious\n")
        os.link(home / "precious.txt", outside / "linked.md")
        commands = {"brief": ["brief", "a", "--task", "Fix it"], "small": ["brief", "a", "--small", "--task", "Fix it"],
                    "branch": ["brief", "a", "--branch", "job", "--task", "Fix it"],
                    "review": ["review", "a", "--branch", "job", "--built-by", "claude"],
                    "hire": ["hire", "writer", "a", "--task", "Write it"]}
        for name, args in commands.items():
            args += ["--why-separate", "Synthetic independent concern"]
            for dest, why in ((home / "b.md", "inside the home"), (wt / "b.md", "inside the home"),
                              (outside / "into-home" / "b.md", "inside the home"), (r / "nodes/a/b.md", "inside the instance"),
                              (r / ".brain/b.md", "inside the instance")):
                self.assertIn(why, self.b(r, *args, "--out", str(dest), ok=False), (name, dest))
                self.assertFalse(dest.exists(), (name, dest))
            self.assertIn("a link or not a folder", self.b(r, *args, "--out", str(outside / "linked.md" / "x"), ok=False))
            mine = outside / f"{name}.md"
            self.assertEqual(self.b(r, *args, "--out", str(mine)).strip(), str(mine))
            self.assertEqual(mine.stat().st_mode & 0o777, 0o600)
        self.b(r, "brief", "a", "--task", "Fix it", "--out", str(outside / "linked.md"))
        self.assertEqual((home / "precious.txt").read_text(), "precious\n")  # replaced, never written through
        self.assertIn("Role: worker", (outside / "linked.md").read_text())
        self.assertEqual([p.name for p in outside.iterdir() if p.name.startswith(".brief-")], [])
        self.assertNotIn("b.md", self.git("status", "--porcelain", "--ignored", cwd=home))

    def test_checkouts_with_newlines_and_folders_moved_mid_write_get_nothing(self):
        from core_loader import SourceFileLoader
        r, home, wt = _test_verify.WorkerReadyPackets.job(self)
        odd = self.tmp / "line\nbreak"
        self.git("worktree", "add", "-q", "-b", "odd", str(odd), cwd=home)
        self.assertIn("inside the home", self.b(r, "brief", "a", "--task", "x", "--out", str(odd / "b.md"), ok=False))
        self.assertFalse((odd / "b.md").exists())
        m = SourceFileLoader("brain_moving", str(CORE / "bin/brain")).load_module()
        inst = m.Instance(r)
        n = inst.node("a")
        put, check = m.history_put, m.fd_outside
        first = self.tmp / "first"
        first.mkdir()

        def put_then_move(fd, name, text):  # the checked folder moves into the home, with its open descriptor
            put(fd, name, text)
            os.rename(first, home / "first")
        m.history_put = put_then_move
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            m.write_out(inst, n, first / "brief.md", "text\n")
        self.assertEqual(list((home / "first").iterdir()), [])
        m.history_put, calls, second = put, [], self.tmp / "second"
        second.mkdir()

        def check_then_move(*args):  # moved between the last check before the rename and the rename
            check(*args)
            calls.append(1)
            if len(calls) == 1:
                os.rename(second, home / "second")
        m.fd_outside = check_then_move
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            m.write_out(inst, n, second / "brief.md", "text\n")
        self.assertEqual(list((home / "second").iterdir()), [])  # published, then taken back
        m.fd_outside = check
        third = self.tmp / "third"
        third.mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            m.write_out(inst, n, third / "brief.md", "text\n")
        self.assertEqual((third / "brief.md").read_text(), "text\n")

    def test_node_files_never_follow_a_link_out_of_the_brain(self):
        r, home, wt = _test_verify.WorkerReadyPackets.job(self)
        hb = r / "nodes/a/job-handbacks"
        shutil.rmtree(hb, ignore_errors=True)
        hb.symlink_to(home)
        out = self.b(r, "handback", "--summary", "Synthetic result", "a", "--branch", "job", "--state", "blocked", "--report", "r.md",
                     "--lessons", "none: none", ok=False)
        self.assertIn("out of the brain", out)
        self.assertEqual([p.name for p in home.iterdir() if p.suffix == ".json"], [])
        hb.unlink()
        inbox = r / "nodes/a/inbox.md"
        inbox.unlink()
        inbox.symlink_to(home / "calc.py")
        before = (home / "calc.py").read_text()
        self.b(r, "handback", "--summary", "Synthetic result", "--question", "Synthetic choice?", "a", "--branch", "job", "--state", "question", "--report", "r.md",
               "--lessons", "none: none", ok=False)
        self.assertEqual((home / "calc.py").read_text(), before)

    def test_a_brain_with_a_link_leading_out_refuses_every_change(self):
        r, home, wt = _test_verify.WorkerReadyPackets.job(self)
        (r / "specialists").mkdir(exist_ok=True)
        cases = [(r / "specialists", ["specialist", "new", "writer", "--at", "root", "--title", "Writer"]),
                 (r / "nodes/a/library", ["item", "add", "x1", "--from", "a", "--title", "X", "--kind", "fact", "--ref", "y"]),
                 (r / ".brain/local.json", ["set", "a", "coordinator=private-thread"])]
        for link, args in cases:
            moved = self.tmp / ("was-" + link.name)
            if link.exists():
                os.rename(link, moved)
            target = home / link.name
            if link.suffix:
                target.write_text("{}\n") if not moved.exists() else shutil.copy(moved, target)
            else:
                target.mkdir(exist_ok=True)
            before = sorted(p.name for p in home.rglob("*") if ".git" not in p.parts)
            text = target.read_text() if target.is_file() else None
            link.symlink_to(target)
            out = self.b(r, *args, ok=False)
            self.assertIn("lead out of the brain", out, args)
            self.assertEqual(sorted(p.name for p in home.rglob("*") if ".git" not in p.parts), before, args)
            if text is not None:
                self.assertEqual(target.read_text(), text)
            self.assertIn("a link leading out of the brain", self.b(r, "check", ok=False))
            link.unlink()
            if moved.exists():
                os.rename(moved, link)
        self.b(r, "check")
        self.b(r, "specialist", "new", "writer", "--at", "root", "--title", "Writer")  # links into the brain are fine

    def test_links_inside_the_brain_are_followed_to_what_lies_beneath(self):
        r, home, wt = _test_verify.WorkerReadyPackets.job(self)
        private = home / "private.json"
        private.write_text("{}\n")
        os.rename(r / ".brain", r / "state")  # .brain is a link to a folder inside the brain...
        (r / ".brain").symlink_to(r / "state")
        os.rename(r / "state/local.json", self.tmp / "local.json")
        (r / "state/local.json").symlink_to(private)  # ...that holds a link out
        self.assertIn("lead out of the brain", self.b(r, "set", "root", "coordinator=review-test", ok=False))
        self.assertEqual(private.read_text(), "{}\n")
        (r / "state/local.json").unlink()
        os.rename(self.tmp / "local.json", r / "state/local.json")
        self.b(r, "set", "root", "coordinator=fine")  # links inside the brain alone are fine
        os.rename(r / "nodes", r / "pool")
        (r / "nodes").symlink_to(r / "pool")
        (r / "pool/a/nodes").symlink_to(home)
        before = sorted(p.name for p in home.iterdir())
        self.assertIn("lead out of the brain", self.b(r, "new", "a/b", "--title", "B", ok=False))
        self.assertEqual(sorted(p.name for p in home.iterdir()), before)
        (r / "pool/a/nodes").unlink()
        (r / "pool/a/nodes").symlink_to(r / "pool/a")  # a loop of links inside the brain ends
        from core_loader import SourceFileLoader
        m = SourceFileLoader("brain_loops", str(CORE / "bin/brain")).load_module()
        self.assertEqual(m.outward_links(r), [])

    def test_git_metadata_reports_merges_hard_links_and_the_hook_stay_inside(self):
        r = self.init("alice")
        self.b(r, "new", "a", "--title", "A")
        self.b(r, "new", "b", "--title", "B")
        outside = self.tmp / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel.txt"
        sentinel.write_text("keep\n")

        def refused(args, link, target):
            link.unlink(missing_ok=True)
            link.symlink_to(target)
            self.assertIn("lead out of the brain", self.b(r, *args, ok=False), args)
            self.assertEqual(sentinel.read_text(), "keep\n")
            link.unlink()
        refused(["new", "c", "--title", "C"], r / ".git/COMMIT_EDITMSG", sentinel)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        (r / f"reports/{stamp}-audit").mkdir(parents=True)
        refused(["report", "root", "audit"], r / f"reports/{stamp}-audit/index.md", outside / "missing-report")
        self.assertFalse((outside / "missing-report").exists())
        (r / "nodes/a/custom.txt").write_text("mine\n")
        (r / "nodes/b/sources/merged-a").mkdir(parents=True)
        refused(["merge", "a", "b"], r / "nodes/b/sources/merged-a/custom.txt", outside)
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ["sentinel.txt"])
        shutil.rmtree(r / "nodes/b/sources/merged-a")
        node = r / "nodes/a/node.md"
        os.link(node, outside / "node-copy.md")
        before = (outside / "node-copy.md").read_text()
        self.assertIn("out of the brain", self.b(r, "relabel", "a", "area:x", ok=False))
        self.assertEqual((outside / "node-copy.md").read_text(), before)
        (outside / "node-copy.md").unlink()
        log = r / ".brain/hooks.log"
        log.unlink(missing_ok=True)
        log.symlink_to(sentinel)
        status = r / "nodes/a/status.md"
        status.write_text(status.read_text().replace("Updated:", "Updated: 2020-01-01 00:00 by x\nOld:", 1))
        out = _test_hosts.StopHook.hook(self, r, r / "nodes/a", session="s-contained")
        self.assertEqual(out["decision"], "block")  # the reminder still goes out...
        self.assertEqual(sentinel.read_text(), "keep\n")  # ...but nothing is written through the link

    def test_hard_links_in_the_lock_git_and_hook_files_and_links_where_init_writes(self):
        r = self.init("alice")
        sentinel = self.tmp / "sentinel.txt"
        sentinel.write_text("keep\n")
        for f in (r / ".git/COMMIT_EDITMSG", r / ".brain/lock"):
            f.parent.mkdir(exist_ok=True)
            f.unlink(missing_ok=True)
            os.link(sentinel, f)
            self.assertIn("out of the brain", self.b(r, "new", "a", "--title", "A", ok=False))
            self.assertEqual(sentinel.read_text(), "keep\n")
            f.unlink()
        alias = r / "scratch/shared"  # an inside file, hard-linked outside, reached through inside links
        alias.parent.mkdir()
        os.link(sentinel, alias)
        for link, args in ((r / "registry.md", ["registry"]), (r / ".git/COMMIT_EDITMSG", ["new", "a", "--title", "A"])):
            link.unlink(missing_ok=True)
            link.symlink_to(alias)
            self.assertIn("out of the brain", self.b(r, *args, ok=False))
            self.assertEqual(sentinel.read_text(), "keep\n")
            link.unlink()
        shutil.rmtree(alias.parent)
        self.b(r, "registry")
        lock = r / ".brain/lock"
        lock.unlink(missing_ok=True)
        os.link(sentinel, lock)  # a lock taken by a command that runs no scan is refused at the lock itself
        from core_loader import SourceFileLoader
        m = SourceFileLoader("brain_lock_link", str(CORE / "bin/brain")).load_module()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            with m.brain_lock(r):
                pass
        self.assertEqual(sentinel.read_text(), "keep\n")
        lock.unlink()
        self.b(r, "new", "a", "--title", "A")
        log = r / ".brain/hooks.log"
        log.unlink(missing_ok=True)
        os.link(sentinel, log)
        status = r / "nodes/a/status.md"
        status.write_text(status.read_text().replace("Updated:", "Updated: 2020-01-01 00:00 by x\nOld:", 1))
        self.assertEqual(_test_hosts.StopHook.hook(self, r, r / "nodes/a", session="s-hard")["decision"], "block")
        self.assertEqual(sentinel.read_text(), "keep\n")
        fresh = self.tmp / "fresh"
        fresh.mkdir()
        (fresh / "AGENTS.md").symlink_to(sentinel)
        out = self.run_(["init", str(fresh), "--operator", "bob", "--title", "B"], cwd=self.tmp, brain=CORE / "bin/brain",
                        ok=False)
        self.assertIn("use an empty folder", out)
        self.assertEqual(sentinel.read_text(), "keep\n")

    def test_merge_commits_when_a_node_has_empty_folders(self):
        r = self.init("alice")
        for nid in ("a", "d"):
            self.b(r, "new", nid, "--title", nid.upper())
        (r / "nodes/a/job-handbacks").mkdir()
        (r / "nodes/a/scratch/deeper").mkdir(parents=True)
        self.b(r, "merge", "a", "d")
        self.assertEqual(self.git("status", "--porcelain", cwd=r), "")
        self.assertIn("Merge a into d", self.git("log", "-1", "--format=%s", cwd=r))
        self.assertIn("✓", self.b(r, "check"))



if __name__ == "__main__":
    unittest.main()


class ForbiddenActions(Scenario):
    def setUp(self):
        super().setUp()
        self.m = core_loader.SourceFileLoader('action_guard_fixture', str(CORE / 'bin/brain')).load_module()

    def test_forbidden_command_blocks_and_exact_allowance_is_consumed_once(self):
        r = self.init('alice')
        self.b(r, 'new', 'app', '--title', 'App')
        node = r / 'nodes/app'
        (node / 'AGENTS.md').write_text('## Forbidden actions\n- git push*\n- rm -rf*\n')
        inst = self.m.Instance(r)
        n = inst.node('app')
        payload = {'cwd': str(node), 'tool_name': 'Bash', 'tool_input': {'command': 'git push origin main'}}
        self.assertIn('forbidden', self.m.pretool_decision(inst, n, payload).lower())
        self.assertIsNone(self.m.pretool_decision(inst, n, dict(payload, tool_input={'command': 'git status'})))
        self.b(r, 'allow', 'app', 'git push origin main', '--once', '--cwd', str(node))
        self.assertIsNotNone(self.m.pretool_decision(inst, n, dict(payload, tool_input={'command': 'git push origin other'})))
        self.assertIsNone(self.m.pretool_decision(inst, n, payload))
        self.assertIsNotNone(self.m.pretool_decision(inst, n, payload))
        state = json.loads(self.m.allowance_path(inst, n).read_text())
        self.assertEqual([e['event'] for e in state['events']], ['issued', 'consumed'])
        self.assertNotIn('git push', json.dumps(state))

    def test_boundary_inheritance_cwd_and_compound_commands_are_scoped(self):
        r = self.init('alice')
        self.b(r, 'principal', 'add', 'team')
        self.b(r, 'new', 'app', '--title', 'App')
        self.b(r, 'new', 'private', '--title', 'Private', '--owners', 'team')
        (r / 'AGENTS.md').write_text('## Forbidden actions\n- git push*\n')
        inst = self.m.Instance(r)
        app = inst.node('app')
        data = {'cwd': str(app['dir']), 'tool_name': 'Bash', 'tool_input': {'command': 'echo ready && git push origin main'}}
        self.assertIsNotNone(self.m.pretool_decision(inst, app, data))
        self.assertIsNone(self.m.pretool_decision(inst, inst.node('private'), data))
        self.b(r, 'allow', 'app', data['tool_input']['command'], '--once', '--cwd', str(app['dir']))
        different = dict(data, cwd=str(r))
        self.assertIsNotNone(self.m.pretool_decision(inst, app, different))
        self.assertIsNone(self.m.pretool_decision(inst, app, data))

    def test_pretool_output_denies_but_malformed_hook_fails_open(self):
        r = self.init('alice')
        (r / 'AGENTS.md').write_text('## Forbidden actions\n- git push*\n')
        p = subprocess.run([sys.executable, str(CORE / 'bin/brain'), 'hook', 'pretool'], cwd=r, env=self.env,
                           input=json.dumps({'cwd': str(r), 'tool_name': 'Bash', 'tool_input': {'command': 'git push'}}),
                           text=True, capture_output=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)['hookSpecificOutput']['permissionDecision'], 'deny')
        p = subprocess.run([sys.executable, str(CORE / 'bin/brain'), 'hook', 'pretool'], cwd=r, env=self.env,
                           input='{bad', text=True, capture_output=True)
        self.assertEqual((p.returncode, p.stdout), (0, ''))

    def test_policy_changes_invalidate_allowance_and_concurrent_hooks_consume_once(self):
        r = self.init('alice')
        rules = r / 'AGENTS.md'
        rules.write_text('## Forbidden actions\n- git push*\n')
        self.b(r, 'allow', 'root', 'git push', '--once', '--cwd', str(r))
        data = json.dumps({'cwd': str(r), 'tool_name': 'Bash', 'tool_input': {'command': 'git push'}})
        processes = [subprocess.Popen([sys.executable, str(CORE / 'bin/brain'), 'hook', 'pretool'],
                     cwd=r, env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                     text=True) for _ in range(2)]
        outputs = [p.communicate(data, timeout=10) for p in processes]
        self.assertEqual(sum(not stdout for stdout, stderr in outputs), 1)
        self.assertTrue(all(p.returncode == 0 for p in processes))
        self.b(r, 'allow', 'root', 'git push', '--once', '--cwd', str(r))
        rules.write_text(rules.read_text() + '- rm -rf*\n')
        m = self.m
        inst = m.Instance(r)
        self.assertIsNotNone(m.pretool_decision(inst, inst.node('root'), json.loads(data)))


class PreviewSampleReceipts(Scenario):
    def context(self):
        r = self.init('alice')
        home = self.repo(self.tmp / 'web')
        self.b(r, 'new', 'web', '--title', 'Web', '--home', str(home))
        self.b(r, 'wt', 'add', 'web', 'job')
        self.b(r, 'set', 'web', 'preview=https://preview.example/app')
        m = core_loader.SourceFileLoader('preview_sample_fixture', str(CORE / 'bin/brain')).load_module()
        inst = m.Instance(r)
        n = inst.node('web')
        row = m.read_jobs(n)[-1]
        head = self.git('rev-parse', 'job', cwd=home).strip()
        data = {'version': 1, 'node': 'web', 'job_key': m.job_key(row), 'branch': 'job', 'head': head,
                'checked_at': inst.now_dt().isoformat(), 'renderer_threadId': 'browser-thread',
                'urls': [{'url': n['local']['preview'], 'rendered': True, 'render_evidence': 'screenshot.png',
                          'items': [{'page_item': 'person-' + str(i), 'source_ref': 'source.json#' + str(i),
                                     'source_item': 'person-' + str(i)} for i in range(3)]}]}
        return m, inst, n, data

    def test_missing_samples_mismatch_wrong_url_and_stale_head_fail_closed(self):
        m, inst, n, data = self.context()
        self.assertTrue(m.validate_ready_preview(inst, n, 'job', data['head']))
        for mutate in ('few', 'duplicate', 'mismatch', 'url', 'head', 'render'):
            bad = json.loads(json.dumps(data))
            route = bad['urls'][0]
            if mutate == 'few': route['items'].pop()
            if mutate == 'duplicate': route['items'][1] = route['items'][0]
            if mutate == 'mismatch': route['items'][1]['source_item'] = 'wrong'
            if mutate == 'url': route['url'] += '/wrong'
            if mutate == 'head': bad['head'] = '0' * 40
            if mutate == 'render': route['rendered'] = False
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                m.record_preview_receipt(inst, n, 'job', bad)
        m.record_preview_receipt(inst, n, 'job', data)
        self.assertEqual(m.validate_ready_preview(inst, n, 'job', data['head']), [])
        n['local']['preview'] += '/new'
        self.assertTrue(m.validate_ready_preview(inst, n, 'job', data['head']))
