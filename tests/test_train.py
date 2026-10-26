"""Local train scenarios use made-up Git homes and isolated brains only."""
import json
import shlex
from pathlib import Path

from scenario import Scenario


class MergeTrain(Scenario):
    fixture_init_in_process = True
    def project(self, command="python3 -B verify.py", settings=()):
        root = self.init("alice")
        home = self.repo(self.tmp / "app", {"verify.py": "from pathlib import Path\nassert not Path('bad').exists()\n", "shared": "base\n"})
        self.fixture_b(root, "new", "a", "--title", "A", "--home", str(home))
        self.fixture_b(root, "set", "a", "verify=" + command, *settings)
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
        self.b(self.root, "handback", "--summary", "Synthetic result", "a", "--branch", branch, "--state", "done", "--reviews", "1")
        return wt

    def detach(self):
        self.git("checkout", "--detach", "-q", cwd=self.home)

    def batches(self):
        return [json.loads(p.read_text()) for p in sorted((self.root / "nodes/a/job-trains").glob("*.json"))]

    def ledger(self):
        return (self.root / "nodes/a/job-ledger.md").read_text()

    def test_legacy_required_allows_local_landing_with_absent_failed_and_stale_rehearsals(self):
        import hashlib
        root, home = self.project(settings=('rehearse=required',))
        reports = {}
        for branch in ('absent', 'failed', 'stale'):
            self.b(root, 'wt', 'add', 'a', branch)
            wt = self.tmp / 'app-work/worktrees' / branch
            (wt / branch).write_text(branch)
            self.git('add', branch, cwd=wt)
            self.git('commit', '-qm', branch, cwd=wt)
            if branch != 'absent':
                path = self.tmp / 'app-work/scratch/rehearse' / (hashlib.sha256(branch.encode()).hexdigest() + '.json')
                path.parent.mkdir(parents=True, exist_ok=True)
                report = dict(version=1, node='a', branch=branch, passed=branch == 'stale',
                              head='0' * 40, regressions=['raw diagnostic failure'])
                path.write_text(json.dumps(report))
                reports[path] = path.read_bytes()
            self.b(root, 'handback', '--summary', 'Synthetic result', 'a', '--branch', branch,
                   '--state', 'done', '--reviews', '1')
            self.assertEqual('ready', self.b(root, 'ready', 'a', '--branch', branch).strip())
        self.detach()
        out = self.b(root, 'train', 'a', '--jobs', 'absent', 'failed', 'stale')
        self.assertIn('merged 3 job(s); 1 full verify run(s)', out)
        self.assertEqual(self.ledger().count('| merged |'), 3)
        for branch in ('absent', 'failed', 'stale'):
            self.assertEqual(self.git('rev-parse', branch, cwd=home),
                             self.git('merge-base', branch, 'main', cwd=home))
        self.assertEqual(set(reports), set((self.tmp / 'app-work/scratch/rehearse').glob('*.json')))
        for path, raw in reports.items():
            self.assertEqual(raw, path.read_bytes())

    def test_legacy_required_allows_manual_fast_forward_and_registered_close(self):
        root, home = self.project(settings=('rehearse=required',))
        self.job('one')
        head = self.git('rev-parse', 'one', cwd=home).strip()
        self.assertEqual('ready', self.b(root, 'ready', 'a', '--branch', 'one').strip())
        self.git('merge', '--ff-only', 'one', cwd=home)
        self.b(root, 'jobs', 'a', '--branch', 'one', '--close')
        self.assertEqual(head, self.git('rev-parse', 'main', cwd=home).strip())
        self.assertEqual(self.ledger().count('| merged |'), 1)
        self.assertIn('manual landing confirmed in local main', self.ledger())
        self.assertFalse((self.tmp / 'app-work/scratch/rehearse').exists())

    def test_three_jobs_one_full_run_and_no_push(self):
        root, home = self.project()
        for key in ('PORT', 'PORTS', 'PORT_LAST'):
            self.env.pop(key, None)
        self.b(root, 'set', 'a', 'ports=3')
        checks = ("import os\ne = os.environ\n"
                  "assert not any(k in e for k in ('PORT', 'PORTS', 'PORT_LAST'))\n"
                  "assert e['BRAIN_PORTS'] == '3'\n"
                  "assert int(e['BRAIN_PORT_LAST']) == int(e['BRAIN_PORT']) + 2\n")
        (home / 'verify.py').write_text((home / 'verify.py').read_text() + checks)
        self.git('commit', '-qam', 'port environment assertions', cwd=home)
        (root / 'nodes/a/worktree-setup.sh').write_text(
            'set -eu\n[ "${PORT+x}${PORTS+x}${PORT_LAST+x}" = "" ]\n'
            '[ "$BRAIN_PORTS" = 3 ]\n[ "$BRAIN_PORT_LAST" = "$((BRAIN_PORT + 2))" ]\n')
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

    def test_configured_land_check_blocks_then_records_exact_integration_tip(self):
        root, home = self.project(settings=('land_check=false', 'rehearse=required'))
        node = root / 'nodes/a'
        (node / 'publication-policy.json').write_text(json.dumps({
            'version': 1, 'node': 'a', 'reviewer': 'alice', 'reviewed_at': '2026-01-01 00:00 +00:00',
            'rules': [], 'consent': {'node': 'a', 'actions': ['push']}}))
        self.job('one')
        self.detach()
        original = self.git('rev-parse', 'main', cwd=home).strip()
        out = self.b(root, 'train', 'a', '--jobs', 'one', ok=False)
        self.assertIn('land-check failed', out)
        self.assertEqual(self.git('rev-parse', 'main', cwd=home).strip(), original)
        self.assertNotIn('| merged |', self.ledger())
        self.assertIn('"result":"failed"', self.ledger())
        self.b(root, 'set', 'a', 'land_check=true')
        out = self.b(root, 'train', 'a', '--jobs', 'one')
        tip = self.git('rev-parse', 'main', cwd=home).strip()
        self.assertIn('push origin ' + tip + ':refs/heads/main', out)
        self.assertIn('"head":"' + tip + '"', self.ledger())
        self.assertIn('"result":"passed"', self.ledger())
        self.assertEqual(self.ledger().count('| merged |'), 1)
        # No origin exists; any actual push would have failed the scenario.
        self.assertEqual(self.git('remote', cwd=home).strip(), '')

    def test_staging_cleanup_lists_bound_threads_and_host_steps(self):
        root, home = self.project()
        self.b(root, 'set', 'a', 'verify_export_port=true')
        self.env['PORT'] = '9200'
        for key in ('PORTS', 'PORT_LAST'):
            self.env.pop(key, None)
        (home / 'verify.py').write_text((home / 'verify.py').read_text() +
            "import os\ne = os.environ\nassert e['PORT'] == '9200'\n"
            "assert e['PORTS'] == e['BRAIN_PORTS']\nassert e['PORT_LAST'] == e['BRAIN_PORT_LAST']\n")
        self.git('commit', '-qam', 'opt-in environment assertions', cwd=home)
        self.job('one')
        # Only staging setup runs after this point; bind its dynamic train branch.
        status = root / 'nodes/a/status.md'
        status.write_text(status.read_text() + '\n## Active work\n\n'
                          '| Branch | Thread | PR | State |\n| --- | --- | --- | --- |\n')
        (root / 'nodes/a/worktree-setup.sh').write_text(
            'set -eu\n[ "$PORT" = 9200 ]\n[ "$PORTS" = "$BRAIN_PORTS" ]\n'
            '[ "$PORT_LAST" = "$BRAIN_PORT_LAST" ]\n' +
            'printf "| %s | fixture-stage-worker | - | done |\\n" "$BRANCH" >> ' +
            shlex.quote(str(status)) + '\n')
        self.detach()
        out = self.b(root, 'train', 'a', '--jobs', 'one')
        self.assertIn('fixture-stage-worker (Active work)', out)
        self.assertIn('worker told the job is done', out)
        self.assertIn('then safely settled', out)
        batch = self.batches()[0]
        local = json.loads((root / '.brain/local.json').read_text())
        self.assertNotIn(batch['batch'], local['nodes']['a']['worktrees'])
        self.assertTrue(all('branch refs/heads/' + batch['batch'] not in line
                            for line in self.git('worktree', 'list', '--porcelain', cwd=home).splitlines()))

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
        self.b(root, "handback", "--summary", "Synthetic result", "a", "--branch", "one", "--state", "done", "--reviews", "0", ok=False)
        self.assertIn("not ready/open", self.b(root, "train", "a", "--jobs", "one", ok=False))

    def test_refuses_pr_mode_and_dirty_checked_out_main(self):
        root, home = self.project()
        self.job("one")
        main = self.git("rev-parse", "main", cwd=home)
        (home / "shared").write_text("operator edit\n")
        self.assertIn("dirty or changed", self.b(root, "train", "a", ok=False))
        self.assertFalse((root / "nodes/a/job-trains").exists())
        (root / 'nodes/a/publication-policy.json').write_text(json.dumps({
            'version': 1, 'node': 'a', 'reviewer': 'alice', 'reviewed_at': '2026-01-01 00:00 +00:00',
            'rules': [], 'consent': {'node': 'a', 'actions': ['pr']}}))
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
        self.b(root, "handback", "--summary", "Synthetic result", "a", "--branch", "badjob", "--state", "done", "--reviews", "1", ok=False)
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

    def test_train_journals_are_covered_by_instance_link_guard(self):
        root, home = self.project()
        self.job("one")
        self.detach()
        outside = self.tmp / "outside.json"
        outside.write_text(json.dumps({"version": 1, "state": "complete", "ejected": []}))
        folder = root / "nodes/a/job-trains"
        folder.mkdir()
        (folder / "old.json").symlink_to(outside)
        main = self.git("rev-parse", "main", cwd=home)
        self.assertIn("these links", self.b(root, "train", "a", ok=False))
        self.assertEqual(self.git("rev-parse", "main", cwd=home), main)
        self.assertEqual(len(list(folder.glob("*.json"))), 1)

    def test_repository_lock_cannot_create_a_symlink_target(self):
        root, home = self.project()
        self.job("one")
        self.detach()
        outside = self.tmp / "must-not-be-created"
        (home / ".git/brain-train.lock").symlink_to(outside)
        self.assertIn("repository lock", self.b(root, "train", "a", ok=False))
        self.assertFalse(outside.exists())
        self.assertNotIn("| merged |", self.ledger())


import os
import sys
import tempfile
import unittest
from unittest import mock


class LandCheckEvidence(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(sys, 'path', [str(Path(__file__).resolve().parent.parent / 'lib'), *sys.path])
        patch.start()
        self.addCleanup(patch.stop)

    def test_p10_tip_command_result_and_publication_gate(self):
        from brain import land_checks, jobs
        from brain.instance import Instance
        from brain.common import git
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / '.brain').mkdir()
            (root / 'instance.md').write_text('---\noperator: alice\nprincipals: alice\ntimezone: UTC\n---\n')
            (root / 'node.md').write_text('---\nid: root\nowners: alice\nhome: external\nland_check: true\n---\n')
            home = root / 'code'
            home.mkdir()
            env = {**os.environ, 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1',
                   'GIT_AUTHOR_NAME': 'Test', 'GIT_AUTHOR_EMAIL': 'test@example.invalid',
                   'GIT_COMMITTER_NAME': 'Test', 'GIT_COMMITTER_EMAIL': 'test@example.invalid'}
            with mock.patch.dict(os.environ, env, clear=True):
                git('init', '-q', '-b', 'main', cwd=home, check=True)
                (home / 'sample').write_text('demo')
                git('add', '.', cwd=home, check=True)
                git('commit', '-qm', 'fixture', cwd=home, check=True)
                inst = Instance(root)
                n = inst.node('root')
                n['home_path'] = home
                inst.local_of('root')['home'] = str(home)
                row = dict.fromkeys(jobs.JOB_COLS, '')
                row.update(Job='main', Started='2026-01-01T10:00:00+00:00')
                jobs.write_jobs(inst, n, [row])
                w = {'path': str(home), 'port': 4200, 'ports': 1}
                n['local']['worktrees'] = {'main': w}
                inst.local_of('root').update(n['local'])
                inst.save_local()
                with self.assertRaisesRegex(ValueError, 'evidence'):
                    land_checks.require_current(inst, n, row, w)
                receipt = land_checks.run_check(inst, n, row, w)
                self.assertEqual(receipt['result'], 'passed')
                current = jobs.read_jobs(n)[0]
                self.assertEqual(jobs.read_land_check(n, current), receipt)
                land_checks.require_current(inst, n, current, w)
                # A replacement episode invalidates old evidence, even if the head is unchanged.
                from types import SimpleNamespace
                adapter = SimpleNamespace(current_attempt=lambda *a, **k: attempt,
                                          attempt_binding=lambda *a: {'head': receipt['head']})
                with mock.patch.object(sys.modules['brain'], 'job_attempts', adapter, create=True):
                    for state in ('queued', 'verifying', 'interrupted', 'invalidated', 'unknown'):
                        attempt = {'id': 'a' * 32, 'state': state, 'binding': {'head': receipt['head']}}
                        with self.subTest(state=state), self.assertRaises(ValueError):
                            land_checks.require_current(inst, n, current, w)
                    attempt = {'id': 'a' * 32, 'state': 'terminal', 'binding': {'head': receipt['head']}}
                    with self.assertRaises(ValueError):
                        land_checks.require_current(inst, n, current, w)
                    with mock.patch.object(jobs, 'read_handback', return_value={'state': 'done', 'attempt_id': 'a' * 32}):
                        with self.assertRaises(ValueError):
                            land_checks.require_current(inst, n, current, w)
                # Evidence alone is not outward consent.
                with self.assertRaisesRegex(ValueError, 'coordinator'):
                    land_checks.push_step(inst, n, current, w)
                policy = {'version': 1, 'node': 'root', 'reviewer': 'alice', 'reviewed_at': inst.now(),
                          'rules': [], 'consent': {'node': 'root', 'actions': ['push']}}
                (root / 'publication-policy.json').write_text(json.dumps(policy))
                # Policy/config identity changed: rerun before generating the step.
                with self.assertRaises(ValueError):
                    land_checks.require_current(inst, n, current, w)
                receipt = land_checks.run_check(inst, n, current, w)
                current = jobs.read_jobs(n)[0]
                self.assertIn('push origin', land_checks.push_step(inst, n, current, w))
                self.assertFalse((home / 'published').exists())
                config = root / 'node.md'
                config.write_text(config.read_text().replace('land_check: true', "land_check: printf 'token=demo-value'"))
                private_receipt = land_checks.run_check(inst, Instance(root).node('root'), current, w)
                self.assertEqual(private_receipt['result'], 'passed')
                self.assertNotIn('demo-value', private_receipt['command'])
                self.assertNotIn('demo-value', (root / 'job-ledger.md').read_text())
                config.write_text(config.read_text().replace("land_check: printf 'token=demo-value'", 'land_check: true'))
                config = root / 'node.md'
                config.write_text(config.read_text().replace('land_check: true', 'land_check: false'))
                n = Instance(root).node('root')
                with self.assertRaises(ValueError):
                    land_checks.require_current(inst, n, current, w)
                receipt = land_checks.run_check(inst, n, current, w)
                self.assertEqual(receipt['result'], 'failed')
                with self.assertRaises(ValueError):
                    land_checks.push_step(inst, n, jobs.read_jobs(n)[0], w)
                config.write_text(config.read_text().replace('land_check: false\n', ''))
                n = Instance(root).node('root')
                self.assertIsNone(land_checks.run_check(inst, n, current, w))
                self.assertIsNone(land_checks.require_current(inst, n, current, w))
                config.write_text(config.read_text().replace('owners: alice', 'land_check: true\nowners: alice'))
                n = Instance(root).node('root')
                receipt = land_checks.run_check(inst, n, current, w)
                current = jobs.read_jobs(n)[0]
                (home / 'sample').write_text('new')
                git('commit', '-qam', 'changed', cwd=home, check=True)
                with self.assertRaises(ValueError):
                    land_checks.require_current(inst, n, current, w)
                # A command that changes the candidate cannot produce passing evidence.
                config.write_text(config.read_text().replace('land_check: true', 'land_check: git commit --allow-empty -qm altered'))
                n = Instance(root).node('root')
                changed = land_checks.run_check(inst, n, current, w)
                self.assertEqual(changed['result'], 'failed')
