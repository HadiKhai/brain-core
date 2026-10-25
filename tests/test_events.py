"""Metadata-only event stream scenarios; all data and instances are fabricated."""
import argparse
import importlib
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from scenario import Scenario, CORE
from test_jobs import settlement_export

sys.path.insert(0, str(CORE / 'lib'))
from brain import events, instance, transitions


class EventAppend(Scenario):
    def records(self, root):
        return events.read(instance.Instance(root))

    def commits(self, root):
        return [e for e in self.records(root) if e['kind'] == 'commit']

    def test_one_commit_per_write_and_no_message_bodies(self):
        r = self.init('demo')
        self.b(r, 'new', 'app', '--title', 'App')
        count = len(self.commits(r))
        self.b(r, 'backlog', 'app', 'add', 'BODY-MUST-STAY-IN-FILE')
        self.assertEqual(len(self.commits(r)), count + 1)
        self.assertIn('BODY-MUST-STAY-IN-FILE', (r / 'nodes/app/backlog.md').read_text())
        self.assertNotIn('BODY-MUST-STAY-IN-FILE', (r / '.brain/events.jsonl').read_text())
        self.assertIn('nodes/app/backlog.md', [x['path'] for x in self.commits(r)[-1]['data']['resources']])
        self.b(r, 'set', 'app', 'note=SECOND-BODY')
        self.assertEqual(len(self.commits(r)), count + 2)
        self.assertNotIn('SECOND-BODY', (r / '.brain/events.jsonl').read_text())
        self.assertTrue((r / 'backlog.md').exists())
        self.assertIn('all checks pass', self.b(r, 'check'))
        self.assertNotIn('.brain/', self.git('ls-files', cwd=r))

    def test_rejected_write_aborts_without_commit(self):
        r = self.init('demo')
        self.b(r, 'new', 'app', '--title', 'App')
        count = len(self.commits(r))
        self.b(r, 'set', 'app', 'id=other', ok=False)
        self.assertEqual(len(self.commits(r)), count)
        self.assertEqual(self.records(r)[-1]['kind'], 'abort')
        self.assertEqual(events.pending(self.records(r)), {})

    def test_hook_outbox_has_no_lock_no_text_and_fold_is_idempotent(self):
        r = self.init('demo')
        n = r / 'nodes/app'
        self.b(r, 'new', 'app', '--title', 'App')
        with mock.patch.object(instance.brain_lock, '__enter__', side_effect=AssertionError('hook took lock')):
            self.assertTrue(transitions.inbox_append(n, '- runtime: INBOX-PRIVATE\n', nonblocking=True))
        outbox = r / '.brain/hook-events.jsonl'
        self.assertNotIn('INBOX-PRIVATE', outbox.read_text())
        inst = instance.Instance(r)
        with instance.brain_lock(inst):
            events.fold_hooks(inst)
            (r / '.brain/hook-events-offset').write_text('0')  # crash after journal append, before cursor publication
            events.fold_hooks(inst)
        hooks = [e for e in self.records(r) if e['kind'] == 'hook']
        self.assertEqual(len(hooks), 1)
        self.assertEqual(hooks[0]['data']['at'], json.loads(outbox.read_text())['ts'])

    def test_protected_metadata_is_screened_and_fsync_is_required(self):
        r = self.init('demo')
        self.b(r, 'new', 'app', '--title', 'App')
        (r / 'never-name.txt').write_text('hiddenword\n')
        self.b(r, 'backlog', 'app', 'add', 'a hiddenword detail')
        self.assertNotIn('hiddenword', (r / '.brain/events.jsonl').read_text().casefold())
        path = r / '.brain/probe.jsonl'
        with mock.patch.object(events.os, 'fsync', side_effect=OSError('injected fsync failure')):
            with self.assertRaises(OSError):
                events.append_line(path, {'test': True})

    def test_rotation_preserves_records_and_unresolved_operations(self):
        r = self.init('demo')
        inst = instance.Instance(r)
        (r / '.brain/events.jsonl').write_text('')
        with instance.brain_lock(inst):
            with mock.patch.object(inst, 'now', return_value='2026-01-31 23:59 +00:00'):
                events.append(inst, 'root', 'begin', 'a' * 32, {'command': 'fixture'})
            with mock.patch.object(inst, 'now', return_value='2026-02-01 00:00 +00:00'):
                events.append(inst, 'root', 'commit', 'a' * 32, {'resources': []})
        self.assertTrue((r / '.brain/events-2026-01.jsonl').exists())
        self.assertEqual(events.pending(self.records(r)), {})

    def test_partial_failure_leaves_begin_and_live_lease_does_not_look_dead(self):
        r = self.init('demo')
        with instance.brain_lock(r):
            with self.assertRaises(RuntimeError):
                with events.Command(r, command='fixture') as op:
                    self.assertTrue(events.lease_live(op.inst, op.ref))
                    (r / 'backlog.md').write_text('- retained\n')
                    raise RuntimeError('injected after persistence')
        opened = events.pending(self.records(r))
        self.assertEqual(len(opened), 1)
        self.assertFalse(events.lease_live(instance.Instance(r), next(iter(opened))))

    def test_linked_journal_refuses_before_mutation(self):
        r = self.init('demo')
        self.b(r, 'new', 'app', '--title', 'App')
        journal = r / '.brain/events.jsonl'
        journal.unlink()
        journal.symlink_to(r / 'backlog.md')
        before = (r / 'nodes/app/node.md').read_bytes()
        target_before = (r / 'backlog.md').read_bytes()
        self.b(r, 'set', 'app', 'note=not-written', ok=False)
        self.assertEqual((r / 'nodes/app/node.md').read_bytes(), before)
        self.assertEqual((r / 'backlog.md').read_bytes(), target_before)

    def test_failed_setup_aborts_after_hook_without_masking_primary_exception(self):
        r = self.init('demo')
        self.b(r, 'new', 'app', '--title', 'App')
        for error in (SystemExit(7), KeyboardInterrupt('fixture cancellation'), RuntimeError('fixture primary')):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(type(error)) as caught:
                    with instance.brain_lock(r):
                        with events.Command(r, command='wt_setup', node='app') as operation:
                            transitions.inbox_append(r / 'nodes/app', '- runtime: PRIVATE-EXIT\n', nonblocking=True)
                            raise error
                self.assertIs(caught.exception, error)
                records = self.records(r)
                terminal = [e for e in records if e['ref'] == operation.ref][-1]
                self.assertEqual(terminal['kind'], 'abort')
                self.assertEqual(terminal['data']['command'], 'wt_setup')
                self.assertEqual(events.pending(records), {})
                hook = [e for e in records if e['kind'] == 'hook'][-1]
                self.assertEqual(hook['data']['resources'][0]['hash'],
                                 events.digest((r / 'nodes/app/inbox.md').read_bytes()))
                self.assertNotIn('PRIVATE-EXIT', (r / '.brain/events.jsonl').read_text())

    def test_failed_setup_with_hook_and_partial_write_stays_pending(self):
        r = self.init('demo')
        self.b(r, 'new', 'app', '--title', 'App')
        error = RuntimeError('fixture primary')
        with self.assertRaises(RuntimeError) as caught:
            with instance.brain_lock(r):
                with events.Command(r, command='wt_setup', node='app') as operation:
                    transitions.inbox_append(r / 'nodes/app', '- runtime: fixture exit\n', nonblocking=True)
                    (r / 'nodes/app/backlog.md').write_text('- retained partial write\n')
                    raise error
        self.assertIs(caught.exception, error)
        records = self.records(r)
        self.assertEqual(set(events.pending(records)), {operation.ref})
        self.assertFalse(any(e['ref'] == operation.ref and e['kind'] in ('abort', 'commit') for e in records))
        self.assertTrue(any(e['kind'] == 'hook' for e in records))
        self.assertEqual((r / 'nodes/app/backlog.md').read_text(), '- retained partial write\n')

    def test_scripted_day_records_each_brain_write_and_omits_external_git(self):
        r = self.init('demo')
        home = self.repo(self.tmp / 'code')
        self.b(r, 'new', 'app', '--title', 'App', '--home', str(home))
        count = len(self.commits(r))
        commands = [
            ('set', 'app', 'preview=none'),
            ('wt', 'add', 'app', 'job-one', '--no-setup'),
            ('handback', 'app', '--branch', 'job-one', '--state', 'done', '--summary', 'PRIVATE-SUMMARY', '--lessons', 'none: PRIVATE-LESSON'),
            ('review', 'app', '--branch', 'job-one', '--built-by', 'codex', '--by', 'claude'),
            ('inbox', 'app', '--add', 'PRIVATE-INBOX'),
            ('schedule', 'add', 'tick', '--node', 'app', '--when', 'daily', '--brief', 'PRIVATE-SCHEDULE'),
        ]
        for args in commands:
            with self.subTest(command=args[0]):
                self.b(r, *args)
                count += 1
                self.assertEqual(len(self.commits(r)), count)
        local = json.loads((r / '.brain/local.json').read_text())
        wt = Path(local['nodes']['app']['worktrees']['job-one']['path'])
        self.git('commit', '--allow-empty', '-qm', 'worker change', cwd=wt)
        self.git('merge', '-q', 'job-one', cwd=home)
        self.assertEqual(len(self.commits(r)), count)
        self.b(r, 'jobs', 'app', '--branch', 'job-one', '--lessons', 'none: PRIVATE-LESSON')
        self.b(r, 'wt', 'prune', 'app', '--offline')
        self.assertEqual(len(self.commits(r)), count + 2)
        journal = (r / '.brain/events.jsonl').read_text()
        for message in ('PRIVATE-SUMMARY', 'PRIVATE-LESSON', 'PRIVATE-INBOX', 'PRIVATE-SCHEDULE'):
            self.assertNotIn(message, journal)
        self.assertEqual(events.pending(self.records(r)), {})
        inst = instance.Instance(r)
        self.assertEqual(events.normalize(events.events_view(inst, inst.node('app'))),
                         events.normalize(events.snapshot_view(inst, inst.node('app'))))


class EventViews(Scenario):
    records = EventAppend.records
    commits = EventAppend.commits
    def view(self, root, node='app'):
        return json.loads(self.b(root, 'status', node, '--from-events', '--json'))

    def test_events_view_matches_refreshed_snapshot_and_never_reads_node_sources(self):
        r = self.init('demo')
        self.b(r, 'new', 'app', '--title', 'App')
        self.b(r, 'inbox', 'app', '--add', 'MESSAGE-PRIVATE')
        self.b(r, 'status', 'app', '--refresh')
        inst = instance.Instance(r)
        expected = events.snapshot_view(inst, inst.node('app'))
        actual = self.view(r)
        self.assertEqual(events.normalize(actual), events.normalize(expected))
        with mock.patch.object(events, 'snapshot_view', side_effect=AssertionError('read snapshot')):
            self.assertEqual(events.events_view(inst, inst.node('app')), actual)
        self.assertNotIn('MESSAGE-PRIVATE', json.dumps(actual))
        self.assertTrue(json.loads(self.b(r, 'events', 'app', '--compare', '--json'))['current']['equal'])

    def test_missing_event_is_detected_before_next_write_and_reconciled_by_refresh(self):
        r = self.init('demo')
        self.b(r, 'new', 'app', '--title', 'App')
        self.b(r, 'backlog', 'app', 'add', 'retained item')
        path = r / '.brain/events.jsonl'
        lines = path.read_text().splitlines()
        removed = json.loads(lines.pop())
        self.assertEqual(removed['kind'], 'commit')
        path.write_text('\n'.join(lines) + '\n')  # mutation control: skip the terminal hook
        self.b(r, 'status', 'app', '--from-events', ok=False)
        result = json.loads(self.b(r, 'events', 'app', '--compare', '--json'))
        self.assertFalse(result['current']['equal'])
        self.assertGreater(result['differences'], 0)
        self.b(r, 'set', 'app', 'runtime=codex')  # must not hide the original gap
        self.b(r, 'status', 'app', '--refresh')
        result = json.loads(self.b(r, 'events', 'app', '--compare', '--json'))
        self.assertGreater(result['differences'], 0)
        self.assertTrue(result['current']['equal'])
        self.assertTrue(any(e['kind'] == 'reconcile' and e['data']['flagged'] for e in self.records(r)))
        self.assertIn('retained item', (r / 'nodes/app/backlog.md').read_text())
        self.view(r)

    def test_live_operation_cannot_be_reconciled_and_event_reader_fails_closed(self):
        r = self.init('demo')
        self.b(r, 'new', 'app', '--title', 'App')
        inst = instance.Instance(r)
        with instance.brain_lock(r):
            with events.Command(r, command='fixture', node='app'):
                before = len(self.records(r))
                events.reconcile(inst)
                self.assertEqual(len(self.records(r)), before)
                with self.assertRaises(SystemExit):
                    events.events_view(inst, inst.node('app'))

    def test_switch_is_explicit_and_requires_a_clean_day(self):
        r = self.init('demo')
        self.b(r, 'new', 'app', '--title', 'App')
        self.b(r, 'set', 'app', 'status_source=events', ok=False)
        self.assertNotIn('status_source: events', (r / 'nodes/app/node.md').read_text())
        inst = instance.Instance(r)
        first = inst.now_dt() - __import__('datetime').timedelta(days=1, minutes=1)
        with mock.patch.object(inst, 'now', return_value=first.strftime('%Y-%m-%d %H:%M +00:00')):
            events.append_line(r / '.brain/events-compare.jsonl', {'ts': inst.now(), 'node': 'app', 'kind': 'comparison', 'equal': True})
        self.b(r, 'status', 'app', '--refresh')
        self.b(r, 'set', 'app', 'status_source=events')
        self.assertIn('status_source: events', (r / 'nodes/app/node.md').read_text())
        self.b(r, 'backlog', 'app', 'add', 'new item after switch')
        self.b(r, 'status', 'app', '--refresh')
        self.view(r)
        self.assertTrue((r / 'nodes/app/status.md').exists())

    def test_facts_refresh_on_write_active_table_and_now_archive_without_extra_commits(self):
        r = self.init('demo')
        home = self.repo(self.tmp / 'code')
        self.b(r, 'new', 'app', '--title', 'App', '--home', str(home))
        self.b(r, 'wt', 'add', 'app', 'job-one', '--no-setup')
        st = r / 'nodes/app/status.md'
        self.assertIn('| job-one | - | - | open |', st.read_text())
        text = st.read_text()
        start = text.index('## Now\n')
        end = text.index('## Next', start)
        text = text[:start] + '## Now\n\n' + ''.join('- note %d\n' % i for i in range(35)) + '\n' + text[end:]
        st.write_text(text)
        count = int(self.git('rev-list', '--count', 'HEAD', cwd=r))
        self.b(r, 'backlog', 'app', 'add', 'second item')
        self.assertEqual(int(self.git('rev-list', '--count', 'HEAD', cwd=r)), count + 1)
        now = st.read_text().split('## Now\n', 1)[1].split('## Next', 1)[0]
        self.assertEqual(sum(bool(line.strip()) for line in now.splitlines()), 30)
        archive = (r / 'nodes/app/status-archive.md').read_text()
        for i in range(30, 35):
            self.assertIn('- note %d\n' % i, archive)
        self.b(r, 'handback', 'app', '--branch', 'job-one', '--state', 'blocked', '--summary', 'PRIVATE-SUMMARY', '--lessons', 'none: PRIVATE-BODY')
        self.assertIn('| job-one | - | - | blocked |', st.read_text())
        self.assertNotIn('PRIVATE-BODY', (r / '.brain/events.jsonl').read_text())

    def test_hook_fold_refreshes_its_node_even_when_next_command_targets_another(self):
        r = self.init('demo')
        self.b(r, 'new', 'app', '--title', 'App')
        self.b(r, 'new', 'other', '--title', 'Other')
        transitions.inbox_append(r / 'nodes/app', '- runtime: private question\n', nonblocking=True)
        self.b(r, 'status', 'app', '--from-events', ok=False)
        self.b(r, 'backlog', 'other', 'add', 'different node')
        actual = self.view(r)
        inst = instance.Instance(r)
        self.assertEqual(events.normalize(actual), events.normalize(events.snapshot_view(inst, inst.node('app'))))

    def test_missing_entire_hook_is_detected_and_explanation_does_not_store_message(self):
        r = self.init('demo')
        self.b(r, 'new', 'app', '--title', 'App')
        self.b(r, 'backlog', 'app', 'add', 'persisted without hooks')
        path = r / '.brain/events.jsonl'
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        ref = rows[-1]['ref']
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows if row['ref'] != ref))
        self.b(r, 'status', 'app', '--refresh')
        result = json.loads(self.b(r, 'events', 'app', '--compare', '--json'))
        self.assertGreater(result['differences'], 0)
        self.assertTrue(result['current']['equal'])
        self.assertTrue(result['unexplained'])
        key = result['unexplained'][0]
        self.b(r, 'events', 'app', '--compare', '--explain', key, '--reason', 'PRIVATE-EXPLANATION')
        self.assertNotIn(key, json.loads(self.b(r, 'events', 'app', '--compare', '--json'))['unexplained'])
        self.assertNotIn('PRIVATE-EXPLANATION', path.read_text())

    def test_job_setup_has_its_own_operation_and_failure_keeps_state_event(self):
        r = self.init('demo')
        home = self.repo(self.tmp / 'code')
        self.b(r, 'new', 'app', '--title', 'App', '--home', str(home))
        (r / 'nodes/app/worktree-setup.sh').write_text('exit 7\n')
        self.b(r, 'wt', 'add', 'app', 'setup-job', ok=False)
        records = self.records(r)
        self.assertTrue(any(e['kind'] == 'commit' and e['data'].get('command') == 'wt_add' for e in records))
        self.assertTrue(any(e['kind'] == 'abort' and e['data'].get('command') == 'wt_setup' for e in records))
        self.assertEqual(events.pending(records), {})
        self.assertIn('setup-job', (r / 'nodes/app/job-ledger.md').read_text())

    def test_partial_hook_line_is_quarantined_without_truncating_outbox(self):
        r = self.init('demo')
        self.b(r, 'new', 'app', '--title', 'App')
        outbox = r / '.brain/hook-events.jsonl'
        outbox.write_bytes(b'{"unfinished":')
        inst = instance.Instance(r)
        with instance.brain_lock(r):
            events.fold_hooks(inst)
        self.assertEqual(outbox.read_bytes(), b'{"unfinished":')
        self.assertEqual((r / '.brain/hook-events-offset').read_text(), str(len(b'{"unfinished":')))
        self.assertTrue(list((r / '.brain').glob('hook-events.rejected.*')))
        with self.assertRaises(SystemExit):
            events.events_view(inst, inst.node('app'))

    def test_active_work_preserves_thread_refs_through_close_and_until_settlement(self):
        r = self.init('demo')
        home = self.repo(self.tmp / 'code')
        self.b(r, 'new', 'app', '--title', 'App', '--home', str(home))
        self.b(r, 'wt', 'add', 'app', 'job-one', '--no-setup')
        st = r / 'nodes/app/status.md'
        st.write_text(st.read_text().replace('| job-one | - | - | open |', '| job-one | fixture-thread | #12 | open |'))
        self.b(r, 'backlog', 'app', 'add', 'retain references')
        self.assertIn('| job-one | fixture-thread | #12 | open |', st.read_text())
        self.b(r, 'jobs', 'app', '--branch', 'job-one', '--close', '--reason', 'fixture complete')
        self.assertIn('| job-one | fixture-thread | #12 | closed |', st.read_text())
        self.assertIn('fixture-thread', self.b(r, 'jobs', 'app', '--branch', 'job-one'))
        self.b(r, 'jobs', 'app', '--branch', 'job-one', '--settled', '--attestation-file', settlement_export(self, r, 'app', branch='job-one'))
        self.assertNotIn('| job-one |', st.read_text())

    def test_projection_preserves_bound_source_states_over_completed_ledger(self):
        from brain import jobs, status
        r = self.init('demo')
        home = self.repo(self.tmp / 'code')
        self.b(r, 'new', 'app', '--title', 'App', '--home', str(home))
        self.b(r, 'wt', 'add', 'app', 'job-one', '--no-setup')
        inst = instance.Instance(r)
        n = inst.node('app')
        rows = jobs.read_jobs(n)
        rows[0].update(Ended='2026-01-01T00:00:00+00:00', Outcome='merged')
        jobs.write_jobs(inst, n, rows)  # ledger data alone is not an owner transition
        path = n['dir'] / 'status.md'
        for state in ('working', 'unknown', '', 'review — merged into preview',
                      'merged — fixture complete', 'done: fixture complete'):
            for branch in ('`job-one`', '`job-one` (fixture annotation)'):
                with self.subTest(state=state, branch=branch):
                    path.write_text('## Active work\n| Branch | Thread | PR | State |\n'
                                    + '| ' + branch + ' | fixture-thread | #12 | ' + state + ' |\n')
                    expected = [['job-one', 'fixture-thread', '#12', state]]
                    self.b(r, 'backlog', 'app', 'add', 'fixture refresh')
                    self.assertEqual(self.view(r)['active'], expected)
                    table = path.read_text().split('## Active work', 1)[1]
                    self.b(r, 'status', 'app', '--refresh')
                    self.assertEqual(self.view(r)['active'], expected)
                    self.assertEqual(path.read_text().split('## Active work', 1)[1], table)
                    self.assertEqual(status.active_work_closed(expected[0]),
                                     status.active_work_closed(status.active_work(n, include_closed=True)[0]))

    def test_cleanup_records_replay_view_without_materializing_status_until_refresh(self):
        for action in ('prune', 'rm'):
            with self.subTest(action=action):
                r = self.init('demo-' + action)
                home = self.repo(self.tmp / ('code-' + action))
                self.b(r, 'new', 'app', '--title', 'App', '--home', str(home))
                self.b(r, 'wt', 'add', 'app', 'job-one', '--no-setup')
                local = json.loads((r / '.brain/local.json').read_text())
                wt = Path(local['nodes']['app']['worktrees']['job-one']['path'])
                self.git('commit', '--allow-empty', '-qm', 'fixture complete', cwd=wt)
                self.git('merge', '-q', 'job-one', cwd=home)
                path = r / 'nodes/app/status.md'
                archive = path.with_name('status-archive.md')
                path.write_text('## Now\n\n' + ''.join('- fixture note %d\n' % i for i in range(35))
                                + '\n## Active work\n| Branch | Thread | PR | State |\n'
                                  '| `job-one` (fixture annotation) | fixture-thread | - | merged — fixture complete |\n')
                before = path.read_bytes()
                self.assertFalse(archive.exists())
                count = len(self.commits(r))
                args = ('--offline',) if action == 'prune' else ('job-one',)
                self.b(r, 'wt', action, 'app', *args)
                self.assertFalse(wt.exists())
                self.assertEqual(path.read_bytes(), before)
                self.assertFalse(archive.exists())
                commits = self.commits(r)
                self.assertEqual(len(commits), count + 1)
                self.assertEqual(commits[-1]['data']['command'], 'wt_' + action)
                self.assertTrue(commits[-1]['data']['maintenance_ok'])
                self.assertEqual(events.pending(self.records(r)), {})
                view = self.view(r)
                self.assertEqual(commits[-1]['data']['views'][events.node_ref('app')], view)
                self.assertEqual(view['active'], [['job-one', 'fixture-thread', '-', 'merged — fixture complete']])
                self.b(r, 'status', 'app', '--refresh')
                self.assertNotEqual(path.read_bytes(), before)
                self.assertIn('## Facts (generated', path.read_text())
                self.assertTrue(archive.exists())
                self.assertIn('- fixture note 34', archive.read_text())
                self.assertEqual(self.view(r)['active'], view['active'])

    def test_projection_retains_ambiguous_unmatched_and_incomplete_owners(self):
        from brain import jobs, status
        r = self.init('demo')
        home = self.repo(self.tmp / 'code')
        self.b(r, 'new', 'app', '--title', 'App', '--home', str(home))
        self.b(r, 'wt', 'add', 'app', 'job-one', '--no-setup')
        inst = instance.Instance(r)
        n = inst.node('app')
        rows = jobs.read_jobs(n)
        rows[0].update(Ended='2026-01-01T00:00:00+00:00', Outcome='merged')
        path = n['dir'] / 'status.md'
        source = ('## Active work\n| Branch | Thread | PR | State |\n'
                  '| `job-one` | fixture-first | - | working |\n'
                  '| job-one (fixture annotation) | fixture-second | - | unknown |\n'
                  '| orphan | fixture-orphan | - | working |\n'
                  '| incomplete | fixture-incomplete |\n')
        for mode in ('duplicate-source', 'duplicate-ledger', 'settled-ledger', 'missing-ledger'):
            with self.subTest(mode=mode):
                ledger = [dict(rows[0])]
                if mode == 'duplicate-ledger':
                    ledger.append({**rows[0], 'Started': '2025-01-01T00:00:00+00:00'})
                elif mode == 'settled-ledger':
                    ledger[0]['Settled'] = '2026-01-01T00:01:00+00:00'
                elif mode == 'missing-ledger':
                    ledger = []
                jobs.write_jobs(inst, n, ledger)
                path.write_text(source)
                first = None
                for _ in range(2):
                    view = events.snapshot_view(inst, n)
                    bound = {cells[1]: cells[3] for cells in view['active'] if cells[1] != '-'}
                    self.assertEqual(bound, {'fixture-first': 'working', 'fixture-second': 'unknown',
                                             'fixture-orphan': 'working', 'fixture-incomplete': 'unknown'})
                    if first is not None:
                        self.assertEqual(view['active'], first)
                    first = view['active']
                    events.active_table(inst, n, view)
                events.close_active_work(inst, n, rows[0])
                self.assertEqual(status.active_work(n, include_closed=True), first)

    def test_status_commit_is_hourly_and_skips_someone_elses_judgement(self):
        r = self.init('demo')
        self.b(r, 'new', 'app', '--title', 'App')
        inst = instance.Instance(r)
        n = inst.node('app')
        with instance.brain_lock(inst):
            with mock.patch.object(inst, '_commit') as commit:
                events.status_commit(inst, n)
                commit.assert_not_called()
            stamp = r / '.brain/events-status-commits.json'
            stamp.write_text('{}')
            with mock.patch.object(inst, '_commit') as commit:
                events.status_commit(inst, n)
                commit.assert_called_once()
            path = n['dir'] / 'status.md'
            original = path.read_bytes()
            path.write_bytes(original + b' ')
            with mock.patch.object(inst, '_commit') as commit:
                events.status_commit(inst, n, force=True)
                commit.assert_not_called()
            path.write_bytes(original.replace(b'\n', b'\r\n'))
            with mock.patch.object(inst, '_commit') as commit:
                events.status_commit(inst, n, force=True)
                commit.assert_not_called()
            path.write_bytes(original)
            path.write_text(path.read_text().replace('- Node created.', '- Other coordinator draft.'))
            with mock.patch.object(inst, '_commit') as commit:
                events.status_commit(inst, n, force=True)
                commit.assert_not_called()

    def test_unlocked_wait_does_not_claim_other_commands_writes_or_leave_false_pending(self):
        r = self.init('demo')
        self.b(r, 'new', 'app', '--title', 'App')
        with instance.brain_lock(r):
            with self.assertRaises(RuntimeError):
                with events.Command(r, command='waiting_writer'):
                    with events.Command(r, command='other_writer', node='app'):
                        (r / 'nodes/app/backlog.md').write_text('- other writer\n')
                    raise RuntimeError('the waiting operation failed without its own writes')
        self.assertEqual(events.pending(self.records(r)), {})
        self.assertEqual(self.records(r)[-1]['kind'], 'abort')
        owner = next(e for e in reversed(self.records(r)) if e['kind'] == 'commit')
        self.assertEqual(owner['data']['command'], 'other_writer')
        self.assertIn('nodes/app/backlog.md', [x['path'] for x in owner['data']['resources']])
