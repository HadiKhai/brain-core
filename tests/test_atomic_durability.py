"""Per-file persistence ordering; these tests do not simulate power loss."""
import os
import json
import subprocess
import sys
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

    def test_replaced_checked_parent_cannot_redirect_publication(self):
        root = Path(self.tmp.name) / 'instance'
        root.mkdir()
        for name in ('instance.md', 'node.md'):
            (root / name).write_text('fixture')
        storage = root / 'storage'
        storage.mkdir()
        moved = root / 'original-storage'
        outside = Path(self.tmp.name) / 'outside'
        outside.mkdir()
        target = storage / 'record.json'
        target.write_text('owned old')
        (outside / target.name).write_text('foreign old')
        before = {p.name: p.read_bytes() for p in outside.iterdir()}
        checked = self.m.safe_target

        def retarget(path):
            result = checked(path)
            if Path(path) == target:
                storage.rename(moved)
                storage.symlink_to(outside)
            return result

        with mock.patch.object(self.m, 'safe_target', side_effect=retarget):
            try:
                self.m.atomic_write(target, 'new')
            except (OSError, SystemExit):
                pass
        self.assertEqual({p.name: p.read_bytes() for p in outside.iterdir()}, before)
        self.assertEqual((moved / target.name).read_text(), 'owned old')

    def test_private_writable_open_tightens_before_append_but_read_is_nonmutating(self):
        self.path.chmod(0o644)
        with self.m.directory_target(self.path, private=True) as target:
            fd = target.open()
            try:
                self.assertEqual(os.read(fd, 3), b'old')
                self.assertEqual(stat.S_IMODE(os.fstat(fd).st_mode), 0o644)
            finally:
                os.close(fd)
            fd = target.open(os.O_WRONLY | os.O_APPEND)
            try:
                self.assertEqual(stat.S_IMODE(os.fstat(fd).st_mode), 0o600)
                self.assertEqual(self.path.read_text(), 'old')
                os.write(fd, b' new')
            finally:
                os.close(fd)
        self.assertEqual(self.path.read_text(), 'old new')

    def test_private_open_chmod_failure_prevents_truncation(self):
        self.path.chmod(0o644)
        with self.m.directory_target(self.path, private=True) as target:
            with mock.patch.object(self.m.os, 'fchmod', side_effect=OSError('mode refused')):
                with self.assertRaisesRegex(OSError, 'mode refused'):
                    target.open(os.O_WRONLY | os.O_TRUNC)
        self.assertEqual(self.path.read_text(), 'old')

    def test_file_sync_then_replace_then_parent_sync(self):
        events = []
        real_sync, real_replace = os.fsync, os.replace
        def sync(fd):
            events.append('directory' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file')
            return real_sync(fd)
        def replace(source, target, **kwargs):
            events.append('replace')
            return real_replace(source, target, **kwargs)
        with mock.patch.object(self.m.os, 'fsync', side_effect=sync), mock.patch.object(self.m.os, 'replace', side_effect=replace):
            self.m.atomic_write(self.path, 'new')
        self.assertEqual(events, ['file', 'file', 'directory', 'file', 'replace', 'directory', 'directory'])
        self.assertEqual(self.path.read_text(), 'new')

    def test_prepublication_sync_failure_preserves_old_file(self):
        with mock.patch.object(self.m.os, 'fsync', side_effect=OSError('disk error')):
            with self.assertRaises(OSError):
                self.m.atomic_write(self.path, 'new')
        self.assertEqual(self.path.read_text(), 'old')
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_directory_sync_failure_is_reported_after_complete_publication(self):
        real_sync, real_replace = os.fsync, os.replace
        published = []
        def replace(*args, **kwargs):
            result = real_replace(*args, **kwargs)
            published.append(True)
            return result
        def sync(fd):
            if published and stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError('directory persistence unavailable')
            return real_sync(fd)
        with mock.patch.object(self.m.os, 'fsync', side_effect=sync), \
                mock.patch.object(self.m.os, 'replace', side_effect=replace):
            with self.assertRaisesRegex(OSError, 'directory persistence'):
                self.m.atomic_write(self.path, 'new')
        self.assertEqual(self.path.read_text(), 'new')
        records = list(self.path.parent.glob('.intended-mode-*'))
        self.assertEqual(len(records), 1)
        with self.m.directory_target(self.path) as target:
            target.recover_mode()
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_cleanup_failure_preserves_primary_publication_error(self):
        for primary_phase, cleanup_phase in (('file-sync', 'unlink'),
                                              ('file-sync', 'directory-sync'),
                                              ('replace', 'directory-sync')):
            with self.subTest(primary=primary_phase, cleanup=cleanup_phase):
                primary = OSError('primary publication failure')
                cleanup = OSError('secondary cleanup failure')
                real_sync, real_unlink = os.fsync, os.unlink
                replacing = []

                def replace(*args, **kwargs):
                    replacing.append(True)
                    raise primary

                def sync(fd):
                    if stat.S_ISREG(os.fstat(fd).st_mode):
                        if primary_phase == 'file-sync':
                            raise primary
                        return real_sync(fd)
                    if primary_phase == 'replace' and not replacing:
                        return real_sync(fd)
                    raise cleanup

                def unlink(name, **kwargs):
                    if cleanup_phase == 'unlink':
                        raise cleanup
                    return real_unlink(name, **kwargs)

                with mock.patch.object(self.m.os, 'fsync', side_effect=sync), \
                        mock.patch.object(self.m.os, 'unlink', side_effect=unlink), \
                        mock.patch.object(self.m.os, 'replace', side_effect=replace):
                    with self.assertRaises(OSError) as raised:
                        self.m.atomic_write(self.path, 'new')
                self.assertIs(raised.exception, primary)
                self.assertIs(raised.exception.__cause__, cleanup)
                self.assertEqual(self.path.read_text(), 'old')
                staging = [p for p in self.path.parent.iterdir() if p != self.path]
                if cleanup_phase == 'unlink':
                    self.assertEqual(len(staging), 1)
                    self.assertEqual(staging[0].read_text(), 'new')
                    self.assertEqual(stat.S_IMODE(staging[0].stat().st_mode), 0o600)
                    staging[0].unlink()  # fixture owns this failed-cleanup inode
                else:
                    self.assertEqual(staging, [])

    def test_public_mode_is_prepared_only_after_private_staging_is_synced(self):
        self.path.chmod(0o644)
        replace, sync, fdopen = os.replace, os.fsync, os.fdopen
        modes = []

        class Writer:
            def __init__(writer, *args, **kwargs):
                writer.stream = fdopen(*args, **kwargs)
            def __enter__(writer):
                return writer
            def __exit__(writer, *args):
                return writer.stream.__exit__(*args)
            def fileno(writer):
                return writer.stream.fileno()
            def write(writer, text):
                mode = stat.S_IMODE(os.fstat(writer.fileno()).st_mode)
                self.assertEqual(mode, 0o600)
                modes.append(('write', mode))
                return writer.stream.write(text)
            def flush(writer):
                modes.append(('flush', stat.S_IMODE(os.fstat(writer.fileno()).st_mode)))
                return writer.stream.flush()

        def synced(fd):
            st = os.fstat(fd)
            modes.append(('directory-sync', None) if stat.S_ISDIR(st.st_mode)
                         else ('file-sync', stat.S_IMODE(st.st_mode)))
            return sync(fd)

        def observe(source, target, **kwargs):
            modes.append(('replace', stat.S_IMODE(os.stat(source, dir_fd=kwargs['src_dir_fd']).st_mode)))
            self.assertEqual(self.path.read_text(), 'old')
            return replace(source, target, **kwargs)
        with mock.patch.object(self.m.os, 'replace', side_effect=observe), \
                mock.patch.object(self.m.os, 'fsync', side_effect=synced), \
                mock.patch.object(self.m.os, 'fdopen', side_effect=Writer):
            self.m.atomic_write(self.path, 'new')
        self.assertEqual(modes, [('write', 0o600), ('flush', 0o600),
                                 ('file-sync', 0o600), ('write', 0o600), ('flush', 0o600),
                                 ('file-sync', 0o600), ('directory-sync', None), ('file-sync', 0o644),
                                 ('replace', 0o644), ('directory-sync', None), ('directory-sync', None)])
        self.assertEqual(self.path.read_text(), 'new')
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o644)

    def test_intended_mode_preserves_existing_defaults_new_and_keeps_private(self):
        for existing, private, intended in ((0o640, False, 0o640),
                                            (None, False, 0o644),
                                            (0o644, True, 0o600),
                                            (None, True, 0o600)):
            with self.subTest(existing=existing, private=private):
                self.path.unlink(missing_ok=True)
                if existing is not None:
                    self.path.write_text('old')
                    self.path.chmod(existing)
                self.m.atomic_write(self.path, 'new', private=private)
                self.assertEqual(self.path.read_text(), 'new')
                self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), intended)

    def test_mode_preparation_failures_never_publish_and_leave_private_staging(self):
        chmod, sync = os.fchmod, os.fsync
        for phase in ('chmod', 'mode-sync'):
            for cleanup_fails in (False, True):
                with self.subTest(phase=phase, cleanup_fails=cleanup_fails):
                    self.path.chmod(0o644)
                    primary = OSError('mode preparation failed')
                    cleanup = OSError('cleanup failed')
                    def changed(fd, mode):
                        if phase == 'chmod' and mode == 0o644:
                            raise primary
                        return chmod(fd, mode)
                    def synced(fd):
                        st = os.fstat(fd)
                        if phase == 'mode-sync' and stat.S_ISREG(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o644:
                            raise primary
                        return sync(fd)
                    unlink = mock.patch.object(self.m.os, 'unlink', side_effect=cleanup) if cleanup_fails else mock.patch.object(self.m.os, 'unlink', wraps=os.unlink)
                    with mock.patch.object(self.m.os, 'fchmod', side_effect=changed), \
                            mock.patch.object(self.m.os, 'fsync', side_effect=synced), \
                            mock.patch.object(self.m.os, 'replace') as replace, unlink:
                        with self.assertRaises(OSError) as raised:
                            self.m.atomic_write(self.path, 'new')
                    self.assertIs(raised.exception, primary)
                    replace.assert_not_called()
                    self.assertEqual(self.path.read_text(), 'old')
                    self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o644)
                    records = list(self.path.parent.glob('.intended-mode-*'))
                    self.assertEqual(len(records), int(cleanup_fails))
                    for record in records:
                        self.assertEqual(stat.S_IMODE(record.stat().st_mode), 0o600)
                        record.unlink()
                    staging = [path for path in self.path.parent.iterdir() if path != self.path]
                    self.assertEqual(len(staging), int(cleanup_fails))
                    if cleanup_fails:
                        self.assertIs(raised.exception.__cause__, cleanup)
                        self.assertEqual(stat.S_IMODE(staging[0].stat().st_mode), 0o600)
                        staging[0].unlink()

    def test_replace_and_cleanup_failure_leaves_private_staging_and_old_target(self):
        self.path.chmod(0o644)
        primary, cleanup = OSError('replace failed'), OSError('cleanup failed')
        with mock.patch.object(self.m.os, 'replace', side_effect=primary), \
                mock.patch.object(self.m.os, 'unlink', side_effect=cleanup):
            with self.assertRaises(OSError) as raised:
                self.m.atomic_write(self.path, 'new')
        self.assertIs(raised.exception, primary)
        self.assertIs(raised.exception.__cause__, cleanup)
        self.assertEqual(self.path.read_text(), 'old')
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o644)
        records = list(self.path.parent.glob('.intended-mode-*'))
        self.assertEqual(len(records), 1)
        self.assertEqual(stat.S_IMODE(records[0].stat().st_mode), 0o600)
        staging = [path for path in self.path.parent.iterdir() if path != self.path and path not in records]
        self.assertEqual(len(staging), 1)
        self.assertEqual(staging[0].read_text(), 'new')
        self.assertEqual(stat.S_IMODE(staging[0].stat().st_mode), 0o600)

    def test_parent_retarget_during_file_sync_preserves_foreign_bytes_and_cleans_owned_temp(self):
        folder = self.path.parent / 'storage'
        folder.mkdir()
        target = folder / 'record'
        target.write_text('old')
        outside = self.path.parent / 'outside'
        outside.mkdir()
        (outside / 'record').write_text('foreign')
        moved = self.path.parent / 'moved'
        real_sync = os.fsync

        def sync(fd):
            real_sync(fd)
            if stat.S_ISREG(os.fstat(fd).st_mode):
                folder.rename(moved)
                folder.symlink_to(outside)

        with mock.patch.object(self.m.os, 'fsync', side_effect=sync):
            with self.assertRaises(OSError):
                self.m.atomic_write(target, 'new')
        self.assertEqual((outside / 'record').read_text(), 'foreign')
        self.assertEqual([p.name for p in outside.iterdir()], ['record'])
        self.assertEqual((moved / 'record').read_text(), 'old')
        self.assertEqual([p.name for p in moved.iterdir()], ['record'])

    def test_private_mkdir_and_cache_publication_use_directory_authority(self):
        target = self.path.parent / 'cache' / 'nested' / 'state.json'
        calls = []
        real_open, real_replace = os.open, os.replace

        def opened(path, flags, *args, **kwargs):
            if flags & os.O_CREAT:
                calls.append(('create', path, kwargs.get('dir_fd')))
            return real_open(path, flags, *args, **kwargs)

        def replaced(src, dst, **kwargs):
            calls.append(('replace', src, dst, kwargs))
            return real_replace(src, dst, **kwargs)

        with mock.patch.object(self.m.os, 'open', side_effect=opened), mock.patch.object(self.m.os, 'replace', side_effect=replaced):
            self.m.atomic_write(target, '{}\n', private=True, create=True)
        self.assertEqual(target.read_text(), '{}\n')
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode), 0o700)
        self.assertTrue(calls)
        self.assertTrue(all('/' not in str(row[1]) for row in calls))
        self.assertIsNotNone(calls[0][2])
        self.assertEqual(calls[-1][3]['src_dir_fd'], calls[-1][3]['dst_dir_fd'])

    def test_mkdir_replaced_before_open_cannot_create_in_foreign_directory(self):
        outside = self.path.parent / 'outside'
        outside.mkdir()
        (outside / 'sentinel').write_text('keep')
        target = self.path.parent / 'cache' / 'nested' / 'record'
        real_mkdir = os.mkdir

        def mkdir(name, *args, **kwargs):
            result = real_mkdir(name, *args, **kwargs)
            if name == 'cache' and kwargs.get('dir_fd') is not None:
                (self.path.parent / 'cache').rename(self.path.parent / 'owned-cache')
                (self.path.parent / 'cache').symlink_to(outside)
            return result

        with mock.patch.object(self.m.os, 'mkdir', side_effect=mkdir):
            with self.assertRaises(OSError):
                self.m.atomic_write(target, 'new', private=True, create=True)
        self.assertEqual({p.name: p.read_text() for p in outside.iterdir()}, {'sentinel': 'keep'})
        self.assertEqual(list((self.path.parent / 'owned-cache').iterdir()), [])

    def test_cleanup_keeps_replacement_temporary_name_and_old_target(self):
        real_sync = os.fsync
        replacement = []

        def sync(fd):
            real_sync(fd)
            if stat.S_ISREG(os.fstat(fd).st_mode):
                temporary, = [p for p in self.path.parent.iterdir() if p.name.startswith('.record.json.')]
                temporary.rename(self.path.parent / 'retained-owned-temp')
                temporary.write_text('foreign replacement')
                replacement.append(temporary)
                raise OSError('injected failure after replacement')

        with mock.patch.object(self.m.os, 'fsync', side_effect=sync):
            with self.assertRaisesRegex(OSError, 'injected failure'):
                self.m.atomic_write(self.path, 'new')
        self.assertEqual(self.path.read_text(), 'old')
        self.assertEqual(replacement[0].read_text(), 'foreign replacement')
        self.assertEqual((self.path.parent / 'retained-owned-temp').read_text(), 'new')

    def test_raced_hardlink_refuses_without_changing_either_name(self):
        foreign = self.path.parent / 'other-name'
        real_sync = os.fsync

        def sync(fd):
            real_sync(fd)
            if stat.S_ISREG(os.fstat(fd).st_mode):
                os.link(self.path, foreign)

        with mock.patch.object(self.m.os, 'fsync', side_effect=sync):
            with self.assertRaises(SystemExit):
                self.m.atomic_write(self.path, 'new')
        self.assertEqual(self.path.read_text(), 'old')
        self.assertEqual(foreign.read_text(), 'old')
        self.assertEqual({p.name for p in self.path.parent.iterdir()}, {'record.json', 'other-name'})

    def interrupted_publication(self, phase='after', private=False):
        code = """
import os, sys
sys.path.insert(0, sys.argv[1])
from brain.common import atomic_write
real = os.replace
def replace(src, dst, **kwargs):
    if sys.argv[3] == 'before':
        os._exit(17)
    real(src, dst, **kwargs)
    # Model a recovered filesystem whose content survived but mode is private.
    os.chmod(dst, 0o600, dir_fd=kwargs['dst_dir_fd'])
    os._exit(17)
os.replace = replace
atomic_write(sys.argv[2], 'new', private=sys.argv[4] == 'True')
"""
        result = subprocess.run([sys.executable, '-B', '-c', code, str(CORE / 'lib'),
                                 str(self.path), phase, str(private)], timeout=5)
        self.assertEqual(result.returncode, 17)
        records = list(self.path.parent.glob('.intended-mode-*'))
        self.assertEqual(len(records), 1)
        self.assertEqual(stat.S_IMODE(records[0].stat().st_mode), 0o600)
        return records[0]

    def test_recovery_restores_durable_intended_mode_before_retry_captures_mode(self):
        for existing, private, intended in ((0o640, False, 0o640),
                                            (None, False, 0o644),
                                            (0o644, True, 0o600)):
            with self.subTest(existing=existing, private=private):
                self.path.unlink(missing_ok=True)
                if existing is not None:
                    self.path.write_text('old')
                    self.path.chmod(existing)
                record = self.interrupted_publication(private=private)
                self.assertEqual(self.path.read_text(), 'new')
                self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
                # A read is nonmutating; the next writer recovers first.
                with self.m.directory_target(self.path, private=private) as target:
                    self.assertEqual(target.read_bytes(), b'new')
                self.assertTrue(record.exists())
                self.m.atomic_write(self.path, 'retry', private=private)
                self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), intended)
                self.assertEqual(self.path.read_text(), 'retry')
                self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_recovery_does_not_promote_unpublished_staging_or_change_old_destination(self):
        self.path.chmod(0o640)
        record = self.interrupted_publication('before')
        before = self.path.stat()
        with self.m.directory_target(self.path) as target:
            target.recover_mode()
        self.assertEqual(self.path.read_text(), 'old')
        self.assertEqual(self.path.stat(), before)
        self.assertTrue(record.exists())
        staging, = self.path.parent.glob('.record.json.*')
        self.assertEqual(staging.read_text(), 'new')

    def test_recovery_refuses_changed_content_and_unsafe_or_invalid_records(self):
        record = self.interrupted_publication()
        original = record.read_bytes()
        for kind in ('json', 'mode', 'hardlink', 'symlink', 'digest'):
            with self.subTest(kind=kind):
                record.write_bytes(original)
                record.chmod(0o600)
                foreign = self.path.parent / 'foreign'
                if kind == 'json':
                    record.write_text('{')
                elif kind == 'mode':
                    record.chmod(0o644)
                elif kind == 'hardlink':
                    os.link(record, foreign)
                elif kind == 'symlink':
                    record.rename(foreign)
                    record.symlink_to(foreign)
                else:
                    data = json.loads(original)
                    data['sha256'] = '0' * 64
                    record.write_text(json.dumps(data))
                with self.m.directory_target(self.path) as target:
                    with self.assertRaises(OSError):
                        target.recover_mode()
                self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
                self.assertEqual(self.path.read_text(), 'new')
                if kind == 'symlink':
                    record.unlink()
                    foreign.rename(record)
                foreign.unlink(missing_ok=True)
        record.write_bytes(original)
        self.path.write_text('foreign edit')
        with self.m.directory_target(self.path) as target:
            with self.assertRaises(OSError):
                target.recover_mode()
        self.assertEqual(self.path.read_text(), 'foreign edit')

    def test_recovery_chmod_or_sync_failure_retains_intent_and_never_publishes(self):
        record = self.interrupted_publication()
        for operation in ('fchmod', 'fsync'):
            with self.subTest(operation=operation):
                self.path.chmod(0o600)
                with mock.patch.object(self.m.os, operation, side_effect=OSError('recovery refused')), \
                        mock.patch.object(self.m.os, 'replace') as replace:
                    with self.assertRaisesRegex(OSError, 'recovery refused'):
                        self.m.atomic_write(self.path, 'retry')
                replace.assert_not_called()
                self.assertEqual(self.path.read_text(), 'new')
                self.assertTrue(record.exists())
        with self.m.directory_target(self.path) as target:
            target.recover_mode()
            target.recover_mode()
        self.assertFalse(record.exists())

    def test_intent_directory_sync_failure_prevents_publication_and_cleans_owned_files(self):
        real_sync = os.fsync
        def sync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError('intent directory sync refused')
            return real_sync(fd)
        with mock.patch.object(self.m.os, 'fsync', side_effect=sync), \
                mock.patch.object(self.m.os, 'replace') as replace:
            with self.assertRaisesRegex(OSError, 'intent directory sync refused'):
                self.m.atomic_write(self.path, 'new')
        replace.assert_not_called()
        self.assertEqual(self.path.read_text(), 'old')
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_recovery_refuses_retargeted_parent_and_preserves_foreign_mode(self):
        record = self.interrupted_publication()
        outside = self.path.parent / 'foreign'
        outside.mkdir()
        foreign = outside / self.path.name
        foreign.write_text('foreign')
        foreign.chmod(0o600)
        root = self.path.parent
        moved = root.with_name(root.name + '-moved')
        with self.m.directory_target(self.path) as target:
            root.rename(moved)
            root.symlink_to(outside.relative_to(root))
            try:
                with self.assertRaises(OSError):
                    target.recover_mode()
                self.assertEqual((moved / 'foreign' / self.path.name).read_text(), 'foreign')
                self.assertEqual(stat.S_IMODE((moved / 'foreign' / self.path.name).stat().st_mode), 0o600)
            finally:
                root.unlink()
                moved.rename(root)
        self.assertTrue(record.exists())

    def test_recovery_preserves_expected_generation_contract(self):
        self.path.chmod(0o640)
        record = self.interrupted_publication()
        with self.m.directory_target(self.path) as target:
            before = target.generation(target.stat())
            with self.assertRaisesRegex(OSError, 'publication target changed'):
                target.atomic_write('stale', expected=None)
            self.assertEqual(target.generation(target.stat()), before)
            self.assertTrue(record.exists())
            target.atomic_write('retry', expected=before)
        self.assertEqual(self.path.read_text(), 'retry')
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)
