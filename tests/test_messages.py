"""Made-up worker commands: four kinds, optional context and direct peer briefs."""
import re

from scenario import consume_fixture_wake, Scenario, CORE
import importlib
import core_loader

_fixture = core_loader.SourceFileLoader('message_envelope_fixture', str(CORE / 'bin/brain')).load_module()
_envelope = importlib.import_module(_fixture._owners['worker_message_guard'].__package__ + '.message_envelope')
marked_message, unmarked_message = _envelope.marked_message, _envelope.unmarked_message

AGENT_MARKER = "Agent message: sent by another agent, not the operator."
AGENT_HEADER = AGENT_MARKER + "\n\n"


class WorkerMessages(Scenario):
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
            self.assertTrue(out.startswith(AGENT_HEADER + "Brain-Envelope: "), out)

    def test_formatter_and_briefs(self):
        root, _ = self.job()
        for kind in ("start", "done", "blocked", "question"):
            out = self.b(root, "msg", "a", kind, "One\nline", "--branch", "job/sample", "--report", "~/reports/example.md")
            self.assertEqual(out.strip(), marked_message(kind + ": One line [branch: job/sample; report: ~/reports/example.md]", AGENT_MARKER) + "\nNOT SENT: use the host send")
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
                    self.assertEqual(output.strip(), marked_message(kind + ": Made-up result [branch: main]", AGENT_MARKER) + "\nNOT SENT: use the host send")
        # A generic worker may be outside Git entirely, with no ledger job or report.
        plain = self.tmp / "plain"
        plain.mkdir()
        for kind in ("start", "done", "blocked", "question"):
            output = self.run_(["msg", "a", kind, "Made-up result"], cwd=plain, brain=CORE / "bin/brain")
            self.assertEqual(output, marked_message(kind + ": Made-up result", AGENT_MARKER) + "\nNOT SENT: use the host send\n")
        self.assertEqual(self.b(root, "msg", "a", "done", "Result", "--branch", "other").strip(),
                         marked_message("done: Result [branch: other]", AGENT_MARKER) + "\nNOT SENT: use the host send")

    def test_freeform_agent_formatter_preserves_bytes_and_never_sends(self):
        root = self.init('alice')
        body = 'Keep original evidence.\n  Preserve indentation and spaces.  '
        path = self.tmp / 'message.txt'
        path.write_text(body)
        expected = marked_message(body, AGENT_MARKER) + '\n'
        self.assertEqual(self.b(root, 'agent-message', '--message-file', str(path)), expected)
        self.assertEqual(self.b(root, 'agent-message', body), expected)
        self.assertEqual(self.b(root, 'agent-message', expected[:-1]), expected)
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
        self.assertEqual(messages.agent_message(prepared), prepared)

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

    def write(self, *records):
        import json
        self.records.extend(records)
        self.path.write_text(''.join(json.dumps(r) + '\n' for r in self.records))

    def operator(self, text):
        self.write({'type': 'user', 'timestamp': '2026-01-15T12:00:00Z', 'message': {'role': 'user', 'content': text}})

    def automated(self, text='Wake: check the queue.'):
        self.write({'type': 'user', 'message': {'role': 'user', 'content': self.m.AUTOMATED_TURN_PREFIX + '\n\n' + text}})

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
            return {'messages': [{'id': row['id'], 'items': [{'kind': self.kind, 'quote': row['text']}]}
                                 for row in data]}, {}
        self.judged.append(data)
        cited = [e['id'] for e in data['evidence']]
        return {'items': [dict(c, quote=data['outgoing'] if c['quote'] is None else c['quote'],
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

    def pretool(self, tool, tool_input, session='runtime-coordinator'):
        import argparse, contextlib, io, json, sys
        from unittest import mock
        payload = {'cwd': str(self.root), 'session_id': session, 'transcript_path': str(self.path),
                   'tool_name': tool, 'tool_input': tool_input}
        with self.stubbed(), mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))), \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            self.m.cmd_hook_pretool(argparse.Namespace())
        if not out.getvalue().strip():
            return None
        decision = json.loads(out.getvalue())['hookSpecificOutput']
        self.assertEqual(decision['permissionDecision'], 'deny')
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

    def test_job_drop_cannot_become_a_global_exclusion_without_standing_authority(self):
        """R91: a one-off drop stays with its job; no write tool or patch format widens it into a rule."""
        self.operator('Forget about the tracker integration for this redesign.')
        self.kind = 'one_off'
        self.claims = [self.claim('standing_exclusion', 'never use the tracker')]
        rule = {'file_path': str(self.root / 'AGENTS.md'), 'old_string': '', 'new_string': '- never use the tracker'}
        patch = '*** Begin Patch\n*** Update File: AGENTS.md\n@@\n+- never use the tracker\n*** End Patch\n'
        unified = '--- a/library/instance-rules.md\n+++ b/library/instance-rules.md\n@@ -1 +1,2 @@\n+- never use the tracker\n'
        for tool, tool_input in (('Edit', rule), ('apply_patch', {'input': patch}), ('apply_patch', {'patch': unified}),
                                 ('exec_command', {'cmd': "apply_patch <<'EOF'\n" + patch + "EOF"}),
                                 ('mcp__t3-code__delegate_task', {'prompt': 'Rule for all jobs: never use the tracker.'})):
            with self.subTest(tool=tool, keys=sorted(tool_input)):
                self.assertIn('job at hand only', self.pretool(tool, tool_input))
        self.claims = []
        self.assertIsNone(self.pretool('Edit', dict(rule, new_string='- skip the tracker in the redesign job only')))
        self.judged.clear()
        self.claims = [self.claim('standing_exclusion', 'never use the tracker')]
        self.assertIsNone(self.pretool('Edit', {'file_path': str(self.root / 'notes.md'), 'old_string': '',
                                                'new_string': '- never use the tracker'}))
        self.assertIsNone(self.pretool('apply_patch', {'input': patch.replace('AGENTS.md', 'notes.md')}))
        self.assertEqual(self.judged, [])  # only rule-file writes are judged
        self.kind = 'standing_instruction'  # separate explicit operator authority, in a later turn
        self.operator('From now on, never use the tracker in any job.')
        self.assertIsNone(self.pretool('Edit', rule))
        self.assertIsNone(self.pretool('apply_patch', {'input': patch}))

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
