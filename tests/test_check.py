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


class FixtureGitConfig(Scenario):
    def test_fixture_git_resolution_preserves_overrides_and_falls_back(self):
        import scenario
        native = self.tmp / 'developer/bin/git'
        native.parent.mkdir(parents=True)
        native.write_text('#!/bin/sh\nexit 0\n')
        native.chmod(0o700)
        path = '/fixture/bin:/usr/bin'
        with mock.patch.object(scenario.sys, 'platform', 'darwin'), \
                mock.patch.object(scenario.shutil, 'which', return_value='/usr/bin/git'), \
                mock.patch.object(scenario.subprocess, 'run', return_value=
                                  subprocess.CompletedProcess([], 0, str(native) + '\n', '')) as run:
            scenario.fixture_executable_path.cache_clear()
            self.addCleanup(scenario.fixture_executable_path.cache_clear)
            resolved = scenario.fixture_executable_path(path)
            directory, original = resolved.split(os.pathsep, 1)
            self.assertEqual(original, path)
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir()), ['git'])
            self.assertEqual((Path(directory) / 'git').resolve(), native)
            self.assertEqual(scenario.fixture_executable_path(path), resolved)
            run.assert_called_once_with(['/usr/bin/xcrun', '--find', 'git'], check=True,
                                        capture_output=True, text=True)
            for selected, platform in (('/custom/git', 'darwin'), ('/usr/bin/git', 'linux')):
                scenario.fixture_executable_path.cache_clear()
                with mock.patch.object(scenario.shutil, 'which', return_value=selected), \
                        mock.patch.object(scenario.sys, 'platform', platform):
                    self.assertEqual(scenario.fixture_executable_path(path), path)
            self.assertEqual(run.call_count, 1)
            for failure in (OSError('missing xcrun'), subprocess.CalledProcessError(1, ['xcrun'])):
                scenario.fixture_executable_path.cache_clear()
                run.side_effect = failure
                self.assertEqual(scenario.fixture_executable_path(path), path)
            run.side_effect = None
            for invalid in ('relative/git', str(self.tmp / 'missing/git')):
                scenario.fixture_executable_path.cache_clear()
                run.return_value.stdout = invalid
                self.assertEqual(scenario.fixture_executable_path(path), path)

    def test_fabricated_repos_disable_automatic_maintenance(self):
        home = self.repo(self.tmp / 'code')
        root = self.init('alice')
        settings = {'gc.auto': '0', 'maintenance.auto': 'false', 'gc.autoDetach': 'false'}
        for repo in (home, root):
            for key, value in settings.items():
                with self.subTest(repo=repo.name, key=key):
                    self.assertEqual(self.git('config', '--get', key, cwd=repo).strip(), value)
                    self.assertNotIn(key.lower() + '=', self.git('config', '--local', '--list', cwd=repo).lower())
        # A hook spawned by the CLI's Git subprocess inherits the same fixture-only settings.
        captured = self.tmp / 'git-settings'
        hook = root / '.git/hooks/post-commit'
        hook.write_text('#!/bin/sh\n' + '\n'.join(
            'git config --get ' + key + (' > ' if i == 0 else ' >> ') + shlex.quote(str(captured))
            for i, key in enumerate(settings)) + '\n')
        hook.chmod(0o700)
        self.b(root, 'new', 'app', '--title', 'App')
        self.assertEqual(captured.read_text().splitlines(), list(settings.values()))


class AutonomyAtomicWrites(AutonomyProject):
    def failed_replace(self, r, destination, args):
        before = destination.read_bytes()
        mode = destination.stat().st_mode & 0o777
        names = {p.name for p in destination.parent.iterdir()}
        head = self.git('rev-parse', 'HEAD', cwd=r)
        code = """
import os, runpy, sys
from pathlib import Path
target = Path(sys.argv[1])
brain = sys.argv[2]
sys.argv = sys.argv[2:]
original_replace = os.replace
def fail_replace(src, dst, **kwargs):
    from brain.history import history_fd_path
    source = Path(history_fd_path(kwargs['src_dir_fd'])) / src if 'src_dir_fd' in kwargs else Path(src)
    destination = Path(history_fd_path(kwargs['dst_dir_fd'])) / dst if 'dst_dir_fd' in kwargs else Path(dst)
    if destination == target:
        assert source.parent == target.parent
        assert source.read_bytes() != target.read_bytes()
        assert source.stat().st_size > 0
        raise OSError('injected replacement failure')
    return original_replace(src, dst, **kwargs)
os.replace = fail_replace
runpy.run_path(brain, run_name='__main__')
"""
        result = subprocess.run([sys.executable, '-c', code, str(destination), str(r / 'core/bin/brain'), *args],
                                cwd=r, env=self.env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('injected replacement failure', result.stderr)
        self.assertEqual(destination.read_bytes(), before)
        self.assertEqual(destination.stat().st_mode & 0o777, mode)
        # Recovery may create the durable comparison index before the injected write.
        durable = {'events-compare-index.json'} if destination.parent == r / '.brain' else set()
        after_names = {p.name for p in destination.parent.iterdir()}
        self.assertEqual(after_names - durable, names - durable, 'temporary file leaked')
        if 'events-compare-index.json' in after_names:
            json.loads((destination.parent / 'events-compare-index.json').read_text())
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=r), head)
        return result.stdout

    def test_status_and_handback_keep_old_complete_file_when_replace_fails(self):
        r, home, wt = self.project()
        status = r / 'nodes/app/status.md'
        status.chmod(0o640)
        (r / 'nodes/app/inbox.md').write_text('- 2035-01-01 00:00 +00:00 from worker: Pending atomic input.\n')
        self.failed_replace(r, status, ['status', 'app', '--refresh'])
        self.b(r, 'status', 'app', '--refresh')
        self.assertIn('<!-- /brain:facts -->', status.read_text())
        self.assertEqual(status.stat().st_mode & 0o777, 0o640)
        self.b(r, 'handback', "--summary", "Synthetic result", 'app', '--branch', 'job-a', '--state', 'blocked')
        signal = next((r / 'nodes/app/job-handbacks').glob('*.json'))
        self.failed_replace(r, signal, ['handback', '--summary', 'Synthetic result', 'app', '--branch', 'job-a', '--state', 'done'])
        self.assertEqual(json.loads(signal.read_text())['state'], 'blocked')
        self.b(r, 'handback', "--summary", "Synthetic result", 'app', '--branch', 'job-a', '--state', 'done')
        self.assertEqual(json.loads(signal.read_text())['state'], 'done')

    def test_delivery_before_failed_persistence_repeats_instead_of_losing_item(self):
        r = self.init('alice')
        self.b(r, 'inbox', 'root', '--new')
        saved = r / '.brain/inbox-seen.json'
        (r / 'inbox.md').write_text('- 2026-10-19 10:00 +00:00 from worker: Question after output.\n')
        output = self.failed_replace(r, saved, ['inbox', 'root', '--new', '--json'])
        delivered = json.loads(output)
        repeated = json.loads(self.b(r, 'inbox', 'root', '--new', '--json'))
        self.assertEqual(delivered, repeated)
        self.assertEqual(json.loads(self.b(r, 'inbox', 'root', '--new', '--json')), [])



class RecoveryCrashes(AutonomyProject):
    # Instrument only the child process: no crash switches in the installed CLI.
    def paused_write(self, root, destination, args, phase, reading=None):
        marker = self.tmp / 'replace-paused'
        candidate = self.tmp / 'replace-candidate'
        marker.unlink(missing_ok=True)
        code = """
import core_loader, importlib.util, json, os, sys, time
from pathlib import Path
target, marker, candidate, phase, source, reading = sys.argv[1:7]
sys.argv = [source, *sys.argv[7:]]
loader = core_loader.SourceFileLoader('recovery_brain', source)
spec = importlib.util.spec_from_loader(loader.name, loader)
m = importlib.util.module_from_spec(spec)
loader.exec_module(m)
if reading:
    m.usage_reading = lambda sub: ('fixture', json.loads(reading))
original = os.replace
paused = False
def replace(src, dst, **kwargs):
    global paused
    source_path = Path(m.history_fd_path(kwargs['src_dir_fd'])) / src if 'src_dir_fd' in kwargs else Path(src)
    destination = Path(m.history_fd_path(kwargs['dst_dir_fd'])) / dst if 'dst_dir_fd' in kwargs else Path(dst)
    if destination != Path(target) or paused:
        return original(src, dst, **kwargs)
    paused = True
    Path(candidate).write_bytes(source_path.read_bytes())
    if phase == 'after':
        original(src, dst, **kwargs)
    Path(marker).write_text('paused')
    while Path(marker).exists():
        time.sleep(0.01)
    if phase == 'before':
        return original(src, dst, **kwargs)
os.replace = replace
m.main()
"""
        source = str(root / 'core/bin/brain')
        proc = subprocess.Popen([sys.executable, '-c', code, str(destination), str(marker), str(candidate),
                                 phase, source, json.dumps(reading) if reading else '', *args],
                                cwd=root, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists(), 'command never reached atomic replace')
            return proc, candidate.read_bytes()
        except BaseException:
            proc.kill(); proc.communicate(timeout=5)
            raise

    def kill_write(self, root, destination, args, phase):
        before = destination.read_bytes()
        mode = destination.stat().st_mode & 0o777
        head = self.git('rev-parse', 'HEAD', cwd=root)
        proc, after = self.paused_write(root, destination, args, phase)
        proc.kill()
        proc.communicate(timeout=5)
        self.assertEqual(proc.returncode, -9)
        self.assertNotEqual(before, after, 'fixture must change the destination')
        self.assertEqual(destination.read_bytes(), before if phase == 'before' else after)
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=root), head)
        # This command takes the instance lock and must not wedge on the dead writer's pid.
        follow = subprocess.run([sys.executable, str(root / 'core/bin/brain'), *args], cwd=root,
                                env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(follow.returncode, 0, follow.stderr)
        self.assertEqual(destination.stat().st_mode & 0o777, mode)

    def test_recover_sigkill_status_handback_inbox_and_jobs(self):
        def vanished_entry(function, path, exc_info):
            if not isinstance(exc_info[1], FileNotFoundError):
                raise exc_info[1]

        for phase in ('before', 'after'):
            for command in ('status', 'handback', 'inbox', 'jobs'):
                with self.subTest(phase=phase, command=command):
                    # Every subcase gets a fresh home, even if a preceding assertion failed.
                    shutil.rmtree(self.tmp / 'alice', ignore_errors=True)
                    shutil.rmtree(self.tmp / 'code', ignore_errors=True)
                    r, home, wt = self.project()
                    node = r / 'nodes/app'
                    if command == 'status':
                        (node / 'inbox.md').write_text('- 2035-01-01 00:00 +00:00 from worker: Pending crash input.\n')
                        dest, args = node / 'status.md', ['status', 'app', '--refresh']
                    elif command == 'handback':
                        self.b(r, 'handback', "--summary", "Synthetic result", 'app', '--branch', 'job-a', '--state', 'blocked')
                        dest = next((node / 'job-handbacks').glob('*.json'))
                        args = ['handback', '--summary', 'Synthetic result', '--question', 'Synthetic choice?', 'app', '--branch', 'job-a', '--state', 'question']
                    elif command == 'inbox':
                        self.b(r, 'inbox', 'app', '--new')
                        (node / 'inbox.md').write_text('- 2026-10-19 10:00 +00:00 from worker: Recover me.\n')
                        dest, args = r / '.brain/inbox-seen.json', ['inbox', 'app', '--new', '--json']
                    else:
                        dest = node / 'job-ledger.md'
                        args = ['jobs', 'app', '--branch', 'job-a', '--switch', 'claude -> codex: usage limit']
                    self.kill_write(r, dest, args, phase)
                    if dest.suffix == '.json':
                        json.loads(dest.read_text())
                    if command == 'inbox':
                        self.assertEqual(json.loads(self.b(r, *args)), [])
                    # Reuse the helper's fixed instance name on the next case.
                    shutil.rmtree(r, onerror=vanished_entry)
                    shutil.rmtree(home.parent)

    def test_recover_sigkill_usage_poller_restarts_and_keeps_last_reading(self):
        r = self.init('alice')
        self.env['BRAIN_CONFIG'] = str(self.tmp / 'config/brains.json')
        cfg = self.tmp / 'config'
        cfg.mkdir()
        sub = {'id': 'test', 'label': 'test', 'runtime': 'claude', 'config_dir': str(self.tmp / 'no-login'),
               'host_provider_instance_id': 'fixture-provider', 'brains': ['alice']}
        (cfg / 'subscriptions.json').write_text(json.dumps({'subscriptions': [sub]}))
        observed = time.time() - 900
        old = {'source': 'lookup', 'read_at': observed, 'note': '',
               'windows': {'five_hour': {'used_percent': 42, 'resets_at': time.time() + 3600}}}
        cache = cfg / 'usage.json'
        cache.write_text(json.dumps({'subscriptions': [{**sub, 'account_key': 'fixture'}],
                                     'accounts': {'fixture': old}, 'checked_at': observed}))
        before = cache.read_bytes()
        newer = {**old, 'read_at': time.time(), 'windows': {
            'five_hour': {'used_percent': 47, 'resets_at': time.time() + 3600}}}
        args = ['usage', '--watch', '--every', '0.1']
        for attempt in range(2):
            proc, candidate = self.paused_write(r, cache, args, 'before', newer)
            try:
                # Both the watch and refresh locks are held while readers use the old snapshot.
                row = json.loads(self.b(r, 'usage', '--json'))['subscriptions'][0]
                self.assertEqual(row['windows']['five_hour']['used_percent'], 42)
                self.assertGreaterEqual(row['age_seconds'], 900)
                self.assertEqual(row['read_at'], observed)
                self.assertIn('15 m', self.b(r, 'usage'))
                self.assertEqual(cache.read_bytes(), before)
            finally:
                proc.kill(); proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, -9)
            # With no poller alive, ordinary readers still retain the observation and its age.
            dead_row = json.loads(self.b(r, 'usage', '--json'))['subscriptions'][0]
            self.assertEqual(dead_row['read_at'], observed)
            self.assertGreaterEqual(dead_row['age_seconds'], 900)
        # A third watch process acquires both stale-pid locks and publishes a full new cache.
        proc, candidate = self.paused_write(r, cache, args, 'before', newer)
        try:
            (self.tmp / 'replace-paused').unlink()
            deadline = time.monotonic() + 5
            while cache.read_bytes() == before and time.monotonic() < deadline:
                time.sleep(0.01)
            result = json.loads(cache.read_text())
            self.assertEqual(result['accounts']['fixture']['windows']['five_hour']['used_percent'], 47)
            self.assertEqual(result['accounts']['fixture']['read_at'], newer['read_at'])
        finally:
            proc.kill(); proc.communicate(timeout=5)
        self.assertEqual(proc.returncode, -9)



class RoundTwo(AutonomyProject):
    def setUp(self):
        super().setUp()
        self.patch_env = mock.patch.dict(os.environ, self.env, clear=True)
        self.patch_env.start()
        self.addCleanup(self.patch_env.stop)
        loader = core_loader.SourceFileLoader("brain_r2_test", str(CORE / "bin/brain"))
        self.m = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(self.m)

    def row(self, branch, hours="1", ended="2026-01-01T01:00:00+00:00", **extra):
        return {**dict.fromkeys(self.m.JOB_COLS, ""), "Job": branch, "Started": "2026-01-01T00:00:00+00:00",
                "Ended": ended, "Hours": hours, "Outcome": "merged" if ended else "", "Reviews": "1", **extra}

    def learn(self, root, key, title="Service configuration", **kw):
        args = ["item", "add", key, "--from", kw.pop("source", "app"), "--title", title,
                "--kind", "lesson", "--ref", "report@commit", "--body", kw.pop("body", "Validate service settings.")]
        self.b(root, *args, *kw.pop("args", []))

    def test_lessons_close_to_next_brief_round_trip(self):
        r, home, wt = self.project()
        self.b(r, "handback", "--summary", "Synthetic result", "app", "--branch", "job-a", "--state", "done", "--lessons", "Validate service settings.")
        self.learn(r, "service-settings")
        self.b(r, "jobs", "app", "--branch", "job-a", "--lessons", "service-settings")
        self.git("commit", "--allow-empty", "-qm", "first job", cwd=wt)
        self.git("merge", "-q", "job-a", cwd=home)
        self.b(r, "wt", "prune", "app", "--offline")
        self.b(r, "wt", "add", "app", "feat/next")
        brief = self.b(r, "brief", "app", "--branch", "feat/next", "--task", "Update service settings")
        self.assertIn("`service-settings`: Service configuration", brief)
        self.assertIn("picked for task: service, settings; same node", brief)
        self.assertIn("brain item show service-settings --node app", brief)
        self.assertIn("Validate service settings.", self.b(r, "item", "show", "service-settings", "--node", "app"))

    def test_library_ranking_cap_and_all_eligibility_states(self):
        r, home, wt = self.project()
        for i in range(7):
            self.learn(r, f"service-{i}")
        self.learn(r, "denied", args=["--proposed", "--repo", "example/repo", "--commit", "a" * 40])
        self.learn(r, "rejected", args=["--proposed", "--repo", "example/repo", "--commit", "a" * 40])
        self.b(r, "item", "reject", "rejected")
        self.learn(r, "quarantined")
        self.b(r, "item", "quarantine", "quarantined")
        self.b(r, "principal", "add", "bob")
        self.b(r, "new", "private", "--title", "Private", "--owners", "bob", "--home", str(self.tmp / "private-home"))
        self.learn(r, "private-service", source="private")
        args = ["brief", "app", "--task", "service settings"]
        brief = self.b(r, *args)
        self.assertEqual(brief, self.b(r, *args))
        picks = [line for line in brief.splitlines() if "Read full:" in line]
        self.assertEqual(len(picks), 5)
        self.assertIn("`service-0`", picks[0])
        self.assertIn("`service-4`", picks[-1])
        for key in ("denied", "rejected", "quarantined", "private-service"):
            self.assertNotIn(f"`{key}`", brief)
            self.b(r, "item", "show", key, "--node", "app", ok=False)

    def test_labels_stack_specialist_and_task_weights_and_review(self):
        r, home, wt = self.project()
        self.learn(r, "task-first", title="SQL transaction", body="SQL")
        self.learn(r, "stack-match", title="Python", body="Python")
        self.b(r, "set", "app", "stack=Python")
        self.b(r, "relabel", "app", "area:database")
        self.b(r, "specialist", "new", "database", "--title", "DB", "--runtime", "codex")
        self.learn(r, "specialist-match", title="Database", body="Database")
        brief = self.b(r, "hire", "database", "app", "--task", "SQL")
        self.assertIn("labels/stack: python", brief)
        self.assertIn("specialist: database", brief)
        self.assertLess(brief.index("`task-first`"), brief.index("`stack-match`"))
        review = self.b(r, "review", "app", "--branch", "job-a", "--built-by", "claude", "--focus", "SQL")
        self.assertIn("`task-first`", review)
        self.assertIn("picked for task: sql", review)

    def test_small_brief_has_limits_guard_no_ceremony_or_state_changes(self):
        r, home, wt = self.project()
        n = r / "nodes/app"
        (n / "brief.md").write_text("## Limits\n- Only local drafts. Why: rehearsal. Lift when: never.\n")
        full = self.b(r, "brief", "app", "--branch", "job-a", "--task", "Prepare a local draft")
        # Full briefs record job routing; isolate the small brief's read-only guarantee.
        before = {p: p.read_bytes() for p in [r / ".brain/local.json", n / "job-ledger.md"]}
        worktrees = self.git("worktree", "list", "--porcelain", cwd=home)
        short = self.b(r, "brief", "app", "--small", "--task", "Prepare a local draft")
        self.assertTrue(all(p.read_bytes() == data for p, data in before.items()))
        self.assertLess(len(short.splitlines()), len(full.splitlines()) * .6)
        self.assertIn("Only local drafts", short)
        self.assertIn("brain guard app", short)
        self.assertNotIn(str(self.tmp), short)
        self.assertIn("brain guard app ~/code/app", short)
        self.assertIn("host `send` to whoever started you", short)
        self.assertIn("Do not commit to main unless the task explicitly says so", short)
        self.b(r, "set", "app", "coordinator=coordinator-thread")
        routed = self.b(r, "brief", "app", "--small", "--to", "override-thread")
        self.assertIn("host `send` to thread `override-thread`", routed)
        self.assertNotIn("coordinator-thread", routed)
        self.b(r, "set", "app", "coordinator=")
        self.assertIn("one line", short)
        self.assertNotIn("brain handback", short)
        self.assertEqual((n / "job-ledger.md").read_bytes(), before[n / "job-ledger.md"])
        self.assertEqual(worktrees, self.git("worktree", "list", "--porcelain", cwd=home))
        self.b(r, "brief", "app", "--small", "--branch", "job-a", ok=False)
        self.b(r, "brief", "app", "--small", "--import", ok=False)
        (n / "never-name.txt").write_text("ForbiddenBrand\n")
        bad = self.b(r, "brief", "app", "--small", "--task", "Use ForbiddenBrand", ok=False)
        self.assertNotIn("ForbiddenBrand", bad)

    def test_legacy_packet_freshness_and_branch_cleanup(self):
        r, home, wt = self.project()
        inst = self.m.Instance(r); n = inst.node("app")
        row = self.m.open_job(self.m.read_jobs(n), "job-a")
        row["Started"] = "2026-10-19T10:00:00.900001+00:00"
        self.m.write_jobs(inst, n, [row])
        f = self.m.ready_path(inst, n, "job-a"); f.parent.mkdir(parents=True)
        for checked in ("2020-01-01T00:00:00Z", "2026-10-19T10:00:00.1+00:00", "bad", None):
            f.write_text(json.dumps({"checked_at": checked}))
            self.assertNotIn("handback", [i["kind"] for i in self.due(r)])
            self.b(r, "handback", "--summary", "Synthetic result", "app", "--branch", "job-a", "--state", "done")
            record = self.m.handback_file(n, row)
            self.assertIsNone(json.loads(record.read_text())["ready"])
            record.unlink()
            # Restore the legacy state, including absence of the synthetic attempt.
            self.m.attempt_path(n, row).unlink()
        f.write_text(json.dumps({"checked_at": row["Started"]}))
        self.assertIn("handback", [i["kind"] for i in self.due(r)])
        self.b(r, "handback", "--summary", "Synthetic result", "app", "--branch", "job-a", "--state", "blocked")
        self.b(r, "wt", "rm", "app", "job-a", "--cause", "restart")
        self.assertFalse(f.exists())
        self.assertFalse(self.m.handback_file(n, row).exists())
        self.assertEqual(self.git("status", "--porcelain", "--", "nodes/app/job-handbacks", cwd=r), "")
        self.assertIn("restart", (n["dir"] / "job-ledger.md").read_text())
        self.b(r, "wt", "add", "app", "job-a")
        self.assertNotIn("handback", [i["kind"] for i in self.due(r)])

    def test_prune_cleans_both_records(self):
        r, home, wt = self.project()
        inst = self.m.Instance(r); n = inst.node("app")
        f = self.m.ready_path(inst, n, "job-a"); f.parent.mkdir(parents=True); f.write_text("{}")
        self.b(r, "handback", "--summary", "Synthetic result", "app", "--branch", "job-a", "--state", "done")
        self.b(r, "jobs", "app", "--branch", "job-a", "--lessons", "none: routine cleanup")
        self.git("commit", "--allow-empty", "-qm", "work", cwd=wt)
        self.git("merge", "-q", "job-a", cwd=home)
        self.b(r, "wt", "prune", "app", "--offline", "--dry-run")
        self.assertTrue(f.exists())
        self.b(r, "wt", "prune", "app", "--offline")
        self.assertFalse(f.exists())
        self.assertEqual(list((n["dir"] / "job-handbacks").glob("*.json")), [])

    def test_merged_cleanup_retains_proposals_until_lesson_outcome(self):
        for command in (("prune", "--offline"), ("rm", "job-a")):
            with self.subTest(command=command):
                r, home, wt = self.project()
                inst = self.m.Instance(r); n = inst.node("app")
                row = self.m.open_job(self.m.read_jobs(n), "job-a")
                record = self.m.handback_file(n, row)
                self.b(r, "handback", "--summary", "Synthetic result", "app", "--branch", "job-a", "--state", "done",
                       "--lessons", "Preserve migration ordering", "--report", "reports/ordering/index.md")
                original = record.read_bytes()
                self.git("commit", "--allow-empty", "-qm", "work", cwd=wt)
                self.git("merge", "-q", "job-a", cwd=home)
                due = next(i for i in self.due(r) if i["kind"] == "merged-job")
                self.assertIn("brain jobs app --branch job-a --lessons first", due["text"])
                for output in (due["text"], self.b(r, "wt", command[0], "app", *command[1:])):
                    self.assertIn("Preserve migration ordering", output)
                    self.assertIn("reports/ordering/index.md", output)
                self.assertEqual(record.read_bytes(), original)
                self.assertIn("Preserve migration ordering", next(i for i in self.due(r) if i["kind"] == "lesson-outcome")["text"])
                self.b(r, "jobs", "app", "--branch", "job-a", "--lessons", "none: specific to this job")
                self.assertFalse(record.exists())
                self.assertNotIn("lesson-outcome", [i["kind"] for i in self.due(r)])
                # project() uses the same paths; keep each subcase independent.
                shutil.rmtree(r)
                shutil.rmtree(home)
                shutil.rmtree(home.parent / (home.name + "-work"))

    def test_import_closes_without_machine_home_or_workbench(self):
        r = self.init("alice")
        self.b(r, "new", "app", "--title", "App", "--home", "external")
        inst = self.m.Instance(r); n = inst.node("app")
        branch = "import:" + "a" * 40
        self.m.job_started(inst, n, branch, "codex")
        self.b(r, "handback", "--summary", "Synthetic result", "app", "--branch", branch, "--state", "done", "--lessons", "none: no reusable lesson")
        self.b(r, "jobs", "app", "--branch", branch, "--lessons", "none: no reusable lesson", "--close")
        row = self.m.read_jobs(n)[0]
        self.assertEqual(row["Outcome"], "studied")
        self.assertTrue(row["Ended"])
        self.assertEqual(row["Lessons"], "none: no reusable lesson")
        self.assertFalse(self.m.handback_file(n, row).exists())

    def test_questions_and_blocked_human_needs_reach_inbox_once(self):
        r, home, wt = self.project()
        path = r / "nodes/app/inbox.md"
        self.b(r, "handback", "--summary", "Synthetic result", "app", "--branch", "job-a", "--state", "blocked", "--report", "report.md")
        self.assertNotIn("worker job-a", path.read_text())
        for _ in range(2):
            self.b(r, "handback", "--summary", "Synthetic result", "--question", "Choose the port", "app", "--branch", "job-a", "--state", "question", "--need-human", "Choose the port")
        self.assertEqual(path.read_text().count("Choose the port"), 1)
        items = json.loads(self.b(r, "inbox", "root", "--json"))
        self.assertTrue(any("Choose the port" in item["text"] for item in items))
        for _ in range(2):
            self.b(r, "handback", "--summary", "Synthetic result", "app", "--branch", "job-a", "--state", "blocked", "--need-human", "Grant access")
        self.assertEqual(path.read_text().count("Grant access"), 1)
        self.b(r, "handback", "--summary", "Synthetic result", "--question", "See report report.md", "app", "--branch", "job-a", "--state", "question", "--report", "report.md")
        self.assertIn("See report report.md", path.read_text())

    def test_facts_header_migration_caps_times_forecasts_preview(self):
        r, home, wt = self.project()
        inst = self.m.Instance(r); n = inst.node("app")
        rows = [self.row(f"feat/{i}", ended=f"2026-01-{i+1:02}T01:00:00+00:00") for i in range(12)]
        self.m.write_jobs(inst, n, rows)
        status = n["dir"] / "status.md"
        original = "# App\n\nUpdated: 2020-01-01 00:00 +00:00\n\n## Now\n- Keep judgement.\n"
        status.write_text("<!-- brain:facts v1 -->\nOld facts\n<!-- /brain:facts -->\n\n" + original)
        self.b(r, "status", "app", "--refresh")
        text = status.read_text()
        self.assertTrue(text.startswith(original.split("## Now")[0]))
        self.assertEqual(text.count(self.m.FACTS_START), 1)
        block = self.m.FACTS_RE.search(text).group()
        self.assertIn("- +4 more", block)
        self.assertLess(block.index("feat/11"), block.index("feat/10"))
        self.assertNotIn("T01:", block)
        self.assertRegex(block, r"Facts refreshed: \d{4}-\d{2}-\d{2} \d{2}:\d{2} \+00:00\n")
        self.assertIn("12 similar jobs, median 1 h", block)
        self.assertIn("Preview: missing", block)
        self.assertIn("missing-preview", [i["kind"] for i in self.due(r)])
        self.b(r, "set", "app", "preview=http://localhost:3000", "preview_tour=reports/tour/index.md")
        self.assertNotIn("missing-preview", [i["kind"] for i in self.due(r)])
        self.b(r, "status", "app", "--refresh")
        self.assertNotIn("http://localhost:3000", status.read_text())
        self.assertNotIn("reports/tour/index.md", status.read_text())
        self.assertIn("## Now\n- Keep judgement.", status.read_text())

    def test_preview_bindings_stay_local_and_none_retains_reminder(self):
        r, home, wt = self.project()
        inst = self.m.Instance(r); n = inst.node("app")
        self.m.write_jobs(inst, n, [self.row("job/preview-one")])
        self.assertIn("missing-preview", [i["kind"] for i in self.due(r)])
        self.b(r, "set", "app", "preview=http://localhost:3456", "preview_tour=~/local-tour/index.md")
        local = json.loads((r / ".brain/local.json").read_text())["nodes"]["app"]
        self.assertEqual(local["preview"], "http://localhost:3456")
        self.assertEqual(local["preview_tour"], "~/local-tour/index.md")
        self.b(r, "status", "app", "--refresh")
        tracked = self.git("show", "HEAD:nodes/app/status.md", cwd=r)
        for path in (n["dir"] / "node.md", n["dir"] / "status.md", r / "registry.md"):
            self.assertNotIn("localhost:3456", path.read_text())
            self.assertNotIn("local-tour", path.read_text())
        self.assertNotIn("localhost:3456", tracked)
        ui = next(n for n in json.loads(self.b(r, "ui", "--json"))["nodes"] if n["id"] == "app")
        self.assertEqual(ui["preview"], "http://localhost:3456")
        self.assertEqual(ui["preview_tour"], "~/local-tour/index.md")
        self.assertNotIn("local-only key", self.b(r, "check"))
        self.b(r, "set", "app", "preview=none")
        self.assertIn("missing-preview", [i["kind"] for i in self.due(r)])
        self.assertEqual(json.loads((r / ".brain/local.json").read_text())["nodes"]["app"]["preview"], "none")
        self.b(r, "set", "app", "preview=")
        self.assertIn("missing-preview", [i["kind"] for i in self.due(r)])
        # Legacy committed values are flagged and are never used as local bindings.
        fm, body = self.m.read_fm(n["dir"] / "node.md")
        fm.update(preview="http://localhost:9876", preview_tour="~/legacy-tour")
        self.m.write_fm(n["dir"] / "node.md", fm, body)
        check = self.b(r, "check", ok=False)
        self.assertIn("preview", check)
        self.assertIn("preview_tour", check)
        self.assertIn("missing-preview", [i["kind"] for i in self.due(r)])
        fm["preview"] = "none"
        self.m.write_fm(n["dir"] / "node.md", fm, body)
        self.b(r, "check", "--fix")
        migrated = json.loads((r / ".brain/local.json").read_text())["nodes"]["app"]
        self.assertEqual(migrated["preview"], "none")
        self.assertEqual(migrated["preview_tour"], "~/legacy-tour")
        self.assertIn("missing-preview", [i["kind"] for i in self.due(r)])
        self.b(r, "set", "app", "preview=none", "preview_tour=", "preview_url=https://demo.example.org")
        self.b(r, "status", "app", "--refresh")
        self.assertIn("Published preview: https://demo.example.org", (n["dir"] / "status.md").read_text())
        self.assertNotIn("preview", self.m.read_fm(n["dir"] / "node.md")[0])

    def test_unsafe_library_candidates_are_silently_skipped(self):
        r, home, wt = self.project()
        self.learn(r, "safe", title="Service guidance")
        self.learn(r, "unsafe-title", title="Service HiddenBrand guidance")
        self.learn(r, "unsafe-body", body="Service work for HiddenBrand")
        self.learn(r, "unsafe-metadata", args=["--ref", "HiddenBrand@commit"])
        n = r / "nodes/app"
        (n / "never-name.txt").write_text("hiddenbrand\n")
        self.b(r, "specialist", "new", "service", "--title", "Service", "--runtime", "codex")
        for args in (("brief", "app", "--task", "Service"),
                     ("brief", "app", "--small", "--task", "Service"),
                     ("hire", "service", "app", "--task", "Service"),
                     ("review", "app", "--branch", "job-a", "--built-by", "claude", "--focus", "Service")):
            text = self.b(r, *args)
            self.assertIn("`safe`", text)
            self.assertNotIn("unsafe-", text)
            self.assertNotIn("hiddenbrand", text.casefold())
        self.assertNotIn("HiddenBrand", self.b(r, "item", "show", "unsafe-title", "--node", "app", ok=False))

    def test_forecasts_only_merged_valid_samples_and_honest_sparse_kinds(self):
        rows = [self.row("feat/a", "1"), self.row("feat/b", "2"), self.row("feat/c", "9"),
                self.row("feat/bad", "nan"), self.row("feat/open", "100", ended=""),
                self.row("feat/dropped", "80", Outcome="abandoned"), self.row("fix/a", "5")]
        fs = {f["kind"]: f for f in self.m.job_forecasts(rows)}
        self.assertEqual(fs["feat"]["samples"], 3)
        self.assertEqual((fs["feat"]["median_h"], fs["feat"]["min_h"], fs["feat"]["max_h"]), (2, 1, 9))
        lines = "\n".join(self.m.forecast_lines(rows, "docs/planned"))
        self.assertIn("fix: 1 similar jobs, too little data", lines)
        self.assertIn("docs: 0 similar jobs, too little data", lines)
        self.assertEqual(self.m.job_kind("bare"), "unclassified")
        self.assertEqual(self.m.job_kind("import:abc"), "import")
        r = self.init("alice")
        self.assertIn("feat: 0 similar jobs, too little data", self.b(r, "jobs", "root", "--branch", "feat/planned"))

    def test_generic_job_branches_use_repeated_slug_families_or_explicit_all_jobs(self):
        rows = [self.row("job/m7-read", "1"), self.row("job/m7-write", "2"), self.row("job/m7-review", "9"),
                self.row("job/ui-overview", "4"), self.row("job/ui-inbox", "6"), self.row("job/oneoff", "8"),
                self.row("job/47-draft", "2"), self.row("job/47-other", "2")]
        fs = {f["kind"]: f for f in self.m.job_forecasts(rows)}
        self.assertNotIn("job", fs)
        self.assertEqual((fs["m7"]["samples"], fs["m7"]["median_h"]), (3, 2))
        self.assertEqual(fs["ui"]["samples"], 2)
        lines = "\n".join(self.m.forecast_lines(rows, "job/preview-first"))
        self.assertIn("m7: 3 similar jobs, median 2 h, spread 1–9 h", lines)
        self.assertIn("ui: 2 similar jobs, too little data", lines)
        self.assertIn("all jobs on this node: 8 jobs", lines)
        self.assertNotIn("8 similar jobs", lines)
        self.assertEqual(self.m.job_kind("job/preview-first", rows), "unclassified")
        self.assertEqual(self.m.job_kind("job/47-next", rows), "unclassified")
        r = self.init("alice")
        self.b(r, "new", "other", "--title", "Other")
        self.assertEqual(self.b(r, "jobs", "--branch", "job/anything", ok=False).strip(),
                         "brain: --branch needs a node: brain jobs <node> --branch <branch>")
        self.assertNotIn("other:", self.b(r, "jobs", "root", "--branch", "job/anything"))

    def test_new_ui_forecasts_and_preview_fields_are_additive_and_redacted(self):
        r, home, wt = self.project()
        inst = self.m.Instance(r); n = inst.node("app")
        self.m.write_jobs(inst, n, [self.row(f"OldCo/{i}") for i in range(3)])
        (n["dir"] / "never-name.txt").write_text("OldCo\n")
        self.b(r, "set", "app", "preview=http://OldCo.local", "preview_tour=reports/OldCo/tour.md")
        output = self.b(r, "ui", "--json")
        node = next(node for node in json.loads(output)["nodes"] if node["id"] == "app")
        self.assertNotIn("oldco", json.dumps(node).lower())
        self.assertIn("forecasts", node["jobs"])
        self.assertIn("rows", node["jobs"])
        self.assertIn("preview", node)
        self.assertIn("preview_tour", node)

    def test_inbox_prefixes_are_unique_full_ids_survive_json_and_state(self):
        keys = ["abcdef0" + "a" * 17, "abcdef1" + "b" * 17, "123456" + "c" * 18]
        short = self.m.shortest_ids(keys)
        self.assertEqual([short[k] for k in keys], ["abcdef0", "abcdef1", "123456"])
        r = self.init("alice")
        (r / "inbox.md").write_text("- Need a choice\n- Need approval\n")
        items = json.loads(self.b(r, "inbox", "--json"))
        out = self.b(r, "inbox", "--new")
        for item in items:
            self.assertEqual(len(item["id"]), 24)
            self.assertIn("[" + item["id"][:6] + "]", out)
        state = json.loads((r / ".brain/inbox-seen.json").read_text())
        self.assertTrue(all(i["id"] in state["reporters"]["root"] for i in items))

    def test_jobs_writes_validate_switches_and_print_human_result(self):
        r, home, wt = self.project()
        ledger = r / "nodes/app/job-ledger.md"
        before = ledger.read_bytes()
        for switch in ("codex", "old -> new", "old -> new: ", "-> new: reason", "old -> new: reason\nextra", "old\n-> new: reason"):
            self.assertIn("--switch needs old -> new: reason", self.b(r, "jobs", "app", "--branch", "job-a", "--switch", switch, ok=False))
            self.assertEqual(ledger.read_bytes(), before)
        out = self.b(r, "jobs", "app", "--branch", "job-a", "--switch", "old -> new: usage limit", "--lessons", "none: local only")
        self.assertEqual(out.strip(), "app: updated job job-a")
        self.assertNotIn("{", out)



if __name__ == "__main__":
    unittest.main()


class DecisionApplicability(Scenario):
    def test_ordinary_check_includes_report_warning_without_changing_business_files(self):
        core = core_loader.SourceFileLoader('report_check', str(CORE / 'bin/brain')).load_module()
        reports = core._owners['report_size_warnings']
        root = self.init('demo')
        folder = root / 'reports/example'
        folder.mkdir(parents=True)
        note = folder / 'index.md'
        note.write_text('fabricated report')
        before = note.read_bytes()
        with mock.patch.object(reports, 'REPORT_WARNING_BYTES', 8):
            problems, warnings, fixable = core.collect_problems(core.Instance(root))
        self.assertTrue(any('report folder exceeds 1 GiB' in warning for warning in warnings))
        self.assertEqual(before, note.read_bytes())
        self.assertNotIn('reports', fixable)

    def test_dated_decisions_require_scope_and_external_claim_sources(self):
        r = self.init('alice')
        decisions = r / 'decisions.md'
        decisions.write_text('# Decisions\n\n## 2026-05-01 10:00 +00:00: Route policy\nExternal fact: Vendor sessions expire after one hour.\nWhy: Integration contract.\n')
        out = self.b(r, 'check')
        self.assertIn('decision has no Applies to:', out)
        self.assertIn('external fact has no valid Source:', out)
        decisions.write_text(decisions.read_text() + 'Applies to: app/auth\nSource: https://vendor.example/policy\n')
        out = self.b(r, 'check')
        self.assertNotIn('decision has no Applies to:', out)
        self.assertNotIn('external fact has no valid Source:', out)

    def test_sources_are_explicit_and_legacy_cutoff_is_configurable(self):
        r = self.init('alice')
        self.b(r, 'set', 'root', 'decision_scope_since=2026-05-01T00:00:00+00:00')
        path = r / 'decisions.md'
        path.write_text('# Decisions\n\n## 2026-04-01 10:00 +00:00: Legacy\nWhy: Earlier choice.\n\n## 2026-05-02 10:00 +00:00: Provider rate limit\nApplies to: \nVendor fact: Provider allows ten requests.\nSource: someone said so\n')
        out = self.b(r, 'check')
        self.assertEqual(out.count('decision has no Applies to:'), 1)
        self.assertIn('external fact has no valid Source:', out)
        path.write_text(path.read_text().replace('Applies to: \n', 'Applies to: app\n').replace('someone said so', 'probe: curl --head https://vendor.example/policy'))
        out = self.b(r, 'check')
        self.assertNotIn('decision has no Applies to:', out)
        self.assertNotIn('external fact has no valid Source:', out)


    def test_malformed_external_source_is_a_warning_not_a_check_crash(self):
        r = self.init('alice')
        (r / 'decisions.md').write_text('## 2026-05-02 10:00 +00:00: Policy\nApplies to: app\nExternal fact: Provider requires consent.\nSource: http://[invalid\n')
        self.assertIn('external fact has no valid Source:', self.b(r, 'check'))


class ReportFolderWarnings(unittest.TestCase):
    def setUp(self):
        core = core_loader.SourceFileLoader('report_warnings', str(CORE / 'bin/brain')).load_module()
        self.reports = reports = core._owners['report_size_warnings']
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.folder = self.base / 'reports/example'
        self.folder.mkdir(parents=True)
        self.node = dict(id='demo')
        self.workbench = mock.patch.object(reports._m_worktrees, 'workbench', return_value=self.base)
        self.workbench.start()
        self.addCleanup(self.workbench.stop)
        self.clock = mock.patch.object(reports.time, 'monotonic', return_value=0)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def warnings(self):
        return self.reports.report_size_warnings(None, [self.node, self.node])

    def test_one_gib_boundary_counts_sparse_bytes_without_reading_contents(self):
        payload = self.folder / 'bulk'
        with payload.open('wb') as stream:
            stream.truncate(1024 ** 3)
        before = payload.stat()
        self.assertEqual([], self.warnings())
        with payload.open('ab') as stream:
            stream.write(b'x')
        with mock.patch.object(Path, 'read_bytes', side_effect=AssertionError('content read')):
            warnings = self.warnings()
        self.assertEqual(1, len(warnings))
        self.assertIn('report folder exceeds 1 GiB', warnings[0])
        self.assertEqual(before.st_ino, payload.stat().st_ino)

    def test_threshold_is_binary_gib_and_strict_with_tiny_fixtures(self):
        self.assertEqual(1024 ** 3, self.reports.REPORT_WARNING_BYTES)
        for size in (7, 8, 9):
            with self.subTest(size=size), mock.patch.object(self.reports, 'REPORT_WARNING_BYTES', 8):
                (self.folder / 'index.md').write_bytes(b'x' * size)
                warnings = self.warnings()
                self.assertEqual(int(size > 8), len(warnings))
                if warnings:
                    self.assertIn('report folder exceeds 1 GiB', warnings[0])

    def test_budget_and_unreadable_sizes_are_unknown_never_small_or_fixable(self):
        (self.folder / 'index.md').touch()
        with mock.patch.object(self.reports, 'REPORT_SIZE_ENTRIES', 1):
            self.assertTrue(any('unknown' in warning for warning in self.warnings()))
        with mock.patch.object(self.reports.os, 'scandir', side_effect=PermissionError):
            self.assertEqual(['demo: reports: size unknown (unreadable)'], self.warnings())

    def test_links_are_never_followed_or_reported_as_known_small(self):
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'keep').write_text('unchanged')
        (self.folder / 'alias').symlink_to(outside, target_is_directory=True)
        self.assertTrue(any('unknown' in warning for warning in self.warnings()))
        (self.base / 'reports/linked').symlink_to(outside, target_is_directory=True)
        self.assertTrue(any('linked: size unknown (link)' in warning for warning in self.warnings()))
        self.assertEqual('unchanged', (outside / 'keep').read_text())
