"""Train ejections are coordinator records, scoped to one exact worker hand-back."""
import json
from unittest import mock
import core_loader
from scenario import Scenario, CORE
import test_verify


class TrainEjections(Scenario):
    job = test_verify.WorkerReadyPackets.job
    record = test_verify.WorkerReadyPackets.record

    def test_ejection_binds_job_packet_and_new_reviewed_handback(self):
        root, home, wt = self.job()
        self.record(root)
        m = core_loader.SourceFileLoader('ejection_hooks', str(CORE / 'bin/brain')).load_module()
        inst = m.Instance(root)
        n = inst.node('a')
        row = m.open_job(m.read_jobs(n), 'job')
        packet = m.read_handback(n, row)['ready']
        folder = n['dir'] / 'job-trains'
        folder.mkdir()
        path = folder / 'fixture.json'
        data = {'version': 1, 'ejected': [{'key': m.job_key(row), 'packet': m.ready_fingerprint(n, row, packet)}]}
        path.write_text(json.dumps(data))
        self.assertIn('previously ejected', m.train_ejection(n, row, packet))
        with mock.patch.dict('os.environ', self.env):
            self.assertFalse(m.ready_state(inst, n, 'job')['ready'])
            self.assertFalse(m.ready_state(inst, n, 'job', train=True)['ready'])
        self.assertIsNone(m.train_ejection(n, {**row, 'Started': '2020-01-01T00:00:00Z'}, packet))
        self.assertIsNone(m.train_ejection(n, row, {**packet, 'head': '0' * 40}))
        # Normal re-handback reuses valid verify evidence, incrementing the reuse counter.
        self.record(root)
        self.assertIsNone(m.train_ejection(n, row, m.read_handback(n, row)['ready']))
        path.write_text('broken')
        self.assertIn('unreadable', m.train_ejection(n, row, packet))
