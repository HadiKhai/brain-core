"""Receipt validators run in both readiness readers and before execution."""
import contextlib
import importlib
import io
from types import SimpleNamespace
from unittest import mock

import core_loader
import test_train
from scenario import CORE, Scenario


class ReadyPolicies(Scenario):
    project = test_train.MergeTrain.project
    job = test_train.MergeTrain.job

    def module(self, root):
        m = core_loader.SourceFileLoader('ready_policy_fixture', str(CORE / 'bin/brain')).load_module()
        with mock.patch.dict('os.environ', self.env, clear=True):
            inst = m.Instance(root)
        return m, inst, inst.node('a')

    def test_both_validators_receive_exact_identity(self):
        root, _ = self.project()
        self.job('one')
        m, inst, n = self.module(root)
        review, preview = mock.Mock(return_value=['review blocked']), mock.Mock(return_value=['preview missing'])
        owner = m.ready_policy_reasons.__globals__
        def module(name, package):
            return SimpleNamespace(**{'validate_ready_reviews' if name == '.review' else 'validate_ready_preview': review if name == '.review' else preview})
        with mock.patch.object(owner['importlib'], 'import_module', side_effect=module):
            reasons = m.ready_policy_reasons(inst, n, 'one', '1' * 40, {'head': '1' * 40})
        self.assertEqual(reasons, ['review blocked', 'preview missing'])
        review.assert_called_once_with(inst, n, 'one', '1' * 40, packet={'head': '1' * 40})
        preview.assert_called_once_with(inst, n, 'one', '1' * 40, packet={'head': '1' * 40})

    def test_recording_and_reading_keep_policy_refusals(self):
        root, _ = self.project()
        self.job('one')
        m, inst, n = self.module(root)
        with mock.patch.object(m, 'ready_policy_reasons', return_value=['preview missing']), \
                mock.patch.object(m, 'run_verify') as execute, contextlib.redirect_stdout(io.StringIO()):
            packet, reasons = m.record_ready(inst, n, 'one', 1)
            state = m.ready_state(inst, n, 'one')
        execute.assert_not_called()
        self.assertIsNone(packet)
        self.assertIn('preview missing', reasons)
        self.assertIn('preview missing', state['reasons'])
