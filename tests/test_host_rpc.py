"""Native tool transport protocol checks; every bridge process is mocked."""
import json
import os
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

import core_loader
from scenario import CORE


class HostRPC(unittest.TestCase):
    def setUp(self):
        core = core_loader.SourceFileLoader('host_rpc_fixture', str(CORE / 'bin/brain')).load_module()
        self.rpc = core._owners['HostRPCError']
        env = mock.patch.dict(os.environ, {'T3_ACP_MCP_NODE': sys.executable}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def response(self, data, code=0):
        return SimpleNamespace(returncode=code, stdout=json.dumps(data), stderr='PRIVATE-ERROR-CONTENT')

    def test_native_arguments_and_decoding_use_no_shell(self):
        for value in ({'structuredContent': {'threads': []}},
                      {'content': [{'type': 'text', 'text': '{"threads":[]}'}]},
                      {'jsonrpc': '2.0', 'result': {'structuredContent': {'threads': []}}}):
            with self.subTest(value=value), mock.patch.object(self.rpc.subprocess, 'run', return_value=self.response(value)) as run:
                self.assertEqual(self.rpc.call('mcp__t3_code__t3_thread_list', {'projectId': 'fixture-project'}), {'threads': []})
                args, options = run.call_args
                self.assertEqual(args[0][1:3], ['acp-mcp-call', 't3_thread_list'])
                self.assertEqual(json.loads(args[0][-1]), {'projectId': 'fixture-project'})
                self.assertNotIn('shell', options)
                self.assertEqual(options['env']['ELECTRON_RUN_AS_NODE'], '1')
                self.assertEqual(options['timeout'], 30)

    def test_organization_refuses_omitted_plural_and_unknown_targets_before_transport(self):
        for args in ({'action': 'settle'}, {'action': 'settle', 'threadIds': ['fixture']},
                     {'action': 'settle', 'threadId': ''},
                     {'action': 'settle', 'threadId': 'fixture', 'threadIds': ['other']}):
            with self.subTest(args=args), mock.patch.object(self.rpc.subprocess, 'run') as run:
                with self.assertRaises(self.rpc.HostRPCError):
                    self.rpc.call('t3_thread_organize', args)
                run.assert_not_called()
        with mock.patch.object(self.rpc.subprocess, 'run', return_value=self.response({'settled': True})) as run:
            self.rpc.call('t3_thread_organize', {'action': 'pin', 'threadId': 'fixture'})
            self.assertEqual(json.loads(run.call_args.args[0][-1]), {'action': 'pin', 'threadId': 'fixture'})

    def test_unavailable_invalid_and_nonfinite_requests_do_not_execute(self):
        with mock.patch.object(self.rpc.subprocess, 'run') as run:
            for tool, args in (('name; command', {}), ('t3_thread_read', []), ('t3_thread_read', {'value': float('nan')})):
                with self.assertRaises(self.rpc.HostRPCError):
                    self.rpc.call(tool, args)
            with mock.patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(self.rpc.HostRPCError, 'unavailable'):
                self.rpc.call('t3_thread_read', {})
            run.assert_not_called()

    def test_timeout_and_errors_never_retry_or_disclose_content(self):
        outcomes = [subprocess.TimeoutExpired('fixture', 30),
                    self.response({}, 7), self.response({'isError': True}),
                    self.response({'jsonrpc': '2.0', 'error': {'message': 'PRIVATE-ERROR-CONTENT'}}),
                    SimpleNamespace(returncode=0, stdout='not JSON', stderr='PRIVATE-ERROR-CONTENT')]
        for outcome in outcomes:
            with self.subTest(outcome=type(outcome).__name__):
                kwargs = {'side_effect': outcome} if isinstance(outcome, Exception) else {'return_value': outcome}
                with mock.patch.object(self.rpc.subprocess, 'run', **kwargs) as run:
                    with self.assertRaises(self.rpc.HostRPCError) as error:
                        self.rpc.call('t3_thread_organize', {'action': 'pin', 'threadId': 'fixture'})
                    self.assertNotIn('PRIVATE-ERROR-CONTENT', str(error.exception))
                    self.assertEqual(run.call_count, 1)

    def test_native_settlement_without_atomic_predicate_never_reaches_transport(self):
        with mock.patch.object(self.rpc.subprocess, 'run') as run:
            with self.assertRaisesRegex(self.rpc.HostRPCError, 'atomic settlement unsupported'):
                self.rpc.call('t3_thread_organize', {'action': 'settle', 'threadId': 'fixture'})
            run.assert_not_called()
