"""r1 regressions: fabricated brains only, one module per focused run."""
import contextlib
import datetime
import json
import sys
import time
from pathlib import Path
from unittest import mock

from scenario import Scenario, CORE
from test_jobs import settlement_export

sys.path.insert(0, str(CORE / 'lib'))
from brain import events, instance, transitions, hosts


class ReviewRegressions(Scenario):
    def app(self, external=False):
        root = self.init('demo')
        args = ['new', 'app', '--title', 'App']
        home = self.repo(self.tmp / 'code') if external else None
        if home:
            args += ['--home', str(home)]
        self.b(root, *args)
        return root, home

    def compare(self, root):
        return json.loads(self.b(root, 'events', 'app', '--compare', '--json'))['current']

    def pending(self, root):
        return events.pending(events.read(instance.Instance(root)))

    def test_b1_short_terms_preserve_structure_hashes_and_history(self):
        root, _ = self.app()
        (root / 'never-name.txt').write_text('Ed\nab\nIve\n')
        inst = instance.Instance(root)
        raw = {'ref': 'ed' * 16, 'id': 'ab' * 32, 'hash': 'ab' * 32,
               'merged': ['ed' * 12], 'active_keys': ['ab' * 12],
               'facts': ['- Ed wrote a display value'], 'active': [['job', '[Ed](thread://fixture)', '-', 'open']]}
        screened = events.screen(inst, raw)
        self.assertEqual(set(screened), set(raw))
        for key in ('ref', 'id', 'hash', 'merged', 'active_keys'):
            self.assertEqual(screened[key], raw[key])
        self.assertNotIn('Ed', screened['facts'][0])
        old = (root / '.brain/events.jsonl').read_bytes()
        with mock.patch.object(events.uuid, 'uuid4', return_value=type('Ref', (), {'hex': 'edab' * 8})()):
            with instance.brain_lock(root):
                with events.Command(root, command='fixture', node='app'):
                    (root / 'nodes/app/backlog.md').write_text('- retained\n')
        for i in range(3):
            self.b(root, 'backlog', 'app', 'add', 'item %d' % i)
        self.assertEqual(self.pending(root), {})
        self.assertTrue(self.compare(root)['equal'])
        self.b(root, 'status', 'app', '--from-events')
        self.assertEqual((root / '.brain/events.jsonl').read_bytes()[:len(old)], old)

    def test_b1_screen_failure_after_mutation_still_pairs_the_operation(self):
        root, _ = self.app()
        original = events.screen
        with instance.brain_lock(root):
            with mock.patch.object(events, 'screen', wraps=original) as screen:
                with events.Command(root, command='fixture', node='app'):
                    (root / 'nodes/app/backlog.md').write_text('- saved\n')
                    transitions.inbox_append(root / 'nodes/app', '- hook: saved\n', nonblocking=True)
                    screen.side_effect = ValueError('injected screen failure')
        self.assertEqual(self.pending(root), {})
        self.b(root, 'status', 'app', '--from-events', ok=False)
        self.b(root, 'status', 'app', '--refresh')
        self.assertTrue(self.compare(root)['equal'])

    def test_b1_protected_node_display_does_not_hide_its_resource_gaps(self):
        root, _ = self.app()
        self.b(root, 'new', 'ed', '--title', 'Fixture')
        (root / 'never-name.txt').write_text('Ed\n')
        self.b(root, 'backlog', 'ed', 'add', 'observed')
        (root / 'nodes/ed/backlog.md').write_text('- hookless edit\n')
        inst = instance.Instance(root)
        self.assertFalse(events.comparison(inst, inst.node('ed'), record=False)['equal'])

    def test_b2_not_ready_is_a_completed_write_with_a_verdict(self):
        root, _ = self.app(external=True)
        self.b(root, 'wt', 'add', 'app', 'job-one', '--no-setup')
        out = self.b(root, 'handback', 'app', '--branch', 'job-one', '--state', 'done', '--summary', 'PRIVATE-SUMMARY',
                     '--reviews', '1', '--lessons', 'none: fixture', ok=False)
        self.assertIn('not ready', out)
        self.assertTrue(list((root / 'nodes/app/job-handbacks').glob('*.json')))
        self.assertEqual(self.pending(root), {})
        self.assertTrue(self.compare(root)['equal'])
        self.b(root, 'status', 'app', '--from-events')

    def test_b2_fix_with_remaining_problems_commits_completed_repairs(self):
        root, _ = self.app()
        path = root / 'nodes/app/node.md'
        path.write_text(path.read_text().replace('owners: ', 'owners: ghost'))
        (root / 'nodes/app/backlog.md').unlink()
        self.b(root, 'check', '--fix', ok=False)
        self.assertTrue((root / 'nodes/app/backlog.md').exists())
        self.assertEqual(self.pending(root), {})
        commits = [e for e in events.read(instance.Instance(root)) if e['kind'] == 'commit']
        self.assertEqual(commits[-1]['data'].get('exit'), 1)

    def test_m3_torn_outbox_does_not_block_writes_and_refresh_repairs(self):
        root, _ = self.app()
        (root / '.brain/hook-events.jsonl').write_bytes(b'{"broken":')
        transitions.inbox_append(root / 'nodes/app', '- runtime: question\n', nonblocking=True)
        self.b(root, 'set', 'app', 'note=saved')
        self.assertTrue(list((root / '.brain').glob('hook-events.rejected*')))
        self.assertFalse(self.compare(root)['equal'])
        self.b(root, 'status', 'app', '--from-events', ok=False)
        self.b(root, 'status', 'app', '--refresh')
        self.assertTrue(self.compare(root)['equal'])
        self.assertEqual(self.pending(root), {})

    def test_m3_bad_journal_rotation_line_and_cursor_are_quarantined(self):
        root, _ = self.app()
        journal = root / '.brain/events.jsonl'
        journal.write_bytes(b'not-json\n' + journal.read_bytes())
        (root / '.brain/hook-events-offset').write_text('bad cursor')
        transitions.inbox_append(root / 'nodes/app', '- runtime: retained\n', nonblocking=True)
        self.b(root, 'set', 'app', 'note=still-writable')
        self.assertFalse(self.compare(root)['equal'])
        self.b(root, 'status', 'app', '--refresh')
        self.assertTrue(self.compare(root)['equal'])

    def test_m4_hook_fold_cannot_overwrite_a_newer_inbox_hash(self):
        root, _ = self.app()
        with instance.brain_lock(root):
            with events.Command(root, command='inbox', node='app'):
                transitions.inbox_append(root / 'nodes/app', '- hook: first\n', nonblocking=True)
                transitions.inbox_append(root / 'nodes/app', '- writer: later\n')
        self.b(root, 'backlog', 'app', 'add', 'unrelated')
        self.assertTrue(self.compare(root)['equal'])

    def test_m3_valid_json_with_invalid_shapes_and_comparison_damage_is_recoverable(self):
        root, _ = self.app()
        inst = instance.Instance(root)
        bad = {'ts': inst.now(), 'node': 'app', 'kind': 'hook', 'ref': 'a' * 32,
               'actor': 'brain', 'data': {'resources': [None]}}
        events.append_line(root / '.brain/events.jsonl', bad)
        events.append_line(root / '.brain/hook-events.jsonl', bad)
        with (root / '.brain/events-compare.jsonl').open('ab') as stream:
            stream.write(b'{"broken":\n')
        self.b(root, 'backlog', 'app', 'add', 'saved through advisory damage')
        self.assertFalse(self.compare(root)['equal'])
        self.b(root, 'status', 'app', '--refresh')
        self.assertTrue(self.compare(root)['equal'])

    def test_m5_external_git_and_clock_changes_are_not_unexplained(self):
        root, home = self.app(external=True)
        self.b(root, 'wt', 'add', 'app', 'job-one', '--no-setup')
        self.b(root, 'status', 'app', '--refresh')
        local = json.loads((root / '.brain/local.json').read_text())
        wt = Path(local['nodes']['app']['worktrees']['job-one']['path'])
        self.git('commit', '--allow-empty', '-qm', 'worker', cwd=wt)
        self.git('merge', '-q', 'job-one', cwd=home)
        self.assertTrue(self.compare(root)['equal'])
        self.b(root, 'handback', 'app', '--branch', 'job-one', '--state', 'question', '--summary', 'PRIVATE-SUMMARY', '--question', 'PRIVATE-QUESTION')
        inst = instance.Instance(root)
        future = inst.now_dt() + datetime.timedelta(days=2)
        with mock.patch.object(inst, 'now_dt', return_value=future):
            self.assertTrue(events.comparison(inst, inst.node('app'), record=False)['equal'])
        ledger = root / 'nodes/app/job-ledger.md'
        ledger.write_text(ledger.read_text().replace('| 0 |', '| 2 |', 1))
        self.assertFalse(self.compare(root)['equal'])  # the real coverage detector must remain

    def test_m6_ten_thousand_records_do_not_cause_repeated_history_reads(self):
        root, _ = self.app()
        journal = root / '.brain/events.jsonl'
        rows = []
        for i in range(5000):
            ref = '%032x' % (i + 1)
            for kind in ('begin', 'abort'):
                rows.append(json.dumps({'ts': '2026-01-01 00:00 +00:00', 'node': 'root', 'kind': kind,
                                        'actor': 'brain', 'ref': ref, 'data': {'command': 'fixture'}}) + '\n')
        with journal.open('a') as stream:
            stream.write(''.join(rows))
        start = time.monotonic()
        with mock.patch.object(events, 'read', wraps=events.read) as read:
            with instance.brain_lock(root):
                with events.Command(root, command='fixture', node='app'):
                    (root / 'nodes/app/backlog.md').write_text('- indexed\n')
        self.assertLessEqual(read.call_count, 1)
        self.assertLess(time.monotonic() - start, 5.0)

    def test_m6_comparison_index_keeps_the_day_and_seeks_only_the_tail(self):
        root, _ = self.app()
        inst = instance.Instance(root)
        path = root / '.brain/events-compare.jsonl'
        old = (inst.now_dt() - datetime.timedelta(days=2)).strftime('%Y-%m-%d %H:%M +00:00')
        with path.open('w') as stream:
            for i in range(10000):
                stream.write(json.dumps({'id': str(i), 'ts': old, 'node': 'app', 'kind': 'coverage', 'equal': True}) + '\n')
            stream.write(json.dumps({'id': 'first-real', 'ts': old, 'node': 'app', 'kind': 'comparison', 'equal': True}) + '\n')
        events.read_comparisons(inst, repair=True)
        consumed = path.stat().st_size
        events.append_line(path, {'id': 'recent', 'ts': inst.now(), 'node': 'app', 'kind': 'comparison', 'equal': True})
        original_open, seeks = events.private_open, []
        class Reader:
            def __init__(self, stream): self.stream = stream
            def __enter__(self): return self
            def __exit__(self, *args): self.stream.close()
            def __iter__(self): return iter(self.stream)
            def __getattr__(self, name): return getattr(self.stream, name)
            def seek(self, offset):
                seeks.append(offset)
                return self.stream.seek(offset)
        @contextlib.contextmanager
        def opened(p, *args, **kwargs):
            with original_open(p, *args, **kwargs) as stream:
                yield Reader(stream) if p == path else stream
        with mock.patch.object(events, 'private_open', opened):
            rows = events.read_comparisons(inst)
        self.assertEqual({r['id'] for r in rows}, {'first-real', 'recent'})
        self.assertTrue(seeks and min(seeks) >= consumed - 256, seeks)

    def test_m7_stop_hook_ignores_generated_dirt_but_keeps_now_edits(self):
        root, _ = self.app()
        request = self.b(root, 'inbox', 'app', '--add', 'needs attention').strip()
        self.b(root, 'queue', 'wait', request)
        inst = instance.Instance(root)
        self.assertEqual(hosts.notes_lag(inst, inst.node('app')), [])
        path = root / 'nodes/app/status.md'
        path.write_text(path.read_text().replace('- Node created.', '- Human changed Now.'))
        self.assertTrue(hosts.notes_lag(instance.Instance(root), instance.Instance(root).node('app')))

    def test_m8_markdown_thread_annotations_survive_writes_and_close(self):
        root, _ = self.app(external=True)
        self.b(root, 'wt', 'add', 'app', 'job-one', '--no-setup')
        path = root / 'nodes/app/status.md'
        link = '[job-one \\| details](t3-thread://v1/fixture-1) (Codex, high)'
        path.write_text(path.read_text().replace('| job-one | - | - | open |',
                          '| job-one | ' + link + ' | #7 | abc123 on def456; review running |'))
        self.b(root, 'backlog', 'app', 'add', 'keep links')
        self.assertIn(link, path.read_text())
        self.b(root, 'jobs', 'app', '--branch', 'job-one', '--close', '--reason', 'fixture complete')
        self.assertIn(link, path.read_text())
        self.b(root, 'jobs', 'app', '--branch', 'job-one', '--settled', '--thread', 'fixture-1',
               '--attestation-file', settlement_export(self, root, 'app', branch='job-one'))
        self.assertNotIn('| job-one |', path.read_text())

    def test_m8_first_indexed_write_does_not_rewrite_unrelated_nodes(self):
        root, _ = self.app()
        self.b(root, 'new', 'other', '--title', 'Other')
        path = root / 'nodes/other/status.md'
        path.write_text(path.read_text().replace('- Node created.', '\n'.join('- note %d' % i for i in range(40))))
        old = path.read_bytes()
        (root / '.brain/events.jsonl').write_bytes(b'')
        (root / '.brain/events-index.json').unlink(missing_ok=True)
        self.b(root, 'backlog', 'app', 'add', 'only app')
        self.assertEqual(path.read_bytes(), old)

    def test_m9_maintenance_failure_does_not_report_primary_write_as_failed(self):
        root, _ = self.app()
        path = root / 'nodes/app/status.md'
        path.write_text(path.read_text() + '\n<!-- brain:facts v1 -->\n')
        request = self.b(root, 'inbox', 'app', '--add', 'retry would duplicate').strip()
        out = self.b(root, 'queue', 'wait', request)
        self.assertEqual(json.loads(out)['state'], 'applied')
        self.assertTrue((root / '.brain/events-repair-needed.json').exists() or
                        any(e['kind'] == 'commit' and not e['data'].get('maintenance_ok', True)
                            for e in events.read(instance.Instance(root))))
        self.assertEqual((root / 'nodes/app/inbox.md').read_text().count('retry would duplicate'), 1)
        self.assertEqual(self.pending(root), {})

    def test_minor10_coverage_alone_cannot_establish_the_comparison_day(self):
        root, _ = self.app()
        inst = instance.Instance(root)
        old = inst.now_dt() - datetime.timedelta(days=2)
        events.append_line(root / '.brain/events-compare.jsonl',
                           {'ts': old.strftime('%Y-%m-%d %H:%M +00:00'), 'node': 'app',
                            'kind': 'coverage', 'equal': True, 'view_equal': True})
        self.b(root, 'status', 'app', '--refresh')
        self.b(root, 'set', 'app', 'status_source=events', ok=False)

    def test_minor11_brains_now_cap_can_be_committed_with_its_archive(self):
        root, _ = self.app()
        path = root / 'nodes/app/status.md'
        path.write_text(path.read_text().replace('- Node created.', '\n'.join('- note %d' % i for i in range(40))))
        self.git('add', 'nodes/app/status.md', cwd=root)
        self.git('commit', '-qm', 'coordinator notes', cwd=root)
        self.b(root, 'backlog', 'app', 'add', 'cap committed notes')
        inst = instance.Instance(root)
        self.assertEqual(hosts.notes_lag(inst, inst.node('app')), [])
        with mock.patch.object(inst, '_commit') as commit:
            events.status_commit(inst, inst.node('app'), force=True)
            commit.assert_called_once()
            self.assertIn('nodes/app/status-archive.md', commit.call_args[0][1])
        path.write_text(path.read_text() + '\nA human edit.\n')
        with mock.patch.object(inst, '_commit') as commit:
            events.status_commit(inst, inst.node('app'), force=True)
            commit.assert_not_called()

    def test_minor13_disappearing_lease_is_dead(self):
        root, _ = self.app()
        inst = instance.Instance(root)
        ref = 'a' * 32
        events.lease_path(inst, ref, create=True).touch()
        with mock.patch.object(events.os, 'open', side_effect=FileNotFoundError('unlinked')):
            self.assertFalse(events.lease_live(inst, ref))
