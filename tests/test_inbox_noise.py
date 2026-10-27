"""Inbox visibility and coordinator-only advice; fabricated records and telemetry only."""
import argparse
import contextlib
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

    def test_terminal_machine_notice_expires_after_delivery_without_changing_source(self):
        reminders = self.m._owners['lifecycle_read']
        path = self.root / 'inbox.md'
        notice = '- 2026-10-25T15:41:07.261523+00:00 from machine: command completed: command completed successfully <!-- machine-wait:00ad91d121f4a41cac633f58bb1275f8:completed -->'
        question = '- 2026-10-25 15:42 +00:00 from worker: Which export? <!-- host-question:fixture -->'
        text = notice + '\n' + question + '\n'
        path.write_text(text)
        first = self.inbox()
        self.assertTrue(any('command completed' in i['text'] for i in first))
        second = self.inbox()
        self.assertFalse(any('command completed' in i['text'] for i in second))
        self.assertTrue(any('Which export?' in i['text'] for i in second))
        self.assertEqual(path.read_text(), text)
        inst = self.m.Instance(self.root)
        key = reminders.inbox_source_id(inst.node('root'), notice)
        record = reminders.lifecycle_read(inst)['inbox']['root'][key]
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
        notice = '- 2026-10-25 15:40 +00:00 from machine: command completed: command completed successfully'
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
