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


class WorkerBriefs(Scenario):
    def test_hire_branch_names_the_same_worktree_and_ports_as_brief(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "code" / "app")
        self.b(r, "new", "app", "--title", "App", "--home", str(home))
        self.b(r, "set", "app", "runtime=codex")
        self.b(r, "specialist", "new", "qa", "--title", "QA", "--runtime", "codex")
        for ports in (1, 3):
            with self.subTest(ports=ports):
                self.b(r, "set", "app", f"ports={ports}")
                branch = f"job-{ports}"
                self.b(r, "wt", "add", "app", branch)
                args = ("--branch", branch, "--task", "test it", "--model", "sample-model")
                brief = self.b(r, "brief", "app", *args)
                hire = self.b(r, "hire", "qa", "app", *args)
                run_line = next(line for line in brief.splitlines() if line.startswith("Run in:"))
                self.assertIn(run_line, hire.splitlines())
                local = json.loads((r / ".brain" / "local.json").read_text())
                port = local["nodes"]["app"]["worktrees"][branch]["port"]
                expected_ports = f"port {port}" if ports == 1 else f"ports {port}-{port + ports - 1}"
                self.assertTrue(run_line.endswith("  ·  " + expected_ports), run_line)
        # Without a branch, keep the existing home-based specialist brief.
        hire = self.b(r, "hire", "qa", "app", "--task", "test it")
        self.assertIn(f"Run in: {home}  ·  runtime: codex", hire)
        self.assertNotIn("  ·  branch ", hire)

    def test_hire_branch_refuses_unknown_or_missing_worktrees(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "code" / "app")
        self.b(r, "new", "app", "--title", "App", "--home", str(home))
        self.b(r, "specialist", "new", "qa", "--title", "QA")
        out = self.b(r, "hire", "qa", "app", "--branch", "missing", "--task", "test it", ok=False)
        self.assertIn("no worktree for missing", out)
        self.b(r, "wt", "add", "app", "job")
        shutil.rmtree(self.tmp / "code" / "app-work" / "worktrees" / "job")
        out = self.b(r, "hire", "qa", "app", "--branch", "job", "--task", "test it", ok=False)
        self.assertIn("the worktree for job is gone", out)

    def test_a_brief_carries_the_job_its_limits_and_the_hand_back(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "code" / "app")
        self.b(r, "new", "app", "--title", "App", "--home", str(home))
        self.b(r, "set", "app", "coordinator=thr-app", "ports=3")
        brief = r / "nodes" / "app" / "brief.md"
        text = brief.read_text()
        self.assertIn("## Limits", text)                              # the template has the section
        brief.write_text(text.replace("## Limits", (
            "## Limits\n\n- At most 2 worktrees at once. Why: the disk is nearly full. Lift when: over 50 GB free.\n"
            "- No deploys.\n")))
        self.b(r, "wt", "add", "app", "job-a")
        out = self.b(r, "brief", "app", "--branch", "job-a", "--task", "Fix the login bug.")
        self.assertIn("Run in: ~/code/app-work/worktrees/job-a", out)
        self.assertIn("ports ", out)                                 # the job's block of ports
        self.assertIn("Fix the login bug.", out)
        self.assertIn("Lift when: over 50 GB free", out)              # limits travel with their reasons
        self.assertIn("Never ask the operator", out)
        self.assertIn("thr-app", out)                                 # who to wake, and how
        self.assertIn("t3_thread_send", out)
        self.assertNotIn("~/alice/nodes/app/inbox.md", out)  # the unified record carries questions too
        self.assertIn("Lessons", out)
        self.b(r, "brief", "app", "--branch", "nope", ok=False)
        # the same hand-back in a specialist's brief and a review brief
        self.b(r, "specialist", "new", "qa", "--title", "QA")
        hb = self.b(r, "hire", "qa", "app", "--task", "test it")
        self.assertIn("Never ask the operator", hb)
        self.assertIn("Lift when: over 50 GB free", hb)
        rb = self.b(r, "review", "app", "--branch", "job-a")
        self.assertIn("thr-app", rb)
        self.assertIn("Lift when: over 50 GB free", rb)
        self.assertNotIn("inbox.md", rb)                              # a reviewer changes nothing, not even that
        self.assertIn("Write no file", rb)
        # a limit with no reason or end is flagged; show gives the status time in UTC for host times
        self.assertIn("a limit in brief.md has no Why: or Lift when:", self.b(r, "check"))
        self.assertRegex(self.b(r, "show", "app"), r"\(= \d{4}-\d{2}-\d{2}T\d{2}:\d{2}Z")
        # a sealed node's brief names nothing above it
        self.b(r, "principal", "add", "team")
        sh = self.repo(self.tmp / "code" / "shared")
        self.b(r, "new", "shared", "--title", "Shared", "--owners", "team", "--home", str(sh))
        sb = self.b(r, "brief", "shared", "--task", "x")
        self.assertNotIn("alice", sb)
        loc = json.loads((r / ".brain" / "local.json").read_text())   # this machine loses the home's path
        home_was = loc["nodes"]["shared"].pop("home")
        (r / ".brain" / "local.json").write_text(json.dumps(loc))
        out = self.b(r, "brief", "shared", "--task", "x", ok=False)
        self.assertIn("isn't on this machine", out)
        loc["nodes"]["shared"]["home"] = home_was
        (r / ".brain" / "local.json").write_text(json.dumps(loc))
        self.assertIn("Leave nothing in it that names", sb)
        d = Path(self.b(r, "report", "app", "job a").strip())
        self.assertIn("## Lessons", (d / "index.md").read_text())

    def test_words_a_home_must_never_contain(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "code" / "app", {"README.md": "app\n", ".gitignore": "backups/\n"})
        self.b(r, "new", "app", "--title", "App", "--home", str(home))
        (r / "nodes" / "app" / "never-name.txt").write_text("# earlier codebases\nOldCo\n")
        self.assertIn("guard: clean", self.b(r, "guard", "app"))
        (home / "backups").mkdir()
        (home / "backups" / "dump.bin").write_text("xx oldco xx\n")       # ignored files don't count
        self.assertIn("guard: clean", self.b(r, "guard", "app"))
        (home / "notes.md").write_text("ported from OLDCO\n")               # untracked, any case
        out = self.b(r, "guard", "app", ok=False)
        self.assertIn("notes.md: term 1 of app/never-name.txt", out)
        self.assertNotIn("oldco", out.lower())                               # never shows the word
        self.assertIn("contains term 1 of app/never-name.txt", self.b(r, "check", ok=False))
        (home / "notes.md").unlink()
        out = self.b(r, "wt", "add", "app", "port-oldco", ok=False)        # brain won't write the word itself
        self.assertNotIn("oldco", out.lower())
        self.assertFalse((r / "nodes" / "app" / "job-ledger.md").exists())
        self.git("branch", "port-OldCo", cwd=home)                           # a branch name someone else made
        self.b(r, "wt", "add", "app", "port")
        w = self.tmp / "code" / "app-work" / "worktrees" / "port"
        (w / "x.md").write_text("x\n")
        self.git("add", "x.md", cwd=w)
        self.git("commit", "-qm", "like oldco did", cwd=w)                   # a commit message
        (w / "oldco-notes.txt").write_text("nothing here\n")                 # a file name
        out = self.b(r, "guard", "app", ok=False)
        self.assertIn("branch port-[…]", out)
        self.assertIn("[…]-notes.txt", out)
        self.assertRegex(out, r"commit [0-9a-f]+ in")
        self.assertNotIn("oldco", out.lower())
        self.assertNotIn("oldco", self.b(r, "check", ok=False).lower())
        self.assertNotIn("oldco", self.b(r, "ui", "--json").lower())       # the live view shows it as guard does
        out = self.b(r, "guard", "app", str(self.tmp / "nowhere"), ok=False)  # a path that isn't there isn't clean
        self.assertIn("not found", out)
        (w / "oldco-notes.txt").unlink()
        # a child's home is covered by the parent's words; briefs that carry a word are refused
        kid = self.repo(self.tmp / "code" / "kid", {"a.md": "OldCo inside\n"})
        self.b(r, "new", "app/kid", "--title", "Kid", "--home", str(kid))
        self.assertIn("a.md: term 1 of app/never-name.txt", self.b(r, "guard", "app/kid", ok=False))
        self.assertIn("guard", self.b(r, "brief", "app", "--task", "tidy up"))
        out = self.b(r, "brief", "app", "--task", "remove what came from OldCo", ok=False)
        self.assertNotIn("oldco", out.lower())
        self.b(r, "new", "plain", "--title", "Plain")
        self.assertIn("nothing to check", self.b(r, "guard", "plain", str(kid)))
        # a node whose home is its own folder: its files are checked, its notes (which state the rule) aren't
        self.b(r, "new", "app/notes", "--title", "Notes")
        nd = next(p_.parent for p_ in r.rglob("node.md") if "id: app/notes" in p_.read_text())
        (nd / "brief.md").write_text((nd / "brief.md").read_text() + "\nNever name OldCo.\n")
        self.assertIn("guard: clean", self.b(r, "guard", "app/notes"))
        (nd / "sources" / "draft.md").write_text("from OldCo\n")
        self.assertIn("draft.md: term 1 of app/never-name.txt", self.b(r, "guard", "app/notes", ok=False))



class RuntimeFallbackResume(Scenario):
    def test_fallback_is_a_plain_machine_local_order(self):
        r = self.init("alice")
        head = self.git("rev-parse", "HEAD", cwd=r)
        self.b(r, "set", "root", "fallback=claudeAgent,codex")
        local = json.loads((r / ".brain/local.json").read_text())
        self.assertEqual(local["fallback"], ["claudeAgent", "codex"])
        self.assertNotIn("fallback:", (r / "node.md").read_text())
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=r), head)
        self.assertIn('["claudeAgent", "codex"]', self.b(r, "machine"))
        self.assertEqual(json.loads(self.b(r, "ui", "--json"))["brain"]["fallback"], local["fallback"])
        self.b(r, "set", "root", "fallback=codex,codex", ok=False)
        self.b(r, "set", "root", "fallback=codex,,claudeAgent", ok=False)
        self.b(r, "new", "a", "--title", "A")
        self.b(r, "set", "a", "fallback=codex", ok=False)
        local["fallback"] = {"first": "codex"}
        (r / ".brain/local.json").write_text(json.dumps(local))
        self.assertIn("fallback must be an ordered list", self.b(r, "check", ok=False))
        self.b(r, "set", "root", "fallback=")
        self.assertNotIn("fallback must", self.b(r, "check"))

    def test_partial_branch_brief_carries_resume_and_ledger_state(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "app")
        self.b(r, "new", "a", "--title", "A", "--home", str(home))
        self.b(r, "wt", "add", "a", "job")
        self.assertNotIn("## Resume existing work", self.b(r, "brief", "a", "--branch", "job"))
        wt = self.tmp / "app-work/worktrees/job"
        self.git("commit", "--allow-empty", "-qm", "partial", cwd=wt)
        sha = self.git("rev-parse", "HEAD", cwd=wt).strip()
        self.b(r, "jobs", "a", "--branch", "job", "--switch", "claudeAgent -> codex: usage limit")
        brief = self.b(r, "brief", "a", "--branch", "job", "--runtime", "codex", "--task", "Finish it")
        self.assertIn("Continue from these commits", brief)
        self.assertIn(sha, brief)
        self.assertIn("do not reset, recreate the branch", brief)
        self.assertIn("scratch/ready/", brief)
        self.assertIn("Job ledger state:", brief)
        self.assertIn("claudeAgent -> codex: usage limit", brief)
        self.assertIn("runtime: codex", brief)
        self.assertIn("Finish it", brief)



class ResumeBriefRegressions(Scenario):
    def job(self, base):
        r = self.init("alice")
        home = self.repo(self.tmp / "app")
        self.git("checkout", "-qb", "feature", cwd=home)
        self.git("commit", "--allow-empty", "-qm", "inherited work", cwd=home)
        if base == "origin/main":
            self.git("update-ref", "refs/remotes/origin/main", "HEAD", cwd=home)
            self.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main", cwd=home)
            self.git("checkout", "-q", "main", cwd=home)
        self.b(r, "new", "a", "--title", "A", "--home", str(home))
        self.b(r, "wt", "add", "a", "job")
        return r, self.tmp / "app-work/worktrees/job"

    def test_fresh_remote_base_does_not_resume_inherited_commits(self):
        r, wt = self.job("origin/main")
        self.assertNotIn("## Resume existing work", self.b(r, "brief", "a", "--branch", "job"))
        self.git("commit", "--allow-empty", "-qm", "own work", cwd=wt)
        self.assertIn("1 own commit(s)", self.b(r, "brief", "a", "--branch", "job"))

    def test_fresh_feature_head_base_does_not_resume_inherited_commits(self):
        r, wt = self.job("HEAD")
        self.assertNotIn("## Resume existing work", self.b(r, "brief", "a", "--branch", "job"))
        self.git("commit", "--allow-empty", "-qm", "own work", cwd=wt)
        self.assertIn("1 own commit(s)", self.b(r, "brief", "a", "--branch", "job"))

    def test_uncommitted_work_resumes_before_first_commit(self):
        r, wt = self.job("HEAD")
        (wt / "partial.txt").write_text("unfinished work\n")
        brief = self.b(r, "brief", "a", "--branch", "job")
        self.assertIn("## Resume existing work", brief)
        self.assertIn("0 own commit(s) and uncommitted changes", brief)
        self.b(r, "jobs", "a", "--branch", "job", "--switch", "claude -> codex: usage limit")
        self.assertIn("claude -> codex: usage limit", self.b(r, "brief", "a", "--branch", "job"))

    def test_switch_or_review_alone_resumes_before_first_commit(self):
        r, wt = self.job("HEAD")
        self.b(r, "review", "a", "--branch", "job", "--built-by", "claude")
        self.assertIn("## Resume existing work", self.b(r, "brief", "a", "--branch", "job"))
        self.b(r, "wt", "add", "a", "switched")
        self.b(r, "jobs", "a", "--branch", "switched", "--switch", "claude -> codex: usage limit")
        brief = self.b(r, "brief", "a", "--branch", "switched")
        self.assertIn("## Resume existing work", brief)
        self.assertIn("0 own commit(s)", brief)



class BriefLimits(Scenario):
    def setup_job(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "app")
        self.b(r, "new", "a", "--title", "A", "--home", str(home))
        self.b(r, "wt", "add", "a", "job")
        return r

    def test_large_specialist_and_item_are_referenced(self):
        r = self.setup_job()
        self.b(r, "specialist", "new", "qa", "--title", "QA")
        role = r / "specialists/qa/role.md"
        role.write_text(role.read_text() + "\n## Standards\n" + "specialist-detail " * 1300)
        self.b(r, "item", "add", "guide", "--from", "a", "--title", "Guide", "--kind", "method",
               "--ref", "fixture", "--body", "library-detail " * 1400)
        out = self.b(r, "hire", "qa", "a", "--task", "Test it", "--items", "guide")
        self.assertLess(len(out.encode()), 8192)
        self.assertIn("specialists/qa/role.md", out)
        self.assertIn("library/guide.md", out)
        self.assertNotIn("specialist-detail", out)
        self.assertNotIn("library-detail", out)
        self.assertIn("second context compaction", out)

    def test_reference_paths_obey_source_never_name_policy(self):
        r = self.setup_job()
        self.b(r, "new", "source", "--title", "Source")
        self.b(r, "specialist", "new", "qa", "--title", "QA")
        self.b(r, "item", "add", "guide", "--from", "source", "--title", "Guide", "--kind", "method",
               "--ref", "fixture", "--body", "Review guide")
        # The source bans a component of the actual library path, while the target allows it.
        (r / "nodes/source/never-name.txt").write_text("library\n")
        out = self.b(r, "hire", "qa", "a", "--task", "Review guide", "--items", "guide", ok=False)
        self.assertNotIn("/library/guide.md", out)
        out = self.b(r, "brief", "a", "--task", "Review guide")
        self.assertNotIn("/library/guide.md", out)
        self.b(r, "specialist", "new", "reader", "--title", "Reader", "--from", "source", "--at", "root", "--proposed")
        role = r / "specialists/reader/role.md"
        role.write_text(role.read_text().replace("status: proposed", "status: active"))
        (r / "nodes/source/never-name.txt").write_text("specialists\n")
        out = self.b(r, "hire", "reader", "a", "--task", "Read", ok=False)
        self.assertNotIn("/specialists/reader/role.md", out)

    def test_all_generators_enforce_size_before_output_or_records(self):
        r = self.setup_job()
        self.b(r, "specialist", "new", "qa", "--title", "QA")
        for command in [("brief", "a", "--task"), ("hire", "qa", "a", "--task"),
                        ("review", "a", "--branch", "job", "--focus")]:
            out = self.b(r, *command, "x" * (9 * 1024))
            self.assertIn("warning: brief", out)
            self.assertIn("largest sections:", out)
            destination = self.tmp / (command[0] + ".md")
            ledger_before = (r / "nodes/a/job-ledger.md").read_bytes()
            # Permit the fixture's second full review to reach the size refusal.
            reason = ("--reason", "security") if command[0] == "review" else ()
            out = self.b(r, *command, "x" * (17 * 1024), "--out", str(destination), *reason, ok=False)
            self.assertIn("refused:", out)
            self.assertIn("Task" if command[0] != "review" else "Focus", out)
            self.assertFalse(destination.exists())
            self.assertEqual(ledger_before, (r / "nodes/a/job-ledger.md").read_bytes())
        self.b(r, "set", "a", "brief_max_kb=32")
        self.assertNotIn("warning: brief", self.b(r, "brief", "a", "--task", "x" * 17000))
        self.b(r, "set", "a", "brief_max_kb=nan")
        self.assertIn("positive finite", self.b(r, "brief", "a", ok=False))

    def test_compaction_restart_due_and_fresh_brief_resume_without_commits(self):
        r = self.setup_job()
        self.b(r, "brief", "a", "--branch", "job", "--task", "Finish fixture work")
        self.b(r, "handback", "a", "--branch", "job", "--state", "done", "--restart", "compactions", ok=False)
        self.b(r, "handback", "a", "--branch", "job", "--state", "blocked", "--restart", "compactions")
        due = json.loads(self.b(r, "due", "a", "--json"))
        self.assertIn("restart", str(due))
        self.assertIn("brain brief a --branch job", str(due))
        self.assertIn("## Resume existing work", self.b(r, "brief", "a", "--branch", "job"))
        self.b(r, "handback", "a", "--branch", "job", "--state", "blocked")
        self.assertNotIn('"kind": "restart"', self.b(r, "due", "a", "--json"))

    def test_restart_replays_exact_inputs_and_acknowledges_each_signal(self):
        r = self.setup_job()
        task_file = self.tmp / "task with spaces.md"
        task_file.write_text("Complete the original fixture task.")
        context = self.tmp / "shared context.md"
        context.write_text("Existing fixture findings must survive restart.")
        for task_args, expected in [
                (["--task-file", str(task_file)], task_file.read_text()),
                (["--task", "Finish the inline fixture's remaining work."], "Finish the inline fixture's remaining work.")]:
            self.b(r, "brief", "a", "--branch", "job", *task_args, "--context-file", str(context))
            self.b(r, "handback", "a", "--branch", "job", "--state", "blocked", "--restart", "compactions")
            due = json.loads(self.b(r, "due", "a", "--json"))
            restart = next(i for i in due if i["kind"] == "restart")
            self.assertEqual(restart["state"], "blocked")
            self.b(r, "status", "a", "--refresh")
            finished = (r / "nodes/a/status.md").read_text().split("Finished jobs awaiting coordinator:", 1)[1]
            self.assertIn("restart: compactions", finished)
            # Refusal must preserve the old replay inputs and leave restart pending.
            self.b(r, "brief", "a", "--branch", "job", "--task", "x" * 18000, ok=False)
            self.assertTrue(any(i["kind"] == "restart" for i in json.loads(self.b(r, "due", "a", "--json"))))
            command = re.search(r"resume with `([^`]+)`", restart["text"]).group(1)
            args = shlex.split(command)
            self.assertIn("--context-file", args)
            self.assertIn(task_args[0], args)
            out = self.b(r, *args[1:])  # Run the exact command due supplies, without the executable.
            self.assertIn(expected, out)
            self.assertIn(context.read_text(), out)
            self.assertIn("## Resume existing work", out)
            self.assertNotIn("<!-- the task", out)
            self.assertFalse(any(i["kind"] in ("restart", "handback", "invalid-handback")
                                 for i in json.loads(self.b(r, "due", "a", "--json"))))
            # A plain subsequent brief also reuses the recorded inputs.
            self.assertIn(expected, self.b(r, "brief", "a", "--branch", "job"))

    def test_explicit_replacement_task_does_not_replay_old_context(self):
        r = self.setup_job()
        context = self.tmp / "context.md"
        context.write_text("Old fixture findings")
        self.b(r, "brief", "a", "--branch", "job", "--task", "Old task", "--context-file", str(context))
        context.write_text("ForbiddenFixturePhrase")
        (r / "nodes/a/never-name.txt").write_text("ForbiddenFixturePhrase\n")
        out = self.b(r, "brief", "a", "--branch", "job", "--task", "Replacement task")
        self.assertIn("Replacement task", out)
        self.assertNotIn("## Shared context", out)
        self.b(r, "handback", "a", "--branch", "job", "--state", "blocked", "--restart", "compactions")
        restart = next(i for i in json.loads(self.b(r, "due", "a", "--json")) if i["kind"] == "restart")
        self.assertNotIn("--context-file", restart["text"])
        self.assertIn("Replacement task", restart["text"])

    def test_explicit_context_does_not_replay_saved_task(self):
        r = self.setup_job()
        self.b(r, "brief", "a", "--branch", "job", "--task", "Previously saved fixture task")
        context = self.tmp / "context.md"
        context.write_text("New fixture context")
        out = self.b(r, "brief", "a", "--branch", "job", "--context-file", str(context))
        self.assertIn("New fixture context", out)
        self.assertNotIn("Previously saved fixture task", out)

    def test_legacy_restart_requires_original_task_and_reviewer_has_no_worker_policy(self):
        r = self.setup_job()
        self.b(r, "handback", "a", "--branch", "job", "--state", "blocked", "--restart", "compactions")
        out = self.b(r, "brief", "a", "--branch", "job", ok=False)
        self.assertIn("supply the original --task or --task-file", out)
        review = self.b(r, "review", "a", "--branch", "job")
        self.assertIn("Write no file", review)
        self.assertNotIn("second context compaction", review)
        self.assertNotIn("restart: compactions", review)

    def test_byte_measurement_and_exact_thresholds(self):
        sys.path.insert(0, str(CORE / "lib"))
        from brain import briefs
        n = {"fm": {}, "dir": self.tmp, "parent": None}
        # Isolate only privacy lookup; retain the real formatter and byte counter.
        with mock.patch.object(briefs._m_guard, "no_terms_in"):
            text = "## Task\n" + "x" * (8192 - 8)
            with contextlib.redirect_stderr(io.StringIO()) as err:
                output = briefs.checked_brief(n, text, inputs=[("Task", text)])
                self.assertIn("chargeable=8192;", output)
                total = int(re.search(r"; total=(\d+);", output).group(1))
                self.assertEqual(len(output.encode()), total)
            self.assertEqual(err.getvalue(), "")
            with contextlib.redirect_stderr(io.StringIO()) as err:
                briefs.checked_brief(n, text + "é", inputs=[("Task", text + "é")])
            self.assertIn("8194 chargeable bytes", err.getvalue())
            with contextlib.redirect_stderr(io.StringIO()) as err:
                output = briefs.checked_brief(n, text + "x" * 1024, inputs=[("Task", text + "x" * 1024)])
                self.assertIn("chargeable=9216;", output)
            self.assertIn("Task:", err.getvalue())
            for extra in (8192, 9216):
                with self.assertRaises(SystemExit) as refused:
                    briefs.checked_brief(n, text + "x" * extra, inputs=[("Task", text + "x" * extra)])
                self.assertIn("refused:", str(refused.exception))
                self.assertIn("Task:", str(refused.exception))



class R2BriefInputs(Scenario):
    def test_b11_all_worker_briefs_explicitly_omit_attribution_for_selected_runtime(self):
        r = self.init("sample")
        self.b(r, "specialist", "new", "qa", "--title", "QA")
        for runtime in ("codex", "claude"):
            for command in (("brief", "root"), ("brief", "root", "--small"), ("hire", "qa", "root")):
                out = self.b(r, *command, "--runtime", runtime, "--model", "sample-model", "--task", "Inspect fixture")
                self.assertIn("Commit attribution: omitted", out)
                self.assertNotIn("Co-Authored-By:", out)
                self.assertIn("runtime: " + runtime, out)
        sys.path.insert(0, str(CORE / "lib"))
        from brain import briefs
        self.assertIn("omitted", " ".join(briefs.attribution_lines("unknown", "unknown-model")))

    def test_r1_restart_acknowledgement_follows_successful_delivery(self):
        r = self.init("sample")
        home = self.repo(self.tmp / "home")
        self.b(r, "new", "app", "--title", "App", "--home", str(home))
        self.b(r, "wt", "add", "app", "job")
        m = core_loader.SourceFileLoader("r2_delivery", str(CORE / "bin/brain")).load_module()
        a = argparse.Namespace(id="app", small=False, branch="job", import_study=False,
            task="Inspect fixture", task_file=None, context_file=None, why_separate="fixture job",
            allow_unexplained=False, peer=None, runtime="codex", model="sample-model", job_class="build",
            effort=None, to=None, out=None)
        with mock.patch.dict(os.environ, self.env), mock.patch.object(m, "load", return_value=m.Instance(r)), \
             mock.patch.object(m, "acknowledge_usage_restart") as ack:
            with contextlib.redirect_stdout(io.StringIO()):
                m.cmd_brief(a)
            ack.assert_called_once()
            ack.reset_mock()
            stream = mock.Mock(wraps=io.StringIO())
            stream.flush.side_effect = BrokenPipeError("fixture delivery failure")
            with mock.patch.object(sys, "stdout", stream), self.assertRaises(BrokenPipeError):
                m.cmd_brief(a)
            ack.assert_not_called()

    def test_p18_input_bytes_ignore_fixed_text_and_forged_headings(self):
        sys.path.insert(0, str(CORE / "lib"))
        from brain import briefs
        n = {"fm": {}, "dir": self.tmp, "parent": None}
        task = "## Limits\n" + "é" * 2100
        context = "ع" * 2100
        with mock.patch.object(briefs._m_guard, "no_terms_in"):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                result = briefs.checked_brief(n, "fixed safety " * 1800 + task + context,
                                              inputs=[("Task", task), ("Shared context", context)])
            self.assertIn("warning: brief", err.getvalue())
            self.assertIn("chargeable", err.getvalue())
            self.assertIn("Task:", err.getvalue())
            self.assertIn("Shared context:", err.getvalue())
            self.assertIn("Brief bytes: chargeable=8410", result)
            self.assertIn("fixed safety", result)
            with contextlib.redirect_stderr(io.StringIO()) as err:
                briefs.checked_brief(n, "safety " * 3000, inputs=[("Task", "small")])
            self.assertEqual(err.getvalue(), "")
            with self.assertRaises(SystemExit):
                briefs.checked_brief(n, "x", inputs=[("Task", "é" * 8192)])
            with self.assertRaises(SystemExit):
                briefs.checked_brief(n, "x" * (256 * 1024), inputs=[])

    def test_p18_library_metadata_charged_in_worker_small_and_specialist(self):
        r = self.init("sample")
        self.b(r, "specialist", "new", "qa", "--title", "QA")
        self.b(r, "item", "add", "guide", "--from", "root", "--title", "Inspect " + "é" * 4500,
               "--kind", "method", "--ref", "fixture", "--body", "Inspect fixture")
        for command in (("brief", "root"), ("brief", "root", "--small"), ("hire", "qa", "root")):
            out = self.b(r, *command, "--task", "Inspect")
            self.assertIn("warning: brief", out)
            self.assertIn("Library metadata", out)
            charge = int(re.search(r"Brief bytes: chargeable=(\d+)", out).group(1))
            self.assertGreater(charge, 9000)
        m = core_loader.SourceFileLoader("r2_library_provenance", str(CORE / "bin/brain")).load_module()
        inst = m.Instance(r)
        inputs = []
        lines = m.knowledge_lines(inst, inst.node("root"), "Inspect", inputs=inputs)
        self.assertEqual(inputs, [("Library metadata", "guide"), ("Library metadata", "Inspect " + "é" * 4500)])
        self.assertGreater(len("\n".join(lines).encode()), sum(len(body.encode()) for _, body in inputs))


if __name__ == "__main__":
    unittest.main()
