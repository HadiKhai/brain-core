"""Evidence-sensitive reports over fabricated commitments and job records."""
import datetime
import tempfile
from unittest import mock
import unittest
from pathlib import Path
import core_loader

CORE = Path(__file__).resolve().parent.parent


class CommitmentDigest(unittest.TestCase):
    def setUp(self):
        self.m = core_loader.SourceFileLoader('digest_fixture', str(CORE / 'bin/brain')).load_module()

    def rows(self):
        return [dict(id='TASK1', title='First', status='testing', created='2026-01-01',
                     requirement='REQ1', evidence=['focused checks']),
                dict(id='TASK2', title='Second', status='merged', merge_sha='a' * 40,
                     code_path='src/app.py', evidence=['commit on main']),
                dict(id='TASK3', title='External', status='done', source='linear'),
                dict(id='TASK4', title='Later', status='parked', park_reason='dependency', resume='adapter exists')]

    def test_only_landed_evidence_enters_done(self):
        result = self.m.commitment_digest(self.rows(), {'REQ1': {'at': '2026-01-01', 'ref': 'request:1', 'quote': 'Ship first'}}, {'TASK2'})
        self.assertEqual([r['id'] for r in result['done']], ['TASK2'])
        self.assertEqual(len(result['promised']), 4)
        self.assertEqual([r['id'] for r in result['not_done']], ['TASK1', 'TASK3', 'TASK4'])
        self.assertIn('2026-01-01', result['promised'][0]['source'])
        self.assertIn('request:1', result['promised'][0]['source'])

    def test_candidate_commit_and_external_done_remain_unfinished(self):
        result = self.m.commitment_digest(self.rows(), {}, set())
        self.assertEqual(result['done'], [])
        self.assertEqual(len(result['not_done']), 4)

    def test_ready_depends_on_proven_completion(self):
        rows = self.rows() + [dict(id='TASK5', title='Ready', status='ready', depends_on=['TASK2']),
                              dict(id='TASK6', title='Blocked', status='ready', depends_on=['TASK3'])]
        result = self.m.commitment_digest(rows, {}, {'TASK2'})
        self.assertEqual([r['id'] for r in result['next']], ['TASK5'])

    def test_domain_runnable_set_and_backfill_date_basis_are_preserved(self):
        rows = [dict(id='TASK1', title='Cross-node', status='ready', depends_on=['other:TASK2'], requirement='REQ1'),
                dict(id='TASK2', title='Held', status='ready')]
        result = self.m.commitment_digest(rows, {'REQ1': {'at':'2026-01-01', 'ref':'file:1',
                                                       'date_basis':'file-observed'}}, set(), {'TASK1'})
        self.assertEqual([r['id'] for r in result['next']], ['TASK1'])
        self.assertIn('file-observed context', result['promised'][0]['source'])

    def test_parked_and_dropped_stay_visible(self):
        rows = self.rows() + [dict(id='TASK5', title='Removed', status='dropped', drop_reason='scope decision')]
        result = self.m.commitment_digest(rows, {}, {'TASK2'})
        text = self.m.digest_text('sample', result)
        for block in ('Promised:', 'Done ✓:', 'Not done ✗:', 'Needs you:', 'Next:'):
            self.assertIn(block, text)
        self.assertIn('adapter exists', text)
        self.assertIn('scope decision', text)
        self.assertLess(text.index('Promised:'), text.index('Done ✓:'))

    def test_retro_flags_actual_multiple_rounds_and_failures(self):
        result = self.m.efficiency_retro([dict(Job='job/a', Reviews='3', Outcome='failed', Hours='1.5',
                                              **{'Active h':'0.5', 'Compactions':'3'}),
                                         dict(Job='job/b', Reviews='1', Outcome='merged', Hours='0.2')])
        self.assertEqual(result['jobs'], 2)
        self.assertEqual(result['failed'], 1)
        self.assertEqual(result['review_rounds'], 4)
        self.assertEqual(result['elapsed_hours'], 1.7)
        self.assertEqual([f['job'] for f in result['findings']], ['job/a'])

    def test_domain_checker_controls_scoped_completion_and_external_labels(self):
        rows = self.rows()
        rows[2]['provider'] = {'state_type':'completed'}
        calls = []
        def checker(node, row):
            calls.append(row['id'])
            if row['id'] == 'TASK2':
                raise ValueError('unchanged baseline')
        done = self.m.verified_completions({}, rows, checker)
        self.assertEqual(done, {'TASK3'})
        self.assertEqual(calls, ['TASK2', 'TASK3'])
        text = self.m.digest_text('sample', self.m.commitment_digest(rows, {}, done))
        self.assertIn('external provider completion; local implementation unproven', text)

    def test_unknown_metrics_remain_unknown(self):
        result = self.m.efficiency_retro([dict(Job='job/a', Hours='?', Reviews='', Outcome='blocked')])
        self.assertIsNone(result['elapsed_hours'])
        self.assertIsNone(result['review_rounds'])
        self.assertEqual(result['unknown_metrics'], ['elapsed_hours', 'review_rounds'])


class DailyRetro(unittest.TestCase):
    def test_opt_in_sampler_records_previous_local_day_once(self):
        m = core_loader.SourceFileLoader('daily_fixture', str(CORE / 'bin/brain')).load_module()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            node = {'id': 'root', 'dir': root, 'fm': {'daily_retro': 'on'}}
            now = datetime.datetime(2026, 1, 2, 7, tzinfo=datetime.timezone.utc)
            inst = mock.Mock(root=root)
            inst.node.return_value = node
            inst.nodes.return_value = [node]
            inst.now_dt.return_value = now
            inst.now.return_value = now.isoformat()
            inst.parse_time.side_effect = lambda text: datetime.datetime.fromisoformat(text) if text else None
            rows = [dict(Job='job/old', Ended='2026-01-01T21:00:00+00:00', Outcome='failed', Hours='1', Reviews='3'),
                    dict(Job='job/today', Ended='2026-01-02T01:00:00+00:00', Outcome='merged', Hours='1', Reviews='1')]
            with mock.patch.object(m, 'read_jobs', return_value=rows) as read:
                m.daily_retro(inst)
                files = list((root / '.brain/daily-retro').glob('*/*.json'))
                self.assertEqual(len(files), 1)
                content = files[0].read_text()
                self.assertIn('2026-01-01', content)
                self.assertIn('job/old', content)
                self.assertNotIn('job/today', content)
                m.daily_retro(inst)
                self.assertEqual(read.call_count, 1)
                self.assertEqual(files[0].read_text(), content)
            node['fm']['daily_retro'] = 'off'
            with mock.patch.object(m, 'read_jobs', side_effect=AssertionError('opted out')):
                m.daily_retro(inst)
