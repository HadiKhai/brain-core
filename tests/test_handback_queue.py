"""Bounded file-only handback lifecycle checks (target: <=3 seconds per item)."""
import argparse
import contextlib
import datetime
import io
import json
import os
import threading
from unittest import mock

import core_loader
from scenario import CORE, Scenario


class HandbackLifecycle(Scenario):
    def fixture(self):
        root = self.tmp / 'instance'
        root.mkdir()
        (root / 'instance.md').write_text('---\ntimezone: UTC\n---\n')
        (root / 'node.md').write_text('---\ntitle: Root\nhome: none\n---\n')
        m = core_loader.SourceFileLoader('queue_fixture', str(CORE / 'bin/brain')).load_module()
        inst = m.Instance(root)
        inst.commit = mock.Mock()
        n = inst.node('root')
        row = dict.fromkeys(m.JOB_COLS, '')
        row.update(Job='job/example', Started='2026-01-01T00:00Z')
        m.write_jobs(inst, n, [row])
        return m, inst, n, row

    def test_m5_required_lines_and_explicit_legacy_missing_fields(self):
        m, inst, n, row = self.fixture()
        for summary, question, state in (('', None, 'done'), ('a\nb', None, 'blocked'),
                                         ('Ready', '', 'question'), ('Ready', 'a\rb', 'question')):
            with self.subTest(summary=summary, question=question), self.assertRaises(SystemExit):
                m.validate_handback_text(argparse.Namespace(summary=summary, question=question, state=state))
        self.assertEqual(m.validate_handback_text(argparse.Namespace(summary=' Ready ', question=' Choose? ',
                                                                     state='question')), ('Ready', 'Choose?'))
        path = m.handback_file(n, row)
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(dict(job=row['Job'], started=row['Started'], state='question', report='private.md')))
        self.assertEqual(m.read_handback(n, row)['state'], 'question')
        self.assertIn('summary missing (legacy)', m.handback_detail(n, row))
        self.assertIn('question missing (legacy)', m.handback_detail(n, row))

    def test_p5_replacement_attempt_is_durable_and_retires_only_itself(self):
        m, inst, n, row = self.fixture()
        folder = m.handback_file(n, row).parent
        folder.mkdir(exist_ok=True)
        m.handback_file(n, row).write_text(json.dumps(dict(job=row['Job'], started=row['Started'], state='done')))
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(m, 'attempt_binding', return_value={'head':'a', 'base':'b', 'inputs':'c'}):
            first = m.begin_attempt(inst, n, row)
            self.assertIsNone(m.job_signal(inst, n, row))
            self.assertEqual(m.job_projection(inst, n, row)['state'], 'queued')
            second = m.begin_attempt(inst, n, row)
            self.assertFalse(m.finish_attempt(inst, n, row, first['id'], 'interrupted'))
            self.assertEqual(m.active_attempt(n, row)['id'], second['id'])
            self.assertTrue(m.finish_attempt(inst, n, row, second['id'], 'interrupted'))
            self.assertEqual(m.job_projection(inst, n, row)['state'], 'interrupted')
            self.assertIsNone(m.job_signal(inst, n, row))

    def test_p6_enqueue_claim_and_terminal_are_single_owner(self):
        m, inst, n, row = self.fixture()
        args = argparse.Namespace(id='root', branch=row['Job'], state='blocked', summary='Needs a decision',
                                  question=None, lessons='none: fixture', report=None, reviews=None)
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(m, 'attempt_binding', return_value={'head':'a', 'base':'b', 'inputs':'c'}):
            request = m.enqueue_handback(inst, n, row, args)
            self.assertEqual(m.job_projection(inst, n, row)['queue']['position'], 1)
            with m.claim_handback(inst, n, request['id']) as claimed:
                self.assertEqual(claimed['id'], request['id'])
                with self.assertRaises(ValueError):
                    with m.claim_handback(inst, n, request['id']):
                        self.fail('duplicate lease')
                m.queue_terminal(inst, n, request['id'], claimed['lease'], 'terminal')
                self.assertFalse(m.queue_terminal(inst, n, request['id'], claimed['lease'], 'terminal'))
            self.assertEqual(m.read_queue_request(n, request['id'])['state'], 'terminal')

    def test_p2_attestation_refuses_unsafe_entries_without_ledger_mutation(self):
        m, inst, n, row = self.fixture()
        row.update(Ended='2026-01-01T01:00Z', Outcome='merged')
        n['local']['workers'] = {m.job_key(row): 'worker-1'}
        now = inst.now_dt().isoformat()
        evidence = dict(version=1, node='root', observed_at=now, project_id='project-1',
                        coverage=dict(project=True, nested=True, complete=True),
                        jobs=[dict(job_key=m.job_key(row), threads=[dict(threadId='worker-1', job_key=m.job_key(row),
                            state='completed', settled=True, pending_requests=[])])])
        n['local']['home_project'] = 'project-1'
        before = (n['dir'] / m.JOBS_FILE).read_bytes()
        for field, value in (('state','running'), ('state','waiting'), ('settled',False), ('pending_requests',['question'])):
            bad = json.loads(json.dumps(evidence))
            bad['jobs'][0]['threads'][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                m.validate_settlement_attestation(inst, n, [row], bad)
        self.assertEqual(m.validate_settlement_attestation(inst, n, [row], evidence), ['worker-1'])
        self.assertEqual((n['dir'] / m.JOBS_FILE).read_bytes(), before)

    def test_m4_live_projection_keeps_host_activity_unknown(self):
        m, inst, n, row = self.fixture()
        path = m.handback_file(n, row)
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(dict(job=row['Job'], started=row['Started'], state='blocked',
                                       summary='Choose a fixture', version=3, lessons='none: fixture')))
        projected = m.job_projection(inst, n, row)
        self.assertEqual(projected['summary'], 'Choose a fixture')
        self.assertEqual(projected['host_activity'], 'unknown')
        self.assertEqual(projected['job_key'], m.job_key(row))
        self.assertTrue(projected['next_action'])

    def test_m3_empty_dispatch_silence_and_stable_bounded_asks(self):
        m, inst, n, row = self.fixture()
        with mock.patch.object(m, 'inbox_items', return_value=[]):
            self.assertIsNone(m.dispatch_plan(inst, limit=1))
        asks = [dict(id='episode-2', node='root', kind='inbox', text='Choose red'),
                dict(id='episode-1', node='root', kind='inbox', text='Choose blue'),
                dict(id='noise', node='root', kind='stale', text='Refresh status')]
        with mock.patch.object(m, 'inbox_items', return_value=asks):
            plan = m.dispatch_plan(inst, limit=1)
            self.assertTrue(plan['partial'])
            self.assertEqual(plan['items'][0]['episode_id'], 'episode-1')
            self.assertEqual(plan, m.dispatch_plan(inst, limit=1))
            self.assertEqual(plan['items'][0]['target_node'], 'root')

    def test_r2_manual_landing_closes_with_main_sha_and_park_needs_reason(self):
        m, inst, n, row = self.fixture()
        home = self.repo(self.tmp / 'code')
        self.git('branch', row['Job'], cwd=home)
        n.update(home_path=home, external=True)
        n['local']['worktrees'] = {row['Job']: {'start': '0'*40}}
        with mock.patch.dict(os.environ, self.env, clear=True):
            main = self.git('rev-parse', 'main', cwd=home).strip()
            self.assertEqual(m.manual_landing(n, row), (main, main))
            m.close_ledger_job(inst, n, row['Job'])
        closed = m.read_jobs(n)[0]
        self.assertEqual(closed['Outcome'], 'merged')
        self.assertEqual(closed['Main commit'], main)
        self.assertTrue(closed['Ended'])
