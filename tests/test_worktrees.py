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


class RebindLedgerJob(Scenario):
    def setUp(self):
        super().setUp()
        self.root = self.init('rebind')
        self.home = self.repo(self.tmp / 'app')
        self.b(self.root, 'new', 'a', '--title', 'A', '--home', str(self.home))
        self.b(self.root, 'set', 'a', 'runtime=codex', 'verify=python3 -c "pass"')

    def start(self, task=False):
        if task:
            self.b(self.root, 'task', 'add', 'a', 'RECOVER', 'status=ready', '--title', 'Recover work')
        self.b(self.root, 'wt', 'add', 'a', 'job', *(['--task-id', 'RECOVER'] if task else []))
        self.wt = self.tmp / 'app-work/worktrees/job'
        (self.wt / 'feature.py').write_text('value = 1\n')
        self.git('add', 'feature.py', cwd=self.wt)
        self.git('commit', '-qm', 'Deliver feature', cwd=self.wt)
        self.module = core_loader.SourceFileLoader('rebind_ledger', str(CORE / 'bin/brain')).load_module()
        inst = self.module.Instance(self.root)
        n = inst.node('a')
        self.old = self.module.read_jobs(n)[0]
        self.module.write_jobs(inst, n, [])

    def test_rebind_restores_missing_row_and_native_handback_is_accepted(self):
        self.start()
        (self.wt / 'draft').write_text('unfinished\n')
        before = self.git('status', '--porcelain', cwd=self.wt)
        self.assertIn('rebound:', self.b(self.root, 'jobs', 'a', '--rebind', 'job'))
        self.assertEqual(self.git('status', '--porcelain', cwd=self.wt), before)
        self.assertIn('already bound:', self.b(self.root, 'jobs', 'a', '--rebind', 'job'))
        self.b(self.root, 'handback', 'a', '--branch', 'job', '--state', 'done',
               '--summary', 'Recovered native handback', '--lessons', 'none: fixture')
        live = json.loads(self.b(self.root, 'jobs', 'a', '--live'))
        self.assertEqual(live['handback']['state'], 'done')
        self.assertEqual(live['handback']['summary'], 'Recovered native handback')

    def test_active_task_attempt_recovers_original_identity_and_checked_handback(self):
        self.start(task=True)
        self.b(self.root, 'jobs', 'a', '--rebind', 'job')
        live = json.loads(self.b(self.root, 'jobs', 'a', '--live'))
        self.assertEqual(live['job_key'], self.module.job_key(self.old))
        self.assertEqual(live['ledger']['Task'], 'RECOVER')
        self.b(self.root, 'handback', 'a', '--branch', 'job', '--state', 'done', '--reviews', '1',
               '--summary', 'Recovered checked handback', '--lessons', 'none: fixture')
        live = json.loads(self.b(self.root, 'jobs', 'a', '--live'))
        self.assertEqual(live['handback']['ready']['verify_result'], 0)

    def test_rebind_refuses_missing_or_mismatched_checkout_and_combined_updates(self):
        self.start()
        for args in (('job/absent',), ('main',), ('job', '--close'), ('job', '--live'),
                     ('job', '--branch', 'job'), ('job', '--thread', 'fixture')):
            self.b(self.root, 'jobs', 'a', '--rebind', *args, ok=False)
        self.git('checkout', '--detach', cwd=self.wt)
        self.b(self.root, 'jobs', 'a', '--rebind', 'job', ok=False)
        self.assertEqual(self.module.read_jobs(self.module.Instance(self.root).node('a')), [])

    def test_rebind_never_recounts_a_historically_landed_branch(self):
        self.start()
        self.b(self.root, 'jobs', 'a', '--rebind', 'job')
        self.git('merge', '--ff-only', 'job', cwd=self.home)
        self.b(self.root, 'jobs', 'a')
        self.assertIn('historical job binding exists', self.b(self.root, 'jobs', 'a', '--rebind', 'job', ok=False))
        self.b(self.root, 'jobs', 'a')
        rows = self.module.read_jobs(self.module.Instance(self.root).node('a'))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['Outcome'], 'merged')


class Workbench(Scenario):
    fixture_init_in_process = True

    def test_setup_holds_machine_slot_and_absent_or_disabled_setup_does_not_queue(self):
        root = self.init('oak')
        home = self.repo(self.tmp / 'code/app')
        self.b(root, 'new', 'app', '--title', 'App', '--home', str(home))
        queue_path = self.tmp / '.local/state/brain/machine-care-queue.json'
        self.b(root, 'wt', 'add', 'app', 'no-script')
        self.assertTrue(not queue_path.exists() or json.loads(queue_path.read_text()) == [])
        setup = root / 'nodes/app/worktree-setup.sh'
        setup.write_text("cat \"$HOME/.local/state/brain/machine-care-queue.json\" > reservation.json\n")
        self.b(root, 'wt', 'add', 'app', 'disabled', '--no-setup')
        self.assertTrue(not queue_path.exists() or json.loads(queue_path.read_text()) == [])
        self.b(root, 'wt', 'add', 'app', 'with-script')
        worktree = self.tmp / 'code/app-work/worktrees/with-script'
        rows = json.loads((worktree / 'reservation.json').read_text())
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['kind'], rows[0]['state']), ('verify', 'running'))
        self.assertIn('child', rows[0])
        self.assertTrue(not queue_path.exists() or json.loads(queue_path.read_text()) == [])

    def projection_race(self, command, change=None):
        root = self.init('projection-race')
        home = self.repo(self.tmp / 'app')
        self.b(root, 'new', 'app', '--title', 'App', '--home', str(home))
        marker, release = self.tmp / 'computed', self.tmp / 'release'
        # A separate process is essential: brain_lock is process-reentrant.
        script = '''import json, os, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1] + '/lib')
from brain import cli, events, instance
original = events.snapshot_view
marker, release = Path(sys.argv[2]), Path(sys.argv[3])
def paused(inst, node, *args):
    view = original(inst, node, *args)
    if node['id'] == 'app':
        assert instance._LOCK['fh'] is None, 'projection retained writer lock'
        head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=node['home_path'],
                              capture_output=True, text=True, check=True).stdout.strip()
        with marker.open('a') as stream:
            stream.write(json.dumps({'head': head, 'view': view, 'node': events.node_ref(node['id'])}) + '\\n')
        deadline = time.monotonic() + 20
        while not release.exists():
            if time.monotonic() > deadline: raise AssertionError('release missing')
            time.sleep(.02)
    return view
events.snapshot_view = paused
args = json.loads(sys.argv[4])
sys.argv = [sys.argv[0], *args]
cli.main()
'''
        env = {**self.env, 'BRAIN_ROOT': str(root)}
        child = subprocess.Popen([sys.executable, '-B', '-c', script, str(CORE),
                                  str(marker), str(release), json.dumps(command)],
                                 cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 20
            while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(marker.exists(), 'command did not compute its view unlocked')
            before = (root / 'nodes/app/status.md').read_bytes()
            if change == 'source':
                # Admission returns before apply; wait for this real writer's
                # native receipt while projection remains paused.
                writer = subprocess.run([sys.executable, '-B', str(CORE / 'bin/brain'),
                                         'inbox', 'app', '--add', 'concurrent question', '--json'],
                                        cwd=root, env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(writer.returncode, 0, writer.stdout + writer.stderr)
                request_id = json.loads(writer.stdout)['request_id']
                waited = subprocess.run([sys.executable, '-B', str(CORE / 'bin/brain'),
                                         'queue', 'wait', request_id, '--timeout', '10'],
                                        cwd=root, env=env, capture_output=True, text=True, timeout=15)
                self.assertEqual(waited.returncode, 0, waited.stdout + waited.stderr)
                receipt = json.loads(waited.stdout)
                self.assertEqual(receipt['id'], request_id)
                self.assertEqual(receipt['state'], 'applied')
                before = (root / 'nodes/app/status.md').read_bytes()
            elif change == 'binding':
                writer = subprocess.run([sys.executable, '-B', str(CORE / 'bin/brain'),
                                         'set', 'app', 'preview=none'],
                                        cwd=root, env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(writer.returncode, 0, writer.stdout + writer.stderr)
            elif change == 'git':
                old_head = self.git('rev-parse', 'HEAD', cwd=home).strip()
                self.git('commit', '--allow-empty', '-qm', 'concurrent integration', cwd=home)
                new_head = self.git('rev-parse', 'HEAD', cwd=home).strip()
            release.write_text('continue')
            out, err = child.communicate(timeout=20)
            self.assertEqual(child.returncode, 0, out + err)
        finally:
            release.touch()
            if child.poll() is None:
                child.terminate()
                child.communicate(timeout=10)
        rows = [json.loads(line) for line in (root / '.brain/events.jsonl').read_text().splitlines()]
        commits = [row for row in rows if row['kind'] == 'commit']
        last = commits[-1]['data']
        retried = command[0] == 'wt' and change == 'git'
        self.assertEqual(last['maintenance_ok'], change is None or retried, out + err)
        self.assertEqual(last['command'], {'report': 'report', 'workbench': 'workbench', 'wt': 'wt_add'}[command[0]])
        observations = [json.loads(line) for line in marker.read_text().splitlines()]
        if retried:
            self.assertEqual([row['head'] for row in observations], [old_head, new_head])
            self.assertEqual(last['views'][observations[-1]['node']], observations[-1]['view'])
        else:
            self.assertEqual(len(observations), 1)
        if change and not retried:
            self.assertEqual((root / 'nodes/app/status.md').read_bytes(), before)
            self.assertEqual(last['views'], {})
            self.assertTrue(last['invalidate'])
        else:
            self.assertTrue(last['views'])
        # The operation's own primary mutation still completed exactly once.
        wb = self.tmp / 'app-work'
        if command[0] == 'report':
            self.assertEqual(len(list((wb / 'reports').glob('*/index.md'))), 1)
        elif command[0] == 'workbench':
            self.assertTrue((wb / 'scripts').is_dir())
        else:
            self.assertTrue((wb / 'worktrees/job-race').is_dir())
        self.assertFalse(list((root / '.brain/events-leases').glob('*')))

    def test_report_publishes_after_unlocked_projection(self):
        self.projection_race(['report', 'app', 'race'])

    def test_workbench_publishes_after_unlocked_projection(self):
        self.projection_race(['workbench', 'app', '--path'])

    def test_wt_publishes_after_unlocked_projection(self):
        self.projection_race(['wt', 'add', 'app', 'job-race', '--no-setup'])

    def test_report_refuses_stale_projection_after_concurrent_source_write(self):
        self.projection_race(['report', 'app', 'race'], 'source')

    def test_workbench_refuses_stale_projection_after_binding_write(self):
        self.projection_race(['workbench', 'app', '--path'], 'binding')

    def test_wt_recomputes_stale_projection_after_integration_moves(self):
        self.projection_race(['wt', 'add', 'app', 'job-race', '--no-setup'], 'git')

    def machine_unlocked(self, command):
        root = self.init('machine-unlocked')
        home = self.repo(self.tmp / 'app')
        self.b(root, 'new', 'app', '--title', 'App', '--home', str(home))
        calls = self.tmp / 'machine-calls'
        # A separate process is essential: brain_lock is process-reentrant.
        script = '''import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1] + '/lib')
from brain import cli, instance, machine
original, calls = machine.machine_state, Path(sys.argv[2])
def observed(*args, **kwargs):
    with calls.open('a') as stream:
        stream.write(('locked' if instance._LOCK['fh'] is not None else 'unlocked') + ' ')
    return original(*args, **kwargs)
machine.machine_state = observed
args = json.loads(sys.argv[3])
sys.argv = [sys.argv[0], *args]
cli.main()
'''
        env = {**self.env, 'BRAIN_ROOT': str(root)}
        run = subprocess.run([sys.executable, '-B', '-c', script, str(CORE), str(calls), json.dumps(command)],
                             cwd=root, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        # The status projection reads machine telemetry twice (facts, snapshot);
        # neither read may hold the writer lock.
        reads = calls.read_text().split()
        self.assertTrue(reads, run.stdout + run.stderr)
        self.assertEqual(set(reads), {'unlocked'}, reads)
        rows = [json.loads(line) for line in (root / '.brain/events.jsonl').read_text().splitlines()]
        last = [row for row in rows if row['kind'] == 'commit'][-1]['data']
        self.assertTrue(last['maintenance_ok'])
        self.assertIn('Machine (snapshot at refresh', (root / 'nodes/app/status.md').read_text())

    def test_report_reads_machine_snapshot_unlocked(self):
        self.machine_unlocked(['report', 'app', 'race'])

    def test_workbench_reads_machine_snapshot_unlocked(self):
        self.machine_unlocked(['workbench', 'app', '--path'])

    def test_wt_reads_machine_snapshot_unlocked(self):
        self.machine_unlocked(['wt', 'add', 'app', 'job-race', '--no-setup'])

    def test_first_ledger_runtime_matches_worker_route_before_brief(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "app")
        self.b(r, "new", "app", "--title", "App", "--home", str(home))
        node_file = r / "nodes/app/node.md"
        baseline = re.sub(r"^runtime:.*\n", "", node_file.read_text(), flags=re.M)
        cases = [
            ("class-default", {"default_runtime": "claude"}, {}, {}, "codex", "runtime default"),
            ("node-pin", {}, {}, {"runtime": "claude", "route_model_claude": "pinned-model"},
             "claude", "pinned-model"),
            ("node-class", {"default_runtime": "claude"}, {},
             {"route_runtime_build": "codex", "route_model_codex_build": "node-model"}, "codex", "node-model"),
            ("instance-class", {"route_runtime_build": "claude", "route_model_claude_build": "instance-model"},
             {}, {}, "claude", "instance-model"),
            ("inherited-model", {"default_runtime": "claude"}, {"route_model_codex_build": "ancestor-model"},
             {}, "codex", "ancestor-model"),
            ("inherited-runtime", {"default_runtime": "claude"},
             {"route_runtime_build": "claude", "route_model_codex_build": "ancestor-model"},
             {}, "codex", "ancestor-model"),
        ]
        # Clear each case's explicit keys so the instance default cannot become a node pin.
        for branch, instance, ancestor, node, runtime, model in cases:
            with self.subTest(branch=branch):
                (r / "instance.md").write_text("---\nname: alice\noperator: alice\nprincipals: alice\n"
                    "runtimes: claude,codex\nhost: t3\ntimezone: UTC\n" +
                    "".join(f"{k}: {v}\n" for k, v in instance.items()) + "---\n")
                (r / "node.md").write_text("---\nid: root\nowners: alice\n" +
                    "".join(f"{k}: {v}\n" for k, v in ancestor.items()) + "---\n")
                node_file.write_text(baseline.replace("---\n", "---\n" +
                    "".join(f"{k}: {v}\n" for k, v in node.items()), 1))
                before = self.git("rev-parse", "HEAD", cwd=r).strip()
                self.b(r, "wt", "add", "app", branch)
                ledger = "nodes/app/job-ledger.md"
                first = self.git("log", "--reverse", "--format=%H", f"{before}..HEAD", "--", ledger, cwd=r).splitlines()[0]
                text = self.git("show", f"{first}:{ledger}", cwd=r)
                rows = [[cell.strip() for cell in line.strip("|").split("|")]
                        for line in text.splitlines() if line.startswith("|")]
                recorded = [dict(zip(rows[0], cells)) for cells in rows[2:] if cells[0] == branch]
                self.assertEqual(len(recorded), 1)
                self.assertEqual(recorded[0]["Runtime"], runtime)
                brief = self.b(r, "brief", "app", "--branch", branch, "--task", "Fixture work")
                self.assertIn(f"runtime: {runtime}", brief)
                self.assertIn(f"model: {model}", brief)

    def test_failed_setup_keeps_the_worktree_and_reports_failure(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "code" / "app")
        self.b(r, "new", "app", "--title", "App", "--home", str(home))
        self.b(r, 'set', 'app', 'verify_export_port=true')
        self.env['PORT'] = '9200'
        self.env.pop('PORTS', None)
        self.env.pop('PORT_LAST', None)
        (r / "nodes" / "app" / "worktree-setup.sh").write_text(
            'set -eu\n[ "$PORT" = 9200 ]\n[ "$PORTS" = "$BRAIN_PORTS" ]\n'
            '[ "$PORT_LAST" = "$BRAIN_PORT_LAST" ]\necho partial > partial.txt\nexit 7\n')
        p = subprocess.run([sys.executable, str(r / "core" / "bin" / "brain"), "wt", "add", "app", "job-fails"],
                           cwd=r, env=self.env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 7, p.stdout + p.stderr)
        self.assertIn("setup: FAILED", p.stdout)
        self.assertNotIn("launch a worker there", p.stdout)
        local = json.loads((r / ".brain" / "local.json").read_text())
        path = Path(local["nodes"]["app"]["worktrees"]["job-fails"]["path"])
        self.assertEqual((path / "partial.txt").read_text(), "partial\n")
        self.assertIn("job-fails", self.b(r, "wt", "list", "app"))

    def test_worktrees_with_setup_ports_and_prune(self):
        r = self.init("alice")
        self.b(r, "principal", "add", "team")
        origin = self.tmp / "origin.git"
        self.git("init", "-q", "--bare", "-b", "main", str(origin), cwd=self.tmp)
        home = self.repo(self.tmp / "code" / "app", {"README.md": "app\n", ".gitignore": ".env\n"})
        self.git("remote", "add", "origin", str(origin), cwd=home)
        self.git("push", "-q", "-u", "origin", "main", cwd=home)
        self.git("remote", "set-head", "origin", "main", cwd=home)
        (home / ".env").write_text("SECRET=1\n")
        self.b(r, "new", "app", "--title", "App", "--owners", "team", "--home", str(home))
        for key in ('PORT', 'PORTS', 'PORT_LAST'):
            self.env.pop(key, None)
        (r / "nodes" / "app" / "worktree-setup.sh").write_text(
            'set -eu\n[ "${PORT+x}${PORTS+x}${PORT_LAST+x}" = "" ]\n'
            'cp "$MAIN/.env" .env\necho "PORT=$BRAIN_PORT" >> .env\n')

        out = self.b(r, "wt", "add", "app", "feat-a")
        wa = self.tmp / "code" / "app-work" / "worktrees" / "feat-a"
        self.assertIn("setup: ok", out)
        self.assertIn("SECRET=1", (wa / ".env").read_text())
        self.b(r, "wt", "add", "app", "feat-b")
        wb_ = wa.parent / "feat-b"
        ports = {(w / ".env").read_text().split("PORT=")[1].strip() for w in (wa, wb_)}
        self.assertEqual(len(ports), 2, "each worktree gets its own port")
        self.assertIn("✓", self.b(r, "check"))                  # nothing in the home, nothing in the instance

        # feat-a is done and merged; feat-b has unfinished work; feat-c is new and untouched
        (wa / "a.txt").write_text("a\n")
        self.git("add", "a.txt", cwd=wa); self.git("commit", "-qm", "A", cwd=wa)
        self.git("merge", "-q", "feat-a", cwd=home); self.git("push", "-q", "origin", "main", cwd=home)
        (wb_ / "wip.txt").write_text("wip\n")
        self.b(r, "wt", "add", "app", "feat-c")
        self.assertIn("would remove: feat-a", self.b(r, "wt", "prune", "app", "--dry-run"))
        out = self.b(r, "wt", "prune", "app")
        self.assertIn("removed: feat-a", out)
        self.assertIn("feat-b (uncommitted work)", out)
        self.assertIn("feat-c (not merged)", out)
        self.assertFalse(wa.exists())
        self.assertTrue((wb_ / "wip.txt").exists())
        self.b(r, "wt", "rm", "app", "feat-b", ok=False)        # uncommitted work is never discarded silently
        self.assertIn("feat-c", self.b(r, "wt", "list"))

    def test_add_reuses_cached_submodule_gitdir_before_setup_without_remote(self):
        r = self.init("alice")
        source = self.repo(self.tmp / "module-source")
        home = self.repo(self.tmp / "app")
        self.git("submodule", "add", "--name", "cached-module", str(source), "vendor/module", cwd=home)
        self.git("commit", "-qam", "Cache submodule", cwd=home)
        cached = home / ".git/modules/cached-module"
        before = (cached / "HEAD").read_bytes()
        before_config = (cached / "config").read_bytes()
        source.rename(self.tmp / "remote-unavailable")
        self.b(r, "new", "app", "--title", "App", "--home", str(home))
        (r / "nodes/app/worktree-setup.sh").write_text("git submodule update --init --recursive\n")
        out = self.b(r, "wt", "add", "app", "job-cached")
        wt = self.tmp / "app-work/worktrees/job-cached"
        self.assertIn("reused submodule: vendor/module", out)
        self.assertIn("setup: ok", out)
        self.assertEqual((wt / "vendor/module/README.md").read_text(), "hello\n")
        common = self.git("rev-parse", "--git-common-dir", cwd=wt / "vendor/module").strip()
        module_gitdir = (wt / "vendor/module" / common).resolve()
        self.assertNotEqual(module_gitdir, cached.resolve())
        self.assertEqual((module_gitdir / "objects/info/alternates").read_text().strip(), str(cached / "objects"))
        self.assertEqual((cached / "HEAD").read_bytes(), before)
        self.assertEqual(self.git("status", "--porcelain", cwd=wt), "")
        # A second job gets independent metadata and the requested detached commit.
        self.b(r, "wt", "add", "app", "job-second", "--no-setup")
        other = wt.parent / "job-second/vendor/module"
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=other),
                         self.git("rev-parse", "HEAD", cwd=home / "vendor/module"))
        self.assertNotEqual(self.git("rev-parse", "--git-dir", cwd=other),
                            self.git("rev-parse", "--git-dir", cwd=wt / "vendor/module"))
        self.assertEqual((cached / "HEAD").read_bytes(), before)
        self.assertEqual((cached / "config").read_bytes(), before_config)

    def test_prune_reports_submodules_and_retains_shared_module_data(self):
        r = self.init("alice")
        source = self.repo(self.tmp / "module-source")
        home = self.repo(self.tmp / "app")
        self.git("submodule", "add", "--name", "shared-name", str(source), "vendor/module", cwd=home)
        self.git("commit", "-qam", "Add module", cwd=home)
        self.b(r, "new", "app", "--title", "App", "--home", str(home))
        self.b(r, "wt", "add", "app", "job-module")
        wt = self.tmp / "app-work/worktrees/job-module"
        self.git("submodule", "update", "--init", cwd=wt)
        self.git("commit", "--allow-empty", "-qm", "Finish job", cwd=wt)
        self.git("merge", "--ff-only", "job-module", cwd=home)
        shared = home / ".git/modules/shared-name"
        before = (shared / "HEAD").read_bytes()
        for flags in (("--dry-run",), ()):
            out = self.b(r, "wt", "prune", "app", "--offline", *flags)
            self.assertIn("job-module (submodules retained: vendor/module)", out)
            self.assertIn("none", out)
            self.assertTrue((wt / "vendor/module/README.md").exists())
            self.assertEqual((shared / "HEAD").read_bytes(), before)
        self.assertIn("job-module", self.b(r, "wt", "list", "app"))

    def test_reports_have_a_predictable_place(self):
        r = self.init("alice")
        self.b(r, "new", "site", "--title", "Site")
        d = Path(self.b(r, "report", "site", "UI audit: mobile").strip())
        self.assertEqual(d.parent, r / "nodes" / "site" / "reports")
        self.assertTrue(d.name.endswith("-ui-audit-mobile"))
        for h in ("## Answer", "## Findings", "## Controls", "## Not run", "## How to reproduce"):
            self.assertIn(h, (d / "index.md").read_text())
        self.assertIn("UI audit: mobile", self.b(r, "reports"))
        self.assertIn("scripts/", self.b(r, "workbench", "site"))

    def test_a_workbench_inside_the_instance_is_refused(self):
        r = self.init("alice")
        self.b(r, "principal", "add", "team")
        home = self.repo(self.tmp / "h")
        self.b(r, "new", "x", "--title", "X", "--owners", "team", "--home", str(home))
        self.b(r, "set", "x", "workbench=" + str(r / "wb"))
        self.assertIn("workbench is inside the instance", self.b(r, "check", ok=False))

    def test_batched_list_matches_individual_git_merge_and_ahead_verdicts(self):
        home = self.repo(self.tmp / 'list-states')
        start = self.git('rev-parse', 'HEAD', cwd=home).strip()
        self.git('checkout', '-qb', 'merged', cwd=home)
        (home / 'landed').write_text('landed\n')
        self.git('add', '.', cwd=home)
        self.git('commit', '-qm', 'landed work', cwd=home)
        self.git('checkout', '-q', 'main', cwd=home)
        self.git('merge', '--ff-only', 'merged', cwd=home)
        self.git('branch', 'fresh', cwd=home)
        fresh = self.git('rev-parse', 'HEAD', cwd=home).strip()
        self.git('checkout', '-qb', 'open', start, cwd=home)
        (home / 'pending').write_text('pending\n')
        self.git('add', '.', cwd=home)
        self.git('commit', '-qm', 'pending work', cwd=home)
        core = core_loader.SourceFileLoader('list_states', str(CORE / 'bin/brain')).load_module()
        wts = {br: {'path': str(home), 'start': start if br != 'fresh' else fresh}
               for br in ('merged', 'open', 'fresh')}
        n = {'home_path': home, 'fm': {}, 'local': {'worktrees': wts}}
        expected = {br: core.worktree_state(home, w, br, n) for br, w in wts.items()}
        with core.git_read_snapshot():
            self.assertEqual(core.list_worktree_states(n, {}), expected)
        self.assertTrue(expected['merged'][1])
        self.assertFalse(expected['open'][1])
        self.assertEqual(expected['open'][2], '1')
        self.assertFalse(expected['fresh'][1])


class PruneSafety(AutonomyProject):
    def close_job(self, r, branch='job-a'):
        ledger = r / 'nodes/app/job-ledger.md'
        lines = ledger.read_text().splitlines()
        for i, line in enumerate(lines):
            cells = line.split('|')
            if len(cells) > 4 and cells[1].strip() == branch:
                cells[3] = ' 2025-01-01T00:00:00+00:00 '
                cells[5] = ' merged '
                lines[i] = '|'.join(cells)
        ledger.write_text('\n'.join(lines) + '\n')

    def merged(self):
        r, home, wt = self.project()
        (wt / 'done.txt').write_text('done\n')
        self.git('add', '.', cwd=wt)
        self.git('commit', '-qm', 'finished', cwd=wt)
        self.git('merge', '-q', 'job-a', cwd=home)
        return r, home, wt

    def test_finished_prunes_and_closes_own_open_row(self):
        r, home, wt = self.merged()
        self.assertIn('removed: job-a', self.b(r, 'wt', 'prune', 'app', '--offline'))
        self.assertFalse(wt.exists())
        self.assertIn('merged', self.b(r, 'jobs', 'app', '--branch', 'job-a'))

    def test_different_open_job_claiming_path_survives(self):
        r, home, wt = self.merged()
        self.b(r, 'wt', 'add', 'app', 'other-job')
        local_path = r / '.brain/local.json'
        local = json.loads(local_path.read_text())
        local['nodes']['app']['worktrees']['other-job']['path'] = str(wt)
        local_path.write_text(json.dumps(local))
        self.assertIn('open ledger job other-job', self.b(r, 'wt', 'prune', 'app', '--offline'))
        self.assertIn('open ledger job other-job', self.b(r, 'wt', 'rm', 'app', 'job-a', '--force', ok=False))
        self.assertTrue(wt.exists())

    def test_reused_folder_followup_survives_and_other_finished_job_prunes(self):
        r, home, wt = self.merged()
        self.close_job(r)
        self.b(r, 'wt', 'add', 'app', 'followup')
        local_path = r / '.brain/local.json'
        local = json.loads(local_path.read_text())
        followup = local['nodes']['app']['worktrees']['followup']
        self.git('worktree', 'remove', followup['path'], cwd=home)
        self.git('switch', 'followup', cwd=wt)
        followup['path'] = str(wt)
        local_path.write_text(json.dumps(local))
        self.b(r, 'wt', 'add', 'app', 'finished')
        done = wt.parent / 'finished'
        (done / 'another').write_text('done')
        self.git('add', '.', cwd=done)
        self.git('commit', '-qm', 'another done', cwd=done)
        self.git('merge', '-q', 'finished', cwd=home)
        out = self.b(r, 'wt', 'prune', 'app', '--offline')
        self.assertIn('removed: finished', out)
        self.assertFalse(done.exists())
        self.assertIn('checked-out branch differs', out)
        self.assertTrue(wt.exists())
        self.assertIn('checked-out branch differs', self.b(r, 'wt', 'rm', 'app', 'job-a', '--force', ok=False))

    def test_detached_head_survives(self):
        r, home, wt = self.merged()
        self.close_job(r)
        self.git('checkout', '--detach', '-q', cwd=wt)
        self.assertIn('HEAD is detached', self.b(r, 'wt', 'prune', 'app', '--offline'))
        self.assertTrue(wt.exists())

    def test_live_binding_survives_but_terminal_binding_allows_cleanup(self):
        r, home, wt = self.merged()
        self.close_job(r)
        status = r / 'nodes/app/status.md'
        status.write_text('## Active work\n| Branch | Thread | PR | State |\n| job-a | fake-thread | - | working |\n')
        self.assertIn('live bound thread', self.b(r, 'wt', 'prune', 'app', '--offline'))
        self.assertIn('live bound thread', self.b(r, 'wt', 'rm', 'app', 'job-a', ok=False))
        status.write_text(status.read_text().replace('working', 'merged'))
        self.assertIn('removed: job-a', self.b(r, 'wt', 'prune', 'app', '--offline'))

    def test_add_refuses_owned_missing_folder_and_existing_folder(self):
        r, home, wt = self.project()
        self.git('worktree', 'remove', str(wt), cwd=home)
        self.assertIn('create a fresh worktree', self.b(r, 'wt', 'add', 'app', 'job/a', ok=False))
        self.assertFalse(wt.exists())
        wt.mkdir()
        self.assertIn('create a fresh worktree', self.b(r, 'wt', 'add', 'app', 'job/a', ok=False))

    def test_creation_sha_and_deleted_base_do_not_control_merge_status(self):
        for mode in ('sha', 'deleted'):
            with self.subTest(mode=mode):
                r = self.init(mode)
                home = self.repo(self.tmp / mode / 'repo')
                self.b(r, 'new', 'app', '--title', 'App', '--home', str(home))
                self.git('branch', 'job-base', cwd=home)
                base = self.git('rev-parse', 'HEAD', cwd=home).strip() if mode == 'sha' else 'job-base'
                self.b(r, 'wt', 'add', 'app', 'job-a', '--base', base)
                local = json.loads((r / '.brain/local.json').read_text())
                wt = Path(local['nodes']['app']['worktrees']['job-a']['path'])
                (wt / 'done').write_text('done')
                self.git('add', '.', cwd=wt)
                self.git('commit', '-qm', 'done', cwd=wt)
                self.git('merge', '-q', 'job-a', cwd=home)
                self.git('branch', '-D', 'job-base', cwd=home)
                due = json.loads(self.b(r, 'due', 'app', '--json'))
                self.assertTrue(any(item['kind'] == 'merged-job' and 'merged in local git' in item['text'] for item in due))
                self.b(r, 'status', 'app', '--refresh')
                self.assertIn('job-a: merged in local git', (r / 'nodes/app/status.md').read_text())
                state = json.loads(self.b(r, 'ui', '--json'))
                node = next(node for node in state['nodes'] if node['id'] == 'app')
                self.assertEqual(node['worktrees'][0]['ahead'], 0)
                self.assertIn('removed: job-a', self.b(r, 'wt', 'prune', 'app', '--offline'))
                self.assertFalse(wt.exists())

    def stacked(self, merge_child=True):
        r, home, base = self.project()
        (base / 'base-result').write_text('base work\n')
        self.git('add', '.', cwd=base)
        self.git('commit', '-qm', 'base work', cwd=base)
        self.b(r, 'wt', 'add', 'app', 'job-b', '--base', 'job-a')
        local = json.loads((r / '.brain/local.json').read_text())
        child = Path(local['nodes']['app']['worktrees']['job-b']['path'])
        self.assertEqual(local['nodes']['app']['worktrees']['job-b']['base'], 'job-a')
        (child / 'child-result').write_text('stacked work\n')
        self.git('add', '.', cwd=child)
        self.git('commit', '-qm', 'stacked work', cwd=child)
        if merge_child:
            self.git('merge', '--ff-only', 'job-b', cwd=base)
        return r, home, base, child

    def test_stacked_branch_prunes_after_landing_via_deleted_base(self):
        r, home, base, child = self.stacked()
        self.git('merge', '--ff-only', 'job-a', cwd=home)
        self.b(r, 'wt', 'rm', 'app', 'job-a')
        self.git('branch', '-d', 'job-a', cwd=home)
        self.assertFalse(base.exists())
        self.assertIn('would remove: job-b', self.b(r, 'wt', 'prune', 'app', '--offline', '--dry-run'))
        self.assertTrue(child.exists())
        self.assertIn('removed: job-b', self.b(r, 'wt', 'prune', 'app', '--offline'))
        self.assertFalse(child.exists())
        self.assertIn('merged', self.b(r, 'jobs', 'app', '--branch', 'job-b'))

    def test_stacked_branch_merged_only_into_base_is_retained(self):
        r, home, base, child = self.stacked()
        for flags in (('--dry-run',), ()):
            out = self.b(r, 'wt', 'prune', 'app', '--offline', *flags)
            self.assertIn('job-a (not merged)', out)
            self.assertIn('job-b (not merged)', out)
            self.assertTrue(base.exists())
            self.assertTrue(child.exists())

    def test_unmerged_stacked_branch_survives_deleted_landed_base(self):
        r, home, base, child = self.stacked(merge_child=False)
        self.git('merge', '--ff-only', 'job-a', cwd=home)
        self.b(r, 'wt', 'rm', 'app', 'job-a')
        self.git('branch', '-d', 'job-a', cwd=home)
        self.assertFalse(base.exists())
        for flags in (('--dry-run',), ()):
            out = self.b(r, 'wt', 'prune', 'app', '--offline', *flags)
            self.assertIn('job-b (not merged)', out)
            self.assertTrue(child.exists())

    def test_pr_mode_uses_remote_default_despite_stale_local_main(self):
        r, home, wt = self.project()
        origin = self.tmp / 'origin.git'
        self.git('init', '-q', '--bare', '-b', 'main', str(origin), cwd=self.tmp)
        self.git('remote', 'add', 'origin', str(origin), cwd=home)
        self.git('push', '-q', 'origin', 'main', cwd=home)
        self.git('remote', 'set-head', 'origin', 'main', cwd=home)
        (r / 'nodes/app/publication-policy.json').write_text(json.dumps({
            'version': 1, 'node': 'app', 'reviewer': 'alice', 'reviewed_at': '2026-01-01 00:00 +00:00',
            'rules': [], 'consent': {'node': 'app', 'actions': ['pr']}}))
        self.b(r, 'set', 'app', 'merge=pr')
        (wt / 'done').write_text('done')
        self.git('add', '.', cwd=wt)
        self.git('commit', '-qm', 'done', cwd=wt)
        self.git('push', '-q', 'origin', 'job-a:main', cwd=wt)
        self.assertIn('removed: job-a', self.b(r, 'wt', 'prune', 'app', '--offline'))
        self.assertFalse(wt.exists())

    def test_different_open_row_for_same_branch_blocks_cleanup(self):
        r, home, wt = self.merged()
        ledger = r / 'nodes/app/job-ledger.md'
        text = ledger.read_text()
        row = next(line for line in text.splitlines() if line.startswith('| job-a |'))
        ledger.write_text(text + row + '\n')
        self.assertIn('open ledger job job-a', self.b(r, 'wt', 'prune', 'app', '--offline'))
        self.assertTrue(wt.exists())

    def test_unknown_ownership_survives_repeated_refresh_and_removal(self):
        r, home, wt = self.merged()
        self.close_job(r)
        status = r / 'nodes/app/status.md'
        for state in ('unknown', '', 'review — merged into preview'):
            with self.subTest(state=state):
                status.write_text('## Active work\n| Branch | Thread | PR | State |\n'
                                  '| `job-a` (fixture annotation) | fixture-thread | - | ' + state + ' |\n')
                for _ in range(2):
                    self.b(r, 'status', 'app', '--refresh')
                self.assertIn('live bound thread', self.b(r, 'wt', 'prune', 'app', '--offline'))
                self.assertIn('live bound thread', self.b(r, 'wt', 'rm', 'app', 'job-a', '--force', ok=False))
                self.assertTrue(wt.exists())

    def test_local_mode_follows_origin_default_named_master(self):
        r, home, wt = self.project()
        self.git('branch', '-m', 'main', 'master', cwd=home)
        origin = self.tmp / 'origin.git'
        self.git('init', '-q', '--bare', '-b', 'master', str(origin), cwd=self.tmp)
        self.git('remote', 'add', 'origin', str(origin), cwd=home)
        self.git('push', '-q', 'origin', 'master', cwd=home)
        self.git('remote', 'set-head', 'origin', 'master', cwd=home)
        self.b(r, 'set', 'app', 'merge=local')
        (wt / 'done').write_text('done')
        self.git('add', '.', cwd=wt)
        self.git('commit', '-qm', 'done', cwd=wt)
        self.git('merge', '-q', 'job-a', cwd=home)
        self.assertIn('merged in local git', self.b(r, 'due', 'app'))
        self.assertIn('removed: job-a', self.b(r, 'wt', 'prune', 'app', '--offline'))
        self.assertFalse(wt.exists())

    def test_live_binding_accepts_backticks_and_branch_annotation(self):
        r, home, wt = self.merged()
        status = r / 'nodes/app/status.md'
        for cell in ('`job-a`', '`job-a` (then followup)'):
            with self.subTest(cell=cell):
                status.write_text('## Active work\n| Branch | Thread | PR | State |\n'
                                  + '| ' + cell + ' | fake-thread | - | working |\n')
                self.assertIn('live bound thread', self.b(r, 'wt', 'prune', 'app', '--offline'))
                refused = self.b(r, 'wt', 'rm', 'app', 'job-a', '--force', ok=False)
                self.assertIn('live bound thread', refused)
                self.assertNotIn('lessons:', refused)
                self.assertTrue(wt.exists())

    def test_refused_rm_prints_no_cleanup_handback(self):
        r, home, wt = self.merged()
        (r / 'nodes/app/status.md').write_text('## Active work\n| Branch | Thread | PR | State |\n'
                                             '| job-a | fake-thread | - | working |\n')
        out = self.b(r, 'wt', 'rm', 'app', 'job-a', '--force', ok=False)
        self.assertIn('live bound thread', out)
        self.assertNotIn('lessons:', out)
        self.assertTrue(wt.exists())

    def test_pr_mode_reports_missing_origin_default(self):
        r, home, wt = self.merged()
        origin = self.tmp / 'origin.git'
        self.git('init', '-q', '--bare', '-b', 'main', str(origin), cwd=self.tmp)
        self.git('remote', 'add', 'origin', str(origin), cwd=home)
        (r / 'nodes/app/publication-policy.json').write_text(json.dumps({
            'version': 1, 'node': 'app', 'reviewer': 'alice', 'reviewed_at': '2026-01-01 00:00 +00:00',
            'rules': [], 'consent': {'node': 'app', 'actions': ['pr']}}))
        self.b(r, 'set', 'app', 'merge=pr')
        out = self.b(r, 'wt', 'prune', 'app', '--offline')
        self.assertIn('origin default unavailable; run git remote set-head origin -a', out)
        self.assertTrue(wt.exists())


class ReleasedCleanupGeneration(AutonomyProject):
    def cleanup_during_accounting(self, action, change):
        r, home, wt = PruneSafety.merged(self)
        ledger = r / 'nodes/app/job-ledger.md'
        before = ledger.read_bytes()
        loader = core_loader.SourceFileLoader('cleanup_generation_fixture', str(CORE / 'bin/brain'))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        m = importlib.util.module_from_spec(spec)
        loader.exec_module(m)
        entered, resume = threading.Event(), threading.Event()
        errors, output = [], io.StringIO()

        def accounting(*args):
            entered.set()
            if not resume.wait(10):
                raise AssertionError('accounting barrier was not released')
            return {}

        def cleanup():
            try:
                with m.brain_lock(inst):
                    args = argparse.Namespace(id='app', branch='job-a', force=True,
                                              cause='fixture cleanup', dry_run=False, offline=True)
                    (m.cmd_wt_prune if action == 'prune' else m.cmd_wt_rm)(args)
            except BaseException as e:
                errors.append(e)

        with mock.patch.dict(os.environ, self.env, clear=True):
            inst = m.Instance(r)
            with mock.patch.object(m, 'load', return_value=inst), \
                 mock.patch.object(m, 'job_usage', side_effect=accounting), \
                 mock.patch.object(m, 'usage_cells', return_value={}), \
                 contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                worker = threading.Thread(target=cleanup)
                worker.start()
                try:
                    self.assertTrue(entered.wait(10), 'command never reached accounting')
                    self.assertIsNone(m._LOCK['fh'], 'accounting must release the writer lock')
                    if change == 'branch':
                        (wt / 'later.txt').write_text('new work\n')
                        self.git('add', '.', cwd=wt)
                        self.git('commit', '-qm', 'new unmerged work', cwd=wt)
                    elif change == 'integration':
                        (home / 'sibling.txt').write_text('integration changed\n')
                        self.git('add', '.', cwd=home)
                        self.git('commit', '-qm', 'advance integration', cwd=home)
                    elif change == 'dirty':
                        (wt / 'done.txt').write_text('unfinished edit\n')
                    elif change == 'untracked':
                        (wt / 'untracked.txt').write_text('unfinished work\n')
                finally:
                    resume.set()
                    worker.join(10)
                self.assertFalse(worker.is_alive(), 'cleanup did not finish')
        if change:
            self.assertTrue(wt.exists(), output.getvalue())
            self.assertEqual(ledger.read_bytes(), before, output.getvalue())
            self.assertIn('cleanup inputs changed', output.getvalue() + '\n'.join(map(str, errors)))
            self.assertIn('job-a', json.loads((r / '.brain/local.json').read_text())['nodes']['app']['worktrees'])
            self.assertTrue(all(isinstance(e, SystemExit) for e in errors), errors)
        else:
            self.assertEqual(errors, [], output.getvalue())
            self.assertFalse(wt.exists(), output.getvalue())
            self.assertIn('merged', self.b(r, 'jobs', 'app', '--branch', 'job-a'))

    def test_prune_retains_commit_created_during_the_final_process_inventory(self):
        r, home, wt = PruneSafety.merged(self)
        ledger = r / 'nodes/app/job-ledger.md'
        before = ledger.read_bytes()
        m = core_loader.SourceFileLoader('cleanup_process_drift', str(CORE / 'bin/brain')).load_module()
        checks = []
        def inventory(path):
            self.assertIsNone(m._LOCK['fh'])
            checks.append(path)
            if len(checks) == 2:
                (wt / 'new-work.txt').write_text('unmerged fixture work\n')
                self.git('add', 'new-work.txt', cwd=wt)
                self.git('commit', '-qm', 'new unmerged fixture work', cwd=wt)
            return ''
        with mock.patch.dict(os.environ, self.env, clear=True):
            inst = m.Instance(r)
            with mock.patch.object(m, 'load', return_value=inst), \
                    mock.patch.object(m, 'prune_process_refusal', side_effect=inventory), \
                    contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                with m.brain_lock(inst):
                    m.cmd_wt_prune(argparse.Namespace(id='app', dry_run=False, offline=True))
        self.assertEqual(len(checks), 2)
        self.assertTrue(wt.exists())
        self.assertEqual(ledger.read_bytes(), before)
        self.assertIn('job-a', json.loads((r / '.brain/local.json').read_text())['nodes']['app']['worktrees'])

    def test_prune_retains_new_branch_commit(self):
        self.cleanup_during_accounting('prune', 'branch')

    def test_prune_retains_moved_integration(self):
        self.cleanup_during_accounting('prune', 'integration')

    def test_prune_retains_new_dirty_work(self):
        self.cleanup_during_accounting('prune', 'dirty')

    def test_prune_retains_new_untracked_work(self):
        self.cleanup_during_accounting('prune', 'untracked')

    def test_rm_retains_new_branch_commit(self):
        self.cleanup_during_accounting('rm', 'branch')

    def test_rm_retains_moved_integration(self):
        self.cleanup_during_accounting('rm', 'integration')

    def test_rm_retains_new_dirty_work_even_with_force(self):
        self.cleanup_during_accounting('rm', 'dirty')

    def test_rm_retains_new_untracked_work_even_with_force(self):
        self.cleanup_during_accounting('rm', 'untracked')

    def test_prune_unchanged_generation_closes_merged(self):
        self.cleanup_during_accounting('prune', None)

    def test_rm_unchanged_generation_closes_merged(self):
        self.cleanup_during_accounting('rm', None)


class WorktreeGuardUnits(unittest.TestCase):
    def setUp(self):
        loader = core_loader.SourceFileLoader('prune_fixture', str(CORE / 'bin/brain'))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        self.core = importlib.util.module_from_spec(spec)
        loader.exec_module(self.core)

    def test_checked_out_head_must_equal_recorded_branch_tip(self):
        def git(*args, **kwargs):
            output = 'refs/heads/job' if args[0] == 'symbolic-ref' else ('a' if args[-1] == 'HEAD' else 'b')
            return subprocess.CompletedProcess(args, 0, output, '')
        n = {'home_path': CORE, 'local': {}}
        with mock.patch.object(self.core, 'git', side_effect=git):
            self.assertIn('HEAD differs', self.core.removal_refusal(n, 'job', {'path': str(CORE)}))

    def test_squash_lookup_is_restricted_to_integration_branch(self):
        def git(*args, **kwargs):
            return subprocess.CompletedProcess(args, 1 if args[0] == 'merge-base' else 0,
                                               'tip' if args[0] == 'rev-parse' else '', '')
        with mock.patch.object(self.core, 'git', side_effect=git) as git_calls, \
             mock.patch.object(self.core, 'remote_url', return_value='https://github.com/example/repo'), \
             mock.patch.object(self.core, 'integration_ref', return_value='refs/remotes/origin/master'), \
             mock.patch.object(shutil, 'which', return_value='/fake/gh'), \
             mock.patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '[{"number": 1}]', '')) as run:
            self.assertTrue(self.core.worktree_state(CORE, {'path': str(CORE), 'base': 'obsolete', 'start': 'old'}, 'job')[1])
        args = run.call_args.args[0]
        self.assertEqual(args[args.index('--base') + 1], 'master')
        self.assertTrue(any(call.args == ('rev-list', '--count', 'refs/heads/master..refs/heads/job') for call in git_calls.call_args_list))

    def test_existing_worktree_documentation_uses_worktree_path(self):
        text = (CORE / 'adapters/hosts/t3.md').read_text()
        snippets = re.findall(r'\{[^{}]*["\']type["\']\s*:\s*["\']existing_worktree["\'][^{}]*\}', text)
        self.assertTrue(snippets)
        for snippet in snippets:
            with self.subTest(snippet=snippet):
                self.assertIn('"worktreePath"', snippet)
                self.assertNotRegex(snippet, r'"path"\s*:')

    def test_commit_counts_use_explicit_branch_refs(self):
        with mock.patch.object(self.core, 'git', return_value=subprocess.CompletedProcess([], 0, '1', '')) as git:
            self.assertEqual(self.core.job_commits(CORE, {'start': 'old'}, 'job'), '1')
            self.assertIn('old..refs/heads/job', git.call_args.args)

    def test_list_batches_git_and_one_pr_lookup_per_repo_without_changing_merge_states(self):
        refs = {'refs/heads/main': 'main', 'refs/heads/merged': 'main',
                'refs/heads/open': 'open', 'refs/heads/squash': 'squash', 'refs/heads/new': 'main'}
        wts = {br: {'path': str(CORE), 'start': 'start'} for br in ('merged', 'open', 'squash')}
        wts['new'] = {'path': str(CORE), 'start': 'main'}
        wts['missing'] = {'path': str(CORE / 'absent-fixture'), 'start': 'start'}
        n = {'home_path': CORE, 'fm': {}, 'local': {'worktrees': wts}}
        def git(*args, **kwargs):
            output = 'open start\nsquash start\n' if args[0] == 'rev-list' else ''
            return subprocess.CompletedProcess(args, 0, output, '')
        with mock.patch.object(self.core, 'branch_refs', return_value=refs), \
             mock.patch.object(self.core, 'integration_ref', return_value='refs/heads/main'), \
             mock.patch.object(self.core, 'git', side_effect=git) as calls, \
             mock.patch.object(self.core, 'remote_url', return_value='https://github.com/example/repo'), \
             mock.patch.object(shutil, 'which', return_value='/fake/gh'), \
             mock.patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 0,
                 '[{"headRefName":"squash","baseRefName":"main"},'
                 '{"headRefName":"open","baseRefName":"other"}]', '')) as run:
            cache = {}
            for _ in range(2):
                states = self.core.list_worktree_states(n, cache)
                self.assertEqual(states['merged'], (0, True, '0'))
                self.assertEqual(states['open'], (0, False, '1'))
                self.assertEqual(states['squash'], (0, True, '1'))
                self.assertEqual(states['new'], (0, False, '0'))
                self.assertEqual(states['missing'], (None, False, ''))
        run.assert_called_once()
        self.assertFalse(any(c.args[0] == 'merge-base' for c in calls.call_args_list))
        self.assertTrue(all(c.args == ('rev-parse', '--git-common-dir') for c in calls.call_args_list
                            if c.args[0] == 'rev-parse'))
        self.assertEqual(sum(c.args[0] == 'rev-list' for c in calls.call_args_list), 2)


    def test_list_failed_graph_or_pr_lookup_never_claims_merged(self):
        n = {'home_path': CORE, 'fm': {}, 'local': {'worktrees': {
            'job': {'path': str(CORE), 'start': 'old'}}}}
        with mock.patch.object(self.core, 'branch_refs', return_value={'refs/heads/main': 'base', 'refs/heads/job': 'tip'}), \
             mock.patch.object(self.core, 'integration_ref', return_value='refs/heads/main'), \
             mock.patch.object(self.core, 'git', return_value=subprocess.CompletedProcess([], 1, '', '')), \
             mock.patch.object(self.core, 'remote_url', return_value='https://github.com/example/repo'), \
             mock.patch.object(shutil, 'which', return_value='/fake/gh'), \
             mock.patch.object(subprocess, 'run', side_effect=subprocess.TimeoutExpired('gh', 30)):
            self.assertEqual(self.core.list_worktree_states(n, {})['job'], (0, False, ''))


    def test_list_pr_lookup_is_bounded_and_overlaps_index_reads(self):
        wts = {'job': {'path': str(CORE), 'start': 'old'}}
        n = {'id': 'app', 'home_path': CORE, 'fm': {}, 'local': {'worktrees': wts}}
        status_seen = threading.Event()
        def git(*args, **kwargs):
            if args[0] == 'status':
                status_seen.set()
            return subprocess.CompletedProcess(args, 0, 'tip base\n' if args[0] == 'rev-list' else '', '')
        def gh(cmd, **kwargs):
            self.assertEqual(kwargs['timeout'], self.core.LIST_PR_TIMEOUT)
            # A serial lookup would finish before any index read starts.
            overlapped.append(status_seen.wait(5))
            raise subprocess.TimeoutExpired(cmd, kwargs['timeout'])
        overlapped = []
        fake = mock.Mock(nodes=lambda: [n])
        with mock.patch.object(self.core, 'branch_refs', return_value={'refs/heads/main': 'base', 'refs/heads/job': 'tip'}), \
             mock.patch.object(self.core, 'integration_ref', return_value='refs/heads/main'), \
             mock.patch.object(self.core, 'git', side_effect=git), \
             mock.patch.object(self.core, 'remote_url', return_value='https://github.com/example/repo'), \
             mock.patch.object(shutil, 'which', return_value='/fake/gh'), \
             mock.patch.object(subprocess, 'run', side_effect=gh), \
             contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            self.core._list_worktrees(fake, argparse.Namespace(id='app'))
        self.assertLessEqual(self.core.LIST_PR_TIMEOUT, 3)
        self.assertEqual(overlapped, [True])
        self.assertIn('job', out.getvalue())
        self.assertNotIn('merged', out.getvalue())
        self.assertIn('merged-PR lookup unavailable', err.getvalue())



class WorktreeSetupDiagnostics(Scenario):
    def finish(self, r):
        return self.b(r, "handback", "--summary", "Synthetic result", "a", "--branch", "job", "--state", "done", "--reviews", "1")

    def test_setup_completes_before_return_and_first_verify(self):
        r = self.init('alice')
        home = self.repo(self.tmp / 'app', {'.gitignore': '.env\nsetup-started\n',
                         'verify.py': "from pathlib import Path\nassert Path('.env').read_text() == 'GENERATED=prepared-fixture\\n'\n"})
        self.b(r, 'new', 'a', '--title', 'A', '--home', str(home))
        self.b(r, 'set', 'a', 'verify=python3 -B verify.py', 'verify_resources=fixture-service')
        release = home / 'allow-setup'
        (r / 'nodes/a/worktree-setup.sh').write_text(
            'touch setup-started\nwhile ! test -f "$MAIN/allow-setup"; do sleep 0.02; done\n'
            "printf 'GENERATED=prepared-fixture\\n' > .env\n")
        process = subprocess.Popen([sys.executable, str(r / 'core/bin/brain'), 'wt', 'add', 'a', 'job'],
                                   cwd=r, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        wt = self.tmp / 'app-work/worktrees/job'
        try:
            deadline = time.monotonic() + 10
            while not (wt / 'setup-started').exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue((wt / 'setup-started').exists())
            self.assertIsNone(process.poll())
            self.assertFalse((wt / '.env').exists())
        finally:
            release.touch()
            output, errors = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertIn('setup: running; waiting for completion', output)
        self.assertRegex(output, r'setup: ok in \d+\.\d+s')
        self.assertLess(output.index('setup: ok'), output.index('launch a worker'))
        self.assertEqual((wt / '.env').read_text(), 'GENERATED=prepared-fixture\n')
        self.assertTrue(self.finish(r).strip().endswith('ready'))
        lease = next((self.tmp / '.local/state/brain/resource-leases').glob('*.json'))
        self.assertEqual(json.loads(lease.read_text())['state'], 'released')

    def test_setup_unsupported_ownership_reports_failure_and_retains_worktree(self):
        root = self.init('oak')
        home = self.repo(self.tmp / 'app')
        self.b(root, 'new', 'a', '--title', 'A', '--home', str(home))
        (root / 'nodes/a/worktree-setup.sh').write_text('touch must-not-run\n')
        source = root / 'core/lib/brain/machine.py'
        text = source.read_text()
        marker = "if sys.platform == 'darwin':\n            import ctypes"
        self.assertIn(marker, text)
        source.write_text(text.replace(marker, 'if False:\n            import ctypes', 1))
        result = subprocess.run([sys.executable, str(root / 'core/bin/brain'), 'wt', 'add', 'a', 'job'],
                                cwd=root, env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 125, result.stdout + result.stderr)
        self.assertIn('setup: FAILED (exit 125)', result.stdout)
        self.assertNotIn('Traceback', result.stdout + result.stderr)
        worktree = self.tmp / 'app-work/worktrees/job'
        self.assertTrue(worktree.exists())
        self.assertFalse((worktree / 'must-not-run').exists())

    def test_setup_failure_reports_duration_masks_words_and_returns_failure(self):
        r = self.init('alice')
        home = self.repo(self.tmp / 'app')
        self.b(r, 'new', 'a', '--title', 'A', '--home', str(home))
        self.b(r, 'set', 'a', 'verify_resources=fixture-service')
        (r / 'nodes/a/never-name.txt').write_text('HiddenFixture\n')
        (r / 'nodes/a/worktree-setup.sh').write_text('echo HiddenFixture\nexit 7\n')
        process = subprocess.run([sys.executable, str(r / 'core/bin/brain'), 'wt', 'add', 'a', 'job'],
                                 cwd=r, env=self.env, capture_output=True, text=True)
        self.assertEqual(process.returncode, 7)
        self.assertRegex(process.stdout, r'setup: FAILED \(exit 7\) in \d+\.\d+s')
        self.assertNotIn('hiddenfixture', (process.stdout + process.stderr).casefold())
        self.assertNotIn('launch a worker', process.stdout)
        self.assertTrue((self.tmp / 'app-work/worktrees/job').exists())
        lease = next((self.tmp / '.local/state/brain/resource-leases').glob('*.json'))
        self.assertEqual(json.loads(lease.read_text())['state'], 'active')


if __name__ == "__main__":
    unittest.main()


class LightWorktreeAdmission(Scenario):
    def test_cli_dispatch_does_not_admit_light_worktree_creation(self):
        core = core_loader.SourceFileLoader('light_worktree_fixture', str(CORE / 'bin/brain')).load_module()
        inst = argparse.Namespace(node=lambda key: {'id': key})
        with mock.patch.dict(os.environ, self.env), \
                mock.patch.object(core.sys, 'argv', ['brain', 'wt', 'add', 'app', 'job-light', '--no-setup']), \
                mock.patch.object(core, 'try_find_root', return_value=None), \
                mock.patch.object(core, 'load', return_value=inst), \
                mock.patch.object(core, 'cmd_wt_add') as add, \
                mock.patch.object(core, 'machine_wait', side_effect=AssertionError('light work must not wait')) as wait:
            add.__name__ = 'cmd_wt_add'
            core.main()
        add.assert_called_once()
        wait.assert_not_called()
