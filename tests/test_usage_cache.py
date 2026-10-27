"""Incremental usage caches count appended bytes and invalidate changed permissions."""
import datetime
import json
import os
from pathlib import Path
from unittest import mock

import core_loader
from scenario import CORE, Scenario


class UsageCache(Scenario):
    def discovery_fixture(self):
        path = self.cfg / 'sessions' / 'rollout-space λ\nfile.jsonl'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'type': 'session_meta', 'payload': {
            'id': '11111111-cached', 'cwd': str(self.wt)}}) + '\n')
        model = self.m.ReadModel(self.inst, 'fixture')
        def read(forbidden=None):
            with model.usage_scope():
                return self.m.codex_index(self.inst, self.cfg, {self.cfg}, forbidden or set())
        return path, model, read

    def test_ui_discovery_cache_reuses_headers_and_invalidates_restored_mtime(self):
        path, model, read = self.discovery_fixture()
        # Instance creation renders status, whose facts sections fill the derived
        # database; start without it so discovery alone is shown not to create it.
        (self.root / '.brain/usage.sqlite').unlink()
        first = read()
        self.assertEqual(len(first), 1)
        module = self.m._owners['codex_index']
        with mock.patch.object(module, 'codex_discovery_source', side_effect=AssertionError('unchanged header')):
            self.assertEqual(read(), first)
        cache = next(model.folder.glob('discovery-*.json'))
        self.assertEqual(cache.stat().st_mode & 0o777, 0o600)
        for private in ('11111111-cached', str(path), str(self.wt)):
            self.assertNotIn(private, cache.read_text())
        self.assertFalse((self.root / '.brain/usage.sqlite').exists())
        stamp = path.stat()
        path.write_bytes(path.read_bytes().replace(b'11111111-cached', b'22222222-cached'))
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        changed = read()
        self.assertNotEqual(changed[0][1]['id'], first[0][1]['id'])
        self.assertEqual(read({self.cfg}), [])

    def test_ui_discovery_malformed_and_old_version_cache_recomputes_current_source(self):
        path, model, read = self.discovery_fixture()
        expected = read()
        cache = next(model.folder.glob('discovery-*.json'))
        original = json.loads(cache.read_text())
        module = self.m._owners['codex_index']
        for bad in ({**original, 'version': self.m.SPEND_VERSION - 1},
                    {**original, 'job': '\ud800'}, {**original, 'places': ['raw source text']}):
            cache.write_text(json.dumps(bad))
            with mock.patch.object(module, 'codex_discovery_source', wraps=module.codex_discovery_source) as source:
                self.assertEqual(read(), expected)
                self.assertEqual(source.call_count, 1)

    def test_ui_discovery_changed_descriptor_after_cache_lookup_is_incomplete(self):
        path, model, read = self.discovery_fixture()
        read()
        original = model.read
        def changed(name):
            value = original(name)
            path.write_bytes(path.read_bytes() + b'\n')
            return value
        with mock.patch.object(model, 'read', side_effect=changed):
            with self.assertRaises(self.m.DiscoveryIncomplete):
                read()

    def test_disabled_ui_cache_scope_reads_current_headers_without_publication(self):
        path, model, read = self.discovery_fixture()
        module = self.m._owners['cache_writes']
        with module.cache_writes(False), module.cache_writes(True):
            self.assertEqual(len(read()), 1)
        self.assertFalse(model.folder.exists())
        self.assertEqual(len(read()), 1)
        self.assertTrue(model.folder.is_dir())

    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, self.env, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.root = self.init('oak')
        self.m = core_loader.SourceFileLoader('cache_fixture', str(CORE / 'bin/brain')).load_module()
        self.inst = self.m.Instance(self.root)
        self.n = self.inst.node('root')
        self.cfg = self.tmp / 'runtime'
        self.wt = self.tmp / 'worktree'
        self.wt.mkdir()
        self.path = self.cfg / 'projects' / self.m.claude_folder(self.wt) / '11111111-session.jsonl'
        self.path.parent.mkdir(parents=True)
        self.row = {**dict.fromkeys(self.m.JOB_COLS, ''), 'Job': 'job/palette',
                    'Started': '2026-01-15T10:00:30Z'}
        self.n['local']['worktrees'] = {'job/palette': {'path': str(self.wt)}}
        self.allowed, self.forbidden = {self.cfg}, set()
        source = mock.patch.object(self.m, 'spend_sources', side_effect=lambda *a:
                                   ([('claude', self.cfg)], self.allowed, self.forbidden))
        source.start()
        self.addCleanup(source.stop)
        self.append('before', '2026-01-15T10:00:00.000Z', 900)
        self.append('during', '2026-01-15T10:01:00.000Z', 10)

    def append(self, mid, at, tokens):
        row = {'type': 'assistant', 'sessionId': '11111111-session', 'cwd': str(self.wt),
               'timestamp': at, 'message': {'id': mid, 'model': 'sample-model',
               'role': 'assistant', 'content': [{'type': 'text', 'text': 'fixture'}],
               'usage': {'input_tokens': tokens, 'output_tokens': 1}}}
        with self.path.open('a') as stream:
            stream.write(json.dumps(row, separators=(',', ':')) + '\n')

    def test_job_window_persists_offsets_across_process_cache_resets(self):
        first = self.m.job_usage(self.inst, self.n, self.row)
        self.assertEqual(first['processed'], 11)
        self.m._SPEND_MEM.clear()
        offsets = []
        original = self.m.spend_read
        def observed(agg, fh, budget=None):
            offsets.append(agg['state']['offset'])
            return original(agg, fh, budget)
        old_size = self.path.stat().st_size
        self.append('next', '2026-01-15T10:02:00.000Z', 20)
        with mock.patch.object(self.m, 'spend_read', side_effect=observed):
            second = self.m.job_usage(self.inst, self.n, self.row)
        self.assertEqual(second['processed'], 32)
        self.assertTrue(offsets)
        self.assertTrue(all(offset == old_size for offset in offsets), offsets)
        import sqlite3
        with sqlite3.connect(str(self.root / '.brain/usage.sqlite')) as cache:
            records = [json.loads(row[0]) for row in cache.execute('SELECT value FROM aggregates')]
        windows = [record for record in records if record.get('state', {}).get('lo') is not None]
        self.assertTrue(windows, 'windowed parser progress must be kept in SQLite too')
        self.assertTrue(all(record['state']['offset'] == self.path.stat().st_size for record in windows))
        for cache in (self.root / '.brain/spend/files').glob('*.json'):
            self.assertNotIn(str(self.wt), cache.read_text())
            self.assertNotIn('fixture', cache.read_text())

    def test_same_size_rewrite_and_truncation_rebuild_aggregates(self):
        self.assertEqual(self.m.job_usage(self.inst, self.n, self.row)['processed'], 11)
        text = self.path.read_text().replace('"input_tokens":10', '"input_tokens":40')
        self.path.write_text(text)
        self.assertEqual(self.m.job_usage(self.inst, self.n, self.row)['processed'], 41)
        self.path.write_text('')
        self.append('fresh', '2026-01-15T10:03:00.000Z', 30)
        self.assertEqual(self.m.job_usage(self.inst, self.n, self.row)['processed'], 31)

    def test_snapshot_refresh_observes_append_and_permission_change(self):
        values = {}
        with self.m.live_usage_snapshot(values, 'same-head'):
            first = self.m.live_usage(self.inst, self.n, self.row)
        self.append('next', '2026-01-15T10:02:00.000Z', 20)
        with self.m.live_usage_snapshot(values, 'same-head'):
            self.assertEqual(self.m.live_usage(self.inst, self.n, self.row)['processed'], 32)
        self.forbidden.add(self.cfg)
        with self.m.live_usage_snapshot(values, 'same-head'):
            denied = self.m.live_usage(self.inst, self.n, self.row)
        self.assertEqual(denied['processed'], 0)
        self.assertEqual(first['processed'], 11)

    def test_unchanged_snapshot_reuses_totals_without_parsing_transcripts(self):
        values = {}
        with self.m.live_usage_snapshot(values, 'head'):
            first = self.m.live_usage(self.inst, self.n, self.row)
        with self.m.live_usage_snapshot(values, 'head'), \
                mock.patch.object(self.m, 'spend_read', side_effect=AssertionError('read unchanged transcript')):
            second = self.m.live_usage(self.inst, self.n, self.row)
        self.assertEqual(second, first)

    def test_explicit_warm_job_total_uses_disk_cache_without_discovery(self):
        first = self.m.job_usage(self.inst, self.n, self.row)
        self.m._SPEND_MEM.clear()
        with mock.patch.object(self.m, 'job_sessions', side_effect=AssertionError('rediscovered unchanged job')):
            self.assertEqual(self.m.job_usage(self.inst, self.n, self.row), first)
        for cache in (self.root / '.brain/spend/jobs').glob('*.json'):
            self.assertNotIn(str(self.cfg), cache.read_text())
            self.assertNotIn(str(self.wt), cache.read_text())

    def test_read_only_snapshot_does_not_publish_or_poison_next_writer(self):
        with self.m.spend_read_only(), self.m.live_usage_snapshot({}, 'head'):
            self.m.live_usage(self.inst, self.n, self.row)
        self.assertFalse((self.root / '.brain/spend').exists())
        self.m.job_usage(self.inst, self.n, self.row)
        self.assertTrue(list((self.root / '.brain/spend/files').glob('*.json')))


    def test_finished_usage_is_reused_but_subscription_revocation_invalidates_it(self):
        with mock.patch.object(self.m, 'job_signal', return_value={'state': 'done'}):
            first = self.m.job_usage(self.inst, self.n, self.row)
            self.assertEqual(first['processed'], 11)
            self.append('late', '2026-01-15T10:02:00.000Z', 20)
            self.n['local']['workers'] = {'unrelated-job': 'fixture-worker'}
            with mock.patch.object(self.m, 'job_sessions', side_effect=AssertionError('recounted terminal episode')):
                self.assertEqual(self.m.job_usage(self.inst, self.n, self.row), first)
            self.forbidden.add(self.cfg)
            self.assertEqual(self.m.job_usage(self.inst, self.n, self.row)['processed'], 0)
