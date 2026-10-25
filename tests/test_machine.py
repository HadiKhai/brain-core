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


import test_verify as _test_verify
from scenario import Scenario, AutonomyProject, shlex_quote


class MachineCare(Scenario):
    def setUp(self):
        super().setUp()
        self.m = core_loader.SourceFileLoader("brain_machine_care", str(CORE / "bin/brain")).load_module()
        self.patch = mock.patch.dict(os.environ, self.env)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.m.MACHINE_RETRY = 0.01
        self.good = json.loads(self.env["BRAIN_MACHINE_READING"])

    def samples(self, count, reading=None):
        now = time.time()
        return [{"at": now - (count - i - 1) * 60, "reading": dict(reading or self.good)} for i in range(count)]

    def test_sustained_spike_thermal_memory_and_configuration(self):
        m = self.m
        p = m.machine_policy()
        spike = {**self.good, "load_per_cpu": [20, 0.2, 0.2]}
        self.assertEqual(m.machine_reasons(spike, p), [])
        self.assertEqual(m.machine_sustained(self.samples(6, spike), p), [])
        small = {**self.good, "mem_total_gb": 8, "mem_free_gb": 3, "disk_total_gb": 32, "disk_free_gb": 10}
        self.assertEqual(m.machine_reasons(small, p), [])
        strict = {**p, "_configured": ["memory_free_gb"]}
        self.assertIn("threshold exceeds machine", m.machine_reasons(small, strict)[0])
        high = {**spike, "load_per_cpu": [20, 1, 1]}
        self.assertIn("5-minute", m.machine_reasons(high, p)[0])
        self.assertEqual(m.machine_sustained(self.samples(4, high), p), [])
        self.assertEqual(m.machine_sustained(self.samples(5, high), p), ["sustained 5-minute load"])
        thermal = {**self.good, "thermal": "pressure"}
        self.assertEqual(m.machine_sustained(self.samples(2, thermal), p), [])
        self.assertEqual(m.machine_sustained(self.samples(3, thermal), p), ["sustained thermal pressure"])
        gap = self.samples(5, high)
        gap[2]["at"] -= 40
        self.assertEqual(m.machine_sustained(gap, p), [])
        self.assertEqual(len(m.machine_reasons({**self.good, "mem_free_gb": 1, "disk_free_gb": 2}, p)), 2)
        self.assertEqual(m.machine_limit(p, 12), 2)
        self.assertEqual(m.machine_limit(p, 23), 1)
        self.assertEqual(m.machine_limit({**p, "night_start": 8, "night_end": 18}, 12), 1)
        m.machine_dir().mkdir(parents=True)
        (m.machine_dir() / "machine-care.json").write_text('{"verify_day": 0}')
        with self.assertRaises(SystemExit):
            m.machine_policy()

    def test_os_readings_are_injected_and_thermal_unknown_is_explicit(self):
        m = self.m
        vm = "Mach Virtual Memory Statistics: (page size of 4096 bytes)\nPages free: 1000000.\nPages inactive: 2000000.\nPages speculative: 9000000."
        def command(args, **kw):
            if args[0] == "vm_stat":
                return subprocess.CompletedProcess(args, 0, vm)
            if args[0] == "sysctl":
                return subprocess.CompletedProcess(args, 0, "64000000000")
            return subprocess.CompletedProcess(args, 0, "CPU_Speed_Limit = 70\nCPU_Scheduler_Limit = 100")
        with mock.patch.object(m.sys, "platform", "darwin"), mock.patch.object(m.os, "getloadavg", return_value=(16, 4, 2)),                 mock.patch.object(m.os, "cpu_count", return_value=8),                 mock.patch.object(m.subprocess, "run", side_effect=command),                 mock.patch.object(m.shutil, "disk_usage", return_value=shutil._ntuple_diskusage(400000000000, 100000000000, 300000000000)):
            reading = m.machine_load()
            self.assertEqual(reading["load_per_cpu"], [2, .5, .25])
            self.assertEqual(reading["mem_free_gb"], 12.3)
            self.assertEqual(reading["thermal"], "pressure")
            with mock.patch.object(m.subprocess, "run", side_effect=OSError("fixture unavailable")):
                self.assertEqual(m.machine_load()["thermal"], "unknown")
        cooling = Path("/fixture/cooling_device0/cur_state")
        def linux_text(path):
            if path.name == "type":
                return "Processor"
            if path.name == "cur_state":
                return "1"
            return "MemFree: 10000000 kB\nInactive: 20000000 kB\nMemTotal: 64000000 kB\n"
        with mock.patch.object(m.sys, "platform", "linux"),                 mock.patch.object(m.Path, "exists", return_value=True),                 mock.patch.object(m.Path, "read_text", linux_text),                 mock.patch.object(m.Path, "glob", return_value=[cooling]):
            reading = m.machine_load()
            self.assertEqual(reading["mem_free_gb"], 30.7)
            self.assertEqual(reading["thermal"], "pressure")

    def test_slots_are_shared_across_registry_and_xdg_overrides(self):
        m = self.m
        with mock.patch.dict(os.environ, {"BRAIN_CONFIG": str(self.tmp / "one/brains.json"),
                                          "XDG_CONFIG_HOME": str(self.tmp / "one")}):
            token = m.machine_wait("verify")
        with mock.patch.dict(os.environ, {"BRAIN_CONFIG": str(self.tmp / "two/brains.json"),
                                          "XDG_CONFIG_HOME": str(self.tmp / "two")}):
            with m.machine_queue() as q:
                self.assertEqual([r["token"] for r in q.rows], [token])
            m.machine_release(token)

    def test_process_identity_ignores_locale_timezone_and_unknown_is_not_dead(self):
        m = self.m
        stamp = m.process_stamp(os.getpid())
        limit = m.machine_limit(m.machine_policy())
        with mock.patch.dict(os.environ, {"TZ": "GMT+11", "LC_ALL": "fixture_invalid_locale"}):
            self.assertEqual(m.process_stamp(os.getpid()), stamp)
            self.assertEqual(m.machine_limit(m.machine_policy()), limit)
        with mock.patch.object(m.sys, "platform", "darwin"), \
                mock.patch.object(m.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "fixture")):
            with mock.patch.object(m.os, "kill", return_value=None):
                self.assertEqual(m.process_stamp(12345), "unknown")
            with mock.patch.object(m.os, "kill", side_effect=ProcessLookupError()):
                self.assertIsNone(m.process_stamp(12345))
        with mock.patch.object(m, "process_stamp", return_value="unknown"), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                m.machine_wait("verify")

    def test_incomplete_fresh_history_does_not_weaken_live_admission(self):
        m = self.m
        root = self.tmp / "oak"
        (root / ".brain").mkdir(parents=True)
        inst = argparse.Namespace(root=root)
        path = root / ".brain/machine.json"
        high = {**self.good, "load_per_cpu": [1, 1, 1]}
        with mock.patch.dict(os.environ, {"BRAIN_MACHINE_TESTING": "0"}), \
                mock.patch.object(m, "machine_load", side_effect=AssertionError("fresh sample first")):
            path.write_text(json.dumps({"samples": self.samples(1, high)}))
            self.assertTrue(m.machine_reading(inst)["sample_reasons"])
            spike = {**self.good, "load_per_cpu": [20, .2, .2]}
            path.write_text(json.dumps({"samples": self.samples(1, spike)}))
            self.assertEqual(m.machine_reading(inst)["sample_reasons"], [])

    def test_hold_retries_and_releases(self):
        m = self.m
        with mock.patch.object(m, "machine_reading", side_effect=[{**self.good, "thermal": "pressure"}, self.good]), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            token = m.machine_wait()
        self.assertIn("thermal pressure", output.getvalue())
        m.machine_release(token)
        with m.machine_queue() as q:
            self.assertEqual(q.rows, [])

    def test_due_machine_actions_are_scoped_and_episode_notices_are_single(self):
        m = self.m
        root, other = self.init("oak-brain"), self.init("elm-brain")
        self.b(root, "new", "maple", "--title", "Maple")
        self.b(root, "new", "ash", "--title", "Ash")
        inst, other_inst = m.Instance(root), m.Instance(other)
        high = {**self.good, "load_per_cpu": [1, .92, .92]}

        def due(node=None, machine=False, quiet=False):
            with mock.patch.object(m, "load", return_value=inst), \
                    mock.patch.object(m, "core_update", return_value=None), mock.patch.object(m, "auto_upgrade", return_value=None), \
                    mock.patch.object(m, "changes_due", return_value=[]), \
                    mock.patch.object(m, "machine_reading", return_value=dict(high)), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                if quiet:
                    with self.assertRaises(SystemExit) as result:
                        m.cmd_due(argparse.Namespace(id=node, quiet=True, json=False, machine=machine))
                    self.assertEqual(output.getvalue(), "")
                    return result.exception.code
                m.cmd_due(argparse.Namespace(id=node, quiet=False, json=True, machine=machine))
            return json.loads(output.getvalue())

        # High load is a fact, not an action for any idle node, including root.
        for node in (None, "root", "maple", "ash"):
            self.assertEqual(due(node), [])
            self.assertEqual(due(node, quiet=True), 0)
            self.assertEqual(len(due(node, machine=True)), 1)
        self.assertFalse(m.machine_dir().exists())  # informational reads publish nothing
        owner = [os.getpid(), m.process_stamp(os.getpid())]
        def row(i, instance, node, state="queued"):
            return {"token": f"{i:032x}", "owner": owner, "kind": "verify", "state": state,
                    "scope": m.machine_scope(instance, node)}
        with m.machine_queue() as q:
            q.rows = [row(1, other_inst, "root")]
        self.assertEqual(due("root"), [])  # another brain never wakes this one
        with m.machine_queue() as q:
            q.rows += [row(2, inst, "maple"), row(3, inst, "maple"), row(4, inst, "ash", "running")]
        self.assertEqual(len(due("maple")), 1)
        self.assertEqual(due("maple", quiet=True), 0)  # the ordinary due result consumed this episode
        self.assertEqual(due("ash"), [])  # running work is left alone
        self.assertEqual(due("ash", quiet=True), 0)
        self.assertEqual(len([i for i in due() if i["kind"] == "machine"]), 1)
        self.assertEqual(len(due("root", machine=True)), 1)
        with m.machine_queue() as q:
            q.rows = []
        inbox = root / "inbox.md"
        initial = inbox.read_text()
        for transition in ("open", "close"):
            line = "- fixture from brain: " + ("hold heavy work: sustained 5-minute load" if transition == "open"
                                               else "heavy work hold cleared")
            inbox.write_text(initial + line + " [machine episode " + "a" * 24 + " " + transition + "]\n")
            # Retired machine records do not change inbox attention or stale the facts.
            self.assertFalse(any(i["kind"] == "stale-status" for i in due("root")))
            m.refresh_status(inst, inst.node("root"))
            self.assertEqual(due("root"), [])  # old sampler lines no longer create coordinator work
            self.assertEqual(due("maple"), [])
            self.assertEqual(due("ash"), [])
            self.assertEqual(len([i for i in due() if i["kind"] == "machine"]), 0)
            inbox.write_text(initial)  # Removing historical noise is equally quiet.
            self.assertFalse(any(i["kind"] == "stale-status" for i in due("root")))
            m.refresh_status(inst, inst.node("root"))
            self.assertEqual(due("root"), [])  # heat still high, no repeated synthetic episode
        inbox.write_text(initial + "- fixture question: choose a plan\n")
        self.assertTrue(any(i["kind"] == "stale-status" for i in due("root")))
        self.assertTrue(any(i["kind"] == "inbox" for i in due("root")))
        # Snapshot telemetry never makes a quiet node's status stale as load varies.
        n = inst.node("maple")
        with mock.patch.object(m, "machine_reading", return_value=dict(high)):
            m.refresh_status(inst, n)
        self.assertIn("Machine (snapshot", (n["dir"] / "status.md").read_text())
        with mock.patch.object(m, "machine_reading", return_value=dict(self.good)):
            self.assertIsNone(m.status_lag(inst, n))

    def test_fifo_day_and_night_with_exit_codes(self):
        m = self.m
        for limit in (2, 1):
            started, ended, results, errors = [], [], [], []
            active = [0, 0]
            mutex = threading.Lock()
            entered = [threading.Event() for _ in range(3)]
            release = [threading.Event() for _ in range(3)]
            finished = [threading.Event() for _ in range(3)]
            def worker(i):
                try:
                    token = m.machine_wait("verify")
                    try:
                        with mutex:
                            started.append(i)
                            active[0] += 1
                            active[1] = max(active)
                        entered[i].set()
                        # Hold the slot until the controller has observed admission.
                        # These timeouts detect deadlocks, not scheduler performance.
                        self.assertTrue(release[i].wait(10), "controller did not release worker")
                        r = subprocess.run([sys.executable, "-c", "raise SystemExit(" + str(i) + ")"])
                        with mutex:
                            results.append((i, r.returncode))
                            ended.append(i)
                            active[0] -= 1
                    finally:
                        m.machine_release(token)
                except BaseException as e:
                    errors.append(e)
                finally:
                    finished[i].set()
            with mock.patch.object(m, "machine_limit", return_value=limit), contextlib.redirect_stdout(io.StringIO()):
                threads = []
                try:
                    for i in range(3):
                        t = threading.Thread(target=worker, args=(i,))
                        t.start()
                        threads.append(t)
                        # Earlier slots stay occupied, so queued rows cannot disappear.
                        deadline = time.monotonic() + 5
                        while time.monotonic() < deadline:
                            with m.machine_queue() as q:
                                if len(q.rows) == i + 1:
                                    break
                            time.sleep(.002)
                        else:
                            self.fail("worker did not join the durable queue")
                        if i < limit:
                            self.assertTrue(entered[i].wait(10), "available slot was not admitted")
                    self.assertEqual(started, list(range(limit)))
                    for i in range(3):
                        self.assertTrue(entered[i].wait(10), "FIFO worker was not admitted")
                        release[i].set()
                        self.assertTrue(finished[i].wait(10), "worker did not finish")
                finally:
                    for event in release:
                        event.set()
                    for t in threads:
                        t.join(10)
                    self.assertFalse(any(t.is_alive() for t in threads))
            self.assertEqual(errors, [])
            self.assertEqual(started, [0, 1, 2])
            self.assertEqual(ended if limit == 1 else sorted(ended), [0, 1, 2])
            self.assertEqual(sorted(results), [(0, 0), (1, 1), (2, 2)])
            self.assertEqual(active[1], limit)

    def test_full_runner_cross_process_limits(self):
        roots = [self.init(name) for name in ('oak', 'elm', 'ash')]
        home = self.repo(self.tmp / 'fixture-home')
        for root in roots:
            self.b(root, 'set', 'root', 'home=' + str(home))
        self.native_runner_capacity(roots, ['root'] * 3, (1, 2))

    def test_node_gates_and_configured_capacity_across_native_runners(self):
        root = self.init('oak')
        for node in ('elm', 'ash'):
            self.b(root, 'new', node, '--title', node.title())
        home = self.repo(self.tmp / 'fixture-home')
        nodes = ['root', 'elm', 'ash']
        for node in nodes:
            self.b(root, 'set', node, 'home=' + str(home))
        self.native_runner_capacity([root] * 3, nodes, (1, 2, 3))

    def native_runner_capacity(self, roots, nodes, limits):
        m = self.m
        events = self.tmp / "events.jsonl"
        child = self.tmp / "barrier_child.py"
        child.write_text(r"""import json, sys, time
from pathlib import Path
events, barrier, i = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
with events.open('a') as f:
    f.write(json.dumps(['start', i, time.monotonic()]) + '\n')
(barrier / ('started-' + str(i))).touch()
deadline = time.monotonic() + 30
while not (barrier / ('release-' + str(i))).exists():
    if time.monotonic() >= deadline:
        raise SystemExit('barrier timeout: runner ' + str(i) + ' was not released')
    time.sleep(.01)
with events.open('a') as f:
    f.write(json.dumps(['end', i, time.monotonic()]) + '\n')
raise SystemExit(i)
""")
        script = self.tmp / "runner.py"
        script.write_text(r"""import core_loader, json, os, shlex, sys, time
from pathlib import Path
m = core_loader.SourceFileLoader('fixture_runner', sys.argv[1]).load_module()
m.MACHINE_RETRY = .01
# Capacity comes from machine-care.json, not a patched limiter.
root, events, i = sys.argv[2:5]
inst = m.Instance(Path(root))
command = ' '.join(shlex.quote(arg) for arg in
                   [sys.executable, sys.argv[6], events, sys.argv[7], i])
result, log = m.run_verify(inst, inst.node(sys.argv[8]), 'fixture', command, {'path': root}, dict(os.environ))
raise SystemExit(result)
""")
        for limit in limits:
            m.machine_dir().mkdir(parents=True, exist_ok=True)
            (m.machine_dir() / "machine-care.json").write_text(json.dumps(
                {"verify_day": limit, "verify_night": 1, "night_start": 0, "night_end": 0}))
            events.write_text("")
            barrier = self.tmp / ('barrier-' + str(limit))
            barrier.mkdir()
            processes = []

            def wait_for(condition, message):
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if condition():
                        return
                    time.sleep(.01)
                self.fail('barrier timeout: ' + message + '; runner statuses: ' +
                          repr([p.poll() for p in processes]))

            def queue_rows():
                with m.machine_queue() as q:
                    return list(q.rows)

            try:
                for i, root in enumerate(roots):
                    processes.append(subprocess.Popen([sys.executable, str(script), str(CORE / "bin/brain"),
                        str(root), str(events), str(i), str(limit), str(child), str(barrier), nodes[i]], env=self.env,
                        start_new_session=True,
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE))
                    wait_for(lambda: len(queue_rows()) >= i + 1,
                             'runner ' + str(i) + ' did not join the durable queue')
                    if i < limit:
                        wait_for(lambda: (barrier / ('started-' + str(i))).exists(),
                                 'available slot ' + str(i) + ' was not admitted')

                # Admitted children cannot finish until released. This observes the
                # actual overlap and the held third runner without a timing window.
                self.assertEqual([r['state'] for r in queue_rows()],
                                 ['running'] * limit + ['queued'] * (3 - limit))
                self.assertEqual(sorted(p.name for p in barrier.glob('started-*')),
                                 ['started-' + str(i) for i in range(limit)])
                held = [json.loads(line) for line in events.read_text().splitlines()]
                self.assertEqual([row[:2] for row in held],
                                 [['start', i] for i in range(limit)])
                self.assertTrue(all(p.poll() is None for p in processes))

                results = []
                for i, p in enumerate(processes):
                    wait_for(lambda: (barrier / ('started-' + str(i))).exists(),
                             'queued runner ' + str(i) + ' was not admitted after release')
                    (barrier / ('release-' + str(i))).touch()
                    results.append(p.wait(timeout=10))
                errors = [p.stderr.read().decode() for p in processes]
                self.assertEqual(results, [0, 1, 2], errors)
                rows = [json.loads(line) for line in events.read_text().splitlines()]
                active, peak, starts = 0, 0, []
                for kind, i, at in rows:
                    active += 1 if kind == "start" else -1
                    peak = max(peak, active)
                    if kind == "start":
                        starts.append(i)
                self.assertEqual(starts, [0, 1, 2])
                self.assertEqual(peak, limit)
                self.assertEqual(active, 0)
            finally:
                for i in range(len(processes)):
                    (barrier / ('release-' + str(i))).touch()
                for p in processes:
                    # Each runner owns its session, including the verify shell and
                    # child. Failed assertions must not leave a barrier waiter alive.
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(p.pid, 9)
                    p.wait()
                    p.stderr.close()

    def test_lane_owner_requeues_behind_waiting_verify(self):
        m = self.m
        root, home, wt = _test_verify.WorkerReadyPackets.job(self)
        self.b(root, 'new', 'other', '--title', 'Other', '--home', str(home))
        inst = m.Instance(root)
        n = inst.node('a')
        w, env = {'path': str(wt)}, dict(os.environ)
        stream = m.run_verify_stream
        for fitness in (False, True):
            with self.subTest(fitness=fitness):
                started, tokens, errors, threads = [], [], [], []
                first = 'fitness' if fitness else 'first'
                n['fm']['verify_lanes_check'] = 'true' if fitness else ''
                lanes = [dict(name=name, command='true', schedule='job', requires=[], always=False)
                         for name in (["second"] if fitness else ["first", "second"])]
                snap = dict(lanes=lanes, hashes={l['name']: 'fixture' for l in lanes},
                            context='fixture', map_hash='fixture', config_hash='fixture')

                def waiting():
                    try:
                        m.run_verify(inst, inst.node('other'), 'waiting', 'true', w, env)
                    except BaseException as e:
                        errors.append(e)

                def observe(inst, n, branch, command, w, env, token, lane=None):
                    started.append(lane or 'waiting')
                    tokens.append(token)
                    if lane == first:
                        thread = threading.Thread(target=waiting)
                        threads.append(thread)
                        thread.start()
                        deadline = time.monotonic() + 5
                        while time.monotonic() < deadline:
                            with m.machine_queue() as q:
                                if len(q.rows) == 2:
                                    self.assertEqual([r['state'] for r in q.rows], ['running', 'queued'])
                                    break
                            time.sleep(.002)
                        else:
                            self.fail('waiting verify did not join the queue')
                    return stream(inst, n, branch, command, w, env, token, lane=lane)

                with mock.patch.object(m, 'machine_limit', return_value=1), \
                        mock.patch.object(m, 'run_verify_stream', side_effect=observe), \
                        contextlib.redirect_stdout(io.StringIO()):
                    try:
                        result = m.run_lanes(inst, n, 'job', w, env, snap, None, 'head', 'main', {})
                    finally:
                        for thread in threads:
                            thread.join(10)
                            self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(result['verify_result'], 0)
                self.assertEqual(started, [first, 'waiting', 'second'])
                self.assertEqual(len(set(tokens)), 3)
                with m.machine_queue() as q:
                    self.assertEqual(q.rows, [])

    def test_one_verify_keeps_slot_until_all_parallel_children_finish(self):
        m = self.m
        root, home, wt = _test_verify.WorkerReadyPackets.job(self)
        inst = m.Instance(root)
        n = inst.node('a')
        starts = [self.tmp / ('start-' + str(i)) for i in range(2)]
        finishes = [self.tmp / ('finish-' + str(i)) for i in range(2)]
        done = [self.tmp / ('done-' + str(i)) for i in range(2)]
        child = ("import sys,time; from pathlib import Path; Path(sys.argv[1]).touch()\n"
                 "while not Path(sys.argv[2]).exists(): time.sleep(.01)\n"
                 "Path(sys.argv[3]).touch()\n")
        code = ('import subprocess,sys; children=[subprocess.Popen([sys.executable,"-c",' +
                repr(child) + ',start,finish,done]) for start,finish,done in ' +
                repr([(str(s), str(f), str(d)) for s, f, d in zip(starts, finishes, done)]) +
                ']; results=[p.wait() for p in children]; raise SystemExit(any(results))')
        command = 'exec ' + shlex.quote(sys.executable) + ' -c ' + shlex.quote(code)
        result, errors = [], []

        def run():
            try:
                result.append(m.run_verify(inst, n, 'job', command, {'path': str(wt)}, dict(os.environ))[0])
            except BaseException as e:
                errors.append(e)

        thread = threading.Thread(target=run)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not all(p.exists() for p in starts) and time.monotonic() < deadline:
                time.sleep(.002)
            self.assertTrue(all(p.exists() for p in starts))
            with m.machine_queue() as q:
                self.assertEqual(len(q.rows), 1)
                reservation = dict(q.rows[0])
                self.assertEqual(reservation['state'], 'running')
            finishes[0].touch()
            deadline = time.monotonic() + 5
            while not done[0].exists() and time.monotonic() < deadline:
                time.sleep(.002)
            self.assertTrue(done[0].exists())
            self.assertFalse(done[1].exists())
            # The second child keeps the command, and thus its sole reservation, alive.
            self.assertTrue(thread.is_alive())
            with m.machine_queue() as q:
                self.assertEqual(q.rows, [reservation])
        finally:
            for path in finishes:
                path.touch()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result, [0])
        with m.machine_queue() as q:
            self.assertEqual(q.rows, [])

    def test_lane_reservation_releases_on_exception(self):
        m = self.m
        root, home, wt = _test_verify.WorkerReadyPackets.job(self)
        inst = m.Instance(root)
        n = inst.node('a')
        lane = dict(name='fixture', command='true', schedule='job', requires=[], always=False)
        snap = dict(lanes=[lane], hashes={'fixture': 'fixture'},
                    context='fixture', map_hash='fixture', config_hash='fixture')
        for fitness in (False, True):
            with self.subTest(fitness=fitness):
                n['fm']['verify_lanes_check'] = 'true' if fitness else ''
                with mock.patch.object(m, 'run_verify_stream', side_effect=KeyboardInterrupt), \
                        contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(KeyboardInterrupt):
                        m.run_lanes(inst, n, 'job', {'path': str(wt)}, dict(os.environ),
                                    snap, None, 'head', 'main', {})
                with m.machine_queue() as q:
                    self.assertEqual(q.rows, [])

    def test_runner_publication_barrier_and_daemon_reclamation(self):
        m = self.m
        root, home, wt = _test_verify.WorkerReadyPackets.job(self)
        script = self.tmp / "fixture-runner.py"
        stage = self.tmp / "publication-stage"
        command_started = self.tmp / "command-started"
        script.write_text(r"""import core_loader, os, sys, time
from pathlib import Path
m=core_loader.SourceFileLoader('fixture',sys.argv[1]).load_module()
root, wt, command, stage, pause = sys.argv[2:]
if pause == 'yes':
 def before_publication(token,pid):
  Path(stage).write_text(str(pid))
  while True: time.sleep(.01)
 m.machine_child=before_publication
inst=m.Instance(Path(root))
result,log=m.run_verify(inst,inst.node('a'),'job',command,{'path':wt},dict(os.environ))
raise SystemExit(result)
""")
        command = "exec " + shlex.quote(sys.executable) + " -c " + shlex.quote(
            "from pathlib import Path; Path(" + repr(str(command_started)) + ").touch()")
        proc = subprocess.Popen([sys.executable, str(script), str(CORE / "bin/brain"), str(root), str(wt),
                                 command, str(stage), "yes"], env=self.env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not stage.exists() and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(stage.exists())
            self.assertFalse(command_started.exists())
            proc.kill()
            proc.wait()
            with m.machine_queue() as q:
                self.assertEqual(q.rows, [])
            self.assertFalse(command_started.exists())
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stderr.close()
        daemon_file, finish, fds_file = [self.tmp / name for name in ("daemon-pid", "finish-command", "extra-fds")]
        code = ("import os,json,time; from pathlib import Path\nextra=[]\n"
                "for fd in range(3,128):\n"
                " try: os.fstat(fd); extra.append(fd)\n"
                " except OSError: pass\n"
                "Path(" + repr(str(fds_file)) + ").write_text(json.dumps({'fds':extra, "
                "'gate':os.environ['BRAIN_GATE_FD'], 'node':os.environ['BRAIN_GATE_NODE']}))\n"
                "pid=os.fork()\n"
                "if pid == 0:\n os.close(0); os.close(1); os.close(2); time.sleep(30); os._exit(0)\n"
                "Path(" + repr(str(daemon_file)) + ").write_text(str(pid))\n"
                "while not Path(" + repr(str(finish)) + ").exists(): time.sleep(.01)\n")
        command = "exec " + shlex.quote(sys.executable) + " -c " + shlex.quote(code)
        proc = subprocess.Popen([sys.executable, str(script), str(CORE / "bin/brain"), str(root), str(wt),
                                 command, str(stage), "no"], env=self.env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        daemon, stamp, child = None, None, None
        try:
            deadline = time.monotonic() + 5
            while not daemon_file.exists() and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(daemon_file.exists())
            daemon = int(daemon_file.read_text())
            stamp = m.process_stamp(daemon)
            self.assertIsNotNone(stamp)
            descriptors = json.loads(fds_file.read_text())
            self.assertEqual(descriptors['fds'], [int(descriptors['gate'])])
            self.assertEqual(descriptors['node'], 'a')
            with m.machine_queue() as q:
                self.assertEqual(len(q.rows), 1)
                child = q.rows[0]["child"]
            proc.kill()
            proc.wait()
            with m.machine_queue() as q:
                self.assertEqual(len(q.rows), 1)  # the actual command still runs
            finish.touch()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with m.machine_queue() as q:
                    if not q.rows:
                        break
                time.sleep(.01)
            with m.machine_queue() as q:
                self.assertEqual(q.rows, [])  # a live background daemon cannot retain the slot
            self.assertEqual(m.process_stamp(daemon), stamp)
        finally:
            finish.touch()
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stderr.close()
            for who in ([daemon, stamp], child):
                if who and who[0] and m.process_stamp(who[0]) == who[1]:
                    os.kill(who[0], 9)

    def test_stale_pid_and_child_retention(self):
        m = self.m
        token = m.machine_wait("verify")
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2)"])
        try:
            m.machine_child(token, child.pid)
            with m.machine_queue() as q:
                q.rows[0]["owner"][1] = "different process start"
            with m.machine_queue() as q:
                self.assertEqual(len(q.rows), 1)
            child.terminate()
            child.wait()
            with m.machine_queue() as q:
                self.assertEqual(q.rows, [])
            with m.machine_queue() as q:
                q.rows.append({"owner": [os.getpid(), "recycled"], "token": "a" * 32,
                               "kind": "verify", "state": "queued"})
            with m.machine_queue() as q:
                self.assertEqual(q.rows, [])
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()

    def test_samples_fallback_episodes_prune_atomic_and_dry_install(self):
        m = self.m
        root = self.init("oak")
        inst = m.Instance(root)
        path = root / ".brain/machine.json"
        high = {**self.good, "load_per_cpu": [1, 1, 1]}
        with mock.patch.dict(os.environ, {"BRAIN_MACHINE_TESTING": "0"}), \
                mock.patch.object(m, "machine_load", return_value=self.good) as live:
            self.assertEqual(m.machine_reading(inst), self.good)
            path.write_text(json.dumps({"samples": self.samples(5, high)}))
            self.assertIn("sustained", m.machine_reading(inst)["sample_reasons"][0])
            self.assertEqual(live.call_count, 1)
            old = self.samples(1)
            old[0]["at"] -= 181
            path.write_text(json.dumps({"samples": old}))
            self.assertEqual(m.machine_reading(inst), self.good)
        samples = self.samples(5, high)
        samples.insert(0, {"at": time.time() - 90000, "reading": self.good})
        path.write_text(json.dumps({"samples": samples, "episode": False}))
        # Repeated sample calls inside one minute cannot inflate sustained duration.
        with mock.patch.object(m, "machine_load", return_value=high), contextlib.redirect_stdout(io.StringIO()):
            with mock.patch.object(m, "machine_notice", side_effect=KeyboardInterrupt("fixture crash before private log")):
                with self.assertRaises(KeyboardInterrupt):
                    m.machine_sample(inst)
            self.assertIsNotNone(json.loads(path.read_text())["pending_notice"])
            m.machine_sample(inst)
            m.machine_sample(inst)
        data = json.loads(path.read_text())
        self.assertTrue(data["episode"])
        self.assertLessEqual(len(data["samples"]), 1440)
        self.assertTrue(all(time.time() - s["at"] < 86401 for s in data["samples"]))
        self.assertEqual([r['state'] for r in data['transitions']], ['open'])
        self.assertNotIn('hold heavy work:', (root / 'inbox.md').read_text())
        with mock.patch.dict(os.environ, {"BRAIN_MACHINE_TESTING": "0"}):
            state = m.ui_state(inst, spend_budget=0)
            self.assertIn("sustained 5-minute load", state["machine"]["held"])
            self.assertEqual(len([r for r in state["due"] if r["kind"] == "machine"]), 1)
            self.env["BRAIN_MACHINE_TESTING"] = "0"
            # Root due can check upgrades; suppress that unrelated path in a direct invocation.
            with mock.patch.object(m, "load", return_value=inst), mock.patch.object(m, "core_update", return_value=None), \
                    mock.patch.object(m, "auto_upgrade", return_value=None), contextlib.redirect_stdout(io.StringIO()) as output:
                m.cmd_due(argparse.Namespace(id="root", json=True, quiet=False))
            self.assertEqual(len([r for r in json.loads(output.getvalue()) if r["kind"] == "machine"]), 1)
            self.env["BRAIN_MACHINE_TESTING"] = "1"
        self.assertNotIn("hold heavy work:", self.git("show", "HEAD:inbox.md", cwd=root))
        self.assertEqual(self.git("status", "--porcelain", "--", "inbox.md", cwd=root), "")
        base = time.time()
        with mock.patch.object(m, "machine_load", return_value=self.good), contextlib.redirect_stdout(io.StringIO()):
            for offset in (60, 120):
                with mock.patch.object(m.time, "time", return_value=base + offset):
                    m.machine_sample(inst)
                self.assertTrue(json.loads(path.read_text())["episode"])
            with mock.patch.object(m.time, "time", return_value=base + 180):
                m.machine_sample(inst)
        self.assertFalse(json.loads(path.read_text())["episode"])
        self.assertEqual([r['state'] for r in json.loads(path.read_text())['transitions']], ['open', 'close'])
        self.assertNotIn("hold cleared", (root / "inbox.md").read_text())
        self.assertNotIn(' open]', (root / 'inbox.md').read_text())
        n = inst.node('root')
        self.assertEqual(m.due_items(inst, n, include_stale=False), [])
        before = path.read_bytes()
        with mock.patch.object(m.os, "replace", side_effect=OSError("fixture interruption")):
            with self.assertRaises(OSError):
                m.atomic_write(path, "broken")
        self.assertEqual(path.read_bytes(), before)
        with mock.patch.object(m.subprocess, "run") as run, contextlib.redirect_stdout(io.StringIO()) as out:
            m.machine_service(inst, "install")
            m.machine_service(inst, "uninstall")
        run.assert_not_called()
        self.assertIn("dry run", out.getvalue())
        self.assertFalse((self.tmp / "Library/LaunchAgents").exists())

    def test_worktree_admission_guard_and_quiet_scope(self):
        m = self.m
        root = self.init("oak")
        home = self.tmp / "fixture-home"
        self.repo(home, {"fixture.txt": "fixture"})
        self.b(root, "set", "root", "home=" + str(home))
        readings = self.tmp / "readings.json"
        readings.write_text(json.dumps({**self.good, "thermal": "pressure"}))
        code = ("import core_loader,json; from pathlib import Path; "
                "m=core_loader.SourceFileLoader('fixture'," + repr(str(CORE / "bin/brain")) + ").load_module(); "
                "m.MACHINE_RETRY=.01; m.machine_reading=lambda inst=None:json.loads(Path(" + repr(str(readings)) + ").read_text()); m.main()")
        proc = subprocess.Popen([sys.executable, "-c", code, "wt", "add", "root", "job/fixture", "--no-setup"],
                                cwd=root, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            first = proc.stdout.readline()
            self.assertIn("held", first)
            self.assertIn("thermal pressure", first)
            self.assertNotEqual(subprocess.run(["git", "show-ref", "--verify", "refs/heads/job/fixture"],
                                cwd=home, env=self.env, capture_output=True).returncode, 0)
            inst = m.Instance(root)
            with mock.patch.object(m, "load", return_value=inst), mock.patch.object(m, "due_items", return_value=[]), \
                    mock.patch.object(m, "machine_reading", return_value=json.loads(readings.read_text())), \
                    mock.patch.object(m, "changes_due", return_value=[]):
                with self.assertRaises(SystemExit) as result:
                    m.cmd_due(argparse.Namespace(id="root", quiet=True, json=False))
                self.assertEqual(result.exception.code, 1)  # this root owns the held start
            readings.write_text(json.dumps(self.good))
            out, err = proc.communicate(timeout=10)
            self.assertEqual(proc.returncode, 0, err)
            self.assertIn("worktree:", out)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()
            proc.stderr.close()
        (root / "never-name.txt").write_text("FixtureHiddenToken\n")
        self.assertIn("machine: load/CPU", self.b(root, "guard", "root", str(home)))
        self.assertIn("guard: clean", self.b(root, "guard", "root", str(home)))

    def test_sampler_killed_mid_write_busy_lock_and_bad_cache_recovery(self):
        m = self.m
        root = self.init("oak")
        inst = m.Instance(root)
        path = root / ".brain/machine.json"
        path.write_text(json.dumps({"samples": self.samples(1), "episode": False}))
        before = path.read_bytes()
        marker = self.tmp / "publication-started"
        code = ("import core_loader,json,time; from pathlib import Path; "
                "m=core_loader.SourceFileLoader('fixture'," + repr(str(CORE / "bin/brain")) + ").load_module(); "
                "m.machine_load=lambda:json.loads(" + repr(json.dumps(self.good)) + "); "
                "m.os.replace=lambda *a,**kw:(Path(" + repr(str(marker)) + ").touch(),time.sleep(30)); "
                "m.machine_sample(m.Instance(Path(" + repr(str(root)) + ")))")
        proc = subprocess.Popen([sys.executable, "-c", code], env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not marker.exists() and proc.poll() is None:
                time.sleep(.01)
            self.assertTrue(marker.exists(), proc.stderr.read().decode() if proc.poll() is not None else "no publication")
            proc.kill()
            proc.wait()
            self.assertEqual(path.read_bytes(), before)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stderr.close()
        with m.brain_lock(inst), contextlib.redirect_stdout(io.StringIO()) as out:
            m.machine_sample_once(inst)
        self.assertIn("skipped", out.getvalue())
        self.assertEqual(path.read_bytes(), before)
        path.write_text("{broken")
        with mock.patch.object(m, "machine_load", return_value=self.good), contextlib.redirect_stdout(io.StringIO()):
            m.machine_sample_once(inst)
        self.assertEqual(len(json.loads(path.read_text())["samples"]), 1)
        path.write_text(json.dumps({"samples": self.samples(1), "episode": "invalid",
                                    "pending_notice": "bad metadata", "episode_id": "a" * 24}))
        with mock.patch.object(m, "machine_load", return_value=self.good), contextlib.redirect_stdout(io.StringIO()):
            m.machine_sample_once(inst)
        self.assertFalse(json.loads(path.read_text())["episode"])

    def test_thermal_unknown_preserves_warning_until_known_recovery(self):
        m = self.m
        readings = self.samples(3, {**self.good, "thermal": "pressure"})
        readings[1]["reading"]["thermal"] = "unknown"
        readings[2]["reading"]["thermal"] = "unknown"
        self.assertIn("sustained thermal pressure", m.machine_sustained(readings, m.machine_policy()))
        for sample in readings:
            sample["reading"]["thermal"] = "unknown"
        self.assertNotIn("sustained thermal pressure", m.machine_sustained(readings, m.machine_policy()))

    def test_sampler_refuses_linked_git_metadata_and_retries_pending_notice(self):
        m = self.m
        for kind in ("message", "index", "reflog", "gitdir", "objects"):
            with self.subTest(kind=kind):
                root = self.init("tree-" + kind)
                inst = m.Instance(root)
                state = root / ".brain/machine.json"
                high = {**self.good, "load_per_cpu": [1, 1, 1]}
                state.write_text(json.dumps({"samples": self.samples(5, high), "episode": False}))
                before_head = self.git("rev-parse", "HEAD", cwd=root)
                before_inbox = (root / "inbox.md").read_bytes()
                external = self.tmp / ("outside-" + kind)
                original = None
                if kind == "gitdir":
                    (root / ".git").rename(external)
                    target = root / ".git"
                    target.symlink_to(external, target_is_directory=True)
                    sentinel = external / "COMMIT_EDITMSG"
                elif kind == "objects":
                    external.mkdir()
                    sentinel = external / "sentinel"
                    sentinel.write_bytes(b"outside fixture")
                    target = next(root / ".git/objects" / f"{i:02x}" for i in range(256)
                                  if not (root / ".git/objects" / f"{i:02x}").exists())
                    target.symlink_to(external, target_is_directory=True)
                else:
                    sentinel = external
                    sentinel.write_bytes(b"outside fixture")
                    target = root / {"message": ".git/COMMIT_EDITMSG", "index": ".git/index",
                                     "reflog": ".git/logs/HEAD"}[kind]
                    original = target.read_bytes()
                    target.unlink()
                    if kind == "message":
                        os.link(sentinel, target)
                    else:
                        target.symlink_to(sentinel)
                before = sentinel.read_bytes()
                with mock.patch.object(m, "machine_load", return_value=high), \
                        contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        m.machine_sample_once(inst)
                self.assertEqual(sentinel.read_bytes(), before)
                self.assertEqual((root / "inbox.md").read_bytes(), before_inbox)
                self.assertEqual(self.git("rev-parse", "HEAD", cwd=root), before_head)
                self.assertIsNotNone(json.loads(state.read_text())["pending_notice"])
                target.unlink()
                if kind == "gitdir":
                    external.rename(target)
                elif original is not None:
                    target.write_bytes(original)
                with mock.patch.object(m, "machine_load", return_value=high), contextlib.redirect_stdout(io.StringIO()):
                    m.machine_sample_once(inst)
                self.assertIsNone(json.loads(state.read_text())["pending_notice"])
                self.assertEqual([r['state'] for r in json.loads(state.read_text())['transitions']], ['open'])
                self.assertEqual((root / "inbox.md").read_bytes(), before_inbox)

    def test_invalid_policy_preserves_diagnostics_and_fails_closed_for_admission(self):
        m = self.m
        root = self.init("oak")
        inst = m.Instance(root)
        m.machine_dir().mkdir(parents=True)
        (m.machine_dir() / "machine-care.json").write_text("{invalid")
        self.assertIn("policy invalid", m.machine_summary(m.machine_state(inst)))
        self.assertIn("policy invalid", "; ".join(m.ui_state(inst, spend_budget=0)["machine"]["held"]))
        with mock.patch.object(m, "load", return_value=inst), mock.patch.object(m, "due_items", return_value=[]), \
                mock.patch.object(m, "changes_due", return_value=[]):
            with self.assertRaises(SystemExit) as result:
                m.cmd_due(argparse.Namespace(id="root", quiet=True, json=False))
            self.assertEqual(result.exception.code, 0)
        seen = []
        def repair(_):
            with m.machine_queue() as q:
                self.assertEqual(q.rows[0]["state"], "queued")
                seen.append(q.rows[0]["token"])
            (m.machine_dir() / "machine-care.json").write_text("{}")
        # The global sleep mock can also intercept subprocess.run's ps polling before
        # machine_wait publishes its row. This test owns policy repair, not OS identity.
        with mock.patch.object(m, 'process_stamp', return_value='fixture-policy-owner'), \
                mock.patch.object(m.time, "sleep", side_effect=repair), contextlib.redirect_stdout(io.StringIO()) as out:
            token = m.machine_wait("verify")
        self.assertEqual(seen, [token])
        self.assertIn("policy invalid", out.getvalue())
        m.machine_release(token)

    def test_linux_process_identity_uses_boot_and_start_ticks_not_wall_clock(self):
        m = self.m
        values = {"boot": "12345678-1234-1234-1234-123456789abc", "ticks": "4321", "state": "S"}
        def reading(path):
            if path.name == "boot_id":
                return values["boot"]
            return "12345 (fixture ) worker) " + " ".join([values["state"]] + ["0"] * 18 + [values["ticks"]])
        with mock.patch.object(m.sys, "platform", "linux"), mock.patch.object(m.Path, "read_text", reading), \
                mock.patch.object(m.subprocess, "run", side_effect=AssertionError("no wall-clock ps on Linux")):
            first = m.process_stamp(12345)
            with mock.patch.object(m.time, "time", return_value=1):
                self.assertEqual(m.process_stamp(12345), first)
            values["boot"] = "abcdef01-1234-1234-1234-123456789abc"
            self.assertNotEqual(m.process_stamp(12345), first)
            values["state"] = "Z"
            self.assertIsNone(m.process_stamp(12345))

    def test_service_payload_round_trips_cron_paths_and_reinstalls_with_mocked_launchd(self):
        m = self.m
        inst = argparse.Namespace(root=self.tmp / "fixture\\% space's root")
        # Cron processes backslash/percent before the shell parses quotes.
        def cron_command(value):
            out, escaped = [], False
            for char in value:
                if escaped:
                    if char == "%":
                        out[-1] = "%"
                    else:
                        out.append(char)
                    escaped = False
                elif char == "\\":
                    out.append(char)
                    escaped = True
                elif char == "%":
                    self.fail("unexpected cron stdin separator")
                else:
                    out.append(char)
            return "".join(out)
        with mock.patch.object(m.sys, "platform", "linux"), mock.patch.object(m.subprocess, "run") as run, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            m.machine_service(inst, "install")
        run.assert_not_called()
        command = output.getvalue().splitlines()[0].split(maxsplit=5)[5]
        tokens = shlex.split(cron_command(command))
        self.assertEqual(tokens[0:2], ["cd", str(inst.root)])
        for value in ("%", "\\%", "\\\\%", "a\\b% c'\"$()"):
            self.assertEqual(shlex.split(cron_command(m.machine_cron_quote(value).replace("%", r"\%"))), [value])
        def service(args, **kw):
            return subprocess.CompletedProcess(args, 0, "", "")
        with mock.patch.object(m.sys, "platform", "darwin"), \
                mock.patch.object(m.subprocess, "run", side_effect=service) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            m.machine_service(inst, "install", True)
            m.machine_service(inst, "install", True)
            m.machine_service(inst, "uninstall", True)
        self.assertEqual([call.args[0][1] for call in run.call_args_list],
                         ["print", "bootout", "bootstrap"] * 2 + ["print", "bootout"])
        self.assertEqual(list((self.tmp / "Library/LaunchAgents").glob("*.plist")), [])

    def test_queue_rejects_links_and_preserves_on_interrupted_publication(self):
        m = self.m
        token = m.machine_wait("verify")
        queue_path = m.machine_dir() / "machine-care-queue.json"
        before = queue_path.read_bytes()
        with mock.patch.object(m.os, "replace", side_effect=OSError("fixture interruption")):
            with self.assertRaises(OSError):
                with m.machine_queue() as q:
                    q.rows.clear()
        self.assertEqual(queue_path.read_bytes(), before)
        m.machine_release(token)
        lock = m.machine_dir() / "machine-care.lock"
        lock.unlink()
        outside = self.tmp / "sentinel"
        outside.write_text("sentinel")
        lock.symlink_to(outside)
        with self.assertRaises(OSError):
            with m.machine_queue():
                pass
        self.assertEqual(outside.read_text(), "sentinel")



class DockerLeftovers(unittest.TestCase):
    def setUp(self):
        from types import SimpleNamespace
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.m = core_loader.SourceFileLoader('brain_leftovers', str(CORE / 'bin/brain')).load_module()
        self.inst = SimpleNamespace(root=self.root)
        self.node = {'id': 'maple', 'dir': self.root / 'maple', 'parent': None,
                     'fm': {'docker_leftover_prefixes': 'maple-verify-*,maple-job-*'}}
        self.node['dir'].mkdir()
        self.calls = []
        self.rows = []
        self.clock = 10.0
        self.env = mock.patch.dict(os.environ, {'DOCKER_HOST': 'unix:///tmp/fixture.sock', 'DOCKER_CONTEXT': ''})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.original_process_stamp = self.m.process_stamp
        self.patches = [mock.patch.object(self.m, 'process_stamp', return_value=None),
                        mock.patch.object(self.m.shutil, 'which', return_value='/fake/docker'),
                        mock.patch.object(self.m.subprocess, 'run', side_effect=self.command),
                        mock.patch.object(self.m.time, 'monotonic', side_effect=lambda: self.clock),
                        mock.patch.object(self.m.os, 'kill', side_effect=ProcessLookupError),
                        mock.patch.object(self.m, 'leftover_records', return_value=[])]
        # Start after regression APIs exist; missing APIs are the initial red proof.
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def row(self, i=1, project='maple-verify-old', **labels):
        return {'Id': '%064x' % i, 'Created': '2020-01-01T00:00:00.123456789Z',
                'State': {'Running': True, 'Status': 'running'},
                'Config': {'Labels': {'com.docker.compose.project': project, **labels}}}

    def command(self, args, **kw):
        self.calls.append((args, kw))
        self.assertFalse({'stop', 'rm', 'prune', 'down', 'kill', 'volume', 'restart', 'exec'} & set(args))
        self.assertGreater(kw['timeout'], 0)
        self.assertLessEqual(kw['timeout'], 5)
        host = ['/fake/docker', '--host', 'unix:///tmp/fixture.sock']
        if args == host + ['ps', '--all', '--quiet', '--no-trunc']:
            output = '\n'.join(r['Id'] for r in self.rows)
        elif len(args) == 5 and args[:4] == host + ['inspect'] and any(r['Id'] == args[4] for r in self.rows):
            output = json.dumps([r for r in self.rows if r['Id'] == args[4]])
        else:
            self.fail('unexpected Docker command: ' + repr(args))
        return subprocess.CompletedProcess(args, 0, output, '')

    def scan(self, nodes=None):
        return self.m.docker_leftover_nodes(self.inst, nodes or [self.node])

    def test_running_ten_second_project_has_no_report(self):
        self.rows = [self.row()]
        with mock.patch.object(self.m.time, 'time', return_value=self.m.leftover_timestamp(self.rows[0]['Created']) + 10):
            self.scan()
        self.assertFalse((self.node['dir'] / 'inbox.md').exists())

    def test_exited_old_unknown_project_reports_once_and_timeout_is_respected(self):
        self.rows = [self.row()]
        self.rows[0]['State'] = {'Running': False, 'Status': 'exited'}
        created = self.m.leftover_timestamp(self.rows[0]['Created'])
        self.node['fm']['docker_leftover_timeout_seconds'] = '2400'
        with mock.patch.object(self.m.time, 'time', return_value=created + 2000):
            self.scan()
        self.assertFalse((self.node['dir'] / 'inbox.md').exists())
        with mock.patch.object(self.m.time, 'time', return_value=created + 2401):
            self.scan()
            self.scan()
        self.assertEqual((self.node['dir'] / 'inbox.md').read_text().count('Docker leftovers'), 1)

    def test_old_running_and_known_owner_never_publish(self):
        self.rows = [self.row(1, 'maple-verify-active'),
                     self.row(2, 'maple-verify-known', **{'brain.owner-pid': '987654'})]
        self.rows[1]['State'] = {'Running': False, 'Status': 'exited'}
        self.scan()
        self.assertFalse((self.node['dir'] / 'inbox.md').exists())

    def test_young_peer_and_minimum_timeout_prevent_reports(self):
        self.rows = [self.row(1), self.row(2)]
        for row in self.rows:
            row['State'] = {'Running': False, 'Status': 'exited'}
        self.rows[1]['Created'] = '2020-01-01T00:29:50Z'
        self.node['fm']['docker_leftover_timeout_seconds'] = '1'
        with mock.patch.object(self.m.time, 'time', return_value=self.m.leftover_timestamp('2020-01-01T00:30:00Z')):
            self.scan()
        self.assertFalse((self.node['dir'] / 'inbox.md').exists())

    def test_report_only_fake_cli_records_every_argv(self):
        # A real subprocess invokes only a fabricated CLI, never a Docker daemon.
        binary = self.root / 'docker'
        log = self.root / 'argv.jsonl'
        row = self.row(**{'brain.owner-pid': '987654'})
        binary.write_text('#!/usr/bin/python3\nimport json, sys\n'
                          'args = sys.argv[1:]\n'
                          'with open(' + repr(str(log)) + ', "a") as f: f.write(json.dumps(args) + "\\n")\n'
                          'host = ["--host", "unix:///tmp/fixture.sock"]\n'
                          'if args == host + ["ps", "--all", "--quiet", "--no-trunc"]: print(' + repr(row['Id']) + ')\n'
                          'elif args == host + ["inspect", ' + repr(row['Id']) + ']: print(' + repr(json.dumps([row])) + ')\n'
                          'elif args == ["context", "inspect"]: print(' + repr('[{"Endpoints":{"docker":{"Host":"unix:///tmp/fixture.sock"}}}]') + ')\n'
                          'else: print("unexpected argv", file=sys.stderr); sys.exit(64)\n')
        binary.chmod(0o755)
        with mock.patch.object(self.m.subprocess, 'run', side_effect=self.real_run):
            with mock.patch.object(self.m.shutil, 'which', return_value=str(binary)):
                result = self.scan()
        self.assertEqual(len(result['leftovers']), 1)
        argv = [json.loads(s) for s in log.read_text().splitlines()]
        self.assertEqual(len(argv), 2)
        self.assertFalse({'stop', 'rm', 'prune', 'down', 'kill', 'volume', 'restart', 'exec'} & {a for row in argv for a in row})

    real_run = staticmethod(subprocess.run)

    def test_only_scoped_test_resources_and_sessions(self):
        scope = self.m.leftover_scope(self.inst, self.node)
        self.rows = [self.row(1), self.row(2, 'maple-preview-old'), self.row(3, 'cedar-verify-old'),
                     self.row(4, ''), self.row(5, 'maple-job-dev', **{'brain.preview': 'true'}),
                     self.row(6, 'maple-job-foreign', **{'brain.scope': 'foreign'}),
                     self.row(7, '', **{'org.testcontainers.session-id': 'unscoped'}),
                     self.row(8, '', **{'org.testcontainers.session-id': 'test-session', 'brain.scope': scope})]
        result = self.scan()
        self.assertEqual({r['name'] for r in result['leftovers']}, {'maple-verify-old', 'test-session'})

    def test_alive_gone_unknown_and_conflicting_owners(self):
        self.rows = [self.row(1, 'maple-job-alive', **{'brain.owner-pid': '101'}),
                     self.row(2, 'maple-job-gone', **{'brain.owner-pid': '102', 'brain.job': 'fixture-job'}),
                     self.row(3, 'maple-job-unknown'),
                     self.row(4, 'maple-job-conflict', **{'brain.owner-pid': '103'}),
                     self.row(5, 'maple-job-conflict', **{'brain.owner-pid': '104'})]
        def alive(pid, signal):
            if pid != 101:
                raise ProcessLookupError
        with mock.patch.object(self.m.os, 'kill', side_effect=alive):
            rows = {r['name']: r for r in self.scan()['leftovers']}
        self.assertNotIn('maple-job-alive', rows)
        self.assertEqual(rows['maple-job-gone']['owner']['pid'], 102)
        self.assertEqual(rows['maple-job-gone']['owner']['job'], 'fixture-job')
        self.assertEqual(rows['maple-job-unknown']['owner'], 'unknown')
        self.assertEqual(rows['maple-job-conflict']['owner'], 'unknown')
        self.assertEqual(rows['maple-job-gone']['state'], 'running')
        self.assertGreater(rows['maple-job-gone']['age_seconds'], 3600)

    def test_reservation_ambiguity_is_unknown_and_foreign_is_excluded(self):
        scope = self.m.machine_scope(self.inst, self.node['id'])
        record = {'token': 'a' * 32, 'scope': scope, 'owner': [102, 'stamp'], 'kind': 'verify', 'state': 'running'}
        self.rows = [self.row(**{'brain.reservation': 'a' * 32})]
        with mock.patch.object(self.m, 'leftover_records', return_value=[record]):
            r = self.scan()['leftovers'][0]
            self.assertEqual(r['owner']['record'], 'a' * 32)
            self.assertEqual(r['owner']['pid'], 102)
        with mock.patch.object(self.m, 'leftover_records', return_value=[record, record]):
            self.assertEqual(self.scan()['leftovers'][0]['owner'], 'unknown')
        with mock.patch.object(self.m, 'leftover_records', return_value=[{**record, 'scope': ['f' * 64] * 2}]):
            self.assertEqual(self.scan()['leftovers'], [])

    def test_timestamps_python39_fractions_offsets_and_invalid_report(self):
        for width in range(10):
            fraction = '.' + '123456789'[:width] if width else ''
            for zone in ('Z', '+03:30', '-04:00'):
                self.assertIsInstance(self.m.leftover_timestamp('2020-01-01T00:00:00' + fraction + zone), float)
        for zone in ('+00:60', '+24:00', '-01:99', '+99:00', '', '+1:00'):
            with self.assertRaises(ValueError):
                self.m.leftover_timestamp('2020-01-01T00:00:00.123456789' + zone)
        self.rows = [self.row()]
        self.rows[0]['Created'] = '2020-01-01T00:00:00+00:60'
        row = self.scan()['leftovers'][0]
        self.assertIsNone(row['age_seconds'])
        self.assertIn('invalid Created timestamp', row['errors'])

    def test_shared_deadline_and_early_node_failures_abort_tick(self):
        other = {**self.node, 'id': 'cedar'}
        self.rows = [self.row()]
        original = self.command
        def slow(args, **kw):
            result = original(args, **kw)
            self.clock += 3
            return result
        with mock.patch.object(self.m.subprocess, 'run', side_effect=slow):
            result = self.scan([self.node, other])
        self.assertTrue(result['partial'])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual([kw['timeout'] for _, kw in self.calls], [5, 2])
        self.assertEqual(len(result['leftovers']), 1)
        for error in (subprocess.TimeoutExpired('fixture', .01), ValueError('fixture failed')):
            self.calls.clear()
            self.clock = 10
            with mock.patch.object(self.m.subprocess, 'run', side_effect=error) as run:
                self.assertTrue(self.scan([self.node, other])['partial'])
                self.assertEqual(run.call_count, 1)

    def test_late_malformed_and_nested_json_preserve_known_leftovers(self):
        self.rows = [self.row(1), self.row(2)]
        original = self.command
        for raw in ('{', '[' * 1200 + '0' + ']' * 1200, '{}'):
            def broken(args, **kw):
                if args[-1] == self.rows[1]['Id']:
                    return subprocess.CompletedProcess(args, 0, raw, '')
                return original(args, **kw)
            with mock.patch.object(self.m.subprocess, 'run', side_effect=broken):
                result = self.scan()
            self.assertTrue(result['partial'])
            self.assertEqual(result['leftovers'][0]['containers'], [self.rows[0]['Id']])
        with mock.patch.object(self.m, 'leftover_records', side_effect=RecursionError):
            self.assertEqual(self.scan()['leftovers'][0]['owner'], 'unknown')

    def test_delivery_once_per_episode_partial_preserves_and_reappearance_rearms(self):
        self.rows = [self.row()]
        self.rows[0]['State'] = {'Running': False, 'Status': 'exited'}
        self.scan()
        inbox = self.node['dir'] / 'inbox.md'
        first = inbox.read_text()
        self.assertIn('owner: unknown', first)
        self.assertIn('age:', first)
        self.assertIn('state: exited', first)
        self.scan()
        self.assertEqual(inbox.read_text(), first)
        # A failed scan cannot retire the episode or discard known facts.
        with mock.patch.object(self.m.subprocess, 'run', side_effect=ValueError('unavailable')):
            result = self.scan()
        self.assertTrue(result['partial'])
        self.assertEqual(len(result['leftovers']), 1)
        self.scan()
        self.assertEqual(inbox.read_text(), first)
        self.rows = []
        self.scan()
        self.rows = [self.row()]
        self.rows[0]['State'] = {'Running': False, 'Status': 'exited'}
        self.scan()
        self.assertEqual(inbox.read_text().count('Docker leftovers'), 2)

    def test_no_configuration_or_invalid_prefix_never_calls_docker(self):
        for value in ('', '*', '*-verify-*', 'maple-preview-*', 'maple-*'):
            self.node['fm']['docker_leftover_prefixes'] = value
            self.assertEqual(self.scan()['leftovers'], [])
        self.assertEqual(self.calls, [])


    def test_live_overlapping_session_and_preview_peer_veto_project(self):
        self.rows = [self.row(1, **{'brain.owner-pid': '102', 'org.testcontainers.session-id': 'shared'}),
                     self.row(2, '', **{'brain.owner-pid': '101', 'org.testcontainers.session-id': 'shared'})]
        def alive(pid, signal):
            if pid != 101:
                raise ProcessLookupError
        with mock.patch.object(self.m.os, 'kill', side_effect=alive):
            self.assertEqual(self.scan()['leftovers'], [])
        self.rows[1]['Config']['Labels']['com.docker.compose.project'] = 'maple-preview-current'
        self.assertEqual(self.scan()['leftovers'], [])

    def test_nested_and_fifo_owner_records_remain_unknown_without_blocking(self):
        record = self.root / 'record.json'
        self.rows = [self.row(**{'brain.reservation': 'a' * 32})]
        for raw in ('{', '[' * 1200 + '0' + ']' * 1200):
            record.write_text(raw)
            with mock.patch.object(self.m, 'leftover_records', side_effect=lambda: self.m.leftover_read(record)):
                self.assertEqual(self.scan()['leftovers'][0]['owner'], 'unknown')
        fifo = self.root / 'record.fifo'
        os.mkfifo(fifo)
        with mock.patch.object(self.m, 'leftover_records', side_effect=lambda: self.m.leftover_read(fifo)):
            self.assertEqual(self.scan()['leftovers'][0]['owner'], 'unknown')

    def test_context_selection_and_malformed_shapes_are_partial(self):
        self.rows = [self.row()]
        original = self.command
        with mock.patch.dict(os.environ, {'DOCKER_HOST': '', 'DOCKER_CONTEXT': 'fixture'}):
            for payload in ('[]', '{}', '[null]', '[' * 1200 + '0' + ']' * 1200):
                with mock.patch.object(self.m.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, payload, '')) as run:
                    self.assertTrue(self.scan()['partial'])
                    self.assertEqual(run.call_count, 1)
            def context(args, **kw):
                if args[1:] == ['context', 'inspect']:
                    self.calls.append((args, kw))
                    return subprocess.CompletedProcess(args, 0, '[{"Endpoints":{"docker":{"Host":"unix:///tmp/fixture.sock"}}}]', '')
                return original(args, **kw)
            with mock.patch.object(self.m.subprocess, 'run', side_effect=context):
                self.assertEqual(len(self.scan()['leftovers']), 1)
        for config in (None, [], 1):
            self.rows[0]['Config'] = config
            self.assertTrue(self.scan()['partial'])

    def test_aborted_tick_keeps_later_nodes_previous_leftovers(self):
        other = {**self.node, 'id': 'cedar', 'fm': {'docker_leftover_prefixes': 'cedar-verify-*'}}
        self.rows = [self.row(1, 'cedar-verify-old')]
        self.scan([other])
        with mock.patch.object(self.m.subprocess, 'run', side_effect=ValueError('unavailable')) as run:
            result = self.scan([self.node, other])
        self.assertTrue(result['partial'])
        self.assertEqual(result['leftovers'][0]['name'], 'cedar-verify-old')
        self.assertTrue(result['leftovers'][0]['unconfirmed'])
        self.assertEqual(run.call_count, 1)

    def test_readonly_command_guard_refuses_mutations_before_cli(self):
        for word in ('stop', 'rm', 'prune', 'down', 'kill', 'volume', 'restart', 'exec'):
            with self.assertRaises(ValueError):
                self.m.leftover_command('/fake/docker', ['--host', 'unix:///tmp/fixture.sock', word], 15)
        self.assertEqual(self.calls, [])


    def test_partial_membership_does_not_open_second_project_episode(self):
        self.rows = [self.row(1), self.row(2)]
        for row in self.rows:
            row['State'] = {'Running': False, 'Status': 'exited'}
        self.scan()
        original = self.command
        def broken(args, **kw):
            if args[-1] == self.rows[1]['Id']:
                return subprocess.CompletedProcess(args, 0, '{', '')
            return original(args, **kw)
        with mock.patch.object(self.m.subprocess, 'run', side_effect=broken):
            result = self.scan()
        self.assertTrue(result['partial'])
        self.assertEqual(len(result['leftovers']), 1)
        self.assertEqual(result['leftovers'][0]['containers'], [self.rows[0]['Id'], self.rows[1]['Id']])
        self.assertEqual((self.node['dir'] / 'inbox.md').read_text().count('Docker leftovers'), 1)

    def test_preview_names_and_foreign_scope_alias_are_never_leftovers(self):
        scope = self.m.leftover_scope(self.inst, self.node)
        self.rows = [self.row(1), self.row(2, '', **{'brain.scope': scope, 'org.testcontainers.session-id': 'preview-session'}),
                     self.row(3, 'maple-job-foreign', **{'brain.docker-scope': 'foreign'})]
        self.rows[0]['Name'] = '/preview-current'
        self.assertEqual(self.scan()['leftovers'], [])


    def test_reservation_birth_identity_and_parent_child_liveness(self):
        record = {'token': 'a' * 32, 'scope': self.m.machine_scope(self.inst, self.node['id']),
                  'owner': [101, 'parent-stamp'], 'child': [102, 'child-stamp'],
                  'kind': 'verify', 'state': 'running'}
        self.rows = [self.row(**{'brain.reservation': record['token']})]
        cases = [({101: None, 102: None}, False, 102),
                 ({101: 'parent-stamp', 102: None}, True, None),
                 ({101: None, 102: 'child-stamp'}, True, None),
                 ({101: 'reused-stamp', 102: None}, False, 'unknown'),
                 ({101: 'unknown', 102: None}, False, 'unknown'),
                 ({101: None, 102: 'unknown'}, False, 'unknown'),
                 ({101: 'unknown', 102: 'child-stamp'}, False, 'unknown')]
        for stamps, alive, owner in cases:
            with self.subTest(stamps=stamps), \
                    mock.patch.object(self.m, 'leftover_records', return_value=[record]), \
                    mock.patch.object(self.m, 'process_stamp', side_effect=lambda pid, **kw: stamps[pid]) as probe, \
                    mock.patch.object(self.m.os, 'kill', return_value=None):
                rows = self.scan()['leftovers']
                self.assertEqual(bool(rows), not alive)
                self.assertEqual({c.args[0] for c in probe.call_args_list}, {101, 102})
                if rows:
                    if owner == 'unknown':
                        self.assertEqual(rows[0]['owner'], 'unknown')
                    else:
                        self.assertEqual(rows[0]['owner']['pid'], owner)
        # With no child, a reused PID is still unknown, never an alive recorded owner.
        record.pop('child')
        with mock.patch.object(self.m, 'leftover_records', return_value=[record]), \
                mock.patch.object(self.m, 'process_stamp', return_value='different-birth'), \
                mock.patch.object(self.m.os, 'kill', return_value=None):
            self.assertEqual(self.scan()['leftovers'][0]['owner'], 'unknown')

    def test_pid_label_record_disagreement_and_unknown_birth_are_unknown(self):
        record = {'token': 'a' * 32, 'scope': self.m.machine_scope(self.inst, self.node['id']),
                  'owner': [101, 'parent-stamp'], 'kind': 'verify', 'state': 'running'}
        self.rows = [self.row(**{'brain.reservation': record['token'], 'brain.owner-pid': '999'})]
        with mock.patch.object(self.m, 'leftover_records', return_value=[record]), \
                mock.patch.object(self.m, 'process_stamp', return_value='parent-stamp'), \
                mock.patch.object(self.m.os, 'kill', return_value=None):
            self.assertEqual(self.scan()['leftovers'][0]['owner'], 'unknown')
        record['owner'][1] = 'unknown'
        self.rows[0]['Config']['Labels']['brain.owner-pid'] = '101'
        with mock.patch.object(self.m, 'leftover_records', return_value=[record]), \
                mock.patch.object(self.m, 'process_stamp', return_value='parent-stamp'), \
                mock.patch.object(self.m.os, 'kill', return_value=None):
            self.assertEqual(self.scan()['leftovers'][0]['owner'], 'unknown')

        for error in (PermissionError('fixture denied'), OSError('fixture unknown')):
            self.rows = [self.row(**{'brain.owner-pid': '101'})]
            with mock.patch.object(self.m.os, 'kill', side_effect=error):
                self.assertEqual(self.scan()['leftovers'][0]['owner'], 'unknown')
        # Bad PID metadata cannot make an explicitly foreign reservation ours.
        record['scope'] = ['f' * 64, 'f' * 64]
        self.rows = [self.row(**{'brain.reservation': record['token'], 'brain.owner-pid': 'bad'})]
        with mock.patch.object(self.m, 'leftover_records', return_value=[record]):
            self.assertEqual(self.scan()['leftovers'], [])

    def test_identity_probe_uses_remaining_deadline_and_aborts_later_nodes(self):
        record = {'token': 'a' * 32, 'scope': self.m.machine_scope(self.inst, self.node['id']),
                  'owner': [101, 'parent-stamp'], 'kind': 'verify', 'state': 'running'}
        self.rows = [self.row(**{'brain.reservation': record['token']})]
        original = self.command
        probes = []
        def bounded(args, **kw):
            if args[0] == 'ps':
                probes.append(kw['timeout'])
                raise subprocess.TimeoutExpired(args, .01)
            result = original(args, **kw)
            self.clock += 2
            return result
        with mock.patch.object(self.m, 'leftover_records', return_value=[record]), \
                mock.patch.object(self.m.sys, 'platform', 'darwin'), \
                mock.patch.object(self.m, 'process_stamp', side_effect=self.original_process_stamp), \
                mock.patch.object(self.m.os, 'kill', return_value=None), \
                mock.patch.object(self.m.subprocess, 'run', side_effect=bounded):
            result = self.scan([self.node, {**self.node, 'id': 'cedar'}])
        self.assertEqual(probes, [1])
        self.assertTrue(result['partial'])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(result['leftovers'][0]['owner'], 'unknown')

    def test_unscoped_session_peers_never_borrow_scope_in_either_order(self):
        scope = self.m.leftover_scope(self.inst, self.node)
        for alias in ('org.testcontainers.session-id', 'org.testcontainers.sessionId'):
            scoped = self.row(1, '', **{alias: 'shared', 'brain.scope': scope})
            unscoped = self.row(2, '', **{alias: 'shared'})
            other_alias = 'org.testcontainers.sessionId' if alias.endswith('session-id') else 'org.testcontainers.session-id'
            bridge = self.row(3, 'maple-job-bridge', **{alias: 'shared', other_alias: 'linked'})
            transitive = self.row(4, '', **{other_alias: 'linked'})
            for rows in ([scoped, unscoped], [unscoped, scoped],
                         [scoped, bridge, transitive], [transitive, bridge, scoped]):
                self.rows = rows
                self.assertEqual(self.scan()['leftovers'], [])
            unscoped['Config']['Labels']['brain.scope'] = scope
            self.rows = [scoped, unscoped]
            self.assertEqual(self.scan()['leftovers'][0]['containers'], [scoped['Id'], unscoped['Id']])

    def test_executable_fake_rejects_every_unknown_argv_and_records_attempts(self):
        self.test_report_only_fake_cli_records_every_argv()
        binary, log = self.root / 'docker', self.root / 'argv.jsonl'
        host = ['--host', 'unix:///tmp/fixture.sock']
        attempts = [host + [word] for word in ('stop', 'rm', 'prune', 'down', 'kill', 'volume', 'restart', 'exec')]
        attempts += [['ps'], host + ['inspect', 'bad-id'], ['context', 'inspect', '--format', 'anything']]
        for args in attempts:
            completed = self.real_run([str(binary)] + args, capture_output=True, text=True, timeout=1)
            self.assertNotEqual(completed.returncode, 0, args)
        argv = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(argv[2:], attempts)

    def test_falsey_non_object_labels_do_not_retire_known_leftovers(self):
        for invalid in (None, [], False, 0, ''):
            self.rows = [self.row()]
            self.scan()
            self.rows[0]['Config']['Labels'] = invalid
            result = self.scan()
            self.assertTrue(result['partial'])
            self.assertEqual(len(result['leftovers']), 1)
        # Also cover falsey/wrong shapes for every enclosing object and required
        # Docker identity/state field. Non-object Labels, including null, fail.
        for field in ('Config', 'State', 'Id'):
            for invalid in (None, [], False, 0, ''):
                self.rows = [self.row()]
                self.scan()
                original = self.command
                def malformed(args, **kw):
                    if 'inspect' in args:
                        return subprocess.CompletedProcess(args, 0, json.dumps([{**self.rows[0], field: invalid}]), '')
                    return original(args, **kw)
                with mock.patch.object(self.m.subprocess, 'run', side_effect=malformed):
                    result = self.scan()
                self.assertTrue(result['partial'])
                self.assertEqual(len(result['leftovers']), 1)


class DockerLeftoverHooks(unittest.TestCase):
    def setUp(self):
        self.m = core_loader.SourceFileLoader('brain_leftover_hooks', str(CORE / 'bin/brain')).load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.inst = mock.Mock(root=self.folder)
        self.node = {'id': 'maple', 'dir': self.folder, 'fm': {}, 'parent': None,
                     'external': True, 'local': {}, 'home_path': None, 'archived': False}
        self.inst.node.return_value = self.node

    def test_verify_and_gate_reservation_close_on_success_and_failure(self):
        for failure in (False, True):
            order = []
            with mock.patch.object(self.m, 'machine_wait', return_value='token'), \
                    mock.patch.object(self.m, 'machine_release', side_effect=lambda _: order.append('release')), \
                    mock.patch.object(self.m, 'docker_leftover_nodes', side_effect=lambda *a: order.append('report')) as report:
                try:
                    with self.m.verify_reservation(self.inst, self.node):
                        if failure:
                            raise RuntimeError('fixture verify failed')
                except RuntimeError:
                    self.assertTrue(failure)
                report.assert_called_once_with(self.inst, [self.node])
                self.assertEqual(order, ['release', 'report'])

    def test_job_close_reports_after_ledger_publication(self):
        self.inst.parse_time.return_value = None
        row = {'Job': 'job/fixture', 'Started': '2020-01-01T00:00Z', 'Ended': '', 'Usage': 'host'}
        with mock.patch.object(self.m, 'read_jobs', return_value=[row]), \
                mock.patch.object(self.m, 'ledger_rows', return_value=[row]), \
                mock.patch.object(self.m, 'brain_lock', side_effect=lambda _: contextlib.nullcontext()), \
                mock.patch.object(self.m, 'retain_integration'), \
                mock.patch.object(self.m, 'published_train', return_value=None), \
                mock.patch.object(self.m, 'job_budget', return_value=None), \
                mock.patch.object(self.m, 'write_jobs'), \
                mock.patch.object(self.m, 'cleanup_handback'), \
                mock.patch.object(self.m, 'ready_path', return_value=self.folder / 'ready.json'), \
                mock.patch.object(self.m, 'print_thread_checklist'), \
                mock.patch.object(self.m, 'docker_leftover_nodes') as report:
            self.m.job_ended(self.inst, self.node, 'job/fixture', 'abandoned')
            report.assert_called_once_with(self.inst, [self.node])
            self.assertEqual(row['Outcome'], 'abandoned')

    def test_train_close_reports_on_success_and_failure(self):
        self.node['fm']['verify'] = 'true'
        for failure in (False, True):
            with mock.patch.object(self.m, 'load', return_value=self.inst), \
                    mock.patch.object(self.m, 'git_home', return_value=(self.node, self.folder)), \
                    mock.patch.object(self.m, 'integration_ref', return_value='refs/heads/main'), \
                    mock.patch.object(self.m, 'main_unoccupied'), \
                    mock.patch.object(self.m, 'git', return_value=subprocess.CompletedProcess([], 0, str(self.folder), '')), \
                    mock.patch.object(self.m, 'run_train', side_effect=RuntimeError if failure else None), \
                    mock.patch.object(self.m, 'docker_leftover_nodes') as report:
                if failure:
                    with self.assertRaises(RuntimeError):
                        self.m.cmd_train(argparse.Namespace(id='maple'))
                else:
                    self.m.cmd_train(argparse.Namespace(id='maple'))
                report.assert_called_once_with(self.inst, [self.node])

    def test_due_tick_calls_one_shared_node_check_before_due_evidence(self):
        order = []
        self.inst.nodes.return_value = [self.node]
        args = argparse.Namespace(id='maple', quiet=False, json=True)
        with mock.patch.object(self.m, 'load', return_value=self.inst), \
                mock.patch.object(self.m, 'auto_upgrade', return_value=None), \
                mock.patch.object(self.m, 'wake_target', return_value=(self.node, None)), \
                mock.patch.object(self.m, 'wake_diagnostic', return_value=None), \
                mock.patch.object(self.m, 'inbox_lines', return_value=[]), \
                mock.patch.object(self.m, 'machine_state', return_value={}), \
                mock.patch.object(self.m, 'machine_due_items', return_value=[]), \
                mock.patch.object(self.m, 'due_items', side_effect=lambda *a, **k: order.append('due') or []), \
                mock.patch.object(self.m, 'changes_due', return_value=[]), \
                mock.patch.object(self.m, 'docker_leftover_nodes', side_effect=lambda *a: order.append('report')) as report, \
                contextlib.redirect_stdout(io.StringIO()):
            self.m.cmd_due(args)
            report.assert_called_once_with(self.inst, [self.node])
            self.assertEqual(order, ['report', 'due'])


if __name__ == "__main__":
    unittest.main()


class DockerCleanup(Scenario):
    """All Docker operations target a disposable stateful CLI fixture."""
    def setUp(self):
        super().setUp()
        self.m = core_loader.SourceFileLoader('cleanup_fixture', str(CORE / 'bin/brain')).load_module()
        self.root = self.init('alice')
        self.inst = self.m.Instance(self.root)
        self.node = self.inst.node('root')
        self.node['fm'].update(docker_cleanup_prefixes='demo-verify-*',
                               docker_cleanup_host='unix:///var/run/docker.sock',
                               docker_cleanup_daemon='fixture-daemon', docker_cleanup_mode='stop')
        self.scope = ':'.join(self.m.machine_scope(self.inst, 'root'))
        self.identity = 'a' * 64
        self.container = {'Id': self.identity, 'Name': '/demo-verify-one-db',
            'Created': '2020-01-01T00:00:00Z', 'State': {'Running': True, 'Status': 'running'},
            'Config': {'Labels': {'com.docker.compose.project': 'demo-verify-one',
                                  'brain.docker-scope': self.scope, 'brain.owner-pid': '12345',
                                  'brain.owner-stamp': 'fixture-birth'}},
            'HostConfig': {'AutoRemove': False}, 'Mounts': [],
            'NetworkSettings': {'Networks': {}}}
        self.state_path = self.tmp / 'docker-state.json'
        self.log_path = self.tmp / 'docker-calls.jsonl'
        self.state = {'containers': [self.container], 'volumes': [], 'networks': [], 'daemon': 'fixture-daemon'}
        self.save_state()
        fake = self.tmp / 'fake-bin'
        fake.mkdir()
        docker = fake / 'docker'
        docker.write_text('#!' + sys.executable + '\n' + '''import json, os, sys
from pathlib import Path
p = Path(os.environ['CLEANUP_STATE'])
s = json.loads(p.read_text())
a = sys.argv[1:]
with open(os.environ['CLEANUP_LOG'], 'a') as f: f.write(json.dumps(a) + '\\n')
if a[:2] == ['context','inspect']:
 print(json.dumps([{'Endpoints':{'docker':{'Host':s.get('host','unix:///var/run/docker.sock')}}}]))
 sys.exit(0)
a = a[2:]
if a[:1] == ['info']: print(json.dumps(s['daemon']))
elif a[:1] == ['ps']: print('\\n'.join(r['Id'] for r in s['containers']))
elif a[:1] == ['inspect']: print(json.dumps([r for r in s['containers'] if r['Id'] in a[1:]]))
elif a[:1] == ['stop']:
 if s.get('stop_failure'): sys.exit(1)
 for r in s['containers']:
  if r['Id'] in a[1:]: r['State'] = {'Running':False,'Status':'exited'}
 p.write_text(json.dumps(s))
elif a[:1] == ['rm']:
 if any(r['State']['Running'] for r in s['containers'] if r['Id'] in a[1:]): sys.exit(1)
 s['containers'] = [r for r in s['containers'] if r['Id'] not in a[1:]]
 p.write_text(json.dumps(s))
elif a[:1] in (['volume'], ['network']):
 kind = a[0] + 's'
 key = 'Name' if kind == 'volumes' else 'Id'
 if a[1] == 'ls': print('\\n'.join(r[key] for r in s[kind]))
 elif a[1] == 'inspect': print(json.dumps([r for r in s[kind] if r[key] in a[2:]]))
 elif a[1] == 'rm':
  if s.get('resource_busy'): sys.exit(1)
  s[kind] = [r for r in s[kind] if r[key] not in a[2:]]
  p.write_text(json.dumps(s))
 else: sys.exit(92)
else: sys.exit(91)
''')
        docker.chmod(0o700)
        self.patch = mock.patch.dict(os.environ, {**self.env, 'PATH': str(fake) + os.pathsep + os.environ['PATH'],
            'CLEANUP_STATE': str(self.state_path), 'CLEANUP_LOG': str(self.log_path),
            'DOCKER_HOST': '', 'DOCKER_CONTEXT': ''})
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.owner = mock.patch.object(self.m, 'process_stamp', return_value=None)
        self.owner.start()
        self.addCleanup(self.owner.stop)

    def save_state(self):
        self.state_path.write_text(json.dumps(self.state))

    def sweep(self):
        return self.m.docker_cleanup_nodes(self.inst, [self.node])

    def mutations(self):
        rows = [json.loads(line) for line in self.log_path.read_text().splitlines()] if self.log_path.exists() else []
        return [r for r in rows if len(r) > 2 and r[2] in ('stop', 'rm', 'volume', 'network')]

    def test_stops_only_proven_owned_abandoned_test_container(self):
        result = self.sweep()
        self.assertEqual(result['stopped'], [self.identity])
        self.assertEqual(self.mutations(), [['--host', 'unix:///var/run/docker.sock', 'stop', self.identity]])
        self.assertFalse(json.loads(self.state_path.read_text())['containers'][0]['State']['Running'])

    def test_live_unknown_or_recycled_identity_is_retained(self):
        for stamp in ('fixture-birth', 'unknown', 'different-birth'):
            with self.subTest(stamp=stamp), mock.patch.object(self.m, 'process_stamp', return_value=stamp):
                self.assertEqual(self.sweep()['stopped'], [])
        self.assertEqual(self.mutations(), [])

    def test_scope_prefix_preview_dev_and_automatically_removed_are_retained(self):
        original = json.loads(json.dumps(self.container))
        variants = [({'brain.docker-scope': 'foreign'}, {}),
                    ({'com.docker.compose.project': 'demo-development'}, {}),
                    ({'com.docker.compose.project': 'demo-verify-preview'}, {}),
                    ({'brain.preview': 'true'}, {}), ({}, {'AutoRemove': True})]
        for labels, config in variants:
            self.container.update(json.loads(json.dumps(original)))
            self.container['Config']['Labels'].update(labels)
            self.container['HostConfig'].update(config)
            self.save_state()
            self.assertEqual(self.sweep()['stopped'], [])
        self.assertEqual(self.mutations(), [])

    def test_missing_owner_young_or_bind_mounted_resources_are_retained(self):
        self.container['Config']['Labels'].pop('brain.owner-stamp')
        self.save_state()
        self.assertEqual(self.sweep()['stopped'], [])
        self.container['Config']['Labels']['brain.owner-stamp'] = 'fixture-birth'
        self.container['Created'] = '2999-01-01T00:00:00Z'
        self.save_state()
        self.assertEqual(self.sweep()['stopped'], [])
        self.container['Created'] = '2020-01-01T00:00:00Z'
        self.container['Mounts'] = [{'Type': 'bind', 'Source': '/fictional/real-data'}]
        self.save_state()
        self.assertEqual(self.sweep()['stopped'], [])
        self.assertEqual(self.mutations(), [])

    def test_incomplete_inventory_or_foreign_project_peer_vetoes_stop(self):
        foreign = json.loads(json.dumps(self.container))
        foreign['Id'] = 'b' * 64
        foreign['Config']['Labels']['brain.docker-scope'] = 'foreign'
        self.state['containers'].append(foreign)
        self.save_state()
        self.assertEqual(self.sweep()['stopped'], [])
        self.assertEqual(self.mutations(), [])

    def test_conflicting_selectors_unpinned_endpoint_or_daemon_retains(self):
        with mock.patch.dict(os.environ, {'DOCKER_HOST': 'unix:///var/run/docker.sock', 'DOCKER_CONTEXT': 'other'}):
            self.assertEqual(self.sweep()['stopped'], [])
        self.state['daemon'] = 'other-daemon'
        self.save_state()
        self.assertEqual(self.sweep()['stopped'], [])
        self.state['daemon'] = 'fixture-daemon'
        self.state['host'] = 'unix:///arbitrary/forwarded.sock'
        self.save_state()
        self.assertEqual(self.sweep()['stopped'], [])
        self.assertEqual(self.mutations(), [])

    def test_discovery_failure_is_quiet_and_stop_failure_is_reported(self):
        with mock.patch.object(self.m.shutil, 'which', return_value=None):
            self.assertEqual(self.sweep()['stopped'], [])
        self.state['stop_failure'] = True
        self.save_state()
        result = self.sweep()
        self.assertEqual(result['stopped'], [])
        self.assertTrue(result['errors'])


    def removable_resources(self):
        self.node['fm']['docker_cleanup_mode'] = 'remove'
        labels = dict(self.container['Config']['Labels'])
        self.state['volumes'] = [{'Name': 'demo-verify-one-data', 'Labels': labels,
                                  'CreatedAt': '2020-01-01T00:00:00Z'}]
        self.state['networks'] = [{'Id': 'c' * 64, 'Name': 'demo-verify-one-net', 'Labels': labels,
                                   'Created': '2020-01-01T00:00:00Z', 'Containers': {}}]
        self.save_state()

    def test_removes_stopped_owned_container_and_unused_owned_volume_network(self):
        self.removable_resources()
        result = self.sweep()
        self.assertEqual(result['stopped'], [self.identity])
        self.assertEqual(result['removed'], [self.identity, 'volume:demo-verify-one-data', 'network:' + 'c' * 64])
        state = json.loads(self.state_path.read_text())
        self.assertEqual([state[k] for k in ('containers', 'volumes', 'networks')], [[], [], []])
        for command in self.mutations():
            self.assertNotIn('--force', command)
            self.assertNotIn('-v', command)
            self.assertNotIn('prune', command)

    def test_unused_foreign_shared_young_or_attached_resources_are_retained(self):
        self.removable_resources()
        self.state['volumes'][0]['Labels'] = {'brain.docker-scope': 'foreign'}
        self.state['networks'][0]['Containers'] = {'foreign-container': {}}
        self.save_state()
        result = self.sweep()
        self.assertEqual(result['removed'], [self.identity])
        state = json.loads(self.state_path.read_text())
        self.assertEqual(len(state['volumes']), 1)
        self.assertEqual(len(state['networks']), 1)

    def test_live_foreign_container_volume_reference_prevents_removal(self):
        self.removable_resources()
        foreign = json.loads(json.dumps(self.container))
        foreign['Id'] = 'b' * 64
        foreign['Config']['Labels'] = {'com.docker.compose.project': 'other-development'}
        foreign['Mounts'] = [{'Type': 'volume', 'Name': 'demo-verify-one-data'}]
        self.state['containers'].append(foreign)
        self.save_state()
        result = self.sweep()
        self.assertNotIn('volume:demo-verify-one-data', result['removed'])
        self.assertEqual(len(json.loads(self.state_path.read_text())['volumes']), 1)

    def test_docker_busy_refusal_retains_resources_and_reports_error(self):
        self.removable_resources()
        self.state['resource_busy'] = True
        self.save_state()
        result = self.sweep()
        self.assertEqual(result['removed'], [self.identity])
        self.assertTrue(result['errors'])
        self.assertEqual(len(json.loads(self.state_path.read_text())['volumes']), 1)


class ReservationBootIdentity(unittest.TestCase):
    def setUp(self):
        self.m = core_loader.SourceFileLoader('boot_fixture', str(CORE / 'bin/brain')).load_module()
        self.row = {'token': 'a' * 32, 'owner': [12345, 'fixture-birth'],
                    'kind': 'verify', 'state': 'running', 'boot': 'linux:old-boot'}

    def test_different_known_boot_reclaims_without_pid_probe(self):
        with mock.patch.object(self.m, 'machine_boot_id', return_value='linux:new-boot'), \
                mock.patch.object(self.m, 'process_stamp', side_effect=AssertionError('do not probe prior-boot PIDs')):
            self.assertFalse(self.m.machine_queue.alive(self.row))

    def test_same_missing_or_unknown_boot_preserves_identity_rules(self):
        for boot in ('linux:old-boot', None):
            with mock.patch.object(self.m, 'machine_boot_id', return_value=boot), \
                    mock.patch.object(self.m, 'process_stamp', return_value='unknown'):
                self.assertTrue(self.m.machine_queue.alive(self.row))
        legacy = {k: v for k, v in self.row.items() if k != 'boot'}
        with mock.patch.object(self.m, 'machine_boot_id', return_value='linux:new-boot'), \
                mock.patch.object(self.m, 'process_stamp', return_value='fixture-birth'):
            self.assertTrue(self.m.machine_queue.alive(legacy))
        self.assertTrue(self.m.machine_queue.valid(legacy))
        for invalid in ('', 12, [], 'unknown'):
            self.assertFalse(self.m.machine_queue.valid(dict(self.row, boot=invalid)))

    def test_linux_and_darwin_boot_reading_is_stable_and_failure_is_unknown(self):
        with mock.patch.object(self.m.sys, 'platform', 'linux'), \
                mock.patch.object(self.m.Path, 'read_text', return_value='12345678-1234-1234-1234-123456789abc\n'):
            self.assertEqual(self.m.machine_boot_id(), 'linux:12345678-1234-1234-1234-123456789abc')
        with mock.patch.object(self.m.sys, 'platform', 'darwin'), \
                mock.patch.object(self.m.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '{ sec = 12345, usec = 67 } fixture date')):
            self.assertEqual(self.m.machine_boot_id(), 'darwin:12345:67')
        with mock.patch.object(self.m.sys, 'platform', 'darwin'), \
                mock.patch.object(self.m.subprocess, 'run', side_effect=OSError('unavailable')):
            self.assertIsNone(self.m.machine_boot_id())

    def test_native_admission_publishes_boot_identity(self):
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(self.m, 'machine_dir', return_value=Path(folder)), \
                mock.patch.object(self.m, 'machine_boot_id', return_value='linux:current-boot'), \
                mock.patch.object(self.m, 'process_stamp', return_value='fixture-birth'), \
                mock.patch.object(self.m, 'machine_reading', return_value={}), \
                mock.patch.object(self.m, 'machine_limit', return_value=2):
            token = self.m.machine_wait('verify')
            rows = json.loads((Path(folder) / 'machine-care-queue.json').read_text())
            self.assertEqual(rows[0]['boot'], 'linux:current-boot')
            self.m.machine_release(token)
            self.assertEqual(json.loads((Path(folder) / 'machine-care-queue.json').read_text()), [])


class HoldCpuDiagnostics(unittest.TestCase):
    def setUp(self):
        self.m = core_loader.SourceFileLoader('cpu_fixture', str(CORE / 'bin/brain')).load_module()

    def test_top_cpu_reports_pid_percent_and_basename_without_arguments(self):
        output = ' 42 180.5 /System/Library/Fixture Indexer\n 43 20.2 /usr/bin/python3\n 44 2.0 helper\n'
        with mock.patch.object(self.m.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, output)) as run:
            self.assertEqual(self.m.machine_top_cpu(), [
                {'pid': 42, 'cpu_percent': 180.5, 'command': 'Fixture Indexer'},
                {'pid': 43, 'cpu_percent': 20.2, 'command': 'python3'},
                {'pid': 44, 'cpu_percent': 2.0, 'command': 'helper'}])
            self.assertEqual(run.call_args[0][0], ['ps', '-A', '-o', 'pid=', '-o', 'pcpu=', '-o', 'comm='])
            self.assertEqual(run.call_args[1]['timeout'], 1)

    def test_bad_rows_and_command_failure_do_not_invent_a_culprit(self):
        for output in ('bad data', '1 nan secret\n2 inf private\n-1 99 wrong\n3 -5 invalid\n', ''):
            with mock.patch.object(self.m.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, output)):
                self.assertEqual(self.m.machine_top_cpu(), [])
        with mock.patch.object(self.m.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ps', 1)):
            self.assertEqual(self.m.machine_top_cpu(), [])

    def test_only_hold_state_collects_and_displays_cpu_observation(self):
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(self.m, 'machine_dir', return_value=Path(folder)), \
                mock.patch.object(self.m, 'machine_reading', return_value={'mem_free_gb': 1}), \
                mock.patch.object(self.m, 'machine_top_cpu', return_value=[{'pid': 42, 'cpu_percent': 80, 'command': 'fixture'}]) as top:
            state = self.m.machine_state()
            self.assertEqual(top.call_count, 1)
            self.assertIn('top CPU: fixture pid=42 80%', self.m.machine_summary(state))
            with mock.patch.object(self.m, 'machine_reading', return_value={}):
                self.assertNotIn('top CPU:', self.m.machine_summary(self.m.machine_state()))
                self.assertEqual(top.call_count, 1)


class TerminalProducer(Scenario):
    def setUp(self):
        super().setUp()
        self.m = core_loader.SourceFileLoader('terminal_producer_fixture', str(CORE / 'bin/brain')).load_module()
        self.root = self.init('alice')
        self.inst = self.m.Instance(self.root)
        self.patch = mock.patch.dict(os.environ, self.env)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.clock = argparse.Namespace(time=lambda: 100000.0, monotonic=lambda: 100000.0,
                                        sleep=lambda _: self.fail('unexpected admission sleep'))

    def waits(self):
        return json.loads((self.root / '.brain/wake/machine-holds.json').read_text())['waits']

    def test_launch_failure_keeps_original_cause_once_and_retries_wake(self):
        with mock.patch.object(self.m, 'request_wake', side_effect=[False, True]) as wake:
            with self.assertRaisesRegex(OSError, 'fixture launch failed'):
                with self.m.machine_run('verify', self.inst, 'root', 'job/leaf', clock=self.clock):
                    raise OSError('fixture launch failed')
            self.m.machine_wait_notices(self.inst, 100001.0)
            self.m.machine_wait_notices(self.inst, 100002.0)
        row = next(iter(self.waits().values()))
        self.assertEqual(row['outcome'], 'error')
        self.assertIn('fixture launch failed', row['reason'])
        self.assertEqual(row['ended_at'], 100000.0)
        self.assertEqual(wake.call_count, 2)
        self.assertEqual(wake.call_args_list[0].kwargs['items'], wake.call_args_list[1].kwargs['items'])
        self.assertEqual(wake.call_args.kwargs['items'][0]['kind'], 'machine-exited')

    def test_cleanup_runs_all_and_handled_outer_exception_cannot_hide_failure(self):
        seen = []
        def fail():
            seen.append('first')
            raise OSError('cleanup failed')
        with mock.patch.object(self.m, 'request_wake', return_value=True):
            try:
                raise ValueError('unrelated handled exception')
            except ValueError:
                with self.assertRaisesRegex(OSError, 'cleanup failed'):
                    with self.m.machine_run('heavy', self.inst, 'root', clock=self.clock) as token:
                        self.m.machine_cleanup(token, [fail, lambda: seen.append('second')], primary=None)
        self.assertEqual(seen, ['first', 'second'])
        row = next(iter(self.waits().values()))
        self.assertEqual(row['outcome'], 'error')
        self.assertIn('cleanup failed', row['reason'])
        self.assertEqual(row['cleanup_errors'], ['OSError: cleanup failed'])

    def test_cleanup_preserves_explicit_primary_and_records_secondary(self):
        def fail():
            raise OSError('secondary cleanup')
        primary = ValueError('primary launcher')
        with mock.patch.object(self.m, 'request_wake', return_value=True):
            with self.assertRaisesRegex(ValueError, 'primary launcher'):
                with self.m.machine_run('verify', self.inst, 'root', clock=self.clock) as token:
                    try:
                        raise primary
                    finally:
                        self.m.machine_cleanup(token, [fail], primary=primary)
        row = next(iter(self.waits().values()))
        self.assertIn('primary launcher', row['reason'])
        self.assertEqual(row['cleanup_errors'], ['OSError: secondary cleanup'])

    def test_success_and_nonzero_exit_have_one_immutable_terminal_transition(self):
        with mock.patch.object(self.m, 'request_wake', return_value=True) as wake:
            with self.m.machine_run('heavy', self.inst, 'root', clock=self.clock) as token:
                token.result = 7
            self.m.machine_terminal(self.inst, token, result=0)
            self.m.machine_wait_notices(self.inst, 100010.0)
        row = self.waits()[token]
        self.assertEqual(row['outcome'], 'failed')
        self.assertIn('status 7', row['reason'])
        self.assertEqual(wake.call_count, 1)

    def test_failure_before_queue_publication_still_has_a_terminal_reason(self):
        with mock.patch.object(self.m, 'request_wake', return_value=True), \
                mock.patch.object(self.m, 'process_stamp', return_value='unknown'):
            with self.assertRaises(SystemExit):
                with self.m.machine_run('verify', self.inst, 'root', clock=self.clock) as token:
                    self.m.machine_wait('verify', self.inst, 'root', token=token)
        row = self.waits()[token]
        self.assertEqual(row['outcome'], 'error')
        self.assertIn('cannot establish', row['reason'])


class RecordedAdmission(Scenario):
    def setUp(self):
        super().setUp()
        self.m = core_loader.SourceFileLoader('recorded_admission_fixture', str(CORE / 'bin/brain')).load_module()
        self.root = self.init('alice')
        self.inst = self.m.Instance(self.root)
        self.patch = mock.patch.dict(os.environ, self.env)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.clock = argparse.Namespace(at=100000.0)
        self.clock.time = self.clock.monotonic = lambda: self.clock.at
        self.good = {'cpu_idle_pct': 75, 'load_per_cpu': [2, 2, 2], 'mem_free_gb': 32, 'disk_free_gb': 200}
        self.hot = dict(self.good, cpu_idle_pct=2)

    def test_recorded_hold_resumes_on_first_clear_reading(self):
        readings = iter([self.hot, self.good])
        def pause(seconds):
            self.clock.at += 900
        with mock.patch.object(self.m, 'request_wake', return_value=True) as wake:
            with self.m.machine_run('verify', self.inst, 'root', clock=self.clock) as token:
                self.m.machine_wait('verify', self.inst, 'root', token=token, clock=self.clock,
                                    read=lambda: next(readings), pause=pause)
        waits = json.loads((self.root / '.brain/wake/machine-holds.json').read_text())['waits']
        self.assertEqual(waits[token]['resumed_at'], 100900.0)
        self.assertEqual(waits[token]['outcome'], 'success')
        self.assertEqual([c.kwargs['items'][0]['kind'] for c in wake.call_args_list],
                         ['machine-resumed', 'machine-completed'])

    def test_fifteen_and_fortyfive_minute_reports_are_once_per_wait(self):
        def pause(seconds):
            self.clock.at += 900
            self.m.machine_wait_notices(self.inst, self.clock.at)
            self.m.machine_wait_notices(self.inst, self.clock.at)
            if self.clock.at >= 102700:
                raise KeyboardInterrupt()
        # Queue capacity, rather than resource pressure, can legitimately wait
        # past the resource deadline. It must remain visible and report once.
        self.good['cpu_idle_pct'] = 75
        with mock.patch.object(self.m, 'request_wake', return_value=True) as wake, \
                mock.patch.object(self.m, 'machine_limit', return_value=0):
            with self.assertRaises(KeyboardInterrupt):
                with self.m.machine_run('verify', self.inst, 'root', clock=self.clock) as token:
                    self.m.machine_wait('verify', self.inst, 'root', token=token, clock=self.clock,
                                        read=lambda: self.good, pause=pause)
        kinds = [c.kwargs['items'][0]['kind'] for c in wake.call_args_list]
        self.assertEqual(kinds.count('machine-coordinator'), 1)
        self.assertEqual(kinds.count('machine-root'), 1)
        self.assertEqual(kinds.count('machine-exited'), 1)

    def test_maximum_hold_rejects_unknown_reduction_with_terminal_reason(self):
        def pause(seconds):
            self.clock.at += 1200
        with mock.patch.object(self.m, 'request_wake', return_value=True):
            with self.assertRaisesRegex(SystemExit, 'cannot enforce reduced parallelism'):
                with self.m.machine_run('verify', self.inst, 'root', clock=self.clock) as token:
                    self.m.machine_wait('verify', self.inst, 'root', token=token, clock=self.clock,
                                        read=lambda: self.hot, pause=pause, reduction_ready=False)
        row = json.loads((self.root / '.brain/wake/machine-holds.json').read_text())['waits'][token]
        self.assertEqual(row['state'], 'exited')
        self.assertIn('cannot enforce', row['reason'])

    def test_supported_reduction_keeps_hard_resource_limits(self):
        def pause(seconds):
            self.clock.at += 1200
        unsafe = dict(self.hot, mem_free_gb=1)
        with mock.patch.object(self.m, 'request_wake', return_value=True):
            with self.assertRaisesRegex(SystemExit, 'unsafe resource pressure'):
                with self.m.machine_run('verify', self.inst, 'root', clock=self.clock) as token:
                    self.m.machine_wait('verify', self.inst, 'root', token=token, clock=self.clock,
                                        read=lambda: unsafe, pause=pause, reduction_ready=True)

    def test_supported_cpu_reduction_is_recorded_and_exported(self):
        def pause(seconds):
            self.clock.at += 1200
        with mock.patch.object(self.m, 'request_wake', return_value=True):
            with self.m.machine_run('verify', self.inst, 'root', clock=self.clock) as token:
                self.m.machine_wait('verify', self.inst, 'root', token=token, clock=self.clock,
                                    read=lambda: self.hot, pause=pause, reduction_ready=True)
                self.assertEqual(self.m.machine_environment(token, {})['BRAIN_PARALLELISM'], '1')
        row = json.loads((self.root / '.brain/wake/machine-holds.json').read_text())['waits'][token]
        self.assertTrue(row['reduced'])

    def test_known_idle_cpu_overrides_io_load_but_unknown_cpu_keeps_load_fallback(self):
        self.assertEqual(self.m.machine_reasons(self.good, self.m.machine_policy()), [])
        unknown = {k: v for k, v in self.good.items() if k != 'cpu_idle_pct'}
        self.assertTrue(self.m.machine_reasons(unknown, self.m.machine_policy()))
