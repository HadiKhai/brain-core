"""Declared cheap checks must pass before full-gate admission."""
import contextlib
import json
import os
from unittest import mock

import core_loader
from scenario import CORE, Scenario


class CheapChecks(Scenario):
    def run_case(self, cheap_result):
        m = core_loader.SourceFileLoader('cheap_check_fixture', str(CORE / 'bin/brain')).load_module()
        owner = m.run_verify.__globals__
        root = self.init('checks')
        inst = m.Instance(root)
        events = []
        reservation = m.command_reservation
        @contextlib.contextmanager
        def cheap(*args, **kwargs):
            events.append('cheap-admission')
            with reservation(*args, **kwargs) as token:
                yield token
            events.append('release-cheap')
        @contextlib.contextmanager
        def full(*args):
            events.append('full-admission')
            yield 'full-token'
        def execute(inst, n, branch, command, w, env, token, lane=None):
            events.append(command)
            result = cheap_result if command == 'cheap' else 0
            if command == 'cheap':
                token.result = result
            return result, 'fixture.log'
        with mock.patch.dict(os.environ, self.env), \
                mock.patch.object(owner['_m_gate_lock'], 'node_gate', side_effect=lambda *a: contextlib.nullcontext({})), \
                mock.patch.object(m, 'command_reservation', side_effect=cheap), \
                mock.patch.object(m, 'verify_reservation', side_effect=full), \
                mock.patch.object(m, 'run_verify_stream', side_effect=execute):
            n = inst.node('root')
            n['fm'].update(verify='full', verify_cheap='cheap')
            result, _ = m.run_verify(inst, n, 'job', 'full', {}, {})
            with m.machine_queue() as queue:
                self.assertEqual(queue.rows, [])
            waits = json.loads((root / '.brain/wake/machine-holds.json').read_text())['waits']
            self.assertEqual(len(waits), 1)
            row = next(iter(waits.values()))
            self.assertIsNotNone(row['ended_at'])
            self.assertEqual(row['outcome'], 'error' if cheap_result else 'success')
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
