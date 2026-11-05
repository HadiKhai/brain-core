"""Independent lanes overlap; dependencies and explicit serial work stay ordered."""
import threading
import time
import unittest

import core_loader
from scenario import CORE

m = core_loader.SourceFileLoader('parallel_lane_fixture', str(CORE / 'bin/brain')).load_module()
from importlib import import_module
lanes = import_module(m.lane_map.__module__)


class ParallelLaneScheduler(unittest.TestCase):
    def lane(self, name, requires=(), **extra):
        return dict(name=name, requires=list(requires), workers=1, **extra)

    def test_independent_lanes_overlap_and_dependencies_wait(self):
        barrier = threading.Barrier(2, timeout=3)
        completed = set()
        lock = threading.Lock()
        def execute(lane):
            if lane['name'] in ('a', 'b'):
                barrier.wait()
            else:
                with lock:
                    self.assertEqual(completed, {'a', 'b'})
            with lock:
                completed.add(lane['name'])
            return 0, lane['name'] + '.log', 0.01
        result = lanes.schedule_lanes([self.lane('a'), self.lane('b'), self.lane('c', ('a', 'b'))], execute, 2)
        self.assertEqual(set(result), {'a', 'b', 'c'})
        self.assertEqual(completed, {'a', 'b', 'c'})

    def test_worker_width_serial_reason_and_failure_propagation(self):
        active, maximum, seen = 0, 0, []
        lock = threading.Lock()
        def execute(lane):
            nonlocal active, maximum
            with lock:
                if lane.get('serial_reason'):
                    self.assertEqual(active, 0)
                active += 1
                maximum = max(active, maximum)
                seen.append(lane['name'])
            time.sleep(.01)
            with lock:
                active -= 1
            return (7 if lane['name'] == 'bad' else 0), lane['name'] + '.log', .01
        plan = [self.lane('a'), self.lane('b'), self.lane('exclusive', serial_reason='Shared state'),
                self.lane('bad'), self.lane('skip', ('bad',)), self.lane('independent')]
        result = lanes.schedule_lanes(plan, execute, 2)
        self.assertEqual(maximum, 2)
        self.assertNotIn('skip', seen)
        self.assertEqual(result['skip'][0], None)
        self.assertEqual(result['independent'][0], 0)
        self.assertEqual(seen.index('exclusive'), 2)

    def test_one_worker_and_undeclared_commands_are_serial(self):
        for width in (1, 4):
            active = 0
            lock = threading.Lock()
            def execute(lane):
                nonlocal active
                with lock:
                    self.assertEqual(active, 0)
                    active += 1
                time.sleep(.005)
                with lock:
                    active -= 1
                return 0, 'log', 0
            plan = [dict(name=str(i), requires=[]) for i in range(3)]
            self.assertEqual(len(lanes.schedule_lanes(plan, execute, width)), 3)

    def test_pool_copies_context_and_cancels_on_an_exception(self):
        import contextvars
        marker = contextvars.ContextVar('lane-lease-fixture', default=None)
        marker.set('owned')
        cancelled = threading.Event()
        barrier = threading.Barrier(2, timeout=3)
        stopped = threading.Event()
        def execute(lane):
            self.assertEqual(marker.get(), 'owned')
            barrier.wait()
            if lane['name'] == 'bad':
                raise ValueError('primary lane error')
            self.assertTrue(cancelled.wait(3))
            stopped.set()
            return 0, 'log', 0
        with self.assertRaisesRegex(ValueError, 'primary lane error'):
            lanes.schedule_lanes([self.lane('bad'), self.lane('slow')], execute, 2, cancelled=cancelled)
        self.assertTrue(stopped.is_set())


import json
import shlex
from pathlib import Path
from scenario import Scenario
import test_verify_lanes


class NativeParallelLanes(Scenario):
    fixture_templates = True
    fixture_init_in_process = True
    job = test_verify_lanes.VerifyLanes.job
    setup_lanes = test_verify_lanes.VerifyLanes.setup_lanes
    record = test_verify_lanes.VerifyLanes.record
    packet = test_verify_lanes.VerifyLanes.packet
    read_packet = test_verify_lanes.VerifyLanes.read_packet

    def test_native_lanes_share_a_barrier_and_keep_private_individual_receipts(self):
        root, home, wt, plan = self.setup_lanes()
        script = ("from pathlib import Path; import sys,time; "
                  "name,peer=sys.argv[1:]; Path(name).touch(); end=time.monotonic()+15; "
                  "exec('while not Path(peer).exists() and time.monotonic()<end: time.sleep(.01)'); "
                  "assert Path(peer).exists(), 'independent lane did not start concurrently'; print(Path(name).name)")
        plan = [dict(name=name, command='python3 -c ' + shlex.quote(script) + ' ' + shlex.quote(str(self.tmp / name))
                     + ' ' + shlex.quote(str(self.tmp / peer)),
                     inputs=['server.txt'], workers=1) for name, peer in [('first', 'second'), ('second', 'first')]]
        self.b(root, 'set', 'a', 'verify_lanes=' + json.dumps(plan))
        self.record(root)
        packet = self.read_packet()
        self.assertEqual([row['result'] for row in packet['verify_lanes']], [0, 0])
        for row in packet['verify_lanes']:
            log = Path(row['log'])
            self.assertEqual(log.read_text().strip(), row['name'])
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertNotEqual(packet['verify_lanes'][0]['log'], packet['verify_lanes'][1]['log'])

    def test_queued_lanes_register_helpers_and_inherit_the_lease(self):
        import argparse
        import os
        from unittest import mock
        root, home, wt, plan = self.setup_lanes()
        module = core_loader.SourceFileLoader('parallel_owned_lease', str(CORE / 'bin/brain')).load_module()
        inst = module.Instance(root)
        n = inst.node('a')
        row = module.open_job(module.read_jobs(n), 'job')
        # Production claim/publication checks run against a real private lease.
        # Only the machine host identity is fixture-owned.
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(
                module, 'lifecycle_host_identity', return_value='fixture-boot-a'):
            args = argparse.Namespace(id='a', branch='job', state='done', summary='Synthetic result',
                                      reviews=1, report=None, lessons='none: synthetic fixture')
            request = module.enqueue_handback(inst, n, row, args)
            lease_path = module.queue_path(n, request['id']).with_suffix('.lease')
            script = ('import os,sys; info=os.stat(sys.argv[1]); found=False\n'
                      'for fd in range(3,256):\n'
                      ' try: current=os.fstat(fd)\n'
                      ' except OSError: continue\n'
                      ' if (current.st_dev,current.st_ino)==(info.st_dev,info.st_ino): found=True\n'
                      'assert found, "queue lease did not reach verify command"\n'
                      'print("lease inherited")\n')
            command = 'python3 -c ' + shlex.quote(script) + ' ' + shlex.quote(str(lease_path))
            # The map is a direct run_lanes snapshot; no queued binding is changed.
            env = module.verify_env(n, 'job', n['local']['worktrees']['job'])
            snapshot = module.lane_snapshot(n, n['local']['worktrees']['job'], env, 'fixture-input')
            for lane in snapshot['lanes']:
                lane.update(command=command, workers=1)
            with module.claim_handback(inst, n, request['id']) as claimed:
                module.queue_dispatch(inst, n, claimed)
                result = module.run_lanes(inst, n, 'job', n['local']['worktrees']['job'], env, snapshot,
                                          None, '1' * 40, '2' * 40, 'fixture-input')
                self.assertEqual(result['verify_result'], 0)
                processes = module.read_queue_request(n, request['id'])['run']['processes']
                self.assertEqual(len(processes), len(snapshot['lanes']))
                self.assertTrue(all(pid != os.getpid() and stamp for pid, stamp in processes))
                for lane in result['verify_lanes']:
                    self.assertEqual(Path(lane['log']).read_text().strip(), 'lease inherited')
                module.queue_terminal(inst, n, request['id'], claimed['lease'], 'terminal')

    def test_cancelled_helper_exits_before_the_caller_returns(self):
        import os
        from unittest import mock
        root, home, wt, plan = self.setup_lanes()
        module = core_loader.SourceFileLoader('parallel_cancel_fixture', str(CORE / 'bin/brain')).load_module()
        inst = module.Instance(root)
        n = inst.node('a')
        env = module.verify_env(n, 'job', n['local']['worktrees']['job'])
        marker = self.tmp / 'command.pid'
        script = 'import os,time; from pathlib import Path; Path(' + repr(str(marker)) + ').write_text(str(os.getpid())); time.sleep(60)'
        lane = dict(name='cancel', command='exec python3 -c ' + shlex.quote(script), workers=1, requires=[])
        cancelled = threading.Event()
        errors = []
        def run():
            try:
                module.execute_lane(inst, n, 'job', n['local']['worktrees']['job'], env, lane, cancelled=cancelled)
            except RuntimeError as error:
                errors.append(str(error))
        with mock.patch.dict(os.environ, self.env, clear=True):
            thread = threading.Thread(target=run)
            thread.start()
            try:
                end = time.monotonic() + 15
                while not marker.exists() and thread.is_alive() and time.monotonic() < end:
                    time.sleep(.01)
                self.assertTrue(marker.exists(), errors)
                pid = int(marker.read_text())
            finally:
                cancelled.set()
                thread.join(15)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, ['verify lane cancelled: cancel'])
            self.assertIsNone(module.process_stamp(pid))

    def test_every_cleanup_runs_without_masking_the_primary_lane_error(self):
        import os
        from unittest import mock
        root, home, wt, plan = self.setup_lanes()
        module = core_loader.SourceFileLoader('parallel_cleanup_fixture', str(CORE / 'bin/brain')).load_module()
        inst = module.Instance(root)
        n = inst.node('a')
        owner = module.run_lanes.__globals__
        env = module.verify_env(n, 'job', n['local']['worktrees']['job'])
        snapshot = module.lane_snapshot(n, n['local']['worktrees']['job'], env, 'inputs')
        seen = []
        def cleanup(*args):
            seen.append('cleanup')
            raise RuntimeError('secondary cleanup error')
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(owner['_m_lanes'], 'execute_lane', side_effect=ValueError('primary lane error')), \
                mock.patch.object(module, 'queued_attempt_phase', side_effect=lambda: seen.append('phase')), \
                mock.patch.object(module, 'verify_cleanup', side_effect=cleanup), \
                mock.patch.object(owner['_m_docker_leftovers'], 'docker_leftover_nodes', side_effect=lambda *a: seen.append('leftovers')):
            with self.assertRaisesRegex(ValueError, 'primary lane error'):
                module.run_lanes(inst, n, 'job', n['local']['worktrees']['job'], env, snapshot,
                                 None, '1' * 40, '2' * 40, 'inputs')
        self.assertEqual(seen, ['phase', 'cleanup', 'leftovers'])
