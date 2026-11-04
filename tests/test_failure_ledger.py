"""Dated failure ledger writes use only a temporary instance."""
import datetime
import sys
from unittest import mock

from scenario import CORE, Scenario
sys.path.insert(0, str(CORE / 'lib'))
from brain import failure_ledger
from brain.instance import Instance


class FailureLedger(Scenario):
    def setUp(self):
        super().setUp()
        self.root = self.init('alice')
        self.path = self.root / 'library/failure-ledger.md'

    def add(self, **changes):
        args = dict(failed='Fixture failed', first='Initial guess', cause='Wrong slot', found_by='agent', fix='Correct slot')
        args.update(changes)
        inst = Instance(self.root)
        with mock.patch.object(Instance, 'now_dt', return_value=datetime.datetime.fromisoformat('2030-02-03T00:00:00+00:00')):
            return failure_ledger.add(inst, **args)

    def test_cli_creates_ledger_and_date_heading(self):
        self.b(self.root, 'ledger', 'add', '--failed', 'Fixture failed', '--first-explanation', 'Initial guess',
               '--cause', 'Wrong slot', '--found-by', 'agent', '--fix', 'Correct slot')
        text = self.path.read_text()
        self.assertRegex(text, r'## \d{4}-\d{2}-\d{2}')
        self.assertIn(failure_ledger.HEADER, text)
        self.assertIn('| 1 | Fixture failed | Initial guess | Wrong slot | agent | Correct slot |', text)

    def test_existing_day_appends_before_next_date_and_keeps_history_bytes(self):
        before = '# Failure ledger\n\n## 2030-02-03 (session)\n\n' + failure_ledger.HEADER + '| 4 | Old | Guess | Cause | agent | Fix |\n'
        after = '\nCount: 1\n\n## 2030-02-04\n\nLater evidence\n'
        self.path.write_text(before + after)
        self.add()
        self.assertEqual(self.path.read_text(), before + '| 5 | Fixture failed | Initial guess | Wrong slot | agent | Correct slot |\n' + after)

    def test_new_day_and_escaped_cells_remain_one_row(self):
        self.path.write_text('# Failure ledger\n\n## 2030-02-02\nOld evidence\n')
        self.add(failed='Pipe | slash \\ newline\nheading ## <x>')
        text = self.path.read_text()
        self.assertEqual(text.count('## 2030-02-03'), 1)
        row = text.splitlines()[-1]
        self.assertEqual(row.count('|'), 7)
        self.assertIn('&#124;', row)
        self.assertIn('<br>', row)
        self.assertIn('&lt;x&gt;', row)
        self.add()
        self.assertEqual(self.path.read_text().count('## 2030-02-03'), 1)
        self.assertIn('| 2 | Fixture failed', self.path.read_text())

    def test_invalid_fields_and_unrecognised_existing_table_leave_bytes(self):
        self.path.write_text('# Failure ledger\n\n## 2030-02-03\nCustom content\n')
        original = self.path.read_bytes()
        for changes in ({'found_by': 'unknown'}, {'cause': ''}, {}):
            with self.assertRaises(ValueError):
                self.add(**changes)
            self.assertEqual(self.path.read_bytes(), original)

    def test_empty_existing_table_and_similar_date_heading(self):
        before = '# Failure ledger\n\n## 2030-02-030\nUnrelated date-like heading\n'
        self.path.write_text(before + '\n## 2030-02-03\n\n' + failure_ledger.HEADER)
        self.add()
        self.assertTrue(self.path.read_text().startswith(before))
        self.assertIn('| 1 | Fixture failed', self.path.read_text())

    def test_append_stays_in_recognised_table_when_another_table_follows(self):
        before = '# Failure ledger\n\n## 2030-02-03\n\n' + failure_ledger.HEADER + '| 4 | Old | Guess | Cause | agent | Fix |\n'
        after = '\nOther table:\n\n| # | Sample |\n|---|---|\n| 99 | Retain |\n'
        self.path.write_text(before + after)
        self.add()
        self.assertEqual(self.path.read_text(), before + '| 5 | Fixture failed | Initial guess | Wrong slot | agent | Correct slot |\n' + after)

    def test_outside_link_is_refused_without_changing_target(self):
        outside = self.tmp / 'outside.md'
        outside.write_text('Retain this evidence\n')
        self.path.symlink_to(outside)
        with self.assertRaises((SystemExit, OSError)):
            self.add()
        self.assertEqual(outside.read_text(), 'Retain this evidence\n')
