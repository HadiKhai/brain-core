"""Native direct-full receipts can satisfy readiness once, without rerunning."""
import json
import shlex
import sys
from pathlib import Path

import test_train
from scenario import Scenario


class FullGateReceipt(Scenario):
    project = test_train.MergeTrain.project
    job = test_train.MergeTrain.job

    def test_direct_full_pass_is_adopted_without_another_execution(self):
        root, _ = self.project()
        self.job('one')
        self.b(root, 'full-run', 'a', '--branch', 'one', '--reviews', '1')
        receipt = next((root / 'nodes/a/full-gates').glob('*.json'))
        before = len(list((root / 'nodes/a/job-handbacks').glob('*-verify-*.log')))
        self.b(root, 'handback', 'a', '--branch', 'one', '--state', 'done', '--reviews', '1',
               '--summary', 'Adopted full gate', '--full-receipt', receipt.stem)
        self.assertEqual(len(list((root / 'nodes/a/job-handbacks').glob('*-verify-*.log'))), before)
        signals = [json.loads(p.read_text()) for p in (root / 'nodes/a/job-handbacks').glob('*.json')]
        self.assertTrue(any(s.get('ready', {}).get('external_full_receipt') == receipt.stem for s in signals))

    def test_stale_full_receipt_refuses_without_a_second_gate(self):
        root, _ = self.project()
        self.job('one')
        self.b(root, 'full-run', 'a', '--branch', 'one', '--reviews', '1')
        receipt = next((root / 'nodes/a/full-gates').glob('*.json'))
        self.b(root, 'set', 'a', 'verify=false')
        out = self.b(root, 'handback', 'a', '--branch', 'one', '--state', 'done', '--reviews', '1',
                     '--summary', 'Stale gate', '--full-receipt', receipt.stem, ok=False)
        self.assertIn('full receipt', out)

    def test_invalid_handback_options_do_not_start_attempt_or_verify(self):
        root, _ = self.project()
        self.job('one')
        before = {p.name: p.read_bytes() for p in (root / 'nodes/a/job-attempts').glob('*.json')}
        self.b(root, 'handback', 'a', '--branch', 'one', '--state', 'done', '--summary', 'Invalid request',
               '--reviews', '0', '--reverify', ok=False)
        self.assertEqual({p.name: p.read_bytes() for p in (root / 'nodes/a/job-attempts').glob('*.json')}, before)


class IsolatedDisposition(Scenario):
    fixture_init_in_process = True
    project = test_train.MergeTrain.project

    def prepare(self, isolate=True, compatibility=False, persistent=False, linked=False):
        from scenario import CORE
        collector = 'tests/test_brain.py' if compatibility else 'tests/test_sample.py'
        root, home = self.project(shlex.join([
            sys.executable, '-B', '-m', 'pytest', '-q', '-p', 'no:cacheprovider', collector]))
        (home / 'tests').mkdir()
        (home / 'tests/conftest.py').write_text((CORE / 'tests/conftest.py').read_text())
        self.source = ('import sys\nimport unittest\n\nclass Flakes(unittest.TestCase):\n'
                       '    def test_one(self):\n        self.assertTrue(any("::" in a for a in sys.argv))\n'
                       '    def test_two(self):\n        self.assertTrue(any("::" in a for a in sys.argv))\n')
        if persistent:
            self.source = self.source.replace('any("::" in a for a in sys.argv)', 'False', 1)
        (home / 'tests/test_sample.py').write_text(self.source)
        if compatibility:
            (home / 'tests/test_brain.py').write_text('from test_sample import Flakes\n')
        self.git('add', 'tests', cwd=home)
        self.git('commit', '-qm', 'native test fixture', cwd=home)
        if linked:
            self.b(root, 'task', 'add', 'a', 'TASK-BUILD', '--title', 'Native build fixture', '--status', 'ready')
        self.b(root, 'wt', 'add', 'a', 'one', *(['--task-id', 'TASK-BUILD'] if linked else []))
        self.wt = self.tmp / 'app-work/worktrees/one'
        self.folder = root / 'nodes/a/full-gates'
        self.b(root, 'full-run', 'a', '--branch', 'one', '--reviews', '1', ok=False)
        self.red = next(self.folder.glob('*.json'))
        self.red_bytes = self.red.read_bytes()
        self.red_head = self.git('rev-parse', 'one', cwd=home).strip()
        self.tests = [collector + '::Flakes::test_' + name for name in ('one', 'two')]
        self.rows = []
        if isolate:
            for test in self.tests:
                before = set(self.folder.glob('*.json'))
                self.b(root, 'full-isolate', 'a', '--branch', 'one', '--full-receipt', self.red.stem, '--test', test)
                path, = set(self.folder.glob('*.json')) - before
                self.rows.append(dict(test=test, isolated_receipt=path.stem, reason='quarantined: isolated pass'))
        return root, home

    def quarantine(self, text=None, extra=None):
        text = text or self.source.replace('    def test_',
            '    @unittest.skip("quarantined: isolated pass")\n    def test_')
        (self.wt / 'tests/test_sample.py').write_text(text)
        for name, value in (extra or {}).items():
            (self.wt / name).write_text(value)
        self.git('add', '-A', cwd=self.wt)
        self.git('commit', '-qm', 'skip-only quarantine', cwd=self.wt)

    def disposition(self, rows=None, ok=True):
        request = self.tmp / 'request.json'
        request.write_text(json.dumps(dict(full_receipt=self.red.stem,
                                           quarantines=self.rows if rows is None else rows)))
        before = set(self.folder.glob('*.json'))
        out = self.b(self.root, 'full-quarantine', 'a', '--branch', 'one', '--file', str(request), ok=ok)
        if ok:
            path, = set(self.folder.glob('*.json')) - before
            return path
        self.assertEqual(set(self.folder.glob('*.json')), before)
        return out

    def test_red_receipt_and_isolated_passes_adopt_only_the_quarantine_head(self):
        root, home = self.prepare(compatibility=True, linked=True)
        red = json.loads(self.red_bytes)
        self.assertEqual(red['packet']['verify_result'], 1)
        self.b(root, 'handback', 'a', '--branch', 'one', '--state', 'done', '--reviews', '1',
               '--summary', 'Red alone refuses', '--full-receipt', self.red.stem, ok=False)
        self.quarantine()
        disposition = self.disposition()
        repairs = sorted(p for p in (root / 'nodes/a/tasks').glob('*.md') if p.stem != 'TASK-BUILD')
        self.b(root, 'task', 'set', 'a', repairs[0].stem, '--status', 'ready')
        tracking = {p.name: p.read_bytes() for p in repairs}
        self.disposition()
        self.assertEqual({p.name: p.read_bytes() for p in repairs}, tracking)
        before = len(list((root / 'nodes/a/job-handbacks').glob('*-verify-*.log')))
        self.b(root, 'handback', 'a', '--branch', 'one', '--state', 'done', '--reviews', '1',
               '--summary', 'Quarantined landing evidence', '--full-receipt', disposition.stem)
        self.assertEqual('ready', self.b(root, 'ready', 'a', '--branch', 'one').strip())
        packet = next(json.loads(p.read_text())['ready'] for p in (root / 'nodes/a/job-handbacks').glob('*.json')
                      if json.loads(p.read_text()).get('ready'))
        self.assertEqual(packet['verify_result'], 1)
        self.assertEqual(packet['verified_head'], self.red_head)
        self.assertNotEqual(packet['head'], self.red_head)
        self.assertTrue(packet['full_disposition'])
        self.assertEqual(self.red.read_bytes(), self.red_bytes)
        self.assertEqual(len(list((root / 'nodes/a/job-handbacks').glob('*-verify-*.log'))), before)
        self.assertEqual(len(list((root / 'nodes/a/tasks').glob('*.md'))), 3)
        self.assertEqual(len(repairs), 2)
        self.assertTrue(all('isolated pass' in p.read_text() for p in repairs))
        build, = json.loads(self.b(root, 'task', 'show', 'a', 'TASK-BUILD', '--json'))
        self.assertEqual(build['status'], 'testing')
        self.assertEqual(build['strikes'], 0)
        self.assertTrue(any('original full verification exit 1' in item for item in build['evidence']))
        # Later source movement cannot reuse the disposition, even through ordinary readiness.
        (self.wt / 'later').write_text('later')
        self.git('add', 'later', cwd=self.wt)
        self.git('commit', '-qm', 'later change', cwd=self.wt)
        self.b(root, 'ready', 'a', '--branch', 'one', ok=False)
        self.b(root, 'handback', 'a', '--branch', 'one', '--state', 'done', '--reviews', '1',
               '--summary', 'Wrong head', '--full-receipt', disposition.stem, ok=False)

    def test_missing_failure_rerun_and_non_skip_changes_refuse(self):
        self.prepare()
        self.quarantine(extra={'unrelated': 'forbidden'})
        self.assertIn('ordinary modified test files', self.disposition(ok=False))
        self.git('reset', '--hard', self.red_head, cwd=self.wt)
        self.quarantine(self.source.replace('    def test_',
            '    @unittest.skip("quarantined: isolated pass")\n    def test_').replace('any("::" in a for a in sys.argv)', 'True'))
        self.assertIn('assertions or other code', self.disposition(ok=False))
        self.git('reset', '--hard', self.red_head, cwd=self.wt)
        self.quarantine()
        self.assertIn('every red-gate failure', self.disposition(self.rows[:1], ok=False))
        bad = [dict(row) for row in self.rows]
        bad[1]['isolated_receipt'] = bad[0]['isolated_receipt']
        self.assertIn('another head', self.disposition(bad, ok=False))
        self.assertEqual(self.red.read_bytes(), self.red_bytes)
        self.assertFalse((self.root / 'nodes/a/tasks').exists())

    def test_failed_changed_and_multi_test_isolated_receipts_refuse(self):
        self.prepare()
        self.quarantine()
        isolated = self.folder / (self.rows[0]['isolated_receipt'] + '.json')
        original = isolated.read_bytes()
        for mutation in (dict(result=1), dict(head='0' * 40), dict(test=self.tests[1]),
                         dict(inputs='different'), dict(job_key='different'), dict(verify_lane_env={})):
            with self.subTest(mutation=mutation):
                data = json.loads(original)
                data.update(mutation)
                isolated.write_text(json.dumps(data))
                self.assertIn('isolated receipt', self.disposition(ok=False))
        isolated.write_bytes(original)
        data = json.loads(original)
        log = Path(data['log'])
        log_bytes = log.read_bytes()
        log.write_text(log.read_text() + 'changed')
        self.assertIn('isolated receipt', self.disposition(ok=False))
        log.write_bytes(log_bytes)
        # A green process without one complete passing test is not isolated evidence.
        import hashlib
        log.write_text('test-result: ' + json.dumps(dict(event='session')) + '\n' +
                       'test-result: ' + json.dumps(dict(event='session_finish', exitstatus=0,
                                                        collected=0, failed=0, incomplete=[])) + '\n')
        data['log_sha256'] = hashlib.sha256(log.read_bytes()).hexdigest()
        isolated.write_text(json.dumps(data))
        self.assertIn('inventory', self.disposition(ok=False))
        self.assertEqual(self.red.read_bytes(), self.red_bytes)

    def test_ready_revalidates_hash_bound_red_receipt_and_tracking_task(self):
        root, _ = self.prepare()
        self.quarantine()
        disposition = self.disposition()
        self.b(root, 'handback', 'a', '--branch', 'one', '--state', 'done', '--reviews', '1',
               '--summary', 'Quarantined', '--full-receipt', disposition.stem)
        import os
        from unittest import mock
        import core_loader
        from scenario import CORE
        m = core_loader.SourceFileLoader('disposition_display_cache', str(CORE / 'bin/brain')).load_module()
        with mock.patch.dict(os.environ, self.env, clear=True):
            inst = m.Instance(root)
            node = inst.node('a')
            with m.display_cache({}, {('a', 'one'): 'unchanged dashboard inputs'}):
                self.assertTrue(m.ready_state(inst, node, 'one', display=True)['ready'])
                self.red.write_text(self.red.read_text() + '\n')
                self.assertFalse(m.ready_state(inst, node, 'one', display=True)['ready'])
                self.red.write_bytes(self.red_bytes)
                self.assertTrue(m.ready_state(inst, node, 'one', display=True)['ready'])
        self.red.write_text(self.red.read_text() + '\n')
        self.b(root, 'ready', 'a', '--branch', 'one', ok=False)
        self.red.write_bytes(self.red_bytes)
        self.assertEqual('ready', self.b(root, 'ready', 'a', '--branch', 'one').strip())
        task = next((root / 'nodes/a/tasks').glob('*.md'))
        task.unlink()
        self.b(root, 'ready', 'a', '--branch', 'one', ok=False)

    def test_execution_environment_drift_refuses_publication_adoption_and_readiness(self):
        root, _ = self.prepare()
        self.quarantine()
        original = self.env.get('CFLAGS')
        self.env['CFLAGS'] = 'quarantine-fixture-context-drift'
        self.assertIn('execution environment changed', self.disposition(ok=False))
        if original is None:
            self.env.pop('CFLAGS')
        else:
            self.env['CFLAGS'] = original
        receipt = self.disposition()
        self.env['CFLAGS'] = 'quarantine-fixture-context-drift'
        self.b(root, 'handback', 'a', '--branch', 'one', '--state', 'done', '--reviews', '1',
               '--summary', 'Changed execution context', '--full-receipt', receipt.stem, ok=False)
        if original is None:
            self.env.pop('CFLAGS')
        else:
            self.env['CFLAGS'] = original
        self.b(root, 'handback', 'a', '--branch', 'one', '--state', 'done', '--reviews', '1',
               '--summary', 'Original execution context', '--full-receipt', receipt.stem)
        self.env['CFLAGS'] = 'quarantine-fixture-context-drift'
        self.b(root, 'ready', 'a', '--branch', 'one', ok=False)
        if original is None:
            self.env.pop('CFLAGS')
        else:
            self.env['CFLAGS'] = original
        self.assertEqual('ready', self.b(root, 'ready', 'a', '--branch', 'one').strip())

    def test_isolated_selector_and_head_refuse_before_execution(self):
        root, _ = self.prepare(isolate=False)
        before = set(self.folder.glob('*'))
        self.b(root, 'full-isolate', 'a', '--branch', 'one', '--full-receipt', self.red.stem,
               '--test', 'tests/test_sample.py::Flakes::not_a_failure', ok=False)
        self.assertEqual(set(self.folder.glob('*')), before)
        (self.wt / 'new').write_text('moved')
        self.git('add', 'new', cwd=self.wt)
        self.git('commit', '-qm', 'move head', cwd=self.wt)
        self.b(root, 'full-isolate', 'a', '--branch', 'one', '--full-receipt', self.red.stem,
               '--test', self.tests[0], ok=False)
        self.assertEqual(set(self.folder.glob('*')), before)

    def test_actual_failed_isolated_execution_stays_red_and_cannot_qualify(self):
        root, _ = self.prepare(isolate=False, persistent=True)
        before = set(self.folder.glob('*.json'))
        self.b(root, 'full-isolate', 'a', '--branch', 'one', '--full-receipt', self.red.stem,
               '--test', self.tests[0], ok=False)
        isolated, = set(self.folder.glob('*.json')) - before
        self.assertEqual(json.loads(isolated.read_text())['result'], 1)
        self.rows = [dict(test=test, isolated_receipt=isolated.stem, reason='quarantined: isolated pass')
                     for test in self.tests]
        self.quarantine()
        self.assertIn('isolated receipt', self.disposition(ok=False))
        self.assertEqual(self.red.read_bytes(), self.red_bytes)

    def test_configuration_change_during_isolated_execution_refuses_publication(self):
        import argparse
        import contextlib
        import io
        import os
        from unittest import mock
        import core_loader
        from scenario import CORE
        root, _ = self.prepare(isolate=False)
        m = core_loader.SourceFileLoader('isolate_configuration', str(CORE / 'bin/brain')).load_module()
        inst = m.Instance(root)
        before = set(self.folder.glob('*'))
        def execute(*args):
            node = root / 'nodes/a/node.md'
            text = node.read_text().replace('verify:', 'verify_isolation_changed: true\nverify:', 1)
            node.write_text(text)
            return 0, json.loads(self.red.read_text())['packet']['verify_log']
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(m, 'load', return_value=inst), \
                mock.patch.object(m, 'run_verify', side_effect=execute), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            m.cmd_full_isolate(argparse.Namespace(id='a', branch='one', full_receipt=self.red.stem, test=self.tests[0]))
        self.assertEqual(set(self.folder.glob('*')), before)

    def test_initial_snapshot_refuses_rebinding_and_configuration_drift_before_execution(self):
        import argparse
        import os
        from unittest import mock
        import core_loader
        from scenario import CORE
        root, home = self.prepare(isolate=False)
        m = core_loader.SourceFileLoader('initial_isolate_binding', str(CORE / 'bin/brain')).load_module()
        inst = m.Instance(root)
        original_node = inst.node
        node_path, local_path = root / 'nodes/a/node.md', root / '.brain/local.json'
        node_bytes, local_bytes = node_path.read_bytes(), local_path.read_bytes()
        rebound = self.tmp / 'app-work/worktrees/rebound'
        self.git('worktree', 'add', '--force', str(rebound), 'one', cwd=home)
        before = set(self.folder.glob('*'))
        for drift in ('configuration', 'checkout'):
            for action in ('isolate', 'quarantine'):
                def stale_node(ident):
                    result = original_node(ident)
                    if drift == 'configuration':
                        node_path.write_text(node_bytes.decode().replace(
                            'verify:', 'verify_isolation_changed: true\nverify:', 1))
                    else:
                        local = json.loads(local_bytes)
                        local['nodes']['a']['worktrees']['one']['path'] = str(rebound)
                        local_path.write_text(json.dumps(local))
                        (self.wt / 'tests/test_sample.py').write_text(self.source.replace(
                            'any("::" in a for a in sys.argv)', 'True'))
                    return result
                try:
                    with self.subTest(drift=drift, action=action), \
                            mock.patch.dict(os.environ, self.env, clear=True), \
                            mock.patch.object(inst, 'node', side_effect=stale_node), \
                            mock.patch.object(m, 'load', return_value=inst), \
                            mock.patch.object(m, 'run_verify') as execute, \
                            self.assertRaises(SystemExit) as refusal:
                        if action == 'isolate':
                            m.cmd_full_isolate(argparse.Namespace(id='a', branch='one',
                                full_receipt=self.red.stem, test=self.tests[0]))
                        else:
                            m.cmd_full_quarantine(argparse.Namespace(id='a', branch='one',
                                file=str(self.tmp / 'unread-request.json')))
                    execute.assert_not_called()
                    self.assertIn('configuration or checkout binding changed', str(refusal.exception))
                    self.assertEqual(set(self.folder.glob('*')), before)
                finally:
                    node_path.write_bytes(node_bytes)
                    local_path.write_bytes(local_bytes)
                    (self.wt / 'tests/test_sample.py').write_text(self.source)


class DispositionIntegrity(Scenario):
    def module(self):
        import core_loader
        from scenario import CORE
        return core_loader.SourceFileLoader('disposition_integrity', str(CORE / 'bin/brain')).load_module()

    def test_native_inventory_refuses_incomplete_duplicate_and_inconsistent_results(self):
        import copy
        import hashlib
        m = self.module()
        path = self.tmp / 'inventory.log'
        test = 'tests/test_example.py::Example::test_one'
        phases = [dict(event='phase', nodeid=test, when=when, outcome='passed', source_path='tests/test_example.py')
                  for when in ('setup', 'call', 'teardown')]
        complete = dict(event='completed', nodeid=test, outcome='passed', source_path='tests/test_example.py')
        finish = dict(event='session_finish', collected=1, failed=0, exitstatus=0, incomplete=[])
        rows = [dict(event='session'), *phases, complete, finish]
        def write(values):
            path.write_text('\n'.join('test-result: ' + json.dumps(row) for row in values) + '\n')
            return hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(set(m.native_inventory(path, 0, write(rows))), {test})
        for bad in (rows[:-1], [*rows[:-1], complete, finish],
                    [rows[0], *phases[:2], complete, finish],
                    [rows[0], *phases, complete, {**finish, 'incomplete': [test]}],
                    [rows[0], *phases, complete, {**finish, 'collected': 2}],
                    [rows[0], *phases, {**complete, 'outcome': 'failed'}, finish]):
            with self.subTest(rows=bad), self.assertRaises(ValueError):
                m.native_inventory(path, 0, write(bad))
        inconsistent = copy.deepcopy(rows)
        inconsistent[2]['outcome'] = 'failed'
        with self.assertRaises(ValueError):
            m.native_inventory(path, 0, write(inconsistent))
        digest = write(rows)
        path.write_text(path.read_text() + 'tampered')
        with self.assertRaises(ValueError):
            m.native_inventory(path, 0, digest)

    def test_isolated_command_removes_parallelism_and_refuses_unknown_shell_or_selectors(self):
        m = self.module()
        test = 'tests/test_example.py::Example::test_one'
        command = m.isolated_command('python3 -m pytest -q -p no:cacheprovider -n 16 '
                                     '--max-worker-restart=0 --durations=30 tests/test_brain.py', test)
        self.assertEqual(command, 'python3 -m pytest -q -p no:cacheprovider ' + test)
        for command in ('true', 'python3 -m pytest tests/test_brain.py && true',
                        'python3 -m pytest -k one tests/test_brain.py',
                        'python3 -m pytest -p unknown tests/test_brain.py'):
            with self.subTest(command=command), self.assertRaises(ValueError):
                m.isolated_command(command, test)

    def test_skip_diff_refuses_modes_missing_reasons_other_methods_and_non_child_commits(self):
        m = self.module()
        (self.tmp / 'app/tests').mkdir(parents=True)
        home = self.repo(self.tmp / 'app', {'tests/test_example.py':
            'import unittest\nclass Example(unittest.TestCase):\n def test_one(self): assert True\n'
            ' def test_two(self): assert True\n'})
        source = home / 'tests/test_example.py'
        original = source.read_text()
        red = self.git('rev-parse', 'HEAD', cwd=home).strip()
        test = 'tests/test_example.py::Example::test_one'
        failures = {test: dict(source_test=test)}
        rows = [dict(test=test, reason='repair timing')]
        valid = original.replace(' def test_one', ' @unittest.skip("repair timing")\n def test_one')
        for text, mode, reason in ((valid, 0o755, 'mode'),
                                    ('# coding: review_invalid_codec\n' + valid, 0o644, 'encoding'),
                                    ('# unrelated comment\n' + valid, 0o644, 'comment'),
                                    (valid.replace('assert True', 'assert  True'), 0o644, 'whitespace'),
                                    (valid.replace('repair timing', ''), 0o644, 'reason'),
                                    (original.replace(' def test_two', ' @unittest.skip("repair timing")\n def test_two'),
                                     0o644, 'method')):
            self.git('reset', '--hard', red, cwd=home)
            source.write_text(text)
            source.chmod(mode)
            self.git('add', '-A', cwd=home)
            self.git('commit', '-qm', 'invalid ' + reason, cwd=home)
            head = self.git('rev-parse', 'HEAD', cwd=home).strip()
            with self.subTest(reason=reason), self.assertRaises(ValueError):
                m.skip_only(dict(home_path=home), red, head, rows, failures)
        self.git('reset', '--hard', red, cwd=home)
        source.write_text(valid)
        self.git('commit', '-qam', 'valid quarantine', cwd=home)
        head = self.git('rev-parse', 'HEAD', cwd=home).strip()
        m.skip_only(dict(home_path=home), red, head, rows, failures)
        self.git('commit', '--allow-empty', '-qm', 'extra commit', cwd=home)
        with self.assertRaises(ValueError):
            m.skip_only(dict(home_path=home), red, self.git('rev-parse', 'HEAD', cwd=home).strip(), rows, failures)

    def test_skip_import_must_be_bound_before_the_class_and_not_shadowed(self):
        m = self.module()
        (self.tmp / 'app/tests').mkdir(parents=True)
        original = 'from unittest import TestCase\nclass Example(TestCase):\n def test_one(self): assert True\n'
        home = self.repo(self.tmp / 'app', {'tests/test_example.py': original})
        source = home / 'tests/test_example.py'
        red = self.git('rev-parse', 'HEAD', cwd=home).strip()
        test = 'tests/test_example.py::Example::test_one'
        failures = {test: dict(source_test=test)}
        rows = [dict(test=test, reason='repair timing')]
        skipped = original.replace(' def test_one', ' @unittest.skip("repair timing")\n def test_one')
        source.write_text(skipped + 'import unittest\n')
        self.git('commit', '-qam', 'late import', cwd=home)
        with self.assertRaises(ValueError):
            m.skip_only(dict(home_path=home), red, self.git('rev-parse', 'HEAD', cwd=home).strip(), rows, failures)
        self.git('reset', '--hard', red, cwd=home)
        source.write_text('import unittest\n' + skipped)
        self.git('commit', '-qam', 'plain import and skip', cwd=home)
        m.skip_only(dict(home_path=home), red, self.git('rev-parse', 'HEAD', cwd=home).strip(), rows, failures)
        self.git('reset', '--hard', red, cwd=home)
        shadowed = 'import unittest\nunittest = object()\n' + original
        source.write_text(shadowed)
        self.git('commit', '-qam', 'shadowed binding', cwd=home)
        red = self.git('rev-parse', 'HEAD', cwd=home).strip()
        source.write_text(shadowed.replace(' def test_one', ' @unittest.skip("repair timing")\n def test_one'))
        self.git('commit', '-qam', 'shadowed skip', cwd=home)
        with self.assertRaises(ValueError):
            m.skip_only(dict(home_path=home), red, self.git('rev-parse', 'HEAD', cwd=home).strip(), rows, failures)
