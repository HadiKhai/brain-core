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
import types
import unittest
from pathlib import Path

CORE = Path(os.environ.get("BRAIN_CORE") or Path(__file__).resolve().parent.parent)


import test_verify as _test_verify
from scenario import Scenario, AutonomyProject, shlex_quote



def consume_fixture_wake(module, inst, node):
    """Simulate the fixture coordinator rendering its frozen receipt after transport."""
    directory = module.wake_directory(inst, node)
    receipt = module.wake_turn_read(directory / 'turn.json', module.wake_turn_binding(inst, node))
    if receipt and not receipt['consumed']:
        return module.wake_turn_consume(inst, node, shown=receipt)


class LockHotfix(Scenario):
    job = _test_verify.WorkerReadyPackets.job

    def assert_compute_allows_writer(self, site, drift=False):
        if site.startswith('receipt'):
            from test_review_discipline import IndependentReviewReceipts
            root, wt, m, inst, n, receipt = IndependentReviewReceipts.context(self)
            if site == 'receipt_preview':
                self.b(root, 'set', 'a', 'preview=http://127.0.0.1:4999/')
                receipt = {'version': 1, 'node': 'a', 'job_key': receipt['job_key'], 'branch': 'job',
                           'head': receipt['head'], 'checked_at': inst.now_dt().isoformat(),
                           'renderer_threadId': 'fixture-renderer', 'urls': [{
                               'url': 'http://127.0.0.1:4999/', 'rendered': True, 'render_evidence': 'fixture',
                               'items': [{'page_item': str(i), 'source_ref': 'fixture:' + str(i),
                                          'source_item': str(i)} for i in range(3)]}]}
            path = self.tmp / 'receipt.json'
            path.write_text(json.dumps(receipt))
            args = (['preview-receipt', 'a', '--branch', 'job', '--file', str(path)]
                    if site == 'receipt_preview' else ['review', 'a', '--branch', 'job', '--receipt-file', str(path)])
        elif site.startswith('handback'):
            root, _, wt = self.job()
            args = ['handback', 'a', '--branch', 'job', '--state', 'done',
                    '--reviews', '1', '--summary', 'Synthetic result', '--lessons', 'none: fixture']
            if site == 'handback_projection':
                # Deferred views now belong only to explicit unchecked direct
                # handbacks; the checked form has a separate synchronous probe.
                args = ['handback', 'a', '--branch', 'job', '--state', 'done', '--direct',
                        '--summary', 'Synthetic result', '--lessons', 'none: fixture']
            if site in ('handback_question', 'handback_blocked'):
                self.b(root, 'set', 'a', 'coordinator=fixture-owner', 'wake=exit 0')
                args = ['handback', 'a', '--branch', 'job', '--state', site.removeprefix('handback_'),
                        '--summary', 'Synthetic result', '--lessons', 'none: fixture']
                if site == 'handback_question':
                    args += ['--question', 'Choose a fixture color']
        else:
            root = self.init('fixture')
            wt = None
            args = ['machine', 'sample'] if site.startswith('sample') else ['wake', 'root', '--drain']
            if site.startswith('task'):
                args = ['task', 'add', 'root', 'PROOF-1', '--title', 'Fixture task']
                if site == 'task_projection':
                    # Open additions skip unrelated projection; managed indexes retain it.
                    (root / 'source.md').write_text('Fixture source\n')
                    self.b(root, 'set', 'root', 'upstream_generator=links',
                           'upstream_inputs=source.md', 'upstream_output=index.md')
            if site == 'wake':
                self.b(root, 'set', 'root', 'coordinator=old-owner', 'wake=exit 0')
                m = core_loader.SourceFileLoader('compute_wake', str(CORE / 'bin/brain')).load_module()
                inst = m.Instance(root)
                with mock.patch.object(m, 'wake_start'):
                    m.request_wake(inst, inst.node('root'), 'fixture')
        self.b(root, 'new', 'other', '--title', 'Other')
        marker, release = self.tmp / 'computing', self.tmp / 'release'
        script = '''import sys,time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from brain import cli, instance, ui, job_attempts, verify, spend, hosts, watch, events, tasks, review, preview_evidence, handback_queue, status
site, marker, release = sys.argv[2:5]
projection = site in ('handback_projection', 'task_projection')
deferred = []
if projection:
    # Keep the real deferred drain in this instrumented child so its snapshot
    # reaches the same lock/barrier hook after the producer has returned.
    handback_queue.start_intent_drainer = deferred.append
owner, name = {'sample': (ui, 'machine_load'), 'sample_watcher': (watch, 'watch_scan'),
               'handback_binding': (job_attempts, 'attempt_binding'),
               'handback_preflight': (verify, 'record_ready'), 'handback_accounting': (spend, 'live_usage'),
               'handback_projection': (events, 'snapshot_view'),
               'handback_checked_projection': (events, 'snapshot_view'),
               'handback_publication': (job_attempts, 'attempt_matches'),
               'handback_question': (watch, 'watch_handback_wake_items'),
               'handback_blocked': (watch, 'watch_handback_wake_items'),
               'task_validation': (tasks, 'validate'), 'task_projection': (events, 'snapshot_view'),
               'receipt_review': (review, 'validate_review_receipt'),
               'receipt_preview': (preview_evidence, 'validate_preview_receipt'),
               'wake': (hosts, 'wake_snapshot')}[site]
original, fired = getattr(owner, name), False
def slow(*args, **kwargs):
    global fired
    if not fired:
        fired = True
        assert instance._LOCK['fh'] is None, site + ' computed under the instance lock'
        Path(marker).touch()
        deadline = time.monotonic() + 15
        while not Path(release).exists():
            if time.monotonic() > deadline: raise RuntimeError('compute barrier timed out')
            time.sleep(.01)
    return original(*args, **kwargs)
setattr(owner, name, slow)
hosts.wake_start = lambda *args, **kwargs: None
if site != 'sample_watcher': watch.watch_once = lambda inst, **kwargs: None
sys.argv = ['brain', *sys.argv[5:]]
cli.main()
if projection:
    assert not fired, 'producer computed a deferred projection before returning'
    assert len(deferred) == 1, 'producer did not schedule its deferred drain'
    current = deferred[0]
    node = current.node('a' if site == 'handback_projection' else 'root')
    assert events.read(current)[-1]['data'].get('deferred'), 'source commit was not deferred'
    assert status.generated_stale(node), 'deferred view was not labelled stale'
    handback_queue.drain_intents(current)
    assert fired, 'deferred drain never computed its projection'
    assert events.node_ref(node['id']) not in events.journal(instance.Instance(current.root)).state['invalid']
    assert not status.generated_stale(node), 'deferred drain left the generated view stale'
    assert events.events_view(current, node), 'deferred drain did not publish a usable view'
'''
        env = {**self.env, 'BRAIN_ROOT': str(root)}
        child = subprocess.Popen([sys.executable, '-B', '-c', script, str(CORE / 'lib'), site,
                                  str(marker), str(release), *args], env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        stale = None
        try:
            self.wait_file(marker, child)
            if site == 'task_projection':
                batch_views = {path: path.read_bytes() if path.exists() else None
                               for path in (root / 'status.md', root / 'status-archive.md')}
            writer_args = (['status', 'other', '--refresh'] if site.startswith('task') else
                           ['set', 'other', 'stack=concurrent'])
            writer = subprocess.run([sys.executable, '-B', str(CORE / 'bin/brain'), *writer_args],
                                    env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(writer.returncode, 0, writer.stdout + writer.stderr)
            self.assertIsNone(child.poll(), 'writer must finish while computation is paused')
            if site == 'task_projection':
                for path, before in batch_views.items():
                    self.assertEqual(path.read_bytes() if path.exists() else None, before,
                                     'peer refresh must leave the live batch view untouched')
                reader = core_loader.SourceFileLoader('peer_batch_read', str(CORE / 'bin/brain')).load_module()
                current = reader.Instance(root)
                journal = reader._owners['Command']
                self.assertIn(journal.node_ref('root'), journal.journal(current).state['invalid'])
                batches = [ref for ref, event in journal.replay(current)[2].items()
                           if event['data'].get('write_batch')]
                self.assertEqual(len(batches), 1)
                self.assertTrue(journal.lease_live(current, batches[0]))
            if drift and site == 'sample':
                stale = b'{"samples": [], "episode": false, "newer": true}\n'
                (root / '.brain/machine.json').write_bytes(stale)
            elif drift and site == 'wake':
                self.b(root, 'set', 'root', 'coordinator=new-owner')
            elif drift:
                self.b(root, 'set', 'a', 'verify=exit 1')
        finally:
            release.touch()
            try:
                out, err = child.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                child.kill()
                out, err = child.communicate()
                self.fail('compute worker failed to finish: ' + out + err)
        if drift and site.startswith('handback'):
            self.assertNotEqual(child.returncode, 0, out + err)
            self.assertFalse(list((root / 'nodes/a/job-handbacks').glob('*.json')))
        else:
            self.assertEqual(child.returncode, 0, out + err)
        if stale is not None:
            self.assertEqual((root / '.brain/machine.json').read_bytes(), stale)
            self.assertIn('state changed', out)
        if site == 'wake' and drift:
            directory = root / '.brain/wake/root'
            self.assertTrue(list(directory.glob('*.pending')))
            self.assertFalse(list(directory.glob('*.sent')))
            self.assertFalse((directory / 'last').exists())
        rows = json.loads(self.b(root, 'timings', '--command', args[0], '--json'))
        self.assertGreaterEqual(rows[-1]['lock_wait_s'], 0)
        self.assertGreaterEqual(rows[-1]['lock_hold_s'], 0)
        if site == 'wake':
            self.assertEqual(rows[-1]['lock_hold_s'], 0)
        print('lock-measurement: ' + json.dumps({'site': site, 'drift': drift,
              **{key: rows[-1][key] for key in ('elapsed_s', 'lock_wait_s', 'lock_hold_s', 'success')}}))
        return rows[-1]

    def test_sampler_collection_allows_writer_and_rejects_stale_publication(self):
        self.assert_compute_allows_writer('sample', drift=True)

    def test_sampler_watcher_scan_allows_writer(self):
        self.assert_compute_allows_writer('sample_watcher')

    def test_handback_binding_allows_writer(self):
        self.assert_compute_allows_writer('handback_binding')

    def test_handback_preflight_allows_writer(self):
        self.assert_compute_allows_writer('handback_preflight')

    def test_handback_accounting_allows_writer(self):
        self.assert_compute_allows_writer('handback_accounting')

    def test_handback_terminal_projection_allows_writer(self):
        self.assert_compute_allows_writer('handback_projection')

    def test_checked_handback_terminal_projection_allows_writer(self):
        self.assert_compute_allows_writer('handback_checked_projection')

    def test_handback_binding_drift_refuses_publication(self):
        self.assert_compute_allows_writer('handback_binding', drift=True)

    def test_handback_publication_binding_allows_writer_and_refuses_drift(self):
        self.assert_compute_allows_writer('handback_publication', drift=True)

    def test_question_handback_wake_scan_allows_writer(self):
        self.assert_compute_allows_writer('handback_question')

    def test_blocked_handback_wake_scan_allows_writer(self):
        self.assert_compute_allows_writer('handback_blocked')

    def test_task_add_validation_allows_writer(self):
        self.assert_compute_allows_writer('task_validation')

    def test_task_add_projection_allows_writer(self):
        self.assert_compute_allows_writer('task_projection')

    def test_review_receipt_validation_allows_writer(self):
        self.assert_compute_allows_writer('receipt_review')

    def test_preview_receipt_validation_allows_writer(self):
        self.assert_compute_allows_writer('receipt_preview')

    def test_wake_projection_allows_writer_and_rechecks_coordinator(self):
        self.assert_compute_allows_writer('wake', drift=True)

    def copy_brain(self, root):
        copy = root.with_name(root.name + "-copy")
        subprocess.run(["cp", "-R", str(root), str(copy)], check=True)
        return copy

    def start(self, root, *args):
        return subprocess.Popen([sys.executable, str(CORE / "bin/brain"), *args], cwd=root,
                                env={**self.env, "BRAIN_ROOT": str(root), "BRAIN_NO_UPDATE_CHECK": "1"},
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def wait_file(self, path, process, timeout=20):
        deadline = time.monotonic() + timeout
        while not path.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        if not path.exists() and process.poll() is not None:
            out, err = process.communicate()
            self.fail('worker never reached its slow step: ' + out + err)
        self.assertTrue(path.exists(), "worker never reached its slow step")

    def responsive(self, root):
        for args in (("status", "other", "--refresh"), ("set", "other", "stack=fixture"),
                     ("due", "other")):
            started = time.monotonic()
            p = self.start(root, *args)
            try:
                out, err = p.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                p.kill()
                p.communicate()
                self.fail("instance lock blocked " + " ".join(args))
            self.assertEqual(p.returncode, 0, out + err)
            self.assertLess(time.monotonic() - started, 20)

    def test_ten_second_verify_leaves_other_commands_responsive(self):
        r, home, wt = self.job()
        r = self.copy_brain(r)
        self.b(r, "new", "other", "--title", "Other")
        marker = self.tmp / "verifying"
        code = "from pathlib import Path; import time; Path(" + repr(str(marker)) + ").touch(); time.sleep(10)"
        command = shlex.quote(sys.executable) + " -c " + shlex.quote(code)
        self.b(r, "set", "a", "verify=" + command)
        p = self.start(r, "handback", "--summary", "Synthetic result", "a", "--branch", "job", "--state", "done", "--reviews", "1",
                       "--lessons", "none: fixture")
        try:
            self.wait_file(marker, p)
            self.responsive(r)
            out, err = p.communicate(timeout=30)
            self.assertEqual(p.returncode, 0, out + err)
            packet = json.loads(next((r / "nodes/a/job-handbacks").glob("*.json")).read_text())["ready"]
            self.assertEqual(packet["verify_result"], 0)
            self.assertEqual(packet["head"], self.git("rev-parse", "HEAD", cwd=wt).strip())
        finally:
            if p.poll() is None:
                p.kill()
                p.communicate()

    def test_concurrent_head_or_job_replacement_during_verify_is_refused(self):
        for mutation in ("head", "job"):
            with self.subTest(mutation=mutation):
                # Each case gets an independent job and instance.
                case = _test_verify.WorkerReadyPackets(methodName="runTest")
                case.setUp()
                try:
                    r, home, wt = case.job()
                    marker, release = (case.tmp / name for name in ("started", "release"))
                    script = case.tmp / "verify.py"
                    script.write_text("from pathlib import Path\nimport time\nPath(" + repr(str(marker)) +
                                      ").touch()\nwhile not Path(" + repr(str(release)) +
                                      ").exists(): time.sleep(.02)\n")
                    case.b(r, "set", "a", "verify=" + shlex.quote(sys.executable) + " " + shlex.quote(str(script)))
                    p = subprocess.Popen([sys.executable, str(CORE / "bin/brain"), "handback", "--summary", "Synthetic result", "a", "--branch", "job",
                                          "--state", "done", "--reviews", "1"], cwd=r,
                                         env={**case.env, "BRAIN_ROOT": str(r), "BRAIN_NO_UPDATE_CHECK": "1"},
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    try:
                        self.wait_file(marker, p)
                        if mutation == "head":
                            case.git("commit", "--allow-empty", "-qm", "concurrent fixture", cwd=wt)
                        else:
                            m = core_loader.SourceFileLoader("brain_changed_job", str(CORE / "bin/brain")).load_module()
                            inst = m.Instance(r)
                            with mock.patch.dict(os.environ, case.env, clear=True), m.brain_lock(inst):
                                n = inst.node("a")
                                rows = m.read_jobs(n)
                                rows[-1]["Started"] = "2020-01-01T00:00:00+00:00"
                                m.write_jobs(inst, n, rows)
                                inst.commit([], "replace fixture job")
                        release.touch()
                        out, err = p.communicate(timeout=20)
                        self.assertNotEqual(p.returncode, 0, out + err)
                        self.assertIn("verify changed the job's branch" if mutation == "head" else
                                      "job changed during verification", out + err)
                        packets = [json.loads(f.read_text()) for f in (r / "nodes/a/job-handbacks").glob("*.json")]
                        self.assertFalse(any(x["ready"].get("verify_result") == 0
                                             for x in packets if isinstance(x.get("ready"), dict)))
                    finally:
                        release.touch()
                        if p.poll() is None:
                            p.kill()
                            p.communicate()
                finally:
                    case.tearDown()

    def test_sleeping_drain_and_second_drain_never_block_instance(self):
        r = self.copy_brain(self.init("fixture"))
        self.b(r, "new", "a", "--title", "Alpha")
        self.b(r, "new", "other", "--title", "Other")
        calls, started, release = (self.tmp / name for name in ("calls", "sending", "release"))
        script = self.tmp / "transport.py"
        script.write_text("from pathlib import Path\nimport sys,time\n"
                          "Path(" + repr(str(started)) + ").touch()\n"
                          "while not Path(" + repr(str(release)) + ").exists(): time.sleep(.02)\n"
                          "with open(" + repr(str(calls)) + ", 'a') as f: f.write(sys.stdin.read()+'\\n')\n")
        self.b(r, "set", "a", "coordinator=fixture-thread",
               "wake=" + shlex.quote(sys.executable) + " " + shlex.quote(str(script)))
        m = core_loader.SourceFileLoader("brain_lock_wake", str(CORE / "bin/brain")).load_module()
        inst = m.Instance(r)
        with mock.patch.object(m, "wake_start"):
            m.request_wake(inst, inst.node("a"), "first")
            m.request_wake(inst, inst.node("a"), "second")
        directory = m.wake_directory(inst, inst.node("a"))
        p = self.start(r, "wake", "a", "--drain")
        try:
            self.wait_file(started, p)
            self.responsive(r)
            second = self.start(r, "wake", "a", "--drain")
            try:
                out, err = second.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                second.kill()
                second.communicate()
                self.fail("second drain waited for the delivery lock")
            self.assertEqual(second.returncode, 0, out + err)
            release.touch()
            out, err = p.communicate(timeout=20)
            self.assertEqual(p.returncode, 0, out + err)
            self.assertEqual(len(calls.read_text().splitlines()), 1)
            self.assertTrue(json.loads(calls.read_text())['reason'])
            self.assertEqual(json.loads(calls.read_text())['event'], 'brain-wake')
            self.assertEqual(set(json.loads(calls.read_text())), {'event', 'reason'})
            self.assertRegex(json.loads(calls.read_text())['reason'],
                             r'^new request [0-9a-f]{64}(; new request [0-9a-f]{64})*$')
            self.assertEqual(len(list(directory.glob("*.sent"))), 2)
            self.assertFalse(list(directory.glob("*.pending")))
        finally:
            release.touch()
            if p.poll() is None:
                p.kill()
                p.communicate()

    def test_rate_wait_leaves_instance_responsive(self):
        import fcntl
        r = self.copy_brain(self.init("fixture"))
        self.b(r, "new", "other", "--title", "Other")
        self.b(r, "set", "root", "wake=exit 0")
        directory = r / ".brain/wake/root"
        directory.mkdir(parents=True)
        (directory / "reason.pending").touch()
        (directory / "last").write_text(str(time.time()))
        def held(name):
            # Lock holders aren't recorded; probe each flock instead.
            with (directory / name).open("a+") as stream:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
                return False
        def wait(condition, message):
            deadline = time.monotonic() + 20
            while not condition():
                self.assertLess(time.monotonic(), deadline, message)
                time.sleep(.02)
        try:
            for _ in range(2):  # a closed window ends every drain; one timer covers the opening
                p = self.start(r, "wake", "root", "--drain")
                out, err = p.communicate(timeout=15)
                self.assertEqual(p.returncode, 0, out + err)
                wait(lambda: held("retry.lock"), "drain scheduled no timer for the window opening")
                self.assertFalse(held("lock"), "the timer must not hold the delivery lock")
                self.assertFalse((directory / "reason.sent").exists())
            self.responsive(r)
            (directory / "last").write_text(str(time.time() - 121))
            wait(lambda: (directory / "reason.sent").exists(), "the timer did not deliver at the opening")
            # Requests published after that delivery wait for the next opening under a new timer.
            wait(lambda: held("retry.lock") or not list(directory.glob("*.pending")),
                 "a request remained without a timer")
        finally:
            (directory / "last").write_text(str(time.time() - 121))  # release any surviving timer

    def test_binding_publication_failure_preserves_readable_prior_state(self):
        r = self.init("fixture")
        m = core_loader.SourceFileLoader("brain_lock_bindings", str(CORE / "bin/brain")).load_module()
        inst = m.Instance(r)
        inst.save_local()
        prior = inst.local_path.read_bytes()
        inst.local_of("root")["wake"] = "exit 0"
        with mock.patch.object(m.os, "fsync", side_effect=OSError("fixture disk error")):
            with self.assertRaises(OSError):
                inst.save_local()
        self.assertEqual(inst.local_path.read_bytes(), prior)
        self.assertEqual(m.Instance(r).local, json.loads(prior))
        inst.save_local()
        self.assertEqual(m.Instance(r).local_of("root")["wake"], "exit 0")

    def test_request_does_not_wait_for_either_lock(self):
        import fcntl
        r = self.init("fixture")
        self.b(r, "set", "root", "wake=exit 0")
        directory = r / ".brain/wake/root"
        directory.mkdir(parents=True)
        with (r / ".brain/lock").open("a+") as instance, (directory / "lock").open("a+") as node:
            fcntl.flock(instance, fcntl.LOCK_EX)
            fcntl.flock(node, fcntl.LOCK_EX)
            p = self.start(r, "wake", "root", "fixture reason")
            try:
                out, err = p.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                p.kill()
                p.communicate()
                self.fail("request waited for a lock")
            self.assertEqual(p.returncode, 0, out + err)
            self.assertEqual(len(list(directory.glob("*.pending"))), 1)


class QuestionWake(Scenario):
    fixture_templates = True
    # Initialization precedes transport mocks and creates only fixture state.
    # Keep all asserted CLI, HTTP, child-process and wake-launcher calls real.
    fixture_init_in_process = True

    def test_scoped_routes_reuse_inventory_and_refresh_in_memory_binding(self):
        inst, m = self.inst, self.module
        with inst.node_observation(), \
                mock.patch.object(inst, '_discover_nodes', wraps=inst._discover_nodes) as discover:
            for _ in range(5):
                node = inst.node('a')
                self.assertEqual(m.wake_target(inst, node)[0]['id'], 'a')
            self.assertEqual(discover.call_count, 1)
            inst.local_of('a').pop('coordinator')
            self.assertEqual(m.wake_target(inst, inst.node('a'))[0]['id'], 'root')
            self.assertEqual(discover.call_count, 2)
        with mock.patch.object(inst, '_discover_nodes', wraps=inst._discover_nodes) as discover:
            m.wake_configured_route(inst, inst.node('a'))
            m.wake_configured_route(inst, inst.node('a'))
            self.assertGreaterEqual(discover.call_count, 2)

    def setUp(self):
        super().setUp()
        environment = mock.patch.dict(os.environ, self.env, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.module = core_loader.SourceFileLoader("brain_wake_test", str(CORE / "bin/brain")).load_module()
        self.r, self.calls, self.command = self.cached_setup('wake', self.wake_fixture)
        self.inst = self.module.Instance(self.r)
        self.n = self.inst.node("a")

    def wake_fixture(self):
        self.r = self.init("alice")
        self.b(self.r, "new", "a", "--title", "Alpha")
        self.calls = self.tmp / "calls"
        script = self.tmp / "fake-wake.py"
        script.write_text("import sys\nfrom pathlib import Path\np=Path(" + repr(str(self.calls)) +
                          ")\nwith p.open('a') as f: f.write(sys.stdin.read()+'\\n')\n")
        self.command = shlex_quote(sys.executable) + " " + shlex_quote(str(script))
        self.b(self.r, "set", "a", "coordinator=synthetic-thread", "wake=" + self.command)
        return self.r, self.calls, self.command

    def enqueue(self, reason, node=None):
        with mock.patch.object(self.module, "wake_start") as start:
            self.module.request_wake(self.inst, node or self.n, reason)
        return start.call_count

    def drain(self, node=None):
        self.module.wake_drain(self.inst, node or self.n, once=True)

    @contextlib.contextmanager
    def inline_timer(self):
        """Run the detached timer and the drains it starts in-process, recording each."""
        calls = types.SimpleNamespace(schedules=[], starts=[])
        def schedule(inst, node):
            calls.schedules.append(node["id"])
            self.module.wake_retry(inst, node)
        def start(inst, node):
            calls.starts.append(node["id"])
            self.module.wake_drain(inst, node)
        with mock.patch.object(self.module, "wake_schedule", side_effect=schedule), \
                mock.patch.object(self.module, "wake_start", side_effect=start):
            yield calls

    def test_dedup_rate_window_and_delayed_delivery(self):
        self.assertEqual(self.enqueue("first question"), 1)
        self.drain()
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)
        self.assertTrue(json.loads(self.calls.read_text())['reason'])
        self.assertEqual(json.loads(self.calls.read_text())['event'], 'brain-wake')
        self.assertEqual(set(json.loads(self.calls.read_text())), {'event', 'reason'})
        self.assertRegex(json.loads(self.calls.read_text())['reason'], r'^new request [0-9a-f]{64}$')
        self.assertEqual(self.enqueue("first question"), 0)
        self.assertEqual(self.enqueue("second question"), 1)
        self.drain()
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)
        directory = self.module.wake_directory(self.inst, self.n)
        (directory / "last").write_text(str(time.time() - 121))
        self.drain()
        self.assertEqual(len(self.calls.read_text().splitlines()), 2)
        state = "".join(p.read_text() for p in directory.iterdir() if p.is_file())
        self.assertNotIn("question", state)

    def test_detached_worker_delivers_held_reason_without_a_tick(self):
        self.enqueue("first")
        self.drain()
        self.enqueue("second")
        directory = self.module.wake_directory(self.inst, self.n)
        (directory / "last").write_text(str(time.time()))
        sleeps = []
        clock = types.SimpleNamespace(monotonic=time.monotonic, time=time.time,
                                      sleep=lambda seconds: (sleeps.append(seconds), time.sleep(seconds)))
        with mock.patch.object(self.module, "WAKE_INTERVAL", 0.15), mock.patch.object(self.module, "WAKE_CLOCK", clock), \
                mock.patch.object(self.module, "wake_schedule") as schedule:
            start = time.monotonic()
            self.module.wake_drain(self.inst, self.n)
            self.assertLess(time.monotonic() - start, 0.14, "a closed window must end the drain, not wait")
            self.assertEqual(sleeps, [])
            self.assertEqual(schedule.call_count, 1)
            self.assertEqual(len(self.calls.read_text().splitlines()), 1)
            self.assertEqual(len(list(directory.glob("*.pending"))), 1)
            with self.inline_timer() as calls:
                start = time.monotonic()
                self.module.wake_retry(self.inst, self.n)
        self.assertGreaterEqual(time.monotonic() - start, 0.14)
        self.assertTrue(sleeps)
        self.assertEqual(calls.starts, ["a"])
        self.assertEqual(calls.schedules, [])
        self.assertEqual(len(self.calls.read_text().splitlines()), 2)
        self.assertFalse(list(directory.glob("*.pending")))

    def test_closed_window_exits_before_pending_revalidation(self):
        self.enqueue("closed window")
        directory = self.module.wake_directory(self.inst, self.n)
        (directory / "last").write_text("1000")
        clock = types.SimpleNamespace(time=lambda: 1001, monotonic=lambda: 1,
                                      sleep=mock.Mock(side_effect=AssertionError("drain slept")))
        with mock.patch.object(self.module, "WAKE_CLOCK", clock), \
                mock.patch.object(self.module, "wake_actionable_pending",
                                  side_effect=AssertionError("closed window revalidated pending")), \
                mock.patch.object(self.module, "wake_snapshot",
                                  side_effect=AssertionError("closed window captured snapshot")), \
                mock.patch.object(self.module, "wake_schedule") as schedule:
            self.module.wake_drain(self.inst, self.n)
        schedule.assert_called_once_with(self.inst, self.n)
        clock.sleep.assert_not_called()
        self.assertEqual(len(list(directory.glob("*.pending"))), 1)
        self.assertFalse(self.calls.exists())

    def test_failed_and_hanging_transport_leave_pending(self):
        self.enqueue("failure")
        directory = self.module.wake_directory(self.inst, self.n)
        for command in ("exit 1", shlex_quote(sys.executable) + " -c 'import time; time.sleep(20)'"):
            self.b(self.r, "set", "a", "wake=" + command)
            (directory / "last").unlink(missing_ok=True)
            start = time.monotonic()
            self.drain()
            self.assertLess(time.monotonic() - start, 11)
            self.assertEqual(len(list(directory.glob("*.pending"))), 1)
            self.assertFalse(list(directory.glob("*.sent")))

    def test_fallback_and_routing_up(self):
        self.b(self.r, "set", "root", "wake=" + self.command)
        self.inst = self.module.Instance(self.r)
        root = self.inst.node("root")
        self.assertEqual(self.enqueue("root reason", root), 1)
        self.drain(root)
        self.b(self.r, "set", "a", "coordinator=")
        self.inst = self.module.Instance(self.r)
        self.assertEqual(self.enqueue("child reason", self.inst.node("a")), 1)
        directory = self.module.wake_directory(self.inst, root)
        self.assertEqual(len(list(directory.glob("*.pending"))), 1)
        self.b(self.r, "set", "root", "wake=", "wake_default=" + self.command)
        self.inst = self.module.Instance(self.r)
        self.assertEqual(self.module.wake_target(self.inst, root)[1], self.command)
        node_text = (self.r / "node.md").read_text() + (self.r / "nodes/a/node.md").read_text()
        self.assertNotIn(self.command, node_text)

    def test_url_transport_uses_constant_body_timeout_and_no_redirect(self):
        import urllib.request
        response = mock.MagicMock()
        response.__enter__.return_value.status = 204
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(urllib.request, "build_opener", return_value=opener) as build:
            self.assertTrue(self.module.wake_send("https://wake.invalid/synthetic-capability"))
        request = opener.open.call_args.args[0]
        self.assertEqual(json.loads(request.data), {'event': 'brain-wake',
                         'reason': 'new request ' + self.module.wake_binding('https://wake.invalid/synthetic-capability')})
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 10)
        handler = build.call_args.args[0]()
        self.assertIsNone(handler.redirect_request(None, None, 302, "redirect", {}, "https://other.invalid/"))

    @contextlib.contextmanager
    def webhook(self, status=202, redirect=False):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(handler):
                body = handler.rfile.read(int(handler.headers.get("Content-Length", "0")))
                calls.append((handler.path, handler.headers.get("User-Agent"), body))
                # urllib supplies Python-urllib by default, which the relay also refuses.
                response = status() if callable(status) else status
                code = response if handler.headers.get("User-Agent") == "brain-wake/1" else 403
                handler.send_response(code)
                if redirect:
                    handler.send_header("Location", "/must-not-follow")
                handler.end_headers()
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/synthetic-capability", calls
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    def bind_webhook(self, url):
        self.b(self.r, "set", "a", "wake=" + url)
        self.inst = self.module.Instance(self.r)
        self.n = self.inst.node("a")

    def due_without_drain(self):
        with mock.patch.object(self.module, "wake_start"), mock.patch.object(self.module, "load", return_value=self.inst):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.module.cmd_due(argparse.Namespace(id="a", quiet=False, json=True, machine=False))
        return output.getvalue()

    def test_real_webhook_drain_requires_explicit_user_agent(self):
        with self.webhook() as (url, calls):
            self.bind_webhook(url)
            self.enqueue("made-up question")
            self.drain()
            directory = self.module.wake_directory(self.inst, self.n)
            self.assertFalse(list(directory.glob("*.pending")))
            self.assertEqual(len(list(directory.glob("*.sent"))), 1)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][:2], ("/synthetic-capability", "brain-wake/1"))
            payload = json.loads(calls[0][2])
            self.assertEqual(set(payload), {'event', 'reason'})
            self.assertEqual(payload['event'], 'brain-wake')
            self.assertRegex(payload['reason'], r'^new request [0-9a-f]{64}$')

    def test_refused_drain_stays_pending_and_due_reports_code_once(self):
        with self.webhook(403) as (url, calls):
            self.bind_webhook(url)
            self.enqueue("made-up question")
            self.drain()
            directory = self.module.wake_directory(self.inst, self.n)
            self.assertEqual(len(list(directory.glob("*.pending"))), 1)
            due = self.due_without_drain()
            self.assertEqual(due.count("HTTP 403"), 1)
            self.assertNotIn(url, due)
            state = "".join(p.read_text() for p in directory.iterdir() if p.is_file())
            self.assertNotIn(url, state)
            self.assertNotIn("made-up question", state)
            self.assertEqual((directory / "delivery.json").stat().st_mode & 0o777, 0o600)

    def test_check_cached_only_until_explicit_live_even_when_stale(self):
        with self.webhook(403) as (url, calls):
            self.bind_webhook(url)
            first = self.b(self.r, "check")
            self.assertIn("not probed; run brain check --live", first)
            self.assertEqual(calls, [])
            directory = self.module.wake_directory(self.inst, self.n)
            self.module.wake_record(directory, url, 202)
            cached = self.b(self.r, "check")
            self.assertIn("wake probe HTTP 202", cached)
            self.assertEqual(calls, [])
            name = "probe-" + self.module.wake_binding(url) + ".json"
            self.module.wake_record(directory, url, 403, name)
            path = directory / name
            value = json.loads(path.read_text())
            value['at'] -= 86401
            path.write_text(json.dumps(value))
            stale = self.b(self.r, "check", ok=False)
            self.assertIn("wake probe HTTP 403", stale)
            self.assertIn("result age 864", stale)
            self.assertEqual(calls, [])
            self.b(self.r, "check", "--live", ok=False)
            self.assertEqual(len(calls), 1)

    def test_check_real_probe_caches_for_day_force_reprobes_and_due_agrees(self):
        with self.webhook(403) as (url, calls):
            self.bind_webhook(url)
            first = self.b(self.r, "check", "--live", ok=False)
            self.assertIn("wake probe HTTP 403", first)
            self.assertIn("result age", first)
            self.assertNotIn(url, first)
            self.assertEqual(len(calls), 1)
            second = self.b(self.r, "check", ok=False)
            self.assertIn("wake probe HTTP 403", second)
            self.assertEqual(len(calls), 1)
            self.b(self.r, "check", "--live", ok=False)
            self.assertEqual(len(calls), 2)
            self.assertEqual(self.due_without_drain().count("HTTP 403"), 1)
            directory = self.module.wake_directory(self.inst, self.n)
            cache = next(directory.glob("probe-*.json"))
            record = json.loads(cache.read_text())
            record["at"] -= 86401
            cache.write_text(json.dumps(record))
            self.b(self.r, "check", ok=False)
            self.assertEqual(len(calls), 2)  # even an expired result is read-only by default
            self.b(self.r, "check", "--live", ok=False)
            self.assertEqual(len(calls), 3)
        with self.webhook() as (url, calls):
            self.bind_webhook(url)
            success = self.b(self.r, "check", "--live")
            self.assertIn("wake probe HTTP 202", success)
            self.assertEqual(len(calls), 1)
            self.assertNotIn("last wake delivery", self.due_without_drain())

    def test_cached_success_does_not_hide_new_drain_refusal(self):
        with self.webhook() as (url, calls):
            self.bind_webhook(url)
            self.b(self.r, "check", "--live")
            directory = self.module.wake_directory(self.inst, self.n)
            self.module.wake_record(directory, url, 403)
            check = self.b(self.r, "check", ok=False)
            self.assertEqual(check.count("HTTP 403"), 1)
            self.assertEqual(len(calls), 1)

    def test_probe_rejects_redirects_and_server_errors_without_url(self):
        for code in (302, 503):
            with self.webhook(code, redirect=True) as (url, calls):
                self.bind_webhook(url)
                self.assertEqual(self.module.wake_probe(self.inst, self.n), code)
                self.assertEqual(len(calls), 1)
                self.assertEqual(self.due_without_drain().count("HTTP " + str(code)), 1)

    def test_check_redirect_is_a_problem_for_probe_and_drain(self):
        with self.webhook(302, redirect=True) as (url, calls):
            self.bind_webhook(url)
            check = self.b(self.r, "check", "--live", ok=False)
            self.assertIn("✗ a: wake probe HTTP 302", check)
            self.assertEqual(len(calls), 1)
            self.assertNotIn(url, check)
            directory = self.module.wake_directory(self.inst, self.n)
            self.module.wake_record(directory, url, 302)
            name = "probe-" + self.module.wake_binding(url) + ".json"
            self.module.wake_record(directory, url, 202, name)
            check = self.b(self.r, "check", ok=False)
            self.assertIn("✗ a: last wake delivery refused: HTTP 302", check)
            self.assertEqual(len(calls), 1)

    def test_probe_transport_error_warns_is_safe_and_not_cached(self):
        self.bind_webhook("http://127.0.0.1:1/synthetic-secret")
        check = self.b(self.r, "check", "--live")
        self.assertIn("! a: wake probe transport error (not cached)", check)
        self.assertNotIn("synthetic-secret", check)
        directory = self.module.wake_directory(self.inst, self.n)
        self.assertFalse(list(directory.glob("probe-*.json")))
        with mock.patch.object(self.module, "wake_http_status", return_value=202) as send:
            self.assertEqual(self.module.wake_probe(self.inst, self.n), 202)
            send.assert_called_once()

    def test_check_fix_never_sends_even_when_live_or_cache_expired(self):
        with self.webhook() as (url, calls):
            self.bind_webhook(url)
            check = self.b(self.r, "check", "--fix", "--live")
            self.assertIn("not probed; run brain check", check)
            self.assertEqual(calls, [])
            directory = self.module.wake_directory(self.inst, self.n)
            name = "probe-" + self.module.wake_binding(url) + ".json"
            self.module.wake_record(directory, url, 202, name)
            cache = directory / name
            record = json.loads(cache.read_text())
            record["at"] -= 86401
            cache.write_text(json.dumps(record))
            check = self.b(self.r, "check", "--fix", "--live")
            self.assertIn("wake probe HTTP 202", check)
            self.assertIn("result age 864", check)
            self.assertEqual(calls, [])
            with mock.patch.object(self.module, "load", return_value=self.inst), \
                    mock.patch.object(self.module, "wake_http_status") as send, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.module.cmd_check(argparse.Namespace(fix=True, live=True))
            send.assert_not_called()

    def test_probe_success_preserves_real_drain_refusal_for_due(self):
        response = [403]
        with self.webhook(lambda: response[0]) as (url, calls):
            self.bind_webhook(url)
            self.enqueue("made-up reason")
            self.drain()
            response[0] = 202
            self.assertEqual(self.module.wake_probe(self.inst, self.n, force=True), 202)
            self.assertEqual(len(calls), 2)
            self.assertEqual(self.due_without_drain().count("HTTP 403"), 1)
            directory = self.module.wake_directory(self.inst, self.n)
            self.assertEqual(json.loads((directory / "delivery.json").read_text())["status"], 403)

    def test_diagnostic_write_failure_cannot_requeue_successful_delivery(self):
        with self.webhook() as (url, calls):
            self.bind_webhook(url)
            self.enqueue("made-up reason")
            with mock.patch.object(self.module, "wake_record", side_effect=OSError("synthetic failure")):
                self.drain()
            directory = self.module.wake_directory(self.inst, self.n)
            self.assertFalse(list(directory.glob("*.pending")))
            self.assertEqual(len(list(directory.glob("*.sent"))), 1)
            self.drain()
            self.assertEqual(len(calls), 1)

    def test_slow_transport_succeeds_and_expired_dedup_rearms(self):
        script = "import time; time.sleep(0.6); " + "open(" + repr(str(self.calls)) + ", 'a').write('called\\n')"
        self.b(self.r, "set", "a", "wake=" + shlex_quote(sys.executable) + " -c " + shlex_quote(script))
        self.enqueue("same explicit reason")
        self.drain()
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)
        directory = self.module.wake_directory(self.inst, self.n)
        sent = next(directory.glob("*.sent"))
        os.utime(sent, (time.time() - 86401, time.time() - 86401))
        (directory / "last").write_text(str(time.time() - 121))
        self.assertEqual(self.enqueue("same explicit reason"), 1)
        self.drain()
        self.assertEqual(len(self.calls.read_text().splitlines()), 2)

    def test_delivery_racing_with_pending_creation_is_deduplicated(self):
        original = self.module.atomic_write
        def publish_delivery(path, *args, **kwargs):
            if str(path).endswith(".pending"):
                Path(path).with_suffix(".sent").write_text("delivered\n")
            return original(path, *args, **kwargs)
        with mock.patch.object(self.module, "atomic_write", side_effect=publish_delivery):
            self.enqueue("racing reason")
        self.drain()
        self.assertFalse(self.calls.exists())
        self.assertFalse(list(self.module.wake_directory(self.inst, self.n).glob("*.pending")))

    def test_pending_wake_follows_coordinator_removal_to_root(self):
        self.enqueue("routed later")
        self.b(self.r, "set", "root", "wake=" + self.command)
        self.b(self.r, "set", "a", "coordinator=")
        with mock.patch.object(self.module, "wake_start") as start:
            self.drain()
        self.assertEqual(start.call_args.args[1]["id"], "root")
        self.inst = self.module.Instance(self.r)
        self.drain(self.inst.node("root"))
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)
        self.assertFalse(list(self.module.wake_directory(self.inst, self.n).glob("*.pending")))

    def test_clock_rollback_defers_for_one_rate_window(self):
        self.enqueue("clock rollback")
        directory = self.module.wake_directory(self.inst, self.n)
        (directory / "last").write_text(str(time.time() + 86400))
        with mock.patch.object(self.module, "WAKE_INTERVAL", 0.1), self.inline_timer() as calls:
            self.module.wake_drain(self.inst, self.n)
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)
        self.assertEqual((calls.schedules, calls.starts), (["a"], ["a"]))

    def test_timer_follows_rollback_without_repair_and_bounds_its_wait(self):
        self.enqueue("timer rollback")
        directory = self.module.wake_directory(self.inst, self.n)
        now, slept = [1000.0], []
        def advance(seconds):
            slept.append(seconds)
            now[0] += seconds
        clock = types.SimpleNamespace(monotonic=lambda: now[0], time=lambda: now[0], sleep=advance)
        (directory / "last").write_text(str(now[0] + 86400))
        with mock.patch.object(self.module, "WAKE_CLOCK", clock), \
                mock.patch.object(self.module, "wake_start") as start:
            self.module.wake_retry(self.inst, self.n)
        self.assertEqual(start.call_count, 1)
        self.assertTrue(all(0 < s <= 5 for s in slept), slept)
        self.assertAlmostEqual(sum(slept), 120)  # one window at most, as the drain bounds it
        self.assertEqual((directory / "last").read_text(), str(1000.0 + 86400), "only the drain repairs the window")

    def test_legacy_request_is_delivered_under_its_opaque_key_without_a_scan(self):
        self.enqueue("structured")
        directory = self.module.wake_directory(self.inst, self.n)
        (directory / ("f" * 64 + ".pending")).write_text("")  # empty legacy metadata
        with mock.patch.object(self.module, "watch_groups") as scan:
            self.drain()
        scan.assert_not_called()
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)
        self.assertFalse(list(directory.glob("*.pending")))
        self.assertTrue((directory / ("f" * 64 + ".sent")).exists())

    def test_delivery_acknowledges_only_its_refs_and_runs_no_watch_scan(self):
        delivered = {'id': 'd' * 64, 'kind': 'inbox'}
        visible = {'id': 'e' * 64, 'kind': 'inbox', 'node': 'a', 'text': 'fixture need'}
        with mock.patch.object(self.module, "wake_start"):
            self.module.request_wake(self.inst, self.n, "delivered", items=[delivered], explicit=True)
        with mock.patch.object(self.module, "watch_groups",
                               return_value={'a': {'node': self.n, 'items': [visible]}}) as scan:
            self.drain()
        scan.assert_not_called()
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)
        shown = self.module.wake_shown(self.inst, self.n)
        self.assertIn(delivered['id'], shown)
        self.assertNotIn(visible['id'], shown)  # undelivered: it may only wake later, never hide
        history = self.module.wake_history(self.module.wake_directory(self.inst, self.n))
        self.assertEqual(history[-1]['snapshot'], [delivered])

    def test_second_timer_exits_while_one_covers_the_opening(self):
        import fcntl
        self.enqueue("covered")
        directory = self.module.wake_directory(self.inst, self.n)
        (directory / "last").write_text(str(time.time()))
        with (directory / "retry.lock").open("a+") as stream, \
                mock.patch.object(self.module, "wake_start") as start, \
                mock.patch.object(self.module.WAKE_CLOCK, "sleep") as sleep:
            fcntl.flock(stream, fcntl.LOCK_EX)
            self.module.wake_retry(self.inst, self.n)
        start.assert_not_called()
        sleep.assert_not_called()

    def test_wake_start_drops_parent_instance_lock_capability(self):
        with mock.patch.dict(os.environ, {"BRAIN_LOCK_HELD": "fixture|123", "FIXTURE_SETTING": "kept"}, clear=True), \
                mock.patch.object(self.module.subprocess, "Popen") as popen:
            self.module.wake_start(self.inst, self.n)
            env = popen.call_args.kwargs["env"]
            self.assertFalse("BRAIN_LOCK_HELD" in env)
            self.assertEqual(env["FIXTURE_SETTING"], "kept")
            self.assertEqual(env["BRAIN_ROOT"], str(self.r))
            self.assertEqual(env["BRAIN_NO_REEXEC"], "1")
            self.assertEqual(os.environ["BRAIN_LOCK_HELD"], "fixture|123")

    def test_reason_arriving_during_each_send_is_delivered_at_each_opening(self):
        self.enqueue("first")
        now, sends, held = [1000.0], [], []
        def advance(seconds):
            now[0] += seconds
        def send(command, reason=None):
            time.sleep(.001)  # An unrelated stdlib poll must not advance the wake clock.
            sends.append(now[0])
            if len(sends) <= 5:
                before = len(calls.starts)
                self.module.request_wake(self.inst, self.n, "during-send-" + str(len(sends)))
                held.append(len(calls.starts) - before)  # inline newcomer really conflicts with LOCK_NB
            return True
        clock = types.SimpleNamespace(monotonic=lambda: now[0], time=lambda: now[0], sleep=advance)
        with mock.patch.object(self.module, "WAKE_CLOCK", clock), \
                mock.patch.object(self.module, "wake_send", side_effect=send), self.inline_timer() as calls:
            self.module.wake_drain(self.inst, self.n)
        directory = self.module.wake_directory(self.inst, self.n)
        self.assertFalse(list(directory.glob("*.pending")))
        self.assertEqual(sends, [1000.0 + i * 120 for i in range(6)])
        self.assertEqual(held, [1] * 5)  # each newcomer lost LOCK_NB to the sending drain
        self.assertEqual(len(calls.schedules), 5)  # one post-unlock timer per closed window
        self.assertEqual(len(calls.starts), 10)
        self.assertEqual(len(list(directory.glob("*.sent"))), 6)
        self.assertFalse((directory / "retry.lock").exists() and self.retry_held(directory))

    def retry_held(self, directory):
        import fcntl
        with (directory / "retry.lock").open("a+") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            return False

    def test_failed_attempts_do_not_respawn_at_deadline(self):
        self.enqueue("failure")
        now = [1000.0]
        def advance(seconds):
            now[0] += seconds
        with mock.patch.object(self.module.time, "monotonic", side_effect=lambda: now[0]), \
                mock.patch.object(self.module.time, "time", side_effect=lambda: now[0]), \
                mock.patch.object(self.module.time, "sleep", side_effect=advance), \
                mock.patch.object(self.module, "wake_send", return_value=False) as send, \
                mock.patch.object(self.module, "wake_start") as start, \
                mock.patch.object(self.module, "wake_schedule") as schedule:
            self.module.wake_drain(self.inst, self.n)
        # The drain no longer waits out a window; the watcher's next start retries the failure.
        self.assertEqual(send.call_count, 1)
        start.assert_not_called()
        schedule.assert_not_called()
        directory = self.module.wake_directory(self.inst, self.n)
        self.assertEqual(len(list(directory.glob("*.pending"))), 1)
        self.assertFalse(list(directory.glob("*.sent")))
        # A fresh start inside the window schedules the opening; the timer's drain retries.
        with mock.patch.object(self.module.time, "monotonic", side_effect=lambda: now[0]), \
                mock.patch.object(self.module.time, "time", side_effect=lambda: now[0]), \
                mock.patch.object(self.module.time, "sleep", side_effect=advance), \
                mock.patch.object(self.module, "wake_send", return_value=True) as send, \
                self.inline_timer() as calls:
            now[0] += 60
            self.module.wake_drain(self.inst, self.n)
        self.assertEqual(send.call_count, 1)
        self.assertEqual((calls.schedules, calls.starts), (["a"], ["a"]))
        self.assertEqual(now[0], 1120.0)
        self.assertFalse(list(directory.glob("*.pending")))

    def test_drain_error_leaves_pending_without_spawning_replacement(self):
        self.enqueue("retry later")
        with mock.patch.object(self.module, "Instance", side_effect=OSError("fixture unavailable")), \
                mock.patch.object(self.module, "wake_start") as start:
            with self.assertRaises(OSError):
                self.module.wake_drain(self.inst, self.n)
        start.assert_not_called()
        directory = self.module.wake_directory(self.inst, self.n)
        self.assertEqual(len(list(directory.glob("*.pending"))), 1)

    def test_retarget_hands_off_reason_published_after_old_folder_scan(self):
        self.enqueue("first")
        self.b(self.r, "set", "root", "wake=" + self.command)
        self.b(self.r, "set", "a", "coordinator=")
        directory = self.module.wake_directory(self.inst, self.n)
        replace, injected = Path.replace, []
        def move(path, destination):
            result = replace(path, destination)
            if path.parent == directory and not injected:
                injected.append(True)
                # The original request snapshot can still route to the old coordinator.
                self.module.request_wake(self.inst, self.n, "during-retarget")
            return result
        def start(inst, node):
            self.module.wake_drain(inst, node)
        with mock.patch.object(Path, "replace", autospec=True, side_effect=move), \
                mock.patch.object(self.module, "wake_start", side_effect=start), \
                mock.patch.object(self.module, "wake_send", return_value=True) as send, \
                mock.patch.object(self.module, "WAKE_INTERVAL", 0):
            self.module.wake_drain(self.inst, self.n)
        root_directory = self.module.wake_directory(self.inst, self.inst.node("root"))
        self.assertEqual(injected, [True])
        self.assertFalse(list(directory.glob("*.pending")))
        self.assertFalse(list(root_directory.glob("*.pending")))
        self.assertEqual(len(list(root_directory.glob("*.sent"))), 2)
        self.assertEqual(send.call_count, 2)

    def test_second_drain_exits_while_node_delivery_lock_is_held(self):
        import fcntl
        self.enqueue("held")
        directory = self.module.wake_directory(self.inst, self.n)
        with (directory / "lock").open("a+") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            p = subprocess.Popen([sys.executable, "-c",
                "import core_loader; from pathlib import Path; "
                "m=core_loader.SourceFileLoader('worker'," + repr(str(CORE / "bin/brain")) + ").load_module(); "
                "i=m.Instance(Path(" + repr(str(self.r)) + ")); m.wake_drain(i,i.node('a'))"],
                env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                out, err = p.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                p.kill()
                p.communicate()
                self.fail("second drain blocked on the per-node lock")
            self.assertEqual(p.returncode, 0, out + err)
        self.assertEqual(len(list(directory.glob("*.pending"))), 1)
        self.assertFalse(self.calls.exists())

    def test_reason_arriving_during_send_is_retained_and_sent_once(self):
        self.enqueue("first")
        sends = []
        def send(command, reason=None):
            sends.append(command)
            if len(sends) == 1:
                self.enqueue("second")
            return True
        with mock.patch.object(self.module, "wake_send", side_effect=send), \
                mock.patch.object(self.module, "WAKE_INTERVAL", .01), self.inline_timer():
            self.module.wake_drain(self.inst, self.n)
        directory = self.module.wake_directory(self.inst, self.n)
        self.assertEqual(len(sends), 2)
        self.assertEqual(len(list(directory.glob("*.sent"))), 2)
        self.assertFalse(list(directory.glob("*.pending")))
        self.assertEqual(self.enqueue("first"), 0)
        self.assertEqual(self.enqueue("second"), 0)

    def test_parallel_requests_have_one_delivery(self):
        with mock.patch.object(self.module, "wake_start"):
            threads = [threading.Thread(target=self.module.request_wake,
                       args=(self.inst, self.n, "same reason")) for _ in range(12)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.drain()
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)

    def test_root_wake_tick_sees_fresh_descendant_question(self):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        (self.n["dir"] / "inbox.md").write_text("- " + now +
            " from runtime: Host question: Choose blue? <!-- host-question:abc -->\n")
        output = io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(self.module, "load", return_value=self.inst), \
                mock.patch.object(self.module, "core_update", return_value=None), \
                mock.patch.object(self.module, "auto_upgrade", return_value=None), contextlib.redirect_stdout(output):
            self.module.cmd_due(argparse.Namespace(id="root", quiet=False, json=True))
        items = json.loads(output.getvalue())
        self.assertTrue(any(i["kind"] == "question" and i["node"] == "a" for i in items))
        self.assertNotIn("Choose blue", output.getvalue())

    def test_root_due_escalates_without_question_text_and_live_shows_age(self):
        self.inst.local_of('root').update(coordinator='synthetic-root', wake=self.command)
        self.inst.save_local()
        self.root_due_escalation()

    def test_due_retains_loaded_ui_instance_state(self):
        self.inst = self.module.UIInstance(self.inst.root)
        self.inst.local_of('root').update(coordinator='synthetic-root', wake=self.command)
        # Deliberately not persisted: cmd_due must retain its explicit loaded view.
        self.root_due_escalation()

    def root_due_escalation(self):
        old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=11)).isoformat()
        (self.n["dir"] / "inbox.md").write_text("- " + old +
            " from runtime: Host question: Choose blue? <!-- host-question:abc -->\n")
        output = io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(self.module, "load", return_value=self.inst), \
                mock.patch.object(self.module, "core_update", return_value=None), \
                mock.patch.object(self.module, "auto_upgrade", return_value=None), \
                mock.patch.object(self.module, "request_wake") as wake, contextlib.redirect_stdout(output):
            self.module.cmd_due(argparse.Namespace(id="root", quiet=False, json=True))
            live = self.module.ui_state(self.inst, spend_budget=0)
        self.assertEqual(wake.call_args.args[1]["id"], "root")
        self.assertNotIn("Choose blue", wake.call_args.args[2])
        self.assertIn("overdue-question", output.getvalue())
        self.assertTrue(any(i["kind"] == "overdue-question" for i in live["due"]))

    def test_outward_state_link_is_refused(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        # Lifecycle scans legitimately create the private state directory already.
        (self.r / ".brain/wake").rename(self.r / ".brain/wake-original")
        (self.r / ".brain/wake").symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.enqueue("question"), 0)
        self.assertEqual(list(outside.iterdir()), [])

    def test_overdue_question_due_live_and_clear(self):
        inbox = self.n["dir"] / "inbox.md"
        marker = "<!-- host-question:" + "a" * 64 + " -->"
        old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=11)).isoformat()
        inbox.write_text("- " + old + " from runtime: Host question: Choose blue? " + marker + "\n")
        out = json.loads(self.b(self.r, "due", "a", "--json"))
        overdue = [i for i in out if i["kind"] == "overdue-question"]
        self.assertEqual(len(overdue), 1)
        self.assertEqual(overdue[0]["escalate_to"], "root")
        self.b(self.r, "set", "a", "question_minutes=20")
        self.assertNotIn("overdue-question", self.b(self.r, "due", "a", "--json"))
        inbox.write_text("")
        self.assertNotIn("overdue-question", self.b(self.r, "due", "a", "--json"))





class StopHook(Scenario):
    fixture_init_in_process = True

    def test_hook_log_append_tightens_existing_mode_and_preserves_contents(self):
        r = self.init('alice')
        time.sleep(1.1)
        (r / 'brief.md').write_text((r / 'brief.md').read_text() + '\nChanged fixture notes\n')
        log = r / '.brain/hooks.log'
        log.write_text('earlier fixture\n')
        log.chmod(0o644)
        inode = log.stat().st_ino
        self.assertEqual(self.hook(r, r, 'mode-session')['decision'], 'block')
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertEqual(log.stat().st_ino, inode)
        self.assertTrue(log.read_text().startswith('earlier fixture\n'))
        self.assertIn('mode-session', log.read_text())

    def test_relay_only_exit_captures_questions_but_skips_note_reminders(self):
        r = self.init("alice")
        (r / "brief.md").write_text("Changed notes\n")
        loader = core_loader.SourceFileLoader("brain_relay_exit", str(CORE / "bin/brain"))
        module = loader.load_module()
        payload = {"cwd": str(r), "last_assistant_message": "relay only"}
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                mock.patch.object(module, "capture_hook_questions") as capture, \
                mock.patch.object(module, "notes_lag", side_effect=AssertionError("relay inspected notes")), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            module.hook_stop(argparse.Namespace(event="stop"))
        capture.assert_called_once()
        self.assertEqual(output.getvalue(), "")
        reason = self.hook(r, r)["reason"]
        self.assertIn("relay only", reason)
        self.assertNotIn("repeating your previous answer", reason)

    def test_hygiene_extension_old_unchanged_notes_do_not_hold_read_only_turns(self):
        r = self.init('maple')
        st = r / 'status.md'
        st.write_text(st.read_text().replace(st.read_text().splitlines()[0],
                                            'Updated: 2020-01-01 00:00 +00:00'))
        self.git('commit', '-qam', 'old but unchanged judgement', cwd=r)
        before = st.read_bytes()
        self.assertIsNone(self.hook(r, r, 'reader-one'))
        self.assertIsNone(self.hook(r, r, 'reader-two'))
        self.assertEqual(st.read_bytes(), before)
        self.assertFalse((r / '.brain/hooks.log').exists())

    def test_hygiene_extension_names_handbacks_and_ignores_rewritten_bookkeeping(self):
        r = self.init('maple')
        self.assertIsNone(self.hook(r, r))
        folder = r / 'job-handbacks'
        folder.mkdir()
        signal = folder / 'fixture.json'
        payload = {'job': 'job-finish', 'state': 'done', 'report': 'reports/finish.md',
                   'ready': {'head': 'a' * 40, 'verify_result': 0}, 'sessions': ['fixture-one']}
        def publish():
            signal.write_text(json.dumps(payload))
            newer = (r / 'status.md').stat().st_mtime + 10
            os.utime(signal, (newer, newer))
        publish()
        out = self.hook(r, r)
        self.assertIn('hand-back job-finish: done', out['reason'])
        self.assertNotIn('job or inbox files changed', out['reason'])
        payload.update(sessions=['fixture-two'], verify_runs=2, verify_reused=1)
        publish()
        self.assertIsNone(self.hook(r, r, 'other-reader'))
        payload['ready']['head'] = 'b' * 40
        publish()
        self.assertIn('hand-back job-finish: done', self.hook(r, r)['reason'])
        # A fresh hand-back episode on the same head is still a completion event.
        payload['at'] = '2020-01-02T00:00:00+00:00'
        publish()
        fresh = self.hook(r, r)
        self.assertIsNotNone(fresh)
        self.assertIn('hand-back job-finish: done', fresh['reason'])

    def test_hygiene_extension_empty_events_do_not_hold_but_inbox_and_questions_do(self):
        r = self.init('maple')
        self.assertIsNone(self.hook(r, r))
        folder = r / 'job-handbacks'
        folder.mkdir()
        (folder / 'empty.json').write_text('{}\n')
        future = (r / 'status.md').stat().st_mtime + 10
        os.utime(folder / 'empty.json', (future, future))
        (r / 'inbox.md').touch()
        os.utime(r / 'inbox.md', (future, future))
        self.assertIsNone(self.hook(r, r))
        (r / 'inbox.md').write_text('- 2020-01-01 from worker: Review the finished job.\n')
        os.utime(r / 'inbox.md', (future, future))
        self.assertIn('inbox: 1 pending entry', self.hook(r, r)['reason'])
        self.assertIsNone(self.hook(r, r))
        # Captured host questions must still be recorded and shown as a new inbox event.
        out = self.hook(r, r, transcript=self.question_log([self.question_call()]))
        self.assertIn('host-question:', (r / 'inbox.md').read_text())
        self.assertIn('inbox: 2 pending entries', out['reason'])

    def test_hygiene_extension_guard_cache_survives_session_timestamp_trimming(self):
        r = self.init('maple')
        (r / 'job-handbacks').mkdir()
        signal = r / 'job-handbacks/fixture.json'
        signal.write_text(json.dumps({'job': 'job-finish', 'state': 'done'}))
        newer = (r / 'status.md').stat().st_mtime + 10
        os.utime(signal, (newer, newer))
        self.assertIn('hand-back job-finish: done', self.hook(r, r)['reason'])
        state_path = r / '.brain/hooks.json'
        state = json.loads(state_path.read_text())
        state_path.write_text(json.dumps({'_note_events': state['_note_events'],
            **{'session-' + str(i): '2020-01-01T00:00:00+00:00' for i in range(199)}}))
        brief = r / 'brief.md'
        original = brief.read_bytes()
        brief.write_bytes(original + b'\nChanged note.\n')
        self.assertIn('not committed', self.hook(r, r, 'new-session')['reason'])
        brief.write_bytes(original)
        older = (r / 'status.md').stat().st_mtime - 2
        os.utime(brief, (older, older))
        self.assertIsNone(self.hook(r, r, 'new-session'))
        self.assertIn('_note_events', json.loads(state_path.read_text()))

    def test_hygiene_extension_guard_nested_event_does_not_silence_upgrade_notice(self):
        r = self.init('maple')
        self.git('commit', '-q', '--allow-empty', '-m', 'Upgrade core abc1234 -> def5678', cwd=r)
        (r / 'job-handbacks').mkdir()
        signal = r / 'job-handbacks/fixture.json'
        signal.write_text('[' * 2000 + '0' + ']' * 2000)
        out = self.hook(r, r)
        self.assertIsNotNone(out)
        self.assertIn('core was upgraded', out['reason'])
        self.assertIn('unreadable event: fixture.json', out['reason'])

    def test_backlog_edits_make_notes_lag(self):
        r = self.init('fixture')
        self.b(r, 'new', 'notes', '--title', 'Notes')
        npath = r / 'nodes/notes'
        (npath / 'backlog.md').write_text('- Deferred fixture task.\n')
        now = time.time()
        os.utime(npath / 'status.md', (now - 10, now - 10))
        os.utime(npath / 'backlog.md', (now, now))
        m = core_loader.SourceFileLoader('brain_backlog_lag', str(CORE / 'bin/brain')).load_module()
        inst = m.Instance(r)
        lag = m.notes_lag(inst, inst.node('notes'))
        self.assertTrue(any('changed after status.md:' in line and 'backlog.md' in line for line in lag), lag)


    def hook(self, r, cwd, session="s1", active=False, transcript=None):
        """Run the exact command the runtimes run, as they do: hook JSON on stdin, in the session's folder."""
        cmd = json.loads((CORE / "adapters" / "runtimes" / "claude-hooks.json").read_text())["hooks"]["Stop"][0]["hooks"][0]["command"]
        env = {k: v for k, v in self.env.items() if k != "BRAIN_HOOK_REMIND_MINUTES"}
        res = subprocess.run(["sh", "-c", cmd], cwd=cwd, env=env, capture_output=True, text=True,
                             input=json.dumps({"cwd": str(cwd), "session_id": session, "stop_hook_active": active,
                                               "hook_event_name": "Stop", "transcript_path": str(transcript) if transcript else None}), timeout=10)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout) if res.stdout.strip() else None

    def test_user_hook_is_quick_silent_and_read_only_in_unrelated_folder(self):
        r = self.init("alice")
        unrelated = self.tmp / "unrelated"
        unrelated.mkdir()
        self.env["PATH"] = str(CORE / "bin") + os.pathsep + self.env["PATH"]
        # Exercise the user-level PATH fallback, with another Brain registered on the machine.
        before = (r / "inbox.md").read_bytes()
        for runtime in ("claude", "codex"):
            command = json.loads((CORE / "adapters/runtimes" / (runtime + "-hooks.json")).read_text())[
                "hooks"]["Stop"][0]["hooks"][0]["command"]
            start = time.monotonic()
            result = subprocess.run(["sh", "-c", command], cwd=unrelated, env=self.env,
                                    input=json.dumps({"cwd": str(unrelated), "transcript_path": "/not/read.jsonl"}),
                                    capture_output=True, text=True, timeout=10)
            self.assertLess(time.monotonic() - start, 10)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(list(unrelated.iterdir()), [])
        self.assertEqual((r / "inbox.md").read_bytes(), before)
        for name in ("CHANGES.md", "adapters/runtimes/README.md", "skills/machine-setup/SKILL.md"):
            text = (CORE / name).read_text()
            self.assertIn("user-level", text)
            self.assertIn("consent", text)
            self.assertRegex(text, r"Never install\s+Brain hooks in a home or worktree")

    def test_large_transcript_reads_only_latest_turn_with_bounded_time_and_io(self):
        loader = core_loader.SourceFileLoader("brain_hook_tail", str(CORE / "bin/brain"))
        module = loader.load_module()
        path = self.tmp / "large-synthetic.jsonl"
        historical = (json.dumps({"type": "assistant", "message": {"content": "x" * 4000}}) + "\n").encode()
        with path.open("wb") as stream:
            block = historical * 1000
            for _ in range(175):  # more than 700 MB of fabricated, valid JSONL history
                stream.write(block)
            stream.write((json.dumps({"type": "user", "message": {"content": "Latest task"}}) + "\n").encode())
            stream.write((json.dumps(self.question_call()) + "\n").encode())
        self.assertGreater(path.stat().st_size, 700_000_000)
        original = module.json.loads
        parsed = []
        def counting_loads(value):
            parsed.append(len(value))
            # A full-history scanner fails deterministically before chewing through the fixture.
            self.assertLessEqual(len(parsed), 8)
            return original(value)
        start = time.monotonic()
        with mock.patch.object(module.json, "loads", side_effect=counting_loads):
            questions = module.hook_questions(path)
        self.assertLess(time.monotonic() - start, 10)
        self.assertEqual([q["question"] for q in questions], ["Which colour?"])
        self.assertLessEqual(sum(parsed), module.HOOK_TRANSCRIPT_CHUNK)
        # Also assert a byte cap when an oversized last turn has no operator boundary in range.
        class CountedFile:
            def __init__(self, stream):
                self.stream, self.bytes_read = stream, 0
            def fileno(self):
                return self.stream.fileno()
            def seek(self, offset):
                return self.stream.seek(offset)
            def read(self, amount):
                data = self.stream.read(amount)
                self.bytes_read += len(data)
                return data
        with path.open("ab") as stream:
            stream.write(b"x" * (module.HOOK_TRANSCRIPT_LIMIT + 1))
        with path.open("rb") as stream:
            counted = CountedFile(stream)
            self.assertEqual(module.hook_turn_records(counted), [])
            self.assertLessEqual(counted.bytes_read, module.HOOK_TRANSCRIPT_LIMIT)

    def test_reverse_transcript_chunks_preserve_answers_boundaries_and_partial_lines(self):
        loader = core_loader.SourceFileLoader("brain_hook_tail_edges", str(CORE / "bin/brain"))
        module = loader.load_module()
        rows = [self.question_call("old", text="Old question?"),
                {"type": "user", "message": {"content": "New task"}},
                self.question_call(),
                {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "q1",
                 "content": "User has answered your questions: Blue"}]}},
                self.question_call("q2", text="Latest question? \u03bb")]
        path = self.question_log(rows)
        with path.open("ab") as stream:
            stream.write(b'\n{"type": "unfinished')
        with mock.patch.object(module, "HOOK_TRANSCRIPT_CHUNK", 17):
            self.assertEqual([q["question"] for q in module.hook_questions(path)], ["Latest question? \u03bb"])

    def question_log(self, rows):
        path = self.tmp / "synthetic.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        return path

    def question_call(self, ident="q1", text="Which colour?", **extra):
        return {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": ident,
                "name": "AskUserQuestion", "input": {"questions": [{"question": text, **extra}]}}]}}

    def test_question_capture_screening_dedup_and_due(self):
        r = self.init("alice")
        self.b(r, "new", "a", "--title", "A")
        d = r / "nodes" / "a"
        (d / "never-name.txt").write_text("ForbiddenColour\n")
        path = self.question_log([{"type": "user", "message": {"content": "Choose a colour"}},
                                  self.question_call(text="Use ForbiddenColour?\npassword=made-up",
                                      options=[{"label": "Blue", "description": "A blue option"}])])
        self.hook(r, d, transcript=path)
        self.hook(r, d, active=True, transcript=path)
        content = (d / "inbox.md").read_text()
        self.assertEqual(content.count("host-question:"), 1)
        self.assertNotIn("ForbiddenColour", content)
        self.assertNotIn("password=", content)
        self.assertIn("Blue A blue option", content)
        self.assertIn("Host question:", self.b(r, "inbox", "a"))
        self.assertIn("Host question:", self.b(r, "due", "a"))
        # A fresh call is a separate episode: an answer to q1 must not retire q2.
        rows = [self.question_call("q2", text="Use ForbiddenColour?\npassword=made-up",
                                  options=[{"label": "Blue", "description": "A blue option"}])]
        self.hook(r, d, transcript=self.question_log(rows))
        self.assertEqual((d / "inbox.md").read_text().count("host-question:"), 2)
        self.hook(r, d, transcript=self.question_log(rows))
        self.assertEqual((d / "inbox.md").read_text().count("host-question:"), 2)

    def test_answered_question_can_be_asked_again_and_slow_wake_succeeds(self):
        r = self.init("alice")
        self.b(r, "new", "a", "--title", "Alpha")
        calls = self.tmp / "wake-calls"
        script = "import time; time.sleep(0.6); open(" + repr(str(calls)) + ", 'a').write('called\\n')"
        self.b(r, "set", "a", "coordinator=synthetic-thread", "wake=" +
               shlex_quote(sys.executable) + " -c " + shlex_quote(script))
        d = r / "nodes/a"
        transcript = self.question_log([self.question_call(text="Choose blue?")])
        for count in (1, 2):
            start = time.monotonic()
            self.hook(r, d, active=True, transcript=transcript)
            self.assertLess(time.monotonic() - start, 10)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if calls.exists() and len(calls.read_text().splitlines()) == count and list((r / ".brain/wake/a").glob("*.sent")):
                    break
                time.sleep(0.05)
            self.assertEqual(len(calls.read_text().splitlines()), count)
            (d / "inbox.md").write_text("")  # coordinator answered the previous episode
            (r / ".brain/wake/a/last").write_text(str(time.time() - 121))

    def test_hanging_wake_keeps_hook_fast_and_inbox_durable(self):
        r = self.init("alice")
        self.b(r, "new", "a", "--title", "Alpha")
        attempted = self.tmp / "wake-attempted"
        script = "from pathlib import Path; import time; Path(" + repr(str(attempted)) + ").touch(); time.sleep(20)"
        self.b(r, "set", "a", "coordinator=synthetic-thread", "wake=" +
               shlex_quote(sys.executable) + " -c " + shlex_quote(script))
        d = r / "nodes/a"
        start = time.monotonic()
        self.hook(r, d, active=True, transcript=self.question_log([self.question_call(text="Choose blue?")]))
        self.assertLess(time.monotonic() - start, 10)
        self.assertIn("Choose blue?", (d / "inbox.md").read_text())
        deadline = time.monotonic() + 5
        while not attempted.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(attempted.exists())
        time.sleep(0.5)
        directory = r / ".brain/wake/a"
        self.assertEqual(len(list(directory.glob("*.pending"))), 1)
        self.assertFalse(list(directory.glob("*.sent")))

    def test_captured_question_wakes_once_without_holding_stop(self):
        r = self.init("alice")
        self.b(r, "new", "a", "--title", "Alpha")
        calls = self.tmp / "wake-calls"
        attempted, release = self.tmp / "wake-attempted", self.tmp / "release-wake"
        m = core_loader.SourceFileLoader("gated_wake_cleanup", str(CORE / "bin/brain")).load_module()
        script = ("import os,time,json,core_loader; from pathlib import Path\n"
                  "m=core_loader.SourceFileLoader('gated_wake'," + repr(str(CORE / "bin/brain")) + ").load_module()\n"
                  "owned=[[pid,m.process_stamp(pid)] for pid in (os.getppid(),os.getpid())]\n"
                  "Path(" + repr(str(attempted)) + ").write_text(json.dumps(owned))\n"
                  "while not Path(" + repr(str(release)) + ").exists(): time.sleep(.02)\n"
                  "with open(" + repr(str(calls)) + ",'a') as f: f.write('called\\n')\n")
        command = "exec " + shlex_quote(sys.executable) + " -c " + shlex_quote(script)
        self.b(r, "set", "a", "coordinator=synthetic-thread", "wake=" + command)
        d = r / "nodes/a"
        transcript = self.question_log([self.question_call(text="Choose blue?")])
        directory = r / ".brain/wake/a"
        try:
            # hook() has a ten-second process timeout. Delivery stays gated until
            # after it returns, proving nonblocking transport independently of load.
            self.assertIsNone(self.hook(r, d, active=True, transcript=transcript))
            deadline = time.monotonic() + 10
            while not attempted.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(attempted.exists())
            self.assertFalse(calls.exists())
            self.assertIn("Choose blue?", (d / "inbox.md").read_text())
            release.touch()
            deadline = time.monotonic() + 10
            while not list(directory.glob("*.sent")) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(len(list(directory.glob("*.sent"))), 1)
            self.assertEqual(calls.read_text().splitlines(), ["called"])
            self.hook(r, d, active=True, transcript=transcript)
            time.sleep(0.1)
            self.assertEqual(calls.read_text().splitlines(), ["called"])
            self.assertIn("Choose blue?", (d / "inbox.md").read_text())
        finally:
            release.touch()
            deadline = time.monotonic() + 10
            while not attempted.exists() and time.monotonic() < deadline:
                time.sleep(.05)
            owned = json.loads(attempted.read_text()) if attempted.exists() else []
            owned = [(pid, stamp) for pid, stamp in owned if stamp not in (None, "unknown")]
            while time.monotonic() < deadline and any(m.process_stamp(pid) == stamp for pid, stamp in owned):
                time.sleep(.05)
            for pid, stamp in owned:
                if m.process_stamp(pid) == stamp:
                    try:
                        os.kill(pid, 9)
                    except ProcessLookupError:
                        pass  # the owned worker may finish between the identity check and kill

    def test_rendered_question_is_screened_after_whitespace_folding(self):
        r = self.init("alice")
        (r / "never-name.txt").write_text("Synthetic Secret\n")
        rows = [self.question_call(text="Use Synthetic\tSecret?"),
                self.question_call("q2", text="password\n=made-up-value")]
        self.hook(r, r, active=True, transcript=self.question_log(rows))
        text = (r / "inbox.md").read_text()
        self.assertNotIn("Synthetic Secret", text)
        self.assertNotIn("made-up-value", text)
        self.assertEqual(text.count("host-question:"), 2)

    def test_answered_old_turn_and_errors(self):
        r = self.init("alice")
        call = self.question_call()
        answer = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "q1",
                   "content": "User has answered your questions: Blue"}]}}
        for rows in ([call, answer], [call, {"type": "user", "message": {"content": "Next task"}}]):
            self.hook(r, r, transcript=self.question_log(rows))
            self.assertNotIn("host-question:", (r / "inbox.md").read_text())
        answer["message"]["content"][0]["is_error"] = True
        self.hook(r, r, transcript=self.question_log([call, answer]))
        self.assertEqual((r / "inbox.md").read_text().count("host-question:"), 1)

    def test_codex_question_rollout_answer_and_pending(self):
        r = self.init("alice")
        call = {"type": "response_item", "payload": {"type": "function_call", "call_id": "c1",
                "name": "request_user_input", "arguments": json.dumps({"questions": [
                    {"id": "colour", "question": "Which colour?"}, {"id": "shape", "question": "Which shape?"}]})}}
        answer = {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "c1",
                  "output": json.dumps({"answers": {"colour": {"answers": ["Blue"]}, "shape": {"answers": ["Round"]}}})}}
        self.hook(r, r, transcript=self.question_log([call, answer]))
        self.assertNotIn("host-question:", (r / "inbox.md").read_text())
        self.hook(r, r, active=True, transcript=self.question_log([call]))
        self.assertEqual((r / "inbox.md").read_text().count("host-question:"), 2)

    def test_codex_partial_and_empty_answers_keep_unanswered_questions(self):
        r = self.init("alice")
        call = {"type": "response_item", "payload": {"type": "function_call", "call_id": "c1",
                "name": "functions.request_user_input", "arguments": json.dumps({"questions": [
                    {"id": "colour", "question": "Which colour?"}, {"id": "shape", "question": "Which shape?"}]})}}
        answer = {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "c1",
                  "output": json.dumps({"answers": {"colour": {"answers": ["Blue"]}, "shape": {"answers": []}}})}}
        self.hook(r, r, transcript=self.question_log([call, answer]))
        text = (r / "inbox.md").read_text()
        self.assertNotIn("Which colour?", text)
        self.assertIn("Which shape?", text)

    def test_question_capture_failure_does_not_suppress_reminder(self):
        r = self.init("alice")
        missing = self.tmp / "absent.jsonl"
        (r / "brief.md").write_text("Changed notes\n")
        result = self.hook(r, r, transcript=missing)
        self.assertEqual(result["decision"], "block")  # existing notes reminder still runs
        for raw in ('not-json\n', '[]\n', '{"message": null}\n'):
            missing.write_text(raw)
            self.hook(r, r, active=True, transcript=missing)

    def test_nonregular_transcript_never_waits_for_a_writer(self):
        r = self.init("alice")
        fifo = self.tmp / "synthetic-transcript-fifo"
        os.mkfifo(fifo)
        self.hook(r, r, active=True, transcript=fifo)
        self.hook(r, r, active=True, transcript=self.tmp)
        self.assertNotIn("host-question:", (r / "inbox.md").read_text())

    def test_question_refusal_never_exits_the_hook_or_suppresses_reminders(self):
        loader = core_loader.SourceFileLoader("brain_hook_refusal", str(CORE / "bin/brain"))
        module = loader.load_module()
        r = self.init("alice")
        (r / "brief.md").write_text("Changed notes\n")
        args = argparse.Namespace(event="stop")
        payload = json.dumps({"cwd": str(r)})
        with mock.patch.object(module, "capture_hook_questions", side_effect=SystemExit(1)), \
                mock.patch.object(sys, "stdin", io.StringIO(payload)), contextlib.redirect_stdout(io.StringIO()) as output:
            module.cmd_hook(args)
        self.assertEqual(json.loads(output.getvalue())["decision"], "block")
        with mock.patch.object(module, "hook_stop", side_effect=SystemExit(1)):
            module.cmd_hook(args)

    def test_question_capture_does_not_wait_on_busy_inbox(self):
        import fcntl
        r = self.init("alice")
        path = self.question_log([self.question_call()])
        with (r / "inbox.md").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.hook(r, r, active=True, transcript=path)
            self.assertIn("Host question:", self.b(r, "due", "root"))
            self.assertIn("Host question:", self.b(r, "inbox", "root"))
        self.assertIn("Host question:", self.b(r, "inbox", "root"))
        self.assertEqual((r / "inbox.md").read_text().count("host-question:"), 1)
        self.hook(r, r, active=True, transcript=path)
        self.assertEqual((r / "inbox.md").read_text().count("host-question:"), 1)

    def test_question_queue_never_reads_or_removes_an_outward_link(self):
        r = self.init("alice")
        outside = self.tmp / "outside-queue"
        (outside / "root").mkdir(parents=True)
        fake = outside / "root" / ("a" * 64 + ".json")
        fake.write_text(json.dumps({"line": "- outside question sentinel\n", "marker": "synthetic-marker"}))
        (r / ".brain/host-questions").symlink_to(outside, target_is_directory=True)
        self.assertNotIn("outside question sentinel", self.b(r, "due", "root"))
        self.assertNotIn("outside question sentinel", self.b(r, "inbox", "root"))
        self.assertTrue(fake.exists())

    def test_question_capture_in_registered_external_worker_folder(self):
        r = self.init("alice")
        home = self.tmp / "synthetic-home"
        home.mkdir()
        self.b(r, "new", "a", "--title", "A", "--home", str(home))
        self.env["PATH"] = str(CORE / "bin") + os.pathsep + self.env["PATH"]
        self.hook(r, home, transcript=self.question_log([self.question_call()]))
        self.assertEqual((r / "nodes/a/inbox.md").read_text().count("host-question:"), 1)
        self.assertNotIn("host-question:", (r / "inbox.md").read_text())

    def test_question_capture_concurrent_hooks_and_parent_lock(self):
        import concurrent.futures
        import fcntl
        r = self.init("alice")
        path = self.question_log([self.question_call()])
        # A parent CLI lock must not delay the hook (which uses only the inbox lock).
        with (r / ".brain/lock").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda _: self.hook(r, r, active=True, transcript=path), range(4)))
        self.assertEqual((r / "inbox.md").read_text().count("host-question:"), 1)

    def test_root_tick_contract_covers_descendants_and_provider(self):
        text = (CORE / "adapters/hosts/t3.md").read_text()
        for clause in ("every descendant node", "brain show <child>", "each child's coordinator thread id",
                       "nested nodes", "without open jobs", "provider it was created under"):
            self.assertIn(clause, text)

    def test_links_for_both_runtimes(self):
        r = self.init("alice")
        self.b(r, "new", "a", "--title", "A")
        for f in (r / ".claude" / "settings.json", r / "nodes" / "a" / ".claude" / "settings.json",
                  r / ".codex" / "hooks.json"):
            self.assertTrue(f.is_symlink() and f.exists(), f)
        self.assertFalse((r / "nodes" / "a" / ".codex").exists(), "codex reads hooks from the root only")
        (r / "nodes" / "a" / ".claude" / "settings.json").unlink()
        self.assertIn("stop hook) is missing", self.b(r, "check", ok=False))
        self.b(r, "check", "--fix")
        self.assertIn("✓", self.b(r, "check"))

    def test_stop_hook_holds_a_turn_until_the_notes_are_current(self):
        r = self.init("alice")
        self.b(r, "new", "a", "--title", "A")
        a = r / "nodes" / "a"
        self.assertIsNone(self.hook(r, a), "fresh, committed notes: let it stop")
        time.sleep(1.1)
        (a / "brief.md").write_text((a / "brief.md").read_text() + "\nAudience: teenagers\n")
        out = self.hook(r, a)
        self.assertEqual(out["decision"], "block")
        self.assertIn("changed after status.md: brief.md", out["reason"])
        self.assertIn("not committed", out["reason"])
        self.assertIsNone(self.hook(r, a, active=True), "never twice in a row")
        st = a / "status.md"
        st.write_text(st.read_text().replace(st.read_text().splitlines()[0],
                                             "Updated: " + self.b(r, "now").strip() + " by a coordinator (claude)"))
        self.git("add", "-A", cwd=r); self.git("commit", "-qm", "a: brief", cwd=r)
        self.assertIsNone(self.hook(r, a))
        self.assertIsNone(self.hook(r, self.tmp), "outside an instance: nothing")
        self.assertIn("stop a s1", (r / ".brain" / "hooks.log").read_text())
        self.assertNotIn("hooks", self.git("ls-files", ".brain", cwd=r))

    def test_old_notes_do_not_hold_an_unchanged_turn_in_any_session(self):
        r = self.init("alice")
        self.b(r, "new", "a", "--title", "A")
        a, st = r / "nodes" / "a", r / "nodes" / "a" / "status.md"
        lines = st.read_text().splitlines()
        lines[0] = "Updated: 2020-01-01 00:00 by a coordinator (claude)"
        st.write_text("\n".join(lines) + "\n")
        self.git("commit", "-qam", "a: old", cwd=r)
        self.assertIsNone(self.hook(r, a, "s1"))
        self.assertIsNone(self.hook(r, a, "s1"))
        self.assertIsNone(self.hook(r, a, "s2"))



if __name__ == "__main__":
    unittest.main()


class WakeConsumption(Scenario):
    """Transport acceptance retains work until the coordinator consumes its receipt."""
    def setUp(self):
        super().setUp()
        environment = mock.patch.dict(os.environ, self.env, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.m = core_loader.SourceFileLoader("brain_consumption", str(CORE / "bin/brain")).load_module()
        self.r = self.init("alice")
        self.b(self.r, "set", "root", "coordinator=fixture-thread", "wake=fixture-send", "wake_coalesce=on")
        self.inst = self.m.Instance(self.r)
        self.n = self.inst.node("root")
        self.now = 1000000
        self.clock = mock.Mock()
        self.clock.time.side_effect = lambda: self.now
        self.clock.monotonic.side_effect = lambda: self.now
        for patch in (mock.patch.object(self.m, "WAKE_CLOCK", self.clock),
                      mock.patch.object(self.m, "WAKE_INTERVAL", 0),
                      mock.patch.object(self.m, "wake_start"),
                      mock.patch.object(self.m, "wake_snapshot", return_value=[])):
            patch.start()
            self.addCleanup(patch.stop)
        sending = mock.patch.object(self.m, "wake_send", return_value=True)
        self.send = sending.start()
        self.addCleanup(sending.stop)

    def enqueue(self, number):
        self.assertTrue(self.m.request_wake(self.inst, self.n, "fixture:" + str(number),
                        items=[{"id": format(number, "064x"), "kind": "inbox"}], explicit=True))
        self.m.wake_drain(self.inst, self.n, once=True)
        self.now += 1

    def test_burst_is_one_transport_until_consumed_then_rearms(self):
        for i in range(1, 21):
            self.enqueue(i)
        self.assertEqual(self.send.call_count, 1)
        summary = self.m.wake_turn_consume(self.inst, self.n)
        self.assertEqual(len(summary["items"]), 20)
        self.assertFalse(list(self.m.wake_directory(self.inst, self.n).glob("*.pending")))
        self.enqueue(21)
        self.assertEqual(self.send.call_count, 2)

    def test_stall_is_visible_without_a_blind_reminder(self):
        self.enqueue(1)
        self.now += 120
        self.m.wake_drain(self.inst, self.n, once=True)
        self.assertEqual(self.send.call_count, 1)
        self.assertIn("unconsumed", self.m.wake_turn_stuck(self.inst, self.n))
        self.now += 1800
        self.m.wake_drain(self.inst, self.n, once=True)
        self.assertEqual(self.send.call_count, 1)

    def test_damaged_turn_retains_new_work_without_duplicate_transport(self):
        self.enqueue(1)
        directory = self.m.wake_directory(self.inst, self.n)
        (directory / "turn.json").write_text("{torn")
        self.enqueue(2)
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual((directory / "turn.json").read_text(), "{torn")
        self.assertEqual(len(list(directory.glob('*.pending'))), 2)
        self.assertIn('receipt unavailable', self.m.wake_turn_stuck(self.inst, self.n))

    def test_failed_transport_does_not_create_consumption_receipt(self):
        self.send.return_value = False
        self.enqueue(1)
        self.send.return_value = True
        self.m.wake_drain(self.inst, self.n, once=True)
        self.assertEqual(self.send.call_count, 2)
        self.assertEqual(len(self.m.wake_turn_consume(self.inst, self.n)["items"]), 1)

    def test_binding_rotation_does_not_inherit_old_turn(self):
        self.enqueue(1)
        self.b(self.r, "set", "root", "coordinator=fixture-new")
        self.inst = self.m.Instance(self.r)
        self.n = self.inst.node("root")
        self.enqueue(2)
        self.assertEqual(self.send.call_count, 2)

    def test_consumption_during_transport_does_not_recreate_turn(self):
        self.send.side_effect = lambda *args, **kw: bool(self.m.wake_turn_consume(self.inst, self.n))
        self.enqueue(1)
        self.enqueue(2)
        self.assertEqual(self.send.call_count, 2)

    def test_readonly_projection_counts_unconsumed_items_with_retained_pending_files(self):
        refs = [{'id': format(i, '064x'), 'kind': 'inbox'} for i in range(1, 5)]
        self.m.request_wake(self.inst, self.n, 'fixture-batch', items=refs, explicit=True)
        self.m.wake_drain(self.inst, self.n, once=True)
        self.assertEqual(len(list(self.m.wake_directory(self.inst, self.n).glob('*.pending'))), 1)
        directory = self.m.wake_directory(self.inst, self.n)
        before = {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}
        view = self.m.wake_turn_queue(self.inst, self.n)
        self.assertEqual(view, {'known': True, 'item_count': 4, 'oldest_at': self.now, 'unconsumed_wake': True})
        self.assertEqual(before, {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()})
        self.m.wake_turn_consume(self.inst, self.n)
        self.assertEqual(self.m.wake_turn_queue(self.inst, self.n)['item_count'], 0)

    def test_invalid_or_future_turn_receipts_cannot_authorize_duplicate_transport(self):
        self.enqueue(1)
        directory = self.m.wake_directory(self.inst, self.n)
        path = directory / 'turn.json'
        baseline = json.loads(path.read_text())
        mutations = [
            ('future', lambda v: v.update(at=self.now + 86400)),
            ('negative-time', lambda v: v.update(at=-1)),
            ('empty-token', lambda v: v.update(token='')),
            ('noncanonical-token', lambda v: v.update(token='A' * 32)),
            ('malformed-binding', lambda v: v.update(binding='z' * 64)),
            ('empty-binding', lambda v: v.update(binding='')),
            ('other-binding', lambda v: v.update(binding='a' * 64)),
            ('numeric-id', lambda v: v.update(items=[{'id': 7, 'kind': 'inbox'}])),
            ('unknown-kind', lambda v: v.update(items=[{'id': 'a' * 64, 'kind': 'unknown-kind'}])),
            ('empty-kind', lambda v: v.update(items=[{'id': 'a' * 64, 'kind': ''}])),
            ('duplicate-id', lambda v: v.update(items=v['items'] * 2)),
            ('conflicting-id', lambda v: v.update(items=[v['items'][0], {**v['items'][0], 'kind': 'handback'}])),
            ('invalid-question-flag', lambda v: v.update(items=[{**v['items'][0], 'question': 1}])),
        ]
        for index, (name, change) in enumerate(mutations, 2):
            with self.subTest(case=name):
                value = json.loads(json.dumps(baseline))
                change(value)
                raw = json.dumps(value).encode()
                path.write_bytes(raw)
                self.assertIsNone(self.m.wake_turn_consume(self.inst, self.n))
                self.m.wake_turn_finish(self.inst, self.n, baseline['token'], True)
                if name != 'other-binding':
                    self.assertIn('receipt unavailable', self.m.wake_turn_stuck(self.inst, self.n))
                self.assertEqual(path.read_bytes(), raw)
                before = self.send.call_count
                self.enqueue(index)
                self.assertEqual(self.send.call_count, before + (name == 'other-binding'))
                retained = [p.read_bytes() for p in directory.glob('turn*.json')]
                self.assertIn(raw, retained, 'invalid or retired evidence must be preserved')

    def test_clock_rollback_cannot_authorize_a_duplicate_transport(self):
        self.enqueue(1)
        path = self.m.wake_directory(self.inst, self.n) / 'turn.json'
        raw = path.read_bytes()
        self.now -= 3600
        self.enqueue(2)
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(path.read_bytes(), raw)
        self.assertIn('receipt unavailable', self.m.wake_turn_stuck(self.inst, self.n))

    def test_damaged_turn_preserves_all_pending_until_receipt_repair(self):
        self.enqueue(1)
        directory = self.m.wake_directory(self.inst, self.n)
        path = directory / 'turn.json'
        value = json.loads(path.read_text())
        value['at'] = self.now + 86400
        raw = json.dumps(value).encode()
        path.write_bytes(raw)
        self.send.return_value = False
        self.enqueue(2)
        self.send.return_value = True
        self.m.wake_drain(self.inst, self.n, once=True)
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(len(list(directory.glob('*.pending'))), 2)
        self.assertEqual(path.read_bytes(), raw)


class AckAllShown(Scenario):
    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(os.environ, self.env)
        patch.start()
        self.addCleanup(patch.stop)
        self.m = core_loader.SourceFileLoader('outbox_fixture', str(CORE / 'bin/brain')).load_module()
        self.root = self.init('oak')
        self.b(self.root, 'set', 'root', 'wake=fixture-send', 'coordinator=fixture-root')
        self.inst = self.m.Instance(self.root)

    def test_legacy_comparison_uses_only_sent_metadata_for_the_live_binding(self):
        m, n = self.m, self.inst.node('root')
        directory = m.wake_directory(self.inst, n)
        directory.mkdir(parents=True, exist_ok=True)
        binding = m.wake_turn_binding(self.inst, n)
        refs = [{'id': str(index) * 64, 'kind': 'inbox'} for index in (1, 2, 3)]
        for name, owner, ref in (('a.sent', binding, refs[0]), ('b.sent', 'old-binding', refs[1]),
                                  ('c.pending', binding, refs[2])):
            (directory / name).write_text(json.dumps({'binding': owner, 'items': [ref]}))
        (directory / 'd.sent').write_text('delivered\n')
        with mock.patch.object(m, 'watch_groups', side_effect=AssertionError('comparison scanned')):
            self.assertEqual(m.wake_comparison_before(self.inst, n, []), refs[:1])
        self.assertEqual(m.wake_shown(self.inst, n), set(), 'display comparison grants no acknowledgement')

    def test_shown_item_queued_later_is_acknowledged_but_new_item_wakes(self):
        m, n = self.m, self.inst.node('root')
        refs = [{'id': str(i) * 64, 'kind': 'inbox'} for i in (1, 2, 3)]
        group = {'root': {'items': refs[:2]}}
        with mock.patch.object(m, 'watch_groups', return_value=group), \
                mock.patch.object(m, 'wake_start'), mock.patch.object(m, 'WAKE_INTERVAL', 0), \
                mock.patch.object(m, 'wake_send', return_value=True) as send:
            self.assertTrue(m.request_wake(self.inst, n, 'fixture first', items=refs[:1]))
            m.wake_drain(self.inst, n, once=True)
            consume_fixture_wake(m, self.inst, n)
            self.assertTrue(m.request_wake(self.inst, n, 'fixture delivered again', items=refs[:1]))
            m.wake_drain(self.inst, n, once=True)
            consume_fixture_wake(m, self.inst, n)
            self.assertEqual(send.call_count, 1)  # the delivered ref is acknowledged
            # Displayed but undelivered work stays unacknowledged and causes the next wake.
            self.assertTrue(m.request_wake(self.inst, n, 'fixture later', items=refs[1:2]))
            m.wake_drain(self.inst, n, once=True)
            consume_fixture_wake(m, self.inst, n)
            self.assertEqual(send.call_count, 2)
            group['root']['items'] = refs
            self.assertTrue(m.request_wake(self.inst, n, 'fixture mixed', items=refs[1:]))
            m.wake_drain(self.inst, n, once=True)
            consume_fixture_wake(m, self.inst, n)
            self.assertEqual(send.call_count, 3)
            history = m.wake_history(m.wake_directory(self.inst, n))
            self.assertEqual(history[-1]['items'], refs[2:])

    def test_failed_transport_cannot_acknowledge_snapshot(self):
        m, n = self.m, self.inst.node('root')
        refs = [{'id': str(i) * 64, 'kind': 'inbox'} for i in (1, 2)]
        with mock.patch.object(m, 'watch_groups', return_value={'root': {'items': refs}}), \
                mock.patch.object(m, 'wake_start'), mock.patch.object(m, 'WAKE_INTERVAL', 0):
            m.request_wake(self.inst, n, 'fixture first', items=refs[:1])
            with mock.patch.object(m, 'wake_send', return_value=False):
                m.wake_drain(self.inst, n, once=True)
                consume_fixture_wake(m, self.inst, n)
            m.request_wake(self.inst, n, 'fixture second', items=refs[1:])
            with mock.patch.object(m, 'wake_send', return_value=True) as send:
                m.wake_drain(self.inst, n, once=True)
                consume_fixture_wake(m, self.inst, n)
            self.assertEqual(send.call_count, 1)
            self.assertEqual(m.wake_history(m.wake_directory(self.inst, n))[-1]['items'], refs)

    def test_enqueue_during_transport_only_covers_the_frozen_snapshot(self):
        m, n = self.m, self.inst.node('root')
        refs = [{'id': str(i) * 64, 'kind': 'inbox'} for i in (1, 2, 3)]
        group = {'root': {'items': refs[:2]}}
        with mock.patch.object(m, 'watch_groups', return_value=group), \
                mock.patch.object(m, 'wake_start'), mock.patch.object(m, 'WAKE_INTERVAL', 0):
            m.request_wake(self.inst, n, 'fixture first', items=refs[:1])
            def send(command, reason=None):
                group['root']['items'] = refs
                m.request_wake(self.inst, n, 'fixture during send', items=refs[1:])
                return True
            with mock.patch.object(m, 'wake_send', side_effect=send):
                m.wake_drain(self.inst, n, once=True)
                consume_fixture_wake(m, self.inst, n)
            with mock.patch.object(m, 'wake_send', return_value=True) as next_send:
                m.wake_drain(self.inst, n, once=True)
                consume_fixture_wake(m, self.inst, n)
            self.assertEqual(next_send.call_count, 1)
            # Only the delivered ref was acknowledged; every undelivered one rides the next wake.
            self.assertEqual(m.wake_history(m.wake_directory(self.inst, n))[-1]['items'], refs[1:])
            self.assertTrue(m.request_wake(self.inst, n, 'fixture first again', items=refs[:1]))
            with mock.patch.object(m, 'wake_send', return_value=True) as last_send:
                m.wake_drain(self.inst, n, once=True)
                consume_fixture_wake(m, self.inst, n)
            last_send.assert_not_called()

    def test_successful_snapshot_is_scoped_to_coordinator_and_transport(self):
        m, n = self.m, self.inst.node('root')
        refs = [{'id': str(i) * 64, 'kind': 'inbox'} for i in (1, 2)]
        with mock.patch.object(m, 'watch_groups', return_value={'root': {'items': refs}}), \
                mock.patch.object(m, 'wake_start'), mock.patch.object(m, 'WAKE_INTERVAL', 0), \
                mock.patch.object(m, 'wake_send', return_value=True) as send:
            m.request_wake(self.inst, n, 'fixture first', items=refs[:1])
            m.wake_drain(self.inst, n, once=True)
            consume_fixture_wake(m, self.inst, n)
            self.b(self.root, 'set', 'root', 'coordinator=fixture-replacement', 'wake=fixture-other-send')
            self.inst = m.Instance(self.root)
            n = self.inst.node('root')
            # The ref delivered under the old binding is not acknowledged for the new one.
            m.request_wake(self.inst, n, 'fixture replacement', items=refs[:1])
            m.wake_drain(self.inst, n, once=True)
            consume_fixture_wake(m, self.inst, n)
            self.assertEqual(send.call_count, 2)

    def test_snapshot_acknowledgement_retries_transient_history_write(self):
        m, n = self.m, self.inst.node('root')
        refs = [{'id': str(i) * 64, 'kind': 'inbox'} for i in (1, 2)]
        atomic, failed = m.atomic_write, []
        def write(path, value, **kwargs):
            if path.name == 'history.json' and not failed:
                failed.append(True)
                raise OSError('fixture transient history write')
            return atomic(path, value, **kwargs)
        with mock.patch.object(m, 'watch_groups', return_value={'root': {'items': refs}}), \
                mock.patch.object(m, 'wake_start'), mock.patch.object(m, 'WAKE_INTERVAL', 0), \
                mock.patch.object(m, 'wake_send', return_value=True) as send:
            m.request_wake(self.inst, n, 'fixture first', items=refs[:1])
            with mock.patch.object(m, 'atomic_write', side_effect=write):
                m.wake_drain(self.inst, n, once=True)
                consume_fixture_wake(m, self.inst, n)
            self.assertTrue(failed)
            m.request_wake(self.inst, n, 'fixture already delivered', items=refs[:1])
            m.wake_drain(self.inst, n, once=True)
            consume_fixture_wake(m, self.inst, n)
            self.assertEqual(send.call_count, 1)  # the delivered ref stays acknowledged
            m.request_wake(self.inst, n, 'fixture displayed only', items=refs[1:])
            m.wake_drain(self.inst, n, once=True)
            consume_fixture_wake(m, self.inst, n)
            self.assertEqual(send.call_count, 2)  # undelivered work causes the next wake

    def test_acknowledgement_outlives_display_history(self):
        m, n = self.m, self.inst.node('root')
        refs = [{'id': str(i) * 64, 'kind': 'inbox'} for i in (1, 2)]
        with mock.patch.object(m, 'watch_groups', return_value={'root': {'items': refs}}), \
                mock.patch.object(m, 'wake_start'), mock.patch.object(m, 'WAKE_INTERVAL', 0), \
                mock.patch.object(m, 'wake_send', return_value=True) as send:
            m.request_wake(self.inst, n, 'fixture first', items=refs[:1])
            m.wake_drain(self.inst, n, once=True)
            consume_fixture_wake(m, self.inst, n)
            # Display history is bounded and is not an acknowledgement ledger.
            history = m.wake_directory(self.inst, n) / 'history.json'
            history.write_text(json.dumps({'version': 1, 'deliveries': []}))
            m.request_wake(self.inst, n, 'fixture already delivered', items=refs[:1])
            m.wake_drain(self.inst, n, once=True)
            consume_fixture_wake(m, self.inst, n)
            self.assertEqual(send.call_count, 1)  # the delivered ref stays acknowledged
            m.request_wake(self.inst, n, 'fixture displayed only', items=refs[1:])
            m.wake_drain(self.inst, n, once=True)
            consume_fixture_wake(m, self.inst, n)
            self.assertEqual(send.call_count, 2)  # undelivered work causes the next wake

    def test_many_resolved_deliveries_retain_one_complete_comparison_baseline(self):
        m, n = self.m, self.inst.node('root')
        delivered = []
        with mock.patch.object(m, 'wake_start'), mock.patch.object(m, 'WAKE_INTERVAL', 0), \
                mock.patch.object(m, 'wake_send', return_value=True) as send:
            for index in range(1, 126):
                # Replacing the source resolves the previous need before the next delivery.
                (self.root / 'inbox.md').write_text('- fixture need ' + str(index) + '\n')
                ref = {'id': format(index, '064x'), 'kind': 'inbox'}
                self.assertTrue(m.request_wake(self.inst, n, 'fixture:' + str(index), items=[ref], explicit=True))
                m.wake_drain(self.inst, n, once=True)
                consume_fixture_wake(m, self.inst, n)
                delivered.append(ref)
        directory = m.wake_directory(self.inst, n)
        history = m.wake_history(directory)
        self.assertEqual(send.call_count, len(delivered))
        self.assertEqual(len(history), 100)
        self.assertEqual(m.wake_previous_items(directory), delivered[:-1])
        self.assertEqual(sum(len(row.get('before', [])) for row in history), len(delivered) - 1)
        self.assertEqual([row['snapshot'] for row in history], [[ref] for ref in delivered[-100:]])
        self.assertEqual(history[-1]['items'], delivered[-1:])

    def test_history_is_parsed_once_per_file_generation(self):
        m, n = self.m, self.inst.node('root')
        binding = m.wake_owner_binding(self.inst, n)
        history = m.wake_directory(self.inst, n) / 'history.json'
        history.parent.mkdir(parents=True, exist_ok=True)
        def write(key):
            row = {'at': 1.0, 'items': [], 'snapshot': [{'id': key * 64, 'kind': 'inbox'}], 'ack_binding': binding}
            history.write_text(json.dumps({'version': 1, 'deliveries': [row]}))
        write('a')
        parse = m.wake_history_parse
        with mock.patch.object(m, 'wake_history_parse', side_effect=parse) as parsed:
            for _ in range(3):
                self.assertEqual(m.wake_shown(self.inst, n), {'a' * 64})
            m.wake_history(history.parent).append('caller-owned copy')
            self.assertEqual(len(m.wake_history(history.parent)), 1)
            self.assertEqual(parsed.call_count, 1)
            write('b')  # same size, rewritten in place: the contents decide, not file metadata
            self.assertEqual(m.wake_shown(self.inst, n), {'b' * 64})
            self.assertEqual(parsed.call_count, 2)
            # A same-size rewrite within one timestamp tick: every stat field is unchanged.
            info, stat, fstat = history.stat(), os.stat, os.fstat
            def frozen(real):
                def call(*args, **kwargs):
                    result = real(*args, **kwargs)
                    return info if (result.st_dev, result.st_ino) == (info.st_dev, info.st_ino) else result
                return call
            torn = history.read_bytes().replace(b'"items"', b'"itemz"')
            with mock.patch('os.stat', side_effect=frozen(stat)), mock.patch('os.fstat', side_effect=frozen(fstat)):
                history.write_bytes(torn)
                self.assertEqual(os.stat(history), info)
                self.assertEqual(m.wake_shown(self.inst, n), set())
                with self.assertRaises(ValueError):
                    m.wake_history(history.parent)
            for encoding in ('utf-8-sig', 'utf-16'):  # json.loads(bytes) would accept these
                write('c')
                history.write_bytes(history.read_text().encode(encoding))
                self.assertEqual(m.wake_shown(self.inst, n), set())
                with self.assertRaises(ValueError):
                    m.wake_history(history.parent)
            write('b')
            history.write_text('{"version": 1, "deliveries": [{}]}')
            self.assertEqual(m.wake_shown(self.inst, n), set())
            with self.assertRaises(ValueError):
                m.wake_history(history.parent)
            history.unlink()
            self.assertEqual(m.wake_history(history.parent), [])

    def test_shown_receipt_keeps_kind_authority_when_history_is_unavailable(self):
        m, n = self.m, self.inst.node('root')
        refs = [{'id': 'a' * 64, 'kind': kind} for kind in ('rotate', 'inbox')]
        directory = m.wake_directory(self.inst, n)
        with mock.patch.object(m, 'wake_start'), mock.patch.object(m, 'WAKE_INTERVAL', 0), \
                mock.patch.object(m, 'wake_send', return_value=True) as send:
            for index, ref in enumerate(refs):
                self.assertTrue(m.request_wake(self.inst, n, 'kind:' + str(index), items=[ref]))
                m.wake_drain(self.inst, n, once=True)
                consume_fixture_wake(m, self.inst, n)
                self.assertEqual(send.call_count, index + 1)
                (directory / 'history.json').unlink()
            for index, ref in enumerate(refs):
                self.assertTrue(m.request_wake(self.inst, n, 'retry:' + str(index), items=[ref]))
                m.wake_drain(self.inst, n, once=True)
                consume_fixture_wake(m, self.inst, n)
            self.assertEqual(send.call_count, 2, 'both actually delivered kinds stay acknowledged')

    def test_legacy_id_only_shown_receipt_does_not_certify_a_typed_need(self):
        m, n = self.m, self.inst.node('root')
        ref = {'id': 'b' * 64, 'kind': 'inbox'}
        path = m.wake_shown_path(self.inst, n)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'version': 1, 'ids': [ref['id']]}))
        self.assertEqual(m.wake_shown(self.inst, n), {ref['id']})
        with mock.patch.object(m, 'wake_start'), mock.patch.object(m, 'WAKE_INTERVAL', 0), \
                mock.patch.object(m, 'wake_send', return_value=True) as send:
            self.assertTrue(m.request_wake(self.inst, n, 'legacy kind unknown', items=[ref]))
            m.wake_drain(self.inst, n, once=True)
            consume_fixture_wake(m, self.inst, n)
            send.assert_called_once()
            (path.parent / 'history.json').unlink()
            self.assertTrue(m.request_wake(self.inst, n, 'typed receipt now known', items=[ref]))
            m.wake_drain(self.inst, n, once=True)
            consume_fixture_wake(m, self.inst, n)
            self.assertEqual(send.call_count, 1)

    def test_shown_receipt_preserves_corrupt_bytes_and_retries_publication(self):
        m, n = self.m, self.inst.node('root')
        refs = [{'id': str(i) * 64, 'kind': 'inbox'} for i in (1, 2)]
        path = m.wake_shown_path(self.inst, n)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'\xfftorn')
        atomic, failed = m.atomic_write, []
        def write(target, value, **kwargs):
            if target == path and not failed:
                failed.append(True)
                raise OSError('fixture transient shown receipt failure')
            return atomic(target, value, **kwargs)
        with mock.patch.object(m, 'watch_groups', return_value={'root': {'items': refs}}), \
                mock.patch.object(m, 'wake_start'), mock.patch.object(m, 'WAKE_INTERVAL', 0), \
                mock.patch.object(m, 'wake_send', return_value=True) as send:
            m.request_wake(self.inst, n, 'fixture first', items=refs[:1])
            with mock.patch.object(m, 'atomic_write', side_effect=write):
                m.wake_drain(self.inst, n, once=True)
                consume_fixture_wake(m, self.inst, n)
            self.assertTrue(failed)
            self.assertEqual([p.read_bytes() for p in path.parent.glob('shown-invalid-*.json')], [b'\xfftorn'])
            (path.parent / 'history.json').write_text(json.dumps({'version': 1, 'deliveries': []}))
            m.request_wake(self.inst, n, 'fixture already delivered', items=refs[:1])
            m.wake_drain(self.inst, n, once=True)
            consume_fixture_wake(m, self.inst, n)
            self.assertEqual(send.call_count, 1)  # the retried receipt alone acknowledges the delivered ref
            m.request_wake(self.inst, n, 'fixture displayed only', items=refs[1:])
            m.wake_drain(self.inst, n, once=True)
            consume_fixture_wake(m, self.inst, n)
            self.assertEqual(send.call_count, 2)  # undelivered work causes the next wake


    def test_terminal_receipt_retry_and_cross_route_fence(self):
        m = self.m
        self.b(self.root, 'new', 'child', '--title', 'Child')
        self.b(self.root, 'set', 'child', 'coordinator=fixture-child', 'wake=fixture-child-send')
        self.inst = m.Instance(self.root)
        owner, root = self.inst.node('child'), self.inst.node('root')
        ref = {'id': 'a' * 64, 'kind': 'machine-exited'}
        original, faults, calls = m.atomic_write, [], []
        def write(path, value, **kwargs):
            if path == m.wake_terminal_receipt(self.inst, ref['id']) and not faults:
                faults.append(True)
                raise OSError('fixture receipt publication fault')
            return original(path, value, **kwargs)
        def send(command, reason=None):
            calls.append(command)
            m.wake_drain(self.inst, root, once=True)
            return True
        with mock.patch.object(m, 'wake_start'), mock.patch.object(m, 'WAKE_INTERVAL', 0), \
                mock.patch.object(m, 'wake_snapshot', return_value=[ref]), \
                mock.patch.object(m, 'wake_send', side_effect=send), mock.patch.object(m, 'atomic_write', side_effect=write):
            m.request_wake(self.inst, root, 'fixture-root', items=[ref])
            m.request_wake(self.inst, owner, 'fixture-owner', items=[ref])
            m.wake_drain(self.inst, owner, once=True)
            consume_fixture_wake(m, self.inst, owner)
            m.wake_drain(self.inst, root, once=True)
            self.assertEqual(calls, ['fixture-child-send'])
            self.assertTrue(faults)
            self.assertTrue(m.wake_terminal_delivered(self.inst, ref['id']))
            self.assertFalse(list(m.wake_directory(self.inst, root).glob('*.pending')))

    def test_same_reason_rotated_binding_gets_its_own_delivery(self):
        m, n = self.m, self.inst.node('root')
        ref = {'id': 'b' * 64, 'kind': 'inbox'}
        with mock.patch.object(m, 'wake_start'), mock.patch.object(m, 'WAKE_INTERVAL', 0), \
                mock.patch.object(m, 'wake_snapshot', return_value=[ref]), \
                mock.patch.object(m, 'wake_send', return_value=True) as send:
            m.request_wake(self.inst, n, 'fixture-same-reason', items=[ref])
            m.wake_drain(self.inst, n, once=True)
            consume_fixture_wake(m, self.inst, n)
            self.b(self.root, 'set', 'root', 'coordinator=fixture-new')
            self.inst = m.Instance(self.root)
            n = self.inst.node('root')
            m.request_wake(self.inst, n, 'fixture-same-reason', items=[ref])
            m.wake_drain(self.inst, n, once=True)
            consume_fixture_wake(m, self.inst, n)
            self.assertEqual(send.call_count, 2)


class NativeHostExport(Scenario):
    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(os.environ, self.env)
        patch.start()
        self.addCleanup(patch.stop)
        self.m = core_loader.SourceFileLoader('host_export_fixture', str(CORE / 'bin/brain')).load_module()
        self.root = self.init('alice')
        self.b(self.root, 'set', 'root', 'host_project=fixture-project', 'coordinator=fixture-root')
        self.inst = self.m.Instance(self.root)
        row = dict.fromkeys(self.m.JOB_COLS, '')
        row.update(Job='import:fixture', Started='2020-01-01T00:00Z')
        self.m.write_jobs(self.inst, self.inst.node('root'), [row])
        self.inst.local_of('root')['workers'] = {self.m.job_key(row): 'fixture-worker'}
        self.inst.save_local()

    def rpc(self, name, args):
        if name == 't3_thread_read':
            return {'thread': {'threadId': args['threadId'], 'activeRunId': None,
                               'latestRunId': 'run-root', 'status': 'running'}, 'recentRuns': []}
        if name == 't3_thread_list':
            return {'threads': [{'threadId': 'fixture-root', 'status': 'running', 'latestRunId': 'run-root'},
                                {'threadId': 'fixture-worker', 'status': 'failed', 'latestRunId': 'run-worker'}],
                    'nextCursor': None}
        self.assertEqual(name, 't3_pending_request_list')
        return {'requestIds': ['private-request'] if args['threadId'] == 'fixture-worker' else []}

    def test_export_has_bound_failures_and_opaque_pending_questions(self):
        snapshot = self.m.host_export(self.inst, rpc=self.rpc)
        self.assertNotIn('private-request', json.dumps(snapshot))
        self.assertEqual(self.m.host_export_runs(self.inst)[0]['run'], 'run-worker')
        self.assertEqual(len(self.m.host_observation_items(self.inst)), 1)

    def test_partial_failure_preserves_previous_export_and_rotation_invalidates_it(self):
        self.m.host_export(self.inst, rpc=self.rpc)
        path = self.root / '.brain/host-observations.json'
        old = path.read_bytes()
        with self.assertRaises(ValueError):
            self.m.host_export(self.inst, rpc=lambda *args: {'threads': []})
        self.assertEqual(path.read_bytes(), old)
        self.inst.local_of('root')['coordinator'] = 'fixture-replacement'
        self.inst.save_local()
        self.assertIsNone(self.m.host_snapshot(self.inst))

    def test_sampler_opt_out_makes_no_native_calls(self):
        with mock.patch.object(self.m, 'host_native_call') as call:
            self.m.host_export_if_enabled(self.inst)
            call.assert_not_called()

    def test_real_run_start_and_native_queue_produce_busy_advisory_without_invented_host_queue(self):
        import datetime
        now = self.inst.now_dt()
        started = (now - datetime.timedelta(minutes=25)).isoformat()
        def rpc(name, args):
            if name == 't3_thread_read':
                return {'thread': {'threadId': 'fixture-root', 'activeRunId': 'run-root',
                                   'latestRunId': 'run-root', 'status': 'running'},
                        'recentRuns': [{'runId': 'run-root', 'startedAt': started, 'status': 'running'}]}
            return self.rpc(name, args)
        self.m.host_export(self.inst, rpc=rpc)
        record = self.m.host_snapshot(self.inst)['records'][0]
        self.assertEqual(record['started_at'], started)
        self.assertNotIn('queued_wakes', record)
        with mock.patch.object(self.m, 'coordinator_queue_view', return_value={
                'count': 1, 'oldest_at': now.timestamp() - 30, 'episode': ['fixture'], 'source': 'coalesced_native_queue'}):
            items = self.m.coordinator_advisories(self.inst)
        self.assertEqual([i['kind'] for i in items], ['coordinator-busy'])

    def test_unknown_or_mismatched_active_run_does_not_invent_start_time(self):
        def rpc(name, args):
            if name == 't3_thread_read':
                return {'thread': {'threadId': 'fixture-root', 'activeRunId': 'new-run',
                                   'latestRunId': 'new-run', 'status': 'running'},
                        'recentRuns': [{'runId': 'run-root', 'startedAt': '2026-01-01T00:00:00Z', 'status': 'running'}]}
            return self.rpc(name, args)
        self.m.host_export(self.inst, rpc=rpc)
        record = self.m.host_snapshot(self.inst)['records'][0]
        self.assertIsNone(record['started_at'])


class WakeIdentityTypes(Scenario):
    def test_malformed_pending_ids_deliver_opaque_requests_without_type_errors(self):
        patch = mock.patch.dict(os.environ, self.env)
        patch.start()
        self.addCleanup(patch.stop)
        m = core_loader.SourceFileLoader('identity_fixture', str(CORE / 'bin/brain')).load_module()
        root = self.init('alice')
        self.b(root, 'set', 'root', 'coordinator=fixture-root', 'wake=fixture-send')
        inst, node = m.Instance(root), None
        node = inst.node('root')
        directory = m.wake_directory(inst, node)
        directory.mkdir(parents=True, exist_ok=True)
        for index, identity in enumerate((7, True, None, [], {}, 'untrusted-identity')):
            with self.subTest(identity=identity):
                pending = directory / (format(index + 1, '064x') + '.pending')
                raw = json.dumps({'items': [{'id': identity, 'kind': 'inbox'}]})
                pending.write_text(raw)
                with mock.patch.object(m, 'WAKE_INTERVAL', 0), mock.patch.object(m, 'wake_start'), \
                        mock.patch.object(m, 'watch_groups', return_value={}), \
                        mock.patch.object(m, 'wake_snapshot', return_value=[]):
                    with mock.patch.object(m, 'wake_send', return_value=False):
                        m.wake_drain(inst, node, once=True)
                    self.assertEqual(pending.read_text(), raw)
                    with mock.patch.object(m, 'wake_send', return_value=True) as send:
                        m.wake_drain(inst, node, once=True)
                    self.assertEqual(send.call_count, 1)
                    self.assertTrue(pending.exists())
                    consume_fixture_wake(m, inst, node)
                    self.assertFalse(pending.exists())
                    self.assertNotIn('untrusted-identity', send.call_args.kwargs['reason'])


class RegisteredDelegatedWake(Scenario):
    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(os.environ, self.env)
        patch.start()
        self.addCleanup(patch.stop)
        self.m = core_loader.SourceFileLoader('delegated_fixture', str(CORE / 'bin/brain')).load_module()
        self.root = self.init('alice')
        self.b(self.root, 'new', 'app', '--title', 'App')
        self.b(self.root, 'set', 'app', 'coordinator=fixture-parent', 'wake=fixture-send')
        self.inst = self.m.Instance(self.root)
        self.parent = 'fixture-parent'
        self.pending_children = True
        self.calls = []

    def rpc(self, name, args):
        self.calls.append((name, args))
        if name == 't3_thread_list':
            return {'currentThreadId': self.parent}
        if name == 'delegate_task':
            self.assertTrue(args['task'].startswith('Automated turn: no operator present; cancellations are host events.'))
            return {'taskId': 'fixture-task', 'childThreadId': 'fixture-child'}
        self.assertEqual(name, 'task_status')
        return {'taskId': args['taskId'], 'status': 'completed', 'childRunId': 'fixture-run',
                'hasPendingChildRuns': self.pending_children,
                'workState': 'waiting_for_children' if self.pending_children else 'result_available'}

    def register(self):
        return self.m.host_delegate(self.inst, self.inst.node('app'), 'Inspect a sample',
                                    {'providerInstanceId': 'fixture', 'model': 'fixture-model'}, 'one-task', rpc=self.rpc)

    def test_registered_task_is_idempotent_and_nested_work_does_not_complete_early(self):
        self.register()
        self.register()
        self.assertEqual(sum(name == 'delegate_task' for name, _ in self.calls), 1)
        with mock.patch.object(self.m, 'wake_start'):
            self.assertEqual(self.m.host_task_refresh(self.inst, rpc=self.rpc)['items'], [])
            self.pending_children = False
            self.assertEqual(len(self.m.host_task_refresh(self.inst, rpc=self.rpc)['items']), 1)
            self.m.host_task_refresh(self.inst, rpc=self.rpc)
        with mock.patch.object(self.m, 'WAKE_INTERVAL', 0), mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            self.m.wake_drain(self.inst, self.inst.node('app'), once=True)
            self.m.wake_drain(self.inst, self.inst.node('app'), once=True)
            self.assertEqual(send.call_count, 1)
        self.assertEqual(len(self.m.host_task_items(self.inst)), 1)
        self.assertEqual(sum(name == 'task_status' for name, _ in self.calls), 2,
                         'immutable completion is not polled again after its receipt')

    def test_wrong_parent_context_cannot_launch_or_claim_completion(self):
        self.parent = 'different-parent'
        with self.assertRaises(ValueError):
            self.register()
        self.assertFalse(any(name == 'delegate_task' for name, _ in self.calls))


class DisabledWakeDiagnostics(Scenario):
    def test_explicit_disable_retains_cached_refusal_and_never_probes_or_drains(self):
        patch = mock.patch.dict(os.environ, self.env)
        patch.start()
        self.addCleanup(patch.stop)
        m = core_loader.SourceFileLoader('disabled_fixture', str(CORE / 'bin/brain')).load_module()
        root = self.init('alice')
        self.b(root, 'set', 'root', 'coordinator=fixture-root', 'wake=https://wake.invalid/fixture',
               'wake_default=https://wake.invalid/fallback')
        inst = m.Instance(root)
        n = inst.node('root')
        directory = m.wake_directory(inst, n)
        m.wake_record(directory, n['local']['wake'], 409)
        inst.local_of('root')['wake_enabled'] = 'off'
        inst.save_local()
        before = (directory / 'delivery.json').read_bytes()
        with mock.patch.object(m, 'load', return_value=inst), \
                mock.patch.object(m, 'collect_problems', return_value=([], [], [])), \
                mock.patch.object(m, 'wake_probe') as probe, mock.patch.object(m, 'wake_send') as send, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            m.cmd_check(argparse.Namespace(fix=True, live=True))
            m.wake_drain(inst, n, once=True)
            self.assertEqual(m.wake_target(inst, n)[1], '')
            self.assertTrue(m.request_wake(inst, n, 'fixture-question'))
            probe.assert_not_called()
            send.assert_not_called()
        self.assertIn('disabled', output.getvalue())
        self.assertIn('409', output.getvalue())
        self.assertEqual((directory / 'delivery.json').read_bytes(), before)

    def test_active_cached_refusal_stays_a_failure(self):
        patch = mock.patch.dict(os.environ, self.env)
        patch.start()
        self.addCleanup(patch.stop)
        m = core_loader.SourceFileLoader('active_fixture', str(CORE / 'bin/brain')).load_module()
        root = self.init('alice')
        self.b(root, 'set', 'root', 'wake=https://wake.invalid/fixture')
        inst = m.Instance(root)
        n = inst.node('root')
        m.wake_record(m.wake_directory(inst, n), n['local']['wake'], 409)
        with mock.patch.object(m, 'load', return_value=inst), \
                mock.patch.object(m, 'collect_problems', return_value=([], [], [])), \
                mock.patch.object(m, 'wake_probe') as probe, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                m.cmd_check(argparse.Namespace(fix=True, live=False))
            self.assertEqual(error.exception.code, 1)
            probe.assert_not_called()
