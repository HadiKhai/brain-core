"""Lane selection and evidence regression scenarios, using only fabricated nodes."""
import json
from pathlib import Path
from unittest import mock

import test_verify
from scenario import Scenario
import core_loader


class VerifyLanes(Scenario):
    job = test_verify.WorkerReadyPackets.job
    record = test_verify.WorkerReadyPackets.record
    packet = test_verify.WorkerReadyPackets.packet

    def setup_lanes(self, extra=None):
        r, home, wt = self.job()
        for name in ('static.txt', 'ui.txt', 'server.txt'):
            (wt / name).write_text('initial')
        lanes = [dict(name=name, command='echo ' + name, inputs=[path]) for name, path in
                 [('static', '**/*.md'), ('ui', 'ui.txt'), ('integration', 'server.txt')]]
        if extra:
            extra(lanes)
        self.git('add', '.', cwd=wt)
        self.git('commit', '-qm', 'lane fixtures', cwd=wt)
        self.b(r, 'set', 'a', 'verify=false', 'verify_lanes=' + json.dumps(lanes))
        return r, home, wt, lanes

    def read_packet(self):
        return json.loads(self.packet().read_text())['ready']

    def commit(self, wt, name, text='edited'):
        (wt / name).write_text(text)
        self.git('add', '.', cwd=wt)
        self.git('commit', '-qm', 'edit fixture', cwd=wt)

    def test_affected_lanes_reuse_and_full_private_logs(self):
        r, home, wt, lanes = self.setup_lanes()
        self.record(r)
        first = self.read_packet()
        self.assertEqual(first['verify_result'], 0)  # full verify=false was not executed
        for row in first['verify_lanes']:
            self.assertFalse(row['reused'])
            self.assertEqual(Path(row['log']).read_text(), row['name'] + '\n')
            self.assertEqual(Path(row['log']).stat().st_mode & 0o777, 0o600)
        for path, expected in [('README.md', ['static']), ('ui.txt', ['ui']), ('server.txt', ['integration'])]:
            self.commit(wt, path)
            self.record(r)
            packet = self.read_packet()
            self.assertEqual([x['name'] for x in packet['verify_lanes'] if not x['reused']], expected)
            self.assertIn('verify lane ui: passed', self.b(r, 'ready', 'a', '--branch', 'job'))
        self.record(r)
        self.assertTrue(all(x['reused'] for x in self.read_packet()['verify_lanes']))

    def test_map_config_lockfile_base_and_ignored_input_force_all(self):
        r, home, wt, lanes = self.setup_lanes()
        self.record(r)
        lanes[0]['command'] = 'echo static-edited'
        self.b(r, 'set', 'a', 'verify_lanes=' + json.dumps(lanes))
        self.assertIn('lane configuration', self.b(r, 'ready', 'a', '--branch', 'job', ok=False))
        self.record(r)
        self.assertTrue(all(not x['reused'] for x in self.read_packet()['verify_lanes']))
        self.commit(wt, 'package-lock.json', '{}')
        self.record(r)
        self.assertTrue(all(not x['reused'] for x in self.read_packet()['verify_lanes']))
        self.b(r, 'set', 'a', 'verify=true')
        self.record(r)
        self.assertTrue(all(not x['reused'] for x in self.read_packet()['verify_lanes']))
        (wt / '.env').write_text('VALUE=fixture')
        self.git('add', '.env', cwd=wt)
        self.git('commit', '-qm', 'env fixture', cwd=wt)
        self.record(r)
        self.assertTrue(all(not x['reused'] for x in self.read_packet()['verify_lanes']))
        self.git('commit', '--allow-empty', '-qm', 'base moves', cwd=home)
        self.git('rebase', 'main', cwd=wt)
        self.record(r)
        self.assertTrue(all(not x['reused'] for x in self.read_packet()['verify_lanes']))

    def test_strict_always_dependencies_nightly_and_fitness_failure(self):
        def customize(lanes):
            lanes[0].update(strict_inputs=['ui.txt'])
            lanes[1].update(always=True)
            lanes.append(dict(name='night', command='false', inputs=['**'], schedule='nightly'))
        r, home, wt, lanes = self.setup_lanes(customize)
        self.record(r)
        self.commit(wt, 'ui.txt')
        self.record(r)
        self.assertEqual([x['name'] for x in self.read_packet()['verify_lanes'] if not x['reused']], ['static', 'ui'])
        lanes[0]['command'] = 'echo failing; exit 7'
        lanes[1]['requires'] = ['static']
        self.b(r, 'set', 'a', 'verify_lanes=' + json.dumps(lanes))
        self.record(r, ok=False)
        packet = self.read_packet()
        self.assertEqual([x['status'] for x in packet['verify_lanes']], ['failed', 'passed', 'skipped'])
        self.assertEqual(packet['verify_result'], 7)
        self.b(r, 'set', 'a', 'verify_lanes_check=echo coverage-failed; exit 3')
        self.record(r, ok=False)
        packet = self.read_packet()
        self.assertEqual(packet['verify_lanes_check']['result'], 3)
        self.assertTrue(all(x['status'] == 'skipped' for x in packet['verify_lanes']))

    def test_dynamic_map_output_glob_refusal_and_verify_mutation(self):
        r, home, wt, lanes = self.setup_lanes()
        self.commit(wt, 'lanes.json', json.dumps(lanes))
        self.b(r, 'set', 'a', 'verify_lanes=', 'verify_lanes_from=cat lanes.json')
        self.record(r)
        # Whitespace in the command output changes the map fingerprint.
        self.commit(wt, 'lanes.json', json.dumps(lanes, indent=2))
        self.record(r)
        self.assertTrue(all(not x['reused'] for x in self.read_packet()['verify_lanes']))
        for pattern in ('src/[ab].py', '../bad', 'a/{b,c}', 'a/+(b)', '/absolute', 'a/**b'):
            lanes[0]['inputs'] = [pattern]
            self.commit(wt, 'lanes.json', json.dumps(lanes))
            self.assertIn('unsupported lane glob', self.record(r, ok=False))
        lanes[0]['inputs'] = ['**']
        lanes[0]['command'] = 'echo mutated > ui.txt'
        self.commit(wt, 'lanes.json', json.dumps(lanes))
        self.assertIn('verify changed', self.record(r, ok=False))

    def test_glob_semantics_and_train_recorded_base(self):
        r, home, wt, lanes = self.setup_lanes()
        m = core_loader.SourceFileLoader('lane_hooks', str(Path(__file__).resolve().parents[1] / 'bin/brain')).load_module()
        self.assertTrue(m.snapshot.__module__.endswith('.nodes'))
        self.assertTrue(m.lane_snapshot.__module__.endswith('.verify_lanes'))
        self.assertTrue(m.matches('root.py', ['**/*.py']))
        self.assertFalse(m.matches('src/nested/a.py', ['src/*.py']))
        self.assertFalse(m.matches('src/no.py', ['**/*.py', '!src/no.py']))
        self.record(r)
        inst = m.Instance(r)
        n = inst.node('a')
        self.git('commit', '--allow-empty', '-qm', 'main advances', cwd=home)
        with mock.patch.dict('os.environ', self.env):
            self.assertFalse(m.ready_state(inst, n, 'job')['ready'])
            self.assertTrue(m.ready_state(inst, n, 'job', train=True)['ready'])
        n['fm']['merge'] = 'pr'
        self.assertIn('train requires', '; '.join(m.ready_preflight(n, 'job', base=self.read_packet()['integration_base'])[0]))

    def test_last_green_survives_failures_and_reverify_forces_all(self):
        r, home, wt, lanes = self.setup_lanes()
        # The UI command depends on an untracked file outside verify_inputs, to
        # exercise a red attempt at the exact same declared inputs/config.
        lanes[1]['command'] = 'test ! -e temporary-red; echo ui; test ! -e temporary-red'
        self.b(r, 'set', 'a', 'verify_lanes=' + json.dumps(lanes))
        self.git('config', 'core.excludesFile', str(self.tmp / 'ignore-fixture'), cwd=wt)
        (self.tmp / 'ignore-fixture').write_text('temporary-red\n')
        self.record(r)
        green = self.read_packet()
        (wt / 'temporary-red').touch()
        self.b(r, 'ready', 'a', '--branch', 'job', '--record', '--reviews', '2', '--reverify', ok=False)
        self.assertEqual(self.read_packet()['verify_lanes'][1]['status'], 'failed')
        (wt / 'temporary-red').unlink()
        self.record(r)
        restored = self.read_packet()
        self.assertTrue(all(x['reused'] for x in restored['verify_lanes']))
        self.assertEqual(restored['verify_lanes'][1]['log'], green['verify_lanes'][1]['log'])
        self.b(r, 'ready', 'a', '--branch', 'job', '--record', '--reviews', '2', '--reverify')
        self.assertTrue(all(not x['reused'] for x in self.read_packet()['verify_lanes']))

    def test_inline_map_and_malformed_lane_evidence_fail_closed(self):
        r, home, wt = self.job()
        self.b(r, 'set', 'a', 'verify_lanes=static: echo inline | **; ui: echo ui | calc.py')
        self.record(r)
        self.assertEqual([x['name'] for x in self.read_packet()['verify_lanes']], ['static', 'ui'])
        path = self.packet()
        original = json.loads(path.read_text())
        import copy
        for field, value in [('result', '0'), ('duration', 'bad'), ('log', None), ('input_hash', 'bad'), ('status', 'skipped')]:
            packet = copy.deepcopy(original)
            packet['ready']['verify_lanes'][0][field] = value
            path.write_text(json.dumps(packet))
            self.assertIn('not ready', self.b(r, 'ready', 'a', '--branch', 'job', ok=False))
        path.write_text(json.dumps(original))
        for bad in ('null', '[{}]', '[{"name":"a","command":"true","inputs":"**"}]',
                    '[{"name":"a","command":"true","requires":["missing"]}]'):
            self.b(r, 'set', 'a', 'verify_lanes_from=echo ' + "'" + bad + "'", 'verify_lanes=')
            self.assertIn('verify lanes invalid', self.record(r, ok=False))

    def test_toolchain_and_ports_force_every_lane(self):
        r, home, wt, lanes = self.setup_lanes()
        tools = self.tmp / 'tools'
        tools.mkdir()
        executable = tools / 'lane-tool'
        executable.write_text('#!/bin/sh\necho fixture-tool\n')
        executable.chmod(0o755)
        self.env['PATH'] = str(tools) + ':' + self.env['PATH']
        lanes[0]['command'] = 'lane-tool'
        self.b(r, 'set', 'a', 'verify_lanes=' + json.dumps(lanes))
        self.record(r)
        executable.write_text('#!/bin/sh\necho changed-tool-version\n')
        self.assertIn('lane configuration', self.b(r, 'ready', 'a', '--branch', 'job', ok=False))
        self.record(r)
        self.assertTrue(all(not x['reused'] for x in self.read_packet()['verify_lanes']))
        local = r / '.brain/local.json'
        data = json.loads(local.read_text())
        # Instance bindings use the same per-node worktree port data as preflight.
        def alter(value):
            if isinstance(value, dict):
                if 'worktrees' in value and 'job' in value['worktrees']:
                    value['worktrees']['job']['port'] += 10
                for item in value.values():
                    alter(item)
            elif isinstance(value, list):
                for item in value:
                    alter(item)
        alter(data)
        local.write_text(json.dumps(data))
        self.record(r)
        self.assertTrue(all(not x['reused'] for x in self.read_packet()['verify_lanes']))

    def test_dynamic_map_cannot_mutate_unselected_files_during_readiness(self):
        r, home, wt, lanes = self.setup_lanes()
        self.commit(wt, 'lanes.json', json.dumps(lanes))
        self.b(r, 'set', 'a', 'verify_lanes=', 'verify_lanes_from=cat lanes.json')
        self.record(r)
        # Replacing the configured map command invalidates context, independently
        # of its side effect; ready must additionally report the dirty worktree.
        self.b(r, 'set', 'a', 'verify_lanes_from=echo dirty > calc.py; cat lanes.json')
        self.assertIn('dirty', self.b(r, 'ready', 'a', '--branch', 'job', ok=False))

    def test_all_reused_lanes_do_not_take_a_verify_slot(self):
        r, home, wt, lanes = self.setup_lanes()
        self.record(r)
        m = core_loader.SourceFileLoader('lane_reuse_slot', str(Path(__file__).resolve().parents[1] / 'bin/brain')).load_module()
        inst = m.Instance(r)
        n = inst.node('a')
        previous = self.read_packet()
        with mock.patch.dict('os.environ', self.env), mock.patch.object(m.record_ready.__globals__['_m_machine'], 'machine_wait', side_effect=AssertionError('unchanged lanes must not queue')):
            packet, reasons = m.record_ready(inst, n, 'job', 2, previous)
        self.assertEqual(reasons, [])
        self.assertTrue(all(row['reused'] for row in packet['verify_lanes']))
