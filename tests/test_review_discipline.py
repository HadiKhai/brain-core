"""Review policy scenarios using only invented projects and findings."""
import json
import subprocess
import sys
from scenario import Scenario
import test_verify as _test_verify


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
        before = (r / "nodes/a/job-ledger.md").read_text()
        self.assertIn("second full round", self.review(r, ok=False))
        self.assertEqual(before, (r / "nodes/a/job-ledger.md").read_text())
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
        self.b(r, "set", "a", "fitness=python3 fitness.py")
        text = self.review(r)
        self.assertEqual((wt / "fitness-ran").read_text(), "yes")
        self.assertIn("Fitness gate: FAIL (exit 7)", text)
        self.assertNotIn("private gate output", text)
        (wt / "fitness.py").write_text("raise SystemExit(0)\n")
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
        (wt / "seed.txt").write_text(word)
        text = self.review(r, ok=False)
        self.assertNotIn(word, text)
        self.assertIn("seed lint failed", text)
        (wt / "seed.txt").unlink()
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
