"""Coordinator settings are private to a host-created worktree."""
import importlib
import json
from pathlib import Path
from unittest import mock

from core_loader import SourceFileLoader
from scenario import CORE, Scenario


SELECTION = {'instanceId': 'provider-one', 'model': 'sample-model', 'options': {'reasoningEffort': 'high'}}


class CoordinatorProfile(Scenario):
    fixture_init_in_process = True

    def setUp(self):
        super().setUp()
        facade = SourceFileLoader('coordinator_profile_tests', str(CORE / 'bin/brain')).load_module()
        self.module = importlib.import_module(facade._owners['cmd_due'].__package__ + '.coordinator_startup')

    def test_profile_refuses_main_and_retains_user_settings_and_brain_skills(self):
        root = self.init('fixture')
        inst = self.module._m_instance.Instance(root)
        home = self.tmp / 'user'
        (home / '.claude/plugins').mkdir(parents=True)
        (home / '.claude/skills/external').mkdir(parents=True)
        (home / '.claude/skills/external/SKILL.md').write_text('---\nname: outside-skill\n---\n')
        (home / '.claude/plugins/installed_plugins.json').write_text(json.dumps({'plugins': {'design@example': []}}))
        settings = home / '.claude/settings.json'
        settings.write_text('{"env":{"UNCHANGED":"yes"}}')
        before = settings.read_bytes()
        with self.assertRaises(ValueError):
            self.module.install_profile(inst, 'root', root, home=home)
        workspace = self.tmp / 'coordinator'
        self.git('worktree', 'add', '--detach', str(workspace), cwd=root)
        result = self.module.install_profile(inst, 'root', workspace, home=home)
        data = json.loads(Path(result['settings']).read_text())
        self.assertEqual(data['env']['BRAIN_ROOT'], str(root))
        self.assertEqual(data['env']['ENABLE_CLAUDEAI_MCP_SERVERS'], 'false')
        self.assertTrue(data['disableClaudeAiConnectors'])
        self.assertFalse(data['enabledPlugins']['design@example'])
        self.assertEqual(data['skillOverrides']['outside-skill'], 'off')
        self.assertNotIn('home-job', data['skillOverrides'])
        self.assertEqual(settings.read_bytes(), before)
        self.assertFalse((root / '.claude/settings.local.json').exists())
        self.assertIn('t3-code', result['kept'])

    def test_foreign_worktree_and_existing_local_settings_are_preserved(self):
        root = self.init('fixture')
        inst = self.module._m_instance.Instance(root)
        foreign = self.tmp / 'foreign'
        self.repo(foreign)
        with self.assertRaises(ValueError):
            self.module.install_profile(inst, 'root', foreign, home=self.tmp)
        workspace = self.tmp / 'coordinator'
        self.git('worktree', 'add', '--detach', str(workspace), cwd=root)
        local = workspace / '.claude/settings.local.json'
        local.write_text('{"env":{"OWNED":"elsewhere"}}')
        with self.assertRaises(ValueError):
            self.module.install_profile(inst, 'root', workspace, home=self.tmp)
        self.assertEqual(local.read_text(), '{"env":{"OWNED":"elsewhere"}}')

    def test_launch_is_idle_until_profile_is_installed_and_uses_idempotent_send(self):
        root = self.init('fixture')
        inst = self.module._m_instance.Instance(root)
        workspace = self.tmp / 'coordinator'
        self.git('worktree', 'add', '--detach', str(workspace), cwd=root)
        calls = []
        def rpc(name, args):
            calls.append((name, args))
            if name == 't3_thread_launch':
                self.assertNotIn('message', args)
                self.assertEqual(args['clientRequestId'], 'coordinator-launch-ticket')
                self.assertEqual(args['workspaceStrategy']['type'], 'worktree')
                self.assertEqual(args['modelSelection'], SELECTION)
                return {'threadId': 'new', 'modelSelection': {'instanceId': 'claudeAgent'}}
            if name == 't3_thread_read':
                return {'thread': {'threadId': 'new', 'worktreePath': str(workspace), 'activeRunId': None}}
            if name == 't3_thread_send':
                self.assertTrue((workspace / '.claude/settings.local.json').exists())
                self.assertEqual(args['clientRequestId'], 'coordinator-start-ticket')
                self.assertIn(str(root), args['message'])
                return {'threadId': 'new'}
            self.fail(name)
        with mock.patch.object(self.module, 'profile_home', return_value=self.tmp):
            result = self.module.launch_scoped(inst, inst.node('root'), rpc, 'ticket', 'Start here',
                                               {'modelSelection': SELECTION})
        self.assertEqual(result['thread'], 'new')
        self.assertEqual([name for name, _ in calls], ['t3_thread_launch', 't3_thread_read', 't3_thread_send'])

    def test_profile_sparse_checkout_has_instructions_but_no_shadow_state(self):
        root = self.init('fixture')
        self.b(root, 'new', 'app', '--title', 'Fixture app')
        (root / 'handover.md').write_text('Current canonical handover\n')
        self.git('add', 'handover.md', cwd=root)
        self.git('commit', '-qm', 'fixture handover', cwd=root)
        before = {name: (root / name).read_bytes() for name in (
            'status.md', 'inbox.md', 'handover.md', 'nodes/app/status.md', 'nodes/app/inbox.md')}
        workspace = self.tmp / 'coordinator'
        self.git('worktree', 'add', '--detach', str(workspace), cwd=root)
        self.module.install_profile(self.module._m_instance.Instance(root), 'app', workspace, home=self.tmp)
        for name, contents in before.items():
            self.assertFalse((workspace / name).exists(), name)
            self.assertEqual((root / name).read_bytes(), contents)
        self.assertTrue((workspace / 'AGENTS.md').is_file())
        self.assertTrue((workspace / 'nodes/app/AGENTS.md').is_file())
        self.assertTrue((workspace / '.claude/settings.local.json').is_file())
        self.assertEqual(self.git('config', '--worktree', '--get', 'core.sparseCheckout', cwd=workspace).strip(), 'true')
        self.assertNotIn('true', self.module._m_common.git('config', '--get', 'core.sparseCheckout', cwd=root).stdout)

    def test_scoped_launch_uses_home_project_when_host_project_is_absent(self):
        root = self.init('fixture')
        inst = self.module._m_instance.Instance(root)
        node = inst.node('root')
        node['local'].update(home_project='home-project')
        node['local'].pop('host_project', None)
        def rpc(name, args):
            if name == 't3_thread_launch':
                self.assertEqual(args['projectId'], 'home-project')
                return {'threadId': 'new'}
            if name == 't3_thread_read':
                return {'thread': {'threadId': 'new', 'worktreePath': str(self.tmp / 'scope')}}
            return {'threadId': 'new'}
        with mock.patch.object(self.module, 'install_profile', return_value={
                'workspace': str(self.tmp / 'scope'), 'settings': str(self.tmp / 'scope/settings')}):
            self.module.launch_scoped(inst, node, rpc, 'ticket', 'Start', {'modelSelection': SELECTION})

    def test_malformed_native_responses_become_bounded_value_errors(self):
        root = self.init('fixture')
        inst = self.module._m_instance.Instance(root)
        for result in (None, [], 'opaque', {'threadId': 'new'}):
            def rpc(name, args):
                return result if name == 't3_thread_launch' else {'thread': None}
            with self.subTest(result=result), self.assertRaises(ValueError):
                self.module.launch_scoped(inst, inst.node('root'), rpc, 'ticket', 'Start', {'modelSelection': SELECTION})

    def test_running_first_input_replays_its_key_without_reinstalling_profile(self):
        root = self.init('fixture')
        inst = self.module._m_instance.Instance(root)
        workspace = self.tmp / 'coordinator'
        self.git('worktree', 'add', '--detach', str(workspace), cwd=root)
        with mock.patch.object(self.module, 'profile_home', return_value=self.tmp):
            profile = self.module.install_profile(inst, 'root', workspace)
        receipt = {'new': 'new', 'workspace': str(workspace), 'profile_ready': True}
        calls = []
        def rpc(name, args):
            calls.append((name, args))
            if name == 't3_thread_read':
                return {'thread': {'threadId': 'new', 'worktreePath': str(workspace), 'activeRunId': 'running'}}
            return {'threadId': 'new'}
        with mock.patch.object(self.module, 'install_profile', side_effect=AssertionError('must not rewrite a running profile')):
            result = self.module.launch_scoped(inst, inst.node('root'), rpc, 'ticket', 'Start', receipt)
        self.assertEqual(result['thread'], 'new')
        self.assertEqual(calls[-1][1]['clientRequestId'], 'coordinator-start-ticket')
        self.assertEqual([name for name, _ in calls], ['t3_thread_read', 't3_thread_send'])
