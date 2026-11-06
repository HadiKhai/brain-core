"""Declared launch policy refuses at the tool call; hosts are faked, no AI or host is called."""
import contextlib
import io
import json
import os
import subprocess
import sys
import time
from unittest import mock

import core_loader
from scenario import CORE, Scenario


POLICY = {
    'launch_allow_coordinator': 'main-agent/model-a',
    'launch_effort_coordinator': 'high',
    'launch_allow_worker': 'code-agent/model-b, main-agent/model-a',
    'launch_effort_worker': 'high',
    'launch_forbid': 'model-retired, retired-account',
    'launch_cap_main-agent': '4',
    'thread_pointers': 'lander.md',
}


def selection(instance, model, effort='high'):
    return {'instanceId': instance, 'model': model, 'options': {'reasoningEffort': effort}}


class PolicyFixture(Scenario):
    fixture_init_in_process = True
    policy = POLICY

    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(os.environ, self.env)
        patch.start()
        self.addCleanup(patch.stop)
        self.root = self.init('meadow')
        self.b(self.root, 'new', 'app', '--title', 'App')
        self.b(self.root, 'set', 'app', 'coordinator=fixture-parent', 'home_project=fixture-project')
        path = self.root / 'instance.md'
        text = path.read_text()
        path.write_text(text.replace('---\n', '---\n' + ''.join(f'{k}: {v}\n' for k, v in self.policy.items()), 1))
        self.m = core_loader.SourceFileLoader('launch_policy_fixture', str(CORE / 'bin/brain')).load_module()
        self.inst = self.m.Instance(self.root)
        self.jobs = []

    def pretool(self, tool, arguments, cwd=None):
        data = {'cwd': str(cwd or self.root), 'session_id': 'fixture-session',
                'tool_name': 'mcp__t3-code__' + tool, 'tool_input': arguments}
        with mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(data))), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.m.cmd_hook_pretool(None)
        text = out.getvalue()
        return json.loads(text)['hookSpecificOutput']['permissionDecisionReason'] if text else None

    def launch(self, chosen, message='Role: worker in node `app`.\n', cwd=None):
        args = {'title': 'fixture', 'message': message}
        if chosen is not None:
            args['modelSelection'] = chosen
        return self.pretool('t3_thread_launch', args, cwd)

    def ledger(self, *branches, ended=()):
        rows = ''.join(f'| {b} | 2026-01-01T00:0{i}:00+00:00 | {"2026-01-02T00:00:00+00:00" if b in ended else ""} |\n'
                       for i, b in enumerate(branches))
        (self.root / 'nodes/app/job-ledger.md').write_text(
            '# Jobs: app\n\n' + self.m._owners['JOBS_MARK'].JOBS_MARK + '\n\n| Job | Started | Ended |\n| --- | --- | --- |\n' + rows)
        return {b: self.m.job_identity(b, f'2026-01-01T00:0{i}:00+00:00') for i, b in enumerate(branches)}

    def brief(self, key):
        return f'Role: worker in node `app`.\nbrain-job: {key} (lets `brain jobs` count this job)\n'

    def observe(self, threads, **arguments):
        """The PostToolUse hook after an agent's t3_thread_list call."""
        args = {'allProjects': True, 'includeSubagents': True, 'limit': 100,
                'statuses': ['preparing', 'queued', 'starting', 'running', 'waiting'], **arguments}
        response = [{'type': 'text', 'text': json.dumps({'threads': threads, 'total': len(threads), 'nextCursor': None})}]
        data = {'cwd': str(self.root), 'tool_name': 'mcp__t3-code__t3_thread_list', 'tool_input': args,
                'tool_response': response}
        with mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(data))):
            self.m.cmd_hook_posttool(None)

    def live(self, *names, provider='main-agent', status='running'):
        return [{'threadId': name, 'providerInstanceId': provider, 'status': status, 'title': name} for name in names]


class RolePolicy(PolicyFixture):
    def test_coordinator_initial_launch_and_rotation_require_allowed_account_model_effort(self):
        coordinator = 'Role: coordinator of node app.\n'
        self.assertIn('explicit provider instance and model', self.launch(None, coordinator))
        self.assertIn('forbidden', self.launch(selection('retired-account', 'model-a'), coordinator))
        self.assertIn('not an allowed coordinator model', self.launch(selection('code-agent', 'model-b'), coordinator))
        self.assertIn('effort low', self.launch(selection('main-agent', 'model-a', 'low'), coordinator))
        self.assertIsNone(self.launch(selection('main-agent', 'model-a'), coordinator))
        rotation = 'native-rotation: ticket\n\nStart here.\n'
        self.assertIn('not an allowed coordinator model', self.launch(selection('code-agent', 'model-b'), rotation))
        # Core paths: refused before any host call; a rotation uses the declared selection, never a host default.
        with mock.patch.object(self.m, 'host_call') as call, self.assertRaisesRegex(ValueError, 'forbidden'):
            self.m.provision_coordinator(self.inst, 'app', selection('retired-account', 'model-a'))
        call.assert_not_called()
        observed = {'providerInstanceId': 'retired-account', 'model': 'model-a'}
        self.assertEqual(self.m.coordinator_selection(self.inst, self.inst.node('app'), observed),
                         selection('main-agent', 'model-a'))

    def test_worker_review_and_diagnosis_routes_obey_effective_model_and_effort_policy(self):
        self.assertIn('forbidden', self.launch(selection('code-agent', 'model-retired')))
        self.assertIn('effort medium', self.launch(selection('code-agent', 'model-b', 'medium')))
        self.assertIn('explicit provider', self.launch(None))
        self.assertIsNone(self.launch(selection('code-agent', 'model-b')))
        review = {'task': 'Role: worker, reviewing a change you didn\'t write.\n', 'role': 'review'}
        self.assertIn('inherited model', self.pretool('delegate_task', review))
        self.assertIn('forbidden', self.pretool('delegate_task', {**review, 'target': {
            'providerInstanceId': 'code-agent', 'model': 'model-retired', 'options': {'reasoningEffort': 'high'}}}))
        self.assertIsNone(self.pretool('delegate_task', {**review, 'target': {
            'providerInstanceId': 'code-agent', 'model': 'model-b', 'options': [{'id': 'reasoningEffort', 'value': 'high'}]}}))
        # The nearest node declaration wins: this node keeps its builders off the capped provider.
        node = self.root / 'nodes/app/node.md'
        node.write_text(node.read_text().replace('---\n', '---\nlaunch_allow_worker: code-agent/model-b\n', 1))
        self.assertIn('not an allowed worker model', self.launch(selection('main-agent', 'model-a'), cwd=self.root / 'nodes/app'))
        calls = []
        def rpc(name, args):
            calls.append(name)
            return {'currentThreadId': 'fixture-parent'}
        with self.assertRaisesRegex(ValueError, 'forbidden'):
            self.m.host_delegate(self.inst, self.inst.node('app'), 'Inspect a sample',
                                 {'providerInstanceId': 'code-agent', 'model': 'model-retired'}, 'k', rpc=rpc)
        self.assertNotIn('delegate_task', calls)

    def test_undeclared_policy_restricts_nothing(self):
        path = self.root / 'instance.md'
        path.write_text(''.join(line for line in path.read_text().splitlines(True)
                                if not line.startswith(('launch_', 'thread_pointers'))))
        self.assertIsNone(self.launch(None))
        self.assertIsNone(self.launch(selection('any-agent', 'any-model', 'low')))


class SubscriptionAdmission(PolicyFixture):
    def test_fifth_builder_or_reviewer_is_refused_coordinators_are_excluded_and_only_ordered_deliverables_admit(self):
        keys = self.ledger('job-a', 'job-b', 'job-c', 'job-d', 'job-e')
        main = selection('main-agent', 'model-a')
        # Without a fresh complete host count the capped provider admits nothing.
        self.assertIn('no fresh host count', self.launch(main, self.brief(keys['job-a'])))
        self.observe(self.live('builder-1', 'builder-2') + self.live('reviewer-1', status='waiting')
                     + self.live('fixture-parent')                 # the bound coordinator is not counted
                     + self.live('other-1', provider='code-agent'))  # other providers are not counted
        self.assertIsNone(self.launch(main, self.brief(keys['job-a'])))       # 3 live + this = 4
        refused = self.launch(main, self.brief(keys['job-a']))                 # same job again is a fifth thread
        self.assertIn('cap 4 is full', refused)
        self.assertIn('builder-1', refused)
        self.assertIn('admitted job-a', refused)
        review = {'task': self.brief(keys['job-b']), 'target': {'providerInstanceId': 'main-agent', 'model': 'model-a',
                                                                'options': {'reasoningEffort': 'high'}}}
        self.assertIn('cap 4 is full', self.pretool('delegate_task', review))
        # Coordinators are never counted, even at the cap.
        self.assertIsNone(self.launch(main, 'Role: coordinator of node app.\n'))
        # A new complete count replaces the admitted guesses: two finished, so two may start.
        self.observe(self.live('builder-1', 'new-a') + self.live('builder-2', status='completed'))
        self.assertIsNone(self.launch(main, self.brief(keys['job-c'])))
        self.assertIsNone(self.pretool('delegate_task', review))
        self.assertIn('cap 4 is full', self.launch(main, self.brief(keys['job-d'])))
        # Partial or filtered lists never count as a complete observation.
        self.observe(self.live('builder-1'), projectId='one-project')
        self.assertIn('cap 4 is full', self.launch(main, self.brief(keys['job-d'])))
        # A stale count admits nothing.
        state = self.root / '.brain/live-threads.json'
        data = json.loads(state.read_text())
        data['observed_at'] = '2020-01-01T00:00:00+00:00'
        state.write_text(json.dumps(data))
        self.assertIn('no fresh host count', self.launch(main, self.brief(keys['job-d'])))
        # Polling or ceremony carries no open job: refused before the host call, even with free slots.
        self.observe([])
        self.assertIn('ordered deliverables', self.launch(main, 'Check the queue.\n'))
        self.assertIn('ordered deliverables', self.launch(main, self.brief('0' * 24)))
        self.assertIsNone(self.launch(selection('code-agent', 'model-b'), 'Role: worker in node `app`.\n'))
        # Unreadable admission state never admits.
        state.write_text('[]')
        self.assertIn('state unavailable', self.launch(main, self.brief(keys['job-d'])))

    def test_core_paths_count_live_host_threads_before_their_host_call(self):
        keys = self.ledger('job-a')
        n = self.inst.node('app')
        live = self.live('builder-1', 'builder-2', 'builder-3', 'builder-4') + self.live('fixture-parent')
        def rpc(name, args):
            self.assertEqual(name, 't3_thread_list')
            self.assertTrue(args['allProjects'] and args['includeSubagents'])
            return {'threads': live, 'total': len(live), 'nextCursor': None}
        with self.assertRaisesRegex(ValueError, 'cap 4 is full'):
            self.m.require_admission(self.inst, n, 'worker', selection('main-agent', 'model-a'), '', rpc=rpc, job=keys['job-a'])
        live.pop(0)
        self.m.require_admission(self.inst, n, 'worker', selection('main-agent', 'model-a'), '', rpc=rpc, job=keys['job-a'])
        def partial(name, args):
            return {'threads': live[:1], 'total': len(live), 'nextCursor': None}
        with self.assertRaisesRegex(ValueError, 'exhaustion'):
            self.m.require_admission(self.inst, n, 'worker', selection('main-agent', 'model-a'), '', rpc=partial, job=keys['job-a'])


class DelegationRouteRecovery(PolicyFixture):
    policy = {k: v for k, v in POLICY.items() if not k.startswith('launch_cap_')}

    def setUp(self):
        super().setUp()
        self.calls = []
        self.delegate = 'rejected'
        self.launched = {'threadId': 'visible-1'}
        self.observed_model = 'model-b'

    def rpc(self, name, args):
        self.calls.append((name, args))
        if name == 't3_thread_list':
            return {'currentThreadId': 'fixture-parent'}
        if name == 'delegate_task':
            if self.delegate == 'rejected':
                raise self.m.HostRPCRejected('delegate_task: native host tool rejected the request')
            raise self.m.HostRPCError('delegate_task: native host timeout; delivery outcome unknown')
        if name == 't3_thread_launch':
            if isinstance(self.launched, Exception):
                raise self.launched
            return self.launched
        self.assertEqual(name, 't3_thread_read')
        return {'thread': {'threadId': args['threadId'], 'providerInstanceId': 'code-agent', 'model': self.observed_model}}

    def register(self, key='one-task'):
        target = {'providerInstanceId': 'code-agent', 'model': 'model-b', 'options': {'reasoningEffort': 'high'}}
        return self.m.host_delegate(self.inst, self.inst.node('app'), 'Review the sample change', target, key, rpc=self.rpc)

    def names(self):
        return [name for name, _ in self.calls]

    def test_confirmed_delegation_route_failure_recovers_same_work_model_and_visible_binding_once(self):
        row = self.register()
        self.assertEqual(row['recovery']['thread'], 'visible-1')
        self.assertEqual(row['recovery']['phase'], 'launched')
        launch = next(args for name, args in self.calls if name == 't3_thread_launch')
        self.assertEqual(launch['modelSelection'], selection('code-agent', 'model-b'))
        self.assertEqual(launch['projectId'], 'fixture-project')
        self.assertIn('Review the sample change', launch['message'])
        self.assertTrue(launch['message'].startswith('delegate-recovery: '))
        self.register()
        self.assertEqual(self.names().count('delegate_task'), 1)
        self.assertEqual(self.names().count('t3_thread_launch'), 1)

    def test_unknown_outcomes_are_never_recovered_or_repeated(self):
        self.delegate = 'unknown'
        with self.assertRaises(self.m.HostRPCError):
            self.register('unknown-delegate')
        self.assertNotIn('t3_thread_launch', self.names())
        self.delegate = 'rejected'
        self.launched = self.m.HostRPCError('t3_thread_launch: native host timeout')
        with self.assertRaisesRegex(ValueError, 'unknown'):
            self.register('lost-launch')
        self.launched = {'threadId': 'visible-2'}
        with self.assertRaisesRegex(ValueError, 'no repeat'):
            self.register('lost-launch')
        self.assertEqual(self.names().count('t3_thread_launch'), 1)

    def test_recovered_thread_on_another_model_is_not_adopted(self):
        self.observed_model = 'model-other'
        with self.assertRaisesRegex(ValueError, 'different|does not run'):
            self.register()
        with self.assertRaisesRegex(ValueError, 'different provider or model'):
            self.register()
        self.assertEqual(self.names().count('t3_thread_launch'), 1)


class LanderPointer(PolicyFixture):
    def point(self, line, commit=False):
        path = self.root / 'nodes/app/lander.md'
        path.write_text(line + '\n\nHistory below the first line is prose.\n')
        if commit:
            self.git('add', str(path), cwd=self.root)
            self.git('commit', '-qm', 'fixture pointer', cwd=self.root)

    def send(self, thread):
        return self.pretool('t3_thread_send', {'threadId': thread, 'message': 'Land the approved batch.'})

    def test_send_to_replaced_pointer_thread_is_refused_and_names_the_current_one(self):
        # The first send after a replacement is refused: the former pointer is in the file's history.
        self.point('Lander: [first lander](t3-thread://v1/thread-a)', commit=True)
        self.point('Lander: [second lander](t3-thread://v2/env-1/thread-b)')
        refused = self.send('thread-a')
        self.assertIn('app/lander.md', refused)
        self.assertIn('t3-thread://v2/env-1/thread-b', refused)
        self.assertIn('no longer the one', self.send('t3-thread://v1/thread-a'))
        self.assertIsNone(self.send('thread-b'))
        self.assertIsNone(self.send('unrelated-thread'))
        # An uncommitted pointer is learned before an agent edits the file.
        self.point('Lander: thread-c')
        self.assertIsNone(self.pretool('Edit', {'file_path': str(self.root / 'nodes/app/lander.md'),
                                                'old_string': 'thread-c', 'new_string': 'thread-d'}))
        self.point('Lander: thread-d')
        self.assertIn('names the current one: Lander: thread-d', self.send('thread-c'))
        # A pointer line that names no single thread refuses sends to any thread it named.
        self.point('Lander: thread-e or thread-f')
        self.assertIn('does not name one thread', self.send('thread-b'))
        self.point('Lander: thread-b')
        self.assertIsNone(self.send('thread-b'))


def declared_keys():
    """The instance-key lines to test: an applied patch's instance.md additions if one is named, else POLICY.

    BRAIN_INSTANCE_KEYS_PATCH names a unified diff whose `+++ b/instance.md` hunk adds the
    keys; the same assertions then prove the exact declarations an instance will apply.
    """
    path = os.environ.get('BRAIN_INSTANCE_KEYS_PATCH')
    if not path:
        return dict(POLICY)
    keys, current = {}, None
    for line in open(path).read().splitlines():
        if line.startswith('+++ '):
            current = line[4:].split('\t')[0].strip()
        elif current in ('b/instance.md', 'instance.md') and line.startswith('+') and ':' in line:
            key, value = line[1:].split(':', 1)
            if key.startswith(('launch_', 'thread_pointers')):
                keys[key.strip()] = value.strip()
    return keys


class InstanceKeys(PolicyFixture):
    policy = {}

    def declare(self, keys):
        path = self.root / 'instance.md'
        path.write_text(path.read_text().replace('---\n', '---\n' + ''.join(f'{k}: {v}\n' for k, v in keys.items()), 1))

    def test_declared_instance_keys_refuse_at_the_call(self):
        keys = declared_keys()
        allowed = {role: [e.split('/', 1) for e in self.m.lst(keys.get('launch_allow_' + role, ''))]
                   for role in ('coordinator', 'worker')}
        efforts = {role: self.m.lst(keys.get('launch_effort_' + role, '')) for role in ('coordinator', 'worker')}
        forbidden = self.m.lst(keys.get('launch_forbid', ''))
        self.assertTrue(allowed['coordinator'] and allowed['worker'] and efforts['coordinator'] and efforts['worker']
                        and forbidden, 'the declarations must cover both roles and a forbidden list')
        coordinator = 'Role: coordinator of node app.\n'
        worker_instance, worker_model = allowed['worker'][0]
        off_policy = [selection(f, worker_model, efforts['worker'][0]) for f in forbidden] + \
                     [selection(worker_instance, f, efforts['worker'][0]) for f in forbidden] + \
                     [selection(worker_instance, worker_model, 'low'), None]
        # Absent declarations: dormant, every launch passes.
        for chosen in off_policy:
            self.assertIsNone(self.launch(chosen))
        self.declare(keys)
        for chosen in off_policy:
            with self.subTest(chosen=chosen):
                self.assertIsNotNone(self.launch(chosen))
                self.assertIsNotNone(self.launch(chosen, coordinator))
        inst = self.m.Instance(self.root)
        for instance, model in allowed['worker']:  # admission (caps) is checked below
            self.assertEqual(self.m.selection_reasons(inst, inst.node('app'), 'worker',
                                                      selection(instance, model, efforts['worker'][0])), [])
        instance, model = allowed['coordinator'][0]
        self.assertIsNone(self.launch(selection(instance, model, efforts['coordinator'][0]), coordinator))
        self.assertIsNotNone(self.launch(selection(instance, model, 'low'), coordinator))
        # Each declared cap: a fresh count at the cap refuses the next ordered launch.
        caps = {k[len('launch_cap_'):]: int(v) for k, v in keys.items() if k.startswith('launch_cap_')}
        self.assertTrue(caps, 'the declarations must cap a provider')
        key = self.ledger('job-a')['job-a']
        for capped, limit in caps.items():
            model = next(m for i, m in allowed['worker'] if i == capped)
            live = [{'threadId': f'live-{i}', 'providerInstanceId': capped, 'status': 'running'} for i in range(limit)]
            self.observe(live)
            self.assertIn(f'cap {limit} is full', self.launch(selection(capped, model, efforts['worker'][0]), self.brief(key)))
        # Each declared pointer file: the first send to a replaced thread is refused.
        for name in self.m.lst(keys.get('thread_pointers', '')):
            path = self.root / 'nodes/app' / name
            path.write_text('Pointer: thread-old\n')
            self.git('add', str(path), cwd=self.root)
            self.git('commit', '-qm', 'fixture pointer', cwd=self.root)
            path.write_text('Pointer: thread-new\n')
            self.assertIn('thread-new', self.pretool('t3_thread_send', {'threadId': 'thread-old', 'message': 'x'}))


class StopHookTiming(Scenario):
    """A turn that does not answer the operator finishes the Stop hook's checks in under 1 s."""
    fixture_init_in_process = True
    budget = 1.0

    def fixture(self):
        root = self.init('meadow')
        for i in range(6):
            self.b(root, 'new', f'area-{i}', '--title', f'Area {i}')
        self.b(root, 'set', 'area-0', 'coordinator=fixture-coordinator')
        m = core_loader.SourceFileLoader('stop_timing_fixture', str(CORE / 'bin/brain')).load_module()
        inst = m.Instance(root)
        inst.local_of('area-0')['coordinator_sessions'] = ['coordinator-session']
        inst.save_local()
        rows = ''.join(f'| job-{i} | 2026-01-01T00:00:{i % 60:02d}+00:00 | 2026-01-02T00:00:00+00:00 | 1 | merged |\n'
                       for i in range(600))
        (root / 'nodes/area-0/job-ledger.md').write_text(
            '# Jobs: area-0\n\n' + m._owners['JOBS_MARK'].JOBS_MARK +
            '\n\n| Job | Started | Ended | Hours | Outcome |\n| --- | --- | --- | --- | --- |\n' + rows)
        (root / 'nodes/area-0/inbox.md').write_text(''.join(f'- 2026-01-01 00:00 note {i}\n' for i in range(200)))
        transcript = self.tmp / 'automated.jsonl'
        records = []
        for i in range(1500):
            records.append({'type': 'user', 'timestamp': '2026-01-01T00:00:00Z', 'isMeta': False,
                            'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': f't{i}',
                                                                      'content': 'fixture output ' * 20}]}})
            records.append({'type': 'assistant', 'timestamp': '2026-01-01T00:00:01Z',
                            'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'Working. ' * 30}]}})
        transcript.write_text(''.join(json.dumps(r) + '\n' for r in records))
        return root, transcript

    def stop_seconds(self, root, payload):
        runs = []
        for _ in range(3):
            start = time.monotonic()
            result = subprocess.run([sys.executable, str(root / 'core/bin/brain'), 'hook', 'stop'], cwd=payload['cwd'],
                                    env={**self.env, 'ANTHROPIC_API_KEY': ''}, input=json.dumps(payload),
                                    capture_output=True, text=True, timeout=30)
            runs.append(time.monotonic() - start)
            self.assertEqual(result.returncode, 0, result.stderr)
        return runs

    def test_non_operator_turn_stop_checks_finish_under_one_second(self):
        root, transcript = self.fixture()
        cases = {
            'coordinator automated turn': {'cwd': str(root / 'nodes/area-0'), 'session_id': 'coordinator-session',
                                           'transcript_path': str(transcript), 'last_assistant_message': 'Done.'},
            'worker turn in node folder': {'cwd': str(root / 'nodes/area-1'), 'session_id': 'worker-session',
                                           'transcript_path': str(transcript), 'last_assistant_message': 'Done.'},
        }
        timings = {name: self.stop_seconds(root, payload) for name, payload in cases.items()}
        print('stop hook seconds: ' + json.dumps({k: [round(v, 3) for v in runs] for k, runs in timings.items()}),
              file=sys.stderr)
        for name, runs in timings.items():
            for seconds in runs:  # every run must meet the budget, not the fastest
                self.assertLess(seconds, self.budget, f'{name}: Stop hook took {seconds:.3f} s')
