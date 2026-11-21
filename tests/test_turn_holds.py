"""Stop refresh contract only; refresh computation performance belongs to its own lane."""
import argparse
import contextlib
import fcntl
import io
import importlib
import json
import os
import subprocess
import sys
import time
from unittest import mock

import core_loader
from scenario import CORE, Scenario


class StopRefreshContract(Scenario):
    fixture_init_in_process = True

    def setUp(self):
        super().setUp()
        self.root = self.init('fixture').resolve()
        self.m = core_loader.SourceFileLoader('stop_refresh_fixture', str(CORE / 'bin/brain')).load_module()
        self.inst = self.m.Instance(self.root)
        self.n = self.inst.node('root')
        self.refresh = importlib.import_module(self.m._owners['stop_reason'].__package__ + '.status_refresh')

    def changed_records(self):
        status = self.root / 'status.md'
        before = status.stat().st_mtime
        inbox = self.root / 'inbox.md'
        inbox.write_text('- 2026-01-01T00:00:00Z from worker: Finished the fixture.\n')
        os.utime(inbox, (before + 10, before + 10))
        self.git('add', 'inbox.md', cwd=self.root)
        self.git('commit', '-qm', 'fixture: record completion', cwd=self.root)

    def stop(self):
        data = {'cwd': str(self.root), 'session_id': 'unbound-read-only'}
        with mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(data))), \
                mock.patch.object(self.m, 'changes_unread', return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.m.hook_stop(argparse.Namespace(event='stop'))
        return output.getvalue()

    def test_committed_records_stop_with_timestamped_stale_facts(self):
        self.changed_records()
        self.assertEqual(self.stop(), '')
        self.assertIn('Facts stale since: ', (self.root / 'status.md').read_text())
        record = json.loads(self.refresh.stop_pending(self.inst, self.n).read_text())
        self.assertEqual(record['node'], 'root')
        self.assertIn('changed after facts', record['reason'])

    def test_contended_refresh_never_holds_stop_and_retains_stale_time(self):
        self.changed_records()
        with (self.root / '.brain/lock').open('a+') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            self.assertEqual(self.stop(), '')
        pending = self.refresh.stop_pending(self.inst, self.n)
        self.assertTrue(pending.exists())
        stamp = json.loads(pending.read_text())['at']
        self.refresh.defer_stop(self.inst, self.n, 'still pending')
        self.assertIn('Facts stale since: ' + stamp, (self.root / 'status.md').read_text())

    def test_slow_retry_is_killed_at_existing_bound_and_keeps_stale_mark(self):
        self.refresh.defer_stop(self.inst, self.n, 'fixture refresh failed')
        original = subprocess.Popen
        children = []
        def slow(*args, **kwargs):
            process = original([sys.executable, '-c', 'import time; time.sleep(60)'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            children.append(process)
            return process
        budget = importlib.import_module(self.refresh.__package__ + '.hook_budget')
        with mock.patch.object(subprocess, 'Popen', side_effect=slow), \
                mock.patch.object(budget, 'seconds', return_value=0.05):
            self.refresh.drain_stop(self.inst)
        self.assertIsNotNone(children[0].poll())
        self.assertTrue(self.refresh.stop_pending(self.inst, self.n).exists())
        self.assertIn('Facts stale since:', (self.root / 'status.md').read_text())

    def test_real_quiet_command_retries_the_pending_refresh(self):
        self.refresh.defer_stop(self.inst, self.n, 'fixture failed')
        self.b(self.root, 'now')
        budget = importlib.import_module(self.refresh.__package__ + '.hook_budget')
        deadline = time.monotonic() + budget.derivation('refresh')['adapter_timeout_s']
        pending = self.refresh.stop_pending(self.inst, self.n)
        while pending.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        log = pending.with_suffix('.log')
        self.assertFalse(pending.exists(), log.read_text() if log.exists() else 'worker did not start')
        self.assertNotIn('Facts stale since:', (self.root / 'status.md').read_text())

    def test_uncommitted_records_still_require_the_record_commit(self):
        (self.root / 'brief.md').write_text('Uncommitted fixture decision\n')
        self.assertIn('not committed', self.stop())
