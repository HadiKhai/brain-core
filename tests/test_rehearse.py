"""Fabricated rehearsal comparisons and real sandbox mutation checks."""
import ast
import contextlib
import datetime
import errno
import hashlib
import importlib
import io
import json
import logging
import signal
import shutil
import stat
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import traceback
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from brain import rehearse as rehearsal
from scenario import Scenario, CORE


def result(text='', code=0, seconds=0.01, unexpected=0):
    return dict(text=text, code=code, seconds=seconds, unexpected=unexpected, timeout=False)


class RehearsalRetention(unittest.TestCase):
    """Failure injection stops before acceptance work; only retention is exercised."""
    def setUp(self):
        raw_directory = Path(tempfile.mkdtemp(prefix='rehearsal-retention-'))
        self.directory = raw_directory.resolve(strict=True)
        self.addCleanup(shutil.rmtree, self.directory)
        destination = os.environ.get('BRAIN_REHEARSAL_EVIDENCE_DIR')
        if destination:
            raw_evidence = Path(tempfile.mkdtemp(prefix='retention-test-', dir=destination))
        else:
            raw_evidence = raw_directory / 'evidence'
            raw_evidence.mkdir(mode=0o700)
        # Only these freshly created, owned positive fixtures are canonicalized.
        # Caller-supplied invalid destinations in the negative controls stay raw.
        self.evidence = raw_evidence.resolve(strict=True)
        observations = dict(nodeid=self.id(), directories={})
        for name, raw, canonical in (
                ('temporary', raw_directory, self.directory),
                ('evidence', raw_evidence, self.evidence)):
            spellings = {}
            for spelling, path in (('raw', raw), ('canonical', canonical)):
                info, link_info = path.stat(), path.lstat()
                spellings[spelling] = dict(
                    path=str(path), uid=info.st_uid, mode=stat.S_IMODE(info.st_mode),
                    device=info.st_dev, inode=info.st_ino,
                    is_directory=stat.S_ISDIR(info.st_mode),
                    lstat_device=link_info.st_dev, lstat_inode=link_info.st_ino,
                    lstat_mode=stat.S_IMODE(link_info.st_mode),
                    ancestry=[dict(path=str(parent), symlink=parent.is_symlink())
                              for parent in (path, *path.parents)])
            observations['directories'][name] = spellings
        # This sidecar is separate from native *.json packets, including off
        # controls. With an external evidence destination it survives cleanup.
        fd = os.open(str(self.evidence / 'fixture-paths.private'),
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(observations, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        for spellings in observations['directories'].values():
            raw, canonical = spellings['raw'], spellings['canonical']
            self.assertEqual((raw['device'], raw['inode']),
                             (canonical['device'], canonical['inode']))
            self.assertEqual(os.getuid(), canonical['uid'])
            self.assertEqual(0o700, canonical['mode'])
            self.assertTrue(canonical['is_directory'])
            self.assertFalse(any(parent['symlink'] for parent in canonical['ancestry']))
        self.environment = mock.patch.dict(os.environ, {
            'BRAIN_REHEARSAL_EVIDENCE_DIR': str(self.evidence)})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def packets(self):
        return [json.loads(p.read_text()) for p in sorted(self.evidence.glob('*.json'))]

    def fixture(self, cls):
        case = cls('runTest')
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.addCleanup(case.tearDown)
        return case

    def test_optional_setup_exception_is_retained_before_fixture_delete(self):
        from nightly_rehearse import RehearsalNativeCore
        case = self.fixture(RehearsalNativeCore)
        error = AssertionError('sample early optional setup')
        with mock.patch.object(case, 'init', side_effect=error):
            with self.assertRaises(AssertionError) as caught:
                case.test_real_core_cli_clean_candidate_and_byte_identical_source()
        self.assertIs(error, caught.exception)
        case.tearDown()
        packets = self.packets()
        self.assertTrue(packets, 'optional setup lost its partial packet')
        self.assertTrue(any(p.get('exception', {}).get('category') == 'AssertionError' for p in packets))
        self.assertFalse(case.tmp.exists())

    def test_helper_initialization_exception_is_retained(self):
        case = self.fixture(RehearsalSandbox)
        error = AssertionError('sample helper setup')
        with mock.patch.object(case, 'init', side_effect=error):
            with self.assertRaises(AssertionError) as caught:
                case.native_transaction_evidence()
        self.assertIs(error, caught.exception)
        self.assertTrue(self.packets(), 'helper entry did not initialize retention')
        self.assertTrue(any(p.get('exception', {}).get('category') == 'AssertionError'
                            for p in self.packets()))

    def test_native_capture_exception_retains_partial_operands(self):
        lane = self.directory / 'lane'
        (lane / 'brain').mkdir(parents=True)
        error = ValueError('sample capture failure')
        with mock.patch.object(rehearsal._m_transactions, 'capture', side_effect=error):
            with self.assertRaises(ValueError) as caught:
                rehearsal.run_command(lane / 'brain/core', lane, [], 'fix', [])
        self.assertIs(error, caught.exception)
        self.assertTrue(self.packets(), 'capture exception bypassed native retention')
        packet = next(p for p in reversed(self.packets()) if p.get('exception'))
        self.assertEqual('UNKNOWN', packet['before'])
        self.assertEqual('UNKNOWN', packet['after'])

    def command(self, stage):
        lane = self.directory / stage
        (lane / 'brain').mkdir(parents=True)
        process = mock.Mock(returncode=0, pid=54321)
        process.communicate.return_value = ('sample output', '')
        before, after, observed = {'sample': 'before'}, {'sample': 'after'}, []
        error = AssertionError('sample ' + stage)
        retention = rehearsal.NativeRetention(lane, 'before-command-setup',
            before='UNKNOWN', after='UNKNOWN', observations='UNKNOWN',
            validation_instant='UNKNOWN', validator_result='UNKNOWN', raw_trace_hex='UNKNOWN')
        capture = [before, error] if stage == 'capture-after' else [before, after]
        validator = mock.Mock(side_effect=error) if stage == 'validator' else mock.Mock(return_value=(set(), 'unproven'))
        parser = mock.Mock(side_effect=ValueError('sample parse')) if stage == 'parse' else mock.Mock(return_value=observed)
        with mock.patch.object(rehearsal, 'NativeRetention', return_value=retention), \
                mock.patch.object(rehearsal, 'sandbox_prefix', return_value=[]), \
                mock.patch.object(rehearsal.subprocess, 'Popen', return_value=process), \
                mock.patch.object(rehearsal.os, 'killpg'), \
                mock.patch.object(rehearsal._m_transactions, 'capture', side_effect=capture), \
                mock.patch.object(rehearsal._m_transactions, 'lines', parser), \
                mock.patch.object(rehearsal._m_transactions, 'validate', validator):
            if stage in ('capture-after', 'validator'):
                with self.assertRaises(AssertionError) as caught:
                    rehearsal.run_command(lane / 'brain/core', lane, [], 'fix', [])
                self.assertIs(error, caught.exception)
            else:
                rehearsal.run_command(lane / 'brain/core', lane, [], 'fix', [])
        return before, after, validator

    def test_after_capture_failure_retains_before_and_actual_trace(self):
        before, _, validator = self.command('capture-after')
        packets = self.packets()
        packet = next(p for p in packets if p.get('stage') == 'command-finally')
        self.assertEqual(before, packet['before'])
        self.assertEqual('UNKNOWN', packet['after'])
        self.assertEqual('', packet['command_finally_trace_hex'])
        validator.assert_not_called()

    def test_parse_failure_retains_exact_parse_input_and_actual_validator_operands(self):
        before, after, validator = self.command('parse')
        packet = next(p for p in self.packets() if p.get('stage') == 'validator-finally')
        self.assertEqual(before, packet['before'])
        self.assertEqual(after, packet['after'])
        self.assertEqual('', packet['raw_trace_hex'])
        self.assertIsNone(packet['observations'])
        self.assertIsNone(validator.call_args.args[2])
        self.assertEqual(validator.call_args.args[5].isoformat(), packet['validation_instant'])
        self.assertEqual('unproven', packet['validator_result']['verdict'])

    def test_validator_exception_retains_actual_call_and_finally_diagnostics(self):
        before, after, validator = self.command('validator')
        packet = next(p for p in self.packets() if p.get('stage') == 'validator-finally')
        self.assertEqual((before, after, [], 54321, 'check'), validator.call_args.args[:5])
        self.assertEqual(validator.call_args.args[5].isoformat(), packet['validation_instant'])
        self.assertEqual('UNKNOWN', packet['validator_result'])
        self.assertEqual('AssertionError', next(p for p in self.packets()
                         if p.get('stage') == 'command-finally')['exception']['category'])

    def test_persistence_failure_is_sticky_and_preserves_fixture_before_delete(self):
        from nightly_rehearse import RehearsalNativeCore
        case = self.fixture(RehearsalNativeCore)
        self.addCleanup(shutil.rmtree, case.tmp, True)
        invalid = self.directory / 'not-a-directory'
        invalid.write_text('sample')
        case._native_retention.destination = str(invalid)
        error = AssertionError('sample original exception')
        with mock.patch.object(case, 'init', side_effect=error):
            with self.assertRaises(AssertionError) as caught:
                case.test_real_core_cli_clean_candidate_and_byte_identical_source()
        self.assertIs(error, caught.exception)
        case.tearDown()
        self.assertTrue(case.tmp.exists())
        self.assertTrue(list(case.tmp.glob('unsaved-native-retention-*.json')))
        case._native_retention.destination = str(self.evidence)
        self.assertFalse(case.retain_native_evidence())
        self.assertEqual('INCOMPLETE', case._native_evidence['status'])

    def native_setup_failure(self, persistence_failure=False, off=False):
        from types import SimpleNamespace
        folders = []
        error = AssertionError('sample native setup')
        def setup(folder):
            folders.append(folder)
            (folder / 'sole-operand').write_bytes(b'sample operand')
            raise error
        invalid = self.directory / 'invalid-destination'
        invalid.write_text('sample')
        target = '' if off else str(invalid if persistence_failure else self.evidence)
        with mock.patch.dict(os.environ, {'BRAIN_REHEARSAL_EVIDENCE_DIR': target}), \
                mock.patch.object(rehearsal._m_verify, 'ready_refs', return_value=('a', 'b')), \
                mock.patch.object(rehearsal._m_verify, 'integration_parent', return_value='b'), \
                mock.patch.object(rehearsal, 'sandbox_self_check', side_effect=setup):
            with self.assertRaises(AssertionError) as caught:
                rehearsal.rehearse(SimpleNamespace(), dict(external=True), 'sample')
        self.assertIs(error, caught.exception)
        return folders[0]

    def test_native_temporary_directory_preserves_unsaved_operands_before_delete(self):
        folder = self.native_setup_failure(persistence_failure=True)
        self.addCleanup(shutil.rmtree, folder, True)
        self.assertEqual(b'sample operand', (folder / 'sole-operand').read_bytes())
        self.assertTrue(list(folder.glob('unsaved-native-retention-*.json')))

    def test_native_early_exception_persists_then_cleans_owned_temporary_directory(self):
        folder = self.native_setup_failure()
        self.assertFalse(folder.exists())
        self.assertTrue(any(p.get('stage') == 'before-native-delete' and
                            p.get('exception', {}).get('category') == 'AssertionError'
                            for p in self.packets()))

    def test_evidence_off_keeps_fixture_and_native_cleanup_unchanged(self):
        from nightly_rehearse import RehearsalNativeCore
        with mock.patch.dict(os.environ, {'BRAIN_REHEARSAL_EVIDENCE_DIR': ''}):
            case = self.fixture(RehearsalNativeCore)
            with mock.patch.object(case, 'init', side_effect=AssertionError('sample off')):
                with self.assertRaises(AssertionError):
                    case.test_real_core_cli_clean_candidate_and_byte_identical_source()
            case.tearDown()
            self.assertFalse(case.tmp.exists())
            self.assertFalse(hasattr(case, '_native_evidence'))
            self.assertFalse(self.native_setup_failure(off=True).exists())
        self.assertEqual([], self.packets())

    def test_private_artifacts_bind_source_hashes_and_modes(self):
        self.test_native_capture_exception_retains_partial_operands()
        paths = list(self.evidence.glob('*.json'))
        self.assertTrue(paths)
        for path in paths:
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
            packet = json.loads(path.read_text())
            for name, digest in packet['source_binding']['source_hashes'].items():
                self.assertEqual(hashlib.sha256((CORE / name).read_bytes()).hexdigest(), digest)

    def test_direct_validator_calls_retain_exact_inputs_and_results_before_assertions(self):
        from brain import rehearse_transactions as transactions
        case = self.fixture(RehearsalSandbox)
        instant = datetime.datetime(2026, 1, 2, tzinfo=datetime.timezone.utc)
        for pid, command in ((12345, 'check'), (23456, 'status')):
            diagnostics = {}
            actual = transactions.validate(None, None, [], pid, command, instant,
                                           diagnostics=diagnostics)
            packets = [p for p in self.packets() if p.get('stage') == 'validation-call-finally']
            packet = next(p for p in packets if p['owner_pid'] == pid)
            self.assertIsNone(packet['before'])
            self.assertIsNone(packet['after'])
            self.assertEqual([], packet['observations'])
            self.assertEqual(command, packet['validator_command'])
            self.assertEqual(instant.isoformat(), packet['validation_instant'])
            self.assertEqual(diagnostics, packet['diagnostics'])
            self.assertEqual(dict(proven_paths=sorted(actual[0]), verdict=actual[1]),
                             packet['validator_result'])
            self.assertEqual(case.id(), packet['context']['nodeid'])
            self.assertEqual('test_direct_validator_calls_retain_exact_inputs_and_results_before_assertions',
                             packet['caller']['function'])
        self.assertEqual(2, len(packets))

    def test_direct_validator_exception_keeps_final_diagnostics_and_original_exception(self):
        from brain import rehearse_transactions as transactions
        self.fixture(RehearsalSandbox)
        error = RuntimeError('sample validator failure')
        def fail(*args):
            args[-1]['checkpoint'] = 'sample-final-checkpoint'
            raise error
        with mock.patch.object(transactions, '_validate', side_effect=fail):
            with self.assertRaises(RuntimeError) as caught:
                transactions.validate(None, None, [], 12345, 'check',
                                      datetime.datetime.now(datetime.timezone.utc))
        self.assertIs(error, caught.exception)
        packet = next(p for p in self.packets() if p.get('stage') == 'validation-call-finally')
        self.assertEqual('sample-final-checkpoint', packet['diagnostics']['checkpoint'])
        self.assertEqual('UNKNOWN', packet['validator_result'])
        self.assertEqual('RuntimeError', packet['exception']['category'])

    def test_nested_native_retention_preserves_only_owned_unsaved_tree(self):
        invalid = self.directory / 'invalid-nested-destination'
        invalid.write_text('sample')
        foreign = self.directory / 'foreign'
        foreign.mkdir()
        with rehearsal.retained_temporary_directory() as outer:
            with rehearsal.retained_temporary_directory() as inner:
                child = rehearsal.NativeRetention(Path(inner) / 'lane', 'sample')
                child.owned.mkdir()
                child.destination = str(invalid)
                self.assertFalse(child.checkpoint())
        self.addCleanup(shutil.rmtree, outer, True)
        self.addCleanup(shutil.rmtree, inner, True)
        self.assertTrue(Path(inner).exists())
        self.assertFalse(Path(outer).exists())
        self.assertTrue(foreign.exists())

    def test_validator_off_keeps_result_and_creates_no_packets(self):
        from brain import rehearse_transactions as transactions
        with mock.patch.dict(os.environ, {'BRAIN_REHEARSAL_EVIDENCE_DIR': ''}):
            self.fixture(RehearsalSandbox)
            self.assertEqual((set(), 'unproven'), transactions.validate(
                None, None, [], 12345, 'check', datetime.datetime.now(datetime.timezone.utc)))
        self.assertEqual([], self.packets())


class RehearsalComparison(unittest.TestCase):
    def test_ui_json_full_snapshot_capability_is_read_from_each_core(self):
        with tempfile.TemporaryDirectory(prefix='ui-capability-') as directory:
            core = Path(directory)
            (core / 'bin').mkdir()
            (core / 'bin/brain').write_text('# fabricated legacy CLI\n')
            args = ['ui', '--json', '--wait']
            self.assertEqual(rehearsal.rehearsal_ui_json_args(core, args), ['ui', '--json'])
            self.assertEqual(args, ['ui', '--json', '--wait'])
            marker = core / 'lib/brain/ui_json.py'
            marker.parent.mkdir(parents=True)
            marker.write_text('UI_JSON_WAIT = 1\n')
            self.assertEqual(rehearsal.rehearsal_ui_json_args(core, args), args)
            for text in ('', 'UI_JSON_WAIT = True\n', 'UI_JSON_WAIT = unknown\n',
                         'UI_JSON_WAIT = 1\nUI_JSON_WAIT = 0\n'):
                marker.write_text(text)
                with self.assertRaisesRegex(rehearsal.RehearsalError, 'capability ambiguous'):
                    rehearsal.rehearsal_ui_json_args(core, args)
            marker.unlink()
            marker.symlink_to(core / 'bin/brain')
            with self.assertRaises(rehearsal.RehearsalError):
                rehearsal.rehearsal_ui_json_args(core, args)


    def test_ui_crash_after_partial_output_is_baseline_incomplete(self):
        complete = '{"nodes":[{"id":"engine","worktrees":[]}]}'
        candidate = result(complete)
        for code, timeout, text in [(-9, False, '{"nodes":['),
                                    (1, False, complete), (-9, False, complete),
                                    (0, True, complete), (0, False, '{"nodes":['),
                                    (0, False, 'not JSON'), (0, False, '[]'),
                                    (0, False, '{"nodes":"invalid"}')]:
            with self.subTest(code=code, timeout=timeout, text=text):
                baseline = dict(result(text, code=code), timeout=timeout)
                self.assertEqual(['ui: baseline incomplete'],
                                 rehearsal.compare({'ui': baseline}, {'ui': candidate}))

    def test_json_errors_compare_individual_lines_and_require_complete_baseline(self):
        old = result(json.dumps({'inbox': ['old report\n✗ historical failure'], 'count': 1}, ensure_ascii=False))
        new = result(json.dumps({'inbox': ['longer old report\n✗ historical failure'], 'count': 2}, ensure_ascii=False))
        self.assertEqual([], rehearsal.added_check_errors(old, new))
        new['text'] = json.dumps({'inbox': ['✗ historical failure\n✗ actual new failure'], 'count': 2}, ensure_ascii=False)
        self.assertEqual(['✗ actual new failure'], rehearsal.added_check_errors(old, new))
        duplicate = result(json.dumps({'inbox': ['✗ historical failure', '✗ historical failure']}, ensure_ascii=False))
        self.assertEqual(['✗ historical failure'], rehearsal.added_check_errors(old, duplicate))
        for field, value in [('timeout', True), ('text', '')]:
            baseline = dict(old, **{field: value})
            flags = rehearsal.compare({'ui': baseline}, {'ui': new})
            self.assertEqual(['ui: baseline incomplete'], flags)

    def test_stderr_only_check_failure_is_a_completed_operand_and_safety_still_fails(self):
        old = dict(result('✗ old failure', code=1), semantic_stdout='', semantic_stderr='✗ old failure')
        new = dict(result('✗ new failure', code=1), semantic_stdout='', semantic_stderr='✗ new failure')
        self.assertIn('check: new check errors', rehearsal.compare({'check': old}, {'check': new}))
        old['timeout'], new['unexpected'] = True, 1
        self.assertEqual(['check: unexpected writes', 'check: baseline incomplete'],
                         rehearsal.compare({'check': old}, {'check': new}))

    def test_clean_candidate_and_normalization(self):
        self.assertEqual([], rehearsal.compare({'check': result('✓ all good')}, {'check': result('✓ all good')}))
        self.assertEqual(rehearsal.normalize('{"b":2,"a":1}'), rehearsal.normalize('{"a":1,"b":2}'))
        self.assertEqual(rehearsal.normalize('/tmp/one 2026-01-02 03:04', ['/tmp/one']),
                         rehearsal.normalize('/tmp/two 2026-02-03 04:05', ['/tmp/two']))

    def test_all_four_incident_classes_and_hook(self):
        before = {'check': result('✓ good'), 'fix': result('✓ good'),
                  'ready:0:0': result('ready'), 'ready:1:0': result('ready'),
                  'due:0': result('[]'), 'due:1': result('[]'),
                  'brief:0': result('Stop when: ask for consent!'), 'hook': result()}
        after = {'check': result('✗ cannot heal', 1), 'fix': result('✗ cannot heal', 1),
                 'ready:0:0': result('not ready: main resolution', 1),
                 'ready:1:0': result('not ready: main resolution', 1),
                 'due:0': result('[{"kind":"load","text":"noise"}]'),
                 'due:1': result('[{"kind":"load","text":"noise"}]'),
                 'brief:0': result('quiet'), 'hook': result(seconds=1.1)}
        flags = rehearsal.compare(before, after)
        for flag in ('fix: new check errors', 'ready:0:0: ready became not ready',
                     'ready:1:0: ready became not ready', 'due:0: new due items',
                     'due:1: new due items', 'brief:0: brief warning disappeared', 'hook: latency exceeds 1 s'):
            self.assertIn(flag, flags)

    def test_additional_duplicate_errors_and_due_items_are_not_lost(self):
        for key, text in [('check', '✗ same'), ('due:0', '[{"kind":"a"}]')]:
            new = text + '\n' + text if key == 'check' else '[{"kind":"a"},{"kind":"a"}]'
            self.assertTrue(rehearsal.compare({key: result(text)}, {key: result(new)}))

    def test_unexpected_write_and_invalid_due_fail(self):
        self.assertIn('ui: unexpected writes', rehearsal.compare({'ui': result()}, {'ui': result(unexpected=1)}))
        self.assertIn('due:0: invalid due output', rehearsal.compare({'due:0': result('[]')}, {'due:0': result('oops')}))

    def test_sandbox_unavailable_refuses(self):
        with mock.patch.object(rehearsal.sys, 'platform', 'linux'):
            with self.assertRaises(rehearsal.RehearsalError):
                rehearsal.sandbox_prefix(Path('/tmp/fabricated'))


class RehearsalRedaction(Scenario):
    def test_public_refusal_identifies_reason_and_phase_without_source_content(self):
        from types import SimpleNamespace
        node = dict(id='engine', external=True, home_path=self.tmp / 'candidate')
        inst = SimpleNamespace(root=self.tmp / 'source', node=lambda _: node, nodes=lambda: [])
        error = PermissionError(1, 'invented-canary private path')
        with mock.patch.object(rehearsal._m_nodes, 'find_root', return_value=inst.root), \
                mock.patch.object(rehearsal._m_instance, 'Instance', return_value=inst), \
                mock.patch.object(rehearsal, 'evidence_path', return_value=self.tmp / 'evidence.json'), \
                mock.patch.object(rehearsal, 'rehearse', side_effect=error):
            with self.assertRaises(SystemExit) as caught:
                rehearsal.cmd_rehearse(SimpleNamespace(id='engine', branch='job'))
        self.assertIn('phase=rehearsal', str(caught.exception))
        self.assertIn('reason=PermissionError:EPERM', str(caught.exception))
        self.assertNotIn('invented-canary', str(caught.exception))
        with mock.patch.object(rehearsal._m_nodes, 'find_root', side_effect=error):
            with self.assertRaises(SystemExit) as caught:
                rehearsal.cmd_rehearse(SimpleNamespace(id='engine', branch='job'))
        self.assertIn('phase=instance', str(caught.exception))
        self.assertIn('reason=PermissionError:EPERM', str(caught.exception))
        self.assertNotIn('invented-canary', str(caught.exception))

    def test_ui_deadline_is_configurable_and_invalid_values_refuse_before_launch(self):
        with mock.patch.dict(os.environ, {'BRAIN_REHEARSE_UI_TIMEOUT': '123.5'}), \
                mock.patch.object(rehearsal.subprocess, 'Popen') as popen:
            process = popen.return_value
            process.returncode, process.pid = 0, 123
            process.communicate.return_value = ('{"nodes": []}', '')
            lane = self.tmp / 'deadline'
            (lane / 'brain').mkdir(parents=True)
            with mock.patch.object(rehearsal, 'sandbox_prefix', return_value=[]), \
                    mock.patch.object(rehearsal.os, 'killpg'):
                rehearsal.run_command(lane / 'brain/core', lane, ['ui', '--json'], 'read', [])
            self.assertEqual(123.5, process.communicate.call_args.kwargs['timeout'])
            for value in ('0', '-1', 'nan', 'inf', 'private-path'):
                with mock.patch.dict(os.environ, {'BRAIN_REHEARSE_UI_TIMEOUT': value}):
                    with self.assertRaisesRegex(rehearsal.RehearsalError, 'UI probe deadline'):
                        rehearsal.run_command(lane / 'brain/core', lane, ['ui', '--json'], 'read', [])
            self.assertEqual(1, popen.call_count)

    def probe(self, out, err='', code=0):
        lane = self.tmp / 'lane'
        (lane / 'brain').mkdir(parents=True, exist_ok=True)
        process = mock.Mock(returncode=code, pid=123)
        process.communicate.return_value = (out, err)
        with mock.patch.object(rehearsal, 'sandbox_prefix', return_value=[]), \
                mock.patch.object(rehearsal.subprocess, 'Popen', return_value=process), \
                mock.patch.object(rehearsal.os, 'killpg'):
            return rehearsal.run_command(lane / 'brain/core', lane, [], 'read', [],
                                         [('invented-canary', 'term'), ('Straße', 'term')])

    def test_stdout_stderr_redacted_before_comparison_and_digest(self):
        printed = io.StringIO()
        handler = logging.StreamHandler(printed)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        with contextlib.redirect_stdout(printed), contextlib.redirect_stderr(printed):
            old = self.probe('[{"text":"invented-canary","kind":"hold"}]')
            same = self.probe('[{"kind":"hold","text":"INVENTED-CANARY"}]')
            changed = self.probe('[{"text":"invented-canary","kind":"new"}]')
            error = self.probe('', '✗ invented-canary failed', code=1)
        self.assertEqual([], rehearsal.compare({'due:0': old}, {'due:0': same}))
        digest = lambda r: hashlib.sha256(r['text'].encode()).hexdigest()
        self.assertEqual(digest(old), digest(same))
        self.assertNotEqual(digest(old), digest(changed))
        self.assertIn('due:0: new due items', rehearsal.compare({'due:0': old}, {'due:0': changed}))
        self.assertIn('check: new check errors', rehearsal.compare({'check': result('✓ good')}, {'check': error}))
        self.assertEqual('', printed.getvalue())
        for r in (old, same, changed, error):
            self.assertNotIn('invented-canary', json.dumps(r).casefold())
            self.assertIn('[…]', r['text'])

    def test_unicode_folds_and_escaped_json_preserve_real_differences(self):
        old = self.probe(json.dumps([{'text': 'STRASSE', 'kind': 'old'}]))
        same = self.probe(json.dumps([{'text': 'Straße', 'kind': 'old'}]))
        changed = self.probe(json.dumps([{'text': 'Straße', 'kind': 'new'}]))
        self.assertEqual(old['text'], same['text'])
        self.assertIn('due:0: new due items', rehearsal.compare({'due:0': old}, {'due:0': changed}))
        self.assertNotIn('strasse', changed['text'].casefold())
        split = self.probe('invented-', 'canary')
        self.assertEqual('[…]', split['text'])

    def test_json_quoted_terms_are_masked_without_losing_structure(self):
        terms = [('fabricated-"cedar', 'one'), ('invented-\\willow', 'two')]
        text = json.dumps({'items': [term for term, _ in terms]})
        masked = rehearsal.redact_output(rehearsal.normalize(text), terms)
        self.assertEqual({'items': ['[…]', '[…]']}, json.loads(masked))

    def test_overlapping_terms_are_order_independent(self):
        terms = [('fabricated-cedar', 'one'), ('cedar-project', 'two')]
        for order in (terms, terms[::-1]):
            self.assertEqual('keep […] changed', rehearsal.redact_output('keep fabricated-cedar-project changed', order))

    def test_evidence_stdout_and_refusal_hide_probe_content(self):
        from types import SimpleNamespace
        node = dict(id='engine', external=True, home_path=self.tmp / 'candidate')
        inst = SimpleNamespace(root=self.tmp / 'source', node=lambda _: node, nodes=lambda: [node])
        path = self.tmp / 'evidence.json'
        probe = self.probe('[{"text":"invented-canary","kind":"hold"}]')
        evidence = dict(passed=True, commands=1, regressions=[],
                        output_digests={'due:0': hashlib.sha256(probe['text'].encode()).hexdigest()})
        args = SimpleNamespace(id='engine', branch='job')
        printed = io.StringIO()
        handler = logging.StreamHandler(printed)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        with mock.patch.object(rehearsal._m_nodes, 'find_root', return_value=inst.root), \
                mock.patch.object(rehearsal._m_instance, 'Instance', return_value=inst), \
                mock.patch.object(rehearsal, 'evidence_path', return_value=path), \
                mock.patch.object(rehearsal._m_guard, 'never_terms', return_value=[('invented-canary', 'term')]), \
                mock.patch.object(rehearsal, 'rehearse', return_value=evidence) as run, \
                contextlib.redirect_stdout(printed), contextlib.redirect_stderr(printed):
            rehearsal.cmd_rehearse(args)
            saved = path.read_text()
            self.assertEqual(evidence, json.loads(saved))
            evidence.update(passed=False, regressions=['check: raw failure'])
            with self.assertRaises(SystemExit) as failed:
                rehearsal.cmd_rehearse(args)
            self.assertEqual(1, failed.exception.code)
            self.assertEqual(evidence, json.loads(path.read_text()))
            self.assertIn('rehearse: failed; commands=1; regressions=1', printed.getvalue())
            self.assertIn('check: raw failure', printed.getvalue())
            run.side_effect = rehearsal.RehearsalError('invented-canary raw probe output')
            with self.assertRaises(SystemExit) as caught:
                rehearsal.cmd_rehearse(args)
            self.assertNotIn('invented-canary', str(caught.exception))
            self.assertIn('rehearsal refused or incomplete', str(caught.exception))
        self.assertFalse(path.exists())
        for text in (saved, printed.getvalue()):
            self.assertNotIn('invented-canary', text)
            self.assertNotIn('raw probe output', text)
        self.assertIn('rehearse: passed', printed.getvalue())


class RehearsalFixture(Scenario):
    def setUp(self):
        super().setUp()
        self.root = self.repo(self.tmp / 'live', {'note.md': 'original\n',
                                                '.gitignore': '.brain/\nignored/\n'})
        (self.root / 'core/bin').mkdir(parents=True)
        (self.root / 'core/bin/brain').write_text('fixture')
        self.git('add', 'core', cwd=self.root)
        self.git('commit', '-qm', 'core fixture', cwd=self.root)
        self.oid = self.git('rev-parse', 'HEAD', cwd=self.root).strip()
        self.home = self.repo(self.tmp / 'candidate', {'README.md': 'candidate'})
        (self.home / 'bin').mkdir()
        (self.home / 'bin/brain').write_text('fixture')
        self.git('add', 'bin', cwd=self.home)
        self.git('commit', '-qm', 'candidate fixture', cwd=self.home)
        self.head = self.git('rev-parse', 'HEAD', cwd=self.home).strip()

    def run_rehearsal(self, mutation=lambda: None, during_run=False, damage_copy=None,
                      outputs=None, inspect_lane=None, now=None, lane_runner=None, diagnostic_dir=None):
        from types import SimpleNamespace
        inst = SimpleNamespace(root=self.root, local={}, nodes=lambda: [],
                               now=lambda: '2035-01-03 00:00 +00:00',
                               now_dt=lambda: now or datetime.datetime(2035, 1, 3, tzinfo=datetime.timezone.utc))
        node = dict(external=True, home_path=self.home, id='engine',
                    dir=diagnostic_dir if diagnostic_dir is not None else self.root)
        copy = rehearsal.copy_tree
        def copying(source, dest):
            copy(source, dest)
            if source == self.root:
                if damage_copy is not None:
                    damage_copy(dest)
                if not during_run:
                    mutation()
        def lane(*args, **kwargs):
            if lane_runner:
                return lane_runner(*args, **kwargs)
            observed = inspect_lane(*args) if inspect_lane else None
            if during_run:
                mutation()
            if observed is not None:
                return observed
            return outputs.pop(0) if outputs is not None else {'hook': result()}
        with mock.patch.dict(os.environ, self.env), \
                mock.patch.object(rehearsal, 'sandbox_self_check'), \
                mock.patch.object(rehearsal, 'sandbox_prefix', return_value=[]), \
                mock.patch.object(rehearsal._m_verify, 'ready_refs', return_value=(self.head, self.head)), \
                mock.patch.object(rehearsal._m_verify, 'integration_parent', return_value=self.head), \
                mock.patch.object(rehearsal, '_m_instance', SimpleNamespace(Instance=lambda directory: inst)), \
                mock.patch.object(rehearsal, 'isolated_probe_instance', return_value=inst), \
                mock.patch.object(rehearsal, 'probe_delivery', return_value=[]), \
                mock.patch.object(rehearsal, 'copy_tree', side_effect=copying), \
                mock.patch.object(rehearsal, 'run_lane', side_effect=lane), \
                mock.patch.object(rehearsal._m_jobs, 'stamp', return_value='fixture'):
            return rehearsal.rehearse(inst, node, 'job')

    def write(self, rel, text='changed'):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


class RehearsalEperm(RehearsalFixture):
    def run_rehearsal(self, **kwargs):
        from brain import events, instance
        real_instance = instance.Instance
        diagnostic = self.init('diagnostic')
        evidence = super().run_rehearsal(diagnostic_dir=diagnostic, **kwargs)
        self.assertIs(instance.Instance, real_instance)
        native = instance.Instance(diagnostic)
        self.assertIsInstance(native, real_instance)
        inbox = (native.node('root')['dir'] / 'inbox.md').read_text()
        self.assertIn('pgid=43210; PIDs=43211,43212; EPERM; no further signals sent', inbox)
        self.assertIn('operator stop: kill -TERM -43210', inbox)
        self.assertTrue(any(row['kind'] == 'begin' and row['data'].get('command') == 'inbox'
                            for row in events.read(native)))
        return evidence

    def denied_lane(self, source, core, folder, nodes, commands, **kwargs):
        folder.mkdir()
        (folder / 'brain').mkdir()
        process = mock.Mock(pid=43210, returncode=0)
        process.poll.return_value = 0
        process.communicate.return_value = ('quiet', '')
        with mock.patch.object(rehearsal, 'sandbox_prefix', return_value=[]), \
                mock.patch.object(rehearsal.subprocess, 'Popen', return_value=process), \
                mock.patch.object(rehearsal, 'group_members', return_value=([43211, 43212], True)), \
                mock.patch.object(rehearsal.os, 'killpg', side_effect=PermissionError(errno.EPERM, 'denied')) as group, \
                mock.patch.object(rehearsal.os, 'kill', side_effect=AssertionError('no PID signals')):
            probes = {}
            for key in ('check', 'hook'):
                probes[key] = rehearsal.run_command(core, folder, [], 'read', [], **kwargs)
            self.assertEqual(2, group.call_count)
            self.assertEqual(2, process.communicate.call_count)
            return probes

    def test_killpg_eperm_continues_and_result_retains_group(self):
        if sys.platform == 'darwin':
            members, complete = rehearsal.group_members(os.getpgrp())
            self.assertTrue(complete)
            self.assertIn(os.getpid(), members)
        evidence = self.run_rehearsal(lane_runner=self.denied_lane)
        self.assertFalse(evidence['passed'])
        self.assertEqual(4, len(evidence['leftovers']))
        for row in evidence['leftovers']:
            self.assertEqual(43210, row['pgid'])
            self.assertEqual([43211, 43212], row['pids'])
            self.assertEqual('kill -TERM -43210', row['stop_command'])
        self.assertIn('rehearsal process cleanup incomplete', evidence['regressions'])

    def test_killpg_eperm_leftover_stays_due_after_later_refusal(self):
        from brain.instance import Instance
        r = self.init('sample')
        inst = Instance(r)
        node = inst.node('root')
        lane = self.tmp / 'denied'
        lane.mkdir()
        (lane / 'brain').mkdir()
        process = mock.Mock(pid=43210, returncode=None)
        def timeout(*args, **kwargs):
            (lane / 'home/rehearsal-startup-failed').write_text('failed')
            raise subprocess.TimeoutExpired('fixture', 60, output=b'partial')
        process.communicate.side_effect = timeout
        with mock.patch.object(rehearsal, 'sandbox_prefix', return_value=[]), \
                mock.patch.object(rehearsal.subprocess, 'Popen', return_value=process), \
                mock.patch.object(rehearsal, 'group_members', return_value=([43211], True)), \
                mock.patch.object(rehearsal.os, 'killpg', side_effect=PermissionError(errno.EPERM, 'denied')) as group, \
                mock.patch.object(rehearsal.os, 'kill', side_effect=AssertionError('no PID signals')):
            # Persist before subsequent clock/output validation can refuse a probe.
            with self.assertRaises(rehearsal.RehearsalError):
                rehearsal.run_command(lane / 'brain/core', lane, [], 'read', [],
                    on_leftover=lambda row: rehearsal.report_leftover(inst, node, row))
            group.assert_called_once()
            process.communicate.assert_called_once()
        for _ in range(2):
            due = self.b(r, 'due', 'root', '--json')
            self.assertIn('pgid=43210', due)
            self.assertIn('PIDs=43211', due)
            self.assertIn('kill -TERM -43210', due)

    def test_supervisor_atexit_never_signals_running_children(self):
        folder = self.tmp / 'supervisor'
        self.denied_lane(self.root, self.root / 'core', folder, [], [])
        script = next(folder.rglob('sitecustomize.py')).read_text()
        tree = ast.parse(script)
        reap = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'reap')
        child = mock.Mock()
        child.poll.return_value = None
        child.kill.side_effect = AssertionError('child-side atexit signal after denied group cleanup')
        namespace = {'children': [child], 'ProcessLookupError': ProcessLookupError}
        exec(compile(ast.Module(body=[reap], type_ignores=[]), '<supervisor>', 'exec'), namespace)
        namespace['reap']()
        child.kill.assert_not_called()
        child.wait.assert_not_called()

    def test_signal_audit_records_every_denied_group_attempt(self):
        outcome = self.run_rehearsal(lane_runner=self.denied_lane)
        audit = outcome['signal_audit']
        self.assertEqual(len(audit), 4)
        for entry in audit:
            self.assertEqual(entry['pgid'], 43210)
            self.assertEqual(entry['pid'], 43210)
            self.assertEqual(entry['signal'], int(signal.SIGKILL))
            self.assertEqual(entry['result'], 'denied')
            self.assertEqual(entry['errno'], errno.EPERM)
            self.assertIsNotNone(datetime.datetime.fromisoformat(entry['at']).utcoffset())


class RehearsalStability(RehearsalFixture):
    def test_frozen_brain_drops_only_derived_clone_caches(self):
        history = {'events.jsonl': b'exact journal\n',
                   'events-compare.jsonl': b'exact comparison history\n',
                   'other-cache.json': b'preserved other cache'}
        metadata = self.root / '.brain'
        metadata.mkdir()
        for name, payload in {**history, 'events-index.json': b'original index',
                              'events-compare-index.json': b'original comparison index'}.items():
            (metadata / name).write_bytes(payload)
        original = {p.name: p.read_bytes() for p in metadata.iterdir()}
        seen = []
        def inspect(source, *unused, **kwargs):
            seen.append(source)
            self.assertFalse((source / '.brain/events-index.json').exists())
            self.assertFalse((source / '.brain/events-compare-index.json').exists())
            for name, payload in history.items():
                self.assertEqual(payload, (source / '.brain' / name).read_bytes())
        with mock.patch.object(rehearsal, 'freeze_machine_inputs'):
            self.run_rehearsal(inspect_lane=inspect)
        self.assertEqual(2, len(seen))
        self.assertEqual(original, {p.name: p.read_bytes() for p in metadata.iterdir()})

    def test_report_observations_ignore_assets_but_track_index_operands(self):
        from types import SimpleNamespace
        workbench = self.tmp / 'external-work'
        report = workbench / 'reports/run'
        (report / 'baseline').mkdir(parents=True)
        dependency = self.tmp / 'dependencies'
        dependency.mkdir()
        (dependency / 'package.json').write_text('{}')
        (report / 'baseline/node_modules').symlink_to(dependency, target_is_directory=True)
        index = report / 'index.md'
        index.write_text('# original')
        node = dict(id='engine', external=True, home_path=self.home, fm={}, dir=self.root,
                    local={'home': str(self.home), 'workbench': str(workbench)})
        inst = SimpleNamespace(root=self.root, nodes=lambda: [node], local={'nodes': {'engine': node['local']}})
        before = rehearsal.external_inputs(inst)
        (dependency / 'package.json').write_text('{"ignored":true}')
        self.assertEqual(before, rehearsal.external_inputs(inst))
        for mutate in (lambda: index.write_text('# changed'), lambda: index.unlink(),
                       lambda: index.write_text('# restored'),
                       lambda: (workbench / 'reports/another').mkdir(),
                       lambda: (workbench / 'reports/another/index.md').write_text('# added'),
                       lambda: (workbench / 'reports/another/index.md').unlink(),
                       lambda: (workbench / 'reports/another').rmdir()):
            mutate()
            after = rehearsal.external_inputs(inst)
            self.assertNotEqual(before, after)
            before = after
        linked_index = self.tmp / 'linked-index.md'
        linked_index.write_text('# linked')
        index.unlink()
        index.symlink_to(linked_index)
        before = rehearsal.external_inputs(inst)
        linked_index.write_text('# changed target')
        self.assertNotEqual(before, rehearsal.external_inputs(inst))
        linked_workbench = self.tmp / 'linked-work'
        linked_workbench.mkdir()
        (linked_workbench / 'reports').symlink_to(workbench / 'reports', target_is_directory=True)
        node['local']['workbench'] = str(linked_workbench)
        with self.assertRaisesRegex(rehearsal.RehearsalError, 'linked external observation'):
            rehearsal.external_inputs(inst)

    def test_external_report_directory_links_refuse_instead_of_omitting_reads(self):
        from types import SimpleNamespace
        workbench = self.tmp / 'external-work'
        (workbench / 'reports').mkdir(parents=True)
        target = self.tmp / 'report-target'
        target.mkdir()
        (target / 'index.md').write_text('# report')
        (workbench / 'reports/run').symlink_to(target, target_is_directory=True)
        node = dict(id='engine', external=True, home_path=self.home, fm={}, dir=self.root,
                    local={'home': str(self.home), 'workbench': str(workbench)})
        inst = SimpleNamespace(root=self.root, nodes=lambda: [node], local={'nodes': {'engine': node['local']}})
        with self.assertRaisesRegex(rehearsal.RehearsalError, 'linked external observation'):
            rehearsal.external_inputs(inst)

    def test_external_inputs_detect_dirty_bytes_refs_reports_and_ignored_inputs(self):
        from types import SimpleNamespace
        workbench = self.tmp / 'external-work'
        report = workbench / 'reports/run/index.md'
        report.parent.mkdir(parents=True)
        report.write_text('# old report')
        node = dict(id='engine', external=True, home_path=self.home, fm={}, dir=self.root,
                    local={'home': str(self.home), 'workbench': str(workbench),
                           'worktrees': {'main': {'path': str(self.home)}}})
        inst = SimpleNamespace(root=self.root, nodes=lambda: [node], local={'nodes': {'engine': node['local']}})
        before = rehearsal.external_inputs(inst)
        self.assertEqual(before, rehearsal.external_inputs(inst))
        for mutate in (lambda: (self.home / 'README.md').write_text('dirty one'),
                       lambda: (self.home / 'README.md').write_text('dirty two'),
                       lambda: (self.home / '.env').write_text('MODE=second'),
                       lambda: report.write_text('# new report'),
                       lambda: self.git('update-ref', 'refs/remotes/origin/main', self.head, cwd=self.home)):
            mutate()
            after = rehearsal.external_inputs(inst)
            self.assertNotEqual(before, after)
            before = after

    def test_external_drift_withholds_pass_without_claiming_candidate_regression(self):
        old = {'ready:0:0': result('not ready: old reason', 1), 'hook': result()}
        new = {'ready:0:0': result('not ready: changed reason', 1), 'hook': result()}
        with mock.patch.object(rehearsal, 'external_inputs', side_effect=['one', 'two', 'two']):
            outcome = self.run_rehearsal(outputs=[old, new])
        self.assertFalse(outcome['passed'])
        self.assertEqual(['DRIFT: external inputs changed; comparison inconclusive'], outcome['regressions'])
        self.assertIn('ready:0:0: ready reason changed', outcome['drift_comparisons'])
        from types import SimpleNamespace
        node = dict(external=True, home_path=self.home)
        inst = SimpleNamespace(root=self.root, node=lambda _: node, nodes=lambda: [])
        printed = io.StringIO()
        with mock.patch.object(rehearsal._m_nodes, 'find_root', return_value=self.root), \
                mock.patch.object(rehearsal._m_instance, 'Instance', return_value=inst), \
                mock.patch.object(rehearsal, 'evidence_path', return_value=self.tmp / 'evidence.json'), \
                mock.patch.object(rehearsal, 'rehearse', return_value=outcome), \
                contextlib.redirect_stdout(printed), self.assertRaises(SystemExit):
            rehearsal.cmd_rehearse(SimpleNamespace(id='engine', branch='job'))
        self.assertIn('inconclusive: ready:0:0: ready reason changed', printed.getvalue())

    def test_machine_snapshot_is_shared_and_preserves_sample_history(self):
        from types import SimpleNamespace
        instant = datetime.datetime(2035, 1, 3, tzinfo=datetime.timezone.utc)
        inst = SimpleNamespace(root=self.root, now_dt=lambda: instant)
        reading = {'cpus': 8, 'thermal': 'pressure', 'load': [12, 12, 12],
                   'load_per_cpu': [1.5, 1.5, 1.5]}
        from brain import ui
        with mock.patch.object(ui, 'machine_load', return_value=reading):
            rehearsal.freeze_machine_inputs(inst, instant.isoformat())
        data = json.loads((self.root / '.brain/machine.json').read_text())
        self.assertEqual(reading, data['samples'][-1]['reading'])
        self.assertEqual(instant.timestamp(), data['samples'][-1]['at'])
        # Fresh sustained samples keep their continuity and episode information.
        data.update(episode=True, heat_reasons=['sustained thermal pressure'])
        data['samples'].insert(0, dict(at=instant.timestamp() - 60, reading=reading))
        (self.root / '.brain/machine.json').write_text(json.dumps(data))
        with mock.patch.object(ui, 'machine_load', side_effect=AssertionError('must use snapshot')):
            rehearsal.freeze_machine_inputs(inst, instant.isoformat())
        self.assertEqual(data, json.loads((self.root / '.brain/machine.json').read_text()))

    def test_volatile_churn_during_copy_and_run_does_not_refuse(self):
        paths = ['.git/refs/t3/checkpoint', '.git/logs/HEAD', '.git/COMMIT_EDITMSG',
                 '.git/hook-once', '.git/index.lock', '.git/config.lock',
                 '.brain/spend.json', '.brain/spend/usage.json', '.brain/watch.json',
                 '.brain/a.lock', '.brain/lock', '.brain/machine.json', '.brain/hooks.log',
                 '.brain/hooks.json', '.brain/local.json', 'ignored/cache', 'untracked.tmp']
        for during_run in (False, True):
            for rel in paths + ['.git/index']:
                with self.subTest(path=rel, during_run=during_run):
                    def mutate():
                        if rel == '.git/index':
                            os.utime(self.root / rel, None)
                        else:
                            self.write(rel, self.oid + '\n' if rel.startswith('.git/refs/') else 'churn')
                    try:
                        self.assertTrue(self.run_rehearsal(mutate, during_run)['passed'])
                    finally:
                        if rel != '.git/index':
                            (self.root / rel).unlink(missing_ok=True)

    def test_tracked_edits_and_real_ref_moves_during_copy_use_frozen_input(self):
        parent = self.git('rev-parse', 'HEAD^', cwd=self.root).strip()
        for rel, text in [('note.md', 'edited'), ('core/bin/brain', 'edited core'),
                          ('.git/refs/heads/main', parent + '\n'),
                          ('.git/refs/tags/release', self.oid + '\n'),
                          ('.git/HEAD', self.oid + '\n')]:
            with self.subTest(path=rel):
                self.assertTrue(self.run_rehearsal(lambda: self.write(rel, text))['passed'])

    def test_tracked_edit_during_run_does_not_refuse_frozen_evidence(self):
        outcome = self.run_rehearsal(lambda: self.write('note.md'), during_run=True)
        self.assertTrue(outcome['passed'], outcome['regressions'])

    def test_index_refresh_and_packing_refs_do_not_change_stability(self):
        with mock.patch.dict(os.environ, self.env):
            before = rehearsal.stability_snapshot(self.root)
            self.git('update-index', '--refresh', cwd=self.root)
            self.git('update-ref', 'refs/t3/checkpoint', self.oid, cwd=self.root)
            self.git('pack-refs', '--all', cwd=self.root)
            self.assertEqual(before, rehearsal.stability_snapshot(self.root))
            self.git('commit', '--allow-empty', '-qm', 'move head', cwd=self.root)
            self.assertNotEqual(before, rehearsal.stability_snapshot(self.root))

    def test_tracked_links_and_replaced_parents_never_read_targets(self):
        outside = self.tmp / 'outside'
        outside.mkdir()
        (outside / 'secret').write_text('original')
        self.write('nested/secret', 'tracked')
        (self.root / 'alias').symlink_to(outside / 'secret')
        self.git('add', 'alias', 'nested', cwd=self.root)
        (self.root / 'nested/secret').unlink()
        (self.root / 'nested').rmdir()
        (self.root / 'nested').symlink_to(outside, target_is_directory=True)
        with mock.patch.dict(os.environ, self.env):
            before = rehearsal.stability_snapshot(self.root)
            (outside / 'secret').write_text('target changed')
            self.assertEqual(before, rehearsal.stability_snapshot(self.root))
            self.assertTrue(before['alias'].startswith('link:'))
            self.assertTrue(before['nested'].startswith('link:'))
            self.assertNotIn('nested/secret', before)

    def test_gitlink_tracks_nested_dirty_content_and_refs_not_private_churn(self):
        nested = self.repo(self.tmp / 'nested-core')
        import shutil
        shutil.rmtree(self.root / 'core')
        self.git('rm', '-qr', '--cached', 'core', cwd=self.root)
        self.git('submodule', 'add', '-q', str(nested), 'core', cwd=self.root)
        self.git('commit', '-qam', 'gitlink core', cwd=self.root)
        with mock.patch.dict(os.environ, self.env):
            before = rehearsal.stability_snapshot(self.root)
            gitdir = Path(self.git('rev-parse', '--absolute-git-dir', cwd=self.root / 'core').strip())
            (gitdir / 'COMMIT_EDITMSG').write_text('churn')
            self.assertEqual(before, rehearsal.stability_snapshot(self.root))
            (self.root / 'core/README.md').write_text('dirty core')
            self.assertNotEqual(before, rehearsal.stability_snapshot(self.root))

    def test_incomplete_git_copies_refuse_and_repacking_live_source_is_allowed(self):
        blob = self.git('rev-parse', 'HEAD:note.md', cwd=self.root).strip()
        def damage(dest):
            (dest / '.git/objects' / blob[:2] / blob[2:]).unlink()
        with self.assertRaises(rehearsal.RehearsalError):
            self.run_rehearsal(damage_copy=damage)
        self.assertTrue(self.run_rehearsal(
            lambda: self.git('repack', '-adq', cwd=self.root))['passed'])

    def test_connectivity_requires_branch_and_tag_objects_but_not_private_refs(self):
        self.git('checkout', '-qb', 'other', cwd=self.root)
        self.write('branch-only', 'branch content')
        self.git('add', 'branch-only', cwd=self.root)
        self.git('commit', '-qm', 'other branch', cwd=self.root)
        blob = self.git('rev-parse', 'HEAD:branch-only', cwd=self.root).strip()
        self.git('tag', 'release', cwd=self.root)
        self.git('checkout', '-q', 'main', cwd=self.root)
        self.write('.git/refs/t3/checkpoint', 'f' * 40 + '\n')
        dest = self.tmp / 'copy'
        rehearsal.copy_tree(self.root, dest)
        with mock.patch.dict(os.environ, self.env):
            rehearsal.materialize_git(self.root, dest)
            (dest / '.git/objects' / blob[:2] / blob[2:]).unlink()
            with self.assertRaises(rehearsal.RehearsalError):
                rehearsal.validate_git_objects(dest)
            (dest / '.git/refs/heads/other').unlink()
            with self.assertRaises(rehearsal.RehearsalError):
                rehearsal.validate_git_objects(dest)
            (dest / '.git/refs/tags/release').unlink()
            rehearsal.validate_git_objects(dest)

    def test_copied_git_locks_do_not_block_materialization(self):
        for rel in ['.git/config.lock', '.git/index.lock', '.git/HEAD.lock',
                    '.git/refs/heads/main.lock']:
            self.write(rel, 'in-progress writer')
        self.assertTrue(self.run_rehearsal()['passed'])
        self.assertTrue((self.root / '.git/config.lock').exists())

    def test_gitlink_metadata_links_and_incomplete_copies_refuse(self):
        import shutil
        nested = self.repo(self.tmp / 'nested-core')
        shutil.rmtree(self.root / 'core')
        self.git('rm', '-qr', '--cached', 'core', cwd=self.root)
        self.git('submodule', 'add', '-q', str(nested), 'core', cwd=self.root)
        self.git('commit', '-qam', 'gitlink core', cwd=self.root)
        pointer = self.root / 'core/.git'
        original = pointer.read_text()
        pointer.unlink()
        pointer.symlink_to(self.root / '.git/modules/core', target_is_directory=True)
        with mock.patch.dict(os.environ, self.env):
            with self.assertRaises(rehearsal.RehearsalError):
                rehearsal.stability_snapshot(self.root)
        pointer.unlink()
        pointer.write_text(original)
        self.assertTrue(self.run_rehearsal()['passed'])
        blob = self.git('rev-parse', 'HEAD:README.md', cwd=self.root / 'core').strip()
        copy = rehearsal.copy_tree
        def copying(source, dest):
            copy(source, dest)
            if dest.name == '.git' and dest.parent.name == 'core':
                (dest / 'objects' / blob[:2] / blob[2:]).unlink()
        with mock.patch.object(rehearsal, 'copy_tree', side_effect=copying):
            with self.assertRaisesRegex(rehearsal.RehearsalError, 'live brain moved while copying'):
                self.run_rehearsal()

    def test_metadata_links_and_unavailable_metadata_refuse(self):
        import shutil
        for rel in ['root', '.git', 'gitdir-parent', '.git/config', '.git/config.worktree',
                    '.git/index', '.git/HEAD', '.git/packed-refs', '.git/refs',
                    '.git/refs/heads', '.git/refs/tags', '.git/refs/heads/alias',
                    '.git/refs/tags/alias', 'unavailable']:
            with self.subTest(path=rel):
                root = self.tmp / 'linked-case'
                shutil.copytree(self.root, root)
                outside = self.tmp / 'metadata-target'
                try:
                    if rel == 'root':
                        outside.symlink_to(root, target_is_directory=True)
                        root_to_check = outside
                    elif rel == 'unavailable':
                        shutil.rmtree(root / '.git')
                        root_to_check = root
                    elif rel == 'gitdir-parent':
                        outside.mkdir()
                        shutil.move(str(root / '.git'), str(outside / 'admin'))
                        alias = root / 'metadata-alias'
                        alias.symlink_to(outside, target_is_directory=True)
                        (root / '.git').write_text('gitdir: ' + str(alias / 'admin') + '\n')
                        root_to_check = root
                    else:
                        path = root / rel
                        path.parent.mkdir(parents=True, exist_ok=True)
                        if path.exists():
                            shutil.move(str(path), str(outside))
                        else:
                            outside.write_text(self.oid + '\n')
                        path.symlink_to(outside, target_is_directory=outside.is_dir())
                        root_to_check = root
                    with mock.patch.dict(os.environ, self.env):
                        with self.assertRaises(rehearsal.RehearsalError):
                            rehearsal.stability_snapshot(root_to_check)
                finally:
                    shutil.rmtree(root)
                    if outside.is_symlink() or outside.is_file():
                        outside.unlink()
                    elif outside.exists():
                        shutil.rmtree(outside)

    def test_untracked_manual_core_is_bound_and_root_must_own_its_git(self):
        import shutil
        self.git('rm', '-qr', 'core', cwd=self.root)
        self.repo(self.root / 'core')
        with mock.patch.dict(os.environ, self.env):
            before = rehearsal.stability_snapshot(self.root)
            (self.root / 'core/README.md').write_text('dirty manual core')
            self.assertNotEqual(before, rehearsal.stability_snapshot(self.root))
            child = self.root / 'non-repository'
            child.mkdir()
            with self.assertRaises(rehearsal.RehearsalError):
                rehearsal.stability_snapshot(child)

    def test_tracked_unusual_paths_remain_bound(self):
        for name in ['line\nbreak', 'unicode-\u2603']:
            self.write(name, 'tracked')
            self.git('add', '--', name, cwd=self.root)
            with mock.patch.dict(os.environ, self.env):
                before = rehearsal.stability_snapshot(self.root)
                self.write(name, 'edited')
                self.assertNotEqual(before, rehearsal.stability_snapshot(self.root))

    @unittest.skipIf(sys.platform == 'darwin', 'filesystem requires valid UTF-8 filenames')
    def test_tracked_non_utf8_path_remains_bound(self):
        name = os.fsdecode(b'byte-\xff')
        self.write(name, 'tracked')
        self.git('add', '--', name, cwd=self.root)
        with mock.patch.dict(os.environ, self.env):
            before = rehearsal.stability_snapshot(self.root)
            self.write(name, 'edited')
            self.assertNotEqual(before, rehearsal.stability_snapshot(self.root))

    def test_exhaustive_snapshot_still_detects_runtime_mutations(self):
        before = rehearsal.snapshot(self.root)
        self.write('.brain/watch.json')
        self.assertNotEqual(before, rehearsal.snapshot(self.root))


@unittest.skipUnless(sys.platform == 'darwin', 'requires macOS OS sandbox')
class RehearsalCoverageFaultTypes(unittest.TestCase):
    def coverage(self, reasons=()):
        from brain import events, rehearse_transactions as transactions
        records = [dict(ts='2026-01-02 03:04 +00:00', node=events.node_ref('root'),
                        kind='damage', actor='brain', ref=ref, data=dict(reason=reason))
                   for ref, reason in reasons]
        body = b''.join((json.dumps(row) + '\n').encode() for row in records)
        state, replayed = transactions.replay({'events.jsonl': dict(hex=body.hex())}, events)
        self.assertEqual(records, replayed)
        operand = dict(recorded={}, opened={}, actual=[], nodes=['root'], preferred='root',
                       leases={}, faults=dict(reasons))
        return events, transactions, state, operand

    def test_empty_raw_fault_map_emits_empty_row_list(self):
        events, transactions, state, operand = self.coverage()
        rows, gap = transactions.coverage_fields(events, state, operand)
        self.assertIs(type(operand['faults']), dict)
        self.assertIs(type(rows[0]['faults']), list)
        self.assertEqual([], rows[0]['faults'])
        self.assertTrue(rows[0]['equal'])
        self.assertFalse(gap)

    def test_nonempty_raw_fault_map_preserves_values_and_native_row_order(self):
        reasons = [('b' * 32, 'sample damage'), ('a' * 32, 'other damage')]
        events, transactions, state, operand = self.coverage(reasons)
        rows, gap = transactions.coverage_fields(events, state, operand)
        self.assertEqual(dict(reasons), operand['faults'])
        self.assertEqual(dict(reasons), state['faults'])
        self.assertEqual([ref for ref, _ in reasons], rows[0]['faults'])
        self.assertFalse(rows[0]['equal'])
        self.assertTrue(gap)

    def test_raw_fault_list_and_other_wrong_types_are_refused(self):
        for reasons in ([], [('b' * 32, 'sample damage')]):
            events, transactions, state, operand = self.coverage(reasons)
            for raw in ([], list(state['faults']), (), None, '', False):
                with self.subTest(reasons=reasons, raw=raw):
                    operand['faults'] = raw
                    with self.assertRaises(transactions.EvidenceError):
                        transactions.coverage_fields(events, state, operand)

    def test_forged_raw_fault_keys_are_refused(self):
        events, transactions, state, operand = self.coverage([('b' * 32, 'sample damage')])
        for raw in ({}, {'a' * 32: 'sample damage'},
                    {'b' * 32: 'sample damage', 'a' * 32: 'other damage'}):
            with self.subTest(raw=raw):
                operand['faults'] = raw
                with self.assertRaises(transactions.EvidenceError):
                    transactions.coverage_fields(events, state, operand)

    def test_forged_raw_fault_values_are_refused(self):
        events, transactions, state, operand = self.coverage([('b' * 32, 'sample damage')])
        for reason in ('forged damage', '', None, False, ['sample damage']):
            with self.subTest(reason=reason):
                operand['faults'] = {'b' * 32: reason}
                with self.assertRaises(transactions.EvidenceError):
                    transactions.coverage_fields(events, state, operand)


class RehearsalCloneCaches(Scenario):
    retain_rehearsal_evidence = True

    def test_native_copied_caches_are_removed_before_each_lane_capture(self):
        from brain import events, instance, rehearse_transactions as transactions
        source = self.init('source')
        inst = instance.Instance(source)
        with mock.patch.dict(os.environ, self.env):
            events.comparison(inst, inst.node('root'))
            events.read_comparisons(inst, repair=True)
            events.journal(inst).sync(persist=True)
        names = ('events-index.json', 'events-compare-index.json',
                 'events.jsonl', 'events-compare.jsonl')
        original = {name: (source / '.brain' / name).read_bytes()
                    for name in names if (source / '.brain' / name).exists()}
        # init's index really is inode-bound: an ordinary copy is stale.
        ordinary = self.tmp / 'ordinary'
        rehearsal.copy_tree(source, ordinary)
        snap = transactions.capture(ordinary)
        with self.assertRaises(transactions.EvidenceError):
            transactions.check_index(snap, transactions.replay(snap, events)[0])
        (self.tmp / 'rehearse-clock').write_text(inst.now_dt().isoformat())
        captured = []
        def command(core, lane, args, kind, *unused, **kwargs):
            root = lane / 'brain'
            before = transactions.capture(root)
            self.assertNotIn('events-index.json', before)
            self.assertNotIn('events-compare-index.json', before)
            for name in ('events.jsonl', 'events-compare.jsonl'):
                if name in original:
                    self.assertEqual(original[name], (root / '.brain' / name).read_bytes())
            with mock.patch.dict(os.environ, self.env):
                native = instance.Instance(root)
                events.journal(native).sync(persist=True)
                events.read_comparisons(native, repair=True)
            rebuilt = transactions.capture(root)
            transactions.check_index(rebuilt, transactions.replay(rebuilt, events)[0], required=True)
            transactions.check_comparisons(rebuilt, inst.now_dt())
            captured.append(lane.name)
            kwargs['raw_output'].append({})
            return result()
        for folder in ('before', 'after_'):
            with mock.patch.object(rehearsal, 'run_command', side_effect=command):
                rehearsal.run_lane(source, source / 'core', self.tmp / folder, [],
                    [('read', [], 'read'), ('fix', [], 'fix'), ('refresh', [], 'refresh')])
        self.assertEqual(['before', 'mutation-fix', 'mutation-refresh',
                          'after_', 'mutation-fix', 'mutation-refresh'], captured)
        self.assertEqual(original, {name: (source / '.brain' / name).read_bytes() for name in original})

    def test_cloned_native_refresh_rebuilds_and_preserves_transaction_refusals(self):
        import copy
        from brain import events, instance, rehearse_transactions as transactions
        source = self.init('native-source')
        self.b(source, 'check', '--fix')
        with mock.patch.dict(os.environ, self.env):
            inst = instance.Instance(source)
            with instance.brain_lock(inst):
                events.read_comparisons(inst, repair=True)
        original = {p.name: p.read_bytes() for p in (source / '.brain').iterdir() if p.is_file()}
        self.assertIn('events-index.json', original)
        self.assertIn('events-compare-index.json', original)
        lane = self.tmp / 'native-clone'
        lane.mkdir()
        rehearsal.copy_brain(source, lane / 'brain', self.tmp)
        for attempt in range(2):
            raw, row, failure = [], None, None
            try:
                row = rehearsal.run_command(lane / 'brain/core', lane,
                    ['status', 'root', '--refresh'], 'refresh', [], raw_output=raw)
            except BaseException as error:
                failure = type(error).__name__
                raise
            finally:
                transactions._persist_fixture_evidence(lane, raw, row, failure)
            self.assertEqual(0, row['code'])
            self.assertEqual('proven', row['transaction_evidence'])
            self.assertEqual(0, row['unexpected'])
            packet = raw[0]['transaction_evidence']
            now = datetime.datetime.fromisoformat(packet['validation_instant'])
            def validate(after, observations, pid=packet['owner_pid']):
                return transactions.validate(packet['before'], after, observations, pid, 'status', now)
            proven, verdict = validate(packet['after'], packet['observations'])
            self.assertEqual('proven', verdict)
            self.assertEqual((set(), 'unproven'), validate(packet['after'], []))
            self.assertEqual((set(), 'unproven'), validate(packet['after'], packet['observations'],
                                                          packet['owner_pid'] + 100000))
            for name in ('events-index.json', 'events-compare-index.json'):
                damaged = copy.deepcopy(packet['after'])
                self.assertIn(name, damaged)
                damaged[name]['hex'] = b'{}'.hex()
                self.assertEqual((set(), 'unproven'), validate(damaged, packet['observations']))
            residual = {'brain/.brain/unknown-state.json', 'brain/private.txt'}
            self.assertEqual(residual, {p for p in proven | residual
                if not rehearsal.allowed_write(p, 'refresh') and p not in proven})
            if attempt == 0:
                self.assertNotIn('events-index.json', packet['before'])
            else:
                self.assertIn('events-index.json', packet['before'])
                self.assertIn('events-compare-index.json', packet['before'])
        self.assertEqual(original, {p.name: p.read_bytes() for p in (source / '.brain').iterdir()
                                    if p.is_file()})

    def test_clone_cache_refuses_alias_foreign_and_existing_destinations(self):
        source = self.tmp / 'source'
        (source / '.brain').mkdir(parents=True)
        cache = source / '.brain/events-index.json'
        cache.write_bytes(b'operator-cache')
        history = source / '.brain/events.jsonl'
        history.write_bytes(b'exact history\n')
        def copied(source, dest):
            dest.mkdir()
            (dest / '.brain').symlink_to(source / '.brain', target_is_directory=True)
        with mock.patch.object(rehearsal, 'copy_tree', side_effect=copied):
            with self.assertRaises(OSError):
                rehearsal.copy_brain(source, self.tmp / 'linked-directory', self.tmp)
        def linked_cache(source, dest):
            (dest / '.brain').mkdir(parents=True)
            (dest / '.brain/events-index.json').symlink_to(cache)
        with mock.patch.object(rehearsal, 'copy_tree', side_effect=linked_cache):
            with self.assertRaises(rehearsal.RehearsalError):
                rehearsal.copy_brain(source, self.tmp / 'linked-cache', self.tmp)
        def shared_cache(source, dest):
            (dest / '.brain').mkdir(parents=True)
            os.link(cache, dest / '.brain/events-index.json')
        with mock.patch.object(rehearsal, 'copy_tree', side_effect=shared_cache):
            with self.assertRaises(rehearsal.RehearsalError):
                rehearsal.copy_brain(source, self.tmp / 'shared-cache', self.tmp)
        with mock.patch.object(rehearsal.os, 'getuid', return_value=os.getuid() + 1):
            with self.assertRaises(rehearsal.RehearsalError):
                rehearsal.copy_brain(source, self.tmp / 'foreign', self.tmp)
        with self.assertRaises(FileExistsError):
            rehearsal.copy_brain(source, source, self.tmp)
        alias = self.tmp / 'alias'
        alias.symlink_to(self.tmp, target_is_directory=True)
        with self.assertRaises(rehearsal.RehearsalError):
            rehearsal.copy_brain(source, alias / 'ambiguous', self.tmp)
        self.assertEqual(b'operator-cache', cache.read_bytes())
        self.assertEqual(b'exact history\n', history.read_bytes())
        self.assertFalse((self.tmp / 'foreign').exists())
        self.assertFalse((self.tmp / 'ambiguous').exists())

    def test_native_comparison_first_excludes_coverage_and_uses_node_ref(self):
        from brain import events, instance, rehearse_transactions as transactions
        root = self.init('prefix')
        inst = instance.Instance(root)
        now = inst.now_dt()
        def row(number, node, kind, days=0):
            value = dict(id=format(number, '032x'), ts=(now - datetime.timedelta(days=days)).isoformat(),
                         node=node, kind=kind, equal=True, resources=[], view_equal=None,
                         interrupted=[], faults=[])
            if kind == 'comparison':
                value.update(view_equal=True, out_of_scope=[])
            return value
        prefixes = [
            [row(1, 'root', 'coverage'), row(2, events.node_ref('other'), 'coverage')],
            [row(3, 'root', 'coverage'), row(4, 'root', 'comparison', 2),
             row(5, events.node_ref('root'), 'comparison'), row(6, 'other', 'comparison'),
             row(7, 'root', 'comparison', 3), row(8, 'other', 'coverage')],
        ]
        for rows in prefixes:
            with self.subTest(kinds=[r['kind'] for r in rows]):
                path = root / '.brain/events-compare.jsonl'
                path.write_bytes(b''.join((json.dumps(r) + '\n').encode() for r in rows))
                cache = root / '.brain/events-compare-index.json'
                cache.unlink(missing_ok=True)
                with mock.patch.dict(os.environ, self.env):
                    events.read_comparisons(inst, repair=True)
                snapshot = transactions.capture(root)
                self.assertEqual(rows, transactions.check_comparisons(snapshot, now))
                saved = json.loads(cache.read_text())
                expected = {} if len(rows) == 2 else {
                    events.node_ref('root'): rows[4], events.node_ref('other'): rows[3]}
                self.assertEqual(expected, saved['first'])
                import copy
                for field in ('first', 'rows', 'anchor', 'identity', 'offset'):
                    forged = copy.deepcopy(snapshot)
                    bad = copy.deepcopy(saved)
                    if field == 'first':
                        bad[field] = {events.node_ref('root'): rows[0]}
                    elif field == 'rows':
                        bad[field] = list(reversed(bad[field]))
                    elif field == 'anchor':
                        bad[field] = '0' * 64
                    elif field == 'identity':
                        bad[field][1] += 1
                    else:
                        bad[field] += 1
                    forged['events-compare-index.json']['hex'] = json.dumps(bad).encode().hex()
                    with self.subTest(forged=field):
                        with self.assertRaises(transactions.EvidenceError):
                            transactions.check_comparisons(forged, now)
                damaged = copy.deepcopy(snapshot)
                damaged['events-compare.jsonl']['hex'] += b'{broken\n'.hex()
                with self.assertRaises((transactions.EvidenceError, ValueError)):
                    transactions.check_comparisons(damaged, now)



class RehearsalSandbox(Scenario):
    retain_rehearsal_evidence = True

    def runner(self, script, args=(), terms=(), kind='read', raw_output=None):
        lane = self.tmp / 'lane'
        lane.mkdir(exist_ok=True)
        (lane / 'brain').mkdir(exist_ok=True)
        core = lane / 'brain/core'
        (core / 'bin').mkdir(parents=True, exist_ok=True)
        (core / 'bin/brain').write_text(script)
        return rehearsal.run_command(core, lane, list(args), kind, [], terms, raw_output=raw_output)

    def native_transaction_evidence(self, failure=None, prepare=None, compare=False):
        trace = None
        try:
            self.start_native_evidence()
            from brain import events, instance, rehearse_transactions as transactions
            self.update_native_evidence(stage='helper-initialization',
                                        observer_path=str(Path(transactions.__file__).resolve()))
            root = self.tmp / ('transaction-' + (failure or 'success'))
            self.update_native_evidence(root=str(root))
            root = self.init('transaction-' + (failure or 'success'))
            inst = instance.Instance(root)
            if prepare is not None:
                prepare(inst)
            trace = self.tmp / ('transaction-' + (failure or 'success') + '.jsonl')
            self.update_native_evidence(trace_path=str(trace), stage='trace-initialization')
            trace.write_text('')
            before_diagnostics = {}
            self.update_native_evidence(stage='capture-before', before_diagnostics=before_diagnostics)
            before = transactions.capture(root, diagnostics=before_diagnostics)
            self.update_native_evidence(before=before, stage='native-command')
            # Restore only these local observation seams. Native publication,
            # journal validation, leases, locking and maintenance all run unchanged.
            with contextlib.ExitStack() as restore, mock.patch.dict(os.environ, self.env):
                for owner, name in ((events.Command, '__enter__'), (events.Command, '__exit__'),
                                    (events.Command, 'finish'), (events.Command, 'close_lease'),
                                    (events.Journal, 'append'), (events, 'comparison'),
                                    (events, 'snapshot_view'), (events, 'detect_gaps'),
                                    (instance.brain_lock, '__enter__'), (instance.brain_lock, '__exit__')):
                    restore.callback(setattr, owner, name, getattr(owner, name))
                transactions.install_observer(events, root, trace)
                try:
                    with instance.brain_lock(inst), events.Command(root, command='check'):
                        if compare:
                            events.comparison(inst, inst.node('root'))
                        if failure == 'partial':
                            (root / 'inbox.md').write_text('Sample partial change\n')
                        if failure:
                            raise RuntimeError('sample command failure')
                except RuntimeError:
                    if not failure:
                        raise
            self.capture_native_trace('command_boundary_trace_hex', trace, required=True)
            after_diagnostics = {}
            self.update_native_evidence(stage='capture-after', after_diagnostics=after_diagnostics)
            after = transactions.capture(root, diagnostics=after_diagnostics)
            self.update_native_evidence(after=after, stage='parse-trace')
            trace_bytes = trace.read_bytes()
            self.update_native_evidence(ordered_trace_hex=trace_bytes.hex())
            observed = transactions.lines(trace_bytes)
            self.update_native_evidence(observations=observed, stage='helper-return')
            return root, inst, before, after, observed
        except BaseException as error:
            self.native_evidence_exception(error)
            raise
        finally:
            # This separately labelled trace never replaces the actual parse input.
            self.capture_native_trace('helper_finally_trace_hex', trace)

    def test_saved_incomplete_coverage_packet_preserves_native_schema_and_refusal(self):
        from brain import rehearse_transactions as transactions
        path = os.environ.get('BRAIN_REHEARSAL_NEGATIVE_PACKET')
        if not path:
            self.skipTest('private historical operand not supplied')
        body = Path(path).read_bytes()
        self.assertEqual('3cbc882a1f3e05b259791b7db2662f90c664a0204f9690515317716bb342bf92',
                         hashlib.sha256(body).hexdigest())
        packet = json.loads(body)
        evidence = packet['raw'][0]['transaction_evidence']
        instant = datetime.datetime.fromisoformat(evidence['validation_instant'])
        # Shape admission must never stand in for the absent coverage observer.
        rows = transactions.check_comparisons(evidence['after'], instant)
        self.assertEqual(['coverage', 'comparison'], [r['kind'] for r in rows])
        self.assertEqual((set(), 'unproven'), transactions.validate(
            evidence['before'], evidence['after'], evidence['observations'],
            evidence['owner_pid'], evidence['validator_command'], instant))
        self.assertEqual(body, Path(path).read_bytes())

    def test_nonempty_coverage_faults_remain_unproven_and_rows_keep_native_order(self):
        import copy
        from brain import events, instance, rehearse_transactions as transactions
        reasons = [('b' * 32, 'sample damage'), ('a' * 32, 'other damage')]
        def prepare(inst):
            with instance.brain_lock(inst):
                for ref, reason in reasons:
                    events.append(inst, 'root', 'damage', ref, dict(reason=reason))
        _, inst, before, after, observed = self.native_transaction_evidence('abort', prepare=prepare)
        end = next(r for r in observed if r['type'] == 'coverage-exit')
        self.assertEqual(dict(reasons), end['operands']['faults'])
        self.assertIs(end['result'], True)
        row = next(r['record'] for r in observed if r['type'] == 'coverage-row')
        self.assertEqual([ref for ref, _ in reasons], row['faults'])
        self.assertIs(row['equal'], False)
        def refuse(rows, checkpoint):
            diagnostics = {}
            self.assertEqual((set(), 'unproven'), transactions.validate(
                before, after, rows, os.getpid(), 'check', inst.now_dt(), diagnostics=diagnostics))
            self.assertEqual(checkpoint, diagnostics['checkpoint'])
        # Exact nonempty operands pass coverage validation but grant no final proof.
        refuse(observed, 'replay-final')
        for faults in ([], list(dict(reasons)), {}, {'c' * 32: 'sample damage'},
                       {'b' * 32: 'forged damage', 'a' * 32: 'other damage'}):
            rows = copy.deepcopy(observed)
            next(r for r in rows if r['type'] == 'coverage-exit')['operands']['faults'] = faults
            with self.subTest(raw_faults=faults):
                refuse(rows, 'lifecycle.coverage-exit')
        for faults in (dict(reasons), list(reversed([ref for ref, _ in reasons]))):
            rows = copy.deepcopy(observed)
            next(r for r in rows if r['type'] == 'coverage-row')['record']['faults'] = faults
            with self.subTest(row_faults=faults):
                refuse(rows, 'lifecycle.coverage-exit')

    def test_native_commit_and_abort_have_content_owned_proof(self):
        from brain import rehearse_transactions as transactions
        for failure in (None, 'abort'):
            with self.subTest(failure=failure):
                root, inst, before, after, observed = self.native_transaction_evidence(failure)
                proven, verdict = transactions.validate(before, after, observed, os.getpid(), 'check', inst.now_dt())
                self.assertEqual('proven', verdict)
                self.assertIn('brain/.brain/events.jsonl', proven)
                self.assertTrue(any(o.get('record', {}).get('kind') == ('abort' if failure else 'commit')
                                    for o in observed))
                self.assertFalse(list((root / '.brain/events-leases').iterdir()))

    def test_transaction_forgery_missing_duplicate_reorder_and_corruption_fail_closed(self):
        import copy
        from brain import rehearse_transactions as transactions
        _, inst, before, after, observed = self.native_transaction_evidence()
        def check(a, rows, pid=os.getpid()):
            return transactions.validate(before, a, rows, pid, 'check', inst.now_dt())
        self.assertEqual('proven', check(after, observed)[1])
        mutations = [[], observed[:-1], list(reversed(observed)), observed + observed,
                     [o for o in observed if o['type'] != 'close']]
        foreign = copy.deepcopy(observed)
        for row in foreign:
            row['pid'] += 100000
        mutations.append(foreign)
        for rows in mutations:
            with self.subTest(rows=len(rows)):
                self.assertEqual((set(), 'unproven'), check(after, rows))
        corrupt = copy.deepcopy(after)
        index = json.loads(bytes.fromhex(corrupt['events-index.json']['hex']))
        index['state']['views'] = {}
        index['state']['resources'] = {}
        corrupt['events-index.json']['hex'] = json.dumps(index).encode().hex()
        self.assertEqual((set(), 'unproven'), check(corrupt, observed))
        for edit in ('truncate', 'duplicate', 'foreign-owner', 'malformed'):
            damaged = copy.deepcopy(after)
            payload = bytes.fromhex(damaged['events.jsonl']['hex'])
            if edit == 'truncate':
                payload = payload.rsplit(b'\n', 2)[0] + b'\n'
            elif edit == 'duplicate':
                payload += payload.splitlines(keepends=True)[-1]
            elif edit == 'foreign-owner':
                rows = payload.splitlines(keepends=True)
                row = json.loads(rows[-1])
                row['node'] = 'n:' + 'f' * 64
                rows[-1] = (json.dumps(row) + '\n').encode()
                payload = b''.join(rows)
            else:
                payload += b'{broken\n'
            damaged['events.jsonl']['hex'] = payload.hex()
            with self.subTest(edit=edit):
                self.assertEqual((set(), 'unproven'), check(damaged, observed))

    def test_coverage_has_pre_begin_lock_and_no_retroactive_lease(self):
        try:
            self.start_native_evidence()
            from brain import rehearse_transactions as transactions
            _, inst, before, after, observed = self.native_transaction_evidence()
            self.update_native_evidence(stage='coverage-assertions')
            kinds = [r['type'] for r in observed]
            start = kinds.index('coverage-enter')
            stop = kinds.index('coverage-exit')
            begin = next(i for i, r in enumerate(observed) if r.get('record', {}).get('kind') == 'begin')
            self.assertLess(start, stop)
            self.assertLess(stop, begin)
            self.assertTrue(all(r['owner']['lease'] is None and r['owner']['ref'] is None
                                for r in observed[start:stop + 1]))
            self.assertTrue(all(r['owner']['lock']['pid'] == os.getpid() for r in observed[start:stop + 1]))
            rows = transactions.check_comparisons(after, inst.now_dt())
            self.assertEqual('coverage', rows[0]['kind'])
            self.assertIsNone(rows[0]['view_equal'])
            self.assertNotIn('out_of_scope', rows[0])
            owner_pid, command, instant, diagnostics = os.getpid(), 'check', inst.now_dt(), {}
            self.update_native_evidence(stage='validate', owner_pid=owner_pid, validator_command=command,
                                        validation_instant=instant.isoformat(), diagnostics=diagnostics)
            try:
                validation = transactions.validate(before, after, observed, owner_pid, command, instant,
                                                   diagnostics=diagnostics)
                self.update_native_evidence(validator_result=dict(proven_paths=sorted(validation[0]),
                                                                  verdict=validation[1]),
                                            status=('CAPTURED' if before is not None and after is not None
                                                    else 'INCOMPLETE'), stage='verdict-assertion')
            finally:
                self.retain_native_evidence()
            self.assertEqual('proven', validation[1])
        except BaseException as error:
            self.native_evidence_exception(error)
            raise
        finally:
            if hasattr(self, '_native_evidence'):
                self.retain_native_evidence()

    def test_every_coverage_operand_owner_row_and_lifetime_is_required(self):
        import copy
        from brain import rehearse_transactions as transactions
        _, inst, before, after, observed = self.native_transaction_evidence(compare=True)
        self.assertEqual('proven', transactions.validate(
            before, after, observed, os.getpid(), 'check', inst.now_dt())[1])
        def reject(rows, snapshot=after):
            self.assertEqual((set(), 'unproven'), transactions.validate(
                before, snapshot, rows, os.getpid(), 'check', inst.now_dt()))
        for kind in ('coverage-enter', 'coverage-row', 'coverage-exit'):
            with self.subTest(missing=kind):
                reject([r for r in observed if r['type'] != kind])
            for field, value in (('pid', -1), ('command', 'status'), ('lease', True), ('ref', 'f' * 32),
                                 ('node', 'unrelated')):
                rows = copy.deepcopy(observed)
                next(r for r in rows if r['type'] == kind)['owner'][field] = value
                with self.subTest(kind=kind, owner=field):
                    reject(rows)
            for field in ('serial', 'pid', 'depth', 'ino'):
                rows = copy.deepcopy(observed)
                next(r for r in rows if r['type'] == kind)['owner']['lock'][field] = -1
                with self.subTest(kind=kind, lock=field):
                    reject(rows)
        rows = copy.deepcopy(observed)
        end = next(r for r in rows if r['type'] == 'coverage-exit')
        end['owner']['lock']['serial'] += 1
        reject(rows)
        for field, value in (('actual', []), ('recorded', {}), ('opened', {'f' * 32: {}}),
                             ('nodes', []), ('leases', {'f' * 32: True}), ('faults', ['forged']),
                             ('preferred', 'unrelated')):
            rows = copy.deepcopy(observed)
            next(r for r in rows if r['type'] == 'coverage-exit')['operands'][field] = value
            with self.subTest(operand=field):
                reject(rows)
        rows = copy.deepcopy(observed)
        end = next(r for r in rows if r['type'] == 'coverage-exit')
        end['result'] = not end['result']
        reject(rows)
        for kind in ('coverage-row', 'coverage-enter', 'coverage-exit'):
            i = next(i for i, r in enumerate(observed) if r['type'] == kind)
            reject(observed[:i] + [observed[i]] + observed[i:])
        rows = copy.deepcopy(observed)
        i = next(i for i, r in enumerate(rows) if r['type'] == 'coverage-exit')
        rows.append(rows.pop(i))
        reject(rows)
        for kind in ('coverage', 'comparison'):
            snapshot = copy.deepcopy(after)
            history = transactions.lines(transactions.blob(snapshot, 'events-compare.jsonl'))
            row = next(r for r in history if r['kind'] == kind)
            row['view_equal'] = True if kind == 'coverage' else None
            snapshot['events-compare.jsonl']['hex'] = b''.join(
                (json.dumps(r) + '\n').encode() for r in history).hex()
            reject(observed, snapshot)
        for edit in ('missing', 'duplicate', 'reorder', 'unknown', 'malformed', 'prefix', 'cache'):
            snapshot = copy.deepcopy(after)
            payload = transactions.blob(snapshot, 'events-compare.jsonl')
            history = payload.splitlines(keepends=True)
            if edit == 'missing':
                payload = b''.join(history[1:])
            elif edit == 'duplicate':
                payload += history[-1]
            elif edit == 'reorder':
                payload = b''.join(reversed(history))
            elif edit == 'unknown':
                row = json.loads(history[-1]); row['kind'] = 'unknown'
                payload = b''.join(history[:-1]) + (json.dumps(row) + '\n').encode()
            elif edit == 'malformed':
                payload += b'{broken\n'
            elif edit == 'prefix':
                # Existing historical bytes are preserved independently of rows.
                name = 'events.jsonl'
                snapshot[name]['hex'] = (' ' + bytes.fromhex(snapshot[name]['hex']).decode()).encode().hex()
            else:
                snapshot['events-compare-index.json'] = dict(snapshot['events-compare.jsonl'], hex=b'{}'.hex())
            snapshot['events-compare.jsonl']['hex'] = payload.hex()
            with self.subTest(history=edit):
                reject(observed, snapshot)

    def test_caught_observer_failure_keeps_native_publication_unproven(self):
        from brain import rehearse_transactions as transactions
        with mock.patch.object(transactions.os, 'pread', side_effect=OSError('sample observer failure')):
            _, inst, before, after, observed = self.native_transaction_evidence()
        self.assertTrue(any(r['type'] == 'invalid' for r in observed))
        self.assertTrue(any(r.get('record', {}).get('kind') == 'commit' for r in observed))
        self.assertEqual((set(), 'unproven'), transactions.validate(
            before, after, observed, os.getpid(), 'check', inst.now_dt()))

    def test_partial_failure_retains_open_begin_and_stays_unexpected(self):
        from brain import events, rehearse_transactions as transactions
        root, inst, before, after, observed = self.native_transaction_evidence('partial')
        self.assertEqual((set(), 'unproven'), transactions.validate(
            before, after, observed, os.getpid(), 'check', inst.now_dt()))
        self.assertTrue(events.replay(inst)[2])
        self.assertEqual('Sample partial change\n', (root / 'inbox.md').read_text())
        self.assertFalse(list((root / '.brain/events-leases').iterdir()))

    def test_live_foreign_lease_is_retained_and_never_adopted(self):
        from brain import events, rehearse_transactions as transactions
        owner = []
        def prepare(inst):
            operation = events.Command(inst.root, command='status')
            operation.start()
            owner.append(operation)
            self.addCleanup(operation.close_lease)
        root, inst, before, after, observed = self.native_transaction_evidence(prepare=prepare)
        self.assertEqual((set(), 'unproven'), transactions.validate(
            before, after, observed, os.getpid(), 'check', inst.now_dt()))
        self.assertTrue(events.lease_live(inst, owner[0].ref))
        self.assertIn(owner[0].ref, events.replay(inst)[2])
        self.assertEqual(before['events-leases'], after['events-leases'])

    def test_unowned_native_append_never_becomes_expected_by_path(self):
        import uuid
        from brain import events, rehearse_transactions as transactions
        root, inst, before, after, observed = self.native_transaction_evidence()
        events.append(inst, 'root', 'begin', uuid.uuid4().hex, {'command': 'check'})
        forged = transactions.capture(root)
        self.assertEqual((set(), 'unproven'), transactions.validate(
            before, forged, observed, os.getpid(), 'check', inst.now_dt()))
        self.assertFalse(rehearsal.allowed_write('brain/.brain/events.jsonl', 'fix'))

    def test_real_refresh_command_proves_journal_without_allowing_residuals(self):
        lane = self.tmp / 'native-lane'
        lane.mkdir()
        raw, row, failure = [], None, None
        try:
            root = self.init('native-lane/brain')
            row = rehearsal.run_command(root / 'core', lane, ['status', 'root', '--refresh'],
                                        'refresh', [], raw_output=raw)
        except BaseException as error:
            failure = type(error).__name__
            raise
        finally:
            rehearsal._m_transactions._persist_fixture_evidence(lane, raw, row, failure)
        self.assertEqual(0, row['code'])
        self.assertEqual('proven', row['transaction_evidence'])
        self.assertEqual(0, row['unexpected'])
        self.assertTrue(raw[0]['transaction_evidence']['observations'])
        self.assertTrue(raw[0]['transaction_evidence']['after']['events.jsonl']['hex'])

    def test_unexplained_residual_write_survives_a_proven_transaction(self):
        from brain import rehearse_transactions as transactions
        _, inst, before, after, observed = self.native_transaction_evidence()
        proven, verdict = transactions.validate(before, after, observed, os.getpid(), 'check', inst.now_dt())
        self.assertEqual('proven', verdict)
        changed = proven | {'brain/.brain/unknown-state.json', 'brain/private.txt'}
        residual = {p for p in changed if not rehearsal.allowed_write(p, 'refresh') and p not in proven}
        self.assertEqual({'brain/.brain/unknown-state.json', 'brain/private.txt'}, residual)

    def test_registered_hook_latency_is_measured(self):
        clock = mock.Mock()
        clock.monotonic.side_effect = [100, 101.5]
        with mock.patch.object(rehearsal, 'time', clock):
            r = self.runner('import json, os\n'
                            'assert json.load(open(os.environ["BRAIN_CONFIG"]))["brains"]\n'
                            'print("registered hook ran")\n', kind='hook')
        self.assertEqual(0, r['code'])
        self.assertIn('registered hook ran', r['text'])
        self.assertEqual(1.5, r['seconds'])
        self.assertFalse(r['timeout'])
        self.assertEqual(2, clock.monotonic.call_count)
        self.assertIn('hook: latency exceeds 1 s', rehearsal.compare({'hook': result()}, {'hook': r}))

    def test_read_command_cannot_rewrite_machine_bindings(self):
        r = self.runner('from pathlib import Path\nPath(".brain").mkdir(exist_ok=True)\n'
                        'Path(".brain/local.json").write_text("{}")\n')
        self.assertEqual(0, r['code'])
        self.assertEqual(1, r['unexpected'])
        self.assertTrue(rehearsal.allowed_write('brain/.brain/local.json', 'fix'))
        self.assertFalse(rehearsal.allowed_write('brain/.brain/unknown-cache.json', 'read'))

    def test_outside_process_survives_candidate_signal(self):
        victim = subprocess.Popen(['/bin/sleep', '10'])
        try:
            r = self.runner('import os, signal\nos.kill(' + str(victim.pid) + ', signal.SIGKILL)\n')
            self.assertNotEqual(0, r['code'])
            self.assertIsNone(victim.poll())
        finally:
            if victim.poll() is None:
                victim.terminate()
            victim.wait()

    def test_detached_drain_is_reaped_before_snapshot(self):
        script = ('import sys, time\nfrom pathlib import Path\n'
                  'if "--drain" in sys.argv:\n'
                  '    time.sleep(0.5)\n'
                  '    Path("late-write").write_text("oops")\n'
                  'else:\n'
                  '    sys.path.insert(0, ' + repr(str(CORE / 'lib')) + ')\n'
                  '    from brain import hosts, common\n'
                  '    from types import SimpleNamespace\n'
                  '    common.CORE = Path(__file__).parents[1]\n'
                  '    hosts.wake_start(SimpleNamespace(root=Path.cwd()), {"id": "root"})\n')
        r = self.runner(script)
        self.assertEqual(0, r['code'], r['text'])
        import time
        time.sleep(0.7)
        self.assertFalse((self.tmp / 'lane/brain/late-write').exists())
        self.assertEqual(0, r['unexpected'])

    def test_invalid_utf8_is_replaced(self):
        r = self.runner('import os\nos.write(1, b"\\xff")\n')
        self.assertEqual(0, r['code'])
        self.assertEqual('\ufffd', r['text'])

    def test_sandbox_self_check_and_shadow_executable_refusal(self):
        lane = self.tmp / 'boundary'
        lane.mkdir()
        rehearsal.sandbox_self_check(lane)
        tools = self.tmp / 'tools'
        tools.mkdir()
        fake = tools / 'sandbox-exec'
        fake.write_text('#!/bin/sh\nexit 0\n')
        fake.chmod(0o755)
        with mock.patch.dict(os.environ, {'PATH': str(tools) + os.pathsep + os.environ['PATH']}):
            with self.assertRaises(rehearsal.RehearsalError):
                rehearsal.sandbox_self_check(lane)
        with mock.patch.object(rehearsal, 'sandbox_prefix', return_value=[]):
            with self.assertRaises(rehearsal.RehearsalError):
                rehearsal.sandbox_self_check(lane)

    def test_alias_escapes_are_denied_and_source_hardlinks_are_detached(self):
        outside = self.tmp / 'outside'
        outside.write_text('unchanged')
        lane = self.tmp / 'lane'
        (lane / 'brain').mkdir(parents=True)
        (lane / 'brain/alias').symlink_to(outside)
        r = self.runner('from pathlib import Path\nPath("alias").write_text("changed")\n')
        self.assertNotEqual(0, r['code'])
        self.assertEqual('unchanged', outside.read_text())
        r = self.runner('import os\nos.link(' + repr(str(outside)) + ', "new-alias")\n')
        self.assertNotEqual(0, r['code'])
        self.assertEqual('unchanged', outside.read_text())
        source = self.tmp / 'source'
        source.mkdir()
        os.link(outside, source / 'hardlink')
        rehearsal.copy_tree(source, self.tmp / 'copy')
        (self.tmp / 'copy/hardlink').write_text('copy only')
        self.assertEqual('unchanged', outside.read_text())

    def test_os_boundary_preserves_live_brain_home_and_git(self):
        live = self.repo(self.tmp / 'live', {'note.md': 'original\n'})
        outside_home = self.tmp / 'operator'
        outside_home.mkdir()
        note = outside_home / 'note'
        note.write_text('original home')
        original = rehearsal.snapshot(live)
        script = 'from pathlib import Path\nPath(' + repr(str(live / 'note.md')) + ').write_text("mutated")\n'
        r = self.runner(script)
        self.assertNotEqual(0, r['code'])
        self.assertEqual(original, rehearsal.snapshot(live))
        r = self.runner('from pathlib import Path\nPath(' + repr(str(note)) + ').write_text("mutated")\n')
        self.assertNotEqual(0, r['code'])
        self.assertEqual('original home', note.read_text())
        r = self.runner('import socket\nsocket.create_connection(("127.0.0.1", 9))\n')
        self.assertNotEqual(0, r['code'])

    def test_caught_external_write_still_fails_and_empty_directory_is_seen(self):
        outside = self.tmp / 'outside'
        outside.write_text('unchanged')
        script = ('from pathlib import Path\ntry:\n    Path(' + repr(str(outside)) +
                  ').write_text("changed")\nexcept OSError:\n    pass\n')
        raw = []
        r = self.runner(script, raw_output=raw)
        self.assertEqual(0, r['code'])
        self.assertEqual(1, r['unexpected'])
        self.assertEqual('unchanged', outside.read_text())
        self.assertEqual([dict(event='open', path=str(outside))], raw[0]['denied_attempts'])
        self.assertEqual(1, r['denied_attempts'])
        self.assertNotIn(str(outside), json.dumps(r))
        r = self.runner('from pathlib import Path\nPath("unapproved").mkdir()\n')
        self.assertEqual(1, r['unexpected'])

    def test_repeated_pathlib_builtin_and_os_open_attempts_are_individually_audited(self):
        outside = self.tmp / 'outside'
        outside.write_text('unchanged')
        for operation, attempts in (('Path(target).write_text("changed")', 2),
                                    ('open(target, "w").close()', 2),
                                    ('os.close(os.open(target, os.O_WRONLY | os.O_TRUNC))', 2),
                                    ('separate_attempts()', 4)):
            with self.subTest(operation=operation):
                script = ('import os\nfrom pathlib import Path\ntarget=' + repr(str(outside)) +
                          '\ndef lowlevel():\n    os.close(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC))\n'
                          'def separate_attempts():\n    try:\n        open(target, "w").close()\n'
                          '    except OSError:\n        pass\n    lowlevel()\n'
                          '\nfor _ in range(2):\n    try:\n        ' + operation +
                          '\n    except OSError:\n        pass\n')
                raw = []
                row = self.runner(script, raw_output=raw)
                self.assertEqual(0, row['code'])
                self.assertEqual(1, row['unexpected'])
                self.assertEqual(attempts, row['denied_attempts'])
                self.assertEqual([dict(event='open', path=str(outside))] * attempts,
                                 raw[0]['denied_attempts'])
                self.assertEqual('unchanged', outside.read_text())


    def test_unexpected_copy_mutation_is_detected(self):
        r = self.runner('from pathlib import Path\nPath("node.md").write_text("changed")\n')
        self.assertEqual(0, r['code'])
        self.assertEqual(1, r['unexpected'])

    def test_write_attribution_retains_strict_journal_failure(self):
        raw = []
        r = self.runner('from pathlib import Path\nPath(".brain").mkdir()\n'
                        'Path(".brain/events.jsonl").write_text("journal bytes")\n', raw_output=raw)
        self.assertEqual(1, r['unexpected'])
        self.assertEqual([hashlib.sha256(b'brain/.brain/events.jsonl').hexdigest()],
                         r['unexpected_path_digests'])
        self.assertIn(dict(path='brain/.brain/events.jsonl', operation='created', unexpected=True),
                      raw[0]['writes'])
        self.assertNotIn('journal bytes', json.dumps(raw))

    def test_never_name_output_is_redacted_without_publishing_it(self):
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed), contextlib.redirect_stderr(printed):
            r = self.runner('print("invented-canary")', terms=[('invented-canary', 'term')])
        self.assertEqual(0, r['code'])
        self.assertEqual('[…]', r['text'])
        self.assertEqual('', printed.getvalue())


class RehearsalEvidence(Scenario):
    def test_train_rehearsal_uses_recorded_base_after_origin_main_advances(self):
        from brain.instance import Instance
        from brain.verify import ready_state
        r = self.init('sample')
        home = self.repo(self.tmp / 'app')
        main = self.git('rev-parse', 'main', cwd=home).strip()
        self.git('remote', 'add', 'origin', str(home), cwd=home)
        self.git('update-ref', 'refs/remotes/origin/main', main, cwd=home)
        self.git('symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/main', cwd=home)
        self.b(r, 'new', 'a', '--title', 'A', '--home', str(home))
        self.b(r, 'set', 'a', 'verify=true', 'rehearse=required')
        self.b(r, 'wt', 'add', 'a', 'job')
        wt = self.tmp / 'app-work/worktrees/job'
        self.git('commit', '--allow-empty', '-qm', 'job change', cwd=wt)
        inst = Instance(r)
        n = inst.node('a')
        head = self.git('rev-parse', 'job', cwd=home).strip()
        evidence = dict(version=1, node='a', branch='job', head=head, main=main,
                        integration_base=main, passed=True,
                        installed_core_commit=rehearsal.installed_core_commit(r))
        path = rehearsal.evidence_path(n, 'job')
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(evidence))
        self.b(r, 'ready', '--summary', 'Synthetic result', 'a', '--branch', 'job', '--record', '--reviews', '1')
        self.git('commit', '--allow-empty', '-qm', 'advance origin main', cwd=home)
        newer = self.git('rev-parse', 'HEAD', cwd=home).strip()
        self.git('update-ref', 'refs/remotes/origin/main', newer, cwd=home)
        with mock.patch.dict(os.environ, self.env):
            train = ready_state(inst, n, 'job', train=True)
            self.assertTrue(train['ready'], train['reasons'])
            self.assertFalse(ready_state(inst, n, 'job')['ready'])
            path.write_text(json.dumps({**evidence, 'integration_base': newer}))
            mismatched = ready_state(inst, n, 'job', train=True)
            self.assertTrue(mismatched['ready'], mismatched['reasons'])
            self.assertIsNone(rehearsal.read_evidence(n, 'job', head, main=main, integration_base=main))

    def test_failed_stale_and_unreadable_reports_do_not_block_or_get_rewritten(self):
        from brain.instance import Instance
        r = self.init('sample')
        home = self.repo(self.tmp / 'app')
        self.b(r, 'new', 'a', '--title', 'A', '--home', str(home))
        self.b(r, 'set', 'a', 'verify=true', 'rehearse=required')
        self.b(r, 'wt', 'add', 'a', 'job')
        wt = self.tmp / 'app-work/worktrees/job'
        self.git('commit', '--allow-empty', '-qm', 'change', cwd=wt)
        n = Instance(r).node('a')
        path = rehearsal.evidence_path(n, 'job')
        path.parent.mkdir(parents=True)
        head = self.git('rev-parse', 'job', cwd=home).strip()
        main = self.git('rev-parse', 'main', cwd=home).strip()
        evidence = dict(version=1, node='a', branch='job', head=head, main=main,
                        integration_base=main, passed=True,
                        installed_core_commit=rehearsal.installed_core_commit(r))
        for report in ({**evidence, 'passed': False, 'regressions': ['raw failure']},
                       {**evidence, 'head': main}, {**evidence, 'main': head},
                       {**evidence, 'installed_core_commit': 'old'}, 'unreadable'):
            with self.subTest(report=report):
                raw = json.dumps(report) if isinstance(report, dict) else report
                path.write_text(raw)
                self.assertIn('ready', self.b(r, 'handback', '--summary', 'Synthetic result',
                                            'a', '--branch', 'job', '--state', 'done', '--reviews', '1'))
                self.assertEqual('ready', self.b(r, 'ready', 'a', '--branch', 'job').strip())
                self.assertEqual(raw, path.read_text())
                self.assertIsNone(rehearsal.read_evidence(n, 'job', head))
        (wt / '.env').write_text('CHANGED=1')
        self.assertIn('verify_inputs changed', self.b(r, 'ready', 'a', '--branch', 'job', ok=False))

    def test_legacy_rehearsal_reason_is_ignored_without_ignoring_other_reasons(self):
        r = self.init('sample')
        home = self.repo(self.tmp / 'app')
        self.b(r, 'new', 'a', '--title', 'A', '--home', str(home))
        self.b(r, 'set', 'a', 'verify=true', 'rehearse=required')
        self.b(r, 'wt', 'add', 'a', 'job')
        self.git('commit', '--allow-empty', '-qm', 'change', cwd=self.tmp / 'app-work/worktrees/job')
        self.b(r, 'handback', '--summary', 'Synthetic result', 'a', '--branch', 'job',
               '--state', 'done', '--reviews', '1')
        path = next((r / 'nodes/a/job-handbacks').glob('*.json'))
        packet = json.loads(path.read_text())
        legacy = 'passing rehearsal missing for this head and main; run brain rehearse'
        for reasons, ready in (([legacy], True), ([legacy, 'node verify missing, changed or failed'], False)):
            packet['ready_reasons'] = reasons
            path.write_text(json.dumps(packet))
            out = self.b(r, 'ready', 'a', '--branch', 'job', ok=ready)
            self.assertNotIn(legacy, out)
            if not ready:
                self.assertIn('node verify missing, changed or failed', out)
        packet['ready_reasons'] = [legacy]
        path.write_text(json.dumps(packet))
        self.b(r, 'handback', '--summary', 'Same evidence', 'a', '--branch', 'job', '--state', 'done')
        self.assertEqual([], json.loads(path.read_text())['ready_reasons'])

    def test_installed_core_commit_change_invalidates_evidence(self):
        from brain.instance import Instance
        r = self.init('sample')
        home = self.repo(self.tmp / 'app')
        self.b(r, 'new', 'a', '--title', 'A', '--home', str(home))
        self.git('branch', 'job', cwd=home)
        self.git('init', '-q', '-b', 'main', cwd=r / 'core')
        self.git('commit', '--allow-empty', '-qm', 'installed baseline', cwd=r / 'core')
        n = Instance(r).node('a')
        head = self.git('rev-parse', 'job', cwd=home).strip()
        evidence = dict(version=1, node='a', branch='job', head=head, main=head,
                        integration_base=head, passed=True,
                        installed_core_commit=rehearsal.installed_core_commit(r))
        path = rehearsal.evidence_path(n, 'job')
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(evidence))
        self.assertEqual(evidence, rehearsal.read_evidence(n, 'job', head))
        self.git('commit', '--allow-empty', '-qm', 'upgraded baseline', cwd=r / 'core')
        self.assertIsNone(rehearsal.read_evidence(n, 'job', head))

    def test_legacy_required_is_advisory_but_head_and_main_still_bind(self):
        r = self.init('sample')
        home = self.repo(self.tmp / 'app')
        self.b(r, 'new', 'a', '--title', 'A', '--home', str(home))
        self.b(r, 'set', 'a', 'verify=true', 'rehearse=required')
        self.b(r, 'wt', 'add', 'a', 'job')
        wt = self.tmp / 'app-work/worktrees/job'
        self.git('commit', '--allow-empty', '-qm', 'change', cwd=wt)
        self.assertIn('ready', self.b(r, 'ready', '--summary', 'Synthetic result', 'a', '--branch', 'job', '--record', '--reviews', '1'))
        self.assertFalse((self.tmp / 'app-work/scratch/rehearse').exists())
        head = self.git('rev-parse', 'job', cwd=home).strip()
        main = self.git('rev-parse', 'main', cwd=home).strip()
        import hashlib
        path = self.tmp / 'app-work/scratch/rehearse' / (hashlib.sha256(b'job').hexdigest() + '.json')
        path.parent.mkdir(parents=True)
        evidence = dict(version=1, node='a', branch='job', head=head, main=main,
                        integration_base=main, passed=True, commands=7,
                        installed_core_commit=rehearsal.installed_core_commit(r))
        path.write_text(json.dumps(evidence))
        self.assertIn('ready', self.b(r, 'ready', '--summary', 'Synthetic result', 'a', '--branch', 'job', '--record', '--reviews', '1'))
        packet = json.loads(next((r / 'nodes/a/job-handbacks').glob('*.json')).read_text())['ready']
        self.assertEqual(evidence, packet['rehearsal'])
        reused = self.b(r, 'ready', '--summary', 'Synthetic result', 'a', '--branch', 'job', '--record', '--reviews', '1')
        self.assertIn('verify: reused', reused)
        packet = json.loads(next((r / 'nodes/a/job-handbacks').glob('*.json')).read_text())['ready']
        self.assertEqual(evidence, packet['rehearsal'])
        self.assertIn('input_files_ref', packet)
        self.assertEqual('ready', self.b(r, 'ready', 'a', '--branch', 'job').strip())
        baseline = r / 'core/CHANGES.md'
        old = baseline.read_text()
        baseline.write_text(old + '\nBaseline changed\n')
        self.assertEqual('ready', self.b(r, 'ready', 'a', '--branch', 'job').strip())
        baseline.write_text(old)
        self.git('commit', '--allow-empty', '-qm', 'new head', cwd=wt)
        self.assertIn('branch head changed', self.b(r, 'ready', 'a', '--branch', 'job', ok=False))
        self.git('commit', '--allow-empty', '-qm', 'new main', cwd=home)
        self.assertIn('main moved', self.b(r, 'ready', 'a', '--branch', 'job', ok=False))

@unittest.skipUnless(sys.platform == 'darwin', 'requires macOS OS sandbox')
class RehearsalPipeline(Scenario):
    def test_committed_candidate_clean_pass_and_all_incident_mutations(self):
        """Actual subprocess orchestration with fabricated installed/candidate CLIs."""
        from brain.instance import Instance
        r = self.init('sample')
        home = self.tmp / 'candidate'
        (home / 'bin').mkdir(parents=True)
        fixture = '''import sys
command = sys.argv[1]
mode = MODE
if command == 'due':
    print('[{"kind":"hold","text":"invented-canary"},{"kind":"load","text":"new noise"}]'
          if mode else '[{"kind":"hold","text":"invented-canary"}]')
elif command in ('check',):
    print('✗ upgrade cannot heal' if mode else '✓ checks pass')
    sys.exit(1 if mode else 0)
elif command == 'ready':
    print('not ready: changed main resolution' if mode else 'ready')
    sys.exit(1 if mode else 0)
elif command == 'brief':
    print('quiet' if mode else 'Stop when: ask for consent!')
elif command == 'ui':
    print('{}')
elif command == 'status':
    print('updated')
elif command == 'hook':
    import json, os, time
    if mode and json.load(open(os.environ['BRAIN_CONFIG']))['brains']:
        time.sleep(1.5)
'''
        baseline = fixture.replace('MODE', 'False')
        (home / 'bin/brain').write_text(baseline)
        (home / 'lib/brain').mkdir(parents=True)
        (home / 'lib/brain/__init__.py').write_text('')
        (home / 'lib/brain/instance.py').write_text('class Instance:\n    pass\n')
        self.git('init', '-q', '-b', 'main', cwd=home)
        self.git('add', '.', cwd=home)
        self.git('commit', '-qm', 'fixture', cwd=home)
        self.b(r, 'new', 'a', '--title', 'A', '--home', str(home))
        self.b(r, 'wt', 'add', 'a', 'job')
        # Register a second open record to prove every ready record is exercised.
        self.b(r, 'wt', 'add', 'a', 'other')
        self.b(r, 'new', 'b', '--title', 'B')
        # Root and sibling probes can legitimately name another node's term.
        (r / 'nodes/b/never-name.txt').write_text('invented-canary\n')
        (r / 'core/bin/brain').write_text(baseline)
        original = rehearsal.snapshot(r)
        inst = Instance(r)
        n = inst.node('a')
        clean = rehearsal.rehearse(inst, n, 'job')
        self.assertTrue(clean['passed'], clean['regressions'])
        self.assertEqual(0, clean['changed_outputs'])
        self.assertNotIn('invented-canary', json.dumps(clean))
        self.assertEqual(original, rehearsal.snapshot(r))
        wt = self.tmp / 'candidate-work/worktrees/job'
        (wt / 'bin/brain').write_text(fixture.replace('MODE', 'True'))
        self.git('commit', '-qam', 'incident mutations', cwd=wt)
        bad = rehearsal.rehearse(inst, n, 'job')
        self.assertFalse(bad['passed'])
        for flag in ('fix: new check errors', 'ready:1:0: ready became not ready',
                     'ready:1:1: ready became not ready', 'due:0: new due items',
                     'due:1: new due items', 'brief:0: brief warning disappeared', 'hook: latency exceeds 1 s'):
            self.assertIn(flag, bad['regressions'])
        self.assertEqual(original, rehearsal.snapshot(r))
        # Only the redacted error diagnostic is retained; no path or private id.
        self.assertIn('✗ upgrade cannot heal', bad['diagnostics'][0]['added_errors'])
        self.assertNotIn('invented-canary', json.dumps(bad))
        self.assertNotEqual(clean['output_digests']['due:0'], bad['output_digests']['due:0'])

class RehearsalDelivery(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)

    def tearDown(self):
        self.folder.cleanup()

    def test_real_post_user_agent_status_codes_and_no_redirect_or_url_output(self):
        import http.server
        import threading
        from brain.instance import Instance
        seen = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append((self.path, self.headers.get('User-Agent'),
                             self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(int(self.path.split('/')[1]))
                self.send_header('Location', '/204/redirected-secret')
                self.end_headers()
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = 'http://127.0.0.1:' + str(server.server_port)
        nodes = [dict(id='root' if i == 0 else 'n' + str(i),
                      local=dict(coordinator='thread', wake=base + '/' + str(status) + '/capability'))
                 for i, status in enumerate((204, 403, 302))]
        (self.root / 'instance.md').write_text('---\nname: sample\n---\n')
        inst = Instance(self.root)
        for node in nodes:
            directory = self.root if node['id'] == 'root' else self.root / 'nodes' / node['id']
            directory.mkdir(parents=True, exist_ok=True)
            (directory / 'node.md').write_text('---\nid: ' + node['id'] + '\nhome: here\n---\n')
            inst.local['nodes'][node['id']] = node['local']
        try:
            statuses = rehearsal.probe_delivery(inst, inst.nodes())
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        self.assertEqual([204, 403, 302], [s['status'] for s in statuses])
        self.assertEqual(['wake:1: HTTP 403', 'wake:2: HTTP 302'], rehearsal.delivery_regressions(statuses))
        self.assertEqual(3, len(seen))
        self.assertTrue(all(agent and 'Python-urllib' not in agent for _, agent, _ in seen))
        for path, _, body in seen:
            self.assertEqual(json.loads(body), {'event': 'brain-wake',
                             'reason': 'new request ' + hashlib.sha256((base + path).encode()).hexdigest()})
        self.assertNotIn('capability', json.dumps(statuses))
        self.assertNotIn(base, json.dumps(statuses))

    def test_outward_links_refuse_delivery_and_shared_addresses_are_deduplicated(self):
        from types import SimpleNamespace
        node = dict(id='root', local=dict(coordinator='t', wake='https://example.invalid/private'))
        other = dict(id='other', local=dict(coordinator='t2', wake=node['local']['wake']))
        inst = SimpleNamespace(root=self.root, node=lambda _: node, local_of=lambda _: {})
        with mock.patch.object(rehearsal._m_common, 'outward_links', return_value=['unsafe']), \
                mock.patch('urllib.request.OpenerDirector.open') as post:
            with self.assertRaises(rehearsal.RehearsalError):
                rehearsal.probe_delivery(inst, [node, other])
            post.assert_not_called()
        with mock.patch.object(rehearsal._m_common, 'outward_links', return_value=[]), \
                mock.patch.object(rehearsal, 'isolated_probe_instance', return_value=inst), \
                mock.patch.object(rehearsal._m_hosts, 'wake_probe', return_value=204, create=True) as post:
            self.assertEqual([dict(coordinator=0, status=204)], rehearsal.probe_delivery(inst, [node, other]))
            post.assert_called_once()

    def test_real_delivery_seeds_only_the_command_copy_cache(self):
        import http.server
        import threading
        from brain.instance import Instance
        seen = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                seen.append(True)
                self.send_response(204)
                self.end_headers()
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        (self.root / 'instance.md').write_text('---\nname: sample\n---\n')
        (self.root / 'node.md').write_text('---\nid: root\nhome: here\n---\n')
        inst = Instance(self.root)
        inst.local['nodes']['root'] = dict(coordinator='t', wake='http://127.0.0.1:' + str(server.server_port))
        original = rehearsal.snapshot(self.root)
        try:
            with tempfile.TemporaryDirectory() as td:
                private = rehearsal.isolated_probe_instance(inst, Path(td))
                statuses = rehearsal.probe_delivery(inst, [inst.node('root')], private)
                self.assertEqual([dict(coordinator=0, status=204)], statuses)
                self.assertEqual(204, rehearsal._m_hosts.wake_probe(private, private.node('root')))
                self.assertEqual(1, len(seen))
                with self.assertRaises(rehearsal.RehearsalError):
                    rehearsal.probe_delivery(inst, [inst.node('root')], inst)
            self.assertEqual(original, rehearsal.snapshot(self.root))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_shared_probe_is_preferred_and_cache_is_isolated(self):
        from brain.instance import Instance
        (self.root / 'instance.md').write_text('---\nname: sample\n---\n')
        (self.root / 'node.md').write_text('---\nid: root\nhome: here\n---\n')
        inst = Instance(self.root)
        inst.local['nodes']['root'] = dict(coordinator='t', wake='https://example.invalid/private')
        node = inst.node('root')
        original = rehearsal.snapshot(self.root)
        def probe(private, target, force=False):
            self.assertTrue(force)
            self.assertNotEqual(inst.root, private.root)
            (private.root / '.brain/probe-cache').write_text('cached')
            return 403
        with mock.patch.object(rehearsal._m_hosts, 'wake_probe', side_effect=probe, create=True) as shared:
            self.assertEqual([dict(coordinator=0, status=403)], rehearsal.probe_delivery(inst, [node]))
            shared.assert_called_once()
        self.assertEqual(original, rehearsal.snapshot(self.root))


class RehearsalGitFormats(Scenario):
    def test_reftable_and_sha256_formats_survive_without_hooks_or_helpers(self):
        for name, option, key, value in [('reftable', '--ref-format=reftable', 'extensions.refstorage', 'reftable'),
                                         ('sha256', '--object-format=sha256', 'extensions.objectformat', 'sha256')]:
            with self.subTest(format=name):
                source = self.tmp / name
                source.mkdir()
                init = subprocess.run(['git', 'init', '-q', '-b', 'main', option], cwd=source,
                                      env=self.env, capture_output=True, text=True)
                if init.returncode and ('unknown option' in init.stderr or 'unknown ref storage format' in init.stderr):
                    self.skipTest('installed Git does not support this repository format')
                self.assertEqual(0, init.returncode, init.stderr)
                (source / 'note').write_text('fixture')
                self.git('add', '.', cwd=source)
                self.git('commit', '-qm', 'fixture', cwd=source)
                canary = self.tmp / (name + '-hook-ran')
                hook = source / '.git/hooks/post-checkout'
                hook.parent.mkdir(exist_ok=True)
                hook.write_text('#!/bin/sh\ntouch ' + str(canary) + '\n')
                hook.chmod(0o755)
                self.git('config', 'core.hooksPath', str(hook.parent), cwd=source)
                self.git('config', 'credential.helper', 'fixture-helper', cwd=source)
                dest = self.tmp / (name + '-copy')
                rehearsal.copy_tree(source, dest)
                rehearsal.materialize_git(source, dest)
                self.assertEqual(value, self.git('config', '--get', key, cwd=dest).strip())
                self.assertEqual(self.git('rev-parse', 'HEAD', cwd=source), self.git('rev-parse', 'HEAD', cwd=dest))
                self.git('checkout', '--detach', 'HEAD', cwd=dest)
                self.assertFalse(canary.exists())
                config = self.git('config', '--local', '--list', cwd=dest)
                self.assertNotIn('fixture-helper', config)
                self.assertIn('core.hookspath=/dev/null', config)
