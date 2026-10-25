"""Warm cache completion must retain data and report incomplete aggregation."""
import unittest
from unittest import mock
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from brain import ui_fill


class WarmDashboardFill(unittest.TestCase):
    def test_cached_slices_are_drained_before_declaring_complete(self):
        spend = mock.Mock(value={'complete': False, 'files': 3, 'read': 0})
        def advance():
            spend.value['read'] += 1
            spend.value['complete'] = spend.value['read'] == 3
            return True
        spend.advance.side_effect = advance
        ui_fill.advance_warm(spend)
        self.assertEqual(spend.advance.call_count, 3)
        self.assertEqual(spend.value['files'], spend.value['read'])
        self.assertTrue(ui_fill.complete({'spend': spend.value}))

    def test_deadline_leaves_partial_data_and_truthful_filling_state(self):
        spend = mock.Mock(value={'complete': False, 'files': 9, 'read': 4})
        with mock.patch.object(ui_fill.time, 'monotonic', side_effect=[0, 0.2, 1.1]):
            ui_fill.advance_warm(spend)
        self.assertEqual(spend.advance.call_count, 2)
        self.assertFalse(ui_fill.complete({'spend': spend.value}))
        self.assertEqual(spend.value['read'], 4)
