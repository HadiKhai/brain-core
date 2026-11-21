"""Made-up worker commands: four kinds, optional context and direct peer briefs."""
import re
import datetime
import json

from scenario import consume_fixture_wake, Scenario, CORE
import importlib
import core_loader

_fixture = core_loader.SourceFileLoader('message_envelope_fixture', str(CORE / 'bin/brain')).load_module()
_envelope = importlib.import_module(_fixture._owners['worker_message_guard'].__package__ + '.message_envelope')
marked_message, unmarked_message = _envelope.marked_message, _envelope.unmarked_message

AGENT_MARKER = "Agent message: sent by another agent, not the operator."
AGENT_HEADER = AGENT_MARKER + "\n\n"


class WorkerMessages(Scenario):
    def assert_framed(self, output, body, trailer='NOT SENT: use the host send with mode queue'):
        frame = output.removesuffix('\n' + trailer + '\n') if trailer else output.removesuffix('\n')
        self.assertEqual(unmarked_message(frame, AGENT_MARKER), body)
        meta = json.loads(frame.split('\n\n')[1].removeprefix('Brain-Envelope: '))
        self.assertTrue(_envelope.valid_stamp(meta['sent_at']))
        self.assertEqual(output, frame + ('\n' + trailer if trailer else '') + '\n')

    def job(self):
        root = self.init("alice")
        home = self.repo(self.tmp / "app")
        self.b(root, "new", "a", "--title", "App", "--home", str(home))
        self.b(root, "wt", "add", "a", "job/sample")
        return root, home

    def test_prepared_messages_explicitly_say_they_were_not_sent(self):
        root, _ = self.job()
        for kind in ("start", "done", "blocked", "question"):
            out = self.b(root, "msg", "a", kind, "Synthetic message", "--branch", "job/sample")
            self.assertIn("NOT SENT: use the host send", out)
            self.assertIn("with mode queue", out)
            self.assertTrue(out.startswith(AGENT_HEADER + "Brain-Envelope: "), out)

    def test_formatter_and_briefs(self):
        root, _ = self.job()
        for kind in ("start", "done", "blocked", "question"):
            out = self.b(root, "msg", "a", kind, "One\nline", "--branch", "job/sample", "--report", "~/reports/example.md")
            self.assert_framed(out, kind + ": One line [branch: job/sample; report: ~/reports/example.md]")
        out = self.b(root, "msg", "a", "question", "What next?", "--branch", "job/sample")
        self.assertIn("job-handbacks/", out)
        self.b(root, "msg", "a", "progress", "Working", "--branch", "job/sample", ok=False)
        for args in (("--branch", "job/sample"), ("--small",), ()):
            brief = self.b(root, "brief", "a", *args, "--task", "Build a sample.")
            protocol = [line for line in brief.splitlines() if "Send the coordinator only" in line]
            self.assertEqual(len(protocol), 1)
            self.assertIn("peer thread", protocol[0])
            self.assertNotRegex(brief, r"(?:done|blocked|question|failed)\|")
        self.assertIn("start/done/blocked/question", self.b(root, "handback", "--summary", "Synthetic result", "--help"))

    def test_every_brief_formatter_usage_works_from_its_run_folder(self):
        root, _ = self.job()
        for options in (("--branch", "job/sample"), ("--small",), ()):
            brief = self.b(root, "brief", "a", *options, "--task", "Build a sample.")
            self.assertIn('brain msg <node> <kind> "<one line>"', brief)
            run_in = re.search(r"^Run in: (.+?)(?:  ·|$)", brief, re.M).group(1)
            folder = run_in.replace("~", str(self.tmp), 1)
            for kind in ("start", "done", "blocked", "question"):
                output = self.run_(["msg", "a", kind, "Made-up result"], cwd=folder, brain=CORE / "bin/brain")
                self.assertTrue(output.startswith(AGENT_HEADER + "Brain-Envelope: "), output)
                if options == ("--branch", "job/sample"):
                    self.assertIn("[branch: job/sample; report:", output)
                else:
                    self.assert_framed(output, kind + ": Made-up result [branch: main]")
        # A generic worker may be outside Git entirely, with no ledger job or report.
        plain = self.tmp / "plain"
        plain.mkdir()
        for kind in ("start", "done", "blocked", "question"):
            output = self.run_(["msg", "a", kind, "Made-up result"], cwd=plain, brain=CORE / "bin/brain")
            self.assert_framed(output, kind + ": Made-up result")
        self.assert_framed(self.b(root, "msg", "a", "done", "Result", "--branch", "other"),
                           "done: Result [branch: other]")

    def test_freeform_agent_formatter_preserves_bytes_and_never_sends(self):
        root = self.init('alice')
        body = 'Keep original evidence.\n  Preserve indentation and spaces.  '
        path = self.tmp / 'message.txt'
        path.write_text(body)
        expected = self.b(root, 'agent-message', '--message-file', str(path))
        self.assert_framed(expected, body, trailer=None)
        self.assert_framed(self.b(root, 'agent-message', body), body, trailer=None)
        self.assert_framed(self.b(root, 'agent-message', expected[:-1]), body, trailer=None)
        self.b(root, 'agent-message', ok=False)
        self.b(root, 'agent-message', '', ok=False)
        self.b(root, 'agent-message', body, '--message-file', str(path), ok=False)

    def test_generated_worker_and_reviewer_send_lines_require_formatter(self):
        import core_loader
        root = self.init('alice')
        m = core_loader.SourceFileLoader('send_brief_fixture', str(CORE / 'bin/brain')).load_module()
        inst = m.Instance(root)
        node = inst.node('root')
        for who in ('worker', 'reviewer'):
            for branch in (None, 'job/sample'):
                lines = m.handback_lines(inst, node, to='fixture-parent', who=who, branch=branch)
                sends = [line for line in lines if 'host `send`' in line]
                self.assertTrue(sends, (who, branch))
                for line in sends:
                    with self.subTest(who=who, branch=branch, line=line):
                        self.assertIn('brain msg root ', line)
                        self.assertIn('unchanged', line)

    def test_single_newline_marker_body_gets_canonical_envelope(self):
        import core_loader
        m = core_loader.SourceFileLoader('marker_envelope_fixture', str(CORE / 'bin/brain')).load_module()
        messages = m._owners['worker_message_guard']
        body = AGENT_MARKER + '\ndone: Synthetic result'
        prepared = messages.agent_message(body)
        self.assertTrue(prepared.startswith(AGENT_HEADER))
        self.assertEqual(unmarked_message(prepared, AGENT_MARKER), body)
        self.assertEqual(unmarked_message(messages.agent_message(prepared), AGENT_MARKER), body)

    def test_peer_threads_and_coordinator_duty(self):
        root, _ = self.job()
        line = "Coordinate shared-commit changes directly with thread `thread-peer`."
        for options in (("--branch", "job/sample"), ("--small",), ()):
            args = ("brief", "a", *options, "--task", "Build a sample.")
            self.assertNotIn(line, self.b(root, *args))
            brief = self.b(root, *args, "--peer", "thread-peer")
            self.assertIn(line, brief)
            self.assertEqual(brief.count("thread-peer"), 1)
        coordinator = (CORE / "roles/coordinator.md").read_text().split("\n\n")[1]
        self.assertIn("Expect only start, done, blocked or question from workers", coordinator)
        self.assertIn("Request no progress notes, receipts or acknowledgements", coordinator)
        self.assertIn("name the peer thread in both briefs", coordinator)

    def test_contract_brief_has_one_message_protocol(self):
        root, _ = self.job()
        contract = self.tmp / "contract.md"
        contract.write_text("mode: untimed\n\n" + "".join("## " + field + "\nSynthetic scope.\n\n" for field in
                                   ("Outcome", "Authorized", "Discretion", "Stop when", "Needs consent", "Done means")))
        self.b(root, "jobs", "a", "--branch", "job/sample", "--contract-file", str(contract))
        brief = self.b(root, "brief", "a", "--branch", "job/sample", "--task", "Build a sample.")
        protocol = [line for line in brief.splitlines() if "Send the coordinator only" in line]
        self.assertEqual(len(protocol), 1)
        rest = brief.replace(protocol[0], "")
        self.assertNotRegex(rest, r"Report starts, results and decisions|not acknowledgements|done\|failed|done\|blocked")
        self.assertNotIn("start, done, blocked or question", rest)
        self.assertIn("Record results and decisions in the report", rest)
        adapter = (CORE / "adapters/hosts/t3.md").read_text()
        self.assertNotIn("when it finishes, fails, gets blocked or has a question", adapter)

    def test_handback_formats_failure_and_question_with_report(self):
        root, _ = self.job()
        for state, kind in (("blocked", "blocked"), ("question", "question"), ("failed", "blocked")):
            out = self.b(root, "handback", "--summary", "Synthetic result", "--question", "Synthetic choice?", "a", "--branch", "job/sample", "--state", state,
                         "--report", "~/reports/sample.md", "--lessons", "none: made-up fixture", '--dissection', str(self.dissection_report()))
            self.assertIn(kind + ": coordinator can see it with brain due [branch: job/sample; report: ~/reports/sample.md]", out)


class ClockStampedMessages(Scenario):
    fixture_init_in_process = True

    def setUp(self):
        super().setUp()
        import os
        from unittest import mock
        patch = mock.patch.dict(os.environ, self.env)
        patch.start()
        self.addCleanup(patch.stop)
        self.root = self.init('oak')
        path = self.root / 'instance.md'
        path.write_text(path.read_text().replace('timezone: UTC', 'timezone: Asia/Kathmandu'))
        self.m = core_loader.SourceFileLoader('stamp_fixture', str(CORE / 'bin/brain')).load_module()
        self.inst = self.m.Instance(self.root)

    def metadata(self, text):
        return json.loads(text.split('\n\n')[1].removeprefix('Brain-Envelope: '))

    def hook(self, tool, args):
        import contextlib, io, sys
        from unittest import mock
        data = {'cwd': str(self.root), 'session_id': 'fixture-coordinator',
                'tool_name': tool, 'tool_input': args}
        with mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(data))), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.m.cmd_hook_pretool(None)
        return json.loads(out.getvalue())['hookSpecificOutput'] if out.getvalue() else None

    def test_cli_stamps_instance_clock_preserves_typed_label_and_bytes(self):
        import subprocess, sys
        body = '23:42 typed label\r\n  Exact café evidence.  '
        before = self.inst.now_dt().replace(microsecond=0)
        for args in [('agent-message', body), ('msg', 'root', 'question', body)]:
            # The scenario text runner uses universal-newline decoding. Observe
            # actual stdout bytes so CRLF evidence is checked without that transform.
            result = subprocess.run([sys.executable, str(CORE / 'bin/brain'), *args],
                                    cwd=self.root, env=self.env, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = result.stdout.decode('utf-8')
            stamp = datetime.datetime.fromisoformat(self.metadata(output)['sent_at'])
            self.assertEqual(stamp.utcoffset(), datetime.timedelta(hours=5, minutes=45))
            self.assertLessEqual(before, stamp)
            self.assertLessEqual(stamp, self.inst.now_dt())
            if args[0] == 'agent-message':
                self.assertEqual(unmarked_message(output[:-1], AGENT_MARKER), body)
            else:
                self.assertIn('23:42 typed label', output)

    def test_hook_refreshes_legacy_and_stamped_sends_without_granting_permission(self):
        body = '23:42 typed label\n  Evidence stays exact.  '
        old = marked_message(body, AGENT_MARKER, sent_at='2000-01-01T00:00:00+00:00')
        before = self.inst.now_dt().replace(microsecond=0)
        for tool in ('t3_thread_send', 'mcp__t3-code__t3_thread_send', 'mcp__t3_code__t3_thread_send'):
            for text in (body, marked_message(body, AGENT_MARKER), old):
                args = {'threadId': 'fixture-child', 'mode': 'queue', 'message': text, 'clientRequestId': 'stable'}
                output = self.hook(tool, args)
                self.assertEqual(set(output), {'hookEventName', 'updatedInput'})
                updated = output['updatedInput']
                self.assertEqual({k: v for k, v in updated.items() if k != 'message'},
                                 {k: v for k, v in args.items() if k != 'message'})
                self.assertEqual(unmarked_message(updated['message'], AGENT_MARKER), body)
                stamp = datetime.datetime.fromisoformat(self.metadata(updated['message'])['sent_at'])
                self.assertLessEqual(before, stamp)
                self.assertLessEqual(stamp, self.inst.now_dt())
                self.assertEqual(args['message'], text)
        self.assertIsNone(self.hook('t3_thread_read', {'threadId': 'fixture-child'}))
        self.assertIsNone(self.hook('t3_thread_send', {'threadId': 'fixture-child', 'message': None}))

    def test_denial_is_marked_and_cannot_become_a_stamped_allow(self):
        (self.root / 'AGENTS.md').write_text('## Forbidden actions\n- tool: *t3_thread_send\n')
        output = self.hook('t3_thread_send', {'threadId': 'fixture-child', 'message': 'ordered relay'})
        self.assertEqual(output['permissionDecision'], 'deny')
        self.assertTrue(output['permissionDecisionReason'].startswith('brain-hook:pretool: '))
        self.assertNotIn('updatedInput', output)

    def test_routine_send_to_busy_coordinator_is_queued_without_cancelling_active_call(self):
        import os, sys, subprocess
        from unittest import mock
        rpc = self.m._owners['HostRPCError']
        active_call = {'id': 'fixture-mid-tool-call', 'state': 'running'}
        queued, cancellations = [], []
        def busy_bridge(command, **kwargs):
            arguments = json.loads(command[-1])
            if arguments.get('mode', 'auto') != 'queue':
                cancellations.append(active_call['id'])
                active_call['state'] = 'cancelled'
                delivery = 'steered'
            else:
                queued.append(arguments)
                delivery = 'queued'
            return subprocess.CompletedProcess(command, 0, json.dumps({'delivery': delivery}), '')
        with mock.patch.dict(os.environ, {'T3_ACP_MCP_NODE': sys.executable,
                                          'T3_ACP_MCP_ENTRYPOINT': ''}), \
                mock.patch.object(rpc.subprocess, 'run', side_effect=busy_bridge):
            for kind in ('done', 'blocked', 'question'):
                for mode in (None, 'auto', 'queue'):
                    args = {'threadId': 'fixture-coordinator', 'message': kind + ': urgent STOP steer now',
                            'clientRequestId': kind + '-' + str(mode)}
                    if mode is not None:
                        args['mode'] = mode
                    updated = self.hook('mcp__t3_code__t3_thread_send', args)['updatedInput']
                    # Check the hook separately: the native bridge must not mask a hook regression.
                    self.assertEqual(updated['mode'], 'queue')
                    self.assertNotIn('permissionDecision', updated)
                    self.assertEqual(rpc.call('t3_thread_send', updated)['delivery'], 'queued')
                    self.assertEqual(unmarked_message(queued[-1]['message'], AGENT_MARKER), args['message'])
                    self.assertEqual(queued[-1]['clientRequestId'], args['clientRequestId'])
        self.assertEqual(active_call, {'id': 'fixture-mid-tool-call', 'state': 'running'})
        self.assertEqual(cancellations, [])
        self.assertEqual(len(queued), len(('done', 'blocked', 'question')) * len((None, 'auto', 'queue')))
        # Negative control: the same busy host cancels on the old default-auto payload.
        busy_bridge(['fixture', json.dumps({'message': 'done: result', 'mode': 'auto'})])
        self.assertEqual(cancellations, ['fixture-mid-tool-call'])

    def test_hook_keeps_only_explicit_structural_interrupt_choices(self):
        for mode in ('steer', 'restart'):
            args = {'threadId': 'fixture-coordinator', 'mode': mode, 'message': 'question: ordinary words'}
            updated = self.hook('t3_thread_send', args)['updatedInput']
            self.assertEqual(updated['mode'], mode)
            self.assertEqual(unmarked_message(updated['message'], AGENT_MARKER), args['message'])

    def test_envelope_compatibility_integrity_and_uncovered_operator_remainder(self):
        body = 'Evidence 🐙\n'
        for stamp in (None, '2026-11-20T09:00:01+05:45'):
            framed = marked_message(body, AGENT_MARKER, sent_at=stamp)
            self.assertEqual(unmarked_message(framed, AGENT_MARKER), body)
            self.assertTrue(_envelope.fully_marked(framed))
            self.assertFalse(_envelope.fully_marked(framed + 'Operator remainder'))
            self.assertFalse(_envelope.fully_marked(framed.replace('Evidence', 'Changed!')))
        legacy = marked_message(body, AGENT_MARKER)
        invalid = legacy.replace('"bytes":', '"sent_at": "23:42", "bytes":')
        self.assertFalse(_envelope.fully_marked(invalid))
        with self.assertRaises(ValueError):
            marked_message(body, AGENT_MARKER, sent_at='2026-11-20T09:00:00')

    def test_notify_uses_receipt_clock_even_when_publication_crosses_a_minute(self):
        from unittest import mock
        receipt = '2026-11-20 09:59 +05:45'
        with mock.patch.object(self.inst, 'now', side_effect=[receipt, '2026-11-20 10:00 +05:45']):
            record = self.m.communication_record(self.inst, self.inst.node('root'), 'fyi',
                                                 '23:42 typed label stays', 'fixture-clock')
        self.assertEqual(record['at'], receipt)
        self.assertIn('- ' + receipt + ' from notification: fyi: 23:42 typed label stays',
                      (self.root / 'inbox.md').read_text())


class AutomatedBriefAttribution(Scenario):
    def test_generated_wake_schedule_and_delegated_brief_start_with_marker(self):
        import core_loader
        import os
        from unittest import mock
        root = self.init('alice')
        self.b(root, 'set', 'root', 'coordinator=fixture-thread')
        m = core_loader.SourceFileLoader('automated_fixture', str(CORE / 'bin/brain')).load_module()
        inst = m.Instance(root)
        marker = 'Automated turn: no operator present; cancellations are host events.'
        with mock.patch.dict(os.environ, self.env):
            self.assertTrue(m.wake_setup_plan(inst, inst.node('root'))['host_call']['prompt'].startswith(marker))
            schedule = m.prepare_schedule(inst, 'fixture', 'hourly', 'root', 'Inspect current work')
            self.assertIn('brief: ' + marker, schedule)
            (root / 'schedules.md').write_text(schedule)
            stored = m.read_schedules(inst)[0]['brief']
            self.assertTrue(_envelope.fully_marked(stored))
            self.assertFalse(_envelope.fully_marked(stored + '\nOperator instruction.'))
            text = m.checked_brief(inst.node('root'), 'Role: worker\nSynthetic task', inputs=[])
            self.assertTrue(text.startswith(marker))
            self.assertTrue(_envelope.fully_marked(text))
            self.assertEqual(m.checked_brief(inst.node('root'), text, inputs=[]).count(marker), 1)

    def test_roles_attribute_automated_cancellation_to_host(self):
        for name in ('coordinator', 'worker'):
            text = (CORE / 'roles' / (name + '.md')).read_text()
            self.assertIn('A cancellation with no operator message in the turn is a host event', text)
            self.assertIn('retry once or use another route', text)


class TypedCommunication(Scenario):
    def setUp(self):
        super().setUp()
        import core_loader
        import os
        from unittest import mock
        p = mock.patch.dict(os.environ, self.env)
        p.start()
        self.addCleanup(p.stop)
        self.m = core_loader.SourceFileLoader('typed_comms', str(CORE / 'bin/brain')).load_module()
        self.root = self.init('alice')
        self.b(self.root, 'set', 'root', 'coordinator=fixture-root', 'wake=fixture-send')
        self.inst = self.m.Instance(self.root)

    def test_absent_store_reconciliation_is_a_byte_identical_observation(self):
        communication = rehearse = self.m
        from unittest import mock
        path = self.root / '.brain/communications.json'
        self.assertFalse(path.exists())
        before = rehearse.snapshot(self.root)
        with mock.patch.object(communication, 'communication_store',
                               side_effect=AssertionError('absent observation took a write lock')):
            communication.communication_reconcile(self.inst)
        self.assertEqual(before, rehearse.snapshot(self.root))

    def test_existing_store_still_locks_and_retains_invalid_bytes(self):
        communication = self.m
        path = self.root / '.brain/communications.json'
        path.write_text('{"version":1,"questions":{},"notices":{}}')
        communication.communication_reconcile(self.inst)
        self.assertTrue((self.root / '.brain/communications.lock').is_file())
        path.write_text('{"version":1,"questions":[]}')
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            communication.communication_reconcile(self.inst)
        self.assertEqual(before, path.read_bytes())

    def spec(self, action='routine'):
        return {'options': [{'id': 'continue', 'label': 'Continue', 'action': action},
                            {'id': 'wait', 'label': 'Wait', 'action': 'routine'}],
                'recommendation': 'continue', 'default': 'continue',
                'deadline': '2020-01-01T00:00:00+00:00'}

    def quiet_recovery_child(self):
        self.b(self.root, 'new', 'child', '--title', 'Synthetic child')
        self.b(self.root, 'set', 'child', 'coordinator=fixture-child', 'wake=fixture-send')
        self.inst = self.m.Instance(self.root)
        self.inst.local_of('root')['message_policy'] = {'quiet_start': '00:00', 'quiet_end': '07:00'}
        self.inst.save_local()
        child = self.inst.node('child')
        self.assertEqual(self.m.wake_target(self.inst, child)[0]['id'], 'child')
        return child

    def test_interrupted_notice_retry_defers_under_current_quiet_policy(self):
        import datetime
        from unittest import mock
        child = self.quiet_recovery_child()
        inbox = child['dir'] / 'inbox.md'
        before = inbox.read_bytes() if inbox.exists() else None
        outside = datetime.datetime.fromisoformat('2026-01-01T23:59:00+00:00')
        with mock.patch.object(self.m.Instance, 'now_dt', return_value=outside) as clock, \
                mock.patch.object(self.m, 'request_wake') as send:
            with mock.patch.object(self.m, 'inbox_append', side_effect=OSError('fixture receipt interruption')):
                with self.assertRaisesRegex(OSError, 'fixture receipt interruption'):
                    self.m.communication_record(self.inst, child, 'fyi', 'Retained update', 'recovery')
            original = next(iter(self.m.communication_read(self.inst)['notices'].values()))
            self.assertNotIn('deferred', original)
            send.assert_not_called()
            clock.return_value = datetime.datetime.fromisoformat('2026-01-02T00:01:00+00:00')
            retry = self.m.communication_record(self.inst, child, 'fyi', 'Retained update', 'recovery')
            send.assert_not_called()
            self.assertEqual(inbox.read_bytes() if inbox.exists() else None, before)
            self.assertEqual(retry, {**original, 'deferred': True})
            self.assertEqual(self.m.communication_read(self.inst)['notices'][original['id']], retry)
            self.assertEqual(self.m.morning_brief(self.inst, child)['state'], 'quiet')
            clock.return_value = datetime.datetime.fromisoformat('2026-01-02T07:00:00+00:00')
            self.assertEqual(self.m.morning_brief(self.inst, child)['items'], [original['id']])
            self.assertEqual(self.m.morning_brief(self.m.Instance(self.root), child)['state'], 'already-published')
            self.assertEqual(self.m.communication_record(self.inst, child, 'fyi', 'Retained update', 'recovery'), retry)
            self.assertEqual(inbox.read_text().count('Retained update'), 1)
            self.assertEqual(inbox.read_text().count('<!-- morning-brief:'), 1)
            self.assertNotIn('<!-- communication:', inbox.read_text())
            send.assert_not_called()

    def test_published_notice_quiet_retry_does_not_become_morning_work(self):
        import datetime
        from unittest import mock
        child = self.quiet_recovery_child()
        inbox = child['dir'] / 'inbox.md'
        with mock.patch.object(self.m.Instance, 'now_dt', return_value=datetime.datetime.fromisoformat(
                '2026-01-01T23:59:00+00:00')) as clock, mock.patch.object(self.m, 'request_wake') as send:
            original = self.m.communication_record(self.inst, child, 'fyi', 'Already delivered', 'delivered')
            self.assertNotIn('deferred', original)
            send.assert_called_once()
            send.reset_mock()
            before = inbox.read_bytes()
            clock.return_value = datetime.datetime.fromisoformat('2026-01-02T00:01:00+00:00')
            retry = self.m.communication_record(self.inst, child, 'fyi', 'Already delivered', 'delivered')
            send.assert_not_called()
            self.assertEqual(inbox.read_bytes(), before)
            clock.return_value = datetime.datetime.fromisoformat('2026-01-02T07:00:00+00:00')
            self.assertEqual(self.m.morning_items(self.inst, [child]), [])
            self.assertEqual(self.m.morning_brief(self.inst, child)['state'], 'empty')
            self.assertEqual(self.m.operator_brief(self.inst)['state'], 'empty')
            self.assertEqual(inbox.read_bytes(), before)
            self.assertEqual(inbox.read_text().count('Already delivered'), 1)
            self.assertEqual(retry, original)
            self.assertEqual(self.m.communication_read(self.inst)['notices'][original['id']], original)
            send.assert_not_called()

    def test_quiet_retry_refuses_unknown_publication_after_collision_check(self):
        import datetime
        from unittest import mock
        child = self.quiet_recovery_child()
        inbox = child['dir'] / 'inbox.md'
        with mock.patch.object(self.m.Instance, 'now_dt', return_value=datetime.datetime.fromisoformat(
                '2026-01-01T23:59:00+00:00')) as clock, mock.patch.object(self.m, 'request_wake') as send:
            self.m.communication_record(self.inst, child, 'fyi', 'Delivered update', 'unknown-publication')
            send.reset_mock()
            store = self.root / '.brain/communications.json'
            before = store.read_bytes()
            inbox.unlink()
            inbox.mkdir()  # Neither published nor unpublished can be established.
            clock.return_value = datetime.datetime.fromisoformat('2026-01-02T00:01:00+00:00')
            with self.assertRaisesRegex(ValueError, 'stable notification key already names different content'):
                self.m.communication_record(self.inst, child, 'fyi', 'Changed update', 'unknown-publication')
            with self.assertRaises((OSError, ValueError)):
                self.m.communication_record(self.inst, child, 'fyi', 'Delivered update', 'unknown-publication')
            self.assertEqual(store.read_bytes(), before)
            self.assertEqual(list(inbox.iterdir()), [])
            send.assert_not_called()

    def test_direct_publisher_rechecks_deferral_after_saving_receipt(self):
        import contextlib
        import datetime
        from unittest import mock
        child = self.quiet_recovery_child()
        inbox = child['dir'] / 'inbox.md'
        before = inbox.read_bytes() if inbox.exists() else None
        store = self.m.communication_store
        interrupted = False
        with mock.patch.object(self.m.Instance, 'now_dt', return_value=datetime.datetime.fromisoformat(
                '2026-01-01T23:59:00+00:00')) as clock, mock.patch.object(self.m, 'request_wake') as send:
            @contextlib.contextmanager
            def quiet_retry_after_receipt(inst):
                nonlocal interrupted
                with store(inst) as data:
                    yield data
                if not interrupted:
                    interrupted = True
                    clock.return_value = datetime.datetime.fromisoformat('2026-01-02T00:01:00+00:00')
                    self.m.communication_record(inst, child, 'fyi', 'Racing update', 'racing')
            with mock.patch.object(self.m, 'communication_store', quiet_retry_after_receipt):
                record = self.m.communication_record(self.inst, child, 'fyi', 'Racing update', 'racing')
            self.assertTrue(record['deferred'])
            self.assertEqual(inbox.read_bytes() if inbox.exists() else None, before)
            send.assert_not_called()
            clock.return_value = datetime.datetime.fromisoformat('2026-01-02T07:00:00+00:00')
            self.assertEqual(self.m.morning_brief(self.inst, child)['items'], [record['id']])
            self.assertEqual(inbox.read_text().count('Racing update'), 1)
            send.assert_not_called()

    def test_interrupted_question_retry_still_bypasses_quiet_policy(self):
        import datetime
        from unittest import mock
        child = self.quiet_recovery_child()
        with mock.patch.object(self.m.Instance, 'now_dt', return_value=datetime.datetime.fromisoformat(
                '2026-01-01T23:59:00+00:00')) as clock, mock.patch.object(self.m, 'request_wake') as send:
            with mock.patch.object(self.m, 'inbox_append', side_effect=OSError('fixture receipt interruption')):
                with self.assertRaisesRegex(OSError, 'fixture receipt interruption'):
                    self.m.communication_record(self.inst, child, 'question', 'Choose recovery?', 'question-recovery', self.spec('outward'))
            original = next(iter(self.m.communication_read(self.inst)['questions'].values()))
            clock.return_value = datetime.datetime.fromisoformat('2026-01-02T00:01:00+00:00')
            retry = self.m.communication_record(self.inst, child, 'question', 'Choose recovery?', 'question-recovery', self.spec('outward'))
            self.assertEqual(retry, original)
            self.assertNotIn('deferred', retry)
            send.assert_called_once_with(self.inst, child, 'question:' + retry['id'],
                                         items=[{'id': retry['id'], 'kind': 'question'}])
            self.assertEqual((child['dir'] / 'inbox.md').read_text().count('<!-- communication:'), 1)
            self.m.communication_reconcile(self.inst)
            self.assertNotIn('answer', self.m.communication_read(self.inst)['questions'][retry['id']])

    def test_interrupted_notice_quiet_retry_rejects_content_collision(self):
        import datetime
        from unittest import mock
        child = self.quiet_recovery_child()
        inbox = child['dir'] / 'inbox.md'
        before_inbox = inbox.read_bytes() if inbox.exists() else None
        with mock.patch.object(self.m.Instance, 'now_dt', return_value=datetime.datetime.fromisoformat(
                '2026-01-01T23:59:00+00:00')) as clock, mock.patch.object(self.m, 'request_wake') as send:
            with mock.patch.object(self.m, 'inbox_append', side_effect=OSError('fixture receipt interruption')):
                with self.assertRaisesRegex(OSError, 'fixture receipt interruption'):
                    self.m.communication_record(self.inst, child, 'park', 'Original content', 'collision')
            store = self.root / '.brain/communications.json'
            original = store.read_bytes()
            clock.return_value = datetime.datetime.fromisoformat('2026-01-02T00:01:00+00:00')
            with self.assertRaisesRegex(ValueError, 'stable notification key already names different content'):
                self.m.communication_record(self.inst, child, 'park', 'Changed content', 'collision')
            self.assertEqual(store.read_bytes(), original)
            self.assertEqual(inbox.read_bytes() if inbox.exists() else None, before_inbox)
            send.assert_not_called()

    def test_root_landings_parks_and_fyi_are_inbox_only(self):
        from unittest import mock
        with mock.patch.object(self.m, 'request_wake') as send:
            for kind in ('landing', 'park', 'fyi'):
                self.m.communication_record(self.inst, self.inst.node('root'), kind, 'Sample update', kind)
            send.assert_not_called()
        text = (self.root / 'inbox.md').read_text()
        self.assertIn('landing: Sample update', text)
        self.assertIn('park: Sample update', text)
        self.assertIn('fyi: Sample update', text)

    def test_question_schema_and_routine_default_never_execute_an_action(self):
        from unittest import mock
        with mock.patch.object(self.m, 'request_wake', return_value=True) as send:
            question = self.m.communication_record(self.inst, self.inst.node('root'), 'question',
                                                  'Which path?', 'fixture', self.spec())
            self.assertEqual(send.call_count, 1)
            self.m.communication_reconcile(self.inst)
            saved = self.m.communication_read(self.inst)['questions'][question['id']]
            self.assertEqual(saved['answer'], 'continue')
            self.assertEqual(saved['answered_by'], 'deadline-default')
            self.m.communication_reconcile(self.inst)
            self.assertEqual(self.m.communication_read(self.inst)['questions'][question['id']], saved)

    def test_outward_and_destructive_questions_never_expire_to_default(self):
        from unittest import mock
        with mock.patch.object(self.m, 'request_wake', return_value=True):
            for action in ('outward', 'destructive'):
                q = self.m.communication_record(self.inst, self.inst.node('root'), 'question',
                                               'Choose action', action, self.spec(action))
                self.m.communication_reconcile(self.inst)
                self.assertNotIn('answer', self.m.communication_read(self.inst)['questions'][q['id']])

    def test_typed_questions_reject_missing_or_unknown_action_authority(self):
        from unittest import mock
        with mock.patch.object(self.m, 'request_wake') as send:
            for spec in ({}, {**self.spec(), 'default': 'unknown'},
                         {**self.spec(), 'options': [{'id': 'continue', 'label': 'Continue'}]}):
                with self.assertRaises(ValueError):
                    self.m.communication_record(self.inst, self.inst.node('root'), 'question', 'Choose?', 'bad', spec)
            send.assert_not_called()

    def test_typed_question_watch_ticks_retain_the_direct_receipt_identity(self):
        from unittest import mock
        m, n = self.m, self.inst.node('root')
        with mock.patch.object(m, 'wake_start'), mock.patch.object(m, 'WAKE_INTERVAL', 0), \
                mock.patch.object(m, 'wake_send', return_value=True) as send:
            question = m.communication_record(self.inst, n, 'question', 'Stable fixture question',
                                              'stable', self.spec('outward'))
            for _ in range(2):
                groups = m.watch_groups(self.inst)
                group = groups['root']
                item = next(i for i in group['items'] if i['kind'] == 'question')
                self.assertEqual(item['id'], question['id'])
                self.assertEqual(item['reason'], 'question:' + question['id'])
                m.watch_run(self.inst, groups=groups, quiet=True)
                m.wake_drain(self.inst, n, once=True)
                consume_fixture_wake(m, self.inst, n)
            self.assertEqual(send.call_count, 1)
            self.assertEqual(m.wake_history(m.wake_directory(self.inst, n))[-1]['snapshot'],
                             [{'id': question['id'], 'kind': 'question'}])

    def test_typed_question_inflight_baseline_excludes_previously_shown_question(self):
        from unittest import mock
        m, n = self.m, self.inst.node('root')
        with mock.patch.object(m, 'wake_start'), mock.patch.object(m, 'WAKE_INTERVAL', 0):
            m.communication_record(self.inst, n, 'question', 'First fixture question', 'first', self.spec('outward'))
            with mock.patch.object(m, 'wake_send', return_value=True):
                m.wake_drain(self.inst, n, once=True)
                consume_fixture_wake(m, self.inst, n)
            m.communication_record(self.inst, n, 'question', 'Second fixture question', 'second', self.spec('outward'))
            observed = []
            def send(*args, **kwargs):
                observed.extend(m.watch_since_wake(self.inst, [n]))
                return True
            with mock.patch.object(m, 'wake_send', side_effect=send):
                m.wake_drain(self.inst, n, once=True)
                consume_fixture_wake(m, self.inst, n)
            questions = [i for i in observed if i['kind'] == 'question']
            self.assertEqual(len(questions), 1)
            self.assertTrue(questions[0]['text'].startswith('Second fixture question'))


class NativeDispatcher(Scenario):
    def setUp(self):
        super().setUp()
        import core_loader
        import os
        from unittest import mock
        patch = mock.patch.dict(os.environ, self.env)
        patch.start()
        self.addCleanup(patch.stop)
        self.m = core_loader.SourceFileLoader('dispatcher_fixture', str(CORE / 'bin/brain')).load_module()
        self.root = self.init('alice')
        self.b(self.root, 'set', 'root', 'coordinator=fixture-root', 'wake=fixture-send', 'wake_coalesce=on', 'wake_policy=questions')
        self.inst = self.m.Instance(self.root)

    def question(self):
        from unittest import mock
        import datetime
        spec = {'options': [{'id': 'continue', 'label': 'Continue', 'action': 'routine'},
                            {'id': 'wait', 'label': 'Wait', 'action': 'routine'}],
                'recommendation': 'continue', 'default': 'continue',
                'deadline': (self.inst.now_dt() + datetime.timedelta(hours=1)).isoformat()}
        with mock.patch.object(self.m, 'request_wake'):
            return self.m.communication_record(self.inst, self.inst.node('root'), 'question',
                                               'Choose a fixture path', 'fixture-question', spec)

    def test_native_dispatch_routes_question_once_without_result_relays(self):
        from unittest import mock
        self.question()
        for kind in ('fyi', 'landing', 'park'):
            self.m.communication_record(self.inst, self.inst.node('root'), kind, 'Sample update', kind)
        with mock.patch.object(self.m, 'wake_start'), mock.patch.object(self.m, 'WAKE_INTERVAL', 0), \
                mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            result = self.m.dispatch_run(self.inst)
            self.assertEqual(result['accepted'], 1)
            self.assertEqual(self.m.dispatch_run(self.inst)['accepted'], 0)
            self.m.wake_drain(self.inst, self.inst.node('root'), once=True)
            consume_fixture_wake(self.m, self.inst, self.inst.node('root'))
            self.assertEqual(send.call_count, 1)

    def test_failed_enqueue_is_not_a_dispatch_receipt(self):
        from unittest import mock
        self.question()
        with mock.patch.object(self.m, 'request_wake', side_effect=[False, True]) as enqueue:
            self.assertEqual(self.m.dispatch_run(self.inst)['accepted'], 0)
            self.assertEqual(self.m.dispatch_run(self.inst)['accepted'], 1)
            self.assertEqual(self.m.dispatch_run(self.inst)['accepted'], 0)
            self.assertEqual(enqueue.call_count, 2)

    def test_sampler_dispatch_defers_on_busy_episode_lock_and_retries_without_receipt_loss(self):
        import fcntl
        import time
        from unittest import mock
        m = self.m
        self.question()
        self.b(self.root, 'set', 'root', 'dispatcher=on')
        inst = m.Instance(self.root)
        with mock.patch.object(m, 'wake_start'):
            self.assertEqual(m.dispatch_if_enabled(inst)['accepted'], 1)
        pending = list(m.wake_directory(inst, inst.node('root')).glob('*.pending'))
        self.assertEqual(len(pending), 1)
        paths = [*pending, self.root / '.brain/dispatch-receipts.json',
                 self.root / '.brain/wake/due-episodes.json']
        before = {path: path.read_bytes() for path in paths}
        (self.root / 'inbox.md').write_text('- Host question: Choose a second path <!-- host-question:second -->\n')
        with (self.root / '.brain/wake/due-episodes.lock').open('a+') as held, \
                mock.patch.object(m, 'request_wake') as wake:
            fcntl.flock(held, fcntl.LOCK_EX)
            start = time.monotonic()
            result = m.dispatch_if_enabled(inst)
            self.assertLess(time.monotonic() - start, 1.5)
            self.assertTrue(result['busy'])
            self.assertEqual(result['accepted'], 0)
            wake.assert_not_called()
            self.assertEqual({path: path.read_bytes() for path in paths}, before)
            self.assertEqual(list(m.wake_directory(inst, inst.node('root')).glob('*.pending')), pending)
        with mock.patch.object(m, 'wake_start'):
            self.assertEqual(m.dispatch_if_enabled(inst)['accepted'], 1)
        self.assertEqual(pending[0].read_bytes(), before[pending[0]])
        self.assertEqual(len(list(m.wake_directory(inst, inst.node('root')).glob('*.pending'))), 2)


class ReportGates(Scenario):
    """Coordinator reports and work starts, gated at the tool call or stop that makes them.

    The AI judges are fixtures: meaning is the stub's answer, and the gate only compares
    that answer's structure with structural turn provenance (operator input, prior tools).
    """
    fixture_init_in_process = True

    def setUp(self):
        import core_loader
        super().setUp()
        self.root = self.init('meadow').resolve()  # the hooks resolve their working folder
        self.m = core_loader.SourceFileLoader('report_gate_fixture', str(CORE / 'bin/brain')).load_module()
        self.inst = self.m.Instance(self.root)
        self.inst.local_of('root').update(coordinator='host-coordinator', coordinator_sessions=['runtime-coordinator'])
        self.inst.save_local()
        self.n = self.inst.node('root')
        self.path = self.tmp / 'turns.jsonl'
        self.records, self.claims, self.kind, self.judged, self.calls = [], [], 'question', [], 0
        self.speaker, self.capture_items, self.classified, self.prior_cancelled = 'operator', None, [], False

    def write(self, *records):
        import json
        self.records.extend(records)
        self.path.write_text(''.join(json.dumps(r) + '\n' for r in self.records))

    def operator(self, text):
        at = len(self.records)  # later inputs carry later times, as the runtime records them
        self.write({'type': 'user', 'timestamp': '2026-01-15T%02d:%02d:%02dZ' % (12 + at // 3600, at // 60 % 60, at % 60),
                    'message': {'role': 'user', 'content': text}})

    def inbound(self, text, **metadata):
        """An input carrying the host's structural metadata (sender thread, origin, human marker)."""
        at = len(self.records)
        self.write(dict({'type': 'user', 'timestamp': '2026-01-15T%02d:%02d:%02dZ' % (12 + at // 3600, at // 60 % 60,
                                                                                   at % 60),
                         'message': {'role': 'user', 'content': text}}, **metadata))

    def automated(self, text='Wake: check the queue.'):
        """An automated turn as the host delivers it: the production length/digest envelope."""
        self.write({'type': 'user', 'message': {'role': 'user', 'content': self.m.automated_brief(text)}})

    def tool(self, name, tool_input, failed=False, result='ok'):
        """A paired call and result; a failed result is the host's error (cancelled, ENOENT)."""
        ident = 'call-' + str(self.calls)
        self.calls += 1
        self.write({'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': ident,
                                                                   'name': name, 'input': tool_input}]}},
                   {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': ident,
                                                             'is_error': failed, 'content': result}]}})

    def claim(self, kind, quote=None, support=(), operator_quote=''):
        """support='cite' cites every evidence entry the gate offered the judge."""
        return {'kind': kind, 'quote': quote, 'support': support, 'operator_quote': operator_quote}

    def ai(self, system, data, schema, **kw):
        """Fixture judge for both schemas; meaning is the fixture's answer."""
        if 'messages' in schema['properties']:
            self.classified.append(data)
            if self.capture_items is not None:
                return {'messages': [{'id': row['id'], 'items': [dict(i, speaker_evidence=i['quote'])
                                                                 for i in self.capture_items(row)]}
                                     for row in data['messages']]}, {}
            return {'messages': [{'id': row['id'], 'items': [{'kind': self.kind, 'quote': row['text'],
                                                              'speaker': self.speaker,
                                                              'speaker_evidence': row['text']}]}
                                 for row in data['messages']]}, {}
        self.judged.append(data)
        cited = [e['id'] for e in data['evidence']]
        return {'prior_cancelled': self.prior_cancelled,
                'items': [dict(c, quote=data['outgoing'] if c['quote'] is None else c['quote'],
                               support=cited if c['support'] == 'cite' else list(c['support']))
                          for c in self.claims]}, {}

    def stubbed(self):
        import contextlib
        from unittest import mock
        stack = contextlib.ExitStack()
        for owner in {id(o): o for o in self.m._owners.values()}.values():
            if hasattr(owner, 'ai_json'):
                stack.enter_context(mock.patch.object(owner, 'ai_json', side_effect=self.ai))
        return stack

    def pretool(self, tool, tool_input, session='runtime-coordinator', cwd=None):
        import argparse, contextlib, io, json, sys
        from unittest import mock
        payload = {'cwd': str(cwd or self.root), 'session_id': session, 'transcript_path': str(self.path),
                   'tool_name': tool, 'tool_input': tool_input}
        with self.stubbed(), mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))), \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            self.m.cmd_hook_pretool(argparse.Namespace())
        if not out.getvalue().strip():
            return None
        decision = json.loads(out.getvalue())['hookSpecificOutput']
        if 'updatedInput' in decision:
            self.assertNotIn('permissionDecision', decision)
            return None
        self.assertEqual(decision['permissionDecision'], 'deny')
        self.assertTrue(decision['permissionDecisionReason'].startswith('brain-hook:pretool: '))
        return decision['permissionDecisionReason']

    def stop(self, last_message, session='runtime-coordinator'):
        import argparse, contextlib, io, json, sys
        from unittest import mock
        payload = {'cwd': str(self.root), 'session_id': session, 'transcript_path': str(self.path),
                   'last_assistant_message': last_message}
        with self.stubbed(), mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))), \
                mock.patch.object(self.m, 'capture_hook_questions'), \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            self.m.hook_stop(argparse.Namespace(event='stop'))
        lines = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
        return next((line['reason'] for line in lines if line.get('decision') == 'block'), None)

    def send(self, message):
        return self.pretool('mcp__t3-code__t3_thread_send', {'threadId': 'peer-thread', 'message': message})

    def test_question_or_complaint_cannot_start_widen_or_restart_work(self):
        """R04: the AI must tie this exact work to an operator order in the turn; any one-off is not enough."""
        delegate = ('mcp__t3-code__delegate_task', {'prompt': 'Rebuild the test runner.'})
        restart = ('Bash', {'command': 'brain lifecycle restart app --job-key k'})
        self.operator('Why are the tests this slow?')
        self.claims = [self.claim('work_order')]
        self.assertIn('not an order', self.pretool(*delegate))
        self.assertIn('not an order', self.send('Start the runner rebuild now.'))
        self.claims = []
        self.assertIn('not an order', self.pretool(*restart))  # a worker start is work even if unjudged
        self.assertIsNone(self.send('The tests are slow because each one starts a fresh store.'))
        # An unrelated one-off (an explanation request) authorises no rebuild.
        self.kind = 'one_off'
        self.operator('Explain why the tests are slow.')
        self.claims = [self.claim('work_order')]
        self.assertIn('not an order', self.pretool(*delegate))
        self.claims = [self.claim('work_order', operator_quote='an invented order')]
        self.assertIn('not an order', self.pretool(*delegate))
        # The AI cites the operator's order for this work, inside a span capture classified as an order.
        self.operator('Rebuild the test runner now.')
        self.claims = [self.claim('work_order', operator_quote='Rebuild the test runner now.')]
        self.assertIsNone(self.pretool(*delegate))
        self.assertIsNone(self.pretool(*restart))
        self.kind = 'question'
        self.operator('Rebuild the test runner now?')
        self.claims = [self.claim('work_order', operator_quote='Rebuild the test runner now?')]
        self.assertIn('not an order', self.pretool(*delegate))  # quoted span is a question, not an order
        self.automated()  # ordered work started by an automated wake is outside this rule
        self.claims = [self.claim('work_order')]
        self.assertIsNone(self.pretool(*delegate))

    def test_automated_cancellation_requires_actual_operator_message_for_operator_attribution(self):
        """R35: blame needs an actual failed call and the operator's own instruction; retries are bounded."""
        listing = ('mcp__t3-code__t3_thread_list', {})
        blame = 'Status: you rejected the thread list, so I stopped.'
        self.automated()
        self.claims = [self.claim('cancel_blamed_on_operator', 'you rejected the thread list')]
        self.assertIn('host cancelled', self.send(blame))  # no cancellation happened at all
        self.tool(*listing, failed=True, result="The user doesn't want to take this action right now.")
        reason = self.send(blame)
        self.assertIn('host cancelled', reason)
        self.assertIn('retry once', reason)
        self.claims = [self.claim('host_problem_report', 'the host cancelled t3_thread_list')]
        self.assertIn('Retry once', self.send('Status: the host cancelled t3_thread_list.'))
        self.tool(*listing, failed=True, result="The user doesn't want to take this action right now.")
        self.assertIn('already failed 2 times', self.pretool(*listing))  # a third identical try is refused
        self.assertIsNone(self.send('Status: the host cancelled t3_thread_list twice; reporting a host problem.'))
        # An operator message that is not the operator's own cancellation does not license blame.
        self.operator('Why was that cancelled?')
        self.tool(*listing, failed=True, result='cancelled')
        self.claims = [self.claim('cancel_blamed_on_operator', 'you rejected the thread list')]
        self.assertIn('host cancelled', self.send(blame))
        self.operator('Stop listing threads.')
        self.tool(*listing, failed=True, result='rejected')
        self.claims = [self.claim('cancel_blamed_on_operator', 'you rejected the thread list',
                                  operator_quote='Stop listing threads.')]
        self.assertIsNone(self.send(blame))

    def test_fix_report_requires_a_prior_source_or_diff_read_in_the_reporting_turn(self):
        """R79: only a successful, AI-cited diff or file read earlier in this turn supports a fix claim."""
        report = 'Done: the gate is fixed, 42 of 42 pass.'
        self.operator('Is the gate fixed?')
        self.tool('Bash', {'command': 'git -C /tmp/app diff main -- lib/gate.py'}, result='+fixed line')
        self.operator('Report the fix to the node.')
        self.kind = 'one_off'
        self.claims = [self.claim('fix_claim', 'the gate is fixed', support='cite')]
        self.assertIn('Read the diff or file', self.send(report))  # the read was in an older turn
        self.tool('Bash', {'command': 'brain status app'})
        self.assertIn('Read the diff or file', self.send(report))
        self.tool('Read', {'file_path': '/tmp/app/lib/missing.py'}, failed=True,
                  result='ENOENT: no such file or directory')
        self.assertIn('Read the diff or file', self.send(report))  # a failed read is no evidence
        self.claims = [self.claim('fix_claim', 'the gate is fixed', support=['e1'])]
        self.assertIn('Read the diff or file', self.send(report))  # citing the failed call does not count
        for name, tool_input in (('Read', {'file_path': '/tmp/app/lib/gate.py'}),
                                 ('exec_command', {'cmd': 'git -C /tmp/app show HEAD -- lib/gate.py'})):
            with self.subTest(tool=name):
                self.operator('Report the fix to the node.')
                self.tool(name, tool_input, result='def gate(): return fixed')
                self.claims = [self.claim('fix_claim', 'the gate is fixed')]  # the AI finds no support
                self.assertIn('Read the diff or file', self.send(report))
                self.claims = [self.claim('fix_claim', 'the gate is fixed', support='cite')]
                self.assertIsNone(self.send(report))
        self.operator('And the final message?')
        self.assertIn('Read the diff or file', self.stop('The gate is fixed: the gate is fixed.'))

    def test_live_claim_requires_current_browser_observation_and_unseen_build_is_unproven(self):
        """R23: "live" needs a successful, AI-cited page observation in this turn, at the send and the stop."""
        self.operator('Is the new page live?')
        self.claims = [self.claim('live_claim', 'the page is live', support='cite')]
        self.assertIn('seen working on the page', self.send('Update: the page is live.'))
        self.assertIn('seen working on the page', self.stop('Yes, the page is live.'))
        self.tool('mcp__t3-code__preview_snapshot', {'tabId': 'fixture'}, failed=True, result='Tab not found')
        self.assertIn('seen working on the page', self.send('Update: the page is live.'))
        self.assertIn('seen working on the page', self.stop('Yes, the page is live.'))
        self.tool('mcp__t3-code__preview_snapshot', {'tabId': 'fixture'}, result='heading "Orders" table 3 rows')
        self.claims = [self.claim('live_claim', 'the page is live')]  # rendered, but the AI finds it unshown
        self.assertIn('seen working on the page', self.send('Update: the page is live.'))
        self.claims = [self.claim('live_claim', 'the page is live', support='cite')]
        self.assertIsNone(self.send('Update: the page is live.'))
        self.assertIsNone(self.stop('Yes, the page is live.'))
        self.claims = []
        self.operator('And now?')
        self.assertIsNone(self.stop('It is on main but not proven yet: nobody has seen it working.'))

    def test_judge_structure_scope_and_fast_automated_stops(self):
        """Invented quotes fail open; workers and automated stops never call the judge."""
        import time
        self.operator('Is the new page live?')
        self.claims = [self.claim('live_claim', 'an invented span')]
        self.assertIsNone(self.send('Update: the page is live.'))
        self.claims = [self.claim('live_claim', 'the page is live')]
        self.judged.clear()
        self.assertIsNone(self.pretool('mcp__t3-code__t3_thread_send', {'threadId': 'p', 'message': 'Update: the page is live.'},
                                       session='some-worker'))
        self.assertEqual(self.judged, [])
        self.automated()
        started = time.monotonic()
        self.assertIsNone(self.stop('Yes, the page is live.'))
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(self.judged, [])

    def reply(self, text):
        """The agent's final reply of a turn: its text and the runtime's end-of-turn marker."""
        self.write({'type': 'assistant', 'message': {'id': 'reply-' + str(len(self.records)), 'stop_reason': 'end_turn',
                                                     'content': [{'type': 'text', 'text': text}]}})

    def items_by_text(self, table):
        """Capture fixture: table maps a message text to [(kind, quote, speaker)]; others are operator questions."""
        for cached in (self.root / '.brain/report-gates').glob('*.json'):
            cached.unlink()  # a new fixture answer replaces capture's per-turn cache
        self.capture_items = lambda row: [{'kind': k, 'quote': q, 'speaker': who}
                                          for k, q, who in table.get(row['text'], [('question', row['text'], 'operator')])]

    def test_overlapping_order_spans_pass_and_each_refusal_names_its_cause(self):
        """F1-a/F1-e (R1, R3): the two AIs' spans need only overlap; a refusal says which check failed."""
        order = 'yes lets go ahead with both 20k lines and cli'
        self.operator(order)
        self.items_by_text({order: [('one_off', 'lets go ahead with both 20k lines and cli', 'operator')]})
        relay = 'Ordered: build both, the 20k-line split and the cli, then re-measure and update the README.'
        self.claims = [self.claim('work_order', operator_quote='yes lets go ahead with both')]
        self.assertIsNone(self.send(relay))  # R1: partial overlap, neither span contains the other
        self.items_by_text({order: [('other', 'yes', 'operator'), ('one_off', 'both 20k lines and cli', 'operator')]})
        self.claims = [self.claim('work_order', operator_quote='yes lets go')]
        reason = self.send(relay)
        self.assertIn('capture did not read the cited words "yes lets go" as an order (it read them as other)', reason)
        self.items_by_text({order: [('one_off', order, 'operator')]})
        self.claims = [self.claim('work_order', operator_quote=order),
                       self.claim('work_order', 'then re-measure and update the README')]
        reason = self.send(relay)  # R3: one unquoted follow-through item is named, not a generic refusal
        self.assertIn('the judge found no message in this turn that orders "then re-measure and update the README"',
                      reason)
        self.claims = [self.claim('work_order', operator_quote='an order nobody typed')]
        self.assertIn('are not verbatim in any message of this turn', self.send(relay))
        self.claims = [self.claim('work_order', operator_quote=order)]
        self.assertIsNone(self.send(relay))  # R3 as the judge now reads it: one relay, one order

    def test_both_ais_get_the_preceding_offer_and_quoted_back_lines(self):
        """F1-c (R9, R11): a short "yes" answers the agent's own offer; quoted-back lines are marked by bytes."""
        offer = 'Want me to have the review coordinator start the third review now?'
        self.reply('The second review is back.\n' + offer)
        answer = 'yes also can you check what updates we have for the other two nodes?'
        self.operator(answer)
        self.items_by_text({answer: [('one_off', 'yes', 'operator'),
                                     ('question', 'also can you check what updates we have for the other two nodes?',
                                      'operator')]})
        self.claims = [self.claim('work_order', operator_quote='yes')]
        self.assertIsNone(self.send('Start the third review now.'))
        self.assertEqual(self.classified[-1]['preceding_assistant_text'], 'The second review is back.\n' + offer)
        self.assertEqual(self.judged[-1]['preceding_assistant_text'], 'The second review is back.\n' + offer)
        self.reply('Release status: the 0.3.0 npm release needs your go, and I will ask you then.')
        quoted = '> the 0.3.0 npm release needs your go, and I will ask you then.\ngo ahead dont wait for me'
        self.operator(quoted)
        self.items_by_text({'go ahead dont wait for me': [('one_off', 'go ahead dont wait for me', 'operator')]})
        self.claims = [self.claim('work_order', operator_quote='go ahead dont wait for me')]
        self.assertIsNone(self.send('Release 0.3.0 now.'))
        self.assertEqual(self.classified[-1]['messages'][0]['quoted_from_preceding'],
                         ['> the 0.3.0 npm release needs your go, and I will ask you then.'])

    def held_turn(self, text, quote, order, relay, to='peer-thread'):
        """One operator turn: its message, capture's reading of it, the judge's cited order, the send, the reply."""
        self.operator(text)
        self.items_by_text({order: [('one_off', order, 'operator')],
                            text: [('question', text, 'operator')] if text != order else [('one_off', order, 'operator')]})
        self.claims = [self.claim('work_order', operator_quote=quote)]
        reason = self.pretool('mcp__t3-code__t3_thread_send', {'threadId': to, 'message': relay})
        self.reply('Answered.')
        return reason

    def held(self):
        return sorted((self.root / '.brain/report-gates/held').glob('*.json'))

    def test_held_send_replays_on_a_later_turn_only_when_declared(self):
        """F1-d (R7): a refused send keeps its turn's order for the declared window; default 0 keeps today's rule."""
        relay = 'Ordered: ship the no-name posts as text only for now.'
        order = 'Yes lets do no name posts as text only for now'
        turn = lambda text, quote: self.held_turn(text, quote, order, relay)
        self.assertIn('not an order', turn(order, ''))  # the judge missed it: refused in its own turn
        self.assertIn('not an order', turn('why did it block? the app looks great', order))  # default 0
        self.assertEqual(list((self.root / '.brain/report-gates').glob('held/*')), [])
        self.b(self.root, 'set', 'root', 'report_gate_held_turns=1')
        self.assertIn('not an order', turn(order, ''))
        self.assertEqual(len(self.held()), 1)
        self.assertIsNone(turn('why did it block? the app looks great', order))  # R7: the next turn sends it
        self.assertEqual(self.judged[-1]['prior_operator_messages'], [order])
        self.assertEqual(self.held(), [])  # used once
        self.assertIn('not an order', turn(order, ''))
        turn('first later turn', '')
        self.assertIn('not an order', turn('second later turn', order))  # past the window
        self.assertNotIn('prior_operator_messages', self.judged[-1])

    def test_cancelled_hold_is_revoked_before_anything_else(self):
        """Review finding 3: a cancellation deletes the stored receipt; a later turn in the window cannot revive it."""
        relay = 'Ordered: ship the no-name posts as text only for now.'
        order = 'Yes lets do no name posts as text only for now'
        self.b(self.root, 'set', 'root', 'report_gate_held_turns=2')
        self.assertIn('not an order', self.held_turn(order, '', order, relay))
        self.assertEqual(len(self.held()), 1)
        self.prior_cancelled = True
        self.assertIn('not an order', self.held_turn('actually hold off on the posts', order, order, relay))
        self.assertIn('prior_operator_messages', self.judged[-1])
        self.assertEqual(self.held(), [])  # revoked, and the refusal did not hold the cancelling turn
        self.prior_cancelled = False
        self.assertIn('not an order', self.held_turn('how is the rest going?', order, order, relay))
        self.assertNotIn('prior_operator_messages', self.judged[-1])

    def test_held_receipt_is_bound_to_its_send_and_rejected_when_inconsistent(self):
        """Review finding 4: a hold serves only its node, session, tool, recipient, transcript and text,
        and a receipt whose stored digest or scope disagrees with its path is refused."""
        import json, shutil
        relay = 'Ordered: ship the no-name posts as text only for now.'
        order = 'Yes lets do no name posts as text only for now'
        self.b(self.root, 'set', 'root', 'report_gate_held_turns=1')
        self.assertIn('not an order', self.held_turn(order, '', order, relay))
        [receipt] = self.held()
        record = json.loads(receipt.read_text())
        self.assertEqual(record['sha256'], receipt.stem)
        self.assertEqual({k: record['scope'][k] for k in ('node', 'session', 'tool', 'recipient', 'transcript')},
                         {'node': 'root', 'session': 'runtime-coordinator', 'tool': 't3_thread_send',
                          'recipient': 'peer-thread', 'transcript': str(self.path)})
        question = 'why did it block? the app looks great'
        self.operator(question)
        self.items_by_text({order: [('one_off', order, 'operator')], question: [('question', question, 'operator')]})
        self.claims = [self.claim('work_order', operator_quote=order)]
        send = lambda to='peer-thread', text=relay: self.pretool('mcp__t3-code__t3_thread_send',
                                                                 {'threadId': to, 'message': text})
        self.assertIn('not an order', send(to='other-thread'))  # another recipient
        self.assertIn('not an order', send(text=relay + ' Also delete the drafts.'))  # other text
        original, moved = self.path, self.tmp / 'other-transcript.jsonl'
        shutil.copyfile(self.path, moved)
        self.path = moved
        self.assertIn('not an order', send())  # another transcript
        self.path = original
        for tamper in (dict(record, sha256='0' * 64), dict(record, scope=dict(record['scope'], recipient='x'))):
            receipt.write_text(json.dumps(tamper))
            self.assertIn('not an order', send())  # inconsistent receipts are refused
        receipt.write_text(json.dumps(record))
        self.assertIsNone(send())  # the bound receipt itself still serves its own send

    def test_host_notice_turn_is_not_an_operator_turn(self):
        """Item 2, today's fixture A: a turn a host notice started never asks for an operator quote."""
        notice = ('Update on pull request #12 (https://example.invalid/pull/12), which the host is watching for you:\n'
                  '- Checks failed on 0a1b2c3:\n  - Unit tests\n'
                  'Look into each item and act on it as your task requires.\n'
                  'This PR stays linked and watched, so do not link, watch, or list it again.')
        self.operator(notice)
        self.items_by_text({notice: [('other', 'Update on pull request #12', 'runtime'),
                                     ('one_off', 'Look into each item and act on it as your task requires.', 'runtime'),
                                     ('standing_instruction', 'This PR stays linked and watched, so do not link, '
                                                              'watch, or list it again.', 'runtime')]})
        relay = 'Ordered work for #12: the unit test fails on the record card; fix it in one push and hand back.'
        self.claims = [self.claim('work_order')]
        self.assertIsNone(self.send(relay))
        self.assertIsNone(self.pretool('mcp__t3-code__delegate_task', {'prompt': relay}))
        for speaker in ('operator', 'unknown'):  # the gate still holds when the AI reads (or cannot rule out) the operator
            with self.subTest(speaker=speaker):
                self.items_by_text({notice: [('other', notice, speaker)]})
                self.assertIn('the judge found no message in this turn that orders', self.send(relay))

    def child(self):
        """A child node whose parent (root) coordinator is bound as host-coordinator."""
        self.b(self.root, 'new', 'child', '--title', 'Synthetic child')
        self.inst = self.m.Instance(self.root)
        self.inst.local_of('child').update(coordinator='child-host', coordinator_sessions=['child-runtime'])
        self.inst.save_local()
        return self.inst.node('child')['dir']

    def test_parent_coordinators_order_reaches_the_child(self):
        """Item 2 and F1-f, today's fixture B: a parent's order, plain or relayed by its host sender thread,
        is not an operator question; an envelope alone, or another sender, carries no relay authority."""
        child = self.child()
        send = lambda: self.pretool('mcp__t3-code__t3_thread_send', {'threadId': 'peer-thread', 'message': relay},
                                    session='child-runtime', cwd=child)
        order = ('Parent coordinator to the child coordinator. ORDER: every item below is ordered work; send and start '
                 'them now. 1. Send every held relay to its builder. This message is your order for each send.')
        self.operator(order)
        self.items_by_text({order: [('one_off', order, 'agent')]})
        relay = ('Ordered work for #7 (head 0a1b2c3). Your fixes are read and accepted. One more push: make the filter '
                 'row one line, re-capture the side-by-side, cheap gates before push, hand back with the head sha.')
        self.claims = [self.claim('work_order')]
        self.assertIsNone(send())  # plain: the AI reads the speaker
        self.reply('Sent.')
        question = 'why is this taking so long?'
        envelope = self.m.marked_message(order, self.m.AGENT_MESSAGE_PREFIX)
        # Forged: a valid envelope coalesced with operator text, but no host sender thread.
        batch = envelope + '\n' + question
        self.operator(batch)
        self.items_by_text({order: [('one_off', 'every item below is ordered work; send and start them now', 'agent')],
                            batch: [('one_off', 'every item below is ordered work; send and start them now', 'agent'),
                                    ('question', question, 'operator')]})
        self.claims = [self.claim('work_order', operator_quote='send and start them now')]
        self.assertIn('not verbatim in any message', send())
        self.assertEqual([m.get('relayed', False) for m in self.judged[-1]['turn_messages']], [False])
        self.reply('Answered.')
        # The parent's host sender thread: the relay is the turn's order, by structure.
        self.inbound(envelope, senderThreadId='host-coordinator')
        self.assertIsNone(send())
        self.assertEqual([(m['text'], m['relayed']) for m in self.judged[-1]['turn_messages']], [(order, True)])
        self.reply('Sent.')
        # Another sender thread is a new input, never a relay from the parent.
        self.inbound(envelope, senderThreadId='peer-thread')
        self.assertIsNone(send())
        self.assertEqual(self.judged[-1]['turn_messages'], [])

    def test_structured_input_after_an_operator_question_ends_its_turn(self):
        """Review finding 2: a sender-thread or system-origin input starts a new turn, so the earlier
        operator question stops gating the builder sends that input asks for."""
        child = self.child()
        relay = 'Ordered work for #7: one more push, then hand back with the head sha.'
        send = lambda: self.pretool('mcp__t3-code__t3_thread_send', {'threadId': 'peer-thread', 'message': relay},
                                    session='child-runtime', cwd=child)
        self.claims = [self.claim('work_order')]
        for metadata in ({'senderThreadId': 'host-coordinator'}, {'promptSource': 'system'}):
            with self.subTest(metadata=metadata):
                self.operator('why is the review taking so long?')
                self.items_by_text({})
                self.assertIn('not an order', send())
                self.reply('It is waiting on the fixture fix.')
                self.inbound('Send the held relay to the builder now.', **metadata)
                self.assertIsNone(send())
                self.reply('Sent.')

    def test_runtime_human_marker_outranks_the_ai_speaker(self):
        """Review finding 5 (gate side): an input the runtime marks as human-typed is the operator's,
        whatever speaker the AI reads."""
        self.inbound('Start the rebuild of the test runner?', turnOrigin='human')
        self.items_by_text({'Start the rebuild of the test runner?': [
            ('question', 'Start the rebuild of the test runner?', 'agent')]})
        self.claims = [self.claim('work_order')]
        self.assertIn('not an order', self.send('Start the runner rebuild now.'))
        self.assertEqual(self.classified[-1]['messages'][0]['text'], 'Start the rebuild of the test runner?')

    def test_refused_send_to_the_parent_is_filed_in_its_inbox(self):
        """F5: a child coordinator's refused send to its parent is filed as is there, and the refusal says where."""
        child = self.child()
        self.operator('Why is the build slow?')
        self.claims = [self.claim('work_order')]
        ask = 'Child coordinator: please launch the second builder for the order you gave at noon.'
        reason = self.pretool('mcp__t3-code__t3_thread_send', {'threadId': 'host-coordinator', 'message': ask},
                              session='child-runtime', cwd=child)
        self.assertIn('not an order', reason)
        self.assertIn("filed as is in root's inbox", reason)
        inbox = (self.root / 'inbox.md').read_text()
        self.assertIn(ask, inbox)
        self.assertIn('from child coordinator', inbox)
        again = self.pretool('mcp__t3-code__t3_thread_send', {'threadId': 'host-coordinator', 'message': ask},
                             session='child-runtime', cwd=child)
        self.assertIn('filed as is', again)
        self.assertEqual((self.root / 'inbox.md').read_text().count(ask), 1)  # one filing per message
        other = self.pretool('mcp__t3-code__t3_thread_send', {'threadId': 'peer-thread', 'message': ask},
                             session='child-runtime', cwd=child)
        self.assertNotIn('filed as is', other)  # only a send to the parent's coordinator is filed

    def codex(self, name, arguments, output):
        """A Codex rollout call and its native output, paired by call_id."""
        import json
        ident = 'codex-' + str(self.calls)
        self.calls += 1
        self.write({'type': 'response_item', 'payload': {'type': 'function_call', 'call_id': ident, 'name': name,
                                                         'arguments': json.dumps(arguments)}},
                   {'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': ident,
                                                         'output': output}})

    def test_codex_nonzero_exit_and_mcp_is_error_are_never_evidence(self):
        """R79/R23: every native error outcome, and any unknown one, fails closed as evidence."""
        import json
        diff = ('exec_command', {'cmd': 'git -C /tmp/app diff main -- lib/gate.py'})
        snapshot = 'mcp__t3-code__preview_snapshot'
        mcp_error = json.dumps({'content': [{'type': 'text', 'text': 'Tab not found'}], 'isError': True})
        failing_reads = [json.dumps({'exit_code': 1, 'output': 'fatal'}),
                         'Chunk ID: a1\nWall time: 0.1 seconds\nProcess exited with code 128\nOutput:\nfatal: bad revision',
                         'Exit code: 1\nWall time: 0.1 seconds\nOutput:\nfatal: bad revision',
                         [{'type': 'input_text', 'text': 'Script completed\nOutput:\n'},
                          {'type': 'input_text', 'text': json.dumps({'exit_code': 2, 'output': ''})}],
                         'fatal: not a git repository']  # no native outcome marker: unknown
        self.kind = 'one_off'
        for output in failing_reads:
            with self.subTest(read=output):
                self.operator('Report the fix.')
                self.codex(diff[0], diff[1], output)
                self.claims = [self.claim('fix_claim', 'the gate is fixed', support='cite')]
                self.assertIn('Read the diff or file', self.send('Done: the gate is fixed.'))
        for record in ('codex', 'claude'):
            with self.subTest(observation=record):
                self.operator('Is the page live?')
                if record == 'codex':
                    self.codex(snapshot, {'tabId': 'fixture'}, mcp_error)
                else:
                    self.tool(snapshot, {'tabId': 'fixture'}, result=mcp_error)  # is_error false, isError true
                self.claims = [self.claim('live_claim', 'the page is live', support='cite')]
                self.assertIn('seen working on the page', self.send('Update: the page is live.'))
        for output in ('Chunk ID: a2\nWall time: 0.1 seconds\nProcess exited with code 0\nOutput:\n+fixed line',
                       json.dumps({'exit_code': 0, 'output': '+fixed line'})):
            with self.subTest(success=output):
                self.operator('Report the fix.')
                self.codex(diff[0], diff[1], output)
                self.claims = [self.claim('fix_claim', 'the gate is fixed', support='cite')]
                self.assertIsNone(self.send('Done: the gate is fixed.'))
        self.operator('Is the page live?')
        self.codex(snapshot, {'tabId': 'fixture'}, json.dumps({'content': [{'type': 'text', 'text': 'Orders table'}],
                                                                'isError': False}))
        self.claims = [self.claim('live_claim', 'the page is live', support='cite')]
        self.assertIsNone(self.send('Update: the page is live.'))
