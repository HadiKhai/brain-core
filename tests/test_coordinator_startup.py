"""Startup inventories stay compact without discarding on-demand evidence."""
import contextlib
import io
import json
from unittest import mock

from core_loader import SourceFileLoader
from scenario import CORE, Scenario


class CoordinatorStartup(Scenario):
    fixture_init_in_process = True

    def test_due_summary_counts_all_items_without_loading_their_bodies(self):
        module = SourceFileLoader('startup_due', str(CORE / 'bin/brain')).load_module()
        status = module._owners['cmd_due']
        items = [{'node': 'app', 'kind': 'handback', 'text': 'PRIVATE DETAIL ' * 100,
                  'id': str(i)} for i in range(1000)]
        items += [{'node': 'app', 'kind': 'question', 'text': 'Choose a format', 'id': 'ask'}]
        output = status.due_summary(items, 'app')
        self.assertIn('1001 item(s)', output)
        self.assertIn('app: handback: 1000', output)
        self.assertIn('app: question: 1', output)
        self.assertIn('brain due app', output)
        self.assertIn('--json', output)
        self.assertNotIn('PRIVATE DETAIL', output)
        self.assertLess(len(output), 250)
        self.assertEqual(status.due_summary([], 'app'), '')
        self.assertEqual(len(items), 1001)

    def test_due_summary_cli_keeps_full_and_json_evidence_available(self):
        root = self.init('fixture')
        (root / 'inbox.md').write_text('- 2026-10-19 10:00 +00:00 from worker: question: Which format?\n')
        full = self.b(root, 'due', 'root')
        summary = self.b(root, 'due', 'root', '--summary')
        self.assertIn('Which format?', full)
        self.assertIn('root: inbox:', summary)
        self.assertNotIn('Which format?', summary)
        self.assertIn('Which format?', self.b(root, 'due', 'root'))
        rows = json.loads(self.b(root, 'due', 'root', '--json'))
        self.assertTrue(any('Which format?' in row['text'] for row in rows))
        # A display option must not change the machine-readable inventory.
        self.assertEqual(rows, json.loads(self.b(root, 'due', 'root', '--summary', '--json')))

    def test_changes_summary_retains_bodies_and_does_not_mark_seen(self):
        root = self.init('fixture')
        module = SourceFileLoader('startup_changes', str(CORE / 'bin/brain')).load_module()
        upgrade = module._owners['cmd_changes']
        inst = module.Instance(root)
        node = inst.node('root')
        entry = {'id': '2026-01-01: Example change', 'body': 'Complete instructions stay on demand.'}
        args = type('Args', (), dict(id='root', seen=False, all=False, json=False, summary=True))()
        before = json.dumps(inst.local, sort_keys=True)
        with mock.patch.object(upgrade._m_nodes, 'load', return_value=inst), \
                mock.patch.object(upgrade, 'changes_unread', return_value=[entry]), \
                mock.patch.object(upgrade, 'core_update', return_value=None):
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                upgrade.cmd_changes(args)
            self.assertIn(entry['id'], stream.getvalue())
            self.assertNotIn(entry['body'], stream.getvalue())
            self.assertNotIn('--seen', stream.getvalue())
            self.assertIn('brain changes root', stream.getvalue())
            args.summary = False
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                upgrade.cmd_changes(args)
            self.assertIn(entry['body'], stream.getvalue())
        self.assertEqual(json.dumps(inst.local, sort_keys=True), before)
