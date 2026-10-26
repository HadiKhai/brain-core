"""Lane selection and evidence regression scenarios, using only fabricated nodes."""
import importlib
import json
import os
import shutil
from pathlib import Path
from unittest import mock

import test_verify
from scenario import Scenario, CORE
import core_loader


class VerifyLanes(Scenario):
    fixture_init_in_process = True
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
        m = core_loader.SourceFileLoader('lane_hooks', str(CORE / 'bin/brain')).load_module()
        package = m._owners['lane_snapshot'].__package__
        nodes = importlib.import_module('.nodes', package)
        verify_lanes = importlib.import_module('.verify_lanes', package)
        self.assertTrue(nodes.snapshot.__module__.endswith('.nodes'))
        self.assertTrue(verify_lanes.lane_snapshot.__module__.endswith('.verify_lanes'))
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
        self.b(r, 'ready', '--summary', 'Synthetic result', 'a', '--branch', 'job', '--record', '--reviews', '2', '--reverify', ok=False)
        self.assertEqual(self.read_packet()['verify_lanes'][1]['status'], 'failed')
        (wt / 'temporary-red').unlink()
        self.record(r)
        restored = self.read_packet()
        self.assertTrue(all(x['reused'] for x in restored['verify_lanes']))
        self.assertEqual(restored['verify_lanes'][1]['log'], green['verify_lanes'][1]['log'])
        self.b(r, 'ready', '--summary', 'Synthetic result', 'a', '--branch', 'job', '--record', '--reviews', '2', '--reverify')
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

    def setup_tool_lane(self):
        r, home, wt, lanes = self.setup_lanes()
        tools = self.tmp / 'tools'
        tools.mkdir()
        executable = tools / 'lane-tool'
        executable.write_text('#!/bin/sh\necho fixture-tool\n')
        executable.chmod(0o755)
        self.env['PATH'] = str(tools) + os.pathsep + self.env['PATH']
        lanes[0]['command'] = 'lane-tool'
        self.b(r, 'set', 'a', 'verify_lanes=' + json.dumps(lanes))
        return r, executable

    def test_ready_uses_recorded_environment_and_rehandback_changes_context(self):
        r, executable = self.setup_tool_lane()
        self.record(r)
        first = self.read_packet()
        empty = self.tmp / 'empty-bin'
        empty.mkdir()
        original_path = self.env['PATH']
        for path in (str(empty) + os.pathsep + original_path,
                     original_path + os.pathsep + str(empty)):
            with self.subTest(path=path):
                self.env['PATH'] = path
                self.env['CFLAGS'] = '-DCOORDINATOR_FIXTURE'
                self.assertEqual(shutil.which('lane-tool', path=path), str(executable))
                self.assertEqual(self.b(r, 'ready', 'a', '--branch', 'job').strip().splitlines()[-1], 'ready')
                self.record(r)
                packet = self.read_packet()
                self.assertNotEqual(packet['lane_context_hash'], first['lane_context_hash'])
                self.assertEqual(packet['verify_lane_env']['PATH'], path)
                self.assertTrue(all(not row['reused'] for row in packet['verify_lanes']))

    def test_different_tool_resolution_changes_context_and_refuses_ready(self):
        r, executable = self.setup_tool_lane()
        self.record(r)
        first = self.read_packet()
        alternate = self.tmp / 'alternate-bin'
        alternate.mkdir()
        shutil.copy2(executable, alternate / executable.name)
        self.env['PATH'] = str(alternate) + os.pathsep + self.env['PATH']
        self.assertEqual(self.b(r, 'ready', 'a', '--branch', 'job').strip().splitlines()[-1], 'ready')
        record = json.loads(self.packet().read_text())
        record['ready']['verify_lane_env']['PATH'] = self.env['PATH']
        self.packet().write_text(json.dumps(record))
        self.assertIn('lane configuration', self.b(r, 'ready', 'a', '--branch', 'job', ok=False))
        self.record(r)
        self.assertNotEqual(self.read_packet()['lane_context_hash'], first['lane_context_hash'])
        self.assertTrue(all(not row['reused'] for row in self.read_packet()['verify_lanes']))

    def test_tool_size_or_mtime_changes_context_and_refuses_ready(self):
        r, executable = self.setup_tool_lane()
        self.record(r)
        for change in ('size', 'mtime'):
            with self.subTest(change=change):
                before = self.read_packet()['lane_context_hash']
                stat = executable.stat()
                if change == 'size':
                    executable.write_text(executable.read_text() + '\n')
                    os.utime(executable, ns=(stat.st_atime_ns, stat.st_mtime_ns))
                    self.assertEqual(executable.stat().st_mtime_ns, stat.st_mtime_ns)
                else:
                    os.utime(executable, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
                    self.assertEqual(executable.stat().st_size, stat.st_size)
                self.assertIn('lane configuration', self.b(r, 'ready', 'a', '--branch', 'job', ok=False))
                self.record(r)
                self.assertNotEqual(self.read_packet()['lane_context_hash'], before)
                self.assertTrue(all(not row['reused'] for row in self.read_packet()['verify_lanes']))

    def test_missing_tool_refuses_ready_and_fails_verification(self):
        r, executable = self.setup_tool_lane()
        self.record(r)
        executable.unlink()
        self.assertIn('lane configuration', self.b(r, 'ready', 'a', '--branch', 'job', ok=False))
        self.record(r, ok=False)
        packet = self.read_packet()
        self.assertEqual(packet['verify_result'], 127)
        self.assertEqual(packet['verify_lanes'][0]['status'], 'failed')
        self.assertFalse(packet['verify_lanes'][0]['reused'])
        self.assertIn('not ready', self.b(r, 'ready', 'a', '--branch', 'job', ok=False))

    def assert_indirect_tool_path_change_refused(self, kind):
        r, home, wt, lanes = self.setup_lanes()
        stable, first, second = [self.tmp / name for name in ('stable-bin', 'first-bin', 'second-bin')]
        for directory in (stable, first, second):
            directory.mkdir()
        helper = 'fixture-interpreter' if kind == 'shebang' else 'fixture-helper'
        for directory, code in ((first, 0), (second, 19)):
            executable = directory / helper
            executable.write_text('#!/bin/sh\nexit %d\n' % code)
            executable.chmod(0o755)
        if kind == 'shebang':
            executable = stable / 'lane-tool'
            executable.write_text('#!/usr/bin/env fixture-interpreter\n')
            executable.chmod(0o755)
            lanes[0]['command'] = 'lane-tool'
        else:
            self.commit(wt, 'lane_module.py',
                        'import subprocess, sys\nsys.exit(subprocess.call(["fixture-helper"]))\n')
            lanes[0]['command'] = 'python3 -B -m lane_module'
        original_path = self.env['PATH']
        self.env['PATH'] = os.pathsep.join((str(stable), str(first), original_path))
        self.b(r, 'set', 'a', 'verify_lanes=' + json.dumps(lanes))
        self.record(r)
        changed_path = os.pathsep.join((str(stable), str(second), original_path))
        record = json.loads(self.packet().read_text())
        record['ready']['verify_lane_env']['PATH'] = changed_path
        self.packet().write_text(json.dumps(record))
        self.assertIn('lane configuration', self.b(r, 'ready', 'a', '--branch', 'job', ok=False))
        self.env['PATH'] = changed_path
        self.record(r, ok=False)
        packet = self.read_packet()
        self.assertEqual(packet['verify_result'], 19)
        self.assertFalse(packet['verify_lanes'][0]['reused'])

    def test_env_shebang_path_change_refuses_ready_and_reuse(self):
        self.assert_indirect_tool_path_change_refused('shebang')

    def test_module_subprocess_path_change_refuses_ready_and_reuse(self):
        self.assert_indirect_tool_path_change_refused('module')

    def test_missing_or_invalid_recorded_environment_refuses_ready(self):
        r, executable = self.setup_tool_lane()
        self.record(r)
        original = self.packet().read_text()
        for value in (None, {}, [], {'PATH': 7}):
            with self.subTest(value=value):
                record = json.loads(original)
                if value is None:
                    record['ready'].pop('verify_lane_env', None)
                else:
                    record['ready']['verify_lane_env'] = value
                self.packet().write_text(json.dumps(record))
                self.assertIn('recorded verify lane environment',
                              self.b(r, 'ready', 'a', '--branch', 'job', ok=False))

    def test_legacy_or_missing_evidence_context_refuses_ready_and_reruns(self):
        r, home, wt, lanes = self.setup_lanes()
        self.record(r)
        m = core_loader.SourceFileLoader('lane_evidence_version', str(CORE / 'bin/brain')).load_module()
        n = m.Instance(r).node('a')
        w = n['local']['worktrees']['job']
        with mock.patch.dict('os.environ', self.env):
            snap = m.lane_snapshot(n, w, m.verify_env(n, 'job', w), m.verify_inputs(n, w))
        self.assertEqual(self.read_packet()['lane_context_hash'], snap['evidence_context'])
        self.assertNotEqual(snap['context'], snap['evidence_context'])
        for context in (None, snap['context']):
            with self.subTest(context=context):
                record = json.loads(self.packet().read_text())
                if context is None:
                    record['ready'].pop('lane_context_hash')
                else:
                    record['ready']['lane_context_hash'] = context
                self.packet().write_text(json.dumps(record))
                self.assertIn('lane configuration', self.b(r, 'ready', 'a', '--branch', 'job', ok=False))
                self.record(r)
                packet = self.read_packet()
                self.assertEqual(packet['lane_context_hash'], snap['evidence_context'])
                self.assertTrue(all(not row['reused'] for row in packet['verify_lanes']))

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
        m = core_loader.SourceFileLoader('lane_reuse_slot', str(CORE / 'bin/brain')).load_module()
        inst = m.Instance(r)
        n = inst.node('a')
        previous = self.read_packet()
        with mock.patch.dict('os.environ', self.env), mock.patch.object(m.record_ready.__globals__['_m_machine'], 'machine_wait', side_effect=AssertionError('unchanged lanes must not queue')):
            packet, reasons = m.record_ready(inst, n, 'job', 2, previous)
        self.assertEqual(reasons, [])
        self.assertTrue(all(row['reused'] for row in packet['verify_lanes']))

    def test_review_newline_and_piped_inline_command_rerun(self):
        r, home, wt = self.job()
        (wt / 'src').mkdir()
        name = 'src/a\nb.ts'
        self.commit(wt, name, 'first')
        output = self.tmp / 'pipe-output'
        self.b(r, 'set', 'a', 'verify_lanes=unit: echo unit | tee ' + str(output) + ' | src/**')
        self.record(r)
        self.assertEqual(self.read_packet()['verify_lanes'][0]['command'], 'echo unit | tee ' + str(output))
        first = self.read_packet()['verify_lanes'][0]['log']
        self.commit(wt, name, 'second')
        self.record(r)
        row = self.read_packet()['verify_lanes'][0]
        self.assertFalse(row['reused'])
        self.assertNotEqual(row['log'], first)
        # The ordinary pipe-command example also invalidates on a plain src path.
        self.commit(wt, 'src/x.ts', 'third')
        self.record(r)
        self.assertFalse(self.read_packet()['verify_lanes'][0]['reused'])

    def test_review_invalid_maps_and_unmatched_warning(self):
        r, home, wt = self.job()
        cases = [([dict(name='unit', command='true', inputs=[])], 'positive input'),
                 ([dict(name='unit', command='true', inputs=['!calc.py'])], 'positive input'),
                 ([dict(name='night', command='true', inputs=['**'], schedule='nightly')], 'job-scheduled'),
                 ([dict(name='unit', command='true', inputs=['tee out.log | src/**'])], 'whitespace or pipe'),
                 ([dict(name='unit', command='true', inputs=['#comment'])], 'unsupported lane glob')]
        for lanes, reason in cases:
            self.b(r, 'set', 'a', 'verify_lanes=' + json.dumps(lanes))
            self.assertIn(reason, self.record(r, ok=False))
        self.b(r, 'set', 'a', 'verify_lanes=unit: true | missing/**')
        self.assertIn('positive glob matches no tracked files: missing/**', self.record(r))
        self.b(r, 'set', 'a', 'verify_lanes=' + json.dumps([dict(name='unit', command='true', always=True)]))
        self.record(r)
        for name in ('literal file', 'literal|pipe'):
            self.commit(wt, name)
            self.b(r, 'set', 'a', 'verify_lanes=' + json.dumps([dict(name='unit', command='true', inputs=[name])]))
            self.record(r)

    def test_review_display_is_cheap_timeout_and_nested_json_refuse(self):
        r, home, wt, lanes = self.setup_lanes()
        self.commit(wt, 'lanes.json', json.dumps(lanes))
        marker = self.tmp / 'map-called'
        self.b(r, 'set', 'a', 'verify_lanes=', 'verify_lanes_from=touch ' + str(marker) + '; cat lanes.json')
        self.record(r)
        marker.unlink()
        m = core_loader.SourceFileLoader('lane_review_display', str(CORE / 'bin/brain')).load_module()
        inst = m.Instance(r)
        n = inst.node('a')
        owner = m.ready_state.__globals__['_m_lanes']
        with mock.patch.dict('os.environ', self.env), mock.patch.object(owner, 'lane_snapshot', side_effect=AssertionError('display executes no map or repo hash')):
            state = m.ready_state(inst, n, 'job', display=True)
            self.assertTrue(state['ready'])
            self.assertEqual(state['validation'], 'recorded')
            self.assertTrue(json.loads(self.b(r, 'ui', '--json'))['nodes'])
            self.b(r, 'due', 'a')
            self.assertFalse(marker.exists())
        n['fm']['verify_lanes_from'] = 'sleep 30'
        with mock.patch.dict('os.environ', self.env), mock.patch.object(owner, 'VERIFY_LANE_MAP_TIMEOUT', .05):
            import time
            start = time.monotonic()
            state = m.ready_state(inst, n, 'job')
            self.assertLess(time.monotonic() - start, 2)
            self.assertIn('timed out', '; '.join(state['reasons']))
            packet, reasons = m.record_ready(inst, n, 'job', 1, self.read_packet())
            self.assertIsNone(packet)
            self.assertIn('timed out', '; '.join(reasons))
        self.commit(wt, 'lanes.json', '[' * 1500 + ']' * 1500)
        n['fm']['verify_lanes_from'] = 'cat lanes.json'
        with mock.patch.dict('os.environ', self.env):
            self.assertIn('verify lanes invalid', '; '.join(m.ready_state(inst, n, 'job')['reasons']))
            packet, reasons = m.record_ready(inst, n, 'job', 1, self.read_packet())
            self.assertIsNone(packet)
            self.assertIn('verify lanes invalid', '; '.join(reasons))
        n['fm']['verify_lanes_from'] = ''
        n['fm']['verify_lanes'] = json.dumps(lanes)
        with mock.patch.dict('os.environ', self.env):
            snap = owner.lane_snapshot(n, n['local']['worktrees']['job'], m.verify_env(n, 'job', n['local']['worktrees']['job']), m.verify_inputs(n, n['local']['worktrees']['job']))
            with mock.patch.object(owner, 'lane_snapshot', side_effect=[snap, RecursionError('nested map')]):
                packet, reasons = m.record_ready(inst, n, 'job', 1)
                self.assertIsNone(packet)
                self.assertIn('became unreadable', '; '.join(reasons))

    def test_review_symlink_hashes_link_and_pipeline_tool_identity(self):
        r, home, wt, lanes = self.setup_lanes()
        target = self.tmp / 'external-data'
        target.write_text('first')
        (wt / 'fixture-link').symlink_to(target)
        self.git('add', 'fixture-link', cwd=wt)
        self.git('commit', '-qm', 'symlink fixture', cwd=wt)
        m = core_loader.SourceFileLoader('lane_review_links', str(CORE / 'bin/brain')).load_module()
        first = m.tracked_files({'path': str(wt)})['fixture-link']
        target.write_text('second')
        self.assertEqual(first, m.tracked_files({'path': str(wt)})['fixture-link'])
        target.unlink()
        self.assertEqual(first, m.tracked_files({'path': str(wt)})['fixture-link'])
        tools = self.tmp / 'tools'
        tools.mkdir()
        executable = tools / 'fixture-runner'
        executable.write_text('#!/bin/sh\ntrue\n')
        executable.chmod(0o755)
        self.env['PATH'] = str(tools) + ':' + self.env['PATH']
        lanes[0]['command'] = 'cd . && fixture-runner'
        self.b(r, 'set', 'a', 'verify_lanes=' + json.dumps(lanes))
        self.record(r)
        executable.write_text('#!/bin/sh\necho new-tool\n')
        self.record(r)
        self.assertTrue(all(not row['reused'] for row in self.read_packet()['verify_lanes']))

    def test_review_input_map_retention_with_per_lane_logs(self):
        r, home, wt, lanes = self.setup_lanes()
        self.record(r)
        m = core_loader.SourceFileLoader('lane_maps_retention', str(CORE / 'bin/brain')).load_module()
        inst = m.Instance(r)
        n = inst.node('a')
        previous = self.read_packet()
        refs = []
        with mock.patch.dict('os.environ', self.env), mock.patch.object(m.record_ready.__globals__['_m_machine'], 'machine_wait', wraps=m.machine_wait), mock.patch.object(m.run_verify_stream.__globals__['_m_common'], 'TOUCHED', set()):
            m.VERIFY_LOG_KEEP = 2
            for i in range(4):
                previous, reasons = m.record_ready(inst, n, 'job', 1, previous, reverify=True)
                self.assertEqual(reasons, [])
                self.assertIsInstance(previous['input_files_ref'], str)
                self.assertEqual(m.read_verify_input_files(n, previous), {})
                refs.append(self.packet().parent / previous['input_files_ref'])
        self.assertEqual([p.exists() for p in refs], [False, False, True, True])
        self.assertEqual(len(list(self.packet().parent.glob('*-inputs-*.map'))), 2)


import unittest


class LaneGlobReviewTable(unittest.TestCase):
    def test_known_minimatch_results_and_conservative_dot_policy(self):
        m = core_loader.SourceFileLoader('lane_review_glob_table', str(CORE / 'bin/brain')).load_module()
        # Known minimatch outcomes for the supported syntax (positive dot:true;
        # exclusions dot:false). No npm runtime is needed by the Python core.
        cases = [('src/a\nb.ts', ['src/**'], True), ('src/a\nb.ts', ['**/*'], True),
                 ('a/x\ny/b', ['a/**/b'], True), ('a/b', ['a/**/b'], True),
                 ('b', ['**/b'], True), ('src/a.ts', ['src/?.ts'], True),
                 ('src/ab.ts', ['src/?.ts'], False), ('src/\n.ts', ['src/?.ts'], True),
                 ('src/nested/a.ts', ['src/*.ts'], False),
                 ('.hidden/a.ts', ['**/*.ts'], True),
                 ('config/.babelrc', ['config/.babelrc', '!config/*'], True),
                 ('.hidden/a.ts', ['**/*.ts', '!**/*.ts'], True),
                 ('.hidden/a.ts', ['**/*.ts', '!.hidden/**'], False),
                 ('config/.babelrc', ['**', '!config/.*'], False),
                 ('src/a.ts', ['!src/**', '**/*.ts'], False)]
        for path, patterns, expected in cases:
            with self.subTest(path=path, patterns=patterns):
                self.assertEqual(bool(m.matches(path, patterns)), expected)
        for pattern in ('a/**b', '{}', '[]', '!!x', '#comment'):
            with self.subTest(pattern=pattern), self.assertRaises(ValueError):
                m.glob_regex(pattern)
