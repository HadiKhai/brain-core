"""Owned synthetic driver, real CLI bootstrap and existing lifecycle hooks."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import types
from unittest import mock

from scenario import CORE, Scenario
import core_loader


DRIVER = '''import json, os, sys, time
from pathlib import Path
request = json.load(sys.stdin)
context = request.pop('context')
log = Path(__file__).parent.parent / 'calls.jsonl'
with log.open('a') as stream:
    stream.write(json.dumps(dict(method=request['method'], step=request['step'], context=context, pid=os.getpid())) + '\\n')
mode = sys.argv[1]
ownership = {key: context[key] for key in ('resource', 'host', 'run_id')}
ownership.update(lease='synthetic-lease', receipt='synthetic-owner')
if request['method'] == 'acquire':
    result = ownership
elif request['method'] == 'release':
    result = dict(ownership=context['ownership'], released=True, quiescent=True)
else:
    result = dict(ok=not (mode == 'known-failure' and request['step'] == 'migration'),
        receipt='synthetic-' + request['step'], ownership=context['ownership'], quiescent=True)
    if request['step'] == 'migration':
        if mode == 'nonzero': sys.exit(7)
        if mode == 'missing': sys.exit(0)
        if mode == 'timeout': time.sleep(30)
        if mode == 'duplicate': print('{"version":1,"version":1}'); sys.exit(0)
        if mode == 'oversized': print('x'*65537); sys.exit(0)
        if mode == 'stderr': sys.stderr.write('x'*16385); sys.stderr.flush()
        if mode == 'foreign': result['ownership'] = dict(ownership, resource='foreign-resource')
        if mode == 'unquiescent': result['quiescent'] = False
        if mode == 'mismatch': request['context_sha256'] = '0'*64
        if mode == 'version': request['version'] = 2
        if mode == 'protocol': request['protocol'] = 'foreign-v2'
        if mode == 'nan': result['ok'] = float('nan')
response = dict(request, result=result)
print(json.dumps(response))
if mode == 'trailing' and request['step'] == 'migration': print('{}')
'''


class PreviewStdio(Scenario):
    def fixture(self, mode='success', install=True):
        root = self.tmp / 'instance'
        root.mkdir()
        (root / 'instance.md').write_text('---\ntimezone: UTC\n---\n')
        (root / 'node.md').write_text('---\ntitle: Synthetic\nhome: external\n---\n')
        product = self.repo(self.tmp / 'product', {'driver.py': DRIVER})
        head = self.git('rev-parse', 'HEAD', cwd=product).strip()
        config = dict(version=1, enabled=True, adapter='synthetic', protocol='synthetic-v1',
            resource='synthetic-resource', backup='snapshot', migration='schema', restart='restart',
            acceptance='accept', rollback='restore')
        (root / '.brain').mkdir(mode=0o700)
        (root / '.brain/local.json').write_text(json.dumps(dict(nodes=dict(root=dict(
            home=str(product), preview_upgrade=config)))))
        # Xcode's interpreter can sit below group-writable /Applications.
        executable = str(Path('/Library/Developer/CommandLineTools/usr/bin/python3'
                              if sys.platform == 'darwin' else sys.executable).resolve())
        driver = str(product / 'driver.py')
        registration = dict(version=1, name='synthetic', protocol='synthetic-v1', executable=executable,
            argv=[driver, mode], checkout=str(product), head=head,
            files={path: hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in (executable, driver)})
        packet = self.tmp / 'registration.json'
        packet.write_text(json.dumps(registration))
        packet.chmod(0o600)
        self.env['BRAIN_ROOT'] = str(root)
        if install:
            self.command(root, 'preview-adapter', 'root', '--register', str(packet))
        return root, product, head, packet, registration

    def command(self, root, *args, ok=True):
        result = subprocess.run([sys.executable, '-B', str(CORE / 'bin/brain'), *args],
            cwd=root, env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
        return result.stdout

    def reconcile(self, root, head):
        return json.loads(self.command(root, 'preview-upgrade', 'root', '--head', head))

    def calls(self):
        path = self.tmp / 'calls.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_real_cli_bootstrap_success_and_persistent_no_replay(self):
        root, product, head, _, _ = self.fixture()
        result = self.reconcile(root, head)
        self.assertEqual(result['state'], 'succeeded')
        self.assertTrue(result['released'])
        self.assertEqual([call['step'] or call['method'] for call in self.calls()],
                         ['acquire', 'backup', 'migration', 'restart', 'acceptance', 'release'])
        self.assertEqual(self.calls()[2]['context']['backup_receipt'], 'synthetic-backup')
        self.assertEqual(self.reconcile(root, head), result)
        self.assertEqual(len(self.calls()), 6)
        self.assertEqual((root / '.brain/preview-adapter.json').stat().st_mode & 0o777, 0o600)

    def test_real_known_failure_uses_existing_rollback_and_release(self):
        root, _, head, _, _ = self.fixture('known-failure')
        result = self.reconcile(root, head)
        self.assertEqual(result['state'], 'failed')
        self.assertTrue(result['rolled_back'])
        self.assertTrue(result['released'])
        self.assertEqual([call['step'] or call['method'] for call in self.calls()],
                         ['acquire', 'backup', 'migration', 'rollback', 'release'])
        self.assertEqual(self.calls()[3]['context']['backup_receipt'], 'synthetic-backup')

    def unknown(self, mode):
        root, _, head, _, _ = self.fixture(mode)
        foreign = self.tmp / 'foreign-resource'
        foreign.write_bytes(b'foreign resource remains untouched\n')
        result = self.reconcile(root, head)
        self.assertEqual(result['state'], 'pending')
        self.assertEqual(result['active_step'], 'migration')
        self.assertFalse(result['released'])
        self.assertEqual([call['step'] or call['method'] for call in self.calls()], ['acquire', 'backup', 'migration'])
        self.assertEqual(self.reconcile(root, head), result)
        self.assertEqual(len(self.calls()), 3)
        self.assertEqual(foreign.read_bytes(), b'foreign resource remains untouched\n')
        # Only owned direct invocations are joined. No descendant claim follows.
        for call in self.calls():
            with self.assertRaises(ProcessLookupError):
                os.kill(call['pid'], 0)

    def test_nonzero_stays_pending_without_rollback_release_or_retry(self): self.unknown('nonzero')
    def test_timeout_joins_direct_child_and_stays_pending(self): self.unknown('timeout')
    def test_missing_output_stays_pending(self): self.unknown('missing')
    def test_duplicate_output_stays_pending(self): self.unknown('duplicate')
    def test_oversized_output_stays_pending(self): self.unknown('oversized')
    def test_oversized_stderr_stays_pending(self): self.unknown('stderr')
    def test_context_mismatch_stays_pending(self): self.unknown('mismatch')
    def test_protocol_mismatch_stays_pending(self): self.unknown('protocol')
    def test_version_mismatch_stays_pending(self): self.unknown('version')
    def test_foreign_ownership_stays_pending(self): self.unknown('foreign')
    def test_product_must_assert_quiescence(self): self.unknown('unquiescent')
    def test_nonfinite_output_stays_pending(self): self.unknown('nan')
    def test_extra_json_output_stays_pending(self): self.unknown('trailing')

    def test_unregistered_and_duplicate_registration_never_invoke_driver(self):
        root, _, head, packet, _ = self.fixture(install=False)
        self.assertEqual(self.reconcile(root, head)['state'], 'pending')
        self.command(root, 'preview-adapter', 'root', '--register', str(packet))
        before = (root / '.brain/preview-adapter.json').read_bytes()
        self.command(root, 'preview-adapter', 'root', '--register', str(packet), ok=False)
        self.assertEqual((root / '.brain/preview-adapter.json').read_bytes(), before)
        self.assertEqual(self.calls(), [])

    def test_changed_driver_identity_refuses_before_acquisition(self):
        root, product, head, _, _ = self.fixture()
        (product / 'driver.py').write_text(DRIVER + '\n# changed\n')
        self.assertEqual(self.reconcile(root, head)['state'], 'pending')
        self.assertFalse((root / '.brain/preview-upgrades').exists())
        self.assertEqual(self.calls(), [])

    def test_replaced_driver_inode_refuses_even_with_same_content_and_clean_head(self):
        root, product, head, _, _ = self.fixture()
        replacement = self.tmp / 'replacement.py'
        replacement.write_text(DRIVER)
        replacement.replace(product / 'driver.py')
        self.assertEqual(self.reconcile(root, head)['state'], 'pending')
        self.assertEqual(self.calls(), [])

    def test_configured_protocol_mismatch_and_stale_main_never_acquire(self):
        root, _, head, _, _ = self.fixture()
        self.assertEqual(self.reconcile(root, 'a'*40)['state'], 'pending')
        local = root / '.brain/local.json'
        value = json.loads(local.read_text())
        value['nodes']['root']['preview_upgrade']['protocol'] = 'other-v1'
        local.write_text(json.dumps(value))
        self.assertEqual(self.reconcile(root, head)['state'], 'pending')
        self.assertEqual(self.calls(), [])

    def test_private_registration_schema_permissions_and_links_refuse(self):
        root, _, _, packet, original = self.fixture(install=False)
        for change in (dict(original, version=2), dict(original, files={}),
                       dict(original, executable='relative'), dict(original, extra=True)):
            packet.write_text(json.dumps(change))
            self.command(root, 'preview-adapter', 'root', '--register', str(packet), ok=False)
        packet.write_text('{"version":1,"version":1}')
        self.command(root, 'preview-adapter', 'root', '--register', str(packet), ok=False)
        packet.write_text(json.dumps(original))
        packet.chmod(0o644)
        self.command(root, 'preview-adapter', 'root', '--register', str(packet), ok=False)
        packet.chmod(0o600)
        linked = self.tmp / 'linked.json'
        linked.symlink_to(packet)
        self.command(root, 'preview-adapter', 'root', '--register', str(linked), ok=False)
        linked.unlink()
        os.link(packet, linked)
        self.command(root, 'preview-adapter', 'root', '--register', str(linked), ok=False)
        self.assertFalse((root / '.brain/preview-adapter.json').exists())
        self.assertEqual(self.calls(), [])

    def test_train_post_publication_and_jobs_hook_bootstrap_without_registry_patch(self):
        root, _, head, _, _ = self.fixture()
        m = core_loader.SourceFileLoader('stdio_hooks', str(CORE / 'bin/brain')).load_module()
        inst = m.Instance(root)
        n = inst.node('root')
        journal = root / 'synthetic-train.json'
        data = dict(tip=head, accepted=[], publication_succeeded=True, settle_requested=False)
        journal.write_text(json.dumps(data))
        with mock.patch.dict(os.environ, self.env, clear=True):
            m.post_publication(inst, n, journal, data)
            self.assertEqual(json.loads(journal.read_text())['preview_upgrade']['state'], 'succeeded')
            m.job_preview_after_landing(inst, n, dict(Outcome='merged'))
        self.assertEqual(len(self.calls()), 6)
        self.assertEqual(m.PREVIEW_ADAPTERS, {})

    def test_oversized_input_never_spawns(self):
        m = core_loader.SourceFileLoader('stdio_input', str(CORE / 'bin/brain')).load_module()
        with self.assertRaisesRegex(ValueError, 'request exceeds'):
            m.bounded_call(['/missing-synthetic-driver'], str(self.tmp), dict(value='x'*65536))


LONG_DRIVER = '''import json, os, sys, time
from pathlib import Path
request = json.load(sys.stdin)
context = request.pop('context')
folder = Path(__file__).parent.parent
with (folder / 'calls.jsonl').open('a') as stream:
    stream.write(json.dumps(dict(request, context=context, pid=os.getpid())) + '\\n')
mode = sys.argv[1]
method = request['method']
original = request.get('operation', {}).get('method', method)
ownership = {key: context[key] for key in ('resource', 'host', 'run_id')}
ownership.update(lease='synthetic-lease', receipt='synthetic-owner')
if original == 'acquire':
    value = ownership
elif original == 'release':
    value = dict(ownership=context['ownership'], released=True, quiescent=True)
else:
    value = dict(ok=not (mode == 'failure' and request['step'] == 'migration'), receipt='synthetic-' + request['step'],
                 ownership=context['ownership'], quiescent=True)
result = value
if method in ('start', 'status'):
    record = folder / ('operation-' + request['operation']['id'] + '.json')
    binding = dict(operation=request['operation'], context=context, step=request['step'])
    if method == 'start':
        if record.exists(): sys.exit(9)
        record.write_text(json.dumps(dict(binding=binding, started=time.time())))
        state = 'pending'
    elif not record.exists() or mode == 'unknown':
        state = 'unknown'
    else:
        saved = json.loads(record.read_text())
        if saved['binding'] != binding: sys.exit(8)
        if mode == 'crash': sys.exit(7)
        if mode == 'slow-call': time.sleep(3)
        state = ('pending' if mode == 'timeout' or time.time() - saved['started'] < .4
                 else 'failed' if mode == 'failure' else 'done')
    if mode == 'foreign' and method == 'status': value['ownership']['resource'] = 'foreign'
    if mode == 'unquiescent' and method == 'status': value['quiescent'] = False
    if mode == 'failed-true' and method == 'status': state = 'failed'
    if mode == 'done-false' and method == 'status': state = 'done'; value['ok'] = False
    if mode == 'invalid-state' and method == 'status': state = 'success'
    result = dict(state=state, receipt='synthetic-observation',
                  value=value if state in ('done', 'failed', 'success') else None)
    if mode == 'no-evidence' and method == 'status': result['receipt'] = ''
    if mode == 'pending-value' and method == 'start': result['value'] = value
    if mode.startswith('mismatch-') and method == 'status':
        key = mode[len('mismatch-'):]
        if key == 'operation': request[key]['id'] = 'f' * 32
        elif key == 'timeout': request['operation']['timeout_seconds'] = float(request['operation']['timeout_seconds'])
        elif key == 'version': request[key] = 2
        else: request[key] = 'foreign'
print(json.dumps(dict(request, result=result)))
'''


class PreviewLongOperations(Scenario):
    command = PreviewStdio.command
    calls = PreviewStdio.calls

    def fixture(self, mode='success', deadlines=None):
        root, product, _, packet, registration = PreviewStdio.fixture(self, mode, install=False)
        (product / 'driver.py').write_text(LONG_DRIVER)
        self.git('add', 'driver.py', cwd=product)
        self.git('commit', '-m', 'synthetic long driver', cwd=product)
        head = self.git('rev-parse', 'HEAD', cwd=product).strip()
        registration.update(head=head, operation_deadlines=deadlines or {'migration': 3})
        registration['files'][str(product / 'driver.py')] = hashlib.sha256(LONG_DRIVER.encode()).hexdigest()
        packet.write_text(json.dumps(registration))
        self.command(root, 'preview-adapter', 'root', '--register', str(packet))
        m = core_loader.SourceFileLoader('long_preview', str(CORE / 'bin/brain')).load_module()
        return root, product, head, m

    def reconcile(self, root, head, m):
        with mock.patch.dict(os.environ, self.env, clear=True):
            return m.landed_preview(m.Instance(root), m.Instance(root).node('root'), head)

    def test_slow_operation_exceeds_individual_call_budget_and_preserves_binding(self):
        root, _, head, m = self.fixture()
        with mock.patch.object(m, 'CALL_SECONDS', .3):
            result = self.reconcile(root, head, m)
        self.assertEqual(result['state'], 'succeeded')
        calls = self.calls()
        long = [call for call in calls if call['method'] in ('start', 'status')]
        self.assertEqual(long[0]['method'], 'start')
        self.assertTrue(any(call['method'] == 'status' for call in long))
        self.assertEqual(len({call['request_id'] for call in long}), len(long))
        for call in long:
            self.assertEqual(call['operation'], long[0]['operation'])
            self.assertEqual(call['context'], long[0]['context'])
            self.assertEqual(call['context_sha256'], long[0]['context_sha256'])
        self.assertEqual([call['step'] or call['method'] for call in calls if call not in long],
                         ['acquire', 'backup', 'restart', 'acceptance', 'release'])
        self.assertEqual(self.reconcile(root, head, m), result)
        self.assertEqual(len(self.calls()), len(calls))

    def test_all_lifecycle_operation_types_can_opt_in(self):
        root, _, head, m = self.fixture(deadlines={name: 3 for name in
            ('acquire', 'backup', 'migration', 'restart', 'acceptance', 'rollback', 'release')})
        result = self.reconcile(root, head, m)
        self.assertEqual(result['state'], 'succeeded')
        self.assertTrue(result['released'])
        self.assertEqual([call['step'] or call['operation']['method'] for call in self.calls()
                          if call['method'] == 'start'],
                         ['acquire', 'backup', 'migration', 'restart', 'acceptance', 'release'])

    def test_known_failure_rolls_back_and_releases(self):
        root, _, head, m = self.fixture('failure')
        result = self.reconcile(root, head, m)
        self.assertEqual(result['state'], 'failed')
        self.assertTrue(result['released'])
        self.assertTrue(result['rolled_back'])
        self.assertEqual([call['step'] for call in self.calls() if call['method'] == 'step'],
                         ['backup', 'rollback'])

    def unknown(self, mode, seconds=3):
        root, _, head, m = self.fixture(mode, {'migration': seconds})
        started = time.monotonic()
        result = self.reconcile(root, head, m)
        elapsed = time.monotonic() - started
        self.assertEqual(result['state'], 'pending')
        self.assertEqual(result['active_step'], 'migration')
        self.assertFalse(result['released'])
        self.assertIn(result['reason'], ('product step outcome unknown',
                                       'product step did not confirm a bounded receipt'))
        calls = self.calls()
        self.assertEqual(sum(call['method'] == 'start' for call in calls), 1)
        self.assertFalse(any(call['step'] == 'rollback' or call['method'] == 'release' for call in calls))
        self.assertEqual(self.reconcile(root, head, m), result)
        self.assertEqual(self.calls(), calls)
        for call in calls:
            with self.assertRaises(ProcessLookupError): os.kill(call['pid'], 0)
        return elapsed

    def test_total_deadline_is_not_reset_by_pending_status(self):
        self.assertLess(self.unknown('timeout', 1), 5)

    def test_status_subprocess_is_bounded_by_remaining_operation_time(self):
        self.assertLess(self.unknown('slow-call', 1), 5)

    def test_crash_mid_operation_is_unknown(self): self.unknown('crash')
    def test_status_unknown_id_is_unknown(self): self.unknown('unknown')
    def test_status_foreign_ownership_is_unknown(self): self.unknown('foreign')
    def test_status_unconfirmed_quiescence_is_unknown(self): self.unknown('unquiescent')
    def test_failed_state_cannot_claim_success(self): self.unknown('failed-true')
    def test_done_state_cannot_hide_known_failure(self): self.unknown('done-false')
    def test_invalid_status_state_is_unknown(self): self.unknown('invalid-state')
    def test_status_requires_evidence(self): self.unknown('no-evidence')
    def test_pending_cannot_supply_terminal_value(self): self.unknown('pending-value')
    def test_operation_id_binding_mismatch_refuses(self): self.unknown('mismatch-operation')
    def test_status_request_id_binding_mismatch_refuses(self): self.unknown('mismatch-request_id')
    def test_status_context_binding_mismatch_refuses(self): self.unknown('mismatch-context_sha256')
    def test_status_protocol_binding_mismatch_refuses(self): self.unknown('mismatch-protocol')
    def test_status_version_binding_mismatch_refuses(self): self.unknown('mismatch-version')
    def test_status_deadline_type_mismatch_refuses(self): self.unknown('mismatch-timeout')

    def test_late_done_at_exact_deadline_is_unknown(self):
        m = core_loader.SourceFileLoader('long_deadline', str(CORE / 'bin/brain')).load_module()
        adapter = m.StdioPreviewAdapter(dict(name='synthetic', protocol='synthetic-v1',
            executable='/not-invoked', argv=[], checkout=str(self.tmp),
            operation_deadlines={'migration': 1}), {}, None, {})
        clock = [0]
        fake_time = types.SimpleNamespace(monotonic=lambda: clock[0])
        def late(argv, cwd, request, seconds):
            self.assertEqual(seconds, 1)
            clock[0] = 1
            return {**{k: v for k, v in request.items() if k != 'context'}, 'result': dict(
                state='done', receipt='observed', value=dict(ok=True, receipt='done',
                ownership={}, quiescent=True))}
        with mock.patch.object(m._owners['StdioPreviewAdapter'], 'time', fake_time), \
                mock.patch.object(adapter, 'check'), mock.patch.object(m, 'bounded_call', side_effect=late):
            with self.assertRaisesRegex(ValueError, 'outcome unknown'):
                adapter.step('migration', {})

    def test_registration_change_after_start_refuses_without_status_or_replay(self):
        root, _, head, m = self.fixture()
        original = m.bounded_call
        def replaced(argv, cwd, request, seconds=None):
            response = original(argv, cwd, request, seconds)
            if request['method'] == 'start':
                path = root / '.brain/preview-adapter.json'
                saved = json.loads(path.read_text())
                saved['registration']['protocol'] = 'changed-v2'
                path.write_text(json.dumps(saved))
            return response
        with mock.patch.object(m, 'bounded_call', side_effect=replaced):
            result = self.reconcile(root, head, m)
        self.assertEqual(result['state'], 'pending')
        self.assertFalse(result['released'])
        self.assertEqual([call['method'] for call in self.calls()], ['acquire', 'step', 'start'])

    def test_unknown_status_handle_never_starts_work(self):
        root, _, _, m = self.fixture()
        saved = m.private_json(root / '.brain/preview-adapter.json')
        adapter = m.StdioPreviewAdapter(saved['registration'], saved['identity'],
                                       root / '.brain/preview-adapter.json', saved)
        context = dict(resource='synthetic-resource', host='synthetic-host', run_id='synthetic-run', ownership={})
        result = adapter.exchange('status', 'migration', context,
                                  dict(id='0'*32, method='step', timeout_seconds=3), time.monotonic()+3)
        self.assertEqual(result, dict(state='unknown', receipt='synthetic-observation', value=None))
        self.assertEqual([call['method'] for call in self.calls()], ['status'])
        self.assertEqual(list(self.tmp.glob('operation-*.json')), [])

    def test_deadline_registration_refuses_invalid_maps_before_driver_invocation(self):
        root, _, _, packet, original = PreviewStdio.fixture(self, install=False)
        for deadlines in ({}, {'migration': True}, {'migration': 0}, {'migration': 86401},
                          {'migration': 1.5}, {'schema': 300}, [], None):
            packet.write_text(json.dumps(dict(original, operation_deadlines=deadlines)))
            self.command(root, 'preview-adapter', 'root', '--register', str(packet), ok=False)
        self.assertFalse((root / '.brain/preview-adapter.json').exists())
        self.assertEqual(self.calls(), [])
