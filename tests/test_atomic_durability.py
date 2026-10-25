"""Per-file persistence ordering; these tests do not simulate power loss."""
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import core_loader

CORE = Path(__file__).resolve().parent.parent


class AtomicDurability(unittest.TestCase):
    def setUp(self):
        self.m = core_loader.SourceFileLoader('durability_fixture', str(CORE / 'bin/brain')).load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'record.json'
        self.path.write_text('old')

    def test_file_sync_then_replace_then_parent_sync(self):
        events = []
        real_sync, real_replace = os.fsync, os.replace
        def sync(fd):
            events.append('directory' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file')
            return real_sync(fd)
        def replace(source, target):
            events.append('replace')
            return real_replace(source, target)
        with mock.patch.object(self.m.os, 'fsync', side_effect=sync), mock.patch.object(self.m.os, 'replace', side_effect=replace):
            self.m.atomic_write(self.path, 'new')
        self.assertEqual(events, ['file', 'replace', 'directory'])
        self.assertEqual(self.path.read_text(), 'new')

    def test_prepublication_sync_failure_preserves_old_file(self):
        with mock.patch.object(self.m.os, 'fsync', side_effect=OSError('disk error')):
            with self.assertRaises(OSError):
                self.m.atomic_write(self.path, 'new')
        self.assertEqual(self.path.read_text(), 'old')
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_directory_sync_failure_is_reported_after_complete_publication(self):
        real_sync = os.fsync
        def sync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError('directory persistence unavailable')
            return real_sync(fd)
        with mock.patch.object(self.m.os, 'fsync', side_effect=sync):
            with self.assertRaisesRegex(OSError, 'directory persistence'):
                self.m.atomic_write(self.path, 'new')
        self.assertEqual(self.path.read_text(), 'new')
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
