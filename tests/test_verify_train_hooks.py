"""Train readiness accepts a verified historical base without weakening normal checks."""
import json
from unittest import mock

import core_loader
from scenario import Scenario, CORE
from test_verify import WorkerReadyPackets


class TrainReadyHooks(Scenario):
    job = WorkerReadyPackets.job
    record = WorkerReadyPackets.record
    packet = WorkerReadyPackets.packet

    def test_recorded_base_ancestor_and_normal_readiness_unchanged(self):
        r, home, wt = self.job()
        self.record(r)
        base = json.loads(self.packet().read_text())['ready']['integration_base']
        self.git('commit', '--allow-empty', '-qm', 'advance main', cwd=home)
        m = core_loader.SourceFileLoader('train_ready_hooks', str(CORE / 'bin/brain')).load_module()
        inst = m.Instance(r)
        n = inst.node('a')
        with mock.patch.dict('os.environ', self.env):
            self.assertFalse(m.ready_state(inst, n, 'job')['ready'])
            self.assertTrue(m.ready_state(inst, n, 'job', train=True)['ready'])
        self.assertEqual(m.ready_preflight(n, 'job', base=base)[0], [])
        n['fm']['merge'] = 'pr'
        self.assertIn('train requires', '; '.join(m.ready_preflight(n, 'job', base=base)[0]))
        n['fm']['merge'] = 'local'
        for invalid in ('not-a-commit', '0' * 40):
            self.assertIn('train requires', '; '.join(m.ready_preflight(n, 'job', base=invalid)[0]))

    def test_ejected_evidence_refuses_ordinary_and_train_readiness(self):
        r, home, wt = self.job()
        self.record(r)
        m = core_loader.SourceFileLoader('train_ejection_hook', str(CORE / 'bin/brain')).load_module()
        inst = m.Instance(r)
        jobs = m.ready_state.__globals__['_m_jobs']
        with mock.patch.dict('os.environ', self.env), mock.patch.object(jobs, 'train_ejection', return_value={'reason': 'fixture'}, create=True):
            for train in (False, True):
                state = m.ready_state(inst, inst.node('a'), 'job', train=train)
                self.assertFalse(state['ready'])
                self.assertIn('train ejected', '; '.join(state['reasons']))
