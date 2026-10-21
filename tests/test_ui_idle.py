"""The live view reuses snapshots; fabricated brains and transcripts only."""
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from scenario import Scenario, CORE


class IdleView(Scenario):
    def setUp(self):
        super().setUp()
        self.servers = []

    def tearDown(self):
        for process in self.servers:
            self.stop_server(process)
        super().tearDown()

    def start_server(self, transcript=False, partial=False, projects=False):
        config = self.tmp / 'config'
        config.mkdir(exist_ok=True)
        self.env['BRAIN_CONFIG'] = str(config / 'brains.json')
        root = self.init('demo')
        if projects:
            for name in ('amber', 'indigo'):
                home = self.repo(self.tmp / name)
                self.b(root, 'new', name, '--title', name.title(), '--home', str(home))
                self.b(root, 'wt', 'add', name, 'job-demo')
            import socket
            path = root / '.brain/local.json'
            local = json.loads(path.read_text())
            for name in ('amber', 'indigo'):
                with socket.socket() as reservation:
                    reservation.bind(('127.0.0.1', 0))
                    local['nodes'][name]['worktrees']['job-demo']['port'] = reservation.getsockname()[1]
            path.write_text(json.dumps(local))
        if transcript:
            cfg = self.tmp / 'runtime'
            folder = cfg / 'sessions'
            folder.mkdir(parents=True)
            (config / 'subscriptions.json').write_text(json.dumps({'subscriptions': [
                {'id': 'demo', 'runtime': 'codex', 'config_dir': str(cfg), 'brains': ['*']}]}))
            stamp = '2026-10-21T00:00:00Z'
            rows = [
                {'timestamp': stamp, 'type': 'session_meta', 'payload': {
                    'id': '00000000-0000-4000-8000-000000000001', 'cwd': str(root)}},
                {'timestamp': stamp, 'type': 'turn_context', 'payload': {'model': 'demo-model'}},
                {'timestamp': stamp, 'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
                    'total_token_usage': {'input_tokens': 20, 'cached_input_tokens': 0, 'output_tokens': 5},
                    'last_token_usage': {'input_tokens': 20, 'cached_input_tokens': 0, 'output_tokens': 5}}}}]
            (folder / 'demo.jsonl').write_text(''.join(json.dumps(row, separators=(',', ':')) + '\n' for row in rows))
            if partial:
                text = (folder / 'demo.jsonl').read_text()
                for i in range(6):
                    (folder / f'chunk-{i}.jsonl').write_text(text * 3)
        script = '''
import sys
sys.path.insert(0, sys.argv[1] + '/lib')
from brain import ui, cli
build = ui.ui_state
if hasattr(ui, 'UI_TELEMETRY_SECONDS'):
    ui.UI_TELEMETRY_SECONDS = 0.05
partial = sys.argv[2] == 'partial'
if hasattr(ui, 'UISpend'):
    ui.UISpend.PUBLISH_EVERY = 0
    if partial:
        ui.UISpend.BUDGET = 0.005
if partial:
    import time
    from brain import spend
    real_file = spend.spend_file
    def bounded_file(*args, **kwargs):
        time.sleep(0.001)  # deterministic per-file cost exceeding the injected slice
        kwargs['budget'] = 0.000001
        return real_file(*args, **kwargs)
    spend.spend_file = bounded_file
node_calls = {}
summary = ui._m_jobs.jobs_summary
def counted_node(inst, node):
    import inspect
    if inspect.currentframe().f_back.f_code.co_name in ('_ui_state', 'ui_node'):
        node_calls[node['id']] = node_calls.get(node['id'], 0) + 1
    return summary(inst, node)
ui._m_jobs.jobs_summary = counted_node
check = ui._m_check.collect_problems
checks = 0
def counted_check(*args, **kwargs):
    global checks
    checks += 1
    return check(*args, **kwargs)
ui._m_check.collect_problems = counted_check
ready_calls = {}
ready_name = '_ready_state' if hasattr(ui._m_verify, '_ready_state') else 'ready_state'
ready = getattr(ui._m_verify, ready_name)
def counted_ready(inst, node, branch, *args, **kwargs):
    key = node['id'] + ':' + branch
    ready_calls[key] = ready_calls.get(key, 0) + 1
    return ready(inst, node, branch, *args, **kwargs)
setattr(ui._m_verify, ready_name, counted_ready)
signature = ui.UIInputs.signature
def fast_signature(self):
    self.slow_at = float('-inf')  # exercise file discovery without a 15-second test wait
    return signature(self)
ui.UIInputs.signature = fast_signature
calls = 0
def counted(*args, **kwargs):
    global calls
    calls += 1
    if partial:
        kwargs['spend_budget'] = 0.000001
    result = build(*args, **kwargs)
    result['_test_builds'] = calls
    result['_test_node_builds'] = dict(node_calls)
    result['_test_checks'] = checks
    result['_test_ready_calls'] = dict(ready_calls)
    result['_test_initial_incomplete'] = not result['spend']['complete']
    return result
ui.ui_state = counted
sys.argv = ['brain', 'ui', '--port', '0', '--every', '0.05']
cli.main()
'''
        process = subprocess.Popen([sys.executable, '-u', '-c', script, str(CORE), 'partial' if partial else 'normal'], cwd=root,
                                   env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.servers.append(process)
        self.assertTrue(process.stdout.readline().startswith('brain ui core:'))
        address = process.stdout.readline().split(': ', 1)[1].split()[0]
        base, key = address.split('?k=')
        def state():
            with urllib.request.urlopen(base + 'api/state?k=' + key, timeout=10) as response:
                return json.load(response)
        return root, state

    @staticmethod
    def stop_server(process):
        process.terminate()
        process.communicate(timeout=5)

    def test_unchanged_reads_reuse_state_and_changed_inputs_are_published(self):
        root, state = self.start_server()
        before = state()['_test_builds']
        for _ in range(12):
            time.sleep(0.05)
            self.assertEqual(state()['_test_builds'], before, 'unchanged poll rebuilt expensive state')
        incoming = self.tmp / 'next-inbox'
        incoming.write_text('- 2026-10-21 00:00 from demo: question: A synthetic decision?\n')
        incoming.replace(root / 'inbox.md')
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            latest = state()
            if latest['_test_builds'] > before:
                break
            time.sleep(0.05)
        self.assertGreater(latest['_test_builds'], before)
        self.assertIn('A synthetic decision?', json.dumps(latest))
        # A metadata scan overlapping the atomic rename can require a catch-up pass.
        # Once the edit has settled, unchanged polls must stop rebuilding entirely.
        stable, since = latest['_test_builds'], time.monotonic()
        deadline = since + 5
        while time.monotonic() < deadline:
            time.sleep(0.05)
            count = state()['_test_builds']
            if count != stable:
                stable, since = count, time.monotonic()
            elif time.monotonic() - since >= 0.3:
                break
        else:
            self.fail('snapshot never settled after one input change')
        time.sleep(0.2)
        self.assertEqual(state()['_test_builds'], stable)

    def test_ui_counts_transcripts_without_writing_spend_cache(self):
        root, state = self.start_server(transcript=True)
        reading = state()
        self.assertTrue(reading['spend']['complete'])
        self.assertEqual(reading['spend']['read'], 1)
        self.assertFalse(list((root / '.brain' / 'spend').rglob('*.json')),
                         'a read-only dashboard wrote transcript aggregates')

    def test_machine_samples_and_spend_output_do_not_invalidate_source_snapshot(self):
        root, state = self.start_server()
        count = state()['_test_builds']
        cache = root / '.brain' / 'spend' / 'files'
        cache.mkdir(parents=True, exist_ok=True)
        for i in range(5):
            (root / '.brain' / 'machine.json').write_text(json.dumps({'sample': i}))
            (cache / 'demo.json').write_text(json.dumps({'cache': i}))
            time.sleep(0.05)
            self.assertEqual(state()['_test_builds'], count)

    def test_appended_transcript_invalidates_live_total_without_waiting_for_old_ttl(self):
        root, state = self.start_server(transcript=True)
        before = state()
        transcript = self.tmp / 'runtime/sessions/demo.jsonl'
        with transcript.open('a') as stream:
            stream.write(json.dumps({'timestamp': '2026-10-21T00:01:00Z', 'type': 'event_msg', 'payload': {
                'type': 'token_count', 'info': {
                    'total_token_usage': {'input_tokens': 40, 'cached_input_tokens': 0, 'output_tokens': 10},
                    'last_token_usage': {'input_tokens': 20, 'cached_input_tokens': 0, 'output_tokens': 5}}}}, separators=(',', ':')) + '\n')
        deadline = time.monotonic() + 5
        latest = before
        while time.monotonic() < deadline:
            latest = state()
            if latest['spend']['since_week'] and latest['spend']['since_week']['processed'] > before['spend']['since_week']['processed']:
                break
            time.sleep(0.05)
        self.assertGreater(latest['spend']['since_week']['processed'], before['spend']['since_week']['processed'])
        self.assertEqual(latest['_test_builds'], before['_test_builds'], 'transcript append rebuilt unrelated state')
        self.assertFalse(list((root / '.brain/spend').rglob('*.json')))

    def test_partial_spend_advances_without_source_changes_or_full_rebuilds(self):
        root, state = self.start_server(transcript=True, partial=True)
        before = state()
        self.assertTrue(before['_test_initial_incomplete'])
        deadline = time.monotonic() + 15
        latest = before
        while not latest['spend']['complete'] and time.monotonic() < deadline:
            time.sleep(0.05)
            latest = state()
        self.assertTrue(latest['spend']['complete'], 'in-memory partial spend never progressed')
        self.assertEqual(latest['spend']['read'], 7)
        self.assertEqual(latest['_test_builds'], before['_test_builds'])
        self.assertFalse(list((root / '.brain/spend').rglob('*.json')))

    def test_a_project_edit_reuses_other_nodes_and_does_not_repeat_integrity_scan(self):
        root, state = self.start_server(projects=True)
        before = state()
        (self.tmp / 'indigo/README.md').write_text('A fabricated project edit.\n')
        latest = state()
        self.assertGreater(latest['_test_builds'], before['_test_builds'])
        self.assertGreater(latest['_test_node_builds']['indigo'], before['_test_node_builds']['indigo'])
        self.assertEqual(latest['_test_node_builds']['amber'], before['_test_node_builds']['amber'],
                         'one project edit rebuilt an unrelated project')
        self.assertEqual(latest['_test_checks'], before['_test_checks'],
                         'a file edit repeated the whole-brain integrity scan')
        # New untracked paths must also reach the watch set and worktree dirty badge.
        local = json.loads((root / '.brain/local.json').read_text())
        worktree = Path(local['nodes']['indigo']['worktrees']['job-demo']['path'])
        (worktree / 'new-folder').mkdir()
        new = worktree / 'new-folder/new.txt'
        new.write_text('made-up first revision\n')
        latest = state()
        indigo = next(n for n in latest['nodes'] if n['id'] == 'indigo')
        self.assertEqual(indigo['worktrees'][0]['dirty'], 1)
        previous = latest['_test_node_builds']['indigo']
        new.write_text('made-up second revision, a little longer\n')
        latest = state()
        self.assertGreater(latest['_test_node_builds']['indigo'], previous,
                           'newly discovered file was lost from the watched inputs')
        self.assertEqual(latest['_test_node_builds']['amber'], before['_test_node_builds']['amber'])
        self.assertEqual(latest['_test_ready_calls']['amber:job-demo'], before['_test_ready_calls']['amber:job-demo'])
        # Declared ignored verify inputs affect readiness even though Git's file list omits them.
        (worktree / '.gitignore').write_text('.env\n')
        state()
        previous = state()['_test_ready_calls']['indigo:job-demo']
        (worktree / '.env').write_text('DEMO_VALUE=made-up\n')
        self.assertGreater(state()['_test_ready_calls']['indigo:job-demo'], previous)

    def test_display_readiness_cache_is_scoped_and_full_validation_is_never_cached(self):
        import contextlib
        import core_loader
        from unittest import mock
        import importlib.util
        loader = core_loader.SourceFileLoader('brain_idle_cache_test', str(CORE / 'bin/brain'))
        core = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(core)
        verify = core._owners['ready_state']
        values, keys = {}, {('demo', 'job-demo'): ('original-inputs',)}
        node = {'id': 'demo'}
        implementation = '_ready_state' if hasattr(verify, '_ready_state') else 'ready_state'
        with mock.patch.object(verify, implementation, return_value={'ready': False}) as read:
            scope = verify.display_cache(values, keys) if hasattr(verify, 'display_cache') else contextlib.nullcontext()
            with scope:
                verify.ready_state(None, node, 'job-demo', display=True)
                verify.ready_state(None, node, 'job-demo', display=True)
                self.assertEqual(read.call_count, 1, 'unchanged display validation repeated readiness work')
                keys[('demo', 'job-demo')] = ('changed-inputs',)
                verify.ready_state(None, node, 'job-demo', display=True)
                self.assertEqual(read.call_count, 2)
                verify.ready_state(None, node, 'job-demo')
                verify.ready_state(None, node, 'job-demo', train=True, display=True)
                self.assertEqual(read.call_count, 4, 'full or train gate reused a display result')
            verify.ready_state(None, node, 'job-demo', display=True)
            self.assertEqual(read.call_count, 5, 'display cache escaped its UI scope')

    def test_job_usage_snapshot_reuses_unchanged_inputs_beyond_the_cli_ttl(self):
        import contextlib
        import core_loader
        import importlib.util
        from unittest import mock
        loader = core_loader.SourceFileLoader('brain_idle_usage_test', str(CORE / 'bin/brain'))
        core = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(core)
        spend = core._owners['live_usage']
        from types import SimpleNamespace
        inst = SimpleNamespace(root=self.tmp)
        node = {'id': 'demo', 'dir': self.tmp, 'parent': None, 'local': {}}
        row = {'Job': 'job-demo', 'Started': '2026-10-21 00:00', 'Usage': ''}
        values = {}
        def scope(revision):
            return spend.live_usage_snapshot(values, revision) if hasattr(spend, 'live_usage_snapshot') else contextlib.nullcontext()
        with mock.patch.object(spend, 'job_usage', return_value={'sessions': 1}) as count, \
             mock.patch.object(spend.time, 'monotonic', return_value=0) as clock:
            with scope(1):
                spend.live_usage(inst, node, row)
                clock.return_value = 120
                spend.live_usage(inst, node, row)
            self.assertEqual(count.call_count, 1, 'unchanged job usage was recomputed after the CLI TTL')
            clock.return_value = 240
            with scope(2):
                spend.live_usage(inst, node, row)
            self.assertEqual(count.call_count, 2, 'changed source revision did not refresh job usage')

    def test_job_usage_batch_shares_session_metadata_reads(self):
        import contextlib
        import core_loader
        import importlib.util
        import io
        from unittest import mock
        loader = core_loader.SourceFileLoader('brain_idle_discovery_test', str(CORE / 'bin/brain'))
        core = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(core)
        spend = core._owners['live_usage']
        text = json.dumps({'type': 'session_meta', 'payload': {'id': 'demo-session', 'cwd': '/made-up/demo'}}).encode()
        with mock.patch.object(spend._m_history, 'transcript_open', side_effect=lambda *args: (io.BytesIO(text), None)) as read:
            scope = spend.live_usage_snapshot({}, 1) if hasattr(spend, 'live_usage_snapshot') else contextlib.nullcontext()
            with scope:
                first = spend.codex_meta(self.tmp / 'demo.jsonl', set(), set())
                self.assertEqual(spend.codex_meta(self.tmp / 'demo.jsonl', set(), set()), first)
                self.assertEqual(read.call_count, 1, 'each job re-read the same session metadata')
            spend.codex_meta(self.tmp / 'demo.jsonl', set(), set())
            self.assertEqual(read.call_count, 2, 'UI batch memo escaped into ordinary CLI reads')

    def test_permission_change_drops_spend_without_transcript_metadata_change(self):
        _, state = self.start_server(transcript=True)
        self.assertEqual(state()['spend']['read'], 1)
        path = self.tmp / 'config/subscriptions.json'
        config = json.loads(path.read_text())
        config['subscriptions'].append({'id': 'separate-demo', 'runtime': 'codex',
                                        'config_dir': str(self.tmp / 'runtime/sessions'), 'brains': ['separate-demo']})
        path.write_text(json.dumps(config))
        self.assertEqual(state()['spend']['read'], 0, 'cached totals retained newly forbidden transcripts')

    def test_external_lookback_reports_and_new_verify_patterns_invalidate_their_inputs(self):
        import core_loader
        import importlib.util
        import os
        from unittest import mock
        root = self.init('demo')
        home = self.repo(self.tmp / 'app')
        self.b(root, 'new', 'app', '--title', 'Demo app', '--home', str(home))
        loader = core_loader.SourceFileLoader('brain_idle_inputs_test', str(CORE / 'bin/brain'))
        core = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(core)
        ui = core._owners['UIInputs']
        with mock.patch.dict(os.environ, self.env, clear=True):
            inst = ui._m_instance.Instance(root)
            inputs = ui.UIInputs(inst)
            before = inputs.node_keys(inst, inputs.signature())['app']
            node = inst.node('app')
            lookback = ui._m_spend.spend_node_file(inst, node)
            lookback.parent.mkdir(parents=True, exist_ok=True)
            lookback.write_text(json.dumps({'total': {'processed': 25, 'models': {}}}))
            self.assertNotEqual(inputs.node_keys(inst, inputs.signature())['app'], before,
                                'external node ignored its newly published spend lookback')
            before = inputs.node_keys(inst, inputs.signature())['app']
            report = ui._m_worktrees.workbench(inst, node) / 'reports/new-demo/index.md'
            report.parent.mkdir(parents=True)
            report.write_text('# Made-up report\n')
            signature = inputs.signature()
            self.assertNotEqual(inputs.node_keys(inst, signature)['app'], before, 'new report was not a node input')
            report.write_text('# Made-up report, second revision\n')
            self.assertNotEqual(inputs.signature(), signature, 'new report index was not watched after discovery')
            before = inputs.topology
            self.b(root, 'set', 'app', 'verify_inputs=runtime-demo.txt')
            self.assertNotEqual(inputs.topology_key(ui._m_instance.Instance(root)), before,
                                'new verify-input patterns did not refresh ignored-file discovery')

    def test_a_read_only_refresh_parses_nodes_and_library_only_once(self):
        import core_loader
        import importlib.util
        from unittest import mock
        loader = core_loader.SourceFileLoader('brain_idle_records_test', str(CORE / 'bin/brain'))
        core = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(core)
        ui = core._owners['UIInputs']
        base = ui._m_instance.Instance
        display = getattr(ui, 'UIInstance', base)
        root = self.init('demo')
        with mock.patch.object(base, 'nodes', return_value=[]) as nodes, mock.patch.object(base, 'items', return_value=[]) as items:
            view = display(root)
            view.nodes(); view.nodes()
            view.items(); view.items()
            self.assertEqual(nodes.call_count, 1, 'every derived view re-parsed the nodes')
            self.assertEqual(items.call_count, 1, 'every derived view re-parsed the library')
            ordinary = base(root)
            ordinary.items(); ordinary.items()
            self.assertEqual(items.call_count, 3, 'read-only snapshot caching escaped into CLI instances')

    def test_port_liveness_refreshes_without_rebuilding_repository_state(self):
        import socket
        root, state = self.start_server(projects=True)
        before = state()
        local = json.loads((root / '.brain/local.json').read_text())
        port = local['nodes']['indigo']['worktrees']['job-demo']['port']
        def listening():
            value = state()
            node = next(n for n in value['nodes'] if n['id'] == 'indigo')
            self.assertEqual(value['_test_builds'], before['_test_builds'])
            return port in node['worktrees'][0]['listening']
        with socket.socket() as server:
            server.bind(('127.0.0.1', port)); server.listen(16)
            deadline = time.monotonic() + 5
            while not listening() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(listening(), 'opening a port remained stale without a file edit')
        deadline = time.monotonic() + 5
        while listening() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(listening(), 'closing a port remained stale without a file edit')

    def test_new_never_name_rule_rescreens_machine_spend_without_transcript_changes(self):
        root, state = self.start_server(transcript=True)
        self.assertIn('demo-model', json.dumps(state()['spend']['since_week']['models']))
        (root / 'never-name.txt').write_text('demo-model\n')
        self.assertNotIn('demo-model', json.dumps(state()['spend']['since_week']['models']),
                         'a newly forbidden model name remained in machine spend')

    def test_installed_core_change_invalidates_rehearsal_display_caches(self):
        import core_loader
        import importlib
        import importlib.util
        import os
        from unittest import mock
        root = self.init('demo')
        home = self.repo(self.tmp / 'app')
        self.b(root, 'new', 'app', '--title', 'Demo app', '--home', str(home))
        self.b(root, 'set', 'app', 'rehearse=required')
        self.b(root, 'wt', 'add', 'app', 'job-demo')
        loader = core_loader.SourceFileLoader('brain_idle_core_change_test', str(CORE / 'bin/brain'))
        core = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(core)
        ui = core._owners['UIInputs']
        rehearse = importlib.import_module(ui.__package__ + '.rehearse')
        with mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch.object(rehearse, 'installed_core_commit', return_value='a' * 40) as installed:
            inst = ui.UIInstance(root)
            inputs = ui.UIInputs(inst)
            # A .git marker makes this behave like an installed versioned core; the reader is mocked.
            (root / 'core/.git').mkdir(exist_ok=True)
            signature = inputs.signature()
            node_key = inputs.node_keys(inst, signature)['app']
            before = inputs.worktree_keys(inst, signature)
            installed.return_value = 'b' * 40
            changed = inputs.signature()
            after = inputs.worktree_keys(inst, changed)
            self.assertTrue(after != before, 'installed core change left readiness inputs unchanged')
            self.assertTrue(inputs.node_keys(inst, changed)['app'] != node_key,
                            'the node row retained the old rehearsal badge')
            values = {}
            node = inst.node('app')
            with mock.patch.object(ui._m_verify, '_ready_state', side_effect=[{'ready': True}, {'ready': False}]) as validate:
                with ui._m_verify.display_cache(values, before):
                    self.assertTrue(ui._m_verify.ready_state(inst, node, 'job-demo', display=True)['ready'])
                with ui._m_verify.display_cache(values, after):
                    self.assertFalse(ui._m_verify.ready_state(inst, node, 'job-demo', display=True)['ready'])
                self.assertEqual(validate.call_count, 2)

    def test_spend_view_rebuckets_on_clock_change_without_reparsing_files(self):
        import core_loader
        import importlib.util
        import datetime
        from types import SimpleNamespace
        from unittest import mock
        loader = core_loader.SourceFileLoader('brain_idle_spend_clock_test', str(CORE / 'bin/brain'))
        core = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(core)
        ui = core._owners['UIInputs']
        current = [datetime.datetime(2026, 10, 21, 23, 59, tzinfo=datetime.timezone.utc)]
        inst = SimpleNamespace(root=self.tmp, nodes=lambda: [], now_dt=lambda: current[0])
        with mock.patch.object(ui._m_spend, 'spend_days', side_effect=lambda *args: {'date': str(current[0].date())}) as days, \
             mock.patch.object(ui._m_spend, 'spend_file') as parse:
            spend = ui.UISpend(inst)
            spend.advance()
            self.assertEqual(spend.value['runtimes']['date'], '2026-10-21')
            spend.advance()
            self.assertEqual(days.call_count, 1, 'unchanged spend was rebuilt per poll')
            current[0] += datetime.timedelta(minutes=2)
            if hasattr(spend, 'refresh_view'):
                spend.refresh_view()
            spend.advance()
            self.assertEqual(spend.value['runtimes']['date'], '2026-10-22', 'spend buckets froze at midnight')
            parse.assert_not_called()
