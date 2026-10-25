"""Projection is an explicit owned phase, never a cleanup side effect."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from brain import events, instance, status, status_projection
from scenario import Scenario


class OwnedStatusProjection(Scenario):
    def fixture(self):
        root = self.init('demo')
        self.b(root, 'new', 'app', '--title', 'App')
        self.b(root, 'set', 'app', 'status_projection=owned-facts')
        inst = instance.Instance(root)
        return inst, inst.node('app')

    def test_phase_preserves_source_bytes_and_archive_and_is_idle_idempotent(self):
        inst, node = self.fixture()
        path = node['dir'] / 'status.md'
        source = '# App\r\nUpdated: fixture\r\n\r\n## Now\r\n- custom judgement\r\n\r\n## Active work\r\n| job/a | live owner | - | working |\r\n'
        path.write_bytes(source.encode())
        archive = node['dir'] / 'status-archive.md'; archive.write_bytes(b'archive source\r\n')
        with events.Command(inst.root, command='project_status', node='app'):
            self.assertTrue(status_projection.project_owned(inst, node))
        projected = path.read_bytes()
        self.assertTrue(projected.startswith(source.encode()))
        self.assertEqual(archive.read_bytes(), b'archive source\r\n')
        self.assertIn(b'| job/a | live owner | - | working |', projected)
        with events.Command(inst.root, command='project_status', node='app'):
            self.assertFalse(status_projection.project_owned(inst, node))
        self.assertEqual(path.read_bytes(), projected)
        self.assertEqual(events.pending(events.read(inst)), {})

    def test_native_phase_requires_opt_in_and_refuses_interrupted_owner(self):
        root = self.init('demo')
        self.b(root, 'new', 'app', '--title', 'App')
        self.assertIn('requires status_projection', self.b(root, 'status', 'app', '--project', ok=False))
        self.b(root, 'set', 'app', 'status_projection=owned-facts')
        inst = instance.Instance(root); node = inst.node('app')
        before = (node['dir'] / 'status.md').read_bytes()
        events.append(inst, 'app', 'begin', 'interrupted-fixture', {'command': 'fixture'})
        with self.assertRaises(SystemExit):
            status_projection.project_owned(inst, node)
        self.assertEqual((node['dir'] / 'status.md').read_bytes(), before)

    def test_broken_markers_refuse_without_touching_source(self):
        inst, node = self.fixture()
        path = node['dir'] / 'status.md'; path.write_text('source\n' + status.FACTS_START)
        before = path.read_bytes()
        with self.assertRaises(SystemExit):
            status_projection.project_owned(inst, node)
        self.assertEqual(path.read_bytes(), before)
