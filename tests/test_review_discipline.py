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

    def test_round_cap_and_free_text_reason(self):
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
        reason = 'Recheck the corrected parser boundary'
        self.assertIn('Review round 3 (full)', self.review(r, '--reason', reason))
        row = self.job_row(r)
        self.assertEqual(row["Reviews"], "3")
        self.assertEqual(row["Full reviews"], "2")
        self.assertEqual(row["Review reason"], reason)
        before = (r / 'nodes/a/job-ledger.md').read_bytes()
        self.assertIn('cap of 3', self.review(r, '--reason', 'security', ok=False))
        self.assertEqual(before, (r / 'nodes/a/job-ledger.md').read_bytes())

    def test_risk_marks_allow_five_rounds_but_never_six(self):
        r, home, wt = self.job()
        m = core_loader.SourceFileLoader('brain_risk_review', str(r / 'core/bin/brain')).load_module()
        inst, n = m.Instance(r), m.Instance(r).node('a')
        for risk in ('security', 'permissions', 'data-loss', 'concurrency'):
            with self.subTest(risk=risk):
                rows = m.read_jobs(n)
                rows[0].update(Reviews='3', **{'Full reviews': '1', 'Review risk': ''})
                m.write_jobs(inst, n, rows)
                reason = 'Check the remaining boundary cases'
                self.assertIn('Review round 4', self.review(r, '--reason', reason, '--risk', risk))
                self.assertEqual(self.job_row(r)['Review risk'], risk)
                before = (r / 'nodes/a/job-ledger.md').read_bytes()
                self.assertIn('requires --reason', self.review(r, ok=False))
                self.assertEqual(before, (r / 'nodes/a/job-ledger.md').read_bytes())
                self.assertIn('Review round 5', self.review(r, '--reason', reason))
                before = (r / 'nodes/a/job-ledger.md').read_bytes()
                self.assertIn('cap of 5', self.review(r, '--reason', reason, '--risk', risk, ok=False))
                self.assertIn('cap of 5', self.review(r, '--reason', reason, '--check-fix',
                                                   '--findings', self.findings(), ok=False))
                self.assertEqual(before, (r / 'nodes/a/job-ledger.md').read_bytes())

    def test_invalid_reasons_do_not_change_the_ledger_or_emit_private_text(self):
        r, home, wt = self.job()
        word = 'invented-vocabulary-omega'
        (r / 'nodes/a/never-name.txt').write_text(word + '\n')
        before = (r / 'nodes/a/job-ledger.md').read_bytes()
        for reason in ('', '  ', 'one\ntwo', word):
            text = self.review(r, '--reason', reason, ok=False)
            self.assertNotIn(word, text)
            self.assertEqual(before, (r / 'nodes/a/job-ledger.md').read_bytes())

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


class IndependentReviewReceipts(Scenario):
    job = _test_verify.WorkerReadyPackets.job

    def context(self):
        r, home, wt = self.job()
        self.b(r, 'set', 'a', 'review_independence=fresh-thread')
        m = core_loader.SourceFileLoader('independent_review_fixture', str(_test_verify.CORE / 'bin/brain')).load_module()
        inst = m.Instance(r)
        n = inst.node('a')
        rows = m.read_jobs(n)
        rows[-1].update(Runtime='codex', Model='example-model')
        m.write_jobs(inst, n, rows)
        key = m.job_key(rows[-1])
        inst.local['nodes']['a'].setdefault('workers', {})[key] = 'builder-thread'
        inst.save_local()
        n = inst.node('a')
        head = self.git('rev-parse', 'HEAD', cwd=wt).strip()
        receipt = {'version': 1, 'node': 'a', 'job_key': key, 'branch': 'job', 'head': head,
                   'observed_at': inst.now_dt().isoformat(), 'builder': {'threadId': 'builder-thread', 'runtime': 'codex', 'model': 'example-model'},
                   'reviewer': {'threadId': 'reviewer-thread', 'runtime': 'codex', 'model': 'example-model',
                                'fresh_context': True, 'context_reused': False, 'built_change': False},
                   'verdict': 'pass', 'findings': []}
        return r, wt, m, inst, n, receipt

    def test_same_model_distinct_fresh_thread_receipt_is_required_and_recorded(self):
        r, wt, m, inst, n, receipt = self.context()
        self.assertTrue(m.validate_ready_reviews(inst, n, 'job', receipt['head']))
        m.record_review_receipt(inst, n, 'job', receipt)
        self.assertEqual(m.validate_ready_reviews(inst, n, 'job', receipt['head']), [])
        evidence = m.review_evidence(inst, n, 'job')
        self.assertEqual(evidence[0]['reviewer']['threadId'], 'reviewer-thread')
        self.assertEqual(evidence[0]['reviewer']['runtime'], 'codex')
        self.assertEqual(evidence[0]['reviewer']['model'], 'example-model')
        self.assertNotIn('reviewer-thread', (n['dir'] / 'job-ledger.md').read_text())
        self.git('commit', '--allow-empty', '-qm', 'next change', cwd=wt)
        head = self.git('rev-parse', 'HEAD', cwd=wt).strip()
        self.assertTrue(m.validate_ready_reviews(inst, n, 'job', head))

    def test_self_unknown_reused_builder_and_failed_receipts_are_rejected(self):
        r, wt, m, inst, n, original = self.context()
        for fields in ({'threadId': 'builder-thread'}, {'threadId': ''}, {'model': ''},
                       {'fresh_context': False}, {'context_reused': True}, {'built_change': True}):
            receipt = json.loads(json.dumps(original))
            receipt['reviewer'].update(fields)
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                m.record_review_receipt(inst, n, 'job', receipt)
        self.b(r, 'set', 'a', 'review_independence=cross-runtime')
        legacy = m.Instance(r).node('a')
        with self.assertRaises(ValueError):
            m.record_review_receipt(inst, legacy, 'job', original)
        self.b(r, 'set', 'a', 'review_independence=fresh-thread')
        receipt = dict(original, verdict='blocked')
        m.record_review_receipt(inst, n, 'job', receipt)
        self.assertTrue(m.validate_ready_reviews(inst, n, 'job', receipt['head']))

    def test_worker_status_change_after_review_validation_refuses_receipt(self):
        root, wt, m, inst, n, receipt = self.context()
        validate = m.validate_review_receipt
        def changed(*args, **kwargs):
            result = validate(*args, **kwargs)
            m.active_table(inst, n, {'active': [['job', 'reviewer-thread', '-', 'building']]})
            return result
        with mock.patch.object(m, 'validate_review_receipt', side_effect=changed), self.assertRaises(SystemExit):
            m.record_review_receipt(inst, n, 'job', receipt)
        row = m.open_job(m.read_jobs(n), 'job')
        self.assertFalse(m.review_receipt_path(n, row, receipt['head']).exists())

    def test_review_route_allows_same_runtime_only_with_explicit_fresh_policy(self):
        r, wt, m, inst, n, receipt = self.context()
        text = self.b(r, 'review', 'a', '--branch', 'job', '--built-by', 'codex', '--by', 'codex', '--model', 'example-model')
        self.assertIn('fresh non-builder thread', text)
        self.assertIn('Reviewer runtime: codex', text)
        self.assertIn('receipt', text)
        file = self.tmp / 'receipt.json'
        file.write_text(json.dumps(receipt))
        self.assertIn('review receipt recorded', self.b(r, 'review', 'a', '--branch', 'job', '--receipt-file', str(file)))
        self.b(r, 'set', 'a', 'review_independence=cross-runtime')
        self.b(r, 'review', 'a', '--branch', 'job', '--built-by', 'codex', '--by', 'codex', '--reason', 'security', ok=False)

    def test_legacy_exception_expires_and_still_requires_thread_evidence(self):
        r, wt, m, inst, n, receipt = self.context()
        self.b(r, 'set', 'a', 'review_independence=cross-runtime',
               'same_runtime_review_until=2099-01-01T00:00:00+00:00')
        args = ('review', 'a', '--branch', 'job', '--built-by', 'codex', '--by', 'codex', '--model', 'example-model')
        self.assertIn('--same-runtime-reason', self.b(r, *args, ok=False))
        text = self.b(r, *args, '--same-runtime-reason', 'Provider unavailable')
        self.assertIn('Same-runtime exception expires:', text)
        inst = m.Instance(r)
        n = inst.node('a')
        self.assertTrue(m.validate_ready_reviews(inst, n, 'job', receipt['head']))
        ledger = (n['dir'] / 'job-ledger.md').read_text()
        self.assertIn('Provider unavailable', ledger)
        self.b(r, 'set', 'a', 'same_runtime_review_until=2020-01-01T00:00:00+00:00')
        text = self.b(r, *args, '--same-runtime-reason', 'Provider unavailable', '--reason', 'security', ok=False)
        self.assertIn('exception expired', text)



class ClosedFindingInputs(Scenario):
    job = _test_verify.WorkerReadyPackets.job
    context = IndependentReviewReceipts.context
    def test_closed_findings_survive_unchanged_and_unrelated_commits(self):
        r, wt, m, inst, n, receipt = self.context()
        finding = {'id': 'bounds', 'class': 'boundary', 'severity': 'blocker', 'text': 'Guard the range',
                   'paths': ['calc.py'], 'state': 'closed', 'evidence': 'test_range passes'}
        m.record_review_findings(inst, n, 'job', receipt['head'], [finding])
        incoming = dict(finding, state='open', evidence='')
        m.record_review_findings(inst, n, 'job', receipt['head'], [incoming])
        self.assertEqual(m.review_finding_state(inst, n, 'job', receipt['head'])[0]['state'], 'closed')
        (wt / 'notes.txt').write_text('unrelated\n')
        self.git('add', 'notes.txt', cwd=wt)
        self.git('commit', '-qm', 'unrelated note', cwd=wt)
        head = self.git('rev-parse', 'HEAD', cwd=wt).strip()
        self.assertEqual(m.review_finding_state(inst, n, 'job', head)[0]['state'], 'closed')
        (wt / 'calc.py').write_text('value = 3\n')
        self.git('commit', '-qam', 'relevant input changed', cwd=wt)
        head = self.git('rev-parse', 'HEAD', cwd=wt).strip()
        self.assertEqual(m.review_finding_state(inst, n, 'job', head)[0]['state'], 'reopened')
        with self.assertRaises(ValueError):
            m.record_review_findings(inst, n, 'job', head, [dict(finding, evidence='')])
        m.record_review_findings(inst, n, 'job', head, [finding])
        self.assertEqual(m.review_finding_state(inst, n, 'job', head)[0]['state'], 'closed')


class ReceiptReadinessIntegration(Scenario):
    job = _test_verify.WorkerReadyPackets.job
    context = IndependentReviewReceipts.context

    def test_unconfigured_mock_settings_preserve_existing_readiness(self):
        r, wt, m, inst, n, review = self.context()
        n['fm'].pop('review_independence')
        inst.cfg = mock.Mock()
        self.assertEqual(m.ready_policy_reasons(inst, n, 'job', review['head']), [])
        n['fm']['requires_preview_evidence'] = True
        self.assertTrue(m.ready_policy_reasons(inst, n, 'job', review['head']))

    def test_real_receipt_schemas_guard_recording_and_all_packet_read_modes(self):
        r, wt, m, inst, n, review = self.context()
        self.b(r, 'set', 'a', 'preview=https://preview.example/app')
        inst = m.Instance(r)
        n = inst.node('a')
        owner = m.record_ready.__globals__
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.dict(owner, {
                'run_verify': mock.Mock(side_effect=AssertionError('verification ran without required receipts'))}):
            packet, reasons = m.record_ready(inst, n, 'job', 1)
        self.assertIsNone(packet)
        self.assertTrue(any('review receipt' in reason for reason in reasons))
        self.assertTrue(any('preview' in reason for reason in reasons))
        m.record_review_receipt(inst, n, 'job', review)
        preview = {'version': 1, 'node': 'a', 'job_key': review['job_key'], 'branch': 'job', 'head': review['head'],
                   'checked_at': inst.now_dt().isoformat(), 'renderer_threadId': 'browser-thread',
                   'urls': [{'url': n['local']['preview'], 'rendered': True, 'render_evidence': 'render-artifact',
                             'items': [{'page_item': str(i), 'source_ref': 'source.json#' + str(i),
                                        'source_item': str(i)} for i in range(3)]}]}
        path = m.record_preview_receipt(inst, n, 'job', preview)
        log = self.tmp / 'fixture-verify.log'
        log.write_text('fixture verifier output\n')
        runner = mock.Mock(return_value=(0, str(log)))
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.dict(owner, {'run_verify': runner}):
            packet, reasons = m.record_ready(inst, n, 'job', 1)
        self.assertEqual(reasons, [])
        self.assertIsNotNone(packet)
        runner.assert_called_once()
        row = m.open_job(m.read_jobs(n), 'job')
        target = m.handback_file(n, row)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({'version': 3, 'job': 'job', 'started': row['Started'], 'state': 'done',
                                     'summary': 'Fixture receipt integration', 'lessons': 'none: fixture',
                                     'ready': packet, 'ready_reasons': []}))
        for mode in ({}, {'display': True}, {'train': True}, {'train': True, 'display': True}):
            state = m._ready_state(inst, n, 'job', **mode)
            self.assertTrue(state['ready'], state['reasons'])
        path.unlink()
        for mode in ({}, {'display': True}, {'train': True}, {'train': True, 'display': True}):
            state = m._ready_state(inst, n, 'job', **mode)
            self.assertFalse(state['ready'])
            self.assertTrue(any('preview' in reason for reason in state['reasons']))
        m.record_preview_receipt(inst, n, 'job', preview)
        (n['dir'] / 'never-name.txt').write_text('receipt\n')
        values, keys = {}, {('a', 'job'): ('stable-job-inputs',)}
        with mock.patch.object(m, '_ready_state', wraps=m._ready_state) as facts, m.display_cache(values, keys):
            self.assertTrue(m.ready_state(inst, n, 'job', display=True)['ready'])
            path.unlink()
            state = m.ready_state(inst, n, 'job', display=True)
            self.assertFalse(state['ready'], 'cached display ignored missing required preview evidence')
            self.assertNotIn('receipt', ' '.join(state['reasons']))
            self.assertTrue(any('preview' in reason for reason in state['reasons']))
            m.record_preview_receipt(inst, n, 'job', preview)
            self.assertTrue(m.ready_state(inst, n, 'job', display=True)['ready'])
            facts.assert_called_once()


class DurableFindingReadiness(Scenario):
    job = _test_verify.WorkerReadyPackets.job
    context = IndependentReviewReceipts.context

    def finding(self, state='open'):
        return {'id': 'range', 'class': 'boundary', 'severity': 'major', 'text': 'Check the range',
                'paths': ['calc.py'], 'state': state, **({'evidence': 'focused range case passes'} if state == 'closed' else {})}

    def pass_receipt(self, m, inst, n, receipt, reviewer, head=None, findings=None):
        data = dict(receipt, verdict='pass', findings=[] if findings is None else findings,
                    head=head or receipt['head'], observed_at=inst.now_dt().isoformat(),
                    reviewer=dict(receipt['reviewer'], threadId=reviewer))
        m.record_review_receipt(inst, n, 'job', data)
        return data

    def test_omitted_major_remains_blocking_in_validator_and_readiness_caller(self):
        r, wt, m, inst, n, receipt = self.context()
        m.record_review_receipt(inst, n, 'job', dict(receipt, verdict='blocked', findings=[self.finding()]))
        self.pass_receipt(m, inst, n, receipt, 'another-reviewer')
        self.assertTrue(m.validate_ready_reviews(inst, n, 'job', receipt['head']))
        self.assertTrue(m.ready_policy_reasons(inst, n, 'job', receipt['head']))
        with mock.patch.object(m, 'run_verify') as runner:
            packet, reasons = m.record_ready(inst, n, 'job', 1)
        self.assertIsNone(packet)
        self.assertTrue(reasons)
        runner.assert_not_called()
        self.assertEqual(m.review_finding_state(inst, n, 'job', receipt['head'])[0]['state'], 'open')

    def assert_input_change_reopens(self, deleted):
        r, wt, m, inst, n, receipt = self.context()
        m.record_review_receipt(inst, n, 'job', dict(receipt, findings=[self.finding('closed')]))
        (wt / 'notes.txt').write_text('unrelated note\n')
        self.git('add', 'notes.txt', cwd=wt)
        self.git('commit', '-qm', 'unrelated input', cwd=wt)
        head = self.git('rev-parse', 'HEAD', cwd=wt).strip()
        self.pass_receipt(m, inst, n, receipt, 'unrelated-reviewer', head)
        self.assertEqual(m.validate_ready_reviews(inst, n, 'job', head), [])
        if deleted:
            (wt / 'calc.py').unlink()
        else:
            (wt / 'calc.py').write_text('value = 9\n')
        self.git('commit', '-qam', 'relevant input change', cwd=wt)
        head = self.git('rev-parse', 'HEAD', cwd=wt).strip()
        self.pass_receipt(m, inst, n, receipt, 'changed-reviewer', head)
        self.assertEqual(m.review_finding_state(inst, n, 'job', head)[0]['state'], 'reopened')
        self.assertTrue(m.validate_ready_reviews(inst, n, 'job', head))
        self.assertTrue(m.ready_policy_reasons(inst, n, 'job', head))
        self.pass_receipt(m, inst, n, receipt, 'closure-reviewer', head, [self.finding('closed')])
        self.assertEqual(m.validate_ready_reviews(inst, n, 'job', head), [])

    def test_unchanged_closure_survives_omission_but_changed_input_reopens(self):
        self.assert_input_change_reopens(False)

    def test_unchanged_closure_survives_omission_but_deleted_input_reopens(self):
        self.assert_input_change_reopens(True)

    def test_malformed_or_missing_union_refuses_without_replacing_durable_evidence(self):
        r, wt, m, inst, n, receipt = self.context()
        m.record_review_receipt(inst, n, 'job', dict(receipt, findings=[self.finding('closed')]))
        row = m.open_job(m.read_jobs(n), 'job')
        path = m.finding_state_path(inst, n, row)
        original = json.loads(path.read_text())
        variants = [dict(original, version=True), dict(original, findings={'range': []})]
        for fields in ({'id': 'other'}, {'severity': 'unknown'}, {'state': 'reopened'}, {'inputs': {}},
                       {'inputs': {'calc.py': '0' * 40}}, {'evidence': ''}, {'paths': [['calc.py']]}):
            data = json.loads(json.dumps(original))
            data['findings']['range'].update(fields)
            variants.append(data)
        for data in variants:
            with self.subTest(data=data):
                text = json.dumps(data)
                path.write_text(text)
                self.assertTrue(m.validate_ready_reviews(inst, n, 'job', receipt['head']))
                self.assertTrue(m.ready_policy_reasons(inst, n, 'job', receipt['head']))
                with self.assertRaises(ValueError):
                    self.pass_receipt(m, inst, n, receipt, 'malformed-reviewer')
                self.assertEqual(path.read_text(), text)
        path.unlink()
        self.assertTrue(m.validate_ready_reviews(inst, n, 'job', receipt['head']))
        with self.assertRaises(ValueError):
            self.pass_receipt(m, inst, n, receipt, 'missing-reviewer')
        self.assertFalse(path.exists())

    def test_existing_major_cannot_be_downgraded_or_closed_without_evidence(self):
        r, wt, m, inst, n, receipt = self.context()
        m.record_review_receipt(inst, n, 'job', dict(receipt, verdict='blocked', findings=[self.finding()]))
        for fields in ({'severity': 'minor'}, {'state': 'closed', 'evidence': ''}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.pass_receipt(m, inst, n, receipt, 'downgrade-reviewer', findings=[dict(self.finding(), **fields)])
        self.assertTrue(m.validate_ready_reviews(inst, n, 'job', receipt['head']))
