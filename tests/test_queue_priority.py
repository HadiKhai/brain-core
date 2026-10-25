"""Explicit admission reorder only touches queued verify rows in one node."""
import contextlib
import copy
from unittest import mock

import core_loader
from scenario import CORE, Scenario


class QueuePriority(Scenario):
    def test_move_preserves_other_rows_and_running_owners(self):
        m = core_loader.SourceFileLoader('queue_priority_fixture', str(CORE / 'bin/brain')).load_module()
        owner = m.move_verify_before.__globals__['_m_machine']
        scope = ['instance', 'node']
        rows = [dict(token=k, kind='verify', state='queued', scope=scope) for k in ('a', 'b', 'c')]
        rows.insert(1, dict(token='running', kind='verify', state='running', scope=scope))
        queue = mock.Mock(rows=copy.deepcopy(rows))
        with mock.patch.object(owner, 'machine_scope', return_value=scope), \
                mock.patch.object(owner, 'machine_queue', return_value=contextlib.nullcontext(queue)):
            m.move_verify_before(mock.Mock(), {'id': 'app'}, 'c', 'a')
        self.assertEqual([r['token'] for r in queue.rows], ['c', 'a', 'running', 'b'])
        self.assertEqual(sorted(queue.rows, key=lambda r: r['token']), sorted(rows, key=lambda r: r['token']))

    def test_running_foreign_and_missing_targets_refuse_atomically(self):
        m = core_loader.SourceFileLoader('queue_refusal_fixture', str(CORE / 'bin/brain')).load_module()
        owner = m.move_verify_before.__globals__['_m_machine']
        scope = ['instance', 'node']
        rows = [dict(token='a', kind='verify', state='queued', scope=scope),
                dict(token='b', kind='verify', state='running', scope=scope),
                dict(token='c', kind='verify', state='queued', scope=['foreign', 'node'])]
        for token in ('b', 'c', 'missing'):
            queue = mock.Mock(rows=copy.deepcopy(rows))
            with mock.patch.object(owner, 'machine_scope', return_value=scope), \
                    mock.patch.object(owner, 'machine_queue', return_value=contextlib.nullcontext(queue)):
                with self.assertRaises(ValueError):
                    m.move_verify_before(mock.Mock(), {'id': 'app'}, token, 'a')
            self.assertEqual(queue.rows, rows)
