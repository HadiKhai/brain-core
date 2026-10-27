"""Scenario tests for core/bin/brain. Run with `brain selftest` (or python3 -m unittest in tests/).

Every test builds instances in a throwaway folder that is also $HOME, so nothing real is touched.
"""
import json
import re
import datetime
import importlib.util
import core_loader
import threading
from unittest import mock
import argparse
import contextlib
import io
import os
import shutil
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

CORE = Path(os.environ.get("BRAIN_CORE") or Path(__file__).resolve().parent.parent)


import test_hosts as _test_hosts
import test_verify as _test_verify
from scenario import Scenario, AutonomyProject, shlex_quote


class TermsFixture(Scenario):
    def setUp(self):
        super().setUp()
        self.m = core_loader.SourceFileLoader('term_cache_fixture', str(CORE / 'bin/brain')).load_module()
        self.guard = self.m._owners['scan_terms']
        notes = self.tmp / 'notes'
        notes.mkdir()
        (notes / 'instance.md').write_text('---\nprincipals: fixture-owner\n---\n')
        self.home = self.repo(self.tmp / 'home', {'one.txt': 'needle\n', 'two.txt': 'clean\n'})
        self.node = dict(dir=notes, parent=None, external=True, home_path=self.home,
                         local={}, id='fixture')
        self.terms = [('Needle', 'term 1'), ('CAFÉ', 'term 2'), ('ss', 'term 3')]

    def scan(self):
        return self.guard.scan_terms(self.node, terms=self.terms)


class ScanTermsCache(TermsFixture):
    def test_cached_scan_matches_uncached_worktree_index_and_unicode(self):
        (self.home / 'one.txt').write_bytes(b'clean\x00' + 'Café Straße'.encode())
        class Uncached(dict):
            def __setitem__(self, key, value):
                pass
        token = self.guard._TERM_SCAN_CACHE.set(Uncached())
        try:
            expected = self.scan()
        finally:
            self.guard._TERM_SCAN_CACHE.reset(token)
        with self.guard.term_scan_cache():
            self.assertEqual(self.scan(), expected)
            with mock.patch.object(self.guard, 'stream_mentions', wraps=self.guard.stream_mentions) as stream:
                self.assertEqual(self.scan(), expected)
                self.assertEqual(stream.call_count, 0)
        # A new command must read sources again.
        with mock.patch.object(self.guard, 'stream_mentions', wraps=self.guard.stream_mentions) as stream:
            self.assertEqual(self.scan(), expected)
            self.assertGreater(stream.call_count, 0)
        self.assertTrue(any('index stage 0:' in where for _, where in expected[0]))
        self.assertTrue(any(label == 'term 2' for label, _ in expected[0]))

    def test_cached_files_invalidate_rewrites_replacements_terms_and_symlinks(self):
        file = self.home / 'one.txt'
        guard = self.guard
        def read(needles=('needle',), principals=(), lessons=False):
            return guard.file_mentions(file, needles, principals=principals, lessons=lessons)
        with guard.term_scan_cache():
            self.assertEqual(read(), {'needle'})
            old = file.stat()
            file.write_text('clean!\n')  # same size, restored mtime; ctime still changes
            os.utime(file, ns=(old.st_atime_ns, old.st_mtime_ns))
            self.assertEqual(read(), set())
            replacement = self.home / 'replacement'
            replacement.write_text('needle\n')
            os.utime(replacement, ns=(old.st_atime_ns, old.st_mtime_ns))
            replacement.replace(file)
            self.assertEqual(read(), {'needle'})
            self.assertEqual(read(('clean',)), set())
            file.unlink()
            file.symlink_to('two.txt')
            self.assertEqual(read(), set())
            (self.home / 'two.txt').write_text('needle')
            self.assertEqual(read(), {'needle'})
            file.unlink()
            errors = []
            self.assertEqual(guard.file_mentions(file, ['needle'], errors=errors), set())
            self.assertEqual(len(errors), 1)

    def test_governance_and_failed_reads_do_not_share_cached_results(self):
        file = self.home / 'one.txt'
        file.write_text('---\nscope: needle\n---\nclean\n')
        with self.guard.term_scan_cache():
            self.assertEqual(self.guard.file_mentions(file, ['needle']), {'needle'})
            self.assertEqual(self.guard.file_mentions(file, ['needle'], principals=['needle']), set())
            self.assertEqual(self.guard.file_mentions(file, ['needle'], principals=['other']), {'needle'})
            errors = []
            with mock.patch('builtins.open', side_effect=PermissionError(13, 'Permission denied')):
                self.assertEqual(self.guard.file_mentions(file, ['needle'], errors=errors), set())
            self.assertEqual(len(errors), 1)

    def test_cached_blob_from_other_repo_does_not_hide_missing_object(self):
        other = self.repo(self.tmp / 'other', {'one.txt': 'needle\n', 'two.txt': 'clean\n'})
        oid = self.git('rev-parse', 'HEAD:one.txt', cwd=other).strip()
        with self.guard.term_scan_cache():
            self.scan()
            (other / '.git/objects' / oid[:2] / oid[2:]).unlink()
            hits, errors = self.guard.scan_terms(self.node, [other], terms=self.terms)
            self.assertTrue(any('Git index blob unavailable' in error for error in errors), errors)

    def test_replacement_refs_never_reuse_a_different_blob_result(self):
        oid = self.git('rev-parse', 'HEAD:one.txt', cwd=self.home).strip()
        clean = self.git('rev-parse', 'HEAD:two.txt', cwd=self.home).strip()
        (self.home / 'one.txt').write_text('clean\n')
        with self.guard.term_scan_cache():
            self.assertTrue(any('index stage' in where for _, where in self.scan()[0]))
            self.git('replace', oid, clean, cwd=self.home)
            self.assertEqual(self.scan(), ([], []))
            self.git('replace', '-d', oid, cwd=self.home)
            self.assertTrue(any('index stage' in where for _, where in self.scan()[0]))

    def test_files_changed_during_read_are_not_cached(self):
        file = self.home / 'one.txt'
        file.write_text('clean\n')
        original = self.guard.stream_mentions
        def changing(*args, **kwargs):
            result = original(*args, **kwargs)
            file.write_text('clean\nneedle\n')
            return result
        errors = []
        with self.guard.term_scan_cache():
            with mock.patch.object(self.guard, 'stream_mentions', side_effect=changing):
                self.assertEqual(self.guard.file_mentions(file, ['needle'], errors=errors), set())
            self.assertEqual(errors, [(file, 'file changed during term scan')])
            self.assertEqual(self.guard.file_mentions(file, ['needle']), {'needle'})

    def test_cached_matches_do_not_hide_corrupt_blob_bodies(self):
        other = self.repo(self.tmp / 'other', {'one.txt': 'needle\n', 'two.txt': 'clean\n'})
        oid = self.git('rev-parse', 'HEAD:one.txt', cwd=other).strip()
        obj = other / '.git/objects' / oid[:2] / oid[2:]
        with self.guard.term_scan_cache():
            self.scan()
            obj.chmod(0o600)
            obj.write_bytes(obj.read_bytes()[:10])
            hits, errors = self.guard.scan_terms(self.node, [other], terms=self.terms)
            self.assertTrue(errors)
            self.assertTrue(any('index' in error or 'cat-file' in error for error in errors), errors)

    def test_short_small_file_reads_fall_back_to_complete_streaming(self):
        file = self.home / 'one.txt'
        with open(file, 'rb', buffering=0) as raw:
            class ShortRead:
                first = True
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    pass
                def fileno(self):
                    return raw.fileno()
                def seek(self, offset):
                    return raw.seek(offset)
                def read(self, size):
                    if self.first:
                        self.first = False
                        return raw.read(1)
                    return raw.read(size)
            with self.guard.term_scan_cache(), mock.patch('builtins.open', return_value=ShortRead()):
                self.assertEqual(self.guard.file_mentions(file, ['needle']), {'needle'})

    def test_index_request_batches_drain_large_blobs_and_all_following_entries(self):
        (self.home / 'a-large.bin').write_bytes(b'x' * ((1 << 20) + 100) + b'needle')
        for index in range(40):
            (self.home / ('entry-%02d.txt' % index)).write_text('needle %d\n' % index)
        self.git('add', '.', cwd=self.home)
        hits, errors = self.scan()
        self.assertEqual(errors, [])
        indexed = [where for label, where in hits if label == 'term 1' and where.startswith('index stage')]
        self.assertEqual(len(indexed), 42)  # original one.txt, large blob and forty small blobs

    def test_repeated_scan_avoids_duplicate_reads_with_timing_budget(self):
        stream = self.guard.stream_mentions
        def delayed(*args, **kwargs):
            time.sleep(.02)  # model read latency; no machine-speed assertion
            return stream(*args, **kwargs)
        def scan_file():
            return self.guard.file_mentions(self.home / 'one.txt', ('needle',))
        # Time the cached file read, excluding Git process startup contention
        # when the full suite runs other fixture repositories concurrently.
        with mock.patch.object(self.guard, 'stream_mentions', side_effect=delayed):
            start = time.monotonic()
            scan_file(); scan_file()
            uncached = time.monotonic() - start
            start = time.monotonic()
            with self.guard.term_scan_cache():
                scan_file(); scan_file()
            cached = time.monotonic() - start
        self.assertLess(cached, uncached * .85)

    def test_nonregular_descriptors_are_never_read_or_blocking(self):
        fifo = self.tmp / 'fifo'
        os.mkfifo(fifo)
        for path in (fifo, self.tmp):
            errors = []
            with mock.patch.object(self.guard, 'stream_mentions', side_effect=AssertionError('nonregular read')):
                self.assertEqual(self.guard.file_mentions(path, ['needle'], errors=errors), set())
            self.assertEqual(errors, [])

    def test_repository_file_opens_share_only_directory_resolution(self):
        guard = self.guard
        original = os.open
        opens = []
        def capture(path, flags, *args, **kwargs):
            opens.append((path, flags, kwargs.get('dir_fd')))
            return original(path, flags, *args, **kwargs)
        with mock.patch.object(guard.os, 'open', side_effect=capture):
            observed, errors = guard.term_file_observations(
                self.node, self.home, ['one.txt', 'two.txt'], [], ('needle',), (), {})
        self.assertEqual(errors, [])
        self.assertEqual(observed, {'one.txt': (False, {'needle'}), 'two.txt': (False, set())})
        relative = [(path, flags, fd) for path, flags, fd in opens if fd is not None]
        self.assertEqual([path for path, _, _ in relative], ['one.txt', 'two.txt'])
        self.assertTrue(all(flags & os.O_NONBLOCK for _, flags, _ in relative))
        self.assertEqual(len({fd for _, _, fd in relative}), 1)
        with self.assertRaises(OSError):
            os.fstat(relative[0][2])
        self.assertIsNone(guard._TERM_READ_DIRECTORY.get())

    def test_replaced_repository_directory_reports_incomplete_and_resets_authority(self):
        guard = self.guard
        original = guard.file_mentions
        saved = self.tmp / 'displaced-home'
        def replace(path, *args, **kwargs):
            if not saved.exists():
                self.home.rename(saved)
                self.home.mkdir()
                (self.home / 'one.txt').write_text('clean\n')
            return original(path, *args, **kwargs)
        with mock.patch.object(guard, 'file_mentions', side_effect=replace):
            observed, errors = guard.term_file_observations(
                self.node, self.home, ['one.txt', 'two.txt'], [], ('needle',), (), {})
        self.assertEqual(observed['one.txt'], (False, {'needle'}))
        self.assertTrue(any('directory changed' in why for _, why in errors))
        self.assertIsNone(guard._TERM_READ_DIRECTORY.get())
        self.assertEqual(guard.file_mentions(self.home / 'one.txt', ('needle',)), set())

    def test_unreadable_repository_directory_keeps_inventory_and_errors(self):
        guard = self.guard
        with mock.patch.object(guard.os, 'open', side_effect=PermissionError(13, 'Permission denied')):
            observed, errors = guard.term_file_observations(
                self.node, self.home, ['one.txt', 'two.txt'], [], ('needle',), (), {})
        self.assertEqual(set(observed), {'one.txt', 'two.txt'})
        self.assertTrue(any('Permission denied' in why for _, why in errors))
        self.assertIsNone(guard._TERM_READ_DIRECTORY.get())


class PersistentTermsCache(TermsFixture):
    @property
    def cache_file(self):
        return self.node['dir'] / '.brain/term-scan-cache.json'

    def prime(self):
        with self.guard.term_scan_cache(persist=True):
            return self.scan()

    def clean_fixture(self):
        (self.home / 'one.txt').write_text('clean\n')
        self.git('add', '.', cwd=self.home)
        self.git('commit', '-m', 'clean fixture', cwd=self.home)

    def test_private_persistent_verdicts_contain_no_terms_paths_or_labels(self):
        expected = self.prime()
        raw = self.cache_file.read_text()
        self.assertEqual(self.cache_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.cache_file.parent.stat().st_mode & 0o777, 0o700)
        for text in [term for term, _ in self.terms] + [label for _, label in self.terms] + [str(self.home), 'one.txt']:
            self.assertNotIn(text.casefold(), raw.casefold())
        data = json.loads(raw)
        for node in data['nodes'].values():
            for context in node['contexts'].values():
                self.assertTrue(all(isinstance(value, bool) for value in context['blobs'].values()))
                self.assertTrue(all(re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', oid) for oid in context['blobs']))
        self.assertEqual(self.scan(), expected)  # failures still recover exact redacted labels

    def test_new_process_reuses_clean_verdicts_and_readers_do_not_publish(self):
        self.clean_fixture()
        self.assertEqual(self.prime(), ([], []))
        before = self.cache_file.read_bytes()
        program = '''from pathlib import Path
from unittest import mock
import sys
from brain import guard
node = dict(dir=Path(sys.argv[1]), parent=None, external=True,
            home_path=Path(sys.argv[2]), local={}, id='fixture')
with mock.patch.object(guard, 'stream_mentions', side_effect=AssertionError('unexpected rescan')):
    assert guard.scan_terms(node, terms=[('Needle', 'term 1'), ('CAFÉ', 'term 2'), ('ss', 'term 3')]) == ([], [])
'''
        env = {**self.env, 'PYTHONPATH': str(CORE / 'lib')}
        for key in list(env):
            if key == 'GIT_CONFIG_COUNT' or key.startswith(('GIT_CONFIG_KEY_', 'GIT_CONFIG_VALUE_')):
                env.pop(key)
        result = subprocess.run([sys.executable, '-B', '-c', program, str(self.node['dir']), str(self.home)],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.cache_file.read_bytes(), before)
        # A cold read-only command must not even create the cache directory.
        self.cache_file.unlink()
        self.cache_file.parent.rmdir()
        self.assertEqual(self.scan(), ([], []))
        self.assertFalse(self.cache_file.parent.exists())

    def test_words_and_scanner_version_invalidate_and_clear_previous_verdicts(self):
        self.clean_fixture()
        self.prime()
        old = json.loads(self.cache_file.read_text())
        self.terms = [('clean', 'new term')]
        self.assertTrue(self.prime()[0])
        new = json.loads(self.cache_file.read_text())
        self.assertNotEqual(next(iter(old['nodes'].values()))['words'], next(iter(new['nodes'].values()))['words'])
        cache_module = self.guard._m_term_cache
        with mock.patch.object(cache_module, 'SCANNER_VERSION', cache_module.SCANNER_VERSION + 1):
            with mock.patch.object(self.guard, 'stream_mentions', wraps=self.guard.stream_mentions) as read:
                self.prime()
                self.assertGreater(read.call_count, 0)
        self.assertNotEqual(new['version'], json.loads(self.cache_file.read_text())['version'])
        self.terms = []
        self.assertEqual(self.prime(), ([], []))
        self.assertEqual(json.loads(self.cache_file.read_text())['nodes'], {})

    def test_warm_file_rewrite_restored_mtime_and_failed_open_are_not_hidden(self):
        self.clean_fixture()
        self.prime()
        file = self.home / 'one.txt'
        old = file.stat()
        file.write_text('needle')  # same six bytes
        os.utime(file, ns=(old.st_atime_ns, old.st_mtime_ns))
        self.assertTrue(self.scan()[0])
        file.write_text('clean\n')
        self.prime()
        original = open
        def fail(path, *args, **kwargs):
            if Path(path) == file:
                raise PermissionError(13, 'Permission denied')
            return original(path, *args, **kwargs)
        with mock.patch('builtins.open', side_effect=fail):
            self.assertTrue(any('Permission denied' in error for error in self.scan()[1]))

    def test_cached_clean_object_corruption_or_missing_object_is_incomplete(self):
        self.clean_fixture()
        self.prime()
        oid = self.git('rev-parse', 'HEAD:two.txt', cwd=self.home).strip()
        obj = self.home / '.git/objects' / oid[:2] / oid[2:]
        original = obj.read_bytes()
        obj.chmod(0o600)
        obj.write_bytes(original[:10])
        self.assertTrue(self.scan()[1])
        obj.write_bytes(original)
        self.prime()
        obj.unlink()
        self.assertTrue(self.scan()[1])

    def test_replacement_refs_and_other_repositories_require_their_own_body_proof(self):
        self.clean_fixture()
        self.prime()
        dirty = self.git('rev-parse', 'HEAD~1:one.txt', cwd=self.home).strip()
        clean = self.git('rev-parse', 'HEAD:one.txt', cwd=self.home).strip()
        self.git('replace', clean, dirty, cwd=self.home)
        self.assertTrue(any(where.startswith('index stage') for _, where in self.scan()[0]))
        other = self.repo(self.tmp / 'other', {'two.txt': 'clean\n'})
        oid = self.git('rev-parse', 'HEAD:two.txt', cwd=other).strip()
        (other / '.git/objects' / oid[:2] / oid[2:]).unlink()
        self.assertTrue(self.guard.scan_terms(self.node, [other], terms=self.terms)[1])

    def test_invalid_private_cache_and_link_targets_fall_back_without_publication(self):
        self.clean_fixture()
        self.prime()
        for invalid in ('not json', '{"version": [], "nodes": null}'):
            self.cache_file.write_text(invalid)
            self.assertEqual(self.prime(), ([], []))
        external = self.tmp / 'external-cache'
        external.write_text('preserve me')
        self.cache_file.unlink()
        self.cache_file.symlink_to(external)
        self.assertEqual(self.prime(), ([], []))
        self.assertEqual(external.read_text(), 'preserve me')
        self.cache_file.unlink()
        os.link(external, self.cache_file)
        self.assertEqual(self.prime(), ([], []))
        self.assertEqual(external.read_text(), 'preserve me')

    def test_cache_publication_loses_race_without_overwriting_other_writer(self):
        with self.guard.term_scan_cache(persist=True):
            self.scan()
            self.cache_file.write_text('a newer generation')
        self.assertEqual(self.cache_file.read_text(), 'a newer generation')

    def test_unrelated_new_object_keeps_existing_blob_verdicts(self):
        self.clean_fixture()
        self.prime()
        self.git('hash-object', '-w', str(self.node['dir'] / 'instance.md'), cwd=self.home)
        with mock.patch.object(self.guard, 'stream_mentions', side_effect=AssertionError('unexpected rescan')):
            self.assertEqual(self.scan(), ([], []))

    def test_tagged_inventory_preserves_untracked_and_nasty_path_names(self):
        names = ['space here.txt', 'é\none.txt', '? odd.txt', '100644 ' + 'a' * 40 + ' 0\tfake']
        for name in names:
            (self.home / name).write_text('needle\n')
        self.git('add', '--', names[0], names[1], cwd=self.home)
        expected, error = self.guard.home_files(self.home, False)
        actual, entries, combined_error = self.guard.home_files(self.home, False, with_stage=True)
        self.assertEqual(error, combined_error)
        self.assertEqual(sorted(actual), sorted(expected))
        self.assertTrue(set(names[:2]).issubset({entry[3] for entry in entries}))
        self.assertTrue(set(names[2:]).isdisjoint({entry[3] for entry in entries}))
        self.assertEqual(self.scan()[1], [])

    def test_parallel_worktree_observations_match_the_uncached_scan(self):
        other = self.tmp / 'second tree'
        self.git('worktree', 'add', '-b', 'parallel-fixture', str(other), cwd=self.home)
        (other / 'untracked\né.txt').write_text('Café needle\n')
        self.node['local']['worktrees'] = {'second': {'path': str(other)}}
        class Uncached(dict):
            def __setitem__(self, key, value):
                pass
        token = self.guard._TERM_SCAN_CACHE.set(Uncached())
        try:
            expected = self.scan()
        finally:
            self.guard._TERM_SCAN_CACHE.reset(token)
        self.assertEqual(self.prime(), expected)
        self.assertEqual(self.scan(), expected)
        self.assertTrue(any('index stage' in where for _, where in expected[0]))
        self.assertTrue(any('untracked\n' in where for _, where in expected[0]))

    def test_object_mutation_after_cached_reuse_makes_this_scan_incomplete(self):
        self.clean_fixture()
        self.prime()
        oid = self.git('rev-parse', 'HEAD:one.txt', cwd=self.home).strip()
        obj = self.home / '.git/objects' / oid[:2] / oid[2:]
        module = self.guard._m_term_cache
        original = module.Policy.blob_clean
        def change(policy, blob, storage):
            result = original(policy, blob, storage)
            if blob == oid and result:
                obj.chmod(0o600)
                obj.write_bytes(obj.read_bytes()[:10])
            return result
        with mock.patch.object(module.Policy, 'blob_clean', change):
            self.assertTrue(any('storage changed' in error for error in self.scan()[1]))
        self.assertTrue(self.scan()[1])

    def test_large_clean_file_and_index_blob_reuse_bounded_stream_verdicts(self):
        self.clean_fixture()
        (self.home / 'large.bin').write_bytes(b'x' * ((1 << 20) + 10))
        self.git('add', '.', cwd=self.home)
        self.assertEqual(self.prime(), ([], []))
        with mock.patch.object(self.guard, 'stream_mentions', side_effect=AssertionError('unexpected rescan')):
            self.assertEqual(self.scan(), ([], []))

    def test_changed_inventory_binding_and_corrupt_packed_storage_are_incomplete(self):
        self.clean_fixture()
        original = self.guard.home_files
        def changing(*args, **kwargs):
            result = original(*args, **kwargs)
            self.git('update-index', '--force-remove', 'two.txt', cwd=self.home)
            return result
        with mock.patch.object(self.guard, 'home_files', side_effect=changing):
            self.assertTrue(any('binding changed' in error for error in self.scan()[1]))
        self.git('add', '.', cwd=self.home)
        self.git('gc', '--prune=now', cwd=self.home)
        self.assertEqual(self.prime(), ([], []))
        pack = next((self.home / '.git/objects/pack').glob('*.pack'))
        pack.chmod(0o600)
        pack.write_bytes(pack.read_bytes()[:10])
        self.assertTrue(self.scan()[1])

    def test_nonprivate_cache_and_replaced_parent_do_not_publish(self):
        self.clean_fixture()
        self.prime()
        self.cache_file.chmod(0o644)
        with mock.patch.object(self.guard, 'stream_mentions', wraps=self.guard.stream_mentions) as read:
            self.assertEqual(self.prime(), ([], []))
            self.assertGreater(read.call_count, 0)
        self.assertEqual(self.cache_file.stat().st_mode & 0o777, 0o644)
        self.cache_file.chmod(0o600)
        outside = self.tmp / 'different-parent'
        outside.mkdir()
        (self.home / 'one.txt').write_text('fresh\n')
        with self.guard.term_scan_cache(persist=True):
            self.scan()
            original = self.cache_file.parent
            original.rename(original.with_name('saved-cache-parent'))
            original.symlink_to(outside, target_is_directory=True)
        self.assertEqual(list(outside.iterdir()), [])

    def test_reftable_replacement_verdict_matches_uncached_and_cold_processes(self):
        repo = self.tmp / 'reftable repo'
        result = subprocess.run(['git', 'init', '-q', '--ref-format=reftable', str(repo)], env=self.env,
                                capture_output=True, text=True)
        if result.returncode:
            self.skipTest('installed Git does not support reftable fixtures: ' + result.stderr)
        self.home = repo
        self.node['home_path'] = repo
        (repo / 'one.txt').write_text('clean\n')
        self.git('add', '.', cwd=repo)
        self.git('commit', '-m', 'fixture', cwd=repo)
        replacement = self.tmp / 'replacement'
        replacement.write_text('needle\n')
        bad = self.git('hash-object', '-w', str(replacement), cwd=repo).strip()
        good = self.git('rev-parse', 'HEAD:one.txt', cwd=repo).strip()
        self.assertEqual(self.prime(), ([], []))
        self.git('replace', good, bad, cwd=repo)
        program = '''from pathlib import Path
import json, sys
from brain import guard
node = dict(dir=Path(sys.argv[1]), parent=None, external=True,
            home_path=Path(sys.argv[2]), local={}, id='fixture')
class Uncached(dict):
    def __setitem__(self, key, value): pass
if sys.argv[3] == 'uncached': guard._TERM_SCAN_CACHE.set(Uncached())
print(json.dumps(guard.scan_terms(node, terms=[('Needle', 'term 1'), ('CAFÉ', 'term 2'), ('ss', 'term 3')])))
'''
        def run(mode):
            env = {**self.env, 'PYTHONPATH': str(CORE / 'lib')}
            for key in list(env):
                if key == 'GIT_CONFIG_COUNT' or key.startswith(('GIT_CONFIG_KEY_', 'GIT_CONFIG_VALUE_')):
                    env.pop(key)
            result = subprocess.run([sys.executable, '-B', '-c', program, str(self.node['dir']), str(repo), mode],
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        expected = run('uncached')
        self.assertTrue(any(where.startswith('index stage') for _, where in expected[0]))
        self.assertEqual(run('warm'), expected)
        self.cache_file.unlink()
        self.assertEqual(run('cold'), expected)

    def test_malformed_parseable_cache_never_republishes_plaintext_or_invalid_values(self):
        self.clean_fixture()
        self.prime()
        original = self.cache_file.read_text()
        def context(data):
            return next(iter(next(iter(data['nodes'].values()))['contexts'].values()))
        def root_field(data):
            data['unexpected_plaintext'] = 'needle /absolute/private/path'
        def bad_store(data):
            context(data)['stores']['/absolute/secret/object-store'] = {'label': 'needle'}
        def bad_verdict(data):
            key = next(iter(context(data)['blobs']))
            context(data)['blobs'][key] = 'needle'
        def bad_file(data):
            record = next(iter(context(data)['files'].values()))
            record[0][0] = 'needle'
        def bad_word_hash(data):
            next(iter(data['nodes'].values()))['words'] = 'needle'
        for corrupt in (root_field, bad_store, bad_verdict, bad_file, bad_word_hash):
            data = json.loads(original)
            corrupt(data)
            self.cache_file.write_text(json.dumps(data))
            prior = self.cache_file.stat().st_ino
            self.assertEqual(self.prime(), ([], []))
            self.assertNotEqual(self.cache_file.stat().st_ino, prior)
            text = self.cache_file.read_text()
            self.assertNotIn('needle', text.casefold())
            self.assertNotIn('/absolute/', text)
            self.assertTrue(self.guard._m_term_cache.valid_cache(json.loads(text)))

    def test_prefetched_repository_cannot_fall_back_to_folder_after_git_disappears(self):
        other = self.repo(self.tmp / 'other', {'file.txt': 'clean\n'})
        (self.home / 'one.txt').write_text('clean\n')  # index alone holds the forbidden word
        self.node['local']['worktrees'] = {'second': {'path': str(other)}}
        self.assertTrue(any(where.startswith('index stage') for _, where in self.scan()[0]))
        for hook in ('home_files', 'term_file_observations'):
            for warm in (False, True):
                for dangling in (False, True):
                    with self.subTest(hook=hook, warm=warm, dangling=dangling):
                        self.cache_file.unlink(missing_ok=True)
                        if warm:
                            self.prime()
                        original = getattr(self.guard, hook)
                        marker = self.home / '.git'
                        saved = self.tmp / 'saved-git'
                        def disappear(*args, **kwargs):
                            result = original(*args, **kwargs)
                            root = args[0 if hook == 'home_files' else 1]
                            if root == self.home and not saved.exists():
                                marker.rename(saved)
                                if dangling:
                                    marker.symlink_to(self.tmp / 'missing-git-target')
                            return result
                        try:
                            with mock.patch.object(self.guard, hook, side_effect=disappear):
                                hits, errors = self.scan()
                            self.assertTrue(errors, (hits, errors))
                        finally:
                            if marker.is_symlink():
                                marker.unlink()
                            if saved.exists():
                                saved.rename(marker)
                        self.assertTrue(any(where.startswith('index stage') for _, where in self.scan()[0]))

    def test_old_loaded_scanner_cannot_publish_under_replaced_source_version(self):
        self.clean_fixture()
        core = self.tmp / 'installed-core'
        shutil.copytree(CORE / 'lib', core / 'lib')
        (core / 'bin').mkdir()
        shutil.copy2(CORE / 'bin/brain', core / 'bin/brain')
        old = core_loader.SourceFileLoader('old_scanner', str(core / 'bin/brain')).load_module()._owners['scan_terms']
        source = core / 'lib/brain/guard.py'
        text = source.read_text()
        self.assertIn('found |= {x for x in needles if x in text}', text)
        source.write_text(text.replace('found |= {x for x in needles if x in text}', 'found |= set(needles)', 1))
        with old.term_scan_cache(persist=True):
            self.assertEqual(old.scan_terms(self.node, terms=self.terms), ([], []))
        new = core_loader.SourceFileLoader('new_scanner', str(core / 'bin/brain')).load_module()._owners['scan_terms']
        self.assertNotEqual(old._m_term_cache.scanner_version(), new._m_term_cache.scanner_version())
        self.assertTrue(new.scan_terms(self.node, terms=self.terms)[0])

    def test_default_only_upgrades_bind_positional_and_keyword_defaults(self):
        notes = self.node['dir']
        (notes / 'node.md').write_text('fixture\n')
        (notes / 'library').mkdir()
        (notes / 'library/item.md').write_text('---\nscope: needle\n---\nclean\n')
        self.node['home_path'] = notes / 'library'
        signature = 'def governance_text(text, principals=(), drop=False):'
        for keyword_only in (False, True):
            with self.subTest(keyword_only=keyword_only):
                self.cache_file.unlink(missing_ok=True)
                core = self.tmp / ('core-defaults-' + str(keyword_only))
                shutil.copytree(CORE / 'lib', core / 'lib')
                (core / 'bin').mkdir()
                shutil.copy2(CORE / 'bin/brain', core / 'bin/brain')
                source = core / 'lib/brain/guard.py'
                original = source.read_text()
                self.assertIn(signature, original)
                prefix = 'def governance_text(text, principals=(), ' + ('*, ' if keyword_only else '')
                source.write_text(original.replace(signature, prefix + 'drop=True):', 1))
                old = core_loader.SourceFileLoader('old_defaults', str(core / 'bin/brain')).load_module()._owners['scan_terms']
                source.write_text(original.replace(signature, prefix + 'drop=False):', 1))
                new = core_loader.SourceFileLoader('new_defaults', str(core / 'bin/brain')).load_module()._owners['scan_terms']
                expected = new.scan_terms(self.node, terms=self.terms)
                self.assertTrue(expected[0])
                self.assertNotEqual(old._TERM_IMPLEMENTATION, new._TERM_IMPLEMENTATION)
                with old.term_scan_cache(persist=True):
                    self.assertEqual(old.scan_terms(self.node, terms=self.terms), ([], []))
                self.assertNotEqual(old._m_term_cache.scanner_version(), new._m_term_cache.scanner_version())
                self.assertEqual(new.scan_terms(self.node, terms=self.terms), expected)
                self.cache_file.unlink()
                self.assertEqual(new.scan_terms(self.node, terms=self.terms), expected)

    def test_unsupported_compiled_defaults_disable_cache_without_disabling_scan(self):
        core = self.tmp / 'core-unsupported'
        shutil.copytree(CORE / 'lib', core / 'lib')
        (core / 'bin').mkdir()
        shutil.copy2(CORE / 'bin/brain', core / 'bin/brain')
        source = core / 'lib/brain/guard.py'
        source.write_text(source.read_text().replace('def term_open_file(path, flags):',
                          'def term_open_file(path, flags, unsupported=object()):', 1))
        new = core_loader.SourceFileLoader('unsupported_defaults', str(core / 'bin/brain')).load_module()._owners['scan_terms']
        self.assertIsNone(new._TERM_IMPLEMENTATION)
        with new.term_scan_cache(persist=True):
            self.assertTrue(new.scan_terms(self.node, terms=self.terms)[0])
        self.assertFalse(self.cache_file.exists())

    def test_receiver_bound_and_ordered_mapping_defaults_cannot_publish_upgrade_verdicts(self):
        notes = self.node['dir']
        (notes / 'node.md').write_text('fixture\n')
        (notes / 'library').mkdir()
        (notes / 'library/item.md').write_text('---\nscope: needle\n---\nclean\n')
        self.node['home_path'] = notes / 'library'
        signature = 'def governance_text(text, principals=(), drop=False):'
        cases = [('receiver', '{"enabled": True}.get', '{"enabled": False}.get', 'if drop("enabled"):'),
                 ('mapping', '{"yes": True, "no": False}', '{"no": False, "yes": True}',
                  'if next(iter(drop.values())):')]
        for name, previous, current, condition in cases:
            with self.subTest(default=name):
                self.cache_file.unlink(missing_ok=True)
                core = self.tmp / ('core-default-' + name)
                shutil.copytree(CORE / 'lib', core / 'lib')
                (core / 'bin').mkdir()
                shutil.copy2(CORE / 'bin/brain', core / 'bin/brain')
                source = core / 'lib/brain/guard.py'
                text = source.read_text().replace('        if drop:', '        ' + condition, 1)
                source.write_text(text.replace(signature, 'def governance_text(text, principals=(), drop=' + previous + '):', 1))
                old = core_loader.SourceFileLoader('old_receiver', str(core / 'bin/brain')).load_module()._owners['scan_terms']
                source.write_text(text.replace(signature, 'def governance_text(text, principals=(), drop=' + current + '):', 1))
                new = core_loader.SourceFileLoader('new_receiver', str(core / 'bin/brain')).load_module()._owners['scan_terms']
                self.assertIsNone(old._TERM_IMPLEMENTATION)
                self.assertIsNone(new._TERM_IMPLEMENTATION)
                expected = new.scan_terms(self.node, terms=self.terms)
                self.assertTrue(expected[0])
                with old.term_scan_cache(persist=True):
                    self.assertEqual(old.scan_terms(self.node, terms=self.terms), ([], []))
                self.assertFalse(self.cache_file.exists())
                self.assertEqual(new.scan_terms(self.node, terms=self.terms), expected)

    def test_mutable_unordered_and_subclass_defaults_are_not_accepted_as_plain_values(self):
        cache = self.guard._m_term_cache
        class SpecialTuple(tuple):
            pass
        for default in ([], {}, frozenset((0, 8)), SpecialTuple((True,)), {'enabled': True}.get):
            def fixture(value=default):
                return value
            with self.subTest(kind=type(default).__name__):
                with self.assertRaises(ValueError):
                    cache.loaded_code({'__name__': fixture.__module__, 'fixture': fixture})

    def test_module_only_upgrade_cannot_reuse_a_clean_loaded_scanner(self):
        notes = self.node['dir']
        (notes / 'node.md').write_text('fixture\n')
        (notes / 'library').mkdir()
        (notes / 'library/item.md').write_text('---\nscope: needle\n---\nclean\n')
        self.node['home_path'] = notes / 'library'
        core = self.tmp / 'module-upgrade'
        shutil.copytree(CORE / 'lib', core / 'lib')
        (core / 'bin').mkdir()
        shutil.copy2(CORE / 'bin/brain', core / 'bin/brain')
        source = core / 'lib/brain/guard.py'
        signature = 'def governance_text(text, principals=(), drop=False):'
        text = source.read_text().replace('        if drop:', '        if _GOVERNANCE_DROP:', 1)
        source.write_text(text.replace(signature, '_GOVERNANCE_DROP = True\n\n' + signature, 1))
        old = core_loader.SourceFileLoader('old_global', str(core / 'bin/brain')).load_module()._owners['scan_terms']
        source.write_text(text.replace(signature, '_GOVERNANCE_DROP = False\n\n' + signature, 1))
        new = core_loader.SourceFileLoader('new_global', str(core / 'bin/brain')).load_module()._owners['scan_terms']
        expected = new.scan_terms(self.node, terms=self.terms)
        self.assertTrue(expected[0])
        self.assertNotEqual(old._TERM_IMPLEMENTATION, new._TERM_IMPLEMENTATION)
        with old.term_scan_cache(persist=True):
            self.assertEqual(old.scan_terms(self.node, terms=self.terms), ([], []))
        self.assertEqual(new.scan_terms(self.node, terms=self.terms), expected)


class Hardening(Scenario):
    """What the second-runtime reviews broke, kept broken-proof."""

    def never(self, r, node):
        nd = next(p_.parent for p_ in r.rglob("node.md") if f"id: {node}\n" in p_.read_text())
        (nd / "never-name.txt").write_text("OldCo\n")
        return nd

    def test_backlog_has_the_same_note_exemption_as_decisions(self):
        r = self.init('fixture')
        self.b(r, 'new', 'notes', '--title', 'Notes')
        nd = self.never(r, 'notes')
        for name in ('decisions.md', 'backlog.md'):
            (nd / name).write_text('Discuss the OldCo never-name rule.\n')
        self.assertIn('guard: clean', self.b(r, 'guard', 'notes'))

    def test_guard_on_a_home_inside_the_brain(self):
        r = self.init("alice")
        self.b(r, "new", "notes", "--title", "Notes")
        nd = self.never(r, "notes")
        (r / ".gitignore").write_text((r / ".gitignore").read_text() + "dist/\n")
        (nd / "dist").mkdir()
        (nd / "dist" / "gen.js").write_text("OldCo\n")                    # ignored: not part of the home
        os.mkfifo(nd / "sources" / "events.pipe")                         # a special file never blocks the scan
        self.assertIn("guard: clean", self.b(r, "guard", "notes"))
        (nd / ".github").mkdir()
        (nd / ".github" / "ci.yml").write_text("run: oldco\n")             # dot-folders are part of it
        (nd / "sources" / "OldCo").mkdir()
        (nd / "sources" / "OldCo" / "clean.txt").write_text("nothing\n")   # a folder's name is too
        out = self.b(r, "guard", "notes", ok=False)
        self.assertIn(".github/ci.yml", out)
        self.assertIn("sources/[…]/clean.txt", out)
        self.assertNotIn("oldco", out.lower())

    def test_an_unreadable_repository_is_never_clean(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "code" / "app")
        self.b(r, "new", "app", "--title", "App", "--home", str(home))
        self.never(r, "app")
        (home / ".git" / "HEAD").rename(home / ".git" / "HEAD.bak")
        r_ = subprocess.run([sys.executable, str(r / "core" / "bin" / "brain"), "guard", "app"], cwd=r, env=self.env,
                            capture_output=True, text=True)
        self.assertEqual(r_.returncode, 2, r_.stdout + r_.stderr)
        self.assertIn("incomplete", r_.stdout)
        (home / ".git" / "HEAD.bak").rename(home / ".git" / "HEAD")
        out = self.b(r, "guard", "app", str(self.tmp / "OldCo-missing"), ok=False)
        self.assertNotIn("oldco", out.lower())

    def test_no_review_brief_into_a_missing_home(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "code" / "app")
        self.b(r, "new", "app", "--title", "App", "--home", str(home))
        self.b(r, "review", "app", "--branch", "main", "--base", "main")
        loc = json.loads((r / ".brain" / "local.json").read_text())
        loc["nodes"]["app"].pop("home")
        (r / ".brain" / "local.json").write_text(json.dumps(loc))
        self.assertIn("isn't on this machine", self.b(r, "review", "app", "--branch", "main", ok=False))

    def test_a_git_hook_that_runs_brain_does_not_deadlock(self):
        r = self.init("alice")
        gi = r / ".gitignore"                       # something for the hook's `check --fix` to repair and commit
        gi.write_text(gi.read_text().replace(".brain/\n", ""))
        self.git("commit", "-qam", "drop .brain/ from .gitignore", cwd=r)
        hook = r / ".git" / "hooks" / "post-commit"
        hook.write_text(f"#!/bin/sh\n'{sys.executable}' '{r / 'core' / 'bin' / 'brain'}' check --fix >/dev/null 2>&1\n")
        hook.chmod(0o755)
        p = subprocess.run([sys.executable, str(r / "core" / "bin" / "brain"), "new", "x", "--title", "X"], cwd=r,
                           env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(".brain/", gi.read_text())    # the hook did run, inside brain's own commit

    def test_a_branch_called_job_keeps_its_row(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "code" / "app")
        self.b(r, "new", "app", "--title", "App", "--home", str(home))
        self.b(r, "wt", "add", "app", "Job")
        self.b(r, "review", "app", "--branch", "Job", "--built-by", "claude")
        self.b(r, "wt", "add", "app", "other")
        self.assertIn("open  Job", self.b(r, "jobs", "app"))
        self.assertIn("| Job | 20", (r / "nodes" / "app" / "job-ledger.md").read_text())



class Hardening3(Scenario):
    """Round three of the second-runtime review."""

    def test_commands_that_change_things_take_turns(self):
        r = self.init("alice")
        for i in range(6):
            self.b(r, "new", f"n{i}", "--title", f"N{i}")
        ps = [subprocess.Popen([sys.executable, str(r / "core" / "bin" / "brain"), "set", f"n{i}", f"coordinator=t{i}"],
                               cwd=r, env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
              for i in range(6)]
        self.assertEqual([p.communicate()[1] and p.returncode or p.returncode for p in ps], [0] * 6)
        loc = json.loads((r / ".brain" / "local.json").read_text())
        self.assertEqual({k: v.get("coordinator") for k, v in loc["nodes"].items() if k.startswith("n")},
                         {f"n{i}": f"t{i}" for i in range(6)})            # no binding lost

    def test_a_dead_holders_permission_does_not_outlive_it(self):
        import fcntl
        r = self.init("alice")
        lock = r / ".brain" / "lock"
        lock.parent.mkdir(exist_ok=True)
        with open(lock, "a+") as fh:                                  # someone else holds the lock now
            fcntl.flock(fh, fcntl.LOCK_EX)
            fh.seek(0); fh.truncate(); fh.write("1"); fh.flush()     # pid 1: alive, but not our ancestor's lock...
            env = {**self.env, "BRAIN_LOCK_HELD": f"{r.resolve()}|999999", "BRAIN_LOCK_WAIT": "2"}  # ...a dead holder
            p = subprocess.run([sys.executable, str(r / "core" / "bin" / "brain"), "new", "x", "--title", "X"], cwd=r,
                               env=env, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(p.returncode, 0)                      # it waited, then gave up: it didn't skip
            self.assertIn("held this instance's lock", p.stderr)
        self.assertNotIn("id: x\n", "".join(q.read_text() for q in r.rglob("node.md")))

    def test_guard_edge_cases(self):
        r = self.init("alice")
        self.b(r, "new", "notes", "--title", "Notes")
        nd = r / "nodes" / "notes"
        (nd / "never-name.txt").write_text("OldCo\n")
        comp = nd / "sources" / "component"
        comp.mkdir(parents=True)
        (comp / "node.md").write_text("# docs for a component, not a brain node\n")
        (comp / "artifact.py").write_text("# from OldCo\n")
        self.assertIn("artifact.py", self.b(r, "guard", "notes", ok=False))   # only real child nodes are skipped
        (comp / "artifact.py").write_text("clean\n")
        locked = nd / "sources" / "locked.txt"
        locked.write_text("OldCo\n")
        locked.chmod(0)
        try:
            p = subprocess.run([sys.executable, str(r / "core" / "bin" / "brain"), "guard", "notes"], cwd=r,
                               env=self.env, capture_output=True, text=True)
            self.assertEqual(p.returncode, 2, p.stdout)                       # unreadable isn't clean
            self.assertIn("couldn't read", p.stdout)
        finally:
            locked.chmod(0o644)
        # a sealed home whose repository can't be read: incomplete, not clean
        self.b(r, "principal", "add", "team")
        sh = self.repo(self.tmp / "code" / "shared")
        self.b(r, "new", "shared", "--title", "Shared", "--owners", "team", "--home", str(sh))
        (sh / ".git" / "HEAD").rename(sh / ".git" / "HEAD.bak")
        p = subprocess.run([sys.executable, str(r / "core" / "bin" / "brain"), "guard", "shared"], cwd=r,
                           env=self.env, capture_output=True, text=True)
        (sh / ".git" / "HEAD.bak").rename(sh / ".git" / "HEAD")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)

    def test_decisions_with_offsets_reach_the_live_view(self):
        r = self.init("alice")
        self.b(r, "new", "a", "--title", "A")
        d = r / "nodes" / "a" / "decisions.md"
        d.write_text(d.read_text() + "\n## 2026-10-19 06:32 +03:00: Use Postgres\nWhy: writes.\n"
                     "\n## 2026-01-10 09:00: An old one, in winter\nWhy: x.\n")
        st = json.loads(self.b(r, "ui", "--json"))
        dec = {x["title"]: x["t"] for x in next(n for n in st["nodes"] if n["id"] == "a")["decisions"]}
        self.assertEqual(dec["Use Postgres"], 1792380720)                  # 03:32Z
        self.assertEqual(dec["An old one, in winter"], 1768035600)         # UTC instance: 09:00Z



class Hardening4(Scenario):
    """Round four of the second-runtime review."""

    def held(self, r):
        import fcntl
        lock = r / ".brain" / "lock"
        lock.parent.mkdir(exist_ok=True)
        fh = open(lock, "a+")
        fcntl.flock(fh, fcntl.LOCK_EX)
        return fh

    def run_b(self, r, *args, env=None):
        return subprocess.run([sys.executable, str(r / "core" / "bin" / "brain"), *args], cwd=r,
                              env={**self.env, "BRAIN_LOCK_WAIT": "2", **(env or {})}, capture_output=True, text=True,
                              input="", timeout=60)

    def test_unreadable_files_in_a_repository_are_not_clean(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "code" / "app", {"a.txt": "OldCo\n"})
        self.b(r, "new", "app", "--title", "App", "--home", str(home))
        (r / "nodes" / "app" / "never-name.txt").write_text("OldCo\n")
        (home / "a.txt").chmod(0)
        try:
            p = self.run_b(r, "guard", "app")
            self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        finally:
            (home / "a.txt").chmod(0o644)

    def test_every_writer_waits_and_readers_do_not(self):
        r = self.init("alice")
        self.b(r, "item", "add", "m1", "--from", "root", "--title", "M", "--kind", "method", "--ref", "x", "--body", "b")
        item = next(r.rglob("library/m1.md"))
        before = item.read_text()
        fh = self.held(r)
        try:
            p = self.run_b(r, "item", "quarantine", "m1")
            self.assertNotEqual(p.returncode, 0)                     # it waited, then gave up...
            self.assertEqual(item.read_text(), before)               # ...without touching the item
            out = self.tmp / "brief.txt"
            self.assertNotEqual(self.run_b(r, "brief", "root", "--task", "x", "--out", str(out)).returncode, 0)
            self.assertFalse(out.exists())
            for args in (["principal", "list"], ["schedule", "list"], ["brief", "root", "--task", "x"], ["show", "root"]):
                self.assertEqual(self.run_b(r, *args).returncode, 0, args)   # reading never waits
        finally:
            fh.close()

    def test_the_stop_hook_never_breaks_a_turn(self):
        r = self.init("alice")
        self.b(r, "new", "a", "--title", "A")
        a = r / "nodes" / "a"
        st = a / "status.md"
        st.write_text(st.read_text().replace(st.read_text().splitlines()[0], "Updated: 2020-01-01 00:00 by a coordinator"))
        (r / ".brain").mkdir(exist_ok=True)
        (r / ".brain" / "hooks.json").write_text("{half a fi")              # half-written by another session
        payload = json.dumps({"cwd": str(a), "session_id": "s1"})
        p = subprocess.run([sys.executable, str(r / "core" / "bin" / "brain"), "hook", "stop"], cwd=a, env=self.env,
                           input=payload, capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('"decision": "block"', p.stdout)                      # the reminder still goes out
        (r / ".brain").chmod(0o500)                                         # nothing can be written there
        try:
            p = subprocess.run([sys.executable, str(r / "core" / "bin" / "brain"), "hook", "stop"], cwd=a, env=self.env,
                               input=json.dumps({"cwd": str(a), "session_id": "s2"}), capture_output=True, text=True,
                               timeout=30)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(p.stderr, "")
        finally:
            (r / ".brain").chmod(0o755)



class Hardening5(Scenario):
    """Round five of the second-runtime review."""

    def test_big_files_and_unreadable_folders(self):
        r = self.init("alice")
        self.b(r, "new", "notes", "--title", "Notes")
        nd = r / "nodes" / "notes"
        (nd / "never-name.txt").write_text("OldCo\n")
        big = nd / "sources" / "big.txt"
        big.write_bytes(b"x" * (2_000_010 - 3) + b"Old" + b"Co and more\n")   # past 2 MB, across a 1 MiB boundary
        self.assertIn("big.txt", self.b(r, "guard", "notes", ok=False))
        big.write_bytes(b"x" * ((1 << 20) - 3) + b"OldCo\n")                     # split exactly at a chunk edge
        self.assertIn("big.txt", self.b(r, "guard", "notes", ok=False))
        big.unlink()
        hidden = nd / "sources" / "hidden"
        hidden.mkdir()
        (hidden / "f.txt").write_text("OldCo\n")
        hidden.chmod(0)
        try:
            p = subprocess.run([sys.executable, str(r / "core" / "bin" / "brain"), "guard", "notes"], cwd=r,
                               env=self.env, capture_output=True, text=True)
            self.assertEqual(p.returncode, 2, p.stdout)                           # couldn't look: not clean
        finally:
            hidden.chmod(0o755)



class Hardening6(Scenario):
    """Round six of the second-runtime review."""

    def test_a_character_split_between_reads_is_still_found(self):
        r = self.init("alice")
        self.b(r, "new", "notes", "--title", "Notes")
        nd = r / "nodes" / "notes"
        (nd / "never-name.txt").write_text("Café\n")
        f = nd / "sources" / "edge.txt"
        for pad in range(1, 6):                                       # é straddles the 1 MiB edge at some pad
            f.write_bytes(b"x" * ((1 << 20) - pad) + "Café\n".encode())
            self.assertIn("edge.txt", self.b(r, "guard", "notes", ok=False), pad)

    def test_seal_commits_nothing_that_names_a_never_word(self):
        r = self.init("alice")
        self.b(r, "principal", "add", "team")
        self.b(r, "new", "legacy", "--title", "OldCo port")
        (r / "nodes" / "legacy" / "never-name.txt").write_text("OldCo\n")
        home = self.tmp / "code" / "legacy"
        p = subprocess.run([sys.executable, str(r / "core" / "bin" / "brain"), "seal", "legacy", "--owners", "team",
                            "--home", str(home)], cwd=r, env=self.env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertNotIn("oldco", (p.stdout + p.stderr).lower())
        self.assertEqual(subprocess.run(["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=home,
                                        capture_output=True).returncode, 1 if (home / ".git").exists() else 128)



class Hardening7(Scenario):
    """Round seven of the second-runtime review."""

    def test_binary_files_greek_words_and_the_word_list_itself(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "code" / "app", {".gitattributes": "*.dat binary\n"})
        self.b(r, "new", "app", "--title", "App", "--home", str(home))
        (r / "nodes" / "app" / "never-name.txt").write_text("OldCo\nΟΣ\n")
        (home / "x.dat").write_bytes(b"\0\1OldCo\0")                       # git calls it binary
        self.assertIn("x.dat", self.b(r, "guard", "app", ok=False))
        (home / "x.dat").unlink()
        for pad in (2, 3):                                                   # Σ on a chunk edge
            (home / "g.txt").write_bytes(b"x" * ((1 << 20) - pad) + "ΟΣ\n".encode())
            self.assertIn("g.txt", self.b(r, "guard", "app", ok=False))
        (home / "g.txt").unlink()
        self.b(r, "principal", "add", "team")
        self.b(r, "new", "legacy", "--title", "Legacy")
        (r / "nodes" / "legacy" / "never-name.txt").write_text("OldCo\n")
        for rel in ("never-name.txt", "./never-name.txt", "sources/../never-name.txt"):
            out = self.b(r, "seal", "legacy", "--owners", "team", "--home", str(self.tmp / "h"), "--move", rel, ok=False)
            self.assertIn("stays on the operator's side", out)
            self.assertTrue((r / "nodes" / "legacy" / "never-name.txt").exists())
            self.assertFalse((self.tmp / "h").exists())                       # refused before anything happened



class Hardening8(Scenario):
    """Ordinary staged edits, deletions and populated submodules from review round eight."""

    def test_runtime_links_visit_only_registered_nodes(self):
        r = self.init("alice")
        impostors = [r / ".git" / "nodes" / "fake", r / "artifacts" / "nodes" / "fake"]
        for folder in impostors:
            folder.mkdir(parents=True)
            (folder / "AGENTS.md").write_text("Unrelated instructions\n")
        self.b(r, "new", "a", "--title", "A")
        self.b(r, "new", "a/b", "--title", "B")
        for folder in impostors:
            self.assertFalse((folder / "CLAUDE.md").exists())
        self.assertTrue((r / "nodes" / "a" / "nodes" / "b" / "CLAUDE.md").is_symlink())

    def test_index_contents_and_tracked_deletions(self):
        r = self.init("alice")
        home = self.repo(self.tmp / "code" / "app", {"a.txt": "clean\n"})
        self.b(r, "new", "app", "--title", "App", "--home", str(home))
        (r / "nodes" / "app" / "never-name.txt").write_text("OldCo\nCafé\n")
        f = home / "a.txt"
        # Binary content and an accented term split across a chunk edge must also be checked in the index.
        f.write_bytes(b"\0OldCo\n" + b"x" * ((1 << 20) - 9) + "Café\n".encode())
        self.git("add", "a.txt", cwd=home)
        f.write_text("clean again\n")
        out = self.b(r, "guard", "app", ok=False)
        self.assertIn("index stage 0:", out)
        self.assertIn("term 1", out)
        self.assertIn("term 2", out)
        self.assertNotIn("OldCo", out)
        f.unlink()
        out = self.b(r, "guard", "app", ok=False)
        self.assertIn("index stage 0:", out)
        self.assertNotIn("incomplete", out)
        self.git("add", "-u", cwd=home)
        self.assertIn("clean", self.b(r, "guard", "app"))
        # A clean unstaged deletion is clean too, while its clean blob remains staged.
        self.git("restore", "--source=HEAD", "--staged", "--worktree", "a.txt", cwd=home)
        f.unlink()
        self.assertIn("clean", self.b(r, "guard", "app"))

    def test_submodules_include_working_index_and_history(self):
        r = self.init("alice")
        lib = self.repo(self.tmp / "lib", {"note.txt": "clean\n"})
        home = self.repo(self.tmp / "code" / "app")
        self.git("submodule", "add", "-q", str(lib), "vendor/lib", cwd=home)
        self.git("commit", "-qam", "add library", cwd=home)
        self.b(r, "new", "app", "--title", "App", "--home", str(home))
        (r / "nodes" / "app" / "never-name.txt").write_text("OldCo\n")
        sub = home / "vendor" / "lib"
        self.assertIn("clean", self.b(r, "guard", "app"))
        (sub / "note.txt").write_text("OldCo\n")
        self.assertIn("vendor/lib/note.txt", self.b(r, "guard", "app", ok=False))
        self.git("add", "note.txt", cwd=sub)
        (sub / "note.txt").write_text("clean\n")
        self.assertIn("index stage 0:", self.b(r, "guard", "app", ok=False))
        self.git("restore", "--staged", "note.txt", cwd=sub)
        self.git("branch", "OldCo-migration", cwd=sub)
        self.assertIn("branch", self.b(r, "guard", "app", ok=False))
        self.git("branch", "-D", "OldCo-migration", cwd=sub)
        self.git("commit", "--allow-empty", "-qm", "OldCo migration", cwd=sub)
        self.assertIn("commit", self.b(r, "guard", "app", ok=False))
        self.git("submodule", "deinit", "-f", "vendor/lib", cwd=home)
        out = self.b(r, "guard", "app", ok=False)
        self.assertIn("incomplete", out)
        self.assertIn("submodule unavailable", out)

    def test_seal_redacts_inherited_owner_summary(self):
        r = self.init("oldco")
        self.b(r, "new", "legacy", "--title", "Legacy")
        (r / "nodes" / "legacy" / "never-name.txt").write_text("OldCo\n")
        self.b(r, "principal", "add", "team")
        out = self.b(r, "seal", "legacy", "--owners", "team", "--home", str(self.tmp / "code" / "legacy"))
        self.assertIn("effective owners", out)
        self.assertNotIn("oldco", out.casefold())



class ImportSafety(Scenario):
    def test_secret_prefixes_do_not_match_project_metadata(self):
        r = self.init("alice")
        for name in ("task-scheduler", "risk-analysis", "disk-usage-tool", "sk-" + "a" * 24):
            h = self.repo(self.tmp / name)
            self.b(r, "adopt", str(h))
            row = json.loads(self.b(r, "scan", str(h), "--json"))[0]
            self.assertEqual(row["name"], name)
            self.assertNotIn("[redacted secret]", row["path"])
            self.b(r, "brief", name, "--import")
            self.b(r, "item", "add", name, "--from", name, "--title", name, "--kind", "lesson",
                   "--ref", name + "/AGENTS.md", "--repo", name, "--commit", "a" * 40,
                   "--proposed", "--body", "Bound memory.")
        for i, body in enumerate(("sk-" + "a" * 24, "ghp_" + "b" * 36, "AKIA" + "A" * 16,
                                  "PASSWORD=fake", "Api_Key: fake")):
            self.b(r, "item", "add", "unsafe-" + str(i), "--from", "root", "--title", "Unsafe",
                   "--kind", "lesson", "--ref", "source", "--repo", "fixture", "--commit", "a" * 40,
                   "--proposed", "--body", body, ok=False)
        self.b(r, "item", "add", "case-sensitive", "--from", "root", "--title", "Safe",
               "--kind", "fact", "--ref", "source", "--repo", "fixture", "--commit", "a" * 40,
               "--proposed", "--body", "SK-" + "a" * 24 + " xsk-" + "b" * 24)

    def test_local_origins_never_enter_committed_node_metadata(self):
        r = self.init("alice")
        for i, origin in enumerate((str(self.tmp / "origin"), "~/origin", "./origin", "../origin",
                                    "file:///tmp/origin", "relative/origin")):
            h = self.repo(self.tmp / ("local-" + str(i)))
            self.git("remote", "add", "origin", origin, cwd=h)
            command = "adopt" if i % 2 else "new"
            args = (str(h),) if command == "adopt" else (h.name, "--title", "Local", "--home", str(h))
            self.b(r, command, *args)
            text = self.git("show", "HEAD:nodes/" + h.name + "/node.md", cwd=r)
            self.assertNotIn("home_url:", text.replace("home_url: none", ""))
            self.assertNotIn(origin, text)
        h = self.repo(self.tmp / "rewritten")
        self.git("config", "url.file:///tmp/private-origin/.insteadOf", "https://example.test/", cwd=h)
        self.git("remote", "add", "origin", "https://example.test/project", cwd=h)
        self.b(r, "adopt", str(h))
        self.assertNotIn("file:", (r / "nodes/rewritten/node.md").read_text())

    def test_ssh_clone_users_survive_without_passwords(self):
        r = self.init("alice")
        for i, (origin, clean) in enumerate((
                ("git@github.com:org/repo", "git@github.com:org/repo"),
                ("ssh://git:fake@host.test/x?private#fragment", "ssh://git@host.test/x"),
                ("user:fake@host.test:org/repo", "user@host.test:org/repo"),
                ("ftp://user:fake@host.test/x", "ftp://host.test/x"))):
            h = self.repo(self.tmp / ("remote-" + str(i)))
            self.git("remote", "add", "origin", origin, cwd=h)
            self.b(r, "new", h.name, "--title", "Remote", "--home", str(h))
            self.assertIn("home_url: " + clean, (r / "nodes" / h.name / "node.md").read_text())
            self.assertEqual(json.loads(self.b(r, "scan", str(h), "--json"))[0]["remotes"], [clean])

    def test_direct_learning_keeps_never_name_compatibility(self):
        r = self.init("alice")
        (r / "never-name.txt").write_text("PrivateFixtureTerm\n")
        args = ("--from", "root", "--title", "Fact", "--kind", "fact", "--ref", "source",
                "--body", "PrivateFixtureTerm")
        self.b(r, "item", "add", "legacy", *args)
        self.assertIn("PrivateFixtureTerm", (r / "library/legacy.md").read_text())
        self.b(r, "item", "add", "proposal", *args, "--proposed", "--repo", "fixture",
               "--commit", "a" * 40, ok=False)



class SmallFixes(Scenario):
    """Briefs written to files, a merge with empty folders, history batches that fit the readers."""

    def test_every_brief_out_stays_out_of_the_home_its_checkouts_and_the_instance(self):
        r, home, wt = _test_verify.WorkerReadyPackets.job(self)
        self.b(r, "specialist", "new", "writer", "--at", "root", "--title", "Writer")
        outside = self.tmp / "briefs"
        outside.mkdir()
        (outside / "into-home").symlink_to(home)
        (home / "precious.txt").write_text("precious\n")
        os.link(home / "precious.txt", outside / "linked.md")
        commands = {"brief": ["brief", "a", "--task", "Fix it"], "small": ["brief", "a", "--small", "--task", "Fix it"],
                    "branch": ["brief", "a", "--branch", "job", "--task", "Fix it"],
                    "review": ["review", "a", "--branch", "job", "--built-by", "claude"],
                    "hire": ["hire", "writer", "a", "--task", "Write it"]}
        for name, args in commands.items():
            args += ["--why-separate", "Synthetic independent concern"]
            for dest, why in ((home / "b.md", "inside the home"), (wt / "b.md", "inside the home"),
                              (outside / "into-home" / "b.md", "inside the home"), (r / "nodes/a/b.md", "inside the instance"),
                              (r / ".brain/b.md", "inside the instance")):
                self.assertIn(why, self.b(r, *args, "--out", str(dest), ok=False), (name, dest))
                self.assertFalse(dest.exists(), (name, dest))
            self.assertIn("a link or not a folder", self.b(r, *args, "--out", str(outside / "linked.md" / "x"), ok=False))
            mine = outside / f"{name}.md"
            self.assertEqual(self.b(r, *args, "--out", str(mine)).strip(), str(mine))
            self.assertEqual(mine.stat().st_mode & 0o777, 0o600)
        self.b(r, "brief", "a", "--task", "Fix it", "--out", str(outside / "linked.md"))
        self.assertEqual((home / "precious.txt").read_text(), "precious\n")  # replaced, never written through
        self.assertIn("Role: worker", (outside / "linked.md").read_text())
        self.assertEqual([p.name for p in outside.iterdir() if p.name.startswith(".brief-")], [])
        self.assertNotIn("b.md", self.git("status", "--porcelain", "--ignored", cwd=home))

    def test_checkouts_with_newlines_and_folders_moved_mid_write_get_nothing(self):
        from core_loader import SourceFileLoader
        r, home, wt = _test_verify.WorkerReadyPackets.job(self)
        odd = self.tmp / "line\nbreak"
        self.git("worktree", "add", "-q", "-b", "odd", str(odd), cwd=home)
        self.assertIn("inside the home", self.b(r, "brief", "a", "--task", "x", "--out", str(odd / "b.md"), ok=False))
        self.assertFalse((odd / "b.md").exists())
        m = SourceFileLoader("brain_moving", str(CORE / "bin/brain")).load_module()
        inst = m.Instance(r)
        n = inst.node("a")
        put, check = m.history_put, m.fd_outside
        first = self.tmp / "first"
        first.mkdir()

        def put_then_move(fd, name, text):  # the checked folder moves into the home, with its open descriptor
            put(fd, name, text)
            os.rename(first, home / "first")
        m.history_put = put_then_move
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            m.write_out(inst, n, first / "brief.md", "text\n")
        self.assertEqual(list((home / "first").iterdir()), [])
        m.history_put, calls, second = put, [], self.tmp / "second"
        second.mkdir()

        def check_then_move(*args):  # moved between the last check before the rename and the rename
            check(*args)
            calls.append(1)
            if len(calls) == 1:
                os.rename(second, home / "second")
        m.fd_outside = check_then_move
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            m.write_out(inst, n, second / "brief.md", "text\n")
        self.assertEqual(list((home / "second").iterdir()), [])  # published, then taken back
        m.fd_outside = check
        third = self.tmp / "third"
        third.mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            m.write_out(inst, n, third / "brief.md", "text\n")
        self.assertEqual((third / "brief.md").read_text(), "text\n")

    def test_node_files_never_follow_a_link_out_of_the_brain(self):
        r, home, wt = _test_verify.WorkerReadyPackets.job(self)
        hb = r / "nodes/a/job-handbacks"
        shutil.rmtree(hb, ignore_errors=True)
        hb.symlink_to(home)
        out = self.b(r, "handback", "--summary", "Synthetic result", "a", "--branch", "job", "--state", "blocked", "--report", "r.md",
                     "--lessons", "none: none", ok=False)
        self.assertIn("out of the brain", out)
        self.assertEqual([p.name for p in home.iterdir() if p.suffix == ".json"], [])
        hb.unlink()
        inbox = r / "nodes/a/inbox.md"
        inbox.unlink()
        inbox.symlink_to(home / "calc.py")
        before = (home / "calc.py").read_text()
        self.b(r, "handback", "--summary", "Synthetic result", "--question", "Synthetic choice?", "a", "--branch", "job", "--state", "question", "--report", "r.md",
               "--lessons", "none: none", ok=False)
        self.assertEqual((home / "calc.py").read_text(), before)

    def test_a_brain_with_a_link_leading_out_refuses_every_change(self):
        r, home, wt = _test_verify.WorkerReadyPackets.job(self)
        (r / "specialists").mkdir(exist_ok=True)
        cases = [(r / "specialists", ["specialist", "new", "writer", "--at", "root", "--title", "Writer"]),
                 (r / "nodes/a/library", ["item", "add", "x1", "--from", "a", "--title", "X", "--kind", "fact", "--ref", "y"]),
                 (r / ".brain/local.json", ["set", "a", "coordinator=private-thread"])]
        for link, args in cases:
            moved = self.tmp / ("was-" + link.name)
            if link.exists():
                os.rename(link, moved)
            target = home / link.name
            if link.suffix:
                target.write_text("{}\n") if not moved.exists() else shutil.copy(moved, target)
            else:
                target.mkdir(exist_ok=True)
            before = sorted(p.name for p in home.rglob("*") if ".git" not in p.parts)
            text = target.read_text() if target.is_file() else None
            link.symlink_to(target)
            out = self.b(r, *args, ok=False)
            self.assertIn("lead out of the brain", out, args)
            self.assertEqual(sorted(p.name for p in home.rglob("*") if ".git" not in p.parts), before, args)
            if text is not None:
                self.assertEqual(target.read_text(), text)
            self.assertIn("a link leading out of the brain", self.b(r, "check", ok=False))
            link.unlink()
            if moved.exists():
                os.rename(moved, link)
        self.b(r, "check")
        self.b(r, "specialist", "new", "writer", "--at", "root", "--title", "Writer")  # links into the brain are fine

    def test_links_inside_the_brain_are_followed_to_what_lies_beneath(self):
        r, home, wt = _test_verify.WorkerReadyPackets.job(self)
        private = home / "private.json"
        private.write_text("{}\n")
        os.rename(r / ".brain", r / "state")  # .brain is a link to a folder inside the brain...
        (r / ".brain").symlink_to(r / "state")
        os.rename(r / "state/local.json", self.tmp / "local.json")
        (r / "state/local.json").symlink_to(private)  # ...that holds a link out
        self.assertIn("lead out of the brain", self.b(r, "set", "root", "coordinator=review-test", ok=False))
        self.assertEqual(private.read_text(), "{}\n")
        (r / "state/local.json").unlink()
        os.rename(self.tmp / "local.json", r / "state/local.json")
        self.b(r, "set", "root", "coordinator=fine")  # links inside the brain alone are fine
        os.rename(r / "nodes", r / "pool")
        (r / "nodes").symlink_to(r / "pool")
        (r / "pool/a/nodes").symlink_to(home)
        before = sorted(p.name for p in home.iterdir())
        self.assertIn("lead out of the brain", self.b(r, "new", "a/b", "--title", "B", ok=False))
        self.assertEqual(sorted(p.name for p in home.iterdir()), before)
        (r / "pool/a/nodes").unlink()
        (r / "pool/a/nodes").symlink_to(r / "pool/a")  # a loop of links inside the brain ends
        from core_loader import SourceFileLoader
        m = SourceFileLoader("brain_loops", str(CORE / "bin/brain")).load_module()
        self.assertEqual(m.outward_links(r), [])

    def test_git_metadata_reports_merges_hard_links_and_the_hook_stay_inside(self):
        r = self.init("alice")
        self.b(r, "new", "a", "--title", "A")
        self.b(r, "new", "b", "--title", "B")
        outside = self.tmp / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel.txt"
        sentinel.write_text("keep\n")

        def refused(args, link, target):
            link.unlink(missing_ok=True)
            link.symlink_to(target)
            self.assertIn("lead out of the brain", self.b(r, *args, ok=False), args)
            self.assertEqual(sentinel.read_text(), "keep\n")
            link.unlink()
        refused(["new", "c", "--title", "C"], r / ".git/COMMIT_EDITMSG", sentinel)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        (r / f"reports/{stamp}-audit").mkdir(parents=True)
        refused(["report", "root", "audit"], r / f"reports/{stamp}-audit/index.md", outside / "missing-report")
        self.assertFalse((outside / "missing-report").exists())
        (r / "nodes/a/custom.txt").write_text("mine\n")
        (r / "nodes/b/sources/merged-a").mkdir(parents=True)
        refused(["merge", "a", "b"], r / "nodes/b/sources/merged-a/custom.txt", outside)
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ["sentinel.txt"])
        shutil.rmtree(r / "nodes/b/sources/merged-a")
        node = r / "nodes/a/node.md"
        os.link(node, outside / "node-copy.md")
        before = (outside / "node-copy.md").read_text()
        self.assertIn("out of the brain", self.b(r, "relabel", "a", "area:x", ok=False))
        self.assertEqual((outside / "node-copy.md").read_text(), before)
        (outside / "node-copy.md").unlink()
        log = r / ".brain/hooks.log"
        log.unlink(missing_ok=True)
        log.symlink_to(sentinel)
        status = r / "nodes/a/status.md"
        status.write_text(status.read_text().replace("Updated:", "Updated: 2020-01-01 00:00 by x\nOld:", 1))
        out = _test_hosts.StopHook.hook(self, r, r / "nodes/a", session="s-contained")
        self.assertEqual(out["decision"], "block")  # the reminder still goes out...
        self.assertEqual(sentinel.read_text(), "keep\n")  # ...but nothing is written through the link

    def test_hard_links_in_the_lock_git_and_hook_files_and_links_where_init_writes(self):
        r = self.init("alice")
        sentinel = self.tmp / "sentinel.txt"
        sentinel.write_text("keep\n")
        for f in (r / ".git/COMMIT_EDITMSG", r / ".brain/lock"):
            f.parent.mkdir(exist_ok=True)
            f.unlink(missing_ok=True)
            os.link(sentinel, f)
            self.assertIn("out of the brain", self.b(r, "new", "a", "--title", "A", ok=False))
            self.assertEqual(sentinel.read_text(), "keep\n")
            f.unlink()
        alias = r / "scratch/shared"  # an inside file, hard-linked outside, reached through inside links
        alias.parent.mkdir()
        os.link(sentinel, alias)
        for link, args in ((r / "registry.md", ["registry"]), (r / ".git/COMMIT_EDITMSG", ["new", "a", "--title", "A"])):
            link.unlink(missing_ok=True)
            link.symlink_to(alias)
            self.assertIn("out of the brain", self.b(r, *args, ok=False))
            self.assertEqual(sentinel.read_text(), "keep\n")
            link.unlink()
        shutil.rmtree(alias.parent)
        self.b(r, "registry")
        lock = r / ".brain/lock"
        lock.unlink(missing_ok=True)
        os.link(sentinel, lock)  # a lock taken by a command that runs no scan is refused at the lock itself
        from core_loader import SourceFileLoader
        m = SourceFileLoader("brain_lock_link", str(CORE / "bin/brain")).load_module()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            with m.brain_lock(r):
                pass
        self.assertEqual(sentinel.read_text(), "keep\n")
        lock.unlink()
        self.b(r, "new", "a", "--title", "A")
        log = r / ".brain/hooks.log"
        log.unlink(missing_ok=True)
        os.link(sentinel, log)
        status = r / "nodes/a/status.md"
        status.write_text(status.read_text().replace("Updated:", "Updated: 2020-01-01 00:00 by x\nOld:", 1))
        self.assertEqual(_test_hosts.StopHook.hook(self, r, r / "nodes/a", session="s-hard")["decision"], "block")
        self.assertEqual(sentinel.read_text(), "keep\n")
        fresh = self.tmp / "fresh"
        fresh.mkdir()
        (fresh / "AGENTS.md").symlink_to(sentinel)
        out = self.run_(["init", str(fresh), "--operator", "bob", "--title", "B"], cwd=self.tmp, brain=CORE / "bin/brain",
                        ok=False)
        self.assertIn("use an empty folder", out)
        self.assertEqual(sentinel.read_text(), "keep\n")

    def test_merge_commits_when_a_node_has_empty_folders(self):
        r = self.init("alice")
        for nid in ("a", "d"):
            self.b(r, "new", nid, "--title", nid.upper())
        (r / "nodes/a/job-handbacks").mkdir()
        (r / "nodes/a/scratch/deeper").mkdir(parents=True)
        self.b(r, "merge", "a", "d")
        self.assertEqual(self.git("status", "--porcelain", cwd=r), "")
        self.assertIn("Merge a into d", self.git("log", "-1", "--format=%s", cwd=r))
        self.assertIn("✓", self.b(r, "check"))



if __name__ == "__main__":
    unittest.main()


class ForbiddenActions(Scenario):
    def setUp(self):
        super().setUp()
        self.m = core_loader.SourceFileLoader('action_guard_fixture', str(CORE / 'bin/brain')).load_module()

    def test_forbidden_command_blocks_and_exact_allowance_is_consumed_once(self):
        r = self.init('alice')
        self.b(r, 'new', 'app', '--title', 'App')
        node = r / 'nodes/app'
        (node / 'AGENTS.md').write_text('## Forbidden actions\n- git push*\n- rm -rf*\n')
        inst = self.m.Instance(r)
        n = inst.node('app')
        payload = {'cwd': str(node), 'tool_name': 'Bash', 'tool_input': {'command': 'git push origin main'}}
        self.assertIn('forbidden', self.m.pretool_decision(inst, n, payload).lower())
        self.assertIsNone(self.m.pretool_decision(inst, n, dict(payload, tool_input={'command': 'git status'})))
        self.b(r, 'allow', 'app', 'git push origin main', '--once', '--cwd', str(node))
        self.assertIsNotNone(self.m.pretool_decision(inst, n, dict(payload, tool_input={'command': 'git push origin other'})))
        self.assertIsNone(self.m.pretool_decision(inst, n, payload))
        self.assertIsNotNone(self.m.pretool_decision(inst, n, payload))
        state = json.loads(self.m.allowance_path(inst, n).read_text())
        self.assertEqual([e['event'] for e in state['events']], ['issued', 'consumed'])
        self.assertNotIn('git push', json.dumps(state))

    def test_boundary_inheritance_cwd_and_compound_commands_are_scoped(self):
        r = self.init('alice')
        self.b(r, 'principal', 'add', 'team')
        self.b(r, 'new', 'app', '--title', 'App')
        self.b(r, 'new', 'private', '--title', 'Private', '--owners', 'team')
        (r / 'AGENTS.md').write_text('## Forbidden actions\n- git push*\n')
        inst = self.m.Instance(r)
        app = inst.node('app')
        data = {'cwd': str(app['dir']), 'tool_name': 'Bash', 'tool_input': {'command': 'echo ready && git push origin main'}}
        self.assertIsNotNone(self.m.pretool_decision(inst, app, data))
        self.assertIsNone(self.m.pretool_decision(inst, inst.node('private'), data))
        self.b(r, 'allow', 'app', data['tool_input']['command'], '--once', '--cwd', str(app['dir']))
        different = dict(data, cwd=str(r))
        self.assertIsNotNone(self.m.pretool_decision(inst, app, different))
        self.assertIsNone(self.m.pretool_decision(inst, app, data))

    def test_pretool_output_denies_but_malformed_hook_fails_open(self):
        r = self.init('alice')
        (r / 'AGENTS.md').write_text('## Forbidden actions\n- git push*\n')
        p = subprocess.run([sys.executable, str(CORE / 'bin/brain'), 'hook', 'pretool'], cwd=r, env=self.env,
                           input=json.dumps({'cwd': str(r), 'tool_name': 'Bash', 'tool_input': {'command': 'git push'}}),
                           text=True, capture_output=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)['hookSpecificOutput']['permissionDecision'], 'deny')
        p = subprocess.run([sys.executable, str(CORE / 'bin/brain'), 'hook', 'pretool'], cwd=r, env=self.env,
                           input='{bad', text=True, capture_output=True)
        self.assertEqual((p.returncode, p.stdout), (0, ''))

    def test_policy_changes_invalidate_allowance_and_concurrent_hooks_consume_once(self):
        r = self.init('alice')
        rules = r / 'AGENTS.md'
        rules.write_text('## Forbidden actions\n- git push*\n')
        self.b(r, 'allow', 'root', 'git push', '--once', '--cwd', str(r))
        data = json.dumps({'cwd': str(r), 'tool_name': 'Bash', 'tool_input': {'command': 'git push'}})
        processes = [subprocess.Popen([sys.executable, str(CORE / 'bin/brain'), 'hook', 'pretool'],
                     cwd=r, env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                     text=True) for _ in range(2)]
        outputs = [p.communicate(data, timeout=10) for p in processes]
        self.assertEqual(sum(not stdout for stdout, stderr in outputs), 1)
        self.assertTrue(all(p.returncode == 0 for p in processes))
        self.b(r, 'allow', 'root', 'git push', '--once', '--cwd', str(r))
        rules.write_text(rules.read_text() + '- rm -rf*\n')
        m = self.m
        inst = m.Instance(r)
        self.assertIsNotNone(m.pretool_decision(inst, inst.node('root'), json.loads(data)))


class PreviewSampleReceipts(Scenario):
    def context(self):
        r = self.init('alice')
        home = self.repo(self.tmp / 'web')
        self.b(r, 'new', 'web', '--title', 'Web', '--home', str(home))
        self.b(r, 'wt', 'add', 'web', 'job')
        self.b(r, 'set', 'web', 'preview=https://preview.example/app')
        m = core_loader.SourceFileLoader('preview_sample_fixture', str(CORE / 'bin/brain')).load_module()
        inst = m.Instance(r)
        n = inst.node('web')
        row = m.read_jobs(n)[-1]
        head = self.git('rev-parse', 'job', cwd=home).strip()
        data = {'version': 1, 'node': 'web', 'job_key': m.job_key(row), 'branch': 'job', 'head': head,
                'checked_at': inst.now_dt().isoformat(), 'renderer_threadId': 'browser-thread',
                'urls': [{'url': n['local']['preview'], 'rendered': True, 'render_evidence': 'screenshot.png',
                          'items': [{'page_item': 'person-' + str(i), 'source_ref': 'source.json#' + str(i),
                                     'source_item': 'person-' + str(i)} for i in range(3)]}]}
        return m, inst, n, data

    def test_missing_samples_mismatch_wrong_url_and_stale_head_fail_closed(self):
        m, inst, n, data = self.context()
        self.assertTrue(m.validate_ready_preview(inst, n, 'job', data['head']))
        for mutate in ('few', 'duplicate', 'mismatch', 'url', 'head', 'render'):
            bad = json.loads(json.dumps(data))
            route = bad['urls'][0]
            if mutate == 'few': route['items'].pop()
            if mutate == 'duplicate': route['items'][1] = route['items'][0]
            if mutate == 'mismatch': route['items'][1]['source_item'] = 'wrong'
            if mutate == 'url': route['url'] += '/wrong'
            if mutate == 'head': bad['head'] = '0' * 40
            if mutate == 'render': route['rendered'] = False
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                m.record_preview_receipt(inst, n, 'job', bad)
        m.record_preview_receipt(inst, n, 'job', data)
        self.assertEqual(m.validate_ready_preview(inst, n, 'job', data['head']), [])
        n['local']['preview'] += '/new'
        self.assertTrue(m.validate_ready_preview(inst, n, 'job', data['head']))


class PreviewDiffScoping(Scenario):
    fixture_templates = True
    fixture_init_in_process = True

    def context(self, declaration='ui_globs=ui/**'):
        m, inst, n, data = PreviewSampleReceipts.context(self)
        self.b(inst.root, 'set', 'web', 'verify=true', declaration)
        inst = m.Instance(inst.root)
        return m, inst, inst.node('web'), data

    def declaration(self, m, inst, n, value):
        path = n['dir'] / 'node.md'
        fm, body = m.read_fm(path)
        for key in ('ui_globs', 'verify_lanes', 'verify_lanes_from'):
            fm.pop(key, None)
        key, raw = value.split('=', 1)
        fm[key] = raw
        m.write_fm(path, fm, body)
        inst = m.Instance(inst.root)
        return inst, inst.node('web')

    def change(self, m, inst, n, data, paths):
        wt = Path(n['local']['worktrees']['job']['path'])
        for name, content in paths.items():
            path = wt / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.git('add', '-A', cwd=wt)
        self.git('commit', '-qm', 'Change declared inputs', cwd=wt)
        data['head'] = self.git('rev-parse', 'HEAD', cwd=wt).strip()
        return m.preview_requirement(inst, n, 'job', data['head'])

    def test_backend_diff_skips_preview_for_globs_inline_and_generated_maps(self):
        m, inst, n, data = self.context()
        self.change(m, inst, n, data, {'server/api.py': 'backend\n'})
        declarations = ['ui_globs=ui/**,templates/**', 'ui_globs=["ui/**"]',
                        'verify_lanes=ui: true | ui/**; server: true | server/**',
                        'verify_lanes_from=printf \'[{"name":"ui","command":"true","inputs":["ui/**"]}]\'']
        for declaration in declarations:
            with self.subTest(declaration=declaration):
                inst, n = self.declaration(m, inst, n, declaration)
                scope = m.preview_requirement(inst, n, 'job', data['head'])
                self.assertEqual(scope['status'], 'not_applicable', scope)
                self.assertEqual(scope['paths'], ['server/api.py'])
                self.assertEqual(scope['reason'], 'preview: not applicable, no rendered paths touched')
                self.assertEqual(m.validate_ready_preview(inst, n, 'job', data['head']), [])

    def test_rendered_add_modify_delete_and_boundary_rename_require_receipt(self):
        m, inst, n, data = self.context()
        for content in ('first\n', 'second\n'):
            scope = self.change(m, inst, n, data, {'ui/page.html': content})
            self.assertEqual(scope['status'], 'required')
            self.assertTrue(m.validate_ready_preview(inst, n, 'job', data['head']))
        wt = Path(n['local']['worktrees']['job']['path'])
        # Put the rendered file in the integration base, then rename it out.
        self.git('merge', '--ff-only', 'job', cwd=n['home_path'])
        self.git('mv', 'ui/page.html', 'backend.html', cwd=wt)
        self.git('commit', '-qm', 'Move rendered input out', cwd=wt)
        data['head'] = self.git('rev-parse', 'HEAD', cwd=wt).strip()
        scope = m.preview_requirement(inst, n, 'job', data['head'])
        self.assertEqual(scope['status'], 'required')
        self.assertEqual(scope['paths'], ['backend.html', 'ui/page.html'])
        self.git('reset', '--hard', 'main', cwd=wt)
        self.git('rm', 'ui/page.html', cwd=wt)
        self.git('commit', '-qm', 'Delete rendered input', cwd=wt)
        data['head'] = self.git('rev-parse', 'HEAD', cwd=wt).strip()
        self.assertEqual(m.preview_requirement(inst, n, 'job', data['head'])['status'], 'required')

    def test_rename_into_ui_requires_receipt_and_backend_rename_is_classified(self):
        m, inst, n, data = self.context()
        self.change(m, inst, n, data, {'source.html': 'same\n'})
        self.git('merge', '--ff-only', 'job', cwd=n['home_path'])
        wt = Path(n['local']['worktrees']['job']['path'])
        self.git('mv', 'source.html', 'destination.html', cwd=wt)
        self.git('commit', '-qm', 'Rename backend input', cwd=wt)
        data['head'] = self.git('rev-parse', 'HEAD', cwd=wt).strip()
        self.assertEqual(m.preview_requirement(inst, n, 'job', data['head'])['status'], 'not_applicable')
        (wt / 'ui').mkdir()
        self.git('mv', 'destination.html', 'ui/page.html', cwd=wt)
        self.git('commit', '-qm', 'Move into rendered paths', cwd=wt)
        data['head'] = self.git('rev-parse', 'HEAD', cwd=wt).strip()
        self.assertEqual(m.preview_requirement(inst, n, 'job', data['head'])['status'], 'required')

    def test_missing_or_unknown_declarations_fail_closed(self):
        m, inst, n, data = self.context()
        self.change(m, inst, n, data, {'server.py': 'backend\n'})
        declarations = ['verify=true', 'ui_globs=', 'ui_globs=none', 'ui_globs=[]',
                        'ui_globs=["!ui/**"]', 'ui_globs=ui/[ab].html',
                        'ui_globs=[true]', 'ui_globs=[invalid',
                        'verify_lanes=server: true | server/**',
                        'verify_lanes_from=false']
        for declaration in declarations:
            with self.subTest(declaration=declaration):
                inst, n = self.declaration(m, inst, n, declaration)
                self.assertEqual(m.preview_requirement(inst, n, 'job', data['head'])['status'], 'required')
                self.assertTrue(m.validate_ready_preview(inst, n, 'job', data['head']))

    def test_unreadable_or_malformed_diff_and_unavailable_base_fail_closed(self):
        m, inst, n, data = self.context()
        self.change(m, inst, n, data, {'server.py': 'backend\n'})
        original = m.git
        def broken(*args, **kwargs):
            if args[0] == 'diff':
                return subprocess.CompletedProcess(args, 0, 'M\0unterminated', '')
            return original(*args, **kwargs)
        with mock.patch.object(m, 'git', side_effect=broken):
            self.assertTrue(m.validate_ready_preview(inst, n, 'job', data['head']))
        def unreadable(*args, **kwargs):
            if args[0] == 'diff':
                return subprocess.CompletedProcess(args, 1, '', 'unreadable')
            return original(*args, **kwargs)
        with mock.patch.object(m, 'git', side_effect=unreadable):
            self.assertTrue(m.validate_ready_preview(inst, n, 'job', data['head']))
        self.assertTrue(m.validate_ready_preview(inst, n, 'job', data['head'], {'integration_base': '0' * 40}))
        # A fabricated narrower base cannot exempt rendered changes.
        self.change(m, inst, n, data, {'ui/page.html': 'rendered\n'})
        self.assertTrue(m.validate_ready_preview(inst, n, 'job', data['head'], {'integration_base': data['head']}))

    def test_existing_receipt_stays_validated_for_backend_diff(self):
        m, inst, n, data = self.context()
        self.change(m, inst, n, data, {'server.py': 'backend\n'})
        path = m.record_preview_receipt(inst, n, 'job', data)
        self.assertEqual(m.validate_ready_preview(inst, n, 'job', data['head']), [])
        data['urls'][0]['items'].pop()
        path.write_text(json.dumps(data))
        self.assertTrue(m.validate_ready_preview(inst, n, 'job', data['head']))

    def test_full_gate_records_paths_and_readers_recheck_policy_without_cached_git(self):
        m, inst, n, data = self.context()
        scope = self.change(m, inst, n, data, {'server/api.py': 'backend\n', 'strange\nname.py': 'backend\n'})
        with mock.patch.dict(os.environ, self.env, clear=True):
            packet, reasons = m.record_ready(inst, n, 'job', 1, full=True)
        self.assertEqual(reasons, [])
        self.assertEqual(packet['verify_result'], 0)
        self.assertEqual(packet['verify_scope'], 'full')
        self.assertEqual(packet['preview_scope'], scope)
        with m.git_read_snapshot():
            self.assertEqual(m.ready_policy_reasons(inst, n, 'job', data['head'], packet), [])
            self.b(inst.root, 'set', 'web', 'ui_globs=server/**')
            self.assertTrue(m.ready_policy_reasons(inst, n, 'job', data['head'], packet))

    def test_rendered_lane_dependencies_and_strict_inputs_are_included(self):
        m, inst, n, data = self.context()
        self.change(m, inst, n, data, {'shared/view.json': '{}\n'})
        for key in ('requires', 'strict_inputs'):
            lanes = [dict(name='ui', command='true', inputs=['ui/**']),
                     dict(name='server', command='true', inputs=['shared/**'])]
            lanes[0][key] = ['server'] if key == 'requires' else ['shared/**']
            inst, n = self.declaration(m, inst, n, 'verify_lanes=' + json.dumps(lanes))
            scope = m.preview_requirement(inst, n, 'job', data['head'])
            self.assertEqual(scope['status'], 'required')

    def test_generated_map_uses_full_gate_identity_and_retains_environment_for_readers(self):
        script = ('import json, os\n'
                  'active = all(os.environ.get(k) for k in '
                  '["BRAIN_ROOT", "BRAIN_VERIFY_BASE", "BRAIN_VERIFY_HEAD"]) '
                  'and os.environ.get("BRAIN_VERIFY_SCOPE") == "full" '
                  'and os.environ.get("NODE_OPTIONS") == "gate-fixture"\n'
                  'print(json.dumps([dict(name="ui", command="true", '
                  'inputs=["ui/**" if active else "server/**"])]))')
        declaration = 'verify_lanes_from=python3 -c ' + shlex.quote(script.replace('\n', '; '))
        m, inst, n, data = self.context(declaration)
        self.change(m, inst, n, data, {'server/api.py': 'backend\n'})
        with mock.patch.dict(os.environ, {**self.env, 'NODE_OPTIONS': 'gate-fixture'}, clear=True):
            packet, reasons = m.record_ready(inst, n, 'job', 1, full=True)
        self.assertEqual(reasons, [])
        self.assertEqual(packet['preview_scope']['status'], 'not_applicable')
        with mock.patch.dict(os.environ, {**self.env, 'NODE_OPTIONS': 'other'}, clear=True):
            self.assertEqual(m.preview_requirement(inst, n, 'job', data['head'], packet), packet['preview_scope'])
        self.change(m, inst, n, data, {'ui/page.html': 'rendered\n'})
        with mock.patch.dict(os.environ, {**self.env, 'NODE_OPTIONS': 'gate-fixture'}, clear=True):
            packet, reasons = m.record_ready(inst, n, 'job', 1, full=True)
        self.assertIsNone(packet)
        self.assertIn('missing or invalid exact-head preview rendering and three-source-sample receipt', reasons)

    def test_policy_mutation_between_loading_and_fencing_refuses_exemption(self):
        m, inst, n, data = self.context()
        self.change(m, inst, n, data, {'server/api.py': 'backend\n'})
        original = m.Instance.nodes
        pending = [True]
        def mutate_after_load(current):
            nodes = original(current)
            if pending[0]:
                pending[0] = False
                self.declaration(m, inst, n, 'ui_globs=server/**')
            return nodes
        with mock.patch.object(m.Instance, 'nodes', mutate_after_load):
            self.assertTrue(m.validate_ready_preview(inst, n, 'job', data['head']))

    def test_full_receipt_adoption_uses_original_map_environment_before_preview_check(self):
        script = ('import json, os; print(json.dumps([dict(name="ui", command="true", '
                  'inputs=["ui/**" if os.environ.get("NODE_OPTIONS") == "gate-fixture" else "server/**"])]))')
        m, inst, n, data = self.context('verify_lanes_from=python3 -c ' + shlex.quote(script))
        self.change(m, inst, n, data, {'server/api.py': 'backend\n'})
        with mock.patch.dict(os.environ, {**self.env, 'NODE_OPTIONS': 'gate-fixture'}, clear=True), \
                mock.patch.object(m, 'load', return_value=inst), contextlib.redirect_stdout(io.StringIO()):
            m.cmd_full_run(argparse.Namespace(id='web', branch='job', reviews=1))
        receipts = list((n['dir'] / 'full-gates').glob('*.json'))
        self.assertEqual(len(receipts), 1)
        original = json.loads(receipts[0].read_text())['packet']
        with mock.patch.dict(os.environ, {**self.env, 'NODE_OPTIONS': 'other'}, clear=True), \
                mock.patch.object(m, 'run_verify', side_effect=AssertionError('adoption must reuse')):
            adopted, reasons = m.record_ready(inst, n, 'job', 1, full_receipt=receipts[0].stem)
        self.assertEqual(reasons, [])
        self.assertTrue(adopted['verify_reused'])
        self.assertEqual(adopted['preview_scope'], original['preview_scope'])
        self.assertEqual(adopted['verify_lane_env'], original['verify_lane_env'])


class GuardAllowanceRenewal(Scenario):
    def test_changed_policy_can_receive_a_new_exact_allowance(self):
        r = self.init('alice')
        rules = r / 'AGENTS.md'
        rules.write_text('## Forbidden actions\n- git push*\n')
        self.b(r, 'allow', 'root', 'git push', '--once', '--cwd', str(r))
        rules.write_text(rules.read_text() + '- rm -rf*\n')
        self.b(r, 'allow', 'root', 'git push', '--once', '--cwd', str(r))
        m = core_loader.SourceFileLoader('allowance_renewal_fixture', str(CORE / 'bin/brain')).load_module()
        inst = m.Instance(r)
        self.assertIsNone(m.pretool_decision(inst, inst.node('root'),
            {'cwd': str(r), 'tool_name': 'Bash', 'tool_input': {'command': 'git push'}}))
        state = json.loads(m.allowance_path(inst, inst.node('root')).read_text())
        self.assertEqual([e['event'] for e in state['events']], ['issued', 'invalidated', 'issued', 'consumed'])


class PreviewUrlConfiguration(Scenario):
    context = PreviewSampleReceipts.context

    def test_presented_review_urls_are_machine_local_and_all_need_source_samples(self):
        m, inst, n, data = self.context()
        extra = 'https://preview.example/review'
        self.b(inst.root, 'set', 'web', 'review_urls=' + json.dumps([extra]))
        inst = m.Instance(inst.root)
        n = inst.node('web')
        self.assertNotIn('review_urls', n['fm'])
        self.assertIn(extra, m.preview_urls(n))
        with self.assertRaises(ValueError):
            m.record_preview_receipt(inst, n, 'job', data)
        data['urls'].append(dict(data['urls'][0], url=extra))
        m.record_preview_receipt(inst, n, 'job', data)
        self.assertEqual(m.validate_ready_preview(inst, n, 'job', data['head']), [])


class JobPreviewReceipts(Scenario):
    def context(self):
        m, inst, n, data = PreviewSampleReceipts.context(self)
        row = m.open_job(m.read_jobs(n), 'job')
        data.update(scope='job', worktree_generation=m.job_preview_generation(inst, n, row, data['head']))
        data['urls'][0]['url'] = 'http://127.0.0.1:' + str(n['local']['worktrees']['job']['port']) + '/review'
        return m, inst, n, row, data

    def skeleton_cli(self, root, node='web', branch='job'):
        return subprocess.run([sys.executable, '-B', str(CORE / 'bin/brain'),
                               'preview-receipt-skeleton', node, '--branch', branch, '--json'],
                              cwd=root, env={**self.env, 'BRAIN_LOCK_WAIT': '0'},
                              capture_output=True, text=True, timeout=10)

    def tree_snapshot(self):
        # Include the instance, external home/worktree and first-use user config;
        # ignore access times, which any legitimate read may change.
        result = {}
        for path in [self.tmp, *self.tmp.rglob('*')]:
            stat = path.lstat()
            content = (os.readlink(path) if path.is_symlink() else
                       path.read_bytes() if path.is_file() else None)
            result[str(path.relative_to(self.tmp))] = (stat.st_mode, stat.st_ino,
                                                      stat.st_mtime_ns, stat.st_ctime_ns, content)
        return result

    def test_skeleton_cli_round_trips_only_after_real_observation_fields_are_supplied(self):
        m, inst, n, row, observation = self.context()
        help_result = self.b(inst.root, 'preview-receipt-skeleton', '--help')
        self.assertIn('--branch', help_result)
        self.assertIn('--json', help_result)
        self.assertIn('writes nothing', help_result)
        result = self.skeleton_cli(inst.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, '')
        data = json.loads(result.stdout)
        for key in ('version', 'scope', 'node', 'job_key', 'branch', 'head', 'worktree_generation'):
            self.assertEqual(data[key], observation[key])
        wt = n['local']['worktrees']['job']
        self.assertEqual(data['binding'], {'home': str(n['home_path'].resolve()),
                         'worktree': str(Path(wt['path']).resolve()), 'start': wt['start'],
                         'created': inst.parse_time(wt['created']).isoformat(),
                         'port': wt['port'], 'ports': wt.get('ports', 1), 'urls': []})
        self.assertEqual(data['urls'][0]['url'], 'http://127.0.0.1:' + str(wt['port']) + '/')
        self.assertIsNone(data['checked_at'])
        self.assertIsNone(data['renderer_threadId'])
        self.assertEqual(data['urls'][0], {'url': data['urls'][0]['url'], 'rendered': None,
                         'render_evidence': None, 'items': [dict.fromkeys(
                             ('page_item', 'source_ref', 'source_item')) for _ in range(3)]})
        with self.assertRaises(ValueError):
            m.validate_preview_receipt(inst, n, row, data['head'], data)
        data['checked_at'] = observation['checked_at']
        data['renderer_threadId'] = observation['renderer_threadId']
        data['urls'][0].update({k: observation['urls'][0][k]
                                for k in ('rendered', 'render_evidence', 'items')})
        self.assertIs(m.validate_preview_receipt(inst, n, row, data['head'], data), data)
        receipt = self.tmp / 'filled-preview.json'
        receipt.write_text(json.dumps(data))
        self.b(inst.root, 'preview-receipt', 'web', '--branch', 'job', '--file', str(receipt))
        self.assertEqual(json.loads(m.preview_receipt_path(inst, n, row, data['head']).read_text()), data)
        self.assertEqual(m.validate_ready_preview(inst, n, 'job', data['head']), [])

    def test_skeleton_is_read_only_on_first_use_and_ignores_a_held_writer_lock(self):
        import fcntl
        m, inst, n, row, data = self.context()
        # A new/unregistered invocation must not even create a user registry.
        with mock.patch.dict(os.environ, self.env, clear=True):
            registry = m.brains_file()
        if registry.exists():
            registry.unlink()
        lock = inst.root / '.brain/lock'
        with lock.open('a+') as holder:
            fcntl.flock(holder, fcntl.LOCK_EX)
            before = self.tree_snapshot()
            result = self.skeleton_cli(inst.root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.tree_snapshot(), before)
        self.assertFalse(registry.exists())

    def test_skeleton_refuses_missing_or_stale_authority_without_writing(self):
        m, inst, n, row, data = self.context()
        local_path = inst.local_path
        original = json.loads(local_path.read_text())
        worktree = original['nodes']['web']['worktrees']['job']
        wt = Path(worktree['path'])

        def refuses(reason, node='web', branch='job'):
            before = self.tree_snapshot()
            result = self.skeleton_cli(inst.root, node, branch)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, '')
            self.assertIn(reason, result.stderr)
            self.assertNotIn('Traceback', result.stderr)
            self.assertEqual(self.tree_snapshot(), before)

        refuses('no node', node='missing')
        refuses('open job', branch='missing')
        rows = m.read_jobs(n)
        m.write_jobs(inst, n, [{**row, 'Ended': inst.now()}])
        refuses('open job')
        m.write_jobs(inst, n, rows)
        for key, value in (('home', None), ('home', str(self.tmp / 'missing-home')),
                           ('worktrees', {})):
            changed = json.loads(json.dumps(original))
            changed['nodes']['web'][key] = value
            local_path.write_text(json.dumps(changed))
            with self.subTest(binding=key, value=value):
                refuses('bound external home' if key == 'home' else 'bound worktree')
        for key, value in (('path', str(self.tmp / 'missing-worktree')),
                           ('path', str(n['home_path'])), ('created', None),
                           ('created', '2999-01-01T00:00:00Z'), ('start', ''),
                           ('port', 0), ('ports', 0), ('port', True)):
            changed = json.loads(json.dumps(original))
            changed['nodes']['web']['worktrees']['job'][key] = value
            local_path.write_text(json.dumps(changed))
            with self.subTest(worktree=key, value=value):
                refuses('preview')
        local_path.write_text(json.dumps(original))
        # A real but unrelated checkout must not impersonate the bound Git repo.
        unrelated = self.repo(self.tmp / 'unrelated')
        self.git('checkout', '-b', 'job', cwd=unrelated)
        changed = json.loads(json.dumps(original))
        changed['nodes']['web']['worktrees']['job']['path'] = str(unrelated)
        local_path.write_text(json.dumps(changed))
        refuses('current bound worktree')
        local_path.write_text(json.dumps(original))
        for declared in ('not json', '{}', '[1]'):
            m.write_jobs(inst, n, [{**row, 'Preview URLs': declared}])
            with self.subTest(declared=declared):
                refuses('preview' if declared != 'not json' else 'Expecting value')
        m.write_jobs(inst, n, rows)
        self.git('checkout', '--detach', cwd=wt)
        refuses('current bound worktree')
        self.git('checkout', '-b', 'different', cwd=wt)
        refuses('current bound worktree')
        self.git('checkout', '--detach', cwd=wt)
        self.git('branch', '-D', 'job', cwd=n['home_path'])
        refuses('current job branch head')

    def test_skeleton_refuses_authority_drift_during_collection(self):
        m, inst, n, row, data = self.context()
        binding = m.job_preview_binding
        def rebind(*args):
            result = binding(*args)
            fresh = m.Instance(inst.root)
            fresh.local_of('web')['worktrees']['job']['port'] += 1
            fresh.save_local()
            return result
        with mock.patch.object(m, 'job_preview_binding', side_effect=rebind):
            with self.assertRaisesRegex(ValueError, 'authority changed during read'):
                m.preview_receipt_skeleton(inst, 'web', 'job')

    def test_skeleton_fences_one_shot_mutations_inside_the_final_collection(self):
        m, inst, n, row, data = self.context()
        original_local = inst.local_path.read_text()
        original_ledger = (n['dir'] / m.JOBS_FILE).read_text()
        contract = m.contract_file(n, row)
        original_contract = contract.read_bytes() if contract.exists() else None
        git = m.git
        for mutation in ('binding', 'node', 'ledger', 'contract', 'git'):
            with self.subTest(mutation=mutation):
                calls = 0
                restore = None
                def change_once(*args, **kwargs):
                    nonlocal calls, restore
                    result = git(*args, **kwargs)
                    if args == ('rev-parse', '--git-dir'):
                        calls += 1
                        if calls == 2:
                            if mutation == 'binding':
                                local = json.loads(original_local)
                                local['nodes']['web']['worktrees']['job']['port'] += 1
                                inst.local_path.write_text(json.dumps(local))
                            elif mutation == 'node':
                                path = n['dir'] / 'node.md'
                                restore = (path, path.read_bytes())
                                path.write_text(path.read_text() + '\nchanged during collection\n')
                            elif mutation == 'ledger':
                                (n['dir'] / m.JOBS_FILE).write_text(original_ledger + '\nchanged\n')
                            elif mutation == 'contract':
                                contract.parent.mkdir(parents=True, exist_ok=True)
                                contract.write_text('## Preview URLs\nhttps://changed.example/\n')
                            else:
                                path = Path(git('rev-parse', '--absolute-git-dir',
                                                cwd=n['home_path']).stdout.strip()) / 'config'
                                restore = (path, path.read_bytes())
                                path.write_text(path.read_text() + '\n# changed during collection\n')
                    return result
                try:
                    with mock.patch.object(m, 'git', side_effect=change_once):
                        with self.assertRaisesRegex(ValueError, 'authority changed during read'):
                            m.preview_receipt_skeleton(inst, 'web', 'job')
                    self.assertEqual(calls, 2)
                finally:
                    inst.local_path.write_text(original_local)
                    (n['dir'] / m.JOBS_FILE).write_text(original_ledger)
                    if original_contract is None:
                        contract.unlink(missing_ok=True)
                    else:
                        contract.write_bytes(original_contract)
                    if restore:
                        restore[0].write_bytes(restore[1])

    def test_skeleton_fences_logical_binding_symlink_retargets_in_final_collection(self):
        m, inst, n, row, data = self.context()
        original = json.loads(inst.local_path.read_text())
        wt = Path(n['local']['worktrees']['job']['path'])
        alternate = self.tmp / 'alternate'
        alternate.mkdir()
        (alternate / wt.name).mkdir()
        link = self.tmp / 'binding-link'
        git = m.git
        for target in ('home', 'worktree', 'worktree-parent'):
            with self.subTest(target=target):
                local = json.loads(json.dumps(original))
                link.symlink_to(n['home_path'] if target == 'home' else
                                wt if target == 'worktree' else wt.parent,
                                target_is_directory=True)
                if target == 'home':
                    local['nodes']['web']['home'] = str(link)
                else:
                    local['nodes']['web']['worktrees']['job']['path'] = str(
                        link if target == 'worktree' else link / wt.name)
                inst.local_path.write_text(json.dumps(local))
                calls = 0
                def retarget_once(*args, **kwargs):
                    nonlocal calls
                    result = git(*args, **kwargs)
                    if args == ('rev-parse', '--git-dir'):
                        calls += 1
                        if calls == 2:
                            link.unlink()
                            link.symlink_to(alternate, target_is_directory=True)
                    return result
                try:
                    # Unchanged symlinked bindings are supported.
                    self.assertEqual(m.preview_receipt_skeleton(inst, 'web', 'job')['head'], data['head'])
                    with mock.patch.object(m, 'git', side_effect=retarget_once):
                        with self.assertRaisesRegex(ValueError, 'authority changed during read'):
                            m.preview_receipt_skeleton(inst, 'web', 'job')
                    self.assertEqual(calls, 2)
                finally:
                    link.unlink()
                    inst.local_path.write_text(json.dumps(original))

    def test_skeleton_fences_the_latest_alias_when_binding_reloads_after_outer_snapshot(self):
        m, inst, n, row, data = self.context()
        original = json.loads(inst.local_path.read_text())
        wt = Path(n['local']['worktrees']['job']['path'])
        aliases = [self.tmp / name for name in ('alias-a', 'alias-b')]
        for alias in aliases:
            alias.symlink_to(wt, target_is_directory=True)
        alternate = self.tmp / 'alternate-worktree'
        alternate.mkdir()
        local = json.loads(json.dumps(original))
        local['nodes']['web']['worktrees']['job']['path'] = str(aliases[0])
        inst.local_path.write_text(json.dumps(local))
        binding, git = m.job_preview_binding, m.git
        bindings = queries = 0
        def replace_alias(*args, **kwargs):
            nonlocal bindings
            bindings += 1
            if bindings == 1:
                local['nodes']['web']['worktrees']['job']['path'] = str(aliases[1])
                inst.local_path.write_text(json.dumps(local))
            return binding(*args, **kwargs)
        def retarget_latest_alias(*args, **kwargs):
            nonlocal queries
            result = git(*args, **kwargs)
            if args == ('rev-parse', '--git-dir'):
                queries += 1
                if queries == 2:
                    aliases[1].unlink()
                    aliases[1].symlink_to(alternate, target_is_directory=True)
            return result
        with mock.patch.object(m, 'job_preview_binding', side_effect=replace_alias), \
                mock.patch.object(m, 'git', side_effect=retarget_latest_alias):
            with self.assertRaisesRegex(ValueError, 'authority changed during read'):
                m.preview_receipt_skeleton(inst, 'web', 'job')
        self.assertEqual((bindings, queries), (2, 2))

    def test_bound_port_accepts_own_job_and_exact_head_without_changing_node_urls(self):
        m, inst, n, row, data = self.context()
        before = json.dumps(n['local'], sort_keys=True)
        m.record_preview_receipt(inst, n, 'job', data)
        self.assertEqual(m.validate_ready_preview(inst, n, 'job', data['head']), [])
        self.assertEqual(m.preview_urls(n), ['https://preview.example/app'])
        self.assertEqual(json.dumps(m.Instance(inst.root).node('web')['local'], sort_keys=True), before)
        legacy = json.loads(json.dumps(data))
        legacy.pop('scope')
        legacy.pop('worktree_generation')
        with self.assertRaises(ValueError):
            m.validate_preview_receipt(inst, n, row, data['head'], legacy)
        legacy['urls'][0]['url'] = n['local']['preview']
        self.assertEqual(m.validate_preview_receipt(inst, n, row, data['head'], legacy), legacy)

    def test_other_job_and_stale_head_refuse_receipt_and_relabelled_url(self):
        m, inst, n, row, data = self.context()
        self.b(inst.root, 'wt', 'add', 'web', 'other')
        other = m.open_job(m.read_jobs(n), 'other')
        with self.assertRaises(ValueError):
            m.validate_preview_receipt(inst, n, other, data['head'], data)
        relabelled = json.loads(json.dumps(data))
        relabelled.update(branch='other', job_key=m.job_key(other),
                          worktree_generation=m.job_preview_generation(inst, n, other, data['head']))
        with self.assertRaises(ValueError):
            m.record_preview_receipt(inst, n, 'other', relabelled)
        m.record_preview_receipt(inst, n, 'job', data)
        wt = Path(n['local']['worktrees']['job']['path'])
        (wt / 'change').write_text('new code\n')
        self.git('add', 'change', cwd=wt)
        self.git('commit', '-qm', 'New code', cwd=wt)
        new_head = self.git('rev-parse', 'HEAD', cwd=wt).strip()
        with self.assertRaises(ValueError):
            m.record_preview_receipt(inst, n, 'job', data)
        with self.assertRaises(ValueError):
            m.validate_preview_receipt(inst, n, row, data['head'], data)
        self.assertTrue(m.validate_ready_preview(inst, n, 'job', new_head))

    def test_generation_changes_on_rebinding_and_same_path_recreation(self):
        m, inst, n, row, data = self.context()
        m.record_preview_receipt(inst, n, 'job', data)
        fresh = m.Instance(inst.root)
        fresh.local_of('web')['worktrees']['job']['created'] = '2026-01-01T00:00:00Z'
        fresh.save_local()
        self.assertTrue(m.validate_ready_preview(inst, n, 'job', data['head']))
        # Restore the record, but replace the physical worktree at the same path
        # and head. Cached callers and identical metadata cannot revive evidence.
        fresh.local_of('web')['worktrees']['job'] = dict(n['local']['worktrees']['job'])
        fresh.save_local()
        wt = Path(n['local']['worktrees']['job']['path'])
        retained = wt.with_name('retained-generation')
        self.git('worktree', 'move', str(wt), str(retained), cwd=n['home_path'])
        self.git('checkout', '--detach', cwd=retained)
        self.git('worktree', 'add', str(wt), 'job', cwd=n['home_path'])
        self.assertTrue(m.validate_ready_preview(inst, n, 'job', data['head']))
        self.assertNotEqual(m.job_preview_generation(inst, n, row, data['head']), data['worktree_generation'])

    def test_unbound_ports_remote_hosts_and_bad_observations_refuse(self):
        m, inst, n, row, data = self.context()
        port = n['local']['worktrees']['job']['port']
        for url in ('http://127.0.0.1:' + str(port + 1000),
                    'http://remote.example:' + str(port),
                    'http://localhost.evil:' + str(port),
                    'http://user@localhost:' + str(port)):
            bad = json.loads(json.dumps(data))
            bad['urls'][0]['url'] = url
            with self.subTest(url=url), self.assertRaises(ValueError):
                m.record_preview_receipt(inst, n, 'job', bad)
        for mutate in ('render', 'few', 'mismatch', 'generation', 'job_key'):
            bad = json.loads(json.dumps(data))
            if mutate == 'render': bad['urls'][0]['rendered'] = False
            if mutate == 'few': bad['urls'][0]['items'].pop()
            if mutate == 'mismatch': bad['urls'][0]['items'][0]['source_item'] = 'different'
            if mutate == 'generation': bad['worktree_generation'] = 'unknown'
            if mutate == 'job_key': bad['job_key'] = 'unknown'
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                m.record_preview_receipt(inst, n, 'job', bad)

    def test_ledger_and_contract_exact_url_declarations_are_job_scoped(self):
        m, inst, n, row, data = self.context()
        for source in ('ledger', 'contract'):
            url = 'http://localhost:65000/' + source
            if source == 'ledger':
                rows = m.read_jobs(n)
                rows[-1]['Preview URLs'] = json.dumps([url])
                m.write_jobs(inst, n, rows)
            else:
                path = m.contract_file(n, row)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('## Preview URLs\n- `' + url + '`\n')
            data['worktree_generation'] = m.job_preview_generation(inst, n, row, data['head'])
            data['urls'][0]['url'] = url
            m.record_preview_receipt(inst, n, 'job', data)
            self.assertEqual(m.validate_ready_preview(inst, n, 'job', data['head']), [])
            bad = json.loads(json.dumps(data))
            bad['urls'][0]['url'] += '/unbound'
            with self.assertRaises(ValueError):
                m.record_preview_receipt(inst, n, 'job', bad)
        m.contract_file(n, row).unlink()
        self.assertTrue(m.validate_ready_preview(inst, n, 'job', data['head']))

    def test_live_node_binding_removal_refuses_even_with_cached_node(self):
        m, inst, n, row, data = self.context()
        m.record_preview_receipt(inst, n, 'job', data)
        fresh = m.Instance(inst.root)
        del fresh.local_of('web')['worktrees']['job']
        fresh.save_local()
        self.assertTrue(m.validate_ready_preview(inst, n, 'job', data['head']))
        with self.assertRaises(ValueError):
            m.record_preview_receipt(inst, n, 'job', data)

    def test_pre_full_verify_policy_accepts_job_receipt_and_blocks_changed_binding(self):
        m, inst, n, row, data = self.context()
        self.b(inst.root, 'set', 'web', 'verify=true')
        inst = m.Instance(inst.root)
        n = inst.node('web')
        m.record_preview_receipt(inst, n, 'job', data)
        self.assertEqual(m.ready_policy_reasons(inst, n, 'job', data['head']), [])
        # Stop at the real execution boundary; never launch a full verify here.
        with mock.patch.object(m, 'run_verify', side_effect=RuntimeError('execution boundary')) as execute:
            with self.assertRaisesRegex(RuntimeError, 'execution boundary'):
                m.record_ready(inst, n, 'job', 1)
            execute.assert_called_once()
        fresh = m.Instance(inst.root)
        fresh.local_of('web')['worktrees']['job']['port'] += 1000
        fresh.save_local()
        with mock.patch.object(m, 'run_verify') as execute:
            packet, reasons = m.record_ready(inst, n, 'job', 1)
        execute.assert_not_called()
        self.assertIsNone(packet)
        self.assertIn('missing or invalid exact-head preview rendering and three-source-sample receipt', reasons)

    def test_optional_job_receipt_is_validated_without_node_urls(self):
        m, inst, n, row, data = self.context()
        self.b(inst.root, 'set', 'web', 'preview=')
        inst = m.Instance(inst.root)
        n = inst.node('web')
        self.assertEqual(m.validate_ready_preview(inst, n, 'job', data['head']), [])
        m.record_preview_receipt(inst, n, 'job', data)
        self.assertEqual(m.validate_ready_preview(inst, n, 'job', data['head']), [])
        fresh = m.Instance(inst.root)
        del fresh.local_of('web')['worktrees']['job']
        fresh.save_local()
        self.assertTrue(m.validate_ready_preview(inst, n, 'job', data['head']))

    def test_display_git_snapshot_cannot_hide_a_changed_checkout_head(self):
        m, inst, n, row, data = self.context()
        wt = Path(n['local']['worktrees']['job']['path'])
        m.record_preview_receipt(inst, n, 'job', data)
        with m.git_read_snapshot():
            self.assertEqual(m.validate_ready_preview(inst, n, 'job', data['head']), [])
            # Prime the exact display queries with the old identity as well.
            m.git('rev-parse', 'HEAD', cwd=wt)
            m.git('rev-parse', '--verify', 'refs/heads/job^{commit}', cwd=n['home_path'])
            (wt / 'changed').write_text('different code\n')
            self.git('add', 'changed', cwd=wt)
            self.git('commit', '-qm', 'Different code', cwd=wt)
            self.assertTrue(m.validate_ready_preview(inst, n, 'job', data['head']))

    def test_allocated_port_block_and_loopback_variants_accept_only_bound_ports(self):
        m, inst, n, row, data = self.context()
        fresh = m.Instance(inst.root)
        worktree = fresh.local_of('web')['worktrees']['job']
        worktree['ports'] = 2
        fresh.save_local()
        data['worktree_generation'] = m.job_preview_generation(inst, n, row, data['head'])
        for host in ('127.0.0.1', 'localhost', '[::1]'):
            data['urls'][0]['url'] = 'http://' + host + ':' + str(worktree['port'] + 1)
            m.record_preview_receipt(inst, n, 'job', data)
        data['urls'][0]['url'] = 'http://localhost:' + str(worktree['port'] + 2)
        with self.assertRaises(ValueError):
            m.record_preview_receipt(inst, n, 'job', data)

    def test_reopened_job_and_removed_node_refuse_previous_receipt(self):
        m, inst, n, row, data = self.context()
        m.record_preview_receipt(inst, n, 'job', data)
        rows = m.read_jobs(n)
        rows[-1]['Started'] = '2026-01-01T00:00:00Z'
        m.write_jobs(inst, n, rows)
        with self.assertRaises(ValueError):
            m.validate_preview_receipt(inst, n, row, data['head'], data)
        # Restore the job identity, then remove its node while retaining cached
        # node objects. Readiness reports refusal rather than trusting the cache.
        m.write_jobs(inst, n, [row])
        (n['dir'] / 'node.md').unlink()
        self.assertTrue(m.validate_ready_preview(inst, n, 'job', data['head']))

    def test_removed_live_home_binding_refuses_before_full_verify(self):
        m, inst, n, row, data = self.context()
        self.b(inst.root, 'set', 'web', 'verify=true')
        inst = m.Instance(inst.root)
        n = inst.node('web')
        m.record_preview_receipt(inst, n, 'job', data)
        fresh = m.Instance(inst.root)
        del fresh.local_of('web')['home']
        fresh.save_local()
        self.assertTrue(m.validate_ready_preview(inst, n, 'job', data['head']))
        with self.assertRaises(ValueError):
            m.record_preview_receipt(inst, n, 'job', data)
        with mock.patch.object(m, 'run_verify') as execute:
            packet, reasons = m.record_ready(inst, n, 'job', 1)
        execute.assert_not_called()
        self.assertIsNone(packet)
        self.assertIn('missing or invalid exact-head preview rendering and three-source-sample receipt', reasons)

    def test_unbound_home_refuses_before_git_state_preflight(self):
        m, inst, n, row, data = self.context()
        file_home = self.tmp / 'file-home'
        file_home.write_text('not a directory\n')
        for home in (None, str(self.tmp / 'missing-home'), str(file_home)):
            fresh = m.Instance(inst.root)
            if home is None:
                fresh.local_of('web').pop('home')
            else:
                fresh.local_of('web')['home'] = home
            fresh.save_local()
            with self.subTest(home=home), mock.patch.object(m, 'git_state_paths') as state, \
                    mock.patch.object(m, 'git') as git:
                with self.assertRaisesRegex(ValueError, 'preview receipt requires a bound home'):
                    m.record_preview_receipt(inst, n, 'job', data)
                state.assert_not_called()
                git.assert_not_called()
