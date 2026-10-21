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


class Basics(Scenario):
    def test_fresh_instance_is_generic_and_passes(self):
        r = self.init("alice")
        self.assertIn("✓", self.b(r, "check"))
        self.assertIn("root: alice brain  owners=alice", self.b(r, "tree"))
        inst = (r / "instance.md").read_text()
        self.assertIn("principals: alice", inst)
        self.assertTrue((r / "CLAUDE.md").is_symlink())
        self.assertTrue((r / ".agents" / "skills").is_symlink())
        self.assertIn(".brain/", (r / ".gitignore").read_text())

    def test_nesting_and_relabel_touch_only_labels(self):
        r = self.init("alice")
        self.b(r, "new", "a", "--title", "A", "--labels", "area:x")
        self.b(r, "new", "a/b/", "--title", "B")
        self.b(r, "new", "a/b/c", "--title", "C")
        self.assertIn("      a/b/c: C", self.b(r, "tree"))
        head = self.git("rev-parse", "HEAD", cwd=r).strip()
        self.b(r, "relabel", "a", "area:y", "kind:z")
        changed = self.git("diff", "--name-only", head, "HEAD", cwd=r).split()
        self.assertEqual(sorted(changed), ["nodes/a/node.md", "registry.md"])

    def test_bare_brain_is_a_dashboard(self):
        r = self.init("alice")
        self.b(r, "new", "a", "--title", "A")
        with open(r / "nodes" / "a" / "inbox.md", "a") as fh:
            fh.write("- 2026-01-01 10:00 from worker: look at the invoice\n")
        out = self.b(r)
        self.assertIn("Needs attention", out)
        self.assertIn("look at the invoice", out)
        self.assertIn("no coordinator on this machine", out)

    def test_principal_add_and_unknown_principals_refused(self):
        r = self.init("alice")
        self.b(r, "new", "x", "--title", "X", "--owners", "bob", ok=False)
        self.b(r, "principal", "add", "bob")
        self.assertIn("bob", self.b(r, "principal", "list"))



class MachineBindings(Scenario):
    def test_host_ids_stay_on_this_machine(self):
        r = self.init("alice")
        self.b(r, "new", "a", "--title", "A")
        head = self.git("rev-parse", "HEAD", cwd=r).strip()
        self.b(r, "set", "a", "coordinator=thread-123", "host_project=proj-9")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=r).strip(), head, "nothing to commit")
        local = json.loads((r / ".brain" / "local.json").read_text())
        self.assertEqual(local["nodes"]["a"]["coordinator"], "thread-123")
        self.assertNotIn("thread-123", (r / "nodes" / "a" / "node.md").read_text())
        self.assertNotIn("thread-123", (r / "registry.md").read_text())
        self.assertIn("t3-thread://v1/thread-123", self.b(r, "show", "a"))
        self.assertEqual(self.git("status", "--porcelain", cwd=r).strip(), "")

    def test_legacy_bindings_in_node_md_are_migrated_by_fix(self):
        r = self.init("alice")
        self.b(r, "new", "a", "--title", "A")
        nm = r / "nodes" / "a" / "node.md"
        nm.write_text(nm.read_text().replace("created:", "coordinator: old-thread\ncreated:"))
        self.git("commit", "-qam", "legacy", cwd=r)
        self.assertIn("machine-specific", self.b(r, "check", ok=False))
        self.assertIn("fixed:", self.b(r, "check", "--fix"))
        self.assertNotIn("coordinator", nm.read_text().split("---")[1])
        self.assertIn("old-thread", (r / ".brain" / "local.json").read_text())

    def test_move_carries_bindings_and_pins_owners(self):
        r = self.init("alice")
        self.b(r, "principal", "add", "team")
        self.b(r, "new", "a", "--title", "A")
        (self.tmp / "t-home").mkdir()
        self.b(r, "new", "t", "--title", "T", "--owners", "team", "--home", str(self.tmp / "t-home"))
        self.b(r, "set", "a", "coordinator=c-a")
        out = self.b(r, "move", "a", "t")
        self.assertIn("owners kept as alice", out)
        self.assertIn("SEALED", out)
        self.assertIn("c-a", json.dumps(json.loads((r / ".brain" / "local.json").read_text())["nodes"]["t/a"]))
        self.assertIn("Node `t/a`", (r / "nodes" / "t" / "nodes" / "a" / "AGENTS.md").read_text())



class SeveralBrains(Scenario):
    """One machine, two brains (say personal and work): one `brain` command, nothing shared between them."""

    def anywhere(self, *args, ok=True, cwd=None, env=None):
        saved = self.env
        self.env = {**self.env, **(env or {})}
        try:
            return self.run_(list(args), cwd=cwd or self.tmp, ok=ok, brain=CORE / "bin" / "brain")
        finally:
            self.env = saved

    def two(self):
        me = self.init("me")
        self.b(me, "new", "blog", "--title", "My blog")
        work = self.tmp / "Work"
        self.run_(["init", str(work), "--operator", "me", "--title", "Work brain", "--name", "work", "--tz", "UTC"],
                  cwd=self.tmp, brain=CORE / "bin" / "brain")
        self.b(work, "principal", "add", "acme")
        return me, work

    def test_one_command_reaches_each_brain(self):
        me, work = self.two()
        self.assertIn("name: work", (work / "instance.md").read_text())
        out = self.anywhere("brains")
        self.assertIn("me ", out)
        self.assertIn("work ", out)
        self.assertIn("[default]", out.split("\n")[0], "the first brain is the default")
        self.assertIn("root: me brain", self.anywhere("tree"))            # outside both: the default
        self.assertIn("root: Work brain", self.anywhere("-b", "work", "tree"))
        self.assertIn("root: Work brain", self.anywhere("tree", "--brain", "work"))
        self.assertIn("root: me brain", self.anywhere("tree", cwd=me / "nodes"))   # inside one: that one
        self.assertIn("no brain 'nope'", self.anywhere("-b", "nope", "tree", ok=False))
        self.anywhere("use", "work")
        self.assertIn("root: Work brain", self.anywhere("tree"))
        self.assertIn("root: me brain", self.anywhere("tree", cwd=me))
        out = self.anywhere("--all")
        self.assertIn("===== me =====", out)
        self.assertIn("===== work =====", out)
        self.assertIn("Work brain (work)", out)

    def test_a_project_folder_belongs_to_its_brain(self):
        me, work = self.two()
        repo = self.repo(self.tmp / "code" / "acme-app")
        self.b(work, "new", "app", "--title", "Acme app", "--owners", "acme", "--home", str(repo))
        self.assertIn("root: Work brain", self.anywhere("tree", cwd=repo))
        self.b(work, "wt", "add", "app", "fix-1")
        wt = self.tmp / "code" / "acme-app-work" / "worktrees" / "fix-1"
        self.assertIn("root: Work brain", self.anywhere("tree", cwd=wt))
        self.assertIn("root: me brain", self.anywhere("tree", cwd=self.tmp / "code"))

    def test_each_brain_runs_its_own_core(self):
        me, work = self.two()
        f = work / "core" / "lib" / "brain" / "cli.py"
        f.write_text(f.read_text().replace("def main():\n", "def main():\n    print('WORK CORE')\n", 1))
        env = {k: v for k, v in self.env.items() if k != "BRAIN_NO_REEXEC"}
        self.assertIn("WORK CORE", self.anywhere("-b", "work", "now", env={**env, "BRAIN_NO_REEXEC": ""}))
        self.assertNotIn("WORK CORE", self.anywhere("now", env={**env, "BRAIN_NO_REEXEC": ""}))

    def test_pointers_go_where_each_brain_is_told(self):
        me, work = self.two()
        claude, codex = self.tmp / ".claude" / "CLAUDE.md", self.tmp / ".codex" / "AGENTS.md"
        self.b(me, "link", "--global")
        self.assertIn("`brain -b me`", claude.read_text())
        out = self.b(work, "link", "--global")                      # the default folders already point to 'me'
        self.assertIn("already points to me: skipped", out)
        self.assertNotIn("Work brain", claude.read_text())
        self.b(work, "link", "--global", "claude=~/.claude-work", "codex=~/.codex-work")
        cw = self.tmp / ".claude-work" / "CLAUDE.md"
        self.assertIn("`brain -b work`", cw.read_text())
        self.assertIn("`brain -b work`", (self.tmp / ".codex-work" / "AGENTS.md").read_text())
        self.assertNotIn("me brain", cw.read_text())
        self.assertNotIn("Work brain", claude.read_text() + codex.read_text())
        self.assertIn("pointer in: claude ~/.claude-work; codex ~/.codex-work", self.anywhere("brains"))
        # a session started with the work config folder reaches the work brain, wherever it runs
        self.assertIn("root: Work brain", self.anywhere("tree", env={"CLAUDE_CONFIG_DIR": str(cw.parent)}))
        self.assertIn("root: me brain", self.anywhere("tree"))
        # moving it: the pointer leaves the old folder
        self.b(work, "link", "--global", "claude=~/.claude-w2")
        self.assertNotIn("brain:begin work", cw.read_text())
        self.assertIn("brain:begin work", (self.tmp / ".claude-w2" / "CLAUDE.md").read_text())
        # sharing a folder is allowed when asked for, and each pointer comes out on its own
        self.b(work, "link", "--global", "claude=~/.claude")
        self.assertEqual(claude.read_text().count("brain:begin"), 2)
        self.b(work, "link", "--remove-global")
        self.assertIn("brain:begin me", claude.read_text())
        self.assertNotIn("brain:begin work", claude.read_text())

    def test_an_older_pointer_is_upgraded_in_place(self):
        me = self.init("me")
        claude = self.tmp / ".claude" / "CLAUDE.md"
        claude.parent.mkdir()
        claude.write_text("# Mine\n\n<!-- brain:begin (written by `brain link --global`; edit outside these lines) -->\n"
                          "me's Brain is at `~/me`: old text\n<!-- brain:end -->\n")
        self.b(me, "link")
        text = claude.read_text()
        self.assertTrue(text.startswith("# Mine\n"))
        self.assertNotIn("old text", text)
        self.assertEqual(text.count("brain:begin"), 1)
        self.assertIn("brain:begin me", text)
        self.assertEqual(json.loads((me / ".brain" / "local.json").read_text())["global"], {"claude": ["~/.claude"]})
        self.assertIn("this brain: me   pointer in: claude ~/.claude", self.b(me, "machine"))

    def test_a_throwaway_brain_stays_out_of_the_real_list(self):
        import tempfile as tf
        real = self.tmp / "real-config"                       # stands in for ~/.config/brain, outside the temp dir?
        if str(real.resolve()).startswith(str(Path(tf.gettempdir()).resolve())):
            # this test's folders are themselves in the temp dir; point the list somewhere that isn't
            real = Path.home() / ".cache" / f"brain-test-{os.getpid()}"
        self.env["BRAIN_CONFIG"] = str(real / "brains.json")
        try:
            r = self.init("alice")                             # lives in the temp dir
            self.b(r, "tree")
            self.assertFalse((real / "brains.json").exists() and "alice" in (real / "brains.json").read_text())
        finally:
            shutil.rmtree(real, ignore_errors=True)

    def test_ports_never_collide_across_brains(self):
        me, work = self.two()
        self.b(me, "principal", "add", "club")
        a = self.repo(self.tmp / "a")
        w = self.repo(self.tmp / "w")
        self.b(me, "new", "a", "--title", "A", "--owners", "club", "--home", str(a))
        self.b(work, "new", "w", "--title", "W", "--owners", "acme", "--home", str(w))
        self.b(me, "set", "a", "ports=3")
        (me / "nodes" / "a" / "worktree-setup.sh").write_text('echo "$PORT $PORTS $PORT_LAST" > ports.txt\n')
        self.assertIn("ports ", self.b(me, "wt", "add", "a", "j1"))
        p, n, last = map(int, (self.tmp / "a-work" / "worktrees" / "j1" / "ports.txt").read_text().split())
        self.assertEqual((n, last), (3, p + 2))
        self.b(work, "wt", "add", "w", "j2")
        q = json.loads((work / ".brain" / "local.json").read_text())["nodes"]["w"]["worktrees"]["j2"]["port"]
        self.assertNotIn(q, range(p, p + 3), "another brain's worktree never gets these ports")
        self.assertIn(f"ports {p}-{p + 2}", self.b(me, "wt", "list"))
        self.b(me, "set", "a", "ports=lots")
        self.assertIn("ports must be a whole number", self.b(me, "wt", "add", "a", "j3", ok=False))



class ImportProjects(Scenario):
    def history_repo(self, path):
        h = self.repo(path, {"README.md": "# Fixture project\n", "main.py": "print('ready')\n",
                             "AGENTS.md": "# Rules\nNo network calls. Why: offline tool.\n",
                             "memory.md": "2024-02-01: use streaming to bound memory.\n"})
        self.git("commit", "--amend", "--no-edit", "--date=2024-01-01T10:00:00+00:00", cwd=h)
        (h / "main.py").write_text("print('refactor')\n")
        self.git("commit", "-qam", "refactor: stream records", "--date=2024-02-01T10:00:00+00:00", cwd=h)
        self.git("revert", "--no-edit", "HEAD", cwd=h)
        # Both dates matter: the scanner reports the last commit's committer date.
        old = self.env.copy()
        self.env.update(GIT_COMMITTER_DATE="2024-03-01T12:00:00+00:00")
        self.git("commit", "--amend", "--no-edit", "--date=2024-03-01T12:00:00+00:00", cwd=h)
        self.env = old
        self.git("remote", "add", "origin", "https://person:fake-import-token@example.test/team/project.git?token=fake-query#fake-fragment", cwd=h)
        return h

    def snapshot_files(self, root):
        import hashlib
        return {str(p.relative_to(root)): (p.lstat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
                for p in root.rglob("*") if p.is_file() and not p.is_symlink()}

    def test_scan_metadata_depth_and_no_writes(self):
        r = self.init("alice")
        base = self.tmp / "code"
        h = self.history_repo(base / "project")
        (h / ".codex").mkdir()
        (h / "skills").mkdir()
        (h / "notes").mkdir()
        (h / "notes" / "lessons.md").write_text("bounded parsing\n")
        self.git("add", ".", cwd=h); self.git("commit", "-qm", "lessons", cwd=h)
        for skip in ("node_modules", "vendor", "build", "dist", ".venv"):
            self.repo(base / skip / "hidden")
        deeper = self.repo(base / "group" / "deeper")
        self.b(r, "adopt", str(h))
        before = self.snapshot_files(self.tmp)
        out = self.b(r, "scan", str(base), str(base), str(r), "--json", "--depth", "1")
        rows = json.loads(out)
        self.assertEqual(len(rows), 1)
        repo = rows[0]
        self.assertEqual(repo["path"], "~/code/project")
        self.assertEqual(repo["name"], "project")
        self.assertEqual(repo["commit_count"], 4)
        self.assertIn("Python", repo["languages"])
        self.assertEqual(repo["readme"], "# Fixture project")
        self.assertEqual(repo["remotes"], ["https://example.test/team/project.git"])
        self.assertTrue(repo["has_remote"])
        self.assertEqual(repo["node"], "project")
        self.assertTrue(repo["already_home"])
        self.assertTrue({"AGENTS.md", "memory.md", ".codex/", "skills/"} <= set(repo["agent_setup"]))
        for secret in ("fake-import-token", "fake-query", "fake-fragment", "person:"):
            self.assertNotIn(secret, out)
            self.assertNotIn(secret, self.all_text(r))
        self.assertEqual(self.snapshot_files(self.tmp), before)
        self.assertEqual(len(json.loads(self.b(r, "scan", str(base), "--depth", "2", "--json"))), 2)
        self.assertEqual(len(json.loads(self.b(r, "scan", str(deeper), "--depth", "0", "--json"))), 1)
        self.b(r, "scan", str(base), "--depth", "-1", ok=False)

    def test_scan_dates_empty_repo_redaction_and_worktrees(self):
        r = self.init("alice")
        h = self.history_repo(self.tmp / "project")
        date = json.loads(self.b(r, "scan", str(h), "--json", "--depth", "0"))[0]
        self.assertEqual(date["last_commit_date"], "2024-03-01T12:00:00Z")
        (r / "never-name.txt").write_text("ForbiddenTerm\n")
        (h / "README.md").write_text("ForbiddenTerm token=fake-value\n")
        self.assertNotIn("fake-value", self.b(r, "scan", str(h)))
        self.assertNotIn("ForbiddenTerm", self.b(r, "scan", str(h), "--json"))
        wt = self.tmp / "checkout"
        self.git("worktree", "add", "-qb", "study", str(wt), cwd=h)
        self.assertEqual(len(json.loads(self.b(r, "scan", str(wt), "--json"))), 1)
        self.assertIn("main repository", self.b(r, "adopt", str(wt), "--id", "checkout", ok=False))
        empty = self.tmp / "empty"
        empty.mkdir(); self.git("init", "-q", cwd=empty)
        row = json.loads(self.b(r, "scan", str(empty), "--json"))[0]
        self.assertEqual(row["commit_count"], 0)
        self.assertIsNone(row["last_commit_date"])
        self.assertFalse(row["has_remote"])
        self.assertEqual(row["languages"], [])

    def test_adopt_multiple_parent_and_machine_local_homes(self):
        r = self.init("alice")
        self.b(r, "new", "projects", "--title", "Projects")
        a = self.history_repo(self.tmp / "alpha")
        b = self.repo(self.tmp / "beta")
        before = self.snapshot_files(a)
        out = self.b(r, "adopt", str(a), str(b), "--parent", "projects")
        self.assertIn("brain brief projects/alpha --import", out)
        loc = json.loads((r / ".brain/local.json").read_text())
        self.assertEqual(loc["nodes"]["projects/alpha"]["home"], str(a))
        self.assertIn("home: external", (r / "nodes/projects/nodes/alpha/node.md").read_text())
        self.assertNotIn(str(a), self.git("show", "HEAD:nodes/projects/nodes/alpha/node.md", cwd=r))
        self.assertEqual(self.snapshot_files(a), before)
        self.assertEqual(self.git("status", "--porcelain", cwd=r), "")
        self.b(r, "check")
        out = self.b(r, "brief", "projects/alpha", "--import")
        self.assertIn("sole initial writer", out)
        self.assertIn("2000 commit headers", out)
        self.assertIn("skills/import/SKILL.md", out)
        self.assertIn("read-only", out)
        self.b(r, "brief", "projects/alpha", "--import", "--branch", "wrong", ok=False)

    def test_adopt_refuses_invalid_batch_before_any_creation(self):
        r = self.init("alice")
        h = self.repo(self.tmp / "project")
        (h / "sub").mkdir()
        for paths in ((str(r),), (str(h / "sub"),), (str(h), str(h)), (str(h), str(self.tmp / "missing"))):
            self.b(r, "adopt", *paths, ok=False)
            self.assertFalse((r / "nodes/project").exists())
        self.b(r, "adopt", str(h), "--id", "bad/id", ok=False)
        self.b(r, "adopt", str(h), str(r), "--title", "Title", ok=False)
        self.b(r, "adopt", str(h), "--id", "renamed", "--title", "New title")
        self.assertIn("title: New title", (r / "nodes/renamed/node.md").read_text())
        self.b(r, "adopt", str(h), ok=False)
        alias = self.tmp / "alias"; alias.symlink_to(h)
        self.b(r, "adopt", str(alias), ok=False)
        (r / "nodes/renamed/never-name.txt").write_text("unsafe-term\n")
        self.b(r, "brief", "renamed", "--import", "--task", "unsafe-term", ok=False)

    def proposal(self, r, source="root", iid="bounded-input"):
        return self.b(r, "item", "add", iid, "--from", source, "--title", "Bound inputs", "--kind", "lesson",
                      "--ref", "AGENTS.md@" + "a" * 40, "--body", "Bound each input before parsing.",
                      "--repo", "fixture-project", "--commit", "a" * 40, "--proposed")

    def test_proposed_acceptance_quarantine_restore_and_ui(self):
        r = self.init("alice")
        self.proposal(r)
        path = r / "library/bounded-input.md"
        original = path.read_text()
        self.assertIn("status: proposed", original)
        self.assertIn("source_commit: " + "a" * 40, original)
        self.b(r, "usable", "bounded-input", "root", ok=False)
        self.b(r, "item", "restore", "bounded-input", ok=False)
        for _ in range(2):
            self.b(r, "item", "quarantine", "bounded-input")
        self.b(r, "item", "accept", "bounded-input", "--reviewed-by", "human", ok=False)
        self.b(r, "item", "restore", "bounded-input")
        self.assertIn("status: proposed", path.read_text())
        self.b(r, "specialist", "new", "reader", "--title", "Reader")
        self.b(r, "lesson", "reader", "--from", "root", "Bound source reads")
        self.b(r, "hire", "reader", "root", "--task", "Study fixture", "--job", "job/import")
        self.b(r, "hire", "reader", "root", "--task", "Study", "--items", "bounded-input", ok=False)
        ui = json.loads(self.b(r, "ui", "--json"))
        item = ui["items"]["all"][0]
        self.assertEqual(item["state"], "proposed")
        self.assertIn("Bound each input", item["body"])
        self.assertEqual(item["provenance"]["repo"], "fixture-project")
        self.assertEqual(item["provenance"]["commit"], "a" * 40)
        self.assertEqual(ui["specialists"][0]["lessons"][0]["text"], "Bound source reads")
        self.assertEqual(ui["specialists"][0]["jobs"][0]["job"], "job/import")
        self.b(r, "item", "accept", "bounded-input", "--reviewed-by", "alice")
        self.assertIn("accepted_by: alice", path.read_text())
        self.b(r, "usable", "bounded-input", "root")
        self.b(r, "item", "quarantine", "bounded-input")
        self.b(r, "item", "restore", "bounded-input")
        self.b(r, "usable", "bounded-input", "root")
        self.assertEqual(json.loads(self.b(r, "ui", "--json"))["items"]["all"][0]["state"], "accepted")
        for field in ("source_ref", "source_commit", "source_repo", "source_owners", "scope"):
            before = next(l for l in original.splitlines() if l.startswith(field + ":"))
            self.assertIn(before, path.read_text())
        self.b(r, "check")
        path.write_text(path.read_text().replace("status: active", "status: proposed").replace("source_repo:", "lost_repo:"))
        self.assertIn("proposed learning needs source_repo", self.b(r, "check", ok=False))

    def test_scan_remote_forms_and_overlapping_roots(self):
        r = self.init("alice")
        base = self.tmp / "projects"
        h = self.repo(base / "outer")
        child = self.repo(h / "child")
        self.git("remote", "add", "origin", "git@example.test:team/outer.git", cwd=h)
        self.git("remote", "add", "extra", "ssh://user:fake-password@example.test/outer.git?fake-query", cwd=h)
        (h / "memory-notes.txt").write_text("untracked memory\n")
        rows = json.loads(self.b(r, "scan", str(base), str(h), "--json", "--depth", "1"))
        self.assertEqual({x["path"] for x in rows}, {"~/projects/outer", "~/projects/outer/child"})
        outer = next(x for x in rows if x["path"] == "~/projects/outer")
        self.assertEqual(outer["remotes"], ["git@example.test:team/outer.git", "ssh://user@example.test/outer.git"])
        self.assertNotIn("memory-notes.txt", outer["agent_setup"])
        self.b(r, "scan", str(self.tmp / "missing"), "--json", ok=False)
        alias = base / "alias"; alias.symlink_to(child)
        self.assertEqual(len(json.loads(self.b(r, "scan", str(base), "--json"))), 2)

    def test_proposal_provenance_and_revocation(self):
        r = self.init("alice")
        self.b(r, "principal", "add", "team")
        h = self.repo(self.tmp / "team-project")
        self.b(r, "new", "team", "--title", "Team", "--owners", "team", "--home", str(h))
        self.proposal(r, source="team")
        self.b(r, "item", "add", "legacy", "--from", "team", "--title", "Legacy", "--kind", "fact", "--ref", "old")
        self.b(r, "revoke", "team")
        # An explicit restore of an older active item keeps its existing behavior.
        self.b(r, "item", "restore", "legacy")
        self.assertIn("status: active", (r / "library/legacy.md").read_text())
        self.assertIn("status_before_quarantine: proposed", (r / "library/bounded-input.md").read_text())
        self.b(r, "item", "restore", "bounded-input", ok=False)
        self.b(r, "usable", "bounded-input", "root", ok=False)
        args = ("item", "add", "bad", "--from", "root", "--title", "Bad", "--kind", "lesson", "--ref", "x", "--proposed")
        self.b(r, *args, ok=False)
        self.b(r, *args, "--repo", "safe", "--commit", "a" * 40, "--title", "Bad\n---\n", ok=False)
        self.b(r, *args, "--repo", str(h), "--commit", "a" * 40, ok=False)
        self.b(r, *args, "--repo", "safe", "--commit", "short", ok=False)
        self.b(r, *args, "--repo", "safe", "--commit", "a" * 40, "--body", "token=fake-token", ok=False)
        (r / "never-name.txt").write_text("NeverCopy\n")
        self.b(r, *args, "--repo", "safe", "--commit", "a" * 40, "--body", "NeverCopy", ok=False)
        self.assertFalse((r / "library/bad.md").exists())



class ImportDiscovery(Scenario):
    def rows(self, r, path):
        return json.loads(self.b(r, "scan", str(path), "--json"))

    def test_common_directory_workbench_and_nested_repositories(self):
        r = self.init("alice")
        h = self.repo(self.tmp / "project")
        wt = self.tmp / "linked"
        self.git("worktree", "add", "-qb", "study", str(wt), cwd=h)
        self.assertEqual(self.rows(r, wt)[0]["kind"], "worktree")
        self.b(r, "adopt", str(wt), ok=False)
        child = self.repo(h / "nested")
        self.assertFalse(next(row for row in self.rows(r, h) if row["name"] == "nested")["adoptable"])
        self.assertIn("enclosing main repository", self.b(r, "adopt", str(child), ok=False))
        self.b(r, "adopt", str(h))
        row = self.rows(r, wt)[0]
        self.assertTrue(row["already_home"])
        self.assertEqual(row["node"], "project")
        self.b(r, "wt", "add", "project", "job/feature")
        self.assertEqual(self.rows(r, self.tmp / "project-work"), [])
        worktrees = json.loads((r / ".brain/local.json").read_text())["nodes"]["project"]["worktrees"]
        self.b(r, "adopt", worktrees["job/feature"]["path"], ok=False)
        source = self.repo(self.tmp / "source")
        self.git("submodule", "add", str(source), "module", cwd=h)
        row = next(row for row in self.rows(r, h) if row["name"] == "module")
        self.assertEqual(row["kind"], "submodule")
        self.assertFalse(row["adoptable"])
        self.assertIn("main repository", self.b(r, "adopt", str(h / "module"), ok=False))

    def test_another_brain_homes_and_linked_checkouts_are_private(self):
        r = self.init("personal")
        work = self.init("work")
        h = self.repo(self.tmp / "client-project")
        self.b(work, "adopt", str(h), "--id", "private-client-node")
        wt = self.tmp / "linked"
        self.git("worktree", "add", "-qb", "study", str(wt), cwd=h)
        self.b(work, "wt", "add", "private-client-node", "job/feature")
        for path in (h, wt):
            row = self.rows(r, path)[0]
            self.assertEqual(row["brain"], "personal")
            self.assertEqual(row["ownership"], "another brain: work")
            self.assertTrue(row["already_home"])
            self.assertIsNone(row["node"])
            out = self.b(r, "adopt", str(path), ok=False)
            self.assertIn("another brain: work", out)
            self.assertNotIn("private-client-node", out)
        wb = self.tmp / "client-project-work"
        self.assertEqual(self.rows(r, wb), [])
        paths = json.loads((work / ".brain/local.json").read_text())["nodes"]["private-client-node"]["worktrees"]
        self.assertIn("another brain: work", self.b(r, "adopt", paths["job/feature"]["path"], ok=False))
        self.assertIn("brain: personal", self.b(r, "scan", str(h)))
        other = self.repo(self.tmp / "personal-project")
        self.assertIn("brain: personal", self.b(r, "adopt", str(other)))

    def test_empty_brief_instance_ancestor_and_pruned_directories(self):
        r = self.init("alice")
        h = self.tmp / "empty"; h.mkdir()
        self.git("init", "-q", cwd=h)
        self.b(r, "adopt", str(h))
        self.assertIn("Snapshot commit: unborn", self.b(r, "brief", "empty", "--import"))
        self.git("init", "-q", cwd=self.tmp)
        self.assertIn("contains an instance", self.b(r, "adopt", str(self.tmp), ok=False))
        for skip in ("Library", ".Trash", ".Trashes", ".cache"):
            self.repo(self.tmp / "discovery" / skip / "hidden")
        self.assertEqual(self.rows(r, self.tmp / "discovery"), [])

    def test_agent_memory_is_bounded_and_non_utf8_names_do_not_crash(self):
        r = self.init("alice")
        h = self.repo(self.tmp / "project")
        for base in ("src/memory", "memory", ".claude/memory"):
            (h / base).mkdir(parents=True)
            for i in range(50):
                (h / base / (str(i) + ".md")).write_text("data")
        self.git("add", ".", cwd=h)
        # APFS rejects invalid UTF-8 filenames, but Git's index accepts them on every host.
        blob = self.git("rev-parse", "HEAD:README.md", cwd=h).strip()
        indexed = subprocess.run(["git", "update-index", "-z", "--index-info"], cwd=h, env=self.env,
                                 input=b"100644 " + blob.encode() + b"\tbad-\xff.py\0", capture_output=True)
        self.assertEqual(indexed.returncode, 0, indexed.stderr)
        row = self.rows(r, h)[0]
        self.assertIn("Python", row["languages"])
        self.assertEqual(len(row["agent_setup"]), 40)
        self.assertGreater(row["agent_setup_omitted"], 0)
        self.assertFalse(any(x.startswith("src/memory") for x in row["agent_setup"]))
        text = self.b(r, "scan", str(h))
        self.assertRegex(text, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} \+00:00")
        self.assertIn("~/project", text)
        self.assertNotIn(str(self.tmp), text)

    def test_unreadable_scan_directory_warns_and_continues(self):
        import contextlib
        import io
        from core_loader import SourceFileLoader
        from unittest import mock
        from types import SimpleNamespace
        r = self.init("alice")
        mod = SourceFileLoader("brain_scan_test", str(CORE / "bin/brain")).load_module()
        out, err = io.StringIO(), io.StringIO()
        def walk(root, **kwargs):
            kwargs["onerror"](PermissionError(13, "Permission denied", str(self.tmp / "blocked")))
            return iter(())
        with mock.patch.dict(os.environ, self.env), mock.patch.object(mod, "find_root", return_value=r), \
                mock.patch.object(mod.os, "walk", side_effect=walk), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            mod.cmd_scan(SimpleNamespace(depth=4, dirs=[str(self.tmp)], json=True))
        self.assertEqual(json.loads(out.getvalue()), [])
        self.assertIn("~/blocked", err.getvalue())
        self.assertIn("skipped", err.getvalue())

    def test_scan_closed_pipe_exits_without_traceback(self):
        r = self.init("alice")
        h = self.repo(self.tmp / "project")
        proc = subprocess.Popen([sys.executable, str(CORE / "bin/brain"), "scan", str(h), "--json"],
                                cwd=r, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        proc.stdout.close()
        error = proc.stderr.read().decode()
        proc.stderr.close()
        self.assertEqual(proc.wait(), 0, error)
        self.assertNotIn("BrokenPipeError", error)



class TeamHomes(Scenario):
    def old_core_job(self):
        # Exercise the actual pre-team-home writer, not a new packet with fields removed.
        history = Path(__file__).resolve().parent.parent
        source = subprocess.run(["git", "show", "4a7a46933ce0ad95d69dbbc91ec5ba730636be6c:bin/brain"],
                                cwd=history, capture_output=True)
        if source.returncode:
            self.skipTest("pre-team-home Git object unavailable in this source archive")
        old = self.tmp / "old-core/bin/brain"
        old.parent.mkdir(parents=True)
        old.write_bytes(source.stdout)
        root = self.init("alice")
        seed = self.repo(self.tmp / "seed")
        origin = self.tmp / "origin.git"
        self.git("clone", "--bare", str(seed), str(origin), cwd=self.tmp)
        home = self.tmp / "app"
        self.git("clone", str(origin), str(home), cwd=self.tmp)
        (home / "local").write_text("unpublished integration")
        self.git("add", "-A", cwd=home)
        self.git("commit", "-qm", "local integration", cwd=home)
        base = self.git("rev-parse", "HEAD", cwd=home).strip()
        self.b(root, "new", "app", "--title", "App", "--home", str(home))
        self.b(root, "set", "app", "verify=python3 -c 'from pathlib import Path; "
               "p=Path(\"../verify-count\"); p.write_text(p.read_text()+\"x\" if p.exists() else \"x\")'")
        self.run_(["wt", "add", "app", "job", "--base", "main"], cwd=root, brain=old)
        wt = self.tmp / "app-work/worktrees/job"
        (wt / "fix").write_text("job contribution")
        self.git("add", "-A", cwd=wt)
        self.git("commit", "-qm", "job contribution", cwd=wt)
        self.run_(["handback", "app", "--branch", "job", "--state", "done", "--reviews", "1",
                   "--lessons", "none: fixture"], cwd=root, brain=old)
        record = next((root / "nodes/app/job-handbacks").glob("*.json"))
        packet = json.loads(record.read_text())["ready"]
        self.assertNotIn("integration_base", packet)
        self.assertEqual(packet["main"], base)
        self.assertNotIn("Base commit", (root / "nodes/app/job-ledger.md").read_text())
        return root, origin, home, wt, old, record, base

    def test_real_old_ready_survives_upgrade_and_reuses_verify_on_next_write(self):
        root, origin, home, wt, old, record, base = self.old_core_job()
        before = record.read_bytes()
        ledger = (root / "nodes/app/job-ledger.md").read_bytes()
        self.assertTrue(self.b(root, "ready", "app", "--branch", "job").strip().endswith("ready"))
        self.assertIn("ready to merge", self.b(root, "due", "app"))
        self.assertEqual(record.read_bytes(), before)
        self.assertEqual((root / "nodes/app/job-ledger.md").read_bytes(), ledger)
        # Even a report-only worker write lazily upgrades the packet without verification.
        self.b(root, "handback", "app", "--branch", "job", "--state", "done", "--report", "fixture report")
        updated = json.loads(record.read_text())["ready"]
        self.assertEqual(updated["integration_base"], base)
        self.assertNotEqual(updated["main"], base)
        self.assertEqual((wt.parent / "verify-count").read_text(), "x")
        record.write_bytes(before)
        # Refreshing origin within the unchanged integration base also preserves old evidence.
        self.git("push", "origin", "main", cwd=home)
        self.git("fetch", "origin", cwd=home)
        self.assertTrue(self.b(root, "ready", "app", "--branch", "job").strip().endswith("ready"))
        # Keep distinct remote/base identities for the lazy upgrade and verify reuse check.
        self.git("update-ref", "refs/remotes/origin/main", base + "^", cwd=home)
        out = self.b(root, "handback", "app", "--branch", "job", "--state", "done", "--reviews", "2")
        self.assertIn("verify: reused", out)
        packet = json.loads(record.read_text())["ready"]
        self.assertEqual(packet["integration_base"], base)
        self.assertNotEqual(packet["main"], base)
        count = wt.parent / "verify-count"
        self.assertEqual(count.read_text(), "x")
        # Rebase keeps preflight green: the old base itself must invalidate the record.
        record.write_bytes(before)
        self.git("commit", "--allow-empty", "-qm", "new integration base", cwd=home)
        self.git("rebase", "main", cwd=wt)
        self.assertIn("main moved", self.b(root, "ready", "app", "--branch", "job", ok=False))
        self.b(root, "handback", "app", "--branch", "job", "--state", "done", "--reviews", "2")
        self.assertEqual(count.read_text(), "xx")

    def test_real_old_ready_no_origin_and_pr_base_change(self):
        root, origin, home, wt, old, record, base = self.old_core_job()
        self.b(root, "set", "app", "merge=local")
        self.assertTrue(self.b(root, "ready", "app", "--branch", "job").strip().endswith("ready"))
        self.b(root, "set", "app", "merge=pr")
        self.assertIn("main moved", self.b(root, "ready", "app", "--branch", "job", ok=False))
        self.b(root, "set", "app", "merge=local")
        seed = self.tmp / "seed"
        self.git("commit", "--allow-empty", "-qm", "peer integration", cwd=seed)
        self.git("push", str(origin), "main", cwd=seed)
        self.git("fetch", "origin", cwd=home)
        self.assertIn("local default must include origin default",
                      self.b(root, "ready", "app", "--branch", "job", ok=False))
        self.git("remote", "remove", "origin", cwd=home)
        self.assertEqual(self.b(root, "ready", "app", "--branch", "job").strip(), "ready")

    def test_real_old_ledger_and_handback_survive_close_lesson_cleanup_and_sync(self):
        root, origin, home, wt, old, record, base = self.old_core_job()
        head = self.git("rev-parse", "HEAD", cwd=wt).strip()
        self.git("merge", "--no-ff", "-m", "integrate job", "job", cwd=home)
        merged = self.git("rev-parse", "HEAD", cwd=home).strip()
        # Old coordinator closes an old ledger row, leaving evidence for lesson acceptance.
        self.run_(["wt", "rm", "app", "job", "--cause", "fixture cleanup"], cwd=root, brain=old)
        self.assertTrue(record.exists())
        self.b(root, "jobs", "app", "--branch", "job", "--lessons", "none: fixture")
        self.assertFalse(record.exists())
        ledger = (root / "nodes/app/job-ledger.md").read_text()
        self.assertIn(base, ledger)
        self.assertIn(head, ledger)
        self.git("branch", "-D", "job", cwd=home)
        self.git("push", "origin", "main", cwd=home)
        # The unpublished base is another owner's integration; only that one is external.
        self.assertIn("1 recorded", self.b(root, "sync", "app"))
        ledger = (root / "nodes/app/job-ledger.md").read_text()
        self.assertIn(merged, ledger)
        self.assertIn("external:" + base, ledger)
        self.assertNotIn("external:" + merged, ledger)
        self.assertIn("0 recorded", self.b(root, "sync", "app", "--offline"))

    def test_real_old_standalone_packet_is_retained_before_cleanup(self):
        import hashlib
        root, origin, home, wt, old, record, base = self.old_core_job()
        packet = json.loads(record.read_text())["ready"]
        packet["checked_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        cache = self.tmp / "app-work/scratch/ready" / (hashlib.sha256(b"job").hexdigest() + ".json")
        cache.parent.mkdir(parents=True)
        cache.write_text(json.dumps(packet))
        record.unlink()
        before = cache.read_bytes()
        self.assertTrue(self.b(root, "ready", "app", "--branch", "job").strip().endswith("ready"))
        self.assertIn("ready to merge", self.b(root, "due", "app"))
        self.assertEqual(cache.read_bytes(), before)
        self.assertIn("verify: reused", self.b(root, "handback", "app", "--branch", "job",
                      "--state", "done", "--reviews", "2"))
        self.assertEqual((wt.parent / "verify-count").read_text(), "x")
        self.assertEqual(json.loads(record.read_text())["ready"]["integration_base"], base)
        record.unlink()  # continue with only the old standalone evidence for the cleanup probe
        self.git("merge", "--no-ff", "-m", "integrate job", "job", cwd=home)
        self.b(root, "wt", "rm", "app", "job", "--cause", "fixture cleanup")
        self.assertFalse(cache.exists())
        self.b(root, "jobs", "app", "--branch", "job", "--lessons", "none: fixture")
        ledger = (root / "nodes/app/job-ledger.md").read_text()
        self.assertIn(base, ledger)
        self.assertIn(packet["head"], ledger)

    def team(self):
        root = self.init("alice")
        seed = self.repo(self.tmp / "seed")
        self.git("branch", "-m", "trunk", cwd=seed)
        origin = self.tmp / "origin.git"
        self.git("clone", "--bare", str(seed), str(origin), cwd=self.tmp)
        home = self.tmp / "app"
        self.git("clone", str(origin), str(home), cwd=self.tmp)
        self.b(root, "new", "app", "--title", "App", "--home", str(home))
        self.b(root, "set", "app", "verify=python3 -c 'pass'", "merge=pr")
        return root, seed, origin, home

    def advance(self, seed, origin, label="other"):
        self.git("checkout", "-b", label, cwd=seed)
        (seed / label).write_text(label)
        self.git("add", "-A", cwd=seed)
        self.git("commit", "-qm", label, cwd=seed)
        self.git("checkout", "trunk", cwd=seed)
        self.git("merge", "--no-ff", "-m", "Merge made-up pull request", label, cwd=seed)
        self.git("push", str(origin), "trunk", cwd=seed)
        return self.git("rev-parse", "trunk", cwd=seed).strip()

    def test_remote_default_readiness_lag_and_packet_invalidation(self):
        root, seed, origin, home = self.team()
        self.git("branch", "main", cwd=home)  # a plausible stale local main must not hide origin's default
        self.b(root, "wt", "add", "app", "job")
        wt = self.tmp / "app-work/worktrees/job"
        self.b(root, "handback", "app", "--branch", "job", "--state", "done", "--reviews", "1")
        old = self.git("rev-parse", "HEAD", cwd=home)
        (home / "operator.txt").write_text("unfinished made-up work")
        merge = self.advance(seed, origin)
        # Nothing fetches as a side effect of ready/handback.
        self.assertTrue(self.b(root, "ready", "app", "--branch", "job").strip().endswith("ready"))
        self.git("fetch", "origin", cwd=home)
        out = self.b(root, "ready", "app", "--branch", "job", ok=False)
        self.assertIn("local trunk is 2 behind", out)
        self.assertIn("In the job worktree: git rebase origin/trunk", out)
        self.assertIn("main moved", out)
        self.assertIn("rebase on current main", out)
        out = self.b(root, "handback", "app", "--branch", "job", "--state", "done", "--reviews", "1", ok=False)
        self.assertIn("local trunk is 2 behind", out)
        self.git("rebase", "origin/trunk", cwd=wt)
        out = self.b(root, "handback", "app", "--branch", "job", "--state", "done", "--reviews", "1")
        self.assertIn("local trunk is 2 behind", out)
        record = json.loads(next((root / "nodes/app/job-handbacks").glob("*.json")).read_text())
        self.assertEqual(record["ready"]["main"], merge)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=home), old)
        self.assertEqual((home / "operator.txt").read_text(), "unfinished made-up work")
        self.assertTrue(self.b(root, "ready", "app", "--branch", "job").strip().endswith("ready"))
        self.b(root, "set", "app", "merge=invalid", ok=False)
        self.assertIn("merge: pr", (root / "nodes/app/node.md").read_text())

    def test_external_integrations_once_including_squash_and_no_remote_mutation(self):
        root, seed, origin, home = self.team()
        self.b(root, "sync", "app", "--offline", "--mark")
        merge = self.advance(seed, origin)
        self.git("commit", "--allow-empty", "-qm", "Squashed made-up change (#23)", cwd=seed)
        squash = self.git("rev-parse", "HEAD", cwd=seed).strip()
        self.git("push", str(origin), "trunk", cwd=seed)
        before = self.git("rev-parse", "HEAD", cwd=home)
        (home / "draft").write_text("operator work")
        self.assertIn("2 recorded", self.b(root, "sync", "app"))
        ledger = root / "nodes/app/job-ledger.md"
        text = ledger.read_text()
        self.assertEqual(text.count("external:" + merge), 1)
        self.assertEqual(text.count("external:" + squash), 1)
        self.assertIn("0 recorded", self.b(root, "sync", "app", "--offline"))
        self.b(root, "sync", "app", "--offline", "--mark")
        self.assertEqual(ledger.read_text(), text)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=home), before)
        self.assertEqual((home / "draft").read_text(), "operator work")
        # Replaying without the machine cursor must still deduplicate by full identity.
        local = root / ".brain/local.json"
        data = json.loads(local.read_text()); data["nodes"]["app"].pop("synced_main")
        data["nodes"]["app"].pop("synced_at")
        local.write_text(json.dumps(data))
        self.b(root, "sync", "app", "--offline")
        self.assertEqual(ledger.read_text(), text)

    def test_known_job_merge_and_rewritten_history(self):
        root, seed, origin, home = self.team()
        self.b(root, "sync", "app", "--offline")
        self.b(root, "wt", "add", "app", "job")
        wt = self.tmp / "app-work/worktrees/job"
        (wt / "fix").write_text("made-up change")
        self.git("add", "-A", cwd=wt)
        self.git("commit", "-qm", "fix", cwd=wt)
        self.b(root, "handback", "app", "--branch", "job", "--state", "done", "--reviews", "1")
        self.git("fetch", str(home), "job:job", cwd=seed)
        self.git("merge", "--no-ff", "-m", "Merge owned job", "job", cwd=seed)
        merge = self.git("rev-parse", "HEAD", cwd=seed).strip()
        self.git("push", str(origin), "trunk", cwd=seed)
        self.git("worktree", "remove", str(wt), cwd=home)
        self.git("branch", "-D", "job", cwd=home)
        self.assertIn("0 recorded", self.b(root, "sync", "app"))
        ledger = (root / "nodes/app/job-ledger.md").read_text()
        self.assertNotIn("external:", ledger)
        self.assertIn(merge, ledger)
        local = root / ".brain/local.json"
        before = json.loads(local.read_text())["nodes"]["app"]["synced_main"]
        # Force a non-descendant remote history, never a real repository.
        self.git("checkout", "--orphan", "replacement", cwd=seed)
        self.git("commit", "-qm", "New made-up root", cwd=seed)
        self.git("push", "--force", str(origin), "replacement:trunk", cwd=seed)
        self.assertIn("history changed", self.b(root, "sync", "app"))
        self.assertEqual(json.loads(local.read_text())["nodes"]["app"]["synced_main"], before)
        self.assertEqual((root / "nodes/app/job-ledger.md").read_text(), ledger)
        # A deliberate rescan recovers changed history and still deduplicates all old rows.
        self.b(root, "sync", "app", "--offline", "--reset-main", "--mark")
        self.assertEqual(json.loads(local.read_text())["nodes"]["app"]["synced_main"],
                         self.git("rev-parse", "HEAD", cwd=seed).strip())

    def test_fresh_branch_does_not_claim_external_merge(self):
        root, seed, origin, home=self.team()
        self.b(root,'sync','app','--offline')
        commit=self.advance(seed,origin)
        self.git('fetch','origin',cwd=home)
        self.b(root,'wt','add','app','job')
        self.assertIn('1 recorded',self.b(root,'sync','app','--offline'))
    def test_squash_owned_job_does_not_create_second_row(self):
        root, seed, origin, home=self.team()
        self.b(root,'sync','app','--offline')
        self.b(root,'wt','add','app','job')
        wt=self.tmp/'app-work/worktrees/job'
        (wt/'fix').write_text('fake fix')
        self.git('add','-A',cwd=wt); self.git('commit','-qm','fix',cwd=wt)
        self.b(root,'handback','app','--branch','job','--state','done','--reviews','1')
        self.git('fetch',str(home),'job:job',cwd=seed)
        self.git('merge','--squash','job',cwd=seed);self.git('commit','-qm','fix (#5)',cwd=seed)
        self.git('push',str(origin),'trunk',cwd=seed)
        self.git("worktree", "remove", str(wt), cwd=home)
        self.git("branch", "-D", "job", cwd=home)
        self.assertIn('0 recorded',self.b(root,'sync','app'))

    def test_changed_origin_default_uses_new_cached_symbolic_target(self):
        root, seed, origin, home = self.team()
        self.git("checkout", "-b", "stable", cwd=seed)
        self.git("commit", "--allow-empty", "-qm", "new default", cwd=seed)
        self.git("push", str(origin), "stable", cwd=seed)
        self.git("symbolic-ref", "HEAD", "refs/heads/stable", cwd=origin)
        self.git("fetch", "origin", cwd=home)
        self.git("remote", "set-head", "origin", "-a", cwd=home)
        self.b(root, "wt", "add", "app", "job")
        out = self.b(root, "handback", "app", "--branch", "job", "--state", "done", "--reviews", "1")
        self.assertIn("origin/stable", out)
        packet = json.loads(next((root / "nodes/app/job-handbacks").glob("*.json")).read_text())["ready"]
        self.assertEqual(packet["main"], self.git("rev-parse", "stable", cwd=seed).strip())

    def test_external_rows_do_not_displace_jobs_or_count_as_finished(self):
        root, seed, origin, home = self.team()
        self.b(root, "wt", "add", "app", "job")
        loader = core_loader.SourceFileLoader("team_summary", str(CORE / "bin/brain"))
        module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(module)
        inst = module.Instance(root)
        node = inst.node("app")
        rows = module.read_jobs(node)
        rows += [{"Job": "external:" + str(i), "Started": "2026-01-01T00:00:00+00:00",
                  "Ended": "2026-01-01T00:00:00+00:00", "Outcome": "external"} for i in range(45)]
        module.write_jobs(inst, node, rows)
        summary = module.jobs_summary(inst, node)
        self.assertEqual(summary["external"], 45)
        self.assertEqual(summary["finished"], 0)
        self.assertEqual([row["Job"] for row in summary["rows"]], ["job"])
        payload = json.loads(self.b(root, "ui", "--json"))
        app = next(n for n in payload["nodes"] if n["id"] == "app")
        self.assertEqual([row["Job"] for row in app["jobs"]["rows"]], ["job"])
        self.assertEqual(app["jobs"]["external"], 45)

    def test_origin_head_cannot_point_at_local_branch_and_missing_head_can_be_repaired(self):
        root, seed, origin, home = self.team()
        self.b(root, "wt", "add", "app", "job")
        self.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/heads/trunk", cwd=home)
        self.assertIn("integration main is missing", self.b(root, "handback", "app", "--branch", "job",
                      "--state", "done", "--reviews", "1", ok=False))
        out = self.b(root, "sync", "app", "--offline", "--mark")
        self.assertIn("not marked", out)
        self.assertIn("set-head origin -a", out)
        self.git("remote", "set-head", "origin", "-a", cwd=home)
        self.assertIn("marked synced", self.b(root, "sync", "app", "--offline", "--mark"))

    def test_local_mode_includes_unpublished_local_main_and_invalidates_packets(self):
        root, seed, origin, home = self.team()
        self.b(root, "set", "app", "merge=local")
        (home / "local-work").write_text("unpublished")
        self.git("add", "-A", cwd=home)
        self.git("commit", "-qm", "local integration", cwd=home)
        self.b(root, "wt", "add", "app", "job", "--base", "origin/trunk")
        wt = self.tmp / "app-work/worktrees/job"
        self.assertIn("current local main", self.b(root, "handback", "app", "--branch", "job", "--state", "done",
                      "--reviews", "1", ok=False))
        self.git("rebase", "trunk", cwd=wt)
        self.b(root, "handback", "app", "--branch", "job", "--state", "done", "--reviews", "1")
        (home / "second-local-work").write_text("another unpublished integration")
        self.git("add", "-A", cwd=home)
        self.git("commit", "-qm", "another local integration", cwd=home)
        self.assertIn("current local main", self.b(root, "ready", "app", "--branch", "job", ok=False))

    def test_fast_forward_job_range_is_owned_and_survives_cleanup_and_replay(self):
        root, seed, origin, home = self.team()
        self.b(root, "sync", "app", "--offline")
        self.b(root, "wt", "add", "app", "job")
        wt = self.tmp / "app-work/worktrees/job"
        for i in range(3):
            (wt / "fix").write_text(str(i))
            self.git("add", "-A", cwd=wt)
            self.git("commit", "-qm", "made-up fix", cwd=wt)
        self.b(root, "handback", "app", "--branch", "job", "--state", "done", "--reviews", "1")
        self.git("fetch", str(home), "job:job", cwd=seed)
        self.git("merge", "--ff-only", "job", cwd=seed)
        self.git("push", str(origin), "trunk", cwd=seed)
        self.assertIn("0 recorded", self.b(root, "sync", "app"))
        for record in (root / "nodes/app/job-handbacks").glob("*.json"):
            record.unlink()  # simulate post-merge evidence cleanup
        local = root / ".brain/local.json"
        data = json.loads(local.read_text()); data["nodes"]["app"].pop("synced_main")
        local.write_text(json.dumps(data))
        self.assertIn("0 recorded", self.b(root, "sync", "app", "--offline"))
        self.assertNotIn("external:", (root / "nodes/app/job-ledger.md").read_text())

    def closed_before_sync(self, strategy, push_first=True, prune=False, legacy=False, backfill=False):
        root, seed, origin, home = self.team()
        self.b(root, "set", "app", "merge=local")
        self.b(root, "sync", "app", "--offline")
        self.b(root, "wt", "add", "app", "job")
        wt = self.tmp / "app-work/worktrees/job"
        for i in range(3):
            (wt / "fix").write_text(str(i))
            self.git("add", "-A", cwd=wt)
            self.git("commit", "-qm", "made-up change", cwd=wt)
        self.b(root, "handback", "app", "--branch", "job", "--state", "done", "--reviews", "1")
        if legacy:
            import hashlib
            record = next((root / "nodes/app/job-handbacks").glob("*.json"))
            packet = json.loads(record.read_text())["ready"]
            packet["checked_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            cache = self.tmp / "app-work/scratch/ready" / (hashlib.sha256(b"job").hexdigest() + ".json")
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(packet))
            record.unlink()
        if strategy == "squash":
            self.git("merge", "--squash", "job", cwd=home)
            self.git("commit", "-qm", "squashed made-up job", cwd=home)
        else:
            self.git("merge", "--ff-only" if strategy == "fast-forward" else "--no-ff",
                     "-m", "integrated made-up job", "job", cwd=home)
        merged = self.git("rev-parse", "HEAD", cwd=home).strip()
        if push_first:
            self.git("push", "origin", "trunk", cwd=home)
        if prune:
            self.b(root, "wt", "prune", "app", "--offline")
        else:
            self.b(root, "wt", "rm", "app", "job", "--cause", "fixture cleanup")
        if backfill:  # a pre-upgrade closed row still has its worker evidence, but no endpoint columns
            loader = core_loader.SourceFileLoader("team_backfill", str(CORE / "bin/brain"))
            module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
            loader.exec_module(module)
            inst = module.Instance(root); node = inst.node("app"); rows = module.read_jobs(node)
            for row in rows:
                row.pop("Base commit", None); row.pop("Head commit", None)
            module.write_jobs(inst, node, rows)
        self.b(root, "jobs", "app", "--branch", "job", "--lessons", "none: fixture")
        self.assertEqual(list((root / "nodes/app/job-handbacks").glob("*.json")), [])
        self.git("branch", "-D", "job", cwd=home)
        if not push_first:
            self.git("push", "origin", "trunk", cwd=home)
        self.assertIn("0 recorded", self.b(root, "sync", "app"))
        ledger = (root / "nodes/app/job-ledger.md").read_text()
        self.assertNotIn("external:", ledger)
        self.assertIn(merged, ledger)

    def test_local_merge_closed_with_lessons_before_sync(self):
        self.closed_before_sync("merge")

    def test_legacy_packet_is_retained_before_close_deletes_it(self):
        self.closed_before_sync("merge", legacy=True)

    def test_lesson_cleanup_backfills_preupgrade_closed_row_before_deleting_packet(self):
        self.closed_before_sync("merge", backfill=True)

    def test_squash_closed_before_push_and_sync(self):
        self.closed_before_sync("squash", push_first=False)

    def test_fast_forward_pruned_with_lessons_before_sync(self):
        self.closed_before_sync("fast-forward", prune=True)

    def test_local_job_does_not_claim_earlier_unpublished_work_from_other_owners(self):
        root, seed, origin, home = self.team()
        self.b(root, "set", "app", "merge=local")
        self.b(root, "sync", "app", "--offline")
        (home / "other").write_text("another owner's unpublished work")
        self.git("add", "-A", cwd=home)
        self.git("commit", "-qm", "another owner's change", cwd=home)
        other = self.git("rev-parse", "HEAD", cwd=home).strip()
        self.b(root, "wt", "add", "app", "job", "--base", "trunk")
        wt = self.tmp / "app-work/worktrees/job"
        (wt / "fix").write_text("job contribution")
        self.git("add", "-A", cwd=wt)
        self.git("commit", "-qm", "fix", cwd=wt)
        self.b(root, "handback", "app", "--branch", "job", "--state", "done", "--reviews", "1")
        self.git("merge", "--no-ff", "-m", "job integration", "job", cwd=home)
        self.git("push", "origin", "trunk", cwd=home)
        self.b(root, "wt", "rm", "app", "job")
        self.b(root, "jobs", "app", "--branch", "job", "--lessons", "none: fixture")
        self.assertIn("1 recorded", self.b(root, "sync", "app"))
        self.assertEqual((root / "nodes/app/job-ledger.md").read_text().count("external:" + other), 1)

    def test_local_mode_requires_current_default_before_merging_and_records_other_owner_once(self):
        root, seed, origin, home = self.team()
        self.b(root, "set", "app", "merge=local")
        self.b(root, "sync", "app", "--offline")
        other = self.advance(seed, origin)
        self.git("fetch", "origin", cwd=home)
        self.b(root, "wt", "add", "app", "job")
        wt = self.tmp / "app-work/worktrees/job"
        (wt / "fix").write_text("worker change")
        self.git("add", "-A", cwd=wt)
        self.git("commit", "-qm", "fix", cwd=wt)
        self.assertIn("local default must include origin default", self.b(root, "handback", "app", "--branch", "job",
                      "--state", "done", "--reviews", "1", ok=False))
        # The fixture owns this checkout; production code never updates an operator checkout.
        self.git("merge", "--ff-only", "origin/trunk", cwd=home)
        self.b(root, "handback", "app", "--branch", "job", "--state", "done", "--reviews", "1")
        self.git("merge", "--no-ff", "-m", "made-up job merge", "job", cwd=home)
        self.git("push", "origin", "trunk", cwd=home)
        self.assertIn("1 recorded", self.b(root, "sync", "app"))
        self.assertEqual((root / "nodes/app/job-ledger.md").read_text().count("external:" + other), 1)
        self.assertIn("0 recorded", self.b(root, "sync", "app", "--offline"))

    def test_reset_is_bounded_and_requires_one_node(self):
        root, seed, origin, home = self.team()
        old = self.env.copy()
        self.env.update(GIT_AUTHOR_DATE="2020-01-01T00:00:00+00:00", GIT_COMMITTER_DATE="2020-01-01T00:00:00+00:00")
        for i in range(3):
            self.git("commit", "--allow-empty", "-qm", "old history", cwd=seed)
        self.env = old
        self.git("push", str(origin), "trunk", cwd=seed)
        self.assertIn("0 recorded", self.b(root, "sync", "app"))
        self.assertIn("0 recorded", self.b(root, "sync", "app", "--offline", "--reset-main"))
        local = root / ".brain/local.json"
        data = json.loads(local.read_text()); data["nodes"]["app"].pop("synced_main")
        local.write_text(json.dumps(data))
        self.assertIn("0 recorded", self.b(root, "sync", "app", "--offline", "--reset-main"))
        self.assertIn("one node", self.b(root, "sync", "--all", "--reset-main", ok=False))

    def test_main_job_branch_is_still_refused_with_another_remote_default(self):
        root, seed, origin, home = self.team()
        self.b(root, "wt", "add", "app", "main")
        self.assertIn("job branch must differ from main", self.b(root, "handback", "app", "--branch", "main",
                      "--state", "done", "--reviews", "1", ok=False))

    def test_failed_ledger_import_cannot_mark_past_unrecorded_merges(self):
        root, seed, origin, home = self.team()
        ledger = root / "nodes/app/job-ledger.md"
        ledger.write_text("Not a brain ledger\n")
        self.advance(seed, origin)
        out = self.b(root, "sync", "app", "--mark")
        self.assertIn("not marked", out)
        data = json.loads((root / ".brain/local.json").read_text())["nodes"]["app"]
        self.assertNotIn("synced_at", data)
        self.assertNotIn("synced_main", data)
        self.assertEqual(ledger.read_text(), "Not a brain ledger\n")
        ledger.unlink()
        self.assertIn("1 recorded", self.b(root, "sync", "app", "--offline", "--mark"))

    def test_missing_origin_head_does_not_fall_back_to_local_main(self):
        root, seed, origin, home = self.team()
        self.git("branch", "main", cwd=home)
        self.b(root, "wt", "add", "app", "job")
        self.git("symbolic-ref", "--delete", "refs/remotes/origin/HEAD", cwd=home)
        out = self.b(root, "handback", "app", "--branch", "job", "--state", "done", "--reviews", "1", ok=False)
        self.assertIn("origin default branch unavailable", out)
        self.assertIn("integration main is missing", out)



if __name__ == "__main__":
    unittest.main()
