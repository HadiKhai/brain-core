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
            def worker(i):
                try:
                    token = m.machine_wait("verify")
                    try:
                        with mutex:
                            started.append(i)
                            active[0] += 1
                            active[1] = max(active)
                        r = subprocess.run([sys.executable, "-c", "import time; time.sleep(.12); raise SystemExit(" + str(i) + ")"])
                        with mutex:
                            results.append((i, r.returncode))
                            ended.append(i)
                            active[0] -= 1
                    finally:
                        m.machine_release(token)
                except BaseException as e:
                    errors.append(e)
            with mock.patch.object(m, "machine_limit", return_value=limit), contextlib.redirect_stdout(io.StringIO()):
                threads = []
                for i in range(3):
                    t = threading.Thread(target=worker, args=(i,))
                    t.start()
                    threads.append(t)
                    # Wait for each fixture to join the durable queue before starting the next.
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        with m.machine_queue() as q:
                            if len(q.rows) >= i + 1:
                                break
                        time.sleep(.002)
                for t in threads:
                    t.join(10)
                    self.assertFalse(t.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(started, [0, 1, 2])
            self.assertEqual(ended if limit == 1 else sorted(ended), [0, 1, 2])
            self.assertEqual(sorted(results), [(0, 0), (1, 1), (2, 2)])
            self.assertEqual(active[1], limit)

    def test_full_runner_cross_process_limits(self):
        m = self.m
        roots = [self.init(name) for name in ("oak", "elm", "ash")]
        home = self.tmp / "fixture-home"
        self.repo(home, {"fixture.txt": "fixture"})
        for root in roots:
            self.b(root, "set", "root", "home=" + str(home))
        events = self.tmp / "events.jsonl"
        script = self.tmp / "runner.py"
        script.write_text(r"""import core_loader, json, os, shlex, sys, time
from pathlib import Path
m = core_loader.SourceFileLoader('fixture_runner', sys.argv[1]).load_module()
m.MACHINE_RETRY = .01
m.machine_limit = lambda p: int(sys.argv[5])
root, events, i = sys.argv[2:5]
inst = m.Instance(Path(root))
code = ('import json, time; f=open(' + repr(events) + ', "a"); '
        'f.write(json.dumps(["start", ' + i + ', time.monotonic()])+"\\n"); f.flush(); '
        'time.sleep(.3); f.write(json.dumps(["end", ' + i + ', time.monotonic()])+"\\n"); f.flush(); '
        'raise SystemExit(' + i + ')')
command = shlex.quote(sys.executable) + ' -c ' + shlex.quote(code)
result, log = m.run_verify(inst, inst.node('root'), 'fixture', command, {'path': root}, dict(os.environ))
raise SystemExit(result)
""")
        for limit in (1, 2):
            events.write_text("")
            processes = []
            try:
                for i, root in enumerate(roots):
                    processes.append(subprocess.Popen([sys.executable, str(script), str(CORE / "bin/brain"),
                        str(root), str(events), str(i), str(limit)], env=self.env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE))
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        with m.machine_queue() as q:
                            if len(q.rows) >= i + 1:
                                break
                        time.sleep(.002)
                results = [p.wait(timeout=10) for p in processes]
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
                for p in processes:
                    if p.poll() is None:
                        p.kill()
                    p.wait()
                    p.stderr.close()

    def test_lane_owner_requeues_behind_waiting_verify(self):
        m = self.m
        root, home, wt = _test_verify.WorkerReadyPackets.job(self)
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
                        m.run_verify(inst, n, 'waiting', 'true', w, env)
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
                "Path(" + repr(str(fds_file)) + ").write_text(json.dumps(extra))\n"
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
            self.assertEqual(json.loads(fds_file.read_text()), [])
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



if __name__ == "__main__":
    unittest.main()
