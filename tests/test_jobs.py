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


def settlement_export(test, root, node, branch=None, key=None, all_closed=False):
    """Explicit synthetic coordinator observations for P2's required export."""
    module = core_loader.SourceFileLoader('settlement_fixture', str(CORE / 'bin/brain')).load_module()
    with mock.patch.dict(os.environ, test.env, clear=True):
        inst = module.Instance(root)
        n = inst.node(node)
        rows = module.read_jobs(n) or []
        targets = module.unsettled_jobs(rows) if all_closed else [r for r in rows if r['Job'] == branch and
                    (key is None or module.job_key(r) == key)]
        jobs = []
        for row in targets:
            identities = set()
            workers = n['local'].get('workers', {})
            if workers.get(module.job_key(row)):
                identities.update(module.thread_reference_ids(workers[module.job_key(row)]))
            for work in module.active_work(n, include_closed=True):
                name = work[0].replace('`','').split()
                if name and name[0] == row['Job'] and work[1] not in ('', '-', '—'):
                    identities.update(module.thread_reference_ids(work[1]))
            jobs.append({'job_key': module.job_key(row), 'threads': [dict(threadId=tid,
                job_key=module.job_key(row), state='completed', settled=True, pending_requests=[]) for tid in sorted(identities)]})
        evidence = {'version': 1, 'node': node, 'observed_at': inst.now_dt().isoformat(),
                    'project_id': n['local'].get('home_project') or n['local'].get('host_project') or 'synthetic-project',
                    'coverage': {'project': True, 'nested': True, 'complete': True}, 'jobs': jobs}
        projects = sorted({n['local'][key] for key in ('home_project', 'host_project') if n['local'].get(key)})
        if projects:
            evidence['project_ids'] = projects
        path = root / '.brain' / 'synthetic-settlement-export.json'
        path.write_text(json.dumps(evidence))
        return str(path)


class LedgerFollowsMain(Scenario):
    def setUp(self):
        super().setUp()
        self.root = self.init('ledger')
        self.home = self.repo(self.tmp / 'app')
        self.b(self.root, 'new', 'a', '--title', 'A', '--home', str(self.home))
        self.b(self.root, 'wt', 'add', 'a', 'job')
        self.wt = self.tmp / 'app-work/worktrees/job'
        self.module = core_loader.SourceFileLoader('ledger_follows_main', str(CORE / 'bin/brain')).load_module()

    def rows(self):
        return self.module.read_jobs(self.module.Instance(self.root).node('a'))

    def commit_work(self):
        (self.wt / 'feature.py').write_text('value = 1\n')
        self.git('add', 'feature.py', cwd=self.wt)
        self.git('commit', '-qm', 'Deliver feature', cwd=self.wt)
        return self.git('rev-parse', 'HEAD', cwd=self.wt).strip()

    def test_jobs_closes_manual_landing_without_ready_packet_and_retains_evidence(self):
        head = self.commit_work()
        self.git('merge', '--ff-only', 'job', cwd=self.home)
        self.git('commit', '--allow-empty', '-qm', 'Later main work', cwd=self.home)
        main = self.git('rev-parse', 'main', cwd=self.home).strip()
        out = self.b(self.root, 'jobs', 'a', '--branch', 'job')
        self.assertIn('1 finished (1 merged', out)
        row, = self.rows()
        self.assertTrue(row['Ended'])
        self.assertEqual((row['Head commit'], row['Main commit']), (head, main))
        self.assertEqual(json.loads(row['Code paths']), ['feature.py'])
        self.assertEqual(row['Outcome'], 'merged')
        self.assertFalse(list((self.root / 'nodes/a/job-handbacks').glob('*.json')))
        before = (self.root / 'nodes/a/job-ledger.md').read_bytes()
        self.b(self.root, 'jobs', 'a')
        self.assertEqual((self.root / 'nodes/a/job-ledger.md').read_bytes(), before)
        inst = self.module.Instance(self.root)
        summary = self.module.jobs_summary(inst, inst.node('a'))
        self.assertEqual(summary['merged'], 1)
        self.assertEqual(sum(day['merged'] for day in summary['throughput']), 1)

    def test_unlanded_and_untouched_branches_stay_open(self):
        self.b(self.root, 'jobs', 'a')
        self.assertFalse(self.rows()[0]['Ended'])
        self.commit_work()
        self.b(self.root, 'jobs', 'a')
        self.assertFalse(self.rows()[0]['Ended'])

    def test_missing_baseline_stays_open_without_waiting_for_writer_lock(self):
        local = self.root / '.brain/local.json'
        data = json.loads(local.read_text())
        data['nodes']['a']['worktrees']['job'].pop('start')
        local.write_text(json.dumps(data))
        before = (self.root / '.brain/events.jsonl').read_bytes()
        self.b(self.root, 'jobs', 'a')
        self.assertFalse(self.rows()[0]['Ended'])
        self.assertEqual((self.root / '.brain/events.jsonl').read_bytes(), before)
        inst = self.module.Instance(self.root)
        jobs = self.module._owners['manual_landing_candidates']
        with mock.patch.object(jobs._m_instance, 'brain_lock', side_effect=AssertionError('unexpected lock')):
            self.assertEqual(jobs.reconcile_manual_landings(inst, inst.node('a')), [])

    def test_event_source_reconciliation_is_a_complete_writer_transaction(self):
        inst = self.module.Instance(self.root)
        events = self.module._owners['cmd_events']
        first = inst.now_dt() - datetime.timedelta(days=1, minutes=1)
        events.append_line(self.root / '.brain/events-compare.jsonl',
            {'ts': first.strftime('%Y-%m-%d %H:%M +00:00'), 'node': 'a', 'kind': 'comparison', 'equal': True})
        self.b(self.root, 'status', 'a', '--refresh')
        self.b(self.root, 'set', 'a', 'status_source=events')
        self.commit_work()
        self.git('merge', '--ff-only', 'job', cwd=self.home)
        self.b(self.root, 'jobs', 'a')
        self.assertEqual(self.rows()[0]['Outcome'], 'merged')
        self.b(self.root, 'backlog', 'a', 'add', 'Follow reconciled landing')
        self.b(self.root, 'status', 'a', '--refresh')

    def test_changed_branch_during_completion_evidence_refuses_closure(self):
        self.commit_work()
        self.git('merge', '--ff-only', 'job', cwd=self.home)
        inst = self.module.Instance(self.root)
        git = self.module.git
        def move(*args, **kwargs):
            result = git(*args, **kwargs)
            if args[:3] == ('diff', '--name-only', '-z'):
                self.git('commit', '--allow-empty', '-qm', 'Concurrent follow-up', cwd=self.wt)
            return result
        with mock.patch.object(self.module, 'git', side_effect=move):
            with self.assertRaisesRegex(SystemExit, 'changed while reading completion evidence'):
                self.module.prepare_ledger_close(inst, inst.node('a'), self.rows()[0])
        self.assertFalse(self.rows()[0]['Ended'])

    def test_branch_advanced_after_landing_stays_open(self):
        self.commit_work()
        self.git('merge', '--ff-only', 'job', cwd=self.home)
        self.git('commit', '--allow-empty', '-qm', 'Unlanded follow-up', cwd=self.wt)
        self.b(self.root, 'jobs', 'a')
        self.assertFalse(self.rows()[0]['Ended'])

    def test_diverged_and_missing_refs_stay_open(self):
        self.commit_work()
        self.git('commit', '--allow-empty', '-qm', 'Separate main change', cwd=self.home)
        self.b(self.root, 'jobs', 'a')
        self.assertFalse(self.rows()[0]['Ended'])
        self.git('checkout', '--detach', cwd=self.wt)
        self.git('branch', '-D', 'job', cwd=self.home)
        self.b(self.root, 'jobs', 'a')
        self.assertFalse(self.rows()[0]['Ended'])

    def test_live_projection_stays_read_only_and_unresolved_train_blocks_reconciliation(self):
        self.commit_work()
        self.git('merge', '--ff-only', 'job', cwd=self.home)
        self.b(self.root, 'jobs', 'a', '--live')
        self.assertFalse(self.rows()[0]['Ended'])
        folder = self.root / 'nodes/a/job-trains'
        folder.mkdir()
        (folder / 'uncertain.json').write_text(json.dumps({'state': 'publication_failed',
            'accepted': [{'key': self.module.job_key(self.rows()[0])}]}))
        self.b(self.root, 'jobs', 'a')
        self.assertFalse(self.rows()[0]['Ended'])

    def test_landing_reconciler_uses_same_ancestry_rule_without_gate_readiness(self):
        self.commit_work()
        self.git('merge', '--ff-only', 'job', cwd=self.home)
        inst = self.module.Instance(self.root)
        train = self.module._owners['cmd_land']
        output = io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), contextlib.redirect_stdout(output):
            closed = train.reconcile_manual_landings(inst, inst.node('a'))
        self.assertIn('train: reconciled manual landing job', output.getvalue())
        self.assertEqual(closed, [self.module.job_key(self.rows()[0])])
        self.assertEqual(self.rows()[0]['Outcome'], 'merged')
        self.assertEqual(json.loads(self.rows()[0]['Code paths']), ['feature.py'])


class LedgerReviewEvidence(Scenario):
    def test_next_write_repairs_only_rows_with_retained_review_evidence(self):
        r = self.init("alice")
        module = core_loader.SourceFileLoader("ledger_reviews", str(CORE / "bin/brain")).load_module()
        inst = module.Instance(r)
        n = inst.node("root")
        rows = []
        cases = [("packet", 3, "3"), ("study", 2, "2"), ("missing", None, "7"),
                 ("bool", True, "7"), ("negative", -1, "7"), ("string", "3", "7"),
                 ("wrong-job", 3, "7"), ("blocked", 3, "7")]
        for name, rounds, expected in cases:
            row = {"Job": name, "Started": "2020-01-01T00:00Z", "Ended": "2020-01-01T01:00Z", "Reviews": "7"}
            rows.append(row)
            if rounds is None:
                continue
            hb = {"job": name if name != "wrong-job" else "other", "started": row["Started"],
                  "state": "blocked" if name == "blocked" else "done"}
            hb.update({"review_rounds": rounds} if name == "study" else {"ready": {"review_rounds": rounds}})
            f = module.handback_file(n, row)
            f.parent.mkdir(exist_ok=True)
            f.write_text(json.dumps(hb))
        module.write_jobs(inst, n, rows)
        self.assertEqual([row["Reviews"] for row in module.read_jobs(n)], [c[2] for c in cases])
        module.write_jobs(inst, n, module.read_jobs(n))
        self.assertEqual([row["Reviews"] for row in module.read_jobs(n)], [c[2] for c in cases])

    def test_ended_rows_with_actual_handback_keep_the_instance_for_accounting(self):
        r = self.init('alice')
        module = core_loader.SourceFileLoader('ledger_actual_instance', str(CORE / 'bin/brain')).load_module()
        inst, n = module.Instance(r), module.Instance(r).node('root')
        row = {'Job': 'job/closed', 'Started': '2020-01-01T00:00Z',
               'Ended': '2020-01-01T01:00Z', 'Reviews': '1'}
        path = module.handback_file(n, row)
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({'job': row['Job'], 'started': row['Started'],
            'state': 'done', 'actual': {'version': 1, 'tokens': 10}}))
        with mock.patch.object(module, 'actual_cost', return_value={'version': 1, 'tokens': 20}) as actual:
            module.write_jobs(inst, n, [row])
        self.assertIs(actual.call_args.args[0], inst)
        self.assertEqual(json.loads(module.read_jobs(n)[0]['Actual'])['tokens'], 20)

    def test_retained_handback_never_lowers_open_review_counts(self):
        r = self.init("alice")
        module = core_loader.SourceFileLoader("ledger_open_reviews", str(CORE / "bin/brain")).load_module()
        inst, n = module.Instance(r), module.Instance(r).node("root")
        rows = [
            {"Job": "job/open", "Started": "2020-01-01T00:00Z", "Reviews": "0", "Class": "standard", "Model": "fixture"},
            {"Job": "job/study", "Started": "2020-01-01T00:00Z", "Reviews": "2"},
            {"Job": "job/closed", "Started": "2020-01-01T00:00Z", "Ended": "2020-01-01T01:00Z", "Reviews": "7"},
        ]
        for row, rounds in zip(rows, (3, 0, 3)):
            hb = {"job": row["Job"], "started": row["Started"], "state": "done"}
            hb.update({"review_rounds": rounds} if row["Job"] == "job/study" else {"ready": {"review_rounds": rounds}})
            f = module.handback_file(n, row)
            f.parent.mkdir(exist_ok=True)
            f.write_text(json.dumps(hb))
        module.write_jobs(inst, n, rows)
        with mock.patch.dict(os.environ, self.env, clear=True):
            module.job_reviewed(inst, n, "job/open")
        repaired = module.read_jobs(n)
        self.assertEqual([row["Reviews"] for row in repaired], ["4", "2", "3"])
        self.assertEqual((repaired[0]["Class"], repaired[0]["Model"]), ("standard", "fixture"))
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.assertTrue(any("over the cap" in line for line in module.job_advice(inst, n, repaired[0])))



class JobLessonOutcomes(Scenario):
    def test_merge_requires_outcome_but_historical_rows_do_not(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "app")
        self.b(r, "new", "a", "--title", "A", "--home", str(home))
        led = r / "nodes/a/job-ledger.md"
        led.write_text("<!-- brain:job-ledger v1 -->\n\n| Job | Started | Ended | Hours | Outcome | Commits"
                       " | Reviews | Runtime | Cause |\n| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
                       "| old | 2020-01-01T00:00Z | 2020-01-01T01:00Z | 1 | merged | 1 | 1 | codex |  |\n")
        self.assertNotIn("lesson outcome", self.b(r, "check"))
        self.b(r, "wt", "add", "a", "job")
        wt = self.tmp / "app-work/worktrees/job"
        self.git("commit", "--allow-empty", "-qm", "job", cwd=wt)
        self.git("merge", "-q", "job", cwd=home)
        self.b(r, "wt", "prune", "a")
        self.assertIn("merged job job has no lesson outcome", self.b(r, "check"))
        self.assertNotIn("merged job old has", self.b(r, "check"))
        for value in ("none", "none: ", "unknown"):
            self.b(r, "jobs", "a", "--branch", "job", "--lessons", value, ok=False)
        self.b(r, "jobs", "a", "--branch", "job", "--lessons", "none: mechanical change")
        self.assertNotIn("lesson outcome", self.b(r, "check"))
        self.assertIn("none: mechanical change", self.b(r, "jobs", "a", "--branch", "job"))
        self.assertNotIn("merged job old has", self.b(r, "check"))

    def test_learned_item_ids_and_switches_are_recorded_without_other_files(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "app")
        self.b(r, "new", "a", "--title", "A", "--home", str(home))
        self.b(r, "wt", "add", "a", "job|pipe")
        self.b(r, "item", "add", "method", "--from", "a", "--title", "Method", "--kind", "lesson",
               "--ref", "report@commit", "--body", "Keep a control.")
        st = r / "nodes/a/status.md"
        st.write_text(st.read_text() + "\nuncommitted coordinator work\n")
        self.b(r, "jobs", "a", "--branch", "job|pipe", "--lessons", "method",
               "--switch", "claudeAgent -> codex: usage limit")
        out = self.b(r, "jobs", "a", "--branch", "job|pipe")
        self.assertIn('"Lessons": "method"', out)
        self.assertIn("claudeAgent -> codex: usage limit", out)
        self.assertEqual(self.git("status", "--porcelain", "--", "nodes", cwd=r).strip(), "M nodes/a/status.md")
        self.b(r, "jobs", "--lessons", "method", ok=False)
        brief = self.b(r, "brief", "a", "--branch", "job|pipe")
        self.assertIn("--lessons", brief)
        self.assertIn("none: <reason>", brief)

    def test_proposed_import_lessons_close_jobs_without_acceptance_or_warning(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "app")
        self.b(r, "adopt", str(home), "--id", "a")
        self.b(r, "wt", "add", "a", "job/import")
        wt = self.tmp / "app-work/worktrees/job-import"
        self.git("commit", "--allow-empty", "-qm", "study complete", cwd=wt)
        self.git("merge", "-q", "job/import", cwd=home)
        self.b(r, "wt", "prune", "a")
        self.assertIn("has no lesson outcome", self.b(r, "check"))
        for iid, source in (("import-lesson", "a"), ("wrong-source", "root")):
            self.b(r, "item", "add", iid, "--from", source, "--at", source, "--title", "Bound reads",
                   "--kind", "lesson", "--ref", "README.md@" + "a" * 40, "--repo", "fixture",
                   "--commit", "a" * 40, "--proposed", "--body", "Bound reads before parsing.")
        self.b(r, "jobs", "a", "--branch", "job/import", "--lessons", "wrong-source", ok=False)
        self.b(r, "jobs", "a", "--branch", "job/import", "--lessons", "import-lesson")
        self.assertNotIn("lesson outcome", self.b(r, "check"))
        self.assertIn('"Lessons": "import-lesson"', self.b(r, "jobs", "a", "--branch", "job/import"))
        self.assertIn("status: proposed", (r / "nodes/a/library/import-lesson.md").read_text())
        self.b(r, "usable", "import-lesson", "a", ok=False)



class UnifiedHandbacks(Scenario):
    job = _test_verify.WorkerReadyPackets.job
    packet = _test_verify.WorkerReadyPackets.packet

    def finish(self, r, ok=True):
        return self.b(r, "handback", "--summary", "Synthetic result", "a", "--branch", "job", "--state", "done", "--reviews", "2",
                      "--report", "reports/result.md", "--lessons", "Use python -B to keep verification clean.", ok=ok)

    def due(self, r):
        return json.loads(self.b(r, "due", "a", "--json"))

    def test_queued_checked_native_verifier_runs_unlocked_and_wait_returns_verdict(self):
        r, home, wt = self.job()
        # A real child must acquire the physical lock during native verification.
        # A marker also proves that transport acceptance did not run verification.
        marker = self.tmp / 'native-verification'
        code = ("import fcntl,os,pathlib\n"
                "with open(pathlib.Path(os.environ['BRAIN_ROOT']) / '.brain/lock', 'a') as lock:\n"
                " fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
                "pathlib.Path(" + repr(str(marker)) + ").write_text('ran')\n")
        (wt / 'verify.py').write_text(code)
        self.git('commit', '-qam', 'Verify physical lock release', cwd=wt)
        m = core_loader.SourceFileLoader('checked_native_queue', str(CORE / 'bin/brain')).load_module()
        inst = m.Instance(r)
        with mock.patch.dict(os.environ, {**self.env, 'BRAIN_ROOT': str(r)}, clear=True):
            fd = m.intent_lease(inst)
            self.assertIsNotNone(fd)
            try:
                with m.brain_lock(inst):
                    request_id = self.b(r, 'handback', 'a', '--branch', 'job', '--state', 'done',
                                        '--reviews', '1', '--summary', 'Native queued fixture', '--enqueue').strip()
                self.assertRegex(request_id, '^[0-9a-f]{32}$')
                self.assertFalse(marker.exists())
                self.assertIsNone(m.current_attempt(inst.node('a'), m.open_job(m.read_jobs(inst.node('a')), 'job')))
            finally:
                os.close(fd)
            self.git('commit', '--allow-empty', '-qm', 'Advance before application', cwd=wt)
            with contextlib.redirect_stdout(io.StringIO()):
                m.drain_intents(inst)
        self.assertEqual(marker.read_text(), 'ran')
        receipt = json.loads(self.b(r, 'queue', 'wait', request_id, '--timeout', '0'))
        self.assertEqual((receipt['state'], receipt['ready'], receipt['exit_code']), ('applied', True, 0))
        self.assertEqual(receipt['binding']['head'], self.git('rev-parse', 'HEAD', cwd=wt).strip())
        # The compatibility --record alias uses the same queued native failure.
        (wt / 'verify.py').write_text('raise SystemExit(7)\n')
        self.git('commit', '-qam', 'Fail native fixture', cwd=wt)
        with mock.patch.dict(os.environ, {**self.env, 'BRAIN_ROOT': str(r)}, clear=True):
            fd = m.intent_lease(inst)
            try:
                failed = self.b(r, 'ready', 'a', '--branch', 'job', '--record', '--reviews', '1',
                                '--summary', 'Native queued failure', '--enqueue').strip()
            finally:
                os.close(fd)
            with contextlib.redirect_stdout(io.StringIO()):
                m.drain_intents(inst)
        result = subprocess.run([sys.executable, str(r / 'core/bin/brain'), 'queue', 'wait', failed, '--timeout', '0'],
                                cwd=r, env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        rejected = json.loads(result.stdout)
        self.assertEqual((rejected['state'], rejected['ready'], rejected['exit_code']), ('applied', False, 1))
        self.assertIn('verify failed (exit 7)', rejected['reason'])

    def test_default_checked_handback_runs_in_place_even_behind_a_busy_applier(self):
        r, home, wt = self.job()
        m = core_loader.SourceFileLoader('default_checked_queue', str(CORE / 'bin/brain')).load_module()
        inst = m.Instance(r)
        args = ('handback', 'a', '--branch', 'job', '--state', 'done', '--reviews', '1', '--lessons', 'none: fixture')
        with mock.patch.dict(os.environ, {**self.env, 'BRAIN_ROOT': str(r)}, clear=True):
            fd = m.intent_lease(inst)
            try:
                # Verification binds this caller's environment: no queue, the native verdict here.
                out = self.b(r, *args, '--summary', 'Default checked')
                self.assertEqual(out.strip().splitlines()[-1], 'ready')
                (wt / 'verify.py').write_text('raise SystemExit(7)\n')
                self.git('commit', '-qam', 'Fail native fixture', cwd=wt)
                out = self.b(r, *args, '--summary', 'Default failure', ok=False)
                self.assertIn('verify failed (exit 7)', out)
                self.assertNotIn('queued', out)
                self.b(r, *args, '--summary', 'Retry', '--request-id', 'c' * 32, ok=False)
            finally:
                os.close(fd)
            self.assertEqual(m.intent_index(inst), [])

    def test_question_and_blocked_handbacks_request_wake(self):
        r, home, wt = self.job()
        module = core_loader.SourceFileLoader("brain_handback_wake", str(CORE / "bin/brain")).load_module()
        inst = module.Instance(r)
        args = argparse.Namespace(summary="Synthetic result", question="Choose blue", id="a", branch="job", state="question", lessons="none: fixture",
                                  report=None, reviews=None, need_human="Choose blue")
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(module, "load", return_value=inst), \
                mock.patch.object(module, "wake_start"), contextlib.redirect_stdout(io.StringIO()):
            # No coordinator on a: both calls route to root's configured fake transport.
            inst.local_of("root")["wake"] = "exit 0"
            inst.save_local()
            module.cmd_handback(args)
            args.state, args.need_human = "blocked", None
            module.cmd_handback(args)
        directory = module.wake_directory(inst, inst.node("root"))
        self.assertEqual(len(list(directory.glob("*.pending"))), 2)
        self.assertIn("Choose blue", (r / "nodes/a/inbox.md").read_text())

    def test_direct_handback_wakes_carry_the_watcher_identity_without_a_scan(self):
        r, home, wt = self.job()
        module = core_loader.SourceFileLoader("brain_handback_identity", str(CORE / "bin/brain")).load_module()
        inst = module.Instance(r)
        inst.local_of("root")["wake"] = "exit 0"
        inst.save_local()
        args = argparse.Namespace(summary="Synthetic result", question="Choose blue", id="a", branch="job", state="question",
                                  lessons="none: fixture", report=None, reviews=None, need_human=None)
        directory = module.wake_directory(inst, inst.node("root"))
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(module, "load", return_value=inst), \
                mock.patch.object(module, "wake_start"), contextlib.redirect_stdout(io.StringIO()):
            with mock.patch.object(module, "watch_groups", wraps=module.watch_groups) as scan:
                module.cmd_handback(args)
                question = json.loads((r / "nodes/a/job-handbacks" / (module.job_key(module.open_job(
                    module.read_jobs(inst.node("a")), "job")) + ".json")).read_text())["wake_id"]
                args.state, args.question = "blocked", None
                module.cmd_handback(args)
            self.assertEqual(scan.call_count, 0, "the producer never rediscovers its own episode")
            fresh = module.Instance(r)
            n = fresh.node("a")
            blocked = module.job_signal(fresh, n, module.open_job(module.read_jobs(n), "job"))["wake_id"]
            pending = {p.name: json.loads(p.read_text()) for p in directory.glob("*.pending")}
            self.assertEqual(len(pending), 2)
            binding = module.wake_turn_binding(fresh, fresh.node("root"))
            for episode, kind in ((question, "inbox"), (blocked, "handback")):
                reason = "wake:" + episode
                value = pending[module.wake_request_key(fresh, n, reason) + ".pending"]
                # Exactly what the scan-based capture computes for the same reason and key.
                self.assertEqual(value["items"], module.wake_reason_items(fresh, n, reason))
                self.assertEqual([i["kind"] for i in value["items"]], [kind])
                self.assertNotIn("question", value["items"][0])  # the watcher flags neither form
                self.assertEqual(value["binding"], binding)
            # A replaced attempt or episode, or a merged job, is not this producer's form.
            for attempt, episode in (("0" * 32, blocked), (module.job_signal(fresh, n, module.open_job(
                    module.read_jobs(n), "job"))["attempt_id"], "f" * 32)):
                self.assertIsNone(module.watch_handback_wake_items(fresh, n, "job", attempt, episode))
            attempt = module.job_signal(fresh, n, module.open_job(module.read_jobs(n), "job"))["attempt_id"]
            self.assertTrue(module.watch_handback_wake_items(fresh, n, "job", attempt, blocked))
            with mock.patch.object(module, "local_job_state", return_value={"merged": True}):
                self.assertIsNone(module.watch_handback_wake_items(fresh, n, "job", attempt, blocked))
            # The watcher's own pass finds both episodes recorded; none is published twice.
            module.watch_once(module.Instance(r), quiet=True)
            ids = [i["id"] for p in directory.glob("*.pending") for i in json.loads(p.read_text())["items"]]
            for value in pending.values():
                self.assertEqual(ids.count(value["items"][0]["id"]), 1)
            # An acknowledged blocked need is omitted by the scan; the producer never revives it.
            candidates = module.due_items(fresh, n, include_handled=True)
            target = next(i for i in candidates if i["kind"] == "handback" and i.get("state") == "blocked")
            module.record_reminder(fresh, n, candidates, target["id"], "ack", "Handled fixture",
                                   resume_condition="Fixture changes")
            self.assertEqual(module.wake_reason_items(module.Instance(r), n, "wake:" + blocked), [])
            self.assertIsNone(module.watch_handback_wake_items(module.Instance(r), n, "job", attempt, blocked))

    def test_legacy_and_restart_handback_wakes_keep_the_scan_fallback(self):
        r, home, wt = self.job()
        module = core_loader.SourceFileLoader("brain_handback_fallback", str(CORE / "bin/brain")).load_module()
        inst = module.Instance(r)
        inst.local_of("root")["wake"] = "exit 0"
        inst.save_local()
        args = argparse.Namespace(summary="Synthetic result", id="a", branch="job", state="blocked", lessons="none: fixture",
                                  report=None, reviews=None, need_human="Grant fixture access")
        directory = module.wake_directory(inst, inst.node("root"))
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(module, "load", return_value=inst), \
                mock.patch.object(module, "wake_start"), contextlib.redirect_stdout(io.StringIO()):
            def requested():
                pending = list(directory.glob("*.pending"))
                self.assertEqual(len(pending), 1, "every direct wake is still requested")
                pending[0].unlink()  # delivered elsewhere: the next handback opens a fresh episode
            with mock.patch.object(module, "watch_groups", wraps=module.watch_groups) as scan:
                module.cmd_handback(args)  # its inbox line is the direct episode
                self.assertEqual(scan.call_count, 0)
                requested()
                # A later blocked handback is owned by that older inbox line: unknown form.
                args.need_human = None
                module.cmd_handback(args)
                self.assertEqual(scan.call_count, 1)
                requested()
                (r / "nodes/a/inbox.md").write_text("")
                args.restart = "compactions"  # a restart is an ordinary due item, never this direct form
                module.cmd_handback(args)
                self.assertEqual(scan.call_count, 2)
                requested()

    def test_answered_handback_same_report_gets_a_fresh_wake(self):
        r, home, wt = self.job()
        module = core_loader.SourceFileLoader("brain_handback_episode", str(CORE / "bin/brain")).load_module()
        inst = module.Instance(r)
        calls = self.tmp / "calls"
        inst.local_of("root")["wake"] = "echo called >> " + shlex_quote(str(calls))
        inst.save_local()
        args = argparse.Namespace(summary="Synthetic result", question="Synthetic choice?", id="a", branch="job", state="question", lessons="none: fixture",
                                  report="report.md", reviews=None, need_human=None)
        directory = module.wake_directory(inst, inst.node("root"))
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(module, "load", return_value=inst), \
                mock.patch.object(module, "wake_start"), contextlib.redirect_stdout(io.StringIO()):
            for count in (1, 2):
                module.cmd_handback(args)
                if count == 2:
                    (directory / "last").write_text(str(time.time() - 121))
                module.wake_drain(inst, inst.node("root"), once=True)
                self.assertEqual(len(calls.read_text().splitlines()), count)
                (r / "nodes/a/inbox.md").write_text("")

    def test_repeated_blocked_handback_same_report_wakes_again(self):
        r, home, wt = self.job()
        module = core_loader.SourceFileLoader("brain_blocked_episode", str(CORE / "bin/brain")).load_module()
        inst = module.Instance(r)
        calls = self.tmp / "calls"
        inst.local_of("root")["wake"] = "echo called >> " + shlex_quote(str(calls))
        inst.save_local()
        args = argparse.Namespace(summary="Synthetic result", id="a", branch="job", state="blocked", lessons="none: fixture",
                                  report="report.md", reviews=None, need_human=None)
        directory = module.wake_directory(inst, inst.node("root"))
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(module, "load", return_value=inst), \
                mock.patch.object(module, "wake_start"), contextlib.redirect_stdout(io.StringIO()):
            for count in (1, 2):
                module.cmd_handback(args)
                if count == 2:
                    (directory / "last").write_text(str(time.time() - 121))
                module.wake_drain(inst, inst.node("root"), once=True)
                self.assertEqual(len(calls.read_text().splitlines()), count)

    def test_question_handback_preserves_a_concurrent_runtime_inbox_append(self):
        r, home, wt = self.job()
        loader = core_loader.SourceFileLoader("brain_handback_append", str(CORE / "bin/brain"))
        module = loader.load_module()
        inst = module.Instance(r)
        original = module.no_terms_in
        def concurrent_append(n, text, what):
            original(n, text, what)
            if what == "the inbox question":
                module.inbox_append(n["dir"], "- synthetic runtime question\n")
        args = argparse.Namespace(summary="Synthetic result", question="Choose a colour", id="a", branch="job", state="question", lessons="none: fixture",
                                  report=None, reviews=None, need_human="Choose a colour")
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(module, "load", return_value=inst), \
                mock.patch.object(module, "no_terms_in", side_effect=concurrent_append), \
                contextlib.redirect_stdout(io.StringIO()):
            module.cmd_handback(args)
        text = (r / "nodes/a/inbox.md").read_text()
        self.assertIn("synthetic runtime question", text)
        self.assertIn("Choose a colour", text)

    def test_one_command_one_worker_file_for_ready_state_and_proposals(self):
        r, home, wt = self.job()
        names = ("brief.md", "decisions.md", "node.md", "job-ledger.md")
        before = {name: (r / "nodes/a" / name).read_bytes() for name in names}
        module = core_loader.SourceFileLoader("brain_generated_judgement", str(CORE / "bin/brain")).load_module()
        judgement = module.judgement((r / "nodes/a/status.md").read_text())
        main = self.git("rev-parse", "main", cwd=home)
        self.assertEqual(self.finish(r).strip().splitlines()[-1], "ready")
        data = json.loads(self.packet().read_text())
        self.assertEqual((data["version"], data["job"], data["state"]), (3, "job", "done"))
        self.assertEqual(data['summary'], 'Synthetic result')
        self.assertEqual(data["lessons"], "Use python -B to keep verification clean.")
        self.assertEqual(data["report"], "reports/result.md")
        self.assertEqual(data["ready"]["review_rounds"], 2)
        self.assertTrue(data["at"] and data["started"])
        self.assertFalse((self.tmp / "app-work/scratch/ready").exists())
        self.assertEqual(len(list(self.packet().parent.glob("*.json"))), 1)
        self.assertEqual({name: (r / "nodes/a" / name).read_bytes() for name in names}, before)
        self.assertEqual(module.judgement((r / "nodes/a/status.md").read_text()), judgement)
        self.assertEqual(self.git("rev-parse", "main", cwd=home), main)
        self.assertIn("ready to merge", self.b(r, "due", "a"))
        self.b(r, "status", "a", "--refresh")
        self.assertIn("ready to merge", (r / "nodes/a/status.md").read_text())
        brief = self.b(r, "brief", "a", "--branch", "job")
        self.assertEqual(brief.count("brain handback a --branch job"), 1)
        self.assertNotIn("--record", brief)
        self.assertIn("host message is optional", brief)

    def test_alias_replaces_same_record_and_blocked_question_clear_readiness(self):
        r, home, wt = self.job()
        self.finish(r)
        path = self.packet()
        self.b(r, "ready", "--summary", "Synthetic result", "a", "--branch", "job", "--record", "--reviews", "2")
        self.assertEqual(json.loads(path.read_text())["lessons"], "Use python -B to keep verification clean.")
        self.assertEqual(json.loads(path.read_text())["report"], "reports/result.md")
        self.b(r, "handback", "--summary", "Synthetic result", "a", "--branch", "job", "--state", "done", "--report", "updated-report.md")
        self.assertEqual(self.b(r, "ready", "a", "--branch", "job").strip(), "ready")
        for state in ("blocked", "question"):
            self.b(r, "handback", "--summary", "Synthetic result", "--question", "Synthetic choice?", "a", "--branch", "job", "--state", state,
                   "--report", "Need a choice.", "--lessons", "none: job incomplete")
            self.assertIn("hand-back is " + state, self.b(r, "ready", "a", "--branch", "job", ok=False))
            self.assertEqual(json.loads(path.read_text())["ready"], None)
            self.assertEqual(next(i for i in self.due(r) if i["kind"] == "handback")["state"], state)
        self.b(r, "ready", "--summary", "Synthetic result", "a", "--branch", "job", "--record", "--reviews", "2",
               "--lessons", "none: routine fix")
        self.assertEqual(list(path.parent.glob("*.json")), [path])
        self.assertEqual(json.loads(path.read_text())["lessons"], "none: routine fix")
        self.assertFalse((self.tmp / "app-work/scratch/ready").exists())

    def test_failed_verify_and_changed_main_are_finished_but_not_ready(self):
        r, home, wt = self.job()
        self.finish(r)
        self.git("commit", "--allow-empty", "-qm", "main moves", cwd=home)
        text = self.b(r, "due", "a")
        self.assertIn("eligible for batch; exact-result verification required", text)
        handback, = [i for i in self.due(r) if i["kind"] == "handback"]
        self.assertIs(handback["ready"], False)
        self.assertIn("main moved", self.b(r, "ready", "a", "--branch", "job", ok=False))
        self.git("rebase", "main", cwd=wt)
        (wt / "calc.py").write_text("value = 3\n")
        self.git("commit", "-qam", "wrong value", cwd=wt)
        self.assertIn("verify failed", self.finish(r, ok=False))
        self.assertNotEqual(json.loads(self.packet().read_text())["ready"]["verify_result"], 0)
        self.assertIn("verify missing, changed or failed", self.b(r, "due", "a"))
        self.b(r, "set", "a", "verify=")
        self.assertIn("no verify command", self.finish(r, ok=False))
        self.assertIn("no verify command", self.b(r, "due", "a"))

    def test_legacy_packets_and_signals_read_but_never_override_new_record(self):
        import hashlib
        r, home, wt = self.job()
        self.finish(r)
        path = self.packet()
        data = json.loads(path.read_text())
        old = self.tmp / "app-work/scratch/ready" / (hashlib.sha256(b"job").hexdigest() + ".json")
        old.parent.mkdir(parents=True)
        data["ready"]["checked_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        old.write_text(json.dumps(data["ready"]))
        path.unlink()
        # A genuine pre-attempt writer produced neither a v3 record nor its sidecar.
        module = core_loader.SourceFileLoader('legacy_attempt_fixture', str(CORE / 'bin/brain')).load_module()
        n = module.Instance(r).node('a')
        row = module.open_job(module.read_jobs(n), 'job')
        module.attempt_path(n,row).unlink()
        self.assertEqual(self.b(r, "ready", "a", "--branch", "job").strip(), "ready")
        self.assertIn("ready to merge", self.b(r, "due", "a"))
        legacy = {k: data[k] for k in ("job", "started", "state", "at", "report")}
        path.write_text(json.dumps(legacy))
        self.assertEqual(self.b(r, "ready", "a", "--branch", "job").strip(), "ready")
        self.assertIn("ready to merge", self.b(r, "due", "a"))
        path.write_text(json.dumps({**data, "ready": None}))
        self.assertIn("packet missing", self.b(r, "ready", "a", "--branch", "job", ok=False))
        path.write_text("{bad")
        self.assertIn("unreadable", self.b(r, "ready", "a", "--branch", "job", ok=False))
        self.assertIn("invalid-handback", self.b(r, "due", "a"))

    def test_invalid_lessons_do_not_replace_previous_worker_file(self):
        r, home, wt = self.job()
        self.finish(r)
        original = self.packet().read_bytes()
        for lesson in ("", "none", "none:"):
            self.b(r, "handback", "--summary", "Synthetic result", "a", "--branch", "job", "--state", "done", "--lessons", lesson, ok=False)
            self.assertEqual(self.packet().read_bytes(), original)

    def test_merged_required_lesson_outcomes_due_and_facts_clear_on_resolution(self):
        r, home, wt = self.job()
        self.finish(r)
        notice, = [i for i in self.due(r) if i['kind'] == 'inbox']
        self.assertRegex(notice['text'], r'command completed: command completed successfully .*<!-- machine-wait:[0-9a-f]+:completed -->')
        inbox = r / 'nodes/a/inbox.md'
        source = inbox.read_bytes()
        request, = [i for i in json.loads(self.b(r, 'inbox', 'a', '--json'))
                    if i['kind'] == 'inbox' and ':completed -->' in i['text']]
        # Terminal delivery is informational and expires; it cannot be answered later.
        refused = self.b(r, 'answer', request['id'], 'Observed successful fixture verification', ok=False)
        self.assertIn('unknown or ambiguous item ID; inspect the current inbox or due output', refused)
        self.assertEqual(inbox.read_bytes(), source)
        # The lesson workflow still answers a real unresolved request and retains its source.
        request = self.b(r, 'inbox', 'a', '--add', 'question: Which retained lesson resolution should be recorded?').strip()
        self.b(r, 'queue', 'wait', request)
        source = inbox.read_bytes()
        request, = [i for i in json.loads(self.b(r, 'inbox', 'a', '--json'))
                    if i['kind'] == 'inbox' and 'Which retained lesson resolution' in i['text']]
        self.b(r, 'answer', request['id'], 'Observed successful fixture verification')
        self.assertEqual(inbox.read_bytes(), source)
        self.assertNotIn('inbox', [i['kind'] for i in self.due(r)])
        self.git("merge", "-q", "job", cwd=home)
        self.b(r, "wt", "prune", "a", "--offline")
        self.assertEqual(len([i for i in self.due(r) if i["kind"] == "lesson-outcome"]), 1)
        self.b(r, "status", "a", "--refresh")
        self.assertIn("missing; coordinator must record outcome", (r / "nodes/a/status.md").read_text())
        self.b(r, "jobs", "a", "--branch", "job", "--lessons", "none: fix specific to this job")
        self.assertNotIn("lesson-outcome", [i["kind"] for i in self.due(r)])
        self.assertNotIn("stale-status", [i["kind"] for i in self.due(r)])
        self.b(r, "status", "a", "--refresh")
        self.assertIn("recorded; see brain jobs", (r / "nodes/a/status.md").read_text())
        self.assertEqual([i["kind"] for i in self.due(r)], ["missing-preview", "unsettled-threads"])
        self.b(r, "jobs", "a", "--attestation-file", settlement_export(self, r, 'a', branch="job"), "--branch", "job", "--settled")
        self.b(r, "set", "a", "preview=http://localhost:4100", "preview_tour=reports/tour.md")
        self.b(r, "status", "a", "--refresh")
        pending = self.due(r)
        self.assertNotIn('lesson-outcome', [i['kind'] for i in pending])
        preview, = pending
        self.assertEqual((preview['node'], preview['kind'], preview['condition']),
                         ('a', 'preview-evidence', 'source-bound-preview-tour'))
        self.assertEqual(json.loads(self.b(r, 'preview', 'a', '--json')),
                         {'state': 'missing', 'reason': 'no source-bound screenshot tour recorded'})

    def test_unbound_home_does_not_break_read_only_due_or_ready(self):
        r, home, wt = self.job()
        local = r / ".brain/local.json"
        data = json.loads(local.read_text())
        data["nodes"]["a"].pop("home", None)
        local.write_text(json.dumps(data))
        self.assertIsInstance(self.due(r), list)
        self.assertIn("external git home", self.b(r, "ready", "a", "--branch", "job", ok=False))

    def test_job_closed_during_verify_cannot_receive_worker_result(self):
        import shlex
        r, home, wt = self.job()
        command = shlex.join([sys.executable, str(CORE / "bin/brain"), "-b", "alice", "wt", "rm", "a", "job",
                              "--force", "--cause", "closed during verification"])
        self.b(r, "set", "a", "verify=" + command)
        self.assertIn("job changed during verification", self.finish(r, ok=False))
        self.assertFalse(list((r / "nodes/a/job-handbacks").glob("*.json")))
        self.assertEqual(len(list((r / "nodes/a/job-handbacks").glob("*.log"))), 1)
        self.assertIn("abandoned", (r / "nodes/a/job-ledger.md").read_text())

    def test_pre_feature_merges_have_no_retroactive_lesson_requirement(self):
        r, home, wt = self.job()
        self.git("merge", "-q", "job", cwd=home)
        self.b(r, "wt", "prune", "a", "--offline")
        ledger = r / "nodes/a/job-ledger.md"
        ledger.write_text(ledger.read_text().replace("| required |", "|  |"))
        self.assertNotIn("lesson-outcome", [i["kind"] for i in self.due(r)])
        self.b(r, "status", "a", "--refresh")
        self.assertIn("legacy; outcome not required", (r / "nodes/a/status.md").read_text())



class LedgerVersionRegressions(Scenario):
    def test_repeated_reads_share_only_immutable_ledger_parsing(self):
        jobs = core_loader.SourceFileLoader("ledger_parse_cache", str(CORE / "bin/brain")).load_module()
        r = self.init("alice")
        n = {"dir": r}
        path = r / "job-ledger.md"
        text = ("<!-- brain:job-ledger v1 -->\n"
                "| Job | Started | Cause | Extra |\n"
                "| --- | --- | --- | --- |\n"
                "| old | 2020-01-01T00:00Z | left\\|right\\\\tail | retained |\n")
        path.write_text(text)
        jobs.parsed_job_rows.cache_clear()
        first = jobs.read_jobs(n)
        self.assertEqual(first[0]['Cause'], 'left|right\\tail')
        self.assertEqual(first[0]['Extra'], 'retained')
        self.assertEqual(first[0]['Ended'], '')
        first[0]['Cause'] = 'caller mutation'
        first.append({'Job': 'injected'})
        second = jobs.read_jobs(n)
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]['Cause'], 'left|right\\tail')
        self.assertEqual(jobs.parsed_job_rows.cache_info().hits, 1)
        # A same-length edit with a restored mtime must still be observed.
        stamp = path.stat()
        path.write_text(text.replace('| old |', '| new |'))
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        self.assertEqual(jobs.read_jobs(n)[0]['Job'], 'new')
        self.assertEqual(jobs.parsed_job_rows.cache_info().misses, 2)
        path.write_text('unrecognized ledger\n')
        self.assertIsNone(jobs.read_jobs(n))
        path.unlink()
        self.assertEqual(jobs.read_jobs(n), [])

    def test_legacy_ledgers_migrate_without_losing_rows_or_extended_fields(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "app")
        self.b(r, "new", "a", "--title", "A", "--home", str(home))
        ledger = r / "nodes/a/job-ledger.md"
        ledger.write_text("<!-- brain:job-ledger v1 -->\n\n"
                          "| Job | Started | Ended | Hours | Outcome | Commits | Reviews | Runtime | Cause |\n"
                          "| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
                          "| old | 2020-01-01T00:00Z | 2020-01-01T01:00Z | 1 | merged | 1 | 1 | codex | original |\n")
        self.b(r, "wt", "add", "a", "job")
        self.b(r, "jobs", "a", "--branch", "job", "--lessons", "none: mechanical",
               "--switch", "claude -> codex: usage limit")
        text = ledger.read_text()
        self.assertIn("<!-- brain:job-ledger v3 -->", text)
        # The exact recognition gate in a v1 core's read_jobs returns None, so its writers skip the file.
        self.assertNotIn("<!-- brain:job-ledger v1 -->", text)
        self.assertIn("| old | 2020-01-01T00:00Z", text)
        self.assertIn("| original |", text)
        self.assertIn('"Lessons": "none: mechanical"', self.b(r, "jobs", "a", "--branch", "job"))
        # Also migrate the 12-column v1 files emitted earlier on this feature branch.
        ledger.write_text(text.replace("brain:job-ledger v3", "brain:job-ledger v1"))
        self.b(r, "review", "a", "--branch", "job", "--built-by", "claude")
        out = self.b(r, "jobs", "a", "--branch", "job")
        self.assertIn('"Lessons": "none: mechanical"', out)
        self.assertIn('"Reviews": "1"', out)
        self.assertIn("claude -> codex: usage limit", out)
        self.assertNotIn("<!-- brain:job-ledger v1 -->", ledger.read_text())
        self.assertIn("✓", self.b(r, "check"))



class Measures(Scenario):
    def test_jobs_are_measured_from_start_to_end(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "code" / "app")
        self.b(r, "new", "app", "--title", "App", "--home", str(home))
        self.b(r, "wt", "add", "app", "job-a")
        self.b(r, "wt", "add", "app", "job-b")
        jobs = (r / "nodes" / "app" / "job-ledger.md").read_text()
        self.assertIn("| job-a |", jobs)
        self.assertEqual(self.git("status", "--porcelain", "--", "nodes/app/job-ledger.md", cwd=r).strip(), "")  # committed
        self.b(r, "review", "app", "--branch", "job-a", "--built-by", "claude")
        self.b(r, "review", "app", "--branch", "job-a", "--built-by", "claude", "--reason", "security")
        wa = self.tmp / "code" / "app-work" / "worktrees" / "job-a"
        (wa / "a.txt").write_text("a\n")
        self.git("add", "a.txt", cwd=wa); self.git("commit", "-qm", "A", cwd=wa)
        self.git("merge", "-q", "job-a", cwd=home)
        self.assertIn("removed: job-a", self.b(r, "wt", "prune", "app"))
        self.b(r, "wt", "rm", "app", "job-b", "--cause", "superseded by job-a")
        rows = {l.split("|")[1].strip(): [c.strip() for c in l.split("|")[1:-1]]
                for l in (r / "nodes" / "app" / "job-ledger.md").read_text().splitlines() if l.startswith("| job-")}
        self.assertEqual(rows["job-a"][4], "merged")
        self.assertEqual(rows["job-a"][5], "1")                 # commits
        self.assertEqual(rows["job-a"][6], "2")                 # review rounds
        self.assertEqual(rows["job-b"][4], "abandoned")
        self.assertEqual(rows["job-b"][8], "superseded by job-a")
        out = self.b(r, "jobs", "app")
        self.assertIn("2 finished (1 merged, 1 abandoned)", out)
        self.assertIn("2 review round(s) per merged job", out)
        self.assertIn("cause superseded by job-a", out)
        self.b(r, "wt", "add", "app", "job-c")
        self.assertIn("open  job-c", self.b(r, "jobs", "app"))
        self.assertIn("✓", self.b(r, "check"))
        self.b(r, "wt", "add", "app", "job|pipe")                  # git allows it; the row must still be found
        self.b(r, "review", "app", "--branch", "job|pipe", "--built-by", "claude")
        self.b(r, "wt", "rm", "app", "job|pipe")
        self.assertIn("job|pipe", (r / "nodes" / "app" / "job-ledger.md").read_text().replace("\\|", "|"))
        self.assertNotIn("open  job|pipe", self.b(r, "jobs", "app"))

    def test_a_file_brain_did_not_write_is_never_overwritten(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "code" / "app")
        self.b(r, "new", "app", "--title", "App", "--home", str(home))
        mine = r / "nodes" / "app" / "job-ledger.md"
        mine.write_text("# My own notes\n\nAcceptance: the export must round-trip.\n")
        out = self.b(r, "wt", "add", "app", "job-a")
        self.assertIn("isn't a job ledger written by brain", out)
        self.assertEqual(mine.read_text(), "# My own notes\n\nAcceptance: the export must round-trip.\n")

    def test_medians_and_daylight_saving(self):
        from core_loader import SourceFileLoader
        import datetime as dt
        from zoneinfo import ZoneInfo
        m = SourceFileLoader("brain_mod", str(CORE / "bin" / "brain")).load_module()
        ny = ZoneInfo("America/New_York")
        a, b = dt.datetime(2026, 3, 8, 1, 30, tzinfo=ny), dt.datetime(2026, 3, 8, 3, 30, tzinfo=ny)
        self.assertEqual(m.elapsed(a, b), 3600)                    # clocks jump forward: one real hour
        r = self.init("alice")
        self.b(r, "new", "app", "--title", "App")
        led = r / "nodes" / "app" / "job-ledger.md"
        led.write_text("# Jobs: app\n\n<!-- brain:job-ledger v1 -->\n\n| Job | Started | Ended | Hours | Outcome | Commits"
                       " | Reviews | Runtime | Cause |\n| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
                       "| a | 2026-01-01T00:00+00:00 | 2026-01-01T01:00+00:00 | 1.0 | merged | 1 | 1 | claude |  |\n"
                       "| b | 2026-01-01T00:00+00:00 | 2026-01-01T09:00+00:00 | 9.0 | merged | 1 | 3 | claude |  |\n")
        self.assertIn("median 5.0 h and 2 review round(s)", self.b(r, "jobs", "app"))

    def test_jobs_survive_commands_running_at_once(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "code" / "app")
        self.b(r, "new", "app", "--title", "App", "--home", str(home))
        self.b(r, "wt", "add", "app", "job-a")
        st = r / "nodes" / "app" / "status.md"
        st.write_text(st.read_text() + "\n- the coordinator's edit, not committed yet\n")
        ps = [subprocess.Popen([sys.executable, str(r / "core" / "bin" / "brain"), "review", "app", "--branch", "job-a",
                                "--built-by", "claude", "--reason", "security"], cwd=r, env=self.env, stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, text=True) for _ in range(16)]
        errs = [p.communicate()[1] for p in ps]
        self.assertEqual([p.returncode for p in ps], [0] * 16, errs)
        row = next(l for l in (r / "nodes" / "app" / "job-ledger.md").read_text().splitlines() if l.startswith("| job-a"))
        self.assertEqual(row.split("|")[7].strip(), "16")         # no round lost
        self.assertEqual(self.git("status", "--porcelain", "--", "nodes", cwd=r).strip(), "M nodes/app/status.md")

    def test_stale_compares_moments_not_digits(self):
        r = self.init("alice")
        self.b(r, "new", "app", "--title", "App")
        inst = r / "instance.md"
        inst.write_text(inst.read_text().replace("timezone: UTC", "timezone: Asia/Beirut"))
        st = r / "nodes" / "app" / "status.md"
        lines = st.read_text().splitlines()
        lines[0] = "Updated: 2026-10-19 06:31 by app coordinator (claude)"   # Beirut, UTC+3: 03:31Z
        st.write_text("\n".join(lines) + "\n")
        self.assertIn("current", self.b(r, "stale", "app", "2026-10-19T03:20:00Z"))     # looks later, isn't
        out = self.b(r, "stale", "app", "2026-10-19T05:00:00.000Z")
        self.assertIn("behind by 1h 29m", out)
        self.assertIn("node-sync", out)
        self.assertIn("current", self.b(r, "stale", "app", "2026-10-19T06:33:00+03:00"))  # within the grace
        self.b(r, "stale", "app", "2026-10-19 05:00", ok=False)                          # no zone: refused



class ThreadSettlement(AutonomyProject):
    def ledger(self, r, rows, node="app", version="v3"):
        cols = ["Job", "Started", "Ended", "Outcome", "Reviews", "Hours", "Settled", "Future field"]
        path = r / "nodes" / node / "job-ledger.md"
        path.write_text("<!-- brain:job-ledger " + version + " -->\n" +
                        "| " + " | ".join(cols) + " |\n| " + " | ".join("---" for _ in cols) + " |\n" +
                        "".join("| " + " | ".join(row.get(c, "") for c in cols) + " |\n" for row in rows))
        return path

    def row(self, branch="job-old", outcome="merged", ended="2026-01-01T12:00+00:00", settled=""):
        return {"Job": branch, "Started": "2026-01-01T10:00+00:00", "Ended": ended,
                "Outcome": outcome, "Settled": settled, "Future field": "kept"}

    def test_count_all_closed_rows_and_root_children_read_only(self):
        r, home, wt = self.project()
        rows = [self.row("job-" + str(i), outcome=("merged", "abandoned", "studied", "closed")[i % 4])
                for i in range(151)]
        rows += [self.row("job-open", ended=""), self.row("job-unknown", outcome="unknown"),
                 self.row("job-settled", settled="2026-01-01T13:00+00:00")]
        ledger = self.ledger(r, rows)
        before = ledger.read_bytes()
        inherited = [i for i in self.due(r, 'root') if i['node'] == 'app']
        self.assertIn('missing-worktree', [i['kind'] for i in inherited])
        self.assertIn('missing-preview', [i['kind'] for i in inherited])
        # A distinct child coordinator owns actionable needs; root retains backlog.
        self.b(r, 'set', 'app', 'coordinator=fixture-app-thread')
        for node in ("app", "root"):
            due = self.due(r, node)
            item = next(i for i in due if i["kind"] == "unsettled-threads" and i["node"] == "app")
            self.assertEqual(item["count"], 151)
            if node == "root":
                child = [i for i in due if i['node'] == 'app']
                landed = [i for i in child if i['kind'] == 'job-landed']
                self.assertEqual(len(landed), sum(bool(r['Ended']) and r['Outcome'] == 'merged' for r in rows))
                self.assertTrue(all(i['source'] == 'app' for i in landed))
                self.assertEqual(len({i['id'] for i in landed}), len(landed))
                self.assertEqual([i['kind'] for i in child if i['kind'] != 'job-landed'], ['unsettled-threads'])
            self.assertIn("151 closed job(s)", item["text"])
        self.assertIn("151 closed job(s)", self.b(r, "jobs", "app"))
        self.assertIn("unsettled job-0 (brain-job:", self.b(r, "jobs", "app"))
        self.assert_checklist(self.b(r, "jobs", "app", "--branch", "job-0"))
        state = json.loads(self.b(r, "ui", "--json"))
        self.assertEqual(next(n for n in state["nodes"] if n["id"] == "app")["jobs"]["unsettled"], 151)
        self.assertTrue(any(i["kind"] == "unsettled-threads" for i in state["due"]))
        self.assertEqual(ledger.read_bytes(), before)
        self.b(r, "status", "app", "--refresh")
        self.assertIn("Closed jobs with unsettled threads: 151", (r / "nodes/app/status.md").read_text())

    def test_legacy_sweep_clears_backlog_without_markers_and_caps_output(self):
        r, home, wt = self.project()
        rows = [self.row("legacy-" + str(i)) for i in range(32)]
        rows += [self.row("open-other", ended=""), self.row("unknown", outcome="unknown"),
                 self.row("already", settled="2020-01-01T10:00+00:00")]
        ledger = self.ledger(r, rows, version="v1")
        self.assertNotIn("brain-job:", ledger.read_text())
        out = self.b(r, "jobs", "app")
        self.assertEqual(sum(line.startswith("  unsettled ") for line in out.splitlines()), 10)
        self.assertIn("… and 22 more", out)
        self.assertIn("32 closed job(s) recorded settled", self.b(r, "jobs", "app", "--attestation-file", settlement_export(self, r, 'app', all_closed=True), "--settled", "--all-closed"))
        self.assertNotIn("unsettled-threads", [i["kind"] for i in self.due(r)])
        def detail(branch):
            return [json.loads(line) for line in self.b(r, "jobs", "app", "--branch", branch).splitlines()
                    if line.startswith("{")]
        for row in rows:
            actual = detail(row["Job"])[0]
            self.assertEqual(actual["Future field"], "kept")
            if row["Job"] in ("open-other", "unknown"):
                self.assertEqual(actual["Settled"], "")
            else:
                self.assertTrue(actual["Settled"])
        self.assertEqual(detail("already")[0]["Settled"], "2020-01-01T10:00+00:00")
        before = ledger.read_bytes()
        self.assertIn("0 closed job(s)", self.b(r, "jobs", "app", "--attestation-file", settlement_export(self, r, 'app', all_closed=True), "--settled", "--all-closed"))
        self.assertEqual(before, ledger.read_bytes())
        # A later job reusing a swept branch starts without an attestation.
        self.b(r, "wt", "add", "app", "legacy-0")
        self.b(r, "jobs", "app", "--attestation-file", settlement_export(self, r, 'app', all_closed=True), "--settled", "--all-closed")
        self.assertEqual(detail("legacy-0")[1]["Settled"], "")
        self.b(r, "wt", "rm", "app", "legacy-0", "--cause", "fixture finished")
        self.assertEqual(detail("legacy-0")[1]["Settled"], "")
        self.assertEqual(next(i for i in self.due(r) if i["kind"] == "unsettled-threads")["count"], 1)

    def test_sweep_refuses_open_target_branch_atomically_and_rejects_mixed_flags(self):
        r, home, wt = self.project()
        older, newer = self.row("reused"), self.row("reused", ended="")
        newer["Started"] = "2026-02-01T10:00+00:00"
        ledger = self.ledger(r, [self.row("safe"), older, newer])
        before = ledger.read_bytes()
        out = self.b(r, "jobs", "app", "--attestation-file", settlement_export(self, r, 'app', all_closed=True), "--settled", "--all-closed", ok=False)
        self.assertIn("a branch to stamp has an open job", out)
        self.assertEqual(before, ledger.read_bytes())
        for flags in (("--all-closed",), ("--settled", "--all-closed", "--branch", "safe"),
                      ("--settled", "--all-closed", "--lessons", "none: fixture"),
                      ("--settled", "--all-closed", "--start"), ("--settled", "--all-closed", "--close"),
                      ("--settled", "--all-closed", "--switch", "a -> b: fixture"),
                      ("--settled", "--all-closed", "--usage-file", "missing"),
                      ("--settled", "--all-closed", "--contract-file", "missing")):
            self.b(r, "jobs", "app", *flags, ok=False)
            self.assertEqual(before, ledger.read_bytes())
        self.b(r, "jobs", "--settled", "--all-closed", ok=False)

    def test_settled_is_guarded_idempotent_and_preserves_legacy_columns(self):
        r, home, wt = self.project()
        for version in ("v1", "v2", "v3"):
            ledger = self.ledger(r, [self.row()], version=version)
            self.b(r, "jobs", "app", "--attestation-file", settlement_export(self, r, 'app', branch="job-old"), "--branch", "job-old", "--settled")
            row = json.loads(next(l for l in self.b(r, "jobs", "app", "--branch", "job-old").splitlines()
                                  if l.startswith("{")))
            self.assertRegex(row["Settled"], r"^\d{4}-\d\d-\d\dT")
            self.assertEqual(row["Future field"], "kept")
            # Move the recorded time back, so always-stamping cannot pass in the same minute.
            ledger.write_text(ledger.read_text().replace(row["Settled"], "2020-01-01T10:00+00:00"))
            before = ledger.read_bytes()
            self.b(r, "jobs", "app", "--attestation-file", settlement_export(self, r, 'app', branch="job-old"), "--branch", "job-old", "--settled")
            self.assertEqual(before, ledger.read_bytes())
            self.assertNotIn("unsettled-threads", [i["kind"] for i in self.due(r)])
        for rows in ([self.row(ended="")], [self.row(outcome="unknown")],
                     [self.row(ended=""), self.row()], []):
            ledger = self.ledger(r, rows)
            before = ledger.read_bytes()
            self.b(r, "jobs", "app", "--attestation-file", settlement_export(self, r, 'app', branch="job-old"), "--branch", "job-old", "--settled", ok=False)
            self.assertEqual(before, ledger.read_bytes())
        older, newer = self.row(), self.row()
        newer["Started"] = "2026-01-02T10:00+00:00"
        ledger = self.ledger(r, [older, newer])
        self.b(r, "jobs", "app", "--attestation-file", settlement_export(self, r, 'app', branch="job-old"), "--branch", "job-old", "--settled", ok=False)
        detail = self.b(r, "jobs", "app", "--branch", "job-old")
        keys = re.findall(r"--job-key ([0-9a-f]{24}) --settled", detail)
        self.assertEqual(len(keys), 2)
        # Exact key must not bypass an open job on the same branch.
        newer["Ended"] = ""
        ledger = self.ledger(r, [older, newer])
        before = ledger.read_bytes()
        self.assertIn("another job on that branch is open", self.b(r, "jobs", "app", "--attestation-file", settlement_export(self, r, 'app', branch="job-old", key=keys[0]), "--branch", "job-old",
                      "--job-key", keys[0], "--settled", ok=False))
        self.assertEqual(before, ledger.read_bytes())
        newer["Ended"] = "2026-01-02T12:00+00:00"
        self.ledger(r, [older, newer])
        for key in keys:
            self.b(r, "jobs", "app", "--attestation-file", settlement_export(self, r, 'app', branch="job-old", key=key), "--branch", "job-old", "--job-key", key, "--settled")
        self.assertNotIn("unsettled-threads", [i["kind"] for i in self.due(r)])
        ledger = self.ledger(r, [self.row()])
        for flags in (("--close",), ("--start",), ("--lessons", "none: test"), ("--switch", "a -> b: test")):
            before = ledger.read_bytes()
            self.b(r, "jobs", "app", "--attestation-file", settlement_export(self, r, 'app', branch="job-old"), "--branch", "job-old", "--settled", *flags, ok=False)
            self.assertEqual(before, ledger.read_bytes())
        self.b(r, "jobs", "--settled", ok=False)
        for flags in ((), ("--settled",)):
            self.assertIn("no job with that --job-key", self.b(r, "jobs", "app", "--branch", "job-old",
                          "--job-key", "0" * 24, *flags, ok=False))
        before = ledger.read_bytes()
        self.b(r, "jobs", "app", "--branch", "job-old", "--job-key", "0" * 24,
               "--lessons", "none: fixture", ok=False)
        self.assertEqual(before, ledger.read_bytes())

    def assert_checklist(self, out):
        for expected in ("Threads to settle", "brain-job:", "parentThreadId", "running or waiting",
                         "pending requests", "Never settle coordinators", "open jobs", "--settled"):
            self.assertIn(expected, out)

    def test_rm_and_prune_print_checklist_and_closed_worker_reference(self):
        for closure in ("rm", "prune"):
            with self.subTest(closure=closure):
                r, home, wt = self.project()
                status = r / "nodes/app/status.md"
                status.write_text(status.read_text().replace("| job-a | - | - | open |\n", "") + "\n## Active work\n\n"
                                  "| branch | thread | PR | state |\n| --- | --- | --- | --- |\n"
                                  "| job-a | fixture-worker | - | merged |\n")
                if closure == "prune":
                    (wt / "result").write_text("done")
                    self.git("add", "result", cwd=wt)
                    self.git("commit", "-qm", "result", cwd=wt)
                    self.git("merge", "-q", "job-a", cwd=home)
                    dry = self.b(r, "wt", "prune", "app", "--offline", "--dry-run")
                    self.assertNotIn("Threads to settle", dry)
                    out = self.b(r, "wt", "prune", "app", "--offline")
                else:
                    out = self.b(r, "wt", "rm", "app", "job-a", "--cause", "fixture done")
                self.assert_checklist(out)
                self.assertIn("fixture-worker", out)
                self.assertEqual(next(i for i in self.due(r) if i["kind"] == "unsettled-threads")["count"], 1)
                self.b(r, "jobs", "app", "--attestation-file", settlement_export(self, r, 'app', branch="job-a"), "--branch", "job-a", "--settled")
                self.assertNotIn("unsettled-threads", [i["kind"] for i in self.due(r)])
                # New job on the same branch must start unsettled and cannot be attested while open.
                self.b(r, "wt", "add", "app", "job-a")
                self.b(r, "jobs", "app", "--attestation-file", settlement_export(self, r, 'app', branch="job-a"), "--branch", "job-a", "--settled", ok=False)
                self.assert_checklist(self.b(r, "wt", "rm", "app", "job-a", "--cause", "fixture done"))
                self.assertEqual(next(i for i in self.due(r) if i["kind"] == "unsettled-threads")["count"], 1)
                # Release this fixture before the next subtest uses project().
                self.git("worktree", "prune", cwd=home)
                shutil.rmtree(r)
                shutil.rmtree(home)

    def test_reused_branch_checklist_never_names_open_or_ambiguous_workers(self):
        r, home, wt = self.project()
        older, newer = self.row("job-a"), self.row("job-a", ended="")
        newer["Started"] = "2026-01-02T10:00+00:00"
        self.ledger(r, [older, newer])
        status = r / "nodes/app/status.md"
        status.write_text(status.read_text() + "\n## Active work\n\n"
                          "| branch | thread | PR | state |\n| --- | --- | --- | --- |\n"
                          "| job-a | fixture-worker-old | - | merged |\n"
                          "| job-a | fixture-worker-open | - | running |\n")
        out = self.b(r, "jobs", "app", "--branch", "job-a")
        self.assertIn("ownership ambiguous", out)
        self.assertNotIn("fixture-worker-open", out)
        self.assertNotIn("fixture-worker-old", out)
        # Even when the new job ends, branch-only matching cannot assign two historical workers.
        newer["Ended"] = "2026-01-02T12:00+00:00"
        self.ledger(r, [older, newer])
        out = self.b(r, "jobs", "app", "--branch", "job-a")
        self.assertIn("ownership ambiguous", out)
        self.assertNotIn("fixture-worker-open", out)
        status.write_text(status.read_text().replace("| job-a | fixture-worker-open | - | running |\n", ""))
        out = self.b(r, "jobs", "app", "--branch", "job-a")
        self.assertIn("ownership ambiguous", out)
        self.assertNotIn("fixture-worker-old", out)

    def test_single_job_active_work_ownership_guards_are_independent(self):
        r, home, wt = self.project()
        self.ledger(r, [self.row("job-a")])
        status = r / "nodes/app/status.md"
        original = status.read_text().replace("| job-a | - | - | open |\n", "")
        header = "\n## Active work\n\n| branch | thread | PR | state |\n| --- | --- | --- | --- |\n"
        for rows in ("| job-a | fixture-worker-one | - | merged |\n"
                     "| job-a | fixture-worker-two | - | closed |\n",
                     "| job-a | fixture-worker-one | - | running |\n",
                     "| job-a | fixture-worker-one | - | waiting |\n",
                     "| job-a | fixture-worker-one | - | unknown |\n",
                     "| job-a | fixture-worker-one |\n", ""):
            with self.subTest(rows=rows):
                status.write_text(original + header + rows)
                out = self.b(r, "jobs", "app", "--branch", "job-a")
                self.assertIn("ownership ambiguous", out)
                self.assertNotIn("fixture-worker-one", out)
                self.assertNotIn("fixture-worker-two", out)
        for state in ("merged", "abandoned", "closed", "done"):
            status.write_text(original + header + "| job-a | fixture-worker-one | - | " + state + " |\n")
            out = self.b(r, "jobs", "app", "--branch", "job-a")
            self.assertIn("Worker from Active work: fixture-worker-one", out)

    def test_study_close_checklist_and_older_core_preserves_settled(self):
        r = self.init("fixture")
        home = self.repo(self.tmp / "source")
        self.b(r, "adopt", str(home))
        job = "import:" + self.git("rev-parse", "HEAD", cwd=home).strip()
        self.assert_checklist(self.b(r, "jobs", "source", "--branch", job, "--close"))
        self.b(r, "jobs", "source", "--attestation-file", settlement_export(self, r, 'source', branch=job), "--branch", job, "--settled")
        # Execute the actual parent of the feature-introducing commit, not a modified new writer.
        source = Path(__file__).resolve().parent.parent
        history = subprocess.run(["git", "log", "--format=%H", "-S", "def closed_job(row):",
                                  "--", "bin/brain"], cwd=source, env=self.env, capture_output=True, text=True)
        # A module move removes the definition from bin/brain. Select the actual
        # addition, rather than treating the most recent removal as its origin.
        revision = None
        for commit in history.stdout.splitlines():
            added = subprocess.run(["git", "show", commit + ":bin/brain"], cwd=source, env=self.env,
                                   capture_output=True, text=True)
            if added.returncode == 0 and 'def closed_job(row):' in added.stdout:
                revision = commit + "^"
                break
        if revision is None:
            self.skipTest("feature introduction unavailable (installed/shallow source)")
        baseline = subprocess.run(["git", "show", revision + ":bin/brain"], cwd=source, env=self.env,
                                  capture_output=True, text=True)
        if baseline.returncode:
            self.skipTest("older core source unavailable (installed/shallow source)")
        self.assertNotIn('def closed_job(row):', baseline.stdout)
        self.assertIn('brain:job-ledger v3', baseline.stdout)
        old = self.tmp / "older-brain"
        old.write_text(baseline.stdout)
        self.run_(["jobs", "source", "--branch", job, "--lessons", "none: fixture study"], cwd=r, brain=old)
        out = self.b(r, "jobs", "source", "--branch", job)
        row = json.loads(next(l for l in out.splitlines() if l.startswith("{")))
        self.assertTrue(row["Settled"])
        self.assertNotIn("unsettled-threads", [i["kind"] for i in self.due(r, "source")])



class AutonomyHandbackPaths(AutonomyProject):
    def test_handback_stores_and_shows_portable_report_paths(self):
        r, home, wt = self.project()
        report = self.tmp / 'code/app-work/reports/result.md'
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text('Result.\n')
        self.b(r, 'handback', "--summary", "Synthetic result", 'app', '--branch', 'job-a', '--state', 'done', '--report', str(report))
        signal = next((r / 'nodes/app/job-handbacks').glob('*.json'))
        data = json.loads(signal.read_text())
        self.assertEqual(data['report'], '~/code/app-work/reports/result.md')
        self.assertNotIn(str(self.tmp), self.git('show', 'HEAD:' + str(signal.relative_to(r)), cwd=r))
        due = self.b(r, 'due', 'app', '--json')
        self.assertIn('~/code/app-work/reports/result.md', due)
        self.assertNotIn(str(self.tmp), due)
        self.b(r, 'handback', "--summary", "Synthetic result", 'app', '--branch', 'job-a', '--state', 'done', '--report', 'reports/relative.md')
        self.assertEqual(json.loads(signal.read_text())['report'], 'reports/relative.md')



class Workflow(Scenario):
    fixture_templates = True
    fixture_init_in_process = True
    """Less ceremony: core changes reach every coordinator, green verify runs are reused, contracts and advice."""

    def job(self):
        return self.cached_setup('ready-job', lambda: _test_verify.WorkerReadyPackets.job(self))

    def test_missing_launch_reason_warns_coordinator_without_addressing_worker(self):
        r, home, wt = self.job()
        self.b(r, "specialist", "new", "tester", "--title", "Tester")
        saved = self.tmp / "briefs" / "worker.md"
        saved.parent.mkdir()
        def capture(*args):
            return subprocess.run([sys.executable, str(r / "core/bin/brain"), *args], cwd=r,
                                  env=self.env, capture_output=True, text=True)
        base = ("brief", "a", "--branch", "job", "--task", "Synthetic checks")
        for args in (base, (*base, "--out", str(saved)),
                     ("brief", "a", "--small", "--task", "Synthetic checks"),
                     ("hire", "tester", "a", "--task", "Synthetic checks", "--out", str(saved))):
            result = capture(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("warning: why separate is missing", result.stderr)
            self.assertIn("--why-separate", result.stderr)
            self.assertIn("## Why separate", result.stderr)
            content = saved.read_text() if "--out" in args else result.stdout
            if "--out" in args:
                self.assertEqual(result.stdout.strip(), str(saved))
            self.assertIn("Why a separate agent: not stated by the coordinator", content)
            self.assertNotIn("Coordinator:", content)
            self.assertNotIn("before launching", content)
            self.assertNotIn("warning:", result.stdout)
            explicit = capture(*args, "--why-separate", "Independent concern")
            self.assertEqual(explicit.returncode, 0, explicit.stderr)
            self.assertEqual(explicit.stderr, "")
            body = saved.read_text() if "--out" in args else explicit.stdout
            self.assertIn("Why a separate agent: Independent concern", body)
        contract = self.tmp / "contract.md"
        contract.write_text("".join("## " + field + "\nSynthetic scope.\n\n" for field in
                            ("Outcome", "Authorized", "Discretion", "Stop when", "Needs consent", "Done means")) +
                            "## Why separate\nDistinct ownership.\n")
        self.b(r, "jobs", "a", "--branch", "job", "--contract-file", str(contract))
        result = capture(*base, "--out", str(saved))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout.strip(), str(saved))
        self.assertIn("Why a separate agent: Distinct ownership.", saved.read_text())

    def test_shared_context_and_separation_reason(self):
        r, home, wt = self.job()
        context = self.tmp / "shared.md"
        context.write_text("Synthetic finding: calc has one caller.\n")
        args = ("brief", "a", "--branch", "job", "--task", "Fix the calculation")
        out = self.b(r, *args, "--why-separate", "Independent arithmetic concern", "--context-file", str(context))
        self.assertIn("Why a separate agent: Independent arithmetic concern", out)
        self.assertIn("Synthetic finding: calc has one caller.", out)
        self.assertIn("## Shared context", out)
        local = json.loads((r / ".brain/local.json").read_text())
        self.assertEqual(local["nodes"]["a"]["worktrees"]["job"]["context_inputs"], [str(context)])
        self.assertIn("warning: why separate is missing", self.b(r, *args))
        saved = self.tmp / "briefs" / "worker.md"
        saved.parent.mkdir()
        self.assertEqual(self.b(r, *args, "--out", str(saved), "--why-separate",
                                "Independent arithmetic concern").strip(), str(saved))
        self.assertIn("Why a separate agent: Independent arithmetic concern", saved.read_text())
        self.assertIn("needs a reason", self.b(r, *args, "--why-separate", "  ", ok=False))
        self.assertIn("explicitly waived", self.b(r, *args, "--why-separate", "", "--allow-unexplained"))
        # The successful replacement briefs above update their local replay inputs.
        local = json.loads((r / ".brain/local.json").read_text())
        context.write_text("")
        self.assertIn("shared context file is empty", self.b(r, *args, "--context-file", str(context), ok=False))
        self.assertIn("cannot read shared context file", self.b(r, *args, "--context-file",
                      str(self.tmp / "missing.md"), ok=False))
        unsafe = self.tmp / "unsafe.md"
        unsafe.write_text("token=synthetic-placeholder")
        out = self.b(r, *args, "--context-file", str(unsafe), ok=False)
        self.assertIn("unsafe shared context text", out)
        self.assertNotIn("synthetic-placeholder", out)
        self.assertEqual(json.loads((r / ".brain/local.json").read_text()), local)
        context.write_text("Synthetic Forbidden Phrase")
        (r / "nodes/a/never-name.txt").write_text("Synthetic Forbidden Phrase\n")
        out = self.b(r, *args, "--context-file", str(context), ok=False)
        self.assertNotIn("Synthetic Forbidden Phrase", out)
        self.assertIn("shared context", out)
        out = self.b(r, *args, "--context-file", str(self.tmp / "Synthetic Forbidden Phrase-missing.md"), ok=False)
        self.assertNotIn("Synthetic Forbidden Phrase", out)
        self.assertNotIn("Traceback", out)
        contract = self.tmp / "contract.md"
        contract.write_text("".join("## " + field + "\nSynthetic scope.\n\n" for field in
                            ("Outcome", "Authorized", "Discretion", "Stop when", "Needs consent", "Done means")) +
                            "## Why separate\nDistinct ownership.\n")
        self.b(r, "jobs", "a", "--branch", "job", "--contract-file", str(contract))
        self.assertIn("Why a separate agent: Distinct ownership.", self.b(r, *args))

    def test_open_job_overlap_is_once_per_job_and_clears(self):
        r, home, wt = self.job()
        self.b(r, "wt", "add", "a", "other")
        self.b(r, "wt", "add", "a", "third")
        m = core_loader.SourceFileLoader("brain_aggregation_ledger", str(CORE / "bin/brain")).load_module()
        inst = m.Instance(r)
        node = inst.node("a")
        rows = m.read_jobs(node)
        for row in rows:
            f = m.contract_file(node, row)
            f.parent.mkdir(exist_ok=True)
            f.write_text("## Inputs\n- src/calc.py\n")
        # Old open jobs still count after forty newer completed entries.
        history = [{**rows[0], "Job": "closed-" + str(i), "Ended": rows[0]["Started"],
                    "Outcome": "abandoned", "Hours": "1"} for i in range(40)]
        m.write_jobs(inst, node, rows + history)
        out = self.b(r, "jobs", "a")
        self.assertEqual(out.count("overlapping inputs with"), 3)
        lines = out.splitlines()
        for branch in ("job", "other", "third"):
            row_at = next(i for i, line in enumerate(lines) if line.startswith("  open  " + branch + " "))
            self.assertTrue(lines[row_at + 1].startswith("        " + branch + ": overlapping inputs"))
        due = json.loads(self.b(r, "due", "a", "--json"))
        overlaps = [i for i in due if i["kind"] == "overlapping-inputs"]
        self.assertEqual(len(overlaps), 3)
        self.assertEqual(len({i["job"] for i in overlaps}), 3)
        context = self.tmp / "shared.md"
        context.write_text("Synthetic findings gathered once\n")
        for branch in ("job", "other", "third"):
            self.b(r, "brief", "a", "--branch", branch, "--task", "Work", "--why-separate", "Independent concern",
                   "--context-file", str(context))
        self.assertNotIn("overlapping-inputs", self.b(r, "due", "a", "--json"))
        out = self.b(r, "jobs", "a")
        self.assertNotIn("overlapping inputs", out)
        self.assertEqual(out.count("shares context with"), 3)
        # One job leaves the common context: the unresolved pairs become due again.
        own = self.tmp / "other.md"
        own.write_text("Separate findings\n")
        self.b(r, "brief", "a", "--branch", "other", "--task", "Work", "--context-file", str(own))
        self.assertEqual(self.b(r, "due", "a", "--json").count('"kind": "overlapping-inputs"'), 3)
        for row in rows:
            m.contract_file(node, row).write_text("## Inputs\n- " + row["Job"] + ".py\n")
        self.assertNotIn("overlapping-inputs", self.b(r, "due", "a", "--json"))

    def test_input_paths_cover_directories_globs_and_list_styles(self):
        m = core_loader.SourceFileLoader("brain_aggregation_paths", str(CORE / "bin/brain")).load_module()
        node = {"dir": self.tmp / "node", "home_path": self.tmp / "home", "local": {}}
        rows = [{"Job": b, "Started": "2026-01-01", "Ended": ""} for b in ("one", "two")]
        for left, right in (("- src/", "* src/calc.py"), ("1. src/*.py", "+ src/calc.py"),
                            ("2) src/calc.py", "src/./calc.py")):
            for row, value in zip(rows, (left, right)):
                f = m.contract_file(node, row)
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text("## Inputs\n" + value + "\n")
            self.assertEqual(len(m.job_overlaps(node, rows)), 2, (left, right))
        for left, right in (("src/", "src-other/calc.py"), ("src/*.py", "src/calc.js")):
            for row, value in zip(rows, (left, right)):
                m.contract_file(node, row).write_text("## Inputs\n" + value + "\n")
            self.assertEqual(m.job_overlaps(node, rows), {}, (left, right))

    def test_overlap_brackets_and_symlink_aliases(self):
        m = core_loader.SourceFileLoader("brain_aggregation_aliases", str(CORE / "bin/brain")).load_module()
        home = self.tmp / "home[x]*"
        home.mkdir()
        node = {"dir": self.tmp / "node", "home_path": home, "local": {}}
        rows = [{"Job": b, "Started": "2026-01-01", "Ended": ""} for b in ("one", "two")]
        for left, right, count in (("src/*.py", "src/a.py", 2),
                                   ("app/[id]/page.tsx", "app/i/page.tsx", 0),
                                   ("app/[id]/*.tsx", "app/[id]/page.tsx", 2)):
            for row, value in zip(rows, (left, right)):
                f = m.contract_file(node, row)
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text("## Inputs\n" + value + "\n")
            self.assertEqual(len(m.job_overlaps(node, rows)), count, (left, right))
        alias = self.tmp / "alias"
        alias.symlink_to(home, target_is_directory=True)
        m.contract_file(node, rows[0]).write_text("## Inputs\n" + str(alias / "ctx.md") + "\n")
        m.contract_file(node, rows[1]).write_text("## Inputs\nNone\n")
        node["local"] = {"worktrees": {"two": {"context_inputs": [str(home / "ctx.md")]}}}
        self.assertEqual(len(m.job_overlaps(node, rows)), 2)
        node["local"]["worktrees"]["one"] = {"context_inputs": [str(alias / "ctx.md")]}
        self.assertEqual(m.job_overlaps(node, rows), {})

    def test_hire_and_review_carry_separation_reasons(self):
        r, home, wt = self.job()
        self.b(r, "specialist", "new", "tester", "--title", "Tester")
        hire = ("hire", "tester", "a", "--branch", "job", "--task", "Synthetic checks")
        self.assertIn("warning: why separate is missing", self.b(r, *hire))
        self.assertIn("Why a separate agent: Dedicated test expertise", self.b(r, *hire,
                      "--why-separate", "Dedicated test expertise"))
        self.assertIn("needs a reason", self.b(r, *hire, "--why-separate", "", ok=False))
        review = ("review", "a", "--branch", "job", "--base", "main", "--built-by", "codex")
        self.assertIn("Why a separate agent: Independent second-runtime review.", self.b(r, *review))
        self.assertIn('"Review reason": ""', self.b(r, "jobs", "a", "--branch", "job"))
        self.assertIn("Why a separate agent: Check independently", self.b(r, *review, "--reason", "security",
                      "--why-separate", "Check independently"))
        contract = self.tmp / "contract.md"
        contract.write_text("".join("## " + field + "\nSynthetic scope.\n\n" for field in
                            ("Outcome", "Authorized", "Discretion", "Stop when", "Needs consent", "Done means")) +
                            "## Why separate\nDistinct expertise.\n")
        self.b(r, "jobs", "a", "--branch", "job", "--contract-file", str(contract))
        self.assertIn("Why a separate agent: Distinct expertise.", self.b(r, *hire))
        self.assertIn("Why a separate agent: Independent second-runtime review.", self.b(r, *review, "--reason", "security"))
        contract.write_text(contract.read_text().replace("Distinct expertise.", ""))
        self.b(r, "jobs", "a", "--branch", "job", "--contract-file", str(contract))
        self.assertIn("Why a separate agent: Independent second-runtime review.", self.b(r, *review, "--reason", "security"))

    def test_contract_input_overlap_normalization_and_closed_jobs(self):
        loader = core_loader.SourceFileLoader("brain_aggregation", str(CORE / "bin/brain"))
        m = loader.load_module()
        node = {"dir": self.tmp / "node", "home_path": self.tmp / "home", "local": {}}
        rows = [{"Job": b, "Started": "2026-01-01", "Ended": ""} for b in ("one", "two", "three")]
        for row, inputs in zip(rows, ("- src/./calc.py", "- src/calc.py", "- other.py")):
            f = m.contract_file(node, row)
            f.parent.mkdir(exist_ok=True, parents=True)
            f.write_text("## Inputs\n" + inputs + "\n")
        overlaps = m.job_overlaps(node, rows)
        self.assertEqual(set(overlaps), {m.job_key(r) for r in rows[:2]})
        rows[1]["Ended"] = "2026-01-02"
        self.assertEqual(m.job_overlaps(node, rows), {})
        rows[1]["Ended"] = ""
        # Prose, commands, refs and empty-input sentinels must never create advice.
        for text in ("## Authorized\nEdit `src/calc.py`; rebase on origin/main.\n",
                     "## Outcome\nFix input and/or output; see https://example.test/design.\n",
                     "## Inputs\nNone\n", "## Inputs\n- n/a\n",
                     "## Done means\n`git diff origin/main...HEAD` is clean.\n",
                     "## Outcome\nLegacy contract without inputs.\n"):
            for row in rows:
                m.contract_file(node, row).write_text(text)
            self.assertEqual(m.job_overlaps(node, rows), {}, text)

    def test_changes_reach_coordinators_once(self):
        r = self.init("alice")
        self.b(r, "new", "child", "--title", "Child")
        self.assertIn("nothing new", self.b(r, "changes"))
        self.assertNotIn("core-changes", self.b(r, "due"))
        self.git("commit", "-q", "--allow-empty", "-m", "Upgrade core abc1234 -> def5678", cwd=r)
        due = self.b(r, "due")
        self.assertIn("root: core-changes: core changed:", due)
        self.assertIn("brain changes root", due)
        self.assertNotIn("child: core-changes", due)  # no coordinator runs it here
        out = self.b(r, "changes", "root")
        self.assertIn("## 2026-10-20: Cross-project learning", out)
        self.assertIn("After upgrading:", out)
        self.b(r, "changes", "root", "--seen")
        self.assertNotIn("core-changes", self.b(r, "due"))
        self.assertIn("nothing new", self.b(r, "changes", "root"))
        self.assertIn("Cross-project learning", self.b(r, "changes", "root", "--all"))
        self.b(r, "set", "child", "coordinator=thread-1")
        self.assertIn("child: core-changes", self.b(r, "due", "child"))
        changes = r / "core/CHANGES.md"
        changes.write_text(changes.read_text().replace("Newest first.", "Newest first.\n\n## 2026-12-01: Something new\n\n"
                                                       "For agents:\n- a new rule\n", 1))
        unread = json.loads(self.b(r, "changes", "root", "--json"))
        self.assertEqual([e["id"] for e in unread], ["2026-12-01: Something new"])
        self.assertEqual(self.git("status", "--porcelain", "--", "nodes", "instance.md", cwd=r), "")

    def test_core_update_is_reported_once_and_upgrades_only_when_idle(self):
        r = self.init("alice")
        core, remote = r / "core", self.tmp / "core-remote"
        self.git("init", "-q", "-b", "main", cwd=core)
        self.git("add", "-A", cwd=core)
        self.git("commit", "-qm", "core", cwd=core)
        self.git("clone", "-q", str(core), str(remote), cwd=self.tmp)
        self.git("commit", "-q", "--allow-empty", "-m", "newer core", cwd=remote)
        self.git("remote", "add", "origin", str(remote), cwd=core)
        self.b(r, "due")  # the root tick asks the remote, at most daily
        cached = json.loads((r / ".brain/core-remote.json").read_text())
        self.assertEqual(cached["head"], self.git("rev-parse", "HEAD", cwd=remote).strip())
        first = self.b(r, "inbox", "root", "--new")
        self.assertIn("core update available", first)
        self.assertNotIn("core update available", self.b(r, "inbox", "root", "--new"))  # reported once
        self.assertIn("core update available", self.b(r, "changes"))
        self.b(r, "set", "root", "auto_upgrade=maybe", ok=False)
        self.b(r, "new", "app", "--title", "App")
        self.b(r, "set", "app", "auto_upgrade=on", ok=False)
        self.b(r, "set", "root", "auto_upgrade=on")
        self.assertNotIn("auto_upgrade", (r / "node.md").read_text())
        self.assertNotIn("core update available", self.b(r, "inbox", "root"))  # it will happen by itself
        # Busy: a live verification lease prevents an upgrade, including with an open job.
        home = self.repo(self.tmp / "app-home")
        self.b(r, "set", "app", f"home={home}")
        self.b(r, "wt", "add", "app", "job")
        m = core_loader.SourceFileLoader("brain_auto_upgrade", str(CORE / "bin/brain")).load_module()
        with mock.patch.dict(os.environ, self.env):
            token = m.machine_wait("verify", m.Instance(r), "app")
            try:
                self.assertNotIn("core-upgrade", self.b(r, "due"))
            finally:
                m.machine_release(token)
        out = self.b(r, "due")
        self.assertIn("root: core-upgrade", out)  # The retained idle job allows the attempt (no core submodule here).
        self.assertIn("run brain upgrade", out)
        self.assertEqual(json.loads(self.b(r, "due", "--json"))[0]["kind"], "core-upgrade")
        self.b(r, "wt", "rm", "app", "job", "--cause", "fixture")

    def test_green_verify_runs_are_reused_only_while_they_still_prove_the_commit(self):
        r, home, wt = self.job()
        runs = self.tmp / "runs.txt"
        self.b(r, "set", "a", f"verify=python3 -B verify.py && echo run >> {runs}")
        count = lambda: len(runs.read_text().split()) if runs.exists() else 0

        def finish(*extra):
            return self.b(r, "handback", "--summary", "Synthetic result", "a", "--branch", "job", "--state", "done", "--reviews", "1",
                          "--lessons", "none: fixture", *extra)
        self.assertEqual(finish().strip().splitlines()[-1], "ready")
        self.assertEqual(count(), 1)
        logs = list((r / "nodes/a/job-handbacks").glob("*.log"))
        self.assertEqual(len(logs), 1)
        out = finish()
        self.assertEqual(list((r / "nodes/a/job-handbacks").glob("*.log")), logs)
        self.assertIn("verify: reused the green run", out)
        self.assertIn("nothing changed since", out)
        self.assertTrue(out.strip().endswith("ready"))
        self.assertEqual(count(), 1)
        reused_packet = json.loads(_test_verify.WorkerReadyPackets.packet(self).read_text())["ready"]
        self.assertEqual(reused_packet["verify_log"], str(logs[0]))
        (wt / "NOTES.md").write_text("notes\n")
        self.git("add", "-A", cwd=wt)
        self.git("commit", "-qm", "notes", cwd=wt)
        finish()
        self.assertEqual(count(), 2)  # no verify_skip declared: any change runs it again
        self.b(r, "set", "a", "verify_skip=*.md,docs/*")
        (wt / "docs").mkdir()
        (wt / "docs/guide.txt").write_text("guide\n")
        (wt / "NOTES.md").write_text("more notes\n")
        self.git("add", "-A", cwd=wt)
        self.git("commit", "-qm", "docs", cwd=wt)
        self.assertIn("only 2 file(s) under verify_skip changed", finish())
        self.assertEqual(count(), 2)
        self.assertEqual(self.b(r, "ready", "a", "--branch", "job").strip(), "ready")
        (wt / "calc.py").write_text("value = 2  # still two\n")
        self.git("commit", "-qam", "code", cwd=wt)
        self.assertNotIn("reused", finish())
        self.assertEqual(count(), 3)
        finish("--reverify")
        self.assertEqual(count(), 4)
        self.b(r, "ready", "--summary", "Synthetic result", "a", "--branch", "job", "--record", "--reviews", "1", "--reverify")
        self.assertEqual(count(), 5)
        data = json.loads(_test_verify.WorkerReadyPackets.packet(self).read_text())
        self.assertEqual((data["verify_runs"], data["verify_reused"]), (5, 2))
        self.assertTrue(data["first_ready"])
        jobs = self.b(r, "jobs", "a")
        self.assertIn("verify: 5 run(s), 2 reused", jobs)
        self.assertIn("first ready after", jobs)
        # A failed run is never reused, even with the same command on the same commit.
        flag = self.tmp / "fail"
        self.b(r, "set", "a", f"verify=python3 -B verify.py && echo run >> {runs} && test ! -e {flag}")
        finish()
        flag.write_text("x")
        (wt / "calc.py").write_text("value = 2  # two\n")
        self.git("commit", "-qam", "code again", cwd=wt)
        self.b(r, "handback", "--summary", "Synthetic result", "a", "--branch", "job", "--state", "done", "--reviews", "1", ok=False)
        flag.unlink()
        before = count()
        self.assertNotIn("reused", finish())
        self.assertEqual(count(), before + 1)

    def test_contracts_review_rounds_and_blocked_entries(self):
        r, home, wt = self.job()
        self.assertIn("no contract", self.b(r, "jobs", "a"))
        contract = self.tmp / "contract.md"
        contract.write_text("## Outcome\nProve the login flow.\n\n## Authorized\nRun the four SQL cases.\n")
        out = self.b(r, "jobs", "a", "--branch", "job", "--contract-file", str(contract), ok=False)
        self.assertIn("## Discretion", out)
        contract.write_text("## Outcome\nProve the login flow.\n\n## Authorized\nRun the four SQL cases.\n\n"
                            "## Discretion\nRepair reversible preflight defects.\n\n## Stop when\nThe target changes.\n\n"
                            "## Needs consent\nA live migration.\n\n## Done means\nAll cases pass; resources cleaned up.\n")
        self.b(r, "jobs", "a", "--branch", "job", "--contract-file", str(contract))
        self.assertEqual(len(list((r / "nodes/a/job-contracts").glob("*.md"))), 1)
        self.b(r, "jobs", "a", "--branch", "nope", "--contract-file", str(contract), ok=False)
        brief = self.b(r, "brief", "a", "--branch", "job", "--task", "Prove it")
        self.assertIn("## Contract for this job", brief)
        self.assertIn("Repair reversible preflight defects.", brief)
        self.assertIn("no progress notes, receipts, acknowledgements", brief)
        self.assertNotIn("no contract", self.b(r, "jobs", "a"))
        first = self.b(r, "review", "a", "--branch", "job", "--built-by", "claude")
        self.assertNotIn("Earlier rounds", first)
        self.assertIn("## The job's contract", first)
        self.assertIn("All cases pass", first)
        second = self.b(r, "review", "a", "--branch", "job", "--built-by", "claude", "--reason", "security")
        self.assertIn("This is review round 2", second)
        self.assertIn("same kind of problem everywhere", second)
        self.assertNotIn("caps reviews", second)
        self.assertIn("This is review round 3", self.b(r, "review", "a", "--branch", "job", "--built-by", "claude", "--reason", "security"))
        self.b(r, "review", "a", "--branch", "job", "--built-by", "claude", "--reason", "security")
        self.assertIn("4 review rounds, over the cap", self.b(r, "jobs", "a"))
        status = r / "nodes/a/status.md"
        text = status.read_text()
        status.write_text(text + "\n## Blocked / needs the operator\n\n- The staging key\n")
        self.assertIn("a: 1 Blocked entry doesn't say what clears it", self.b(r, "check"))
        status.write_text(text + "\n## Blocked / needs the operator\n\n- The staging key\n- The vendor's answer\n")
        out = self.b(r, "check")
        self.assertIn("a: 2 Blocked entries don't say what clears them", out)
        self.assertEqual(out.count("Blocked entr"), 1)  # one warning per node, with a count
        status.write_text(text + "\n## Blocked / needs the operator\n\n- The staging key — waiting on Sam; "
                                 "clears when the key is in the vault\n")
        self.assertNotIn("doesn't say what clears it", self.b(r, "check"))
        self.b(r, "wt", "rm", "a", "job", "--cause", "fixture done")
        self.assertEqual(list((r / "nodes/a/job-contracts").glob("*.md")), [])

    def test_workflow_review_round_one(self):
        from core_loader import SourceFileLoader
        r, home, wt = self.job()
        # Verify that changes what it verifies records nothing, and the next hand-back runs it again.
        runs = self.tmp / "runs.txt"
        self.b(r, "set", "a", f"verify=echo '# touched' >> calc.py && python3 -B verify.py && echo run >> {runs}")
        out = self.b(r, "handback", "--summary", "Synthetic result", "a", "--branch", "job", "--state", "done", "--reviews", "1", ok=False)
        self.assertIn("verify changed the job's branch, local main or worktree", out)
        self.assertIsNone(json.loads(_test_verify.WorkerReadyPackets.packet(self).read_text())["ready"])
        self.git("checkout", "--", "calc.py", cwd=wt)
        self.b(r, "set", "a", f"verify=python3 -B verify.py && echo run >> {runs}")
        self.assertNotIn("reused", self.b(r, "handback", "--summary", "Synthetic result", "a", "--branch", "job", "--state", "done", "--reviews", "1"))
        # --reverify forces a run, so it needs one to force.
        before = runs.read_bytes()
        self.assertIn("--reverify requires --state done --reviews N", self.b(r, "handback", "--summary", "Synthetic result", "a", "--branch", "job", "--state",
                                                                "done", "--reverify", ok=False))
        self.assertEqual(runs.read_bytes(), before)
        # A submodule change counts, whatever git's config hides.
        m = SourceFileLoader("brain_workflow_r1", str(CORE / "bin/brain")).load_module()
        m.machine_dir = lambda: self.tmp / "machine"
        repo = self.repo(self.tmp / "with-sub", {"README.md": "x\n"})
        self.git("config", "diff.ignoreSubmodules", "all", cwd=repo)
        base = self.git("rev-parse", "HEAD", cwd=repo).strip()
        self.git("update-index", "--add", "--cacheinfo", f"160000,{'1' * 40},dep", cwd=repo)
        self.git("commit", "-qm", "dep", cwd=repo)
        head = self.git("rev-parse", "HEAD", cwd=repo).strip()
        n = {"id": "a", "fm": {"verify_skip": "*.md"}, "home_path": repo}
        packet = {"version": 1, "node": "a", "branch": "job", "verify_command": "v", "verify_result": 0, "main": "m",
                  "head": base, "review_rounds": 1, "checked_at": "2026-01-01 10:00 +00:00", "inputs": "i"}
        port_context = m.verify_port_context(n, {})
        packet['verify_port_context'] = port_context
        self.assertEqual(m.verify_reuse(n, "job", packet, base, "m", "v", str(repo), "i", port_context), (base, []))  # control
        self.assertIsNone(m.verify_reuse(n, "job", packet, head, "m", "v", str(repo), "i", port_context))
        # The idle check runs after taking the instance lock: a lease acquired while waiting counts.
        self.b(r, "set", "root", "auto_upgrade=on")
        self.b(r, "wt", "rm", "a", "job", "--cause", "fixture done")  # idle, until the late job below
        inst = m.Instance(r)
        self.assertEqual(m.open_jobs_anywhere(inst), [])
        started = []

        class Lock:
            def __init__(lock, i): pass
            def __enter__(lock):
                self.b(r, "wt", "add", "a", "late")  # a job created while the tick waited for the lock
                with mock.patch.dict(os.environ, self.env):
                    lock.token = m.machine_wait("verify", m.Instance(r), "a")
                started.append(True)
            def __exit__(lock, *exc):
                m.machine_release(lock.token)
                return False
        calls, real_run = [], subprocess.run

        def run(args, *x, **k):  # everything runs, except an upgrade, which is recorded instead
            if "upgrade" in args:
                calls.append(args)
                return subprocess.CompletedProcess(args, 0, "", "")
            return real_run(args, *x, **k)
        with mock.patch.object(m, "core_update", return_value=("a" * 40, "b" * 40)), \
                mock.patch.object(m, "brain_lock", Lock), mock.patch.object(m.subprocess, "run", side_effect=run):
            self.assertIsNone(m.auto_upgrade(inst))
        self.assertEqual((started, calls), ([True], []))
        # The changelog never shows a node's never-name words.
        (r / "never-name.txt").write_text("Zebracorp\n")
        changes = r / "core/CHANGES.md"
        changes.write_text(changes.read_text().replace("Newest first.", "Newest first.\n\n## 2026-12-01: Zebracorp "
                                                       "notes\n\n- Ask Zebracorp first.\n", 1))
        self.git("commit", "-q", "--allow-empty", "-m", "Upgrade core abc1234 -> def5678", cwd=r)
        for args in (("changes",), ("changes", "--json"), ("changes", "--all"), ("due",)):
            self.assertNotIn("Zebracorp", self.b(r, *args))

    def test_workflow_review_round_two(self):
        from core_loader import SourceFileLoader
        r, home, wt = self.job()
        runs = self.tmp / "runs.txt"
        self.b(r, "set", "a", f"verify=python3 -B verify.py && echo run >> {runs}")
        count = lambda: len(runs.read_text().split()) if runs.exists() else 0
        (wt / ".gitignore").write_text(".env\n")
        self.git("add", ".gitignore", cwd=wt)
        self.git("commit", "-qm", "ignore env", cwd=wt)
        (wt / ".env").write_text("MODE=a\n")

        def finish(ok=True):
            return self.b(r, "handback", "--summary", "Synthetic result", "a", "--branch", "job", "--state", "done", "--reviews", "1", ok=ok)
        finish()
        self.assertIn("reused", finish())
        # Files verify reads that git ignores, and the ports it runs on, are part of the evidence.
        (wt / ".env").write_text("MODE=b\n")
        before = count()
        self.assertNotIn("reused", finish())
        self.assertEqual(count(), before + 1)
        # Readiness itself, wherever it is read, holds only for the inputs verify ran with.
        self.assertEqual(self.b(r, "ready", "a", "--branch", "job").strip(), "ready")
        (wt / ".env").write_text("MODE=broken\n")
        self.assertIn("changed since the last verify", self.b(r, "ready", "a", "--branch", "job", ok=False))
        self.assertNotIn("ready to merge", self.b(r, "due", "a"))
        ui = json.loads(self.b(r, "ui", "--json"))
        self.assertFalse(any(i.get("ready") for i in ui["due"]))
        (wt / ".env").write_text("MODE=b\n")
        self.assertEqual(self.b(r, "ready", "a", "--branch", "job").strip(), "ready")
        local = json.loads((r / ".brain/local.json").read_text())
        local["nodes"]["a"]["worktrees"]["job"]["port"] += 7
        (r / ".brain/local.json").write_text(json.dumps(local))
        self.assertIn("changed since the last verify", self.b(r, "ready", "a", "--branch", "job", ok=False))
        self.assertNotIn("reused", finish())
        # A verify that rewrites what it reads records nothing.
        self.b(r, "set", "a", f"verify=echo MODE=c > .env && python3 -B verify.py && echo run >> {runs}")
        self.assertIn("verify_inputs", finish(ok=False))
        self.b(r, "set", "a", f"verify=python3 -B verify.py && echo run >> {runs}")
        finish()
        # Untracked files count even when git's config hides them.
        self.git("config", "status.showUntrackedFiles", "no", cwd=wt)
        (wt / "stray.py").write_text("x = 1\n")
        self.assertIn("dirty", finish(ok=False))
        (wt / "stray.py").unlink()
        finish()
        # Only a complete packet whose verified commit is its own (or before it) can be reused.
        path = _test_verify.WorkerReadyPackets.packet(self)
        data = json.loads(path.read_text())
        for bad in ({"version": 7}, {"review_rounds": 0}, {"checked_at": 5}):
            path.write_text(json.dumps({**data, "ready": {**data["ready"], **bad}}))
            self.assertNotIn("reused", finish())
            data = json.loads(path.read_text())
        (wt / "calc.py").write_text("value = 3\n")  # broken
        self.git("commit", "-qam", "broken", cwd=wt)
        broken = self.git("rev-parse", "HEAD", cwd=wt).strip()
        path.write_text(json.dumps({**data, "ready": {**data["ready"], "verified_head": broken}}))
        self.assertIn("verify failed", finish(ok=False))
        self.assertIn("--reverify forces a verify run", self.b(r, "ready", "a", "--branch", "job", "--reverify",
                                                                ok=False))
        # Retained worktrees are idle; job inventory that can't be read still refuses.
        m = SourceFileLoader("brain_workflow_r2", str(CORE / "bin/brain")).load_module()
        m.machine_dir = lambda: self.tmp / "machine"
        self.assertEqual(m.open_jobs_anywhere(m.Instance(r)), [])
        ledger = r / "nodes/a/job-ledger.md"
        text = ledger.read_text()
        ledger.write_text(text.replace("<!-- brain:job-ledger v3 -->", "").replace("<!-- brain:job-ledger -->", ""))
        self.assertIn(("a", "unreadable job ledger"), m.open_jobs_anywhere(m.Instance(r)))
        ledger.write_text(text)

    def test_running_coordinators_hear_of_an_upgrade_at_their_next_stop(self):
        r = self.init("alice")
        self.b(r, "new", "a", "--title", "A")
        a = r / "nodes/a"
        sessions = iter(f"s{i}" for i in range(100))
        hook = lambda cwd, active=False, session=None: _test_hosts.StopHook.hook(self, r, cwd, session or next(sessions), active)
        reason = lambda out: (out or {}).get("reason", "")
        self.assertNotIn("core was upgraded", reason(hook(r)))
        self.git("commit", "-q", "--allow-empty", "-m", "Upgrade core abc1234 -> def5678", cwd=r)
        out = hook(r, session="tick")
        self.assertEqual(out["decision"], "block")
        self.assertIn("core was upgraded", out["reason"])
        self.assertIn(f"{r}/core/bin/brain changes root --seen", out["reason"])  # absolute: runs from any folder
        self.assertIn(f"{r}/core/roles/coordinator.md", out["reason"])
        self.assertIn("don't mark them read", out["reason"])  # only the coordinator acknowledges
        self.assertIsNone(hook(r, active=True))  # never twice in one stop
        self.assertNotIn("core was upgraded", reason(hook(r, session="tick")))  # spaced out per session
        self.assertNotIn("core was upgraded", reason(hook(a)))  # no coordinator runs it here
        self.b(r, "set", "a", "coordinator=thread-1")
        time.sleep(1.1)
        (a / "brief.md").write_text((a / "brief.md").read_text() + "\nAudience: teenagers\n")
        both = reason(hook(a))
        self.assertIn(f"{r}/core/bin/brain changes a", both)
        self.assertIn("Also:", both)  # the notes reminder still comes, in the same notice
        self.assertIn("brief.md", both)
        res = subprocess.run(["sh", "-c", f"{r}/core/bin/brain changes a --seen"], cwd=a, env=self.env,
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.b(r, "changes", "root", "--seen")
        self.assertNotIn("core was upgraded", reason(hook(r)))
        self.assertNotIn("core was upgraded", reason(hook(a)))
        log = (r / ".brain/hooks.log").read_text().splitlines()
        self.assertTrue(any(line.startswith(tuple("0123456789")) and " stop root tick: upgrade notice (" in line
                            for line in log), log)  # an upgrade-only hold says why, too



if __name__ == "__main__":
    unittest.main()
