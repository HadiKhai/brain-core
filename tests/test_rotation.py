"""Coordinator rotation uses accounting numbers and files, never transcript text."""
import json
import os
from unittest import mock
from core_loader import SourceFileLoader
from scenario import Scenario, CORE


class Rotation(Scenario):
    fixture_init_in_process = True
    fixture_templates = True
    def setUp(self):
        super().setUp()
        self.r = self.init('fixture')
        self.b(self.r, 'new', 'app', '--title', 'App')
        self.node = self.r / 'nodes/app'
        self.cfg = self.tmp / 'config'
        self.cfg.mkdir()
        self.codex = self.tmp / 'codex'
        self.claude = self.tmp / 'claude'
        self.env['BRAIN_CONFIG'] = str(self.cfg / 'brains.json')
        (self.cfg / 'subscriptions.json').write_text(json.dumps({'subscriptions': [
            {'id': 'cx', 'runtime': 'codex', 'config_dir': str(self.codex), 'brains': ['*']},
            {'id': 'cl', 'runtime': 'claude', 'config_dir': str(self.claude), 'brains': ['*']}]}))
        self.sid = '11111111-aaaa'
        self.bind()

    def bind(self, ids=None):
        path = self.r / '.brain/local.json'
        data = json.loads(path.read_text())
        data['nodes'].setdefault('app', {}).update(coordinator='host-thread', coordinator_sessions=ids or [self.sid])
        path.write_text(json.dumps(data))

    def transcript(self, runtime, records, sid=None):
        sid = sid or self.sid
        path = (self.codex / 'sessions' / '2026/01/01' / ('rollout-' + sid + '.jsonl') if runtime == 'codex'
                else self.claude / 'projects' / '-scratch' / (sid + '.jsonl'))
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = {'type': 'session_meta', 'payload': {'id': sid, 'cwd': str(self.tmp / 'scratch')}}
        rows = ([meta] if runtime == 'codex' else []) + records
        staged = path.with_suffix('.tmp')
        staged.write_text(''.join(json.dumps({'timestamp': '2026-01-01T00:00:00.000Z', **r},
                                         separators=(',', ':')) + '\n' for r in rows))
        staged.replace(path)
        return path

    def usage(self, inp, cached=0, rid='one'):
        return {'type': 'token_usage_record', 'payload': {'thread_id': self.sid, 'response_id': rid,
                    'usage': {'input_tokens': inp, 'cached_input_tokens': cached}}}

    def due(self):
        return [x for x in json.loads(self.b(self.r, 'due', 'app', '--json')) if x['kind'] == 'rotate']

    def test_fix3_default_boundary_and_unknown_current_session(self):
        self.transcript('codex', [self.usage(299999)])
        self.assertEqual(self.due(), [])
        self.transcript('codex', [self.usage(300000)])
        self.assertEqual(len(self.due()), 1)
        self.b(self.r, 'set', 'app', 'rotate_tokens=400000')
        self.assertEqual(self.due(), [])
        self.transcript('codex', [self.usage(400000)])
        self.assertEqual(len(self.due()), 1)
        self.bind([self.sid, '22222222-new'])
        self.assertEqual(self.due(), [])
        self.bind()
        self.b(self.r, 'set', 'app', 'rotate_tokens=200000')
        self.transcript('codex', [self.usage(200000)])
        self.assertEqual(len(self.due()), 1)

    def test_codex_context_includes_cache_once_and_latest_usage_wins(self):
        self.transcript('codex', [self.usage(100000, 90000)])
        self.assertEqual(self.due(), [])
        self.transcript('codex', [self.usage(300000, 90000)])
        self.assertEqual(len(self.due()), 1)
        self.transcript('codex', [self.usage(300000), self.usage(100000, rid='two')])
        self.assertEqual(self.due(), [])

    def test_claude_cache_and_deduplicated_turns(self):
        def response(mid, fresh, read, write):
            return {'type': 'assistant', 'message': {'id': mid, 'model': 'fixture-model',
                'content': [{'type': 'text', 'text': 'TRANSCRIPT MUST NOT BE COPIED'}],
                'usage': {'input_tokens': fresh, 'cache_read_input_tokens': read,
                          'cache_creation_input_tokens': write}}}
        self.transcript('claude', [response('one', 10000, 270000, 20000)] * 60)
        self.assertEqual(len(self.due()), 1)
        self.transcript('claude', [response('one', 10000, 80000, 10000)] * 60)
        self.assertEqual(self.due(), [])
        self.transcript('claude', [record for i in range(50) for record in
            ({'type': 'user', 'message': {'content': 'fixture prompt'}}, response(str(i), 1, 0, 0))])
        self.assertEqual(len(self.due()), 1)

    def test_codex_counter_only_and_duplicate_notifications(self):
        def counter(i):
            return {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
                'last_token_usage': {'input_tokens': 100000, 'cached_input_tokens': 90000},
                'total_token_usage': {'input_tokens': i * 100000}}}}
        self.transcript('codex', [counter(1)] * 60)
        self.assertEqual(self.due(), [])
        self.transcript('codex', [counter(i) for i in range(1, 51)])
        self.assertEqual(self.due(), [])
        self.transcript('codex', [self.usage(100000, rid=str(i)) for i in range(50)])
        self.assertEqual(self.due(), [])

    def test_incremental_context_and_cache_keep_only_numeric_metadata(self):
        path = self.transcript('codex', [self.usage(300000),
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant',
                'content': [{'type': 'output_text', 'text': 'PRIVATE-FIXTURE-TEXT'}]}}])
        self.assertEqual(len(self.due()), 1)
        with path.open('a') as stream:
            stream.write(json.dumps({'timestamp': '2026-01-01T00:01:00.000Z',
                                    **self.usage(100000, rid='two')}, separators=(',', ':')) + '\n')
        self.assertEqual(self.due(), [])
        caches = list((self.r / '.brain/spend/files').glob('*.json'))
        self.assertTrue(caches)
        for cache in caches:
            text = cache.read_text()
            self.assertNotIn('PRIVATE-FIXTURE-TEXT', text)
            self.assertNotIn(self.sid, text)
            self.assertNotIn(str(self.tmp / 'scratch'), text)

    def test_coordinator_session_does_not_override_subscription_privacy(self):
        self.transcript('codex', [self.usage(300000)])
        path = self.cfg / 'subscriptions.json'
        config = json.loads(path.read_text())
        config['subscriptions'][0]['brains'] = ['other-brain']
        path.write_text(json.dumps(config))
        self.assertEqual(self.due(), [])

    def test_current_session_only_and_custom_thresholds(self):
        self.transcript('codex', [self.usage(300000)])
        self.bind([self.sid, '22222222-bbbb'])
        self.assertEqual(self.due(), [])
        self.bind()
        self.b(self.r, 'set', 'app', 'rotate_tokens=310000')
        self.assertEqual(self.due(), [])
        self.b(self.r, 'set', 'app', 'rotate_tokens=bad')
        self.assertEqual(len(self.due()), 1)

    def test_handover_preserves_all_file_sources_and_no_capabilities_or_transcript(self):
        for name, body in [('status.md', 'Pending ask: fixture ask'), ('decisions.md', 'fixture decision'),
                           ('backlog.md', '- deferred fixture item'), ('inbox.md', '- unanswered fixture ask')]:
            (self.node / name).write_text(body)
        m = SourceFileLoader('rotation_fixture', str(CORE / 'bin/brain')).load_module()
        inst = m.Instance(self.r)
        n = inst.node('app')
        rows = [{'Job': 'job/a', 'Started': '2026-01-01T00:00Z', 'Ended': ''},
                {'Job': 'job/b', 'Started': '2026-01-02T00:00Z', 'Ended': ''}]
        m.write_jobs(inst, n, rows)
        hb = m.handback_file(n, rows[0]); hb.parent.mkdir(exist_ok=True)
        hb.write_text(json.dumps({'job': 'job/a', 'started': rows[0]['Started'], 'state': 'blocked',
                                 'report': 'fixture report', 'need': 'fixture need'}))
        self.b(self.r, 'schedule', 'add', 'tick', '--node', 'app', '--when', 'hourly', '--brief', 'fixture tick')
        self.b(self.r, 'schedule', 'bind', 'tick', 'fixture-host-task')
        self.b(self.r, 'set', 'app', 'wake=https://example.invalid/SECRET-CAPABILITY')
        wake = self.r / '.brain/wake/app'; wake.mkdir(parents=True)
        (wake / ('a' * 64 + '.pending')).touch()
        self.transcript('codex', [self.usage(300000), {'type': 'response_item', 'payload': {
            'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text',
                'text': 'TRANSCRIPT MUST NOT BE COPIED'}]}}])
        out = self.b(self.r, 'rotate', 'app')
        text = (self.node / 'handover.md').read_text()
        for value in ('fixture ask', 'fixture decision', 'deferred fixture item', 'unanswered fixture ask',
                      'job/a', 'job/b', 'fixture report', 'fixture need', 'fixture tick', 'Pending wakes: 1'):
            self.assertIn(value, text)
        local = json.loads((self.r / '.brain/rotation/app.json').read_text())
        self.assertEqual(local['coordinator'], 'host-thread')
        self.assertEqual(local['coordinator_sessions'], [self.sid])
        self.assertEqual(local['schedules'], {'tick': 'fixture-host-task'})
        self.assertEqual(local['pending_wakes'], ['a' * 64])
        self.assertNotIn('a' * 64, text)
        self.assertNotIn('fixture-host-task', text)
        self.assertNotIn('SECRET-CAPABILITY', json.dumps(local))
        self.assertNotIn('SECRET-CAPABILITY', text)
        self.assertNotIn('TRANSCRIPT MUST NOT BE COPIED', text)
        self.assertIn('Launch a fresh coordinator', out)
        self.assertIn('settle', out)
        self.assertIn('backlog.md', [p.name for p in self.node.iterdir()])
        self.assertNotIn('rotation/app.json', self.git('ls-files', cwd=self.r))

    def test_rotation_refuses_unreadable_handback_without_overwriting_packet(self):
        m = SourceFileLoader('rotation_refusal', str(CORE / 'bin/brain')).load_module()
        inst = m.Instance(self.r); n = inst.node('app')
        row = {'Job': 'job/a', 'Started': '2026-01-01T00:00Z', 'Ended': ''}
        m.write_jobs(inst, n, [row])
        path = m.handback_file(n, row); path.parent.mkdir(exist_ok=True); path.write_text('broken')
        packet = self.node / 'handover.md'; packet.write_text('prior packet')
        self.assertIn('refused', self.b(self.r, 'rotate', 'app', ok=False))
        self.assertEqual(packet.read_text(), 'prior packet')

    def test_conversation_turns_exclude_sixty_tool_responses_in_both_runtimes(self):
        for runtime in ('claude', 'codex'):
            with self.subTest(runtime=runtime):
                if runtime == 'claude':
                    prompt = {'type': 'user', 'message': {'content': 'fixture prompt'}}
                    responses = [{'type': 'assistant', 'message': {'id': str(i), 'model': 'fixture-model',
                        'usage': {'input_tokens': 20000}, 'content': [{'type': 'tool_use', 'id': 'tool-' + str(i)}]}}
                        for i in range(60)]
                    excluded = [
                        {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'content': 'fixture result'}]}},
                        *[{'type': 'user', flag: True, 'message': {'content': 'fixture internal'}}
                          for flag in ('isMeta', 'isCompactSummary', 'isSidechain')]]
                    self.transcript(runtime, [prompt, *responses, *excluded * 15])
                    self.assertEqual(self.due(), [])
                    self.transcript(runtime, [r for i in range(50) for r in (prompt, responses[i])])
                else:
                    # Remove Claude's matching file so a second runtime cannot mask the result.
                    for path in self.claude.rglob('*.jsonl'):
                        path.unlink()
                    prompt = {'type': 'turn_context', 'payload': {'model': 'fixture-model'}}
                    responses = [self.usage(20000, rid=str(i)) for i in range(60)]
                    self.transcript(runtime, [prompt, *responses])
                    self.assertEqual(self.due(), [])
                    self.transcript(runtime, [r for i in range(50) for r in (prompt, responses[i])])
                self.assertEqual(len(self.due()), 1)

    def test_codex_user_message_fallback_counts_inputs_without_turn_context(self):
        prompt = {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'fixture input'}}
        self.transcript('codex', [prompt, *[self.usage(20000, rid=str(i)) for i in range(60)]])
        self.assertEqual(self.due(), [])
        self.transcript('codex', [r for i in range(50) for r in (prompt, self.usage(20000, rid=str(i)))])
        self.assertEqual(len(self.due()), 1)

    def test_handover_contains_queued_host_question_even_if_inbox_is_busy(self):
        m = SourceFileLoader('rotation_queue', str(CORE / 'bin/brain')).load_module()
        n = m.Instance(self.r).node('app')
        queue = m.hook_question_queue(n); queue.mkdir(parents=True)
        line = '- QUEUED-FIXTURE-ASK <!-- host-question:fixture -->'
        (queue / 'fixture.json').write_text(json.dumps({'line': line, 'marker': '<!-- host-question:fixture -->'}))
        # The returned inbox view also covers a question that could not be flushed.
        with mock.patch.object(m, 'load', return_value=m.Instance(self.r)), \
             mock.patch.object(m, 'inbox_append', side_effect=OSError('fixture busy')):
            m.cmd_rotate(type('Args', (), {'id': 'app'})())
        self.assertIn(line, (self.node / 'handover.md').read_text())

    def test_coordinator_measure_opens_only_selected_names_and_memoizes_metadata(self):
        m = SourceFileLoader('rotation_selection', str(CORE / 'bin/brain')).load_module()
        inst = m.Instance(self.r); n = inst.node('app')
        for i in range(30):
            self.transcript('codex', [self.usage(300000)], sid='unrelated-' + str(i))
        evidence = ({'jobs': [], 'worktrees': {}, 'handbacks': {}, 'inbox': []}, None, '')
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(m, 'codex_meta', wraps=m.codex_meta) as meta:
            m.due_items(inst, n, evidence=evidence, include_stale=False)
            self.assertEqual(meta.call_count, 0)
            self.transcript('codex', [self.usage(300000)])
            for _ in range(3):
                items = m.due_items(inst, n, evidence=evidence, include_stale=False)
                self.assertTrue(any(item['kind'] == 'rotate' for item in items))
            self.assertEqual(m.coordinator_measure(inst, n)['context'], 300000)
            self.assertLessEqual(meta.call_count, 1)

    def test_claude_file_stem_drives_both_spend_and_rotation(self):
        self.transcript('claude', [
            {'type': 'user', 'cwd': str(self.tmp / 'scratch'), 'sessionId': '99999999-old',
             'message': {'content': 'fixture copied leading record'}},
            {'type': 'assistant', 'message': {'id': 'one', 'model': 'fixture-model',
                                            'usage': {'input_tokens': 300000}}}])
        self.assertEqual(len(self.due()), 1)
        self.assertEqual(len(json.loads(self.b(self.r, 'spend', 'app', '--json'))['sessions']), 1)

    def test_rebound_coordinator_needs_its_runtime_session_before_rotation(self):
        self.transcript('codex', [self.usage(300000)])
        self.b(self.r, 'rotate', 'app')
        self.b(self.r, 'set', 'app', 'coordinator=new-host-thread')
        due = self.due()
        self.assertEqual(len(due), 1)
        self.assertIn("append the new coordinator's runtime session id", due[0]['text'])
        self.assertNotIn('run brain rotate', due[0]['text'])
        # A packet refresh must not erase the outstanding binding action.
        self.b(self.r, 'rotate', 'app')
        self.assertIn("append the new coordinator's runtime session id", self.due()[0]['text'])
        self.b(self.r, 'set', 'app', 'coordinator_sessions=22222222-new')
        self.assertEqual(self.due(), [])

    def test_rotation_retains_all_previous_coordinators(self):
        self.b(self.r, 'rotate', 'app')
        self.b(self.r, 'set', 'app', 'coordinator=second-host-thread')
        self.b(self.r, 'rotate', 'app')
        self.b(self.r, 'set', 'app', 'coordinator=third-host-thread')
        self.b(self.r, 'rotate', 'app')
        sidecar = json.loads((self.r / '.brain/rotation/app.json').read_text())
        self.assertEqual(sidecar['coordinator'], 'third-host-thread')
        self.assertEqual([old['coordinator'] for old in sidecar['previous']], ['host-thread', 'second-host-thread'])

    def test_session_selection_uses_last_duplicate_for_rotation(self):
        self.transcript('codex', [self.usage(300000)])
        self.transcript('codex', [self.usage(100000)], sid='22222222-other')
        self.b(self.r, 'set', 'app', 'coordinator_sessions:=' + self.sid + ',22222222-other,' + self.sid)
        self.assertEqual(len(self.due()), 1)

    def test_backlog_commit_is_in_status_freshness_evidence(self):
        (self.node / 'backlog.md').write_text('- Deferred fixture task.\n')
        self.git('add', 'nodes/app/backlog.md', cwd=self.r)
        self.git('commit', '-qm', 'fixture: backlog changed', cwd=self.r)
        head = self.git('rev-parse', 'HEAD', cwd=self.r).strip()
        m = SourceFileLoader('rotation_backlog_evidence', str(CORE / 'bin/brain')).load_module()
        inst = m.Instance(self.r)
        self.assertEqual(m.due_evidence(inst, inst.node('app'))[2].split()[0], head)
