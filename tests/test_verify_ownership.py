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
