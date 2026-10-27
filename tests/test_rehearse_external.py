"""Declared snapshots use fabricated repositories only; no rehearsal is run."""
import json
import copy
import os
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from brain import instance, rehearse, rehearse_external
from scenario import Scenario, CORE
from unittest import mock


class OwnedCopyLifecycle(unittest.TestCase):
    def setUp(self):
        from test_rehearse import persistent_fixture_directory
        self.parent = persistent_fixture_directory(self)

    def test_persistent_copy_root_refuses_temp_and_realpath_aliases_before_start(self):
        from types import SimpleNamespace
        aged = self.parent / 'aged'
        aged.mkdir()
        alias = self.parent / 'aged-alias'
        alias.symlink_to(aged, target_is_directory=True)
        with mock.patch.dict(os.environ, {'TMPDIR': str(aged)}), \
                mock.patch.object(rehearse_external.tempfile, 'mkdtemp') as allocate, \
                mock.patch.object(rehearse_external.OwnedCopies, 'start_guardian') as start:
            for options in ({}, {'persistent': True}):
                for path in (aged, aged / 'run', alias / 'run', Path('/var/folders/sample/run'),
                             Path('/private/var/folders/sample/run'), Path(tempfile.gettempdir()) / 'run'):
                    with self.subTest(path=path, options=options), self.assertRaisesRegex(ValueError, 'temporary rehearsal'):
                        rehearse_external.OwnedCopies(parent=path, **options)
            allocate.assert_not_called()
            start.assert_not_called()
            node = dict(external=True, home_path=self.parent / 'candidate',
                        local=dict(workbench=str(aged / 'workbench')))
            with mock.patch.object(rehearse._m_verify, 'ready_refs', return_value=('a', 'b')), \
                    mock.patch.object(rehearse._m_verify, 'integration_parent', return_value='b'), \
                    mock.patch.object(rehearse, 'sandbox_self_check') as sandbox:
                with self.assertRaisesRegex(ValueError, 'temporary rehearsal'):
                    rehearse.rehearse(SimpleNamespace(), node, 'sample')
                sandbox.assert_not_called()
            self.assertEqual([], list(aged.iterdir()))

    def test_all_copy_allocations_refuse_home_dot_directories_before_start(self):
        home = self.parent / 'fabricated-home'
        home.mkdir()
        alias = self.parent / 'alias'
        alias.symlink_to(home / '.codex', target_is_directory=True)
        before = rehearse.snapshot(self.parent)
        with mock.patch.dict(os.environ, {'HOME': str(home)}), \
                mock.patch.object(rehearse_external.tempfile, 'mkdtemp') as allocate, \
                mock.patch.object(rehearse_external.OwnedCopies, 'start_guardian') as start:
            for options in ({}, {'persistent': True}):
                for name in ('.codex', '.claude', '.claude-work', '.config', '.cache', '.ssh', '.other'):
                    for path in (home / name, home / name / 'runs'):
                        with self.subTest(path=path, options=options), self.assertRaisesRegex(ValueError, 'HOME dot-directory'):
                            rehearse_external.OwnedCopies(parent=path, **options)
                for path in (alias / 'runs', Path('~/.codex/runs')):
                    with self.subTest(path=path, options=options), self.assertRaisesRegex(ValueError, 'HOME dot-directory'):
                        rehearse_external.OwnedCopies(parent=path, **options)
                with self.assertRaisesRegex(ValueError, 'HOME dot-directory'):
                    rehearse_external.OwnedCopies(parent=home, prefix='.hidden-', **options)
            allocate.assert_not_called()
            start.assert_not_called()
        self.assertEqual(before, rehearse.snapshot(self.parent))

    def test_copy_allocation_requires_an_explicit_parent(self):
        with mock.patch.object(rehearse_external.tempfile, 'mkdtemp') as allocate:
            with self.assertRaisesRegex(ValueError, 'copy parent required'):
                rehearse_external.OwnedCopies()
        allocate.assert_not_called()

    def test_persistent_workbench_copy_preserves_timestamps_and_cleans_owned_bulk(self):
        from test_rehearse import persistent_fixture_directory
        from types import SimpleNamespace
        work = persistent_fixture_directory(self)
        home = work / 'candidate'
        node = dict(external=True, home_path=home, local={})
        parent = rehearse.rehearsal_copy_parent(SimpleNamespace(), node)
        self.assertEqual(home.parent / 'candidate-work-rehearsal-copies', parent)
        original = self.parent / 'original'
        original.mkdir()
        source = original / 'old'
        source.write_bytes(b'sample old content')
        os.utime(source, ns=(946684800000000000, 946684800000000000))
        before = source.stat()
        with rehearse_external.OwnedCopies(parent=parent, persistent=True) as owner:
            self.assertEqual(parent.resolve(), owner.root.parent)
            self.assertEqual(0o700, parent.stat().st_mode & 0o777)
            rehearse.copy_tree(original, owner.bulk / 'copy')
            self.assertEqual(before.st_mtime_ns, (owner.bulk / 'copy/old').stat().st_mtime_ns)
            self.assertEqual(b'sample old content', (owner.bulk / 'copy/old').read_bytes())
        self.assertFalse(owner.bulk.exists())
        self.assertEqual(['cleanup.json'], [p.name for p in owner.root.iterdir()])
        self.assertEqual('removed', json.loads((owner.root / 'cleanup.json').read_text())['state'])
        self.assertEqual(b'sample old content', source.read_bytes())

    def test_copy_parent_refuses_overlap_with_declared_sources(self):
        from types import SimpleNamespace
        node = dict(external=True, home_path=self.parent / 'candidate', local={})
        selected = self.parent / 'candidate-work-rehearsal-copies'
        for source in (selected, selected.parent, selected / 'nested'):
            inst = SimpleNamespace(local=dict(rehearse_external=dict(roots=[str(source)])))
            with self.subTest(source=source), self.assertRaisesRegex(rehearse.RehearsalError, 'overlap source'):
                rehearse.rehearsal_copy_parent(inst, node)

    def test_frozen_accounting_uses_only_attested_aggregates(self):
        import contextlib
        import hashlib
        from types import SimpleNamespace
        from brain import spend, jobs
        row = dict(Job='job/sample', Started='2026-01-01 00:00 +00:00', Ended='')
        missing = {**row, 'Job': 'job/unavailable'}
        node = dict(id='demo', dir=self.parent, local={})
        inst = SimpleNamespace(root=self.parent, nodes=lambda: [node])
        usage = {k: 3 for k in (*spend.SPEND_TOKENS, 'processed', 'compactions', 'sessions',
                                'tools', 'subagents', 'active_minutes', 'agent_minutes')}
        usage.update(models={'safe-model': 3}, runtimes={}, source='transcripts: 1 session')
        key = jobs.digest([node['id'], jobs.job_key(row), ''])
        cache = self.parent / '.brain/spend/jobs' / (key + '.json')
        cache.parent.mkdir(parents=True)
        cache.write_text(json.dumps(dict(version=spend.SPEND_VERSION, signature='a' * 64, usage=usage)))
        original = cache.read_bytes()
        target = self.parent / 'accounting.json'
        with mock.patch.object(jobs, 'read_jobs', return_value=[row, missing]), \
                mock.patch.object(spend, 'brain_terms', return_value=[]), \
                mock.patch.object(spend, 'spend_sources', side_effect=AssertionError('provider discovery')):
            binding = rehearse_external.freeze_accounting(inst, '2035-01-01T00:00:00+00:00', target)
        self.assertEqual(original, cache.read_bytes())
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), binding['sha256'])
        packet = json.loads(target.read_text())
        entry = packet['entries'][jobs.digest([node['id'], jobs.job_key(row)])]
        self.assertEqual(hashlib.sha256(original).hexdigest(), entry['evidence']['sha256'])
        with contextlib.ExitStack() as restore:
            for name in ('job_usage', 'live_usage', '_live_usage', '_read_live_usage',
                         'spend_machine', 'spend_input_signature', 'spend_privacy_signature',
                         'spend_sources', 'coordinator_measure'):
                restore.callback(setattr, spend, name, getattr(spend, name))
            with mock.patch.object(spend, 'spend_terms', return_value=[]):
                rehearse_external.install_accounting(target, binding['sha256'], packet['frozen_now'])
                self.assertEqual(usage, spend.live_usage(inst, node, row))
                self.assertIsNone(spend.live_usage(inst, node, missing))
                self.assertIsNone(spend.coordinator_measure(inst, node))
                self.assertFalse(spend.spend_machine(inst)['complete'])
                self.assertIsNone(spend.spend_machine(inst)['since_week'])
                self.assertIn(binding['sha256'], spend.spend_input_signature(inst))
                self.assertIn(binding['sha256'], spend.spend_privacy_signature(inst))
                with self.assertRaises(ValueError):
                    spend.spend_sources(inst)
                with self.assertRaises(ValueError):
                    spend.live_usage(inst, node, {**row, 'Started': 'other generation'})
                target.write_text('{}')
                with self.assertRaises(ValueError):
                    rehearse_external.install_accounting(target, binding['sha256'], packet['frozen_now'])

    def test_killed_owner_removes_only_its_bulk_and_keeps_small_receipt(self):
        import time
        foreign = self.parent / 'foreign'
        foreign.mkdir()
        (foreign / 'keep').write_text('foreign bytes')
        program = '''
import sys, time
from brain.rehearse_external import OwnedCopies
with OwnedCopies(parent=sys.argv[1]) as copies:
    (copies.bulk / 'copy').write_bytes(b'x' * 4096)
    print(copies.root, flush=True)
    time.sleep(60)
'''
        env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'lib')}
        env.pop('BRAIN_REHEARSAL_EVIDENCE_DIR', None)
        process = subprocess.Popen([sys.executable, '-B', '-c', program, str(self.parent)],
                                   env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            root = Path(process.stdout.readline().strip())
            self.assertEqual(self.parent, root.parent)
            self.assertTrue((root / 'bulk/copy').is_file())
            process.kill()  # only the worker this fixture just started
            process.communicate(timeout=5)
            deadline = time.monotonic() + 5
            while (root / 'bulk').exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertFalse((root / 'bulk').exists())
            self.assertEqual(['cleanup.json'], [p.name for p in root.iterdir()])
            receipt = json.loads((root / 'cleanup.json').read_text())
            self.assertEqual('removed', receipt['state'])
            self.assertEqual('owner-exit', receipt['terminal'])
            self.assertEqual('foreign bytes', (foreign / 'keep').read_text())
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

    def test_kill_recovery_covers_live_child_staging_and_preserves_refusals(self):
        import time
        program = '''
import json, os, subprocess, sys, time
from pathlib import Path
from brain import rehearse, rehearse_external as external
mode = sys.argv[2]
copies = external.OwnedCopies(parent=sys.argv[1])
original = rehearse.shutil.rmtree
def deleting(path, *args, **kwargs):
    if Path(path).name == 'retiring-' + copies.run:
        print(copies.root, flush=True)
        time.sleep(60)
    return original(path, *args, **kwargs)
if mode == 'staging':
    rehearse.shutil.rmtree = deleting
with copies:
    (copies.bulk / 'keep').write_text('owned bytes')
    if mode == 'child':
        copies.before_process()
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
            start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        copies.track_process(child)
        (copies.bulk / 'pid').write_text(str(child.pid))
    if mode == 'uncertain':
        copies.retain_unknown()
    if mode == 'receipt':
        (copies.root / 'cleanup.json').write_text('{}')
    if mode == 'metadata':
        path = copies.root / 'cleanup.json'
        data = json.loads(path.read_text())
        data['state'] = 'foreign-change'
        path.write_text(json.dumps(data))
    if mode != 'staging':
        print(copies.root, flush=True)
        time.sleep(60)
'''
        env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'lib')}
        env.pop('BRAIN_REHEARSAL_EVIDENCE_DIR', None)
        for mode in ('child', 'staging', 'uncertain', 'receipt', 'metadata'):
            with self.subTest(mode=mode):
                process = subprocess.Popen([sys.executable, '-B', '-c', program, str(self.parent), mode],
                    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    root = Path(process.stdout.readline().strip())
                    self.assertEqual(self.parent, root.parent)
                    child_pid = int((root / 'bulk/pid').read_text()) if mode == 'child' else None
                    process.kill()
                    process.communicate(timeout=5)

                    expected = mode in ('child', 'staging')
                    deadline = time.monotonic() + (7 if expected else .2)
                    while time.monotonic() < deadline:
                        if expected and list(root.iterdir()) == [root / 'cleanup.json']:
                            break
                        time.sleep(.02)
                    self.assertEqual(expected, list(root.iterdir()) == [root / 'cleanup.json'])
                    if child_pid is not None:
                        with self.assertRaises(ProcessLookupError):
                            os.killpg(child_pid, 0)
                    if not expected:
                        self.assertEqual('owned bytes', (root / 'bulk/keep').read_text())
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.communicate(timeout=5)

    def test_terminal_cleanup_ignores_repeat_cancellation_and_restores_handlers(self):
        import signal
        handlers = [signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)]
        original = rehearse.shutil.rmtree
        seen = []
        def deleting(path, *args, **kwargs):
            if Path(path).name.startswith('retiring-'):
                seen.append(path)
                os.kill(os.getpid(), signal.SIGTERM)
                os.kill(os.getpid(), signal.SIGINT)
            return original(path, *args, **kwargs)
        error = AssertionError('original body failure')
        owner = rehearse_external.OwnedCopies(parent=self.parent)
        with mock.patch.object(rehearse.shutil, 'rmtree', side_effect=deleting):
            with self.assertRaises(AssertionError) as caught:
                with owner:
                    (owner.bulk / 'copy').write_text('owned')
                    raise error
        self.assertIs(error, caught.exception)
        self.assertEqual(1, len(seen))
        self.assertFalse(owner.bulk.exists())
        self.assertEqual(handlers, [signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)])

    def test_registration_failure_still_stops_and_reaps_its_started_process(self):
        owner = rehearse_external.OwnedCopies(parent=self.parent)
        error = OSError('registration unavailable')
        message = owner.guardian_message
        def registering(value):
            if value['type'] == 'process':
                owner.retain_unknown()
                raise error
            return message(value)
        with self.assertRaises(OSError) as caught:
            with owner, mock.patch.object(owner, 'guardian_message', side_effect=registering):
                rehearse_external.owned_run([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.assertIs(error, caught.exception)
        self.assertEqual(1, len(owner.processes))
        process = owner.processes[0]
        self.assertIsNotNone(process.poll())
        with self.assertRaises(ProcessLookupError):
            os.killpg(process.pid, 0)
        self.assertTrue(owner.bulk.exists())

    def test_guardian_setup_failure_restores_context_and_signal_handler(self):
        import signal
        previous = signal.getsignal(signal.SIGTERM)
        owner = rehearse_external.OwnedCopies(parent=self.parent)
        error = OSError('supervisor launch failure')
        with mock.patch.object(rehearse_external, '_guardian_popen', side_effect=error):
            with self.assertRaises(OSError) as caught:
                with owner:
                    self.fail('setup failure entered body')
        self.assertIs(error, caught.exception)
        self.assertIsNone(rehearse_external.ACTIVE_COPIES.get())
        self.assertEqual(previous, signal.getsignal(signal.SIGTERM))
        self.assertTrue(owner.bulk.is_dir())

    def test_success_failure_timeout_and_cancel_remove_only_owned_bulk(self):
        foreign = self.parent / 'foreign'
        foreign.mkdir()
        (foreign / 'keep').write_text('unowned')
        for error in (None, ValueError('private-canary'),
                      subprocess.TimeoutExpired(['private-canary'], 1), KeyboardInterrupt()):
            with self.subTest(terminal=type(error).__name__):
                owner = rehearse_external.OwnedCopies(parent=self.parent)
                try:
                    with owner:
                        (owner.bulk / 'copy').write_text('private-canary')
                        # Even an internal link cannot authorize target deletion.
                        (owner.bulk / 'outside').symlink_to(foreign, target_is_directory=True)
                        if error is not None:
                            raise error
                except BaseException as caught:
                    self.assertIs(caught, error)
                self.assertFalse(owner.bulk.exists())
                receipt = (owner.root / 'cleanup.json').read_text()
                self.assertEqual('removed', json.loads(receipt)['state'])
                self.assertNotIn('private-canary', receipt)
                self.assertEqual(['cleanup.json'], [p.name for p in owner.root.iterdir()])
                self.assertEqual('unowned', (foreign / 'keep').read_text())

    def test_live_denied_or_unknown_process_retains_and_proven_absence_removes(self):
        for code, group in ((None, None), (0, None), (0, PermissionError()),
                            (0, ProcessLookupError())):
            with self.subTest(code=code, group=type(group).__name__):
                owner = rehearse_external.OwnedCopies(parent=self.parent)
                process = mock.Mock(pid=424242)
                process.poll.return_value = code
                owner.processes.append(process)
                with mock.patch.object(rehearse_external.os, 'killpg', side_effect=group):
                    with owner:
                        (owner.bulk / 'keep').touch()
                removed = code == 0 and isinstance(group, ProcessLookupError)
                self.assertEqual(not removed, owner.bulk.exists())
                self.assertEqual(removed, owner.receipt['processes_ended'])

    def test_missing_changed_owner_replaced_root_and_linked_bulk_retain(self):
        for change in ('missing', 'owner', 'pid', 'root', 'link'):
            owner = rehearse_external.OwnedCopies(parent=self.parent)
            with self.subTest(change=change), owner:
                (owner.bulk / 'keep').write_text('retained')
                if change == 'missing':
                    (owner.root / 'cleanup.json').unlink()
                elif change == 'owner':
                    (owner.root / 'cleanup.json').write_text('{}')
                elif change == 'pid':
                    owner.pid += 1
                elif change == 'root':
                    owner.root.rename(owner.root.with_name(owner.root.name + '-moved'))
                    owner.root.mkdir()
                    (owner.root / 'bulk').mkdir()
                else:
                    moved = owner.root / 'moved'
                    owner.bulk.rename(moved)
                    owner.bulk.symlink_to(moved, target_is_directory=True)
            self.assertTrue(owner.bulk.exists())

    def test_inner_uncertainty_prevents_outer_deletion(self):
        with rehearse_external.OwnedCopies(parent=self.parent) as outer:
            with rehearse_external.OwnedCopies(parent=outer.bulk) as inner:
                inner.unknown = True
        self.assertTrue(inner.bulk.exists())
        self.assertTrue(outer.bulk.exists())

    def test_identity_change_during_terminal_publication_retains_both_copies(self):
        # All replacements and sentinels belong to this disposable fixture.
        # Publication/process checks separate the initial proof from deletion.
        for changed in ('bulk', 'root', 'receipt'):
            with self.subTest(changed=changed):
                with rehearse_external.OwnedCopies(parent=self.parent) as outer:
                    owner = rehearse_external.OwnedCopies(parent=outer.bulk)
                    displaced = owner.root.with_name(owner.root.name + '-displaced')
                    publish = owner.publish

                    fired = False
                    def replace_after_publish():
                        nonlocal fired
                        publish()
                        if fired:
                            return
                        fired = True
                        if changed == 'bulk':
                            owner.bulk.rename(displaced)
                            owner.bulk.mkdir(mode=0o700)
                        elif changed == 'root':
                            owner.root.rename(displaced)
                            owner.root.mkdir(mode=0o700)
                            owner.bulk.mkdir(mode=0o700)
                        else:
                            (owner.root / 'cleanup.json').write_text('{}')
                        (owner.bulk / 'replacement').write_text('fixture sentinel')

                    with mock.patch.object(owner, 'publish', side_effect=replace_after_publish), owner:
                        (owner.bulk / 'original').write_text('fixture copy')
                    self.assertTrue(owner.bulk.exists())
                    self.assertEqual('fixture sentinel', (owner.bulk / 'replacement').read_text())
                    if changed != 'receipt':
                        original = displaced / ('bulk/original' if changed == 'root' else 'original')
                        self.assertEqual('fixture copy', original.read_text())
                    self.assertNotEqual('removed', owner.receipt['state'])
                    self.assertTrue(outer.unknown)
                self.assertTrue(outer.bulk.exists())

    def test_native_retention_unknown_or_incomplete_preserves_owned_bulk(self):
        for saved, incomplete in ((None, False), (False, False), (True, True)):
            with self.subTest(saved=saved, incomplete=incomplete):
                owner = rehearse_external.OwnedCopies(parent=self.parent)
                retain = mock.Mock(incomplete=incomplete)
                retain.checkpoint.return_value = saved
                with mock.patch.object(rehearse, 'NativeRetention', return_value=retain), owner:
                    (owner.bulk / 'keep').write_text('fixture bytes')
                self.assertEqual('fixture bytes', (owner.bulk / 'keep').read_text())
                self.assertEqual('retained', owner.receipt['state'])
                self.assertIn('refusal', owner.receipt)

    def test_native_terminal_publication_failure_restores_owned_bulk(self):
        with rehearse_external.OwnedCopies(parent=self.parent) as outer:
            owner = rehearse_external.OwnedCopies(parent=outer.bulk)
            publish = owner.publish
            calls = 0
            def fail_terminal_publication():
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError('fixture terminal publication failure')
                publish()
            with mock.patch.object(owner, 'publish', side_effect=fail_terminal_publication):
                with self.assertRaises(OSError), owner:
                    (owner.bulk / 'keep').write_text('fixture bytes')
            self.assertEqual('fixture bytes', (owner.bulk / 'keep').read_text())
            self.assertTrue(outer.unknown)
        self.assertTrue(outer.bulk.exists())

    def _retirement_failure_preserves_primary(self, body_failure, native_failure=False):
        primary = AssertionError('fixture original body failure') if body_failure else None
        retirement = OSError('fixture retirement publication failure')
        finalization = OSError('fixture native finalization failure')
        original_traceback = None
        caught = None
        entries = []
        native_exceptions = []
        checkpoint = rehearse.NativeRetention.checkpoint
        exception = rehearse.NativeRetention.exception
        with mock.patch.dict(os.environ, {'BRAIN_REHEARSAL_EVIDENCE_DIR': ''}):
            with rehearse_external.OwnedCopies(parent=self.parent) as ancestor:
                with rehearse_external.OwnedCopies(parent=ancestor.bulk) as outer:
                    owner = rehearse_external.OwnedCopies(parent=outer.bulk)
                    def capture_exception(retention, error):
                        if retention.owned == owner.bulk:
                            native_exceptions.append(error)
                        return exception(retention, error)
                    def fail_checkpoint(retention, **fields):
                        if retention.owned == owner.bulk and fields.get('stage') == 'before-native-delete':
                            entries.append((owner.unknown, outer.unknown, ancestor.unknown))
                            if native_failure:
                                raise finalization
                        return checkpoint(retention, **fields)
                    def body():
                        nonlocal original_traceback
                        if primary is not None:
                            try:
                                raise primary
                            except AssertionError:
                                original_traceback = sys.exc_info()[2]
                                raise
                    with mock.patch.object(owner, 'publish', side_effect=retirement), \
                            mock.patch.object(rehearse.NativeRetention, 'exception', capture_exception), \
                            mock.patch.object(rehearse.NativeRetention, 'checkpoint', fail_checkpoint):
                        try:
                            with owner:
                                (owner.bulk / 'original').write_bytes(b'fixture original operands\x00\xff')
                                body()
                        except BaseException as error:
                            caught = error
            self.assertTrue(owner.unknown)
            self.assertTrue(outer.unknown)
            self.assertTrue(ancestor.unknown)
            self.assertTrue(ancestor.bulk.exists())
            self.assertTrue(outer.bulk.exists())
            self.assertEqual(b'fixture original operands\x00\xff', (owner.bulk / 'original').read_bytes())
            self.assertEqual([(True, True, True)], entries)
            self.assertIs(caught, primary if body_failure else retirement)
            self.assertEqual(1, len(native_exceptions))
            self.assertIs(native_exceptions[0], caught)
            if body_failure:
                tracebacks = []
                cursor = caught.__traceback__
                while cursor is not None:
                    tracebacks.append(cursor)
                    cursor = cursor.tb_next
                self.assertIn(original_traceback, tracebacks)
                self.assertIs(caught.__cause__, retirement)
                if native_failure:
                    self.assertIs(retirement.__cause__, finalization)
            elif native_failure:
                self.assertIs(caught.__cause__, finalization)

    def test_nested_evidence_off_body_assertion_survives_retirement_publication_failure(self):
        self._retirement_failure_preserves_primary(True)

    def test_nested_evidence_off_body_assertion_survives_retirement_and_native_failure(self):
        self._retirement_failure_preserves_primary(True, True)

    def test_nested_evidence_off_no_body_propagates_retirement_publication_failure(self):
        self._retirement_failure_preserves_primary(False)

    def test_nested_evidence_off_no_body_retirement_survives_native_failure(self):
        self._retirement_failure_preserves_primary(False, True)

    def _nested_rollback_failure(self, failure):
        primary = OSError('fixture terminal publication failure')
        secondary = OSError('fixture rollback ' + failure + ' failure')
        caught = None
        fired = False
        with mock.patch.dict(os.environ, {'BRAIN_REHEARSAL_EVIDENCE_DIR': ''}):
            with rehearse_external.OwnedCopies(parent=self.parent) as ancestor:
                with rehearse_external.OwnedCopies(parent=ancestor.bulk) as outer:
                    owner = rehearse_external.OwnedCopies(parent=outer.bulk)
                    staged = owner.root / ('retiring-' + owner.run)
                    original_identity = owner.identities[1]
                    publish, identity, rename = owner.publish, owner.identity, Path.rename
                    terminal_failed = False
                    calls = 0
                    def fail_publication():
                        nonlocal calls, terminal_failed
                        calls += 1
                        if calls == 2:
                            terminal_failed = True
                            raise primary
                        publish()
                    def inspect(path):
                        nonlocal fired
                        if terminal_failed and failure == 'identity':
                            fired = True
                            raise secondary
                        return identity(path)
                    def move(path, target):
                        nonlocal fired
                        if path == staged and failure == 'rename':
                            fired = True
                            raise secondary
                        return rename(path, target)
                    with mock.patch.object(owner, 'publish', side_effect=fail_publication), \
                            mock.patch.object(owner, 'identity', side_effect=inspect), \
                            mock.patch.object(Path, 'rename', autospec=True, side_effect=move):
                        try:
                            with owner:
                                (owner.bulk / 'original').write_bytes(b'fixture original bytes')
                        except BaseException as error:
                            caught = error
                    self.assertTrue(fired)
                # The parent has exited: its exceptional cleanup must retain bytes.
            self.assertTrue(ancestor.bulk.exists())
            self.assertTrue(outer.bulk.exists())
            self.assertTrue(staged.exists())
            self.assertEqual(original_identity, identity(staged))
            self.assertEqual(b'fixture original bytes', (staged / 'original').read_bytes())
            self.assertTrue(owner.unknown)
            self.assertTrue(outer.unknown)
            self.assertTrue(ancestor.unknown)
            self.assertIs(caught, primary)
            self.assertIs(caught.__cause__, secondary)

    def test_nested_evidence_off_rollback_identity_failure_retains_original_bytes(self):
        self._nested_rollback_failure('identity')

    def test_nested_evidence_off_rollback_rename_failure_retains_original_bytes(self):
        self._nested_rollback_failure('rename')

    def _nested_refusal_checkpoint_failure(self, body_failure):
        primary = ValueError('fixture body failure') if body_failure else None
        secondary = OSError('fixture refusal checkpoint failure')
        caught = None
        with mock.patch.dict(os.environ, {'BRAIN_REHEARSAL_EVIDENCE_DIR': ''}):
            with rehearse_external.OwnedCopies(parent=self.parent) as ancestor:
                with rehearse_external.OwnedCopies(parent=ancestor.bulk) as outer:
                    owner = rehearse_external.OwnedCopies(parent=outer.bulk)
                    retain = rehearse.NativeRetention(owner.bulk, 'fixture')
                    def checkpoint(**fields):
                        if 'bulk_cleanup' in fields:
                            raise secondary
                        return fields.get('stage') != 'before-native-delete'
                    with mock.patch.object(rehearse, 'NativeRetention', return_value=retain), \
                            mock.patch.object(retain, 'checkpoint', side_effect=checkpoint):
                        try:
                            with owner:
                                (owner.bulk / 'original').write_bytes(b'fixture original bytes')
                                if primary is not None:
                                    raise primary
                        except BaseException as error:
                            caught = error
            self.assertTrue(ancestor.bulk.exists())
            self.assertTrue(outer.bulk.exists())
            self.assertEqual(b'fixture original bytes', (owner.bulk / 'original').read_bytes())
            self.assertTrue(owner.unknown)
            self.assertTrue(outer.unknown)
            self.assertTrue(ancestor.unknown)
            self.assertIs(caught, primary if body_failure else secondary)
            if body_failure:
                self.assertIs(caught.__cause__, secondary)

    def test_nested_evidence_off_refusal_checkpoint_failure_retains_original_bytes(self):
        self._nested_refusal_checkpoint_failure(False)

    def test_nested_evidence_off_refusal_checkpoint_failure_preserves_body_error(self):
        self._nested_refusal_checkpoint_failure(True)

    def _incomplete_child_at_predelete_entry(self, body_failure):
        primary = ValueError('fixture body failure') if body_failure else None
        secondary = OSError('fixture first predelete checkpoint failure')
        evidence = self.parent / 'evidence'
        evidence.mkdir(mode=0o700)
        caught = None
        entries = []
        checkpoint = rehearse.NativeRetention.checkpoint
        with mock.patch.dict(os.environ, {'BRAIN_REHEARSAL_EVIDENCE_DIR': str(evidence)}):
            with rehearse_external.OwnedCopies(parent=self.parent) as ancestor:
                with rehearse_external.OwnedCopies(parent=ancestor.bulk) as outer:
                    owner = rehearse_external.OwnedCopies(parent=outer.bulk)
                    def fail_first_predelete(retention, **fields):
                        if retention.owned == owner.bulk and fields.get('stage') == 'before-native-delete':
                            entries.append((retention.incomplete, owner.unknown,
                                            outer.unknown, ancestor.unknown))
                            raise secondary
                        return checkpoint(retention, **fields)
                    with mock.patch.object(rehearse.NativeRetention, 'checkpoint', fail_first_predelete):
                        try:
                            with owner:
                                (owner.bulk / 'original').write_bytes(b'fixture original bytes')
                                child_path = owner.bulk / 'child'
                                child_path.mkdir(mode=0o700)
                                evidence.chmod(0o755)
                                try:
                                    child = rehearse.NativeRetention(child_path, 'fixture-child')
                                finally:
                                    evidence.chmod(0o700)
                                self.assertTrue(child.incomplete)
                                self.assertIn(child, rehearse._retention_children.get())
                                self.assertTrue(list(child_path.glob('unsaved-native-retention-*.json')))
                                if primary is not None:
                                    raise primary
                        except BaseException as error:
                            caught = error
            self.assertTrue(ancestor.bulk.exists())
            self.assertTrue(outer.bulk.exists())
            self.assertEqual(b'fixture original bytes', (owner.bulk / 'original').read_bytes())
            self.assertTrue(list(child_path.glob('unsaved-native-retention-*.json')))
            self.assertTrue(owner.unknown)
            self.assertTrue(outer.unknown)
            self.assertTrue(ancestor.unknown)
            self.assertIs(caught, primary if body_failure else secondary)
            if body_failure:
                self.assertIs(caught.__cause__, secondary)
            self.assertEqual([(True, True, True, True)], entries)

    def test_incomplete_child_marks_ancestors_at_first_predelete_checkpoint_entry(self):
        self._incomplete_child_at_predelete_entry(False)

    def test_incomplete_child_predelete_failure_preserves_body_error(self):
        self._incomplete_child_at_predelete_entry(True)

    def test_linked_parent_refuses_before_allocation(self):
        link = self.parent / 'alias'
        link.symlink_to(self.parent, target_is_directory=True)
        with self.assertRaises(ValueError):
            rehearse_external.OwnedCopies(parent=link)
        self.assertEqual([link], list(self.parent.iterdir()))

    def test_owned_subprocess_timeout_and_cancel_reap_before_cleanup(self):
        for error in (subprocess.TimeoutExpired(['private-canary'], 1), KeyboardInterrupt()):
            owner = rehearse_external.OwnedCopies(parent=self.parent)
            process = mock.Mock(pid=424242, returncode=-9)
            process.communicate.side_effect = error
            process.poll.return_value = -9
            kill_sent = False
            def kill_owned_group(pgid, sig):
                nonlocal kill_sent
                self.assertEqual(process.pid, pgid)
                if not kill_sent:
                    self.assertEqual(rehearse_external.signal.SIGKILL, sig)
                    kill_sent = True
                    return
                self.assertEqual(0, sig)
                raise ProcessLookupError()
            with mock.patch.object(rehearse_external.subprocess, 'Popen', return_value=process) as popen, \
                    mock.patch.object(rehearse_external.os, 'killpg', side_effect=kill_owned_group):
                with self.assertRaises(type(error)), owner:
                    rehearse_external.owned_run(['private-canary'], capture_output=True, timeout=1)
            self.assertTrue(popen.call_args.kwargs['start_new_session'])
            process.wait.assert_called_once()
            self.assertFalse(owner.bulk.exists())
            self.assertNotIn('private-canary', (owner.root / 'cleanup.json').read_text())

    def test_owned_subprocess_success_failure_and_denied_stop(self):
        for code in (0, 1):
            owner = rehearse_external.OwnedCopies(parent=self.parent)
            process = mock.Mock(pid=424242, returncode=code)
            process.communicate.return_value = (b'private-canary', b'')
            process.poll.return_value = code
            kill_sent = False
            def kill_owned_group(pgid, sig):
                nonlocal kill_sent
                self.assertEqual(process.pid, pgid)
                if not kill_sent:
                    self.assertEqual(rehearse_external.signal.SIGKILL, sig)
                    kill_sent = True
                    return
                self.assertEqual(0, sig)
                raise ProcessLookupError()
            with mock.patch.object(rehearse_external.subprocess, 'Popen', return_value=process), \
                    mock.patch.object(rehearse_external.os, 'killpg', side_effect=kill_owned_group):
                with owner:
                    result = rehearse_external.owned_run(['example'], capture_output=True)
                    self.assertEqual(code, result.returncode)
            self.assertFalse(owner.bulk.exists())
        denied = rehearse_external.OwnedCopies(parent=self.parent)
        with mock.patch.object(rehearse_external.subprocess, 'Popen', return_value=process), \
                mock.patch.object(rehearse_external.os, 'killpg', side_effect=PermissionError) as kill:
            with self.assertRaises(rehearse.RehearsalError), denied:
                rehearse_external.owned_run(['example'], capture_output=True)
        kill.assert_called_once()
        self.assertTrue(denied.bulk.exists())

    def test_failed_evidence_sync_keeps_bulk_and_blocks_outer_cleanup(self):
        with rehearse_external.OwnedCopies(parent=self.parent) as outer:
            inner = rehearse_external.OwnedCopies(parent=outer.bulk)
            with mock.patch.object(inner, 'publish', side_effect=OSError('sync failed')):
                with self.assertRaises(OSError), inner:
                    (inner.bulk / 'keep').touch()
        self.assertTrue(inner.bulk.exists())
        self.assertTrue(outer.bulk.exists())

    def test_term_cancellation_unwinds_cleanup_and_restores_previous_handler(self):
        previous = mock.Mock()
        owner = rehearse_external.OwnedCopies(parent=self.parent)
        with mock.patch.object(rehearse_external.signal, 'getsignal', return_value=previous), \
                mock.patch.object(rehearse_external.signal, 'signal') as install:
            with self.assertRaises(KeyboardInterrupt), owner:
                handler = install.call_args.args[1]
                handler(rehearse_external.signal.SIGTERM, None)
        install.assert_called_with(rehearse_external.signal.SIGTERM, previous)
        self.assertFalse(owner.bulk.exists())


class DeclaredExternalSnapshot(Scenario):
    temporary_parent = CORE.parent
    def test_provider_installation_routes_are_excluded_without_waiving_link_guard(self):
        root = self.init('provider-routes')
        provider = self.tmp / 'provider-config'
        provider.mkdir()
        outside = self.tmp / 'outside'
        outside.mkdir()
        (provider / 'linked-directory').symlink_to(outside, target_is_directory=True)
        local_file = root / '.brain/local.json'
        local = copy.deepcopy(instance.Instance(root).local)
        local['global'] = {'codex': [str(provider)]}
        local_file.write_text(json.dumps(local))
        original = local_file.read_bytes()
        inst = instance.Instance(root)
        with self.assertRaises(rehearse.RehearsalError):
            rehearse.external_inputs(inst)
        with mock.patch.object(rehearse, 'copy_tree', side_effect=AssertionError('provider copied')):
            captured = rehearse_external.without_provider_bindings(inst)
        self.assertNotIn('global', captured.local)
        self.assertEqual(local['nodes'], captured.local['nodes'])
        self.assertTrue(rehearse.external_inputs(captured))
        self.assertEqual(original, local_file.read_bytes())
        self.assertEqual(local, inst.local)
        with self.assertRaises(rehearse.RehearsalError):
            rehearse.external_inputs(inst)
        inst.local['global'] = []
        with self.assertRaises(ValueError):
            rehearse_external.without_provider_bindings(inst)

    def test_comparison_uses_captured_operands_during_peer_publication(self):
        inst, home, tree = self.fixture(persistent=True)
        (home / 'bin').mkdir()
        (home / 'bin/brain').write_text('fixture')
        self.git('add', '.', cwd=home)
        self.git('commit', '-qm', 'candidate entry', cwd=home)
        head = self.git('rev-parse', 'HEAD', cwd=home).strip()
        bench = Path(inst.node('app')['local']['workbench'])
        inst.local['rehearse_external']['roots'] = [str(home), str(bench)]
        (inst.root / '.brain/local.json').write_text(json.dumps(inst.local))
        original = rehearse.snapshot(inst.root)
        observed = []
        selected = rehearse.rehearsal_copy_parent(inst, inst.node('app'))
        self.assertEqual(bench.parent / (bench.name + '-rehearsal-copies'), selected)
        self.assertNotIn(bench, selected.parents)
        def sandbox(folder):
            self.assertEqual(selected.resolve(), folder.parent.parent)
        def lane(source, core, folder, nodes, commands, **kwargs):
            captured = instance.Instance(source).node('app')
            observed.append(rehearse.snapshot(captured['home_path']))
            self.assertNotEqual(captured['home_path'], home)
            self.assertNotEqual(captured['local']['workbench'], str(bench))
            (home / 'README.md').write_text('peer publication ' + str(len(observed)))
            (bench / 'peer-report').write_text('peer report ' + str(len(observed)))
            return {'hook': dict(text='', code=0, timeout=False, unexpected=0, seconds=.01)}
        with mock.patch.dict('os.environ', self.env), \
                mock.patch.object(rehearse, 'sandbox_self_check', side_effect=sandbox), \
                mock.patch.object(rehearse, 'sandbox_prefix', return_value=[]), \
                mock.patch.object(rehearse, 'probe_delivery', return_value=[]), \
                mock.patch.object(rehearse._m_verify, 'ready_refs', return_value=(head, head)), \
                mock.patch.object(rehearse._m_verify, 'integration_parent', return_value=head), \
                mock.patch.object(rehearse, 'run_lane', side_effect=lane):
            evidence = rehearse.rehearse(inst, inst.node('app'), 'job/sample')
        self.assertTrue(evidence['passed'], evidence['regressions'])
        self.assertEqual(2, len(observed))
        self.assertEqual(observed[0], observed[1])
        self.assertEqual(original, rehearse.snapshot(inst.root))

    def test_capture_never_rewrites_source_and_retains_real_instance(self):
        inst, home, tree = self.fixture()
        original = rehearse.snapshot(inst.root)
        bindings = copy.deepcopy(inst.local)
        captured = rehearse_external.freeze_declared(inst, self.tmp / 'captured')
        self.assertEqual(original, rehearse.snapshot(inst.root))
        self.assertEqual(bindings, inst.local)
        self.assertIsInstance(captured, instance.Instance)
        self.assertIsNot(captured, inst)
        self.assertNotEqual(captured.node('app')['home_path'], home)
        self.assertNotEqual(captured.local['nodes']['app']['worktrees']['job/sample']['path'], str(tree))
    def test_declared_clone_scope_removes_success_and_partial_failure_copies(self):
        inst, home, tree = self.fixture()
        original = rehearse.snapshot(home)
        with rehearse_external.OwnedCopies(parent=self.tmp) as owner:
            private = rehearse_external.freeze_declared(inst, owner.bulk / 'captured')
            self.assertTrue(private.node('app')['home_path'].is_dir())
        self.assertFalse(owner.bulk.exists())
        def fail(source, target):
            target.mkdir()
            (target / 'partial').write_text('fabricated private bytes')
            raise OSError('fabricated copy failure')
        with rehearse_external.OwnedCopies(parent=self.tmp) as failed:
            with mock.patch.object(rehearse, 'copy_tree', side_effect=fail), self.assertRaises(OSError):
                rehearse_external.freeze_declared(inst, failed.bulk / 'captured')
        self.assertFalse(failed.bulk.exists())
        self.assertEqual(original, rehearse.snapshot(home))

    def fixture(self, persistent=False):
        root = self.init('demo')
        base = self.tmp
        if persistent:
            from test_rehearse import persistent_fixture_directory
            base = persistent_fixture_directory(self)
        home = self.repo(base / 'source')
        self.b(root, 'new', 'app', '--title', 'App', '--home', str(home))
        self.b(root, 'wt', 'add', 'app', 'job/sample', '--no-setup')
        inst = instance.Instance(root)
        tree = Path(inst.node('app')['local']['worktrees']['job/sample']['path'])
        data = inst.local
        data['rehearse_external'] = {'version': 1, 'roots': [str(home), str(tree)]}
        (root / '.brain/local.json').write_text(json.dumps(data))
        return instance.Instance(root), home, tree

    def test_declared_home_and_worktree_survive_peer_writes_with_detached_git(self):
        inst, home, tree = self.fixture()
        (tree / 'dirty.txt').write_text('captured dirty bytes')
        before = rehearse.snapshot(home)
        private = rehearse_external.freeze_declared(inst, self.tmp / 'captured')
        node = private.node('app')
        captured_tree = Path(node['local']['worktrees']['job/sample']['path'])
        self.assertNotEqual(node['home_path'], home)
        self.assertEqual((captured_tree / 'dirty.txt').read_text(), 'captured dirty bytes')
        self.assertTrue((captured_tree / '.git').is_dir())
        self.assertEqual(before, rehearse.snapshot(home))
        captured = rehearse.snapshot(self.tmp / 'captured')
        (tree / 'dirty.txt').write_text('peer edit')
        self.git('commit', '--allow-empty', '-qm', 'peer commit', cwd=tree)
        self.assertEqual(captured, rehearse.snapshot(self.tmp / 'captured'))
        self.assertEqual(private.local['nodes']['app']['home'], str(self.tmp / 'captured/0'))
        self.assertEqual(private.local['nodes']['app']['workbench'], str(home) + '-work')

    def test_unknown_overlapping_and_linked_roots_refuse_without_copy(self):
        inst, home, tree = self.fixture()
        link = self.tmp / 'linked'; link.symlink_to(home, target_is_directory=True)
        for roots in ([str(self.tmp)], [str(home), str(home)], [str(link)], [str(home / 'missing')]):
            inst.local['rehearse_external']['roots'] = roots
            with self.subTest(roots=roots), self.assertRaises(rehearse.RehearsalError):
                rehearse_external.freeze_declared(inst, self.tmp / 'refused')
            self.assertFalse((self.tmp / 'refused').exists())

    def test_absent_contract_preserves_observation_mode(self):
        root = self.init('demo')
        inst = instance.Instance(root)
        self.assertIs(rehearse_external.freeze_declared(inst, self.tmp / 'unused'), inst)
        self.assertFalse((self.tmp / 'unused').exists())

    def test_declared_workbench_sanitizes_nested_worktree_configuration(self):
        inst, home, tree = self.fixture()
        bench = Path(inst.node('app')['local']['workbench'])
        self.git('config', 'include.path', str(self.tmp / 'external-config'), cwd=tree)
        inst.local['rehearse_external']['roots'] = [str(bench)]
        private = rehearse_external.freeze_declared(inst, self.tmp / 'captured')
        copied = Path(private.node('app')['local']['worktrees']['job/sample']['path'])
        config = (copied / '.git/config').read_text()
        self.assertNotIn('include', config)
        self.assertIn('hooksPath = /dev/null', config)
        self.assertFalse((copied / '.git/commondir').exists())
