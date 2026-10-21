"""Coordinator rotation uses accounting numbers and files, never transcript text."""
import json
from core_loader import SourceFileLoader
from scenario import Scenario, CORE


class Rotation(Scenario):
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

    def test_codex_context_includes_cache_once_and_latest_usage_wins(self):
        self.transcript('codex', [self.usage(100000, 90000)])
        self.assertEqual(self.due(), [])
        self.transcript('codex', [self.usage(160000, 90000)])
        self.assertEqual(len(self.due()), 1)
        self.transcript('codex', [self.usage(160000), self.usage(100000, rid='two')])
        self.assertEqual(self.due(), [])

    def test_claude_cache_and_deduplicated_turns(self):
        def response(mid, fresh, read, write):
            return {'type': 'assistant', 'message': {'id': mid, 'model': 'fixture-model',
                'content': [{'type': 'text', 'text': 'TRANSCRIPT MUST NOT BE COPIED'}],
                'usage': {'input_tokens': fresh, 'cache_read_input_tokens': read,
                          'cache_creation_input_tokens': write}}}
        self.transcript('claude', [response('one', 10000, 130000, 20000)] * 60)
        self.assertEqual(len(self.due()), 1)
        self.transcript('claude', [response('one', 10000, 80000, 10000)] * 60)
        self.assertEqual(self.due(), [])
        self.transcript('claude', [response(str(i), 1, 0, 0) for i in range(50)])
        self.assertEqual(len(self.due()), 1)

    def test_codex_counter_only_and_duplicate_notifications(self):
        def counter(i):
            return {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
                'last_token_usage': {'input_tokens': 100000, 'cached_input_tokens': 90000},
                'total_token_usage': {'input_tokens': i * 100000}}}}
        self.transcript('codex', [counter(1)] * 60)
        self.assertEqual(self.due(), [])
        self.transcript('codex', [counter(i) for i in range(1, 51)])
        self.assertEqual(len(self.due()), 1)
        self.transcript('codex', [self.usage(100000, rid=str(i)) for i in range(50)])
        self.assertEqual(len(self.due()), 1)

    def test_incremental_context_and_cache_keep_only_numeric_metadata(self):
        path = self.transcript('codex', [self.usage(160000),
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
        self.transcript('codex', [self.usage(160000)])
        path = self.cfg / 'subscriptions.json'
        config = json.loads(path.read_text())
        config['subscriptions'][0]['brains'] = ['other-brain']
        path.write_text(json.dumps(config))
        self.assertEqual(self.due(), [])

    def test_current_session_only_and_custom_thresholds(self):
        self.transcript('codex', [self.usage(160000)])
        self.bind([self.sid, '22222222-bbbb'])
        self.assertEqual(self.due(), [])
        self.bind()
        self.b(self.r, 'set', 'app', 'rotate_tokens=170000')
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
        self.transcript('codex', [self.usage(160000), {'type': 'response_item', 'payload': {
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
