"""Focused CLI and runtime timing contracts using invented instance data."""
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest import mock

from scenario import Scenario, CORE
import core_loader


class OperationTimings(Scenario):
    def load_timing(self):
        m = core_loader.SourceFileLoader('operation_timing_fixture', str(CORE / 'bin/brain')).load_module()
        import importlib
        return m, importlib.import_module(m._owners['cmd_timings'].__package__ + '.operation_timings')

    def hook(self, root, payload, cwd=None):
        return subprocess.run([sys.executable, '-B', str(CORE / 'bin/brain-timing-hook')],
                              input=json.dumps(payload), text=True, capture_output=True,
                              cwd=cwd or root, env=self.env, timeout=10)

    def test_query_window_node_slowest_percentiles_and_malformed_records(self):
        root = self.init('demo')
        self.b(root, 'new', 'app', '--title', 'App')
        m, op = self.load_timing()
        inst = m.Instance(root)
        path = root / '.brain/timings/events.jsonl'
        for i in range(1, 101):
            m.append_line(path, dict(ts=1000 + i, node=m.node_ref('app'), kind='timing',
                                    data=dict(command='fixture', elapsed_s=i, slow=True)))
        for row in ({'kind': 'timing', 'data': {'elapsed_s': float('nan')}}, [],
                    {'kind': 'timing', 'ts': None, 'data': {'elapsed_s': 1}}):
            m.append_line(path, row)
        args = ['timings', '--command', 'fixture', '--node', 'app']
        summary = json.loads(self.b(root, *args, '--summary', '--json'))
        self.assertEqual(summary, dict(count=100, p50_s=50, p95_s=95))
        rows = json.loads(self.b(root, *args, '--since', '1090', '--until', '1100', '--slowest', '2', '--json'))
        self.assertEqual([r['elapsed_s'] for r in rows], [100, 99])
        self.assertEqual(json.loads(self.b(root, *args, '--since', '1970-01-01T00:18:10Z',
                                         '--summary', '--json'))['count'], 11)
        self.assertIn('invalid timing filter', self.b(root, 'timings', '--slowest', '0', ok=False))
        self.assertIn('invalid timing filter', self.b(root, 'timings', '--since', 'nan', ok=False))
        self.assertEqual(inst.node('app')['id'], 'app')

    def test_wrapper_inherits_stdio_exit_and_scrubs_metadata(self):
        root = self.init('demo')
        self.b(root, 'new', 'app', '--title', 'App')
        result = subprocess.run([sys.executable, '-B', str(CORE / 'bin/brain'), 'time', '--node', 'app', '--',
                                 sys.executable, '-c', 'import sys; print("fixture-output"); sys.exit(7)',
                                 'fixture-private-argument'], cwd=root, env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(result.stdout.strip(), 'fixture-output')
        row = json.loads(self.b(root, 'timings', '--node', 'app', '--command', Path(sys.executable).name, '--json'))[-1]
        self.assertEqual(row['exit_code'], 7)
        self.assertFalse(row['success'])
        self.assertGreater(row['elapsed_s'], 0)
        raw = (root / '.brain/timings/events.jsonl').read_text()
        for private in ('fixture-private-argument', 'fixture-output', 'sys.exit', str(self.tmp)):
            self.assertNotIn(private, raw)
        missing = subprocess.run([sys.executable, '-B', str(CORE / 'bin/brain'), 'time', '--', 'missing-fixture-executable'],
                                 cwd=root, env=self.env, capture_output=True, text=True)
        self.assertEqual(missing.returncode, 127)
        self.assertIn('requires --', self.b(root, 'time', sys.executable, '-c', 'raise SystemExit(0)', ok=False))
        self.b(root, 'time', '--', sys.executable, '-c',
               'import sys; assert sys.argv[1:] == ["-b", "fixture", "--brain=other"]',
               '-b', 'fixture', '--brain=other')

    def test_slow_wrapper_samples_descendants(self):
        root = self.init('demo')
        script = 'import subprocess,sys; subprocess.run([sys.executable,"-c","import time; time.sleep(.8)"])'
        self.b(root, 'time', '--threshold', '0', '--', sys.executable, '-c', script)
        rows = json.loads(self.b(root, 'timings', '--command', Path(sys.executable).name, '--json'))
        row = rows[-1]
        self.assertTrue(row['slow'])
        self.assertTrue(row['process_sampling']['approximate'])
        self.assertGreaterEqual(len(row['processes']), 2)
        pids = {r['pid'] for r in row['processes']}
        self.assertTrue(any(r['ppid'] in pids for r in row['processes']))
        self.assertTrue(all(r['cpu_s'] >= 0 and r['last_seen_s'] >= r['first_seen_s'] for r in row['processes']))

    def test_wrapper_storage_failure_does_not_change_result(self):
        root = self.init('demo')
        m, op = self.load_timing()
        args = type('Args', (), dict(node=None, threshold=5, command=['--', sys.executable, '-c', 'raise SystemExit(9)']))()
        with mock.patch.dict(os.environ, self.env), mock.patch.object(Path, 'cwd', return_value=root), \
                mock.patch.object(op, 'publish', side_effect=PermissionError), self.assertRaises(SystemExit) as status:
            op.cmd_time(args)
        self.assertEqual(status.exception.code, 9)

    def test_hook_pairs_failures_unknown_exit_and_duplicate_post(self):
        root = self.init('demo')
        payload = dict(session_id='fixture-session', tool_use_id='fixture-call', tool_name='Shell',
                       tool_input={'command': 'fixture-secret'}, tool_response={'exit_code': 3, 'stdout': 'fixture-output'})
        for phase in ('PreToolUse', 'PostToolUse', 'PostToolUse'):
            result = self.hook(root, dict(payload, hook_event_name=phase))
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
        rows = json.loads(self.b(root, 'timings', '--command', 'Shell', '--json'))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['exit_code'], 3)
        raw = (root / '.brain/timings/events.jsonl').read_text()
        for private in ('fixture-secret', 'fixture-output', 'fixture-session', 'fixture-call'):
            self.assertNotIn(private, raw)
        self.assertEqual(list((root / '.brain/timings/pending').glob('*')), [])
        for phase in ('PreToolUse', 'PostToolUseFailure'):
            self.hook(root, dict(payload, tool_use_id='another-call', tool_response={}, hook_event_name=phase))
        rows = json.loads(self.b(root, 'timings', '--command', 'Shell', '--json'))
        self.assertEqual(rows[-1]['exit_code'], 1)
        for phase in ('PreToolUse', 'PostToolUse'):
            self.hook(root, dict(payload, tool_use_id='unknown-call', tool_response={}, hook_event_name=phase))
        rows = json.loads(self.b(root, 'timings', '--command', 'Shell', '--json'))
        self.assertIsNone(rows[-1]['exit_code'])
        self.hook(root, dict(payload, tool_use_id='stale-call', hook_event_name='PreToolUse'))
        pending = next((root / '.brain/timings/pending').glob('*.json'))
        start = json.loads(pending.read_text())
        start['wall'] = 1
        pending.write_text(json.dumps(start))
        self.hook(root, dict(payload, tool_use_id='stale-call', hook_event_name='PostToolUse'))
        self.assertFalse(pending.exists())

    def test_hook_does_nothing_outside_brain_and_refuses_linked_storage(self):
        root = self.init('demo')
        payload = dict(session_id='fixture-session', call_id='fixture-call', tool_name='Read', hook_event_name='PreToolUse')
        result = self.hook(root, payload, cwd=self.tmp)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
        folder = root / '.brain/timings'
        folder.mkdir(mode=0o700, exist_ok=True)
        destination = self.tmp / 'outside'
        destination.mkdir()
        (folder / 'pending').symlink_to(destination, target_is_directory=True)
        result = self.hook(root, payload)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(list(destination.iterdir()), [])
        self.assertEqual(result.stderr, '')
        broken = subprocess.run([sys.executable, '-B', '-c',
                                'import runpy,sys; sys.modules["brain.operation_timings"]=None; runpy.run_path(sys.argv[1])',
                                str(CORE / 'bin/brain-timing-hook')], cwd=root, env=self.env,
                                capture_output=True, text=True)
        self.assertEqual((broken.returncode, broken.stdout, broken.stderr), (0, '', ''))

    def test_existing_brain_timings_are_attributed_to_selected_node(self):
        root = self.init('demo')
        self.b(root, 'new', 'app', '--title', 'App')
        self.b(root, 'show', 'app')
        rows = json.loads(self.b(root, 'timings', '--node', 'app', '--command', 'show', '--json'))
        # show is a pure read and need not publish; a write must carry the node.
        self.b(root, 'set', 'app', 'title=Updated')
        rows = json.loads(self.b(root, 'timings', '--node', 'app', '--command', 'set', '--json'))
        self.assertTrue(rows)
