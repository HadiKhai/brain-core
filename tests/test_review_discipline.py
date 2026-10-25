"""Review policy scenarios using only invented projects and findings."""
import json
import os
import subprocess
import sys
import time
import shlex
from unittest import mock
from scenario import Scenario
import test_verify as _test_verify
import core_loader


class ReviewDiscipline(Scenario):
    job = _test_verify.WorkerReadyPackets.job

    def review(self, root, *args, **kw):
        return self.b(root, "review", "a", "--branch", "job", "--built-by", "claude", *args, **kw)

    def job_row(self, root):
        text = self.b(root, "jobs", "a", "--branch", "job")
        return json.loads(next(line for line in text.splitlines() if line.startswith("{")))

    def findings(self):
        path = self.tmp / "findings.json"
        path.write_text(json.dumps([{"severity": "blocker", "text": "Unsafe calculation in calc.py:1"},
                                    {"severity": "major", "text": "Missing boundary check in calc.py:1"},
                                    {"severity": "minor", "text": "Unrelated formatting suggestion"}]))
        return str(path)

    def test_round_cap_and_all_exception_reasons(self):
        r, home, wt = self.job()
        self.assertIn("Review round 1 (full)", self.review(r))
        self.assertEqual(self.job_row(r)["Review reason"], "")
        before = (r / "nodes/a/job-ledger.md").read_text()
        self.assertIn("second full round", self.review(r, ok=False))
        self.assertEqual(before, (r / "nodes/a/job-ledger.md").read_text())
        self.git("commit", "--allow-empty", "-qm", "repair findings", cwd=wt)
        self.assertIn("Review round 2 (check-fix)", self.review(r, "--check-fix", "--findings", self.findings()))
        self.assertIn("review round 3 refused", self.review(r, ok=False))
        self.assertIn("review round 3 refused", self.review(r, "--check-fix", "--findings", self.findings(), ok=False))
        for i, reason in enumerate(("security", "schema", "privacy"), 3):
            self.assertIn(f"Review round {i} (full)", self.review(r, "--reason", reason))
        row = self.job_row(r)
        self.assertEqual(row["Reviews"], "5")
        self.assertEqual(row["Full reviews"], "4")
        self.assertEqual(row["Review reason"], "privacy")

    def test_fix_check_contains_only_prior_major_and_fix_range(self):
        r, home, wt = self.job()
        old = self.git("rev-parse", "HEAD", cwd=wt).strip()
        self.review(r)
        self.git("commit", "--allow-empty", "-qm", "repair boundary", cwd=wt)
        new = self.git("rev-parse", "HEAD", cwd=wt).strip()
        text = self.review(r, "--check-fix", "--findings", self.findings(), "--focus", "Unrelated focus")
        self.assertIn(f"{old}..{new}", text)
        self.assertIn("Missing boundary check", text)
        self.assertIn("Unsafe calculation", text)
        self.assertNotIn("Unrelated formatting", text)
        self.assertNotIn("Unrelated focus", text)
        for heading in ("## What to check", "## The job's contract", "## Change", "## Earlier rounds"):
            self.assertNotIn(heading, text)
        self.assertIn("did the fix break anything", text)
        self.assertIn("brain rehearse", text)

    def test_contract_audit_flags_missing_and_nonexistent_named_test(self):
        r, home, wt = self.job()
        (wt / "tests").mkdir()
        (wt / "tests/test_calc.py").write_text("def test_value():\n    assert True\n")
        self.git("add", "tests", cwd=wt)
        self.git("commit", "-qm", "add contract test", cwd=wt)
        contract = self.tmp / "contract.md"
        contract.write_text("## Done means\n- Value works. test: tests/test_calc.py::test_value\n"
                            "- Boundary holds.\n- Unknown. test: tests/test_calc.py::test_absent\n"
                            "- Deferred. gap: needs an external service\n")
        text = self.review(r, "--contract", str(contract))
        self.assertIn("Done item 1: named test tests/test_calc.py::test_value", text)
        self.assertIn("Done item 2: GAP", text)
        self.assertIn("Done item 3: GAP", text)
        self.assertIn("Done item 4: stated GAP", text)

    def test_fitness_runs_before_emission_without_leaking_output(self):
        r, home, wt = self.job()
        (wt / "fitness.py").write_text("from pathlib import Path\nPath('fitness-ran').write_text('yes')\n"
                                       "print('private gate output')\nraise SystemExit(7)\n")
        self.git("add", "fitness.py", cwd=wt)
        self.git("commit", "-qm", "add fitness gate", cwd=wt)
        self.b(r, "set", "a", "fitness=python3 fitness.py")
        text = self.review(r)
        self.assertEqual((wt / "fitness-ran").read_text(), "yes")
        self.assertIn("Fitness gate: FAIL (exit 7)", text)
        self.assertNotIn("private gate output", text)
        (wt / "fitness-ran").unlink()
        (wt / "fitness.py").write_text("raise SystemExit(0)\n")
        self.git("commit", "-qam", "repair fitness gate", cwd=wt)
        self.assertIn("Fitness gate: PASS", self.review(r, "--reason", "schema"))

    def test_seed_lint_flags_configured_names_and_home_paths(self):
        r, home, wt = self.job()
        (wt / "fixtures").mkdir()
        # Assemble invented private-looking strings so the repository itself stays portable.
        (wt / "fixtures/seed.txt").write_text("alice " + "/" + "home" + "/" + "example/data\n")
        self.git("add", "fixtures", cwd=wt)
        self.git("commit", "-qm", "add invented seed", cwd=wt)
        text = self.review(r)
        self.assertIn("Seed-text lint: FLAG", text)
        self.assertIn("1 changed test/fixture file(s), 1 flagged", text)
        self.assertNotIn("example/data", text)

    def test_never_name_contract_refuses_without_brief_or_round_increment(self):
        r, home, wt = self.job()
        word = "invented-vocabulary-omega"
        (r / "nodes/a/never-name.txt").write_text(word + "\n")
        contract = self.tmp / "contract.md"
        contract.write_text("## Done means\n- " + word + "\n")
        result = self.review(r, "--contract", str(contract), ok=False)
        self.assertNotIn(word, result)
        self.assertNotIn("Reviewer runtime:", result)
        self.assertEqual(self.job_row(r)["Reviews"], "0")

    def test_never_name_seed_and_findings_are_never_emitted(self):
        r, home, wt = self.job()
        word = "invented-vocabulary-omega"
        (r / "nodes/a/never-name.txt").write_text(word + "\n")
        (wt / "fixtures").mkdir()
        (wt / "fixtures/seed.txt").write_text(word)
        self.git("add", "fixtures", cwd=wt)
        self.git("commit", "-qm", "add unsafe seed", cwd=wt)
        text = self.review(r, ok=False)
        self.assertNotIn(word, text)
        self.assertIn("committed review evidence", text)
        (wt / "fixtures/seed.txt").write_text("invented safe seed")
        self.git("commit", "-qam", "remove unsafe seed", cwd=wt)
        self.review(r)
        file = self.tmp / "findings.json"
        file.write_text(json.dumps([{"severity": "minor", "text": word}]))
        text = self.review(r, "--check-fix", "--findings", str(file), ok=False)
        self.assertNotIn(word, text)
        self.assertNotIn("Reviewer runtime:", text)

    def test_fix_check_rejects_invalid_inputs_and_unrelated_base(self):
        r, home, wt = self.job()
        self.assertIn("prior review", self.review(r, "--check-fix", ok=False))
        self.review(r)
        self.assertIn("requires --findings", self.review(r, "--check-fix", ok=False))
        f = self.tmp / "bad.json"
        f.write_text('[{"severity":"major"}]')
        self.assertIn("JSON list", self.review(r, "--check-fix", "--findings", str(f), ok=False))
        self.assertIn("ancestor fix base", self.review(r, "--check-fix", "--findings", self.findings(),
                                                       "--fix-base", "missing-ref", ok=False))
        unrelated = self.git("commit-tree", "HEAD^{tree}", "-m", "unrelated root", cwd=wt).strip()
        self.assertIn("ancestor fix base", self.review(r, "--check-fix", "--findings", self.findings(),
                                                       "--fix-base", unrelated, ok=False))

    def test_failed_output_does_not_consume_round(self):
        r, home, wt = self.job()
        self.assertIn("inside the home", self.review(r, "--out", str(wt / "review.md"), ok=False))
        self.assertEqual(self.job_row(r)["Reviews"], "0")
        self.assertIn("Review round 1 (full)", self.review(r))

    def test_concurrent_full_reviews_cannot_bypass_cap(self):
        r, home, wt = self.job()
        processes = [subprocess.Popen([sys.executable, str(r / "core/bin/brain"), "review", "a", "--branch", "job",
                                      "--built-by", "claude"], cwd=r, env=self.env, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True) for _ in range(3)]
        outputs = [p.communicate() for p in processes]
        self.assertEqual(sum(p.returncode == 0 for p in processes), 1, outputs)
        self.assertEqual(self.job_row(r)["Reviews"], "1")

    def test_plain_branch_review_does_not_create_an_open_job(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "app")
        self.b(r, "new", "a", "--title", "A", "--home", str(home))
        self.git("branch", "plain", cwd=home)
        text = self.b(r, "review", "a", "--branch", "plain", "--built-by", "claude")
        m = core_loader.SourceFileLoader("brain_plain_review", str(r / "core/bin/brain")).load_module()
        with mock.patch.dict(os.environ, self.env):
            self.assertEqual(m.open_jobs_anywhere(m.Instance(r)), [])
        self.assertNotIn("plain", self.b(r, "jobs", "a"))
        self.assertIn("round not recorded", text)
        self.assertIn("round not recorded", self.b(r, "review", "a", "--branch", "plain", "--built-by", "claude"))

    def test_legacy_blank_full_count_does_not_reset_the_cap(self):
        r, home, wt = self.job()
        self.review(r)
        m = core_loader.SourceFileLoader("brain_legacy_review", str(r / "core/bin/brain")).load_module()
        inst = m.Instance(r)
        n = inst.node("a")
        rows = m.read_jobs(n)
        rows[0]["Full reviews"] = ""
        m.write_jobs(inst, n, rows)
        self.assertIn("second full round", self.review(r, ok=False))
        self.review(r, "--reason", "schema")
        self.assertEqual(self.job_row(r)["Full reviews"], "2")

    def test_fix_check_keeps_job_marker_and_handback(self):
        r, home, wt = self.job()
        self.review(r)
        self.git("commit", "--allow-empty", "-qm", "fix boundary", cwd=wt)
        text = self.review(r, "--check-fix", "--findings", self.findings())
        self.assertIn("brain-job:", text)
        self.assertIn("## Questions and hand-back", text)

    def test_internal_seed_lint_reads_committed_repo_root_paths(self):
        r = self.init("alice")
        self.b(r, "new", "a", "--title", "A")
        self.git("checkout", "-qb", "job", cwd=r)
        (r / "fixtures").mkdir()
        (r / "fixtures/seed.txt").write_text("alice\n")
        self.git("add", "fixtures", cwd=r)
        self.git("commit", "-qm", "invented seed", cwd=r)
        self.git("checkout", "-q", "main", cwd=r)
        text = self.review(r, "--base", "main")
        self.assertIn("Seed-text lint: FLAG", text)
        self.assertIn("1 changed test/fixture file(s), 1 flagged", text)

    def test_unregistered_branch_uses_committed_tests_and_skips_fitness(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "app", {"fitness.py": "from pathlib import Path\nPath('gate-ran').touch()\n"})
        self.b(r, "new", "a", "--title", "A", "--home", str(home))
        self.b(r, "set", "a", "fitness=python3 fitness.py")
        self.git("checkout", "-qb", "job", cwd=home)
        (home / "tests").mkdir()
        (home / "tests/test_value.py").write_text("def test_value():\n    assert True\n")
        self.git("add", "tests", cwd=home)
        self.git("commit", "-qm", "committed test", cwd=home)
        self.git("checkout", "-q", "main", cwd=home)
        contract = self.tmp / "contract.md"
        contract.write_text("## Done means\n- Value works. test: tests/test_value.py::test_value\n")
        text = self.review(r, "--base", "main", "--contract", str(contract))
        self.assertFalse((home / "gate-ran").exists())
        self.assertIn("Fitness gate: skipped: no worktree", text)
        self.assertIn("Done item 1: named test", text)

    def test_malformed_review_count_is_refused_without_traceback(self):
        r, home, wt = self.job()
        m = core_loader.SourceFileLoader("brain_bad_count", str(r / "core/bin/brain")).load_module()
        inst = m.Instance(r)
        n = inst.node("a")
        rows = m.read_jobs(n)
        rows[0]["Reviews"] = "broken"
        m.write_jobs(inst, n, rows)
        text = self.review(r, ok=False)
        self.assertIn("invalid review count", text)
        self.assertNotIn("Traceback", text)

    def test_fix_check_refuses_an_empty_range(self):
        r, home, wt = self.job()
        self.review(r)
        self.assertIn("empty fix range", self.review(r, "--check-fix", "--findings", self.findings(), ok=False))
        self.assertEqual(self.job_row(r)["Reviews"], "1")

    def test_seed_lint_matches_scrub_check_newline_and_control_rules(self):
        r, home, wt = self.job()
        (wt / "fixtures").mkdir()
        for name, data, clean in (("blank.txt", b"clean\n\n", True),
                                  ("crlf.txt", b"clean\r\n", True),
                                  ("control.txt", b"clean\x0c\n", False)):
            path = wt / "fixtures" / name
            path.write_bytes(data)
            self.b(r, "scrub", "a", "--check", str(path), ok=clean)
        self.git("add", "fixtures", cwd=wt)
        self.git("commit", "-qm", "seed newline cases", cwd=wt)
        text = self.review(r)
        self.assertIn("3 changed test/fixture file(s), 1 flagged", text)

    def test_fitness_timeout_stops_descendants(self):
        m = core_loader.SourceFileLoader("brain_fitness_timeout", str(_test_verify.CORE / "bin/brain")).load_module()
        m.FITNESS_TIMEOUT = 0.3
        late = self.tmp / "late"
        started = self.tmp / "started"
        child = "import time; from pathlib import Path; time.sleep(0.7); Path(" + repr(str(late)) + ").touch()"
        script = self.tmp / "fitness.py"
        script.write_text("import subprocess, sys, time\nfrom pathlib import Path\n"
                          "subprocess.Popen([sys.executable, '-c', " + repr(child) + "])\n"
                          "Path(" + repr(str(started)) + ").touch()\ntime.sleep(5)\n")
        result = m.review_fitness(shlex.quote(sys.executable) + " " + shlex.quote(str(script)), self.tmp)
        self.assertTrue(started.exists())
        self.assertIn("process group stopped", result)
        time.sleep(0.8)
        self.assertFalse(late.exists())

    def test_nested_numbered_done_items_have_independent_coverage(self):
        r, home, wt = self.job()
        (wt / "tests").mkdir()
        (wt / "tests/test_value.py").write_text("def test_value():\n    assert True\n")
        self.git("add", "tests", cwd=wt)
        self.git("commit", "-qm", "add named test", cwd=wt)
        contract = self.tmp / "contract.md"
        contract.write_text("## Done means\n- Parent. gap: summary only\n"
                            "  1. Child. test: tests/test_value.py::test_value\n  2. Untested child.\n"
                            "- Sibling. gap: external dependency\n")
        text = self.review(r, "--contract", str(contract))
        self.assertIn("Contract audit: 4 Done means item(s)", text)
        self.assertIn("Done item 1: stated GAP", text)
        self.assertIn("Done item 2: named test", text)
        self.assertIn("Done item 3: GAP", text)
        self.assertIn("Done item 4: stated GAP", text)

    def test_rehearsal_instructions_precede_report(self):
        r, home, wt = self.job()
        text = self.review(r)
        self.assertLess(text.index("## Rehearsal"), text.index("## Report"))
        self.assertIn("optional diagnostic", text)
        self.assertIn("Do not start one automatically", text)

    def test_fitness_skips_dirty_or_mismatched_registered_worktrees(self):
        r, home, wt = self.job()
        self.b(r, "set", "a", "fitness=touch gate-ran")
        (wt / "drift.txt").touch()
        text = self.review(r)
        self.assertIn("Fitness gate: skipped: worktree differs", text)
        self.assertFalse((wt / "gate-ran").exists())
        (wt / "drift.txt").unlink()
        self.git("checkout", "--detach", "-q", "main", cwd=wt)
        text = self.review(r, "--reason", "schema")
        self.assertIn("Fitness gate: skipped: worktree differs", text)
        self.assertFalse((wt / "gate-ran").exists())
