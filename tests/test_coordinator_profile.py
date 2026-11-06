"""Coordinator settings are private to a host-created worktree."""
import importlib
import json
from pathlib import Path
from unittest import mock

from core_loader import SourceFileLoader
from scenario import CORE, Scenario


class CoordinatorProfile(Scenario):
    fixture_init_in_process = True

    def setUp(self):
        super().setUp()
        facade = SourceFileLoader('coordinator_profile_tests', str(CORE / 'bin/brain')).load_module()
        self.module = importlib.import_module(facade._owners['cmd_due'].__package__ + '.coordinator_startup')

    def test_index_removes_only_annotation_lines_and_leaves_library_bytes(self):
        original = '# Rules\n\n## Instance rules\n\n- R01 Keep the exact words.\n  Gate: none (manual) Applies: all\n- R02 Another rule.\n  Gate: app:tests/test_x.py::test_x Applies: app\n\n## Other\nGate: keep outside index\n'
        compact = self.module.compact_index(original)
        self.assertEqual(compact, original.replace('  Gate: none (manual) Applies: all\n', '').replace('  Gate: app:tests/test_x.py::test_x Applies: app\n', ''))
        self.assertEqual(self.module.compact_index(compact), compact)

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
                self.assertEqual(args['workspaceStrategy']['type'], 'worktree')
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
            result = self.module.launch_scoped(inst, inst.node('root'), rpc, 'ticket', 'Start here')
        self.assertEqual(result['thread'], 'new')
        self.assertEqual([name for name, _ in calls], ['t3_thread_launch', 't3_thread_read', 't3_thread_send'])
