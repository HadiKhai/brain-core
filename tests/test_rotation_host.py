"""Native rotation waits for children and requires replacement-side acceptance."""
import json
import sys
from types import SimpleNamespace
from unittest import mock

import core_loader
from scenario import CORE, Scenario


class RotationHost(Scenario):
    def setUp(self):
        super().setUp()
        self.root = self.init('oak')
        self.m = core_loader.SourceFileLoader('rotation_host_fixture', str(CORE / 'bin/brain')).load_module()
        self.inst = self.m.Instance(self.root)
        self.inst.local_of('root').update(coordinator='old', host_project='project', coordinator_sessions=['old-session'])
        self.inst.save_local()
        self.n = self.inst.node('root')
        history = self.m.rotation_file(self.inst, self.n)
        history.parent.mkdir(parents=True, exist_ok=True)
        history.write_text(json.dumps({'coordinator': 'old', 'coordinator_sessions': ['old-session'], 'previous': []}))
        self.caller = 'old'
        self.threads = {'old': self.thread('old', 'running', 'old-run')}
        self.calls = []
        self.tasks = [{'scheduledTaskId': 'timer', 'boundThreadId': 'old', 'projectId': 'project'}]
        self.lost = False

    def thread(self, identity, status, run=None, parent=None):
        return {'threadId': identity, 'status': status, 'activeRunId': run, 'parentThreadId': parent,
                'pendingRequestCount': 0, 'providerInstanceId': 'codex', 'model': 'fixture-model',
                'worktreePath': str(self.root), 'settled': False}

    def rpc(self, name, args):
        self.calls.append((name, dict(args)))
        if name == 't3_thread_list':
            return {'projectId': 'project', 'currentThreadId': self.caller, 'threads': list(self.threads.values()),
                    'total': len(self.threads), 'nextCursor': None}
        if name == 't3_thread_read':
            return {'thread': dict(self.threads[args['threadId']]), 'items': []}
        if name == 't3_pending_request_list':
            return {'requestIds': []}
        if name == 'list_scheduled_tasks':
            return {'tasks': [dict(task) for task in self.tasks]}
        if name == 't3_thread_launch':
            if self.lost:
                raise self.m.HostRPCError('fixture lost response')
            self.threads['new'] = self.thread('new', 'running', 'new-run')
            return {'threadId': 'new', 'projectId': 'project'}
        if name == 'update_scheduled_task':
            self.assertTrue(args['bindToCurrentThread'])
            self.tasks[0]['boundThreadId'] = self.caller
            return dict(self.tasks[0])
        if name == 't3_thread_organize':
            self.assertEqual(args, {'threadId': 'old', 'action': 'settle'})
            self.threads['old']['settled'] = True
            return {'settled': True}
        if name == 't3_thread_interrupt':
            self.assertEqual(args['threadId'], 'old')
            self.assertEqual(args['runId'], 'old-run')
            self.threads['old'].update(status='interrupted', activeRunId=None)
            return {'threadId': 'old', 'runId': 'old-run', 'status': 'interrupt_requested'}
        self.fail('unexpected native action ' + name)

    def rotate(self):
        owner = self.m._owners['rotate_coordinator']
        with mock.patch.object(owner._m_rotation, 'rotation_due', return_value='due'), \
                mock.patch.object(owner, 'rotation_packet', return_value='Preserve every open and parked task.'):
            return self.m.rotate_coordinator(self.inst, self.n, rpc=self.rpc)

    def test_live_children_are_listed_first_and_preserved_without_launch(self):
        self.threads['child'] = self.thread('child', 'running', 'child-run', 'old')
        self.assertEqual(self.rotate()['state'], 'pending')
        self.assertEqual(self.calls[0][0], 't3_thread_list')
        self.assertFalse(any(name in ('t3_thread_launch', 't3_thread_interrupt', 't3_thread_organize') for name, _ in self.calls))

    def test_replacement_rebinds_tasks_itself_before_old_thread_can_settle(self):
        launch = self.rotate()
        self.assertEqual(launch['state'], 'launched')
        self.assertEqual(self.m.Instance(self.root).local_of('root')['coordinator'], 'old')
        self.caller = 'new'
        result = self.m.accept_rotation(self.inst, self.n, launch['ticket'], 'new-session', rpc=self.rpc)
        self.assertEqual(result['state'], 'bound')
        current = self.m.Instance(self.root).local_of('root')
        self.assertEqual(current['coordinator'], 'new')
        self.assertEqual(current['coordinator_sessions'], ['old-session', 'new-session'])
        self.assertEqual(self.tasks[0]['boundThreadId'], 'new')
        self.assertFalse(self.threads['old']['settled'])
        self.threads['old'].update(status='completed', activeRunId=None)
        self.assertEqual(self.m.accept_rotation(self.inst, self.n, launch['ticket'], 'new-session', rpc=self.rpc)['state'], 'done')
        self.assertTrue(self.threads['old']['settled'])
        previous = self.m.read_rotation(self.inst, self.m.Instance(self.root).node('root'))['previous']
        self.assertEqual(previous[-1]['coordinator'], 'old')

    def test_foreign_caller_or_missing_runtime_session_cannot_adopt(self):
        launch = self.rotate()
        for caller, session in (('foreign', 'new-session'), ('new', '')):
            self.caller = caller
            self.assertEqual(self.m.accept_rotation(self.inst, self.n, launch['ticket'], session, rpc=self.rpc)['state'], 'pending')
        self.assertEqual(self.m.Instance(self.root).local_of('root')['coordinator'], 'old')
        self.assertFalse(any(name == 'update_scheduled_task' for name, _ in self.calls))

    def test_unknown_launch_is_never_repeated(self):
        self.lost = True
        self.assertEqual(self.rotate()['state'], 'pending')
        self.assertEqual(self.rotate()['state'], 'pending')
        self.assertEqual(sum(name == 't3_thread_launch' for name, _ in self.calls), 1)

    def test_rotation_resumes_old_thread_settlement_below_new_session_threshold(self):
        launch = self.rotate()
        self.caller = 'new'
        self.assertEqual(self.m.accept_rotation(self.inst, self.n, launch['ticket'], 'new-session', rpc=self.rpc)['state'], 'bound')
        self.assertEqual(self.m.resume_rotation(self.inst, self.n, rpc=self.rpc)['state'], 'done')
        self.assertIsNone(self.m.resume_rotation(self.inst, self.n, rpc=self.rpc))

    def test_new_child_or_unknown_wake_transport_retains_old_coordinator(self):
        self.n['local']['wake'] = 'https://invalid.example/private-capability'
        self.inst.save_local()
        self.assertEqual(self.rotate()['state'], 'pending')
        self.assertFalse(any(name == 't3_thread_launch' for name, _ in self.calls))
        self.assertEqual(self.m.Instance(self.root).local_of('root')['coordinator'], 'old')

    def test_native_calls_never_hold_the_instance_writer_lock(self):
        original = self.rpc
        def unlocked(name, args):
            self.assertIsNone(self.m._owners['brain_lock']._LOCK['fh'])
            return original(name, args)
        self.rpc = unlocked
        launch = self.rotate()
        self.assertEqual(launch['state'], 'launched')
        self.caller = 'new'
        self.assertEqual(self.m.accept_rotation(self.inst, self.n, launch['ticket'], 'new-session', rpc=self.rpc)['state'], 'bound')
        from types import SimpleNamespace
        self.assertFalse(self.m.writes(SimpleNamespace(f=self.m.cmd_lifecycle)))

    def test_registered_child_outside_parent_edges_is_retained_until_exact_terminal_receipt(self):
        registry = {'version': 1, 'tasks': {'registration': {'node': 'root', 'parent_thread': 'old',
                    'taskId': 'task', 'childThreadId': 'child'}}}
        (self.root / '.brain/host-tasks.json').write_text(json.dumps(registry))
        adapter = SimpleNamespace(host_task_read=lambda inst: registry,
                                  HOST_TASK_TERMINAL=('completed', 'failed', 'cancelled', 'interrupted'))
        owner = self.m._owners['rotate_coordinator']
        package = sys.modules[owner.__package__]
        with mock.patch.object(package, 'host_tasks', adapter, create=True):
            self.assertEqual(self.rotate()['state'], 'pending')
            self.assertFalse(any(name == 't3_thread_launch' for name, _ in self.calls))
            registry['tasks']['registration']['completion'] = {'id': 'a' * 64, 'node': 'root',
                'parent_thread': 'old', 'task_key': 'registration', 'run': 'child-run', 'status': 'completed'}
            self.threads['child'] = self.thread('child', 'completed')
            self.threads['child']['latestRunId'] = 'child-run'
            self.assertEqual(self.rotate()['state'], 'launched')
            self.assertEqual(registry['tasks']['registration']['parent_thread'], 'old')

    def test_verified_rotation_reuses_webhook_without_stalling_next_wake(self):
        import os
        from scenario import consume_fixture_wake
        self.inst.local_of('root')['wake'] = 'https://wake.invalid/fixture'
        self.inst.save_local()
        self.tasks[0]['webhookUrl'] = 'https://wake.invalid/fixture'
        self.inst = self.m.Instance(self.root)
        self.n = self.inst.node('root')
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(self.m, 'wake_start'), mock.patch.object(self.m, 'WAKE_INTERVAL', 0), \
                mock.patch.object(self.m, 'wake_send', return_value=202) as send:
            self.assertTrue(self.m.request_wake(self.inst, self.n, 'fixture-old', items=[
                {'id': '1' * 64, 'kind': 'inbox'}], explicit=True))
            self.m.wake_drain(self.inst, self.n, once=True)
            consume_fixture_wake(self.m, self.inst, self.n, required=True)
            launch = self.rotate()
            self.caller = 'new'
            self.assertEqual(self.m.accept_rotation(self.inst, self.n, launch['ticket'], 'new-session', rpc=self.rpc)['state'], 'bound')
            current = self.m.Instance(self.root)
            node = current.node('root')
            self.assertTrue(self.m.request_wake(current, node, 'fixture-new', items=[
                {'id': '2' * 64, 'kind': 'inbox'}], explicit=True))
            self.m.wake_drain(current, node, once=True)
            self.assertEqual(send.call_count, 2)
            self.assertEqual(current.local_of('root')['wake_verified_binding'], self.m.wake_turn_binding(current, node))
