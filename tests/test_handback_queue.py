"""Bounded file-only handback lifecycle checks (target: <=3 seconds per item)."""
import argparse
import contextlib
import datetime
import io
import json
import os
import shlex
import subprocess
import sys
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
        inst.now_dt = lambda: datetime.datetime(2026,10,24,4,0,tzinfo=datetime.timezone.utc)
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

    def test_p5_train_and_ordinary_readiness_refuse_replacement_attempts(self):
        m, inst, n, row = self.fixture()
        home = self.repo(self.tmp / 'home')
        wt = self.tmp / 'worktree'
        branch = row['Job']
        self.git('worktree', 'add', '-qb', branch, str(wt), cwd=home)
        n.update(external=True, home_path=home)
        n['fm']['verify'] = 'true'
        n['local']['worktrees'] = {branch: {'path': str(wt), 'port': 4100}}
        with mock.patch.dict(os.environ, self.env, clear=True):
            head, main = m.ready_refs(n, branch)
            packet = dict(version=1, node=n['id'], branch=branch, head=head, main=main,
                          integration_base=main, verify_command='true', verify_result=0,
                          inputs=m.verify_inputs(n, n['local']['worktrees'][branch]),
                          review_rounds=1, checked_at=inst.now_dt().isoformat(),
                          verify_port_context=m.verify_port_context(n, m.verify_env(n, branch,
                              n['local']['worktrees'][branch])))
            signal = dict(version=3, job=branch, started=row['Started'], state='done',
                          summary='Synthetic completed result', lessons='none: fixture',
                          attempt_id='a' * 32, ready=packet)
            path = m.handback_file(n, row)
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps(signal))
            for train in (False, True):
                ready = m.ready_state(inst, n, branch, train=train)
                self.assertTrue(ready['ready'], ready['reasons'])
            observed = []
            for state in ('queued', 'verifying', 'interrupted', 'invalidated', 'terminal', 'unknown'):
                attempt = m.begin_attempt(inst, n, row)
                if state == 'unknown':
                    m.attempt_path(n, row).write_text('{broken')
                elif state != 'queued':
                    m.update_attempt(inst, n, row, attempt['id'], state)
                for train in (False, True):
                    observed.append((state, train, m.ready_state(inst, n, branch, train=train)))
            self.assertEqual([(state, train) for state, train, ready in observed if ready['ready']], [])
            for state, train, ready in observed:
                with self.subTest(state=state, train=train):
                    self.assertIn('replacement handback attempt is ' + state,
                                  '; '.join(ready['reasons']))
            # Matching terminal publication restores readiness in both paths.
            attempt = m.begin_attempt(inst, n, row)
            m.finish_attempt(inst, n, row, attempt['id'])
            path.write_text(json.dumps({**signal, 'attempt_id': attempt['id']}))
            for train in (False, True):
                ready = m.ready_state(inst, n, branch, train=train)
                self.assertTrue(ready['ready'], ready['reasons'])

    def test_legacy_wakeless_handback_keeps_lifecycle_acknowledgement(self):
        m, inst, n, row = self.fixture()
        path = m.handback_file(n, row)
        path.parent.mkdir(exist_ok=True)
        signal = dict(version=2, job=row['Job'], started=row['Started'], state='blocked',
                      summary='Dependency unchanged', lessons='none: fixture')
        path.write_text(json.dumps(signal))
        state = dict(jobs=[row], worktrees={}, handbacks={m.job_key(row): signal}, inbox=[])
        with mock.patch.dict(os.environ, self.env, clear=True):
            current = next(i for i in m.due_items(inst, n, (state, '', ''),
                           include_stale=False, include_advice=False) if i['kind'] == 'handback')
            legacy = {k: v for k, v in current.items() if k not in ('id', 'handling')}
            legacy['episode'] = m.digest([m.job_key(row), {k: signal.get(k) for k in
                ('state', 'summary', 'question', 'report', 'lessons', 'restart')}])
            legacy = m.reminder_items(inst, n, [legacy])[0]
            m.record_reminder(inst, n, [legacy], legacy['id'], 'ack', 'Dependency handled',
                              resume_condition='Dependency changes')
            self.assertEqual(m.reminder_items(inst, n, [legacy]), [])
            unchanged = m.due_items(inst, n, (state, '', ''), include_stale=False, include_advice=False)
            self.assertFalse(any(i['kind'] == 'handback' for i in unchanged))
            self.assertEqual(m.job_projection(inst, n, row)['episode_id'], legacy['episode'])
            signal['summary'] = 'A different dependency'
            path.write_text(json.dumps(signal))
            changed = m.due_items(inst, n, (state, '', ''), include_stale=False, include_advice=False)
            item = next(i for i in changed if i['kind'] == 'handback')
            self.assertNotEqual(item['id'], legacy['id'])

    def test_failed_native_input_check_retains_diagnostics_without_stale_readiness(self):
        m, inst, n, row = self.fixture()
        initial = dict(head='a', base='b', inputs='before', integration_base='b', verify_command='true',
                       input_selection='original selection', port_reservation='4100/1')
        args = argparse.Namespace(id='root', branch=row['Job'], state='done', summary='Synthetic result',
                                  question=None, lessons='none: fixture', report=None, reviews=1)
        cases = [('negative inputs', {'inputs': 'after'}, None, True),
                 ('stale packet', {'inputs': 'after'}, {'version': 1, 'verify_result': 0}, False),
                 ('head', {'inputs': 'after', 'head': 'changed'}, None, False),
                 ('base', {'inputs': 'after', 'base': 'changed'}, None, False),
                 ('integration base', {'inputs': 'after', 'integration_base': 'changed'}, None, False),
                 ('command', {'inputs': 'after', 'verify_command': 'changed'}, None, False),
                 ('port', {'inputs': 'after', 'port_reservation': '4101/1'}, None, False),
                 ('selection', {'inputs': 'after', 'input_selection': 'changed'}, None, False)]
        path = m.handback_file(n, row)
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(m, 'load', return_value=inst), mock.patch.object(m.Instance, 'commit'), \
                mock.patch.object(m, 'attempt_binding') as binding, mock.patch.object(m, 'record_ready') as native, \
                mock.patch.object(m, 'ready_state', return_value={'reasons': ['native input check failed']}), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            for name, changed, packet, retained in cases:
                with self.subTest(case=name):
                    binding.return_value = initial
                    before = path.read_bytes() if path.exists() else None
                    def result(*args, **kwargs):
                        binding.return_value = {**initial, **changed}
                        return packet, ['native input check failed']
                    native.side_effect = result
                    with self.assertRaises(SystemExit):
                        m.cmd_handback(args)
                    if retained:
                        signal = json.loads(path.read_text())
                        self.assertIsNone(signal['ready'])
                        self.assertEqual(signal['ready_reasons'], ['native input check failed'])
                        self.assertEqual(m.current_attempt(n, row)['state'], 'terminal')
                    else:
                        self.assertEqual(path.read_bytes() if path.exists() else None, before)
                        self.assertEqual(m.current_attempt(n, row)['state'], 'interrupted')

    def native_input_failure_with_configuration_drift(self, change):
        from test_verify import VerifyInputDiagnostics
        case = VerifyInputDiagnostics()
        case.setUp()
        try:
            root, home, wt = case.inputs_job()
            source = wt / 'apps/web/.env.local'
            source.write_text('FIXTURE_VALUE=before\n')
            script = case.tmp / 'native-input-drift.py'
            code = ('import json\nfrom pathlib import Path\n'
                    + f'p = Path({str(source)!r})\np.write_text(p.read_text() + "changed\\n")\n')
            if change == 'port':
                local = root / '.brain/local.json'
                code += (f'p = Path({str(local)!r})\ndata = json.loads(p.read_text())\n'
                         'data["nodes"]["a"]["worktrees"]["job"]["port"] = 4101\n'
                         'p.write_text(json.dumps(data))\n')
            else:
                node = root / 'nodes/a/node.md'
                code += (f'p = Path({str(node)!r})\ntext = p.read_text()\n'
                         'assert text.count("apps/web/.env.*") == 1\n'
                         'p.write_text(text.replace("apps/web/.env.*", "apps/web/.env.other"))\n')
            script.write_text(code)
            case.b(root, 'set', 'a', 'verify=' + shlex.join([sys.executable, '-B', str(script)]))
            output = case.finish(root, ok=False)
            m = core_loader.SourceFileLoader('native_configuration_drift', str(CORE / 'bin/brain')).load_module()
            n = m.Instance(root).node('a')
            row = m.open_job(m.read_jobs(n), 'job')
            if change == 'port':
                self.assertEqual(n['local']['worktrees']['job']['port'], 4101)
            else:
                self.assertEqual(n['fm']['verify_inputs'], 'apps/web/.env.other')
            self.assertFalse(m.handback_file(n, row).exists())
            self.assertEqual(m.current_attempt(n, row)['state'], 'interrupted')
            self.assertIn('no stale handback published', output)
        finally:
            case.tearDown()

    def test_native_input_failure_refuses_port_drift(self):
        self.native_input_failure_with_configuration_drift('port')

    def test_native_input_failure_refuses_selection_drift(self):
        self.native_input_failure_with_configuration_drift('selection')

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

    def test_p2_rejects_stale_incomplete_ambiguous_and_protected_coverage(self):
        m, inst, n, row = self.fixture()
        row.update(Ended='2026-01-01T01:00Z', Outcome='closed')
        m.write_jobs(inst, n, [row])
        n['local']['workers'] = {m.job_key(row): 'worker-1'}
        inst.local['nodes']['root'] = n['local']
        base = dict(version=1, node='root', project_id='fixture', observed_at=inst.now_dt().isoformat(),
                    coverage=dict(project=True, nested=True, complete=True), jobs=[dict(job_key=m.job_key(row), threads=[
                        dict(threadId='worker-1', job_key=m.job_key(row), state='completed', settled=True, pending_requests=[])])])
        variants = []
        def variant():
            value = json.loads(json.dumps(base)); variants.append(value); return value
        variant()['observed_at'] = (inst.now_dt() - datetime.timedelta(seconds=301)).isoformat()
        variant()['coverage']['nested'] = False
        variant()['jobs'][0]['threads'] = []
        variant()['jobs'][0]['threads'][0]['job_key'] = 'other'
        variant()['jobs'][0]['threads'].append(base['jobs'][0]['threads'][0])
        variant()['jobs'][0]['threads'][0]['parentThreadId'] = 'uncovered-parent'
        variant()['jobs'].append(base['jobs'][0])
        before = (n['dir'] / m.JOBS_FILE).read_bytes()
        for value in variants:
            with self.subTest(value=value), self.assertRaises(ValueError):
                m.validate_settlement_attestation(inst, n, [row], value)
        n['local']['coordinator'] = 'worker-1'
        with self.assertRaises(ValueError):
            m.validate_settlement_attestation(inst, n, [row], base)
        n['local'].pop('coordinator')
        open_row = {**row, 'Job': 'job/open', 'Ended': '', 'Started': '2026-01-02T00:00Z'}
        n['local']['workers'][m.job_key(open_row)] = 'worker-1'
        m.write_jobs(inst, n, [row, open_row])
        before = (n['dir'] / m.JOBS_FILE).read_bytes()
        with self.assertRaises(ValueError):
            m.validate_settlement_attestation(inst, n, [row], base)
        self.assertEqual((n['dir'] / m.JOBS_FILE).read_bytes(), before)

    def test_p6_two_process_enqueues_and_no_pid_reuse_takeover(self):
        m, inst, n, row = self.fixture()
        script = """import argparse,sys
from pathlib import Path
from brain.instance import Instance
from brain.jobs import read_jobs
from brain.handback_queue import enqueue_handback
i=Instance(Path(sys.argv[1])); n=i.node('root'); r=read_jobs(n)[0]
print('barrier',flush=True); sys.stdin.readline()
a=argparse.Namespace(id='root',branch=r['Job'],state='blocked',summary='Synthetic result',question=None)
print(enqueue_handback(i,n,r,a)['id'],flush=True)
"""
        env = {**self.env, 'PYTHONPATH': str(CORE / 'lib')}
        children = [subprocess.Popen([sys.executable, '-c', script, str(inst.root)], env=env,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        try:
            self.assertEqual([p.stdout.readline().strip() for p in children], ['barrier', 'barrier'])
            for child in children:
                child.stdin.write('\n'); child.stdin.flush()
            ids = []
            for child in children:
                out, err = child.communicate(timeout=5)
                self.assertEqual(child.returncode, 0, err)
                ids.append(out.strip())
            self.assertEqual(len(set(ids)), 2)
            self.assertEqual(len(list((n['dir'] / 'handback-queue').glob('*.json'))), 2)
            with mock.patch.dict(os.environ, self.env, clear=True):
                with m.claim_handback(inst, n, ids[0]) as claimed:
                    lease = claimed['lease']
                # Released FD after a crashed runner is not authority to adopt it.
                data = m.read_queue_request(n, ids[0])
                data['owner'] = [os.getpid(), 'reused-birth-stamp']
                m.write_queue_request(n, data)
                with self.assertRaises(ValueError):
                    with m.claim_handback(inst, n, ids[0]):
                        self.fail('abandoned owner adopted')
                self.assertEqual(m.read_queue_request(n, ids[0])['lease'], lease)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill(); child.wait()

    def test_p5_stale_attempt_binding_and_closed_or_replaced_job_refuse(self):
        m, inst, n, row = self.fixture()
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(m, 'attempt_binding') as binding:
            binding.return_value = {'head':'a', 'base':'b', 'inputs':'c'}
            attempt = m.begin_attempt(inst, n, row)
            for changed in ({'head':'d','base':'b','inputs':'c'}, {'head':'a','base':'d','inputs':'c'},
                            {'head':'a','base':'b','inputs':'d'}):
                binding.return_value = changed
                self.assertFalse(m.attempt_matches(n, row, attempt))
            binding.return_value = attempt['binding']
            closed = {**row, 'Ended':'2026-01-01T01:00Z', 'Outcome':'closed'}
            m.write_jobs(inst, n, [closed])
            self.assertFalse(m.attempt_matches(n, row, attempt))
            m.write_jobs(inst, n, [closed, {**row, 'Started':'2026-01-02T00:00Z'}])
            self.assertFalse(m.attempt_matches(n, row, attempt))
            # Minute-precision legacy job keys can collide after immediate reuse.
            m.write_jobs(inst,n,[closed,{**row}])
            self.assertFalse(m.attempt_matches(n,row,attempt))
            with self.assertRaises(ValueError):
                m.begin_attempt(inst,n,row)
            m.attempt_path(n, row).write_text('{')
            with self.assertRaises(ValueError):
                m.active_attempt(n, row)
            self.assertEqual(m.job_projection(inst, n, row)['state'], 'unknown')

    def test_p6_foreground_runner_uses_native_reservation_and_publishes_once(self):
        m, inst, n, row = self.fixture()
        args = argparse.Namespace(id='root', branch=row['Job'], state='done', summary='Synthetic result',
                                  question=None, lessons='none: fixture', report=None, reviews=1)
        phases = []
        def admission(*args, **kwargs):
            phases.append(m.current_attempt(n, row)['state'])
            self.assertIsNone(m.job_signal(inst, n, row))
            return 'native-token'
        def native(*args, **kwargs):
            m.unlock()  # record_ready releases its outer CLI scope before native admission
            with m.verify_reservation(inst, n):
                phases.append(m.current_attempt(n, row)['state'])
                self.assertIsNone(m._LOCK['fh'], 'attempt phase re-held instance lock during verify')
            return {'version':1, 'integration_base':'b', 'verify_result':0}, []
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(m, 'attempt_binding', return_value={'head':'a','base':'b','inputs':'c'}), \
                mock.patch.object(m, 'load', return_value=inst), mock.patch.object(m.Instance, 'commit'), \
                mock.patch.object(m, 'machine_wait', side_effect=admission), mock.patch.object(m, 'machine_release') as release, \
                mock.patch.object(m, 'docker_leftover_nodes'), mock.patch.object(m, 'record_ready', side_effect=native), \
                mock.patch.object(m, 'ready_state', return_value={'reasons':[]}), contextlib.redirect_stdout(io.StringIO()):
            request = m.enqueue_handback(inst, n, row, args)
            with m.brain_lock(inst):
                m.run_queue_once(inst, n, request['id'])
            self.assertEqual(phases, ['queued', 'verifying'])
            release.assert_called_once_with('native-token')
            handback = m.handback_file(n, row).read_bytes()
            self.assertEqual(m.current_attempt(n, row)['state'], 'terminal')
            self.assertEqual(m.read_queue_request(n, request['id'])['state'], 'terminal')
            with self.assertRaises(ValueError):
                m.run_queue_once(inst, n, request['id'])
            self.assertEqual(m.handback_file(n, row).read_bytes(), handback)

    def test_p6_stale_foreground_request_does_not_publish(self):
        m, inst, n, row = self.fixture()
        args = argparse.Namespace(id='root', branch=row['Job'], state='done', summary='Synthetic result',
                                  question=None, lessons='none: fixture', report=None, reviews=1)
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(m, 'attempt_binding') as binding:
            binding.return_value={'head':'a','base':'b','inputs':'c'}
            request = m.enqueue_handback(inst, n, row, args)
            binding.return_value={'head':'d','base':'b','inputs':'c'}
            m.run_queue_once(inst, n, request['id'])
            self.assertFalse(m.handback_file(n, row).exists())
            self.assertEqual(m.read_queue_request(n, request['id'])['state'], 'invalidated')
            self.assertEqual(m.current_attempt(n, row)['state'], 'invalidated')

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
        with mock.patch.object(m, 'inbox_items', return_value=([], {})):
            self.assertIsNone(m.dispatch_plan(inst, limit=1))
        asks = [dict(id='episode-2', node='root', kind='inbox', text='Choose red'),
                dict(id='episode-1', node='root', kind='inbox', text='Choose blue'),
                dict(id='noise', node='root', kind='stale', text='Refresh status')]
        with mock.patch.object(m, 'inbox_items', return_value=(asks, {})):
            plan = m.dispatch_plan(inst, limit=1)
            self.assertTrue(plan['partial'])
            self.assertEqual(plan['items'][0]['episode_id'], 'episode-1')
            self.assertEqual(plan, m.dispatch_plan(inst, limit=1))
            self.assertEqual(plan['items'][0]['target_node'], 'root')

    def test_m3_synthetic_ask_scan_is_readonly_and_ignores_machine_advice(self):
        m, inst, n, row = self.fixture()
        (n['dir'] / 'inbox.md').write_text('- 2026-01-01 00:00 +00:00 from worker: question: Choose a fixture color?\n')
        before = {str(path.relative_to(inst.root)): path.read_bytes() for path in inst.root.rglob('*') if path.is_file()}
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(m, 'machine_episode_due', side_effect=AssertionError('machine scan')), \
                mock.patch.object(m, 'pending_imports', side_effect=AssertionError('import scan')), \
                mock.patch.object(m, 'runaway_jobs', side_effect=AssertionError('job advice scan')):
            plan = m.dispatch_plan(inst, limit=1)
            self.assertEqual(len(plan['items']), 1)
            self.assertEqual(plan['items'][0]['target_node'], 'root')
            self.assertEqual(plan, m.dispatch_plan(inst, limit=1))
        after = {str(path.relative_to(inst.root)): path.read_bytes() for path in inst.root.rglob('*') if path.is_file()}
        self.assertEqual(after, before)

    def test_m4_recorded_native_admission_is_readonly_and_never_live(self):
        m, inst, n, row = self.fixture()
        directory = self.tmp / 'machine'
        directory.mkdir()
        owner = [123, 'birth']
        record = dict(token='a'*32, owner=owner, kind='verify', state='queued', scope=m.machine_scope(inst,'root'))
        path = directory / 'machine-care-queue.json'
        path.write_text(json.dumps([record]))
        before = path.read_bytes()
        with mock.patch.object(m, 'machine_dir', return_value=directory), \
                mock.patch.object(m, 'process_stamp', side_effect=AssertionError('live process lookup')):
            view = m.job_queue_projection(inst, 'root', owner)
            self.assertEqual(view['position'], 1)
            self.assertEqual(view['owner_activity'], 'unknown')
            self.assertIsNone(m.job_queue_projection(inst, 'root', [123, 'reused']))
        self.assertEqual(path.read_bytes(), before)

    def test_m4_queued_rehandback_replaces_due_and_generated_active_work(self):
        m, inst, n, row = self.fixture()
        path = m.handback_file(n, row)
        path.parent.mkdir(exist_ok=True)
        hb = dict(job=row['Job'], started=row['Started'], state='done', summary='Previous result')
        path.write_text(json.dumps(hb))
        state = {'jobs':[row], 'worktrees':{}, 'handbacks':{m.job_key(row):hb}, 'inbox':[]}
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(m, 'attempt_binding', return_value={'head':'a','base':'b','inputs':'c'}), \
                mock.patch.object(m, 'rotation_due', return_value=None), mock.patch.object(m, 'missing_preview', return_value=False), \
                mock.patch.object(m, 'runnable_work', return_value=[]):
            m.begin_attempt(inst, n, row)
            items = m.due_items(inst,n,(state,'',''),include_stale=False,include_advice=False)
            self.assertFalse(any(i['kind'] in ('handback','invalid-handback') for i in items))
            queued = next(i for i in items if i['kind']=='handback-attempt')
            self.assertEqual(queued['state'],'queued')
            self.assertTrue(queued['informational'])
            with mock.patch.object(m, 'due_items', return_value=[]), mock.patch.object(m, 'attention_items', return_value=[]):
                lines, _ = m.status_facts(inst,n,state,[])
            text = '\n'.join(lines)
            self.assertIn('Active work (generated', text)
            self.assertIn('historical: Previous result', text)
            self.assertIn('| queued |',text)
            self.assertIn('host activity unknown',text)

    def test_m5_same_wake_changed_need_changes_suppression_identity(self):
        m, inst, n, row = self.fixture()
        path = m.handback_file(n, row)
        path.parent.mkdir(exist_ok=True)
        hb = dict(version=3, job=row['Job'], started=row['Started'], state='blocked', summary='Choose red',
                  lessons='none: fixture', wake_id='a'*32)
        state = {'jobs':[row], 'worktrees':{}, 'handbacks':{m.job_key(row):hb}, 'inbox':[]}
        with mock.patch.object(m, 'missing_preview', return_value=False), \
                mock.patch.object(m, 'rotation_due', return_value=False), mock.patch.object(m, 'changes_due', return_value=[]), \
                mock.patch.object(m, 'runnable_work', return_value=[]), mock.patch.object(m, 'blocked_entries', return_value=[]):
            path.write_text(json.dumps(hb))
            first = next(i for i in m.due_items(inst,n,(state,'',''),include_stale=False,include_advice=False) if i['kind']=='handback')
            hb.update(summary='Choose blue', verify_runs=99)
            path.write_text(json.dumps(hb))
            second = next(i for i in m.due_items(inst,n,(state,'',''),include_stale=False,include_advice=False) if i['kind']=='handback')
            self.assertEqual(first['episode'], second['episode'])
            self.assertNotEqual(first['need'], second['need'])
            hb.update(verify_runs=100)
            path.write_text(json.dumps(hb))
            third = next(i for i in m.due_items(inst,n,(state,'',''),include_stale=False,include_advice=False) if i['kind']=='handback')
            self.assertEqual(second['need'], third['need'])

    def test_m5_cli_refuses_missing_summary_and_question_before_an_attempt(self):
        m, inst, n, row = self.fixture()
        base = [sys.executable,str(CORE / 'bin/brain'),'handback','root','--branch',row['Job'],'--state','question']
        for extra in ([],['--summary','Synthetic result'],['--summary','bad\nsummary','--question','Choose?']):
            result = subprocess.run(base+extra,cwd=inst.root,env=self.env,capture_output=True,text=True,timeout=3)
            self.assertNotEqual(result.returncode,0)
            self.assertFalse(m.attempt_path(n,row).exists())
            self.assertFalse(m.handback_file(n,row).exists())

    def test_p6_cli_enqueue_foreground_terminal_and_live_join(self):
        m, inst, n, row = self.fixture()
        path = m.handback_file(n,row)
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(dict(job=row['Job'],started=row['Started'],state='done',summary='Historical result')))
        def cli(*args,ok=True):
            result = subprocess.run([sys.executable,str(CORE / 'bin/brain'),*args],cwd=inst.root,
                                    env=self.env,capture_output=True,text=True,timeout=3)
            self.assertEqual(result.returncode==0,ok,result.stdout+result.stderr)
            return result.stdout
        identity=cli('handback','root','--branch',row['Job'],'--state','blocked','--summary','New result',
                     '--lessons','none: fixture','--enqueue').strip()
        self.assertRegex(identity,r'^[0-9a-f]{32}$')
        view=json.loads(cli('jobs','root','--live'))
        self.assertEqual(view['state'],'queued')
        self.assertEqual(view['historical_summary'],'Historical result')
        self.assertEqual(view['queue']['position'],1)
        cli('queue','run','root','--once','--request-id',identity)
        hb=path.read_bytes()
        self.assertEqual(json.loads(hb)['summary'],'New result')
        self.assertEqual(m.read_queue_request(n,identity)['state'],'terminal')
        cli('queue','run','root','--once','--request-id',identity,ok=False)
        self.assertEqual(path.read_bytes(),hb)

    def test_m3_cli_empty_scan_silent_then_one_synthetic_ask(self):
        m, inst, n, row = self.fixture()
        def dispatch():
            result=subprocess.run([sys.executable,str(CORE / 'bin/brain'),'dispatch','--limit','1'],
                                  cwd=inst.root,env=self.env,capture_output=True,text=True,timeout=3)
            self.assertEqual(result.returncode,0,result.stderr)
            return result.stdout
        self.assertEqual(dispatch(),'')
        (n['dir']/'inbox.md').write_text('- 2026-01-01 00:00 +00:00 from worker: question: Choose a color?\n')
        first=dispatch()
        self.assertEqual(json.loads(first)['items'][0]['target_node'],'root')
        self.assertEqual(dispatch(),first)

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
        parked = {**row, 'Job':'job/parked', 'Started':'2026-01-02T00:00Z'}
        n['home_path'] = None
        m.write_jobs(inst,n,[closed,parked])
        before = (n['dir'] / m.JOBS_FILE).read_bytes()
        with self.assertRaises(SystemExit):
            m.close_ledger_job(inst,n,parked['Job'])
        self.assertEqual((n['dir'] / m.JOBS_FILE).read_bytes(),before)
        m.close_ledger_job(inst,n,parked['Job'],reason='Coordinator parked this synthetic job')
        final = m.read_jobs(n)[1]
        self.assertEqual(final['Outcome'],'closed')
        self.assertEqual(final['Cause'],'Coordinator parked this synthetic job')
        self.assertTrue(home.is_dir())
