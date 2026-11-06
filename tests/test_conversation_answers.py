"""Real-shaped runtime replies retire only their exact current question episode."""
import datetime
import argparse
import base64
import contextlib
import io
import hashlib
import json
import os
import subprocess
import sys
import textwrap
from unittest import mock

import core_loader
from scenario import CORE, Scenario


class ConversationAnswers(Scenario):
    def setUp(self):
        super().setUp()
        self.root = self.init('oak')
        self.m = core_loader.SourceFileLoader('conversation_fixture', str(CORE / 'bin/brain')).load_module()
        self.inst = self.m.Instance(self.root)
        self.inst.local_of('root')
        self.n = self.inst.node('root')
        self.now = datetime.datetime(2026, 1, 15, 12, tzinfo=datetime.timezone.utc)
        clock = mock.patch.object(self.m.Instance, 'now_dt', side_effect=lambda: self.now)
        clock.start(); self.addCleanup(clock.stop)
        self.path = self.tmp / 'conversation.jsonl'
        self.records = []
        self.data = {'transcript_path': str(self.path), 'session_id': 'fixture-session'}

    def question(self, identity='call-one', count=1):
        return {'type': 'response_item', 'payload': {'type': 'function_call', 'call_id': identity,
                'name': 'request_user_input', 'arguments': json.dumps({'questions': [
                    {'id': 'color-' + str(i), 'question': 'Which color ' + str(i) + '?',
                     'options': [{'label': 'Blue', 'description': 'Use blue'}, {'label': 'Red', 'description': 'Use red'}]}
                    for i in range(count)]})}}

    def capture(self, *records, data=None):
        self.records.extend(records)
        self.path.write_text(''.join(json.dumps(record) + '\n' for record in self.records))
        with mock.patch.object(self.m, 'request_wake', return_value=False):
            self.m.capture_hook_questions(self.inst, self.n, data or self.data)

    def overdue(self):
        return list(self.m.overdue_questions(self.inst, self.n, self.m.inbox_lines(self.n)))

    def reply(self, text):
        return {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': text}}

    def test_chat_option_answer_clears_overdue_and_retains_actual_answer(self):
        self.capture(self.question())
        self.now += datetime.timedelta(minutes=15)
        self.assertEqual(len(self.overdue()), 1)
        self.capture(self.reply('Blue.'))
        self.assertEqual(self.overdue(), [])
        path = self.root / '.brain/wake/lifecycle.json'
        evidence = json.loads(path.read_text())
        self.assertEqual(evidence['answers'][-1]['answer'], 'Blue.')
        self.assertEqual(evidence['answers'][-1]['evidence']['source'], 'conversation-option')
        self.assertIn('Host question:', (self.root / 'inbox.md').read_text())
        self.assertNotIn('fixture-session', path.read_text())
        self.m.inbox_lines(self.n)  # a subsequent read can retire only durably applied evidence
        self.assertTrue(list((self.root / '.brain/wake/conversation-answers').glob('*.done')))

    def test_ambiguous_unrelated_cancelled_and_foreign_replies_stay_open(self):
        self.capture(self.question(count=2))
        self.now += datetime.timedelta(minutes=15)
        self.capture(self.reply('Blue'))
        self.assertEqual(len(self.overdue()), 2)
        self.capture(self.reply('What do you mean?'))
        self.assertEqual(len(self.overdue()), 2)
        cancelled = {'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': 'call-one',
                     'output': json.dumps({'error': "The user doesn't want to take this action right now"})}}
        self.capture(cancelled)
        self.assertEqual(len(self.overdue()), 2)
        exact = {'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': 'call-one',
                 'output': json.dumps({'answers': {'color-0': {'answers': ['Blue']}}})}}
        self.capture(exact, data={**self.data, 'session_id': 'foreign-session'})
        self.assertEqual(len(self.overdue()), 2)
        self.capture(exact)
        self.assertEqual(len(self.overdue()), 1)

    def test_new_call_same_question_is_a_new_episode_old_answers_do_not_clear_it(self):
        self.capture(self.question())
        self.capture(self.reply('Blue'))
        self.assertEqual(self.m.inbox_lines(self.n), [])
        self.capture(self.question('call-two'))
        self.now += datetime.timedelta(minutes=15)
        self.assertEqual(len(self.overdue()), 1)
        old = {'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': 'call-one',
               'output': json.dumps({'answers': {'color-0': {'answers': ['Red']}}})}}
        self.capture(old)
        self.assertEqual(len(self.overdue()), 1)
        self.capture(self.reply('Red'))
        self.assertEqual(self.overdue(), [])

    def test_answer_evidence_survives_failed_lifecycle_publication(self):
        self.capture(self.question())
        self.now += datetime.timedelta(minutes=15)
        self.assertEqual(len(self.overdue()), 1)
        self.capture(self.reply('Blue'))
        owner = self.m._owners['lifecycle_store']
        with mock.patch.object(owner._m_machine, 'machine_publish', side_effect=OSError('fixture disk unavailable')):
            self.assertEqual(len(self.overdue()), 1)
        self.assertTrue(list((self.root / '.brain/wake/conversation-answers').glob('*.json')))
        self.assertEqual(self.overdue(), [])

    def test_exact_tool_result_accepts_free_text_without_option_guessing(self):
        self.capture(self.question())
        self.now += datetime.timedelta(minutes=15)
        answer = {'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': 'call-one',
                  'output': json.dumps({'answers': {'color-0': {'answers': ['Indigo, with a narrow border']}}})}}
        self.capture(answer)
        self.assertEqual(self.overdue(), [])
        self.assertEqual(json.loads((self.root / '.brain/wake/lifecycle.json').read_text())['answers'][-1]['answer'],
                         'Indigo, with a narrow border')

    def test_native_single_claude_answer_result_is_bound_to_its_call(self):
        call = {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 'claude-call',
                'name': 'AskUserQuestion', 'input': {'questions': [{'question': 'Which color?',
                'options': [{'label': 'Blue', 'description': 'Use blue'}]}]}}]}}
        self.capture(call)
        self.now += datetime.timedelta(minutes=15)
        self.assertEqual(len(self.overdue()), 1)
        self.capture({'type': 'user', 'message': {'content': [{'type': 'tool_result',
                     'tool_use_id': 'claude-call', 'content': 'User has answered your questions: Blue'}]}})
        self.assertEqual(self.overdue(), [])


class InstructionCapture(Scenario):
    fixture_init_in_process = True

    def setUp(self):
        super().setUp()
        self.root = self.init('meadow')
        self.m = core_loader.SourceFileLoader('capture_fixture', str(CORE / 'bin/brain')).load_module()
        self.inst = self.m.Instance(self.root)
        self.inst.local_of('root')
        self.n = self.inst.node('root')
        self.n['local'].update(coordinator='host-coordinator', coordinator_sessions=['runtime-coordinator'])
        self.inst.save_local()
        self.capture = self.m._owners['capture_stop']
        self.path = self.tmp / 'capture.jsonl'
        self.quote = 'Always preserve original evidence.'
        self.records = []
        self.data = {'session_id': 'runtime-coordinator', 'transcript_path': str(self.path), 'cwd': str(self.root)}
        self.message(self.quote)

    def message(self, text, **metadata):
        self.records.append(dict(type='user', timestamp='2020-01-01T00:00:00Z',
                                 message={'role': 'user', 'content': text}, **metadata))
        self.path.write_text(''.join(json.dumps(r) + '\n' for r in self.records))

    def answer(self, data, kind='standing_instruction', quote=None):
        return {'messages': [{'id': row['id'], 'items': [{'kind': kind, 'quote': quote or row['text']}]}
                             for row in data]}, {'model': 'fixture', 'usage': {}}

    def stop(self, kind='standing_instruction', data=None, quote=None):
        with mock.patch.object(self.capture, 'ai_json', side_effect=lambda system, data, schema, **kw:
                               self.answer(data, kind, quote)):
            return self.capture.capture_stop(self.inst, self.n, data or self.data)

    def commit_rule(self, gate='Gate: tests/test_sample.py::test_evidence', quote=None):
        text = quote or self.quote
        (self.root / 'AGENTS.md').write_text('- Preserve evidence: "' + text + '"\n')
        library = self.root / 'library/instance-rules.md'
        library.write_text('<!-- rule:R01 -->\n- Preserve evidence: "' + text + '"\n' + gate + '\n')
        self.git('add', 'AGENTS.md', 'library/instance-rules.md', cwd=self.root)
        self.git('commit', '-qm', 'capture fixture instruction', cwd=self.root)

    def test_standing_instruction_without_rule_commit_blocks_and_names_quote(self):
        reason = self.stop()
        self.assertIn(self.quote, reason)
        self.assertIn('same-turn citing rule commit', reason)

    def test_citing_instance_commit_with_gate_passes(self):
        self.commit_rule()
        self.assertIsNone(self.stop())

    def test_explicit_none_gate_with_reason_passes(self):
        self.commit_rule('Gate: none (requires operator judgement)')
        self.assertIsNone(self.stop())

    def test_uncommitted_rule_or_missing_gate_does_not_pass(self):
        self.commit_rule('')
        self.assertIn(self.quote, self.stop())
        path = self.root / 'library/instance-rules.md'
        path.write_text(path.read_text() + '\nGate: none (human judgement)\n')
        self.assertIn(self.quote, self.stop())

    def test_unrelated_rule_gate_does_not_satisfy_instruction(self):
        self.commit_rule('<!-- rule:R02 -->\n- Another rule\nGate: none (human judgement)')
        self.assertIn(self.quote, self.stop())

    def test_one_off_and_question_pass(self):
        for kind in ('one_off', 'question', 'other'):
            with self.subTest(kind=kind):
                self.message('Show the current count.')
                self.assertIsNone(self.stop(kind))

    def test_invented_quote_is_structurally_rejected(self):
        self.assertIn('exact nonempty substring', self.stop(quote='An invented instruction.'))

    def test_all_messages_and_mixed_items_checked_in_one_call(self):
        self.message('How many rows?')
        def classify(system, data, schema, **kw):
            self.assertEqual([row['text'] for row in data], [self.quote, 'How many rows?'])
            value, usage = self.answer(data)
            value['messages'][0]['items'].append({'kind': 'one_off', 'quote': 'evidence'})
            value['messages'][1]['items'][0]['kind'] = 'question'
            return value, usage
        with mock.patch.object(self.capture, 'ai_json', side_effect=classify) as call:
            self.assertIn(self.quote, self.capture.capture_stop(self.inst, self.n, self.data))
        call.assert_called_once()

    def test_missing_or_duplicate_message_items_rejected(self):
        messages = [{'id': '1', 'text': self.quote, 'at': None}]
        for rows in ([], [{'id': '1', 'items': []}],
                     [{'id': '1', 'items': [{'kind': 'question', 'quote': self.quote}]}] * 2):
            with self.subTest(rows=rows), mock.patch.object(self.capture, 'ai_json', return_value=({'messages': rows}, {})):
                with self.assertRaises(ValueError):
                    self.capture.classifications(messages)

    def test_outage_records_deduplicated_structured_inbox_gap_and_allows_stop(self):
        with mock.patch.object(self.capture, 'ai_json', side_effect=self.capture.AIUnavailable('missing_key')):
            self.assertIsNone(self.capture.capture_stop(self.inst, self.n, self.data))
            self.assertIsNone(self.capture.capture_stop(self.inst, self.n, self.data))
        inbox = (self.root / 'inbox.md').read_text()
        self.assertEqual(inbox.count('"kind": "instruction-capture-gap"'), 1)
        self.assertIn('"code": "missing_key"', inbox)
        self.assertNotIn(self.quote, inbox)

    def test_busy_inbox_queues_gap_for_existing_drain(self):
        with mock.patch.object(self.capture._m_transitions, 'inbox_append', side_effect=BlockingIOError), \
                mock.patch.object(self.capture, 'ai_json', side_effect=self.capture.AIUnavailable('timeout')):
            self.assertIsNone(self.capture.capture_stop(self.inst, self.n, self.data))
        rows = list(self.m.hook_question_queue(self.n).glob('*.json'))
        self.assertEqual(len(rows), 1)
        self.assertIn('"code": "timeout"', json.loads(rows[0].read_text())['line'])

    def test_worker_identity_is_not_gated_even_in_node_folder(self):
        with mock.patch.object(self.capture, 'ai_json', side_effect=AssertionError('worker called AI')):
            self.assertIsNone(self.capture.capture_stop(self.inst, self.n, {**self.data, 'session_id': 'worker'}))
        self.assertTrue(self.capture.coordinator(self.n, {'thread_id': 'host-coordinator'}))
        self.n['local']['coordinator'] = 't3-thread://v1/host-coordinator'
        self.assertTrue(self.capture.coordinator(self.n, {'thread_id': 'host-coordinator'}))
        self.assertFalse(self.capture.coordinator(self.n, {'session_id': ''}))

    def test_structural_provenance_excludes_tools_meta_sidechains_and_senders(self):
        records = [dict(self.records[0], **metadata) for metadata in (
            {'isMeta': True}, {'isCompactSummary': True}, {'isSidechain': True},
            {'promptSource': 'system'}, {'turnOrigin': 'task_notification'}, {'senderThreadId': 'worker'},
            {'origin': 'system'}, {'sender': 'agent'})]
        records.append({'type': 'user', 'message': {'content': [{'type': 'tool_result', 'content': self.quote}]}})
        for record in records:
            with self.subTest(record=record):
                self.assertIsNone(self.capture.operator_text(record))
        self.assertEqual(self.capture.operator_text(self.records[0]), self.quote)
        self.assertEqual(self.capture.operator_text(dict(self.records[0], promptSource='sdk', turnOrigin='sdk')), self.quote)

    def test_second_stop_escapes_and_records_unconverted_gap_without_ai(self):
        self.assertIn(self.quote, self.stop())
        with mock.patch.object(self.capture, 'ai_json', side_effect=AssertionError('second stop called AI')):
            self.assertIsNone(self.capture.capture_stop(self.inst, self.n, {**self.data, 'stop_hook_active': True}))
        self.assertIn('second_stop_unconverted', (self.root / 'inbox.md').read_text())
        evidence = list((self.root / '.brain/instruction-capture/gaps').glob('*.json'))
        self.assertEqual(len(evidence), 1)
        self.assertIn(self.quote, evidence[0].read_text())

    def test_second_stop_rechecks_conversion_and_does_not_record_false_gap(self):
        self.assertIn(self.quote, self.stop())
        self.commit_rule()
        with mock.patch.object(self.capture, 'ai_json', side_effect=AssertionError('second stop called AI')):
            self.assertIsNone(self.capture.capture_stop(self.inst, self.n, {**self.data, 'stop_hook_active': True}))
        self.assertNotIn('instruction-capture-gap', (self.root / 'inbox.md').read_text())
        state = json.loads((self.root / '.brain/instruction-capture' /
                            (self.capture.capture_key(self.n, self.data) + '.json')).read_text())
        self.assertEqual(state['head'], self.git('rev-parse', 'HEAD', cwd=self.root).strip())

    def test_second_stop_always_escapes_bad_state_links_and_git_errors(self):
        self.assertIn(self.quote, self.stop())
        active = {**self.data, 'stop_hook_active': True}
        with mock.patch.object(self.capture, 'rule_committed', side_effect=self.capture.AIUnavailable('rule_git_unavailable')):
            self.assertIsNone(self.capture.capture_stop(self.inst, self.n, active))
        state = self.root / '.brain/instruction-capture' / (self.capture.capture_key(self.n, self.data) + '.json')
        for content in (b'{', b'\xff'):
            state.write_bytes(content)
            self.assertIsNone(self.capture.capture_stop(self.inst, self.n, active))
        with mock.patch.object(self.capture._m_common, 'outward_links', return_value=['unsafe-fixture']), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertIsNone(self.capture.capture_stop(self.inst, self.n, active))
        self.assertIn('second_stop_evidence_unavailable', stderr.getvalue())

    def test_rejected_batch_is_not_retried_and_second_stop_records_unclassified_gap(self):
        with mock.patch.object(self.capture, 'ai_json', side_effect=lambda system, data, schema, **kw:
                               self.answer(data, quote='invented')) as call:
            self.assertIn('rejected structurally', self.capture.capture_stop(self.inst, self.n, self.data))
            self.assertIn('rejected structurally', self.capture.capture_stop(self.inst, self.n, self.data))
            self.assertIsNone(self.capture.capture_stop(self.inst, self.n, {**self.data, 'stop_hook_active': True}))
        call.assert_called_once()
        self.assertIn('second_stop_unclassified', (self.root / 'inbox.md').read_text())

    def test_missing_key_deduplicates_across_turns_and_does_not_create_note_event(self):
        with mock.patch.object(self.capture, 'ai_json', side_effect=self.capture.AIUnavailable('missing_key')):
            self.assertIsNone(self.capture.capture_stop(self.inst, self.n, self.data))
            self.message('How many rows?')
            self.assertIsNone(self.capture.capture_stop(self.inst, self.n, self.data))
        inbox = (self.root / 'inbox.md').read_text()
        self.assertEqual(inbox.count('"kind": "instruction-capture-gap"'), 1)
        events = self.m.hook_note_events(self.n)
        self.assertFalse(any(e['label'].startswith('inbox:') for e in events.values()))

    def test_replaced_transcript_records_gap_then_recovers(self):
        self.assertIsNone(self.stop('one_off'))
        replacement = self.path.with_suffix('.replacement')
        replacement.write_text(json.dumps(self.records[0]) + '\n')
        replacement.replace(self.path)
        with mock.patch.object(self.capture, 'ai_json', side_effect=lambda system, data, schema, **kw:
                               self.answer(data, 'question')) as call:
            self.assertIsNone(self.capture.capture_stop(self.inst, self.n, self.data))
            call.assert_not_called()
            self.assertIsNone(self.capture.capture_stop(self.inst, self.n, self.data))
            call.assert_called_once()
        self.assertIn('transcript_changed', (self.root / 'inbox.md').read_text())

    def test_bad_transcript_type_and_gap_write_failure_are_not_silent(self):
        self.assertIsNone(self.capture.capture_stop(self.inst, self.n, {**self.data, 'transcript_path': None}))
        self.assertIn('transcript_path_unavailable', (self.root / 'inbox.md').read_text())
        with mock.patch.object(self.capture._m_transitions, 'inbox_append', side_effect=OSError('synthetic shared target')), \
                mock.patch.object(self.capture, 'ai_json', side_effect=self.capture.AIUnavailable('missing_key')), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertIsNone(self.capture.capture_stop(self.inst, self.n, self.data))
        self.assertIn('inbox_unavailable', stderr.getvalue())

    def test_hook_joins_capture_and_notice_and_preserves_notice_guard_on_outage(self):
        def run():
            with mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(self.data))), \
                    mock.patch.object(self.m, 'capture_hook_questions'), \
                    mock.patch.object(self.m, 'hook_notice_unread', return_value=True) as notice, \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.m.hook_stop(argparse.Namespace(event='stop'))
            notice.assert_called_once()
            return output.getvalue()
        with mock.patch.object(self.capture, 'ai_json', side_effect=lambda system, data, schema, **kw: self.answer(data)):
            reason = run()
            self.assertIn(self.quote, reason)
            self.assertIn('Read the notice', reason)
        self.message('How many rows?')
        with mock.patch.object(self.capture, 'ai_json', side_effect=self.capture.AIUnavailable('missing_key')):
            self.assertIn('Read the notice', run())
        with mock.patch.object(self.capture, 'capture_stop', side_effect=SystemExit(1)):
            self.assertIn('Read the notice', run())
        self.assertIn('capture_unexpected_error', (self.root / 'inbox.md').read_text())

    def test_node_rule_commit_and_same_turn_timestamp_are_enforced(self):
        self.n = dict(self.n, id='oak', dir=self.root / 'nodes/oak')
        self.n['dir'].mkdir(parents=True)
        (self.n['dir'] / 'AGENTS.md').write_text('- "' + self.quote + '" Gate: none (human judgement)\n')
        self.git('add', 'nodes/oak/AGENTS.md', cwd=self.root)
        self.git('commit', '-qm', 'capture node fixture', cwd=self.root)
        self.assertIsNone(self.stop())
        item = {'kind': 'standing_instruction', 'quote': self.quote, 'at': '2099-01-01T00:00:00Z'}
        self.assertFalse(self.capture.rule_committed(self.inst, self.n, item))

    def test_capped_bootstrap_retains_current_turn_with_explicit_boundary(self):
        self.records = [{'type': 'user', 'message': {'content': 'x' * 2000}},
                        {'type': 'event_msg', 'payload': {'type': 'task_complete'}}, self.records[0]]
        self.path.write_text(''.join(json.dumps(r) + '\n' for r in self.records))
        with mock.patch.object(self.capture, 'LIMIT', 512):
            messages, _ = self.capture.snapshot(self.path, {})
        self.assertEqual([m['text'] for m in messages], [self.quote])

    def test_cursor_covers_multiple_new_messages_and_does_not_repeat_old_input(self):
        self.assertIsNone(self.stop('one_off'))
        self.message('Use green for this chart.')
        self.message('How many rows?')
        def classify(system, data, schema, **kw):
            self.assertEqual([row['text'] for row in data], ['Use green for this chart.', 'How many rows?'])
            return self.answer(data, 'one_off')
        with mock.patch.object(self.capture, 'ai_json', side_effect=classify) as call:
            self.assertIsNone(self.capture.capture_stop(self.inst, self.n, self.data))
            self.assertIsNone(self.capture.capture_stop(self.inst, self.n, self.data))
        call.assert_called_once()

    def test_previous_head_rule_cannot_satisfy_new_turn(self):
        self.commit_rule()
        self.assertIsNone(self.stop('one_off'))
        self.message(self.quote)
        self.assertIn(self.quote, self.stop())

    def test_bootstrap_starts_after_previous_completed_turn_and_codex_deduplicates(self):
        self.records = [self.records[0], {'type': 'event_msg', 'payload': {'type': 'task_complete'}},
                        {'type': 'event_msg', 'timestamp': '2020-01-01T00:00:00Z',
                         'payload': {'type': 'user_message', 'message': 'How many rows?'}},
                        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                         'content': [{'type': 'input_text', 'text': 'How many rows?'}]}}]
        self.path.write_text(''.join(json.dumps(r) + '\n' for r in self.records))
        messages, _ = self.capture.snapshot(self.path, {})
        self.assertEqual([r['text'] for r in messages], ['How many rows?'])

    def test_hook_blocks_capture_before_relay_only_and_skips_notes_on_outage(self):
        def run():
            with mock.patch.object(sys, 'stdin', io.StringIO(json.dumps({**self.data, 'last_assistant_message': 'relay only'}))), \
                    mock.patch.object(self.m, 'capture_hook_questions'), \
                    mock.patch.object(self.m, 'notes_lag', side_effect=AssertionError('unexpected notes check')), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.m.hook_stop(argparse.Namespace(event='stop'))
            return output.getvalue()
        with mock.patch.object(self.capture, 'ai_json', side_effect=lambda system, data, schema, **kw: self.answer(data)):
            self.assertIn(self.quote, run())
        with mock.patch.object(self.capture, 'ai_json', side_effect=self.capture.AIUnavailable('timeout')):
            # Add new input so the first block's persisted items are not the only work.
            self.message('What changed?')
            self.assertEqual(run(), '')

    def test_ai_request_separates_cached_system_and_data_and_bounds_total_wait(self):
        client = sys.modules[self.capture.ai_json.__module__]
        request = client.json_request(self.capture.SYSTEM, [{'id': '0', 'text': self.quote}], self.capture.SCHEMA)
        self.assertEqual(request['system'][0]['text'], self.capture.SYSTEM)
        self.assertEqual(request['system'][0]['cache_control'], {'type': 'ephemeral'})
        self.assertEqual(json.loads(request['messages'][0]['content']), [{'id': '0', 'text': self.quote}])
        self.assertEqual(request['output_config']['format']['schema'], self.capture.SCHEMA)
        with mock.patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'synthetic-secret'}), \
                mock.patch.object(client.subprocess, 'run', side_effect=subprocess.TimeoutExpired('fixture', 20)) as call:
            with self.assertRaises(client.AIUnavailable):
                client.ai_json(self.capture.SYSTEM, [], self.capture.SCHEMA)
        self.assertEqual(call.call_args.kwargs['timeout'], 20)
        self.assertEqual(call.call_args.args[0][1:3], ['-I', '-c'])
        self.assertEqual(call.call_args.kwargs['cwd'], CORE / 'lib/brain')
        self.assertEqual(call.call_args.kwargs['env'], {'ANTHROPIC_API_KEY': 'synthetic-secret'})

    def cli_events(self):
        return json.loads((CORE / 'tests/fixtures/claude-cli/2.1.312-events.json').read_text())

    def cli_result(self, value=None, **overrides):
        value = value if value is not None else self.answer([{'id': '0', 'text': self.quote}], 'other')[0]
        result = next(event for event in reversed(self.cli_events()) if event['type'] == 'result')
        result.update(structured_output=value, **overrides)
        return subprocess.CompletedProcess(['claude'], 0, json.dumps(result), '')

    def test_cli_recorded_array_fixture_through_stub_and_every_fixture_has_known_shape(self):
        client = sys.modules[self.capture.ai_json.__module__]
        folder = CORE / 'tests/fixtures/claude-cli'
        provenance = json.loads((folder / 'provenance.json').read_text())
        self.assertEqual((folder / 'version.txt').read_text().strip(), provenance['cli_version'])
        self.assertEqual(provenance['cli_version'], '2.1.312 (Claude Code)')
        self.assertEqual(hashlib.sha256(self.capture.SYSTEM.encode()).hexdigest(), provenance['system_sha256'])
        self.assertEqual(hashlib.sha256(json.dumps(self.capture.SCHEMA).encode()).hexdigest(), provenance['schema_sha256'])
        # New stored protocol fixtures must join this explicit known-shape inventory.
        self.assertEqual({p.name for p in folder.glob('*.json')} - {'provenance.json'},
                         {provenance['fixture']})
        raw = (folder / provenance['fixture']).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), provenance['fixture_sha256'])
        self.assertEqual(provenance['shape'], 'event_array')
        events = json.loads(raw)
        self.assertIsInstance(events, list)
        result = next(event for event in reversed(events) if event['type'] == 'result')
        stub_dir = self.tmp / 'recorded-cli-bin'
        stub_dir.mkdir()
        stub = stub_dir / 'claude'
        stub.write_text('#!' + sys.executable + '\nimport json, sys\n'
            'from pathlib import Path\n'
            'assert json.load(sys.stdin) == ' + repr(provenance['synthetic_input']) + '\n'
            'sys.stdout.buffer.write(Path(' + repr(str(folder / provenance['fixture'])) + ').read_bytes())\n')
        stub.chmod(0o700)
        with mock.patch.dict(os.environ, {'ANTHROPIC_API_KEY': '',
                'PATH': str(stub_dir) + os.pathsep + os.environ['PATH']}):
            value, usage = client.ai_json(self.capture.SYSTEM, provenance['synthetic_input'], self.capture.SCHEMA)
        self.assertEqual(value, result['structured_output'])
        self.assertEqual(usage['usage'], result['usage'])
        self.assertEqual(len(self.capture.validated_classifications(provenance['synthetic_input'], value)), 2)
        # Derive both object variants from the recorded result, including the
        # older envelope without type. Intermediate events never supply usage.
        legacy = dict(result)
        legacy.pop('type')
        earlier_error = dict(result, is_error=True)
        for response in (result, legacy, [earlier_error] + events + [events[0]]):
            with self.subTest(shape=type(response).__name__), mock.patch.object(client.subprocess,
                    'run', return_value=subprocess.CompletedProcess(['claude'], 0, json.dumps(response), '')):
                parsed, metadata = client.cli_json(self.capture.SYSTEM, [], self.capture.SCHEMA, self.capture.MODEL)
            self.assertEqual(parsed, value)
            self.assertEqual(metadata['usage'], result['usage'])

    def test_cli_unseen_shapes_fail_named_with_exact_cause_and_raw_diagnostics(self):
        client = sys.modules[self.capture.ai_json.__module__]
        events = self.cli_events()
        result = events[-1]
        # Fault injections derived from the captured protocol, never extra fixtures.
        unknown = [None, True, 7, 'result', {'events': events}, events[:-1], [None] + events]
        for key in ('subtype', 'is_error', 'structured_output'):
            unknown.append({k: v for k, v in result.items() if k != key})
        unknown.extend([dict(result, type='assistant'), dict(result, is_error='false'),
                        dict(result, subtype=False), dict(result, structured_output=[])])
        for response in unknown:
            variants = [response]
            if isinstance(response, dict) and response.get('type') == 'result':
                variants.append([response])
            for shape in variants:
                raw = json.dumps(shape).encode()
                with self.subTest(shape=type(shape).__name__), mock.patch.object(client.subprocess,
                        'run', return_value=subprocess.CompletedProcess(['claude'], 0, raw, b'')):
                    with self.assertRaises(client.AIUnavailable) as caught:
                        client.cli_json(self.capture.SYSTEM, [], self.capture.SCHEMA, self.capture.MODEL)
                self.assertEqual(str(caught.exception), 'cli_unknown_shape')
                detail = caught.exception.detail
                self.assertEqual(detail['stage'], 'unknown_shape')
                self.assertEqual(detail['error_type'], 'ValueError')
                self.assertTrue(detail['error_message'])
                self.assertEqual(base64.b64decode(detail['stdout_tail_b64']), raw[-client.CLI_OUTPUT_BYTES:])
                self.assertEqual(detail['stdout_bytes'], len(raw))

    def test_cli_argv_stdin_environment_and_disposable_cwd(self):
        client = sys.modules[self.capture.ai_json.__module__]
        payload = [{'id': '0', 'text': self.quote}]
        def run(argv, **kw):
            self.assertEqual(argv, ['claude', '-p', '--model', 'claude-haiku-5-5',
                '--system-prompt', self.capture.SYSTEM, '--json-schema', json.dumps(self.capture.SCHEMA),
                '--output-format', 'json', '--tools', '', '--no-session-persistence',
                '--setting-sources', '', '--safe-mode', '--settings',
                json.dumps({'disableAllHooks': True, 'autoMemoryEnabled': False}),
                '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
                '--disable-slash-commands', '--no-chrome'])
            self.assertNotIn('--bare', argv)
            self.assertNotIn(self.quote, argv)
            self.assertEqual(json.loads(kw['input']), payload)
            self.assertNotIn(self.capture.SYSTEM, kw['input'].decode('utf-8'))
            cwd = client.Path(kw['cwd']).resolve()
            self.assertTrue(cwd.is_dir())
            self.assertNotIn(client.Path(os.environ['HOME']).resolve(), cwd.parents)
            self.assertNotIn(self.root.resolve(), cwd.parents)
            self.assertEqual(kw['timeout'], 20)
            self.assertEqual(kw['env'], {'HOME': os.environ['HOME'], 'PATH': os.environ['PATH'],
                'USER': 'fixture-user', 'LOGNAME': 'fixture-user',
                'BRAIN_CAPTURE_CHILD': '1', 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1'})
            self.cli_cwd = cwd
            return self.cli_result()
        with mock.patch.dict(os.environ, {'ANTHROPIC_API_KEY': '', 'CLAUDECODE': 'parent',
                                         'USER': 'fixture-user', 'LOGNAME': 'fixture-user',
                                         'CLAUDE_CONFIG_DIR': '/synthetic/foreign', 'TMPDIR': str(self.root)}), \
                mock.patch.object(client.subprocess, 'run', side_effect=run):
            value, usage = client.ai_json(self.capture.SYSTEM, payload, self.capture.SCHEMA)
        self.assertEqual(value, json.loads(self.cli_result().stdout)['structured_output'])
        self.assertEqual(usage['transport'], 'claude_cli')
        self.assertEqual(usage['usage'], json.loads(self.cli_result().stdout)['usage'])
        self.assertEqual(set(usage), {'transport', 'model', 'usage', 'latency_seconds'})
        self.assertNotIn('detail', json.dumps(usage))
        self.assertGreaterEqual(usage['latency_seconds'], 0)
        self.assertFalse(self.cli_cwd.exists())

    def test_cli_child_reenters_hook_without_recursion(self):
        client = sys.modules[self.capture.ai_json.__module__]
        stub_dir = self.tmp / 'stub-bin'
        stub_dir.mkdir()
        marker = self.tmp / 'classified'
        # The stub launches the actual CLI dispatch. Its instrumented capture
        # would run on this bound session if the env guard were absent.
        hook = textwrap.dedent('''
            import sys
            sys.path.insert(0, LIB)
            from brain import cli, instruction_capture
            def classified(*args):
                from pathlib import Path
                Path(MARKER).write_text('classified')
                return None
            instruction_capture.capture_stop = classified
            sys.argv = ['brain', 'hook', 'stop']
            cli.main()
        ''').replace('LIB', repr(str(CORE / 'lib'))).replace('MARKER', repr(str(marker)))
        stub = stub_dir / 'claude'
        stub.write_text('#!' + sys.executable + '\n' + textwrap.dedent('''
            import json, os, subprocess, sys
            assert os.environ['BRAIN_CAPTURE_CHILD'] == '1'
            payload = json.loads(sys.stdin.read())
            result = subprocess.run([sys.executable, '-B', '-c', HOOK], input=DATA,
                env=dict(os.environ, BRAIN_ROOT=__ROOT__, BRAIN_NO_REEXEC='1'),
                text=True, capture_output=True, timeout=5)
            assert result.returncode == 0, result.stderr
            assert result.stdout == '', result.stdout
            print(json.dumps({'subtype': 'success', 'is_error': False,
                'structured_output': {'messages': [{'id': row['id'], 'items': [
                    {'kind': 'other', 'quote': row['text']}]} for row in payload]}}))
        ''').replace('HOOK', repr(hook)).replace('DATA', repr(json.dumps(self.data)))
            .replace('__ROOT__', repr(str(self.root))))
        stub.chmod(0o700)
        with mock.patch.dict(os.environ, {'ANTHROPIC_API_KEY': '',
                'PATH': str(stub_dir) + os.pathsep + os.environ['PATH']}):
            self.assertIsNone(self.capture.capture_stop(self.inst, self.n, self.data))
        self.assertFalse(marker.exists(), 'guarded child classified recursively')
        self.assertFalse((self.root / '.brain/instruction-capture/diagnostics').exists())
        # Negative control: the same real hook dispatch reaches capture without
        # the guard, so the guarded assertion cannot pass merely on bad routing.
        env = dict(self.env, BRAIN_ROOT=str(self.root), BRAIN_NO_REEXEC='1')
        env.pop('BRAIN_CAPTURE_CHILD', None)
        result = subprocess.run([sys.executable, '-B', '-c', hook], input=json.dumps(self.data),
                                env=env, text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(marker.exists(), 'negative control never reached capture')

    def test_cli_failures_record_gap_and_allow_exit_without_private_output(self):
        client = sys.modules[self.capture.ai_json.__module__]
        failures = [
            ('timeout', 'timeout', subprocess.TimeoutExpired('claude', 20,
                output=b'private partial stdout', stderr=b'private timeout stderr')),
            ('transport', 'transport', FileNotFoundError(2, 'private CLI executable missing')),
            ('provider_or_network', 'nonzero_exit', subprocess.CompletedProcess(
                ['claude'], 3, b'private partial response', b'private auth error')),
            ('provider_or_network', 'provider_error', subprocess.CompletedProcess(['claude'], 0,
                json.dumps({'subtype': 'success', 'is_error': True, 'error': self.quote}), '')),
            ('provider_or_network', 'response_subtype', subprocess.CompletedProcess(['claude'], 0,
                json.dumps({'subtype': 'error_max_turns', 'is_error': False, 'error': self.quote}), '')),
            ('cli_invalid_response', 'json_decode', subprocess.CompletedProcess(['claude'], 0,
                b'private invalid output\xff', b'private invalid stderr')),
            ('cli_unknown_shape', 'unknown_shape', subprocess.CompletedProcess(['claude'], 0, '[]', '')),
            ('cli_unknown_shape', 'unknown_shape', subprocess.CompletedProcess(['claude'], 0,
                json.dumps({'subtype': 'success', 'is_error': False}), '')),
            ('cli_unknown_shape', 'unknown_shape', subprocess.CompletedProcess(['claude'], 0,
                json.dumps({'subtype': 'success', 'is_error': False, 'structured_output': []}), '')),
            ('cli_unknown_shape', 'unknown_shape', subprocess.CompletedProcess(['claude'], 0,
                json.dumps(self.cli_events()[:-1]), '')),
            ('provider_or_network', 'provider_error', subprocess.CompletedProcess(['claude'], 0,
                json.dumps(self.cli_events()[:-1] + [dict(self.cli_events()[-1], is_error=True)]), '')),
            ('cli_invalid_response', 'classification_schema', self.cli_result({'messages': []})),
            ('cli_invalid_response', 'classification_schema', self.cli_result({'messages': [{'id': '0', 'items': [
                {'kind': 'unknown', 'quote': self.quote}]}]})),
            ('cli_invalid_response', 'classification_schema', self.cli_result({'messages': [{'id': '0', 'items': [
                {'kind': 'other', 'quote': 'Invented quote.'}]}]})),
        ]
        original_run = client.subprocess.run
        for index, (code, stage, failure) in enumerate(failures):
            with self.subTest(code=code, index=index):
                self.data['session_id'] = 'failure-' + str(index)
                self.n['local']['coordinator_sessions'].append(self.data['session_id'])
                def run(argv, **kw):
                    if argv[0] != 'claude':
                        return original_run(argv, **kw)
                    if isinstance(failure, Exception):
                        raise failure
                    return failure
                with mock.patch.dict(os.environ, {'ANTHROPIC_API_KEY': ''}), \
                        mock.patch.object(client.subprocess, 'run', side_effect=run):
                    self.assertIsNone(self.capture.capture_stop(self.inst, self.n, self.data))
                inbox = (self.root / 'inbox.md').read_text()
                self.assertIn('"code": "' + code + '"', inbox)
                self.assertNotIn(self.quote, inbox)
                self.assertNotIn('private', inbox)
                line = inbox.split('from runtime: ')[-1].split(' <!--')[0]
                record = json.loads(line)
                self.assertEqual(record['cause'], stage)
                self.assertEqual(set(record) - {'returncode'},
                                 {'kind', 'code', 'node', 'capture', 'cause', 'diagnostic'})
                path = self.root / '.brain/instruction-capture' / record['diagnostic']
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertLessEqual(path.stat().st_size, client.CLI_DETAIL_BYTES)
                detail = json.loads(path.read_text())
                self.assertEqual(detail['stage'], stage)
                self.assertEqual(detail['returncode'], getattr(failure, 'returncode', None))
                for name in ('stdout', 'stderr'):
                    raw = getattr(failure, name, None) or b''
                    raw = raw if isinstance(raw, bytes) else raw.encode('utf-8')
                    self.assertEqual(base64.b64decode(detail[name + '_tail_b64']), raw[-client.CLI_OUTPUT_BYTES:])
                    self.assertEqual(detail[name + '_bytes'], len(raw))
                if isinstance(failure, Exception):
                    self.assertEqual(detail['error_type'], type(failure).__name__)
                    self.assertEqual(detail['error_message'], str(failure))
                if stage == 'timeout':
                    self.assertEqual(detail['timeout_seconds'], 20)
                if stage == 'transport':
                    self.assertEqual(detail['errno'], 2)
                if stage in ('provider_error', 'response_subtype'):
                    response = json.loads(failure.stdout)
                    response = response[-1] if isinstance(response, list) else response
                    self.assertEqual(detail['response_subtype'], response['subtype'])
                if stage == 'unknown_shape':
                    self.assertEqual(detail['error_type'], 'ValueError')
                    self.assertTrue(detail['error_message'])
                if stage == 'classification_schema':
                    self.assertEqual(detail['error_type'], 'CaptureRejected')
                    self.assertTrue(detail['error_message'])
                state = self.root / '.brain/instruction-capture' / (self.capture.capture_key(self.n, self.data) + '.json')
                self.assertNotIn('rejected', json.loads(state.read_text()))

    def test_cli_failure_detail_bounds_exact_byte_tails_and_survives_raised_error(self):
        client = sys.modules[self.capture.ai_json.__module__]
        stdout = b'private-prefix' + bytes(range(256)) * 100
        stderr = b'private-stderr' + b'\xff' * 50000
        failures = [subprocess.CompletedProcess(['claude'], 7, stdout, stderr),
                    subprocess.TimeoutExpired('claude', 20, output=stdout, stderr=stderr)]
        for failure in failures:
            with self.subTest(kind=type(failure).__name__), \
                    mock.patch.object(client.subprocess, 'run', side_effect=(failure if isinstance(failure, Exception)
                        else lambda *a, **kw: failure)):
                with self.assertRaises(client.AIUnavailable) as caught:
                    client.cli_json(self.capture.SYSTEM, [], self.capture.SCHEMA, self.capture.MODEL)
            detail = caught.exception.detail
            self.assertNotIn('private', str(caught.exception))
            self.assertLessEqual(len(json.dumps(detail, ensure_ascii=False).encode('utf-8')) + 1,
                                 client.CLI_DETAIL_BYTES)
            for name, raw in [('stdout', stdout), ('stderr', stderr)]:
                self.assertEqual(base64.b64decode(detail[name + '_tail_b64']), raw[-client.CLI_OUTPUT_BYTES:])
                self.assertEqual(detail[name + '_bytes'], len(raw))
                self.assertTrue(detail[name + '_truncated'])
            self.capture.capture_gap(self.inst, self.n, 'bound-probe', str(caught.exception), detail=detail)
        for path in (self.root / '.brain/instruction-capture/diagnostics').glob('*.json'):
            self.assertLessEqual(path.stat().st_size, client.CLI_DETAIL_BYTES)
        self.assertNotIn('private', (self.root / 'inbox.md').read_text())

    def test_api_key_keeps_http_success_and_invalid_response_behavior(self):
        client = sys.modules[self.capture.ai_json.__module__]
        value = self.answer([{'id': '0', 'text': self.quote}], 'other')[0]
        response = {'stop_reason': 'end_turn', 'model': 'fixture-model', 'usage': {'output_tokens': 8},
                    'content': [{'type': 'text', 'text': json.dumps(value)}]}
        with mock.patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'synthetic-secret'}), \
                mock.patch.object(client.subprocess, 'run', return_value=subprocess.CompletedProcess(
                    ['python'], 0, json.dumps(response), '')) as call:
            parsed, usage = client.ai_json(self.capture.SYSTEM, [], self.capture.SCHEMA)
        self.assertEqual(parsed, value)
        self.assertEqual(usage['model'], 'fixture-model')
        self.assertNotIn('transport', usage)
        self.assertEqual(call.call_args.args[0][0:3], [sys.executable, '-I', '-c'])
        self.assertEqual(call.call_args.kwargs['env'], {'ANTHROPIC_API_KEY': 'synthetic-secret'})
        with mock.patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'synthetic-secret'}), \
                mock.patch.object(client.subprocess, 'run', return_value=subprocess.CompletedProcess(
                    ['python'], 0, 'invalid', '')):
            with self.assertRaises(client.AIResponseInvalid):
                client.ai_json(self.capture.SYSTEM, [], self.capture.SCHEMA)


class StopNoticeGuard(Scenario):
    """Synthetic completion notices never silently retire unread task output."""
    def setUp(self):
        super().setUp()
        self.m = core_loader.SourceFileLoader('stop_notice_fixture', str(CORE / 'bin/brain')).load_module()
        self.path = self.tmp / 'notice.jsonl'
        self.notice = {'type': 'user', 'message': {'content': (
            '<task-notification><task-id>task-17</task-id><output-file>/tmp/task-17.output</output-file>'
            '<status>completed</status><summary>Fixture finished</summary></task-notification>')}}

    def assistant(self, text):
        return {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': text}]}}

    def tool(self, name='Read', value='/tmp/task-17.output'):
        return {'type': 'assistant', 'message': {'content': [
            {'type': 'tool_use', 'id': 'read-17', 'name': name, 'input': {'file_path': value}}]}}

    def unread(self, *records):
        self.path.write_text(''.join(json.dumps(r) + '\n' for r in records))
        return self.m.hook_notice_unread(self.path)

    def test_reply_text_does_not_change_structural_read_decision(self):
        for text in ('No response requested', '  NO RESPONSE REQUESTED.  ', 'No response requested!',
                     'No response requested—', 'Done.', '任意の返答'):
            with self.subTest(text=text):
                self.assertTrue(self.unread(self.notice, self.assistant(text)))
                self.assertFalse(self.unread(self.notice, self.tool(), self.assistant(text)))

    def test_notice_without_output_read_blocks(self):
        self.assertTrue(self.unread(self.notice, self.assistant('Done.')))
        self.assertTrue(self.unread(self.notice))
        self.assertTrue(self.unread(self.notice, self.tool('SendMessage'), self.assistant('Root notified.')))
        self.assertTrue(self.unread(self.notice, self.tool(value='/tmp/task-17.output.other')))
        self.assertTrue(self.unread(self.notice, self.tool(value='/tmp/other.output')))

    def test_notice_read_and_bash_or_task_output_pass(self):
        for name, value in (('Read', '/tmp/task-17.output'), ('Grep', '/tmp/task-17.output'),
                            ('Bash', 'cat /tmp/task-17.output'),
                            ('Bash', 'tail -n 40 /tmp/task-17.output'), ('TaskOutput', 'task-17'),
                            ('BashOutput', 'task-17')):
            with self.subTest(name=name, value=value):
                self.assertFalse(self.unread(self.notice, self.tool(name, value), self.assistant('Fixed.')))
        self.assertFalse(self.unread(self.notice, self.tool(), self.tool('SendMessage'),
                                     self.assistant('Root notified.')))

    def test_no_notice_and_old_turn_are_unaffected(self):
        self.assertFalse(self.unread(self.assistant('No response requested.')))
        self.assertFalse(self.unread(self.notice, {'type': 'user', 'message': {'content': 'New request'}},
                                     self.assistant('No response requested.')))
        quoted = {'type': 'user', 'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': 'old', 'content': self.notice['message']['content']}]}}
        self.assertFalse(self.unread(quoted, self.assistant('Done.')))

    def test_read_before_notice_and_partial_task_id_do_not_count(self):
        self.assertTrue(self.unread(self.tool(), self.notice, self.assistant('Done.')))
        self.assertTrue(self.unread(self.notice, self.tool('TaskOutput', 'task-170')))

    def test_queued_attachment_notices_require_a_later_read(self):
        notice = {'type': 'attachment', 'attachment': {'type': 'queued_command',
                  'commandMode': 'task-notification', 'prompt': self.notice['message']['content']}}
        operator = {'type': 'user', 'message': {'content': 'Run the fixture task.'}}
        result = {'type': 'user', 'message': {'content': [
                  {'type': 'tool_result', 'tool_use_id': 'read-17', 'content': 'Earlier output.'}]}}
        prefix = [operator, self.tool(), result, notice]
        self.assertTrue(self.unread(*prefix, self.assistant('Done.')))
        self.assertTrue(self.unread(*prefix, self.assistant('No response requested.')))
        self.assertFalse(self.unread(*prefix, self.tool(), self.assistant('Fixed.')))
        second = {'type': 'attachment', 'attachment': {'type': 'queued_command',
                  'prompt': self.notice['message']['content'].replace('task-17', 'task-18')}}
        self.assertTrue(self.unread(*prefix, self.tool(), second, self.assistant('Done.')))
        self.assertFalse(self.unread(*prefix, self.tool(), second,
                                    self.tool(value='/tmp/task-18.output'), self.assistant('Fixed.')))
        self.assertFalse(self.unread(*prefix, operator, self.assistant('Done.')))
        notice['attachment']['type'] = 'file'
        self.assertFalse(self.unread(operator, notice, self.assistant('Done.')))

    def test_list_content_multiple_notices_require_every_output(self):
        text = self.notice['message']['content']
        notice = {'type': 'user', 'message': {'content': [{'type': 'text', 'text': text +
                  text.replace('task-17', 'task-18')}]}}
        self.assertTrue(self.unread(notice, self.tool()))
        self.assertFalse(self.unread(notice, self.tool(), self.tool(value='/tmp/task-18.output')))

    def test_prose_without_structural_notice_or_identifiers_is_ignored(self):
        notice = {'type': 'user', 'message': {'content':
                  'Background command fixture completed with exit code 0. Output file: /tmp/task-17.output'}}
        self.assertFalse(self.unread(notice, self.assistant('Done.')))
        self.assertFalse(self.unread(notice, self.tool()))
        notice['message']['content'] = ('<task-notification>Output file: /tmp/task-17.output; '
                                        'task-id: task-17</task-notification>')
        self.assertFalse(self.unread(notice))

    def test_envelopes_without_identifiers_do_not_create_unread_obligations(self):
        for body in ('', '<status>failed</status>', '<task-id> </task-id>',
                     '<task-id>task-17</output-file>'):
            notice = {'type': 'user', 'message': {'content': '<task-notification>' + body +
                      '</task-notification>'}}
            with self.subTest(body=body):
                self.assertFalse(self.unread(notice, self.assistant('No response requested.')))
        text = self.notice['message']['content'] + '<task-notification><status>completed</status></task-notification>'
        mixed = {'type': 'user', 'message': {'content': text}}
        self.assertTrue(self.unread(mixed))
        self.assertFalse(self.unread(mixed, self.tool()))

    def test_bounded_chunks_malformed_records_and_nonregular_input(self):
        with mock.patch.object(self.m, 'HOOK_TRANSCRIPT_CHUNK', 17):
            self.assertTrue(self.unread(None, self.notice, self.assistant('Done.')))
        with self.path.open('a') as stream:
            stream.write('{"unfinished')
        self.assertTrue(self.m.hook_notice_unread(self.path))
        self.assertFalse(self.m.hook_notice_unread('/dev/null'))
