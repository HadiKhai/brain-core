"""Owned synthetic driver, real CLI bootstrap and existing lifecycle hooks."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
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
