"""Failure handbacks and questions need stored-record diagnosis, without real runs."""
import argparse
import contextlib
import io
import json
import os
import sys
import unittest
from unittest import mock

from scenario import CORE, Scenario
import core_loader
import test_verify

sys.path.insert(0, str(CORE / 'lib'))
from brain import dissection

VALID = ('## Dissection\nCause: Fixture parser used the wrong slot.\nItems covered: 3\n'
         '### Cases\n1. Ref: records/a.json; Wrong slot.\n2. Ref: sample-b; Same cause.\n'
         '3. Ref: https://example.test/c; Same cause.\n### Fixes\n'
         '1. Correct the slot, low cost.\n2. Rebuild the parser, high cost.\nRecommendation: 1\n')


class DissectionFormat(unittest.TestCase):
    def test_required_states_words_and_measurements(self):
        for state in ('blocked', 'failed', 'parked', 'refused', 'two-strikes'):
            self.assertTrue(dissection.required(state, 'Fixture'))
        for word in ('blocked', 'FAILED', 'parked', 'two strikes', 'two-strikes', 'refused'):
            self.assertTrue(dissection.required('question', 'Fixture ' + word))
        self.assertFalse(dissection.required('done', 'Implemented the fixture'))
        self.assertFalse(dissection.required('question', 'Choose a colour'))
        self.assertFalse(dissection.required('done', 'Unblocked the fixture'))
        self.assertFalse(dissection.required('done', 'No failed tests; fixture is not blocked'))
        self.assertTrue(dissection.required('question', '0 failed tests; publication blocked'))
        for direction, result, target, misses in [('at-least', 6, 7, True), ('at-least', 7, 7, False),
                                                  ('at-most', 11, 10, True), ('at-most', 10, 10, False)]:
            self.assertEqual(dissection.required('done', 'Complete', dissection.measurement(result, target, direction)), misses)

    def test_rotation_exempts_only_state_trigger(self):
        self.assertFalse(dissection.required('blocked', 'Restart after compactions', restart='compactions'))
        self.assertTrue(dissection.required('blocked', 'Fixture failed', restart='compactions'))
        self.assertTrue(dissection.required('blocked', 'Restart after compactions',
                                            dissection.measurement(6, 7), 'compactions'))

    def test_missing_fields_are_named(self):
        self.assertEqual(dissection.parse('Narrative\n' + VALID + '\n## Other\nCause: unrelated')['items_covered'], 3)
        for old, new, message in [('## Dissection', '## Result', '## Dissection'),
                                  ('Cause: Fixture parser used the wrong slot.', 'Cause:', 'Cause:'),
                                  ('Items covered: 3', 'Items covered: many', 'Items covered:'),
                                  ('3. Ref: https://example.test/c; Same cause.\n', '', '3 numbered cases'),
                                  ('2. Ref: sample-b;', '2. sample-b;', 'case 2 stored-record Ref:'),
                                  ('2. Rebuild the parser, high cost.\n', '', '2-3 ranked fixes'),
                                  ('Recommendation: 1', 'Recommendation: 9', 'Recommendation:')]:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                dissection.parse(VALID.replace(old, new))

    def test_invalid_measurements_are_refused(self):
        for result, target, direction in [(1, None, 'at-least'), (True, 2, 'at-least'),
                                           (float('nan'), 1, 'at-least'), (1, float('inf'), 'at-least'),
                                           (1, 2, 'unknown')]:
            with self.assertRaisesRegex(ValueError, 'finite result and target'):
                dissection.measurement(result, target, direction)

    def test_placeholder_references_do_not_count_as_stored_records(self):
        for placeholder in (';', '-', 'none', 'n/a', 'NA', 'TBD', 'todo', 'unknown', '`none`', '...'):
            with self.subTest(placeholder=placeholder), self.assertRaisesRegex(ValueError, 'case 1 stored-record Ref:'):
                dissection.parse(VALID.replace('records/a.json', placeholder))

    def test_fenced_examples_are_not_a_report_section(self):
        with self.assertRaisesRegex(ValueError, '## Dissection section'):
            dissection.parse('```markdown\n' + VALID + '```\n')
        self.assertEqual(dissection.parse('```markdown\n## Dissection\n```\n' + VALID)['items_covered'], 3)


class HandbackDissection(Scenario):
    job = test_verify.WorkerReadyPackets.job

    def send(self, root, state='blocked', summary='Fixture result', *extra, ok=True):
        return self.b(root, 'handback', 'a', '--branch', 'job', '--state', state,
                      '--summary', summary, '--direct', *extra, ok=ok)

    def test_blocked_failed_and_summary_refusal_before_attempt_then_accepts_report(self):
        root, _, _ = self.job()
        for state, summary in [('blocked', 'Fixture'), ('failed', 'Fixture'),
                                ('question', 'Fixture parked'), ('done', 'Fixture refused')]:
            extra = ['--question', 'Choose a repair?'] if state == 'question' else []
            out = self.send(root, state, summary, *extra, ok=False)
            self.assertIn('dissection required', out)
            self.assertFalse(list((root / 'nodes/a/job-attempts').glob('*.json')))
        report = self.dissection_report()
        self.send(root, 'blocked', 'Fixture', '--report', str(report))
        record = json.loads(next((root / 'nodes/a/job-handbacks').glob('*.json')).read_text())
        self.assertEqual(record['dissection']['items_covered'], 3)
        self.assertEqual(record['dissection']['source'], str(report))

    def test_done_and_compaction_rotation_pass_without_report_but_bad_result_does_not(self):
        root, _, _ = self.job()
        self.send(root, 'done', 'Fixture implemented')
        self.send(root, 'blocked', 'Restart after compactions', '--restart', 'compactions')
        out = self.send(root, 'blocked', 'Fixture failed', '--restart', 'compactions', ok=False)
        self.assertIn('dissection required', out)
        out = self.send(root, 'done', 'Fixture complete', '--result', '6', '--target', '7', ok=False)
        self.assertIn('dissection required', out)
        self.send(root, 'done', 'Fixture complete', '--result', '6', '--target', '7',
                  '--dissection', str(self.dissection_report()))
        record = json.loads(next((root / 'nodes/a/job-handbacks').glob('*.json')).read_text())
        self.assertEqual(record['measurement'], dict(result=6.0, target=7.0, direction='at-least'))

    def test_queued_dissection_is_snapshotted_once_and_survives_source_removal(self):
        root, _, _ = self.job()
        module = core_loader.SourceFileLoader('dissection_queue_fixture', str(CORE / 'bin/brain')).load_module()
        inst = module.Instance(root)
        report = self.dissection_report()
        with mock.patch.dict(os.environ, self.env, clear=True):
            lease = module.intent_lease(inst)
            try:
                identity = self.b(root, 'handback', 'a', '--branch', 'job', '--state', 'blocked',
                                  '--summary', 'Fixture', '--report', str(report), '--enqueue').strip()
            finally:
                os.close(lease)
            report.unlink()
            with contextlib.redirect_stdout(io.StringIO()):
                module.drain_intents(inst)
        self.assertEqual(module.intent_receipt(inst, identity)['state'], 'applied')
        record = json.loads(next((root / 'nodes/a/job-handbacks').glob('*.json')).read_text())
        self.assertEqual(record['dissection']['items_covered'], 3)

    def test_report_read_once_within_one_handback_and_bounded_regular_input(self):
        path = self.dissection_report()
        a = argparse.Namespace(state='blocked', summary='Fixture', report=str(path))
        with mock.patch.object(dissection, 'read_report', wraps=dissection.read_report) as read:
            self.assertEqual(dissection.handback(a), dissection.handback(a))
            read.assert_called_once()
        path.write_bytes(b'x' * (dissection.MAX_REPORT + 1))
        with self.assertRaisesRegex(ValueError, '512 KiB'):
            dissection.read_report(path)
        pipe = self.tmp / 'pipe'
        os.mkfifo(pipe)
        with self.assertRaisesRegex(ValueError, 'regular file'):
            dissection.read_report(pipe)

    def test_reusing_library_arguments_starts_a_new_report_snapshot(self):
        root, _, _ = self.job()
        module = core_loader.SourceFileLoader('dissection_reused_args', str(CORE / 'bin/brain')).load_module()
        inst = module.Instance(root)
        report = self.dissection_report()
        args = argparse.Namespace(id='a', branch='job', state='blocked', summary='Fixture',
                                  report=str(report), lessons=None, reviews=None, need_human=None)
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(module, 'load', return_value=inst), \
                contextlib.redirect_stdout(io.StringIO()):
            module.cmd_handback(args)
            report.write_text('## Dissection\nCause: Incomplete new evidence\n')
            with self.assertRaisesRegex(SystemExit, 'dissection required'):
                module.cmd_handback(args)

    def test_unicode_never_term_refuses_direct_and_queued_diagnoses_before_publication(self):
        root, _, _ = self.job()
        (root / 'nodes/a/never-name.txt').write_text('Café\n')
        report = self.dissection_report()
        report.write_text(report.read_text().replace('Synthetic fixture input.', 'Café fixture input.'))
        for mode in ('--direct', '--enqueue'):
            out = self.b(root, 'handback', 'a', '--branch', 'job', '--state', 'blocked', '--summary', 'Fixture',
                         '--dissection', str(report), mode, ok=False)
            self.assertIn('never-name', out)
            self.assertFalse(list((root / 'nodes/a/job-handbacks').glob('*.json')))
            self.assertFalse(list((root / '.brain/write-queue').glob('*.request')))


class QuestionDissection(Scenario):
    def setUp(self):
        super().setUp()
        self.root = self.init('alice')
        self.spec = dict(options=[dict(id='repair', label='Repair', action='routine'),
                                  dict(id='hold', label='Hold', action='routine')],
                         recommendation='repair', default='repair', deadline='2035-01-01T00:00:00Z')

    def send(self, text, spec, key, ok=True):
        path = self.tmp / 'question.json'
        path.write_text(json.dumps(spec))
        return self.b(self.root, 'notify', 'root', 'question', text, '--key', key, '--question-file', str(path), ok=ok)

    def test_failure_state_summary_and_measurement_use_same_gate_then_valid_file_passes(self):
        for spec, text in [(self.spec, 'Fixture failed'), ({**self.spec, 'state': 'blocked'}, 'Choose'),
                           ({**self.spec, 'summary': 'Fixture parked'}, 'Choose'),
                           ({**self.spec, 'result': 6, 'target': 7}, 'Choose')]:
            with self.subTest(spec=spec):
                out = self.send(text, spec, 'failure', ok=False)
                self.assertIn('dissection required', out)
                self.assertFalse((self.root / '.brain/communications.json').exists())
        self.send('Fixture failed', {**self.spec, 'dissection': VALID}, 'failure')
        records = json.loads((self.root / '.brain/communications.json').read_text())['questions']
        self.assertEqual(next(iter(records.values()))['dissection']['items_covered'], 3)

    def test_ordinary_question_unaffected_and_diagnosis_is_bound_to_idempotency(self):
        self.send('Choose a colour', self.spec, 'ordinary')
        diagnosed = {**self.spec, 'dissection': VALID}
        self.send('Fixture failed', diagnosed, 'failure')
        self.send('Fixture failed', diagnosed, 'failure')
        out = self.send('Fixture failed', {**diagnosed, 'dissection': VALID.replace('Items covered: 3', 'Items covered: 4')},
                        'failure', ok=False)
        self.assertIn('different content', out)

    def test_unicode_never_term_in_question_diagnosis_is_rejected(self):
        (self.root / 'never-name.txt').write_text('Café\n')
        out = self.send('Fixture failed', {**self.spec, 'dissection': VALID.replace('Fixture parser', 'Café parser')},
                        'private', ok=False)
        self.assertIn('never-name', out)
        self.assertFalse((self.root / '.brain/communications.json').exists())
