"""Local train scenarios use made-up Git homes and isolated brains only."""
import json
from pathlib import Path

from scenario import Scenario


class MergeTrain(Scenario):
    def project(self, command="python3 -B verify.py"):
        root = self.init("alice")
        home = self.repo(self.tmp / "app", {"verify.py": "from pathlib import Path\nassert not Path('bad').exists()\n", "shared": "base\n"})
        self.b(root, "new", "a", "--title", "A", "--home", str(home))
        self.b(root, "set", "a", "verify=" + command)
        self.root, self.home = root, home
        return root, home

    def job(self, branch, files=None):
        self.b(self.root, "wt", "add", "a", branch)
        local = json.loads((self.root / ".brain/local.json").read_text())
        wt = Path(local["nodes"]["a"]["worktrees"][branch]["path"])
        for name, value in (files or {branch: branch + "\n"}).items():
            (wt / name).write_text(value)
        self.git("add", "-A", cwd=wt)
        self.git("commit", "-qm", branch, cwd=wt)
        self.b(self.root, "handback", "a", "--branch", branch, "--state", "done", "--reviews", "1")
        return wt

    def detach(self):
        self.git("checkout", "--detach", "-q", cwd=self.home)

    def batches(self):
        return [json.loads(p.read_text()) for p in sorted((self.root / "nodes/a/job-trains").glob("*.json"))]

    def ledger(self):
        return (self.root / "nodes/a/job-ledger.md").read_text()

    def test_three_jobs_one_full_run_and_no_push(self):
        root, home = self.project()
        for branch in ("one", "two", "three"):
            self.job(branch)
        self.detach()
        out = self.b(root, "train", "a", "--jobs", "one", "two", "three")
        self.assertIn("merged 3 job(s); 1 full verify run(s)", out)
        self.assertIn("push origin main", out)  # no origin exists: an actual push would fail
        batch = self.batches()[0]
        self.assertEqual([e["branch"] for e in batch["accepted"]], ["one", "two", "three"])
        self.assertEqual(batch["state"], "complete")
        for branch in ("one", "two", "three"):
            self.assertEqual(self.git("merge-base", branch, "main", cwd=home).strip(), self.git("rev-parse", branch, cwd=home).strip())
        self.assertEqual(self.ledger().count("| merged |"), 3)
        self.assertEqual(self.ledger().count(batch["batch"]), 3)
        self.assertIn("no ready jobs", self.b(root, "train", "a"))
        self.assertEqual(self.ledger().count("| merged |"), 3)

    def test_conflict_ejected_and_rest_release(self):
        root, home = self.project()
        self.job("one", {"shared": "first\n"})
        self.job("two", {"shared": "second\n"})
        self.job("three")
        self.detach()
        out = self.b(root, "train", "a", "--jobs", "one,two,three")
        self.assertIn("ejected two: conflict in 'shared'", out)
        self.assertIn("merged 2 job(s); 1 full verify run(s)", out)
        self.assertEqual(self.batches()[0]["ejected"][0]["branch"], "two")
        self.assertEqual(self.ledger().count("| merged |"), 2)
        self.assertIn("previously ejected", self.b(root, "train", "a"))

    def test_red_batch_bisects_bad_job_and_releases_rest(self):
        # Each job passes on its own; two together expose the bad second job.
        root, home = self.project()
        (home / "verify.py").write_text("from pathlib import Path\nassert not (Path('one').exists() and Path('two').exists())\n")
        self.git("commit", "-qam", "interaction verify", cwd=home)
        for branch in ("one", "two", "three"):
            self.job(branch)
        self.detach()
        out = self.b(root, "train", "a", "--jobs", "one", "two", "three")
        self.assertIn("ejected two: first failing prefix", out)
        batch = self.batches()[0]
        self.assertEqual([e["branch"] for e in batch["accepted"]], ["one", "three"])
        self.assertTrue(Path(batch["ejected"][0]["log"]).is_file())
        self.assertGreater(len(batch["runs"]), 1)
        self.assertEqual(batch["runs"][-1]["result"], 0)
        self.assertEqual(self.ledger().count("| merged |"), 2)

    def test_explicit_unready_job_refuses_entire_batch(self):
        root, home = self.project()
        self.job("one")
        self.b(root, "wt", "add", "a", "two")
        self.detach()
        main = self.git("rev-parse", "main", cwd=home)
        self.assertIn("not ready/open", self.b(root, "train", "a", "--jobs", "one", "two", ok=False))
        self.assertEqual(self.git("rev-parse", "main", cwd=home), main)
        self.assertEqual(self.batches(), [])

    def test_changed_head_dirty_worktree_and_missing_review_rejected(self):
        root, home = self.project()
        wt = self.job("one")
        self.detach()
        (wt / "extra").write_text("dirty\n")
        self.assertIn("not ready/open", self.b(root, "train", "a", "--jobs", "one", ok=False))
        self.git("add", "extra", cwd=wt)
        self.git("commit", "-qm", "new head", cwd=wt)
        self.assertIn("not ready/open", self.b(root, "train", "a", "--jobs", "one", ok=False))
        self.b(root, "handback", "a", "--branch", "one", "--state", "done", "--reviews", "0", ok=False)
        self.assertIn("not ready/open", self.b(root, "train", "a", "--jobs", "one", ok=False))

    def test_refuses_pr_mode_and_checked_out_main(self):
        root, home = self.project()
        self.job("one")
        main = self.git("rev-parse", "main", cwd=home)
        self.assertIn("main is checked out", self.b(root, "train", "a", ok=False))
        self.b(root, "set", "a", "merge=pr")
        self.assertIn("merge=pr", self.b(root, "train", "a", ok=False))
        self.assertEqual(self.git("rev-parse", "main", cwd=home), main)

    def test_first_ready_order_and_main_moved_only_train_can_admit(self):
        root, home = self.project()
        self.job("one")
        self.job("two")
        # Persist deliberately different first-ready instants rather than depend on wall-clock minutes.
        for path in (root / "nodes/a/job-handbacks").glob("*.json"):
            data = json.loads(path.read_text())
            data["first_ready"] = "2026-01-01T00:0" + ("2" if data["job"] == "one" else "1") + ":00Z"
            path.write_text(json.dumps(data))
        (home / "new-main").write_text("main advanced independently\n")
        self.git("add", "new-main", cwd=home)
        self.git("commit", "-qm", "main advance", cwd=home)
        self.assertIn("not ready", self.b(root, "ready", "a", "--branch", "one", ok=False))
        self.detach()
        self.b(root, "train", "a")
        self.assertEqual([e["branch"] for e in self.batches()[0]["accepted"]], ["two", "one"])

    def test_baseline_red_does_not_blame_or_release_jobs(self):
        root, home = self.project()
        self.job("one")
        (home / "bad").write_text("bad base\n")
        self.git("add", "bad", cwd=home)
        self.git("commit", "-qm", "red main", cwd=home)
        self.detach()
        main = self.git("rev-parse", "main", cwd=home)
        self.assertIn("baseline verify is red", self.b(root, "train", "a", ok=False))
        self.assertEqual(self.git("rev-parse", "main", cwd=home), main)
        self.assertFalse(self.batches()[0]["ejected"])
        self.assertNotIn("| merged |", self.ledger())

    def test_verifier_mutating_stage_cannot_publish(self):
        root, home = self.project("if test \"${BRANCH#train/}\" != \"$BRANCH\"; then echo changed > shared; fi")
        self.job("one")
        self.detach()
        main = self.git("rev-parse", "main", cwd=home)
        self.assertIn("verify changed staging state", self.b(root, "train", "a", ok=False))
        self.assertEqual(self.git("rev-parse", "main", cwd=home), main)
        self.assertNotIn("| merged |", self.ledger())

    def test_verifier_moving_main_is_not_overwritten(self):
        root, home = self.project("if test \"${BRANCH#train/}\" != \"$BRANCH\"; then git update-ref refs/heads/main HEAD; fi")
        self.job("one")
        self.detach()
        out = self.b(root, "train", "a", ok=False)
        self.assertIn("main moved", out)
        self.assertNotIn("| merged |", self.ledger())

    def test_write_ahead_recovery_records_each_job_once(self):
        root, home = self.project()
        self.job("one")
        self.detach()
        before = self.ledger()
        self.b(root, "train", "a")
        path = next((root / "nodes/a/job-trains").glob("*.json"))
        data = json.loads(path.read_text())
        # Simulate main's CAS succeeding and process dying before atomic ledger replacement.
        data["state"] = "verified"
        path.write_text(json.dumps(data))
        (root / "nodes/a/job-ledger.md").write_text(before)
        self.assertIn("no ready jobs", self.b(root, "train", "a"))
        self.assertEqual(self.ledger().count("| merged |"), 1)
        self.b(root, "train", "a")
        self.assertEqual(self.ledger().count("| merged |"), 1)
        self.assertEqual(json.loads(path.read_text())["state"], "complete")

    def test_job_full_verify_fallback_is_still_required(self):
        root, home = self.project()
        self.b(root, "wt", "add", "a", "badjob")
        local = json.loads((root / ".brain/local.json").read_text())
        wt = Path(local["nodes"]["a"]["worktrees"]["badjob"]["path"])
        (wt / "bad").write_text("fails per-job verify\n")
        self.git("add", "bad", cwd=wt)
        self.git("commit", "-qm", "bad", cwd=wt)
        self.b(root, "handback", "a", "--branch", "badjob", "--state", "done", "--reviews", "1", ok=False)
        self.detach()
        self.assertIn("not ready/open", self.b(root, "train", "a", "--jobs", "badjob", ok=False))

    def test_repository_train_lock_refuses_concurrent_admission(self):
        import fcntl
        root, home = self.project()
        self.job("one")
        self.detach()
        main = self.git("rev-parse", "main", cwd=home)
        with open(home / ".git/brain-train.lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertIn("another train owns", self.b(root, "train", "a", ok=False))
        self.assertEqual(self.git("rev-parse", "main", cwd=home), main)
        self.assertNotIn("| merged |", self.ledger())

    def test_verifier_mutating_ignored_inputs_cannot_publish(self):
        root, home = self.project("if test \"${BRANCH#train/}\" != \"$BRANCH\"; then echo changed > .env; fi")
        (home / ".gitignore").write_text(".env\n")
        self.git("add", ".gitignore", cwd=home)
        self.git("commit", "-qm", "ignore env", cwd=home)
        self.job("one")
        self.detach()
        main = self.git("rev-parse", "main", cwd=home)
        self.assertIn("verify changed staging state", self.b(root, "train", "a", ok=False))
        self.assertEqual(self.git("rev-parse", "main", cwd=home), main)
        self.assertNotIn("| merged |", self.ledger())
