"""Native settlement consumes fresh exact host evidence; no real host is called."""
import json
from unittest import mock

import core_loader
from scenario import CORE, Scenario


class HostLifecycle(Scenario):
    def setUp(self):
        super().setUp()
        self.root = self.init('oak')
        self.m = core_loader.SourceFileLoader('host_lifecycle_fixture', str(CORE / 'bin/brain')).load_module()
        self.inst = self.m.Instance(self.root)
        self.inst.local_of('root')
        self.n = self.inst.node('root')
        self.closed = {'Job': 'job/palette', 'Started': '2026-01-15T09:00Z',
                       'Ended': '2026-01-15T10:00Z', 'Outcome': 'closed'}
        self.opened = {'Job': 'job/brush', 'Started': '2026-01-15T09:01Z', 'Ended': ''}
        self.m.write_jobs(self.inst, self.n, [self.closed, self.opened])
        self.n['local'].update(coordinator='fixture-parent', host_project='fixture-host', home_project='fixture-home',
                               workers={self.m.job_key(self.closed): 'fixture-worker', self.m.job_key(self.opened): 'fixture-live'})
        self.inst.save_local()
        self.threads = {identity: {'threadId': identity, 'status': state, 'settled': False,
                                  'parentThreadId': parent, 'activeRunId': None, 'pendingRequestCount': 0}
                        for identity, state, parent in (
                            ('fixture-parent', 'running', None), ('fixture-worker', 'completed', None),
                            ('fixture-child', 'completed', 'fixture-worker'), ('fixture-live', 'running', None))}
        self.calls, self.pending, self.reads = [], {}, {}
        self.on_read = lambda identity: None

    def rpc(self, tool, args):
        self.calls.append((tool, dict(args)))
        if tool == 't3_thread_list':
            return {'projectId': args['projectId'], 'currentThreadId': 'fixture-parent',
                    'threads': [dict(t) for t in self.threads.values()], 'total': len(self.threads), 'nextCursor': None}
        identity = args['threadId']
        if tool == 't3_thread_read':
            self.reads[identity] = self.reads.get(identity, 0) + 1
            self.on_read(identity)
            return {'thread': dict(self.threads[identity]), 'items': []}
        if tool == 't3_pending_request_list':
            return {'requestIds': self.pending.get(identity, [])}
        if tool == 't3_thread_organize':
            self.assertEqual(set(args), {'threadId', 'action'})
            self.assertEqual(args['action'], 'settle')
            self.threads[identity]['settled'] = True
            return {'settled': True}
        self.fail('unexpected native tool: ' + tool)

    def settled_calls(self):
        return [args['threadId'] for tool, args in self.calls if tool == 't3_thread_organize']

    def test_closed_worker_and_nested_child_settle_and_publish_once(self):
        result = self.m.settle_closed_jobs(self.inst, self.n, self.rpc)
        self.assertEqual(set(result['settled_threads']), {'fixture-worker', 'fixture-child'})
        self.assertEqual(self.settled_calls(), ['fixture-child', 'fixture-worker'])
        self.assertEqual(result['settled_jobs'], [self.m.job_key(self.closed)])
        row = self.m.read_jobs(self.n)[0]
        self.assertTrue(row['Settled'])
        evidence = json.loads((self.n['dir'] / 'job-settlements' / (self.m.job_key(row) + '.json')).read_text())
        self.assertEqual(evidence['source'], 'native-host-rpc')
        count = len(self.calls)
        self.assertEqual(self.m.settle_closed_jobs(self.inst, self.n, self.rpc)['settled_jobs'], [])
        self.assertEqual(len(self.calls), count)

    def test_live_pending_or_unknown_child_keeps_entire_job_unsettled(self):
        for state, requests in (('running', []), ('waiting', []), ('completed', ['fixture-request']), ('unknown', [])):
            with self.subTest(state=state, requests=requests):
                self.calls.clear()
                self.threads['fixture-child']['status'] = state
                self.pending['fixture-child'] = requests
                result = self.m.settle_closed_jobs(self.inst, self.n, self.rpc)
                self.assertEqual(self.settled_calls(), [])
                self.assertEqual(result['settled_jobs'], [])
                self.assertTrue(result['retained'])
                self.assertFalse(self.m.read_jobs(self.n)[0].get('Settled'))

    def test_changed_local_ownership_is_rechecked_before_mutation(self):
        self.threads.pop('fixture-child')
        def reopen(identity):
            if identity == 'fixture-worker' and self.reads[identity] == 2:
                rows = self.m.read_jobs(self.n)
                rows[0]['Ended'] = ''
                self.m.write_jobs(self.inst, self.n, rows)
        self.on_read = reopen
        result = self.m.settle_closed_jobs(self.inst, self.n, self.rpc)
        self.assertEqual(self.settled_calls(), [])
        self.assertEqual(result['settled_jobs'], [])
        self.assertTrue(result['retained'])

    def test_incomplete_inventory_refuses_without_any_settle_call(self):
        original = self.rpc
        def incomplete(tool, args):
            result = original(tool, args)
            if tool == 't3_thread_list':
                result['total'] += 1
            return result
        with self.assertRaisesRegex(ValueError, 'exhaustion'):
            self.m.settle_closed_jobs(self.inst, self.n, incomplete)
        self.assertEqual(self.settled_calls(), [])

    def test_unattributed_threads_never_create_false_complete_attestation(self):
        self.threads['fixture-unknown'] = {'threadId': 'fixture-unknown', 'status': 'completed',
                                         'settled': False, 'parentThreadId': None}
        result = self.m.settle_closed_jobs(self.inst, self.n, self.rpc)
        self.assertEqual(result['unknown_threads'], ['fixture-unknown'])
        self.assertNotIn('fixture-unknown', self.settled_calls())
        self.assertEqual(result['settled_jobs'], [])
        self.assertFalse(self.m.read_jobs(self.n)[0].get('Settled'))

    def test_train_hook_scopes_actions_to_exact_closed_job_keys(self):
        with mock.patch.object(self.m._owners['settle_closed_jobs']._m_rpc, 'call', side_effect=self.rpc):
            result = self.m.settle_landed_jobs(self.inst, self.n, [self.m.job_key(self.closed)])
        self.assertEqual(result['settled'], [self.m.job_key(self.closed)])
        self.assertEqual(result['pending'], [])
        self.assertEqual(result['errors'], [])
        self.assertNotIn('fixture-live', self.settled_calls())

    def test_train_hook_preserves_pending_and_reports_errors_without_calls(self):
        with mock.patch.object(self.m._owners['settle_closed_jobs']._m_rpc, 'call', side_effect=self.rpc):
            result = self.m.settle_landed_jobs(self.inst, self.n, [self.m.job_key(self.opened)])
        self.assertTrue(result['pending'])
        self.assertTrue(result['errors'])
        self.assertEqual(self.calls, [])
