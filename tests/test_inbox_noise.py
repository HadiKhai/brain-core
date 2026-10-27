"""Inbox visibility and coordinator-only advice; fabricated records and telemetry only."""
import argparse
import contextlib
import datetime
import io
import json
import os
import time
from unittest import mock

import core_loader
from scenario import CORE, Scenario


class InboxNoise(Scenario):
    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, self.env)
        env.start()
        self.addCleanup(env.stop)
        self.m = core_loader.SourceFileLoader('inbox_fixture', str(CORE / 'bin/brain')).load_module()
        self.root = self.init('oak')
        self.inst = self.m.Instance(self.root)

    def new(self, nid, *labels):
        self.b(self.root, 'new', nid, '--title', nid)
        if labels:
            self.b(self.root, 'relabel', nid, *labels)
        self.inst = self.m.Instance(self.root)
        return self.inst.node(nid)

    def inbox(self, reporter='root'):
        return json.loads(self.b(self.root, 'inbox', reporter, '--json'))

    def sample(self, at, reading):
        with mock.patch.object(self.m.time, 'time', return_value=at), \
                mock.patch.object(self.m, 'machine_load', return_value=reading), \
                contextlib.redirect_stdout(io.StringIO()):
            self.m.machine_sample(self.inst)

    def machine_items(self, at, new=False):
        with mock.patch.object(self.m.time, 'time', return_value=at):
            if not new:
                items = self.m.inbox_items(self.inst)[0]
            else:
                with mock.patch.object(self.m, 'load', return_value=self.inst), \
                        contextlib.redirect_stdout(io.StringIO()) as output:
                    self.m.cmd_inbox(argparse.Namespace(id='root', json=True, new=True))
                items = json.loads(output.getvalue())
            return [i for i in items if i['kind'] == 'machine-alert']

    def jobs(self, n):
        base = dict.fromkeys(self.m.JOB_COLS, '')
        done = {**base, 'Job': 'import:old', 'Started': '2020-01-01T00:00Z',
                'Ended': '2020-01-01T01:00Z', 'Outcome': 'merged', 'Hours': '1',
                'Usage': 'host: fixture', 'Fresh in': '1000000', 'Settled': 'yes'}
        running = {**base, 'Job': 'import:current', 'Started': '2020-01-02T00:00Z',
                   'Usage': 'host: fixture', 'Fresh in': '20000001'}
        self.m.write_jobs(self.inst, n, [done, running])
        contract = self.m.contract_file(n, running)
        contract.parent.mkdir(exist_ok=True)
        contract.write_text('## Budget\n\n10M tokens, 3 h\n')

    def test_closed_machine_records_hidden_everywhere_without_deleting_history(self):
        n = self.inst.node('root')
        legacy = []
        for i in range(14):
            episode = format(i, '024x')
            legacy += [f'- fixture from brain: hold heavy work: sustained 5-minute load [machine episode {episode} open]',
                       f'- fixture from brain: heavy work hold cleared [machine episode {episode} close]']
        protected = [legacy[-1] + ' <!-- ' + marker + ':fixture -->'
                     for marker in ('host-question', 'handback', 'worker-failure')]
        protected += [legacy[0].replace('sustained 5-minute load',
                                       'sustained 5-minute load <!-- ' + marker + ':fixture -->')
                      for marker in ('host-question', 'handback', 'worker-failure')]
        protected += ['- fixture from worker: heavy work hold cleared; please choose a plan']
        path = self.root / 'inbox.md'
        path.write_text('\n'.join(legacy + protected) + '\n')
        before = path.read_bytes()
        self.assertEqual(self.m.inbox_lines(n), protected)
        self.assertEqual([i['text'] for i in self.inbox() if i['kind'] == 'inbox'],
                         ['inbox: ' + line[2:] for line in protected])
        state = json.loads(self.b(self.root, 'ui', '--json'))
        self.assertEqual(next(x for x in state['nodes'] if x['id'] == 'root')['inbox'],
                         [line[2:] for line in protected])
        self.assertFalse(any(i['text'].startswith('inbox: fixture from brain:') and '<!--' not in i['text']
                             for i in state['attention']))
        self.assertEqual(path.read_bytes(), before)

    def test_machine_inbox_has_one_rolling_line_only_when_escalation_worthy(self):
        base = time.time()
        good = json.loads(self.env['BRAIN_MACHINE_READING'])
        high = {**good, 'load_per_cpu': [1, 1, 1]}
        path = self.root / '.brain/machine.json'
        path.write_text(json.dumps({'samples': [{'at': base - i * 60, 'reading': high}
                                                for i in (4, 3, 2, 1)]}))
        self.sample(base, high)
        self.assertTrue(json.loads(path.read_text())['episode'])
        self.assertEqual(self.machine_items(base), [])
        self.sample(base + 3600, high)
        self.assertEqual(self.machine_items(base + 3600), [])
        self.sample(base + 3601, high)
        long = self.machine_items(base + 3601)
        self.assertEqual(len(long), 1)
        self.assertEqual([i['id'] for i in self.machine_items(base + 3601, new=True)], [long[0]['id']])
        self.assertEqual(self.machine_items(base + 3601, new=True), [])
        self.sample(base + 3661, {**high, 'thermal': 'pressure'})
        hot = self.machine_items(base + 3661)
        self.assertEqual(len(hot), 1)
        self.assertEqual(hot[0]['id'], long[0]['id'])
        self.assertEqual(self.machine_items(base + 3661, new=True), [])
        for offset in (3721, 3781, 3841):
            self.sample(base + offset, good)
        self.assertEqual(self.machine_items(base + 3841), [])
        self.assertEqual([t['state'] for t in json.loads(path.read_text())['transitions']], ['open', 'close'])
        # Thermal pressure is urgent even before the sustained admission hold opens.
        self.sample(base + 3901, {**good, 'thermal': 'pressure'})
        later = self.machine_items(base + 3901)
        self.assertEqual(len(later), 1)
        self.assertNotEqual(later[0]['id'], long[0]['id'])
        # No --new read occurred during recovery; a later episode still reports once.
        self.assertEqual([i['id'] for i in self.machine_items(base + 3901, new=True)], [later[0]['id']])
        self.assertEqual(self.machine_items(base + 3901, new=True), [])
        self.assertEqual((self.root / 'inbox.md').read_text().count('machine episode'), 0)

    def test_paused_node_hidden_from_human_views_but_questions_still_route(self):
        n = self.new('snoozed', 'state:paused')
        line = '- 2020-01-01 00:00 +00:00 from runtime: Choose blue <!-- host-question:fixture -->'
        (n['dir'] / 'inbox.md').write_text(line + '\n')
        with (n['dir'] / 'status.md').open('a') as stream:
            stream.write('\n## Blocked\n- Choose a plan; since: 2020-01-01T00:00Z; recommendation: blue; default: gray\n')
        self.jobs(n)
        with mock.patch.object(self.m, 'pending_imports', return_value=(1, 1, 1)):
            self.assertFalse(any(i[0] == n['id'] for i in self.m.attention_items(self.inst, [n])))
            self.assertEqual(self.m.inbox_items(self.inst, [n], n['id'])[0], [])
        self.assertFalse(any(i['node'] == n['id'] for i in self.inbox()))
        state = json.loads(self.b(self.root, 'ui', '--json'))
        self.assertFalse(any(i['node'] == n['id'] for i in state['attention'] + state['inbox']))
        # Pausing presentation must not silence the coordinator's protected routes.
        due = self.m.due_items(self.inst, n, include_stale=False)
        self.assertTrue({'inbox', 'blocked-overdue', 'overdue-question'} <= {i['kind'] for i in due})
        with mock.patch.object(self.m, 'changes_due', return_value=[]):
            groups = self.m.watch_groups(self.inst)
        self.assertTrue(any(i['kind'] == 'inbox' for g in groups.values() for i in g['items']))
        self.assertIn(line, (n['dir'] / 'inbox.md').read_text())
        self.b(self.root, 'relabel', n['id'], 'state:active')
        self.assertTrue(any(i['node'] == n['id'] for i in self.inbox()))

    def test_study_never_needs_coordinator_but_keeps_its_questions(self):
        study = self.new('reading', 'kind:study')
        ordinary = self.new('active')
        (study['dir'] / 'inbox.md').write_text('- fixture question for study\n')
        items = self.inbox()
        self.assertFalse(any(i['kind'] == 'setup' and i['node'] == 'reading' for i in items))
        self.assertTrue(any(i['kind'] == 'setup' and i['node'] == ordinary['id'] for i in items))
        self.assertTrue(any(i['kind'] == 'inbox' and i['node'] == 'reading' for i in items))
        self.assertNotIn('reading: no coordinator', self.b(self.root))
        self.assertNotIn('setup', [i['kind'] for i in self.inbox('reading')])

    def test_paused_root_hides_cached_update_attention(self):
        self.b(self.root, 'relabel', 'root', 'state:paused')
        self.inst = self.m.Instance(self.root)
        root = self.inst.node('root')
        with mock.patch.object(self.m, 'core_update', return_value=('a' * 40, 'b' * 40)):
            self.assertEqual(self.m.attention_items(self.inst, [root]), [])
            self.assertEqual(self.m.inbox_items(self.inst, [root])[0], [])

    def test_job_budget_and_median_advice_only_reaches_owning_coordinator(self):
        self.new('parent')
        owner = self.new('parent/project')
        sibling = self.new('peer')
        self.jobs(owner)
        instant = self.inst.parse_time('2020-01-03T12:00Z')
        entry = self.tmp / 'clocked-brain.py'
        # The parent and real CLI subprocesses must render the same elapsed age.
        entry.write_text('import datetime, sys\n'
                         'sys.dont_write_bytecode = True\n'
                         'sys.path.insert(0, ' + repr(str(self.root / 'core/lib')) + ')\n'
                         'from brain.instance import Instance\n'
                         'Instance.now_dt = lambda self: datetime.datetime.fromisoformat('
                         + repr(instant.isoformat()) + ')\n'
                         'from brain.cli import main\nmain()\n')

        def read_json(*args):
            return json.loads(self.run_(list(args), cwd=self.root, brain=entry))

        with mock.patch.object(self.inst, 'now_dt', return_value=instant):
            alerts = self.m.runaway_jobs(self.inst, owner)
        self.assertEqual(len(alerts), 1)
        self.assertIn('open 36.0 h, over twice the median of 1.0 h', alerts[0])
        self.assertIn('over its 10M-token budget', alerts[0])
        self.assertIn('over twice the median', alerts[0])
        # Human rollups and ancestors cannot act on the child's job advice.
        self.assertFalse(any(i['kind'] == 'job' for i in read_json('inbox', 'root', '--json')))
        self.assertFalse(any(i['kind'] == 'job' for i in read_json('inbox', 'parent', '--json')))
        self.assertFalse(any(i['kind'] == 'job' for i in read_json('inbox', sibling['id'], '--json')))
        own = read_json('inbox', owner['id'], '--json')
        expected_advice = [
            'job import:current: open 36.0 h, over twice the median of 1.0 h',
            'job import:current: 20M tokens, over twice the median of 1M',
            'job import:current: over its 10M-token budget',
            'job import:current: over its 3-hour budget',
        ]
        self.assertEqual([i['text'] for i in own if i['kind'] == 'job'], expected_advice)
        self.assertTrue(all(i.get('informational') for i in own if i['kind'] == 'job'))
        due = read_json('due', owner['id'], '--json')
        self.assertEqual([i['text'] for i in due if i['kind'] == 'job'], expected_advice)
        self.assertTrue(all(i['informational'] for i in due if i['kind'] == 'job'))
        root_due = read_json('due', 'root', '--json')
        self.assertFalse(any(i['kind'] == 'job' for i in root_due))
        state = read_json('ui', '--json')
        self.assertEqual([(i['node'], i['text']) for i in state['attention'] if i['kind'] == 'job'],
                         [(owner['id'], alert) for alert in alerts])
        self.assertFalse(any(i['kind'] == 'job' for i in state['inbox']))
        # Advice must not add expensive usage scans or fallback wakes to the watcher.
        with mock.patch.object(self.m, 'runaway_jobs', side_effect=AssertionError('watcher scanned usage')), \
                mock.patch.object(self.m, 'changes_due', return_value=[]):
            self.assertNotIn('job', [i['kind'] for i in self.m.watch_items(self.inst, owner)])

    def test_root_owned_job_advice_stays_visible_without_waking(self):
        root = self.inst.node('root')
        self.jobs(root)
        self.assertTrue(self.m.runaway_jobs(self.inst, root))
        self.assertFalse(any(i['kind'] == 'job' for i in self.inbox()))
        due = json.loads(self.b(self.root, 'due', 'root', '--json'))
        self.assertTrue(any(i['kind'] == 'job' for i in due))
        state = json.loads(self.b(self.root, 'ui', '--json'))
        self.assertEqual([i['text'] for i in state['attention'] if i['kind'] == 'job' and i['node'] == 'root'],
                         self.m.runaway_jobs(self.inst, root))
        with mock.patch.object(self.m, 'changes_due', return_value=[]):
            self.assertNotIn('job', [i['kind'] for i in self.m.watch_items(self.inst, root)])
        # The real merged history row also produces an independently owned
        # completion. Informational advice stays quiet; that completion does not.
        landed, = [i for i in due if i['kind'] == 'job-landed']
        self.assertEqual((landed['node'], landed['source']), ('root', 'root'))
        self.assertTrue(landed['id'])
        self.assertFalse(landed.get('informational', False))
        ledger = (self.root / 'job-ledger.md').read_bytes()
        with mock.patch.object(self.m, 'load', return_value=self.inst), \
                mock.patch.object(self.m, 'changes_due', return_value=[]), \
                mock.patch.object(self.m, 'due_evidence', return_value=(
                    {'jobs': [], 'worktrees': {}, 'handbacks': {}, 'inbox': []}, '', '')), \
                mock.patch.object(self.m, 'status_lag', return_value=None), \
                self.assertRaises(SystemExit) as quiet:
            self.m.cmd_due(argparse.Namespace(id='root', quiet=True, json=False))
        self.assertEqual(quiet.exception.code, 1)
        self.assertEqual((self.root / 'job-ledger.md').read_bytes(), ledger)
        after = json.loads(self.b(self.root, 'due', 'root', '--json'))
        self.assertEqual([i['id'] for i in after if i['kind'] == 'job-landed'], [landed['id']])

    def test_terminal_machine_notice_expires_on_delivery_then_archives_on_next_read(self):
        reminders = self.m._owners['lifecycle_read']
        path = self.root / 'inbox.md'
        notice = '- ' + self.inst.now() + ' from machine: command completed: command completed successfully <!-- machine-wait:00ad91d121f4a41cac633f58bb1275f8:completed -->'
        question = '- 2026-10-25 15:42 +00:00 from worker: Which export? <!-- host-question:fixture -->'
        text = notice + '\n' + question + '\n'
        path.write_text(text)
        first = json.loads(self.b(self.root, 'inbox', 'root', '--new', '--json'))
        self.assertTrue(any('command completed' in i['text'] for i in first))
        second = self.inbox()
        self.assertFalse(any('command completed' in i['text'] for i in second))
        self.assertTrue(any('Which export?' in i['text'] for i in second))
        self.assertEqual(path.read_text(), question + '\n')
        self.assertIn((notice + '\n').encode(), (self.root / 'inbox-archive.md').read_bytes())
        inst = self.m.Instance(self.root)
        key = reminders.inbox_source_id(inst.node('root'), notice)
        record = next(json.loads(line[len('<!-- episode '):-len(' -->')])
                      for line in (self.root / 'inbox-archive.md').read_text().splitlines()
                      if line.startswith('<!-- episode '))
        delivered = next(item for item in first if 'command completed' in item['text'])
        self.assertEqual(record['id'], delivered['id'])
        self.assertIn(key, reminders.lifecycle_read(inst)['inbox_retired']['root'])
        self.assertTrue(record['expired'])
        self.assertFalse(record['answered'])
        self.assertTrue(record['seen_at'])

    def test_drain_only_terminal_notices_and_readonly_counts_never_write(self):
        inbox_notices = self.m._owners['terminal_notice']
        task_reconcile = self.m._owners['count_node']
        path = self.root / 'inbox.md'
        lines = ['- 2026-10-25 15:40 +00:00 from machine: held step exited: command exited with status 2',
                 '- 2026-10-25 15:41 +00:00 from machine: hold heavy work: thermal warning',
                 '- 2026-10-25 15:42 +00:00 from worker: command completed: command completed successfully',
                 '- 2026-10-25 15:43 +00:00 from machine: command completed: command completed successfully <!-- host-question:protected -->']
        path.write_text('\n'.join(lines) + '\n')
        n = self.inst.node('root')
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file() and not p.is_symlink()}
        self.assertEqual(task_reconcile.count_node(self.inst, n)['inbox_pending'], 4)
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertEqual(len(inbox_notices.expire(self.inst, [n])), 1)
        self.assertEqual(inbox_notices.expire(self.inst, [n]), [])
        self.assertEqual(task_reconcile.count_node(self.inst, n)['inbox_pending'], 3)
        self.assertEqual(path.read_text(), '\n'.join(lines) + '\n')

    def test_failed_stdout_delivery_never_expires_a_notice(self):
        import argparse
        reminders = self.m._owners['lifecycle_read']
        path = self.root / 'inbox.md'
        notice = '- ' + self.inst.now() + ' from machine: command completed: command completed successfully'
        path.write_text(notice + '\n')
        with mock.patch.object(self.m, 'load', return_value=self.inst), \
                mock.patch('sys.stdout.write', side_effect=BrokenPipeError):
            with self.assertRaises(BrokenPipeError):
                self.m.cmd_inbox(argparse.Namespace(id='root', json=True, new=False))
        state = reminders.lifecycle_read(self.inst)
        key = reminders.inbox_source_id(self.inst.node('root'), notice)
        self.assertNotIn('expired', state['inbox'].get('root', {}).get(key, {}))

    def test_resolved_machine_variants_expire_but_live_holds_and_questions_remain(self):
        notices = self.m._owners['terminal_notice']
        for text, event in [('held step exited: command exited with status -15', 'exited'),
                            ('held step exited: interrupted (KeyboardInterrupt)', 'exited'),
                            ('held step exited: owner stopped before the command completed', 'exited'),
                            ('held step resumed automatically: heavy example held since 2026-10-25T15:00Z: pressure; clears when resources clear', 'resumed')]:
            line = '- 2026-10-25T15:40:29+00:00 from machine: ' + text + ' <!-- machine-wait:' + 'a' * 32 + ':' + event + ' -->'
            with self.subTest(text=text):
                self.assertTrue(notices.terminal_notice(line))
                self.assertFalse(notices.terminal_notice(line + ' <!-- host-question:protected -->'))
        held = '- 2026-10-25T15:40:29+00:00 from machine: heavy example held since 2026-10-25T15:00Z: pressure; clears when resources clear <!-- machine-wait:' + 'a' * 32 + ':coordinator -->'
        self.assertFalse(notices.terminal_notice(held))
        self.assertFalse(notices.terminal_notice(held.replace(':coordinator -->', ':completed -->')))

    def test_archive_handled_retains_exact_lines_and_unresolved_requests(self):
        notices = self.m._owners['terminal_notice']
        reminders = self.m._owners['lifecycle_read']
        reconcile = self.m._owners['count_node']
        n = self.inst.node('root')
        question = '- 2026-10-25 15:40 +00:00 from worker: Which export?'
        pending = '- 2026-10-25 15:41 +00:00 from worker: Still needs an answer'
        notice = '- 2026-10-25 15:42 +00:00 from machine: command completed: command completed successfully'
        text = '# Inbox\r\n' + question + '\r\n' + pending + '\r\n' + notice + '\r\n'
        source = self.root / 'inbox.md'
        source.write_bytes(text.encode())
        with reminders.lifecycle_store(self.inst) as data:
            current = reminders.reconcile_requests(self.inst, n, [question, pending, notice], data)
            key = reminders.inbox_source_id(n, question)
            record = current[key]
            record['answered'] = True
            data['answers'].append({'id': record['id'], 'source_id': key, 'node': n['id'], 'answer': 'Use the CSV export'})
        before = source.read_bytes()
        plan = reconcile.reconcile(self.inst, [n], drain=True, archive=True)
        self.assertEqual(plan[0]['inbox_lines_archived'], 2)
        self.assertEqual(source.read_bytes(), before)
        self.assertFalse((self.root / 'inbox-archive.md').exists())
        reconcile.reconcile(self.inst, [n], apply=True, drain=True, archive=True)
        archive = (self.root / 'inbox-archive.md').read_bytes()
        self.assertIn((question + '\r\n').encode(), archive)
        self.assertIn((notice + '\r\n').encode(), archive)
        self.assertIn(b'"answered": true', archive)
        self.assertIn(b'"expired": true', archive)
        self.assertEqual(source.read_bytes(), ('# Inbox\r\n' + pending + '\r\n').encode())
        self.assertEqual(reconcile.count_node(self.inst, n)['inbox_pending'], 1)
        self.assertEqual(reconcile.reconcile(self.inst, [n], apply=True, drain=True, archive=True)[0]['inbox_lines_archived'], 0)
        self.assertEqual((self.root / 'inbox-archive.md').read_bytes(), archive)
        # The exact retained answer and expired episode remain auditable.
        self.assertEqual(reminders.lifecycle_read(self.inst)['answers'][-1]['answer'], 'Use the CSV export')

    def test_archive_failure_retains_source_and_retry_deduplicates(self):
        notices = self.m._owners['terminal_notice']
        reminders = self.m._owners['lifecycle_read']
        n = self.inst.node('root')
        source = self.root / 'inbox.md'
        notice = '- 2026-10-25 15:42 +00:00 from machine: held step exited: command exited with status 2\n'
        source.write_text(notice)
        notices.expire(self.inst, [n])
        plan = notices.archive_plan(self.inst, n, reminders.lifecycle_read(self.inst))
        write = notices.archive_replace
        with mock.patch.object(notices, 'archive_replace', side_effect=OSError('archive unavailable')):
            with self.assertRaises(OSError):
                notices.publish_archive(plan)
        self.assertEqual(source.read_text(), notice)
        def fail_source(target, text, generation):
            if target.path == source:
                raise OSError('inbox publication unavailable')
            return write(target, text, generation)
        with mock.patch.object(notices, 'archive_replace', side_effect=fail_source):
            with self.assertRaises(OSError):
                notices.publish_archive(plan)
        self.assertEqual(source.read_text(), notice)
        archived = (self.root / 'inbox-archive.md').read_bytes()
        notices.publish_archive(notices.archive_plan(self.inst, n, reminders.lifecycle_read(self.inst)))
        self.assertEqual(source.read_text(), '')
        self.assertEqual((self.root / 'inbox-archive.md').read_bytes(), archived)

    def test_archive_refuses_linked_target_before_any_inbox_change(self):
        notices = self.m._owners['terminal_notice']
        reminders = self.m._owners['lifecycle_read']
        n = self.inst.node('root')
        source = self.root / 'inbox.md'
        notice = '- 2026-10-25 15:42 +00:00 from machine: command completed: command completed successfully\n'
        source.write_text(notice)
        notices.expire(self.inst, [n])
        target = self.root / 'untouched.md'
        target.write_text('Original bytes\n')
        (self.root / 'inbox-archive.md').symlink_to(target)
        with self.assertRaises(ValueError):
            notices.archive_plan(self.inst, n, reminders.lifecycle_read(self.inst))
        self.assertEqual(source.read_text(), notice)
        self.assertEqual(target.read_text(), 'Original bytes\n')

    def test_archive_marker_without_retained_source_cannot_discard_a_line(self):
        notices = self.m._owners['terminal_notice']
        reminders = self.m._owners['lifecycle_read']
        n = self.inst.node('root')
        source, archive = self.root / 'inbox.md', self.root / 'inbox-archive.md'
        notice = '- 2026-10-25 15:42 +00:00 from machine: command completed: command completed successfully\n'
        source.write_text(notice)
        notices.expire(self.inst, [n])
        state = reminders.lifecycle_read(self.inst)
        first = notices.archive_plan(self.inst, n, state)
        archive.write_text(first['archive_text'].splitlines()[0] + '\n')
        notices.publish_archive(notices.archive_plan(self.inst, n, state))
        self.assertEqual(source.read_text(), '')
        self.assertIn(notice, archive.read_text())
        self.assertIn('"expired": true', archive.read_text())

    def test_archive_refuses_a_native_question_append_after_planning(self):
        notices = self.m._owners['terminal_notice']
        reminders = self.m._owners['lifecycle_read']
        transitions = self.m._owners['inbox_append']
        n = self.inst.node('root')
        source, archive = self.root / 'inbox.md', self.root / 'inbox-archive.md'
        notice = '- 2026-10-25 15:42 +00:00 from machine: command completed: command completed successfully\n'
        question = '- 2026-10-25 15:43 +00:00 from runtime: Which export? <!-- host-question:concurrent -->\n'
        source.write_text(notice)
        notices.expire(self.inst, [n])
        plan = notices.archive_plan(self.inst, n, reminders.lifecycle_read(self.inst))
        self.assertTrue(transitions.inbox_append(n['dir'], question, nonblocking=True))
        with self.assertRaisesRegex(OSError, 'inbox changed'):
            notices.publish_archive(plan)
        self.assertEqual(source.read_text(), notice + question)
        self.assertFalse(archive.exists())
        notices.publish_archive(notices.archive_plan(self.inst, n, reminders.lifecycle_read(self.inst)))
        self.assertEqual(source.read_text(), question)
        self.assertIn(notice, archive.read_text())
        self.assertIn(question.rstrip('\n'), self.m.inbox_lines(n, readonly=True))

    def test_appender_opening_old_inode_retries_after_archive_replacement(self):
        import builtins
        notices = self.m._owners['terminal_notice']
        reminders = self.m._owners['lifecycle_read']
        transitions = self.m._owners['inbox_append']
        n = self.inst.node('root')
        source = self.root / 'inbox.md'
        notice = '- 2026-10-25 15:42 +00:00 from machine: command completed: command completed successfully\n'
        question = '- 2026-10-25 15:43 +00:00 from runtime: Still pending <!-- host-question:old-inode -->\n'
        for nonblocking in (True, False):
            with self.subTest(nonblocking=nonblocking):
                source.write_text(notice)
                notices.expire(self.inst, [n])
                plan = notices.archive_plan(self.inst, n, reminders.lifecycle_read(self.inst))
                opened, actual_open = [], builtins.open
                def replace_after_open(path, mode='r', *args, **kwargs):
                    stream = actual_open(path, mode, *args, **kwargs)
                    if path == source and mode == 'a+' and not opened:
                        opened.append(stream)
                        notices.publish_archive(plan)
                    return stream
                with mock.patch('builtins.open', side_effect=replace_after_open):
                    if nonblocking:
                        with self.assertRaises(BlockingIOError):
                            transitions.inbox_append(n['dir'], question, nonblocking=True)
                    else:
                        self.assertTrue(transitions.inbox_append(n['dir'], question))
                if nonblocking:
                    self.assertTrue(transitions.inbox_append(n['dir'], question, nonblocking=True))
                self.assertEqual(source.read_text(), question)
                self.assertIn(notice, (self.root / 'inbox-archive.md').read_text())
                self.assertIn(question.rstrip('\n'), self.m.inbox_lines(n, readonly=True))

    def test_archive_lock_rejects_nonblocking_append_until_replacement_finishes(self):
        notices = self.m._owners['terminal_notice']
        reminders = self.m._owners['lifecycle_read']
        transitions = self.m._owners['inbox_append']
        n = self.inst.node('root')
        source = self.root / 'inbox.md'
        notice = '- 2026-10-25 15:42 +00:00 from machine: command completed: command completed successfully\n'
        question = '- 2026-10-25 15:43 +00:00 from runtime: Awaiting retry <!-- host-question:lock -->\n'
        source.write_text(notice)
        notices.expire(self.inst, [n])
        plan = notices.archive_plan(self.inst, n, reminders.lifecycle_read(self.inst))
        actual_replace = notices.archive_replace
        attempts = []
        def blocked_append(target, text, generation):
            if target.path == source:
                with self.assertRaises(BlockingIOError):
                    transitions.inbox_append(n['dir'], question, nonblocking=True)
                attempts.append(True)
            actual_replace(target, text, generation)
        with mock.patch.object(notices, 'archive_replace', side_effect=blocked_append):
            notices.publish_archive(plan)
        self.assertEqual(attempts, [True])
        self.assertTrue(transitions.inbox_append(n['dir'], question, nonblocking=True))
        self.assertEqual(source.read_text(), question)

    def test_archive_refuses_intervening_archive_change_without_overwriting_it(self):
        notices = self.m._owners['terminal_notice']
        reminders = self.m._owners['lifecycle_read']
        n = self.inst.node('root')
        source, archive = self.root / 'inbox.md', self.root / 'inbox-archive.md'
        notice = '- 2026-10-25 15:42 +00:00 from machine: command completed: command completed successfully\n'
        source.write_text(notice)
        notices.expire(self.inst, [n])
        plan = notices.archive_plan(self.inst, n, reminders.lifecycle_read(self.inst))
        archive.write_text('Concurrent retained history\n')
        with self.assertRaisesRegex(OSError, 'publication target changed'):
            notices.publish_archive(plan)
        self.assertEqual(source.read_text(), notice)
        self.assertEqual(archive.read_text(), 'Concurrent retained history\n')

    def test_hook_question_queued_during_archive_is_delivered_after_replacement(self):
        notices = self.m._owners['terminal_notice']
        reminders = self.m._owners['lifecycle_read']
        n = self.inst.node('root')
        source = self.root / 'inbox.md'
        notice = '- 2026-10-25 15:42 +00:00 from machine: command completed: command completed successfully\n'
        source.write_text(notice)
        notices.expire(self.inst, [n])
        plan = notices.archive_plan(self.inst, n, reminders.lifecycle_read(self.inst))
        transcript = self.tmp / 'archive-question.jsonl'
        transcript.write_text(json.dumps({'type': 'response_item', 'payload': {
            'type': 'function_call', 'call_id': 'archive-race', 'name': 'request_user_input',
            'arguments': json.dumps({'questions': [{'id': 'export', 'question': 'Which concurrent export?'}]})}}) + '\n')
        actual_replace = notices.archive_replace
        queued = []
        def queue_hook(target, text, generation):
            if target.path == source:
                with mock.patch.object(self.m, 'request_wake', return_value=False):
                    self.m.capture_hook_questions(self.inst, n, {'transcript_path': str(transcript), 'session_id': 'archive-race'})
                pending = list(self.m.hook_question_queue(n).glob('*.json'))
                self.assertEqual(len(pending), 1)
                queued.append(json.loads(pending[0].read_text())['line'])
            actual_replace(target, text, generation)
        with mock.patch.object(notices, 'archive_replace', side_effect=queue_hook):
            notices.publish_archive(plan)
        current = self.m.inbox_lines(n)
        self.assertEqual(len(queued), 1)
        self.assertIn(queued[0].rstrip('\n'), current)
        self.assertIn(queued[0], source.read_text())
        self.assertEqual(list(self.m.hook_question_queue(n).glob('*.json')), [])


    def test_autoarchive_aged_routine_notices_keeps_questions_and_exact_bytes(self):
        notices = self.m._owners['terminal_notice']
        n = self.inst.node('root')
        now = self.inst.parse_time('2026-10-27T12:00Z')
        aged = ['- 2026-10-26T11:59Z from worker: FYI: export is available',
                '- 2026-10-26T11:59Z from notification: landing: export shipped <!-- communication:receipt -->',
                '- 2026-10-26T11:59Z from machine: command completed: command completed successfully']
        keep = ['- 2026-10-20T00:00Z from worker: Which export?',
                '- 2026-10-20T00:00Z from worker: FYI: Choose an export <!-- host-question:ask -->',
                '- 2026-10-20T00:00Z from notification: question: choose blue <!-- communication:question -->',
                '- 2026-10-20T00:00Z from worker: FYI: please review the export',
                '- 2026-10-26T12:00Z from worker: FYI: exactly 24 hours',
                '- 2026-10-27T11:59Z from worker: landing: fresh export',
                '- undated from worker: FYI: unknown age']
        source = self.root / 'inbox.md'
        source.write_bytes(('# Inbox\r\n' + '\r\n'.join(aged + keep) + '\r\n').encode())
        head = self.git('rev-parse', 'HEAD', cwd=self.root).strip()
        with mock.patch.object(self.inst, 'now_dt', return_value=now):
            self.assertEqual(notices.autoarchive(self.inst, [n]), 3)
            self.assertEqual(notices.autoarchive(self.inst, [n]), 0)
        self.assertEqual(source.read_bytes(), ('# Inbox\r\n' + '\r\n'.join(keep) + '\r\n').encode())
        archive = (self.root / 'inbox-archive.md').read_bytes()
        for line in aged:
            self.assertIn((line + '\r\n').encode(), archive)
        self.assertEqual(archive.count(b'<!-- episode '), 3)
        self.assertIn(b'routine-notice-24h-v1', archive)
        self.assertEqual(self.git('rev-list', '--count', head + '..HEAD', cwd=self.root).strip(), '1')

    def test_owner_seen_archives_fresh_notices_but_ancestor_seen_does_not(self):
        notices = self.m._owners['terminal_notice']
        reminders = self.m._owners['lifecycle_read']
        n = self.new('project')
        line = '- ' + self.inst.now() + ' from worker: FYI: export available'
        question = '- ' + self.inst.now() + ' from worker: Which export?'
        source = n['dir'] / 'inbox.md'
        source.write_text(line + '\n' + question + '\n')
        with reminders.lifecycle_store(self.inst) as data:
            records = reminders.reconcile_requests(self.inst, n, [line, question], data)
        seen = {r['id']: {'reported_at': self.inst.now()} for r in records.values()}
        saved = self.root / '.brain/inbox-seen.json'
        saved.write_text(json.dumps({'version': 2, 'reporters': {'root': seen}}))
        self.assertEqual(notices.autoarchive(self.inst, [n]), 0)
        saved.write_text(json.dumps({'version': 2, 'reporters': {'project': seen}}))
        self.assertEqual(notices.autoarchive(self.inst, [n]), 1)
        self.assertEqual(source.read_text(), question + '\n')
        archive = (n['dir'] / 'inbox-archive.md').read_text()
        self.assertIn('owner-inbox-seen-v1', archive)
        # A recurrence has a different episode and cannot inherit the old seen receipt.
        self.m.inbox_append(n['dir'], line + '\n')
        self.assertEqual(notices.autoarchive(self.inst, [n]), 0)
        self.assertIn(line, source.read_text())

    def test_inbox_seen_records_action_and_refuses_other_node_item(self):
        n = self.new('project')
        line = '- ' + self.inst.now() + ' from worker: Apply the export'
        (n['dir'] / 'inbox.md').write_text(line + '\n')
        items = [i for i in json.loads(self.b(self.root, 'inbox', n['id'], '--json')) if i['kind'] == 'inbox']
        key = items[0]['id']
        self.b(self.root, 'inbox-seen', 'root', key, 'Export applied', ok=False)
        self.assertIn(line, (n['dir'] / 'inbox.md').read_text())
        self.b(self.root, 'inbox-seen', n['id'], key, 'Export applied')
        self.assertEqual((n['dir'] / 'inbox.md').read_text(), '')
        data = self.m.lifecycle_read(self.inst)
        self.assertEqual(data['answers'][-1]['answer'], 'Export applied')
        self.assertEqual(data['answers'][-1]['disposition'], 'acted-on')
        self.assertIn(line, (n['dir'] / 'inbox-archive.md').read_text())

    def test_dashboard_counts_open_asks_and_weekly_read_preserves_sources(self):
        source = self.root / 'inbox.md'
        fyi = '- 2020-01-01T00:00Z from worker: FYI: export available'
        question = '- 2020-01-01T00:00Z from worker: Which export?'
        source.write_text(fyi + '\n' + question + '\n')
        before = source.read_bytes()
        state = json.loads(self.b(self.root, 'ui', '--json'))
        self.assertEqual(next(n['inbox'] for n in state['nodes'] if n['id'] == 'root'), [question[2:]])
        self.assertFalse(any(fyi[2:] in i['text'] for i in state['inbox'] + state['attention']))
        routine = next(i for i in state['due'] if i['kind'] == 'inbox' and i['text'] == fyi[2:])
        self.assertTrue(routine['informational'])
        ask = next(i for i in state['due'] if i['kind'] == 'inbox' and i['text'] == question[2:])
        self.assertFalse(ask.get('informational'))
        self.b(self.root, 'inbox', 'root', '--weekly', '--json')
        self.assertEqual(source.read_bytes(), before)
        self.assertFalse((self.root / 'inbox-archive.md').exists())
        self.inbox()
        self.assertEqual(source.read_text(), question + '\n')

    def test_autoarchive_docker_report_ages_from_source_and_batches_nodes(self):
        notices = self.m._owners['terminal_notice']
        root, child = self.inst.node('root'), self.new('project')
        line = '- Docker leftovers (report only): stopped test resources <!-- docker-leftover:' + 'a' * 24 + ' -->'
        (self.root / 'inbox.md').write_text(line + '\n')
        os.utime(self.root / 'inbox.md', (1, 1))
        fyi = '- 2020-01-01T00:00Z from worker: FYI: export available'
        (child['dir'] / 'inbox.md').write_text(fyi + '\n')
        head = self.git('rev-parse', 'HEAD', cwd=self.root).strip()
        self.assertEqual(notices.autoarchive(self.inst, [root, child]), 2)
        self.assertEqual(self.git('rev-list', '--count', head + '..HEAD', cwd=self.root).strip(), '1')
        self.assertIn(line, (self.root / 'inbox-archive.md').read_text())

    def test_autoarchive_rejects_append_between_plan_and_publication(self):
        notices = self.m._owners['terminal_notice']
        n = self.inst.node('root')
        line = '- 2020-01-01T00:00Z from worker: FYI: export available\n'
        question = '- 2020-01-01T00:00Z from worker: Which export?\n'
        source = self.root / 'inbox.md'
        source.write_text(line)
        publish = notices.publish_archive
        def interleave(plan):
            self.m.inbox_append(n['dir'], question)
            publish(plan)
        with mock.patch.object(notices, 'publish_archive', side_effect=interleave):
            with self.assertRaisesRegex(OSError, 'inbox changed'):
                notices.autoarchive(self.inst, [n])
        self.assertEqual(source.read_text(), line + question)
        self.assertFalse((self.root / 'inbox-archive.md').exists())
        self.assertEqual(notices.autoarchive(self.inst, [n]), 1)
        self.assertEqual(source.read_text(), question)

    def test_autoarchive_plans_and_reconciles_without_instance_lock(self):
        notices = self.m._owners['terminal_notice']
        reminders = self.m._owners['lifecycle_read']
        instance = self.m._owners['state_signature']
        n = self.inst.node('root')
        source = self.root / 'inbox.md'
        source.write_text('- 2020-01-01T00:00Z from worker: FYI: available\n')
        plan, reconcile, publish = notices.archive_plan, reminders.reconcile_requests, notices.publish_archive
        observed = []
        def unlocked_plan(*args, **kwargs):
            self.assertIsNone(instance._LOCK['fh'])
            observed.append('plan')
            return plan(*args, **kwargs)
        def unlocked_reconcile(*args, **kwargs):
            self.assertIsNone(instance._LOCK['fh'])
            observed.append('reconcile')
            return reconcile(*args, **kwargs)
        def locked_publish(prepared):
            self.assertIsNotNone(instance._LOCK['fh'])
            # The identities being removed must already be durable.
            self.assertEqual(reminders.lifecycle_read(self.inst), prepared['lifecycle'])
            publish(prepared)
        with mock.patch.object(notices, 'archive_plan', side_effect=unlocked_plan), \
                mock.patch.object(reminders, 'reconcile_requests', side_effect=unlocked_reconcile), \
                mock.patch.object(notices, 'publish_archive', side_effect=locked_publish):
            with instance.brain_lock(self.inst):
                self.assertEqual(notices.autoarchive(self.inst, [n]), 1)
                self.assertIsNotNone(instance._LOCK['fh'])
        self.assertIn('plan', observed)
        self.assertEqual(observed.count('reconcile'), 2)
        self.assertEqual(source.read_text(), '')

    def test_autoarchive_replans_changed_source_seen_and_lifecycle(self):
        notices = self.m._owners['terminal_notice']
        reminders = self.m._owners['lifecycle_read']
        n = self.inst.node('root')
        source = self.root / 'inbox.md'
        line = '- 2020-01-01T00:00Z from worker: FYI: available\n'
        question = '- 2020-01-01T00:00Z from worker: Which export?\n'
        for resource in ('source', 'seen', 'lifecycle'):
            with self.subTest(resource=resource):
                source.write_text(line)
                plan, calls = notices.archive_plan, []
                def drift(*args, **kwargs):
                    self.assertIsNone(self.m._owners['state_signature']._LOCK['fh'])
                    result = plan(*args, **kwargs)
                    calls.append(True)
                    if len(calls) == 1:
                        if resource == 'source':
                            self.m.inbox_append(n['dir'], question)
                        elif resource == 'seen':
                            (self.root / '.brain/inbox-seen.json').write_text(
                                json.dumps({'version': 2, 'reporters': {'other': {}}}))
                        else:
                            with reminders.lifecycle_store(self.inst) as data:
                                data['usage']['concurrent'] = {'retained': True}
                    return result
                with mock.patch.object(notices, 'archive_plan', side_effect=drift):
                    with self.m.brain_lock(self.inst):
                        self.assertEqual(notices.autoarchive(self.inst, [n]), 1)
                self.assertEqual(len(calls), 2)
                self.assertEqual(source.read_text(), question if resource == 'source' else '')
                if resource == 'lifecycle':
                    self.assertEqual(reminders.lifecycle_read(self.inst)['usage']['concurrent'], {'retained': True})

    def test_autoarchive_defers_after_three_drifts_and_on_busy_lifecycle(self):
        import fcntl
        notices = self.m._owners['terminal_notice']
        reminders = self.m._owners['lifecycle_read']
        n = self.inst.node('root')
        source = self.root / 'inbox.md'
        line = '- 2020-01-01T00:00Z from worker: FYI: available\n'
        source.write_text(line)
        plan, calls = notices.archive_plan, []
        def drift(*args, **kwargs):
            result = plan(*args, **kwargs)
            calls.append(True)
            self.m.inbox_append(n['dir'], '- fixture from worker: Question ' + str(len(calls)) + '?\n')
            return result
        with mock.patch.object(notices, 'archive_plan', side_effect=drift):
            self.assertEqual(notices.autoarchive(self.inst, [n]), 0)
        self.assertEqual(len(calls), 3)
        self.assertIn(line, source.read_text())
        self.assertFalse((self.root / 'inbox-archive.md').exists())
        with reminders.lifecycle_store(self.inst):
            pass
        with (self.root / '.brain/wake/lifecycle.lock').open('a+') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with mock.patch.object(notices, 'archive_plan', wraps=plan) as planned:
                self.assertEqual(notices.autoarchive(self.inst, [n]), 0)
                self.assertEqual(planned.call_count, 3)
        self.assertIn(line, source.read_text())

    def test_autoarchive_does_not_wait_for_busy_source_append_lock(self):
        import fcntl
        notices = self.m._owners['terminal_notice']
        n = self.inst.node('root')
        source = self.root / 'inbox.md'
        line = '- 2020-01-01T00:00Z from worker: FYI: available\n'
        source.write_text(line)
        with source.open('rb') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(notices.autoarchive(self.inst, [n]), 0)
        self.assertEqual(source.read_text(), line)
        self.assertEqual(notices.autoarchive(self.inst, [n]), 1)

    def test_autoarchive_retirement_preserves_intervening_answers_and_new_requests(self):
        notices = self.m._owners['terminal_notice']
        reminders = self.m._owners['lifecycle_read']
        n = self.inst.node('root')
        source = self.root / 'inbox.md'
        old = '- 2020-01-01T00:00Z from worker: FYI: available'
        ask = '- 2020-01-01T00:00Z from worker: Which export?'
        new = '- 2020-01-01T00:00Z from worker: Which new export?'
        source.write_text(old + '\n' + ask + '\n')
        publish = notices.publish_archive
        def interleave(plan):
            publish(plan)
            self.m.inbox_append(n['dir'], new + '\n' + old + '\n', nonblocking=True)
            with reminders.lifecycle_store(self.inst) as data:
                records = reminders.reconcile_requests(self.inst, n, [ask, new, old], data)
                key = reminders.inbox_source_id(n, ask)
                reminders.record_answer(self.inst, n, ask, records[key]['id'], 'Blue export', data)
                data['usage']['concurrent'] = {'retained': True}
        with mock.patch.object(notices, 'publish_archive', side_effect=interleave):
            self.assertEqual(notices.autoarchive(self.inst, [n]), 1)
        saved = reminders.lifecycle_read(self.inst)
        self.assertEqual(saved['answers'][-1]['answer'], 'Blue export')
        self.assertEqual(saved['usage']['concurrent'], {'retained': True})
        visible = self.m.inbox_lines(n, readonly=True)
        self.assertIn(new, visible)
        self.assertIn(old, visible)  # recurrence cannot inherit the expired episode
        self.assertNotIn(ask, visible)

    def test_autoarchive_waits_for_post_publish_lifecycle_without_instance_lock(self):
        import fcntl
        notices = self.m._owners['terminal_notice']
        reminders = self.m._owners['lifecycle_read']
        instance = self.m._owners['state_signature']
        n = self.inst.node('root')
        source = self.root / 'inbox.md'
        old = '- 2020-01-01T00:00Z from worker: FYI: available\n'
        source.write_text(old)
        publish, store = notices.publish_archive, reminders.lifecycle_store
        held, checked = [], []
        def busy_after_publish(plan):
            publish(plan)
            lock = (self.root / '.brain/wake/lifecycle.lock').open('a+')
            held.append(lock)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.m.inbox_append(n['dir'], old, nonblocking=True)
        @contextlib.contextmanager
        def lifecycle(inst, readonly=False, blocking=True):
            if held:
                self.assertTrue(blocking)
                self.assertIsNone(instance._LOCK['fh'])
                checked.append(True)
                fcntl.flock(held[0], fcntl.LOCK_UN)
                held.pop().close()
            with store(inst, readonly=readonly, blocking=blocking) as data:
                yield data
        try:
            with mock.patch.object(notices, 'publish_archive', side_effect=busy_after_publish), \
                    mock.patch.object(reminders, 'lifecycle_store', side_effect=lifecycle):
                with instance.brain_lock(self.inst):
                    self.assertEqual(notices.autoarchive(self.inst, [n]), 1)
                    self.assertIsNotNone(instance._LOCK['fh'])
        finally:
            for lock in held:
                lock.close()
        self.assertEqual(checked, [True])
        self.assertIn(old.rstrip(), self.m.inbox_lines(n, readonly=True))


    def test_root_delivery_does_not_archive_fresh_child_machine_completion(self):
        child = self.new('project')
        source = child['dir'] / 'inbox.md'
        line = '- ' + self.inst.now() + ' from machine: command completed: command completed successfully\n'
        source.write_text(line)
        self.inbox()
        self.inbox()
        self.assertEqual(source.read_text(), line)
        self.assertFalse((child['dir'] / 'inbox-archive.md').exists())
        # An ancestor's read must leave the notice deliverable to its owner.
        own = json.loads(self.b(self.root, 'inbox', child['id'], '--new', '--json'))
        self.assertTrue(any(i['kind'] == 'inbox' and 'command completed' in i['text'] for i in own))
        self.inbox()
        self.assertEqual(source.read_text(), '')
        self.assertIn(line, (child['dir'] / 'inbox-archive.md').read_text())

    def test_autoarchive_commits_completed_nodes_when_later_publication_fails(self):
        notices = self.m._owners['terminal_notice']
        root, child = self.inst.node('root'), self.new('project')
        line = '- 2020-01-01T00:00Z from worker: FYI: export available\n'
        for n in [root, child]:
            (n['dir'] / 'inbox.md').write_text(line)
        head = self.git('rev-parse', 'HEAD', cwd=self.root).strip()
        publish = notices.publish_archive
        def fail_child(plan):
            if plan['source'].parent == child['dir']:
                raise OSError('second node unavailable')
            publish(plan)
        with mock.patch.object(notices, 'publish_archive', side_effect=fail_child):
            with self.assertRaisesRegex(OSError, 'second node unavailable'):
                notices.autoarchive(self.inst, [root, child])
        self.assertEqual((self.root / 'inbox.md').read_text(), '')
        self.assertIn(line, (self.root / 'inbox-archive.md').read_text())
        self.assertEqual(self.git('rev-list', '--count', head + '..HEAD', cwd=self.root).strip(), '1')
        self.assertEqual(self.git('status', '--porcelain', '--', 'inbox.md', 'inbox-archive.md', cwd=self.root), '')
        self.assertEqual(notices.autoarchive(self.inst, [root, child]), 1)
        self.assertEqual(self.git('status', '--porcelain', '--', 'inbox.md', 'inbox-archive.md', 'nodes/project/inbox.md', 'nodes/project/inbox-archive.md', cwd=self.root), '')
