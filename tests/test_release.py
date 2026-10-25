"""Release preflight is pure; mocked transport proves no forced or partial push."""
import datetime
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import core_loader
from scenario import CORE, Scenario


class GreenRelease(Scenario):
    def setup_release(self):
        m = core_loader.SourceFileLoader('green_release_fixture', str(CORE / 'bin/brain')).load_module()
        folder = self.tmp / 'node'
        folder.mkdir()
        log = folder / 'verify.log'
        log.write_text('passed\n')
        head, base = '1' * 40, '2' * 40
        key = 'a' * 24
        entry = dict(key=key, branch='job', head='3' * 40)
        data = dict(version=1, state='complete', publication_succeeded=True, tip=head, origin_base=base,
                    accepted=[entry], runs=[dict(head=head, result=0, command='verify', seconds=2.0,
                    log=str(log), log_digest=hashlib.sha256(log.read_bytes()).hexdigest())])
        (folder / 'job-trains').mkdir()
        (folder / 'job-trains/train.json').write_text(json.dumps(data))
        n = dict(id='app', dir=folder, home_path=self.tmp, fm={'verify': 'verify', 'release_auto': 'on'}, local={})
        inst = SimpleNamespace(now_dt=lambda: datetime.datetime(2035, 1, 1, 12, tzinfo=datetime.timezone.utc),
                               parse_time=lambda v: datetime.datetime.fromisoformat(v))
        return m, inst, n, data

    def test_release_requires_current_full_digest_and_release_section(self):
        m, inst, n, data = self.setup_release()
        owner = m.release_candidate.__globals__
        with mock.patch.object(m, 'release_ref', return_value=data['tip']), \
                mock.patch.object(m, 'release_reviews'), \
                mock.patch.object(owner['_m_common'], 'git', return_value=SimpleNamespace(returncode=0, stdout='## 2035-01-01: Release 1\nFor agents: update\nAfter upgrading: restart\n')):
            selected = m.release_candidate(inst, n, 1)
            self.assertEqual(selected['tip'], data['tip'])
            Path(data['runs'][0]['log']).write_text('modified proof')
            with self.assertRaises(ValueError):
                m.release_candidate(inst, n, 1)

    def test_atomic_push_has_no_force_and_pins_head_and_tag(self):
        m, inst, n, data = self.setup_release()
        owner = m.push_release.__globals__
        calls = []
        def git(*args, **kw):
            calls.append(args)
            if args[0] == 'ls-remote':
                return SimpleNamespace(returncode=0, stdout=data['origin_base'] + '\trefs/heads/main\n')
            return SimpleNamespace(returncode=0, stdout='')
        with mock.patch.object(owner['_m_common'], 'git', side_effect=git):
            m.push_release(n, data, 'release-1')
        push = next(c for c in calls if 'push' in c)
        self.assertIn('--atomic', push)
        self.assertNotIn('--force', ' '.join(push))
        self.assertIn(data['tip'] + ':refs/heads/main', push)
        self.assertIn(data['tip'] + ':refs/tags/release-1', push)

    def test_remote_movement_never_reaches_push(self):
        m, inst, n, data = self.setup_release()
        owner = m.push_release.__globals__
        with mock.patch.object(owner['_m_common'], 'git', return_value=SimpleNamespace(returncode=0, stdout='4' * 40 + '\trefs/heads/main\n')) as git:
            with self.assertRaises(ValueError):
                m.push_release(n, data, 'release-1')
        self.assertFalse(any(c.args[0] == 'push' for c in git.call_args_list))

    def test_daily_cap_includes_uncertain_attempts(self):
        m, inst, n, _ = self.setup_release()
        rows = [dict(at='2035-01-01T01:00:00+00:00', state=state) for state in ('complete', 'prepared')]
        self.assertFalse(m.release_due(inst, n, rows))
        self.assertTrue(m.release_due(inst, n, rows[:1]))

    def test_advertised_origin_race_and_existing_hook_are_guarded(self):
        m, inst, n, data = self.setup_release()
        hook = self.tmp / 'existing-pre-push'
        marker = self.tmp / 'existing-ran'
        hook.write_text('#!/bin/sh\ncat > ' + str(marker) + '\n')
        hook.chmod(0o700)
        guard = self.tmp / 'guard.py'
        guard.write_text(m.release_guard_script(data['origin_base'], data['tip'], 'release-1', hook))
        def run(base):
            stdin = ('source ' + data['tip'] + ' refs/heads/main ' + base + '\nsource '
                     + data['tip'] + ' refs/tags/release-1 ' + '0' * 40 + '\n')
            return subprocess.run([sys.executable, str(guard), 'origin', 'fixture'], input=stdin,
                                  text=True, capture_output=True)
        self.assertNotEqual(run('4' * 40).returncode, 0)
        self.assertFalse(marker.exists())
        self.assertEqual(run(data['origin_base']).returncode, 0)
        self.assertTrue(marker.is_file())
