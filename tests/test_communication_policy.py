"""Delivery policy uses synthetic instances and no host transport."""
import datetime
import json
import sys
from unittest import mock
from scenario import Scenario, CORE

sys.path.insert(0, str(CORE / 'lib'))
from brain import communication, communication_policy
from brain.instance import Instance


class CommunicationPolicy(Scenario):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / 'fixture'
        self.root.mkdir()
        (self.root / 'instance.md').write_text('---\nprincipals: sample\ntimezone: Asia/Tokyo\nruntimes: codex\ndefault_runtime: codex\n---\n')
        (self.root / 'node.md').write_text('---\nowners: sample\n---\n')
        self.inst = Instance(self.root)
        self.inst.local_of('root')['message_policy'] = {'quiet_start': '00:00', 'quiet_end': '07:00'}
        self.inst.save_local()
        self.n = self.inst.node('root')
        self.clock = mock.patch.object(Instance, 'now_dt', return_value=datetime.datetime.fromisoformat('2026-01-01T18:30:00+00:00'))
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.wakes = mock.patch.object(communication._m_hosts, 'request_wake')
        self.send = self.wakes.start()
        self.addCleanup(self.wakes.stop)

    def notice(self, key='one'):
        return communication.communication_record(self.inst, self.n, 'fyi', 'Example ' + key, key)

    def test_timezone_defer_one_morning_and_late_work_retained(self):
        first = self.notice()
        self.assertTrue(first['deferred'])  # 03:30 at the instance, not at the UTC clock
        self.assertFalse((self.root / 'inbox.md').exists())
        self.assertEqual(communication_policy.morning_brief(self.inst, self.n)['state'], 'quiet')
        self.now.return_value = datetime.datetime.fromisoformat('2026-01-01T22:01:00+00:00')
        self.assertEqual(communication_policy.morning_items(self.inst, [self.n])[0]['kind'], 'morning-brief')
        result = communication_policy.morning_brief(self.inst, self.n)
        self.assertEqual(result['state'], 'published')
        self.assertIn('Example one', (self.root / 'inbox.md').read_text())
        self.assertEqual(communication_policy.morning_brief(Instance(self.root), self.n)['state'], 'already-published')
        self.assertEqual(communication_policy.morning_items(self.inst, [self.n]), [])
        self.now.return_value = datetime.datetime.fromisoformat('2026-01-02T18:30:00+00:00')
        later = self.notice('two')
        state = communication.communication_read(self.inst)
        self.assertNotIn(later['id'], next(iter(state['morning_briefs'].values()))['items'])
        self.now.return_value = datetime.datetime.fromisoformat('2026-01-02T22:01:00+00:00')
        self.assertEqual(communication_policy.morning_brief(self.inst, self.n)['items'], [later['id']])
        self.send.assert_not_called()  # morning delivery never changes root's wake policy

    def test_questions_bypass_quiet_and_notice_cannot_claim_urgency(self):
        spec = {'options': [{'id': 'a', 'label': 'Continue', 'action': 'routine'},
                            {'id': 'b', 'label': 'Hold', 'action': 'routine'}],
                'recommendation': 'a', 'default': 'a', 'deadline': '2026-01-03T12:00:00Z'}
        question = communication.communication_record(self.inst, self.n, 'question', 'Choose', 'q', spec)
        self.assertNotIn('deferred', question)
        self.send.assert_called_once()
        with self.assertRaises(ValueError):
            communication.communication_record(self.inst, self.n, 'urgent', 'Skip policy', 'u')

    def test_interrupted_publication_replays_exact_snapshot_without_losing_new_work(self):
        first = self.notice()
        self.now.return_value = datetime.datetime.fromisoformat('2026-01-01T22:01:00+00:00')
        with mock.patch.object(communication_policy._m_transitions, 'inbox_append', side_effect=OSError('fixture interruption')):
            with self.assertRaises(OSError):
                communication_policy.morning_brief(self.inst, self.n)
        self.now.return_value = datetime.datetime.fromisoformat('2026-01-02T18:30:00+00:00')
        second = self.notice('two')
        self.now.return_value = datetime.datetime.fromisoformat('2026-01-02T22:01:00+00:00')
        result = communication_policy.morning_brief(self.inst, self.n)
        self.assertEqual(result['items'], [first['id']])
        self.assertNotIn(second['id'], result['items'])
        # Recovery publishes the old brief; a new daily brief remains separately available.
        self.assertEqual(communication_policy.morning_brief(self.inst, self.n)['items'], [second['id']])

    def test_bad_policy_zone_and_linked_state_fail_closed(self):
        self.inst.tz = 'Invalid/Zone'
        with self.assertRaises(ValueError):
            self.notice()
        self.inst.tz = 'Asia/Tokyo'
        self.inst.local_of('root')['message_policy']['quiet_end'] = '25:00'
        with self.assertRaises(ValueError):
            self.notice()
        self.assertEqual(communication_policy.morning_items(self.inst, [self.n]), [])

    def test_operator_brief_aggregates_nodes_once_without_consuming_node_briefs(self):
        first = self.notice()
        self.run_(['new', 'maple', '--title', 'Maple'], cwd=self.root, brain=CORE / 'bin' / 'brain')
        self.inst = Instance(self.root)
        child = self.inst.node('maple')
        second = communication.communication_record(self.inst, child, 'park', 'Sample park', 'park')
        self.assertEqual(communication_policy.operator_brief(self.inst)['state'], 'quiet')
        self.now.return_value = datetime.datetime.fromisoformat('2026-01-01T22:01:00+00:00')
        result = communication_policy.operator_brief(self.inst)
        self.assertEqual(set(result['items']), {first['id'], second['id']})
        text = (self.root / 'inbox.md').read_text()
        self.assertIn('maple: Sample park', text)
        self.assertEqual(communication_policy.operator_brief(Instance(self.root))['state'], 'already-published')
        self.assertEqual(communication_policy.morning_brief(self.inst, child)['items'], [second['id']])
        self.send.assert_not_called()

    def test_operator_recovery_uses_publication_day_and_retains_late_work(self):
        first = self.notice()
        self.now.return_value = datetime.datetime.fromisoformat('2026-01-01T22:01:00+00:00')
        original = communication_policy._m_transitions.inbox_append
        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError('fixture crash after inbox append')
        with mock.patch.object(communication_policy._m_transitions, 'inbox_append', side_effect=interrupted):
            with self.assertRaises(OSError):
                communication_policy.operator_brief(self.inst)
        self.now.return_value = datetime.datetime.fromisoformat('2026-01-02T18:30:00+00:00')
        second = self.notice('late')
        self.now.return_value = datetime.datetime.fromisoformat('2026-01-02T22:01:00+00:00')
        self.assertEqual(communication_policy.operator_brief(self.inst)['items'], [first['id']])
        self.assertEqual(communication_policy.operator_brief(self.inst)['state'], 'already-published')
        self.assertEqual((self.root / 'inbox.md').read_text().count('<!-- operator-brief:'), 1)
        self.now.return_value = datetime.datetime.fromisoformat('2026-01-03T22:01:00+00:00')
        self.assertEqual(communication_policy.operator_brief(self.inst)['items'], [second['id']])
        self.send.assert_not_called()

    def test_empty_operator_brief_does_not_reserve_the_day(self):
        self.now.return_value = datetime.datetime.fromisoformat('2026-01-01T22:01:00+00:00')
        self.assertEqual(communication_policy.operator_brief(self.inst)['state'], 'empty')
        self.now.return_value = datetime.datetime.fromisoformat('2026-01-01T18:30:00+00:00')
        first = self.notice()
        self.now.return_value = datetime.datetime.fromisoformat('2026-01-01T22:01:00+00:00')
        self.assertEqual(communication_policy.operator_brief(self.inst)['items'], [first['id']])

    def test_operator_policy_is_explicit_and_archived_sources_are_excluded(self):
        self.run_(['new', 'maple', '--title', 'Maple'], cwd=self.root, brain=CORE / 'bin' / 'brain')
        self.inst = Instance(self.root)
        child = self.inst.node('maple')
        communication.communication_record(self.inst, child, 'fyi', 'Archived notice', 'archived')
        first = self.notice()
        nodes = [self.n, {**child, 'archived': True}]
        self.now.return_value = datetime.datetime.fromisoformat('2026-01-01T22:01:00+00:00')
        with mock.patch.object(Instance, 'nodes', return_value=nodes):
            self.assertEqual(communication_policy.operator_brief(self.inst)['items'], [first['id']])
        self.inst.local_of('root').pop('message_policy')
        with self.assertRaises(ValueError):
            communication_policy.operator_brief(self.inst)
        self.send.assert_not_called()
