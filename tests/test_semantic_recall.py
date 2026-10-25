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
