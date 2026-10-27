"""Native restart fences and recovery, using a fake host and a real ledger."""
import json
import os
from unittest import mock

import core_loader
from scenario import CORE, Scenario


class WorkerLifecycle(Scenario):
    def setUp(self):
        super().setUp()
        self.root = self.init('oak')
        self.git('checkout', '-b', 'job/palette', cwd=self.root)
        self.m = core_loader.SourceFileLoader('restart_fixture', str(CORE / 'bin/brain')).load_module()
        self.jobs = self.m._owners['restart_worker']._m_jobs
        self.inst = self.m.Instance(self.root)
        self.inst.local_of('root')
        self.n = self.inst.node('root')
        self.row = {'Job': 'job/palette', 'Started': '2026-01-15T09:00Z', 'Ended': ''}
        self.m.write_jobs(self.inst, self.n, [self.row])
        self.key = self.m.job_key(self.row)
        self.n['local'].update(coordinator='parent', host_project='project', workers={self.key: 'old'},
            worktrees={'job/palette': {'path': str(self.root), 'port': 4100, 'start': self.git('rev-parse', 'HEAD', cwd=self.root).strip(),
                       'brief_inputs': {'job': self.key, 'task': 'Draw'}}})
        self.inst.save_local()
        self.usage = {'processed': 800, 'compactions': 3, 'active_minutes': 4, 'sessions': 1, 'source': 'host'}
        self.threads = {'old': self.thread('old', 'running', 'old-run'),
                        'parent': self.thread('parent', 'running', 'parent-run')}
        self.calls, self.pending = [], []
        self.launch_unknown = False
        self.owner = None

    def thread(self, identity, status, run=None, parent=None):
        return {'threadId': identity, 'status': status, 'activeRunId': run, 'pendingRequestCount': 0,
                'parentThreadId': parent, 'worktreePath': str(self.root), 'branch': 'job/palette',
                'providerInstanceId': 'codex', 'model': 'fixture-model', 'settled': False}

    def rpc(self, name, args):
        self.calls.append((name, dict(args)))
        if name == 't3_thread_list':
            return {'projectId': 'project', 'currentThreadId': 'parent', 'threads': list(self.threads.values()),
                    'total': len(self.threads), 'nextCursor': None}
        if name == 't3_thread_read':
            return {'thread': dict(self.threads[args['threadId']]), 'items': []}
        if name == 't3_pending_request_list':
            return {'requestIds': self.pending}
        if name == 't3_thread_interrupt':
            self.assertEqual(args['runId'], 'old-run')
            self.threads['old'].update(status='interrupted', activeRunId=None)
            return {'threadId': 'old', 'runId': 'old-run', 'status': 'interrupt_requested'}
        if name == 't3_thread_launch':
            if self.launch_unknown:
                raise self.m.HostRPCError('fixture lost launch response')
            self.threads['new'] = self.thread('new', 'running', 'new-run')
            return {'threadId': 'new', 'runId': 'new-run', 'projectId': 'project',
                    'status': 'running', 'modelSelection': args['modelSelection']}
        self.fail('unexpected action ' + name)

    def tick(self):
        # The brief generator is independently exercised by native scenario tests;
        # host orchestration tests keep routing deterministic without a live account.
        self.owner = self.m._owners['restart_worker']
        with mock.patch.object(self.owner._m_spend, 'live_usage', side_effect=lambda *args: dict(self.usage)), \
                mock.patch.object(self.owner, 'restart_brief', return_value=('Draw', {
                    'instanceId': 'codex', 'model': 'fixture-model', 'options': {'reasoningEffort': 'high'}})):
            return self.m.restart_worker(self.inst, self.n, self.key, rpc=self.rpc)

    def actions(self, name):
        return [args for tool, args in self.calls if tool == name]

    def test_stop_then_launch_bind_and_ack_captured_usage_once(self):
        self.assertEqual(self.tick()['state'], 'stopping')
        self.assertEqual(self.inst.local_of('root')['workers'][self.key], 'old')
        self.assertEqual(self.tick()['state'], 'done')
        self.assertEqual(self.m.Instance(self.root).local_of('root')['workers'][self.key], 'new')
        self.assertEqual(len(self.actions('t3_thread_interrupt')), 1)
        self.assertEqual(len(self.actions('t3_thread_launch')), 1)
        self.assertEqual(self.tick()['state'], 'idle')
        self.assertEqual(len(self.actions('t3_thread_launch')), 1)
        receipt = self.m.lifecycle_read(self.inst)['usage'][self.jobs.digest(['root', self.key])]
        self.assertEqual(receipt['baseline'], {'processed': 800, 'compactions': 3, 'active_minutes': 4})

    def test_restart_receipt_from_another_boot_never_launches_replacement(self):
        self.assertEqual(self.tick()['state'], 'stopping')
        self.calls.clear()
        with mock.patch.object(self.owner._m_lifecycle_identity, 'lifecycle_host_identity', return_value='foreign-boot'):
            result = self.tick()
        self.assertEqual(result['state'], 'pending')
        self.assertIn('host changed', result['reason'])
        self.assertEqual(self.actions('t3_thread_launch'), [])
        self.assertEqual(self.actions('t3_thread_interrupt'), [])

    def test_boot_changes_during_inventory_prevent_interrupt(self):
        original = self.rpc
        host = self.m._owners['lifecycle_host_identity']
        identity = host.lifecycle_host_identity()
        changed = [False]
        def rpc(name, args):
            result = original(name, args)
            if name == 't3_thread_list':
                changed[0] = True
            return result
        with mock.patch.object(host, 'lifecycle_host_identity', side_effect=lambda: 'foreign' if changed[0] else identity), \
                mock.patch.object(self, 'rpc', side_effect=rpc):
            result = self.tick()
        self.assertEqual(result['state'], 'pending')
        self.assertEqual(self.actions('t3_thread_interrupt'), [])
        self.assertEqual(self.actions('t3_thread_launch'), [])

    def test_pending_request_or_live_child_prevents_even_interrupt(self):
        self.pending = ['request']
        self.assertEqual(self.tick()['state'], 'pending')
        self.pending = []
        self.threads['child'] = self.thread('child', 'running', 'child-run', 'old')
        self.assertEqual(self.tick()['state'], 'pending')
        self.assertEqual(self.actions('t3_thread_interrupt'), [])
        self.assertEqual(self.actions('t3_thread_launch'), [])

    def test_unknown_launch_is_not_repeated_or_acknowledged(self):
        self.tick()
        self.launch_unknown = True
        self.assertEqual(self.tick()['state'], 'pending')
        self.assertEqual(self.tick()['state'], 'pending')
        self.assertEqual(len(self.actions('t3_thread_launch')), 1)
        self.assertTrue(self.m.lifecycle_read(self.inst)['usage'][self.jobs.digest(['root', self.key])]['episode'])

    def test_closed_or_changed_binding_never_relaunches(self):
        self.tick()
        local = self.m.Instance(self.root)
        local.local_of('root')['workers'][self.key] = 'foreign'
        local.save_local()
        self.assertEqual(self.tick()['state'], 'pending')
        self.assertEqual(self.actions('t3_thread_launch'), [])
        self.row['Ended'] = '2026-01-15T10:00Z'
        self.m.write_jobs(self.inst, self.n, [self.row])
        self.assertEqual(self.tick()['state'], 'idle')

    def test_unknown_usage_preserves_worker_and_blocking_episode(self):
        self.usage['active_minutes'] = None
        self.assertEqual(self.tick()['state'], 'pending')
        self.assertEqual(self.actions('t3_thread_interrupt'), [])
        self.assertEqual(self.actions('t3_thread_launch'), [])

    def test_fresh_restart_charges_replacement_startup(self):
        self.tick()
        original = self.rpc
        def startup(name, args):
            result = original(name, args)
            if name == 't3_thread_launch':
                self.usage.update(processed=900, compactions=4, active_minutes=5)
            return result
        self.rpc = startup
        self.assertEqual(self.tick()['state'], 'done')
        receipt = self.m.lifecycle_read(self.inst)['usage'][self.jobs.digest(['root', self.key])]
        self.assertEqual(receipt['baseline']['processed'], 800)
        self.assertEqual(self.tick()['state'], 'idle')

    def test_local_binding_crash_recovers_without_launch_or_counter_reread(self):
        self.tick()
        owner = self.m._owners['restart_worker']
        publish = owner._m_machine.machine_publish
        failed = []
        def crash(inst, path, text):
            data = json.loads(text)
            if data.get('phase') == 'bound' and not failed:
                failed.append(True)
                raise OSError('fixture failure after binding')
            return publish(inst, path, text)
        with mock.patch.object(owner._m_machine, 'machine_publish', side_effect=crash):
            self.assertEqual(self.tick()['state'], 'pending')
        self.usage.update(processed=900, compactions=4, active_minutes=5)
        self.assertEqual(self.tick()['state'], 'done')
        self.assertEqual(len(self.actions('t3_thread_launch')), 1)
        self.assertEqual(self.m.lifecycle_read(self.inst)['usage'][self.jobs.digest(['root', self.key])]['baseline']['processed'], 800)

    def test_changed_worktree_or_pending_handback_never_stops(self):
        self.git('checkout', 'main', cwd=self.root)
        self.assertEqual(self.tick()['state'], 'pending')
        self.assertEqual(self.actions('t3_thread_interrupt'), [])
        self.assertEqual(self.actions('t3_thread_launch'), [])

    def test_child_inventory_is_first_and_incomplete_inventory_refuses(self):
        original = self.rpc
        def incomplete(name, args):
            result = original(name, args)
            if name == 't3_thread_list':
                result['total'] += 1
            return result
        self.rpc = incomplete
        self.assertEqual(self.tick()['state'], 'pending')
        self.assertEqual(self.calls[0][0], 't3_thread_list')
        self.assertEqual(self.actions('t3_thread_interrupt'), [])

    def test_maintenance_advances_existing_need_without_file_watch_host_calls(self):
        owner = self.m._owners['restart_worker']
        with mock.patch.object(owner._m_spend, 'live_usage', return_value=self.usage), \
                mock.patch.object(owner, 'restart_brief', return_value=('Draw', {
                    'instanceId': 'codex', 'model': 'fixture-model', 'options': {'reasoningEffort': 'high'}})):
            result = self.m.maintain_lifecycle(self.inst, self.n, rpc=self.rpc)
        self.assertEqual(result['restarts'][0]['state'], 'stopping')
        self.assertEqual(result['settlement']['settled_jobs'], [])

    def test_lost_launch_response_recovers_exact_first_prompt_once(self):
        self.tick()
        original = self.rpc
        prompts = {}
        def lost(name, args):
            result = original(name, args)
            if name == 't3_thread_launch':
                prompts['new'] = args['message']
                raise self.m.HostRPCError('fixture launch response lost after success')
            if name == 't3_thread_read' and args.get('afterPosition') == 0 and args['threadId'] in prompts:
                result['items'] = [{'createdBy': 'user', 'text': prompts[args['threadId']], 'textTruncated': False}]
            return result
        self.rpc = lost
        self.assertEqual(self.tick()['state'], 'pending')
        self.assertEqual(self.tick()['state'], 'done')
        self.assertEqual(len(self.actions('t3_thread_launch')), 1)

    def test_replacement_question_is_not_acknowledged_or_launched_twice(self):
        self.tick()
        original = self.rpc
        def question(name, args):
            result = original(name, args)
            if name == 't3_thread_launch':
                self.threads['new']['pendingRequestCount'] = 1
            return result
        self.rpc = question
        self.assertEqual(self.tick()['state'], 'pending')
        self.assertEqual(self.m.Instance(self.root).local_of('root')['workers'][self.key], 'old')
        self.threads['new']['pendingRequestCount'] = 0
        self.assertEqual(self.tick()['state'], 'done')
        self.assertEqual(len(self.actions('t3_thread_launch')), 1)

    def test_native_resume_brief_defers_baseline_until_replacement_binding(self):
        owner = self.m._owners['restart_worker']
        def route(inst, n, args, *, extra):
            self.assertEqual(extra, [])  # No shared context or output paths in this restart.
            self.assertTrue(args._defer_restart_ack)
            self.assertEqual(args.branch, self.row['Job'])
            args.model, args.job_class, args.effort = 'fixture-model', 'build', 'high'
            args.selection_line = 'modelSelection: ' + json.dumps({'instanceId': 'codex', 'model': 'fixture-model',
                'options': {'reasoningEffort': args.effort}})
            return 'codex', 'Fixture account snapshot'
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(owner._m_spend, 'live_usage', return_value=self.usage), \
                mock.patch.object(owner._m_briefs._m_knowledge, 'worker_route', autospec=True, side_effect=route) as routed, \
                mock.patch.object(owner._m_reminders, 'acknowledge_usage_restart', side_effect=AssertionError('early acknowledgement')):
            self.m.usage_restart_item(self.inst, self.n, self.row)
            before = self.m.lifecycle_read(self.inst)
            before_usage = before['usage'][self.jobs.digest(['root', self.key])]
            text, selection = self.m.restart_brief(self.inst, self.n, self.row)
        routed.assert_called_once()
        self.assertIn('Draw', text)
        self.assertEqual(selection['model'], 'fixture-model')
        self.assertEqual(selection['options'], {'reasoningEffort': 'high'})
        self.assertEqual(self.m.lifecycle_read(self.inst)['usage'][self.jobs.digest(['root', self.key])], before_usage)
        self.assertEqual(self.m.Instance(self.root).local_of('root')['workers'][self.key], 'old')
        self.assertTrue(self.m.lifecycle_read(self.inst)['usage'][self.jobs.digest(['root', self.key])]['episode'])
        self.assertEqual(self.m.lifecycle_read(self.inst), before)

    def initial_tick(self):
        owner = self.m._owners['launch_worker']
        with mock.patch.object(owner, 'restart_brief', return_value=('Draw', {
                'instanceId': 'codex', 'model': 'fixture-model', 'options': {'reasoningEffort': 'high'}})):
            return self.m.launch_worker(self.inst, self.n, self.key, rpc=self.rpc)

    def unbound(self):
        self.n['local']['workers'].clear()
        self.inst.save_local()
        self.threads.pop('old')

    def test_initial_launch_binds_once_without_resetting_lifetime_budget(self):
        self.unbound()
        self.assertEqual(self.initial_tick()['state'], 'done')
        self.assertEqual(self.m.Instance(self.root).local_of('root')['workers'][self.key], 'new')
        self.assertEqual(self.initial_tick()['state'], 'idle')
        self.assertEqual(len(self.actions('t3_thread_launch')), 1)
        self.assertEqual(self.m.lifecycle_read(self.inst)['usage'], {})

    def test_initial_launch_preserves_unknown_worker_in_same_checkout(self):
        self.unbound()
        self.threads['foreign'] = self.thread('foreign', 'running', 'foreign-run')
        self.assertEqual(self.initial_tick()['state'], 'pending')
        self.assertEqual(self.actions('t3_thread_launch'), [])

    def test_initial_launch_unknown_outcome_never_repeats(self):
        self.unbound()
        self.launch_unknown = True
        self.assertEqual(self.initial_tick()['state'], 'pending')
        self.assertEqual(self.initial_tick()['state'], 'pending')
        self.assertEqual(len(self.actions('t3_thread_launch')), 1)
