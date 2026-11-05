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
