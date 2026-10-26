"""Owned local fixtures; no native host calls or physical power-loss claims."""
import importlib
import json
import os
import stat
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import core_loader

CORE = Path(__file__).resolve().parent.parent


class MachineDurability(unittest.TestCase):
    def test_recovery_changes_select_every_nonselective_consumer(self):
        import lanes
        matches = importlib.import_module('.verify_lanes', self.package).matches
        mapping = lanes.lane_map()
        missing = [lane['name'] for lane in mapping
                   if not matches('lib/brain/metadata_recovery.py', lane['inputs'])]
        self.assertEqual(missing, [], 'recovery callable changes must invalidate every consumer')
        target = 'tests/test_machine_durability.py::MachineDurability'
        self.assertEqual([lane['name'] for lane in mapping if target in lane['command'].split()],
                         ['machine'])

    def setUp(self):
        self.m = core_loader.SourceFileLoader('machine_durability_fixture', str(CORE/'bin/brain')).load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root/'instance.md').write_text('---\nhost: t3\nprincipals: sample\n---\n')
        (self.root/'node.md').write_text('---\ntitle: Sample\n---\n')
        self.inst = self.m.Instance(self.root)
        self.package = self.m._owners['Instance'].__package__
        def git(*args):
            return subprocess.run(['git', *args], cwd=self.root, capture_output=True, text=True, check=True).stdout.strip()
        self.git = git
        (self.root/'.gitignore').write_text('.brain/\n')
        git('init', '-q')
        git('config', 'user.name', 'Sample')
        git('config', 'user.email', 'sample@example.invalid')
        git('add', '.'); git('commit', '-qm', 'fixture')

    def hosts(self):
        return importlib.import_module('.host_instances', self.package)

    def test_separate_inventory_is_private_and_remote_route_never_calls_local_host(self):
        h = self.hosts()
        h.remember_host_instance(self.inst, 'desk', self.inst.local['machine'], 't3', {'root':{'coordinator':'desk-thread'}})
        h.remember_host_instance(self.inst, 'other', 'other-machine', 't3', {'root':{'coordinator':'other-thread'}})
        h.select_host_instance(self.inst, 'root', 'other')
        plan = h.host_route(self.m.Instance(self.root), 'root')
        self.assertEqual(plan['state'], 'remote')
        self.assertEqual(plan['instance'], 'other')
        with mock.patch.object(self.m, 'host_call') as call:
            with self.assertRaisesRegex(ValueError, 'remote'):
                self.m.provision_coordinator(self.m.Instance(self.root), 'root', {'instanceId':'sample','model':'sample'})
            call.assert_not_called()
        self.assertNotIn('other-thread', (self.root/'node.md').read_text())
        self.assertEqual(stat.S_IMODE(self.inst.local_path.stat().st_mode), 0o600)

    def test_local_route_requires_exact_existing_binding_and_revalidates_each_call(self):
        h = self.hosts()
        self.inst.local_of('root')['coordinator'] = 'desk-thread'
        self.inst.save_local()
        h.remember_host_instance(self.inst, 'desk', self.inst.local['machine'], 't3', {'root':{'coordinator':'desk-thread'}})
        h.select_host_instance(self.inst, 'root', 'desk')
        calls = []
        rpc = h.routed_host_rpc(self.inst, 'root', lambda *args: calls.append(args))
        rpc('t3_thread_read', {'threadId':'desk-thread'})
        other = self.m.Instance(self.root)
        other.local_of('root')['coordinator'] = 'replacement'
        other.save_local()
        with self.assertRaisesRegex(ValueError, 'binding|changed'):
            rpc('t3_thread_organize', {'threadId':'desk-thread','action':'settle'})
        self.assertEqual(len(calls), 1)

    def test_inventory_refuses_replacement_and_nonbinding_fields(self):
        h = self.hosts()
        h.remember_host_instance(self.inst, 'desk', 'machine-a', 't3', {})
        with self.assertRaisesRegex(ValueError, 'already|conflict'):
            h.remember_host_instance(self.inst, 'desk', 'machine-b', 't3', {})
        with self.assertRaises(ValueError):
            h.remember_host_instance(self.inst, 'bad', 'machine-a', 't3', {'root':{'wake':'https://host.example/private'}})
        self.assertEqual(h.host_instance_inventory(self.m.Instance(self.root))['desk']['machine'], 'machine-a')

    def test_owned_rotation_advances_descriptor_without_losing_route_fence(self):
        h = self.hosts()
        self.inst.local_of('root')['coordinator'] = 'old'
        self.inst.save_local()
        h.remember_host_instance(self.inst, 'desk', self.inst.local['machine'], 't3', {'root':{'coordinator':'old'}})
        h.select_host_instance(self.inst, 'root', 'desk')
        calls = []
        rpc = h.routed_host_rpc(self.inst, 'root', lambda *args: calls.append(args))
        with self.m.brain_lock(self.inst):
            h.set_owned_host_binding(self.inst, 'root', 'coordinator', 'old', 'new')
            self.inst.save_local()
        rpc('t3_thread_read', {'threadId':'new'})
        self.assertEqual(len(calls), 1)
        self.assertEqual(h.host_route(self.m.Instance(self.root),'root')['bindings']['coordinator'], 'new')

    def test_auto_settle_policy_is_unsupported_before_provisioning(self):
        with mock.patch.object(self.m, 'host_call') as call:
            with self.assertRaisesRegex(ValueError, 'auto.settle|unsupported'):
                self.m.provision_coordinator(self.inst, 'root', {'instanceId':'sample','model':'sample'}, require_no_auto_settle=True)
            call.assert_not_called()
        self.assertNotIn('host_setup_pending', self.inst._load_local()['nodes'].get('root', {}))

    def test_created_ancestors_are_synced_before_atomic_publication(self):
        folder = self.root/'new'/'nested'
        events = []
        real = os.fsync
        def sync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                events.append((os.fstat(fd).st_dev, os.fstat(fd).st_ino))
            return real(fd)
        with mock.patch.object(self.m.os, 'fsync', side_effect=sync):
            self.m.durable_makedirs(folder)
            self.m.atomic_write(folder/'record', 'complete')
        expected = [(p.stat().st_dev,p.stat().st_ino) for p in (self.root, self.root/'new', folder)]
        self.assertEqual(events, expected)

    def test_existing_ancestors_are_resynced_after_interrupted_creation(self):
        folder = self.root/'new'/'nested'
        with mock.patch.object(self.m.os, 'fsync', side_effect=OSError('fixture sync failure')):
            with self.assertRaises(OSError):
                self.m.durable_makedirs(folder)
        self.m.durable_makedirs(folder)
        self.m.atomic_write(folder/'record', 'complete')
        self.assertEqual((folder/'record').read_text(), 'complete')

    def test_recovery_completes_partial_metadata_once_and_preserves_foreign_edits(self):
        recovery = importlib.import_module('.metadata_recovery', self.package)
        a, b = self.root/'node.md', self.root/'schedules.md'
        before = a.read_text()
        packet = self.root/'.brain/settings-batches/fixture.json'
        packet.parent.mkdir(parents=True)
        packet.write_text(json.dumps({'state':'pending','base':self.git('rev-parse','HEAD'),'before':{'node.md':before,'schedules.md':None},
                                     'after':{'node.md':before+'new\n','schedules.md':'# Sample\n'},'operations':2}))
        a.write_text(before+'new\n')
        with mock.patch.object(self.inst, 'commit_settings_batch') as commit:
            recovery.recover_settings_batch(self.inst, packet)
            recovery.recover_settings_batch(self.inst, packet)
        self.assertEqual(commit.call_count, 1)
        self.assertEqual(b.read_text(), '# Sample\n')
        self.assertEqual(json.loads(packet.read_text())['state'], 'recovered')
        data = json.loads(packet.read_text()); data['state'] = 'pending'; packet.write_text(json.dumps(data))
        a.write_text('foreign edit')
        with self.assertRaisesRegex(ValueError, 'changed|foreign'):
            recovery.recover_settings_batch(self.inst, packet)
        self.assertEqual(a.read_text(), 'foreign edit')

    def test_process_crash_packet_recovers_exact_files_with_real_git(self):
        recovery = importlib.import_module('.metadata_recovery', self.package)
        def git(*args):
            return subprocess.run(['git', '-c', 'user.name=Sample', '-c', 'user.email=sample@example.invalid',
                                   *args], cwd=self.root, capture_output=True, text=True, check=True).stdout.strip()
        original = git('rev-parse', 'HEAD')
        script = """
import os, signal, sys
from pathlib import Path
sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1])
from brain import common, instance, settings_batch
root = Path(sys.argv[2]); inst = instance.Instance(root)
write = common.atomic_write
def fault(path, text):
    if path == root/'schedules.md':
        os.kill(os.getpid(), signal.SIGKILL)
    return write(path, text)
common.atomic_write = fault
with instance.brain_lock(inst):
    settings_batch.publish_settings_batch(inst, {root/'node.md': (root/'node.md').read_text()+'new\\n',
                                               root/'schedules.md':'# Sample\\n'}, 2)
"""
        child = subprocess.run([sys.executable, '-c', script, str(CORE/'lib'), str(self.root)],
                               capture_output=True, text=True, timeout=15)
        self.assertEqual(child.returncode, -signal.SIGKILL, child.stderr)
        packets = list((self.root/'.brain/settings-batches').glob('*.json'))
        self.assertEqual(len(packets), 1)
        record = json.loads(packets[0].read_text())
        self.assertEqual(record['state'], 'pending')
        self.assertFalse((self.root/'schedules.md').exists())
        # An unrelated staged path must survive the path-limited recovery commit.
        (self.root/'unrelated').write_text('keep staged')
        git('add', 'unrelated')
        recovery.recover_settings_batch(self.inst, packets[0])
        head = git('rev-parse', 'HEAD')
        self.assertNotEqual(original, head)
        self.assertEqual((self.root/'schedules.md').read_text(), record['after']['schedules.md'])
        self.assertEqual(git('diff', '--cached', '--name-only'), 'unrelated')
        recovery.recover_settings_batch(self.inst, packets[0])
        self.assertEqual(git('rev-parse', 'HEAD'), head)

    def test_recovery_refuses_unsafe_packet_target_and_changed_head(self):
        recovery = importlib.import_module('.metadata_recovery', self.package)
        for relative in ('../outside', '.brain/other.json', 'notes.md', 'nodes/../node.md'):
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                recovery.metadata_target(self.root, relative)
        packet = self.root/'.brain/settings-batches/fixture.json'
        packet.parent.mkdir(parents=True)
        packet.write_text(json.dumps({'state':'pending','base':'different-head',
                           'before':{'node.md':(self.root/'node.md').read_text()},'after':{'node.md':'new'}}))
        with self.assertRaisesRegex(ValueError, 'HEAD changed'):
            recovery.recover_settings_batch(self.inst, packet)
        self.assertIn('Sample', (self.root/'node.md').read_text())

    def test_remote_lifecycle_and_observation_consumers_refuse_before_rpc(self):
        h = self.hosts()
        self.inst.local_of('root').update(coordinator='local-thread',host_project='project')
        self.inst.save_local()
        h.remember_host_instance(self.inst, 'other', 'other-machine', 't3', {'root':{'coordinator':'remote-thread'}})
        h.select_host_instance(self.inst, 'root', 'other')
        for module, name, args in (
            ('host_lifecycle', 'settle_closed_jobs', (self.inst, self.inst.node('root'))),
            ('worker_lifecycle', 'launch_worker', (self.inst, self.inst.node('root'), 'a'*24)),
            ('coordinator_lifecycle', 'rotate_coordinator', (self.inst, self.inst.node('root'))),
            ('host_observations', '_host_export', (self.inst,))):
            with self.subTest(module=module):
                call = mock.Mock()
                part = importlib.import_module('.'+module, self.package)
                with self.assertRaisesRegex(ValueError, 'remote'):
                    getattr(part, name)(*args, rpc=call)
                call.assert_not_called()

    def test_new_batch_cannot_bypass_unfinished_owner(self):
        packet = self.root/'.brain/settings-batches/fixture.json'
        packet.parent.mkdir(parents=True)
        packet.write_text(json.dumps({'state':'pending'}))
        before = (self.root/'node.md').read_text()
        with self.assertRaisesRegex(SystemExit, 'unfinished settings batch'):
            self.m.publish_settings_batch(self.inst, {self.root/'node.md':'replacement'}, 1)
        self.assertEqual((self.root/'node.md').read_text(), before)

    def test_inventory_cli_and_generic_set_keep_route_out_of_shared_metadata(self):
        bindings = self.root/'bindings.json'
        bindings.write_text(json.dumps({'root':{'coordinator':'remote-thread'}}))
        env = dict(os.environ, BRAIN_ROOT=str(self.root), BRAIN_NO_REEXEC='1',
                   BRAIN_CORE=str(CORE), PYTHONDONTWRITEBYTECODE='1')
        def cli(*args):
            return subprocess.run([sys.executable, str(CORE/'bin/brain'), *args], cwd=self.root,
                                  env=env, capture_output=True, text=True, timeout=20)
        result = cli('host-instance','remember','other','--machine','other-machine','--adapter','t3','--file',str(bindings))
        self.assertEqual(result.returncode, 0, result.stderr)
        result = cli('host-instance','route','other')
        self.assertEqual(result.returncode, 0, result.stderr)
        result = cli('host-instance','plan')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['state'], 'remote')
        self.assertNotIn('remote-thread', result.stdout)
        result = cli('set','root','host_instance=other')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('host_instance', (self.root/'node.md').read_text())
        self.assertNotIn('other-machine', self.git('show','HEAD:node.md'))

    def test_provisioning_extends_only_its_addressed_local_entry(self):
        h = self.hosts()
        h.remember_host_instance(self.inst, 'desk', self.inst.local['machine'], 't3', {'root':{}})
        h.select_host_instance(self.inst, 'root', 'desk')
        def call(inst, name, args):
            return {'id':'project'} if name == 't3_project_create' else {'threadId':'coordinator'}
        with mock.patch.object(self.m, 'host_call', side_effect=call):
            self.m.provision_coordinator(self.inst, 'root', {'instanceId':'sample','model':'sample'})
        fresh = self.m.Instance(self.root)
        self.assertEqual(h.require_local_host(fresh,'root')['bindings'],
                         {'host_project':'project','coordinator':'coordinator'})
