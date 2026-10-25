"""Host lifecycle is faked; no test launches a real conversation or schedule."""
import argparse
import hashlib
import json
import os
import tempfile
import sys
import unittest
from pathlib import Path
from unittest import mock
import core_loader

CORE = Path(__file__).resolve().parent.parent


class HostActions(unittest.TestCase):
    def setUp(self):
        self.m = core_loader.SourceFileLoader('host_actions_fixture', str(CORE / 'bin/brain')).load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'instance.md').write_text('---\nhost: t3\nruntimes: codex\nprincipals: alice\n---\n')
        (self.root / 'node.md').write_text('---\ntitle: Sample\nowners: alice\nruntime: codex\n---\n')
        self.inst = self.m.Instance(self.root)

    def test_provision_binds_project_then_launches_once_in_node_workspace(self):
        calls = []
        def call(inst, tool, args):
            calls.append((tool, args))
            return {'id': 'project-1'} if tool == 't3_project_create' else {'threadId': 'thread-1'}
        with mock.patch.object(self.m, 'host_call', side_effect=call):
            self.m.provision_coordinator(self.inst, 'root', {'instanceId':'codex', 'model':'sample-model'})
            self.m.provision_coordinator(self.inst, 'root', {'instanceId':'codex', 'model':'sample-model'})
        self.assertEqual([x[0] for x in calls], ['t3_project_create', 't3_thread_launch', 't3_thread_organize', 't3_thread_organize'])
        launch = calls[1][1]
        self.assertEqual(launch['workspaceStrategy'], {'type':'root'})
        self.assertEqual(launch['projectId'], 'project-1')
        self.assertEqual(self.inst.local_of('root')['coordinator'], 'thread-1')
        self.assertEqual(calls[2][1], {'threadId':'thread-1','action':'pin'})
        self.assertNotIn('thread-1', (self.root / 'node.md').read_text())

    def test_lost_launch_response_never_retries_blindly(self):
        self.inst.local_of('root')['host_project'] = 'project-1'
        self.inst.save_local()
        with mock.patch.object(self.m, 'host_call', side_effect=TimeoutError('lost')) as call:
            with self.assertRaises(TimeoutError):
                self.m.provision_coordinator(self.inst, 'root', {'instanceId':'codex','model':'sample-model'})
            with self.assertRaisesRegex(ValueError, 'pending'):
                self.m.provision_coordinator(self.inst, 'root', {'instanceId':'codex','model':'sample-model'})
            self.assertEqual(call.call_count, 1)
        self.assertTrue(self.inst._load_local()['nodes']['root']['host_setup_pending'])

    def test_wake_setup_must_run_inside_bound_coordinator(self):
        self.inst.local_of('root').update(coordinator='thread-1', host_project='project-1')
        self.inst.save_local()
        with mock.patch.object(self.m, 'host_call', return_value={'threadId':'foreign'}) as call:
            with self.assertRaisesRegex(ValueError, 'bound coordinator'):
                self.m.provision_wake(self.inst, 'root')
            self.assertEqual(call.call_count, 1)
        self.assertNotIn('wake', self.inst.local_of('root'))

    def test_wake_setup_binds_exact_returned_coordinator_and_private_url(self):
        self.inst.local_of('root').update(coordinator='thread-1', host_project='project-1')
        self.inst.save_local()
        values = [{'threadId':'thread-1'}, {'boundThreadId':'thread-1','scheduledTaskId':'task-1',
                  'webhookUrl':'https://host.example/wake/private-capability'}]
        with mock.patch.object(self.m, 'host_call', side_effect=values) as call:
            self.m.provision_wake(self.inst, 'root')
        args = call.call_args_list[1].args[2]
        self.assertEqual(args['schedule'], {'type':'webhook'})
        self.assertTrue(args['bindToCurrentThread'])
        self.assertEqual(self.inst.local_of('root')['wake'], values[1]['webhookUrl'])
        self.assertNotIn('private-capability', (self.root / 'schedules.md').read_text())
        self.assertEqual(self.inst.local['schedules']['wake-' + hashlib.sha256(b'root').hexdigest()], 'task-1')

    def test_missing_tunnel_retains_task_id_without_fabricated_binding(self):
        self.inst.local_of('root').update(coordinator='thread-1', host_project='project-1')
        self.inst.save_local()
        values = [{'threadId':'thread-1'}, {'boundThreadId':'thread-1','scheduledTaskId':'task-1'}]
        with mock.patch.object(self.m, 'host_call', side_effect=values):
            with self.assertRaisesRegex(ValueError, 'Connect'):
                self.m.provision_wake(self.inst, 'root')
        self.assertNotIn('wake', self.inst.local_of('root'))
        self.assertEqual(self.inst.local['schedules']['wake-' + hashlib.sha256(b'root').hexdigest()], 'task-1')

    def test_concurrent_binding_is_preserved_instead_of_overwritten(self):
        self.inst.local_of('root')['host_project'] = 'project-1'
        self.inst.save_local()
        def call(inst, tool, args):
            other = self.m.Instance(self.root)
            other.local_of('root')['coordinator'] = 'replacement'
            other.save_local()
            return {'threadId':'launched-thread'}
        with mock.patch.object(self.m, 'host_call', side_effect=call):
            with self.assertRaisesRegex(ValueError, 'binding changed'):
                self.m.provision_coordinator(self.inst, 'root', {'instanceId':'codex','model':'sample-model'})
        self.assertEqual(self.inst._load_local()['nodes']['root']['coordinator'], 'replacement')
        self.assertEqual(self.inst._load_local()['nodes']['root']['host_setup_pending']['returned_bindings']['coordinator'], 'launched-thread')

    def test_rotation_during_pin_does_not_report_old_coordinator_success(self):
        self.inst.local_of('root')['host_project'] = 'project-1'
        self.inst.save_local()
        def call(inst, tool, args):
            if tool == 't3_thread_organize':
                other = self.m.Instance(self.root)
                other.local_of('root')['coordinator'] = 'replacement'
                other.save_local()
            return {'threadId':'launched-thread'}
        with mock.patch.object(self.m, 'host_call', side_effect=call):
            with self.assertRaisesRegex(ValueError, 'changed during pinning'):
                self.m.provision_coordinator(self.inst, 'root', {'instanceId':'codex','model':'sample-model'})
        self.assertEqual(self.inst._load_local()['nodes']['root']['coordinator'], 'replacement')

    def test_rotation_during_wake_keeps_returned_task_without_binding_stale_url(self):
        self.inst.local_of('root').update(coordinator='thread-1', host_project='project-1')
        self.inst.save_local()
        def call(inst, tool, args):
            if tool == 't3_thread_configuration':
                return {'threadId':'thread-1'}
            other = self.m.Instance(self.root)
            other.local_of('root')['coordinator'] = 'replacement'
            other.save_local()
            return {'boundThreadId':'thread-1','scheduledTaskId':'task-1','webhookUrl':'https://host.example/private'}
        with mock.patch.object(self.m, 'host_call', side_effect=call):
            with self.assertRaisesRegex(ValueError, 'changed during host setup'):
                self.m.provision_wake(self.inst, 'root')
        saved = self.inst._load_local()
        self.assertNotIn('wake', saved['nodes']['root'])
        self.assertEqual(next(iter(saved['host_unbound_tasks'].values()))['task'], 'task-1')

    def test_host_watch_requires_exact_context_and_positive_confirmation(self):
        self.inst.local_of('root').update(coordinator='thread-1', host_project='project-1')
        self.inst.save_local()
        values = [{'threadId':'thread-1'}, {'watching':True}]
        with mock.patch.object(self.m, 'host_call', side_effect=values) as call:
            self.m.watch_change(self.inst, 'root', 'https://example.com/org/repo/pull/1')
        self.assertEqual(call.call_args.args[1], 'watch_pull_request')
        self.assertEqual(call.call_args.args[2]['threadId'], 'thread-1')
        with mock.patch.object(self.m, 'host_call', side_effect=[{'threadId':'thread-1'}, {'watching':False}]):
            with self.assertRaisesRegex(ValueError, 'did not confirm'):
                self.m.watch_change(self.inst, 'root', 'https://example.com/org/repo/pull/1')

    def test_schedule_bridge_creates_structured_trigger_and_preserves_records(self):
        self.inst.local_of('root').update(coordinator='thread-1', host_project='project-1')
        self.inst.save_local()
        path = self.root / 'schedules.md'
        path.write_text('## morning\nwhen: daily 09:00\nnode: root\nbrief: Run node-sync then node-brief.\n')
        before = path.read_bytes()
        with mock.patch.object(self.m, 'host_call', side_effect=[{'threadId':'thread-1'},
                {'boundThreadId':'thread-1', 'scheduledTaskId':'schedule-1'}]) as call:
            self.m.provision_schedule(self.inst, 'root', 'morning', 'codex')
        request = call.call_args.args[2]
        self.assertEqual(request['schedule'], {'type':'fixed_time','timeOfDay':'09:00'})
        self.assertTrue(request['prompt'].startswith('Automated turn:'))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.inst.local['schedule_providers']['morning'], 'codex')
        with mock.patch.object(self.m, 'host_call', return_value={'threadId':'thread-1'}) as call:
            self.m.provision_schedule(self.inst, 'root', 'morning', 'codex')
            self.assertEqual(call.call_count, 1)
        path.write_text(path.read_text().replace('09:00','10:00'))
        with mock.patch.object(self.m, 'host_call', return_value={'threadId':'thread-1'}) as call:
            with self.assertRaisesRegex(ValueError, 'reconcile'):
                self.m.provision_schedule(self.inst, 'root', 'morning', 'codex')
            self.assertEqual(call.call_count, 1)

    def test_schedule_trigger_validation_and_node_scope(self):
        for invalid in ['daily 25:00','hourly; execute','every 0 minutes','tomorrow']:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    self.m.schedule_trigger(invalid)
        self.assertEqual(self.m.schedule_trigger('every 5 minutes'), {'type':'interval','everyMs':300000})
        self.assertEqual(self.m.schedule_trigger('weekdays 07:30'),
                         {'type':'fixed_time','timeOfDay':'07:30','weekdays':[1,2,3,4,5]})
        self.inst.local_of('root').update(coordinator='thread-1')
        self.inst.save_local()
        (self.root / 'schedules.md').write_text('## other\nwhen: hourly\nnode: child\nbrief: Check work.\n')
        with mock.patch.object(self.m, 'host_call', return_value={'threadId':'thread-1'}) as call:
            with self.assertRaisesRegex(ValueError, 'this node'):
                self.m.provision_schedule(self.inst, 'root', 'other')
            self.assertEqual(call.call_count, 1)

    def test_missing_native_transport_refuses_before_routing_or_pending_state(self):
        with mock.patch.dict(os.environ, {'T3_ACP_MCP_NODE':''}), \
                mock.patch.object(self.m, 'load', return_value=self.inst), \
                mock.patch.object(self.m, 'worker_route') as route, \
                mock.patch.object(self.m, 'provision_coordinator') as provision:
            with self.assertRaisesRegex(SystemExit, 'native host MCP transport unavailable'):
                self.m.cmd_host(argparse.Namespace(action='provision', id='root', model='sample', url=None))
        route.assert_not_called()
        provision.assert_not_called()
        self.assertFalse(self.inst.local_path.exists())

    def test_rotation_during_watch_retains_owner_without_claiming_current_success(self):
        self.inst.local_of('root').update(coordinator='thread-1')
        self.inst.save_local()
        def call(inst, tool, args):
            if tool == 't3_thread_configuration':
                return {'threadId':'thread-1'}
            other = self.m.Instance(self.root)
            other.local_of('root')['coordinator'] = 'replacement'
            other.save_local()
            return {'watching':True}
        with mock.patch.object(self.m, 'host_call', side_effect=call):
            with self.assertRaisesRegex(ValueError, 'changed during watch setup'):
                self.m.watch_change(self.inst, 'root', 'https://example.com/org/repo/pull/1')
        saved = self.inst._load_local()
        self.assertEqual(next(iter(saved['host_unbound_watches'].values()))['coordinator'], 'thread-1')

    def test_transport_uses_native_acp_without_a_shell(self):
        envelope = {'structuredContent': {'threadId':'thread-1'}}
        result = mock.Mock(returncode=0, stdout=json.dumps(envelope))
        with mock.patch.dict(os.environ, {'T3_ACP_MCP_NODE':'/fake/app', 'T3_ACP_MCP_ENTRYPOINT':'/fake/main.js'}), \
                mock.patch.object(self.m.subprocess, 'run', return_value=result) as run:
            value = self.m.host_call(self.inst, 't3_thread_configuration', {})
        self.assertEqual(value, envelope['structuredContent'])
        self.assertEqual(run.call_args.args[0], ['/fake/app','/fake/main.js','acp-mcp-call','t3_thread_configuration','{}'])
        self.assertEqual(run.call_args.kwargs['env']['ELECTRON_RUN_AS_NODE'], '1')
        self.assertNotIn('shell', run.call_args.kwargs)


from scenario import Scenario


class HostProvisionCLI(Scenario):
    def test_one_command_creates_node_and_binds_native_host_result(self):
        root = self.init('alice')
        calls = self.tmp / 'host-calls.jsonl'
        stub = self.tmp / 'native-host.py'
        stub.write_text("""import json,os,sys
from pathlib import Path
tool, args = sys.argv[2], json.loads(sys.argv[3])
with open(os.environ['FIXTURE_HOST_CALLS'], 'a') as stream:
    stream.write(json.dumps({'tool':tool,'args':args})+'\\n')
value = {'id':'project-1'} if tool == 't3_project_create' else {'threadId':'thread-1'}
print(json.dumps({'structuredContent':value}))
""")
        self.env.update(T3_ACP_MCP_NODE=sys.executable, T3_ACP_MCP_ENTRYPOINT=str(stub),
                        FIXTURE_HOST_CALLS=str(calls))
        output = self.b(root, 'new', 'app', '--title', 'Application', '--runtime', 'codex',
                        '--brief', 'Build a sample.', '--coordinator', '--model', 'sample-model')
        self.assertIn('coordinator:', output)
        local = json.loads((root / '.brain/local.json').read_text())
        self.assertEqual(local['nodes']['app']['coordinator'], 'thread-1')
        self.assertEqual(local['nodes']['app']['host_project'], 'project-1')
        recorded = [json.loads(line) for line in calls.read_text().splitlines()]
        self.assertEqual(recorded[0]['args']['workspaceRoot'], str(root / 'nodes/app'))
        self.assertEqual(recorded[1]['args']['modelSelection']['instanceId'], 'codex')
        self.assertIn('Automated turn:', recorded[1]['args']['message'])
        self.assertIn('Build a sample.', (root / 'nodes/app/brief.md').read_text())
        self.assertNotIn('thread-1', self.git('show', 'HEAD', cwd=root))
