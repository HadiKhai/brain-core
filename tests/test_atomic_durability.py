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
        self.assertEqual(events, ['file', 'file', 'replace', 'directory'])
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

    def test_cleanup_failure_preserves_primary_publication_error(self):
        for primary_phase, cleanup_phase in (('file-sync', 'unlink'),
                                              ('file-sync', 'directory-sync'),
                                              ('replace', 'directory-sync')):
            with self.subTest(primary=primary_phase, cleanup=cleanup_phase):
                primary = OSError('primary publication failure')
                cleanup = OSError('secondary cleanup failure')
                real_sync, real_unlink = os.fsync, os.unlink

                def sync(fd):
                    if stat.S_ISREG(os.fstat(fd).st_mode):
                        if primary_phase == 'file-sync':
                            raise primary
                        return real_sync(fd)
                    raise cleanup

                def unlink(name, **kwargs):
                    if cleanup_phase == 'unlink':
                        raise cleanup
                    return real_unlink(name, **kwargs)

                with mock.patch.object(self.m.os, 'fsync', side_effect=sync), \
                        mock.patch.object(self.m.os, 'unlink', side_effect=unlink), \
                        mock.patch.object(self.m.os, 'replace', side_effect=primary):
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
                                 ('file-sync', 0o600), ('file-sync', 0o644),
                                 ('replace', 0o644), ('directory-sync', None)])
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
        staging = [path for path in self.path.parent.iterdir() if path != self.path]
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
