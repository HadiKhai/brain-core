"""Only positively bound workers are subject to the coordinator message guard."""
from unittest import mock

import core_loader
from scenario import CORE, Scenario


class WorkerMessageGuard(Scenario):
    def setUp(self):
        super().setUp()
        self.root = self.init('oak')
        self.m = core_loader.SourceFileLoader('worker_message_fixture', str(CORE / 'bin/brain')).load_module()
        self.inst = self.m.Instance(self.root)
        self.inst.local_of('root')
        self.n = self.inst.node('root')
        self.row = {'Job': 'job/palette', 'Started': '2026-01-15T09:00Z', 'Ended': ''}
        self.m.write_jobs(self.inst, self.n, [self.row])
        self.n['local']['coordinator'] = 'fixture-parent'
        self.n['local']['workers'] = {self.m.job_key(self.row): 'fixture-worker'}

    def guard(self, message, sender='fixture-worker', target='fixture-parent', tool='t3_thread_send'):
        return self.m.worker_message_guard(self.inst, sender, tool, {'threadId': target, 'message': message})

    def test_four_kinds_allowed_and_progress_denied_for_bound_parent(self):
        for kind in ('start', 'done', 'blocked', 'question'):
            self.assertIsNone(self.guard(kind + ': fixture result'))
        self.assertIsNotNone(self.guard('progress: still working'))
        self.assertIsNotNone(self.guard('FYI: no action requested'))
        self.assertIsNotNone(self.guard('I completed another small step.'))

    def test_unknown_coordinator_and_peer_calls_are_not_misclassified(self):
        for sender in ('unknown-worker', 'fixture-parent', ''):
            self.assertIsNone(self.guard('a message', sender=sender))
        self.assertIsNone(self.guard('source coordination', target='fixture-peer'))
        self.assertIsNone(self.guard('a message', tool='t3_thread_read'))
        self.n['local']['workers']['a' * 24] = 'fixture-parent'
        self.assertIsNone(self.guard('a message', sender='fixture-parent'))

    def test_native_name_and_linked_worker_identity_are_recognized(self):
        self.n['local']['workers'][self.m.job_key(self.row)] = '[worker](t3-thread://v1/fixture-worker)'
        self.assertIsNotNone(self.guard('progress: a step', tool='mcp__t3_code__t3_thread_send'))
        self.assertIsNone(self.guard('Question: choose a color', tool='mcp__t3_code__t3_thread_send'))
        with mock.patch.object(self.m, 'read_jobs', return_value=None):
            self.assertIsNone(self.guard('progress: a step'))
