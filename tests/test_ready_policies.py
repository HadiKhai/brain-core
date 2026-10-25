"""Receipt validators run in both readiness readers and before execution."""
import contextlib
import json
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


import ast
import subprocess
from pathlib import Path
import test_review_discipline as _reviews


class NativeReceiptReadiness(Scenario):
    job = _reviews.IndependentReviewReceipts.job
    context = _reviews.IndependentReviewReceipts.context

    def publish_native(self):
        root, wt, m, inst, n, receipt = self.context()
        m.record_review_receipt(inst, n, 'job', receipt)
        with mock.patch.dict('os.environ', self.env, clear=True), \
                mock.patch.object(m, 'request_wake', return_value=True):
            packet, reasons = m.record_ready(inst, n, 'job', 1)
        self.assertEqual(reasons, [])
        self.assertEqual(packet['verify_result'], 0)
        row = m.open_job(m.read_jobs(n), 'job')
        path = m.handback_file(n, row)
        path.write_text(json.dumps({'version': 3, 'job': 'job', 'started': row['Started'], 'state': 'done',
                                    'summary': 'Native fixture proof', 'lessons': 'none: fixture',
                                    'ready': packet, 'ready_reasons': []}))
        return root, wt, m, inst, n, receipt, packet, row

    def test_native_full_fixture_then_missing_receipt_blocks_final_train_recheck(self):
        root, wt, m, inst, n, receipt, packet, row = self.publish_native()
        entry = dict(branch='job', key=m.job_key(row), packet=m.ready_fingerprint(n, row, packet))
        with mock.patch.dict('os.environ', self.env, clear=True):
            m.entry_ready(inst, n, entry)
            m.review_receipt_path(n, row, receipt['head']).unlink()
            for mode in ({}, {'display': True}, {'train': True}, {'train': True, 'display': True}):
                self.assertFalse(m.ready_state(inst, n, 'job', **mode)['ready'])
            with self.assertRaisesRegex(SystemExit, 'readiness changed'):
                m.entry_ready(inst, n, entry)

    def test_same_existing_spec_proves_frozen_and_main_bypass_current_reader_refuses(self):
        root, wt, m, inst, n, receipt, packet, row = self.publish_native()
        m.review_receipt_path(n, row, receipt['head']).unlink()
        owner = m._ready_state.__globals__
        for source in ('a8bab01ae1acfad46ff337a2b79efaada9ca4118', '808a6a9af75588526f456fea574ed8798227a134'):
            text = subprocess.run(['git', 'show', source + ':lib/brain/verify.py'], cwd=CORE,
                                  capture_output=True, text=True, check=True).stdout
            tree = ast.parse(text)
            node = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == '_ready_state')
            namespace = dict(owner)
            exec(compile(ast.Module(body=[node], type_ignores=[]), '<retained-ready-reader>', 'exec'), namespace)
            with mock.patch.dict('os.environ', self.env, clear=True):
                previous = namespace['_ready_state'](inst, n, 'job', train=True)
            self.assertTrue(previous['ready'], (source, previous['reasons']))
        with mock.patch.dict('os.environ', self.env, clear=True):
            self.assertFalse(m.ready_state(inst, n, 'job', train=True)['ready'])
