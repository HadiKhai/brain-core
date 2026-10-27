"""Managed source indexes are opt-in, deterministic and scoped to owned output."""
import json
from scenario import Scenario


class ManagedIndex(Scenario):
    cli_in_process = True
    def app(self):
        root = self.init('demo')
        home = self.repo(self.tmp / 'code', {'source.md': 'first\n'})
        self.b(root, 'new', 'app', '--title', 'App', '--home', str(home))
        return root, home

    def test_configured_index_regenerates_on_write_and_records_new_input_hash(self):
        root, home = self.app()
        self.b(root, 'set', 'app', 'upstream_inputs=source.md', 'upstream_output=index.md', 'upstream_generator=links')
        first = (home / 'index.md').read_text()
        self.assertIn('[source.md](source.md)', first)
        self.assertIn('index fresh', self.b(root, 'index', 'app'))
        (home / 'source.md').write_text('second\n')
        self.assertIn('index stale', self.b(root, 'index', 'app'))
        self.b(root, 'backlog', 'app', 'add', 'deferred item')
        self.assertNotEqual((home / 'index.md').read_text(), first)
        self.assertIn('index fresh', self.b(root, 'index', 'app'))
        before = (home / 'index.md').stat().st_mtime_ns
        self.b(root, 'index', 'app', '--refresh')
        self.assertEqual((home / 'index.md').stat().st_mtime_ns, before)

    def test_unowned_output_and_external_links_are_preserved(self):
        root, home = self.app()
        (home / 'index.md').write_text('independent index\n')
        self.b(root, 'set', 'app', 'upstream_inputs=source.md', 'upstream_output=index.md', 'upstream_generator=links')
        self.b(root, 'index', 'app', '--refresh', ok=False)
        self.assertEqual((home / 'index.md').read_text(), 'independent index\n')
        (home / 'index.md').unlink()
        outside = self.tmp / 'outside.md'
        outside.write_text('foreign\n')
        (home / 'index.md').symlink_to(outside)
        self.b(root, 'index', 'app', '--refresh', ok=False)
        self.assertEqual(outside.read_text(), 'foreign\n')

    def test_freshness_only_configuration_never_creates_an_output(self):
        root, home = self.app()
        self.b(root, 'set', 'app', 'upstream_inputs=source.md', 'upstream_output=index.md')
        self.b(root, 'backlog', 'app', 'add', 'deferred item')
        self.assertFalse((home / 'index.md').exists())
        self.assertIn('index missing', self.b(root, 'index', 'app'))
