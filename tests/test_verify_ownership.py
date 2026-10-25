"""Managed admission preserves primary outcomes and attempts every cleanup."""
import contextlib
from types import SimpleNamespace
from unittest import mock

import core_loader
from scenario import CORE, Scenario


class VerificationOwnership(Scenario):
    def test_all_cleanup_runs_without_masking_primary(self):
        m = core_loader.SourceFileLoader('verify_ownership_fixture', str(CORE / 'bin/brain')).load_module()
        owner = m.verify_reservation.__wrapped__.__globals__
        machine = owner['_m_machine']
        token = SimpleNamespace(result=0)
        actions = []
        @contextlib.contextmanager
        def managed(*args, **kwargs):
            actions.append('managed')
            yield token
        def cleanup(run, callbacks, primary=None):
            for callback in callbacks:
                try:
                    callback()
                except BaseException:
                    if primary is None:
                        raise
        with mock.patch.object(machine, 'machine_run', side_effect=managed, create=True), \
                mock.patch.object(machine, 'machine_wait', return_value=token), \
                mock.patch.object(machine, 'machine_release', side_effect=lambda t: actions.append('release')), \
                mock.patch.object(machine, 'machine_cleanup', side_effect=cleanup, create=True), \
                mock.patch.object(m, 'verify_cleanup', side_effect=lambda *a: actions.append('docker')), \
                mock.patch.object(owner['_m_docker_leftovers'], 'docker_leftover_nodes', side_effect=lambda *a: actions.append('diagnostic')):
            with self.assertRaisesRegex(RuntimeError, 'primary'):
                with m.verify_reservation(mock.Mock(), {'id': 'app'}, 'job'):
                    raise RuntimeError('primary')
        self.assertEqual(actions, ['managed', 'release', 'docker', 'diagnostic'])

    def test_resources_are_not_imported_without_configuration(self):
        m = core_loader.SourceFileLoader('verify_resource_fixture', str(CORE / 'bin/brain')).load_module()
        owner = m.verification_resources.__wrapped__.__globals__
        with mock.patch.object(owner['importlib'], 'import_module', side_effect=AssertionError('unexpected resource IO')):
            with m.verification_resources(mock.Mock(), {'fm': {}}):
                pass


import json
import os
import fcntl
import test_verify as _verify_tests


class NativeVerifierLifecycle(Scenario):
    job = _verify_tests.WorkerReadyPackets.job
    runner = _verify_tests.VerifyLogs.runner
    command = _verify_tests.VerifyLogs.command
    execute = _verify_tests.VerifyLogs.run_verify

    def terminal(self, inst):
        rows = json.loads((inst.root / '.brain/wake/machine-holds.json').read_text())['waits']
        rows = [row for row in rows.values() if row['kind'] == 'verify']
        self.assertEqual(len(rows), 1)
        return rows[0]

    def assert_released(self, m):
        path = m.machine_dir() / 'machine-care-queue.json'
        self.assertEqual(json.loads(path.read_text()), [])

    def test_success_stream_records_success_and_runs_cleanup(self):
        setup = self.runner()
        m, inst = setup[:2]
        wait = m.machine_wait
        def admitted(*args, **kwargs):
            fd = os.open(m.gate_path(inst.root, setup[2]['id']), os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)
            return wait(*args, **kwargs)
        with mock.patch.object(m, 'machine_wait', side_effect=admitted), \
                mock.patch.object(m, 'request_wake', return_value=True), \
                mock.patch.object(m, 'docker_cleanup_nodes', return_value={'stopped': [], 'removed': [], 'errors': []}) as cleanup:
            result, log, _ = self.execute(setup, 'print("native fixture")')
        self.assertEqual(result, 0)
        self.assertEqual(log.read_text(), 'native fixture\n')
        self.assertEqual(self.terminal(inst)['outcome'], 'success')
        cleanup.assert_called_once_with(inst, [setup[2]])
        self.assert_released(m)

    def test_failed_stream_retains_nonzero_with_secondary_cleanup_error(self):
        setup = self.runner()
        m, inst = setup[:2]
        with mock.patch.object(m, 'request_wake', return_value=True), \
                mock.patch.object(m, 'docker_cleanup_nodes', return_value={'stopped': [], 'removed': [], 'errors': ['fixture']}):
            result, _, _ = self.execute(setup, 'raise SystemExit(7)')
        self.assertEqual(result, 7)
        row = self.terminal(inst)
        self.assertEqual(row['outcome'], 'failed')
        self.assertIn('7', row['reason'])
        self.assertTrue(row['cleanup_errors'])
        self.assert_released(m)

    def test_cancelled_bootstrap_preserves_primary_and_cleans_queue(self):
        setup = self.runner()
        m, inst = setup[:2]
        register = m.machine_child
        def interrupt(token, pid):
            register(token, pid)
            raise KeyboardInterrupt('fixture cancellation')
        with mock.patch.object(m, 'request_wake', return_value=True), \
                mock.patch.object(m, 'machine_child', side_effect=interrupt), \
                mock.patch.object(m, 'docker_cleanup_nodes', return_value={'stopped': [], 'removed': [], 'errors': ['fixture']}):
            with self.assertRaises(KeyboardInterrupt):
                self.execute(setup, 'raise AssertionError("must never execute")')
        row = self.terminal(inst)
        self.assertEqual(row['outcome'], 'cancelled')
        self.assertIn('KeyboardInterrupt', row['reason'])
        self.assertTrue(row['cleanup_errors'])
        self.assert_released(m)

    def test_cleanup_failure_cannot_record_a_successful_run(self):
        setup = self.runner()
        m, inst = setup[:2]
        with mock.patch.object(m, 'request_wake', return_value=True), \
                mock.patch.object(m, 'docker_cleanup_nodes', return_value={'stopped': [], 'removed': [], 'errors': ['fixture']}):
            with self.assertRaisesRegex(ValueError, 'cleanup incomplete'):
                self.execute(setup, 'pass')
        self.assertEqual(self.terminal(inst)['outcome'], 'error')
        self.assert_released(m)


    def test_configured_resource_lease_spans_cleanup_and_failure_retains_claim(self):
        setup = self.runner()
        m, inst, n = setup[:3]
        n['fm']['verify_resources'] = 'fixture-service'
        records = m.machine_dir() / 'resource-leases'
        observed = []
        def cleanup(*args):
            rows = [json.loads(path.read_text()) for path in records.glob('*.json')]
            self.assertEqual(len(rows), 1)
            observed.append(rows[0]['state'])
            return {'stopped': [], 'removed': [], 'errors': []}
        with mock.patch.object(m, 'request_wake', return_value=True), \
                mock.patch.object(m, 'docker_cleanup_nodes', side_effect=cleanup):
            self.assertEqual(self.execute(setup, 'pass')[0], 0)
            self.assertEqual(json.loads(next(records.glob('*.json')).read_text())['state'], 'released')
            self.assertEqual(self.execute(setup, 'raise SystemExit(7)')[0], 7)
            self.assertEqual(json.loads(next(records.glob('*.json')).read_text())['state'], 'active')
        self.assertEqual(observed, ['active', 'active'])
        self.assert_released(m)
