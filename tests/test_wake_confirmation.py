"""Mocked transport/consumption regressions; never contact a host or webhook."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
from unittest import mock

import core_loader
from scenario import Scenario

UnmockedTransport = type('UnmockedTransport', (BaseException,), {})


CORE = Path(os.environ.get('BRAIN_CORE') or Path(__file__).resolve().parent.parent)


class WakeConfirmation(Scenario):
    fixture_templates = True
    fixture_init_in_process = True

    def setUp(self):
        super().setUp()
        environment = mock.patch.dict(os.environ, self.env, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.m = core_loader.SourceFileLoader('wake_confirmed', str(CORE / 'bin/brain')).load_module()
        self.r = self.init('fixture-owner')
        self.b(self.r, 'new', 'app', '--title', 'Fixture app')
        self.b(self.r, 'set', 'app', 'coordinator=fixture-current', 'wake=fixture-send')
        self.refresh()
        os.environ['T3CODE_THREAD_ID'] = 'fixture-current'
        os.environ['CODEX_THREAD_ID'] = 'fixture-provider-session'
        os.environ.pop('CLAUDE_CODE_SESSION_ID', None)
        self.now = 1000000
        clock = mock.Mock()
        clock.time.side_effect = lambda: self.now
        clock.monotonic.side_effect = lambda: self.now
        for patch in (mock.patch.object(self.m, 'WAKE_CLOCK', clock),
                      mock.patch.object(self.m, 'wake_start'),
                      mock.patch.object(self.m, 'wake_schedule')):
            patch.start()
            self.addCleanup(patch.stop)
        # Fail closed if any test accidentally reaches a real transport.
        patch = mock.patch.object(self.m, 'wake_send', side_effect=UnmockedTransport('unmocked transport'))
        patch.start()
        self.addCleanup(patch.stop)

    def refresh(self):
        self.inst = self.m.Instance(self.r)
        self.n = self.inst.node('app')
        self.directory = self.m.wake_directory(self.inst, self.n)

    def configure(self, *settings):
        self.b(self.r, 'set', 'app', *settings)
        self.refresh()

    def enqueue(self, number=1):
        self.assertTrue(self.m.request_wake(self.inst, self.n, 'fixture:' + str(number),
                        items=[{'id': format(number, '064x'), 'kind': 'inbox'}], explicit=True))
        return self.directory / (self.m.wake_request_key(self.inst, self.n, 'fixture:' + str(number)) + '.pending')

    def drain(self, result=True):
        with mock.patch.object(self.m, 'wake_send', return_value=result) as send:
            self.m.wake_drain(self.inst, self.n, once=True)
        return send

    def test_known_noop_is_disabled_without_transport_or_acknowledgement(self):
        for command in ('/usr/bin/true', '/bin/true', 'true', ':'):
            with self.subTest(command=command):
                self.configure('wake=' + command)
                pending = self.enqueue()
                send = self.drain()
                send.assert_not_called()
                self.assertTrue(pending.exists())
                self.assertFalse(list(self.directory.glob('*.sent')))
                self.assertFalse(list(self.directory.glob('shown-*.json')))
                self.assertIn('disabled', self.m.wake_diagnostic(self.inst, self.n))

    def test_disabled_route_keeps_new_requests_and_prevents_consumption_or_fallback(self):
        pending = self.enqueue()
        self.drain()
        self.configure('wake_enabled=off')
        later = self.enqueue(2)
        self.now += 121
        self.drain().assert_not_called()
        self.assertIsNone(self.m.wake_turn_consume(self.inst, self.n))
        self.assertTrue(pending.exists())
        self.assertTrue(later.exists())
        self.assertIn('disabled', self.m.wake_diagnostic(self.inst, self.n))

    def test_shell_exit_is_never_an_http_status(self):
        pending = self.enqueue()
        self.drain('shell exit 0')
        record = json.loads((self.directory / 'delivery.json').read_text())
        self.assertEqual(record['status'], 'shell exit 0')
        self.assertTrue(pending.exists())
        row = self.m.wake_history(self.directory)[-1]
        self.assertEqual(row['transport_status'], 'shell exit 0')
        self.assertNotIn('ack_binding', row)
        self.assertEqual(row['start_state'], 'UNKNOWN')

    def test_shell_legacy_synthetic_http_is_not_read_as_http_evidence(self):
        self.m.wake_record(self.directory, 'fixture-send', 200)
        self.assertIsNone(self.m.wake_read(self.directory, 'fixture-send'))

    def test_real_shell_sender_returns_shell_outcome(self):
        process = mock.Mock(returncode=0)
        process.communicate.return_value = (None, None)
        sender = core_loader.SourceFileLoader('wake_sender', str(CORE / 'bin/brain')).load_module()
        with mock.patch.object(sender._owners['wake_send'].subprocess, 'Popen', return_value=process):
            self.assertEqual(sender.wake_send('fixture-send'), 'shell exit 0')

    def test_http_acceptance_preserves_pending_until_consumption(self):
        self.configure('wake=https://wake.invalid/fixture')
        pending = self.enqueue()
        self.drain(202)
        self.assertTrue(pending.exists())
        self.assertFalse(pending.with_suffix('.sent').exists())
        self.assertFalse(self.m.wake_shown_evidence(self.inst, self.n)[0])
        self.assertEqual(json.loads((self.directory / 'delivery.json').read_text())['status'], 202)
        receipt = self.m.wake_turn_consume(self.inst, self.n)
        self.assertIsNotNone(receipt)
        self.assertFalse(pending.exists())
        self.assertEqual(pending.with_suffix('.sent').read_text(), 'consumed\n')

    def test_no_run_stalls_at_120_seconds_without_blind_retry(self):
        pending = self.enqueue()
        self.drain()
        self.now += 119
        self.assertIsNone(self.m.wake_turn_stuck(self.inst, self.n))
        self.now += 1
        notice = self.m.wake_turn_stuck(self.inst, self.n)
        self.assertIn('UNKNOWN', notice)
        self.assertIn('120 seconds', notice)
        token = json.loads((self.directory / 'turn.json').read_text())['token']
        for _ in range(3):
            self.drain().assert_not_called()
            self.assertEqual(self.m.wake_turn_stuck(self.inst, self.n), notice)
        self.assertTrue(pending.exists())
        self.assertEqual(json.loads((self.directory / 'turn.json').read_text())['token'], token)

    def test_unrelated_manual_run_does_not_acknowledge_or_rearm(self):
        pending = self.enqueue()
        self.drain()
        # No host run timestamp is acknowledgement authority, even if newer.
        self.configure('status=active')
        (self.r / '.brain/coordinator-runs.json').write_text(json.dumps({
            'version': 1, 'observed_at': self.inst.now_dt().isoformat(),
            'runs': [{'node': 'app', 'thread': 'fixture-current', 'run': 'fixture-unrelated-manual-run',
                      'status': 'active', 'started_at': self.inst.now_dt().isoformat(), 'queued_wakes': []}]}))
        self.now += 121
        self.drain().assert_not_called()
        self.due('app', 'fixture-unrelated')
        self.assertTrue(pending.exists())
        self.assertIn('UNKNOWN', self.m.wake_turn_stuck(self.inst, self.n))

    def test_busy_unknown_host_cannot_create_duplicate_on_later_request(self):
        first = self.enqueue()
        self.drain()
        second = self.enqueue(2)
        self.now += 121
        self.drain().assert_not_called()
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())
        self.assertEqual(len(self.m.wake_turn_consume(self.inst, self.n)['items']), 2)

    def test_rotation_during_metadata_capture_never_sends_retired_binding(self):
        pending = self.enqueue()
        def rotate(*args):
            self.configure('coordinator=fixture-next', 'wake=fixture-next-send')
            return []
        with mock.patch.object(self.m, 'wake_snapshot', side_effect=rotate):
            self.drain().assert_not_called()
        self.assertTrue(pending.exists())
        self.assertFalse(list(self.directory.glob('*.sent')))

    def test_old_receipt_does_not_confirm_replacement_binding(self):
        pending = self.enqueue()
        self.drain()
        old = (self.directory / 'turn.json').read_bytes()
        self.configure('coordinator=fixture-next', 'wake=fixture-next-send')
        self.assertIsNone(self.m.wake_turn_consume(self.inst, self.n))
        self.now += 121
        self.drain().assert_called_once()
        self.assertTrue(pending.exists())
        self.assertTrue(any(p.read_bytes() == old for p in self.directory.glob('turn-retired-*.json')))

    def test_separate_consumed_rate_windows_keep_separate_history_episodes(self):
        self.enqueue()
        self.drain()
        self.m.wake_turn_consume(self.inst, self.n)
        self.now += 121
        self.enqueue(2)
        self.drain()
        rows = self.m.wake_history(self.directory)
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]['episode'], rows[1]['episode'])
        self.assertEqual([r['at'] for r in rows], [1000000, 1000121])

    def test_failed_enqueue_does_not_become_recorded_progress(self):
        with mock.patch.object(self.m, 'atomic_write', side_effect=OSError('fixture refusal')):
            self.assertFalse(self.m.request_wake(self.inst, self.n, 'fixture:1', items=[
                {'id': format(1, '064x'), 'kind': 'inbox'}], explicit=True))
        self.assertFalse(self.m.wake_recorded(self.inst, self.n, 'fixture:1'))
        self.assertFalse(list(self.directory.glob('*.sent')))

    def test_disabled_notice_is_once_in_owner_and_root_due(self):
        self.configure('wake=/usr/bin/true')
        self.enqueue()
        for node in ('app', 'root'):
            with self.subTest(node=node), mock.patch.object(self.m, 'wake_start'):
                out = io.StringIO()
                args = argparse.Namespace(id=node, quiet=False, json=False, handled=False, machine=False)
                with contextlib.redirect_stdout(out):
                    self.m._due_tick(args, self.inst, [self.inst.node(node)])
                self.assertEqual(out.getvalue().count('wake delivery explicitly disabled'), 1)

    def test_stall_notice_is_once_in_owner_and_root_due(self):
        self.enqueue()
        self.drain()
        self.now += 121
        for node in ('app', 'root'):
            with self.subTest(node=node), mock.patch.object(self.m, 'wake_start'):
                out = io.StringIO()
                args = argparse.Namespace(id=node, quiet=False, json=False, handled=False, machine=False)
                with contextlib.redirect_stdout(out):
                    self.m._due_tick(args, self.inst, [self.inst.node(node)])
                self.assertEqual(out.getvalue().count('coordinator wake stalled'), 1)
            # The owner's due consumes its receipt, so reserve another for the root view.
            if node == 'app':
                self.now += 121
                self.enqueue(2)
                self.drain()
                self.now += 121

    def test_unknown_transport_outcome_retains_episode_without_resend(self):
        pending = self.enqueue()
        self.drain(None)
        self.now += 121
        self.drain().assert_not_called()
        self.assertTrue(pending.exists())
        self.assertIn('transport outcome unknown', self.m.wake_turn_stuck(self.inst, self.n))

    def test_failed_consumption_publication_does_not_authorize_resend(self):
        pending = self.enqueue()
        self.drain()
        original = self.m.atomic_write
        def refuse_shown(path, *args, **kwargs):
            if path.name.startswith('shown-'):
                raise OSError('fixture acknowledgement fault')
            return original(path, *args, **kwargs)
        with mock.patch.object(self.m, 'atomic_write', side_effect=refuse_shown):
            self.assertIsNone(self.m.wake_turn_consume(self.inst, self.n))
        self.assertTrue(pending.exists())
        self.now += 121
        self.drain().assert_not_called()
        self.assertIsNotNone(self.m.wake_turn_consume(self.inst, self.n))
        self.assertFalse(pending.exists())

    def test_terminal_acceptance_has_cross_route_fence_until_consumed(self):
        self.b(self.r, 'set', 'root', 'coordinator=fixture-root', 'wake=fixture-root-send')
        self.refresh()
        root = self.inst.node('root')
        ref = {'id': 'a' * 64, 'kind': 'machine-exited'}
        for node in (self.n, root):
            self.assertTrue(self.m.request_wake(self.inst, node, 'fixture-terminal', items=[ref], explicit=True))
        self.drain().assert_called_once()
        self.assertFalse(self.m.wake_terminal_delivered(self.inst, ref['id']))
        with mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            self.m.wake_drain(self.inst, root, once=True)
            send.assert_not_called()
        self.assertTrue(list(self.m.wake_directory(self.inst, root).glob('*.pending')))
        self.m.wake_turn_consume(self.inst, self.n)
        self.assertTrue(self.m.wake_terminal_delivered(self.inst, ref['id']))
        with mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            self.m.wake_drain(self.inst, root, once=True)
            send.assert_not_called()
        self.assertFalse(list(self.m.wake_directory(self.inst, root).glob('*.pending')))

    def test_known_retired_webhook_is_not_sent_after_rotation(self):
        self.configure('wake=https://wake.invalid/fixture-old')
        pending = self.enqueue()
        self.drain(202)
        self.configure('coordinator=fixture-next')
        self.now += 121
        self.drain(202).assert_not_called()
        self.assertTrue(pending.exists())
        self.assertIn('stale after coordinator rotation', self.m.wake_diagnostic(self.inst, self.n))
        self.configure('wake=https://wake.invalid/fixture-next')
        send = self.drain(202)
        self.assertEqual(send.call_args.args[0], 'https://wake.invalid/fixture-next')
        self.assertTrue(pending.exists())

    def test_root_due_deduplicates_a_disabled_inherited_route(self):
        self.b(self.r, 'set', 'root', 'coordinator=fixture-root', 'wake=/usr/bin/true')
        self.configure('coordinator=', 'wake=')
        self.b(self.r, 'new', 'another', '--title', 'Another fixture')
        self.refresh()
        out = io.StringIO()
        args = argparse.Namespace(id='root', quiet=False, json=False, handled=False, machine=False)
        with contextlib.redirect_stdout(out):
            self.m._due_tick(args, self.inst, [self.inst.node('root')])
        self.assertEqual(out.getvalue().count('wake delivery explicitly disabled'), 1)

    def due(self, node, caller='fixture-current', output=None, host=True):
        args = argparse.Namespace(id=node, quiet=False, json=False, handled=False, machine=False)
        out = output or io.StringIO()
        with mock.patch.object(self.m, 'load', return_value=self.inst), \
                mock.patch.dict(os.environ, {'T3CODE_THREAD_ID': caller, 'CODEX_THREAD_ID': 'fixture-provider-session' if host else caller, 'CLAUDE_CODE_SESSION_ID': ''}), \
                contextlib.redirect_stdout(out):
            if not host:
                os.environ.pop('T3CODE_THREAD_ID', None)
            self.m.cmd_due(args)
        return out

    def test_aggregate_and_foreign_due_never_acknowledge_coordinator_work(self):
        pending = self.enqueue()
        self.drain()
        for node, caller in ((None, 'fixture-current'), ('app', 'fixture-unrelated'), ('app', '')):
            self.due(node, caller)
            self.assertTrue(pending.exists())
            self.assertFalse(pending.with_suffix('.sent').exists())
        self.due('app')
        self.assertFalse(pending.exists())
        self.assertEqual(pending.with_suffix('.sent').read_text(), 'consumed\n')

    def test_due_output_failure_never_acknowledges(self):
        class BrokenOutput(io.StringIO):
            def flush(self):
                raise OSError('fixture broken output')
        pending = self.enqueue()
        self.drain()
        with self.assertRaises(OSError):
            self.due('app', output=BrokenOutput())
        self.assertTrue(pending.exists())
        self.assertFalse(pending.with_suffix('.sent').exists())
        self.due('app')
        self.assertFalse(pending.exists())

    def test_bound_due_reconstructs_damaged_receipt_without_resending(self):
        pending = self.enqueue()
        self.drain()
        path = self.directory / 'turn.json'
        path.write_bytes(b'{fixture-torn')
        self.due('app', 'fixture-unrelated')
        self.assertEqual(path.read_bytes(), b'{fixture-torn')
        self.assertTrue(pending.exists())
        self.due('app')
        self.assertFalse(pending.exists())
        self.assertTrue(any(p.read_bytes() == b'{fixture-torn' for p in self.directory.glob('turn-retired-*.json')))
        self.drain().assert_not_called()

    def test_definite_connection_failure_remains_retryable(self):
        import urllib.error
        sender = core_loader.SourceFileLoader('refused_sender', str(CORE / 'bin/brain')).load_module()
        sender._owners['wake_send'].WakeNotStarted = self.m.WakeNotStarted
        self.configure('wake=https://wake.invalid/fixture')
        pending = self.enqueue()
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.URLError(ConnectionRefusedError('fixture refused'))
        with mock.patch('urllib.request.build_opener', return_value=opener), \
                mock.patch.object(self.m, 'wake_send', side_effect=sender.wake_send):
            for _ in range(2):
                self.m.wake_drain(self.inst, self.n, once=True)
                self.now += 121
        self.assertEqual(opener.open.call_count, 2)
        self.assertTrue(pending.exists())
        self.assertFalse((self.directory / 'turn.json').exists())

    def test_pre_send_reason_failure_never_claims_an_unknown_send(self):
        pending = self.enqueue()
        with mock.patch.object(self.m, 'wake_reason_summary', side_effect=ValueError('fixture preflight')):
            self.drain().assert_not_called()
        self.assertTrue(pending.exists())
        self.assertFalse((self.directory / 'turn.json').exists())
        self.now += 121
        self.drain().assert_called_once()

    def test_disabled_child_cannot_leak_through_enabled_root(self):
        self.b(self.r, 'set', 'root', 'coordinator=fixture-root', 'wake=fixture-root-send')
        self.configure('coordinator=', 'wake=', 'wake_enabled=off')
        root = self.inst.node('root')
        self.assertTrue(self.m.request_wake(self.inst, self.n, 'fixture-child-off', items=[
            {'id': 'e' * 64, 'kind': 'inbox'}], explicit=True))
        directory = self.m.wake_directory(self.inst, root)
        with mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            self.m.wake_drain(self.inst, root, once=True)
            send.assert_not_called()
        self.assertTrue(list(directory.glob('*.pending')))
        with mock.patch.dict(os.environ, {'T3CODE_THREAD_ID': 'fixture-root', 'CODEX_THREAD_ID': 'fixture-provider-session', 'CLAUDE_CODE_SESSION_ID': ''}):
            self.assertIsNone(self.m.wake_turn_consume(self.inst, root))
        self.assertTrue(list(directory.glob('*.pending')))

    def test_peer_terminal_claim_never_holds_an_independent_question(self):
        self.b(self.r, 'set', 'root', 'coordinator=fixture-root', 'wake=fixture-root-send')
        self.refresh()
        root = self.inst.node('root')
        terminal = {'id': 'a' * 64, 'kind': 'machine-exited'}
        question = {'id': 'b' * 64, 'kind': 'inbox'}
        self.m.request_wake(self.inst, self.n, 'fixture-owner-terminal', items=[terminal], explicit=True)
        self.drain().assert_called_once()
        self.m.request_wake(self.inst, root, 'fixture-root-mixed', items=[terminal, question], explicit=True)
        with mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            self.m.wake_drain(self.inst, root, once=True)
            send.assert_called_once()
        row = self.m.wake_history(self.m.wake_directory(self.inst, root))[-1]
        self.assertEqual(row['items'], [question])
        self.assertFalse(self.m.wake_terminal_delivered(self.inst, terminal['id']))

    def test_transport_has_fixed_counts_without_local_exit_reason(self):
        terminal = {'id': 'd' * 64, 'kind': 'machine-exited'}
        self.m.request_wake(self.inst, self.n, 'fixture-private', items=[terminal], explicit=True)
        with mock.patch.object(self.m, 'machine_exit_notice', return_value='PRIVATE-FIXTURE-EXIT'):
            send = self.drain()
        self.assertEqual(send.call_args.kwargs['reason'], '1 changed item')
        self.assertNotIn('PRIVATE-FIXTURE-EXIT', (self.directory / 'history.json').read_text())

    def test_outstanding_episode_prevents_provider_pull_and_revalidation(self):
        self.enqueue()
        self.drain()
        self.now += 121
        with mock.patch.object(self.m, 'provider_pull_on_wake') as pull, \
                mock.patch.object(self.m, 'wake_actionable_pending') as scan:
            self.drain().assert_not_called()
            pull.assert_not_called()
            scan.assert_not_called()

    def test_wake_cli_reports_enqueue_failure(self):
        args = argparse.Namespace(id='app', drain=False, retry=False, reason='fixture')
        with mock.patch.object(self.m, 'load', return_value=self.inst), \
                mock.patch.object(self.m, 'request_wake', return_value=False), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                self.m.cmd_wake(args)
        self.assertNotEqual(result.exception.code, 0)

    def test_child_disabled_after_acceptance_does_not_hold_root_forever(self):
        self.b(self.r, 'set', 'root', 'coordinator=fixture-root', 'wake=fixture-root-send')
        self.configure('coordinator=', 'wake=')
        root = self.inst.node('root')
        self.m.request_wake(self.inst, self.n, 'fixture-child', items=[{'id': 'c' * 64, 'kind': 'inbox'}], explicit=True)
        directory = self.m.wake_directory(self.inst, root)
        with mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            self.m.wake_drain(self.inst, root, once=True)
            send.assert_called_once()
        child_paths = list(directory.glob('*.pending'))
        self.configure('wake_enabled=off')
        self.due('root', 'fixture-root')
        self.assertTrue(all(p.exists() and not p.with_suffix('.sent').exists() for p in child_paths))
        root = self.inst.node('root')
        self.m.request_wake(self.inst, root, 'fixture-independent', items=[{'id': 'e' * 64, 'kind': 'inbox'}], explicit=True)
        self.now += 121
        with mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            self.m.wake_drain(self.inst, root, once=True)
            send.assert_called_once()
        self.assertEqual(self.m.wake_history(directory)[-1]['items'], [{'id': 'e' * 64, 'kind': 'inbox'}])
        self.assertTrue(all(p.exists() for p in child_paths))

    def test_old_selected_session_cannot_acknowledge_a_replacement_coordinator(self):
        self.configure('coordinator_sessions=fixture-old-session')
        self.configure('coordinator=fixture-next', 'wake=fixture-next-send')
        pending = self.enqueue()
        self.drain()
        self.due('app', 'fixture-old-session', host=False)
        self.assertTrue(pending.exists())
        self.configure('coordinator_sessions=fixture-next-session')
        self.due('app', 'fixture-next-session', host=False)
        self.assertFalse(pending.exists())

    def test_consumption_between_terminal_revalidation_and_peer_read_does_not_duplicate(self):
        self.b(self.r, 'set', 'root', 'coordinator=fixture-root', 'wake=fixture-root-send')
        self.refresh()
        root = self.inst.node('root')
        ref = {'id': 'f' * 64, 'kind': 'machine-exited'}
        self.m.request_wake(self.inst, self.n, 'fixture-owner', items=[ref], explicit=True)
        self.drain()
        self.m.request_wake(self.inst, root, 'fixture-root', items=[ref], explicit=True)
        original = self.m.wake_terminal_claims
        reads = []
        def claims(inst, node):
            reads.append(True)
            if len(reads) == 1:
                return set(), False  # first observation precedes the peer reservation
            with mock.patch.object(self.m, 'wake_terminal_claims', side_effect=original):
                self.assertIsNotNone(self.m.wake_turn_consume(self.inst, self.n))
            return original(inst, node)
        with mock.patch.object(self.m, 'wake_terminal_claims', side_effect=claims), \
                mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            self.m.wake_drain(self.inst, root, once=True)
            send.assert_not_called()
        self.assertGreaterEqual(len(reads), 2)
        self.assertTrue(self.m.wake_terminal_delivered(self.inst, ref['id']))

    def test_native_host_identity_is_independent_of_runtime_and_legacy_session_state(self):
        for number, recorded in enumerate((None, 'fixture-other-session', 'fixture-provider-session'), 1):
            if recorded:
                self.configure('coordinator_sessions=' + recorded)
                self.inst.local_of('app').pop('coordinator_session_binding', None)
                self.inst.save_local()
                self.refresh()
            pending = self.enqueue(number)
            self.drain()
            self.due('app')  # native host id matches; provider session deliberately differs
            self.assertFalse(pending.exists())
            self.now += 121

    def test_foreign_native_host_cannot_fall_back_to_a_matching_runtime_session(self):
        self.configure('coordinator_sessions=fixture-provider-session')
        pending = self.enqueue()
        self.drain()
        self.due('app', 'fixture-foreign-host')
        self.assertTrue(pending.exists())
        self.due('app')
        self.assertFalse(pending.exists())

    def test_session_binding_is_independent_of_settings_argument_order(self):
        self.configure('coordinator_sessions=fixture-next-session', 'coordinator=fixture-next', 'wake=fixture-next-send')
        pending = self.enqueue()
        self.drain()
        self.due('app', 'fixture-next-session', host=False)
        self.assertFalse(pending.exists())

    def test_empty_corrupt_receipt_is_quarantined_by_bound_due(self):
        self.enqueue()
        self.drain()
        self.due('app')
        path = self.directory / 'turn.json'
        path.write_bytes(b'{fixture-empty-corrupt')
        self.due('app')
        self.assertFalse(path.exists())
        self.assertTrue(any(p.read_bytes() == b'{fixture-empty-corrupt' for p in self.directory.glob('turn-retired-*.json')))
        self.assertFalse(self.m.wake_terminal_claims(self.inst, self.inst.node('root'))[1])

    def test_archived_peer_receipt_does_not_hold_active_terminal_work(self):
        with mock.patch.object(self.inst, 'nodes', return_value=[{'id': 'retired-fixture', 'archived': True}]), \
                mock.patch.object(self.m, 'wake_turn_read', side_effect=AssertionError('archived peer read')):
            self.assertEqual(self.m.wake_terminal_claims(self.inst, self.n), (set(), False))

    def test_route_provenance_digests_do_not_change_train_execution_inputs(self):
        before = self.m.train_configuration(self.n)
        self.configure('coordinator_sessions=fixture-provider-session')
        self.inst.local_of('app')['wake_verified_binding'] = 'e' * 64
        self.inst.save_local()
        self.refresh()
        self.assertEqual(self.m.train_configuration(self.n), before)
        self.configure('verify=fixture-changed-check')
        self.assertNotEqual(self.m.train_configuration(self.n), before)

    def test_held_disabled_source_does_not_pull_provider_on_each_pass(self):
        self.b(self.r, 'set', 'root', 'coordinator=fixture-root', 'wake=fixture-root-send')
        self.configure('coordinator=', 'wake=', 'wake_enabled=off')
        self.m.request_wake(self.inst, self.n, 'fixture-held-source', items=[{'id': '9' * 64, 'kind': 'inbox'}], explicit=True)
        with mock.patch.object(self.m, 'provider_pull_on_wake') as pull:
            for _ in range(3):
                self.m.wake_drain(self.inst, self.inst.node('root'), once=True)
            pull.assert_not_called()

    def test_arrival_during_flush_restarts_a_drain_after_consumption(self):
        first = self.enqueue()
        self.drain()
        fixture = self
        class ArrivalOutput(io.StringIO):
            fired = False
            def flush(self):
                if not self.fired:
                    self.fired = True
                    fixture.enqueue(2)
                return super().flush()
        self.m.wake_start.reset_mock()
        self.due('app', output=ArrivalOutput())
        self.assertFalse(first.exists())
        self.assertEqual(len(list(self.directory.glob('*.pending'))), 1)
        self.assertEqual(self.m.wake_start.call_count, 2)  # arrival's held start, then post-consumption continuation

    def test_missing_transport_is_a_distinct_configuration_result(self):
        self.configure('wake=')
        args = argparse.Namespace(id='app', drain=False, retry=False, reason='fixture')
        with mock.patch.object(self.m, 'load', return_value=self.inst), contextlib.redirect_stderr(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as result:
                self.m.cmd_wake(args)
        self.assertEqual(result.exception.code, 2)
        self.assertIn('configure before retrying', output.getvalue())

    def test_disabled_inherited_source_notice_names_the_source(self):
        self.b(self.r, 'set', 'root', 'coordinator=fixture-root', 'wake=fixture-root-send')
        self.configure('coordinator=', 'wake=', 'wake_enabled=off')
        out = self.due('root', 'fixture-root').getvalue()
        self.assertIn('app: wake: wake delivery explicitly disabled', out)
        self.assertNotIn('root: wake: wake delivery explicitly disabled', out)
