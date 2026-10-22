"""Stable wake episodes and root completion notices from fabricated stored evidence."""
import contextlib
import io
import json
import os
from unittest import mock

import core_loader
from scenario import CORE, Scenario


class WatchExtension(Scenario):
    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, self.env)
        env.start()
        self.addCleanup(env.stop)
        self.m = core_loader.SourceFileLoader('extension_fixture', str(CORE / 'bin/brain')).load_module()
        self.root = self.init('oak')
        self.b(self.root, 'set', 'root', 'coordinator=fixture-root', 'wake=fixture-send')
        self.inst = self.m.Instance(self.root)

    def watch(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.m.watch_once(self.inst)

    def root_events(self):
        group = self.m.watch_groups(self.inst).get('root', {})
        return [i for i in group.get('items', []) if i['kind'] in ('job-ready', 'job-landed')]

    def project(self):
        home = self.repo(self.tmp / 'source')
        self.b(self.root, 'new', 'app', '--title', 'App', '--home', str(home))
        self.b(self.root, 'set', 'app', 'coordinator=fixture-child', 'wake=fixture-child-send', 'verify=echo fixture-verify')
        self.b(self.root, 'wt', 'add', 'app', 'job/fixture', '--no-setup')
        self.inst = self.m.Instance(self.root)
        n = self.inst.node('app')
        w = n['local']['worktrees']['job/fixture']
        self.tip(w['path'], 'first')
        row = self.m.open_job(self.m.read_jobs(n), 'job/fixture')
        return home, n, w, row

    def tip(self, path, text):
        from pathlib import Path
        (Path(path) / 'README.md').write_text(text + '\n')
        self.git('add', 'README.md', cwd=path)
        self.git('commit', '-qm', text, cwd=path)

    def signal(self, n, row, ready=True, episode='a' * 32):
        head = self.git('rev-parse', 'refs/heads/' + row['Job'], cwd=n['home_path']).strip()
        main = self.git('rev-parse', 'main', cwd=n['home_path']).strip()
        packet = {'version': 1, 'node': n['id'], 'branch': row['Job'], 'head': head, 'main': main,
                  'integration_base': main, 'verify_command': n['fm']['verify'], 'verify_result': 0,
                  'inputs': 'c' * 64, 'review_rounds': 1, 'checked_at': self.inst.now_dt().isoformat()}
        value = {'version': 2, 'job': row['Job'], 'started': row['Started'], 'state': 'done',
                 'at': self.inst.now(), 'lessons': 'none: fixture', 'report': 'private fixture report',
                 'ready': packet if ready else None, 'ready_reasons': [], 'wake_id': episode}
        self.save_signal(n, row, value)
        return value

    def save_signal(self, n, row, value):
        path = self.m.handback_file(n, row)
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(value))

    def test_rotate_counts_deduplicate_until_coordinator_episode_changes(self):
        root = self.inst.node('root')
        root['local']['coordinator_sessions'] = ['fixture-session-a']
        with mock.patch.object(self.m, 'changes_due', return_value=[]), mock.patch.object(self.m, 'wake_start'), \
                mock.patch.object(self.m, 'WAKE_INTERVAL', 0), mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            for context, turns in ((160000, 50), (170000, 51), (180000, 52)):
                with mock.patch.object(self.m, 'rotation_due', return_value=f'coordinator context {context} tokens, {turns} turns; rotate'):
                    self.watch()
                    self.m.wake_drain(self.inst, root, once=True)
            self.assertEqual(send.call_count, 1)
            root['local']['coordinator_sessions'] = ['fixture-session-b']
            self.inst.local_of('root')['coordinator_sessions'] = ['fixture-session-b']
            with mock.patch.object(self.m, 'rotation_due', return_value='coordinator context 160000 tokens, 50 turns; rotate'):
                self.watch()
                self.m.wake_drain(self.inst, root, once=True)
            self.assertEqual(send.call_count, 2)

    def test_republished_done_after_new_tip_wakes_owner_again(self):
        home, n, w, row = self.project()
        root = self.inst.node('root')
        with mock.patch.object(self.m, 'changes_due', return_value=[]), mock.patch.object(self.m, 'wake_start'), \
                mock.patch.object(self.m, 'WAKE_INTERVAL', 0), mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            self.b(self.root, 'handback', 'app', '--branch', row['Job'], '--state', 'done')
            first = self.m.read_handback(n, row)
            self.watch()
            self.m.wake_drain(self.inst, n, once=True)
            self.assertEqual(send.call_count, 1)
            self.tip(w['path'], 'rebased-tip')
            self.b(self.root, 'handback', 'app', '--branch', row['Job'], '--state', 'done')
            second = self.m.read_handback(n, row)
            self.watch()
            self.m.wake_drain(self.inst, n, once=True)
            self.m.wake_drain(self.inst, root, once=True)
            self.assertEqual(send.call_count, 2)
            self.assertNotEqual(first.get('wake_id'), second.get('wake_id'))
            self.assertEqual(self.root_events(), [])  # done is never a ready claim
            self.watch()
            self.m.wake_drain(self.inst, n, once=True)
            self.assertEqual(send.call_count, 2)

    def test_stored_ready_notifies_root_once_and_second_tip_is_new(self):
        home, n, w, row = self.project()
        self.signal(n, row, ready=False)
        self.assertEqual(self.root_events(), [])
        signal = self.signal(n, row)
        with mock.patch.object(self.m, 'ready_state', side_effect=AssertionError('no readiness path')), \
                mock.patch.object(self.m, 'record_ready', side_effect=AssertionError('no verification')):
            events = self.root_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['kind'], 'job-ready')
        self.assertNotIn('private fixture report', json.dumps(events))
        self.assertNotIn(row['Job'], json.dumps(events))
        signal.update(report='changed display only', at='2099-01-01', verify_runs=99)
        self.save_signal(n, row, signal)
        self.assertEqual([i['id'] for i in self.root_events()], [events[0]['id']])
        self.tip(w['path'], 'new-reviewed-tip')
        self.assertEqual(self.root_events(), [])  # stored ready head is now stale
        self.signal(n, row, episode='b' * 32)
        self.assertNotEqual(self.root_events()[0]['id'], events[0]['id'])

    def test_malformed_failed_or_wrong_job_ready_never_notifies_root(self):
        home, n, w, row = self.project()
        good = self.signal(n, row)
        self.assertEqual(len(self.root_events()), 1)
        cases = [('verify_result', True), ('verify_result', False), ('verify_result', 1), ('version', 99),
                 ('node', 'other'), ('branch', 'job/other'), ('head', '0' * 40),
                 ('main', '0' * 40), ('review_rounds', 0), ('checked_at', 'bad'), ('checked_at', ['bad']),
                 ('verify_lanes', [{'status': 'passed', 'result': 1}]), ('verify_lanes_check', {'result': 1})]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                changed = {**good, 'ready': {**good['ready'], key: value}}
                self.save_signal(n, row, changed)
                self.assertEqual(self.root_events(), [])
        self.save_signal(n, row, {**good, 'ready_reasons': ['not current']})
        self.assertEqual(self.root_events(), [])
        self.save_signal(n, row, {**good, 'state': 'blocked'})
        self.assertEqual(self.root_events(), [])
        self.save_signal(n, row, good)
        journal = n['dir'] / 'job-trains' / 'fixture.json'
        journal.parent.mkdir(exist_ok=True)
        journal.write_text(json.dumps({'version': 1, 'ejected': [{'key': self.m.job_key(row),
            'packet': self.m.ready_fingerprint(n, row, good['ready'])}]}))
        self.assertEqual(self.root_events(), [])

    def test_ready_events_deliver_once_per_packet_without_verification(self):
        home, n, w, row = self.project()
        self.signal(n, row)
        root = self.inst.node('root')
        with mock.patch.object(self.m, 'changes_due', return_value=[]), mock.patch.object(self.m, 'wake_start'), \
                mock.patch.object(self.m, 'WAKE_INTERVAL', 0), mock.patch.object(self.m, 'wake_send', return_value=True) as send, \
                mock.patch.object(self.m, 'ready_state', side_effect=AssertionError('no readiness path')), \
                mock.patch.object(self.m, 'run_verify', side_effect=AssertionError('no verify execution')):
            for _ in range(2):
                self.watch()
                self.m.wake_drain(self.inst, root, once=True)
            self.assertEqual(send.call_count, 1)
            self.tip(w['path'], 'second-reviewed-tip')
            self.signal(n, row, episode='b' * 32)
            for _ in range(2):
                self.watch()
                self.m.wake_drain(self.inst, root, once=True)
            self.assertEqual(send.call_count, 2)

    def test_legacy_ready_packet_is_read_but_never_resurrected_over_new_signal(self):
        home, n, w, row = self.project()
        signal = self.signal(n, row)
        handback = self.m.handback_file(n, row)
        handback.unlink()
        legacy = self.m.ready_path(self.inst, n, row['Job'])
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(json.dumps(signal['ready']))
        self.assertEqual(len(self.root_events()), 1)
        handback.write_text('{broken')
        self.assertEqual(self.root_events(), [])
        self.save_signal(n, row, {**signal, 'state': 'blocked'})
        self.assertEqual(self.root_events(), [])

    def test_landed_jobs_batch_one_root_wake_and_ignore_bookkeeping(self):
        self.b(self.root, 'new', 'app', '--title', 'App')
        self.inst = self.m.Instance(self.root)
        n, root = self.inst.node('app'), self.inst.node('root')
        base = dict.fromkeys(self.m.JOB_COLS, '')
        rows = [{**base, 'Job': 'job/private-' + str(i), 'Started': '2020-01-01T00:00Z',
                 'Ended': '2020-01-02T00:00Z', 'Outcome': 'merged'} for i in range(2)]
        self.m.write_jobs(self.inst, n, rows)
        with mock.patch.object(self.m, 'changes_due', return_value=[]), mock.patch.object(self.m, 'wake_start'), \
                mock.patch.object(self.m, 'WAKE_INTERVAL', 0), mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            self.watch()
            self.m.wake_drain(self.inst, root, once=True)
            self.assertEqual(send.call_count, 1)
            history = self.m.wake_history(self.m.wake_directory(self.inst, root))
            self.assertEqual([i['kind'] for i in history[-1]['items']], ['job-landed', 'job-landed'])
            rows[0].update(Settled='yes', Reviews='2', Lessons='none: fixture')
            self.m.write_jobs(self.inst, n, rows)
            self.watch()
            self.m.wake_drain(self.inst, root, once=True)
            self.assertEqual(send.call_count, 1)
            self.assertNotIn('job/private-', json.dumps(self.root_events()))

    def test_stale_pending_ready_is_removed_without_dropping_question(self):
        home, n, w, row = self.project()
        signal = self.signal(n, row)
        events = self.root_events()
        self.assertEqual(len(events), 1)
        root = self.inst.node('root')
        question = {'id': 'd' * 64, 'kind': 'inbox'}
        with mock.patch.object(self.m, 'wake_start'), mock.patch.object(self.m, 'WAKE_INTERVAL', 0), \
                mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            self.m.request_wake(self.inst, root, 'ready mixed question', items=events + [question])
            signal['state'] = 'blocked'
            self.save_signal(n, row, signal)
            self.m.wake_drain(self.inst, root, once=True)
            self.assertEqual(send.call_count, 1)
            history = self.m.wake_history(self.m.wake_directory(self.inst, root))
            self.assertEqual(history[-1]['items'], [question])

    def test_completion_read_failure_cannot_block_a_queued_question(self):
        home, n, w, row = self.project()
        self.signal(n, row)
        events = self.root_events()
        self.assertEqual(len(events), 1)
        root = self.inst.node('root')
        question = {'id': 'd' * 64, 'kind': 'inbox'}
        with mock.patch.object(self.m, 'wake_start'), mock.patch.object(self.m, 'WAKE_INTERVAL', 0), \
                mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            self.m.request_wake(self.inst, root, 'ready and protected question', items=events + [question])
            with mock.patch.object(self.m, 'watch_completion_items', side_effect=OSError('fixture unreadable evidence')), \
                    contextlib.suppress(OSError):
                self.m.wake_drain(self.inst, root, once=True)
            self.assertEqual(send.call_count, 1)
            history = self.m.wake_history(self.m.wake_directory(self.inst, root))
            self.assertIn(question, history[-1]['items'])

    def test_active_drain_reloads_new_child_bindings_before_filtering(self):
        old_inst, root = self.inst, self.inst.node('root')
        calls = []
        def send(command):
            calls.append(command)
            if len(calls) == 1:
                home, n, w, row = self.project()
                self.signal(n, row)
                self.watch()  # new binding and completion arrive during the previous send
            return True
        with mock.patch.object(self.m, 'changes_due', return_value=[]), mock.patch.object(self.m, 'wake_start'), \
                mock.patch.object(self.m, 'WAKE_INTERVAL', 0), mock.patch.object(self.m, 'wake_send', side_effect=send):
            self.m.request_wake(old_inst, root, 'first unrelated request', items=[{'id': 'f' * 64, 'kind': 'inbox'}])
            self.m.wake_drain(old_inst, root)
            self.assertEqual(len(calls), 2)
            history = self.m.wake_history(self.m.wake_directory(self.inst, root))
            self.assertEqual([[i['kind'] for i in h['items']] for h in history], [['inbox'], ['job-ready']])
            self.watch()
            self.assertFalse(list(self.m.wake_directory(self.inst, root).glob('*.pending')))

    def assert_unknown_completion_survives(self, mixed):
        from pathlib import Path
        home, n, w, row = self.project()
        root = self.inst.node('root')
        original = Path.read_text
        def denied(path, *args, **kwargs):
            if path == self.m.handback_file(n, row):
                raise PermissionError('fixture handback temporarily unreadable')
            return original(path, *args, **kwargs)
        question = {'id': 'd' * 64, 'kind': 'inbox'}
        for mode in ('handback', 'packet'):
            with self.subTest(mode=mode), mock.patch.object(self.m, 'changes_due', return_value=[]), \
                    mock.patch.object(self.m, 'wake_start'), mock.patch.object(self.m, 'WAKE_INTERVAL', 0), \
                    mock.patch.object(self.m, 'wake_send', return_value=True) as send:
                self.signal(n, row)
                self.watch()  # acknowledge a valid notice before the transient read failure
                if mixed:
                    self.m.request_wake(self.inst, root, 'protected mixed ' + mode, items=self.root_events() + [question])
                failure = (mock.patch.object(Path, 'read_text', denied) if mode == 'handback' else
                           mock.patch.object(self.m, 'watch_recorded_ready', side_effect=OSError('fixture read failure')))
                with failure:
                    self.m.wake_drain(self.inst, root, once=True)
                self.assertEqual(send.call_count, 1)
                history = self.m.wake_history(self.m.wake_directory(self.inst, root))
                self.assertIn('job-ready', [i['kind'] for i in history[-1]['items']])
                if mixed:
                    self.assertIn(question, history[-1]['items'])
                self.watch()
                self.assertFalse(list(self.m.wake_directory(self.inst, root).glob('*.pending')))

    def test_unknown_completion_read_preserves_pure_notice(self):
        self.assert_unknown_completion_survives(False)

    def test_unknown_completion_read_preserves_notice_and_question(self):
        self.assert_unknown_completion_survives(True)

    def test_legacy_packet_fences_cached_origin_advance_and_keeps_local_ahead(self):
        home, n, w, row = self.project()
        signal = self.signal(n, row)
        self.m.handback_file(n, row).unlink()
        packet = signal['ready']
        packet.pop('integration_base')
        path = self.m.ready_path(self.inst, n, row['Job'])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(packet))
        base = packet['main']
        self.git('remote', 'add', 'origin', 'https://example.invalid/source.git', cwd=home)
        self.git('symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/main', cwd=home)
        self.git('update-ref', 'refs/remotes/origin/main', base, cwd=home)
        tree = self.git('rev-parse', base + '^{tree}', cwd=home).strip()
        advanced = self.git('commit-tree', tree, '-p', base, '-m', 'cached advance', cwd=home).strip()
        root = self.inst.node('root')
        with mock.patch.object(self.m, 'changes_due', return_value=[]), mock.patch.object(self.m, 'wake_start'), \
                mock.patch.object(self.m, 'WAKE_INTERVAL', 0), mock.patch.object(self.m, 'wake_send', return_value=True) as send, \
                mock.patch.object(self.m, 'ready_state', side_effect=AssertionError('no readiness execution')):
            self.assertEqual(len(self.root_events()), 1)

            self.watch()
            self.git('update-ref', 'refs/remotes/origin/main', advanced, cwd=home)
            self.assertEqual(self.root_events(), [])
            self.m.wake_drain(self.inst, root, once=True)
            self.assertEqual(send.call_count, 0)
            self.assertFalse(list(self.m.wake_directory(self.inst, root).glob('*.pending')))
            # A valid local integration branch ahead of cached origin remains eligible.
            self.git('update-ref', 'refs/heads/main', advanced, cwd=home)
            self.git('rebase', advanced, cwd=w['path'])
            packet.update(main=advanced, head=self.git('rev-parse', row['Job'], cwd=home).strip())
            path.write_text(json.dumps(packet))
            self.git('update-ref', 'refs/remotes/origin/main', base, cwd=home)
            self.assertEqual(len(self.root_events()), 1)

    def test_unknown_job_does_not_keep_other_jobs_stale_ready_notice(self):
        from pathlib import Path
        home, n, w, row = self.project()
        self.b(self.root, 'wt', 'add', 'app', 'job/other', '--no-setup')
        self.inst = self.m.Instance(self.root)
        n = self.inst.node('app')
        other = self.m.open_job(self.m.read_jobs(n), 'job/other')
        first = self.signal(n, row)
        stale = self.root_events()[0]
        self.signal(n, other, episode='b' * 32)
        unknown = next(i for i in self.root_events() if i['id'] != stale['id'])
        self.save_signal(n, row, {**first, 'state': 'blocked'})
        original = Path.read_text
        def denied(path, *args, **kwargs):
            if path == self.m.handback_file(n, other):
                raise PermissionError('fixture other job unreadable')
            return original(path, *args, **kwargs)
        root, question = self.inst.node('root'), {'id': 'd' * 64, 'kind': 'inbox'}
        original_packet = self.m.watch_recorded_ready
        def unavailable(inst, node, job, signal, **kwargs):
            if job['Job'] == other['Job']:
                raise SystemExit('fixture per-job reader exit')
            return original_packet(inst, node, job, signal, **kwargs)
        with mock.patch.object(self.m, 'wake_start'), mock.patch.object(self.m, 'WAKE_INTERVAL', 0), \
                mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            failures = [mock.patch.object(Path, 'read_text', denied),
                        mock.patch.object(self.m, 'watch_recorded_ready', side_effect=unavailable)]
            for index, failure in enumerate(failures):
                self.m.request_wake(self.inst, root, 'two jobs and a question ' + str(index), items=[stale, unknown, question])
                with failure:
                    self.m.wake_drain(self.inst, root, once=True)
                self.assertEqual(send.call_count, index + 1)
                refs = self.m.wake_history(self.m.wake_directory(self.inst, root))[-1]['items']
                self.assertNotIn(stale['id'], [i['id'] for i in refs])
                self.assertIn(unknown['id'], [i['id'] for i in refs])
                self.assertIn(question, refs)

    def test_unknown_unrelated_ledger_does_not_keep_stale_ready_notice(self):
        home, n, w, row = self.project()
        first = self.signal(n, row)
        stale = self.root_events()[0]
        self.save_signal(n, row, {**first, 'state': 'blocked'})
        self.b(self.root, 'new', 'unrelated', '--title', 'Unrelated')
        self.inst = self.m.Instance(self.root)
        other = self.inst.node('unrelated')
        (other['dir'] / self.m.JOBS_FILE).write_text('unreadable legacy ledger\n')
        root, question = self.inst.node('root'), {'id': 'd' * 64, 'kind': 'inbox'}
        with mock.patch.object(self.m, 'wake_start'), mock.patch.object(self.m, 'WAKE_INTERVAL', 0), \
                mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            self.m.request_wake(self.inst, root, 'stale job and a question', items=[stale, question])
            self.m.wake_drain(self.inst, root, once=True)
            self.assertEqual(send.call_count, 1)
            refs = self.m.wake_history(self.m.wake_directory(self.inst, root))[-1]['items']
            self.assertEqual(refs, [question])
