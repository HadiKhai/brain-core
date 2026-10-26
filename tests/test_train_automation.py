"""Landing reconciles observed manual merges and delegates exact closed keys."""
import argparse
import contextlib
import io
import json
import os
from types import SimpleNamespace
from unittest import mock

import core_loader
import test_train
from scenario import CORE, Scenario


class TrainAutomation(Scenario):
    project = test_train.MergeTrain.project
    job = test_train.MergeTrain.job
    ledger = test_train.MergeTrain.ledger

    def module(self, root):
        m = core_loader.SourceFileLoader('train_automation_fixture', str(CORE / 'bin/brain')).load_module()
        with mock.patch.dict(os.environ, self.env, clear=True):
            inst = m.Instance(root)
        return m, inst, inst.node('a')

    def test_land_reconciles_a_manually_merged_completed_job_without_a_gate(self):
        root, home = self.project()
        self.job('one')
        self.git('merge', '--ff-only', 'one', cwd=home)
        out = self.b(root, 'land', 'a')
        self.assertIn('manual landing', out)
        self.assertIn('| merged |', self.ledger())
        self.assertFalse((root / 'nodes/a/job-trains').exists() and any((root / 'nodes/a/job-trains').glob('*.json')))
        self.b(root, 'land', 'a')
        self.assertEqual(self.ledger().count('| merged |'), 1)

    def test_post_publication_delegates_only_accepted_keys_and_keeps_landing(self):
        root, _ = self.project()
        m, inst, n = self.module(root)
        key = 'a' * 24
        path = n['dir'] / 'fixture-train.json'
        data = dict(version=1, state='complete', publication_succeeded=True, settle_requested=True,
                    tip='1' * 40, accepted=[dict(key=key)])
        path.write_text(json.dumps(data))
        adapter = mock.Mock(return_value={'settled': [], 'pending': [{'job': key, 'reason': 'still active'}], 'errors': []})
        owner = m.post_publication.__globals__
        with mock.patch.object(owner['importlib'], 'import_module', return_value=SimpleNamespace(settle_landed_jobs=adapter)), \
                contextlib.redirect_stdout(io.StringIO()):
            m.post_publication(inst, n, path, data)
        adapter.assert_called_once_with(inst, n, [key])
        after = json.loads(path.read_text())
        self.assertEqual(after['state'], 'complete')
        self.assertTrue(after['publication_succeeded'])
        self.assertEqual(after['settlement']['pending'][0]['job'], key)
