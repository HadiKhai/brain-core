"""Feedback closes only after executing its mapped check successfully."""
import json
from scenario import Scenario, AutonomyProject


class FeedbackProof(AutonomyProject):
    def fixture(self):
        r, home, worktree = self.project()
        check = worktree / 'checks/correction.py'
        check.parent.mkdir()
        check.write_text('raise SystemExit(1)\n')
        self.b(r, 'feedback', 'app', 'add', 'Avoid repeated headings', '--principle', 'one-place',
               '--job', 'job-a', '--check', 'checks/correction.py', '--before', 'failed example')
        return r, check

    def rows(self, r, node="root"):
        return json.loads(self.b(r, 'feedback', node, '--json'))

    def test_failed_check_stays_open_green_check_records_exact_proof(self):
        r, check = self.fixture()
        key = self.rows(r, "app")[0]['id']
        self.assertIn('remains open', self.b(r, 'feedback', 'app', 'close', key, '--after', 'fixed example', ok=False))
        self.assertEqual(self.rows(r, "app")[0]['status'], 'open')
        self.assertIn(key, self.b(r, 'due', 'app'))
        check.write_text('print("green")\n')
        self.b(r, 'feedback', 'app', 'close', key, '--after', 'fixed example')
        row = self.rows(r, "app")[0]
        self.assertEqual(row['status'], 'closed')
        self.assertEqual(row['proof']['exit_code'], 0)
        self.assertEqual(len(row['proof']['check_sha256']), 64)
        self.assertEqual(len(row['proof']['head']), 40)
        self.assertEqual(row['before'], 'failed example')
        self.assertEqual(row['after'], 'fixed example')
        self.assertNotIn(key, self.b(r, 'due', 'app'))
        self.b(r, 'feedback', 'app', 'map', key, '--check', 'checks/correction.py', '--job', 'job/correction2')
        self.assertEqual(self.rows(r, "app")[0]['status'], 'open')

    def test_unmapped_or_escaping_check_cannot_close_and_readers_do_not_write(self):
        r = self.init('alice')
        self.b(r, 'feedback', 'root', 'add', 'Keep labels distinct')
        key = self.rows(r)[0]['id']
        before = (r / 'feedback.md').read_bytes()
        self.b(r, 'feedback', 'root', 'close', key, '--after', 'a claim', ok=False)
        self.assertEqual(before, (r / 'feedback.md').read_bytes())
        self.b(r, 'feedback', 'root', 'map', key, '--check', '../outside.py', ok=False)
        self.assertEqual(before, (r / 'feedback.md').read_bytes())
        ui = json.loads(self.b(r, 'ui', '--json'))
        self.assertEqual(ui['nodes'][0]['feedback'][0]['id'], key)
        self.assertEqual(before, (r / 'feedback.md').read_bytes())
        self.b(r, 'status', 'root', '--refresh')
        self.assertIn('Open feedback: 1', (r / 'status.md').read_text())

    def test_duplicate_comments_get_distinct_ids_and_corrupt_store_is_retained(self):
        r = self.init('alice')
        for _ in range(2):
            self.b(r, 'feedback', 'root', 'add', 'Keep labels distinct')
        self.assertEqual([x['id'] for x in self.rows(r)], ['F-0001', 'F-0002'])
        (r / 'feedback.md').write_text('damaged legacy data\n')
        self.assertIn('repair feedback.md', self.b(r, 'due', 'root'))
        self.b(r, 'feedback', 'root', 'add', 'another comment', ok=False)
        self.assertEqual((r / 'feedback.md').read_text(), 'damaged legacy data\n')

    def test_check_that_rewrites_itself_cannot_close_feedback(self):
        r, check = self.fixture()
        key = self.rows(r, 'app')[0]['id']
        check.write_text('from pathlib import Path\nPath(__file__).write_text("pass\\n")\n')
        self.assertIn('remains open', self.b(r, 'feedback', 'app', 'close', key, '--after', 'changed check', ok=False))
        self.assertEqual(self.rows(r, 'app')[0]['status'], 'open')
