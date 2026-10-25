"""Optional semantic recall uses fake vectors; tests never download or load models."""
import importlib.util
import json
from unittest import mock
import core_loader
from scenario import CORE, Scenario


class SemanticRecall(Scenario):
    def fixture(self):
        r = self.init('alice')
        self.b(r, 'new', 'app', '--title', 'Application')
        for item, body in (('serialization', 'Serialize concurrent mutations.'), ('palette', 'Paint the interface.')):
            self.b(r, 'item', 'add', item, '--kind', 'lesson', '--title', item,
                   '--from', 'app', '--ref', 'report.md:3', '--body', body)
        loader = core_loader.SourceFileLoader('semantic_core', str(CORE / 'bin/brain'))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        core = importlib.util.module_from_spec(spec)
        loader.exec_module(core)
        inst = core.Instance(r)
        return r, core, inst, inst.node('app')

    @staticmethod
    def vectors(texts):
        return [[1, 0] if 'races' in t.lower() or 'concurrent' in t.lower() else [0, 1] for t in texts]

    def test_retrieves_meaning_without_shared_words_and_reuses_only_unchanged_documents(self):
        r, core, inst, node = self.fixture()
        self.assertEqual(json.loads(self.b(r, 'recall', 'app', 'races', '--json'))['items'], [])
        embed = mock.Mock(side_effect=self.vectors)
        found, _ = core.semantic_ranked(inst, node, 'races', embedder=embed)
        self.assertEqual([x[1] for x in found], ['serialization'])
        self.assertEqual(len(embed.call_args[0][0]), 3)
        core.semantic_ranked(inst, node, 'races', embedder=embed)
        self.assertEqual(embed.call_args[0][0], ['query: races'])
        (r / 'library/serialization.md').write_text((r / 'library/serialization.md').read_text() + '\nChanged content.')
        core.semantic_ranked(inst, node, 'races', embedder=embed)
        self.assertEqual(len(embed.call_args[0][0]), 2)
        self.assertTrue(all('Changed content' not in p.read_text() for p in (r / '.brain/semantic').glob('*.json')))

    def test_revocation_state_and_content_policy_apply_before_cache_and_embedding(self):
        r, core, inst, node = self.fixture()
        core.semantic_ranked(inst, node, 'races', embedder=self.vectors)
        self.b(r, 'item', 'quarantine', 'serialization', '--reason', 'withdrawn')
        embed = mock.Mock(side_effect=self.vectors)
        found, _ = core.semantic_ranked(core.Instance(r), node, 'races', embedder=embed)
        self.assertEqual(found, [])
        self.assertEqual(embed.call_args[0][0], ['query: races'])
        self.b(r, 'item', 'restore', 'serialization')
        (r / 'nodes/app/never-name.txt').write_text('concurrent\n')
        self.assertEqual(core.semantic_ranked(core.Instance(r), node, 'races', embedder=self.vectors)[0], [])

    def test_embedding_failure_or_corrupt_cache_falls_back_without_a_crash(self):
        r, core, inst, node = self.fixture()
        for answer in ([], [[float('nan')]], [[True]], [[0, 0]], [[1, 0], [1], [1, 0]]):
            self.assertIsNone(core.semantic_ranked(inst, node, 'races', embedder=lambda texts: answer))
        with mock.patch.object(core._owners['local_embed'], 'semantic_capability', return_value=(True, 'fake')), \
                mock.patch.object(core._owners['local_embed'], 'local_embed', side_effect=RuntimeError('failed')):
            self.assertIsNone(core.semantic_ranked(inst, node, 'races'))
        self.assertIn('serialization', self.b(r, 'recall', 'app', 'concurrent'))
        self.assertIn('lexical', self.b(r, 'check', ok=None))
        self.assertIn('lexical recall remains available', self.b(r, 'recall', '--setup'))

    def test_foreign_scope_and_proposals_remain_separate(self):
        r, core, inst, node = self.fixture()
        self.b(r, 'principal', 'add', 'team')
        self.b(r, 'new', 'private', '--title', 'Private', '--owners', 'team')
        self.b(r, 'item', 'add', 'secret', '--kind', 'lesson', '--title', 'Hidden', '--from', 'private',
               '--ref', 'notes.md:1', '--body', 'concurrent private mutations')
        self.b(r, 'item', 'add', 'proposal', '--kind', 'lesson', '--title', 'Proposal', '--from', 'app',
               '--ref', 'notes.md:2', '--body', 'concurrent suggestions', '--proposed', '--repo', 'example/repo', '--commit', 'a' * 40)
        embed = mock.Mock(side_effect=self.vectors)
        found, waiting = core.semantic_ranked(core.Instance(r), node, 'races', embedder=embed)
        self.assertEqual([x[1] for x in found], ['serialization'])
        self.assertEqual([x[1] for x in waiting], ['proposal'])
        self.assertFalse(any('private mutations' in t for t in embed.call_args[0][0]))

    def test_decisions_and_reports_are_indexed_without_following_foreign_links(self):
        r, core, inst, node = self.fixture()
        (node['dir'] / 'decisions.md').write_text('# Decisions\n## Serialization\nConcurrent changes must serialize.\n')
        report = node['dir'] / 'reports/example'
        report.mkdir(parents=True)
        (report / 'index.md').write_text('# Experiment\nConcurrent writes are serialized.\n')
        found, _ = core.semantic_ranked(inst, node, 'races', embedder=self.vectors)
        documents = [x[4]['fm'] for x in found if x[4]['fm']['kind'] == 'document']
        self.assertEqual(len(documents), 2)
        self.assertTrue(all(x['source_node'] == 'app' and x['source_ref'] for x in documents))

    def test_optional_configuration_and_linked_documents_fail_closed(self):
        r, core, inst, node = self.fixture()
        inst.local['semantic'] = False
        import os
        with mock.patch.dict(os.environ, {'BRAIN_USAGE_TESTING': '0', 'BRAIN_SEMANTIC_OFFLINE': '0'}):
            self.assertFalse(core.semantic_capability(inst)[0])
            self.assertIn('lexical', core.semantic_mode(inst))
        source = self.tmp / 'linked-note.md'
        source.write_text('# Notes\nconcurrent mutations\n')
        (node['dir'] / 'knowledge.md').symlink_to(source)
        self.assertEqual(list(core.semantic_documents(inst, node)), [])


class SemanticDocumentOwnership(Scenario):
    fixture = SemanticRecall.fixture
    marker = 'EXCLUDED_OWNER_PAYLOAD'

    def ownership_fixture(self):
        r, core, inst, node = self.fixture()
        self.b(r, 'principal', 'add', 'team')
        self.b(r, 'new', 'app/vault', '--title', 'Vault', '--owners', 'team')
        vault = r / 'nodes/app/nodes/vault'
        hidden = vault / 'reports/private'
        hidden.mkdir(parents=True)
        text = '# Private report\n## Observation\n' + self.marker + '\n'
        (hidden / 'index.md').write_text(text)
        (vault / 'knowledge.md').write_text(text)
        (vault / 'decisions.md').write_text(text)
        (node['dir'] / 'knowledge.md').write_text('# Own knowledge\nconcurrent same-node-visible\n')
        return r, core, core.Instance(r), node, vault, hidden

    def assert_owned_inputs_only(self, r, core, inst, node):
        # Older lexical-only cores must preserve the same non-disclosure invariant.
        # The semantic assertions below always execute when the feature exists.
        if not hasattr(core, 'semantic_ranked'):
            out = self.b(r, 'recall', 'app', 'concurrent', '--json')
            self.assertIn('serialization', out)
            self.assertNotIn(self.marker, out)
            self.assertFalse(list((r / '.brain/semantic').glob('*.json')))
            return
        calls = []
        def embed(texts):
            calls.extend(texts)
            return [[0, 1] if self.marker in text else [1, 0] for text in texts]
        ranked = core.semantic_ranked(inst, node, 'concurrent', embedder=embed)
        self.assertIsNotNone(ranked, 'same-node discovery must not disable semantic recall')
        self.assertFalse(any(self.marker in text for text in calls), 'excluded owner content reached the embedder')
        found, _ = ranked
        self.assertTrue(any(row[4]['fm'].get('kind') == 'document' for row in found))
        caches = list((r / '.brain/semantic').glob('*.json'))
        self.assertEqual(len(caches), 1)
        entries = json.loads(caches[0].read_text())['items']
        self.assertTrue(entries)
        self.assertNotIn([0.0, 1.0], entries.values(), 'excluded owner vector entered the cache')

    def test_parent_report_links_to_sealed_descendant_never_reach_model_or_cache(self):
        r, core, inst, node, vault, hidden = self.ownership_fixture()
        reports = node['dir'] / 'reports'
        (reports / 'regular').mkdir(parents=True)
        (reports / 'regular/index.md').write_text('# Regular report\nconcurrent same-node-visible\n')
        (reports / 'linked-directory').symlink_to(hidden, target_is_directory=True)
        (reports / 'linked-file').mkdir()
        (reports / 'linked-file/index.md').symlink_to(hidden / 'index.md')
        self.assert_owned_inputs_only(r, core, inst, node)

    def test_hard_linked_report_is_not_caller_owned(self):
        import os
        r, core, inst, node, vault, hidden = self.ownership_fixture()
        report = node['dir'] / 'reports/hard-linked-file'
        report.mkdir(parents=True)
        os.link(hidden / 'index.md', report / 'index.md')
        self.assert_owned_inputs_only(r, core, inst, node)

    def test_linked_notes_and_report_root_never_reach_model_or_cache(self):
        r, core, inst, node, vault, hidden = self.ownership_fixture()
        for name in ('knowledge.md', 'decisions.md'):
            path = node['dir'] / name
            path.unlink()
            path.symlink_to(vault / name)
        (node['dir'] / 'reports').symlink_to(vault / 'reports', target_is_directory=True)
        # A regular workbench report supplies the positive document control here.
        workbench = self.tmp / 'regular-workbench'
        (workbench / 'reports/regular').mkdir(parents=True)
        (workbench / 'reports/regular/index.md').write_text('# Regular report\nconcurrent same-node-visible\n')
        with mock.patch.object(core._owners['workbench'], 'workbench', return_value=workbench):
            self.assert_owned_inputs_only(r, core, inst, node)

    def test_workbench_links_and_foreign_regular_bindings_never_reach_model_or_cache(self):
        r, core, inst, node, vault, hidden = self.ownership_fixture()
        direct = self.tmp / 'workbench-link'
        direct.symlink_to(vault, target_is_directory=True)
        (vault / 'workspace/reports/private').mkdir(parents=True)
        (vault / 'workspace/reports/private/index.md').write_text((hidden / 'index.md').read_text())
        ancestor = self.tmp / 'workbench-parent'
        ancestor.symlink_to(vault, target_is_directory=True)
        for workbench in (direct, ancestor / 'workspace', vault):
            with self.subTest(workbench=workbench.name):
                with mock.patch.object(core._owners['workbench'], 'workbench', return_value=workbench):
                    self.assert_owned_inputs_only(r, core, inst, node)

    def test_safely_discovered_document_is_not_reopened_by_path(self):
        r, core, inst, node, vault, hidden = self.ownership_fixture()
        if not hasattr(core, 'semantic_ranked'):
            return self.assert_owned_inputs_only(r, core, inst, node)
        common = core._owners['read_fm']
        original = common.read_fm
        note = node['dir'] / 'knowledge.md'
        def swapped_read(path):
            if path == note:
                return {}, '# Swapped source\n' + self.marker
            return original(path)
        with mock.patch.object(common, 'read_fm', side_effect=swapped_read):
            self.assert_owned_inputs_only(r, core, inst, node)
