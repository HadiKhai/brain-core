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
        alerts = self.m.runaway_jobs(self.inst, owner)
        self.assertEqual(len(alerts), 1)
        self.assertIn('over its 10M-token budget', alerts[0])
        self.assertIn('over twice the median', alerts[0])
        # Human rollups and ancestors cannot act on the child's job advice.
        self.assertFalse(any(i['kind'] == 'job' for i in self.inbox()))
        self.assertFalse(any(i['kind'] == 'job' for i in self.inbox('parent')))
        self.assertFalse(any(i['kind'] == 'job' for i in self.inbox(sibling['id'])))
        own = self.inbox(owner['id'])
        self.assertEqual([i['text'] for i in own if i['kind'] == 'job'], alerts)
        self.assertTrue(all(i.get('informational') for i in own if i['kind'] == 'job'))
        due = json.loads(self.b(self.root, 'due', owner['id'], '--json'))
        self.assertEqual([i['text'] for i in due if i['kind'] == 'job'], alerts)
        self.assertTrue(all(i['informational'] for i in due if i['kind'] == 'job'))
        root_due = json.loads(self.b(self.root, 'due', 'root', '--json'))
        self.assertFalse(any(i['kind'] == 'job' for i in root_due))
        state = json.loads(self.b(self.root, 'ui', '--json'))
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
        # Advice alone is informational and does not make a quiet tick actionable.
        with mock.patch.object(self.m, 'load', return_value=self.inst), \
                mock.patch.object(self.m, 'changes_due', return_value=[]), \
                mock.patch.object(self.m, 'due_evidence', return_value=(
                    {'jobs': [], 'worktrees': {}, 'handbacks': {}, 'inbox': []}, '', '')), \
                mock.patch.object(self.m, 'status_lag', return_value=None), \
                self.assertRaises(SystemExit) as quiet:
            self.m.cmd_due(argparse.Namespace(id='root', quiet=True, json=False))
        self.assertEqual(quiet.exception.code, 0)
