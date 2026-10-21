"""Frozen-input hotfix contracts; all brains and diagnostic text are fabricated."""
import contextlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from test_rehearse import RehearsalFixture, rehearsal, result


class FrozenInputs(RehearsalFixture):
    def test_one_snapshot_survives_live_edits_and_is_cleaned_up(self):
        sources = []
        copies = []
        def inspect(source, core, folder, nodes, commands):
            sources.append(source)
            self.assertNotEqual(self.root, source)
            self.assertEqual('original\n', (source / 'note.md').read_text())
            copies.append(rehearsal.snapshot(source))
        outcome = self.run_rehearsal(lambda: self.write('note.md', 'busy writer'),
                                    during_run=True, inspect_lane=inspect)
        self.assertTrue(outcome['passed'], outcome['regressions'])
        self.assertEqual(2, len(sources))
        self.assertEqual(sources[0], sources[1])
        self.assertEqual(copies[0], copies[1])
        self.assertFalse(sources[0].exists())
        self.assertEqual('busy writer', (self.root / 'note.md').read_text())

    def test_copy_time_live_edits_do_not_change_frozen_input(self):
        seen = []
        def inspect(source, *args):
            seen.append((source / 'note.md').read_text())
        outcome = self.run_rehearsal(lambda: self.write('note.md', 'busy writer'), inspect_lane=inspect)
        self.assertTrue(outcome['passed'], outcome['regressions'])
        self.assertEqual(['original\n', 'original\n'], seen)

    def test_snapshot_write_refuses_and_cleans_up(self):
        sources = []
        def inspect(source, *args):
            sources.append(source)
            (source / 'note.md').write_text('unexpected snapshot mutation')
        outcome = self.run_rehearsal(inspect_lane=inspect)
        self.assertFalse(outcome['passed'])
        self.assertIn('frozen brain changed during rehearsal; evidence refused', outcome['regressions'])
        self.assertEqual('original\n', (self.root / 'note.md').read_text())
        self.assertFalse(sources[0].exists())

    def test_identical_baseline_failure_is_named_in_evidence_and_printed(self):
        old = {'brief:0': result('refused: brief limit is exceeded', code=1), 'hook': result()}
        outcome = self.run_rehearsal(outputs=[old, old])
        warning = 'brief:0: identical baseline failure'
        self.assertTrue(outcome['passed'], outcome['regressions'])
        self.assertIn(warning, outcome['warnings'])
        node = dict(id='engine', external=True, home_path=self.home)
        inst = SimpleNamespace(root=self.root, nodes=lambda: [], node=lambda _: node)
        evidence = self.tmp / 'evidence.json'
        printed = io.StringIO()
        with mock.patch.object(rehearsal._m_nodes, 'find_root', return_value=self.root), \
                mock.patch.object(rehearsal._m_instance, 'Instance', return_value=inst), \
                mock.patch.object(rehearsal, 'evidence_path', return_value=evidence), \
                mock.patch.object(rehearsal, 'rehearse', return_value=outcome), \
                contextlib.redirect_stdout(printed):
            rehearsal.cmd_rehearse(SimpleNamespace(id='engine', branch='job'))
        self.assertIn(warning, json.loads(evidence.read_text())['warnings'])
        self.assertIn('warning: ' + warning, printed.getvalue())
        self.assertNotIn('refused: brief limit is exceeded', evidence.read_text() + printed.getvalue())

    def test_baseline_timeouts_writes_and_different_failures_still_refuse(self):
        for field, value in [('timeout', True), ('unexpected', 1), ('text', 'different failure'), ('code', 2)]:
            with self.subTest(field=field):
                old = {'brief:0': result('failure', code=1), 'hook': result()}
                new = {'brief:0': result('failure', code=1), 'hook': result()}
                if field in ('timeout', 'unexpected'):
                    old['brief:0'][field] = value
                else:
                    new['brief:0'][field] = value
                outcome = self.run_rehearsal(outputs=[old, new])
                self.assertFalse(outcome['passed'])
                self.assertIn('brief:0: baseline incomplete', outcome['regressions'])
                self.assertNotIn('brief:0: identical baseline failure', outcome.get('warnings', []))

    def test_candidate_only_failure_still_refuses(self):
        old = {'brief:0': result('healthy'), 'hook': result()}
        new = {'brief:0': result('failed', code=1), 'hook': result()}
        outcome = self.run_rehearsal(outputs=[old, new])
        self.assertFalse(outcome['passed'])
        self.assertIn('brief:0: new command failure', outcome['regressions'])

    def test_identical_non_json_due_failure_warns_and_different_result_refuses(self):
        old = {'due:0': result('cannot render due', code=1), 'hook': result()}
        outcome = self.run_rehearsal(outputs=[old, old])
        self.assertTrue(outcome['passed'], outcome['regressions'])
        self.assertEqual(['due:0: identical baseline failure'], outcome['warnings'])
        new = {'due:0': result('[]'), 'hook': result()}
        outcome = self.run_rehearsal(outputs=[old, new])
        self.assertFalse(outcome['passed'])
        self.assertIn('due:0: baseline incomplete', outcome['regressions'])

    def test_path_lengths_cannot_reorder_largest_brief_sections(self):
        from brain import briefs
        fixed_size = []
        orders = []
        def inspect(source, core, folder, nodes, commands):
            variable = '## Variable\n' + (str(folder / 'brain') + '\n') * 2 + 'x' * 3000 + '\n'
            if not fixed_size:
                fixed_size.append(len(variable.encode()) - 1)
            fixed = '## Fixed\n' + 'x' * (fixed_size[0] - len('## Fixed\n'))
            printed = io.StringIO()
            with mock.patch.object(briefs._m_guard, 'no_terms_in'), \
                    contextlib.redirect_stderr(printed):
                with self.assertRaises(SystemExit) as caught:
                    briefs.checked_brief({'fm': {'brief_max_kb': 1}}, variable + fixed, worker=False)
            text = printed.getvalue() + str(caught.exception)
            orders.append(text.index('Variable:') < text.index('Fixed:'))
            return {'brief:0': result(rehearsal.normalize(text, [folder / 'brain']), code=1),
                    'hook': result()}
        outcome = self.run_rehearsal(inspect_lane=inspect)
        self.assertEqual([True, True], orders)
        self.assertTrue(outcome['passed'], outcome['regressions'])
        self.assertIn('brief:0: identical baseline failure', outcome['warnings'])


class StableComparisons(unittest.TestCase):
    def test_due_text_changes_preserve_identity_and_duplicates_still_flag(self):
        item = dict(node='sample', kind='hold', id='one', text='old description')
        changed = {**item, 'text': 'rewritten description'}
        before = {'due:0': result(json.dumps([item]))}
        after = {'due:0': result(json.dumps([changed]))}
        self.assertEqual([], rehearsal.compare(before, after))
        after['due:0']['text'] = json.dumps([changed, changed])
        self.assertIn('due:0: new due items', rehearsal.compare(before, after))
        for field, value in [('node', 'other'), ('kind', 'other'), ('id', 'two')]:
            with self.subTest(field=field):
                after['due:0']['text'] = json.dumps([{**changed, field: value}])
                self.assertIn('due:0: new due items', rehearsal.compare(before, after))

    def test_brief_sizes_normalize_but_removed_warning_still_flags(self):
        def warning(size, section):
            return ('warning: brief is ' + size + ' bytes (brief_max_kb=8); '
                    'largest sections: Task: ' + section + ' bytes. Retain limits!')
        before = {'brief:0': result(rehearsal.normalize(warning('20582', '8816')))}
        after = {'brief:0': result(rehearsal.normalize(warning('20575', '8809')))}
        self.assertEqual([], rehearsal.compare(before, after))
        after['brief:0']['text'] = 'quiet'
        self.assertIn('brief:0: brief warning disappeared', rehearsal.compare(before, after))
        after['brief:0']['text'] = before['brief:0']['text'].replace('Retain limits!', 'Ignore consent!')
        self.assertIn('brief:0: brief warning disappeared', rehearsal.compare(before, after))

    def test_non_warning_byte_counts_are_not_normalized(self):
        self.assertNotEqual(rehearsal.normalize('payload: 10 bytes'), rehearsal.normalize('payload: 20 bytes'))

    def test_expected_notice_cannot_exempt_duplicate_of_existing_identity(self):
        notice = dict(node='root', kind='core-changes', text='candidate notice')
        before = {'due:0': result(json.dumps([{**notice, 'text': 'existing notice'}]))}
        after = {'due:0': result(json.dumps([notice, notice]))}
        self.assertIn('due:0: new due items',
                      rehearsal.compare(before, after, {'due:0': notice}))


class CandidateNotices(RehearsalFixture):
    def test_only_candidate_changes_notice_is_expected(self):
        # Real CHANGES parsing and notice attribution; fabricated subprocess results.
        old_changes = '## 2026-01-01: Existing entry\n\nOriginal body\n'
        new_changes = '## 2026-01-02: New entry\n\nNew body\n' + old_changes
        (self.root / 'core/CHANGES.md').write_text(old_changes)
        (self.home / 'CHANGES.md').write_text(new_changes)
        self.git('add', 'CHANGES.md', cwd=self.home)
        self.git('commit', '-qm', 'new notice', cwd=self.home)
        self.head = self.git('rev-parse', 'HEAD', cwd=self.home).strip()
        notice = dict(node='root', kind='core-changes', text=(
            'core changed: 1 unread entry (2026-01-02: New entry); '
            'read them: brain changes root, then brain changes root --seen'))
        fixture_node = dict(id='root', local={}, dir=self.root, fm={})
        inst = SimpleNamespace(root=self.root, local={'changes_seen': {'root': ['2026-01-01: Existing entry']}},
                               nodes=lambda: [fixture_node])
        node = dict(id='engine', external=True, home_path=self.home)
        def run(items):
            with mock.patch.dict(os.environ, self.env), \
                    mock.patch.object(rehearsal, 'sandbox_self_check'), \
                    mock.patch.object(rehearsal, 'sandbox_prefix', return_value=[]), \
                    mock.patch.object(rehearsal._m_verify, 'ready_refs', return_value=(self.head, self.head)), \
                    mock.patch.object(rehearsal._m_verify, 'integration_parent', return_value=self.head), \
                    mock.patch.object(rehearsal._m_instance, 'Instance', return_value=inst), \
                    mock.patch.object(rehearsal, 'isolated_probe_instance', return_value=inst), \
                    mock.patch.object(rehearsal._m_jobs, 'read_jobs', return_value=[]), \
                    mock.patch.object(rehearsal._m_jobs, 'stamp', return_value='fixture'), \
                    mock.patch.object(rehearsal._m_guard, 'never_terms', return_value=[]), \
                    mock.patch.object(rehearsal, 'probe_delivery', return_value=[]), \
                    mock.patch.object(rehearsal, 'run_lane', side_effect=[
                        {'due:0': result('[]'), 'hook': result()},
                        {'due:0': result(json.dumps(items)), 'hook': result()}]):
                return rehearsal.rehearse(inst, node, 'job')
        self.assertTrue(run([notice])['passed'])
        for items in [[notice, dict(node='root', kind='hold', id='new', text='extra due')],
                      [{**notice, 'text': 'unexplained new core notice'}],
                      [notice, notice]]:
            with self.subTest(items=items):
                self.assertIn('due:0: new due items', run(items)['regressions'])
        (self.home / 'CHANGES.md').write_text(old_changes)
        self.git('commit', '-qam', 'no candidate notice', cwd=self.home)
        self.head = self.git('rev-parse', 'HEAD', cwd=self.home).strip()
        self.assertIn('due:0: new due items', run([notice])['regressions'])
