"""Declared cheap checks must pass before full-gate admission."""
import contextlib
from unittest import mock

import core_loader
from scenario import CORE, Scenario


class CheapChecks(Scenario):
    def run_case(self, cheap_result):
        m = core_loader.SourceFileLoader('cheap_check_fixture', str(CORE / 'bin/brain')).load_module()
        owner = m.run_verify.__globals__
        events = []
        @contextlib.contextmanager
        def full(*args):
            events.append('full-admission')
            yield 'full-token'
        def execute(inst, n, branch, command, w, env, token, lane=None):
            events.append(command)
            return (cheap_result if command == 'cheap' else 0), 'fixture.log'
        with mock.patch.object(owner['_m_gate_lock'], 'node_gate', side_effect=lambda *a: contextlib.nullcontext({})), \
                mock.patch.object(owner['_m_machine'], 'machine_wait', side_effect=lambda *a: events.append('cheap-admission') or 'cheap-token'), \
                mock.patch.object(owner['_m_machine'], 'machine_release', side_effect=lambda *a: events.append('release-cheap')), \
                mock.patch.object(m, 'verify_reservation', side_effect=full), \
                mock.patch.object(m, 'run_verify_stream', side_effect=execute):
            result, _ = m.run_verify(mock.Mock(), {'id': 'app', 'fm': {'verify': 'full', 'verify_cheap': 'cheap'}}, 'job', 'full', {}, {})
        return result, events

    def test_cheap_runs_before_full_admission(self):
        result, events = self.run_case(0)
        self.assertEqual(result, 0)
        self.assertEqual(events, ['cheap-admission', 'cheap', 'release-cheap', 'full-admission', 'full'])

    def test_cheap_failure_preserves_result_and_never_admits_full(self):
        result, events = self.run_case(7)
        self.assertEqual(result, 7)
        self.assertNotIn('full-admission', events)
        self.assertEqual(events[-1], 'release-cheap')
