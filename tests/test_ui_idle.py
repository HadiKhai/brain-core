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

    def start_server(self, transcript=False):
        config = self.tmp / 'config'
        config.mkdir(exist_ok=True)
        self.env['BRAIN_CONFIG'] = str(config / 'brains.json')
        root = self.init('demo')
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
        script = '''
import sys
sys.path.insert(0, sys.argv[1] + '/lib')
from brain import ui, cli
build = ui.ui_state
calls = 0
def counted(*args, **kwargs):
    global calls
    calls += 1
    result = build(*args, **kwargs)
    result['_test_builds'] = calls
    return result
ui.ui_state = counted
sys.argv = ['brain', 'ui', '--port', '0', '--every', '0.05']
cli.main()
'''
        process = subprocess.Popen([sys.executable, '-u', '-c', script, str(CORE)], cwd=root,
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
            if latest['_test_builds'] > before['_test_builds']:
                break
            time.sleep(0.05)
        self.assertGreater(latest['spend']['since_week']['processed'], before['spend']['since_week']['processed'])
        self.assertFalse(list((root / '.brain/spend').rglob('*.json')))
