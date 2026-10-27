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
    def test_many_scoped_refs_only_query_git_for_loose_symbolic_targets(self):
        m, inst, n, row = self.fixture()
        home = self.repo(self.tmp / 'home')
        self.git('branch', 'stable', cwd=home)
        self.git('symbolic-ref', 'refs/heads/main', 'refs/heads/middle', cwd=home)
        self.git('symbolic-ref', 'refs/heads/middle', 'refs/heads/stable', cwd=home)
        refs = {'refs/heads/main'} | {f'refs/heads/missing-{i}' for i in range(100)}
        calls = []
        git = m.git
        def record(*args, **kwargs):
            calls.append(args)
            return git(*args, **kwargs)
        with mock.patch.object(m, 'git', side_effect=record):
            paths = m.git_state_paths(home, refs=refs, include_head=False)
        self.assertEqual(sum(c[:1] == ('symbolic-ref',) for c in calls), 2)
        self.assertIn(home / '.git/refs/heads/middle', paths)
        self.assertIn(home / '.git/refs/heads/stable', paths)
        before = m.state_signature(inst, extra=paths)
        self.git('commit', '--allow-empty', '-qm', 'Selected target changed', cwd=home)
        self.assertNotEqual(before, m.state_signature(inst, extra=paths))
    def test_write_intent_identity_order_and_corruption_are_fail_closed(self):
        m, inst, n, row = self.fixture()
        payload = dict(kind='inbox', node='root', text='same text', at=inst.now())
        first = m.submit_intent(inst, payload, start=False)
        m.submit_intent(inst, payload, identity=first['id'], start=False)
        second = m.submit_intent(inst, payload, start=False)
        indexed = m.intent_index(inst)
        self.assertEqual([data['id'] for _, data in indexed], [first['id'], second['id']])
        self.assertLess(indexed[0][0], indexed[1][0])
        with self.assertRaisesRegex(ValueError, 'different payload'):
            m.submit_intent(inst, {**payload, 'text': 'changed'}, identity=first['id'], start=False)
        with open(m.intent_path(inst, 'index'), 'ab') as stream:
            stream.write(b'{"id":')
        with self.assertRaisesRegex(ValueError, 'blocked'):
            m.intent_index(inst)
        self.assertIsNone(m.intent_receipt(inst, first['id']))

    def test_write_intents_apply_in_order_and_inbox_dedupes_by_identity(self):
        m, inst, n, row = self.fixture()
        payload = dict(kind='inbox', node='root', text='same text', at=inst.now())
        first = m.submit_intent(inst, payload, start=False)
        second = m.submit_intent(inst, payload, start=False)
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(m.Instance, 'commit'):
            m.drain_intents(inst)
            m.submit_intent(inst, payload, identity=first['id'], start=False)
            m.drain_intents(inst)
        text = (n['dir'] / 'inbox.md').read_text()
        self.assertEqual(text.count('same text'), 2)
        self.assertEqual(text.count(first['id']), 1)
        self.assertEqual(text.count(second['id']), 1)
        self.assertLess(m.intent_receipt(inst, first['id'])['sequence'], m.intent_receipt(inst, second['id'])['sequence'])
        self.assertEqual(m.intent_receipt(inst, second['id'])['state'], 'applied')

    def test_write_task_replay_compares_before_after_and_refuses_drift(self):
        m, inst, n, row = self.fixture()
        m.mutate(inst, n, 'TASK1', {'title': 'Before', 'owner': 'fixture'}, create=True)
        request = m.submit_intent(inst, dict(kind='task', node='root', task='TASK1', changes={'title': 'After'}), start=False)
        save = m.save_record
        def killed(*args, **kwargs):
            save(*args, **kwargs)
            raise RuntimeError('owned kill after primary write')
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(m.Instance, 'commit'):
            with mock.patch.object(m, 'save_record', side_effect=killed), self.assertRaises(RuntimeError):
                m.drain_intents(inst)
            self.assertIsNone(m.intent_receipt(inst, request['id']))
            self.assertEqual(m.read_records(n)['TASK1']['title'], 'After')
            m.drain_intents(inst)
            self.assertEqual(m.intent_receipt(inst, request['id'])['state'], 'applied')
            later = m.submit_intent(inst, dict(kind='task', node='root', task='TASK1', changes={'title': 'Later'}), start=False)
            before = m.read_records(n)['TASK1']
            m.intent_store(inst, later['id'] + '.prepared', dict(hash=later['hash'], before=before,
                           after={**before, 'title': 'Later'}), exclusive=True)
            save(n, {**before, 'title': 'Independent synchronous edit'})
            with self.assertRaisesRegex(RuntimeError, 'drift'):
                m.drain_intents(inst)
            self.assertIsNone(m.intent_receipt(inst, later['id']))
            self.assertEqual(m.read_records(n)['TASK1']['title'], 'Independent synchronous edit')

    def test_write_fast_route_never_acquires_instance_lock_and_keeps_gates_sync(self):
        m, inst, n, row = self.fixture()
        args = argparse.Namespace(f=m.cmd_task, action='set', node='root', task_id='TASK1', fields=['priority=P1'],
                                  title=None, status=None, owner=None, json=False, request_id=None)
        with mock.patch.object(m, 'brain_lock', side_effect=AssertionError('producer acquired instance lock')), \
                mock.patch.object(m, 'start_intent_drainer'), contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(m.fast_intent(args, inst.root))
            for fields in (['status=ready'], ['owner=someone'], ['depends_on=TASK2'], ['hold=reason'],
                           ['authority=operator'], ['contract=path'], ['priority=P1', 'status=ready']):
                args.fields = fields
                self.assertFalse(m.fast_intent(args, inst.root), fields)

    def test_write_applier_lease_is_exclusive_and_submission_starts_detached(self):
        m, inst, n, row = self.fixture()
        fd = m.intent_lease(inst)
        try:
            self.assertIsNone(m.intent_lease(inst))
            with mock.patch.object(subprocess, 'Popen') as launch:
                m.start_intent_drainer(inst)
                launch.assert_not_called()
        finally:
            os.close(fd)
        with mock.patch.object(subprocess, 'Popen') as launch:
            m.start_intent_drainer(inst)
            self.assertTrue(launch.call_args.kwargs['start_new_session'])
            self.assertEqual(len(launch.call_args.kwargs['pass_fds']), 1)

    def test_attempt_binding_ignores_other_branch_commits_but_fences_own_branch(self):
        m, inst, n, row = self.fixture()
        home = self.repo(self.tmp / 'home')
        wt = self.tmp / 'worktree'
        branch = row['Job']
        self.git('worktree', 'add', '-qb', branch, str(wt), cwd=home)
        self.git('checkout', '-qb', 'job/other', cwd=home)
        n.update(external=True, home_path=home)
        n['local']['worktrees'] = {branch: {'path': str(wt), 'port': 4100}}
        binding = m.attempt_binding
        def changed(n, row):
            value = binding(n, row)
            self.git('commit', '--allow-empty', '-qm', 'Other worker commit', cwd=home)
            return value
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(m, 'attempt_binding', side_effect=changed):
            attempt = m.begin_attempt(inst, n, row)
        self.assertEqual(attempt['binding'], binding(n, row))
        def own_branch_changed(n, row):
            value = changed(n, row)
            head = self.git('rev-parse', 'HEAD', cwd=home).strip()
            self.git('update-ref', 'refs/heads/' + branch, head, cwd=home)
            return value
        before = m.attempt_path(n, row).read_bytes()
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(m, 'attempt_binding', side_effect=own_branch_changed), \
                self.assertRaises(SystemExit):
            m.begin_attempt(inst, n, row)
        self.assertEqual(m.attempt_path(n, row).read_bytes(), before)

    def fixture(self):
        root = self.tmp / 'instance'
        root.mkdir()
        (root / 'instance.md').write_text('---\ntimezone: UTC\n---\n')
        (root / 'node.md').write_text('---\ntitle: Root\nhome: none\n---\n')
        m = core_loader.SourceFileLoader('queue_fixture', str(CORE / 'bin/brain')).load_module()
        host = mock.patch.object(m, 'lifecycle_host_identity', return_value='fixture-boot-a', create=True)
        host.start()
        self.addCleanup(host.stop)
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
            legacy['episode'] = m._owners['job_key'].digest([m.job_key(row), {k: signal.get(k) for k in
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
            # Queue validation uses the same bound import-study row as direct handback.
            study = {**row, 'Job': 'import:' + 'a' * 40}
            m.write_jobs(inst, n, [row, study])
            study_args = argparse.Namespace(**{**vars(args), 'branch': study['Job'],
                                              'state': 'done', 'reviews': 0})
            queued = m.enqueue_handback(inst, n, study, study_args)
            self.assertEqual(queued['job_key'], m.job_key(study))
            self.assertEqual(queued['args']['reviews'], 0)
            self.assertEqual(queued['state'], 'queued')
            with m.claim_handback(inst, n, queued['id']) as claimed:
                self.assertTrue(m.queue_terminal(inst, n, queued['id'], claimed['lease'], 'terminal'))
            before = {p.name: p.read_bytes() for p in (n['dir'] / 'handback-queue').glob('*.json')}
            for invalid in (argparse.Namespace(**{**vars(args), 'state': 'done', 'reviews': 0}),
                            argparse.Namespace(**{**vars(study_args), 'reviews': -1}),
                            argparse.Namespace(**{**vars(study_args), 'reverify': True}),
                            argparse.Namespace(**{**vars(study_args), 'full_receipt': 'missing.json'})):
                with self.assertRaises(SystemExit):
                    m.enqueue_handback(inst, n, study if invalid.branch == study['Job'] else row, invalid)
                self.assertEqual(before, {p.name: p.read_bytes()
                                          for p in (n['dir'] / 'handback-queue').glob('*.json')})

    def publication_case(self, fault=None, invalidation=False):
        """Dispatch for real, then revoke authority at the final writer boundary."""
        m, inst, n, row = self.fixture()
        args = argparse.Namespace(id='root', branch=row['Job'], state='blocked', summary='Fixture result',
                                  question=None, lessons='none: fixture', report=None, reviews=None)
        binding = {'head': 'a', 'base': 'b', 'inputs': 'c'}
        captured = {}
        original_publish, original_dispatch = m.publish_handback, m.queue_dispatch

        def revoke(request):
            data = m.read_queue_request(n, request['id'])
            self.assertEqual(data['run']['phase'], 'dispatched')
            lease = m.queue_path(n, request['id']).with_suffix('.lease')
            if fault == 'failure':
                m.attempt_phase('verifying')
            if fault in ('inode', 'unlink', 'failure'):
                lease.unlink()
                if fault != 'unlink':
                    lease.write_text('foreign lease: preserve me\n')
            elif fault == 'copied':
                m.QUEUE_LEASE.set(None)  # claim_handback resets its own outer token
            elif fault in ('process', 'host', 'run', 'request'):
                if fault == 'process':
                    data['owner'] = [os.getpid() + 1, 'foreign-process']
                elif fault == 'host':
                    data['run']['host'] = 'foreign-boot'
                elif fault == 'run':
                    data['run']['id'] = 'f' * 32
                else:
                    data['attempt_id'] = 'f' * 32
                m.write_queue_request(n, data)
            captured['queue'] = m.queue_path(n, request['id']).read_bytes()
            captured['attempt'] = m.attempt_path(n, row).read_bytes()
            captured['lease'] = lease.read_bytes() if lease.exists() else None

        def publish(a):
            revoke(a._queue_request)
            if fault == 'failure':
                raise ValueError('fixture verifier failure after capability loss')
            return original_publish(a)

        def dispatch(*values):
            original_dispatch(*values)
            revoke(values[2])

        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(m, 'load', return_value=inst), mock.patch.object(m.Instance, 'commit'), \
                mock.patch.object(m, 'attempt_binding', return_value=binding) as current_binding, \
                mock.patch.object(m, 'request_wake') as wake, \
                mock.patch.object(m, 'write_record', wraps=m.write_record) as writes, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            request = m.enqueue_handback(inst, n, row, args)
            if invalidation:
                current_binding.return_value = {**binding, 'inputs': 'drift'}
            with mock.patch.object(m, 'queue_dispatch', side_effect=dispatch if invalidation else original_dispatch), \
                    mock.patch.object(m, 'publish_handback', side_effect=original_publish if invalidation else publish), \
                    contextlib.ExitStack() as stack:
                if fault == 'false':
                    stack.enter_context(mock.patch.object(m, 'queue_terminal', return_value=False))
                try:
                    m.run_queue_once(inst, n, request['id'])
                except (SystemExit, ValueError, OSError):
                    if fault is None:
                        raise
            queue = m.read_queue_request(n, request['id'])
            attempt = m.current_attempt(n, row)
            handback = m.handback_file(n, row)
            if fault is not None:
                self.assertFalse(handback.exists(), 'refused capability published a terminal handback')
                self.assertFalse(any(call.args[2] == 'handback' for call in writes.call_args_list),
                                 'refused capability wrote terminal handback evidence')
                self.assertEqual(m.attempt_path(n, row).read_bytes(), captured['attempt'],
                                 'refused capability mutated the retained attempt')
                self.assertEqual(m.queue_path(n, request['id']).read_bytes(), captured['queue'])
                lease = m.queue_path(n, request['id']).with_suffix('.lease')
                self.assertEqual(lease.read_bytes() if lease.exists() else None, captured['lease'])
                self.assertEqual(queue['state'], 'running')
                self.assertIn(attempt['state'], ('queued', 'verifying'))
                wake.assert_not_called()
                self.assertFalse((n['dir'] / 'inbox.md').exists())
            else:
                self.assertEqual(queue['state'], 'invalidated' if invalidation else 'terminal')
                self.assertEqual(attempt['state'], queue['state'])
                if invalidation:
                    self.assertFalse(handback.exists())
                    wake.assert_not_called()
                else:
                    self.assertEqual(m.read_handback(n, row)['attempt_id'], request['attempt_id'])
                    wake.assert_called_once()

    def test_publication_inode_loss(self):
        self.publication_case('inode')

    def test_publication_unlinked_lease(self):
        self.publication_case('unlink')

    def test_publication_copied_capability(self):
        self.publication_case('copied')

    def test_publication_foreign_process(self):
        self.publication_case('process')

    def test_publication_foreign_host(self):
        self.publication_case('host')

    def test_publication_changed_run(self):
        self.publication_case('run')

    def test_publication_changed_request(self):
        self.publication_case('request')

    def test_publication_terminal_false(self):
        self.publication_case('false')

    def test_publication_failure_retains_running_attempt(self):
        self.publication_case('failure')

    def test_publication_owned_control(self):
        self.publication_case()

    def test_invalidation_inode_loss(self):
        self.publication_case('inode', invalidation=True)

    def test_invalidation_copied_capability(self):
        self.publication_case('copied', invalidation=True)

    def test_invalidation_foreign_host(self):
        self.publication_case('host', invalidation=True)

    def test_invalidation_changed_run(self):
        self.publication_case('run', invalidation=True)

    def test_invalidation_terminal_false(self):
        self.publication_case('false', invalidation=True)

    def test_invalidation_owned_control(self):
        self.publication_case(invalidation=True)

    def test_crash_recovery_is_explicit_and_fenced_before_dispatch(self):
        m, inst, n, row = self.fixture()
        args = argparse.Namespace(id='root', branch=row['Job'], state='blocked', summary='Fixture result',
                                  question=None, lessons='none: fixture', report=None, reviews=None)
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(m, 'attempt_binding', return_value={'head':'a', 'base':'b', 'inputs':'c'}):
            request = m.enqueue_handback(inst, n, row, args)
            with m.claim_handback(inst, n, request['id']) as first:
                old = first['lease']
            data = m.read_queue_request(n, request['id'])
            data['owner'] = [12345, 'old-run']
            m.write_queue_request(n, data)
            for stamp in ('old-run', 'unknown'):
                with mock.patch.object(m, 'process_stamp', return_value=stamp), self.assertRaises(ValueError):
                    with m.claim_handback(inst, n, request['id'], recover=True):
                        self.fail('live or unknown runner adopted')
            with mock.patch.object(m, 'process_stamp', side_effect=lambda pid: 'new-run' if pid == os.getpid() else None):
                with m.claim_handback(inst, n, request['id'], recover=True) as second:
                    self.assertNotEqual(second['lease'], old)
                    self.assertEqual(second['recovered'][0]['lease'], old)
                    self.assertFalse(m.queue_terminal(inst, n, request['id'], old, 'terminal'))

    def test_dispatched_crash_and_legacy_owner_are_never_adopted(self):
        m, inst, n, row = self.fixture()
        args = argparse.Namespace(id='root', branch=row['Job'], state='blocked', summary='Fixture result')
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(m, 'attempt_binding', return_value={'head':'a', 'base':'b', 'inputs':'c'}):
            request = m.enqueue_handback(inst, n, row, args)
            with m.claim_handback(inst, n, request['id']) as claimed:
                m.queue_dispatch(inst, n, claimed)
                self.assertIsInstance(m.queue_descendant_fd(), int)
            data = m.read_queue_request(n, request['id'])
            data['owner'] = [12345, 'dead-run']
            m.write_queue_request(n, data)
            with mock.patch.object(m, 'process_stamp', return_value=None):
                with self.assertRaisesRegex(ValueError, 'descendant'):
                    with m.claim_handback(inst, n, request['id'], recover=True):
                        self.fail('dispatched run adopted')
                data.pop('run', None)
                m.write_queue_request(n, data)
                with self.assertRaises(ValueError):
                    with m.claim_handback(inst, n, request['id'], recover=True):
                        self.fail('legacy ownership adopted')

    def test_copied_lease_cannot_dispatch_or_stamp_without_owned_descriptor(self):
        m, inst, n, row = self.fixture()
        args = argparse.Namespace(id='root', branch=row['Job'], state='blocked', summary='Fixture result')
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(
                m, 'attempt_binding', return_value={'head':'a', 'base':'b', 'inputs':'c'}):
            request = m.enqueue_handback(inst, n, row, args)
            with m.claim_handback(inst, n, request['id']) as claimed:
                copied = dict(claimed)
            before = m.queue_path(n, request['id']).read_bytes()
            with self.assertRaisesRegex(ValueError, 'caller-owned'):
                m.queue_dispatch(inst, n, copied)
            self.assertFalse(m.queue_terminal(inst, n, request['id'], copied['lease'], 'terminal'))
            self.assertEqual(m.queue_path(n, request['id']).read_bytes(), before)

    def test_replaced_lease_inode_cannot_launch_or_publish(self):
        m, inst, n, row = self.fixture()
        args = argparse.Namespace(id='root', branch=row['Job'], state='blocked', summary='Fixture result')
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(
                m, 'attempt_binding', return_value={'head':'a', 'base':'b', 'inputs':'c'}):
            request = m.enqueue_handback(inst, n, row, args)
            with m.claim_handback(inst, n, request['id']) as claimed:
                lease = m.queue_path(n, request['id']).with_suffix('.lease')
                lease.unlink()
                lease.touch()
                with self.assertRaisesRegex(ValueError, 'replaced|unlinked'):
                    m.queue_descendant_fd()
                with self.assertRaisesRegex(ValueError, 'replaced|unlinked'):
                    m.queue_dispatch(inst, n, claimed)
                self.assertFalse(m.queue_terminal(inst, n, request['id'], claimed['lease'], 'terminal'))

    def test_foreign_boot_never_probes_local_pid_or_recovers_lease(self):
        m, inst, n, row = self.fixture()
        args = argparse.Namespace(id='root', branch=row['Job'], state='blocked', summary='Fixture result')
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(
                m, 'attempt_binding', return_value={'head':'a', 'base':'b', 'inputs':'c'}):
            request = m.enqueue_handback(inst, n, row, args)
            with m.claim_handback(inst, n, request['id']):
                pass
            data = m.read_queue_request(n, request['id'])
            data['owner'] = [12345, 'foreign-process']
            data['run']['host'] = 'fixture-boot-b'
            m.write_queue_request(n, data)
            before = m.queue_path(n, request['id']).read_bytes()
            with mock.patch.object(m, 'process_stamp', return_value=None) as probe:
                with self.assertRaisesRegex(ValueError, 'host'):
                    with m.claim_handback(inst, n, request['id'], recover=True):
                        self.fail('foreign runner adopted')
                probe.assert_not_called()
            self.assertEqual(m.queue_path(n, request['id']).read_bytes(), before)

    def test_unknown_host_cannot_claim_or_publish_a_queue_run(self):
        m, inst, n, row = self.fixture()
        args = argparse.Namespace(id='root', branch=row['Job'], state='blocked', summary='Fixture result')
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(
                m, 'attempt_binding', return_value={'head':'a', 'base':'b', 'inputs':'c'}):
            request = m.enqueue_handback(inst, n, row, args)
            with mock.patch.object(m, 'lifecycle_host_identity', return_value=None):
                with self.assertRaisesRegex(ValueError, 'host'):
                    with m.claim_handback(inst, n, request['id']):
                        self.fail('unknown host claimed')
            with m.claim_handback(inst, n, request['id']) as claimed:
                with mock.patch.object(m, 'lifecycle_host_identity', return_value='fixture-boot-b'):
                    self.assertFalse(m.queue_terminal(inst, n, request['id'], claimed['lease'], 'terminal'))
                    with self.assertRaises(ValueError):
                        m.queue_dispatch(inst, n, claimed)
                    with self.assertRaises(ValueError):
                        m.queue_descendant_fd()

    def test_exact_terminal_publication_reconciles_without_second_writer(self):
        m, inst, n, row = self.fixture()
        args = argparse.Namespace(id='root', branch=row['Job'], state='blocked', summary='Fixture result')
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(m, 'attempt_binding', return_value={'head':'a', 'base':'b', 'inputs':'c'}):
            request = m.enqueue_handback(inst, n, row, args)
            with m.claim_handback(inst, n, request['id']) as claimed:
                m.queue_dispatch(inst, n, claimed)
            data = m.read_queue_request(n, request['id'])
            data['owner'] = [12345, 'dead-run']
            m.write_queue_request(n, data)
            path = m.handback_file(n, row)
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps(dict(version=3, job=row['Job'], started=row['Started'], state='blocked',
                summary='Fixture result', lessons='none: fixture', ready=None, ready_reasons=[],
                attempt_id=request['attempt_id'], wake_id='b'*32,
                queue_publication=dict(request=request['id'], lease=claimed['lease']))))
            before = path.read_bytes()
            with mock.patch.object(m, 'process_stamp', return_value=None), mock.patch.object(m, 'cmd_handback') as writer:
                self.assertEqual(m.run_queue_once(inst, n, request['id'], recover=True), request['id'])
                writer.assert_not_called()
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(m.read_queue_request(n, request['id'])['state'], 'terminal')
            self.assertEqual(m.current_attempt(n, row)['state'], 'terminal')

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

    def test_p6_overlapping_binding_reads_allow_both_process_enqueues(self):
        m, inst, n, row = self.fixture()
        script = """import argparse,sys
from pathlib import Path
from brain.instance import Instance
from brain.jobs import read_jobs
from brain import job_attempts
from brain.handback_queue import enqueue_handback
i=Instance(Path(sys.argv[1])); n=i.node('root'); r=read_jobs(n)[0]
original=job_attempts.attempt_binding
def binding(n,r):
    value=original(n,r)
    print('binding',flush=True); sys.stdin.readline()
    return value
job_attempts.attempt_binding=binding
a=argparse.Namespace(id='root',branch=r['Job'],state='blocked',summary='Synthetic result',question=None)
print(enqueue_handback(i,n,r,a)['id'],flush=True)
"""
        env = {**self.env, 'PYTHONPATH': str(CORE / 'lib')}
        children = [subprocess.Popen([sys.executable, '-B', '-c', script, str(inst.root)], env=env,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        try:
            # Both signatures precede either publication. Finish the first
            # enqueue before the second process retakes the writer lock.
            self.assertEqual([p.stdout.readline().strip() for p in children], ['binding', 'binding'])
            ids = []
            for child in children:
                out, err = child.communicate(input='\n', timeout=5)
                self.assertEqual(child.returncode, 0, err)
                ids.append(out.strip())
            self.assertEqual(len(set(ids)), 2)
            requests = [m.read_queue_request(n, identity) for identity in ids]
            self.assertEqual([request['state'] for request in requests], ['queued', 'queued'])
            self.assertEqual(len(list((n['dir'] / 'handback-queue').glob('*.json'))), 2)
            self.assertEqual(m.current_attempt(n, row)['request_id'], ids[1])
            self.assertEqual(m.current_attempt(n, row)['id'], requests[1]['attempt_id'])
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill(); child.wait()

    def test_p6_creation_still_refuses_changed_binding_sources(self):
        m, inst, n, row = self.fixture()
        paths = (n['dir'] / m.JOBS_FILE, n['dir'] / 'node.md', inst.local_path)
        before = {path: path.read_bytes() if path.exists() else None for path in paths}
        for source in ('ledger', 'policy', 'local'):
            with self.subTest(source=source):
                original = m.attempt_binding
                def changed(n, row):
                    value = original(n, row)
                    if source == 'ledger':
                        m.write_jobs(inst, n, [{**row, 'Ended': '2026-01-01T01:00Z', 'Outcome': 'closed'}])
                    elif source == 'policy':
                        (n['dir'] / 'node.md').write_text('---\ntitle: Changed\nhome: none\n---\n')
                    else:
                        inst.local.setdefault('nodes', {}).setdefault(n['id'], {})['preview'] = 'changed'
                        inst.save_local()
                    return value
                args = argparse.Namespace(id='root', branch=row['Job'], state='blocked',
                                          summary='Synthetic result', question=None)
                with mock.patch.dict(os.environ, self.env, clear=True), \
                        mock.patch.object(m, 'attempt_binding', side_effect=changed):
                    with self.assertRaisesRegex(SystemExit, 'state changed during the unlocked step'):
                        m.enqueue_handback(inst, n, row, args)
                self.assertIsNone(m.current_attempt(n, row))
                self.assertFalse((n['dir'] / 'handback-queue').exists())
                for path, data in before.items():
                    if data is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.write_bytes(data)
                inst.local = inst._load_local()

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

    @contextlib.contextmanager
    def owned_takeover_fixture(self):
        m, inst, n, row = self.fixture()
        args = argparse.Namespace(id='root', branch=row['Job'], state='blocked', summary='Fixture result')
        stamps = {os.getpid(): 'controller', 12345: 'owner', 12346: 'child'}
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(
                m, 'attempt_binding', return_value={'head':'a', 'base':'b', 'inputs':'c'}), \
                mock.patch.object(m, 'process_stamp', side_effect=lambda pid: stamps.get(pid)), \
                mock.patch.object(m, 'owned_process_parent', return_value=os.getpid()):
            request = m.enqueue_handback(inst, n, row, args)
            with m.claim_handback(inst, n, request['id']) as claimed:
                m.queue_dispatch(inst, n, claimed)
                m.queue_child(12346)
            data = m.read_queue_request(n, request['id'])
            data['owner'] = [12345, 'owner']
            data['run']['owner'] = data['owner']
            m.write_queue_request(n, data)
            stamps[12345] = None
            yield m, inst, n, row, request['id'], stamps

    def takeover_refusal(self, change, reason):
        with self.owned_takeover_fixture() as (m, inst, n, row, identity, stamps):
            data = m.read_queue_request(n, identity)
            change(data, stamps, inst)
            m.write_queue_request(n, data)
            before = m.queue_path(n, identity).read_bytes(), m.attempt_path(n, row).read_bytes()
            with mock.patch('os.kill') as signal:
                with self.assertRaisesRegex(ValueError, reason):
                    m.run_queue_once(inst, n, identity, recover=True)
                signal.assert_not_called()
            self.assertEqual(before, (m.queue_path(n, identity).read_bytes(), m.attempt_path(n, row).read_bytes()))
            audit = json.loads(m.queue_path(n, identity).with_suffix('.takeover.json').read_text())
            self.assertEqual(audit['state'], 'refused')
            self.assertTrue(audit['reason'])
            self.assertFalse(m.handback_file(n, row).exists())

    def test_owned_takeover_foreign_host(self):
        self.takeover_refusal(lambda d, s, i: d['run'].update(host='foreign'), 'host')

    def test_owned_takeover_reused_child_pid(self):
        self.takeover_refusal(lambda d, s, i: s.update({12346:'another-start'}), 'process')

    def test_owned_takeover_reused_owner_pid(self):
        self.takeover_refusal(lambda d, s, i: s.update({12345:'another-start'}), 'owner')

    def test_owned_takeover_unknown_child(self):
        self.takeover_refusal(lambda d, s, i: s.update({12346:'unknown'}), 'process')

    def test_owned_takeover_stale_lease(self):
        self.takeover_refusal(lambda d, s, i: d.update(lease='e'*32), 'lease')

    def test_owned_takeover_expired_lease(self):
        self.takeover_refusal(lambda d, s, i: d['run'].update(takeover_until=0), 'expired')

    def test_owned_takeover_live_owner(self):
        self.takeover_refusal(lambda d, s, i: s.update({12345:'owner'}), 'owner')

    def test_owned_takeover_crashed_run_is_settled_without_replay(self):
        with self.owned_takeover_fixture() as (m, inst, n, row, identity, stamps):
            stamps[12346] = None
            with mock.patch('os.kill') as signal, mock.patch.object(m, 'cmd_handback') as publish:
                self.assertEqual(m.run_queue_once(inst, n, identity, recover=True), identity)
                signal.assert_not_called()
                publish.assert_not_called()
            self.assertEqual(m.read_queue_request(n, identity)['state'], 'interrupted')
            self.assertEqual(m.current_attempt(n, row)['state'], 'interrupted')
            self.assertFalse(m.handback_file(n, row).exists())
            audit = json.loads(m.queue_path(n, identity).with_suffix('.takeover.json').read_text())
            self.assertEqual(audit['state'], 'settled')

    def test_owned_takeover_partial_run_intent_precedes_stop_and_concurrent_refuses(self):
        """A live partial run is retained; an intent still serializes contenders."""
        with self.owned_takeover_fixture() as (m, inst, n, row, identity, stamps):
            before = m.queue_path(n, identity).read_bytes(), m.attempt_path(n, row).read_bytes()
            original = m.write_takeover
            def intent(n, identity, audit):
                original(n, identity, audit)
                if audit['state'] == 'intent':
                    with self.assertRaisesRegex(ValueError, 'takeover.*owner|already'):
                        m.run_queue_once(inst, n, identity, recover=True)
            with mock.patch.object(m, 'write_takeover', side_effect=intent), \
                    mock.patch('os.kill') as kill, mock.patch.object(m, 'cmd_handback') as publish:
                with self.assertRaisesRegex(ValueError, 'lifetime-bound'):
                    m.run_queue_once(inst, n, identity, recover=True)
                kill.assert_not_called()
                publish.assert_not_called()
            self.assertEqual(before, (m.queue_path(n, identity).read_bytes(), m.attempt_path(n, row).read_bytes()))
            audit = json.loads(m.queue_path(n, identity).with_suffix('.takeover.json').read_text())
            self.assertEqual(audit['state'], 'refused')
            self.assertEqual(audit['signalled'], [])

    def test_owned_takeover_rechecks_after_intent(self):
        with self.owned_takeover_fixture() as (m, inst, n, row, identity, stamps):
            original = m.write_takeover
            def changed(n, identity, audit):
                original(n, identity, audit)
                if audit['state'] == 'intent':
                    stamps[12346] = 'reused-after-intent'
            before = m.queue_path(n, identity).read_bytes()
            with mock.patch.object(m, 'write_takeover', side_effect=changed), mock.patch('os.kill') as signal:
                with self.assertRaisesRegex(ValueError, 'process'):
                    m.run_queue_once(inst, n, identity, recover=True)
                signal.assert_not_called()
            self.assertEqual(m.queue_path(n, identity).read_bytes(), before)

    def test_owned_takeover_replaced_inode(self):
        with self.owned_takeover_fixture() as (m, inst, n, row, identity, stamps):
            path = m.queue_path(n, identity).with_suffix('.lease')
            path.rename(path.with_suffix('.retained'))
            path.write_text('foreign lease')
            before = m.queue_path(n, identity).read_bytes()
            with mock.patch('os.kill') as signal:
                with self.assertRaisesRegex(ValueError, 'lease'):
                    m.run_queue_once(inst, n, identity, recover=True)
                signal.assert_not_called()
            self.assertEqual(path.read_text(), 'foreign lease')
            self.assertEqual(m.queue_path(n, identity).read_bytes(), before)

    def test_owned_takeover_foreign_owner_record(self):
        self.takeover_refusal(lambda d, s, i: d.update(owner=[12347, 'foreign']), 'owner')

    def test_owned_takeover_changed_attempt_binding(self):
        self.takeover_refusal(lambda d, s, i: (d.update(binding={'head':'other'}), s.update({12346: None})), 'binding')

    def test_owned_takeover_last_signal_check_preserves_reused_pid(self):
        self.takeover_refusal(lambda d, s, i: s.update({12346:'replacement'}), 'identity')

    def test_owned_takeover_registration_refuses_foreign_child(self):
        with self.owned_takeover_fixture() as (m, inst, n, row, identity, stamps):
            request = m.enqueue_handback(inst, n, row, argparse.Namespace(
                id='root', branch=row['Job'], state='blocked', summary='Fixture result'))
            with m.claim_handback(inst, n, request['id']) as claimed:
                m.queue_dispatch(inst, n, claimed)
                with mock.patch.object(m, 'owned_process_parent', return_value=999):
                    with self.assertRaisesRegex(ValueError, 'identity'):
                        m.queue_child(12346)

    def test_owned_takeover_pending_stop_is_not_repeated(self):
        with self.owned_takeover_fixture() as (m, inst, n, row, identity, stamps):
            before = m.queue_path(n, identity).read_bytes()
            with mock.patch('os.kill') as kill:
                with self.assertRaisesRegex(ValueError, 'lifetime-bound'):
                    m.run_queue_once(inst, n, identity, recover=True)
                kill.assert_not_called()
            # Preserve evidence from a legacy uncertain signal; never replay it.
            audit = json.loads(m.queue_path(n, identity).with_suffix('.takeover.json').read_text())
            audit['signalled'] = [[12346, 'child']]
            m.write_takeover(n, identity, audit)
            with mock.patch('os.kill') as kill:
                with self.assertRaisesRegex(ValueError, 'previous partial'):
                    m.run_queue_once(inst, n, identity, recover=True)
                kill.assert_not_called()
            self.assertEqual(m.queue_path(n, identity).read_bytes(), before)

    def test_owned_takeover_real_crash_child_and_foreign_process(self):
        """Real OS processes and inherited flock; only fabricated instance data."""
        m, inst, n, row = self.fixture()
        args = argparse.Namespace(id='root', branch=row['Job'], state='blocked', summary='Fixture result')
        # The helper is the original Brain runner. Its direct child inherits the
        # lease; runner os._exit simulates a crash without closing that child's FD.
        script = '''import os,sys,json,subprocess
sys.path.insert(0,sys.argv[1])
from brain import instance,handback_queue as q,owned_runs as owned,lifecycle_identity
lifecycle_identity.lifecycle_host_identity=lambda:'fixture-boot-a'
from pathlib import Path
inst=instance.Instance(Path(sys.argv[2])); n=inst.node('root')
with q.claim_handback(inst,n,sys.argv[3]) as request:
 q.queue_dispatch(inst,n,request)
 fd=q.queue_descendant_fd()
 control=int(sys.argv[4])
 child=subprocess.Popen([sys.executable,'-c','import os,sys; os.read(int(sys.argv[1]),1)',str(control)],pass_fds=(fd,control),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 owned.queue_child(child.pid)
 print(json.dumps([os.getpid(),child.pid]),flush=True)
 os._exit(0)
'''
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(
                m, 'attempt_binding', return_value={'head':'a', 'base':'b', 'inputs':'c'}):
            request = m.enqueue_handback(inst, n, row, args)
            foreign = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], env=self.env)
            control_read, control_write = os.pipe()
            runner = subprocess.Popen([sys.executable, '-B', '-c', script, str(CORE/'lib'),
                                       str(inst.root), request['id'], str(control_read)], env=self.env,
                                      pass_fds=(control_read,),
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            os.close(control_read)
            child = None
            try:
                # Child inherits no stdout pipe, so the crashed runner can be reaped.
                import select
                ready, _, _ = select.select([runner.stdout], [], [], 5)
                self.assertTrue(ready, 'runner never published child ownership')
                line = runner.stdout.readline()
                if not line:
                    self.fail(runner.stderr.read())
                owner, child = json.loads(line)
                self.assertEqual(runner.wait(timeout=5), 0)
                data = m.read_queue_request(n, request['id'])
                self.assertEqual(data['owner'][0], owner)
                self.assertEqual(data['run']['processes'][0][0], child)
                self.assertEqual(m.process_stamp(child), data['run']['processes'][0][1])
                foreign_stamp = m.process_stamp(foreign.pid)
                before = m.queue_path(n, request['id']).read_bytes(), m.attempt_path(n, row).read_bytes()
                with mock.patch.object(m, 'cmd_handback') as publish:
                    with self.assertRaisesRegex(ValueError, 'lifetime-bound'):
                        m.run_queue_once(inst, n, request['id'], recover=True)
                    publish.assert_not_called()
                self.assertEqual(before, (m.queue_path(n, request['id']).read_bytes(), m.attempt_path(n, row).read_bytes()))
                self.assertEqual(m.process_stamp(child), data['run']['processes'][0][1])
                # Let the child exit through its pipe, then reconcile absence.
                os.close(control_write)
                control_write = None
                import time
                deadline = time.monotonic() + 5
                while m.process_stamp(child) is not None and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertIsNone(m.process_stamp(child))
                m.run_queue_once(inst, n, request['id'], recover=True)
                self.assertEqual(m.process_stamp(foreign.pid), foreign_stamp)
                self.assertIsNone(foreign.poll())
                self.assertEqual(m.read_queue_request(n, request['id'])['state'], 'interrupted')
            finally:
                # These exact Popen objects were created by this test. No scan,
                # broad kill or cleanup of any process belonging to another run.
                if runner.poll() is None:
                    runner.terminate()
                    runner.wait(timeout=5)
                foreign.terminate()
                foreign.wait(timeout=5)
                if control_write is not None:
                    os.close(control_write)  # releases only this test child's pipe
                runner.stdout.close()
                runner.stderr.close()

    def test_owned_takeover_concurrent_process_owner_refuses(self):
        with self.owned_takeover_fixture() as (m, inst, n, row, identity, stamps):
            path = m.queue_path(n, identity).with_suffix('.takeover.lock')
            script = ('import fcntl,sys\n'
                      'with open(sys.argv[1],"a+") as f:\n'
                      ' fcntl.flock(f,fcntl.LOCK_EX)\n'
                      ' print("locked",flush=True)\n'
                      ' sys.stdin.read(1)\n')
            holder = subprocess.Popen([sys.executable, '-c', script, str(path)], env=self.env,
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            try:
                import select
                self.assertTrue(select.select([holder.stdout], [], [], 5)[0])
                self.assertEqual(holder.stdout.readline().strip(), 'locked')
                before = m.queue_path(n, identity).read_bytes()
                with mock.patch('os.kill') as signal:
                    with self.assertRaisesRegex(ValueError, 'takeover already'):
                        m.run_queue_once(inst, n, identity, recover=True)
                    signal.assert_not_called()
                self.assertEqual(m.queue_path(n, identity).read_bytes(), before)
            finally:
                holder.stdin.write('x')
                holder.stdin.flush()
                holder.wait(timeout=5)
                holder.stdin.close()
                holder.stdout.close()

    def test_owned_takeover_held_descendant_lease_prevents_settlement(self):
        import fcntl
        with self.owned_takeover_fixture() as (m, inst, n, row, identity, stamps):
            stamps[12346] = None
            path = m.queue_path(n, identity).with_suffix('.lease')
            held = os.open(path, os.O_RDWR)
            try:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                before = m.queue_path(n, identity).read_bytes()
                with mock.patch('os.kill') as signal:
                    with self.assertRaisesRegex(ValueError, 'descendant lease'):
                        m.run_queue_once(inst, n, identity, recover=True)
                    signal.assert_not_called()
                self.assertEqual(m.queue_path(n, identity).read_bytes(), before)
                self.assertEqual(m.current_attempt(n, row)['state'], 'queued')
            finally:
                os.close(held)

    def test_owned_takeover_pid_reused_after_observation_never_signals(self):
        with self.owned_takeover_fixture() as (m, inst, n, row, identity, stamps):
            before = m.queue_path(n, identity).read_bytes(), m.attempt_path(n, row).read_bytes()
            observed = []
            def reuse_after_observation(pid):
                stamp = stamps.get(pid)
                if pid == 12346 and stamp == 'child':
                    observed.append(stamp)
                    stamps[pid] = 'foreign-replacement'
                return stamp
            with mock.patch.object(m, 'process_stamp', side_effect=reuse_after_observation), \
                    mock.patch('os.kill') as kill:
                with self.assertRaises(ValueError):
                    m.run_queue_once(inst, n, identity, recover=True)
                self.assertEqual(observed, ['child'])
                kill.assert_not_called()
            self.assertEqual(before, (m.queue_path(n, identity).read_bytes(), m.attempt_path(n, row).read_bytes()))
            audit = json.loads(m.queue_path(n, identity).with_suffix('.takeover.json').read_text())
            self.assertEqual(audit['state'], 'refused')
            self.assertIn('lifetime-bound', audit['reason'])

    def takeover_audit_queue_control(self, live):
        with self.owned_takeover_fixture() as (m, inst, n, row, identity, stamps):
            if not live:
                stamps[12346] = None
            with mock.patch('os.kill') as kill:
                if live:
                    with self.assertRaises(ValueError):
                        m.run_queue_once(inst, n, identity, recover=True)
                else:
                    self.assertEqual(m.run_queue_once(inst, n, identity, recover=True), identity)
                kill.assert_not_called()
            audit_path = m.queue_path(n, identity).with_suffix('.takeover.json')
            audit = json.loads(audit_path.read_text())
            self.assertEqual(audit['state'], 'refused' if live else 'settled')
            second = {**row, 'Job': 'job/second', 'Started': '2026-01-02T00:00Z'}
            m.write_jobs(inst, n, [row, second])
            queued = m.enqueue_handback(inst, n, second, argparse.Namespace(
                id='root', branch=second['Job'], state='blocked', summary='Second fixture result'))
            before = audit_path.read_bytes()
            for strict in (False, True):
                projection = m.queue_projection(n, queued['id'], strict=strict)
                self.assertEqual((projection['state'], projection['position']), ('queued', 1))
            with self.assertRaisesRegex(ValueError, 'invalid handback request ID'):
                m.read_queue_request(n, identity + '.takeover')
            with mock.patch.object(m, 'cmd_handback') as publish:
                self.assertEqual(m.run_queue_once(inst, n), queued['id'])
                publish.assert_called_once()
                self.assertEqual(publish.call_args.args[0]._queue_request['id'], queued['id'])
            self.assertEqual(audit_path.read_bytes(), before)

    def test_owned_takeover_success_audit_keeps_queue_selection_and_projection(self):
        self.takeover_audit_queue_control(live=False)

    def test_owned_takeover_refused_audit_keeps_queue_selection_and_projection(self):
        self.takeover_audit_queue_control(live=True)
