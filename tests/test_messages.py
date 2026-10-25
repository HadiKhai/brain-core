"""Made-up worker commands: four kinds, optional context and direct peer briefs."""
import re

from scenario import Scenario, CORE


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
            self.assertTrue(out.startswith(kind + ": Synthetic message"), out)

    def test_formatter_and_briefs(self):
        root, _ = self.job()
        for kind in ("start", "done", "blocked", "question"):
            out = self.b(root, "msg", "a", kind, "One\nline", "--branch", "job/sample", "--report", "~/reports/example.md")
            self.assertEqual(out.strip(), kind + ": One line [branch: job/sample; report: ~/reports/example.md]\nNOT SENT: use the host send")
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
                self.assertTrue(output.startswith(kind + ": Made-up result"), output)
                if options == ("--branch", "job/sample"):
                    self.assertIn("[branch: job/sample; report:", output)
                else:
                    self.assertEqual(output.strip(), kind + ": Made-up result [branch: main]\nNOT SENT: use the host send")
        # A generic worker may be outside Git entirely, with no ledger job or report.
        plain = self.tmp / "plain"
        plain.mkdir()
        for kind in ("start", "done", "blocked", "question"):
            output = self.run_(["msg", "a", kind, "Made-up result"], cwd=plain, brain=CORE / "bin/brain")
            self.assertEqual(output, kind + ": Made-up result\nNOT SENT: use the host send\n")
        self.assertEqual(self.b(root, "msg", "a", "done", "Result", "--branch", "other").strip(),
                         "done: Result [branch: other]\nNOT SENT: use the host send")

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
        contract.write_text("".join("## " + field + "\nSynthetic scope.\n\n" for field in
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
                         "--report", "~/reports/sample.md", "--lessons", "none: made-up fixture")
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
            text = m.checked_brief(inst.node('root'), 'Role: worker\nSynthetic task', inputs=[])
            self.assertTrue(text.startswith(marker))
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

    def spec(self, action='routine'):
        return {'options': [{'id': 'continue', 'label': 'Continue', 'action': action},
                            {'id': 'wait', 'label': 'Wait', 'action': 'routine'}],
                'recommendation': 'continue', 'default': 'continue',
                'deadline': '2020-01-01T00:00:00+00:00'}

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

    def test_typed_question_inflight_baseline_excludes_previously_shown_question(self):
        from unittest import mock
        m, n = self.m, self.inst.node('root')
        with mock.patch.object(m, 'wake_start'), mock.patch.object(m, 'WAKE_INTERVAL', 0):
            m.communication_record(self.inst, n, 'question', 'First fixture question', 'first', self.spec('outward'))
            with mock.patch.object(m, 'wake_send', return_value=True):
                m.wake_drain(self.inst, n, once=True)
            m.communication_record(self.inst, n, 'question', 'Second fixture question', 'second', self.spec('outward'))
            observed = []
            def send(*args, **kwargs):
                observed.extend(m.watch_since_wake(self.inst, [n]))
                return True
            with mock.patch.object(m, 'wake_send', side_effect=send):
                m.wake_drain(self.inst, n, once=True)
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
            self.assertEqual(send.call_count, 1)

    def test_failed_enqueue_is_not_a_dispatch_receipt(self):
        from unittest import mock
        self.question()
        with mock.patch.object(self.m, 'request_wake', side_effect=[False, True]) as enqueue:
            self.assertEqual(self.m.dispatch_run(self.inst)['accepted'], 0)
            self.assertEqual(self.m.dispatch_run(self.inst)['accepted'], 1)
            self.assertEqual(self.m.dispatch_run(self.inst)['accepted'], 0)
            self.assertEqual(enqueue.call_count, 2)
