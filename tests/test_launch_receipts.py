"""Adapter receipt checks compare explicit identities without claiming execution."""
import copy
import json
import unittest

import core_loader
from scenario import CORE, Scenario


class LaunchReceiptChecks(unittest.TestCase):
    def setUp(self):
        self.m = core_loader.SourceFileLoader('receipt_fixture', str(CORE / 'bin/brain')).load_module()
        self.selection = {'instanceId': 'provider-one', 'model': 'sample-model',
                          'options': {'reasoningEffort': 'high', 'fastMode': False}}
        self.receipt = {'taskId': 'task-one', 'providerInstanceId': 'provider-one', 'model': 'sample-model',
                        'options': [{'id': 'fastMode', 'value': False}, {'id': 'reasoningEffort', 'value': 'high'}]}

    def test_exact_receipt_matches_options_independent_of_representation(self):
        result = self.m.check_launch_receipt(self.selection, self.receipt)
        self.assertEqual(result['status'], 'match')
        self.assertFalse(result['execution_verified'])
        self.assertEqual(self.m.check_launch_receipt(self.selection, {'structuredContent': self.receipt}), result)

    def test_provider_model_and_each_option_mismatch_reject(self):
        for field, value in (('providerInstanceId', 'provider-two'), ('model', 'other-model'),
                             ('options', {'reasoningEffort': 'low', 'fastMode': False}),
                             ('options', {'reasoningEffort': 'high', 'fastMode': 'false'}),
                             ('options', {'reasoningEffort': 'high'}),
                             ('options', {'reasoningEffort': 'high', 'fastMode': False, 'serviceTier': 'priority'})):
            receipt = {**self.receipt, field: value}
            with self.subTest(field=field, value=value):
                self.assertEqual(self.m.check_launch_receipt(self.selection, receipt)['status'], 'mismatch')

    def test_unknown_missing_null_duplicate_and_request_echo_reject(self):
        bad = [None, {}, {'target': self.receipt}, {**self.receipt, 'model': None},
               {**self.receipt, 'options': None}, {**self.receipt, 'providerInstanceId': ''},
               {k: v for k, v in self.receipt.items() if k != 'options'},
               {**self.receipt, 'options': [{'id': 'fastMode', 'value': False}, {'id': 'fastMode', 'value': False}]},
               {**self.receipt, 'options': {'fastMode': 0}},
               {**self.receipt, 'model': 'runtime default'}]
        for receipt in bad:
            with self.subTest(receipt=receipt):
                self.assertEqual(self.m.check_launch_receipt(self.selection, receipt)['status'], 'unknown')
        for selection in ({**self.selection, 'instanceId': None},
                          {**self.selection, 'providerInstanceId': 'conflicting'},
                          {**self.selection, 'options': None}):
            self.assertEqual(self.m.check_launch_receipt(selection, self.receipt)['status'], 'unknown')

    def test_empty_options_must_be_explicit_on_both_sides(self):
        selection = {**self.selection, 'options': {}}
        receipt = {**self.receipt, 'options': []}
        self.assertEqual(self.m.check_launch_receipt(selection, receipt)['status'], 'match')
        receipt.pop('options')
        self.assertEqual(self.m.check_launch_receipt(selection, receipt)['status'], 'unknown')

    def test_validation_never_mutates_returned_or_declared_data(self):
        before = copy.deepcopy([self.selection, self.receipt])
        self.m.check_launch_receipt(self.selection, self.receipt)
        self.assertEqual([self.selection, self.receipt], before)


class LaunchReceiptCLI(Scenario):
    def test_cli_refuses_mismatch_unknown_and_duplicate_json_keys(self):
        root = self.init('oak')
        selection = self.tmp / 'selection.json'
        receipt = self.tmp / 'receipt.json'
        selection.write_text(json.dumps({'instanceId': 'engine', 'model': 'sample-model', 'options': {'effort': 'high'}}))
        receipt.write_text(json.dumps({'providerInstanceId': 'engine', 'model': 'sample-model', 'options': {'effort': 'high'}}))
        args = ('launch-check', 'root', '--selection-file', str(selection), '--receipt-file', str(receipt))
        result = json.loads(self.b(root, *args))
        self.assertEqual(result['status'], 'match')
        receipt.write_text(json.dumps({'providerInstanceId': 'engine', 'model': 'different', 'options': {'effort': 'high'}}))
        self.assertEqual(json.loads(self.b(root, *args, ok=False))['status'], 'mismatch')
        receipt.write_text('{"providerInstanceId":"engine","model":"bad","model":"sample-model","options":{"effort":"high"}}')
        self.assertEqual(json.loads(self.b(root, *args, ok=False))['status'], 'unknown')
        receipt.write_text('{malformed')
        self.assertEqual(json.loads(self.b(root, *args, ok=False))['status'], 'unknown')
