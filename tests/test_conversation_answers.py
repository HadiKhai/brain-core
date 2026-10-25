"""Real-shaped runtime replies retire only their exact current question episode."""
import datetime
import json
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
