"""Verification context follows executable identity and semantic configuration."""
import os
from unittest import mock

import core_loader
from scenario import CORE, Scenario


class GateContext(Scenario):
    def setUp(self):
        super().setUp()
        self.m = core_loader.SourceFileLoader('gate_context_fixture', str(CORE / 'bin/brain')).load_module()

    def test_irrelevant_path_suffix_preserves_lane_identity(self):
        n = {'home_path': self.tmp, 'fm': {'verify_lanes': '[{"name":"unit","command":"python3 -V","inputs":["src.py"]}]'}}
        w = {'path': str(self.tmp)}
        with mock.patch.object(self.m, 'tracked_files', return_value={'src.py': ['100644', 'source']}):
            before = self.m.lane_snapshot(n, w, self.env, 'inputs')
            after = self.m.lane_snapshot(n, w, {**self.env, 'PATH': self.env['PATH'] + os.pathsep + str(self.tmp / 'unused')}, 'inputs')
        self.assertEqual(before['context'], after['context'])

    def test_worker_rebinding_preserves_train_configuration(self):
        n = {'home_path': self.tmp, 'fm': {'verify': 'true'}, 'local': {'workers': {'job': 'old'}, 'coordinator': 'old', 'worktrees': {'job': {'path': str(self.tmp)}}}}
        before = self.m.train_configuration(n)
        n['local']['workers']['job'] = 'new'
        n['local']['coordinator'] = 'new'
        self.assertEqual(self.m.train_configuration(n), before)
        n['fm']['verify'] = 'false'
        self.assertNotEqual(self.m.train_configuration(n), before)
        n['fm']['verify'] = 'true'
        n['local']['worktrees']['job']['path'] = 'changed'
        self.assertNotEqual(self.m.train_configuration(n), before)

    def test_semantic_identity_and_execution_evidence_have_distinct_path_contracts(self):
        n = {'home_path': self.tmp, 'fm': {'verify_lanes': '[{"name":"unit","command":"python3 -V","inputs":["src.py"]}]'}}
        w = {'path': str(self.tmp)}
        env = {key: value for key, value in self.env.items() if key != 'CFLAGS'}
        with mock.patch.object(self.m, 'tracked_files', return_value={'src.py': ['100644', 'source']}):
            before = self.m.lane_snapshot(n, w, env, 'inputs')
            suffix = self.m.lane_snapshot(n, w, {**env, 'PATH': env['PATH'] + os.pathsep + str(self.tmp / 'unused')}, 'inputs')
            restored = self.m.lane_snapshot(n, w, self.m.recorded_lane_environment(
                {**env, 'PATH': '/caller-only'}, self.m.lane_environment(env)), 'inputs')
            empty = self.m.lane_snapshot(n, w, {**env, 'CFLAGS': ''}, 'inputs')
            unrelated = self.m.lane_snapshot(n, w, {**env, 'BRAIN_GATE_FD': '999'}, 'inputs')
        self.assertEqual(before['context'], suffix['context'])
        self.assertNotEqual(before['evidence_context'], suffix['evidence_context'])
        self.assertNotEqual(before['evidence_context'], before['context'])
        self.assertEqual(before, restored)
        self.assertNotEqual(before['context'], empty['context'])
        self.assertNotEqual(before['evidence_context'], empty['evidence_context'])
        self.assertEqual(before, unrelated)

    def test_pr_moved_ref_details_do_not_invent_a_local_main_move(self):
        with mock.patch.object(self.m, 'integration_ref', return_value='refs/remotes/origin/main'):
            details = self.m.moved_ref_details({'fm': {'merge': 'pr'}, 'home_path': self.tmp}, 'job',
                                              '1' * 40, '1' * 40, '2' * 40, '3' * 40, '2' * 40, '3' * 40)
        self.assertEqual(len(details), 1)
        self.assertTrue(details[0].startswith('refs/remotes/origin/main '))
        self.assertNotIn('refs/heads/main', str(details))
