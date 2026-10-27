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

    def test_restart_binds_replacement_that_fails_before_confirmation(self):
        self.assertEqual(self.tick()['state'], 'stopping')
        original = self.rpc
        def rpc(name, args):
            result = original(name, args)
            if name == 't3_thread_launch':
                self.threads['new'].update(status='failed', activeRunId=None)
            return result
        with mock.patch.object(self, 'rpc', side_effect=rpc):
            self.assertEqual(self.tick()['state'], 'done')
        self.assertEqual(self.m.Instance(self.root).local_of('root')['workers'][self.key], 'new')
        self.assertEqual(len(self.actions('t3_thread_launch')), 1)

    def test_initial_launch_binds_worker_that_fails_before_confirmation(self):
        self.unbound()
        original = self.rpc
        def rpc(name, args):
            result = original(name, args)
            if name == 't3_thread_launch':
                self.threads['new'].update(status='failed', activeRunId=None)
            return result
        with mock.patch.object(self, 'rpc', side_effect=rpc):
            self.assertEqual(self.initial_tick()['state'], 'done')
        self.assertEqual(self.m.Instance(self.root).local_of('root')['workers'][self.key], 'new')
        self.assertEqual(self.initial_tick()['state'], 'idle')
        self.assertEqual(len(self.actions('t3_thread_launch')), 1)

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


class WorkerFailureCatch(Scenario):
    """A fake native host drives the minute-sampler recovery path without sleeps."""
    fixture_init_in_process = True

    def setUp(self):
        super().setUp()
        import datetime
        self.patch = mock.patch.dict(os.environ, self.env)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.m = core_loader.SourceFileLoader('failure_fixture', str(CORE / 'bin/brain')).load_module()
        self.root = self.init('oak')
        self.inst = self.m.Instance(self.root)
        self.inst.local_of('root')
        self.row = {'Job': 'job/palette', 'Started': '2026-01-15T09:00Z', 'Ended': '', 'Class': 'build'}
        self.n = self.inst.node('root')
        self.m.write_jobs(self.inst, self.n, [self.row])
        self.key = self.m.job_key(self.row)
        self.n['local'].update(coordinator='parent', host_project='coord-project', home_project='home-project',
                               workers={self.key: 'worker'}, host_export='on', wake='fixture-send', wake_policy='questions')
        self.inst.save_local()
        self.now = datetime.datetime(2026, 10, 27, 9, 0, tzinfo=datetime.timezone.utc)
        clock = mock.patch.object(self.m.Instance, 'now_dt', side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.thread = {'threadId': 'worker', 'status': 'failed', 'latestRunId': 'failed-run',
                       'activeRunId': None, 'pendingRequestCount': 0, 'projectId': 'home-project',
                       'branch': 'job/palette', 'worktreePath': str(self.root), 'itemCount': 1}
        self.error = 'Selected model is at capacity; private-error-content'
        self.error_run = 'failed-run'
        self.error_type = 'error'
        self.error_truncated = False
        self.pending = []
        self.calls = []
        self.on_pending = lambda: None
        self.unknown = False
        self.send_status = 'failed'
        self.on_send = lambda: None

    def rpc(self, name, args):
        self.calls.append((name, dict(args)))
        if name == 't3_thread_list':
            rows = [dict(self.thread)] if args['projectId'] == 'home-project' else [
                {'threadId': 'parent', 'status': 'idle', 'latestRunId': None}]
            return {'threads': rows, 'nextCursor': None}
        if name == 't3_thread_read':
            if args['threadId'] == 'parent':
                return {'thread': {'threadId': 'parent'}, 'recentRuns': []}
            return {'thread': dict(self.thread), 'recentRuns': [], 'hasMore': False,
                    'items': [{'itemId': 'error-item', 'runId': self.error_run, 'type': self.error_type,
                               'sourceThreadId': 'worker', 'visibility': 'local', 'text': self.error,
                               'status': 'failed', 'textTruncated': self.error_truncated}]}
        if name == 't3_pending_request_list':
            self.on_pending()
            return {'requestIds': self.pending if args['threadId'] == 'worker' else []}
        if name == 't3_thread_send':
            self.on_send()
            run = 'continued-' + str(len(self.sends()))
            self.thread.update(latestRunId=run, status=self.send_status,
                               activeRunId=run if self.send_status == 'running' else None)
            self.error_run = run
            if self.unknown:
                raise self.m.HostRPCError('fixture unknown outcome')
            return {'threadId': args['threadId'], 'runId': run, 'delivery': 'started'}
        self.fail('unexpected native call ' + name)

    def advance(self, seconds):
        import datetime
        self.now += datetime.timedelta(seconds=seconds)

    def tick(self):
        self.m.host_export(self.inst, rpc=self.rpc)
        self.m.resume_failures_if_enabled(self.inst, rpc=self.rpc)
        with mock.patch.object(self.m, 'wake_start'):
            self.m.watch_once(self.inst, quiet=True)

    def sends(self):
        return [args for name, args in self.calls if name == 't3_thread_send']

    def inbox(self):
        return (self.root / 'inbox.md').read_text()

    def test_backoff_steps_replay_cap_and_coordinator_wake(self):
        self.tick()
        self.advance(59)
        self.tick()
        self.assertEqual(self.sends(), [])
        self.advance(1)
        self.tick()
        self.tick()  # replay of the same exported run sends nothing
        self.assertEqual(len(self.sends()), 1)
        self.assertNotIn('worker run failed', self.inbox())
        self.advance(179)
        self.tick()
        self.assertEqual(len(self.sends()), 1)
        self.advance(1)
        self.tick()
        self.assertEqual(len(self.sends()), 2)
        self.advance(599)
        self.tick()
        self.assertEqual(len(self.sends()), 2)
        self.advance(1)
        self.tick()
        self.assertEqual(len(self.sends()), 3)
        self.assertEqual(self.inbox().count('worker run failed'), 1)
        self.advance(3600)
        self.tick()
        self.assertEqual(len(self.sends()), 3)
        self.assertEqual(self.inbox().count('worker run failed'), 1)
        self.assertEqual({a['threadId'] for a in self.sends()}, {'worker'})
        self.assertEqual(len({a['clientRequestId'] for a in self.sends()}), 3)
        self.assertEqual({a['mode'] for a in self.sends()}, {'steer'})
        self.assertEqual({a['message'] for a in self.sends()}, {
            'resume your task from your state note; the host failed your last turn on at capacity'})
        receipts = list((self.root / '.brain/wake').glob('failure-*.json'))
        self.assertEqual(len(receipts), 1)
        receipt = json.loads(receipts[0].read_text())
        self.assertEqual([a['run'] for a in receipt['attempts']], ['failed-run', 'continued-1', 'continued-2'])
        self.assertEqual([a['continuation'] for a in receipt['attempts']], ['continued-1', 'continued-2', 'continued-3'])
        self.assertTrue(all(set(a) == {'threadId', 'mode', 'clientRequestId', 'message'} for a in self.sends()))
        self.assertEqual(self.m.read_jobs(self.n)[0]['Switches'], '')
        self.assertEqual(self.m.read_jobs(self.n)[0].get('Capacity retry', ''), '')
        self.assertNotIn('private-error-content', self.inbox())
        self.assertNotIn('private-error-content', (self.root / '.brain/host-observations.json').read_text())
        pending = list((self.root / '.brain/wake/root').glob('*.pending'))
        self.assertEqual(len(pending), 1)
        self.assertEqual(json.loads(pending[0].read_text())['items'][0]['kind'], 'worker-failure')

    def test_work_error_notices_immediately_without_resume(self):
        self.error = 'pytest failed: expected result does not match'
        self.tick()
        self.assertIn('worker run failed', self.inbox())
        self.advance(3600)
        self.tick()
        self.assertEqual(self.sends(), [])
        self.assertEqual(self.inbox().count('worker run failed'), 1)

    def test_only_current_local_untruncated_error_items_are_retried(self):
        for attr, value in (('error_type', 'assistant_message'), ('error_run', 'old-run'), ('error_truncated', True)):
            with self.subTest(attr=attr):
                old = getattr(self, attr)
                setattr(self, attr, value)
                snapshot = self.m.host_export(self.inst, rpc=self.rpc)
                worker = next(r for r in snapshot['records'] if r['role'] == 'worker')
                self.assertEqual(worker['category'], 'work error')
                setattr(self, attr, old)

    def test_provider_patterns_from_recorded_errors(self):
        for text, category in [('Selected model is at capacity', 'at capacity'), ('Server overloaded', 'overloaded'),
                               ('429 Too Many Requests', 'rate limit'), ('Rate limit exceeded', 'rate limit'),
                               ('stream disconnected before completion', 'stream disconnected')]:
            self.error = text
            snapshot = self.m.host_export(self.inst, rpc=self.rpc)
            self.assertEqual(next(r for r in snapshot['records'] if r['role'] == 'worker')['category'], category)

    def test_new_run_and_pending_question_prevent_stale_resume(self):
        self.tick()
        self.advance(60)
        record = self.m.host_export_runs(self.inst)[0]
        for change in ({'latestRunId': 'new-run'}, {'activeRunId': 'live-run'}, {'status': 'running'},
                       {'pendingRequestCount': 1}, {'projectId': 'other-project'}):
            old = dict(self.thread)
            self.thread.update(change)
            self.m.resume_failure(self.inst, record, rpc=self.rpc)
            self.thread = old
        self.pending = ['question']
        self.m.resume_failure(self.inst, record, rpc=self.rpc)
        self.assertEqual(self.sends(), [])

    def test_rebinding_or_closure_before_claim_prevents_send(self):
        self.tick()
        self.advance(60)
        record = self.m.host_export_runs(self.inst)[0]
        def rebind():
            latest = self.m.Instance(self.root)
            latest.local_of('root')['workers'][self.key] = 'other-worker'
            latest.save_local()
        self.on_pending = rebind
        self.m.resume_failure(self.inst, record, rpc=self.rpc)
        self.assertEqual(self.sends(), [])
        self.n['local']['workers'][self.key] = 'worker'
        self.inst.save_local()
        self.row['Ended'] = '2026-10-27T10:00Z'
        self.m.write_jobs(self.inst, self.n, [self.row])
        self.m.resume_failure(self.inst, record, rpc=self.rpc)
        self.assertEqual(self.sends(), [])

    def test_unknown_send_consumes_attempt_and_never_replays(self):
        self.tick()
        self.advance(60)
        self.unknown = True
        self.tick()
        self.tick()
        self.assertEqual(len(self.sends()), 1)
        self.advance(180)
        self.tick()
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.inbox().count('worker run failed'), 1)

    def test_discovery_catches_unbound_home_project_branch_and_resumes_same_thread(self):
        self.n['local']['workers'].clear()
        self.inst.save_local()
        self.tick()
        record = self.m.host_export_runs(self.inst)[0]
        self.assertTrue(record['discovered'])
        self.assertEqual(record['branch'], self.row['Job'])
        self.advance(60)
        self.tick()
        self.assertEqual([a['threadId'] for a in self.sends()], ['worker'])
        self.assertEqual(self.m.Instance(self.root).local_of('root')['workers'], {})

    def test_multiple_unbound_threads_for_one_job_are_not_resumed(self):
        self.n['local']['workers'].clear()
        self.inst.save_local()
        original = self.rpc
        def rpc(name, args):
            if name == 't3_thread_list' and args['projectId'] == 'home-project':
                result = original(name, args)
                result['threads'].append({**self.thread, 'threadId': 'other-worker'})
                return result
            if args.get('threadId') == 'other-worker':
                result = original(name, {**args, 'threadId': 'worker'})
                if name == 't3_thread_read':
                    result['thread']['threadId'] = 'other-worker'
                    result['items'][0]['sourceThreadId'] = 'other-worker'
                return result
            return original(name, args)
        self.m.host_export(self.inst, rpc=rpc)
        records = self.m.host_export_runs(self.inst)
        self.assertEqual(len(records), 2)
        self.assertTrue(all(r['ambiguous'] for r in records))
        for record in records:
            self.m.resume_failure(self.inst, record, rpc=rpc)
        self.advance(60)
        for record in records:
            self.m.resume_failure(self.inst, record, rpc=rpc)
        self.assertEqual(self.sends(), [])

    def test_discovery_cannot_resume_retired_or_differently_bound_worker(self):
        self.n['local']['workers'][self.key] = 'other-worker'
        self.inst.save_local()
        # Keep the other binding visible to the complete exporter.
        original = self.rpc
        def rpc(name, args):
            if name == 't3_thread_list' and args['projectId'] == 'home-project':
                result = original(name, args)
                result['threads'].append({'threadId': 'other-worker', 'status': 'completed', 'latestRunId': 'other-run'})
                return result
            if name == 't3_pending_request_list' and args['threadId'] == 'other-worker':
                return {'requestIds': []}
            return original(name, args)
        self.m.host_export(self.inst, rpc=rpc)
        record = self.m.host_export_runs(self.inst)[0]
        self.m.resume_failure(self.inst, record, rpc=rpc)
        self.advance(60)
        self.m.resume_failure(self.inst, record, rpc=rpc)
        self.assertEqual(self.sends(), [])
        self.n['local']['workers'].clear()
        self.n['local']['retired_workers'] = {self.key: ['worker']}
        self.inst.save_local()
        self.m.resume_failure(self.inst, record, rpc=rpc)
        self.assertEqual(self.sends(), [])

    def test_concurrent_claim_and_corrupt_receipt_never_duplicate(self):
        import fcntl
        self.tick()
        self.advance(60)
        record = self.m.host_export_runs(self.inst)[0]
        path = self.m.failure_receipt_path(self.inst, record)
        with self.m.failure_lock_path(self.inst, record).open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.m.resume_failure(self.inst, record, rpc=self.rpc)
        self.assertEqual(self.sends(), [])
        path.write_text('{torn')
        self.tick()
        self.assertEqual(self.sends(), [])
        self.assertEqual(path.read_text(), '{torn')
        self.assertIn('worker run failed', self.inbox())

    def test_machine_sample_invokes_export_then_recovery_then_watcher(self):
        import argparse
        owner = self.m._owners['resume_failure']
        with mock.patch.object(self.m, 'load', return_value=self.inst), \
                mock.patch.object(self.m, 'machine_sample_once'), \
                mock.patch.object(owner._m_host_rpc, 'call', side_effect=self.rpc), \
                mock.patch.object(self.m, 'host_task_refresh_if_enabled'), \
                mock.patch.object(self.m, 'dispatch_if_enabled'), \
                mock.patch.object(self.m, 'daily_retro'), mock.patch.object(self.m, 'tick_releases'), \
                mock.patch.object(self.m, 'wake_start'):
            self.m.cmd_machine(argparse.Namespace(action='sample'))
            self.advance(60)
            self.m.cmd_machine(argparse.Namespace(action='sample'))
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(sum(name == 't3_thread_list' for name, _ in self.calls), 4)
        self.assertNotIn('worker run failed', self.inbox())

    def test_sampler_opt_out_does_not_read_or_send(self):
        self.n['local']['host_export'] = 'off'
        self.inst.save_local()
        with mock.patch.object(self.m, 'host_native_call', side_effect=AssertionError('no host')):
            self.m.host_export_if_enabled(self.inst)
            self.m.resume_failures_if_enabled(self.inst, rpc=self.rpc)
        self.assertEqual(self.calls, [])

    def test_successful_continuation_stops_recovery_and_old_export_replays_nothing(self):
        self.tick()
        original = self.m.host_export_runs(self.inst)[0]
        self.send_status = 'completed'
        self.advance(60)
        self.tick()
        self.advance(2000)
        self.tick()
        self.m.resume_failure(self.inst, original, rpc=self.rpc)
        self.assertEqual(len(self.sends()), 1)
        self.assertNotIn('worker run failed', self.inbox())

    def test_third_claim_escalates_after_running_export_and_a_skipped_watcher(self):
        import fcntl
        self.tick()
        for seconds in (60, 180, 600):
            self.advance(seconds)
            self.m.host_export(self.inst, rpc=self.rpc)
            record = self.m.host_export_runs(self.inst)[0]
            if seconds == 600:
                self.send_status = 'running'
            self.m.resume_failure(self.inst, record, rpc=self.rpc)
        self.assertEqual(len(self.sends()), 3)
        with (self.root / '.brain/lock').open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.m.watch_once(self.inst, quiet=True)
        self.assertNotIn('worker run failed', self.inbox())
        self.inst = self.m.Instance(self.root)  # a later sampler/process sees only running host state
        self.m.host_export(self.inst, rpc=self.rpc)
        self.assertEqual(self.m.host_export_runs(self.inst), [])
        with mock.patch.object(self.m, 'wake_start'):
            self.m.watch_once(self.inst, quiet=True)
            self.m.watch_once(self.inst, quiet=True)
        self.assertEqual(self.inbox().count('worker run failed'), 1)
        self.assertEqual(len(list((self.root / '.brain/wake/root').glob('*.pending'))), 1)

    def test_pending_rpc_run_change_is_seen_by_final_read(self):
        self.tick()
        self.advance(60)
        record = self.m.host_export_runs(self.inst)[0]
        def start_operator_run():
            self.thread.update(latestRunId='operator-run', activeRunId='operator-run', status='running')
        self.on_pending = start_operator_run
        self.m.resume_failure(self.inst, record, rpc=self.rpc)
        self.assertEqual(self.sends(), [])

    def test_busy_instance_lock_skips_without_wait_and_send_holds_local_ownership(self):
        import fcntl
        self.tick()
        self.advance(60)
        record = self.m.host_export_runs(self.inst)[0]
        owner = self.m._owners['resume_failure']
        with (self.root / '.brain/lock').open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with mock.patch.object(owner._m_instance.time, 'sleep', side_effect=AssertionError('no blocking wait')):
                self.m.resume_failure(self.inst, record, rpc=self.rpc)
        self.assertEqual(self.sends(), [])
        self.assertEqual(self.m.failure_receipt(self.inst, record)['attempts'], [])
        def prove_writer_fence():
            with (self.root / '.brain/lock').open('a+') as other:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.on_send = prove_writer_fence
        self.m.resume_failure(self.inst, record, rpc=self.rpc)
        self.assertEqual(len(self.sends()), 1)
        self.assertIsNone(owner._m_instance._LOCK['fh'])

    def test_directory_sync_failure_after_claim_rename_sends_nothing_and_never_replays(self):
        import stat
        self.tick()
        self.advance(60)
        record = self.m.host_export_runs(self.inst)[0]
        path = self.m.failure_receipt_path(self.inst, record)
        real_sync = os.fsync
        def sync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode) and json.loads(path.read_text())['pending']:
                raise OSError('fixture directory sync failure')
            return real_sync(fd)
        with mock.patch.object(os, 'fsync', side_effect=sync):
            with self.assertRaises(OSError):
                self.m.resume_failure(self.inst, record, rpc=self.rpc)
        self.assertEqual(self.sends(), [])
        self.assertTrue(json.loads(path.read_text())['pending'])
        self.tick()
        self.assertEqual(self.sends(), [])
        self.assertEqual(self.inbox().count('worker run failed'), 1)

    def test_new_receipt_directory_sync_failure_precedes_all_host_calls(self):
        import shutil
        import stat
        self.m.host_export(self.inst, rpc=self.rpc)
        record = self.m.host_export_runs(self.inst)[0]
        shutil.rmtree(self.root / '.brain/wake', ignore_errors=True)
        self.calls.clear()
        real_sync = os.fsync
        def sync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError('fixture mkdir directory sync failure')
            return real_sync(fd)
        with mock.patch.object(os, 'fsync', side_effect=sync):
            with self.assertRaises(OSError):
                self.m.resume_failure(self.inst, record, rpc=self.rpc)
        self.assertEqual(self.calls, [])
